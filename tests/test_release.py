from __future__ import annotations

import tomllib
from pathlib import Path

from clipsoon import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_release_version_is_consistent() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    product_spec = (PROJECT_ROOT / "docs/产品规格与验收.md").read_text(encoding="utf-8")
    acceptance = (PROJECT_ROOT / "docs/验收报告.md").read_text(encoding="utf-8")

    assert __version__ == "1.1.12"
    assert project["project"]["version"] == __version__
    assert f"当前发布版本：`v{__version__}`" in readme
    assert f'git tag -a v{__version__} -m "ClipSoon {__version__}"' in readme
    assert f"↵ 发送 | Esc 隐藏 | v{__version__}" in product_spec
    assert f"## 0. v{__version__} 追加记录" in acceptance


def test_supported_python_versions_and_dependency_floors_are_declared() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(project["project"]["dependencies"])
    optional_dependencies = project["project"]["optional-dependencies"]
    classifiers = set(project["project"]["classifiers"])

    assert project["project"]["requires-python"] == ">=3.11,<3.15"
    assert project["tool"]["ruff"]["target-version"] == "py311"
    for version in ("3.11", "3.12", "3.13", "3.14"):
        assert f"Programming Language :: Python :: {version}" in classifiers
    assert "Programming Language :: Python :: Implementation :: CPython" in classifiers
    assert "pynput>=1.8.2,<2" in dependencies
    assert "PySide6-Essentials>=6.10.1,<7" in dependencies
    assert "pyobjc-framework-ApplicationServices>=11.1,<13; sys_platform == 'darwin'" in dependencies
    assert "pyobjc-framework-Cocoa>=11.1,<13; sys_platform == 'darwin'" in dependencies
    assert "coverage[toml]>=7.10,<8" in optional_dependencies["dev"]
    assert "pytest>=8.4.2,<9" in optional_dependencies["dev"]
    assert "pytest-qt>=4.5,<5" in optional_dependencies["dev"]
    assert "pytest-timeout>=2.4,<3" in optional_dependencies["dev"]
    assert "ruff>=0.15,<1" in optional_dependencies["dev"]
    assert "pyinstaller>=6.15,<7" in optional_dependencies["package"]


def test_multi_send_contract_is_documented() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    product_spec = (PROJECT_ROOT / "docs/产品规格与验收.md").read_text(encoding="utf-8")
    architecture = (PROJECT_ROOT / "docs/架构设计.md").read_text(encoding="utf-8")

    assert "合并为一个临时文本负载" in readme
    assert "仅触发一次系统粘贴" in readme
    assert "不额外追加末尾换行" in product_spec
    assert "稳定扁平化" in readme
    assert "一个 `FILES` 负载" in readme
    assert "规范化重复路径保留首次" in product_spec
    assert "去重后最多 1,000 个文件或目录" in product_spec
    assert "1,001 个及以上在 write/hide/use_count 前失败" in product_spec
    assert "write/ACK/activate/verify/paste" in product_spec
    assert "数量不超过 20" in architecture
    assert "图片批次非原子" in readme
    assert "`image_batch_interval_ms`" in readme
    assert "默认 100 ms" in readme
    assert "发送途中修改只影响下一批" in readme
    assert "20–1000 ms" in product_spec
    assert "`k/N`" in product_spec
    assert "混合类型必须零副作用拒绝" in product_spec
    assert "图片或文件请单项发送" not in product_spec
    assert "不模拟 Enter" in architecture
    assert "只报告“已触发粘贴”" in product_spec


def test_static_panel_border_contract_is_documented() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    product_spec = (PROJECT_ROOT / "docs/产品规格与验收.md").read_text(encoding="utf-8")
    architecture = (PROJECT_ROOT / "docs/架构设计.md").read_text(encoding="utf-8")

    for document in (readme, product_spec, architecture):
        assert "静态细线边框" in document
    assert "流动霓虹边框" not in readme
    assert "流动霓虹边框" not in product_spec
    assert "流动霓虹边框" not in architecture
    assert "neon_border_enabled" not in architecture
    assert "主面板不得安装 `QGraphicsDropShadowEffect`" in product_spec
    assert "设置窗口同样不得在 Windows 顶层 mask 边缘描边" in product_spec
    assert "Windows 主面板和设置窗口都不在顶层二值 mask 边缘描边" in architecture


def test_tag_release_workflow_builds_requested_platforms() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert 'tags:\n      - "v*"' in workflow
    assert "cancel-in-progress: true" in workflow
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
    assert "- name: Lint Windows source" in workflow
    assert "pytest -vv --durations=20 --timeout=90 --timeout-method=thread" in workflow
    assert "pytest-timeout>=2.4,<3" in project["project"]["optional-dependencies"]["dev"]
    assert workflow.count('python-version: "3.12"') == 3


def test_python_compatibility_workflow_covers_supported_platform_matrix() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/compatibility.yml").read_text(encoding="utf-8")
    supported_versions = 'python-version: ["3.11", "3.12", "3.13", "3.14"]'

    assert workflow.count(supported_versions) == 2
    assert "runs-on: windows-latest" in workflow
    assert "architecture: x64" in workflow
    assert "runs-on: macos-15" in workflow
    assert "architecture: arm64" in workflow
    assert workflow.count("timeout-minutes: 30") == 2
    assert 'python -m pip install -e ".[dev]"' in workflow
    assert "python -m pip install -e '.[dev]'" in workflow
    assert workflow.count("python -m ruff check .") == 2
    assert workflow.count("python -m pytest -vv --durations=20 --timeout=90 --timeout-method=thread") == 2
    assert '$env:QT_QPA_PLATFORM = "offscreen"' in workflow
    assert "QT_QPA_PLATFORM: offscreen" in workflow


def test_windows_helper_smoke_uses_only_registered_combo_hotkeys() -> None:
    smoke = (PROJECT_ROOT / "scripts/smoke_windows_helpers.py").read_text(encoding="utf-8")

    assert "combo:ctrl+shift+space" in smoke
    assert "double:" not in smoke


def test_windows_build_script_keeps_portable_runtime_contract_clear() -> None:
    launcher = (PROJECT_ROOT / "build_windows.bat").read_text(encoding="utf-8")
    script = (PROJECT_ROOT / "scripts/build_windows.bat").read_text(encoding="utf-8")

    assert 'scripts\\build_windows.bat" %*' in launcher
    assert "--onedir" in script
    assert "--onefile" in script
    assert "sys.implementation.name == 'cpython'" in script
    assert "print(f'python{sys.version_info.major}{sys.version_info.minor}.dll')" in script
    assert 'set "PYTHON_DLL=' in script
    assert "dist\\ClipSoon\\_internal\\%PYTHON_DLL%" in script
    assert "dist\\ClipSoon\\%PYTHON_DLL%" in script
    assert "python312.dll" not in script
    assert "Do not move only ClipSoon.exe" in script


def test_source_launch_and_build_prompts_name_the_supported_cpython_range() -> None:
    paths = (
        "run.bat",
        "run.command",
        "scripts/build_windows.bat",
        "scripts/build_macos.command",
    )

    for path in paths:
        content = (PROJECT_ROOT / path).read_text(encoding="utf-8")
        assert "CPython 3.11-3.14" in content
        assert "Py_GIL_DISABLED" in content


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
    assert "SetWindowSubclass" in smoke
    assert "text_paste_observed.wait(3)" in smoke
    assert "did not receive WM_PASTE for text data" in smoke
    assert "GetWindowTextW" in smoke
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
