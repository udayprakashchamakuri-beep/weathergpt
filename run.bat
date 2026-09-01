@echo off
setlocal
title WeatherGPT - SIH26068
cd /d "%~dp0backend"

if not exist "..\.venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -3 -m venv "..\.venv" 2>nul || python -m venv "..\.venv"
)
if not exist "..\.venv\Scripts\python.exe" (
  echo.
  echo Python was not found. Install Python 3.10+ from python.org and re-run.
  pause
  exit /b 1
)

set PY=..\.venv\Scripts\python.exe
echo Installing dependencies ^(first run only^)...
"%PY%" -m pip install --quiet --disable-pip-version-check --upgrade pip
"%PY%" -m pip install --quiet --disable-pip-version-check -r requirements.txt

echo.
echo   WeatherGPT is starting.
echo   UI    http://localhost:8000
echo   API   http://localhost:8000/docs
echo   Stop with Ctrl+C
echo.
start "" http://localhost:8000
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
