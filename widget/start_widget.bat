@echo off
setlocal
cd /d "%~dp0"

:: Check for python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python not found. Please install Python to run the Agent Wasp Widget.
    pause
    exit /b
)

:: Install Pillow dependency only if missing to make startups instantaneous
python -c "import PIL" >nul 2>&1
if %errorlevel% neq 0 (
    echo Pillow dependency not found. Installing requirements...
    pip install -r requirements.txt --quiet
)

:: Run the widget in background mode (pythonw doesn't open a console window)
start "" pythonw wasp_widget.py

echo Agent Wasp Widget Started!
echo You can find it in the bottom right of your screen.
timeout /t 2 >nul
exit
