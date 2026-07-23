from __future__ import annotations

import ctypes
import json
import plistlib
import struct
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

from PySide6.QtCore import QMimeData, QObject, QRunnable, QThreadPool, QUrl, Signal
from PySide6.QtGui import QClipboard, QColor, QImage

import clipsoon.core as core_module
import clipsoon.system as system_module
from clipsoon.core import (
    WINDOWS_DEFAULT_HOTKEY,
    AppSettings,
    ClipboardWriteReceipt,
    ClipItem,
    ClipKind,
    HistoryRepository,
)
from clipsoon.system import (
    ClipboardController,
    ForegroundTargetHandle,
    GlobalHotkeyService,
    HotkeyActivationContext,
    HotkeyStateMachine,
    LaunchAtLoginManager,
    PlatformBridge,
    PynputPasteAdapter,
    SelectionSender,
    _bitmap_v5_payload,
    _canonical_key,
    _NativeClipboardStoreTask,
    _prepare_windows_write_artifacts,
)


def test_windows_ipc_cleanup_preserves_only_live_helper_sessions(tmp_path: Path) -> None:
    clipboard = FakeClipboard()
    controller, repository = make_controller(tmp_path, clipboard)
    controller._windows_ipc_dir.mkdir(parents=True, exist_ok=True)
    live = "a" * 32
    stale = "b" * 32
    live_manifest = controller._windows_ipc_dir / f"manifest-{live}-7-test.json"
    live_dib = controller._windows_ipc_dir / f"write-dib-{live}-request.bin"
    stale_manifest = controller._windows_ipc_dir / f"manifest-{stale}-8-test.json"
    stale_payload = controller._windows_ipc_dir / f".clip-{stale}-8-test.tmp"
    stale_dib = controller._windows_ipc_dir / f"write-dib-{stale}-request.bin"
    legacy = controller._windows_ipc_dir / "manifest-8-legacy.json"
    for path in (
        live_manifest,
        live_dib,
        stale_manifest,
        stale_payload,
        stale_dib,
        legacy,
    ):
        path.write_text("x", encoding="utf-8")

    controller._cleanup_windows_ipc_orphans({live})

    assert live_manifest.exists()
    assert live_dib.exists()
    assert not stale_manifest.exists()
    assert not stale_payload.exists()
    assert not stale_dib.exists()
    assert not legacy.exists()
    repository.close()


class FakeClipboard(QObject):
    dataChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.mime = QMimeData()
        self.sequence = 0
        self.owned = False
        self.write_history: list[QMimeData] = []

    def mimeData(self, _mode=QClipboard.Mode.Clipboard) -> QMimeData:
        return self.mime

    def image(self, _mode=QClipboard.Mode.Clipboard) -> QImage:
        value = self.mime.imageData()
        return value if isinstance(value, QImage) else QImage()

    def setMimeData(self, mime: QMimeData, _mode=QClipboard.Mode.Clipboard) -> None:
        self.mime = mime
        self.owned = True
        self.write_history.append(mime)
        self.sequence += 1
        self.dataChanged.emit()

    def ownsClipboard(self) -> bool:
        return self.owned

    def set_external(self, mime: QMimeData) -> None:
        self.mime = mime
        self.owned = False
        self.sequence += 1
        self.dataChanged.emit()


class FlakyClipboard(FakeClipboard):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.read_attempts = 0

    def mimeData(self, _mode=QClipboard.Mode.Clipboard) -> QMimeData:
        self.read_attempts += 1
        if self.read_attempts <= self.failures:
            raise RuntimeError("clipboard busy")
        return self.mime


class CallbackSignal:
    def __init__(self) -> None:
        self.callbacks: list = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *arguments) -> None:
        for callback in tuple(self.callbacks):
            callback(*arguments)


class FakeNativeCall:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *arguments):
        return self.callback(*arguments)


class FakeWorkerSupervisor:
    instances: list[FakeWorkerSupervisor] = []

    def __init__(self, role, arguments) -> None:
        self.role, self.arguments = role, arguments
        self.message = CallbackSignal()
        self.failed = CallbackSignal()
        self.health_changed = CallbackSignal()
        self.is_healthy = False
        self.is_capture_pipeline_busy = False
        self.session_id = "a" * 32
        self.sent: list[dict] = []
        self.started = 0
        self.stopped = 0
        self.restarted = 0
        self.instances.append(self)

    def start(self) -> None:
        self.started += 1
        self.is_healthy = True

    def stop(self) -> None:
        self.stopped += 1
        self.is_healthy = False

    def restart(self) -> None:
        self.restarted += 1

    def send(self, message) -> bool:
        self.sent.append(dict(message))
        return self.is_healthy


def test_double_modifier_state_machine_contract() -> None:
    hits: list[str] = []
    machine = HotkeyStateMachine("double:ctrl", 420, lambda: hits.append("hit"))
    machine.press("ctrl_l", 0.00)
    machine.release("ctrl_l", 0.05)
    machine.press("ctrl_r", 0.30)
    machine.release("ctrl_r", 0.35)
    assert hits == ["hit"]

    # Auto-repeat, a slow pair, a long hold, and a Ctrl+C chord never trigger.
    machine.press("ctrl", 1.00)
    machine.press("ctrl", 1.02)
    machine.release("ctrl", 1.05)
    machine.press("ctrl", 1.60)
    machine.release("ctrl", 1.65)
    machine.press("ctrl", 2.00)
    machine.release("ctrl", 2.50)
    machine.press("ctrl", 3.00)
    machine.press("c", 3.02)
    machine.release("c", 3.03)
    machine.release("ctrl", 3.05)
    machine.press("ctrl", 3.20)
    machine.release("ctrl", 3.22)
    assert hits == ["hit"]


def test_combo_state_machine_triggers_once_per_chord() -> None:
    hits: list[int] = []
    machine = HotkeyStateMachine("combo:ctrl+shift+v", 420, lambda: hits.append(1))
    for key in ("ctrl", "shift", "v"):
        machine.press(key, 0)
    machine.press("v", 0.1)
    assert len(hits) == 1
    machine.release("v", 0.2)
    machine.press("v", 0.3)
    assert len(hits) == 2
    machine.release("missing", 1)
    machine.configure("double:shift", 300)
    machine.press("shift", 2)
    machine.release("shift", 2.01)


def test_hotkey_state_machine_recovers_from_a_missing_non_modifier_release() -> None:
    hits: list[str] = []
    machine = HotkeyStateMachine("double:ctrl", 420, lambda: hits.append("hit"))
    machine.press("c", 0.0)

    machine.press("ctrl", 1.1)
    machine.release("ctrl", 1.15)
    machine.press("ctrl", 1.3)
    machine.release("ctrl", 1.35)

    assert hits == ["hit"]


def make_controller(tmp_path: Path, clipboard: FakeClipboard) -> tuple[ClipboardController, HistoryRepository]:
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(clipboard, repository, AppSettings, lambda: "Source")
    controller._sequence_number = lambda: clipboard.sequence  # type: ignore[method-assign]
    controller._last_sequence = clipboard.sequence
    controller.start()
    return controller, repository


def test_clipboard_capture_precedence_and_self_write(qtbot, tmp_path: Path) -> None:
    clipboard = FakeClipboard()
    controller, repository = make_controller(tmp_path, clipboard)
    captured: list[ClipItem] = []
    controller.captured.connect(captured.append)

    mime = QMimeData()
    mime.setText("text fallback")
    mime.setImageData(QImage(2, 2, QImage.Format.Format_ARGB32))
    file_path = tmp_path / "hello.txt"
    file_path.write_text("hello", encoding="utf-8")
    mime.setUrls([QUrl.fromLocalFile(str(file_path))])
    clipboard.set_external(mime)
    qtbot.waitUntil(lambda: bool(captured), timeout=1_000)
    assert captured[-1].kind is ClipKind.FILES
    assert len(repository.list_items()) == 1

    text_mime = QMimeData()
    text_mime.setText("plain text")
    clipboard.set_external(text_mime)
    qtbot.waitUntil(lambda: captured[-1].text == "plain text", timeout=1_000)
    assert captured[-1].text == "plain text"
    before = len(repository.list_items())
    assert controller.write_item(captured[-1])
    assert len(repository.list_items()) == before
    controller.stop()
    repository.close()


def test_windows_bitmap_v5_payload_has_header_and_bottom_up_bgra_pixels() -> None:
    image = QImage(1, 2, QImage.Format.Format_ARGB32)
    image.setPixelColor(0, 0, QColor(10, 20, 30, 40))
    image.setPixelColor(0, 1, QColor(50, 60, 70, 80))

    payload = _bitmap_v5_payload(image)

    assert len(payload) == 124 + 8
    assert struct.unpack_from("<IiiHHI", payload, 0) == (124, 1, 2, 1, 32, 0)
    assert struct.unpack_from("<IIIII", payload, 40) == (
        0x00FF0000,
        0x0000FF00,
        0x000000FF,
        0xFF000000,
        0x57696E20,
    )
    assert payload[124:] == bytes((22, 19, 16, 80, 5, 3, 2, 40))


def test_windows_write_artifacts_use_session_scoped_atomic_manifest(tmp_path: Path) -> None:
    image_path = tmp_path / "outgoing.png"
    image = QImage(2, 1, QImage.Format.Format_ARGB32)
    image.fill(0x88776655)
    assert image.save(str(image_path), "PNG")
    item = ClipItem("image", ClipKind.IMAGE, "hash", 1, 1, image_path=str(image_path))
    session_id, request_id = "a" * 32, "b" * 32

    prepared = _prepare_windows_write_artifacts(tmp_path / "ipc", item, request_id, session_id)
    manifest_path = tmp_path / "ipc" / prepared.manifest_name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["session_id"] == session_id
    assert manifest["request_id"] == request_id
    assert manifest["kind"] == "image"
    assert manifest["png_bytes"] == (tmp_path / "ipc" / manifest["png_file"]).stat().st_size
    assert manifest["dibv5_bytes"] == (tmp_path / "ipc" / manifest["dibv5_file"]).stat().st_size
    dib_path = tmp_path / "ipc" / manifest["dib_file"]
    assert manifest["dib_bytes"] == dib_path.stat().st_size
    dib = dib_path.read_bytes()
    assert struct.unpack_from("<IiiHHI", dib) == (40, 2, 1, 1, 32, 0)
    assert not tuple((tmp_path / "ipc").glob(".*.tmp"))


