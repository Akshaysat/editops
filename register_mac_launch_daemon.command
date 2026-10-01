#!/bin/bash
# ── EditOps — Register as a persistent Mac server (launchd) ─────────────────
# The Mac equivalent of register_scheduled_task.bat: installs EditOps as a
# launchd LaunchDaemon so it starts automatically on boot (before anyone logs
# in), restarts itself if it ever crashes, and — critically — keeps running
# if someone closes a Terminal window. That last part is the exact bug that
# forced this office off Windows: start_mac.command (like its Windows
# equivalent) ties the server's life to a visible window staying open; a
# LaunchDaemon is managed directly by the OS instead, with no window to close.
#
# Run this ONCE on the Mac Mini that will host EditOps. It needs sudo, since
# LaunchDaemons live in /Library/LaunchDaemons (system-wide, not per-user) and
# macOS requires that file to be owned by root:wheel before launchd will load
# it — this script handles that, but will prompt for your password.
#
# Safe to re-run: it reinstalls dependencies and re-registers the daemon with
# today's paths, which is exactly what you want if this script itself gets
# updated or the repo moves.

set -e
cd "$(dirname "$0")"
REPO_DIR="$(pwd)"
LABEL="com.moneymediia.editops"
PLIST_PATH="/Library/LaunchDaemons/${LABEL}.plist"
LOG_PATH="${REPO_DIR}/editops_server.log"

echo ""
echo "🎬  EditOps — Register as a persistent Mac server"
echo "────────────────────────────────────────────────────"

# ── 1. Same dependency checks/setup as start_mac_server.command ────────────
if ! command -v python3 &>/dev/null; then
  echo "❌  Python 3 not found. Install it from https://www.python.org/downloads/"
  exit 1
fi

if ! command -v ffmpeg &>/dev/null; then
  echo "⚠️   ffmpeg not found. Installing via Homebrew..."
  if ! command -v brew &>/dev/null; then
    echo "    Homebrew not found. Install ffmpeg manually: https://ffmpeg.org/download.html"
    exit 1
  fi
  brew install ffmpeg
fi

if [ ! -d "venv" ]; then
  echo "📦  Setting up virtual environment (first time only)..."
  python3 -m venv venv
fi

echo "📦  Installing dependencies (lite server profile)..."
venv/bin/pip install -r requirements-server.txt -q

# ── 2. Figure out what launchd needs to know that an interactive shell ─────
#      takes for granted. A LaunchDaemon runs with almost no environment —
#      it does NOT inherit your shell's PATH, so `subprocess.run(['ffmpeg',
#      ...])` inside app.py would fail to find ffmpeg even though it works
#      fine from Terminal, unless PATH is set explicitly in the plist below.
#      PYTHONUNBUFFERED=1 matters too: Python fully buffers stdout when it's
#      not attached to a terminal (i.e. redirected to StandardOutPath below),
#      so without it the log file would only update in delayed bursts rather
#      than showing what's happening in real time.
FFMPEG_PATH="$(command -v ffmpeg)"
BREW_BIN_DIR="$(dirname "$FFMPEG_PATH")"
RUN_AS_USER="$(whoami)"
RUN_AS_GROUP="$(id -gn)"
PYTHON_BIN="${REPO_DIR}/venv/bin/python"

echo "👤  Will run as user: ${RUN_AS_USER}"
echo "🔧  ffmpeg found at:  ${FFMPEG_PATH}"
echo "📄  Logs will go to:  ${LOG_PATH}"

# ── 3. Unload any previous registration of this daemon before replacing it ─
if sudo launchctl list | grep -q "$LABEL"; then
  echo "🔄  Unloading existing daemon before re-registering..."
  sudo launchctl unload -w "$PLIST_PATH" 2>/dev/null || true
fi

# ── 4. Generate the plist with this machine's actual paths/user baked in ───
#      EDITOPS_REQUIREMENTS_FILE is the same env var auto_update() already
#      checks (originally introduced for start_windows_server.bat) — setting
#      it here means an auto-pulled update reinstalls requirements-server.txt
#      instead of silently drifting back to the full desktop requirements.txt.
#
#      To also opt into periodic self-updates (checking GitHub for new
#      commits on a schedule, not just at startup), add another <key> in
#      EnvironmentVariables below: EDITOPS_AUTO_UPDATE_HOURS set to a number
#      of hours — unset by default, matching start_periodic_auto_update()'s
#      opt-in design.
sudo tee "$PLIST_PATH" > /dev/null <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${REPO_DIR}/app.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${REPO_DIR}</string>

    <key>UserName</key>
    <string>${RUN_AS_USER}</string>
    <key>GroupName</key>
    <string>${RUN_AS_GROUP}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${BREW_BIN_DIR}:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>EDITOPS_REQUIREMENTS_FILE</key>
        <string>requirements-server.txt</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${LOG_PATH}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_PATH}</string>
</dict>
</plist>
PLIST

# launchd refuses to load a LaunchDaemon plist unless it's owned by
# root:wheel with no group/other write access — the file was just written
# via sudo tee, but tee doesn't change ownership on its own.
sudo chown root:wheel "$PLIST_PATH"
sudo chmod 644 "$PLIST_PATH"

# ── 5. Validate the plist before asking launchd to load it ─────────────────
if ! plutil -lint "$PLIST_PATH" >/dev/null; then
  echo "❌  Generated plist failed validation — not loading it. Check the output above."
  exit 1
fi

# ── 6. Load it ───────────────────────────────────────────────────────────────
sudo launchctl load -w "$PLIST_PATH"

sleep 2
if sudo launchctl list | grep -q "$LABEL"; then
  echo ""
  echo "✅  EditOps is now registered as a persistent server."
  echo "👉  It will start automatically on boot and restart itself if it crashes —"
  echo "    closing this Terminal window will NOT stop it."
  echo "👉  Open from another machine on the office network: http://$(scutil --get LocalHostName 2>/dev/null || hostname).local:5001"
  echo "📄  Logs: ${LOG_PATH}"
  echo ""
  echo "To stop/remove it later: sudo launchctl unload -w \"$PLIST_PATH\" && sudo rm \"$PLIST_PATH\""
else
  echo ""
  echo "⚠️   Daemon did not appear in 'launchctl list' after loading — check ${LOG_PATH} for errors."
fi
