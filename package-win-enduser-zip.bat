@echo off
REM Package a naive-user Windows ZIP from existing installer artifacts.
REM Output: dist-windows\Flymoon-Windows-EndUser.zip
REM Prerequisite: run build-win-oneclick.bat (or build-win.bat) first.

setlocal enabledelayedexpansion
cd /d "%~dp0"
set ROOT=%CD%

set DIST_ELECTRON=%ROOT%\dist-electron
set DIST_WINDOWS=%ROOT%\dist-windows
set STAGE=%DIST_WINDOWS%\Flymoon-Windows-EndUser
set ZIP=%DIST_WINDOWS%\Flymoon-Windows-EndUser.zip

if not exist "%DIST_ELECTRON%" (
    echo ERROR: %DIST_ELECTRON% does not exist.
    echo Build the installer first with build-win-oneclick.bat
    exit /b 1
)

set INSTALLER=
for /f "delims=" %%F in ('dir /b /o:-d "%DIST_ELECTRON%\*Setup*.exe" 2^>nul') do (
    set INSTALLER=%DIST_ELECTRON%\%%F
    goto :found_installer
)

:found_installer
if not defined INSTALLER (
    echo ERROR: No installer EXE found in %DIST_ELECTRON%.
    echo Build the installer first with build-win-oneclick.bat
    exit /b 1
)

echo Using installer:
echo   %INSTALLER%

if not exist "%DIST_WINDOWS%" mkdir "%DIST_WINDOWS%"
if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%STAGE%"

copy /Y "%INSTALLER%" "%STAGE%\" >nul
copy /Y "%ROOT%\START-HERE-Install-Flymoon.bat" "%STAGE%\" >nul
copy /Y "%ROOT%\README-WINDOWS.txt" "%STAGE%\" >nul

if exist "%ZIP%" del /q "%ZIP%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Compress-Archive -Path '%STAGE%\*' -DestinationPath '%ZIP%' -Force"

if errorlevel 1 (
    echo ERROR: Failed to create ZIP archive.
    exit /b 1
)

echo.
echo Created:
echo   %ZIP%
echo.
echo Contents:
dir /b "%STAGE%"

exit /b 0
