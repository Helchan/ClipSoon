#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
PACKAGED_EXEC="${PROJECT_DIR}/dist/ClipSoon.app/Contents/MacOS/ClipSoon"
# macOS resolves the virtualenv launcher to the framework executable in the
# process table (usually "Python"), so matching the symlink path misses it.
SOURCE_PATTERN='[Pp]ython(3(\.[0-9]+)?)? -m clipsoon'

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ClipSoon 的 Python 3.12 开发环境不存在。"
  echo "请先执行：python3.12 -m venv .venv"
  echo "然后执行：.venv/bin/python -m pip install -e '.[dev,package]'"
  read -r "?按回车键关闭..."
  exit 1
fi

stop_matching_processes() {
  local pattern="$1"
  if pgrep -f -- "${pattern}" >/dev/null 2>&1; then
    pkill -TERM -f -- "${pattern}" >/dev/null 2>&1 || true
    for _attempt in {1..20}; do
      pgrep -f -- "${pattern}" >/dev/null 2>&1 || return 0
      sleep 0.1
    done
  fi
}

echo "正在停止旧的 ClipSoon 打包实例和源码实例..."
stop_matching_processes "${PACKAGED_EXEC}"
stop_matching_processes "${SOURCE_PATTERN}"

echo "正在从当前源码启动 ClipSoon（不会执行打包）..."
cd "${PROJECT_DIR}"
exec "${PYTHON_BIN}" -m clipsoon --show
