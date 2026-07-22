from __future__ import annotations

import sys

import clipsoon.launcher as launcher


def test_windows_helper_request_is_parsed_before_the_ui() -> None:
    assert launcher.windows_helper_request(["ClipSoon.exe"]) is None
    assert launcher.windows_helper_request(
        ["ClipSoon.exe", "--windows-helper=hotkey", "--spec", "double:ctrl"]
    ) == ("hotkey", ["--spec", "double:ctrl"])
    assert launcher.windows_helper_request(
        ["ClipSoon.exe", "--windows-helper=hotkey", "--windows-helper=clipboard"]
    ) == ("", [])


def test_launcher_dispatches_helper_without_importing_the_qt_application(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        launcher,
        "run_windows_helper",
        lambda role, arguments: calls.append((role, arguments)) or 23,
    )

    assert launcher.main(["ClipSoon.exe", "--windows-helper=clipboard", "--payload-dir", "C:/ipc"]) == 23
    assert calls == [("clipboard", ["--payload-dir", "C:/ipc"])]


def test_launcher_rejects_helpers_outside_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    assert launcher.run_windows_helper("hotkey", []) == 64
