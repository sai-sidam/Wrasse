import shutil
from pathlib import Path

from wrasse import db, executor
from wrasse.session import Session, create_project
from wrasse.tools import ToolBox
from wrasse.ui import NullUI
from conftest import response, text_block, tool_block

TEMPLATE = Path(__file__).resolve().parent.parent / "workspace_template"


def make_box(tmp_path):
    ws = tmp_path / "ws"
    shutil.copytree(TEMPLATE, ws)
    return ToolBox(ws)


def test_agent_loop_writes_and_tests(fake_llm, tmp_path):
    box = make_box(tmp_path)
    fake_llm.queue = [
        response(text_block("Looking."), tool_block("read_file", {"path": "expenses.py"}, "a")),
        response(tool_block("write_file", {"path": "hello.py", "content": "X = 1\n"}, "b"),
                 tool_block("run_tests", {}, "c")),
        response(text_block("Done: added hello.py.")),
    ]
    res = executor.run("add hello.py", box)
    assert res.stop == "end_turn" and res.steps == 3
    assert [c.name for c in res.calls] == ["read_file", "write_file", "run_tests"]
    assert (box.root / "hello.py").read_text() == "X = 1\n"
    assert "Done: added hello.py." in res.text
    # both tool results returned in a single user message
    tool_msg = fake_llm.requests[2]["messages"][-1]
    assert tool_msg["role"] == "user" and [r["tool_use_id"] for r in tool_msg["content"]] == ["b", "c"]


def test_read_only_mode_removes_write_tools(fake_llm, tmp_path):
    box = make_box(tmp_path)
    fake_llm.queue = [response(tool_block("write_file", {"path": "x.py", "content": ""})),
                      response(text_block("No, json.dump can't serialize datetime."))]
    res = executor.run("does json.dump handle datetime?", box, read_only=True)
    names = {t["name"] for t in fake_llm.requests[0]["tools"]}
    assert "write_file" not in names and "READ-ONLY" in fake_llm.requests[0]["system"]
    assert res.calls[0].is_error and not (box.root / "x.py").exists()


def test_max_steps(fake_llm, tmp_path):
    box = make_box(tmp_path)
    fake_llm.queue = [response(tool_block("list_dir", {}, f"id{i}")) for i in range(3)]
    res = executor.run("loop", box, max_steps=3)
    assert res.stop == "max_steps" and res.steps == 3


def test_api_error_does_not_crash(fake_llm, tmp_path):
    def boom(_):
        raise RuntimeError("overloaded")
    fake_llm.queue = [boom]
    res = executor.run("hi", make_box(tmp_path))
    assert res.stop == "error" and "overloaded" in res.text


def test_history_is_normalized():
    msgs = executor._history_messages([{"role": "assistant", "content": "stray"},
                                       {"role": "user", "content": "a"}, {"role": "user", "content": "b"},
                                       {"role": "assistant", "content": ""}, {"role": "assistant", "content": "c"}])
    assert msgs == [{"role": "user", "content": "a\n\nb"}, {"role": "assistant", "content": "c"}]


def test_session_persists_and_resumes(fake_llm, workspaces):
    ws = create_project("demo")
    assert (ws / "expenses.py").exists() and db.get_state("demo")["phase"] == "gap_check"
    fake_llm.queue = [response(text_block("hi there"))]
    Session("demo", NullUI()).handle("hello")
    fake_llm.queue = [response(text_block("still here"))]
    Session("demo", NullUI()).handle("")   # resumed session, empty message == go
    sent = fake_llm.requests[-1]["messages"]
    assert sent[0] == {"role": "user", "content": "hello"} and sent[1]["content"] == "hi there"
    assert sent[-1]["content"] == "go"
    assert [m["role"] for m in db.get_messages("demo")] == ["user", "assistant", "user", "assistant"]
