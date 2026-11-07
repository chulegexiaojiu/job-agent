@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python --version >nul 2>nul
if errorlevel 1 (
    echo Python 3.10 or newer is required. Enable Tcl/Tk and Add Python to PATH.
    pause
    exit /b 2
)
python -c "import tkinter, inspect, pydantic, docx, pypdf, pywinauto; from PIL import ImageGrab; assert 'window' in inspect.signature(ImageGrab.grab).parameters" >nul 2>nul
if errorlevel 1 (
    echo Installing local workbench components...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Installation failed. Check your network and Python setup.
        pause
        exit /b 2
    )
)
python -m job_agent.app
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
    echo Workbench exited with error: %APP_EXIT%
    pause
)
exit /b %APP_EXIT%
