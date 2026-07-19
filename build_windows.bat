@echo off
setlocal

echo 正在构建 Windows ClipSoon.exe...
call "%~dp0scripts\build_windows.bat"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
