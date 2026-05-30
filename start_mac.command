#!/bin/bash
# ── EditOps Launcher (Mac) ──────────────────────────────────────────────

cd "$(dirname "$0")"

echo ""
echo "🎬  EditOps — Money Mediia"
echo "────────────────────────────"

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "❌  Python 3 not found."
  echo "    Install it from https://www.python.org/downloads/"
  read -p "Press Enter to exit..."
  exit 1
fi

# Check ffmpeg
if ! command -v ffmpeg &>/dev/null; then
  echo "⚠️   ffmpeg not found. Installing via Homebrew..."
  if ! command -v brew &>/dev/null; then
    echo "    Homebrew not found. Install ffmpeg manually:"
    echo "    https://ffmpeg.org/download.html"
    read -p "Press Enter to exit..."
    exit 1
  fi
  brew install ffmpeg
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
  echo "📦  Setting up virtual environment (first time only)..."
  python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install / update dependencies inside the venv
echo "📦  Checking Python dependencies..."
pip install -r requirements.txt -q

# Free port 5001 if a previous instance is still running
if lsof -ti :5001 &>/dev/null; then
  echo "🔄  Freeing port 5001 from previous session..."
  lsof -ti :5001 | xargs kill -9
  sleep 0.5
fi

# Launch app in background (auto-update runs inside app.py on startup)
python app.py &
SERVER_PID=$!

# Kill Flask cleanly when this script exits (Ctrl+C or window close)
trap "kill $SERVER_PID 2>/dev/null; exit" INT TERM EXIT

# Wait until server is actually responding, then open browser
echo "⏳  Waiting for server to start..."
until curl -s http://localhost:5001 > /dev/null 2>&1; do
  sleep 0.5
done
echo "🌐  Opening browser..."
open http://localhost:5001

# Keep terminal open until Ctrl+C
wait $SERVER_PID
