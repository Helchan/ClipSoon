from __future__ import annotations

import sys

from clipsoon.windows_workers import (
    _PERMANENT_FATAL_CODES,
    WindowsWorkerSupervisor,
    WorkerProtocolCursor,
    WorkerWatchdog,
    windows_worker_command,
)


def test_windows_worker_command_handles_source_and_frozen_modes(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", "/python")

    assert windows_worker_command("hotkey", ("--spec", "double:ctrl")) == (
        "/python",
        ["-u", "-m", "clipsoon", "--windows-helper=hotkey", "--spec", "double:ctrl"],
    )

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert windows_worker_command("clipboard", ("--payload-dir", "C:/clips")) == (
        "/python",
        ["--windows-helper=clipboard", "--payload-dir", "C:/clips"],
    )


def test_worker_protocol_rejects_wrong_role_session_and_duplicate_events() -> None:
    cursor = WorkerProtocolCursor("hotkey")
    first = {
        "protocol": 1,
        "role": "hotkey",
        "session_id": "one",
        "event_id": 0,
    }

    cursor.reset("one")
    assert not cursor.accept({**first, "type": "heartbeat"})
    assert cursor.accept({**first, "type": "ready"})
    assert not cursor.accept(first)
    assert not cursor.accept({**first, "event_id": 1, "session_id": "two"})
    assert not cursor.accept({**first, "event_id": 1, "role": "clipboard"})
    assert not cursor.accept({**first, "event_id": 1, "protocol": 2})
    assert cursor.accept({**first, "event_id": 2})

    cursor.reset("two")
    assert cursor.accept({**first, "session_id": "two", "type": "ready"})

    cursor.reset("fatal")
    assert cursor.accept(
        {
            **first,
            "session_id": "fatal",
            "type": "error",
            "fatal": True,
        }
    )


def test_worker_watchdog_distinguishes_startup_and_heartbeat_timeouts() -> None:
    watchdog = WorkerWatchdog(startup_timeout=4.0, heartbeat_timeout=1.75)
    watchdog.started(10.0)

    assert watchdog.unhealthy_reason(running=True, at=13.9) is None
    assert watchdog.unhealthy_reason(running=True, at=14.1) == "启动握手超时"

    watchdog.activity(14.2, ready=True)
    assert watchdog.unhealthy_reason(running=True, at=15.9) is None
    assert watchdog.unhealthy_reason(running=True, at=16.0) == "心跳超时"
    assert watchdog.unhealthy_reason(running=False, at=14.3) == "进程已停止"


def test_stale_hotkey_mutex_is_retryable_but_registration_conflict_is_permanent() -> None:
    assert "already_active" not in _PERMANENT_FATAL_CODES
    assert "startup_failed" not in _PERMANENT_FATAL_CODES
    assert "invalid_hotkey" in _PERMANENT_FATAL_CODES
    assert "registration_failed" in _PERMANENT_FATAL_CODES


def test_supervisor_retries_stale_mutex_once_but_stops_invalid_configuration(qapp) -> None:
    del qapp
    supervisor = WindowsWorkerSupervisor("hotkey", lambda: [])
    failures: list[str] = []
    supervisor.failed.connect(failures.append)
    supervisor._desired = True
    supervisor._accept_events = True

    stale = {"type": "error", "fatal": True, "code": "already_active"}
    supervisor._handle_message(stale)
    supervisor._handle_message(stale)

    assert supervisor._desired
    assert failures == ["正在等待旧的 ClipSoon 热键宿主退出"]

    supervisor._handle_message(
        {
            "type": "error",
            "fatal": True,
            "code": "invalid_hotkey",
            "message": "invalid shortcut",
        }
    )

    assert not supervisor._desired
    assert failures[-1] == "invalid shortcut"


def test_supervisor_emits_structured_fatal_error_before_display_failure(qapp) -> None:
    del qapp
    supervisor = WindowsWorkerSupervisor("hotkey", lambda: [])
    events: list[tuple[str, object]] = []
    supervisor.message.connect(lambda message: events.append(("message", message)))
    supervisor.failed.connect(lambda message: events.append(("failed", message)))
    supervisor._desired = True
    supervisor._accept_events = True
    error = {
        "type": "error",
        "fatal": True,
        "code": "registration_failed",
        "message": "shortcut already registered",
    }

    supervisor._handle_message(error)

    assert events == [
        ("message", error),
        ("failed", "shortcut already registered"),
    ]
    assert not supervisor._desired


def test_clipboard_native_capture_has_a_separate_bounded_timeout(qapp) -> None:
    del qapp
    now = [12.0]
    supervisor = WindowsWorkerSupervisor("clipboard", lambda: [], clock=lambda: now[0])
    supervisor._watchdog.started(10.0)
    supervisor._watchdog.activity(10.1, ready=True)
    supervisor._accept_events = True
    supervisor._handle_message({"type": "capture_started", "sequence": 7})

    assert supervisor.native_operation_kind == "capture"
    assert supervisor.is_native_capture_active
    assert supervisor.is_capture_pipeline_busy
    assert supervisor._unhealthy_reason(running=True, at=21.9) is None
    assert supervisor._unhealthy_reason(running=True, at=22.1) == "原生剪贴板读取超时"

    now[0] = 30.0
    supervisor._handle_message({"type": "capture_materializing", "sequence": 7})
    assert supervisor.native_operation_kind is None
    assert not supervisor.is_native_capture_active
    assert supervisor.is_capture_pipeline_busy
    supervisor._watchdog.activity(149.5)
    assert supervisor._unhealthy_reason(running=True, at=149.9) is None
    assert supervisor._unhealthy_reason(running=True, at=150.1) == "剪贴板内容转换落盘超时"

    supervisor._handle_message(
        {
            "type": "clipboard",
            "sequence": 7,
            "kind": "text",
            "manifest": "manifest.json",
        }
    )
    assert not supervisor.is_capture_pipeline_busy


def test_clipboard_write_protocol_is_forwarded_and_has_a_bounded_native_timeout(qapp) -> None:
    del qapp
    now = [40.0]
    supervisor = WindowsWorkerSupervisor("clipboard", lambda: [], clock=lambda: now[0])
    supervisor._watchdog.started(39.0)
    supervisor._watchdog.activity(39.1, ready=True)
    supervisor._accept_events = True
    messages: list[dict[str, object]] = []
    supervisor.message.connect(messages.append)

    started = {"type": "write_started", "request_id": "request-one"}
    supervisor._handle_message(started)

    assert supervisor.native_operation_kind == "write"
    assert not supervisor.is_native_capture_active
    assert not supervisor.is_capture_pipeline_busy
    assert supervisor._unhealthy_reason(running=True, at=49.9) is None
    assert supervisor._unhealthy_reason(running=True, at=50.1) == "原生剪贴板写入超时"

    now[0] = 50.2
    result = {"type": "write_result", "request_id": "request-one", "ok": True}
    supervisor._handle_message(result)
    assert supervisor.native_operation_kind is None

    now[0] = 51.0
    supervisor._handle_message({"type": "write_started", "request_id": "request-two"})
    verified = {"type": "verify_result", "request_id": "request-two", "ok": True}
    supervisor._handle_message(verified)

    assert supervisor.native_operation_kind is None
    assert messages == [
        started,
        result,
        {"type": "write_started", "request_id": "request-two"},
        verified,
    ]


def test_hotkey_supervisor_ignores_clipboard_write_protocol(qapp) -> None:
    del qapp
    supervisor = WindowsWorkerSupervisor("hotkey", lambda: [])
    supervisor._accept_events = True
    messages: list[dict[str, object]] = []
    supervisor.message.connect(messages.append)

    supervisor._handle_message({"type": "write_started", "request_id": "request-one"})

    assert messages == []
    assert supervisor.native_operation_kind is None
