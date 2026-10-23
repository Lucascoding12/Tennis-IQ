@echo off
rem Double-click this on Windows to start Tennis IQ.
cd /d "%~dp0"
if not defined PORT set PORT=8000

where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Tennis IQ needs Python 3.11 or newer. Install it from https://www.python.org/downloads/
  echo ^(tick "Add python.exe to PATH" during install^), then run this again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\uvicorn.exe" (
  echo First run: installing ^(takes a minute or two^)...
  %PY% -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r backend\requirements.txt || goto :fail
)

echo Starting Tennis IQ at http://localhost:%PORT%
echo The first start downloads ATP data (about 2-3 minutes); the site works while that finishes.
echo Leave this window open. Close it to stop.
start "" "http://localhost:%PORT%"
".venv\Scripts\uvicorn.exe" app:app --app-dir backend --host 127.0.0.1 --port %PORT%
pause
exit /b 0

:fail
echo Install failed. Check your internet connection and try again.
rmdir /s /q .venv 2>nul
pause
exit /b 1
