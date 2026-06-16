@echo off
REM ── EditOps — One-time setup for Windows ──────────────────────────────────
cd /d "%~dp0"

echo.
echo  EditOps Setup -- Money Mediia
echo  --------------------------------
echo.

REM ── 1. Python ──────────────────────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found.
    echo  Install Python 3.11+ from: https://www.python.org/downloads/
    echo  IMPORTANT: Check "Add Python to PATH" during install.
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('python --version') do echo  OK: %%i

REM ── 2. Git ─────────────────────────────────────────────────────────────────
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Git not found.
    echo  Install Git from: https://git-scm.com/download/win
    pause
    exit /b 1
)
echo  OK: Git found

REM ── 3. ffmpeg ──────────────────────────────────────────────────────────────
ffmpeg -version >nul 2>&1
if %errorlevel% neq 0 (
    echo  Installing ffmpeg via winget...
    winget install --id Gyan.FFmpeg -e --silent
    if %errorlevel% neq 0 (
        echo  [ERROR] Could not auto-install ffmpeg.
        echo  Please install manually:
        echo    1. Go to https://ffmpeg.org/download.html
        echo    2. Download a Windows build
        echo    3. Extract and add the "bin" folder to your system PATH
        pause
        exit /b 1
    )
    echo  OK: ffmpeg installed
    echo  NOTE: Restart this script after ffmpeg finishes installing.
    pause
    exit /b 0
) else (
    echo  OK: ffmpeg found
)

REM ── 4. Virtual environment ─────────────────────────────────────────────────
if not exist "venv" (
    echo  Creating virtual environment...
    python -m venv venv
)

REM ── 5. Python dependencies ─────────────────────────────────────────────────
echo  Installing Python dependencies...
echo  (This includes PyTorch for Whisper — may take 5-10 mins on first run)
call venv\Scripts\pip install -r requirements-windows.txt -q
echo  OK: Dependencies installed

REM ── 6. Pre-download Whisper model ──────────────────────────────────────────
echo  Pre-downloading Whisper transcription model...
echo  (This only happens once. May take several minutes.)
call venv\Scripts\python -c "import whisper; whisper.load_model('turbo')"
echo  OK: Whisper model ready

echo.
echo  --------------------------------
echo  Setup complete!
echo.
echo  To launch EditOps:
echo    Double-click  start_windows.bat
echo.
echo  The app will auto-update from GitHub every time it starts.
echo.
pause
