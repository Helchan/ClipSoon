#!/bin/zsh
set -euo pipefail
SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Create .venv with Python 3.12 and install '.[package]' first."
  exit 1
fi
APP_VERSION="${CLIPSOON_VERSION:-$("${PYTHON_BIN}" -c 'import clipsoon; print(clipsoon.__version__)')}"
APP_VERSION="${APP_VERSION#v}"
BUILD_VERSION="${GITHUB_RUN_NUMBER:-1}"
if [[ ! "${APP_VERSION}" =~ '^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$' ]]; then
  echo "Invalid ClipSoon version: ${APP_VERSION}"
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
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${APP_VERSION}" "${PROJECT_DIR}/dist/ClipSoon.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string ${BUILD_VERSION}" "${PROJECT_DIR}/dist/ClipSoon.app/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion ${BUILD_VERSION}" "${PROJECT_DIR}/dist/ClipSoon.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "${PROJECT_DIR}/dist/ClipSoon.app/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "${PROJECT_DIR}/dist/ClipSoon.app/Contents/Info.plist"
codesign --force --deep --sign - "${PROJECT_DIR}/dist/ClipSoon.app"
codesign --verify --deep --strict --verbose=2 "${PROJECT_DIR}/dist/ClipSoon.app"
echo "Built: ${PROJECT_DIR}/dist/ClipSoon.app"
