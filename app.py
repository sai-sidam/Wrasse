"""WSGI entrypoint for Vercel: serves the web chat (site/) and the stateless harness API (/api/chat)."""
from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

os.environ.setdefault("WRASSE_BASE_URL", "https://openrouter.ai/api")
os.environ.setdefault("WRASSE_MODEL", "anthropic/claude-sonnet-5")
os.environ.setdefault("WRASSE_TRIAGE_MODEL", "anthropic/claude-haiku-4.5")

from wrasse import llm, web  # noqa: E402

SITE = Path(__file__).resolve().parent / "site"
PAGES = {"/": "index.html", "/index.html": "index.html", "/replay.html": "replay.html"}


def _json(start_response, code: str, payload: dict):
    body = web.dumps(payload).encode()
    start_response(code, [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
    return [body]


def chat(req: dict) -> dict:
    state = req.get("state") or web.new_state(req.get("goal", ""), req.get("deadline", ""), req.get("done", ""))
    llm.USAGE.reset()
    state, blocks = web.handle(state, req.get("message", ""), req.get("now"))
    return {"state": state, "blocks": blocks, "tokens": llm.USAGE.total, "calls": llm.USAGE.calls}


def app(environ, start_response):
    path, method = environ.get("PATH_INFO", "/"), environ.get("REQUEST_METHOD", "GET")
    if path == "/api/chat":
        if method == "GET":
            return _json(start_response, "200 OK", {"ok": True, "models": [llm.SONNET, llm.HAIKU],
                                                    "key": bool(os.getenv("WRASSE_AUTH_TOKEN"))})
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
            req = json.loads(environ["wsgi.input"].read(size) or b"{}")
            return _json(start_response, "200 OK", chat(req))
        except Exception as e:
            traceback.print_exc()
            return _json(start_response, "500 Internal Server Error", {"error": f"{type(e).__name__}: {e}"})
    page = PAGES.get(path)
    if page is None:
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"not found"]
    body = (SITE / page).read_bytes()
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body)))])
    return [body]
