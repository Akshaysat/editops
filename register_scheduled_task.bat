@echo off
REM ── Register EditOps to auto-start on boot (Windows Task Scheduler) ────────
REM Run this ONCE, as Administrator (right-click this file -> "Run as
REM administrator"). After this, EditOps will start automatically every time
REM this machine boots — including after a power loss or restart — with no
REM one needing to log in first, since it runs as the built-in SYSTEM
REM account (no password required, unlike running it as a real user account).
cd /d "%~dp0"

echo.
echo  Registering EditOps as a startup task...
echo.

schtasks /create ^
    /tn "EditOps Server" ^
    /tr "\"%~dp0start_windows_server_unattended.bat\"" ^
    /sc onstart ^
    /ru SYSTEM ^
    /rl highest ^
    /f

if %errorlevel% equ 0 (
    echo.
    echo  ✅  Done. EditOps will now start automatically on every boot.
    echo.
    echo  To start it right now without rebooting:
    echo    schtasks /run /tn "EditOps Server"
    echo.
    echo  To check on it:
    echo    schtasks /query /tn "EditOps Server" /v /fo list
    echo    (or open Task Scheduler -^> Task Scheduler Library -^> "EditOps Server")
    echo.
    echo  To remove it later:
    echo    schtasks /delete /tn "EditOps Server" /f
    echo.
    echo  Logs from each run are written to startup_log.txt in this folder.
) else (
    echo.
    echo  [ERROR] Could not register the task. Make sure you ran this file
    echo  as Administrator: right-click register_scheduled_task.bat -^>
    echo  "Run as administrator".
)

echo.
pause
