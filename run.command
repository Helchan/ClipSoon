#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
PACKAGED_EXEC="${PROJECT_DIR}/dist/ClipSoon.app/Contents/MacOS/ClipSoon"
# macOS resolves the virtualenv launcher to the framework executable in the
# process table (usually "Python"), so matching the symlink path misses it.
SOURCE_PATTERN='[Pp]ython(3(\.[0-9]+)?)? -m clipsoon'

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "ClipSoon 的标准 CPython 3.11-3.14 开发环境不存在。"
  echo "请先使用任一受支持版本创建环境，例如：python3.12 -m venv .venv"
  echo "然后执行：.venv/bin/python -m pip install -e '.[dev,package]'"
  read -r "?按回车键关闭..."
  exit 1
fi

if ! "${PYTHON_BIN}" -c 'import sys, sysconfig; supported = sys.implementation.name == "cpython" and (3, 11) <= sys.version_info[:2] <= (3, 14) and not sysconfig.get_config_var("Py_GIL_DISABLED"); raise SystemExit(0 if supported else 1)'; then
  echo "当前 .venv 不是受支持的标准 CPython 3.11-3.14 环境。"
  echo "请删除并使用 CPython 3.11、3.12、3.13 或 3.14 重新创建 .venv。"
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
