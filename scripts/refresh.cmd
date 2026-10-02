@echo off
rem Scheduler wrapper for the watchlist refresh (JOB-31). Self-locating: works
rem from any clone location, no hardcoded paths. Schedule this file daily, or
rem run it by hand. The only real contract is `python -m src.refresh`.
rem
rem Before running the refresh, checks data\refresh-health.json to skip if a
rem successful refresh completed within the last 12 hours (StartWhenAvailable
rem guard to avoid redundant runs).
cd /d "%~dp0.."

rem Create data directory if it doesn't exist
if not exist data mkdir data

rem Check if we should skip based on recent successful refresh
python -c "
import json
import os
from datetime import datetime, timedelta, timezone

health_file = os.path.join('data', 'refresh-health.json')
try:
    if os.path.exists(health_file):
        with open(health_file, 'r') as f:
            health = json.load(f)
        if health.get('ok') == True:
            run_at_str = health.get('run_at')
            if run_at_str:
                try:
                    # Parse ISO-8601 UTC timestamp
                    run_at = datetime.fromisoformat(run_at_str.replace('Z', '+00:00'))
                    now = datetime.now(timezone.utc)
                    elapsed = now - run_at
                    if elapsed < timedelta(hours=12):
                        print(f'Refresh completed {elapsed.total_seconds()/3600:.1f}h ago; skipping.')
                        exit(0)
                except Exception as e:
                    pass
except Exception as e:
    pass
" 2>nul

rem Log to data\refresh.log
if not exist data mkdir data

echo %DATE% %TIME% >> data\refresh.log
python -m src.refresh >> data\refresh.log 2>&1
set EXITCODE=%ERRORLEVEL%
echo Exit code: %EXITCODE% >> data\refresh.log
echo. >> data\refresh.log

exit /b %EXITCODE%
