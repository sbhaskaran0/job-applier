@echo off
rem Scheduler wrapper for the watchlist refresh (JOB-31). Self-locating: works
rem from any clone location, no hardcoded paths. Schedule this file daily, or
rem run it by hand. The only real contract is `python -m src.refresh`.
cd /d "%~dp0.."
python -m src.refresh
