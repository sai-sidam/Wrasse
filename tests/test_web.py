import json

from wrasse import llm, web
from conftest import response, text_block

NOW = "2026-09-26T14:00:00-04:00"
GAP = {"ask": [{"question": "Default category?", "default": "general"}], "assume": ["JSON storage"], "ignore": ["auth"]}
PLAN = {"steps": [{"title": "Add category field", "why": "w", "size": "cupcake", "est_min": 10, "files_scope": ["expenses.py"]},
                  {"title": "Monthly report", "why": "w", "size": "cupcake", "est_min": 10, "files_scope": ["expenses.py"]}],
        "cut": [], "assumptions": []}
DETOUR = {"kind": "new_request", "implies_change": True, "restatement": "Colored output", "category": "polish",
          "recommendation": "later", "related_to_goal": "no"}


def j(o):
    return response(text_block(json.dumps(o)))


def roundtrip(state, msg):
    state, blocks = web.handle(json.loads(web.dumps(state)), msg, NOW)
    return json.loads(web.dumps(state)), blocks


def test_full_web_flow(fake_llm):
    st = web.new_state("category field and monthly report", "in 45 minutes", "tests pass")
    fake_llm.queue = [j(GAP)]
    st, b = roundtrip(st, "")
    assert st["phase"] == "answers" and b[0]["type"] == "gap"
    fake_llm.queue = [j(PLAN)]
    st, b = roundtrip(st, "")
    assert st["phase"] == "confirm" and b[0]["type"] == "plan"
    fake_llm.queue = [response(text_block("I will add a category param."))]
    st, b = roundtrip(st, "y")
    assert st["phase"] == "building" and [x["type"] for x in b] == ["progress", "rail", "agent"]
    assert "45m left" in b[1]["clock"]

    fake_llm.queue = [j(DETOUR)]
    st, b = roundtrip(st, "could we add colored output?")
    assert st["pending"] and b[-1]["type"] == "detour"
    st, b = roundtrip(st, "later")
    assert st["pending"] is None and st["parked"][0]["summary"] == "Colored output"
    assert any(x["type"] == "back" for x in b)

    def learn(req):
        ids = [l.split()[1][3:] for l in req["messages"][0]["content"].splitlines() if "id=" in l]
        return j({"ops": [{"op": "add", "text": "Parks polish when time is short", "evidence_event_ids": ids}]})
    fake_llm.queue = [j({**DETOUR, "restatement": "User accounts"}), learn]
    st, _ = roundtrip(st, "what about user accounts?")
    st, b = roundtrip(st, "later")
    assert any(x["type"] == "toast" for x in b) and st["rules"][0]["confidence"] == 2

    fake_llm.queue = [j({"kind": "question", "implies_change": False, "restatement": "q"}),
                      response(text_block("No, use isoformat."))]
    st, b = roundtrip(st, "does json.dump handle datetime?")
    assert b[0]["text"] == "No, use isoformat." and b[1]["type"] == "back"
    assert "write_file" not in json.dumps(fake_llm.requests[-1])

    st, b = roundtrip(st, "done")
    assert st["plan"]["steps"][0]["status"] == "done" and st["plan"]["current_step_id"] == "s2"
    st, b = roundtrip(st, "done")
    assert st["phase"] == "done"


def test_missing_deadline_asks(fake_llm):
    st, b = roundtrip(web.new_state("g", "", "tests pass"), "")
    assert "deadline" in b[0]["text"] and fake_llm.requests == []
