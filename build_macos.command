#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
echo "正在构建 macOS ClipSoon.app..."
exec "${PROJECT_DIR}/scripts/build_macos.command"
