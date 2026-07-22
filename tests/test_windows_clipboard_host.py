from __future__ import annotations

import ctypes
import io
import json
import struct
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipsoon.windows_clipboard_host import (
    CF_DIB,
    CF_HDROP,
    CF_UNICODETEXT,
    CapturePhaseTracker,
    ClipboardBusyError,
    ClipboardDataError,
    ClipboardHost,
    ImagePayloadStore,
    JsonLineEmitter,
    WindowsClipboardReader,
    WindowsMessageLoop,
    WindowsPipeReader,
    WindowsPipeWriter,
    _dib_to_bmp,
    _parse_args,
)


class FakeClipboardApi:
    def __init__(self, sequence: int = 1) -> None:
        self.sequence = sequence
        self.png_format = 49_001
        self.formats: list[int] = []
        self.names: dict[int, str] = {}
        self.payloads: dict[int, bytes] = {}
        self.files: list[str] = []
        self.open_results: list[bool] = []
        self.close_result = True
        self.opened = False
        self.open_calls = 0
        self.close_calls = 0
        self.calls: list[str] = []
        self.source_app = "Editor"

    def sequence_number(self) -> int:
        return self.sequence

    def foreground_app_name(self) -> str:
        self.calls.append("source")
        return self.source_app

    def open_clipboard(self, _owner: int) -> bool:
        self.open_calls += 1
        succeeded = self.open_results.pop(0) if self.open_results else True
        self.opened = succeeded
        self.calls.append("open")
        return succeeded

    def close_clipboard(self) -> bool:
        assert self.opened
        self.opened = False
        self.close_calls += 1
        self.calls.append("close")
        return self.close_result

    def enum_formats(self) -> list[int]:
        assert self.opened
        self.calls.append("formats")
        return list(self.formats)

    def format_name(self, format_id: int) -> str:
        return self.names.get(format_id, f"format/{format_id}")

    def register_format(self, name: str) -> int:
        assert name == "PNG"
        return self.png_format

    def global_bytes(self, format_id: int) -> bytes:
        assert self.opened
        self.calls.append(f"data:{format_id}")
        return self.payloads[format_id]

    def hdrop_files(self, format_id: int) -> list[str]:
        assert self.opened and format_id == CF_HDROP
        self.calls.append("files")
        return list(self.files)


def make_reader(
    tmp_path: Path,
    api: FakeClipboardApi,
    started: list[int] | None = None,
    materializing: list[int] | None = None,
) -> WindowsClipboardReader:
    return WindowsClipboardReader(
        api,
        owner=123,
        payload_store=ImagePayloadStore(tmp_path),
        capture_started=(started.append if started is not None else lambda _sequence: None),
        capture_materializing=(
            materializing.append if materializing is not None else lambda _sequence: None
        ),
    )


def test_text_snapshot_marks_capture_before_data_and_closes(tmp_path: Path) -> None:
    api = FakeClipboardApi(sequence=7)
    api.formats = [CF_UNICODETEXT]
    api.payloads[CF_UNICODETEXT] = "hello 世界\0ignored".encode("utf-16-le")
    started: list[int] = []
    materializing: list[int] = []
    reader = make_reader(tmp_path, api, started, materializing)

    snapshot = reader.read(7)

    assert snapshot.kind == "text"
    assert snapshot.payload == {"text": "hello 世界"}
    assert started == [7]
    assert materializing == [7]
    assert api.calls == ["open", "formats", f"data:{CF_UNICODETEXT}", "close"]
    assert api.open_calls == api.close_calls == 1


def test_capture_started_precedes_any_get_clipboard_data(tmp_path: Path) -> None:
    api = FakeClipboardApi()
    api.formats = [CF_UNICODETEXT]
    api.payloads[CF_UNICODETEXT] = "value\0".encode("utf-16-le")
    timeline: list[str] = []
    read_bytes = api.global_bytes

    def global_bytes(format_id: int) -> bytes:
        timeline.append("data")
        return read_bytes(format_id)

    api.global_bytes = global_bytes  # type: ignore[method-assign]
    reader = WindowsClipboardReader(
        api,
        123,
        ImagePayloadStore(tmp_path),
        capture_started=lambda _sequence: timeline.append("started"),
        capture_materializing=lambda _sequence: timeline.append("materializing"),
    )

    reader.read(1)

    assert timeline == ["started", "data", "materializing"]


