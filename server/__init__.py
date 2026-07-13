"""Applyer web wrapper — FastAPI backend for the frontend in frontend/.

Serves the agent's real data (postings store, applications log, profile,
watchlist, context files) over a small REST API, plus a WebSocket chat that
drives real Claude Code sessions in this repo via the Claude Agent SDK.

Run: python -m server   (or: uvicorn server.app:app --port 8765)
"""
