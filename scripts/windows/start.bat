@echo off
REM Double-click entry for Chinese Windows sellers. Delegates to start.ps1 (UTF-8 BOM).
setlocal
cd /d "%~dp0\..\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set ERR=%ERRORLEVEL%
if not "%ERR%"=="0" (
  echo.
  echo [ERROR] Start failed, exit code %ERR%. Run scripts\windows\install.ps1 first.
  pause
)
exit /b %ERR%
