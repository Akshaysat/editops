@echo off
REM ── EditOps Launcher (Windows) ──────────────────────────────────────────────
cd /d "%~dp0"

echo.
echo  EditOps -- Money Mediia
echo  -------------------------

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

REM Install/update dependencies
echo  Checking Python dependencies...
call venv\Scripts\pip install -r requirements-windows.txt -q

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
