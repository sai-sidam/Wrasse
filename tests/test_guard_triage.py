import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from wrasse import clock, db, llm, triage
from wrasse.guard import Guard, in_scope
from wrasse.session import Session, create_project, install_plan
from wrasse.ui import NullUI
from conftest import response, text_block, tool_block

REF = datetime(2026, 9, 26, 14, 0, tzinfo=timezone(timedelta(hours=-7)))
STEPS = [
    {"id": "s1", "title": "Add category field", "why": "w", "size": "cupcake", "est_min": 15,
     "files_scope": ["expenses.py", "tests/*"]},
    {"id": "s2", "title": "Monthly total report", "why": "w", "size": "cupcake", "est_min": 15,
     "files_scope": ["expenses.py", "tests/*"]},
]

DETOUR = {
    "kind": "new_request", "implies_change": True, "restatement": "Colored terminal output",
    "related_to_goal": "no", "related_reason": "cosmetic", "related_step_id": None, "related_parked_id": None,
    "impact": {"delays_steps": ["s2"], "est_min": 20, "files_likely": ["expenses.py", "colors.py"], "risk": "low"},
    "options": {"now": {"pro": "looks nice", "con": "delays S2"}, "later": {"pro": "keeps pace", "con": "plain now"},
                "skip": {"pro": "zero cost", "con": "never colored"}},
    "recommendation": "later", "recommendation_reason": "2h left, polish can wait", "rule_applied": None,
    "category": "polish",
}


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
    install_plan("demo", "category + monthly report", "tests pass", REF.replace(hour=16).isoformat(), STEPS)
    return "demo"


def session(ui=None):
    return Session("demo", ui or NullUI())


# ---- guard ------------------------------------------------------------------

def test_in_scope_patterns():
    step = {"files_scope": ["expenses.py", "tests/*", "docs/"]}
    assert in_scope(step, "expenses.py") and in_scope(step, "tests/test_x.py") and in_scope(step, "docs/a.md")
    assert not in_scope(step, "colors.py") and not in_scope(step, "tests_extra.py")
    assert in_scope({"files_scope": []}, "anything.py") and in_scope(None, "x")


def test_scope_card_no_then_yes(fake_llm, project):
    answers = iter([False, True])
    g = Guard("demo", ask_scope=lambda path, step: next(answers))
    err = g.before_write("colors.py")
    assert err and "outside Step S1's scope" in err
    assert g.before_write("colors.py").startswith("REFUSED") and "already declined" in g.before_write("colors.py")
    assert g.before_write("helpers.py") is None                   # user says y
    assert "helpers.py" in db.get_plan("demo")["steps"][0]["files_scope"]
    assert g.before_write("expenses.py") is None                  # in scope, no card
    edits = list(db.col("edits").find({}, {"_id": 0, "file": 1, "in_scope": 1, "allowed": 1}))
    assert {"file": "colors.py", "in_scope": False, "allowed": False} in edits
    assert {"file": "helpers.py", "in_scope": False, "allowed": True} in edits
    scope_events = list(db.col("events").find({"kind": "scope"}))
    assert [e["user_decision"] for e in scope_events] == ["n", "y"]


def test_write_refused_while_detour_pending(project):
    db.set_state("demo", detour_pending={"prompt": "colors?", "verdict": DETOUR})
    err = Guard("demo", ask_scope=lambda *_: True).before_write("expenses.py")
    assert err and "detour decision is pending" in err


def test_tests_before_done(project, tmp_path):
    from wrasse.tools import ToolBox
    box = ToolBox(tmp_path, guard=Guard("demo", ask_scope=lambda *_: True))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    out, err = box.call("mark_step_done", {"summary": "x"})
    assert err and "run_tests" in out
    box.call("write_file", {"path": "expenses.py", "content": "x = 1\n"})
    box.call("run_tests", {})
    box.call("write_file", {"path": "expenses.py", "content": "x = 2\n"})
    out, err = box.call("mark_step_done", {"summary": "x"})
    assert err and "since your last write" in out
    box.call("run_tests", {})
    out, err = box.call("mark_step_done", {"summary": "x"})
    assert not err


# ---- triage -----------------------------------------------------------------

def test_triage_quick_routes_and_fallbacks(fake_llm, project):
    plan = db.get_plan("demo")
    assert triage.classify("", plan)["kind"] == "on_plan"
    assert triage.classify("later", plan, pending={"prompt": "x"})["decision"] == "later"
    fake_llm.queue = [lambda _: (_ for _ in ()).throw(RuntimeError("boom"))]
    v = triage.classify("add colors", plan)
    assert v["kind"] == "on_plan" and "boom" in v["fallback"]

    def slow(_):
        time.sleep(0.5)
        return j(DETOUR)
    fake_llm.queue = [slow]
    v = triage.classify("add colors", plan, timeout=0.1)
    assert v["kind"] == "on_plan" and "timed out" in v["fallback"]


def test_triage_prompt_has_clock_rules_parked(fake_llm, project):
    db.add_parked("demo", text="dark mode", category="polish", related_step_id="s1", summary="Dark mode")
    db.col("rules").insert_one({"project": "demo", "text": "Parks polish when <2h left", "active": True,
                                "confidence": 2})
    fake_llm.queue = [j(DETOUR)]
    triage.classify("could we add colors?", db.get_plan("demo"), rules=db.active_rules("demo"),
                    parked=db.get_parked("demo"))
    req = fake_llm.requests[-1]
    assert req["model"] == llm.HAIKU
    body = req["messages"][0]["content"]
    assert "2h00m left" in body and "Parks polish when <2h left" in body and "Dark mode" in body


