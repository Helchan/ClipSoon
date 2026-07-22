"""Isolated Win32 clipboard monitor with a JSON-lines process protocol.

The host deliberately has no PySide dependency.  Windows may synchronously
block while rendering clipboard data owned by another process, so callers are
expected to run this module in a child process and restart it when heartbeats
stop.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, TextIO

PROTOCOL_VERSION = 1

CF_TEXT = 1
CF_BITMAP = 2
CF_METAFILEPICT = 3
CF_SYLK = 4
CF_DIF = 5
CF_TIFF = 6
CF_OEMTEXT = 7
CF_DIB = 8
CF_PALETTE = 9
CF_PENDATA = 10
CF_RIFF = 11
CF_WAVE = 12
CF_UNICODETEXT = 13
CF_ENHMETAFILE = 14
CF_HDROP = 15
CF_LOCALE = 16
CF_DIBV5 = 17

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_CLIPBOARDUPDATE = 0x031D
HWND_MESSAGE = -3
HEARTBEAT_TIMER_ID = 1
RETRY_TIMER_ID = 2

_MAX_IMAGE_BYTES = 256 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_MAX_FILE_COUNT = 10_000
_MAX_FILE_PATH_CHARS = 32_767
_SETTLE_INTERVAL_MS = 70
_INITIAL_RETRY_INTERVAL_MS = 80
_MAX_RETRY_INTERVAL_MS = 1_000
_HEARTBEAT_INTERVAL_MS = 500
_MAX_MATERIALIZATION_SECONDS = 120.0
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
ERROR_BROKEN_PIPE = 109
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF
_PRIVATE_FORMAT_MARKERS = (
    "concealed",
    "transient",
    "excludeclipboardcontentfrommonitorprocessing",
)
_STANDARD_FORMAT_NAMES = {
    CF_TEXT: "CF_TEXT",
    CF_BITMAP: "CF_BITMAP",
    CF_METAFILEPICT: "CF_METAFILEPICT",
    CF_SYLK: "CF_SYLK",
    CF_DIF: "CF_DIF",
    CF_TIFF: "CF_TIFF",
    CF_OEMTEXT: "CF_OEMTEXT",
    CF_DIB: "CF_DIB",
    CF_PALETTE: "CF_PALETTE",
    CF_PENDATA: "CF_PENDATA",
    CF_RIFF: "CF_RIFF",
    CF_WAVE: "CF_WAVE",
    CF_UNICODETEXT: "CF_UNICODETEXT",
    CF_ENHMETAFILE: "CF_ENHMETAFILE",
    CF_HDROP: "CF_HDROP",
    CF_LOCALE: "CF_LOCALE",
    CF_DIBV5: "CF_DIBV5",
}


class ClipboardHostError(RuntimeError):
    """Base error raised by the isolated clipboard host."""


class ClipboardBusyError(ClipboardHostError):
    """The clipboard is currently opened by another process."""


class ClipboardDataError(ClipboardHostError):
    """Clipboard data was malformed, unavailable, or too large."""


class NativeClipboardApi(Protocol):
    def sequence_number(self) -> int: ...

    def foreground_app_name(self) -> str: ...

    def open_clipboard(self, owner: int) -> bool: ...

    def close_clipboard(self) -> bool: ...

    def enum_formats(self) -> list[int]: ...

    def format_name(self, format_id: int) -> str: ...

    def register_format(self, name: str) -> int: ...

    def global_bytes(self, format_id: int) -> bytes: ...

    def hdrop_files(self, format_id: int) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class ClipboardSnapshot:
    sequence: int
    kind: str
    payload: Mapping[str, Any]
    source_app: str = ""

    def manifest(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL_VERSION,
            "sequence": self.sequence,
            "kind": self.kind,
            "source_app": self.source_app,
            **self.payload,
        }


@dataclass(frozen=True, slots=True)
class _ImageClipboardData:
    data: bytes
    encoding: str


class JsonLineEmitter:
    """Thread-safe JSON-lines writer used by both message and stdin threads."""

    def __init__(self, stream: TextIO, session_id: str = "") -> None:
        self._stream = stream
        self._session_id = session_id or uuid.uuid4().hex
        self._lock = threading.Lock()
        self._event_id = 0

    def emit(self, event: Mapping[str, Any]) -> None:
        with self._lock:
            self._event_id += 1
            payload = dict(event)
            payload.update(
                {
                    "protocol": PROTOCOL_VERSION,
                    "role": "clipboard",
                    "session_id": self._session_id,
                    "event_id": self._event_id,
                }
            )
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            self._stream.write(line + "\n")
            self._stream.flush()


class WindowsPipeWriter:
    """Minimal UTF-8 text writer over a Win32 inherited stdout handle."""

    def __init__(self, api: CtypesWindowsApi, handle: int | None = None) -> None:
        self.api = api
        self.handle = api.std_handle(STD_OUTPUT_HANDLE) if handle is None else handle
        if not self.handle:
            raise ClipboardHostError("stdout pipe handle is unavailable")

    def write(self, value: str) -> int:
        data = value.encode("utf-8")
        offset = 0
        while offset < len(data):
            written = wintypes.DWORD()
            chunk = data[offset:]
            if not self.api.kernel32.WriteFile(
                self.handle,
                chunk,
                len(chunk),
                ctypes.byref(written),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not written.value:
                raise ClipboardHostError("stdout pipe accepted zero bytes")
            offset += int(written.value)
        return len(value)

    def flush(self) -> None:
        return None


class WindowsPipeReader:
    """Line iterator over a Win32 inherited stdin handle."""

    def __init__(self, api: CtypesWindowsApi, handle: int | None = None) -> None:
        self.api = api
        self.handle = api.std_handle(STD_INPUT_HANDLE) if handle is None else handle
        if not self.handle:
            raise ClipboardHostError("stdin pipe handle is unavailable")
        self._buffer = bytearray()
        self._eof = False

    def __iter__(self) -> WindowsPipeReader:
        return self

    def __next__(self) -> str:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                value = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                return value.decode("utf-8")
            if self._eof:
                if not self._buffer:
                    raise StopIteration
                value = bytes(self._buffer)
                self._buffer.clear()
                return value.decode("utf-8")
            buffer = ctypes.create_string_buffer(4_096)
            count = wintypes.DWORD()
            succeeded = self.api.kernel32.ReadFile(
                self.handle,
                buffer,
                len(buffer),
                ctypes.byref(count),
                None,
            )
            if not succeeded:
                error = ctypes.get_last_error()
                if error == ERROR_BROKEN_PIPE:
                    self._eof = True
                    continue
                raise ctypes.WinError(error)
            if not count.value:
                self._eof = True
            else:
                self._buffer.extend(buffer.raw[: count.value])


class ImagePayloadStore:
    """Writes payloads and atomic manifests outside the process pipes."""

    def __init__(self, root: Path | None = None, *, session_id: str = "") -> None:
        if root is None:
            root = Path(tempfile.mkdtemp(prefix="clipsoon-clipboard-host-"))
        else:
            root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        candidate = session_id or uuid.uuid4().hex
        if len(candidate) != 32 or any(character not in "0123456789abcdef" for character in candidate.casefold()):
            raise ClipboardDataError("invalid clipboard helper session id")
        self.session_id = candidate.casefold()

    def write(self, sequence: int, data: bytes, encoding: str) -> Path:
        if not data:
            raise ClipboardDataError("image payload is empty")
        if len(data) > _MAX_IMAGE_BYTES:
            raise ClipboardDataError(f"image payload exceeds {_MAX_IMAGE_BYTES} bytes")
        suffix = {"png": ".png", "bmp": ".bmp"}.get(encoding)
        if suffix is None:
            raise ClipboardDataError(f"unsupported image encoding: {encoding}")
        stem = f"clip-{self.session_id}-{sequence}-{uuid.uuid4().hex}"
        temporary = self.root / f".{stem}.tmp"
        path = self.root / f"{stem}{suffix}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            with suppress(OSError):
                temporary.unlink()
            raise
        return path

    def write_manifest(self, snapshot: ClipboardSnapshot) -> tuple[str, int]:
        encoded = json.dumps(
            snapshot.manifest(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise ClipboardDataError(f"manifest exceeds {_MAX_MANIFEST_BYTES} bytes")
        stem = f"manifest-{self.session_id}-{snapshot.sequence}-{uuid.uuid4().hex}"
        temporary = self.root / f".{stem}.tmp"
        final = self.root / f"{stem}.json"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, final)
        except BaseException:
            with suppress(OSError):
                temporary.unlink()
            raise
        return final.name, len(encoded)


class WindowsClipboardReader:
    """Copies a clipboard snapshot while enforcing Open/Close pairing."""

    def __init__(
        self,
        api: NativeClipboardApi,
        owner: int,
        payload_store: ImagePayloadStore,
        capture_started: Callable[[int], None] = lambda _sequence: None,
        capture_materializing: Callable[[int], None] = lambda _sequence: None,
    ) -> None:
        self.api = api
        self.owner = owner
        self.payload_store = payload_store
        self.capture_started = capture_started
        self.capture_materializing = capture_materializing
        self.png_format = api.register_format("PNG")

    def read(self, sequence: int) -> ClipboardSnapshot:
        if not self.api.open_clipboard(self.owner):
            raise ClipboardBusyError("OpenClipboard failed")
        try:
            result = self._read_locked(sequence)
        finally:
            if not self.api.close_clipboard():
                raise ClipboardDataError("CloseClipboard failed")
        self.capture_materializing(sequence)
        if isinstance(result, _ImageClipboardData):
            return self._materialize_image(sequence, result)
        return result

    def _read_locked(self, sequence: int) -> ClipboardSnapshot | _ImageClipboardData:
        locked_sequence = self.api.sequence_number()
        if locked_sequence != sequence:
            raise ClipboardBusyError(f"clipboard sequence changed before snapshot: {sequence} -> {locked_sequence}")
        formats = self.api.enum_formats()
        names = [self.api.format_name(format_id) for format_id in formats]
        # Everything below can call GetClipboardData and synchronously ask the
        # owner to render delayed data.  The parent combines this marker with
        # heartbeat loss to distinguish a stuck capture from an idle host.
        self.capture_started(sequence)
        if self._is_private(formats, names):
            return ClipboardSnapshot(sequence, "ignored", {"reason": "private"})
        if CF_HDROP in formats:
            files = self.api.hdrop_files(CF_HDROP)
            if files:
                return ClipboardSnapshot(sequence, "files", {"files": files})
        image = self._read_image(formats)
        if image is not None:
            return image
        if CF_UNICODETEXT in formats:
            text = _decode_unicode_text(self.api.global_bytes(CF_UNICODETEXT))
            if text:
                return ClipboardSnapshot(sequence, "text", {"text": text})
        return ClipboardSnapshot(sequence, "unsupported", {"formats": names})

    def _is_private(self, formats: list[int], names: list[str]) -> bool:
        folded = [name.casefold() for name in names]
        if any(marker in name for marker in _PRIVATE_FORMAT_MARKERS for name in folded):
            return True
        for format_id, name in zip(formats, folded, strict=True):
            if "canincludeinclipboardhistory" not in name:
                continue
            try:
                value = self.api.global_bytes(format_id)
            except ClipboardDataError:
                return True
            return not bool(value and any(value))
        return False

    def _read_image(self, formats: list[int]) -> _ImageClipboardData | None:
        if self.png_format and self.png_format in formats:
            try:
                data = self.api.global_bytes(self.png_format)
                if data.startswith(b"\x89PNG\r\n\x1a\n"):
                    return _ImageClipboardData(data, "png")
            except ClipboardDataError:
                if CF_DIBV5 not in formats and CF_DIB not in formats:
                    raise
        dib_format = CF_DIBV5 if CF_DIBV5 in formats else CF_DIB if CF_DIB in formats else 0
        if not dib_format:
            return None
        return _ImageClipboardData(self.api.global_bytes(dib_format), "dib")

    def _materialize_image(self, sequence: int, image: _ImageClipboardData) -> ClipboardSnapshot:
        if image.encoding == "png":
            data = image.data
            encoding = "png"
            width, height = _png_dimensions(data)
        else:
            data, width, height = _dib_to_bmp(image.data)
            encoding = "bmp"
        path = self.payload_store.write(sequence, data, encoding)
        return ClipboardSnapshot(
            sequence,
            "image",
            {
                "payload_file": path.name,
                "encoding": encoding,
                "bytes": len(data),
                "width": width,
                "height": height,
            },
        )


def _decode_unicode_text(data: bytes) -> str:
    if len(data) % 2:
        data = data[:-1]
    terminator = next(
        (index for index in range(0, max(0, len(data) - 1), 2) if data[index : index + 2] == b"\0\0"),
        len(data),
    )
    try:
        return data[:terminator].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise ClipboardDataError("invalid CF_UNICODETEXT payload") from exc


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise ClipboardDataError("invalid PNG clipboard payload")
    width, height = struct.unpack_from(">II", data, 16)
    if not width or not height:
        raise ClipboardDataError("invalid PNG dimensions")
    return width, height


def _dib_to_bmp(dib: bytes) -> tuple[bytes, int, int]:
    """Add a BITMAPFILEHEADER to a CF_DIB/CF_DIBV5 byte buffer."""

    if len(dib) < 12:
        raise ClipboardDataError("DIB clipboard payload is truncated")
    header_size = struct.unpack_from("<I", dib)[0]
    if header_size == 12:
        width, height, _planes, bit_count = struct.unpack_from("<HHHH", dib, 4)
        palette_entries = 1 << bit_count if bit_count <= 8 else 0
        pixel_offset = header_size + palette_entries * 3
    elif header_size >= 40 and len(dib) >= header_size:
        width, signed_height = struct.unpack_from("<ii", dib, 4)
        bit_count = struct.unpack_from("<H", dib, 14)[0]
        compression = struct.unpack_from("<I", dib, 16)[0]
        colors_used = struct.unpack_from("<I", dib, 32)[0]
        palette_entries = colors_used or (1 << bit_count if bit_count <= 8 else 0)
        extra_masks = 0
        if header_size == 40 and compression in (3, 6):
            extra_masks = 16 if compression == 6 else 12
        pixel_offset = header_size + extra_masks + palette_entries * 4
        if header_size >= 124:
            profile_offset, profile_size = struct.unpack_from("<II", dib, 112)
            if profile_offset and profile_size:
                pixel_offset = max(pixel_offset, profile_offset + profile_size)
        height = abs(signed_height)
        width = abs(width)
    else:
        raise ClipboardDataError(f"unsupported DIB header size: {header_size}")
    if not width or not height or pixel_offset > len(dib):
        raise ClipboardDataError("invalid DIB dimensions or pixel offset")
    file_offset = 14 + pixel_offset
    file_size = 14 + len(dib)
    file_header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, file_offset)
    return file_header + dib, int(width), int(height)


class ClipboardHost:
    """Coordinates sequence de-duplication, retry timers, and protocol events."""

    def __init__(
        self,
        api: NativeClipboardApi,
        reader: WindowsClipboardReader,
        emit: Callable[[Mapping[str, Any]], None],
        *,
        schedule_capture: Callable[[int | None], None] = lambda _delay: None,
        after_sequence: int | None = None,
        capture_finished: Callable[[int], None] = lambda _sequence: None,
    ) -> None:
        self.api = api
        self.reader = reader
        self.emit = emit
        self.schedule_capture = schedule_capture
        self.capture_finished = capture_finished
        current_sequence = api.sequence_number()
        self.last_sequence = current_sequence if after_sequence is None else max(0, after_sequence)
        self.pending_sequence: int | None = None
        self.pending_source = ""
        self._retry_attempt = 0

    def clipboard_changed(self) -> None:
        sequence = self.api.sequence_number()
        if not sequence or sequence == self.last_sequence:
            return
        if sequence != self.pending_sequence:
            self._bind_pending(sequence)
        self.schedule_capture(_SETTLE_INTERVAL_MS)

    def retry_pending(self) -> None:
        if self.pending_sequence is not None:
            self._attempt_pending()

    def heartbeat(self) -> None:
        self.emit(
            {
                "type": "heartbeat",
                "sequence": self.last_sequence,
                "time_ns": time.time_ns(),
            }
        )

    def _attempt_pending(self) -> None:
        attempted_sequence = self.pending_sequence
        try:
            self._attempt_pending_once()
        finally:
            if attempted_sequence is not None:
                self.capture_finished(attempted_sequence)

    def _attempt_pending_once(self) -> None:
        sequence = self.api.sequence_number()
        if not sequence:
            if self.pending_sequence is not None:
                self.schedule_capture(_INITIAL_RETRY_INTERVAL_MS)
            return
        if sequence != self.pending_sequence:
            self._bind_pending(sequence)
            self.schedule_capture(_SETTLE_INTERVAL_MS)
            return
        try:
            snapshot = self.reader.read(sequence)
        except ClipboardBusyError as exc:
            delay = min(
                _MAX_RETRY_INTERVAL_MS,
                _INITIAL_RETRY_INTERVAL_MS * (2**self._retry_attempt),
            )
            self._retry_attempt = min(self._retry_attempt + 1, 10)
            self.schedule_capture(delay)
            self.emit(
                {
                    "type": "error",
                    "stage": "open",
                    "message": str(exc),
                    "sequence": sequence,
                    "retrying": True,
                }
            )
            return
        except Exception as exc:
            self.pending_sequence = None
            self.pending_source = ""
            self.schedule_capture(None)
            self.emit(
                {
                    "type": "error",
                    "stage": "read",
                    "message": str(exc),
                    "sequence": sequence,
                    "retrying": False,
                }
            )
            return
        snapshot = replace(snapshot, source_app=self.pending_source)
        try:
            manifest, manifest_bytes = self.reader.payload_store.write_manifest(snapshot)
        except Exception as exc:
            self.pending_sequence = None
            self.pending_source = ""
            self.schedule_capture(None)
            self.emit(
                {
                    "type": "error",
                    "stage": "manifest",
                    "message": str(exc),
                    "sequence": sequence,
                    "retrying": False,
                }
            )
            return
        self.last_sequence = sequence
        self.pending_sequence = None
        self.pending_source = ""
        self.schedule_capture(None)
        self.emit(
            {
                "type": "clipboard",
                "sequence": sequence,
                "kind": snapshot.kind,
                "manifest": manifest,
                "manifest_bytes": manifest_bytes,
            }
        )

    def _bind_pending(self, sequence: int) -> None:
        self.pending_sequence = sequence
        self._retry_attempt = 0
        try:
            self.pending_source = self.api.foreground_app_name()
        except Exception:
            self.pending_source = ""


_LRESULT = ctypes.c_ssize_t
_WNDPROC_TYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    _LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = (
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC_TYPE),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    )


class _POINT(ctypes.Structure):
    _fields_ = (("x", ctypes.c_int32), ("y", ctypes.c_int32))


class _MSG(ctypes.Structure):
    _fields_ = (
        ("hwnd", wintypes.HWND),
        ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt", _POINT),
        ("lPrivate", ctypes.c_uint32),
    )


class CtypesWindowsApi:
    """Small typed ctypes facade; constructed only on Windows."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows clipboard host is only available on Windows")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.ole32 = ctypes.OleDLL("ole32")
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self.user32.GetClipboardSequenceNumber.argtypes = ()
        self.user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        self.user32.GetForegroundWindow.argtypes = ()
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.OpenClipboard.argtypes = (wintypes.HWND,)
        self.user32.OpenClipboard.restype = wintypes.BOOL
        self.user32.CloseClipboard.argtypes = ()
        self.user32.CloseClipboard.restype = wintypes.BOOL
        self.user32.EnumClipboardFormats.argtypes = (wintypes.UINT,)
        self.user32.EnumClipboardFormats.restype = wintypes.UINT
        self.user32.GetClipboardFormatNameW.argtypes = (wintypes.UINT, wintypes.LPWSTR, ctypes.c_int)
        self.user32.GetClipboardFormatNameW.restype = ctypes.c_int
        self.user32.RegisterClipboardFormatW.argtypes = (wintypes.LPCWSTR,)
        self.user32.RegisterClipboardFormatW.restype = wintypes.UINT
        self.user32.GetClipboardData.argtypes = (wintypes.UINT,)
        self.user32.GetClipboardData.restype = wintypes.HANDLE
        self.user32.AddClipboardFormatListener.argtypes = (wintypes.HWND,)
        self.user32.AddClipboardFormatListener.restype = wintypes.BOOL
        self.user32.RemoveClipboardFormatListener.argtypes = (wintypes.HWND,)
        self.user32.RemoveClipboardFormatListener.restype = wintypes.BOOL
        self.user32.SetTimer.argtypes = (wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID)
        self.user32.SetTimer.restype = ctypes.c_size_t
        self.user32.KillTimer.argtypes = (wintypes.HWND, ctypes.c_size_t)
        self.user32.KillTimer.restype = wintypes.BOOL
        self.user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.DestroyWindow.argtypes = (wintypes.HWND,)
        self.user32.DestroyWindow.restype = wintypes.BOOL
        self.user32.PostQuitMessage.argtypes = (ctypes.c_int,)
        self.user32.PostQuitMessage.restype = None
        self.user32.DefWindowProcW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self.user32.DefWindowProcW.restype = _LRESULT
        self.user32.GetMessageW.argtypes = (ctypes.POINTER(_MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        self.user32.GetMessageW.restype = wintypes.BOOL
        self.user32.TranslateMessage.argtypes = (ctypes.POINTER(_MSG),)
        self.user32.TranslateMessage.restype = wintypes.BOOL
        self.user32.DispatchMessageW.argtypes = (ctypes.POINTER(_MSG),)
        self.user32.DispatchMessageW.restype = _LRESULT
        self.kernel32.GlobalSize.argtypes = (wintypes.HGLOBAL,)
        self.kernel32.GlobalSize.restype = ctypes.c_size_t
        self.kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
        self.kernel32.GlobalLock.restype = wintypes.LPVOID
        self.kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
        self.kernel32.GlobalUnlock.restype = wintypes.BOOL
        self.kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
        self.kernel32.GetStdHandle.restype = wintypes.HANDLE
        self.kernel32.WriteFile.argtypes = (
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            wintypes.LPDWORD,
            wintypes.LPVOID,
        )
        self.kernel32.WriteFile.restype = wintypes.BOOL
        self.kernel32.ReadFile.argtypes = (
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPDWORD,
            wintypes.LPVOID,
        )
        self.kernel32.ReadFile.restype = wintypes.BOOL
        self.kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.shell32.DragQueryFileW.argtypes = (
            wintypes.HANDLE,
            wintypes.UINT,
            wintypes.LPWSTR,
            wintypes.UINT,
        )
        self.shell32.DragQueryFileW.restype = wintypes.UINT
        self.ole32.OleInitialize.argtypes = (wintypes.LPVOID,)
        self.ole32.OleInitialize.restype = ctypes.c_long
        self.ole32.OleUninitialize.argtypes = ()
        self.ole32.OleUninitialize.restype = None

    def sequence_number(self) -> int:
        return int(self.user32.GetClipboardSequenceNumber())

    def foreground_app_name(self) -> str:
        window = self.user32.GetForegroundWindow()
        if not window:
            return ""
        length = min(max(0, int(self.user32.GetWindowTextLengthW(window))), 4_095)
        buffer = ctypes.create_unicode_buffer(length + 1)
        copied = int(self.user32.GetWindowTextW(window, buffer, len(buffer)))
        return buffer.value[:copied]

    def std_handle(self, identifier: int) -> int:
        handle = self.kernel32.GetStdHandle(identifier & 0xFFFFFFFF)
        value = int(handle or 0)
        invalid = ctypes.c_void_p(-1).value
        return 0 if value == invalid else value

    def wait_for_process_exit(self, process_id: int) -> None:
        handle = self.kernel32.OpenProcess(SYNCHRONIZE, False, process_id)
        if not handle:
            return
        try:
            if self.kernel32.WaitForSingleObject(handle, INFINITE) != WAIT_OBJECT_0:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel32.CloseHandle(handle)

    def open_clipboard(self, owner: int) -> bool:
        return bool(self.user32.OpenClipboard(wintypes.HWND(owner)))

    def close_clipboard(self) -> bool:
        return bool(self.user32.CloseClipboard())

    def enum_formats(self) -> list[int]:
        formats: list[int] = []
        format_id = 0
        while True:
            format_id = int(self.user32.EnumClipboardFormats(format_id))
            if not format_id:
                break
            formats.append(format_id)
        return formats

    def format_name(self, format_id: int) -> str:
        if format_id in _STANDARD_FORMAT_NAMES:
            return _STANDARD_FORMAT_NAMES[format_id]
        buffer = ctypes.create_unicode_buffer(256)
        length = self.user32.GetClipboardFormatNameW(format_id, buffer, len(buffer))
        return buffer.value[:length] if length else f"format/{format_id}"

    def register_format(self, name: str) -> int:
        return int(self.user32.RegisterClipboardFormatW(name))

    def global_bytes(self, format_id: int) -> bytes:
        handle = self.user32.GetClipboardData(format_id)
        if not handle:
            raise ClipboardDataError(f"GetClipboardData({format_id}) failed")
        size = int(self.kernel32.GlobalSize(handle))
        if size <= 0:
            raise ClipboardDataError(f"GlobalSize({format_id}) returned {size}")
        if size > _MAX_IMAGE_BYTES:
            raise ClipboardDataError(f"clipboard payload exceeds {_MAX_IMAGE_BYTES} bytes")
        pointer = self.kernel32.GlobalLock(handle)
        if not pointer:
            raise ClipboardDataError(f"GlobalLock({format_id}) failed")
        try:
            return ctypes.string_at(pointer, size)
        finally:
            self.kernel32.GlobalUnlock(handle)

    def hdrop_files(self, format_id: int) -> list[str]:
        handle = self.user32.GetClipboardData(format_id)
        if not handle:
            raise ClipboardDataError("GetClipboardData(CF_HDROP) failed")
        count = int(self.shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0))
        if count > _MAX_FILE_COUNT:
            raise ClipboardDataError(f"CF_HDROP contains more than {_MAX_FILE_COUNT} files")
        files: list[str] = []
        for index in range(count):
            length = int(self.shell32.DragQueryFileW(handle, index, None, 0))
            if length > _MAX_FILE_PATH_CHARS:
                raise ClipboardDataError("CF_HDROP path is too long")
            buffer = ctypes.create_unicode_buffer(length + 1)
            copied = int(self.shell32.DragQueryFileW(handle, index, buffer, len(buffer)))
            if copied:
                files.append(buffer.value)
        return files


class CapturePhaseTracker:
    """Allow heartbeats after native clipboard calls have safely returned."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        materialization_timeout: float = _MAX_MATERIALIZATION_SECONDS,
    ) -> None:
        self._clock = clock
        self._materialization_timeout = max(1.0, materialization_timeout)
        self._lock = threading.Lock()
        self._phase = "idle"
        self._sequence: int | None = None
        self._materializing_at: float | None = None

    def native_started(self, sequence: int) -> None:
        with self._lock:
            self._phase = "native"
            self._sequence = sequence
            self._materializing_at = None

    def materializing(self, sequence: int) -> None:
        with self._lock:
            if self._sequence != sequence:
                return
            self._phase = "materializing"
            self._materializing_at = self._clock()

    def finished(self, sequence: int) -> None:
        with self._lock:
            if self._sequence != sequence:
                return
            self._phase = "idle"
            self._sequence = None
            self._materializing_at = None

    def should_emit_background_heartbeat(self) -> bool:
        with self._lock:
            if self._phase != "materializing" or self._materializing_at is None:
                return False
            return self._clock() - self._materializing_at <= self._materialization_timeout


class WindowsMessageLoop:
    """Message-only window receiving WM_CLIPBOARDUPDATE and timer messages."""

    def __init__(
        self,
        api: CtypesWindowsApi,
        emitter: JsonLineEmitter,
        payload_dir: Path | None,
        after_sequence: int | None,
        parent_pid: int | None,
        session_id: str = "",
        hard_exit: Callable[[int], None] = os._exit,
    ) -> None:
        self.api = api
        self.emitter = emitter
        self.payload_store = ImagePayloadStore(payload_dir, session_id=session_id)
        self.after_sequence = after_sequence
        self.parent_pid = parent_pid
        self.hard_exit = hard_exit
        self.capture_phase = CapturePhaseTracker()
        self.hwnd = 0
        self.host: ClipboardHost | None = None
        self._wndproc = _WNDPROC_TYPE(self._window_proc)
        self._listener_added = False
        self._heartbeat_stop = threading.Event()

    def run(self, stdin: TextIO) -> int:
        initialized = self.api.ole32.OleInitialize(None)
        if initialized not in (0, 1):
            raise ClipboardHostError(f"OleInitialize failed: 0x{initialized & 0xFFFFFFFF:08x}")
        try:
            self._create_window()
            reader = WindowsClipboardReader(
                self.api,
                self.hwnd,
                self.payload_store,
                capture_started=self._capture_started,
                capture_materializing=self._capture_materializing,
            )
            self.host = ClipboardHost(
                self.api,
                reader,
                self.emitter.emit,
                schedule_capture=self._schedule_capture,
                after_sequence=self.after_sequence,
                capture_finished=self.capture_phase.finished,
            )
            self._start_heartbeat()
            threading.Thread(
                target=self._heartbeat_while_materializing,
                name="ClipSoon-clipboard-materialization-heartbeat",
                daemon=True,
            ).start()
            threading.Thread(target=self._watch_stdin, args=(stdin,), daemon=True).start()
            if self.parent_pid is not None:
                threading.Thread(target=self._watch_parent, daemon=True).start()
            self.emitter.emit(
                {
                    "type": "ready",
                    "protocol": PROTOCOL_VERSION,
                    "pid": os.getpid(),
                    "sequence": self.host.last_sequence,
                    "payload_dir": str(self.payload_store.root),
                    "parent_pid": self.parent_pid,
                }
            )
            # A restarted worker receives only the last successfully consumed
            # sequence.  If Windows already moved on, replay the current value
            # immediately instead of waiting for another update notification.
            if self.after_sequence is not None:
                self.host.clipboard_changed()
            return self._message_loop()
        finally:
            self._heartbeat_stop.set()
            self.api.ole32.OleUninitialize()

    def _capture_started(self, sequence: int) -> None:
        self.capture_phase.native_started(sequence)
        self.emitter.emit(
            {
                "type": "capture_started",
                "sequence": sequence,
                "time_ns": time.time_ns(),
            }
        )

    def _capture_materializing(self, sequence: int) -> None:
        self.capture_phase.materializing(sequence)
        self.emitter.emit(
            {
                "type": "capture_materializing",
                "sequence": sequence,
                "time_ns": time.time_ns(),
            }
        )

    def _heartbeat_while_materializing(self) -> None:
        while not self._heartbeat_stop.wait(_HEARTBEAT_INTERVAL_MS / 1_000):
            host = self.host
            if host is None or not self.capture_phase.should_emit_background_heartbeat():
                continue
            try:
                host.heartbeat()
            except BaseException:
                self.api.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
                return

    def _create_window(self) -> None:
        user32 = self.api.user32
        kernel32 = self.api.kernel32
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = (ctypes.POINTER(_WNDCLASSW),)
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = (
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        )
        user32.CreateWindowExW.restype = wintypes.HWND
        class_name = f"ClipSoonClipboardHost-{os.getpid()}"
        instance = kernel32.GetModuleHandleW(None)
        window_class = _WNDCLASSW(
            0,
            self._wndproc,
            0,
            0,
            instance,
            None,
            None,
            None,
            None,
            class_name,
        )
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            class_name,
            0,
            0,
            0,
            0,
            0,
            wintypes.HWND(HWND_MESSAGE),
            None,
            instance,
            None,
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = int(hwnd)
        if not user32.AddClipboardFormatListener(hwnd):
            raise ctypes.WinError(ctypes.get_last_error())
        self._listener_added = True

    def _start_heartbeat(self) -> None:
        if not self.api.user32.SetTimer(self.hwnd, HEARTBEAT_TIMER_ID, _HEARTBEAT_INTERVAL_MS, None):
            raise ctypes.WinError(ctypes.get_last_error())

    def _schedule_capture(self, delay_ms: int | None) -> None:
        if delay_ms is None:
            self.api.user32.KillTimer(self.hwnd, RETRY_TIMER_ID)
        elif not self.api.user32.SetTimer(self.hwnd, RETRY_TIMER_ID, max(1, delay_ms), None):
            raise ctypes.WinError(ctypes.get_last_error())

    def _watch_stdin(self, stdin: TextIO) -> None:
        try:
            for line in stdin:
                value = line.strip()
                if not value:
                    continue
                shutdown = value.casefold() == "shutdown"
                if not shutdown:
                    try:
                        shutdown = json.loads(value).get("type") == "shutdown"
                    except (AttributeError, json.JSONDecodeError):
                        shutdown = False
                if shutdown:
                    self.api.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
                    return
        except BaseException:
            pass
        # A broken control pipe means the owner can no longer supervise a
        # blocking clipboard RPC.  Bypass the message thread unconditionally.
        self.hard_exit(0)

    def _watch_parent(self) -> None:
        assert self.parent_pid is not None
        try:
            self.api.wait_for_process_exit(self.parent_pid)
        finally:
            self.hard_exit(0)

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        try:
            if message == WM_CLIPBOARDUPDATE and self.host is not None:
                self.host.clipboard_changed()
                return 0
            if message == WM_TIMER and self.host is not None:
                if wparam == HEARTBEAT_TIMER_ID:
                    self.host.heartbeat()
                    return 0
                if wparam == RETRY_TIMER_ID:
                    self.api.user32.KillTimer(self.hwnd, RETRY_TIMER_ID)
                    self.host.retry_pending()
                    return 0
            if message == WM_CLOSE:
                self.api.user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                if self._listener_added:
                    self.api.user32.RemoveClipboardFormatListener(hwnd)
                    self._listener_added = False
                self.api.user32.PostQuitMessage(0)
                return 0
        except BaseException as exc:
            try:
                self.emitter.emit(
                    {
                        "type": "error",
                        "stage": "window-proc",
                        "message": str(exc),
                        "fatal": True,
                    }
                )
                if message not in (WM_CLOSE, WM_DESTROY):
                    self.api.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except BaseException:
                self.hard_exit(1)
        return int(self.api.user32.DefWindowProcW(hwnd, message, wparam, lparam))

    def _message_loop(self) -> int:
        message = _MSG()
        while True:
            result = int(self.api.user32.GetMessageW(ctypes.byref(message), None, 0, 0))
            if result == 0:
                return 0
            if result == -1:
                raise ctypes.WinError(ctypes.get_last_error())
            self.api.user32.TranslateMessage(ctypes.byref(message))
            self.api.user32.DispatchMessageW(ctypes.byref(message))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ClipSoon isolated Windows clipboard host")
    parser.add_argument("--windows-helper", choices=("clipboard",), help=argparse.SUPPRESS)
    parser.add_argument("--ipc-dir", dest="payload_dir", type=Path, help="directory for manifests and payloads")
    parser.add_argument("--payload-dir", dest="payload_dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--after-sequence", type=int, help="last sequence successfully consumed by the parent")
    parser.add_argument("--session-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_args(argv)
    if sys.platform != "win32":
        if sys.stdout is not None:
            JsonLineEmitter(sys.stdout, arguments.session_id).emit(
                {
                    "type": "error",
                    "stage": "startup",
                    "message": "Windows clipboard host is only available on Windows",
                    "fatal": True,
                }
            )
        return 2
    emitter: JsonLineEmitter | None = None
    try:
        api = CtypesWindowsApi()
        output = sys.stdout if sys.stdout is not None else WindowsPipeWriter(api)
        stdin = sys.stdin if sys.stdin is not None else WindowsPipeReader(api)
        emitter = JsonLineEmitter(output, arguments.session_id)
        return WindowsMessageLoop(
            api,
            emitter,
            arguments.payload_dir,
            arguments.after_sequence,
            arguments.parent_pid,
            arguments.session_id,
        ).run(stdin)
    except BaseException as exc:
        if emitter is not None:
            emitter.emit(
                {
                    "type": "error",
                    "stage": "startup",
                    "message": str(exc),
                    "fatal": True,
                }
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
