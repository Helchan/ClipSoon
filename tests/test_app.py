from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QRunnable, QThreadPool
from PySide6.QtWidgets import QApplication

from clipsoon.app import ClipSoonApplication, _WindowsPanelGuard
from clipsoon.core import WINDOWS_DEFAULT_HOTKEY, AppSettings, JsonSettingsStore
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


@pytest.mark.parametrize(
    "invalid_hotkey",
    ("double:alt", "combo:ctrl+print"),
)
def test_windows_application_migrates_and_persists_invalid_hotkey(
    qtbot,
    tmp_path,
    monkeypatch,
    invalid_hotkey: str,
) -> None:
    store = JsonSettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(hotkey=invalid_hotkey, double_tap_interval_ms=650))
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)

    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()

    assert application._windows_hotkey_migrated
    assert application.settings.value.hotkey == WINDOWS_DEFAULT_HOTKEY
    persisted = store.load()
    assert persisted.hotkey == WINDOWS_DEFAULT_HOTKEY
    assert persisted.double_tap_interval_ms == 650
    application.shutdown()


def test_windows_hotkey_registration_failure_rolls_back_to_last_ready_setting(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    previous_hotkey = "combo:ctrl+alt+o"
    candidate_hotkey = "combo:ctrl+alt+k"
    store = JsonSettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(hotkey=previous_hotkey))
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application._hotkey_ready(previous_hotkey)
    previous = application.settings.value
    application.settings.update(hotkey=candidate_hotkey)
    application._pending_hotkey_rollback = application._confirmed_windows_hotkey
    application._pending_hotkey_candidate = candidate_hotkey
    starts: list[AppSettings] = []
    monkeypatch.setattr(application.hotkey, "start", starts.append)

    application._hotkey_registration_failed(
        candidate_hotkey,
        "candidate shortcut already registered",
    )

    qtbot.waitUntil(
        lambda: application.settings.value.hotkey == previous_hotkey,
        timeout=500,
    )
    assert store.load().hotkey == previous_hotkey
    assert starts == [previous]
    assert application._pending_hotkey_rollback is None
    assert application._pending_hotkey_candidate == ""
    assert "已恢复上一个快捷键" in application.panel.status.text()

    # If the restored custom combination is also unavailable, there is one
    # final fallback to the built-in combination.
    application._hotkey_registration_failed(
        previous_hotkey,
        "restored shortcut also unavailable",
    )
    qtbot.waitUntil(
        lambda: application.settings.value.hotkey == WINDOWS_DEFAULT_HOTKEY,
        timeout=500,
    )
    assert [settings.hotkey for settings in starts] == [
        previous_hotkey,
        WINDOWS_DEFAULT_HOTKEY,
    ]
    assert store.load().hotkey == WINDOWS_DEFAULT_HOTKEY

    # Failure of the built-in fallback is display-only and cannot create a
    # default-to-default restart loop.
    application._hotkey_registration_failed(
        WINDOWS_DEFAULT_HOTKEY,
        "default shortcut also unavailable",
    )
    qtbot.wait(20)
    assert len(starts) == 2
    assert application.settings.value.hotkey == WINDOWS_DEFAULT_HOTKEY
    assert application.panel.status.text() == "default shortcut also unavailable"
    application.shutdown()


def test_windows_hotkey_candidate_ready_commits_and_clears_rollback(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    previous_hotkey = "combo:ctrl+alt+o"
    candidate_hotkey = "combo:ctrl+alt+k"
    store = JsonSettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(hotkey=previous_hotkey))
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application._hotkey_ready(previous_hotkey)
    previous = application.settings.value
    candidate = application.settings.update(hotkey=candidate_hotkey)
    application._pending_hotkey_rollback = previous
    application._pending_hotkey_candidate = candidate_hotkey

    application._hotkey_ready(candidate_hotkey)

    assert application._confirmed_windows_hotkey == candidate
    assert application._pending_hotkey_rollback is None
    assert application._pending_hotkey_candidate == ""
    assert store.load().hotkey == candidate_hotkey
    application.shutdown()


def test_initial_windows_custom_registration_failure_falls_back_to_default(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    custom_hotkey = "combo:ctrl+alt+o"
    store = JsonSettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(hotkey=custom_hotkey))
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    starts: list[AppSettings] = []
    monkeypatch.setattr(application.hotkey, "start", starts.append)

    assert application._confirmed_windows_hotkey is None
    assert application._pending_hotkey_rollback is None
    application._hotkey_registration_failed(
        custom_hotkey,
        "initial shortcut already registered",
    )

    qtbot.waitUntil(
        lambda: application.settings.value.hotkey == WINDOWS_DEFAULT_HOTKEY,
        timeout=500,
    )
    assert [settings.hotkey for settings in starts] == [WINDOWS_DEFAULT_HOTKEY]
    assert store.load().hotkey == WINDOWS_DEFAULT_HOTKEY
    assert "已改用 Ctrl+Shift+Space" in application.panel.status.text()
    application.shutdown()


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
    assert application.panel.isVisible()
    application.shutdown()


def test_windows_toggle_reactivates_visible_background_panel_instead_of_hiding(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    application.panel.set_native_deactivation_managed(True)
    application.panel.show()
    panel_window = int(application.panel.winId())
    foreground = [404]
    shown: list[HotkeyActivationContext | None] = []
    context = HotkeyActivationContext(target_window=404, foreground_granted=True)
    monkeypatch.setattr(PlatformBridge, "is_windows", lambda: True)
    monkeypatch.setattr(PlatformBridge, "foreground_window_id", lambda: foreground[0])
    monkeypatch.setattr(application, "show_panel", shown.append)

    application.toggle_panel(context)

    assert application.panel.isVisible()
    assert shown == [context]

    foreground[0] = panel_window
    application.toggle_panel(context)

    assert not application.panel.isVisible()
    application.shutdown()


def test_file_history_sweep_removes_deleted_source_without_blocking_panel(
    qtbot,
    tmp_path,
) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    path = tmp_path / "external-file.txt"
    path.write_text("content", encoding="utf-8")
    item = application.repository.add_files((str(path),))
    application._reload_history()
    assert application.panel.model.rowCount() == 1
    path.unlink()

    application._schedule_file_history_sweep()

    qtbot.waitUntil(lambda: not application._file_history_sweep_active, timeout=1_000)
    assert application.repository.get(item.id) is None
    assert application.panel.model.rowCount() == 0
    application.shutdown()


def test_hung_file_history_sweep_does_not_occupy_global_pool_or_block_shutdown(
    qtbot,
    tmp_path,
    monkeypatch,
) -> None:
    application = ClipSoonApplication(QApplication.instance(), tmp_path)
    qtbot.addWidget(application.panel)
    application.clipboard.start()
    sweep_started = threading.Event()
    release_sweep = threading.Event()

    def blocked_prune() -> tuple[str, ...]:
        sweep_started.set()
        assert release_sweep.wait(2)
        return ()

    monkeypatch.setattr(application.repository, "prune_missing_file_items", blocked_prune)
    application._schedule_file_history_sweep()
    assert sweep_started.wait(1)

    global_pool_ran = threading.Event()

    class MarkerTask(QRunnable):
        def run(self) -> None:
            global_pool_ran.set()

    QThreadPool.globalInstance().start(MarkerTask())
    assert global_pool_ran.wait(1)

    started = time.perf_counter()
    application.shutdown()
    assert time.perf_counter() - started < 1

    release_sweep.set()
    qtbot.waitUntil(lambda: not application._file_history_sweep_active, timeout=1_000)
