@echo off
setlocal enabledelayedexpansion

:: ==============================================================================
:: Agent WASP - Gentle Standalone & Low-Impact Background Bootstrapper
:: ==============================================================================

title Agent WASP - Autonomous Worker Stack
color 0E

cd /d "%~dp0"

:: 1. Boot Delay Check
if "%1"=="--boot" (
    echo [Agent WASP] Windows Boot detected. Waiting 45s for OS boot storm to settle...
    timeout /t 45 >nul
) else (
    echo [Agent WASP] Initializing in low-priority gentle mode...
)

:: 2. Pre-Flight Hardware Health & Memory Check
echo [Pre-Flight] Verifying Host CPU and RAM headroom...
python -c "import psutil; mem = psutil.virtual_memory(); sys_ok = mem.percent < 85.0; print(f'RAM Usage: {mem.percent:.1f}%% (Threshold: 85%%) - Safe: {sys_ok}'); exit(0 if sys_ok else 1)"
if errorlevel 1 (
    echo [WARNING] Host RAM is saturated.
    echo Pausing Agent WASP launch to protect Windows desktop stability...
    exit /b 1
)

:: 3. Verify Docker Desktop Engine Status
echo [Pre-Flight] Checking Docker Daemon...
docker info >nul 2>&1
if errorlevel 1 (
    echo [Notice] Docker Desktop is starting in background...
    start "" /b /low "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    
    echo [Notice] Waiting up to 60s for Docker Engine pipe to initialize...
    for /l %%i in (1,1,12) do (
        docker info >nul 2>&1
        if !errorlevel! equ 0 (
            goto DOCKER_READY
        )
        echo Waiting for Docker Engine... (%%i/12)
        timeout /t 5 >nul
    )
    echo [Error] Docker Desktop engine did not finish starting.
    exit /b 1
)

:DOCKER_READY
echo [OK] Docker Engine is online and responsive.

:: 4. Sequential Staged Service Startup
echo.
echo [Stage 1/3] Starting Storage and Memory Layer (Redis and Postgres)...
docker compose up -d agent-redis agent-postgres
timeout /t 5 >nul

echo [Stage 2/3] Starting Autonomous Worker Core...
docker compose up -d agent-core
timeout /t 5 >nul

echo [Stage 3/3] Starting Integration Bridges (Telegram and Broker)...
docker compose up -d agent-telegram agent-broker

echo.
echo ==============================================================================
echo [SUCCESS] Agent WASP Autonomous Worker is running!
echo Dashboard: http://127.0.0.1:1434/
echo ==============================================================================

:: 5. Launch Mini Desktop Widget
echo [Widget] Ensuring Agent WASP Desktop Widget is active...
start /b "" /low cmd /c "cd /d %~dp0widget && call start_widget.bat"

if "%1"=="--boot" (
    exit /b 0
)

echo Agent WASP is running.
exit /b 0
