#!/bin/bash
# ── EditOps — One-time setup for Mac ────────────────────────────────────────
cd "$(dirname "$0")"

echo ""
echo "✦  EditOps Setup — Money Mediia"
echo "────────────────────────────────"
echo ""

# ── 1. Xcode Command Line Tools (git, etc.) ──────────────────────────────────
if ! command -v git &>/dev/null; then
  echo "📦  Installing Xcode Command Line Tools..."
  xcode-select --install
  echo "    Once the installer finishes, re-run this script."
  read -p "Press Enter to exit..."
  exit 1
fi
echo "✅  Git found: $(git --version)"

# ── 2. Homebrew ───────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  echo "📦  Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Add brew to PATH for Apple Silicon
  echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi
echo "✅  Homebrew found"

# ── 3. Python ─────────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "📦  Installing Python..."
  brew install python
fi
echo "✅  Python found: $(python3 --version)"

# ── 4. ffmpeg ─────────────────────────────────────────────────────────────────
if ! command -v ffmpeg &>/dev/null; then
  echo "📦  Installing ffmpeg..."
  brew install ffmpeg
fi
echo "✅  ffmpeg found"

# ── 5. Virtual environment & dependencies ────────────────────────────────────
if [ ! -d "venv" ]; then
  echo "📦  Creating virtual environment..."
  python3 -m venv venv
fi
source venv/bin/activate
echo "📦  Installing Python dependencies (this may take a few minutes on first run)..."
pip install -r requirements.txt -q
echo "✅  Dependencies installed"

# ── 6. Pre-download Whisper model ────────────────────────────────────────────
echo "📦  Pre-downloading Whisper transcription model (~1.6 GB)..."
echo "    This only happens once. May take a few minutes on slow connections..."
python3 -c "from huggingface_hub import snapshot_download; snapshot_download('mlx-community/whisper-large-v3-turbo')"
echo "✅  Whisper model ready"

# ── 7. Generate app icon ─────────────────────────────────────────────────────
echo "🎨  Generating app icon..."
python3 make_icon.py

# ── 8. Make launcher executable & remove quarantine ──────────────────────────
chmod +x start_mac.command start_mac.sh EditOps.app/Contents/MacOS/EditOps 2>/dev/null
xattr -cr EditOps.app 2>/dev/null

echo ""
echo "────────────────────────────────"
echo "✅  Setup complete!"
echo ""
echo "To launch EditOps:"
echo "  → Double-click  EditOps.app"
echo "  → Or run:        ./start_mac.sh"
echo ""
echo "The app will auto-update from GitHub every time it starts."
echo ""
read -p "Press Enter to exit..."