def test_materialization_heartbeat_starts_only_after_clipboard_is_closed() -> None:
    now = [10.0]
    phase = CapturePhaseTracker(clock=lambda: now[0], materialization_timeout=3.0)

    phase.native_started(7)
    assert not phase.should_emit_background_heartbeat()
    phase.materializing(7)
    assert phase.should_emit_background_heartbeat()
    now[0] = 13.1
    assert not phase.should_emit_background_heartbeat()
    phase.finished(7)
    assert not phase.should_emit_background_heartbeat()


def test_open_failure_never_closes_or_touches_data(tmp_path: Path) -> None:
    api = FakeClipboardApi()
    api.open_results = [False]
    reader = make_reader(tmp_path, api)

    with pytest.raises(ClipboardBusyError):
        reader.read(1)

    assert api.open_calls == 1
    assert api.close_calls == 0
    assert api.calls == ["open"]


def test_open_success_always_closes_on_read_error(tmp_path: Path) -> None:
    api = FakeClipboardApi()
    api.formats = [CF_UNICODETEXT]
    reader = make_reader(tmp_path, api)

    with pytest.raises(KeyError):
        reader.read(1)

    assert api.open_calls == api.close_calls == 1
    assert not api.opened


def test_sequence_change_after_open_is_retried_without_mislabelling(tmp_path: Path) -> None:
    api = FakeClipboardApi(sequence=2)
    api.formats = [CF_UNICODETEXT]
    api.payloads[CF_UNICODETEXT] = "new\0".encode("utf-16-le")
    reader = make_reader(tmp_path, api)

    with pytest.raises(ClipboardBusyError, match="1 -> 2"):
        reader.read(1)

    assert api.close_calls == 1
    assert f"data:{CF_UNICODETEXT}" not in api.calls


def test_close_failure_is_reported(tmp_path: Path) -> None:
    api = FakeClipboardApi()
    api.close_result = False
    reader = make_reader(tmp_path, api)

    with pytest.raises(ClipboardDataError, match="CloseClipboard"):
        reader.read(1)


def test_hdrop_has_precedence_over_image_and_text(tmp_path: Path) -> None:
    api = FakeClipboardApi()
    api.formats = [CF_UNICODETEXT, api.png_format, CF_HDROP]
    api.files = [r"C:\work\a.txt", r"D:\b.txt"]
    api.payloads[CF_UNICODETEXT] = "fallback\0".encode("utf-16-le")
    api.payloads[api.png_format] = _png(2, 3)

    snapshot = make_reader(tmp_path, api).read(1)

    assert snapshot.kind == "files"
    assert snapshot.payload["files"] == api.files
    assert "files" in api.calls
    assert not any(call.startswith("data:") for call in api.calls)


def test_png_payload_is_private_file_and_manifest_is_atomic(tmp_path: Path) -> None:
    api = FakeClipboardApi(sequence=22)
    api.formats = [api.png_format]
    api.payloads[api.png_format] = _png(5, 9)
    session_id = "a" * 32
    store = ImagePayloadStore(tmp_path, session_id=session_id)
    write_payload = store.write

    def write_after_close(sequence: int, data: bytes, encoding: str) -> Path:
        assert not api.opened
        return write_payload(sequence, data, encoding)

    store.write = write_after_close  # type: ignore[method-assign]
    reader = WindowsClipboardReader(api, 123, store)

    snapshot = reader.read(22)
    manifest_name, manifest_bytes = store.write_manifest(snapshot)

    assert snapshot.kind == "image"
    assert snapshot.payload["encoding"] == "png"
    assert (snapshot.payload["width"], snapshot.payload["height"]) == (5, 9)
    payload_path = tmp_path / snapshot.payload["payload_file"]
    assert payload_path.read_bytes() == api.payloads[api.png_format]
    manifest_path = tmp_path / manifest_name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["payload_file"] == payload_path.name
    assert manifest["sequence"] == 22
    assert payload_path.name.startswith(f"clip-{session_id}-22-")
    assert manifest_path.name.startswith(f"manifest-{session_id}-22-")
    assert manifest_bytes == manifest_path.stat().st_size
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))


