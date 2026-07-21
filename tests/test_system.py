from __future__ import annotations

import plistlib
import subprocess
import sys
import types
from pathlib import Path

from PySide6.QtCore import QMimeData, QObject, QUrl, Signal
from PySide6.QtGui import QClipboard, QImage

import clipsoon.system as system_module
from clipsoon.core import AppSettings, ClipItem, ClipKind, HistoryRepository
from clipsoon.system import (
    ClipboardController,
    GlobalHotkeyService,
    HotkeyStateMachine,
    LaunchAtLoginManager,
    PlatformBridge,
    SelectionSender,
    _canonical_key,
)


class FakeClipboard(QObject):
    dataChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.mime = QMimeData()
        self.sequence = 0

    def mimeData(self, _mode=QClipboard.Mode.Clipboard) -> QMimeData:
        return self.mime

    def image(self, _mode=QClipboard.Mode.Clipboard) -> QImage:
        value = self.mime.imageData()
        return value if isinstance(value, QImage) else QImage()

    def setMimeData(self, mime: QMimeData, _mode=QClipboard.Mode.Clipboard) -> None:
        self.mime = mime
        self.sequence += 1
        self.dataChanged.emit()

    def set_external(self, mime: QMimeData) -> None:
        self.mime = mime
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
    service = GlobalHotkeyService(lambda: events.append("hit"), events.append)
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
    service = GlobalHotkeyService(lambda: None, lambda _message: None)
    service.start(AppSettings())
    assert service.is_running

    service._listener.running = False

    assert not service.is_running
    service.stop()


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
        ShowWindow=show_window,
        BringWindowToTop=bring_to_top,
        SetForegroundWindow=set_foreground,
        GetForegroundWindow=get_foreground,
    )
    monkeypatch.setattr(system_module.sys, "platform", "win32")
    monkeypatch.setattr(system_module.ctypes, "windll", types.SimpleNamespace(user32=user32), raising=False)

    assert PlatformBridge.request_window_activation(202)
    assert PlatformBridge.foreground_window_id() == 202
    assert calls == [("show", 202), ("top", 202), ("foreground", 202)]


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

    def write_item(self, item: ClipItem) -> bool:
        self.writes.append(item)
        return self.succeeds


class FakeRepository:
    def __init__(self) -> None:
        self.used: list[str] = []

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
    sender.send(clip, target)  # type: ignore[arg-type]
    qtbot.waitUntil(lambda: bool(finished), timeout=1_000)
    assert finished[-1] == ("已发送", True)
    assert repository.used == ["id"]
    assert hidden == [True]
    assert paste.count == 1

    failed = FakeTarget(active=False)
    sender.send(clip, failed)  # type: ignore[arg-type]
    qtbot.waitUntil(lambda: len(finished) == 2, timeout=1_000)
    assert finished[-1] == ("已复制，但无法恢复目标窗口", False)


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


def test_platform_reveal_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(system_module.sys, "platform", "darwin")
    monkeypatch.setattr(system_module.os, "spawnlp", lambda *_args: (_ for _ in ()).throw(OSError("no")))
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
    monkeypatch.setattr(system_module.os, "spawnlp", lambda *args: opened.append(args) or 1)

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
