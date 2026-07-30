@echo off
setlocal
cd /d "%~dp0\.."

set "MODE=onedir"
if not "%~1"=="" (
  if /i "%~1"=="onedir" (
    set "MODE=onedir"
  ) else if /i "%~1"=="--onedir" (
    set "MODE=onedir"
  ) else if /i "%~1"=="onefile" (
    set "MODE=onefile"
  ) else if /i "%~1"=="--onefile" (
    set "MODE=onefile"
  ) else (
    echo Usage: build_windows.bat [onedir^|onefile]
    exit /b 2
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo Create .venv with Python 3.12 and install ".[package]" first.
  exit /b 1
)

if /i "%MODE%"=="onefile" (
  ".venv\Scripts\python.exe" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --windowed ^
    --onefile ^
    --name ClipSoon ^
    --collect-submodules pynput ^
    clipsoon\launcher.py
  if errorlevel 1 exit /b 1
  if not exist "dist\ClipSoon.exe" (
    echo ERROR: dist\ClipSoon.exe was not produced.
    exit /b 1
  )
  echo Built standalone EXE: %CD%\dist\ClipSoon.exe
  echo Note: one-file starts slower because it extracts runtime files to a temporary directory.
) else (
  ".venv\Scripts\python.exe" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --windowed ^
    --onedir ^
    --name ClipSoon ^
    --collect-submodules pynput ^
    clipsoon\launcher.py
  if errorlevel 1 exit /b 1
  if not exist "dist\ClipSoon\ClipSoon.exe" (
    echo ERROR: dist\ClipSoon\ClipSoon.exe was not produced.
    exit /b 1
  )
  if not exist "dist\ClipSoon\_internal\python312.dll" (
    if not exist "dist\ClipSoon\python312.dll" (
      echo ERROR: python312.dll was not collected into the portable package.
      exit /b 1
    )
  )
  echo Built portable folder: %CD%\dist\ClipSoon
  echo Run %CD%\dist\ClipSoon\ClipSoon.exe after copying or extracting the whole folder.
  echo Do not move only ClipSoon.exe; it needs _internal\python312.dll and Qt libraries.
  echo For a single EXE, run: build_windows.bat onefile
)
