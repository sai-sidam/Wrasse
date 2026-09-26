"""The harness pipeline: every user message is routed through Session.handle."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import db, executor
from .tools import ToolBox
from .ui import UI

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "workspace_template"
HISTORY_TURNS = 20


def workspaces_dir() -> Path:
    return Path(os.getenv("WRASSE_WORKSPACES", REPO_ROOT / "workspaces"))


def workspace_path(project: str) -> Path:
    return workspaces_dir() / project


def create_project(project: str, mode: str = "wrasse", template: Path = TEMPLATE_DIR) -> Path:
    ws = workspace_path(project)
    if ws.exists() or db.get_state(project):
        raise FileExistsError(f"project '{project}' already exists (use `wrasse resume {project}`)")
    shutil.copytree(template, ws, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    db.set_state(project, mode=mode, phase="gap_check", detour_pending=None)
    return ws


class Session:
    def __init__(self, project: str, ui: UI | None = None, mode: str | None = None):
        state = db.get_state(project)
        if state is None:
            raise KeyError(f"no project '{project}' (use `wrasse new {project}`)")
        self.project = project
        self.ui = ui or UI()
        self.mode = mode or state.get("mode", "wrasse")
        if mode and mode != state.get("mode"):
            db.set_state(project, mode=mode)
        self.workspace = workspace_path(project)
        self.box = ToolBox(self.workspace)

    def history(self) -> list[dict]:
        return db.get_messages(self.project, limit=HISTORY_TURNS)

    def handle(self, message: str) -> executor.ExecResult:
        message = message.strip() or "go"
        history = self.history()
        db.add_message(self.project, "user", message)
        result = executor.run(message, self.box, history=history, on_tool=self.ui.tool)
        self.ui.agent(result.text)
        if result.stop not in ("end_turn", "tool_use"):
            self.ui.warn(f"agent stopped: {result.stop}")
        db.add_message(self.project, "assistant", result.text or f"({result.stop})")
        return result
