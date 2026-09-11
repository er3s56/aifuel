@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-installer.ps1" %*
set "buildResult=%ERRORLEVEL%"
if not "%buildResult%"=="0" echo Installer build failed. See the error above.
echo.
pause
exit /b %buildResult%
