@echo off
REM One-click installer launcher for naive Windows users.
REM Put this file in the same folder as Flymoon Setup*.exe and double-click it.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set INSTALLER=
for /f "delims=" %%F in ('dir /b /o:-d "*Setup*.exe" 2^>nul') do (
    set INSTALLER=%%F
    goto :found
)

:found
if not defined INSTALLER (
    echo Could not find Flymoon Setup*.exe in this folder.
    echo Make sure this launcher is in the extracted ZIP folder.
    pause
    exit /b 1
)

echo Launching installer:
echo   %INSTALLER%
start "" "%INSTALLER%"

exit /b 0

