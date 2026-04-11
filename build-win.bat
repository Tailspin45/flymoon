@echo off
REM Build script for Zipcatcher desktop app (Windows)
REM Produces: dist-electron\Zipcatcher-Setup-{version}.exe
REM
REM Prerequisites:
REM   - Python 3.9+  https://python.org/downloads/
REM   - Node.js 18+  https://nodejs.org/
REM   - Git           https://git-scm.com/
REM   Run from the zipcatcher repo root in a regular Command Prompt or PowerShell.

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

REM ── 2. PyInstaller (onedir mode) ────────────────────────────────────────
REM Output: electron\zipcatcher-server\zipcatcher-server.exe + _internal\
echo ^> Running PyInstaller (onedir)...
if exist "%ROOT%\electron\zipcatcher-server" rmdir /S /Q "%ROOT%\electron\zipcatcher-server"
pyinstaller zipcatcher.spec --distpath "%ROOT%\electron" --workpath "%TEMP%\pyi-build" --noconfirm
if not exist "%ROOT%\electron\zipcatcher-server\zipcatcher-server.exe" (
    echo ERROR: zipcatcher-server.exe not found after PyInstaller build.
    exit /b 1
)
echo    zipcatcher-server built

REM ── 3. npm install ──────────────────────────────────────────────────────
echo ^> Installing npm dependencies...
cd electron
call npm install --save-dev electron electron-builder

REM ── 5. electron-builder ─────────────────────────────────────────────────
echo ^> Building Electron installer...
call npx electron-builder --win

cd ..
echo.
echo Build complete^^! Output in: %ROOT%\dist-electron\
endlocal
