import shutil
from pathlib import Path

import pytest

from wrasse.tools import ToolBox

TEMPLATE = Path(__file__).resolve().parent.parent / "workspace_template"


@pytest.fixture
def box(tmp_path):
    ws = tmp_path / "ws"
    shutil.copytree(TEMPLATE, ws)
    return ToolBox(ws)


def test_list_and_read(box):
    out, err = box.call("list_dir", {})
    assert not err and "expenses.py" in out and "tests/test_expenses.py" in out
    out, err = box.call("read_file", {"path": "expenses.py"})
    assert not err and "def total" in out


def test_sandbox_boundary(box):
    for path in ("../escape.txt", "/etc/passwd", "tests/../../x"):
        out, err = box.call("write_file", {"path": path, "content": "x"})
        assert err and "outside the workspace" in out
    out, err = box.call("read_file", {"path": "../../etc/passwd"})
    assert err


def test_write_then_tests_tracking(box):
    out, err = box.call("run_tests", {})
    assert not err and "PASS" in out and box.tests_green_since_write
    box.call("write_file", {"path": "notes.txt", "content": "hi"})
    assert not box.tests_green_since_write
    box.call("write_file", {"path": "tests/test_bad.py", "content": "def test_x():\n    assert False\n"})
    out, err = box.call("run_tests", {})
    assert err and "FAIL" in out and not box.tests_passed


def test_read_only_blocks_writes(box):
    out, err = box.call("write_file", {"path": "a.txt", "content": "x"}, read_only=True)
    assert err and "read-only" in out
    assert not (box.root / "a.txt").exists()
    assert {t["name"] for t in box.specs(read_only=True)} == {"list_dir", "read_file", "run_tests"}


def test_guard_hook(box):
    class Deny:
        def before_write(self, path): return f"no writes to {path}"
        def before_done(self, b): return "not yet"
    box.guard = Deny()
    out, err = box.call("write_file", {"path": "a.txt", "content": "x"})
    assert err and "no writes" in out and box.write_seq == 0
    out, err = box.call("mark_step_done", {"summary": "s"})
    assert err and out == "not yet"


def test_bad_args(box):
    out, err = box.call("read_file", {})
    assert err and "bad arguments" in out
