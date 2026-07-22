"""Qt-side supervision for isolated Windows platform workers."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
_HEALTH_CHECK_INTERVAL_MS = 250
_STARTUP_TIMEOUT_SECONDS = 4.0
_HEARTBEAT_TIMEOUT_SECONDS = 1.75
_CLIPBOARD_NATIVE_CAPTURE_TIMEOUT_SECONDS = 10.0
_CLIPBOARD_MATERIALIZATION_TIMEOUT_SECONDS = 120.0
_STABLE_UPTIME_SECONDS = 10.0
_MAX_PROTOCOL_BUFFER_BYTES = 1_048_576
_PERMANENT_FATAL_CODES = frozenset({"invalid_hotkey", "registration_failed"})


def windows_worker_command(role: str, arguments: Sequence[str] = ()) -> tuple[str, list[str]]:
    """Build the source and PyInstaller-safe command for a worker role."""

    helper_argument = f"--windows-helper={role}"
    if bool(getattr(sys, "frozen", False)):
        return sys.executable, [helper_argument, *arguments]
    return sys.executable, ["-u", "-m", "clipsoon", helper_argument, *arguments]


@dataclass(slots=True)
class WorkerWatchdog:
    """Pure liveness state used by the QProcess supervisor."""

    startup_timeout: float = _STARTUP_TIMEOUT_SECONDS
    heartbeat_timeout: float = _HEARTBEAT_TIMEOUT_SECONDS
    started_at: float | None = None
    ready_at: float | None = None
    last_activity_at: float | None = None

    def started(self, at: float) -> None:
        self.started_at = at
        self.ready_at = None
        self.last_activity_at = at

    def activity(self, at: float, *, ready: bool = False) -> None:
        self.last_activity_at = at
        if ready and self.ready_at is None:
            self.ready_at = at

    def unhealthy_reason(self, *, running: bool, at: float) -> str | None:
        if not running:
            return "进程已停止"
        if self.started_at is None:
            return "进程尚未启动"
        if self.ready_at is None:
            if at - self.started_at > self.startup_timeout:
                return "启动握手超时"
            return None
        last_activity = self.last_activity_at if self.last_activity_at is not None else self.ready_at
        if at - last_activity > self.heartbeat_timeout:
            return "心跳超时"
        return None


class WorkerProtocolCursor:
    """Validate protocol envelopes and discard duplicate/stale child events."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.session_id: str | None = None
        self.last_event_id = -1
        self.ready_seen = False

    def reset(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.last_event_id = -1
        self.ready_seen = False

    def accept(self, message: Mapping[str, Any]) -> bool:
        if message.get("protocol") != PROTOCOL_VERSION:
            return False
        if message.get("role") != self.role:
            return False
        session_id = message.get("session_id")
        event_id = message.get("event_id")
        if not isinstance(session_id, str) or not session_id:
            return False
        if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 0:
            return False
        if self.session_id is None or session_id != self.session_id:
            return False
        if not self.ready_seen:
            first_type = message.get("type")
            if first_type != "ready" and not (first_type == "error" and message.get("fatal") is True):
                return False
        if event_id <= self.last_event_id:
            return False
        self.last_event_id = event_id
        if message.get("type") == "ready":
            self.ready_seen = True
        return True


class WindowsWorkerSupervisor(QObject):
    """Run one helper, validate its messages, and replace it when it stalls."""

    message = Signal(object)
    failed = Signal(str)
    health_changed = Signal(bool)

    def __init__(
        self,
        role: str,
        arguments: Callable[[], Sequence[str]],
        *,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.role = role
        self._arguments = arguments
        self._clock = clock
        self._desired = False
        self._intentional_restart = False
        self._restart_attempt = 0
        self._stop_handled = False
        self._reported_health = False
        self._accept_events = False
        self._session_id = ""
        self._expected_process_id = 0
        self._native_capture_started_at: float | None = None
        self._materialization_started_at: float | None = None
        self._reported_error_codes: set[str] = set()
        self._stdout_buffer = bytearray()
        self._protocol = WorkerProtocolCursor(role)
        self._watchdog = WorkerWatchdog()

        self._process = QProcess(self)
        self._process.started.connect(self._process_started)
        self._process.finished.connect(self._process_finished)
        self._process.errorOccurred.connect(self._process_error)
        self._process.readyReadStandardOutput.connect(self._read_stdout)
        self._process.readyReadStandardError.connect(self._read_stderr)

        self._health_timer = QTimer(self)
        self._health_timer.setInterval(_HEALTH_CHECK_INTERVAL_MS)
        self._health_timer.timeout.connect(self._check_health)
        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self._launch)

    @property
    def is_healthy(self) -> bool:
        running = self._process.state() == QProcess.ProcessState.Running
        return self._watchdog.ready_at is not None and self._unhealthy_reason(
            running=running, at=self._clock()
        ) is None

    @property
    def process_id(self) -> int:
        return self._expected_process_id or int(self._process.processId())

    @property
    def session_id(self) -> str:
        return self._session_id

    def start(self) -> None:
        self._desired = True
        self._intentional_restart = False
        self._restart_attempt = 0
        self._reported_error_codes.clear()
        self._restart_timer.stop()
        if self._process.state() == QProcess.ProcessState.NotRunning:
            self._launch()
        self._health_timer.start()

    def restart(self) -> None:
        self._desired = True
        self._accept_events = False
        self._intentional_restart = True
        self._restart_attempt = 0
        self._restart_timer.stop()
        if self._process.state() == QProcess.ProcessState.NotRunning:
            self._schedule_restart(0)
            return
        self._send_shutdown()
        QTimer.singleShot(350, self._kill_for_restart_if_needed)

    def stop(self) -> None:
        self._desired = False
        self._accept_events = False
        self._intentional_restart = False
        self._restart_timer.stop()
        self._health_timer.stop()
        self._set_reported_health(False)
        if self._process.state() == QProcess.ProcessState.NotRunning:
            return
        self._send_shutdown()
        if not self._process.waitForFinished(350):
            self._process.kill()
            self._process.waitForFinished(350)

    def send(self, message: Mapping[str, Any]) -> bool:
        if self._process.state() != QProcess.ProcessState.Running:
            return False
        payload = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        return self._process.write(payload) == len(payload)

    def _launch(self) -> None:
        if not self._desired or self._process.state() != QProcess.ProcessState.NotRunning:
            return
        self._stdout_buffer.clear()
        self._expected_process_id = 0
        self._native_capture_started_at = None
        self._materialization_started_at = None
        self._session_id = uuid.uuid4().hex
        self._protocol.reset(self._session_id)
        self._stop_handled = False
        self._intentional_restart = False
        self._accept_events = True
        self._watchdog.started(self._clock())
        self._set_reported_health(False)
        worker_arguments = [
            *self._arguments(),
            "--session-id",
            self._session_id,
            "--parent-pid",
            str(os.getpid()),
        ]
        program, arguments = windows_worker_command(self.role, worker_arguments)
        LOGGER.info("Starting Windows %s helper", self.role)
        self._process.start(program, arguments)

    def _process_started(self) -> None:
        self._expected_process_id = int(self._process.processId())
        self._watchdog.started(self._clock())

    def _process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._read_stdout()
        self._read_stderr()
        self._handle_process_stopped(exit_code)

    def _handle_process_stopped(self, exit_code: int) -> None:
        if self._stop_handled:
            return
        self._stop_handled = True
        self._set_reported_health(False)
        self._native_capture_started_at = None
        self._materialization_started_at = None
        self._expected_process_id = 0
        if not self._desired:
            return
        LOGGER.warning("Windows %s helper exited with code %d", self.role, exit_code)
        delay = 0 if self._intentional_restart else min(5_000, 125 * (2**self._restart_attempt))
        self._intentional_restart = False
        self._restart_attempt = min(6, self._restart_attempt + 1)
        self._schedule_restart(delay)

    def _process_error(self, error: QProcess.ProcessError) -> None:
        LOGGER.warning("Windows %s helper process error: %s", self.role, error)
        if error == QProcess.ProcessError.FailedToStart and self._desired:
            self.failed.emit(f"Windows {self.role} 宿主无法启动")
            self._handle_process_stopped(-1)

    def _read_stdout(self) -> None:
        self._stdout_buffer.extend(bytes(self._process.readAllStandardOutput()))
        if len(self._stdout_buffer) > _MAX_PROTOCOL_BUFFER_BYTES and b"\n" not in self._stdout_buffer:
            self.failed.emit(f"Windows {self.role} 宿主协议数据过大")
            self._process.kill()
            return
        while b"\n" in self._stdout_buffer:
            raw_line, _, remainder = self._stdout_buffer.partition(b"\n")
            self._stdout_buffer = bytearray(remainder)
            if not raw_line.strip():
                continue
            if len(raw_line) > _MAX_PROTOCOL_BUFFER_BYTES:
                self.failed.emit(f"Windows {self.role} 宿主协议消息过大")
                self._process.kill()
                return
            try:
                message = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                LOGGER.warning("Ignoring invalid %s helper protocol line", self.role)
                continue
            if not isinstance(message, dict):
                LOGGER.warning("Ignoring invalid or duplicate %s helper message", self.role)
                continue
            if message.get("type") == "ready" and message.get("pid") != self.process_id:
                LOGGER.warning("Ignoring %s helper handshake with the wrong pid", self.role)
                continue
            if not self._protocol.accept(message):
                LOGGER.warning("Ignoring invalid or duplicate %s helper message", self.role)
                continue
            self._handle_message(message)

    def _handle_message(self, message: dict[str, Any]) -> None:
        now = self._clock()
        kind = message.get("type")
        allowed_types = {"ready", "heartbeat", "error"}
        allowed_types.add("hotkey" if self.role == "hotkey" else "clipboard")
        if self.role == "clipboard":
            allowed_types.update({"capture_materializing", "capture_started"})
        if kind not in allowed_types:
            LOGGER.warning("Ignoring unknown %s helper message type: %s", self.role, kind)
            return
        if not self._accept_events:
            return
        self._watchdog.activity(now, ready=kind == "ready")
        if kind == "ready":
            self._native_capture_started_at = None
            self._materialization_started_at = None
            self._reported_error_codes.clear()
            self._set_reported_health(True)
        elif kind == "capture_started" and self.role == "clipboard":
            self._native_capture_started_at = now
            self._materialization_started_at = None
        elif kind in {"capture_materializing", "clipboard"} and self.role == "clipboard":
            self._native_capture_started_at = None
            self._materialization_started_at = now if kind == "capture_materializing" else None
        elif kind == "error":
            if self.role == "clipboard":
                self._native_capture_started_at = None
                self._materialization_started_at = None
            text = str(message.get("message") or f"Windows {self.role} 宿主发生错误")
            if message.get("fatal"):
                code = str(message.get("code") or "fatal")
                if code == "already_active":
                    text = "正在等待旧的 ClipSoon 热键宿主退出"
                if code in _PERMANENT_FATAL_CODES:
                    self._desired = False
                if code not in self._reported_error_codes:
                    self._reported_error_codes.add(code)
                    self.failed.emit(text)
        self.message.emit(message)

    def _read_stderr(self) -> None:
        output = bytes(self._process.readAllStandardError()).decode("utf-8", errors="replace").strip()
        if output:
            LOGGER.warning("Windows %s helper stderr: %s", self.role, output)

    def _check_health(self) -> None:
        state = self._process.state()
        if state == QProcess.ProcessState.NotRunning:
            self._set_reported_health(False)
            return
        running = state in {QProcess.ProcessState.Starting, QProcess.ProcessState.Running}
        now = self._clock()
        reason = self._unhealthy_reason(running=running, at=now)
        if reason is not None:
            self._set_reported_health(False)
            if running:
                LOGGER.warning("Windows %s helper is unhealthy (%s); replacing it", self.role, reason)
                self._process.kill()
            return
        if self._watchdog.ready_at is not None:
            self._set_reported_health(True)
            if now - self._watchdog.ready_at >= _STABLE_UPTIME_SECONDS:
                self._restart_attempt = 0

    def _schedule_restart(self, delay_ms: int) -> None:
        if not self._desired or self._restart_timer.isActive():
            return
        self._restart_timer.start(max(0, int(delay_ms)))

    def _unhealthy_reason(self, *, running: bool, at: float) -> str | None:
        if self.role == "clipboard" and self._native_capture_started_at is not None:
            if not running:
                return "进程已停止"
            if at - self._native_capture_started_at > _CLIPBOARD_NATIVE_CAPTURE_TIMEOUT_SECONDS:
                return "原生剪贴板读取超时"
            return None
        if self.role == "clipboard" and self._materialization_started_at is not None:
            if not running:
                return "进程已停止"
            if at - self._materialization_started_at > _CLIPBOARD_MATERIALIZATION_TIMEOUT_SECONDS:
                return "剪贴板内容转换落盘超时"
        return self._watchdog.unhealthy_reason(running=running, at=at)

    def _send_shutdown(self) -> None:
        if self.send({"type": "shutdown", "parent_pid": os.getpid()}):
            self._process.waitForBytesWritten(100)

    def _kill_for_restart_if_needed(self) -> None:
        if self._desired and self._intentional_restart and self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()

    def _set_reported_health(self, healthy: bool) -> None:
        if self._reported_health == healthy:
            return
        self._reported_health = healthy
        self.health_changed.emit(healthy)
