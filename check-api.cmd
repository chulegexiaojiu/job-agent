@echo off
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
where python >nul 2>nul
if errorlevel 1 (
    echo Python 3.10 or newer is required. Install Python and enable PATH.
    pause
    exit /b 2
)
python -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo Python tkinter is required. Repair Python with Tcl/Tk enabled.
    pause
    exit /b 2
)
python -c "from PIL import Image, ImageDraw, ImageFont" >nul 2>nul
if errorlevel 1 (
    echo Installing the image test component...
    python -m pip install -r requirements-probe.txt
    if errorlevel 1 (
        echo Component installation failed. Check your network.
        pause
        exit /b 2
    )
)
python -m job_agent.setup_ui
set "PROBE_EXIT=%ERRORLEVEL%"
if not "%PROBE_EXIT%"=="0" (
    echo Setup window exited with error: %PROBE_EXIT%
    pause
)
exit /b %PROBE_EXIT%
