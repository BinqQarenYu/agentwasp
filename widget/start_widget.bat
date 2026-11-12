@echo off
setlocal
cd /d "%~dp0"

:: Check for python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python not found. Please install Python to run the Agent Wasp Widget.
    pause
    exit /b 1
)

:: Ensure psutil and Pillow are installed without halting startup
python -c "import PIL, psutil" >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing widget dependencies in background...
    pip install Pillow psutil --quiet
)

:: Run the widget in background mode with BelowNormal priority (doesn't open console window)
start "" /belownormal pythonw wasp_widget.py

exit /b 0
