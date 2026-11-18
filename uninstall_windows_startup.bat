@echo off
setlocal

:: ==============================================================================
:: Agent WASP - Windows Boot Auto-Start Removal
:: ==============================================================================

title Agent WASP - Remove Auto-Start
color 0C

cd /d "%~dp0"

set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS_FILE=%STARTUP_DIR%\AgentWASP_AutoStart.vbs"
set "OLD_LNK=%STARTUP_DIR%\AgentWASP_Gentle_Startup.lnk"

echo [Agent WASP] Removing Auto-Start shortcuts from Windows Startup folder...

if exist "%VBS_FILE%" (
    del /f /q "%VBS_FILE%"
    echo [Removed] %VBS_FILE%
)

if exist "%OLD_LNK%" (
    del /f /q "%OLD_LNK%"
    echo [Removed] %OLD_LNK%
)

echo.
echo ==============================================================================
echo [OK] Agent WASP will no longer start automatically on Windows boot.
echo You can still start it manually at any time by running start_standalone.bat.
echo ==============================================================================

exit /b 0
