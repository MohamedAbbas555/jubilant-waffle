@echo off
REM One-time setup: double-click this file.
cd /d "%~dp0"
echo Setting up Co-op Apply (this takes a few minutes)...
python -m venv .venv || goto :fail
call .venv\Scripts\activate.bat || goto :fail
python -m pip install --upgrade pip >nul
pip install -e . || goto :fail
playwright install chromium || goto :fail
echo.
echo Done! From now on, double-click "Start Co-op Apply.bat".
pause
exit /b 0
:fail
echo.
echo Setup failed. Make sure Python is installed and "Add python.exe to PATH" was ticked.
pause
exit /b 1
