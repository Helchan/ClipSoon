"""Smoke-test the two helper modes of a packaged Windows executable."""

from __future__ import annotations

import base64
import json
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    if sys.platform != "win32" or len(arguments) != 1:
        return 64
    executable = Path(arguments[0]).resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    with tempfile.TemporaryDirectory(prefix="clipsoon-helper-smoke-") as temporary:
        ipc_dir = Path(temporary)
        _smoke_role(executable, "hotkey", ["--hotkey", "combo:ctrl+shift+space"])
        _smoke_clipboard(executable, ipc_dir)
    return 0


def _smoke_role(executable: Path, role: str, role_arguments: list[str]) -> None:
    process, messages, session_id = _start_role(executable, role, role_arguments)
    try:
        _wait_for(messages, role, session_id, "ready", timeout=8)
        _wait_for(messages, role, session_id, "heartbeat", timeout=4)
        _shutdown(process, role)
    except BaseException as exc:
        _abort(process, role, exc)


def _smoke_clipboard(executable: Path, ipc_dir: Path) -> None:
    # Imported only on Windows so this script keeps its historical return-64
    # behavior when release checks invoke it on another operating system.
    from clipsoon.windows_clipboard_host import (
        CF_DIB,
        CF_DIBV5,
        CF_HDROP,
        CF_UNICODETEXT,
        CtypesWindowsApi,
    )

    process, messages, session_id = _start_role(
        executable,
        "clipboard",
        ["--ipc-dir", str(ipc_dir)],
    )
    api = CtypesWindowsApi()
    try:
        _wait_for(messages, "clipboard", session_id, "ready", timeout=8)
        _wait_for(messages, "clipboard", session_id, "heartbeat", timeout=4)

        text = "ClipSoon 打包剪贴板冒烟\n第二行"
        _write_clipboard_value(
            process,
            messages,
            ipc_dir,
            session_id,
            "text",
            {"text": text},
        )
        text_bytes = _read_clipboard(api, lambda: api.global_bytes(CF_UNICODETEXT))
        if _decode_unicode_text(text_bytes) != text.replace("\n", "\r\n"):
            raise RuntimeError("CF_UNICODETEXT round-trip mismatch")

        source_file = ipc_dir / "hdrop-source.txt"
        source_file.write_text("ClipSoon CF_HDROP smoke", encoding="utf-8")
        _write_clipboard_value(
            process,
            messages,
            ipc_dir,
            session_id,
            "files",
            {"files": [str(source_file.resolve())]},
        )
        copied_files = _read_clipboard(api, lambda: api.hdrop_files(CF_HDROP))
        if (
            len(copied_files) != 1
            or Path(copied_files[0]).resolve() != source_file.resolve()
        ):
            raise RuntimeError(f"CF_HDROP round-trip mismatch: {copied_files!r}")

        dibv5 = _dibv5_1x1()
        png_name = f"write-png-{session_id}-{uuid.uuid4().hex}.png"
        dibv5_name = f"write-dibv5-{session_id}-{uuid.uuid4().hex}.bin"
        (ipc_dir / png_name).write_bytes(_PNG_1X1)
        (ipc_dir / dibv5_name).write_bytes(dibv5)
        _write_clipboard_value(
            process,
            messages,
            ipc_dir,
            session_id,
            "image",
            {
                "png_file": png_name,
                "png_bytes": len(_PNG_1X1),
                "dibv5_file": dibv5_name,
                "dibv5_bytes": len(dibv5),
            },
        )

        # This ordering is the regression gate: the writer process must be
        # gone before a different process asks Windows for every image format.
        # Delayed-rendering/null-handle implementations fail this check.
        _shutdown(process, "clipboard")
        png_format = api.register_format("PNG")

        def read_image_formats() -> tuple[bytes, bytes, bytes]:
            if not all(
                api.is_format_available(format_id)
                for format_id in (png_format, CF_DIBV5, CF_DIB)
            ):
                raise RuntimeError("PNG, CF_DIBV5, or synthesized CF_DIB is unavailable")
            return (
                api.global_bytes(png_format),
                api.global_bytes(CF_DIBV5),
                api.global_bytes(CF_DIB),
            )

        png, persisted_dibv5, synthesized_dib = _read_clipboard(api, read_image_formats)
        if png[: len(_PNG_1X1)] != _PNG_1X1 or any(png[len(_PNG_1X1) :]):
            raise RuntimeError("registered PNG did not survive clipboard helper exit")
        if persisted_dibv5[: len(dibv5)] != dibv5 or any(persisted_dibv5[len(dibv5) :]):
            raise RuntimeError("CF_DIBV5 did not survive clipboard helper exit")
        if not synthesized_dib:
            raise RuntimeError("synthesized CF_DIB is empty after clipboard helper exit")
    except BaseException as exc:
        _abort(process, "clipboard", exc)


