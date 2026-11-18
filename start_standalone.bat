@echo off
setlocal

:: ==============================================================================
:: Agent WASP - Fast Standalone Bootstrapper & Low-Impact Resource Governor
:: ==============================================================================
:: Boots the complete Agent WASP stack (Docker, Local Ollama, 24/7 Worker Daemon,
:: Glassmorphic HUD Widget, and Native Web Cockpit) in seconds with balanced,
:: equal CPU and memory management. Antigravity is NOT required to restart or run.
:: ==============================================================================

title Agent WASP - Standalone Fast Bootstrapper
color 0E

cd /d "%~dp0"

echo ==============================================================================
echo [Agent WASP] Initiating Fast Standalone Bootstrapper...
echo ==============================================================================

:: --- 1. Boot Delay Check ---
if "%1"=="--boot" (
    echo [Boot Mode] Windows PC restart detected. Allowing 3s for system network stack...
    timeout /t 3 /nobreak >nul
)

:: --- 2. Host RAM & Resource Safety Check ---
echo [1/6] Verifying Host CPU and RAM headroom...
python scripts\verify_ram.py
if not errorlevel 1 goto RAM_OK
echo [WARNING] Host RAM is heavily saturated.
echo Pausing startup to protect Windows desktop stability.
exit /b 1

:RAM_OK
:: --- 3. Local Model Engine (Ollama) Verification ---
echo [2/6] Checking Local Inference Engine (Ollama)...
python scripts\start_ollama.py

:: --- 4. Docker Desktop Engine Verification ---
echo [3/6] Verifying Docker Desktop Daemon...
docker info >nul 2>&1
if not errorlevel 1 goto DOCKER_READY

echo [Docker] Docker Desktop is offline. Starting in background...
if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
    start "" /belownormal "C:\Program Files\Docker\Docker\Docker Desktop.exe"
)
echo [Docker] Waiting for Docker Engine pipe to become responsive...
set /a DOCKER_WAIT=0

:WAIT_DOCKER
timeout /t 2 /nobreak >nul
docker info >nul 2>&1
if not errorlevel 1 goto DOCKER_READY
set /a DOCKER_WAIT+=2
if %DOCKER_WAIT% geq 30 (
    echo [Error] Docker Desktop engine did not finish starting within 30s.
    exit /b 1
)
echo [Docker] Connecting to engine... (%DOCKER_WAIT%s/30s)
goto WAIT_DOCKER

:DOCKER_READY
echo [Docker] Docker Engine is online and responsive.

:: --- 5. Start Core Docker Stack in Parallel ---
echo [4/6] Bringing up containers in parallel with resource caps...
docker compose up -d agent-redis agent-postgres agent-core agent-broker

:: --- 6. Fast Health Readiness Verification ---
echo [5/6] Waiting for Agent WASP Core to be ready (http://127.0.0.1:1434/health)...
python scripts\check_health.py

:: --- 7. Launch Autonomous 24/7 Continuous Worker Daemon ---
echo [6/6] Launching 24/7 Autonomous Worker Daemon (BelowNormal priority)...
netstat -ano | findstr ":14348 " >nul 2>&1
if not errorlevel 1 goto DAEMON_ACTIVE
start "" /belownormal pythonw scripts\run_continuous_worker_daemon.py
echo [Daemon] Started 24/7 worker daemon for ATSE, Prometheus, Hermes, Athena.
goto WIDGET_CHECK

:DAEMON_ACTIVE
echo [Daemon] 24/7 worker daemon is already active on port 14348.

:WIDGET_CHECK
:: --- 8. Launch Mini Desktop Glassmorphic Widget ---
echo [Widget] Ensuring Agent WASP Desktop HUD Widget is active...
netstat -ano | findstr ":14349 " >nul 2>&1
if not errorlevel 1 goto WIDGET_ACTIVE
start "" /belownormal pythonw widget\wasp_widget.py
echo [Widget] Desktop HUD Widget active.
goto UI_CHECK

:WIDGET_ACTIVE
echo [Widget] Desktop HUD Widget is already running on port 14349.

:UI_CHECK
:: --- 9. Launch Standalone Web Cockpit App ---
if "%1"=="--no-browser" goto FINISH
if "%1"=="--headless" goto FINISH

echo.
echo [UI/UX] Opening Web Cockpit in standalone application window...
python scripts\open_cockpit.py

:FINISH
echo.
echo ==============================================================================
echo [SUCCESS] Agent WASP Autonomous Stack is RUNNING!
echo Dashboard: http://127.0.0.1:1434/overview
echo Chat:      http://127.0.0.1:1434/chat
echo Mesh:      http://127.0.0.1:1434/mesh
echo ==============================================================================

if "%1"=="--boot" (
    exit /b 0
)

exit /b 0
