"""Enforcement inside the tools, not just in prompts.

- write_file while a detour is pending → refused
- write_file outside the current step's files_scope → refused unless the user expands scope (SCOPE CARD)
- mark_step_done → refused unless run_tests ran after the last write and passed
"""
from __future__ import annotations

from fnmatch import fnmatch
from typing import Callable

from . import clock, db


def in_scope(step: dict | None, path: str) -> bool:
    if step is None:
        return True
    scope = step.get("files_scope") or []
    if not scope:
        return True
    for pat in scope:
        pat = pat.strip().removeprefix("./")
        if fnmatch(path, pat):
            return True
        if pat.endswith(("/", "/*", "/**")) and path.startswith(pat.rstrip("*").rstrip("/") + "/"):
            return True
    return False


class Guard:
    def __init__(self, project: str, ask_scope: Callable[[str, dict], bool],
                 on_scope_decision: Callable[[object], None] | None = None):
        self.project = project
        self.ask_scope = ask_scope
        self.on_scope_decision = on_scope_decision
        self.denied: set[tuple[str, str]] = set()   # (step_id, path) the user already said n to

    def before_write(self, path: str) -> str | None:
        state = db.get_state(self.project) or {}
        plan = db.get_plan(self.project)
        _, step = clock.current_step(plan)
        step_id = step["id"] if step else None
        scoped = in_scope(step, path)

        pending = state.get("detour_pending")
        if pending:
            db.log_edit(self.project, path, step_id, scoped, allowed=False)
            return (f"REFUSED: a detour decision is pending ({pending.get('prompt')!r}). "
                    "No file changes until the user answers now / later / skip.")
        if scoped:
            db.log_edit(self.project, path, step_id, True, allowed=True)
            return None

        label = f"Step {step_id.upper()}" if step_id else "the plan"
        if (step_id, path) in self.denied:
            db.log_edit(self.project, path, step_id, False, allowed=False)
            return f"REFUSED: {path} is outside {label}'s scope and the user already declined. Stay in scope."

        yes = bool(self.ask_scope(path, step))
        db.log_edit(self.project, path, step_id, False, allowed=yes)
        event_id = db.log_event(self.project, prompt=f"write {path}", kind="scope",
                                verdict={"file": path, "step_id": step_id, "size": step.get("size")},
                                user_decision="y" if yes else "n", decision_ts=db.now())
        if yes:
            step.setdefault("files_scope", []).append(path)
            db.save_plan(plan)
        else:
            self.denied.add((step_id, path))
        if self.on_scope_decision:
            self.on_scope_decision(event_id)
        if yes:
            return None
        scope = ", ".join(step.get("files_scope") or [])
        return (f"REFUSED: {path} is outside {label}'s scope ({scope}). The user said no to expanding it. "
                "Stay within scope; if the step truly needs this file, say so in your summary.")

    def before_done(self, box) -> str | None:
        if box.tests_seq != box.write_seq:
            return "REFUSED: run_tests has not run since your last write. Run the tests; they must pass."
        if not box.tests_passed:
            return "REFUSED: the tests are failing. Fix them, run_tests again, then mark_step_done."
        return None
