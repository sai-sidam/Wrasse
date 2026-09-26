"""Rule learning: Wrasse reads the user's decisions and rewrites its own triage rules.

Sonnet proposes ops (add | strengthen | weaken | retire); this module validates and applies them.
A new rule needs >= 2 evidence events. confidence = evidence count."""
from __future__ import annotations

from bson import ObjectId

from . import db, llm

RECENT_EVENTS = 30
MIN_EVIDENCE = 2

SCHEMA = {
    "type": "object", "required": ["ops"],
    "properties": {"ops": {"type": "array", "items": {
        "type": "object", "required": ["op"],
        "properties": {
            "op": {"type": "string", "enum": ["add", "strengthen", "weaken", "retire"]},
            "rule_id": {"type": ["string", "null"]},
            "text": {"type": ["string", "null"]},
            "category": {"type": ["string", "null"]},
            "evidence_event_ids": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        }}}},
}

SYSTEM = """You maintain Wrasse's learned triage rules: short, conditional statements of how THIS user decides
detours and scope expansions, so future recommendations match them.

Good rules name a condition and an action, e.g.
- "Parks polish requests when <2h left."
- "Approves scope expansion for test files."
- "Adds knowledge questions' follow-up work now if it touches the current step."

Look at the decided events and the current rules, then return ops:
- add: a NEW pattern supported by at least 2 decided events (list their ids in evidence_event_ids).
  Do not add a rule that duplicates or contradicts an existing active rule; strengthen or weaken that instead.
- strengthen: an existing rule (rule_id) confirmed by new events (evidence_event_ids = the new ones).
- weaken: an existing rule that a recent decision went against (evidence_event_ids = the contradicting ones).
- retire: an existing rule that is clearly wrong now.
Only use event ids and rule ids that appear below. Return {"ops": []} when nothing is warranted."""


def _event_line(e: dict) -> str:
    v = e.get("verdict") or {}
    if e.get("kind") == "scope":
        what = f"scope expansion for file {v.get('file')} (step size {v.get('size')})"
    else:
        what = (f"{e.get('kind')} '{v.get('restatement') or e.get('prompt')}' category={v.get('category')} "
                f"related_to_goal={v.get('related_to_goal')} recommended={v.get('recommendation')}")
    left = e.get("mins_left")
    return f"- id={e['_id']} {what} · {left if left is not None else '?'}m left → user chose {e['user_decision']}"


def decided_events(project: str, limit: int = RECENT_EVENTS) -> list[dict]:
    cur = db.col("events").find({"project": project, "user_decision": {"$ne": None}}).sort("ts", -1).limit(limit)
    return list(reversed(list(cur)))


def all_rules(project: str) -> list[dict]:
    return list(db.col("rules").find({"project": project}).sort("created_at", 1))


def _oid(x) -> ObjectId | None:
    try:
        return ObjectId(str(x))
    except Exception:
        return None


def apply_ops(project: str, ops: list[dict], events: list[dict]) -> list[tuple[str, str]]:
    """Validate and apply ops. Returns [(op, rule_text)] for what actually changed."""
    known = {e["_id"]: e for e in events}
    changes: list[tuple[str, str]] = []
    now = db.now()
    for op in ops:
        evidence = [oid for oid in (_oid(x) for x in op.get("evidence_event_ids") or []) if oid in known]
        evidence = list(dict.fromkeys(evidence))
        kind = op["op"]
        if kind == "add":
            text = (op.get("text") or "").strip()
            if len(evidence) < MIN_EVIDENCE or not text:
                continue
            if db.col("rules").find_one({"project": project, "active": True, "text": text}):
                continue
            db.col("rules").insert_one({"project": project, "text": text, "category": op.get("category"),
                                        "evidence_event_ids": evidence, "confidence": len(evidence),
                                        "active": True, "created_at": now, "updated_at": now})
            changes.append(("add", text))
            continue

        rule = db.col("rules").find_one({"project": project, "_id": _oid(op.get("rule_id"))})
        if rule is None or not rule.get("active"):
            continue
        if kind == "strengthen":
            merged = list(dict.fromkeys(rule["evidence_event_ids"] + evidence))
            if len(merged) == len(rule["evidence_event_ids"]):
                continue
            db.col("rules").update_one({"_id": rule["_id"]}, {"$set": {
                "evidence_event_ids": merged, "confidence": len(merged), "updated_at": now}})
            changes.append(("strengthen", rule["text"]))
        elif kind == "weaken":
            conf = rule.get("confidence", 1) - max(1, len(evidence))
            db.col("rules").update_one({"_id": rule["_id"]}, {"$set": {
                "confidence": max(conf, 0), "active": conf > 0, "updated_at": now}})
            changes.append(("weaken" if conf > 0 else "retire", rule["text"]))
        elif kind == "retire":
            db.col("rules").update_one({"_id": rule["_id"]}, {"$set": {"active": False, "updated_at": now}})
            changes.append(("retire", rule["text"]))
    return changes


def reflect(project: str) -> list[tuple[str, str]]:
    """Read recent decisions + current rules, ask Sonnet for ops, apply them. Never raises."""
    events = decided_events(project)
    if len(events) < MIN_EVIDENCE:
        return []
    rules = [r for r in all_rules(project) if r.get("active")]
    prompt = "DECIDED EVENTS (oldest first):\n" + "\n".join(_event_line(e) for e in events)
    prompt += "\n\nCURRENT ACTIVE RULES:\n" + ("\n".join(
        f"- id={r['_id']} \"{r['text']}\" confidence={r['confidence']} evidence={len(r['evidence_event_ids'])}"
        for r in rules) or "(none)")
    try:
        out = llm.json_call(model=llm.SONNET, system=SYSTEM, prompt=prompt, schema=SCHEMA, max_tokens=2000)
    except Exception:
        return []
    return apply_ops(project, out["ops"], events)
