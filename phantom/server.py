"""Local dashboard server. Listens on 127.0.0.1 only, so nothing outside your Mac can reach it."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


def make_app(get_state: Callable[[], dict], kill: Callable[[str], None], lock: threading.Lock | None = None) -> FastAPI:
    """`lock` is shared with the trading loop so the page never reads half-written state."""
    app = FastAPI(title="Phantom", docs_url=None, redoc_url=None, openapi_url=None)
    lock = lock or threading.Lock()

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Blocks DNS rebinding (foreign Host) and cross-site requests from other websites in your browser.
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return PlainTextResponse("forbidden host", status_code=403)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return PlainTextResponse("forbidden origin", status_code=403)
        if request.method == "POST" and request.headers.get("x-phantom") != "1":
            return PlainTextResponse("missing header", status_code=403)
        return await call_next(request)

    @app.get("/")
    def index():
        return FileResponse(DASHBOARD)

    @app.get("/api/state")
    def state():
        with lock:
            return JSONResponse(get_state())

    @app.post("/api/kill")
    def kill_now():
        with lock:
            kill("Kill switch pressed on the dashboard")
            return {"ok": True}

    return app


def serve_in_background(app: FastAPI, port: int = 8000) -> threading.Thread:
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True, name="dashboard")
    t.start()
    return t
