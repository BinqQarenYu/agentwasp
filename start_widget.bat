@echo off
setlocal
cd /d "%~dp0\widget"
start "" /belownormal pythonw wasp_widget.py
exit /b 0
