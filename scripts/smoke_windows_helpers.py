"""Smoke-test the two helper modes of a packaged Windows executable."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    if sys.platform != "win32" or len(arguments) != 1:
        return 64
    executable = Path(arguments[0]).resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    with tempfile.TemporaryDirectory(prefix="clipsoon-helper-smoke-") as temporary:
        _smoke_role(executable, "hotkey", ["--hotkey", "combo:ctrl+shift+space"])
        _smoke_role(executable, "clipboard", ["--ipc-dir", temporary])
    return 0


def _smoke_role(executable: Path, role: str, role_arguments: list[str]) -> None:
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
    try:
        ready = messages.get(timeout=8)
        _assert_envelope(ready, role, session_id, "ready")
        while True:
            heartbeat = messages.get(timeout=4)
            if heartbeat.get("type") == "heartbeat":
                _assert_envelope(heartbeat, role, session_id, "heartbeat")
                break
        assert process.stdin is not None
        process.stdin.write('{"type":"shutdown"}\n')
        process.stdin.flush()
        if process.wait(timeout=5) != 0:
            raise RuntimeError(f"{role} helper exited with {process.returncode}")
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        stderr = process.stderr.read() if process.stderr is not None else ""
        raise RuntimeError(f"{role} helper smoke failed: {stderr}") from None


def _assert_envelope(message: dict[str, object], role: str, session_id: str, event_type: str) -> None:
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
