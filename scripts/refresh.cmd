@echo off
rem Scheduler wrapper for the watchlist refresh (JOB-31). Self-locating: works
rem from any clone location, no hardcoded paths. Schedule this file daily, or
rem run it by hand. The only real contract is `python -m src.refresh`.
rem
rem refresh_guard.py skips the run (exit 10) when data\refresh-health.json shows
rem a successful refresh in the last 12h, so the 09:00 + at-logon triggers do
rem not double up. Pass --force (or set JOBAPPLIER_FORCE_REFRESH=1) to override.
cd /d "%~dp0.."

if not exist data mkdir data

python scripts\refresh_guard.py %*
if errorlevel 10 (
    echo %DATE% %TIME% skipped: recent success>> data\refresh.log
    exit /b 0
)

echo %DATE% %TIME% >> data\refresh.log
python -m src.refresh >> data\refresh.log 2>&1
set EXITCODE=%ERRORLEVEL%
echo Exit code: %EXITCODE% >> data\refresh.log
echo. >> data\refresh.log

exit /b %EXITCODE%
