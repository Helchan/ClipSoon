from __future__ import annotations

import tomllib
from pathlib import Path

from clipsoon import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_release_version_is_consistent() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert __version__ == "0.9.4"
    assert project["project"]["version"] == __version__


def test_tag_release_workflow_builds_requested_platforms() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'tags:\n      - "v*"' in workflow
    assert "runs-on: windows-latest" in workflow
    assert "architecture: x64" in workflow
    assert "runs-on: macos-15" in workflow
    assert "architecture: arm64" in workflow
    assert 'MACOSX_DEPLOYMENT_TARGET: "13.0"' in workflow
    assert "ClipSoon-${{ github.ref_name }}-windows-x64.zip" in workflow
    assert "ClipSoon-${{ github.ref_name }}-macOS-arm64.zip" in workflow
    assert "contents: write" in workflow
    assert "gh release create" in workflow
