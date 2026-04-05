@echo off
REM One-click installer launcher for Zipcatcher on Windows.
REM Double-click this file to install Zipcatcher - no other software required.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set INSTALLER=
for /f "delims=" %%F in ('dir /b /o:-d "*Setup*.exe" 2^>nul') do (
    set INSTALLER=%%F
    goto :found
)

:found
if not defined INSTALLER (
    echo Could not find the Zipcatcher installer in this folder.
    echo Make sure you extracted the full ZIP before running this file.
    pause
    exit /b 1
)

echo Launching Zipcatcher installer: %INSTALLER%
start "" "%INSTALLER%"

exit /b 0

