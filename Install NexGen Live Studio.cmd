@echo off
setlocal
cd /d "%~dp0"
echo Setting up NexGen Live Studio
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed
> ".venv\installed-v1" echo installed
exit /b 0

:failed
echo.
echo Setup did not finish. Confirm Python 3.12 64-bit with the Windows launcher is installed.
echo Check your internet connection and open the Live Studio launcher again.
pause
exit /b 1
