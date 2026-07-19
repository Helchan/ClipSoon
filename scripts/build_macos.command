#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Create .venv with Python 3.12 and install '.[package]' first."
  exit 1
fi
cd "${PROJECT_DIR}"
"${PYTHON_BIN}" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name ClipSoon \
  --osx-bundle-identifier com.clipsoon.app \
  --collect-submodules pynput \
  --hidden-import AppKit \
  --hidden-import ApplicationServices \
  clipsoon/app.py
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 0.1.0" "${PROJECT_DIR}/dist/ClipSoon.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string 1" "${PROJECT_DIR}/dist/ClipSoon.app/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion 1" "${PROJECT_DIR}/dist/ClipSoon.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "${PROJECT_DIR}/dist/ClipSoon.app/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "${PROJECT_DIR}/dist/ClipSoon.app/Contents/Info.plist"
codesign --force --deep --sign - "${PROJECT_DIR}/dist/ClipSoon.app"
codesign --verify --deep --strict --verbose=2 "${PROJECT_DIR}/dist/ClipSoon.app"
echo "Built: ${PROJECT_DIR}/dist/ClipSoon.app"
