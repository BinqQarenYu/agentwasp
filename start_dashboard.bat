@echo off
setlocal
cd /d "%~dp0"

title Agent WASP - Web Cockpit & UI/UX Runner
color 0E

echo ==============================================================================
echo [Agent WASP] Starting Native Web Cockpit UI/UX...
echo URL: http://127.0.0.1:1434/chat
echo ==============================================================================

python run_dashboard_native.py
pause
