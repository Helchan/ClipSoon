"""Minimal process launcher that keeps Windows helpers independent of Qt UI."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv)
    helper_request = windows_helper_request(arguments)
    if helper_request is not None:
        role, helper_arguments = helper_request
        return run_windows_helper(role, helper_arguments)

    from clipsoon.app import main as application_main

    return int(application_main(arguments))


def windows_helper_request(arguments: list[str]) -> tuple[str, list[str]] | None:
    matches = [value for value in arguments[1:] if value.startswith("--windows-helper=")]
    if not matches:
        return None
    if len(matches) != 1:
        return "", []
    role = matches[0].partition("=")[2]
    helper_arguments = [value for value in arguments[1:] if not value.startswith("--windows-helper=")]
    return role, helper_arguments


def run_windows_helper(role: str, arguments: list[str]) -> int:
    if sys.platform != "win32":
        return 64
    if role == "hotkey":
        from clipsoon.windows_hotkey_host import main as worker_main
    elif role == "clipboard":
        from clipsoon.windows_clipboard_host import main as worker_main
    else:
        return 64
    return int(worker_main(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
