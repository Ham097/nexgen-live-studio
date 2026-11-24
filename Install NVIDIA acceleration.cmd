@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call "Install NexGen Live Studio.cmd"
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-cuda.txt
if errorlevel 1 (
    echo NVIDIA runtime installation failed.
    pause
    exit /b 1
)
echo NVIDIA runtime installed. Restart Live Studio.
pause
