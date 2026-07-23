from __future__ import annotations

import tomllib
from pathlib import Path

from clipsoon import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_release_version_is_consistent() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == "0.10.3"
    assert project["project"]["version"] == __version__


def test_tag_release_workflow_builds_requested_platforms() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'tags:\n      - "v*"' in workflow
    assert "runs-on: windows-latest" in workflow
    assert "timeout-minutes: 30" in workflow
    assert "architecture: x64" in workflow
    assert "runs-on: macos-15" in workflow
    assert "architecture: arm64" in workflow
    assert 'MACOSX_DEPLOYMENT_TARGET: "13.0"' in workflow
    assert "ClipSoon-${{ github.ref_name }}-windows-x64.zip" in workflow
    assert "ClipSoon-${{ github.ref_name }}-macOS-arm64.zip" in workflow
    assert "contents: write" in workflow
    assert "gh release create" in workflow
    assert "scripts\\smoke_windows_helpers.py dist\\ClipSoon\\ClipSoon.exe" in workflow


def test_windows_helper_smoke_uses_only_registered_combo_hotkeys() -> None:
    smoke = (PROJECT_ROOT / "scripts/smoke_windows_helpers.py").read_text(encoding="utf-8")

    assert "combo:ctrl+shift+space" in smoke
    assert "double:" not in smoke


def test_windows_helper_smoke_exercises_eager_native_clipboard_formats() -> None:
    smoke = (PROJECT_ROOT / "scripts/smoke_windows_helpers.py").read_text(encoding="utf-8")

    assert '"text"' in smoke
    assert '"files"' in smoke
    assert '"image"' in smoke
    assert '"verify_clipboard"' in smoke
    assert '"verify_result"' in smoke
    assert "CF_UNICODETEXT" in smoke
    assert "CF_HDROP" in smoke
    assert "CF_DIBV5" in smoke
    assert "api.global_bytes(CF_DIB)" in smoke
    assert "_smoke_windows_input_delivery" in smoke
    assert 'f"--windows-helper={role}"' in smoke
    assert '_run_packaged_helper(executable, "paste", [])' in smoke
    assert "SendMessageTimeoutW" in smoke
    assert "SetWindowSubclass" in smoke
    assert "image_paste_observed.wait(2)" in smoke
    assert "did not receive WM_PASTE for image data" in smoke
    assert "were not all available during WM_PASTE" in smoke
    assert "timeout=20" in smoke
    assert "Win32 EDIT paste mismatch" in smoke
    assert 'register_format("PNG")' in smoke
    assert "_shutdown(process, \"clipboard\")" in smoke
    assert "did not survive clipboard helper exit" in smoke
    shutdown = smoke.index('_shutdown(process, "clipboard")')
    independent_png_lookup = smoke.index('api.register_format("PNG")')
    assert shutdown < independent_png_lookup