def test_dib_is_wrapped_as_loadable_bmp_payload(tmp_path: Path) -> None:
    api = FakeClipboardApi(sequence=3)
    api.formats = [CF_DIB]
    dib = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0) + b"\x00\x00\xff\x00"
    api.payloads[CF_DIB] = dib

    snapshot = make_reader(tmp_path, api).read(3)

    payload = (tmp_path / snapshot.payload["payload_file"]).read_bytes()
    assert snapshot.kind == "image"
    assert snapshot.payload["encoding"] == "bmp"
    assert payload.startswith(b"BM")
    assert struct.unpack_from("<I", payload, 10)[0] == 54
    assert payload[14:] == dib


def test_private_marker_suppresses_payload_read(tmp_path: Path) -> None:
    api = FakeClipboardApi()
    private_format = 50_001
    api.formats = [private_format, CF_UNICODETEXT]
    api.names[private_format] = "ExcludeClipboardContentFromMonitorProcessing"
    api.payloads[CF_UNICODETEXT] = "secret\0".encode("utf-16-le")

    snapshot = make_reader(tmp_path, api).read(1)

    assert snapshot.kind == "ignored"
    assert snapshot.payload == {"reason": "private"}
    assert not any(call.startswith("data:") for call in api.calls)


def test_host_retries_busy_current_sequence_and_emits_short_manifest_event(tmp_path: Path) -> None:
    api = FakeClipboardApi(sequence=10)
    api.formats = [CF_UNICODETEXT]
    api.payloads[CF_UNICODETEXT] = ("large-ish text " * 100 + "\0").encode("utf-16-le")
    api.open_results = [False, True]
    events: list[dict[str, object]] = []
    capture_delays: list[int | None] = []
    reader = make_reader(tmp_path, api)
    host = ClipboardHost(
        api,
        reader,
        events.append,
        schedule_capture=capture_delays.append,
        after_sequence=9,
    )

    host.clipboard_changed()

    assert host.last_sequence == 9
    assert host.pending_sequence == 10
    assert api.open_calls == 0
    assert capture_delays == [70]

    host.retry_pending()

    assert events[-1]["stage"] == "open"
    assert capture_delays[-1] == 80

    api.source_app = "Next window"
    host.retry_pending()

    assert host.last_sequence == 10
    assert host.pending_sequence is None
    event = events[-1]
    assert event["type"] == "clipboard"
    assert event["sequence"] == 10
    assert set(event) == {"type", "sequence", "kind", "manifest", "manifest_bytes"}
    manifest = json.loads((tmp_path / str(event["manifest"])).read_text(encoding="utf-8"))
    assert manifest["text"].startswith("large-ish text")
    assert manifest["source_app"] == "Editor"
    before = len(events)
    host.clipboard_changed()
    assert len(events) == before


def test_host_baselines_current_sequence_without_after_sequence(tmp_path: Path) -> None:
    api = FakeClipboardApi(sequence=41)
    api.formats = [CF_UNICODETEXT]
    api.payloads[CF_UNICODETEXT] = "existing\0".encode("utf-16-le")
    events: list[dict[str, object]] = []
    host = ClipboardHost(api, make_reader(tmp_path, api), events.append)

    host.clipboard_changed()

    assert events == []
    assert api.open_calls == 0


def test_busy_retries_use_exponential_delays_and_new_sequence_resets_settle(tmp_path: Path) -> None:
    api = FakeClipboardApi(sequence=5)
    api.formats = [CF_UNICODETEXT]
    api.payloads[CF_UNICODETEXT] = "eventual\0".encode("utf-16-le")
    api.open_results = [False, False, False]
    delays: list[int | None] = []
    host = ClipboardHost(
        api,
        make_reader(tmp_path, api),
        lambda _event: None,
        schedule_capture=delays.append,
        after_sequence=4,
    )

    host.clipboard_changed()
    for expected in (80, 160, 320):
        host.retry_pending()
        assert delays[-1] == expected

    api.sequence = 6
    api.source_app = "New source"
    host.clipboard_changed()

    assert delays[-1] == 70
    assert host.pending_sequence == 6
    assert host.pending_source == "New source"


