"""Applyer backend app: REST data API + chat WebSocket + built-frontend hosting.

Dev:  uvicorn server.app:app --port 8765 --reload   (+ `npm run dev` in frontend/)
Prod: npm run build in frontend/, then just this server — it serves frontend/dist.
"""

from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import chat, data_api

app = FastAPI(title="Applyer — Job Applier web wrapper")

# The Vite dev server (5173) proxies /api and /ws here, but allow direct
# cross-origin calls too so either wiring works during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(data_api.router)


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await chat.chat_session(ws)


_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        candidate = _DIST / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