def _start_role(
    executable: Path,
    role: str,
    role_arguments: list[str],
) -> tuple[subprocess.Popen[str], queue.Queue[dict[str, object]], str]:
    session_id = uuid.uuid4().hex
    command = [
        str(executable),
        f"--windows-helper={role}",
        *role_arguments,
        "--session-id",
        session_id,
        "--parent-pid",
        str(os.getpid()),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    messages: queue.Queue[dict[str, object]] = queue.Queue()

    def read_messages() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                messages.put(value)

    threading.Thread(target=read_messages, daemon=True).start()
    return process, messages, session_id


def _write_clipboard_value(
    process: subprocess.Popen[str],
    messages: queue.Queue[dict[str, object]],
    ipc_dir: Path,
    session_id: str,
    kind: str,
    value: dict[str, Any],
) -> int:
    request_id = uuid.uuid4().hex
    manifest = {
        "protocol": 1,
        "session_id": session_id,
        "request_id": request_id,
        "kind": kind,
        **value,
    }
    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
    manifest_name = f"write-manifest-{session_id}-{request_id}.json"
    (ipc_dir / manifest_name).write_bytes(payload)
    _send(
        process,
        {
            "type": "write_clipboard",
            "request_id": request_id,
            "kind": kind,
            "manifest": manifest_name,
            "manifest_bytes": len(payload),
        },
    )
    _wait_for(
        messages,
        "clipboard",
        session_id,
        "write_started",
        request_id=request_id,
        timeout=8,
    )
    result = _wait_for(
        messages,
        "clipboard",
        session_id,
        "write_result",
        request_id=request_id,
        timeout=12,
    )
    if result.get("ok") is not True or not isinstance(result.get("sequence"), int):
        raise RuntimeError(f"{kind} clipboard write failed: {result!r}")
    sequence = int(result["sequence"])
    _send(
        process,
        {
            "type": "verify_clipboard",
            "request_id": request_id,
            "kind": kind,
            "sequence": sequence,
        },
    )
    verification = _wait_for(
        messages,
        "clipboard",
        session_id,
        "verify_result",
        request_id=request_id,
        timeout=8,
    )
    if verification.get("ok") is not True or verification.get("sequence") != sequence:
        raise RuntimeError(f"{kind} clipboard verification failed: {verification!r}")
    return sequence


def _wait_for(
    messages: queue.Queue[dict[str, object]],
    role: str,
    session_id: str,
    event_type: str,
    *,
    request_id: str | None = None,
    timeout: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for {role} {event_type}")
        message = messages.get(timeout=remaining)
        if message.get("type") != event_type:
            continue
        if request_id is not None and message.get("request_id") != request_id:
            continue
        _assert_envelope(message, role, session_id, event_type)
        return message


def _send(process: subprocess.Popen[str], message: dict[str, object]) -> None:
    if process.stdin is None:
        raise RuntimeError("helper stdin is unavailable")
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _shutdown(process: subprocess.Popen[str], role: str) -> None:
    _send(process, {"type": "shutdown"})
    if process.wait(timeout=5) != 0:
        raise RuntimeError(f"{role} helper exited with {process.returncode}")


def _abort(
    process: subprocess.Popen[str],
    role: str,
    failure: BaseException,
) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    stderr = process.stderr.read().strip() if process.stderr is not None else ""
    detail = f"{failure}"
    if stderr:
        detail = f"{detail}; helper stderr: {stderr}"
    raise RuntimeError(f"{role} helper smoke failed: {detail}") from failure


def _read_clipboard(api: Any, read: Callable[[], Any]) -> Any:
    deadline = time.monotonic() + 2
    while not api.open_clipboard(0):
        if time.monotonic() >= deadline:
            raise RuntimeError("OpenClipboard failed during independent verification")
        time.sleep(0.01)
    try:
        return read()
    finally:
        if not api.close_clipboard():
            raise RuntimeError("CloseClipboard failed during independent verification")


def _decode_unicode_text(data: bytes) -> str:
    terminator = next(
        (
            offset
            for offset in range(0, max(0, len(data) - 1), 2)
            if data[offset : offset + 2] == b"\0\0"
        ),
        len(data),
    )
    return data[:terminator].decode("utf-16-le")


def _dibv5_1x1() -> bytes:
    header = bytearray(124)
    struct.pack_into("<IiiHHIIiiII", header, 0, 124, 1, 1, 1, 32, 0, 4, 0, 0, 0, 0)
    struct.pack_into(
        "<IIIII",
        header,
        40,
        0x00FF0000,
        0x0000FF00,
        0x000000FF,
        0xFF000000,
        0x57696E20,
    )
    struct.pack_into("<I", header, 108, 4)
    return bytes(header) + b"\x00\x00\xff\xff"


def _assert_envelope(
    message: dict[str, object],
    role: str,
    session_id: str,
    event_type: str,
) -> None:
    if not (
        message.get("protocol") == 1
        and message.get("role") == role
        and message.get("session_id") == session_id
        and message.get("type") == event_type
        and isinstance(message.get("event_id"), int)
    ):
        raise RuntimeError(f"invalid {role} {event_type} message: {message!r}")


if __name__ == "__main__":
    raise SystemExit(main())
