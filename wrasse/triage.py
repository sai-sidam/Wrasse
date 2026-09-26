"""Message classification + detour pricing (Haiku, for speed).

A question is a question, not an order. Every detour is priced against the plan."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from . import clock, llm

TIMEOUT_S = 8
KINDS = ["on_plan", "question", "new_request", "plan_change", "decision"]
DECISIONS = ["now", "later", "skip"]
CONTINUE_WORDS = {"", "go", "continue", "next", "keep going", "carry on", "ok go"}

_pro_con = {"type": "object", "required": ["pro", "con"],
            "properties": {"pro": {"type": "string"}, "con": {"type": "string"}}}

SCHEMA = {
    "type": "object",
    "required": ["kind", "implies_change", "restatement"],
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "decision": {"type": ["string", "null"], "enum": DECISIONS + [None]},
        "implies_change": {"type": "boolean"},
        "restatement": {"type": "string"},
        "related_to_goal": {"type": "string", "enum": ["yes", "partly", "no"]},
        "related_reason": {"type": "string"},
        "related_step_id": {"type": ["string", "null"]},
        "related_parked_id": {"type": ["string", "null"]},
        "impact": {"type": "object", "properties": {
            "delays_steps": {"type": "array", "items": {"type": "string"}},
            "est_min": {"type": "integer"},
            "files_likely": {"type": "array", "items": {"type": "string"}},
            "risk": {"type": "string"}}},
        "options": {"type": "object", "properties": {"now": _pro_con, "later": _pro_con, "skip": _pro_con}},
        "recommendation": {"type": "string", "enum": DECISIONS},
        "recommendation_reason": {"type": "string"},
        "rule_applied": {"type": ["string", "null"]},
        "category": {"type": "string", "enum": ["polish", "feature", "refactor", "infra", "knowledge"]},
    },
}

SYSTEM = """You are Wrasse's triage. A coding agent is executing a plan against a deadline. Classify the user's
latest message and, when it is a detour, price it against the plan.

kind:
- on_plan: an instruction that advances the CURRENT step (or 'go'/continue).
- question: the user asks something. A question is NOT an order. implies_change=true only if answering it
  properly would require changing code or the plan (e.g. "could we also add X?", "what if we added Y?").
  Pure knowledge questions ("does json.dump handle datetime?") have implies_change=false.
- new_request: asks for work that is not the current step (a new feature, polish, refactor, or a later step).
- plan_change: explicitly changes the plan itself (reorder, drop, replace steps, change deadline/goal).
- decision: answers a pending detour with now / later / skip.

For new_request, or question with implies_change=true, fill EVERY detour field:
related_to_goal (yes|partly|no) + related_reason; related_step_id if it belongs to an existing step;
related_parked_id if it matches a parked item; impact {delays_steps (step ids pushed back), est_min,
files_likely, risk: low|medium|high}; options now/later/skip each with one-line pro and con;
recommendation now|later|skip with a recommendation_reason that cites the time left and any rule you used
(put that rule's text in rule_applied); category.
Weigh time left heavily: with little time, protect the plan. Learned rules describe this user's preferences."""


def _context(message: str, plan: dict, rules: list[dict], parked: list[dict], pending: dict | None) -> str:
    i, step = clock.current_step(plan)
    steps = [f"{s['id']} [{s['status']}] {s['title']} ({s['size']}, {s['est_min']}m)" for s in plan["steps"]]
    parts = [f"CLOCK: {clock.clock_line(plan)}",
             f"MINUTES LEFT: {clock.minutes_left(plan)}",
             f"GOAL: {plan['goal']}",
             f"CURRENT STEP: {step['id'] + ' ' + step['title'] if step else '(none: plan complete)'}",
             "STEPS:\n" + "\n".join(steps)]
    if rules:
        parts.append("LEARNED RULES:\n" + "\n".join(f"- {r['text']} (confidence {r.get('confidence', 1)})"
                                                   for r in rules))
    if parked:
        parts.append("PARKED:\n" + "\n".join(f"- id={p['_id']} at {clock.fmt_ts(p['ts'])}: {p.get('summary') or p['text']}"
                                             for p in parked))
    if pending:
        parts.append(f"PENDING DETOUR awaiting now/later/skip: {pending.get('prompt')}")
    parts.append(f"USER MESSAGE: {message}")
    return "\n\n".join(parts)


def fallback(message: str, reason: str) -> dict:
    return {"kind": "on_plan", "implies_change": False, "restatement": message, "fallback": reason}


def quick(message: str, pending: dict | None) -> dict | None:
    """Deterministic routing that needs no model call."""
    m = message.strip().lower().rstrip(".!")
    if pending and m in DECISIONS:
        return {"kind": "decision", "decision": m, "implies_change": False, "restatement": m}
    if m in CONTINUE_WORDS:
        return {"kind": "on_plan", "implies_change": False, "restatement": "continue"}
    return None


def classify(message: str, plan: dict, rules: list[dict] | None = None, parked: list[dict] | None = None,
             pending: dict | None = None, timeout: float = TIMEOUT_S) -> dict:
    """Never raises: on failure or timeout the message is treated as on_plan (with a 'fallback' reason)."""
    if (q := quick(message, pending)) is not None:
        return q
    prompt = _context(message, plan, rules or [], parked or [], pending)
    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(llm.json_call, model=llm.HAIKU, system=SYSTEM, prompt=prompt, schema=SCHEMA,
                      retries=1, max_tokens=1500, timeout=timeout)
    try:
        verdict = fut.result(timeout=timeout)
    except FutureTimeout:
        return fallback(message, f"triage timed out (>{timeout:g}s)")
    except Exception as e:
        return fallback(message, f"triage failed: {e}")
    finally:
        pool.shutdown(wait=False)
    return normalize(verdict, message, pending)


def normalize(v: dict, message: str, pending: dict | None) -> dict:
    """Make the verdict safe to route on and to render."""
    if v["kind"] == "decision" and not (pending and v.get("decision") in DECISIONS):
        v["kind"] = "on_plan" if not pending else "decision"
    v.setdefault("decision", None)
    v.setdefault("related_to_goal", "partly")
    v.setdefault("related_reason", "")
    v.setdefault("related_step_id", None)
    v.setdefault("related_parked_id", None)
    imp = v.setdefault("impact", {})
    imp.setdefault("delays_steps", [])
    imp.setdefault("est_min", 0)
    imp.setdefault("files_likely", [])
    imp.setdefault("risk", "unknown")
    opts = v.setdefault("options", {})
    for k in DECISIONS:
        opts.setdefault(k, {"pro": "", "con": ""})
    v.setdefault("recommendation", "later")
    v.setdefault("recommendation_reason", "")
    v.setdefault("rule_applied", None)
    v.setdefault("category", "feature")
    return v


def is_detour(v: dict) -> bool:
    return v["kind"] == "new_request" or (v["kind"] == "question" and v["implies_change"])


def dumps(v: dict) -> str:
    return json.dumps(v, default=str)
