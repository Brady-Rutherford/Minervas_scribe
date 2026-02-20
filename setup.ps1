# Class Transcriber - PowerShell Startup Script
# This script sets up and runs the Class Transcriber application

# Change to script directory
Set-Location -Path $PSScriptRoot

# -------------------------------
# Step 0: Check Python version
# -------------------------------
Write-Host "Checking Python installation..." -ForegroundColor Cyan
try {
    $pythonVersion = python --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Python not found"
    }
    Write-Host $pythonVersion -ForegroundColor Green
    Write-Host ""
} catch {
    Write-Host ""
    Write-Host "❌ Python is not installed or not in PATH." -ForegroundColor Red
    Write-Host ""
    Write-Host "Please install Python 3.9 or higher from:" -ForegroundColor Yellow
    Write-Host "https://www.python.org/downloads/windows/" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Make sure to check 'Add Python to PATH' during installation." -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

# -------------------------------
# Step 1: Create venv if not exists
# -------------------------------
if (-not (Test-Path ".venv")) {
    Write-Host "👉 Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ Failed to create virtual environment." -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# -------------------------------
# Step 2: Activate venv
# -------------------------------
Write-Host "👉 Activating virtual environment..." -ForegroundColor Cyan
& ".venv\Scripts\Activate.ps1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Failed to activate virtual environment." -ForegroundColor Red
    Write-Host "Note: You may need to run: Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser" -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit 1
}

# -------------------------------
# Step 3: Upgrade pip & install dependencies
# -------------------------------
Write-Host "👉 Installing/updating dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Failed to install dependencies." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# -------------------------------
# Step 4: Run Streamlit app
# -------------------------------
Write-Host ""
Write-Host "🚀 Starting Class Transcriber..." -ForegroundColor Green
Write-Host "The app will open in your default browser automatically." -ForegroundColor Cyan
Write-Host ""
Write-Host "To stop the app, press Ctrl+C in this window." -ForegroundColor Yellow
Write-Host ""

# Run Streamlit (it will automatically open the browser)
python -m streamlit run app.py

# If we get here, the app has stopped
Write-Host ""
Write-Host "Application stopped." -ForegroundColor Yellow
Read-Host "Press Enter to exit"

