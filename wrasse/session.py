"""The harness pipeline: every user message is routed through Session.handle."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import clock, db, executor, planner
from .tools import ToolBox
from .ui import UI

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "workspace_template"
HISTORY_TURNS = 20
YES = {"y", "yes", "ok", "okay", "confirm", "looks good", "lgtm", "go"}


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


def build_context(plan: dict, ref=None) -> str:
    """What the executor is told every turn in wrasse mode: time, goal, step, scope."""
    i, step = clock.current_step(plan)
    lines = [f"CLOCK: {clock.clock_line(plan, ref)}", f"GOAL: {plan['goal']}",
             f"DONE WHEN: {plan['done_definition']}"]
    if step:
        lines += [f"CURRENT STEP {step['id'].upper()} ({i + 1}/{len(plan['steps'])}): {step['title']}",
                  f"  why: {step['why']}", f"  size: {step['size']}, estimate {step['est_min']}m",
                  f"  files in scope: {', '.join(step['files_scope']) or '(any)'}"]
    rest = [f"{s['id'].upper()} {s['title']} [{s['status']}]" for s in plan["steps"] if s is not step]
    if rest:
        lines.append("OTHER STEPS (do not work on these now): " + "; ".join(rest))
    lines.append("Work only on the current step, at its size. Edit only files in scope. When the step meets its "
                 "goal, run the tests; once they pass, call mark_step_done and stop.")
    return "PLAN CONTEXT\n" + "\n".join(lines)


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
        self.box = ToolBox(self.workspace, on_step_done=self._step_done)
        self._step_advanced = False

    # ---- helpers ----
    def history(self) -> list[dict]:
        msgs = [m for m in db.get_messages(self.project)
                if m["role"] in ("user", "assistant") and m.get("phase") != "gap_check"]
        return msgs[-HISTORY_TURNS:]

    def say(self, text: str) -> None:
        """Harness (not agent) output, logged so a resumed session has the record."""
        db.add_message(self.project, "wrasse", text)

    def plan(self) -> dict | None:
        return db.get_plan(self.project)

    def show_rail(self, plan: dict) -> None:
        self.ui.rail(plan, clock.clock_line(plan), parked=len(db.get_parked(self.project)))

    def intro(self) -> None:
        """What the user sees on open: the opening question, or where we are."""
        state = db.get_state(self.project)
        plan = self.plan()
        if self.mode == "naive":
            return
        if state.get("phase") == "gap_check" and not (state.get("gap") or {}).get("stage"):
            self.ui.info("🐟 What are we building, by when, and how will we know it's done?")
        elif plan:
            self.show_rail(plan)

    # ---- pipeline ----
    def handle(self, message: str) -> executor.ExecResult | None:
        message = message.strip()
        state = db.get_state(self.project)
        if self.mode == "naive":
            return self._naive(message)
        if state.get("phase") == "gap_check":
            db.add_message(self.project, "user", message, phase="gap_check")
            return self._gap_check(message, state.get("gap") or {})
        plan = self.plan()
        if plan is None:
            db.set_state(self.project, phase="gap_check", gap={})
            return self._gap_check(message, {})
        self.show_rail(plan)
        return self._work(message, plan)

    def _naive(self, message: str) -> executor.ExecResult:
        """Baseline: every message straight to the executor, full tools, no plan/clock context."""
        return self._run(message or "go", context="")

    # ---- phase: gap_check ----
    def _gap_check(self, message: str, gap_state: dict) -> executor.ExecResult | None:
        stage = gap_state.get("stage", "basics")
        basics = gap_state.get("basics", {})

        if stage == "basics":
            if message:
                basics = planner.extract_basics(message, basics)
            dl = clock.parse_deadline(basics.get("deadline"))
            if dl and not basics.get("deadline_iso"):
                basics["deadline_iso"] = dl.isoformat()
            missing = planner.missing_basics(basics)
            if missing:
                self._save_gap(stage="basics", basics=basics)
                return self._ask_basics(missing)
            return self._run_gap_check(basics)

        if stage == "answers":
            return self._draft_plan(basics, gap_state["gap"], answers=message)

        if stage == "confirm":
            draft = gap_state["draft"]
            if message.lower() in YES or not message:
                return self._confirm_plan(basics, gap_state["gap"], draft)
            return self._draft_plan(basics, gap_state["gap"], answers=gap_state.get("answers", ""),
                                    edit=message, previous=draft)
        return None

    def _save_gap(self, **gap) -> None:
        db.set_state(self.project, phase="gap_check", gap=gap)

    def _ask_basics(self, missing: list[str]) -> None:
        labels = {"goal": "the goal (what are we building?)",
                  "deadline": "the deadline (e.g. 17:00, 5pm, or 'in 2h')",
                  "done_definition": "the definition of done (how will we know it works?)"}
        text = "Before I plan, I need " + "; ".join(labels[m] for m in missing) + ". One message is fine."
        self.ui.info("🐟 " + text)
        self.say(text)

    def _mins_left(self, basics: dict) -> int:
        return clock.minutes_left({"deadline": basics["deadline_iso"]})

    def _run_gap_check(self, basics: dict) -> None:
        mins = self._mins_left(basics)
        try:
            gap = planner.gap_check(basics, self.workspace, mins)
        except Exception as e:
            self.ui.warn(f"gap check failed ({e}); planning with defaults")
            gap = {"ask": [], "assume": [], "ignore": []}
        if not gap["ask"]:
            return self._draft_plan(basics, gap, answers="")
        self._save_gap(stage="answers", basics=basics, gap=gap)
        self.ui.gap_card(basics, gap, mins, clock.budget_minutes(mins))
        self.say("GAP CHECK " + str(gap))

    def _draft_plan(self, basics: dict, gap: dict, answers: str, edit: str = "",
                    previous: dict | None = None) -> None:
        mins = self._mins_left(basics)
        try:
            if previous:
                draft = planner.make_plan(basics, self.workspace, mins, gap, answers,
                                          previous={"steps": previous["steps"], "version": 1}, instruction=edit)
            else:
                draft = planner.make_plan(basics, self.workspace, mins, gap, answers)
        except Exception as e:
            self.ui.warn(f"planner failed: {e}. Reply to retry.")
            self._save_gap(stage="answers", basics=basics, gap=gap)
            return None
        self._save_gap(stage="confirm", basics=basics, gap=gap, answers=answers, draft=draft)
        self.ui.plan_card(draft, clock.budget_minutes(mins))
        self.say("PLAN DRAFT " + str([s["title"] for s in draft["steps"]]))
        return None

    def _confirm_plan(self, basics: dict, gap: dict, draft: dict) -> executor.ExecResult:
        plan = planner.new_plan(self.project, basics, draft, basics["deadline_iso"], gap)
        db.save_plan(plan)
        db.set_state(self.project, phase="building", gap=None)
        self.ui.progress(f"Plan v1 saved · {len(plan['steps'])} steps. Starting Step 1.")
        self.say("PLAN CONFIRMED")
        self.show_rail(plan)
        return self._work("", plan)

    # ---- phase: building ----
    def _work(self, message: str, plan: dict) -> executor.ExecResult | None:
        """Executor works the current step (step 3 puts triage in front of this)."""
        i, step = clock.current_step(plan)
        if step is None:
            self.ui.progress("All steps done. 🎉")
            if not message:
                return None
        if not message or message.lower() == "go":
            message = f"Continue Step {i + 1}: {step['title']}." if step else "go"
        return self._run(message, context=build_context(plan))

    def _run(self, message: str, context: str, read_only: bool = False) -> executor.ExecResult:
        history = self.history()
        db.add_message(self.project, "user", message)
        self._step_advanced = False
        result = executor.run(message, self.box, context=context, history=history, read_only=read_only,
                              on_tool=self.ui.tool, should_stop=lambda: self._step_advanced)
        self.ui.agent(result.text)
        if result.stop not in ("end_turn", "interrupted"):
            self.ui.warn(f"agent stopped: {result.stop}")
        db.add_message(self.project, "assistant", result.text or f"({result.stop})")
        return result

    def _step_done(self, summary: str) -> str:
        plan = self.plan()
        if plan is None or plan.get("current_step_id") is None:
            return "no current step to mark done"
        i, step = clock.current_step(plan)
        nxt = planner.advance(plan)
        db.save_plan(plan)
        self._step_advanced = True
        done = sum(s["status"] == "done" for s in plan["steps"])
        line = f"✅ Step {i + 1} '{step['title']}' done · {done}/{len(plan['steps'])} · {clock.clock_line(plan)}"
        self.ui.progress(line)
        self.say(line + f" · {summary}")
        if nxt is None:
            db.set_state(self.project, phase="done")
            return "Step marked done. That was the last step: the plan is complete. Summarize and stop."
        return f"Step marked done. Next is {nxt['id'].upper()} '{nxt['title']}'. Stop here and summarize."
