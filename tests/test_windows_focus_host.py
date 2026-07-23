from __future__ import annotations

import sys
from dataclasses import dataclass

import clipsoon.launcher as launcher
import clipsoon.windows_focus_host as focus_host
from clipsoon.windows_focus_host import (
    EXIT_ATTACH_FAILED,
    EXIT_DETACH_FAILED,
    EXIT_OK,
    EXIT_STALE_IDENTITY,
    EXIT_USAGE,
    PanelActivationRequest,
    TargetActivationRequest,
    activate_panel,
    activate_target,
)


@dataclass
class FakeWindow:
    thread_id: int
    process_id: int
    root: int
    visible: bool = True
    iconic: bool = False


class FakeFocusApi:
    def __init__(self) -> None:
        self.windows = {
            101: FakeWindow(11, 1001, 101),
            202: FakeWindow(22, 2002, 202),
            303: FakeWindow(7001, 8001, 303),
            808: FakeWindow(7002, 8001, 303),
        }
        self.foreground = 101
        self.focus = {7001: 0, 7002: 999}
        self.current_thread = 9000
        self.calls: list[tuple[object, ...]] = []
        self.attach_results: dict[tuple[int, bool], list[bool]] = {}

    def is_window(self, identifier: int) -> bool:
        return identifier in self.windows

    def is_window_visible(self, identifier: int) -> bool:
        return self.windows[identifier].visible

    def is_iconic(self, identifier: int) -> bool:
        return self.windows[identifier].iconic

    def restore_window(self, identifier: int) -> None:
        self.calls.append(("restore", identifier))
        self.windows[identifier].iconic = False

    def window_identity(self, identifier: int) -> tuple[int, int]:
        window = self.windows[identifier]
        return window.thread_id, window.process_id

    def root_window(self, identifier: int) -> int:
        return self.windows[identifier].root

    def foreground_window(self) -> int:
        return self.foreground

    def focus_window(self, thread_id: int) -> int:
        return self.focus.get(thread_id, 0)

    def ensure_message_queue(self) -> None:
        self.calls.append(("queue", self.current_thread))

    def current_thread_id(self) -> int:
        return self.current_thread

    def attach_thread_input(
        self,
        current_thread_id: int,
        target_thread_id: int,
        enabled: bool,
    ) -> bool:
        self.calls.append(
            ("attach" if enabled else "detach", current_thread_id, target_thread_id)
        )
        configured = self.attach_results.get((target_thread_id, enabled))
        return configured.pop(0) if configured else True

    def bring_to_top(self, identifier: int) -> None:
        self.calls.append(("top", identifier))

    def set_foreground(self, identifier: int) -> None:
        self.calls.append(("foreground", identifier))
        self.foreground = identifier

    def set_focus(self, identifier: int) -> None:
        self.calls.append(("focus", identifier))
        thread_id = self.windows[identifier].thread_id
        self.focus[thread_id] = identifier


def target_request() -> TargetActivationRequest:
    return TargetActivationRequest(
        target_window=303,
        target_thread_id=7001,
        target_process_id=8001,
        focus_window=808,
        focus_thread_id=7002,
        focus_process_id=8001,
    )


def test_target_activation_uses_short_lived_helper_queue_and_exact_focus() -> None:
    api = FakeFocusApi()
    api.windows[303].iconic = True

    assert activate_target(api, target_request()) == EXIT_OK
    assert api.foreground == 303
    assert api.focus[7002] == 808
    assert api.calls == [
        ("queue", 9000),
        ("attach", 9000, 11),
        ("attach", 9000, 7001),
        ("attach", 9000, 7002),
        ("restore", 303),
        ("top", 303),
        ("foreground", 303),
        ("focus", 808),
        ("detach", 9000, 7002),
        ("detach", 9000, 7001),
        ("detach", 9000, 11),
    ]


def test_target_activation_rejects_stale_focus_identity_before_attach() -> None:
    api = FakeFocusApi()
    api.windows[808].root = 101

    assert activate_target(api, target_request()) == EXIT_STALE_IDENTITY
    assert api.calls == []


def test_second_attach_failure_still_detaches_the_first_queue() -> None:
    api = FakeFocusApi()
    api.attach_results[(7002, True)] = [False]

    assert activate_target(api, target_request()) == EXIT_ATTACH_FAILED
    assert api.calls == [
        ("queue", 9000),
        ("attach", 9000, 11),
        ("attach", 9000, 7001),
        ("attach", 9000, 7002),
        ("detach", 9000, 7001),
        ("detach", 9000, 11),
    ]


def test_detach_failure_is_retried_and_never_reported_as_success() -> None:
    api = FakeFocusApi()
    api.attach_results[(7002, False)] = [False, False]

    assert activate_target(api, target_request()) == EXIT_DETACH_FAILED
    assert api.calls[-4:] == [
        ("detach", 9000, 7002),
        ("detach", 9000, 7002),
        ("detach", 9000, 7001),
        ("detach", 9000, 11),
    ]


def test_target_activation_allows_exact_cross_process_embedded_focus() -> None:
    api = FakeFocusApi()
    api.windows[808].process_id = 8100
    request = target_request()
    request = TargetActivationRequest(
        request.target_window,
        request.target_thread_id,
        request.target_process_id,
        request.focus_window,
        request.focus_thread_id,
        8100,
    )

    assert activate_target(api, request) == EXIT_OK
    assert api.foreground == 303
    assert api.focus[7002] == 808


def test_panel_activation_validates_owner_and_never_shows_hidden_window() -> None:
    api = FakeFocusApi()

    assert (
        activate_panel(
            api,
            PanelActivationRequest(panel_window=202, panel_process_id=2002),
        )
        == EXIT_OK
    )
    assert api.calls == [
        ("queue", 9000),
        ("attach", 9000, 11),
        ("attach", 9000, 22),
        ("top", 202),
        ("foreground", 202),
        ("detach", 9000, 22),
        ("detach", 9000, 11),
    ]

    hidden = FakeFocusApi()
    hidden.windows[202].visible = False
    assert (
        activate_panel(
            hidden,
            PanelActivationRequest(panel_window=202, panel_process_id=2002),
        )
        == EXIT_STALE_IDENTITY
    )
    assert hidden.calls == []


def test_cli_requires_complete_focus_identity(monkeypatch) -> None:
    monkeypatch.setattr(focus_host.sys, "platform", "win32")

    assert (
        focus_host.main(
            [
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
            ],
            api_factory=FakeFocusApi,
        )
        == EXIT_USAGE
    )


def test_cli_dispatches_target_and_panel_requests(monkeypatch) -> None:
    monkeypatch.setattr(focus_host.sys, "platform", "win32")
    target_api = FakeFocusApi()
    panel_api = FakeFocusApi()

    assert (
        focus_host.main(
            [
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
                "8001",
            ],
            api_factory=lambda: target_api,
        )
        == EXIT_OK
    )
    assert (
        focus_host.main(
            [
                "--mode",
                "panel",
                "--panel-hwnd",
                "202",
                "--panel-process-id",
                "2002",
            ],
            api_factory=lambda: panel_api,
        )
        == EXIT_OK
    )


def test_launcher_dispatches_focus_helper_before_importing_qt(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        focus_host,
        "main",
        lambda arguments: calls.append(arguments) or 23,
    )

    arguments = ["--mode", "panel", "--panel-hwnd", "202"]
    assert launcher.run_windows_helper("focus", arguments) == 23
    assert calls == [arguments]