# ---- session routing --------------------------------------------------------

def test_pure_question_is_read_only(fake_llm, project):
    ui = NullUI()
    fake_llm.queue = [j({"kind": "question", "implies_change": False, "restatement": "json.dump + datetime?"}),
                      response(text_block("No: pass default=str."))]
    session(ui).handle("does json.dump handle datetime objects?")
    exec_req = fake_llm.requests[-1]
    assert "write_file" not in {t["name"] for t in exec_req["tools"]}
    assert any("Plan unaffected, continuing Step 1 'Add category field'" in l for l in ui.lines)
    assert db.get_state("demo")["detour_pending"] is None


def test_detour_later_parks_and_resumes(fake_llm, project):
    ui = NullUI()
    fake_llm.queue = [j(DETOUR)]
    session(ui).handle("could we also add colored terminal output?")
    assert len(fake_llm.requests) == 1          # new_request: triage only, the agent does not run
    st = db.get_state("demo")
    assert st["detour_pending"]["prompt"] == "could we also add colored terminal output?"
    assert any(l.startswith("DETOUR Colored terminal output") for l in ui.lines)

    # while pending, an on-plan message does not run the agent
    n = len(fake_llm.requests)
    fake_llm.queue = [j({"kind": "on_plan", "implies_change": False, "restatement": "do s1"})]
    session(ui).handle("add the category field")
    assert len(fake_llm.requests) == n + 1 and db.get_state("demo")["detour_pending"]

    fake_llm.queue = [response(text_block("Resuming step 1."))]
    session(ui).handle("later")
    assert db.get_state("demo")["detour_pending"] is None
    parked = db.get_parked("demo")
    assert len(parked) == 1 and parked[0]["category"] == "polish" and parked[0]["summary"] == "Colored terminal output"
    ev = db.col("events").find_one({"kind": "new_request"})
    assert ev["user_decision"] == "later" and ev["decision_ts"] is not None
    assert any("BACK Back to plan → Step 1 'Add category field'" in l for l in ui.lines)
    sent = fake_llm.requests[-1]["messages"]
    assert sent[-1]["content"] == "Continue Step 1: Add category field."
    assert "parked for later; do not work on it now" in sent[-2]["content"]


def test_detour_now_inserts_step(fake_llm, project):
    fake_llm.queue = [j(DETOUR)]
    session().handle("add colors please")
    fake_llm.queue = [response(text_block("ok"))]
    session().handle("now")
    plan = db.get_plan("demo")
    assert [s["id"] for s in plan["steps"]] == ["s1", "s3", "s2"]
    assert plan["steps"][1]["title"] == "Colored terminal output" and plan["steps"][1]["files_scope"] == \
        ["expenses.py", "colors.py"]
    assert plan["version"] == 2 and plan["history"][-1]["reason"] == "detour approved: now"
    assert plan["current_step_id"] == "s1"


def test_skip_logs_only(fake_llm, project):
    fake_llm.queue = [j(DETOUR)]
    session().handle("what if we added user accounts?")
    fake_llm.queue = [response(text_block("ok"))]
    session().handle("skip")
    assert db.get_parked("demo") == [] and len(db.get_plan("demo")["steps"]) == 2
    assert db.col("events").find_one({"kind": "new_request"})["user_decision"] == "skip"


def test_triage_failure_falls_back_to_on_plan(fake_llm, project):
    ui = NullUI()
    fake_llm.queue = [j({"bad": 1}), j({"bad": 2}), response(text_block("working"))]
    session(ui).handle("add the category field")
    assert any("WARN triage failed" in l for l in ui.lines)
    assert "CURRENT STEP S1" in fake_llm.requests[-1]["system"]


def test_plan_change_confirm(fake_llm, project):
    fake_llm.queue = [j({"kind": "plan_change", "implies_change": True, "restatement": "drop s2"}),
                      j({"steps": [STEPS[0]], "cut": [{"item": "monthly report", "reason": "user dropped"}],
                         "assumptions": [], "change_reason": "dropped S2"})]
    session().handle("let's drop the monthly report step from the plan")
    assert db.get_state("demo")["plan_change_pending"]["instruction"].startswith("let's drop")
    assert db.get_plan("demo")["version"] == 1
    fake_llm.queue = [response(text_block("ok"))]
    session().handle("y")
    plan = db.get_plan("demo")
    assert plan["version"] == 2 and [s["id"] for s in plan["steps"]] == ["s1"]
    assert plan["history"][-1]["change"] == "dropped S2"
    assert db.get_state("demo")["plan_change_pending"] is None


def test_scope_card_during_agent_turn(fake_llm, project):
    ui = NullUI()
    fake_llm.queue = [j({"kind": "on_plan", "implies_change": False, "restatement": "s1"}),
                      response(tool_block("write_file", {"path": "colors.py", "content": "RED = 1\n"})),
                      response(text_block("staying in scope"))]
    session(ui).handle("add the category field")
    assert any(l == "SCOPE colors.py" for l in ui.lines)
    tool_result = fake_llm.requests[-1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] and "outside Step S1's scope" in tool_result["content"]
