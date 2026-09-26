"""Gap check + plan generation. Default size is cupcake, not wedding cake."""
from __future__ import annotations

import json
from pathlib import Path

from . import clock, llm

SIZES = ["cupcake", "layer", "wedding"]
MAX_SNAPSHOT = 12_000

BASICS_SCHEMA = {
    "type": "object", "required": ["goal", "deadline", "done_definition"],
    "properties": {"goal": {"type": ["string", "null"]}, "deadline": {"type": ["string", "null"]},
                   "done_definition": {"type": ["string", "null"]}},
}

GAP_SCHEMA = {
    "type": "object", "required": ["ask", "assume", "ignore"],
    "properties": {
        "ask": {"type": "array", "items": {"type": "object", "required": ["question", "default"],
                                           "properties": {"question": {"type": "string"},
                                                          "default": {"type": "string"},
                                                          "why": {"type": "string"}}}},
        "assume": {"type": "array", "items": {"type": "string"}},
        "ignore": {"type": "array", "items": {"type": "string"}},
    },
}

STEP_SCHEMA = {
    "type": "object", "required": ["title", "why", "size", "est_min", "files_scope"],
    "properties": {"title": {"type": "string"}, "why": {"type": "string"},
                   "size": {"type": "string", "enum": SIZES}, "est_min": {"type": "integer"},
                   "files_scope": {"type": "array", "items": {"type": "string"}}},
}

