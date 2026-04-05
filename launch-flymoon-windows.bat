@echo off
REM Local launcher for Zipcatcher on Windows (from source checkout).
REM Starts app.py and opens the UI in your configured/default browser.

setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo Creating Python virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo Failed to activate virtual environment.
    exit /b 1
)

if not exist ".env" if exist ".env.mock" copy /Y ".env.mock" ".env" >nul

echo Installing/updating Python dependencies...
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo Starting Zipcatcher...
python app.py

endlocal

