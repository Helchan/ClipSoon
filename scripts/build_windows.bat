@echo off
setlocal
cd /d "%~dp0\.."
if not exist ".venv\Scripts\python.exe" (
  echo Create .venv with Python 3.12 and install ".[package]" first.
  exit /b 1
)
".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --onedir ^
  --name ClipSoon ^
  --collect-submodules pynput ^
  clipsoon\app.py
if errorlevel 1 exit /b %errorlevel%
echo Built: %CD%\dist\ClipSoon\ClipSoon.exe
