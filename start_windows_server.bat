@echo off
REM ── EditOps Launcher (Windows, lite server build) ───────────────────────────
REM Same as start_windows.bat, but installs from requirements-server.txt
REM instead of requirements-windows.txt — skips openai-whisper, easyocr,
REM pyspellchecker, and svgelements, since this deployment doesn't use local
REM Whisper, Spelling QA, or SVG to After Effects. Use this instead of
REM start_windows.bat on machines set up this way, so re-running it doesn't
REM reinstall the heavy packages this deployment doesn't need.
cd /d "%~dp0"
set EDITOPS_REQUIREMENTS_FILE=requirements-server.txt

echo.
echo  EditOps -- Money Mediia (lite server build)
echo  --------------------------------------------

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found. Run setup_windows.bat first.
    pause
    exit /b 1
)

REM Check ffmpeg
ffmpeg -version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] ffmpeg not found. Run setup_windows.bat first.
    pause
    exit /b 1
)

REM Create venv if missing
if not exist "venv" (
    echo  Setting up virtual environment - first time only...
    python -m venv venv
)

REM Install/update dependencies (lite set)
echo  Checking Python dependencies (lite set)...
call venv\Scripts\pip install -r requirements-server.txt -q

REM Free port 5001 if in use from a previous session
for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":5001 "') do (
    taskkill /PID %%p /F >nul 2>&1
)

REM Start server in background
echo  Starting server...
start /b venv\Scripts\python app.py

REM Wait until server is responding, then open browser
echo  Waiting for server to start...
set /a tries=0
:wait_loop
set /a tries+=1
if %tries% gtr 60 (
    echo  [ERROR] Server did not respond after 60 seconds.
    echo  Check the messages above for errors from app.py.
    pause
    exit /b 1
)
timeout /t 1 /nobreak >nul
curl -s -o nul http://localhost:5001
if %errorlevel% neq 0 goto wait_loop

echo  Opening browser...
start "" http://localhost:5001

echo.
echo  EditOps is running. Close this window to stop the server.
echo.
pause
