import io
import json

import app as webapp
from conftest import response, text_block


def call(path, method="GET", body=None):
    out = {}
    def start(status, headers):
        out["status"] = status
    data = json.dumps(body).encode() if body is not None else b""
    env = {"PATH_INFO": path, "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(data)), "wsgi.input": io.BytesIO(data)}
    return out, b"".join(webapp.app(env, start))


def test_pages_and_api(fake_llm):
    st, body = call("/")
    assert st["status"].startswith("200") and b"WRASSE" in body
    st, body = call("/replay.html")
    assert st["status"].startswith("200")
    assert call("/nope")[0]["status"].startswith("404")
    fake_llm.queue = [response(text_block(json.dumps({"ask": [], "assume": [], "ignore": []}))),
                      response(text_block(json.dumps({"steps": [{"title": "t", "why": "w", "size": "cupcake",
                                                                  "est_min": 5, "files_scope": []}],
                                                       "cut": [], "assumptions": []})))]
    st, body = call("/api/chat", "POST", {"goal": "g", "deadline": "in 45 minutes", "done": "tests pass",
                                          "message": "", "now": "2026-09-26T14:00:00-04:00"})
    out = json.loads(body)
    assert st["status"].startswith("200") and out["state"]["phase"] == "confirm" and out["blocks"][0]["type"] == "plan"
