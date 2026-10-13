#!/bin/bash
set -e
cd "$(dirname "$0")"

# -------------------------------
# Step 0: Check Python version
# -------------------------------
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
REQUIRED="3.9"

if [ "$(printf '%s\n' "$REQUIRED" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED" ]; then
  echo "❌ Python $REQUIRED or higher is required (found $PYTHON_VERSION)."
  echo ""
  echo " Please upgrade manually:"
  echo "   • On macOS (Homebrew):  brew install python@3.11"
  echo "   • On Ubuntu/Debian:    sudo apt-get install python3.11"
  echo "   • On Windows:          Download from https://www.python.org/downloads/windows/"
  exit 1
fi

# -------------------------------
# Step 1: Create venv if not exists
# -------------------------------
if [ ! -d ".venv" ]; then
  echo "👉 Creating virtual environment..."
  python3 -m venv .venv
fi

# -------------------------------
# Step 2: Activate venv
# -------------------------------
echo "👉 Activating virtual environment..."
# shellcheck disable=SC1091
source .venv/bin/activate

# -------------------------------
# Step 3: Upgrade pip & install dependencies
# -------------------------------
echo "👉 Installing dependencies..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

# -------------------------------
# Step 4: Run Streamlit app
# -------------------------------
echo "🚀 Starting Streamlit app..."
exec python3 -m streamlit run app.py
