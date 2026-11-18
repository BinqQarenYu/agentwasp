@echo off
setlocal

:: ==============================================================================
:: Agent WASP - Windows Boot Auto-Start Registration
:: ==============================================================================
:: Installs a silent, zero-popup VBS launcher in the user's Windows Startup folder
:: so Agent WASP, its workers, HUD widget, and Web Cockpit start automatically
:: whenever your PC boots or restarts, with BelowNormal CPU/RAM governance.
:: ==============================================================================

title Agent WASP - Auto-Start Setup
color 0A

cd /d "%~dp0"

echo ==============================================================================
echo [Agent WASP] Configuring Windows PC Reboot Auto-Start...
echo ==============================================================================

powershell -NoProfile -ExecutionPolicy Bypass -Command "$vbs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\AgentWASP_AutoStart.vbs'; $oldLnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\AgentWASP_Gentle_Startup.lnk'; if (Test-Path $oldLnk) { Remove-Item -Force $oldLnk }; $bat = (Get-Item 'start_standalone.bat').FullName.Trim(); $line2 = 'WshShell.Run \"cmd.exe /c \"\"\" & \"' + $bat + '\" & \"\"\" --boot\", 0, False'; $lines = @('Set WshShell = CreateObject(\"WScript.Shell\")', $line2); [IO.File]::WriteAllLines($vbs, $lines); if (Test-Path $vbs) { Write-Host '[SUCCESS] Registered:' $vbs } else { exit 1 }"

if errorlevel 1 (
    echo [ERROR] Failed to register startup script.
    exit /b 1
)

echo.
echo ==============================================================================
echo [SUCCESS] Agent WASP has been registered to Windows Startup!
echo Auto-Start File: %%APPDATA%%\Microsoft\Windows\Start Menu\Programs\Startup\AgentWASP_AutoStart.vbs
echo.
echo Next time your PC restarts:
echo - The complete stack boots in about 5 seconds in the background.
echo - CPU and RAM remain strictly guarded (BelowNormal priority, Docker caps).
echo - The Desktop HUD Widget will appear in the bottom-right corner.
echo - The Web Cockpit will open in standalone app mode.
echo - NO Antigravity is required!
echo ==============================================================================

exit /b 0
