@echo off
REM Double-click to open the app. Keep the black window open while you use it.
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Run setup.bat first.
  pause
  exit /b 1
)
if not exist src\coop_apply\gui.py (
  echo The app files are missing from this folder: src\coop_apply\gui.py not found.
  echo Download the latest ZIP and copy its contents into this folder.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
REM Always run the code that sits next to this file, even if an older copy was installed elsewhere.
set "PYTHONPATH=%~dp0src"
python -m coop_apply.cli gui
pause
