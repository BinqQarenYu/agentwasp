@echo off
:: ==============================================================================
:: Agent WASP - Gentle Standalone Bootstrapper (Fast Modern Launcher)
:: ==============================================================================
:: Automatically forwards to start_standalone.bat with full CPU/RAM safety.
:: ==============================================================================

cd /d "%~dp0"
call "%~dp0start_standalone.bat" %*
exit /b %errorlevel%