def test_json_lines_are_unicode_flushed_and_sequenced() -> None:
    stream = io.StringIO()
    emitter = JsonLineEmitter(stream, "session-1")
    emitter.emit({"type": "ready", "message": "剪贴板"})
    emitter.emit({"type": "heartbeat"})

    ready, heartbeat = (json.loads(line) for line in stream.getvalue().splitlines())
    assert ready == {
        "type": "ready",
        "message": "剪贴板",
        "protocol": 1,
        "role": "clipboard",
        "session_id": "session-1",
        "event_id": 1,
    }
    assert heartbeat["event_id"] == 2
    assert stream.getvalue().endswith("\n")


def test_windows_pipe_fallback_reads_and_writes_utf8_lines() -> None:
    kernel = FakePipeKernel([b"{\"type\":\"shutdown\"}\n"])
    api = SimpleNamespace(kernel32=kernel, std_handle=lambda _identifier: 77)
    writer = WindowsPipeWriter(api)
    reader = WindowsPipeReader(api)

    writer.write("剪贴板\n")

    assert b"".join(kernel.writes).decode() == "剪贴板\n"
    assert list(reader) == ['{"type":"shutdown"}\n']


def test_control_pipe_eof_hard_exits_but_explicit_shutdown_posts_close() -> None:
    posts: list[int] = []
    exits: list[int] = []
    loop = object.__new__(WindowsMessageLoop)
    loop.hwnd = 12
    loop.api = SimpleNamespace(
        user32=SimpleNamespace(PostMessageW=lambda _hwnd, message, _wparam, _lparam: posts.append(message))
    )
    loop.hard_exit = exits.append

    loop._watch_stdin(iter([]))
    assert exits == [0]
    assert posts == []

    exits.clear()
    loop._watch_stdin(iter(['{"type":"shutdown"}\n']))
    assert exits == []
    assert len(posts) == 1


def test_parent_exit_watcher_hard_exits() -> None:
    waited: list[int] = []
    exits: list[int] = []
    loop = object.__new__(WindowsMessageLoop)
    loop.parent_pid = 42
    loop.api = SimpleNamespace(wait_for_process_exit=waited.append)
    loop.hard_exit = exits.append

    loop._watch_parent()

    assert waited == [42]
    assert exits == [0]


def test_frozen_helper_arguments_include_replay_cursor(tmp_path: Path) -> None:
    arguments = _parse_args(
        [
            "--windows-helper=clipboard",
            "--ipc-dir",
            str(tmp_path),
            "--after-sequence",
            "123",
            "--session-id",
            "nonce",
            "--parent-pid",
            "456",
        ]
    )

    assert arguments.windows_helper == "clipboard"
    assert arguments.payload_dir == tmp_path
    assert arguments.after_sequence == 123
    assert arguments.session_id == "nonce"
    assert arguments.parent_pid == 456


def test_malformed_dib_is_rejected() -> None:
    with pytest.raises(ClipboardDataError):
        _dib_to_bmp(b"short")


def _png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height)


class FakePipeKernel:
    def __init__(self, reads: list[bytes]) -> None:
        self.reads = list(reads)
        self.writes: list[bytes] = []

    def WriteFile(self, _handle, data, length, written, _overlapped) -> bool:
        self.writes.append(bytes(data[:length]))
        ctypes.cast(written, ctypes.POINTER(wintypes.DWORD)).contents.value = length
        return True

    def ReadFile(self, _handle, buffer, length, count, _overlapped) -> bool:
        data = self.reads.pop(0) if self.reads else b""
        data = data[:length]
        if data:
            ctypes.memmove(buffer, data, len(data))
        ctypes.cast(count, ctypes.POINTER(wintypes.DWORD)).contents.value = len(data)
        return True
