"""The harness pipeline: every user message is routed through Session.handle."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import clock, db, executor, planner, rules, triage
from .guard import Guard, in_scope
from .tools import ToolBox
from .ui import UI

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "workspace_template"
HISTORY_TURNS = 20
YES = {"y", "yes", "ok", "okay", "confirm", "looks good", "lgtm", "go"}
NO = {"n", "no", "cancel", "nevermind", "never mind"}


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


def install_plan(project: str, goal: str, done_definition: str, deadline_iso: str, steps: list[dict]) -> dict:
    """Install a predefined plan and skip the gap check (used by the eval)."""
    draft = planner.normalize_draft({"steps": steps, "cut": [], "assumptions": []})
    for given, s in zip(steps, draft["steps"]):
        s["id"] = given.get("id", s["id"])
    plan = planner.new_plan(project, {"goal": goal, "done_definition": done_definition}, draft, deadline_iso)
    plan["history"][0]["reason"] = "predefined plan"
    db.save_plan(plan)
    db.set_state(project, phase="building", gap=None, detour_pending=None, plan_change_pending=None)
    return plan


def build_context(plan: dict, ref=None, active_rules: list[dict] | None = None) -> str:
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
    if active_rules:
        lines.append("USER PREFERENCES (learned): " + "; ".join(r["text"] for r in active_rules))
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
        if self.mode == "wrasse":
            self.box.guard = Guard(project, ask_scope=self.ui.scope_card, on_scope_decision=self._scope_decided)
        else:
            self.box.on_write = self._log_naive_edit
        self._step_advanced = False
        self._history: list[dict] = []
        self._reflect_due = False

    # ---- helpers ----
    def history(self) -> list[dict]:
        msgs = [m for m in db.get_messages(self.project)
                if m["role"] in ("user", "assistant") and m.get("phase") != "gap_check"]
        return msgs[-HISTORY_TURNS:]

    def say(self, text: str, to_agent: bool = False) -> None:
        """Harness (not agent) output, logged so a resumed session has the record.
        to_agent=True also puts it in the executor's history, so it knows a detour was handled."""
        if to_agent:
            db.add_message(self.project, "assistant", f"[wrasse] {text}", kind="wrasse")
        else:
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
        if state.get("phase") == "gap_check" and self.mode == "wrasse":
            db.add_message(self.project, "user", message, phase="gap_check")
            return self._gap_check(message, state.get("gap") or {})
        self._history = self.history()
        db.add_message(self.project, "user", message)
        if self.mode == "naive":
            return self._naive(message)
        plan = self.plan()
        if plan is None:
            db.set_state(self.project, phase="gap_check", gap={})
            return self._gap_check(message, {})
        self.show_rail(plan)
        if state.get("plan_change_pending"):
            return self._plan_change_reply(message, state["plan_change_pending"], plan)
        return self._triage(message, state, plan)

    def _naive(self, message: str) -> executor.ExecResult:
        """Baseline: every message straight to the executor, full tools, no triage/guard/plan/clock."""
        return self._run(message or "go", context="")

    def _log_naive_edit(self, path: str) -> None:
        _, step = clock.current_step(self.plan())
        db.log_edit(self.project, path, step["id"] if step else None, in_scope(step, path), allowed=True)

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
    def _triage(self, message: str, state: dict, plan: dict) -> executor.ExecResult | None:
        pending = state.get("detour_pending")
        v = triage.classify(message, plan, rules=db.active_rules(self.project),
                            parked=db.get_parked(self.project), pending=pending)
        if v.get("fallback"):
            self.ui.warn(f"{v['fallback']} → treating as on-plan")
        kind = v["kind"]

        if kind == "decision":
            if v.get("decision") in triage.DECISIONS:
                return self._decide(v["decision"], pending, plan)
            self._remind_pending(pending)
            return None

        _, cur = clock.current_step(plan)
        event_id = db.log_event(self.project, prompt=message, kind=kind, verdict=v,
                                mins_left=clock.minutes_left(plan), step_id=cur["id"] if cur else None) \
            if message else None

        if pending:
            # a detour is waiting: answer pure questions, otherwise hold the line until now/later/skip
            if kind == "question" and not v["implies_change"]:
                result = self._run(message, context=self._context(plan), read_only=True)
            else:
                result = None
            self._remind_pending(pending)
            return result

        if kind == "on_plan":
            self._maybe_switch_step(plan, v.get("related_step_id"))
            return self._work(message, plan)

        if kind == "question" and not v["implies_change"]:
            result = self._run(message, context=self._context(plan), read_only=True)
            self.ui.back_to_plan(f"Plan unaffected, continuing {self._step_label(plan)}.")
            return result

        if kind == "plan_change":
            return self._propose_plan_change(message, plan, event_id)

        # new_request, or a question that implies a change: answer the question part, then price the detour
        result = None
        if kind == "question":
            result = self._run(message, context=self._context(plan), read_only=True)
        self.ui.detour_card(v, link=self._link(v, plan))
        db.set_state(self.project, detour_pending={"event_id": event_id, "prompt": message, "verdict": v})
        self.say(f"Detour check: '{v['restatement']}' is not part of the current step. Waiting for the user to "
                 f"decide now / later / skip (recommended: {v['recommendation']}). Do not start it.", to_agent=True)
        return result

    def _maybe_switch_step(self, plan: dict, step_id: str | None) -> None:
        """An on-plan message aimed at another planned step makes that step current."""
        target = next((s for s in plan["steps"] if s["id"] == step_id and s["status"] == "todo"), None)
        if target is None or step_id == plan.get("current_step_id"):
            return
        for s in plan["steps"]:
            if s["status"] == "doing":
                s["status"] = "todo"
        target["status"], plan["current_step_id"] = "doing", target["id"]
        db.save_plan(plan)
        i, _ = clock.current_step(plan)
        self.ui.back_to_plan(f"Switching to Step {i + 1} '{target['title']}' (already in the plan)")

    def _context(self, plan: dict) -> str:
        return build_context(plan, active_rules=db.active_rules(self.project))

    def _step_label(self, plan: dict) -> str:
        i, step = clock.current_step(plan)
        return f"Step {i + 1} '{step['title']}'" if step else "the plan (all steps done)"

    def _link(self, v: dict, plan: dict) -> str | None:
        if v.get("related_parked_id"):
            for p in db.get_parked(self.project):
                if str(p["_id"]) == str(v["related_parked_id"]):
                    return f"you parked this at {clock.fmt_ts(p['ts'])} (\"{p.get('summary') or p['text']}\")"
        if v.get("related_step_id"):
            for i, s in enumerate(plan["steps"]):
                if s["id"] == v["related_step_id"]:
                    return f"this belongs to Step {i + 1} '{s['title']}' [{s['status']}]"
        return None

    def _remind_pending(self, pending: dict) -> None:
        self.ui.detour_card(pending["verdict"])
        self.ui.info("A detour is pending. Reply now / later / skip to get back to the plan.")

    def _decide(self, decision: str, pending: dict, plan: dict) -> executor.ExecResult | None:
        v = pending["verdict"]
        if pending.get("event_id") is not None:
            db.update_event(pending["event_id"], user_decision=decision, decision_ts=db.now())
        if decision == "now":
            step = self._insert_step(plan, v)
            self.ui.progress(f"+ Added {step['id'].upper()} '{step['title']}' after the current step "
                             f"(plan v{plan['version']}).")
        elif decision == "later":
            _, cur = clock.current_step(plan)
            db.add_parked(self.project, text=pending["prompt"], category=v.get("category"),
                          related_step_id=v.get("related_step_id") or (cur["id"] if cur else None),
                          summary=v.get("restatement"))
            self.ui.progress(f"⏸ Parked: {v.get('restatement')}")
        else:
            self.ui.progress(f"✗ Skipped: {v.get('restatement')}")
        db.set_state(self.project, detour_pending=None)
        outcome = {"now": "added to the plan as a new step after the current one",
                   "later": "parked for later; do not work on it now", "skip": "skipped; do not work on it"}[decision]
        self.say(f"Detour '{v.get('restatement')}' {outcome}. Back to the plan.", to_agent=True)
        self._reflect(pending.get("event_id"))
        return self._resume()

    def _insert_step(self, plan: dict, v: dict) -> dict:
        i, _ = clock.current_step(plan)
        used = {s["id"] for s in plan["steps"]}
        n = len(used) + 1
        while f"s{n}" in used:
            n += 1
        est = int(v["impact"].get("est_min") or 15)
        step = {"id": f"s{n}", "title": v["restatement"], "why": v.get("related_reason", ""),
                "size": "cupcake" if est <= 30 else "layer", "est_min": est,
                "files_scope": list(v["impact"].get("files_likely") or []), "status": "todo"}
        pos = (i + 1) if i is not None else len(plan["steps"])
        plan["steps"].insert(pos, step)
        if plan.get("current_step_id") is None:
            step["status"], plan["current_step_id"] = "doing", step["id"]
        plan["version"] = plan.get("version", 1) + 1
        plan.setdefault("history", []).append({"version": plan["version"], "ts": clock.now().isoformat(),
                                               "change": f"added {step['id']} '{step['title']}'",
                                               "reason": "detour approved: now"})
        db.save_plan(plan)
        return step

    def _resume(self) -> executor.ExecResult | None:
        """Return-to-plan guarantee: after any detour decision, the executor resumes the current step."""
        plan = self.plan()
        i, step = clock.current_step(plan)
        if step is None:
            self.ui.back_to_plan("Back to plan → all steps done.")
            return None
        self.ui.back_to_plan(f"Back to plan → Step {i + 1} '{step['title']}'")
        if step.get("fresh"):
            # nothing done on this step yet: wait for the user instead of starting it from its title alone
            return None
        self._history = self.history()
        return self._run(f"Continue Step {i + 1}: {step['title']}.", context=self._context(plan))

    def _scope_decided(self, event_id=None) -> None:
        """Scope y/n happens mid-turn; learn once the agent's turn is over."""
        self._reflect_due = True

    def _reflect(self, event_id=None) -> None:
        """Rule learning after every detour decision and scope y/n."""
        self._reflect_due = False
        for op, text in rules.reflect(self.project):
            if op == "add":
                self.ui.toast(f"🧠 Wrasse learned: {text}")
            else:
                past = {"strengthen": "strengthened", "weaken": "weakened", "retire": "retired"}[op]
                self.ui.info(f"🧠 rule {past}: {text}")

    # ---- plan_change ----
    def _propose_plan_change(self, instruction: str, plan: dict, event_id) -> None:
        basics = {"goal": plan["goal"], "done_definition": plan["done_definition"]}
        mins = clock.minutes_left(plan) or 0
        try:
            draft = planner.make_plan(basics, self.workspace, mins, previous=plan, instruction=instruction)
        except Exception as e:
            self.ui.warn(f"planner failed ({e}); plan unchanged.")
            return None
        self.ui.plan_card(draft, clock.budget_minutes(mins), title="🐟 WRASSE: PLAN CHANGE",
                          diff=planner.plan_diff(plan, draft["steps"]))
        db.set_state(self.project, plan_change_pending={"draft": draft, "instruction": instruction,
                                                        "event_id": event_id})
        return None

    def _plan_change_reply(self, message: str, pending: dict, plan: dict) -> executor.ExecResult | None:
        low = message.lower()
        if low in YES:
            new = planner.apply_revision(plan, pending["draft"], reason=pending["instruction"])
            db.save_plan(new)
            db.set_state(self.project, plan_change_pending=None)
            if pending.get("event_id") is not None:
                db.update_event(pending["event_id"], user_decision="y", decision_ts=db.now())
            self.ui.progress(f"Plan v{new['version']} saved: {new['history'][-1]['change']}")
            return self._resume()
        if low in NO:
            db.set_state(self.project, plan_change_pending=None)
            if pending.get("event_id") is not None:
                db.update_event(pending["event_id"], user_decision="n", decision_ts=db.now())
            self.ui.info("Plan change discarded.")
            return self._resume()
        db.set_state(self.project, plan_change_pending=None)
        return self._propose_plan_change(f"{pending['instruction']}; then: {message}", plan,
                                         pending.get("event_id"))

    def _work(self, message: str, plan: dict) -> executor.ExecResult | None:
        """Executor works the current step."""
        i, step = clock.current_step(plan)
        if step is None:
            self.ui.progress("All steps done. 🎉")
            if not message or message.lower() in triage.CONTINUE_WORDS:
                return None
        if step.pop("fresh", None):
            db.save_plan(plan)
        if not message or message.lower() in triage.CONTINUE_WORDS:
            message = f"Continue Step {i + 1}: {step['title']}."
        return self._run(message, context=self._context(plan))

    def _run(self, message: str, context: str, read_only: bool = False) -> executor.ExecResult:
        self._step_advanced = False
        result = executor.run(message, self.box, context=context, history=self._history, read_only=read_only,
                              on_tool=self.ui.tool, should_stop=lambda: self._step_advanced)
        self.ui.agent(result.text)
        if result.stop not in ("end_turn", "interrupted"):
            self.ui.warn(f"agent stopped: {result.stop}")
        db.add_message(self.project, "assistant", result.text or f"({result.stop})")
        self._history = self.history()
        if self._reflect_due:
            self._reflect()
        return result

    def _step_done(self, summary: str) -> str:
        plan = self.plan()
        if plan is None or plan.get("current_step_id") is None:
            return "no current step to mark done"
        i, step = clock.current_step(plan)
        nxt = planner.advance(plan)
        if nxt:
            nxt["fresh"] = True     # reached by finishing the last step; no work on it yet
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
