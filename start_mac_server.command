#!/bin/bash
# ── EditOps Launcher (Mac, lite server build) ───────────────────────────────
# Same idea as start_mac.command, but installs from requirements-server.txt
# instead of requirements.txt — skips mlx-whisper, since a shared office
# server like this is meant to be used via the browser by multiple
# teammates, not for local Whisper transcription on this machine itself.
# Use this instead of start_mac.command on a Mac being set up as the
# shared EditOps server, so re-running it doesn't reinstall the heavy
# package this deployment doesn't need.
#
# This script is for interactive, see-the-output testing — double-click it,
# confirm EditOps actually starts and is reachable, then Ctrl+C it and set up
# register_mac_launch_daemon.command instead for the real persistent
# deployment. Like start_mac.command, closing this window kills the server;
# that's expected here, and exactly what the launchd daemon fixes.

cd "$(dirname "$0")"
export EDITOPS_REQUIREMENTS_FILE=requirements-server.txt

echo ""
echo "🎬  EditOps — Money Mediia (lite server build)"
echo "────────────────────────────────────────────────"

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
echo "📦  Checking Python dependencies (lite server profile)..."
pip install -r "$EDITOPS_REQUIREMENTS_FILE" -q

# Free port 5001 if a previous instance is still running
if lsof -ti :5001 &>/dev/null; then
  echo "🔄  Freeing port 5001 from previous session..."
  lsof -ti :5001 | xargs kill -9
  sleep 0.5
fi

# Launch app in background (auto-update runs inside app.py on startup)
python app.py &
SERVER_PID=$!

# Kill the server cleanly when this script exits (Ctrl+C or window close) —
# deliberate for this interactive script; register_mac_launch_daemon.command
# is what you want for a server that survives the window closing.
trap "kill $SERVER_PID 2>/dev/null; exit" INT TERM EXIT

# Wait until server is actually responding, then open browser
echo "⏳  Waiting for server to start..."
until curl -s http://localhost:5001 > /dev/null 2>&1; do
  sleep 0.5
done
echo "🌐  Opening browser..."
open http://localhost:5001
echo ""
echo "This window must stay open for the server to keep running."
echo "For a server that survives closing this window and reboots,"
echo "run register_mac_launch_daemon.command instead (one-time setup)."
echo ""

# Keep terminal open until Ctrl+C
wait $SERVER_PID