PLAN_SCHEMA = {
    "type": "object", "required": ["steps", "cut", "assumptions"],
    "properties": {
        "steps": {"type": "array", "items": STEP_SCHEMA},
        "cut": {"type": "array", "items": {"type": "object", "required": ["item", "reason"],
                                           "properties": {"item": {"type": "string"},
                                                          "reason": {"type": "string"}}}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "change_reason": {"type": "string"},
    },
}

PLANNER_SYSTEM = """You are Wrasse's planner. You keep a coding agent on a plan that fits the time available.
Size matters: a cupcake is the smallest thing that meets the definition of done; a layer adds depth;
a wedding cake is the full-featured version. Default to cupcake. Never plan more than the time budget allows."""


def workspace_snapshot(root: Path, max_chars: int = MAX_SNAPSHOT) -> str:
    root = Path(root)
    files = sorted(p for p in root.rglob("*") if p.is_file()
                   and not {"__pycache__", ".pytest_cache", ".git"}.intersection(p.parts))
    out, used = [f"Files: {', '.join(p.relative_to(root).as_posix() for p in files)}"], 0
    for p in files:
        if p.suffix not in (".py", ".md", ".txt", ".toml", ".ini", ".cfg", ".json"):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if used + len(text) > max_chars:
            out.append(f"--- {p.relative_to(root).as_posix()} (omitted, budget)")
            continue
        used += len(text)
        out.append(f"--- {p.relative_to(root).as_posix()}\n{text}")
    return "\n".join(out)


# ---- a. basics ---------------------------------------------------------------

def extract_basics(message: str, known: dict) -> dict:
    """Pull goal / deadline / done definition out of a free-form message."""
    prompt = (f"Already known: {json.dumps(known)}\nNew user message: {message}\n\n"
              "Extract the project goal, the deadline exactly as the user wrote it (e.g. '17:00', '5pm', "
              "'in 2h'), and the definition of done. Keep known values unless the message replaces them. "
              "Use null for anything still unknown. Do not invent a deadline.")
    try:
        got = llm.json_call(model=llm.SONNET, system=PLANNER_SYSTEM, prompt=prompt, schema=BASICS_SCHEMA,
                            max_tokens=1000)
    except Exception:
        got = {"goal": known.get("goal") or message, "deadline": None, "done_definition": None}
    merged = dict(known)
    for k in ("goal", "deadline", "done_definition"):
        if got.get(k):
            merged[k] = got[k]
    # deterministic fallback: the model missed the deadline, but the message names one
    if not clock.find_deadline(merged.get("deadline")) and clock.find_deadline(message):
        merged["deadline"] = message
    return merged


def missing_basics(basics: dict) -> list[str]:
    missing = []
    if not basics.get("goal"):
        missing.append("goal")
    if not clock.find_deadline(basics.get("deadline")):
        missing.append("deadline")
    if not basics.get("done_definition"):
        missing.append("done_definition")
    return missing


# ---- b. gap check ------------------------------------------------------------

def gap_check(basics: dict, workspace: Path, mins_left: int) -> dict:
    prompt = f"""GOAL: {basics['goal']}
DEFINITION OF DONE: {basics['done_definition']}
TIME LEFT: {clock.fmt_minutes(mins_left)} (plan budget {clock.fmt_minutes(clock.budget_minutes(mins_left))})

WORKSPACE:
{workspace_snapshot(workspace)}

Do a gap check before any building.

First, judge fit. If the goal cannot be done in THIS workspace in the time left (wrong kind of project, or far
too big), say so plainly: the FIRST ask must state that it isn't achievable here in this time and offer the
closest achievable version as the default, e.g. "Building X isn't possible in this workspace in 45m. Closest
achievable: Y. Go with Y?". Never silently swap in a different task.

Then sort every open question into:
- ask: at most 3 questions whose answer would CHANGE the outcome (what gets built, or whether it fits the time).
  Each needs a suggested default the user can accept with a blank reply, and a short why.
- assume: sensible defaults you will state and use without asking.
- ignore: things that don't matter for this goal in this time.
Ask as few questions as possible; zero is fine."""
    gap = llm.json_call(model=llm.SONNET, system=PLANNER_SYSTEM, prompt=prompt, schema=GAP_SCHEMA)
    gap["ask"] = gap["ask"][:3]
    return gap


# ---- c. plan -----------------------------------------------------------------

def make_plan(basics: dict, workspace: Path, mins_left: int, gap: dict | None = None,
              answers: str = "", previous: dict | None = None, instruction: str = "") -> dict:
    """Build (or revise) a plan draft: {steps, cut, assumptions, change_reason?}."""
    budget = clock.budget_minutes(mins_left)
    parts = [f"GOAL: {basics['goal']}", f"DEFINITION OF DONE: {basics['done_definition']}",
             f"TIME LEFT: {clock.fmt_minutes(mins_left)}. PLAN BUDGET (after 20% buffer): {budget} minutes."]
    if gap:
        qa = "\n".join(f"- Q: {q['question']} (default: {q['default']})" for q in gap.get("ask", []))
        parts.append(f"GAP CHECK QUESTIONS:\n{qa or '(none)'}")
        parts.append(f"USER ANSWERS: {answers.strip() or '(blank: accept all defaults)'}")
        parts.append("ASSUMPTIONS: " + "; ".join(gap.get("assume", [])))
        parts.append("IGNORED: " + "; ".join(gap.get("ignore", [])))
    if previous:
        steps = [{k: s[k] for k in ("id", "title", "size", "est_min", "status", "files_scope")}
                 for s in previous["steps"]]
        parts.append(f"CURRENT PLAN (v{previous.get('version', 1)}):\n{json.dumps(steps, indent=1)}")
        parts.append(f"REVISION REQUESTED: {instruction}\nKeep done steps as they are (same title), "
                     "change only what the request needs, and give a change_reason.")
    parts.append(f"WORKSPACE:\n{workspace_snapshot(workspace)}")
    parts.append(f"""Write the plan as ordered steps. Rules:
- The sum of est_min must be <= {budget}. Default each step to size cupcake.
- Each step: short title, why (how it serves the goal), size, est_min, files_scope (workspace-relative
  globs the step may edit, including its tests, e.g. ["expenses.py", "tests/test_expenses.py"]).
- Plan only the work the goal needs. Never add "fix X" steps for problems you have not seen in the
  WORKSPACE above; if the tests may already pass, plan one short step that runs them and fixes only what fails.
- Do not pad the plan to fill the budget: finishing early is better. Fewer, smaller steps win.
- cut: what you deliberately left out (bigger versions, nice-to-haves) and why.
- assumptions: the defaults you are building on.""")
    draft = llm.json_call(model=llm.SONNET, system=PLANNER_SYSTEM, prompt="\n\n".join(parts), schema=PLAN_SCHEMA)
    return normalize_draft(draft, previous)


def normalize_draft(draft: dict, previous: dict | None = None) -> dict:
    """Assign stable ids, keep statuses of steps that survive a revision."""
    old = {s["title"].strip().lower(): s for s in (previous or {}).get("steps", [])}
    used = {s["id"] for s in (previous or {}).get("steps", [])}
    next_n = len(used) + 1
    steps = []
    for s in draft["steps"]:
        prior = old.get(s["title"].strip().lower())
        if prior:
            sid, status = prior["id"], prior["status"]
        else:
            while f"s{next_n}" in used:
                next_n += 1
            sid, status = f"s{next_n}", "todo"
            used.add(sid)
        steps.append({"id": sid, "title": s["title"], "why": s.get("why", ""),
                      "size": s.get("size") if s.get("size") in SIZES else "cupcake",
                      "est_min": max(1, int(s.get("est_min") or 1)),
                      "files_scope": list(s.get("files_scope") or []), "status": status})
    return {"steps": steps, "cut": draft.get("cut", []), "assumptions": draft.get("assumptions", []),
            "change_reason": draft.get("change_reason", "")}


def total_est(steps: list[dict]) -> int:
    return sum(s["est_min"] for s in steps if s["status"] in ("todo", "doing"))


def new_plan(project: str, basics: dict, draft: dict, deadline_iso: str, gap: dict | None = None) -> dict:
    steps = [dict(s) for s in draft["steps"]]
    first = next((s for s in steps if s["status"] == "todo"), None)
    if first:
        first["status"] = "doing"
    assumptions = list(dict.fromkeys((gap or {}).get("assume", []) + draft.get("assumptions", [])))
    return {"project": project, "goal": basics["goal"], "done_definition": basics["done_definition"],
            "deadline": deadline_iso, "assumptions": assumptions, "steps": steps, "cut": draft.get("cut", []),
            "current_step_id": first["id"] if first else None, "version": 1,
            "history": [{"version": 1, "ts": clock.now().isoformat(), "change": "plan created",
                         "reason": "gap check confirmed"}]}


def apply_revision(plan: dict, draft: dict, reason: str) -> dict:
    """Swap in a revised step list and bump the version."""
    plan = dict(plan)
    steps = [dict(s) for s in draft["steps"]]
    cur = next((s for s in steps if s["id"] == plan.get("current_step_id") and s["status"] in ("todo", "doing")),
               None) or next((s for s in steps if s["status"] in ("todo", "doing")), None)
    for s in steps:
        if s["status"] == "doing" and s is not cur:
            s["status"] = "todo"
    if cur:
        cur["status"] = "doing"
    plan["steps"], plan["current_step_id"] = steps, cur["id"] if cur else None
    plan["cut"] = draft.get("cut", plan.get("cut", []))
    plan["version"] = plan.get("version", 1) + 1
    plan["history"] = plan.get("history", []) + [{"version": plan["version"], "ts": clock.now().isoformat(),
                                                  "change": draft.get("change_reason") or "plan revised",
                                                  "reason": reason}]
    return plan


def plan_diff(old: dict, new_steps: list[dict]) -> list[tuple[str, str]]:
    """[(marker, text)] with marker in + - ~ = ."""
    before = {s["id"]: s for s in old["steps"]}
    after = {s["id"]: s for s in new_steps}
    out = []
    for s in new_steps:
        b = before.get(s["id"])
        if b is None:
            out.append(("+", f"{s['id']} {s['title']} ({s['size']}, {s['est_min']}m)"))
        elif (b["title"], b["est_min"], b["size"]) != (s["title"], s["est_min"], s["size"]):
            out.append(("~", f"{s['id']} {b['title']} ({b['est_min']}m) → {s['title']} ({s['size']}, {s['est_min']}m)"))
        else:
            out.append(("=", f"{s['id']} {s['title']}"))
    out += [("-", f"{sid} {s['title']}") for sid, s in before.items() if sid not in after]
    return out


def advance(plan: dict) -> dict | None:
    """Mark the current step done and move to the next todo step. Returns the next step."""
    for s in plan["steps"]:
        if s["id"] == plan.get("current_step_id"):
            s["status"] = "done"
    nxt = next((s for s in plan["steps"] if s["status"] == "todo"), None)
    if nxt:
        nxt["status"] = "doing"
    plan["current_step_id"] = nxt["id"] if nxt else None
    return nxt
