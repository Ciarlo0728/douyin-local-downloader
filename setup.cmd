@echo off
cd /d "%~dp0"
py -3.12 -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.lock.txt
if errorlevel 1 goto failed
echo Setup complete. Double-click start.cmd.
pause
exit /b 0
:failed
echo Setup failed. Python 3.12 and Microsoft Edge are required.
pause
exit /b 1
