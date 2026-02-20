@echo off
REM Class Transcriber - Windows Startup Script
REM This script sets up and runs the Class Transcriber application

cd /d "%~dp0"

REM -------------------------------
REM Step 0: Check Python version
REM -------------------------------
echo Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ❌ Python is not installed or not in PATH.
    echo.
    echo Please install Python 3.9 or higher from:
    echo https://www.python.org/downloads/windows/
    echo.
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

python --version
echo.

REM -------------------------------
REM Step 1: Create venv if not exists
REM -------------------------------
if not exist ".venv" (
    echo 👉 Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ❌ Failed to create virtual environment.
        pause
        exit /b 1
    )
)

REM -------------------------------
REM Step 2: Activate venv
REM -------------------------------
echo 👉 Activating virtual environment...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ❌ Failed to activate virtual environment.
    pause
    exit /b 1
)

REM -------------------------------
REM Step 3: Upgrade pip & install dependencies
REM -------------------------------
echo 👉 Installing/updating dependencies...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ❌ Failed to install dependencies.
    pause
    exit /b 1
)

REM -------------------------------
REM Step 4: Run Streamlit app
REM -------------------------------
echo.
echo 🚀 Starting Class Transcriber...
echo The app will open in your default browser automatically.
echo.
echo To stop the app, press Ctrl+C in this window.
echo.

REM Run Streamlit (it will automatically open the browser)
python -m streamlit run app.py

REM If we get here, the app has stopped
echo.
echo Application stopped.
pause

