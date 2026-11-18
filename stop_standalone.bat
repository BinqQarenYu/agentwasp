@echo off
setlocal

:: ==============================================================================
:: Agent WASP - Standalone Graceful Stop
:: ==============================================================================
:: Safely shuts down containers, continuous worker daemon, and HUD widget.
:: ==============================================================================

title Agent WASP - Graceful Shutdown
color 0C

cd /d "%~dp0"

echo ==============================================================================
echo [Agent WASP] Stopping Stack and Background Workers...
echo ==============================================================================

:: --- 1. Stop 24/7 Worker Daemon (Port 14348) ---
echo [Stop] Stopping Continuous Worker Daemon...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 14348 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"

:: --- 2. Stop Desktop HUD Widget (Port 14349) ---
echo [Stop] Stopping Desktop Widget HUD...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 14349 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"

:: --- 3. Stop Docker Containers ---
echo [Stop] Stopping Docker Compose services...
docker compose stop

echo.
echo ==============================================================================
echo [OK] Agent WASP has been safely stopped. All resources released.
echo ==============================================================================

exit /b 0
