@echo off
setlocal EnableExtensions

set "PROJECT_DIR=%~dp0"
set "PYTHON_BIN=%PROJECT_DIR%.venv\Scripts\python.exe"

if not exist "%PYTHON_BIN%" (
  echo ClipSoon 的标准 CPython 3.11-3.14 开发环境不存在。
  echo 请先使用任一受支持版本创建环境，例如：py -3.12 -m venv .venv
  echo 然后执行：.venv\Scripts\python.exe -m pip install -e ".[dev,package]"
  pause
  exit /b 1
)

"%PYTHON_BIN%" -c "import sys, sysconfig; supported = sys.implementation.name == 'cpython' and sys.version_info[:2] in {(3, 11), (3, 12), (3, 13), (3, 14)} and not sysconfig.get_config_var('Py_GIL_DISABLED'); raise SystemExit(0 if supported else 1)"
if errorlevel 1 (
  echo 当前 .venv 不是受支持的标准 CPython 3.11-3.14 环境。
  echo 请删除并使用 CPython 3.11、3.12、3.13 或 3.14 重新创建 .venv。
  pause
  exit /b 1
)

echo 正在停止旧的 ClipSoon 打包实例...
taskkill /IM ClipSoon.exe /T >nul 2>&1

echo 正在停止旧的 ClipSoon 源码实例...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$python = [Regex]::Escape('%PROJECT_DIR%.venv\Scripts\python');" ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match ($python + 'w?\.exe') -and $_.CommandLine -match '-m\s+clipsoon' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" ^
  >nul 2>&1

echo 正在从当前源码启动 ClipSoon（不会执行打包）...
cd /d "%PROJECT_DIR%"
"%PYTHON_BIN%" -m clipsoon --show
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo ClipSoon 异常退出，退出码：%EXIT_CODE%
  echo Python 异常日志：%LOCALAPPDATA%\ClipSoon\logs\clipsoon.log
  echo 原生崩溃日志：%LOCALAPPDATA%\ClipSoon\logs\native-crash.log
  pause
)
exit /b %EXIT_CODE%
