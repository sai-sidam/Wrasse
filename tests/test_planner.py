import json
from datetime import datetime, timedelta, timezone

import pytest

from wrasse import clock, db, planner
from wrasse.session import Session, build_context, create_project
from wrasse.ui import NullUI
from conftest import response, text_block, tool_block

REF = datetime(2026, 9, 26, 14, 0, tzinfo=timezone(timedelta(hours=-7)))


@pytest.fixture(autouse=True)
def frozen_clock():
    clock.set_now(REF)
    yield
    clock.set_now(None)


def j(obj):
    return response(text_block(json.dumps(obj)))


PLAN_DRAFT = {"steps": [
    {"title": "Add category field", "why": "needed for reports", "size": "cupcake", "est_min": 15,
     "files_scope": ["expenses.py", "tests/*"]},
    {"title": "Monthly total report", "why": "the goal", "size": "cupcake", "est_min": 25,
     "files_scope": ["expenses.py", "tests/*"]}],
    "cut": [{"item": "charts", "reason": "no time"}], "assumptions": ["JSON storage stays"]}


def test_normalize_and_revise():
    d = planner.normalize_draft(PLAN_DRAFT)
    assert [s["id"] for s in d["steps"]] == ["s1", "s2"] and d["steps"][0]["status"] == "todo"
    plan = planner.new_plan("p", {"goal": "g", "done_definition": "d"}, d, REF.isoformat())
    assert plan["current_step_id"] == "s1" and plan["steps"][0]["status"] == "doing"
    planner.advance(plan)
    assert plan["current_step_id"] == "s2" and plan["steps"][0]["status"] == "done"
    # revision keeps done step's id+status, adds a new id for a new step
    rev = planner.normalize_draft({"steps": PLAN_DRAFT["steps"] + [
        {"title": "CSV export", "why": "w", "size": "wedding", "est_min": 10, "files_scope": []}],
        "cut": [], "assumptions": [], "change_reason": "added csv"}, previous=plan)
    assert [(s["id"], s["status"]) for s in rev["steps"]] == [("s1", "done"), ("s2", "doing"), ("s3", "todo")]
    diff = planner.plan_diff(plan, rev["steps"])
    assert ("+", "s3 CSV export (wedding, 10m)") in diff
    new = planner.apply_revision(plan, rev, "user asked")
    assert new["version"] == 2 and new["current_step_id"] == "s2" and new["history"][-1]["reason"] == "user asked"


def test_missing_basics():
    assert planner.missing_basics({"goal": "x", "deadline": "soon"}) == ["deadline", "done_definition"]
    assert planner.missing_basics({"goal": "x", "deadline": "5pm", "done_definition": "d"}) == []


def test_full_gap_check_flow(fake_llm, workspaces):
    create_project("demo")
    ui = NullUI()
    s = Session("demo", ui)

    # a. basics missing → one message asking for what's missing
    fake_llm.queue = [j({"goal": "monthly report for the expense tracker", "deadline": None,
                         "done_definition": None})]
    s.handle("I want a monthly report")
    assert any("deadline" in l and "definition of done" in l for l in ui.lines)
    assert db.get_state("demo")["gap"]["stage"] == "basics"

    # b. basics complete → gap check card
    gap = {"ask": [{"question": "Group by calendar month?", "default": "yes"}],
           "assume": ["keep JSON storage"], "ignore": ["auth"]}
    fake_llm.queue = [j({"goal": "monthly report for the expense tracker", "deadline": "16:00",
                         "done_definition": "tests pass"}), j(gap)]
    s.handle("by 16:00, done when tests pass")
    st = db.get_state("demo")
    assert st["gap"]["stage"] == "answers"
    assert st["gap"]["basics"]["deadline_iso"] == REF.replace(hour=16).isoformat()
    gap_prompt = fake_llm.requests[-1]["messages"][0]["content"]
    assert "TIME LEFT: 2h00m (plan budget 1h36m)" in gap_prompt and "expenses.py" in gap_prompt

    # c. blank answer = defaults → plan card
    fake_llm.queue = [j(PLAN_DRAFT)]
    s.handle("")
    assert "blank: accept all defaults" in fake_llm.requests[-1]["messages"][0]["content"]
    assert db.get_state("demo")["gap"]["stage"] == "confirm" and db.get_plan("demo") is None

    # edit instruction → revised draft
    fake_llm.queue = [j({**PLAN_DRAFT, "steps": PLAN_DRAFT["steps"][:1]})]
    s.handle("drop the report step")
    assert "REVISION REQUESTED: drop the report step" in fake_llm.requests[-1]["messages"][0]["content"]
    assert len(db.get_state("demo")["gap"]["draft"]["steps"]) == 1

    # y → plan saved, phase building, executor auto-starts Step 1 with plan context
    fake_llm.queue = [response(text_block("Working on step 1."))]
    s.handle("y")
    plan = db.get_plan("demo")
    assert plan["version"] == 1 and plan["current_step_id"] == "s1" and "keep JSON storage" in plan["assumptions"]
    assert db.get_state("demo")["phase"] == "building"
    sys_prompt = fake_llm.requests[-1]["system"]
    assert "CURRENT STEP S1 (1/1): Add category field" in sys_prompt and "2h00m left" in sys_prompt
    assert fake_llm.requests[-1]["messages"][-1]["content"] == "Continue Step 1: Add category field."
    assert any(l.startswith("RAIL") for l in ui.lines)


def test_mark_step_done_advances_and_stops_turn(fake_llm, workspaces):
    create_project("demo")
    d = planner.normalize_draft(PLAN_DRAFT)
    db.save_plan(planner.new_plan("demo", {"goal": "g", "done_definition": "d"}, d, REF.replace(hour=16).isoformat()))
    db.set_state("demo", phase="building")
    ui = NullUI()
    fake_llm.queue = [response(tool_block("mark_step_done", {"summary": "x"}, "a")),
                      response(tool_block("run_tests", {}, "b")),
                      response(tool_block("mark_step_done", {"summary": "added category"}, "c"))]
    Session("demo", ui).handle("go")
    first = fake_llm.requests[1]["messages"][-1]["content"][0]
    assert first["is_error"] and "run_tests" in first["content"]     # guard: tests before done
    plan = db.get_plan("demo")
    assert plan["current_step_id"] == "s2" and plan["steps"][0]["status"] == "done"
    assert len(fake_llm.requests) == 3          # turn stopped after the step advanced
    assert any("Step 1 'Add category field' done · 1/2" in l for l in ui.lines)
    assert "CURRENT STEP S2 (2/2)" in build_context(plan)


def test_planner_prompts_are_grounded(fake_llm, tmp_path):
    fake_llm.queue = [j({"ask": [], "assume": [], "ignore": []}), j(PLAN_DRAFT)]
    basics = {"goal": "Build a model like opus", "done_definition": "tests pass"}
    planner.gap_check(basics, tmp_path, 45)
    gap_prompt = fake_llm.requests[-1]["messages"][0]["content"]
    assert "isn't achievable here" in gap_prompt and "Never silently swap in a different task" in gap_prompt
    planner.make_plan(basics, tmp_path, 45)
    plan_prompt = fake_llm.requests[-1]["messages"][0]["content"]
    assert "Never add \"fix X\" steps for problems you have not seen" in plan_prompt
    assert "Do not pad the plan to fill the budget" in plan_prompt
