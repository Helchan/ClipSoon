from __future__ import annotations

from PySide6.QtWidgets import QApplication

from clipsoon.app import ClipSoonApplication, _WindowsPanelGuard
from clipsoon.system import ForegroundTargetHandle, HotkeyActivationContext, PlatformBridge


def test_windows_panel_guard_hides_on_first_outside_click_without_prior_activation() -> None:
    guard = _WindowsPanelGuard()
    guard.arm(initial_foreground=101, panel_window=202, primary_button_down=False)

    assert not guard.should_hide(foreground=101, primary_button_down=False, cursor_inside=False)
    assert guard.should_hide(foreground=101, primary_button_down=True, cursor_inside=False)


def test_windows_panel_guard_tracks_activation_and_foreground_changes() -> None:
    guard = _WindowsPanelGuard()
    guard.arm(initial_foreground=101, panel_window=202, primary_button_down=False)

    assert not guard.should_hide(foreground=202, primary_button_down=False, cursor_inside=True)
    assert guard.saw_panel_foreground
    assert guard.should_hide(foreground=303, primary_button_down=False, cursor_inside=False)

    guard.arm(initial_foreground=101, panel_window=202, primary_button_down=False)
    assert guard.should_hide(foreground=303, primary_button_down=False, cursor_inside=False)


def test_windows_panel_guard_keeps_inside_click_and_can_sync_ignored_input() -> None:
    guard = _WindowsPanelGuard()
    guard.arm(initial_foreground=101, panel_window=202, primary_button_down=False)

    assert not guard.should_hide(foreground=101, primary_button_down=True, cursor_inside=True)
    guard.sync_primary_button(True)
    assert not guard.should_hide(foreground=101, primary_button_down=True, cursor_inside=False)
    assert not guard.should_hide(foreground=101, primary_button_down=False, cursor_inside=False)
    assert guard.should_hide(foreground=101, primary_button_down=True, cursor_inside=False)


def test_application_requests_verified_native_activation_after_show(qtbot, tmp_path, monkeypatch) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application.panel.keep_open(True)
    activation_requests: list[int] = []
    monkeypatch.setattr(PlatformBridge, "accessibility_permission_status", lambda: None)
    monkeypatch.setattr(
        PlatformBridge,
        "capture_target",
        lambda: ForegroundTargetHandle("windows", 101, "Editor"),
    )
    monkeypatch.setattr(PlatformBridge, "foreground_window_id", lambda: 101)
    monkeypatch.setattr(PlatformBridge, "primary_button_down", lambda: False)
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    monkeypatch.setattr(
        PlatformBridge,
        "request_window_activation",
        lambda identifier: activation_requests.append(identifier) or True,
    )

    application.show_panel()
    assert application._panel_watch_timer.isActive()

    qtbot.waitUntil(lambda: bool(activation_requests), timeout=500)
    assert activation_requests == [int(application.panel.winId())]
    assert application._panel_guard.initial_foreground == 101
    assert application._panel_guard.saw_panel_foreground
    application.panel.keep_open(False)
    application.shutdown()


def test_hotkey_context_preserves_target_across_panel_activation_retry(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application.panel.keep_open(True)
    activation_results = iter((False, True))
    activation_requests: list[int] = []
    foreground = [303]
    monkeypatch.setattr(PlatformBridge, "accessibility_permission_status", lambda: None)
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    monkeypatch.setattr(
        PlatformBridge,
        "target_from_window_id",
        lambda identifier: ForegroundTargetHandle("windows", identifier, "Editor"),
    )
    monkeypatch.setattr(
        PlatformBridge,
        "capture_target",
        lambda: (_ for _ in ()).throw(AssertionError("late target capture")),
    )
    monkeypatch.setattr(PlatformBridge, "foreground_window_id", lambda: foreground[0])
    monkeypatch.setattr(PlatformBridge, "primary_button_down", lambda: False)
    def request_activation(identifier: int) -> bool:
        activation_requests.append(identifier)
        activated = next(activation_results)
        if activated:
            foreground[0] = identifier
        return activated

    monkeypatch.setattr(PlatformBridge, "request_window_activation", request_activation)
    application.show_panel(
        HotkeyActivationContext(target_window=303, foreground_granted=True)
    )

    qtbot.waitUntil(lambda: len(activation_requests) == 2, timeout=500)
    panel_window = int(application.panel.winId())
    assert application.target == ForegroundTargetHandle("windows", 303, "Editor")
    assert activation_requests == [panel_window, panel_window]
    assert application._panel_guard.initial_foreground == 303
    assert application._panel_guard.saw_panel_foreground
    application.panel.keep_open(False)
    application.shutdown()
