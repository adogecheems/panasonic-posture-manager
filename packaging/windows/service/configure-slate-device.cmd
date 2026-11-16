@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure-slate-device.ps1" %*
exit /b %ERRORLEVEL%
