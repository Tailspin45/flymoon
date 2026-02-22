@echo off
REM Build script for Flymoon desktop app (Windows)
REM Produces: dist-electron\Flymoon-Setup-{version}.exe
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
echo   Flymoon build (Windows)
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

REM ── 2. PyInstaller ──────────────────────────────────────────────────────
echo ^> Running PyInstaller...
pyinstaller flymoon.spec --distpath "%ROOT%\electron\assets\bin" --workpath "%TEMP%\pyi-build" --noconfirm
move /Y "%ROOT%\electron\assets\bin\flymoon-server.exe" "%ROOT%\flymoon-server.exe"
echo    flymoon-server.exe built

REM ── 3. npm install ──────────────────────────────────────────────────────
echo ^> Installing npm dependencies...
cd electron
call npm install --save-dev electron electron-builder

REM ── 4. electron-builder ─────────────────────────────────────────────────
echo ^> Building Electron installer...
call npx electron-builder --win

cd ..
echo.
echo Build complete^^! Output in: %ROOT%\dist-electron\
endlocal
