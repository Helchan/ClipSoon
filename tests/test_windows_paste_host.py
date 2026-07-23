from __future__ import annotations

import ctypes
import sys
import types

import clipsoon.launcher as launcher
import clipsoon.windows_paste_host as paste_host
from clipsoon.windows_paste_host import (
    EXIT_INPUT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    _WindowsInput,
    _WindowsKeyboardInput,
    send_windows_paste_input,
)


def test_windows_x64_input_layout_and_single_checked_batch() -> None:
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
        assert int(size) == ctypes.sizeof(_WindowsInput)
        return int(count)

    assert ctypes.sizeof(_WindowsInput) == 40
    assert ctypes.sizeof(_WindowsKeyboardInput) == 24
    assert send_windows_paste_input(types.SimpleNamespace(SendInput=send_input))
    assert calls == [
        [
            (1, 0x11, 0),
            (1, 0x56, 0),
            (1, 0x56, 0x0002),
            (1, 0x11, 0x0002),
        ]
    ]


def test_partial_send_input_fails_and_releases_pressed_keys() -> None:
    call_sizes: list[int] = []

    def send_input(count, _events, _size) -> int:
        call_sizes.append(int(count))
        return 2 if len(call_sizes) == 1 else int(count)

    assert not send_windows_paste_input(
        types.SimpleNamespace(SendInput=send_input)
    )
    assert call_sizes == [4, 2]


def test_paste_helper_main_is_windows_only_and_reports_native_result(
    monkeypatch,
) -> None:
    monkeypatch.setattr(paste_host.sys, "platform", "darwin")
    assert paste_host.main([]) == EXIT_USAGE

    monkeypatch.setattr(paste_host.sys, "platform", "win32")
    monkeypatch.setattr(
        paste_host.ctypes,
        "WinDLL",
        lambda name, **_kwargs: types.SimpleNamespace(name=name),
        raising=False,
    )
    outcomes = iter((True, False))
    monkeypatch.setattr(
        paste_host,
        "send_windows_paste_input",
        lambda _user32, _kernel32: next(outcomes),
    )

    assert paste_host.main([]) == EXIT_OK
    assert paste_host.main([]) == EXIT_INPUT_FAILED
    assert paste_host.main(["unexpected"]) == EXIT_USAGE


def test_launcher_dispatches_paste_helper_before_importing_qt(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        paste_host,
        "main",
        lambda arguments: calls.append(arguments) or 23,
    )

    assert launcher.run_windows_helper("paste", []) == 23
    assert calls == [[]]
