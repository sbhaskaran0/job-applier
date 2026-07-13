"""python -m server — run the Applyer backend on http://localhost:8765."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("server.app:app", host="127.0.0.1", port=8765)
