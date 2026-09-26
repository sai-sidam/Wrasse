"""Stateless web engine: the Wrasse harness for a browser chat.

The browser holds the state (plan, pending detour, parked ideas, decisions, learned rules) and sends
it with every message; this module runs the real planner, triage and rule learning and returns the
new state plus UI blocks. Code execution stays in the CLI: on-plan messages get the agent's intended
change, and the user marks a step done.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import clock, llm, planner, rules, triage

TEMPLATE = Path(__file__).resolve().parent.parent / "workspace_template"
YES = {"y", "yes", "ok", "okay", "confirm", "lgtm", "looks good"}
NO = {"n", "no", "cancel"}
DONE = {"done", "step done", "finished", "mark done"}


def new_state(goal: str, deadline: str, done: str) -> dict:
    return {"phase": "basics", "basics": {"goal": goal, "deadline": deadline, "done_definition": done},
            "gap": None, "draft": None, "answers": "", "plan": None, "pending": None, "plan_change": None,
            "parked": [], "events": [], "rules": [], "seq": 0}


def _id(state: dict, prefix: str) -> str:
    state["seq"] = state.get("seq", 0) + 1
    return f"{prefix}{state['seq']}"


def _rail(plan: dict) -> dict:
    return {"type": "rail", "steps": [{k: s[k] for k in ("id", "title", "status", "size", "est_min")}
                                      for s in plan["steps"]],
            "clock": clock.clock_line(plan), "version": plan.get("version", 1)}


def _plan_block(draft: dict, mins: int, title: str = "PLAN", diff=None) -> dict:
    return {"type": "plan", "title": title, "steps": draft["steps"], "cut": draft.get("cut", []),
            "budget": clock.budget_minutes(mins), "diff": diff or []}


def _mins(state: dict) -> int:
    return clock.minutes_left({"deadline": state["basics"]["deadline_iso"]}) or 0


def _answer(question: str, plan: dict | None, instruction: str) -> str:
    ctx = ""
    if plan:
        i, step = clock.current_step(plan)
        ctx = f"GOAL: {plan['goal']}\nCURRENT STEP: {step['title'] if step else '(none)'}\nCLOCK: {clock.clock_line(plan)}\n\n"
    resp = llm.create(model=llm.SONNET, max_tokens=900,
                      system=("You are the coding agent inside Wrasse, working on a small Python CLI expense "
                              "tracker. " + instruction + " Be brief: at most 6 short lines, no headings."),
                      messages=[{"role": "user", "content": f"{ctx}WORKSPACE:\n{planner.workspace_snapshot(TEMPLATE, 6000)}"
                                                            f"\n\nUSER: {question}"}])
    return llm.text_of(resp)


# ---- rule learning without a database -------------------------------------

def _reflect(state: dict) -> list[dict]:
    decided = [e for e in state["events"] if e.get("user_decision")]
    if len(decided) < rules.MIN_EVIDENCE:
        return []
    active = [r for r in state["rules"] if r["active"]]
    prompt = "DECIDED EVENTS (oldest first):\n" + "\n".join(rules._event_line({**e, "_id": e["id"]}) for e in decided)
    prompt += "\n\nCURRENT ACTIVE RULES:\n" + ("\n".join(
        f"- id={r['id']} \"{r['text']}\" confidence={r['confidence']}" for r in active) or "(none)")
    try:
        ops = llm.json_call(model=llm.SONNET, system=rules.SYSTEM, prompt=prompt, schema=rules.SCHEMA,
                            max_tokens=1500)["ops"]
    except Exception:
        return []
    known = {e["id"] for e in decided}
    blocks = []
    for op in ops:
        ev = list(dict.fromkeys(x for x in op.get("evidence_event_ids") or [] if x in known))
        if op["op"] == "add":
            text = (op.get("text") or "").strip()
            if len(ev) < rules.MIN_EVIDENCE or not text or any(r["text"] == text and r["active"] for r in state["rules"]):
                continue
            state["rules"].append({"id": _id(state, "r"), "text": text, "evidence": ev, "confidence": len(ev),
                                   "active": True})
            blocks.append({"type": "toast", "text": f"🧠 Wrasse learned: {text}"})
            continue
        rule = next((r for r in state["rules"] if r["id"] == op.get("rule_id") and r["active"]), None)
        if rule is None:
            continue
        if op["op"] == "strengthen" and set(ev) - set(rule["evidence"]):
            rule["evidence"] = list(dict.fromkeys(rule["evidence"] + ev))
            rule["confidence"] = len(rule["evidence"])
            blocks.append({"type": "info", "text": f"🧠 rule strengthened: {rule['text']}"})
        elif op["op"] == "weaken":
            rule["confidence"] -= max(1, len(ev))
            rule["active"] = rule["confidence"] > 0
            blocks.append({"type": "info", "text": f"🧠 rule {'weakened' if rule['active'] else 'retired'}: {rule['text']}"})
        elif op["op"] == "retire":
            rule["active"] = False
            blocks.append({"type": "info", "text": f"🧠 rule retired: {rule['text']}"})
    return blocks


# ---- the pipeline -----------------------------------------------------------

def handle(state: dict, message: str, now_iso: str | None = None) -> tuple[dict, list[dict]]:
    if now_iso:
        clock.set_now(datetime.fromisoformat(now_iso.replace("Z", "+00:00")))
    try:
        return _route(state, (message or "").strip())
    finally:
        clock.set_now(None)


def _route(state: dict, msg: str) -> tuple[dict, list[dict]]:
    phase = state["phase"]
    basics = state["basics"]
    low = msg.lower()

    if phase == "basics":
        if msg:
            basics["deadline"] = msg if clock.find_deadline(msg) and not clock.find_deadline(basics.get("deadline")) \
                else basics.get("deadline")
        dl = clock.find_deadline(basics.get("deadline"))
        missing = [m for m in ("goal", "done_definition") if not basics.get(m)] + ([] if dl else ["deadline"])
        if missing:
            return state, [{"type": "info", "text": "Before I plan, I need: " + ", ".join(missing) +
                            " (a deadline like 17:00, 5pm or 'in 45 minutes')."}]
        basics["deadline_iso"] = dl.isoformat()
        gap = planner.gap_check(basics, TEMPLATE, _mins(state))
        state["gap"] = gap
        if gap["ask"]:
            state["phase"] = "answers"
            return state, [{"type": "gap", "goal": basics["goal"], "done": basics["done_definition"],
                            "mins": _mins(state), "budget": clock.budget_minutes(_mins(state)), **gap}]
        msg, phase = "", "answers"

    if phase == "answers":
        state["answers"] = msg
        draft = planner.make_plan(basics, TEMPLATE, _mins(state), state["gap"], msg)
        state["draft"], state["phase"] = draft, "confirm"
        return state, [_plan_block(draft, _mins(state))]

    if phase == "confirm":
        if low in YES or not msg:
            plan = planner.new_plan("web", basics, state["draft"], basics["deadline_iso"], state["gap"])
            state["plan"], state["phase"], state["draft"] = plan, "building", None
            _, step = clock.current_step(plan)
            return state, [{"type": "progress", "text": f"Plan v1 saved · {len(plan['steps'])} steps."},
                           _rail(plan),
                           {"type": "agent", "title": "agent", "text": _answer(
                               f"Start Step 1: {step['title']}", plan,
                               "Say in 2-4 lines what you will change for this step (files, functions). "
                               "Do not claim you already changed anything.")}]
        draft = planner.make_plan(basics, TEMPLATE, _mins(state), state["gap"], state["answers"],
                                  previous={"steps": state["draft"]["steps"], "version": 1}, instruction=msg)
        state["draft"] = draft
        return state, [_plan_block(draft, _mins(state))]

    plan = state["plan"]
    if phase == "done":
        return state, [{"type": "progress", "text": "All steps done. 🎉 Start a new session to plan again."}]

    # building
    if state.get("plan_change"):
        pc = state["plan_change"]
        state["plan_change"] = None
        if low in YES:
            state["plan"] = plan = planner.apply_revision(plan, pc["draft"], reason=pc["instruction"])
            return state, [{"type": "progress", "text": f"Plan v{plan['version']} saved: {plan['history'][-1]['change']}"},
                           _rail(plan)]
        if low in NO:
            return state, [{"type": "info", "text": "Plan change discarded."}, _rail(plan)]

    if low in DONE and not state["pending"]:
        i, step = clock.current_step(plan)
        nxt = planner.advance(plan)
        blocks = [{"type": "progress", "text": f"✅ Step {i + 1} '{step['title']}' done · "
                   f"{sum(s['status'] == 'done' for s in plan['steps'])}/{len(plan['steps'])}"}, _rail(plan)]
        if nxt is None:
            state["phase"] = "done"
            blocks.append({"type": "progress", "text": "All steps done. 🎉"})
        return state, blocks

    pending = state["pending"]
    parked = [{"_id": p["id"], "ts": p["ts"], "text": p["text"], "summary": p["summary"]} for p in state["parked"]]
    v = triage.classify(msg, plan, rules=[r for r in state["rules"] if r["active"]], parked=parked, pending=pending)
    blocks: list[dict] = []
    if v.get("fallback"):
        blocks.append({"type": "warn", "text": f"{v['fallback']} → treating as on-plan"})
    kind = v["kind"]

    if kind == "decision":
        if v.get("decision") not in triage.DECISIONS:
            return state, blocks + [{"type": "detour", **pending["verdict"]}]
        return state, blocks + _decide(state, v["decision"])

    event = {"id": _id(state, "e"), "prompt": msg, "kind": kind, "verdict": v, "user_decision": None,
             "mins_left": clock.minutes_left(plan)}
    state["events"].append(event)

    if pending:
        if kind == "question" and not v["implies_change"]:
            blocks.append({"type": "agent", "title": "agent · read-only",
                           "text": _answer(msg, plan, "Answer the question. Do not propose code changes.")})
        return state, blocks + [{"type": "detour", **pending["verdict"]},
                                {"type": "info", "text": "A detour is pending. Reply now / later / skip."}]

    if kind == "on_plan":
        target = next((s for s in plan["steps"] if s["id"] == v.get("related_step_id") and s["status"] == "todo"), None)
        if target:
            for s in plan["steps"]:
                if s["status"] == "doing":
                    s["status"] = "todo"
            target["status"], plan["current_step_id"] = "doing", target["id"]
            blocks.append({"type": "back", "text": f"Switching to '{target['title']}' (already in the plan)"})
        i, step = clock.current_step(plan)
        if step is None:
            return state, blocks + [{"type": "progress", "text": "All steps done. 🎉"}]
        blocks.append({"type": "agent", "title": f"agent · Step {i + 1} '{step['title']}'",
                       "text": _answer(msg or f"Continue Step {i + 1}: {step['title']}", plan,
                                       "Say in 2-5 lines exactly what you will change for the current step "
                                       "(files, functions, tests). Stay inside the step's scope. Do not claim you "
                                       "already changed anything.")})
        blocks.append({"type": "info", "text": "In the CLI the agent now edits files and runs the tests. "
                                               "Here, type done when the step is finished."})
        return state, blocks

    if kind == "question" and not v["implies_change"]:
        _, step = clock.current_step(plan)
        return state, blocks + [
            {"type": "agent", "title": "agent · read-only",
             "text": _answer(msg, plan, "Answer the question. Do not propose code changes.")},
            {"type": "back", "text": f"Plan unaffected, continuing '{step['title'] if step else 'the plan'}'."}]

    if kind == "plan_change":
        draft = planner.make_plan({"goal": plan["goal"], "done_definition": plan["done_definition"]}, TEMPLATE,
                                  clock.minutes_left(plan) or 0, previous=plan, instruction=msg)
        state["plan_change"] = {"draft": draft, "instruction": msg}
        return state, blocks + [_plan_block(draft, clock.minutes_left(plan) or 0, "PLAN CHANGE",
                                            planner.plan_diff(plan, draft["steps"]))]

    # a detour: answer any question part, then price it
    if kind == "question":
        blocks.append({"type": "agent", "title": "agent · read-only",
                       "text": _answer(msg, plan, "Answer the question part only. Do not start any work.")})
    state["pending"] = {"event_id": event["id"], "prompt": msg, "verdict": v}
    return state, blocks + [{"type": "detour", **v}]


def _decide(state: dict, decision: str) -> list[dict]:
    plan, pending = state["plan"], state["pending"]
    v = pending["verdict"]
    for e in state["events"]:
        if e["id"] == pending["event_id"]:
            e["user_decision"] = decision
    blocks = []
    if decision == "now":
        i, _ = clock.current_step(plan)
        used = {s["id"] for s in plan["steps"]}
        n = len(used) + 1
        while f"s{n}" in used:
            n += 1
        est = int(v["impact"].get("est_min") or 15)
        step = {"id": f"s{n}", "title": v["restatement"], "why": v.get("related_reason", ""),
                "size": "cupcake" if est <= 30 else "layer", "est_min": est,
                "files_scope": list(v["impact"].get("files_likely") or []), "status": "todo"}
        plan["steps"].insert((i + 1) if i is not None else len(plan["steps"]), step)
        plan["version"] = plan.get("version", 1) + 1
        plan.setdefault("history", []).append({"version": plan["version"], "ts": clock.now().isoformat(),
                                               "change": f"added {step['id']}", "reason": "detour approved: now"})
        blocks.append({"type": "progress", "text": f"+ Added '{step['title']}' after the current step (plan v{plan['version']})."})
    elif decision == "later":
        state["parked"].append({"id": _id(state, "p"), "ts": clock.now().isoformat(), "text": pending["prompt"],
                                "summary": v.get("restatement"), "category": v.get("category")})
        blocks.append({"type": "progress", "text": f"⏸ Parked: {v.get('restatement')}"})
    else:
        blocks.append({"type": "progress", "text": f"✗ Skipped: {v.get('restatement')}"})
    state["pending"] = None
    blocks += _reflect(state)
    i, step = clock.current_step(plan)
    blocks.append({"type": "back", "text": f"Back to plan → Step {i + 1} '{step['title']}'" if step
                   else "Back to plan → all steps done"})
    blocks.append(_rail(plan))
    return blocks


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
