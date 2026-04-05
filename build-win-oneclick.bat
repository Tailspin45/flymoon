@echo off
REM One-click Windows build + installer launcher for Zipcatcher
REM Usage: double-click this file from the repository root on Windows.

setlocal enabledelayedexpansion
cd /d "%~dp0"
set ROOT=%CD%

echo ===============================================
echo   Zipcatcher one-click Windows installer build
echo ===============================================

REM Ensure .env exists
if not exist "%ROOT%\.env" (
    if exist "%ROOT%\.env.mock" (
        echo ^> Creating .env from .env.mock...
        copy /Y "%ROOT%\.env.mock" "%ROOT%\.env" >nul
    )
)

call "%ROOT%\build-win.bat"
if errorlevel 1 (
    echo.
    echo Build failed.
    exit /b 1
)

set INSTALLER=
for /f "delims=" %%F in ('dir /b /o:-d "%ROOT%\dist-electron\*Setup*.exe" 2^>nul') do (
    set INSTALLER=%ROOT%\dist-electron\%%F
    goto :found
)

:found
if not defined INSTALLER (
    echo.
    echo No installer EXE found in dist-electron.
    exit /b 1
)

echo.
echo Launching installer:
echo   %INSTALLER%
start "" "%INSTALLER%"

echo.
echo Installer started. Follow setup prompts.
exit /b 0

