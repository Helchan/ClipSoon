@echo off
setlocal EnableExtensions

set "PROJECT_DIR=%~dp0"
set "PYTHON_BIN=%PROJECT_DIR%.venv\Scripts\python.exe"

if not exist "%PYTHON_BIN%" (
  echo ClipSoon 的 Python 3.12 开发环境不存在。
  echo 请先执行：py -3.12 -m venv .venv
  echo 然后执行：.venv\Scripts\python.exe -m pip install -e ".[dev,package]"
  pause
  exit /b 1
)

echo 正在停止旧的 ClipSoon 打包实例...
taskkill /IM ClipSoon.exe /T >nul 2>&1

echo 正在停止旧的 ClipSoon 源码实例...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$python = [Regex]::Escape('%PYTHON_BIN%');" ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $python -and $_.CommandLine -match '-m\s+clipsoon' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" ^
  >nul 2>&1

echo 正在从当前源码启动 ClipSoon（不会执行打包）...
cd /d "%PROJECT_DIR%"
"%PYTHON_BIN%" -m clipsoon --show
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