def test_windows_image_write_ack_cleans_all_prepared_artifacts(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(
        system_module,
        "WindowsWorkerSupervisor",
        FakeWorkerSupervisor,
    )
    image_path = tmp_path / "outgoing.png"
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(0xFF336699)
    assert image.save(str(image_path), "PNG")
    repository = HistoryRepository(tmp_path / "data")
    controller = ClipboardController(
        FakeClipboard(),
        repository,
        AppSettings,
        lambda: "Source",
    )
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    item = ClipItem(
        "image",
        ClipKind.IMAGE,
        "hash",
        1,
        1,
        image_path=str(image_path),
    )
    results: list[tuple[ClipboardWriteReceipt | None, str]] = []

    controller.request_write(
        item,
        lambda receipt, error: results.append((receipt, error)),
    )
    qtbot.waitUntil(lambda: bool(worker.sent), timeout=1_000)
    command = worker.sent[-1]
    request_id = str(command["request_id"])
    prepared_paths = tuple(controller._windows_pending_writes[request_id].paths)
    assert len(prepared_paths) == 4
    assert all(path.exists() for path in prepared_paths)

    worker.message.emit(
        {
            "type": "write_result",
            "ok": True,
            "request_id": request_id,
            "kind": "image",
            "sequence": 41,
        }
    )

    assert results == [
        (ClipboardWriteReceipt(request_id, ClipKind.IMAGE, 41), "")
    ]
    assert all(not path.exists() for path in prepared_paths)
    controller.stop()
    repository.close()


def test_windows_write_uses_broker_ack_and_never_qt_clipboard(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    clipboard = FakeClipboard()
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(clipboard, repository, AppSettings, lambda: "Source")
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    item = ClipItem("text", ClipKind.TEXT, "hash", 1, 1, text="payload")
    results: list[tuple[ClipboardWriteReceipt | None, str]] = []

    assert not controller.write_item(item)
    controller.request_write(item, lambda receipt, error: results.append((receipt, error)))
    qtbot.waitUntil(lambda: bool(worker.sent), timeout=1_000)
    command = worker.sent[-1]
    assert command["type"] == "write_clipboard"
    assert clipboard.write_history == []
    worker.message.emit(
        {
            "type": "write_result",
            "ok": True,
            "request_id": "f" * 32,
            "kind": "text",
            "sequence": 41,
        }
    )
    assert results == []
    worker.message.emit(
        {
            "type": "write_result",
            "ok": True,
            "request_id": command["request_id"],
            "kind": "text",
            "sequence": 42,
            "code": "",
            "error": "",
        }
    )
    assert results == [(ClipboardWriteReceipt(command["request_id"], ClipKind.TEXT, 42), "")]
    worker.message.emit(
        {
            "type": "write_result",
            "ok": True,
            "request_id": command["request_id"],
            "kind": "text",
            "sequence": 42,
        }
    )
    assert len(results) == 1
    controller.stop()
    repository.close()


def test_windows_write_waits_for_starting_helper_and_keeps_request_id(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(FakeClipboard(), repository, AppSettings, lambda: "Source")
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    worker.is_healthy = False
    worker.session_id = ""
    results: list[tuple[ClipboardWriteReceipt | None, str]] = []

    controller.request_write(
        ClipItem("text", ClipKind.TEXT, "hash", 1, 1, text="payload"),
        lambda receipt, error: results.append((receipt, error)),
    )

    assert worker.sent == []
    assert len(controller._windows_pending_writes) == 1
    request_id = next(iter(controller._windows_pending_writes))

    worker.session_id = "b" * 32
    worker.is_healthy = True
    worker.health_changed.emit(True)
    qtbot.waitUntil(lambda: bool(worker.sent), timeout=1_000)
    assert worker.sent[-1]["request_id"] == request_id

    worker.message.emit(
        {
            "type": "write_result",
            "ok": True,
            "request_id": request_id,
            "kind": "text",
            "sequence": 43,
        }
    )
    assert results == [(ClipboardWriteReceipt(request_id, ClipKind.TEXT, 43), "")]
    controller.stop()
    repository.close()


def test_windows_transient_write_failure_replays_once_with_same_request_id(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(FakeClipboard(), repository, AppSettings, lambda: "Source")
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    results: list[tuple[ClipboardWriteReceipt | None, str]] = []

    controller.request_write(
        ClipItem("text", ClipKind.TEXT, "hash", 1, 1, text="payload"),
        lambda receipt, error: results.append((receipt, error)),
    )
    qtbot.waitUntil(lambda: bool(worker.sent), timeout=1_000)
    request_id = str(worker.sent[-1]["request_id"])
    worker.message.emit(
        {
            "type": "write_result",
            "ok": False,
            "request_id": request_id,
            "kind": "text",
            "sequence": 44,
            "code": "clipboard_busy",
            "error": "clipboard busy",
        }
    )

    qtbot.waitUntil(
        lambda: len([command for command in worker.sent if command["type"] == "write_clipboard"])
        == 2,
        timeout=1_000,
    )
    assert results == []
    assert worker.sent[-1]["request_id"] == request_id

    worker.message.emit(
        {
            "type": "write_result",
            "ok": True,
            "request_id": request_id,
            "kind": "text",
            "sequence": 45,
        }
    )
    assert results == [(ClipboardWriteReceipt(request_id, ClipKind.TEXT, 45), "")]
    controller.stop()
    repository.close()


def test_windows_transient_write_failure_never_retries_more_than_once(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(FakeClipboard(), repository, AppSettings, lambda: "Source")
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    results: list[tuple[ClipboardWriteReceipt | None, str]] = []

    controller.request_write(
        ClipItem("text", ClipKind.TEXT, "hash", 1, 1, text="payload"),
        lambda receipt, error: results.append((receipt, error)),
    )
    qtbot.waitUntil(lambda: bool(worker.sent), timeout=1_000)
    request_id = str(worker.sent[-1]["request_id"])
    for code in ("verification_failed", "close_failed"):
        worker.message.emit(
            {
                "type": "write_result",
                "ok": False,
                "request_id": request_id,
                "kind": "text",
                "sequence": 46,
                "code": code,
                "error": code,
            }
        )
        if code == "verification_failed":
            qtbot.waitUntil(
                lambda: len(
                    [command for command in worker.sent if command["type"] == "write_clipboard"]
                )
                == 2,
                timeout=1_000,
            )

    assert results == [(None, "close_failed")]
    assert len([command for command in worker.sent if command["type"] == "write_clipboard"]) == 2
    controller.stop()
    repository.close()


def test_windows_materializing_capture_is_preempted_once_before_write(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(FakeClipboard(), repository, AppSettings, lambda: "Source")
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    worker.is_capture_pipeline_busy = True
    worker.message.emit({"type": "capture_materializing", "sequence": 70})
    results: list[tuple[ClipboardWriteReceipt | None, str]] = []

    controller.request_write(
        ClipItem("text", ClipKind.TEXT, "hash", 1, 1, text="payload"),
        lambda receipt, error: results.append((receipt, error)),
    )
    worker.message.emit({"type": "capture_materializing", "sequence": 70})

    assert worker.restarted == 1
    assert worker.sent == []
    assert controller._windows_accepted_sequence == 70
    request_id = next(iter(controller._windows_pending_writes))

    worker.is_healthy = False
    worker.health_changed.emit(False)
    worker.session_id = "b" * 32
    worker.is_capture_pipeline_busy = False
    worker.is_healthy = True
    worker.health_changed.emit(True)
    qtbot.waitUntil(lambda: bool(worker.sent), timeout=1_000)
    assert worker.sent[-1]["request_id"] == request_id

    worker.message.emit(
        {
            "type": "write_result",
            "ok": True,
            "request_id": request_id,
            "kind": "text",
            "sequence": 71,
        }
    )
    assert results == [(ClipboardWriteReceipt(request_id, ClipKind.TEXT, 71), "")]
    controller.stop()
    repository.close()


def test_windows_capture_event_preempts_inflight_write_and_ignores_late_ack(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(FakeClipboard(), repository, AppSettings, lambda: "Source")
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    results: list[tuple[ClipboardWriteReceipt | None, str]] = []

    controller.request_write(
        ClipItem("text", ClipKind.TEXT, "hash", 1, 1, text="payload"),
        lambda receipt, error: results.append((receipt, error)),
    )
    qtbot.waitUntil(lambda: bool(worker.sent), timeout=1_000)
    first_command = worker.sent[-1]
    request_id = str(first_command["request_id"])
    first_manifest = controller._windows_ipc_dir / str(first_command["manifest"])
    assert first_manifest.exists()

    worker.is_capture_pipeline_busy = True
    worker.message.emit({"type": "capture_started", "sequence": 80})
    worker.message.emit({"type": "capture_materializing", "sequence": 80})
    assert worker.restarted == 1
    assert not first_manifest.exists()

    worker.message.emit(
        {
            "type": "write_result",
            "ok": True,
            "request_id": request_id,
            "kind": "text",
            "sequence": 81,
        }
    )
    assert results == []

    worker.is_healthy = False
    worker.health_changed.emit(False)
    worker.session_id = "c" * 32
    worker.is_capture_pipeline_busy = False
    worker.is_healthy = True
    worker.health_changed.emit(True)
    qtbot.waitUntil(
        lambda: len([command for command in worker.sent if command["type"] == "write_clipboard"])
        == 2,
        timeout=1_000,
    )
    second_command = worker.sent[-1]
    assert second_command["request_id"] == request_id
    assert second_command["manifest"] != first_command["manifest"]

    worker.message.emit(
        {
            "type": "write_result",
            "ok": True,
            "request_id": request_id,
            "kind": "text",
            "sequence": 82,
        }
    )
    assert results == [(ClipboardWriteReceipt(request_id, ClipKind.TEXT, 82), "")]
    controller.stop()
    repository.close()


def test_windows_preparing_preemption_still_allows_one_native_transient_replay(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    original_prepare = system_module._prepare_windows_write_artifacts
    first_started = threading.Event()
    release_first = threading.Event()
    call_count = [0]

    def controlled_prepare(ipc_dir, item, request_id, session_id):
        call_count[0] += 1
        if call_count[0] == 1:
            first_started.set()
            assert release_first.wait(2)
        return original_prepare(ipc_dir, item, request_id, session_id)

    monkeypatch.setattr(
        system_module,
        "_prepare_windows_write_artifacts",
        controlled_prepare,
    )
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(FakeClipboard(), repository, AppSettings, lambda: "Source")
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    results: list[tuple[ClipboardWriteReceipt | None, str]] = []

    controller.request_write(
        ClipItem("text", ClipKind.TEXT, "hash", 1, 1, text="payload"),
        lambda receipt, error: results.append((receipt, error)),
    )
    qtbot.waitUntil(first_started.is_set, timeout=1_000)
    request_id = next(iter(controller._windows_pending_writes))

    worker.is_capture_pipeline_busy = True
    worker.message.emit({"type": "capture_materializing", "sequence": 84})
    worker.is_healthy = False
    worker.health_changed.emit(False)
    worker.session_id = "d" * 32
    worker.is_capture_pipeline_busy = False
    worker.is_healthy = True
    worker.health_changed.emit(True)
    release_first.set()

    qtbot.waitUntil(lambda: bool(worker.sent), timeout=1_000)
    assert worker.restarted == 1
    assert call_count[0] == 2
    assert worker.sent[-1]["request_id"] == request_id
    assert str(worker.sent[-1]["manifest"]).startswith(f"write-manifest-{'d' * 32}-")
    assert not tuple(controller._windows_ipc_dir.glob(f"write-*-{'a' * 32}-*"))

    worker.message.emit(
        {
            "type": "write_result",
            "ok": False,
            "request_id": request_id,
            "kind": "text",
            "sequence": 85,
            "code": "clipboard_busy",
            "error": "clipboard busy",
        }
    )
    qtbot.waitUntil(
        lambda: len(
            [command for command in worker.sent if command["type"] == "write_clipboard"]
        )
        == 2,
        timeout=1_000,
    )
    assert call_count[0] == 3
    assert worker.sent[-1]["request_id"] == request_id

    worker.message.emit(
        {
            "type": "write_result",
            "ok": True,
            "request_id": request_id,
            "kind": "text",
            "sequence": 86,
        }
    )
    assert results == [(ClipboardWriteReceipt(request_id, ClipKind.TEXT, 86), "")]
    controller.stop()
    repository.close()


def test_windows_preemption_during_transient_replay_still_reaches_second_native_attempt(
    qtbot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    original_prepare = system_module._prepare_windows_write_artifacts
    second_started = threading.Event()
    release_second = threading.Event()
    call_count = [0]

    def controlled_prepare(ipc_dir, item, request_id, session_id):
        call_count[0] += 1
        if call_count[0] == 2:
            second_started.set()
            assert release_second.wait(2)
        return original_prepare(ipc_dir, item, request_id, session_id)

    monkeypatch.setattr(
        system_module,
        "_prepare_windows_write_artifacts",
        controlled_prepare,
    )
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(FakeClipboard(), repository, AppSettings, lambda: "Source")
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    results: list[tuple[ClipboardWriteReceipt | None, str]] = []

    controller.request_write(
        ClipItem("text", ClipKind.TEXT, "hash", 1, 1, text="payload"),
        lambda receipt, error: results.append((receipt, error)),
    )
    qtbot.waitUntil(lambda: bool(worker.sent), timeout=1_000)
    request_id = str(worker.sent[-1]["request_id"])
    worker.message.emit(
        {
            "type": "write_result",
            "ok": False,
            "request_id": request_id,
            "kind": "text",
            "sequence": 90,
            "code": "clipboard_busy",
            "error": "clipboard busy",
        }
    )
    qtbot.waitUntil(second_started.is_set, timeout=1_000)

    worker.is_capture_pipeline_busy = True
    worker.message.emit({"type": "capture_materializing", "sequence": 90})
    worker.is_healthy = False
    worker.health_changed.emit(False)
    worker.session_id = "e" * 32
    worker.is_capture_pipeline_busy = False
    worker.is_healthy = True
    worker.health_changed.emit(True)
    release_second.set()

    qtbot.waitUntil(
        lambda: len(
            [command for command in worker.sent if command["type"] == "write_clipboard"]
        )
        == 2,
        timeout=1_000,
    )
    assert call_count[0] == 3
    assert worker.sent[-1]["request_id"] == request_id
    assert str(worker.sent[-1]["manifest"]).startswith(f"write-manifest-{'e' * 32}-")

    worker.message.emit(
        {
            "type": "write_result",
            "ok": True,
            "request_id": request_id,
            "kind": "text",
            "sequence": 91,
        }
    )
    assert results == [(ClipboardWriteReceipt(request_id, ClipKind.TEXT, 91), "")]
    controller.stop()
    repository.close()


def test_windows_verify_accepts_broker_attested_current_sequence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(FakeClipboard(), repository, AppSettings, lambda: "Source")
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    receipt = ClipboardWriteReceipt("c" * 32, ClipKind.IMAGE, 51)
    results: list[tuple[bool, str]] = []

    controller.request_verify(receipt, lambda ok, error: results.append((ok, error)))
    assert worker.sent[-1] == {
        "type": "verify_clipboard",
        "request_id": receipt.request_id,
        "kind": "image",
        "sequence": 51,
    }
    worker.message.emit(
        {
            "type": "verify_result",
            "ok": True,
            "request_id": receipt.request_id,
            "kind": "image",
            "sequence": 52,
        }
    )
    assert results == [(True, "")]
    assert controller._last_sequence == 52
    assert controller._suppressed_sequence == 52
    worker.message.emit(
        {
            "type": "verify_result",
            "ok": True,
            "request_id": receipt.request_id,
            "kind": "image",
            "sequence": 51,
        }
    )
    assert results == [(True, "")]
    controller.stop()
    repository.close()


def test_windows_verify_nacks_capture_pipeline_without_queueing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(FakeClipboard(), repository, AppSettings, lambda: "Source")
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    receipt = ClipboardWriteReceipt("e" * 32, ClipKind.IMAGE, 91)
    results: list[tuple[bool, str]] = []
    worker.is_capture_pipeline_busy = True

    controller.request_verify(receipt, lambda ok, error: results.append((ok, error)))

    assert results == [(False, "剪贴板内容已变化")]
    assert worker.sent == []
    assert controller._windows_pending_verifications == {}
    controller.stop()
    repository.close()


def test_windows_capture_event_nacks_inflight_verify_and_ignores_late_result(
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(FakeClipboard(), repository, AppSettings, lambda: "Source")
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    receipt = ClipboardWriteReceipt("f" * 32, ClipKind.IMAGE, 92)
    results: list[tuple[bool, str]] = []

    controller.request_verify(receipt, lambda ok, error: results.append((ok, error)))
    assert worker.sent[-1]["type"] == "verify_clipboard"
    worker.is_capture_pipeline_busy = True
    worker.message.emit({"type": "capture_started", "sequence": 93})
    worker.message.emit(
        {
            "type": "verify_result",
            "ok": True,
            "request_id": receipt.request_id,
            "kind": "image",
            "sequence": receipt.sequence,
        }
    )

    assert results == [(False, "剪贴板内容已变化")]
    controller.stop()
    repository.close()


def test_windows_verify_reports_changed_sequence_without_waiting_for_timeout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(FakeClipboard(), repository, AppSettings, lambda: "Source")
    controller.start()
    worker = FakeWorkerSupervisor.instances[-1]
    receipt = ClipboardWriteReceipt("d" * 32, ClipKind.IMAGE, 61)
    results: list[tuple[bool, str]] = []

    controller.request_verify(receipt, lambda ok, error: results.append((ok, error)))
    worker.message.emit(
        {
            "type": "verify_result",
            "ok": False,
            "request_id": receipt.request_id,
            "kind": "image",
            "sequence": 62,
            "code": "verification_failed",
            "error": "clipboard changed",
        }
    )

    assert results == [(False, "clipboard changed")]
    controller.stop()
    repository.close()


def test_async_image_capture(qtbot, tmp_path: Path) -> None:
    clipboard = FakeClipboard()
    controller, repository = make_controller(tmp_path, clipboard)
    captured: list[ClipItem] = []
    controller.captured.connect(captured.append)
    image = QImage(3, 2, QImage.Format.Format_ARGB32)
    image.fill(0x88FF0000)
    mime = QMimeData()
    mime.setImageData(image)
    clipboard.set_external(mime)
    qtbot.waitUntil(lambda: bool(captured), timeout=3_000)
    assert captured[0].kind is ClipKind.IMAGE
    assert (captured[0].width, captured[0].height) == (3, 2)
    assert QImage(captured[0].image_path).pixelColor(0, 0).alpha() == 136
    controller.stop()
    repository.close()


def test_native_windows_manifest_task_stores_text_and_removes_ipc_file(tmp_path: Path) -> None:
    repository = HistoryRepository(tmp_path)
    ipc_dir = tmp_path / "ipc"
    ipc_dir.mkdir()
    manifest = {
        "protocol": 1,
        "sequence": 17,
        "kind": "text",
        "source_app": "Editor",
        "text": "isolated clipboard text",
    }
    manifest_path = ipc_dir / "manifest-17-test.json"
    encoded = json.dumps(manifest).encode()
    manifest_path.write_bytes(encoded)
    stored: list[tuple[ClipItem, int]] = []
    task = _NativeClipboardStoreTask(
        repository,
        ipc_dir,
        manifest_path.name,
        len(encoded),
        17,
        "text",
        AppSettings(),
        store=True,
    )
    task.signals.stored.connect(lambda item, sequence: stored.append((item, sequence)))

    task.run()

    assert stored[0][0].text == "isolated clipboard text"
    assert stored[0][0].source_app == "Editor"
    assert stored[0][1] == 17
    assert not manifest_path.exists()
    repository.close()


def test_native_windows_manifest_task_discards_stale_capture_epoch(tmp_path: Path) -> None:
    repository = HistoryRepository(tmp_path)
    ipc_dir = tmp_path / "ipc"
    ipc_dir.mkdir()
    manifest = {
        "protocol": 1,
        "sequence": 19,
        "kind": "text",
        "source_app": "Editor",
        "text": "must not return after clear",
    }
    manifest_path = ipc_dir / "manifest-19-test.json"
    encoded = json.dumps(manifest).encode()
    manifest_path.write_bytes(encoded)
    consumed: list[int] = []
    task = _NativeClipboardStoreTask(
        repository,
        ipc_dir,
        manifest_path.name,
        len(encoded),
        19,
        "text",
        AppSettings(),
        store=True,
        store_guard=lambda: False,
    )
    task.signals.consumed.connect(consumed.append)

    task.run()

    assert consumed == [19]
    assert repository.list_items() == []
    assert not manifest_path.exists()
    repository.close()


def test_native_windows_manifest_task_converts_image_and_cleans_payload(tmp_path: Path) -> None:
    repository = HistoryRepository(tmp_path)
    ipc_dir = tmp_path / "ipc"
    ipc_dir.mkdir()
    image = QImage(3, 2, QImage.Format.Format_ARGB32)
    image.fill(0xFF336699)
    payload_path = ipc_dir / "clip-18-test.png"
    assert image.save(str(payload_path), "PNG")
    manifest = {
        "protocol": 1,
        "sequence": 18,
        "kind": "image",
        "source_app": "Capture",
        "payload_file": payload_path.name,
        "encoding": "png",
        "bytes": payload_path.stat().st_size,
        "width": 3,
        "height": 2,
    }
    manifest_path = ipc_dir / "manifest-18-test.json"
    encoded = json.dumps(manifest).encode()
    manifest_path.write_bytes(encoded)
    stored: list[ClipItem] = []
    task = _NativeClipboardStoreTask(
        repository,
        ipc_dir,
        manifest_path.name,
        len(encoded),
        18,
        "image",
        AppSettings(),
        store=True,
    )
    task.signals.stored.connect(lambda item, _sequence: stored.append(item))

    task.run()

    assert stored[0].kind is ClipKind.IMAGE
    assert (stored[0].width, stored[0].height) == (3, 2)
    assert not manifest_path.exists()
    assert not payload_path.exists()
    repository.close()


def test_clipboard_capture_waits_for_owner_and_retries_without_losing_item(qtbot, tmp_path: Path) -> None:
    clipboard = FlakyClipboard(failures=2)
    controller, repository = make_controller(tmp_path, clipboard)
    captured: list[ClipItem] = []
    failures: list[str] = []
    controller.captured.connect(captured.append)
    controller.failed.connect(failures.append)
    mime = QMimeData()
    mime.setText("available after owner releases it")

    clipboard.set_external(mime)

    qtbot.waitUntil(lambda: bool(captured), timeout=2_000)
    assert captured[0].text == "available after owner releases it"
    assert clipboard.read_attempts == 3
    assert failures == []
    controller.stop()
    repository.close()


def test_clipboard_capture_coalesces_rapid_changes_to_latest_sequence(qtbot, tmp_path: Path) -> None:
    clipboard = FakeClipboard()
    controller, repository = make_controller(tmp_path, clipboard)
    captured: list[ClipItem] = []
    controller.captured.connect(captured.append)
    first = QMimeData()
    first.setText("first")
    latest = QMimeData()
    latest.setText("latest")

    clipboard.set_external(first)
    clipboard.set_external(latest)

    qtbot.waitUntil(lambda: bool(captured), timeout=1_000)
    assert [item.text for item in captured] == ["latest"]
    controller.stop()
    repository.close()


def test_delayed_clipboard_capture_keeps_source_from_change_notification(qtbot, tmp_path: Path) -> None:
    clipboard = FakeClipboard()
    source = ["Copying app"]
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(clipboard, repository, AppSettings, lambda: source[0])
    controller._sequence_number = lambda: clipboard.sequence  # type: ignore[method-assign]
    controller._last_sequence = clipboard.sequence
    controller.start()
    mime = QMimeData()
    mime.setText("captured before switching")

    clipboard.set_external(mime)
    source[0] = "Next app"

    qtbot.waitUntil(lambda: bool(repository.list_items()), timeout=1_000)
    assert repository.list_items()[0].source_app == "Copying app"
    controller.stop()
    repository.close()


def test_clipboard_pause_secret_poll_and_write_formats(qtbot, tmp_path: Path) -> None:
    clipboard = FakeClipboard()
    settings = AppSettings(capture_enabled=False)
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(clipboard, repository, lambda: settings, lambda: "Source")
    controller._sequence_number = lambda: clipboard.sequence  # type: ignore[method-assign]
    controller._last_sequence = clipboard.sequence
    controller.start()
    text = QMimeData()
    text.setText("paused")
    clipboard.set_external(text)
    assert not repository.list_items()

    settings.capture_enabled = True
    secret = QMimeData()
    secret.setText("password")
    secret.setData("org.nspasteboard.ConcealedType", b"1")
    clipboard.set_external(secret)
    assert not repository.list_items()

    text.setText("captured")
    clipboard.mime = text
    clipboard.sequence += 1
    controller._poll_native_sequence()
    qtbot.waitUntil(lambda: bool(repository.list_items()), timeout=1_000)
    assert repository.list_items()[0].text == "captured"
    controller.sync_cursor()
    controller._poll_native_sequence()

    files = ClipItem("f", ClipKind.FILES, "f", 1, 1, files=(str(tmp_path),))
    assert controller.write_item(files)
    assert clipboard.mime.hasUrls()
    bad_image = ClipItem("i", ClipKind.IMAGE, "i", 1, 1, image_path=str(tmp_path / "missing.png"))
    assert not controller.write_item(bad_image)
    controller.stop()
    repository.close()


def test_sensitive_clipboard_opt_out_always_wins() -> None:
    allowed = QMimeData()
    allowed.setData("CanIncludeInClipboardHistory", (1).to_bytes(4, "little"))
    assert not ClipboardController._is_secret(allowed)

    denied = QMimeData()
    denied.setData("CanIncludeInClipboardHistory", bytes(4))
    assert ClipboardController._is_secret(denied)

    combined = QMimeData()
    combined.setData("CanIncludeInClipboardHistory", (1).to_bytes(4, "little"))
    combined.setData("ExcludeClipboardContentFromMonitorProcessing", b"1")
    assert ClipboardController._is_secret(combined)


def test_hotkey_service_listener_lifecycle_and_failure(monkeypatch) -> None:
    events: list[str] = []

    class FakeListener:
        IS_TRUSTED = True

        def __init__(self, on_press, on_release) -> None:
            self.on_press, self.on_release = on_press, on_release
            self.stopped = False

        def start(self) -> None:
            return None

        def stop(self) -> None:
            self.stopped = True

    keyboard = types.SimpleNamespace(Listener=FakeListener)
    monkeypatch.setitem(sys.modules, "pynput", types.SimpleNamespace(keyboard=keyboard))
    service = GlobalHotkeyService(lambda _context: events.append("hit"), events.append)
    service.start(AppSettings())
    listener = service._listener
    assert service.is_running
    class FakeKey:
        char = None

        def __str__(self) -> str:
            return "Key.ctrl"

    key = FakeKey()
    listener.on_press(key)
    listener.on_release(key)
    listener.on_press(key)
    listener.on_release(key)
    assert events == ["hit"]
    service.stop()
    assert listener.stopped
    assert not service.is_running

    class BrokenListener:
        def __init__(self, **_kwargs) -> None:
            raise RuntimeError("denied")

    monkeypatch.setitem(
        sys.modules, "pynput", types.SimpleNamespace(keyboard=types.SimpleNamespace(Listener=BrokenListener))
    )
    service.start(AppSettings())
    assert "denied" in events[-1]
    assert _canonical_key("Key.cmd_l") == "meta"


def test_hotkey_service_detects_listener_that_stopped_after_start(monkeypatch) -> None:
    class FakeListener:
        IS_TRUSTED = True

        def __init__(self, **_kwargs) -> None:
            self.running = False

        def start(self) -> None:
            self.running = True

        def stop(self) -> None:
            self.running = False

    monkeypatch.setitem(
        sys.modules,
        "pynput",
        types.SimpleNamespace(keyboard=types.SimpleNamespace(Listener=FakeListener)),
    )
    service = GlobalHotkeyService(lambda _context: None, lambda _message: None)
    service.start(AppSettings())
    assert service.is_running

    service._listener.running = False

    assert not service.is_running
    service.stop()


def test_hotkey_service_detects_listener_thread_that_died_with_running_flag(monkeypatch) -> None:
    class FakeListener:
        running = True

        def __init__(self, **_kwargs) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            self.running = False

        def is_alive(self) -> bool:
            return False

    monkeypatch.setitem(
        sys.modules,
        "pynput",
        types.SimpleNamespace(keyboard=types.SimpleNamespace(Listener=FakeListener)),
    )
    service = GlobalHotkeyService(lambda _context: None, lambda _message: None)
    service.start(AppSettings())

    assert not service.is_running
    service.stop()


def test_windows_hotkey_service_reports_ready_and_structured_registration_failure(
    monkeypatch,
) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    ready_specs: list[str] = []
    structured_failures: list[tuple[str, str]] = []
    display_failures: list[str] = []
    service = GlobalHotkeyService(
        lambda _context: None,
        display_failures.append,
        ready_specs.append,
        lambda spec, message: structured_failures.append((spec, message)),
    )
    service.start(AppSettings(hotkey="combo:ctrl+alt+k"))
    worker = FakeWorkerSupervisor.instances[-1]

    worker.message.emit({"type": "ready"})
    worker.message.emit(
        {
            "type": "error",
            "fatal": True,
            "code": "registration_failed",
            "message": "shortcut already registered",
        }
    )
    worker.failed.emit("shortcut already registered")

    assert ready_specs == ["combo:ctrl+alt+k"]
    assert structured_failures == [
        ("combo:ctrl+alt+k", "shortcut already registered"),
    ]
    assert display_failures == []

    # The supervisor emits the same fatal error through its display-only
    # channel after the structured message. Only that paired duplicate is
    # suppressed; a later independent failure must still reach the user.
    worker.failed.emit("shortcut already registered")
    assert display_failures == ["shortcut already registered"]
    service.stop()


def test_windows_services_use_supervised_native_workers(monkeypatch, tmp_path: Path) -> None:
    FakeWorkerSupervisor.instances.clear()
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module, "WindowsWorkerSupervisor", FakeWorkerSupervisor)
    failures: list[str] = []
    contexts: list[HotkeyActivationContext | None] = []
    hotkey = GlobalHotkeyService(contexts.append, failures.append)

    hotkey.start(AppSettings(hotkey="double:shift", double_tap_interval_ms=360))

    hotkey_worker = FakeWorkerSupervisor.instances[-1]
    assert hotkey_worker.role == "hotkey"
    assert hotkey_worker.arguments() == ["--hotkey", WINDOWS_DEFAULT_HOTKEY]
    assert len(failures) == 1
    assert "双修饰键" in failures[0]
    assert "Ctrl+Shift+Space" in failures[0]
    hotkey_worker.message.emit(
        {
            "type": "hotkey",
            "target_hwnd": 404,
            "target_thread_id": 7001,
            "target_process_id": 8001,
            "focus_hwnd": 405,
            "focus_thread_id": 7002,
            "focus_process_id": 8001,
            "foreground_granted": True,
        }
    )
    assert contexts == [
        HotkeyActivationContext(
            target_window=404,
            target_thread_id=7001,
            target_process_id=8001,
            focus_window=405,
            focus_thread_id=7002,
            focus_process_id=8001,
            foreground_granted=True,
        )
    ]
    assert hotkey.is_running

    failure_count = len(failures)
    hotkey.start(AppSettings(hotkey="combo:alt+space", double_tap_interval_ms=777))
    custom_hotkey_worker = FakeWorkerSupervisor.instances[-1]
    assert custom_hotkey_worker.role == "hotkey"
    assert custom_hotkey_worker.arguments() == ["--hotkey", "combo:alt+space"]
    assert len(failures) == failure_count

    monkeypatch.setattr(ClipboardController, "_sequence_number", lambda _self: 77)
    clipboard = FakeClipboard()
    repository = HistoryRepository(tmp_path)
    controller = ClipboardController(clipboard, repository, AppSettings, lambda: "ignored")
    controller.start()

    clipboard_worker = FakeWorkerSupervisor.instances[-1]
    assert clipboard_worker.role == "clipboard"
    assert clipboard_worker.arguments() == [
        "--ipc-dir",
        str(tmp_path / "ipc"),
        "--after-sequence",
        "77",
    ]
    controller.stop()
    hotkey.stop()
    assert clipboard_worker.stopped == 1
    assert hotkey_worker.stopped == 1
    assert custom_hotkey_worker.stopped == 1
    repository.close()


def test_windows_panel_activation_uses_and_verifies_foreground_window(monkeypatch) -> None:
    foreground = [101]
    calls: list[tuple[str, int]] = []

    def handle_value(value) -> int:
        return int(value.value if hasattr(value, "value") else value)

    def show_window(hwnd, command) -> int:
        calls.append(("show", handle_value(hwnd)))
        assert command == 5
        return 1

    def bring_to_top(hwnd) -> int:
        calls.append(("top", handle_value(hwnd)))
        return 1

    def set_foreground(hwnd) -> int:
        foreground[0] = handle_value(hwnd)
        calls.append(("foreground", foreground[0]))
        return 1

    def get_foreground() -> int:
        return foreground[0]

    user32 = types.SimpleNamespace(
        IsWindow=lambda _handle: True,
        ShowWindow=show_window,
        BringWindowToTop=bring_to_top,
        SetForegroundWindow=set_foreground,
        GetForegroundWindow=get_foreground,
    )
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module.os, "getpid", lambda: 3003)
    monkeypatch.setattr(
        system_module,
        "_windows_window_identity",
        lambda _api, _hwnd: (33, 3003),
    )
    monkeypatch.setattr(system_module.ctypes, "windll", types.SimpleNamespace(user32=user32), raising=False)

    assert PlatformBridge.request_window_activation(202)
    assert PlatformBridge.foreground_window_id() == 202
    assert calls == [("show", 202), ("top", 202), ("foreground", 202)]


def test_windows_panel_activation_uses_one_shot_helper_on_denial(monkeypatch) -> None:
    foreground = [101]
    helper_allowed = [True]
    calls: list[tuple[str, int, int | bool | None]] = []

    def handle_value(value) -> int:
        return int(value.value if hasattr(value, "value") else value)

    def set_foreground(hwnd) -> int:
        identifier = handle_value(hwnd)
        calls.append(("foreground", identifier, False))
        return 0

    def run_focus_helper(**arguments) -> bool:
        calls.append(("helper", arguments["target_window"], arguments["target_process_id"]))
        if helper_allowed[0]:
            foreground[0] = arguments["target_window"]
        return helper_allowed[0]

    user32 = types.SimpleNamespace(
        IsWindow=lambda _handle: True,
        ShowWindow=lambda handle, command: calls.append(
            ("show", handle_value(handle), command)
        )
        or 1,
        BringWindowToTop=lambda handle: calls.append(
            ("top", handle_value(handle), None)
        )
        or 1,
        SetForegroundWindow=set_foreground,
        GetForegroundWindow=lambda: foreground[0],
    )
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module.os, "getpid", lambda: 3003)
    monkeypatch.setattr(
        system_module,
        "_windows_window_identity",
        lambda _api, _hwnd: (33, 3003),
    )
    monkeypatch.setattr(
        system_module,
        "_run_windows_focus_helper",
        run_focus_helper,
    )
    monkeypatch.setattr(
        system_module.ctypes,
        "windll",
        types.SimpleNamespace(user32=user32),
        raising=False,
    )

    assert PlatformBridge.request_window_activation(202)
    assert calls == [
        ("show", 202, 5),
        ("top", 202, None),
        ("foreground", 202, False),
        ("helper", 202, 3003),
    ]

    foreground[0] = 101
    helper_allowed[0] = False
    calls.clear()
    assert not PlatformBridge.request_window_activation(202)
    assert calls[-1] == ("helper", 202, 3003)


def test_windows_focus_helper_command_is_bounded_and_complete(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(
        system_module,
        "windows_worker_command",
        lambda role, arguments: (
            "ClipSoon.exe",
            [f"--windows-helper={role}", *arguments],
        ),
    )

    def run(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(system_module.subprocess, "run", run)

    assert system_module._run_windows_focus_helper(
        mode="target",
        target_window=303,
        target_thread_id=7001,
        target_process_id=8001,
        focus_window=808,
        focus_thread_id=7002,
        focus_process_id=8100,
    )
    command, options = calls[0]
    assert command == [
        "ClipSoon.exe",
        "--windows-helper=focus",
        "--mode",
        "target",
        "--target-hwnd",
        "303",
        "--target-thread-id",
        "7001",
        "--target-process-id",
        "8001",
        "--focus-hwnd",
        "808",
        "--focus-thread-id",
        "7002",
        "--focus-process-id",
        "8100",
    ]
    assert options["timeout"] == system_module._WINDOWS_FOCUS_HELPER_TIMEOUT_SECONDS


def test_windows_focus_helper_timeout_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        system_module,
        "windows_worker_command",
        lambda _role, arguments: ("ClipSoon.exe", list(arguments)),
    )

    def run(_command, **_options):
        raise subprocess.TimeoutExpired("ClipSoon.exe", 1.5)

    monkeypatch.setattr(system_module.subprocess, "run", run)

    assert not system_module._run_windows_focus_helper(
        mode="panel",
        target_window=202,
        target_process_id=3003,
    )


def test_windows_target_activation_preserves_non_minimized_window_state(monkeypatch) -> None:
    calls: list[tuple[str, int, int | None]] = []

    def handle_value(value) -> int:
        return int(value.value if hasattr(value, "value") else value)

    user32 = types.SimpleNamespace(
        IsWindow=lambda _handle: True,
        IsIconic=lambda _handle: False,
        ShowWindow=lambda handle, command: calls.append(("show", handle_value(handle), command)) or 1,
        BringWindowToTop=lambda handle: calls.append(("top", handle_value(handle), None)) or 1,
        SetForegroundWindow=lambda handle: calls.append(("foreground", handle_value(handle), None)) or 1,
        GetForegroundWindow=lambda: 303,
    )
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(
        system_module.ctypes,
        "windll",
        types.SimpleNamespace(user32=user32),
        raising=False,
    )
    monkeypatch.setattr(
        system_module,
        "_windows_window_identity",
        lambda _api, _hwnd: (7001, 8001),
    )
    monkeypatch.setattr(
        system_module,
        "_windows_focus_window",
        lambda _api, _thread: 0,
    )

    assert ForegroundTargetHandle("windows", 303, "Editor").activate()
    assert calls == [("top", 303, None), ("foreground", 303, None)]


def test_windows_target_activation_restores_minimized_window(monkeypatch) -> None:
    calls: list[tuple[str, int, int | None]] = []

    def handle_value(value) -> int:
        return int(value.value if hasattr(value, "value") else value)

    user32 = types.SimpleNamespace(
        IsWindow=lambda _handle: True,
        IsIconic=lambda _handle: True,
        ShowWindow=lambda handle, command: calls.append(("show", handle_value(handle), command)) or 1,
        BringWindowToTop=lambda handle: calls.append(("top", handle_value(handle), None)) or 1,
        SetForegroundWindow=lambda handle: calls.append(("foreground", handle_value(handle), None)) or 1,
        GetForegroundWindow=lambda: 303,
    )
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(
        system_module.ctypes,
        "windll",
        types.SimpleNamespace(user32=user32),
        raising=False,
    )
    monkeypatch.setattr(
        system_module,
        "_windows_window_identity",
        lambda _api, _hwnd: (7001, 8001),
    )
    monkeypatch.setattr(
        system_module,
        "_windows_focus_window",
        lambda _api, _thread: 0,
    )

    assert ForegroundTargetHandle("windows", 303, "Editor").activate()
    assert calls == [
        ("show", 303, 9),
        ("top", 303, None),
        ("foreground", 303, None),
    ]


def test_windows_target_without_snapshot_resolves_identity_for_helper(
    monkeypatch,
) -> None:
    foreground = [999]
    focus = [0]
    helper_arguments: list[dict[str, object]] = []
    user32 = types.SimpleNamespace(
        IsWindow=lambda _handle: True,
        IsIconic=lambda _handle: False,
        BringWindowToTop=lambda _handle: 1,
        SetForegroundWindow=lambda _handle: 0,
    )
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(
        system_module.ctypes,
        "windll",
        types.SimpleNamespace(user32=user32),
        raising=False,
    )
    monkeypatch.setattr(
        system_module,
        "_windows_window_identity",
        lambda _api, hwnd: (7001, 8001) if hwnd == 303 else (7002, 8001),
    )
    monkeypatch.setattr(
        system_module,
        "_windows_foreground_window",
        lambda: foreground[0],
    )
    monkeypatch.setattr(
        system_module,
        "_windows_focus_window",
        lambda _api, _thread: focus[0],
    )
    monkeypatch.setattr(
        system_module,
        "_windows_root_window",
        lambda _api, _hwnd: 303,
    )

    def run_helper(**arguments) -> bool:
        helper_arguments.append(arguments)
        foreground[0] = 303
        return True

    monkeypatch.setattr(system_module, "_run_windows_focus_helper", run_helper)
    target = ForegroundTargetHandle("windows", 303, "Editor")

    assert target.activate()
    assert (target.target_thread_id, target.target_process_id) == (7001, 8001)
    assert helper_arguments == [
        {
            "mode": "target",
            "target_window": 303,
            "target_thread_id": 7001,
            "target_process_id": 8001,
        }
    ]
    assert not target.is_active()
    focus[0] = 909
    assert target.is_active()
    assert (target.focus_window, target.focus_thread_id) == (909, 7002)


def test_windows_target_activation_restores_and_verifies_captured_focus(
    monkeypatch,
) -> None:
    calls: list[tuple[str, int, int | None]] = []
    foreground = [999]
    focus = [999]

    def number(value) -> int:
        return int(value.value if hasattr(value, "value") else value)

    def set_foreground(handle) -> int:
        foreground[0] = number(handle)
        calls.append(("foreground", foreground[0], None))
        return 1

    user32 = types.SimpleNamespace(
        IsWindow=lambda _handle: True,
        IsIconic=lambda _handle: False,
        BringWindowToTop=lambda handle: calls.append(
            ("top", number(handle), None)
        )
        or 1,
        SetForegroundWindow=set_foreground,
    )
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(
        system_module.ctypes,
        "windll",
        types.SimpleNamespace(user32=user32),
        raising=False,
    )
    monkeypatch.setattr(
        system_module,
        "_windows_window_identity",
        lambda _api, hwnd: (7001, 8001) if hwnd == 303 else (7002, 8001),
    )
    monkeypatch.setattr(
        system_module,
        "_windows_root_window",
        lambda _api, _hwnd: 303,
    )
    monkeypatch.setattr(
        system_module,
        "_windows_foreground_window",
        lambda: foreground[0],
    )
    monkeypatch.setattr(
        system_module,
        "_windows_focus_window",
        lambda _api, _thread: focus[0],
    )

    def restore_with_helper(**arguments) -> bool:
        calls.append(
            (
                "helper",
                arguments["target_window"],
                arguments["focus_window"],
            )
        )
        focus[0] = int(arguments["focus_window"])
        return True

    monkeypatch.setattr(
        system_module,
        "_run_windows_focus_helper",
        restore_with_helper,
    )
    target = ForegroundTargetHandle(
        "windows",
        303,
        "WeLink",
        target_thread_id=7001,
        target_process_id=8001,
        focus_window=808,
        focus_thread_id=7002,
        focus_process_id=8001,
    )

    assert target.activate()
    assert target.is_active()
    assert calls == [
        ("top", 303, None),
        ("foreground", 303, None),
        ("helper", 303, 808),
    ]


def test_windows_target_adopts_recreated_focus_within_same_root(monkeypatch) -> None:
    current_focus = [809]
    queried_threads: list[int] = []
    user32 = types.SimpleNamespace(IsWindow=lambda _handle: True)
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(
        system_module.ctypes,
        "windll",
        types.SimpleNamespace(user32=user32),
        raising=False,
    )
    monkeypatch.setattr(system_module, "_windows_foreground_window", lambda: 303)
    monkeypatch.setattr(
        system_module,
        "_windows_window_identity",
        lambda _api, hwnd: {
            303: (7001, 8001),
            808: (7002, 8001),
            809: (7003, 8001),
        }[hwnd],
    )
    monkeypatch.setattr(
        system_module,
        "_windows_root_window",
        lambda _api, _hwnd: 303,
    )
    def focus_window(_api, thread_id: int) -> int:
        queried_threads.append(thread_id)
        return current_focus[0]

    monkeypatch.setattr(system_module, "_windows_focus_window", focus_window)
    target = ForegroundTargetHandle(
        "windows",
        303,
        "WeLink",
        7001,
        8001,
        808,
        7002,
        8001,
    )

    assert target.is_active()
    assert (target.focus_window, target.focus_thread_id, target.focus_process_id) == (
        809,
        7003,
        8001,
    )
    assert 0 in queried_threads


def test_windows_target_rejects_reused_top_level_handle(monkeypatch) -> None:
    user32 = types.SimpleNamespace(IsWindow=lambda _handle: True)
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(
        system_module.ctypes,
        "windll",
        types.SimpleNamespace(user32=user32),
        raising=False,
    )
    monkeypatch.setattr(
        system_module,
        "_windows_window_identity",
        lambda _api, _hwnd: (7001, 9999),
    )

    assert not ForegroundTargetHandle(
        "windows",
        303,
        "WeLink",
        target_thread_id=7001,
        target_process_id=8001,
    ).activate()


def test_windows_paste_uses_one_checked_send_input_batch(monkeypatch) -> None:
    calls: list[list[tuple[int, int, int]]] = []

    def send_input(count, events, size) -> int:
        calls.append(
            [
                (
                    int(events[index].type),
                    int(events[index].keyboard.wVk),
                    int(events[index].keyboard.dwFlags),
                )
                for index in range(int(count))
            ]
        )
        assert int(size) == ctypes.sizeof(system_module._WindowsInput)
        return int(count)

    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(
        system_module.ctypes,
        "windll",
        types.SimpleNamespace(
            user32=types.SimpleNamespace(SendInput=send_input)
        ),
        raising=False,
    )

    assert PynputPasteAdapter().paste()
    assert calls == [
        [
            (1, 0x11, 0),
            (1, 0x56, 0),
            (1, 0x56, 0x0002),
            (1, 0x11, 0x0002),
        ]
    ]


def test_windows_partial_send_input_fails_and_releases_keys(monkeypatch) -> None:
    call_sizes: list[int] = []

    def send_input(count, _events, _size) -> int:
        call_sizes.append(int(count))
        return 2

    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(
        system_module.ctypes,
        "windll",
        types.SimpleNamespace(
            user32=types.SimpleNamespace(SendInput=send_input)
        ),
        raising=False,
    )

    assert not PynputPasteAdapter().paste()
    assert call_sizes == [4, 2]


def test_windows_primary_button_detects_short_click_between_polls(monkeypatch) -> None:
    user32 = types.SimpleNamespace(GetAsyncKeyState=lambda _key: 0x0001)
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(
        system_module.ctypes,
        "windll",
        types.SimpleNamespace(user32=user32),
        raising=False,
    )

    assert PlatformBridge.primary_button_down()


class FakeTarget:
    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.activations = 0

    def activate(self) -> bool:
        self.activations += 1
        return self.active

    def is_active(self) -> bool:
        return self.active


class FakeWriter:
    def __init__(self, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.writes: list[ClipItem] = []
        self.verifications: list[ClipboardWriteReceipt] = []

    def write_item(self, item: ClipItem) -> bool:
        self.writes.append(item)
        return self.succeeds

    def request_write(self, item: ClipItem, callback) -> None:
        if self.write_item(item):
            callback(ClipboardWriteReceipt("a" * 32, item.kind, len(self.writes)), "")
        else:
            callback(None, "")

    def request_verify(self, receipt: ClipboardWriteReceipt, callback) -> None:
        self.verifications.append(receipt)
        callback(True, "")


class DeferredWriter:
    def __init__(self) -> None:
        self.writes: list[ClipItem] = []
        self.write_callbacks: list = []
        self.verify_callbacks: list = []

    def request_write(self, item: ClipItem, callback) -> None:
        self.writes.append(item)
        self.write_callbacks.append(callback)

    def request_verify(self, receipt: ClipboardWriteReceipt, callback) -> None:
        self.verify_callbacks.append((receipt, callback))


class FakeRepository:
    def __init__(self) -> None:
        self.used: list[str] = []

    def validate_file_item(self, _item_id: str) -> ClipItem | None:
        return None

    def mark_used(self, item_id: str) -> None:
        self.used.append(item_id)


class FakePaste:
    def __init__(self, succeeds: bool = True) -> None:
        self.count = 0
        self.succeeds = succeeds

    def paste(self) -> bool:
        self.count += 1
        return self.succeeds


def test_selection_sender_success_and_activation_fallback(qtbot) -> None:
    clip = ClipItem("id", ClipKind.TEXT, "h", 1, 1, text="hello")
    writer, repository, paste = FakeWriter(), FakeRepository(), FakePaste()
    hidden: list[bool] = []
    sender = SelectionSender(writer, repository, paste, AppSettings, lambda: hidden.append(True))  # type: ignore[arg-type]
    finished: list[tuple[str, bool]] = []
    sender.finished.connect(lambda message, success: finished.append((message, success)))
    target = FakeTarget()
    target.kind = "windows"
    sender.send(clip, target)  # type: ignore[arg-type]
    qtbot.waitUntil(lambda: bool(finished), timeout=1_000)
    assert finished[-1] == ("已触发粘贴", True)
    assert repository.used == ["id"]
    assert hidden == [True]
    assert paste.count == 1

    failed = FakeTarget(active=False)
    sender.send(clip, failed)  # type: ignore[arg-type]
    qtbot.waitUntil(lambda: len(finished) == 2, timeout=1_000)
    assert finished[-1] == ("已复制，但无法恢复目标窗口", False)


def test_selection_sender_waits_for_write_ack_and_verify_before_paste(qtbot) -> None:
    clip = ClipItem("id", ClipKind.TEXT, "h", 1, 1, text="hello")
    writer, repository, paste = DeferredWriter(), FakeRepository(), FakePaste()
    target = FakeTarget()
    hidden: list[bool] = []
    finished: list[tuple[str, bool]] = []
    sender = SelectionSender(
        writer,  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        paste,  # type: ignore[arg-type]
        AppSettings,
        lambda: hidden.append(True),
    )
    sender.finished.connect(lambda message, success: finished.append((message, success)))

    sender.send(clip, target)  # type: ignore[arg-type]
    assert hidden == []
    assert target.activations == 0
    assert paste.count == 0

    receipt = ClipboardWriteReceipt("a" * 32, ClipKind.TEXT, 7)
    writer.write_callbacks[0](receipt, "")
    qtbot.waitUntil(lambda: bool(writer.verify_callbacks), timeout=1_000)
    assert hidden == [True]
    assert target.activations == 1
    assert paste.count == 0

    verify_callback = writer.verify_callbacks[0][1]
    verify_callback(True, "")
    verify_callback(True, "")  # duplicate/late ACK must not paste twice
    assert paste.count == 1
    assert finished == [("已发送", True)]


def test_selection_sender_waits_for_async_windows_focus_recreation(
    qtbot,
    monkeypatch,
) -> None:
    clip = ClipItem("id", ClipKind.TEXT, "h", 1, 1, text="hello")
    writer, paste = FakeWriter(), FakePaste()
    finished: list[tuple[str, bool]] = []

    class AsyncWindowsTarget(FakeTarget):
        kind = "windows"
        identifier = 303

        def __init__(self) -> None:
            super().__init__()
            self.focus_checks = 0

        def is_active(self) -> bool:
            self.focus_checks += 1
            return self.focus_checks >= 3

    target = AsyncWindowsTarget()
    monkeypatch.setattr(PlatformBridge, "foreground_window_id", lambda: 303)
    sender = SelectionSender(
        writer,  # type: ignore[arg-type]
        FakeRepository(),  # type: ignore[arg-type]
        paste,  # type: ignore[arg-type]
        AppSettings,
        lambda: None,
    )
    sender.finished.connect(lambda message, success: finished.append((message, success)))

    sender.send(clip, target)  # type: ignore[arg-type]

    qtbot.waitUntil(lambda: bool(finished), timeout=1_000)
    assert target.focus_checks >= 4
    assert paste.count == 1
    assert finished == [("已触发粘贴", True)]


def test_selection_sender_verify_failure_rewrites_once_then_cancels(qtbot) -> None:
    clip = ClipItem("id", ClipKind.IMAGE, "h", 1, 1, image_path="image.png")
    writer, paste = DeferredWriter(), FakePaste()
    finished: list[tuple[str, bool]] = []
    sender = SelectionSender(
        writer,  # type: ignore[arg-type]
        FakeRepository(),  # type: ignore[arg-type]
        paste,  # type: ignore[arg-type]
        AppSettings,
        lambda: None,
    )
    sender.finished.connect(lambda message, success: finished.append((message, success)))
    target = FakeTarget()

    sender.send(clip, target)  # type: ignore[arg-type]
    writer.write_callbacks[0](ClipboardWriteReceipt("a" * 32, ClipKind.IMAGE, 10), "")
    qtbot.waitUntil(lambda: len(writer.verify_callbacks) == 1, timeout=1_000)
    writer.verify_callbacks[0][1](False, "changed")
    assert writer.writes == [clip, clip]
    writer.write_callbacks[1](ClipboardWriteReceipt("b" * 32, ClipKind.IMAGE, 11), "")
    qtbot.waitUntil(lambda: len(writer.verify_callbacks) == 2, timeout=1_000)
    writer.verify_callbacks[1][1](False, "still changed")

    assert paste.count == 0
    assert len(finished) == 1
    assert not finished[0][1]
    assert "已取消自动粘贴" in finished[0][0]


def test_selection_sender_copy_only_write_failure_and_inactive(qtbot) -> None:
    clip = ClipItem("id", ClipKind.TEXT, "h", 1, 1, text="hello")
    finished: list[tuple[str, bool]] = []
    sender = SelectionSender(
        FakeWriter(False), FakeRepository(), FakePaste(), AppSettings, lambda: None  # type: ignore[arg-type]
    )
    sender.finished.connect(lambda message, success: finished.append((message, success)))
    sender.send(clip, None)
    assert finished[-1] == ("无法写入系统剪贴板", False)

    sender = SelectionSender(
        FakeWriter(), FakeRepository(), FakePaste(), AppSettings, lambda: None  # type: ignore[arg-type]
    )
    sender.finished.connect(lambda message, success: finished.append((message, success)))
    sender.send(clip, None)
    assert finished[-1] == ("已复制到剪贴板", True)

    class InactiveTarget(FakeTarget):
        def activate(self) -> bool:
            return True

        def is_active(self) -> bool:
            return False

    sender.send(clip, InactiveTarget())  # type: ignore[arg-type]
    qtbot.waitUntil(lambda: len(finished) == 3, timeout=1_000)
    assert finished[-1] == ("已复制，但目标窗口未激活", False)


def test_selection_sender_removes_file_item_deleted_before_send(qtbot, tmp_path: Path) -> None:
    repository = HistoryRepository(tmp_path)
    path = tmp_path / "deleted-before-send.txt"
    path.write_text("gone", encoding="utf-8")
    item = repository.add_files((str(path),))
    path.unlink()
    writer, paste = FakeWriter(), FakePaste()
    hidden: list[bool] = []
    sender = SelectionSender(
        writer,  # type: ignore[arg-type]
        repository,
        paste,  # type: ignore[arg-type]
        AppSettings,
        lambda: hidden.append(True),
    )
    finished: list[tuple[str, bool]] = []
    sender.finished.connect(lambda message, success: finished.append((message, success)))

    sender.send(item, FakeTarget())  # type: ignore[arg-type]

    qtbot.waitUntil(lambda: bool(finished), timeout=1_000)
    assert finished == [("原文件已不存在，已从历史移除", False)]
    assert repository.get(item.id) is None
    assert writer.writes == []
    assert paste.count == 0
    assert hidden == []
    repository.close()


def test_selection_sender_rejects_row_already_removed_by_background_sweep(
    qtbot, tmp_path: Path
) -> None:
    repository = HistoryRepository(tmp_path)
    path = tmp_path / "stale-ui-item.txt"
    path.write_text("stale", encoding="utf-8")
    stale_ui_item = repository.add_files((str(path),))
    assert repository.delete(stale_ui_item.id)
    writer, paste = FakeWriter(), FakePaste()
    sender = SelectionSender(
        writer,  # type: ignore[arg-type]
        repository,
        paste,  # type: ignore[arg-type]
        AppSettings,
        lambda: None,
    )
    finished: list[tuple[str, bool]] = []
    sender.finished.connect(lambda message, success: finished.append((message, success)))

    sender.send(stale_ui_item, FakeTarget())  # type: ignore[arg-type]

    qtbot.waitUntil(lambda: bool(finished), timeout=1_000)
    assert finished == [("原文件已不存在，已从历史移除", False)]
    assert writer.writes == []
    assert paste.count == 0
    repository.close()


def test_selection_sender_rejects_row_deleted_after_worker_validation(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = HistoryRepository(tmp_path)
    path = tmp_path / "deleted-before-validation-callback.txt"
    path.write_text("available", encoding="utf-8")
    item = repository.add_files((str(path),))
    validation_finished = threading.Event()
    original_validate = repository.validate_file_item

    def tracked_validate(item_id: str):
        result = original_validate(item_id)
        validation_finished.set()
        return result

    monkeypatch.setattr(repository, "validate_file_item", tracked_validate)
    writer = FakeWriter()
    sender = SelectionSender(
        writer,  # type: ignore[arg-type]
        repository,
        FakePaste(),  # type: ignore[arg-type]
        AppSettings,
        lambda: None,
    )
    finished: list[tuple[str, bool]] = []
    sender.finished.connect(lambda message, success: finished.append((message, success)))

    sender.send(item, None)
    assert validation_finished.wait(1)
    assert repository.delete(item.id)

    qtbot.waitUntil(lambda: bool(finished), timeout=1_000)
    assert finished == [("原文件已不存在，已从历史移除", False)]
    assert writer.writes == []
    repository.close()


def test_selection_sender_revalidates_row_refreshed_before_validation_callback(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = HistoryRepository(tmp_path)
    path = tmp_path / "refreshed-before-validation-callback.txt"
    path.write_text("available", encoding="utf-8")
    item = repository.add_files((str(path),), "old source")
    first_validation_finished = threading.Event()
    original_validate = repository.validate_file_item
    calls = 0

    def tracked_validate(item_id: str):
        nonlocal calls
        result = original_validate(item_id)
        calls += 1
        if calls == 1:
            first_validation_finished.set()
        return result

    monkeypatch.setattr(repository, "validate_file_item", tracked_validate)
    writer = FakeWriter()
    sender = SelectionSender(
        writer,  # type: ignore[arg-type]
        repository,
        FakePaste(),  # type: ignore[arg-type]
        AppSettings,
        lambda: None,
    )
    finished: list[tuple[str, bool]] = []
    sender.finished.connect(lambda message, success: finished.append((message, success)))

    sender.send(item, None)
    assert first_validation_finished.wait(1)
    refreshed = repository.add_files((str(path),), "fresh source")

    qtbot.waitUntil(lambda: bool(finished), timeout=1_000)
    assert calls == 2
    assert writer.writes == [refreshed]
    assert finished == [("已复制到剪贴板", True)]
    repository.close()


def test_selection_sender_checks_file_paths_off_the_gui_thread(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = HistoryRepository(tmp_path)
    path = tmp_path / "slow-network-path.txt"
    path.write_text("available", encoding="utf-8")
    item = repository.add_files((str(path),))
    writer = FakeWriter()
    sender = SelectionSender(
        writer,  # type: ignore[arg-type]
        repository,
        FakePaste(),  # type: ignore[arg-type]
        AppSettings,
        lambda: None,
    )
    finished: list[tuple[str, bool]] = []
    sender.finished.connect(lambda message, success: finished.append((message, success)))
    gui_thread = threading.get_ident()
    stat_threads: list[int] = []
    write_threads: list[int] = []
    stat_started = threading.Event()
    release_stat = threading.Event()
    original_missing = core_module._file_path_is_definitively_missing
    original_write = writer.write_item

    def delayed_stat(value: str) -> bool:
        stat_threads.append(threading.get_ident())
        stat_started.set()
        assert release_stat.wait(2)
        return original_missing(value)

    def tracked_write(value: ClipItem) -> bool:
        write_threads.append(threading.get_ident())
        return original_write(value)

    monkeypatch.setattr(core_module, "_file_path_is_definitively_missing", delayed_stat)
    monkeypatch.setattr(writer, "write_item", tracked_write)

    started = time.perf_counter()
    sender.send(item, None)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1
    assert stat_started.wait(1)
    assert stat_threads and all(thread_id != gui_thread for thread_id in stat_threads)
    assert writer.writes == []
    assert finished == []

    release_stat.set()
    qtbot.waitUntil(lambda: bool(finished), timeout=1_000)
    assert writer.writes == [item]
    assert write_threads == [gui_thread]
    assert finished == [("已复制到剪贴板", True)]
    repository.close()


def test_selection_sender_file_validation_timeout_releases_busy_and_ignores_late_result(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = HistoryRepository(tmp_path)
    path = tmp_path / "hung-network-path.txt"
    path.write_text("available", encoding="utf-8")
    file_item = repository.add_files((str(path),))
    stat_started = threading.Event()
    release_stat = threading.Event()
    original_missing = core_module._file_path_is_definitively_missing

    def blocked_stat(value: str) -> bool:
        stat_started.set()
        assert release_stat.wait(2)
        return original_missing(value)

    monkeypatch.setattr(core_module, "_file_path_is_definitively_missing", blocked_stat)
    writer = FakeWriter()
    sender = SelectionSender(
        writer,  # type: ignore[arg-type]
        repository,
        FakePaste(),  # type: ignore[arg-type]
        AppSettings,
        lambda: None,
        file_validation_timeout_ms=50,
    )
    finished: list[tuple[str, bool]] = []
    sender.finished.connect(lambda message, success: finished.append((message, success)))

    sender.send(file_item, None)
    assert stat_started.wait(1)
    qtbot.waitUntil(lambda: bool(finished), timeout=1_000)
    assert finished == [("原文件验证超时，请重试", False)]
    assert writer.writes == []

    sender.send(file_item, None)
    assert finished[-1] == ("原文件仍在验证，请稍后重试", False)
    text_item = ClipItem("text", ClipKind.TEXT, "text", 1, 1, text="still responsive")
    sender.send(text_item, None)
    assert writer.writes == [text_item]
    assert finished[-1] == ("已复制到剪贴板", True)

    release_stat.set()
    qtbot.waitUntil(lambda: not sender._validation_tasks, timeout=1_000)
    assert writer.writes == [text_item]
    repository.close()


def test_hung_file_validations_are_bounded_and_do_not_consume_global_pool(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = HistoryRepository(tmp_path)
    items: list[ClipItem] = []
    for index in range(3):
        path = tmp_path / f"hung-network-path-{index}.txt"
        path.write_text("available", encoding="utf-8")
        items.append(repository.add_files((str(path),)))
    release_stats = threading.Event()
    started_lock = threading.Lock()
    started_count = 0

    def blocked_stat(_value: str) -> bool:
        nonlocal started_count
        with started_lock:
            started_count += 1
        assert release_stats.wait(2)
        return False

    monkeypatch.setattr(core_module, "_file_path_is_definitively_missing", blocked_stat)
    sender = SelectionSender(
        FakeWriter(),  # type: ignore[arg-type]
        repository,
        FakePaste(),  # type: ignore[arg-type]
        AppSettings,
        lambda: None,
        file_validation_timeout_ms=50,
    )
    finished: list[tuple[str, bool]] = []
    sender.finished.connect(lambda message, success: finished.append((message, success)))

    sender.send(items[0], None)
    qtbot.waitUntil(lambda: len(finished) == 1, timeout=1_000)
    sender.send(items[1], None)
    qtbot.waitUntil(lambda: len(finished) == 2, timeout=1_000)
    qtbot.waitUntil(lambda: started_count == 2, timeout=1_000)
    sender.send(items[2], None)

    assert finished[-1] == ("后台文件验证繁忙，请稍后重试", False)
    assert len(sender._validation_tasks) == 2

    global_pool_ran = threading.Event()

    class MarkerTask(QRunnable):
        def run(self) -> None:
            global_pool_ran.set()

    QThreadPool.globalInstance().start(MarkerTask())
    assert global_pool_ran.wait(1)

    release_stats.set()
    qtbot.waitUntil(lambda: not sender._validation_tasks, timeout=1_000)
    repository.close()


def test_platform_reveal_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(system_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        system_module.os,
        "spawnlp",
        lambda *_args: (_ for _ in ()).throw(OSError("no")),
        raising=False,
    )
    assert not PlatformBridge.reveal(tmp_path)


def test_macos_accessibility_status_prompt_and_settings_link(monkeypatch) -> None:
    prompted: list[dict] = []
    opened: list[tuple] = []
    services = types.SimpleNamespace(
        AXIsProcessTrusted=lambda: True,
        AXIsProcessTrustedWithOptions=lambda options: prompted.append(options),
        kAXTrustedCheckOptionPrompt="prompt",
    )
    monkeypatch.setattr(system_module.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "ApplicationServices", services)
    monkeypatch.setattr(
        system_module.os,
        "spawnlp",
        lambda *args: opened.append(args) or 1,
        raising=False,
    )

    assert PlatformBridge.accessibility_permission_status() is True
    assert PlatformBridge.request_accessibility_permission()
    assert prompted == [{"prompt": True}]
    assert PlatformBridge._MACOS_ACCESSIBILITY_URL in opened[0]


def test_accessibility_permission_is_not_required_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(system_module.sys, "platform", "win32")

    assert PlatformBridge.accessibility_permission_status() is None
    assert not PlatformBridge.request_accessibility_permission()


def test_macos_launch_at_login_writes_source_command_and_removes_it(tmp_path: Path) -> None:
    executable = tmp_path / ".venv" / "bin" / "python"
    manager = LaunchAtLoginManager(
        platform="darwin",
        executable=executable,
        frozen=False,
        home=tmp_path,
    )

    success, message = manager.set_enabled(True)

    target = tmp_path / "Library" / "LaunchAgents" / "com.clipsoon.app.plist"
    payload = plistlib.loads(target.read_bytes())
    assert success
    assert message == "已开启开机自启动"
    assert payload["Label"] == "com.clipsoon.app"
    assert payload["ProgramArguments"] == [str(executable.resolve()), "-m", "clipsoon"]
    assert payload["RunAtLoad"] is True

    success, message = manager.set_enabled(False)
    assert success
    assert message == "已关闭开机自启动"
    assert not target.exists()


def test_windows_launch_at_login_uses_pythonw_and_current_user_run_key(
    tmp_path: Path, monkeypatch
) -> None:
    values: dict[str, str] = {}

    class FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def set_value(_key, name, _reserved, _kind, value) -> None:
        values[name] = value

    def delete_value(_key, name) -> None:
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]

    winreg = types.SimpleNamespace(
        HKEY_CURRENT_USER="HKCU",
        KEY_SET_VALUE=2,
        REG_SZ=1,
        CreateKey=lambda *_args: FakeKey(),
        OpenKey=lambda *_args: FakeKey(),
        SetValueEx=set_value,
        DeleteValue=delete_value,
    )
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    python = scripts / "python.exe"
    pythonw = scripts / "pythonw.exe"
    python.write_bytes(b"")
    pythonw.write_bytes(b"")
    manager = LaunchAtLoginManager(platform="win32", executable=python, frozen=False)

    assert manager.set_enabled(True)[0]
    assert values["ClipSoon"] == subprocess.list2cmdline((str(pythonw.resolve()), "-m", "clipsoon"))
    assert manager.set_enabled(False)[0]
    assert "ClipSoon" not in values


def test_frozen_launch_at_login_command_only_contains_the_app_executable(tmp_path: Path) -> None:
    executable = tmp_path / "ClipSoon.exe"
    manager = LaunchAtLoginManager(platform="win32", executable=executable, frozen=True)

    assert manager.command == (str(executable.resolve()),)
