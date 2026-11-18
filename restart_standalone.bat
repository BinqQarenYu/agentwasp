@echo off
setlocal

:: ==============================================================================
:: Agent WASP - Standalone Fast Restart
:: ==============================================================================
:: Gracefully restarts the core container and background workers in under 10s.
:: Does NOT require Antigravity. Preserves low host CPU and memory limits.
:: ==============================================================================

title Agent WASP - Standalone Fast Restart
color 0B

cd /d "%~dp0"

echo ==============================================================================
echo [Agent WASP] Restarting Autonomous Stack and Background Workers...
echo ==============================================================================

:: --- 1. Terminate Running Background Daemon if Restart Requested ---
echo [Restart] Releasing worker daemon lock (Port 14348)...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 14348 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"

if "%1"=="--full" (
    echo [Restart] Full container teardown requested...
    docker compose restart
) else (
    echo [Restart] Restarting Agent WASP Core Container...
    docker compose restart agent-core
)

:: --- 2. Re-trigger Standalone Bootstrapper ---
echo [Restart] Bootstrapping fresh workers and Web Cockpit...
call "%~dp0start_standalone.bat" %*

exit /b 0
