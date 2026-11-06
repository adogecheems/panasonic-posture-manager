@echo off
setlocal
cd /d "%~dp0\..\.."
set "PYTHONPATH=%CD%\src"
if not exist ".venv-windows\Scripts\python.exe" (
    echo Create .venv-windows and install packaging\windows\requirements-ui.txt first.
    exit /b 2
)
".venv-windows\Scripts\python.exe" -m panasonic_posture.fluent --demo %*
