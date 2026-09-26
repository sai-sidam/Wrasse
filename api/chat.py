"""Vercel serverless endpoint: POST /api/chat → the real Wrasse harness (stateless; state lives in the browser)."""
from __future__ import annotations

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("WRASSE_BASE_URL", "https://openrouter.ai/api")
os.environ.setdefault("WRASSE_MODEL", "anthropic/claude-sonnet-5")
os.environ.setdefault("WRASSE_TRIAGE_MODEL", "anthropic/claude-haiku-4.5")

from wrasse import llm, web  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = web.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, {"ok": True, "models": [llm.SONNET, llm.HAIKU],
                         "key": bool(os.getenv("WRASSE_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY"))})

    def do_POST(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            state = req.get("state") or web.new_state(req.get("goal", ""), req.get("deadline", ""), req.get("done", ""))
            llm.USAGE.reset()
            state, blocks = web.handle(state, req.get("message", ""), req.get("now"))
            self._send(200, {"state": state, "blocks": blocks, "tokens": llm.USAGE.total, "calls": llm.USAGE.calls})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
