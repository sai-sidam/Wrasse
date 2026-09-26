import json
from datetime import datetime, timedelta, timezone

import pytest

from wrasse import clock, db, llm, rules
from wrasse.main import main
from wrasse.session import Session, create_project, install_plan
from wrasse.ui import NullUI
from conftest import response, text_block

REF = datetime(2026, 9, 26, 15, 30, tzinfo=timezone(timedelta(hours=-7)))
STEPS = [{"id": "s1", "title": "Add category field", "why": "w", "size": "cupcake", "est_min": 15,
          "files_scope": ["expenses.py"]}]


def j(obj):
    return response(text_block(json.dumps(obj)))


@pytest.fixture(autouse=True)
def frozen_clock():
    clock.set_now(REF)
    yield
    clock.set_now(None)


@pytest.fixture
def project(workspaces):
    create_project("demo")
    install_plan("demo", "g", "tests pass", REF.replace(hour=17).isoformat(), STEPS)


def polish_event(decision="later", text="colors"):
    return db.log_event("demo", prompt=text, kind="new_request", mins_left=90,
                        verdict={"restatement": text, "category": "polish", "recommendation": "later"},
                        user_decision=decision, decision_ts=db.now())


def test_add_needs_two_evidence_events(project):
    e1, e2 = polish_event(), polish_event(text="emoji")
    events = rules.decided_events("demo")
    assert rules.apply_ops("demo", [{"op": "add", "text": "Parks polish when <2h left",
                                     "evidence_event_ids": [str(e1)]}], events) == []
    assert rules.apply_ops("demo", [{"op": "add", "text": "Bogus", "evidence_event_ids": [str(e1), "nope"]}],
                           events) == []
    changes = rules.apply_ops("demo", [{"op": "add", "text": "Parks polish when <2h left",
                                        "evidence_event_ids": [str(e1), str(e2), str(e2)]}], events)
    assert changes == [("add", "Parks polish when <2h left")]
    r = db.active_rules("demo")[0]
    assert r["confidence"] == 2 and r["evidence_event_ids"] == [e1, e2]
    # duplicate add is ignored
    assert rules.apply_ops("demo", [{"op": "add", "text": "Parks polish when <2h left",
                                     "evidence_event_ids": [str(e1), str(e2)]}], events) == []


def test_strengthen_weaken_retire(project):
    e1, e2, e3 = polish_event(), polish_event(), polish_event()
    events = rules.decided_events("demo")
    rules.apply_ops("demo", [{"op": "add", "text": "R", "evidence_event_ids": [str(e1), str(e2)]}], events)
    rid = str(db.active_rules("demo")[0]["_id"])
    assert rules.apply_ops("demo", [{"op": "strengthen", "rule_id": rid, "evidence_event_ids": [str(e3)]}],
                           events) == [("strengthen", "R")]
    assert db.active_rules("demo")[0]["confidence"] == 3
    assert rules.apply_ops("demo", [{"op": "weaken", "rule_id": rid, "evidence_event_ids": [str(e1)]}],
                           events) == [("weaken", "R")]
    assert db.active_rules("demo")[0]["confidence"] == 2
    assert rules.apply_ops("demo", [{"op": "retire", "rule_id": rid}], events) == [("retire", "R")]
    assert db.active_rules("demo") == [] and len(rules.all_rules("demo")) == 1


def test_reflect_prompt_and_skip_when_too_few(fake_llm, project):
    polish_event()
    assert rules.reflect("demo") == []          # < 2 decided events: no model call
    assert fake_llm.requests == []
    e2 = polish_event(text="emoji")
    db.log_event("demo", prompt="write tests/test_x.py", kind="scope",
                 verdict={"file": "tests/test_x.py", "size": "cupcake"}, user_decision="y", decision_ts=db.now())
    fake_llm.queue = [j({"ops": []})]
    rules.reflect("demo")
    body = fake_llm.requests[-1]["messages"][0]["content"]
    assert fake_llm.requests[-1]["model"] == llm.SONNET
    assert "category=polish" in body and "90m left → user chose later" in body
    assert "scope expansion for file tests/test_x.py" in body and f"id={e2}" in body


def test_decisions_trigger_learning_toast_and_feed_triage(fake_llm, project):
    ui = NullUI()
    detour = {"kind": "new_request", "implies_change": True, "restatement": "Colored output",
              "category": "polish", "recommendation": "later"}
    fake_llm.queue = [j(detour)]
    Session("demo", ui).handle("add colors?")
    fake_llm.queue = [response(text_block("resuming"))]          # 1 decided event: no reflect call
    Session("demo", ui).handle("later")

    fake_llm.queue = [j({**detour, "restatement": "Emoji status"})]
    Session("demo", ui).handle("add emoji?")

    def learn(req):
        ids = [line.split()[1][3:] for line in req["messages"][0]["content"].splitlines() if "id=" in line]
        return j({"ops": [{"op": "add", "text": "Parks polish requests when <2h left",
                           "evidence_event_ids": ids, "category": "polish"}]})
    fake_llm.queue = [learn, response(text_block("resuming"))]
    Session("demo", ui).handle("later")
    assert "TOAST 🧠 Wrasse learned: Parks polish requests when <2h left" in ui.lines
    # the executor that auto-resumed already sees the rule
    assert "USER PREFERENCES (learned): Parks polish requests when <2h left" in fake_llm.requests[-1]["system"]

    # and triage gets it next time
    fake_llm.queue = [j({"kind": "on_plan", "implies_change": False, "restatement": "x"}),
                      response(text_block("ok"))]
    Session("demo", ui).handle("add the category field")
    assert "Parks polish requests when <2h left" in fake_llm.requests[-2]["messages"][0]["content"]


def test_scope_decision_reflects_after_turn(fake_llm, project):
    from conftest import tool_block
    polish_event()
    ui = NullUI()
    ui.scope_answer = "y"
    fake_llm.queue = [j({"kind": "on_plan", "implies_change": False, "restatement": "x"}),
                      response(tool_block("write_file", {"path": "tests/test_cat.py", "content": "x=1\n"})),
                      response(text_block("done")),
                      j({"ops": []})]
    Session("demo", ui).handle("add a test for categories")
    assert fake_llm.requests[-1]["model"] == llm.SONNET and "DECIDED EVENTS" in fake_llm.requests[-1]["messages"][0]["content"]
    assert "tests/test_cat.py" in db.get_plan("demo")["steps"][0]["files_scope"]


def test_rules_cli(project, capsys):
    e1, e2 = polish_event(), polish_event(text="emoji")
    rules.apply_ops("demo", [{"op": "add", "text": "Parks polish when <2h left",
                              "evidence_event_ids": [str(e1), str(e2)]}], rules.decided_events("demo"))
    main(["rules", "demo"])
    out = capsys.readouterr().out
    assert "Parks polish when <2h left" in out and "emoji → later" in out
