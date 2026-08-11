@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."

set "PYTHON_BIN=.venv\Scripts\python.exe"

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

if not exist "%PYTHON_BIN%" (
  echo Create .venv with standard CPython 3.11-3.14 and install ".[package]" first.
  exit /b 1
)

"%PYTHON_BIN%" -c "import sys, sysconfig; supported = sys.implementation.name == 'cpython' and sys.version_info[:2] in {(3, 11), (3, 12), (3, 13), (3, 14)} and not sysconfig.get_config_var('Py_GIL_DISABLED'); raise SystemExit(0 if supported else 'ERROR: ClipSoon packaging requires standard CPython 3.11-3.14.')"
if errorlevel 1 exit /b 1

set "PYTHON_DLL="
for /f "delims=" %%D in ('%PYTHON_BIN% -c "import sys; print(f'python{sys.version_info.major}{sys.version_info.minor}.dll')"') do set "PYTHON_DLL=%%D"
if not defined PYTHON_DLL (
  echo ERROR: Could not derive the CPython runtime DLL name from %PYTHON_BIN%.
  exit /b 1
)

if /i "%MODE%"=="onefile" (
  "%PYTHON_BIN%" -m PyInstaller ^
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
  "%PYTHON_BIN%" -m PyInstaller ^
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
  if not exist "dist\ClipSoon\_internal\%PYTHON_DLL%" (
    if not exist "dist\ClipSoon\%PYTHON_DLL%" (
      echo ERROR: %PYTHON_DLL% was not collected into the portable package.
      exit /b 1
    )
  )
  echo Built portable folder: %CD%\dist\ClipSoon
  echo Run %CD%\dist\ClipSoon\ClipSoon.exe after copying or extracting the whole folder.
  echo Do not move only ClipSoon.exe; it needs %PYTHON_DLL% and Qt libraries from the portable folder.
  echo For a single EXE, run: build_windows.bat onefile
)
