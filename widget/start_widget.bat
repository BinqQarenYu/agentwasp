@echo off
setlocal
cd /d "%~dp0"

:: Check for python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python not found. Please install Python.
    pause
    exit /b
)

:: Install dependencies if needed
pip install -r requirements.txt --quiet

:: Run the widget in background mode (pythonw doesn't open a terminal)
start "" pythonw wasp_widget.py

echo Agent Wasp Widget Started!
echo You can find it in the bottom right of your screen.
timeout /t 3 >nul
exit
