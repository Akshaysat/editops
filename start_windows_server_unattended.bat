@echo off
REM ── EditOps Launcher (Windows, unattended/scheduled-task build) ────────────
REM For running EditOps as a background server that survives reboots without
REM anyone logged in — meant to be launched by Task Scheduler (see
REM register_scheduled_task.bat), not double-clicked directly.
REM
REM Differs from start_windows_server.bat in exactly the ways that matter for
REM running headless, unattended, as the SYSTEM account:
REM   - No `pause` anywhere. A scheduled task run as SYSTEM has no one to
REM     press a key — a `pause` here would hang the task forever.
REM   - No browser auto-launch. SYSTEM has no interactive desktop session to
REM     open a browser in.
REM   - Errors are logged to startup_log.txt instead of shown and paused on,
REM     since there's no one watching the console.
REM Otherwise identical: same lite requirements-server.txt install, same
REM EDITOPS_REQUIREMENTS_FILE hint so a later auto-update reinstalls from the
REM right file, same port-5001 cleanup before starting.
cd /d "%~dp0"
set EDITOPS_REQUIREMENTS_FILE=requirements-server.txt

echo [%date% %time%] Starting EditOps (unattended)... >> startup_log.txt

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [%date% %time%] ERROR: Python not found. Run setup_windows.bat first. >> startup_log.txt
    exit /b 1
)

ffmpeg -version >nul 2>&1
if %errorlevel% neq 0 (
    echo [%date% %time%] ERROR: ffmpeg not found. Run setup_windows.bat first. >> startup_log.txt
    exit /b 1
)

if not exist "venv" (
    echo [%date% %time%] Creating virtual environment... >> startup_log.txt
    python -m venv venv
)

echo [%date% %time%] Checking Python dependencies (lite set)... >> startup_log.txt
call venv\Scripts\pip install -r requirements-server.txt -q

for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":5001 "') do (
    taskkill /PID %%p /F >nul 2>&1
)

echo [%date% %time%] Launching server... >> startup_log.txt
venv\Scripts\python app.py >> startup_log.txt 2>&1
