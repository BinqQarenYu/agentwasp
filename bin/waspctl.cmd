@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
python "%SCRIPT_DIR%..\containers\agent-core\src\harnesses\waspctl.py" %*
exit /b %ERRORLEVEL%
