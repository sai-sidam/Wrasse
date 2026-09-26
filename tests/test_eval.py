import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from wrasse import db
from wrasse.session import REPO_ROOT, TEMPLATE_DIR
from conftest import response, text_block, tool_block

spec = importlib.util.spec_from_file_location("run_eval", REPO_ROOT / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_eval)

REFERENCE = Path(__file__).parent / "fixtures" / "reference_expenses.py"


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "ws"
    shutil.copytree(TEMPLATE_DIR, d)
    return d


def test_checks_fail_on_template_and_pass_on_reference(ws):
    assert run_eval.run_checks(ws) == {"s1": False, "s2": False, "s3": False, "s4": False}
    shutil.copy(REFERENCE, ws / "expenses.py")
    assert run_eval.run_checks(ws) == {"s1": True, "s2": True, "s3": True, "s4": True}


def test_diff_curveballs_and_off_plan(ws):
    assert run_eval.changed_files(ws) == {}
    (ws / "expenses.py").write_text((ws / "expenses.py").read_text() + "\nRED = '\\033[31m'\n")
    (ws / "auth.py").write_text("def login(user, password):\n    pass\n")
    (ws / "expenses.json").write_text("[]")          # data files are ignored
    changes = run_eval.changed_files(ws)
    assert set(changes) == {"expenses.py", "auth.py"}
    assert run_eval.curveballs_built(changes) == {"color": True, "auth": True}
    steps = [{"files_scope": ["expenses.py", "tests/*"]}]
    assert run_eval.off_plan(changes, steps) == ["auth.py"]


def fake_brain(req):
    """Request-aware fake: triage (Haiku), rules (Sonnet JSON) and the executor (Sonnet + tools)."""
    msgs = req["messages"]
    if req["model"] == "claude-haiku-4-5":
        body = msgs[0]["content"].split("USER MESSAGE:")[-1]
        if "json.dump" in body:
            v = {"kind": "question", "implies_change": False, "restatement": "json.dump and datetime"}
        elif "colored" in body or "user accounts" in body:
            v = {"kind": "new_request", "implies_change": True, "restatement": body.strip(),
                 "category": "polish", "recommendation": "later", "related_to_goal": "no"}
        else:
            v = {"kind": "on_plan", "implies_change": False, "restatement": "step"}
        return response(text_block(json.dumps(v)))
    if "tools" not in req:
        return response(text_block('{"ops": []}'))
    last = msgs[-1]["content"]
    if isinstance(last, list):                       # tool results came back → finish the turn
        return response(text_block("done"))
    if "colored" in last:
        return response(tool_block("write_file", {"path": "colors.py", "content": "RED = '\\033[31m'\n"}))
    if "user accounts" in last:
        return response(tool_block("write_file", {"path": "auth.py", "content": "def login(u, password): ...\n"}))
    return response(text_block("ok"))


def test_run_mode_end_to_end(fake_llm, workspaces):
    fake_llm.queue = [fake_brain] * 200
    script = json.loads(run_eval.SCRIPT_PATH.read_text())
    naive = run_eval.run_mode("naive", script, "t")
    wrasse = run_eval.run_mode("wrasse", script, "t")

    assert naive["curveballs_executed"] == 2 and naive["off_plan_files"] == 2 and naive["turns"] == 7
    assert wrasse["curveballs_executed"] == 0 and wrasse["off_plan_files"] == 0 and wrasse["turns"] == 9
    assert wrasse["parked"] == 2 and wrasse["tokens"] > 0
    assert db.col("eval_runs").count_documents({}) == 2
    table = run_eval.comparison_table({"naive": naive, "wrasse": wrasse})
    assert table.row_count == len(run_eval.ROWS)
