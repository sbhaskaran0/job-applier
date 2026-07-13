@echo off
rem Launch the Applyer web wrapper. Self-locating: works from any clone
rem location. Serves the built frontend (frontend/dist) + the data API + the
rem Claude Code chat bridge on http://localhost:8765 and opens the browser.
rem Dev loop instead: `python -m server` + `npm run dev` in frontend/.
cd /d "%~dp0.."
start "" http://localhost:8765
python -m server
