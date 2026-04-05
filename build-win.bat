@echo off
REM Build script for Zipcatcher desktop app (Windows)
REM Produces: dist-electron\Zipcatcher-Setup-{version}.exe
REM
REM Prerequisites:
REM   - Python 3.9+  https://python.org/downloads/
REM   - Node.js 18+  https://nodejs.org/
REM   - Git           https://git-scm.com/
REM   Run from the flymoon repo root in a regular Command Prompt or PowerShell.

setlocal enabledelayedexpansion
cd /d "%~dp0"
set ROOT=%CD%

echo ===============================================
echo   Zipcatcher build (Windows)
echo ===============================================

REM ── 1. Python venv ──────────────────────────────────────────────────────
if not exist .venv (
    echo ^> Creating virtualenv...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q --upgrade pip
pip install -q -r requirements.txt
pip install -q pyinstaller
echo ^> Downloading astronomical data (de421.bsp)...
python -c "from skyfield.api import load; load('de421.bsp')"

REM ── 2. PyInstaller ──────────────────────────────────────────────────────
echo ^> Running PyInstaller...
pyinstaller flymoon.spec --distpath "%ROOT%\electron\assets\bin" --workpath "%TEMP%\pyi-build" --noconfirm
move /Y "%ROOT%\electron\assets\bin\flymoon-server.exe" "%ROOT%\flymoon-server.exe"
if not exist "%ROOT%\flymoon-server.exe" (
    echo ERROR: flymoon-server.exe not found after PyInstaller build.
    exit /b 1
)
echo    flymoon-server.exe built

REM ── 3. npm install ──────────────────────────────────────────────────────
echo ^> Installing npm dependencies...
cd electron
call npm install --save-dev electron electron-builder

REM ── 4. Copy server binary into electron dir for packaging ────────────────
echo ^> Copying flymoon-server.exe into electron folder...
copy /Y "%ROOT%\flymoon-server.exe" "%ROOT%\electron\flymoon-server.exe"
if errorlevel 1 (
    echo ERROR: Could not copy flymoon-server.exe into electron folder.
    exit /b 1
)

REM ── 5. electron-builder ─────────────────────────────────────────────────
echo ^> Building Electron installer...
call npx electron-builder --win

cd ..
echo.
echo Build complete^^! Output in: %ROOT%\dist-electron\
endlocal
