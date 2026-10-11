@echo off
REM Double-click to open the app. Keep the black window open while you use it.
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Run setup.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
coop-apply gui
pause
