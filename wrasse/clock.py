"""Time-left math and the clock line the agent sees every turn."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

BUFFER = 0.20   # plans fit time left minus a 20% buffer

_now_override: datetime | None = None


def now() -> datetime:
    """Local, timezone-aware now (overridable for tests and the eval)."""
    return _now_override or datetime.now().astimezone()


def set_now(dt: datetime | None) -> None:
    global _now_override
    _now_override = dt


def parse_deadline(text: str | None, ref: datetime | None = None) -> datetime | None:
    """'17:00', '5pm', '5:30 pm', 'in 2h', '90m', '1h30m', '60 minutes', or ISO → aware datetime."""
    if not text:
        return None
    ref = ref or now()
    t = text.strip().lower()
    try:
        dt = datetime.fromisoformat(text.strip())
        return dt if dt.tzinfo else dt.replace(tzinfo=ref.tzinfo)
    except ValueError:
        pass
    rel = re.fullmatch(r"(?:in\s+)?(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?", t)
    if rel and (rel.group(1) or rel.group(2)):
        return ref + timedelta(hours=float(rel.group(1) or 0), minutes=int(rel.group(2) or 0))
    abs_ = re.fullmatch(r"(?:at\s+|by\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if abs_:
        hour, minute, ampm = int(abs_.group(1)), int(abs_.group(2) or 0), abs_.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        dt = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return dt + timedelta(days=1) if dt <= ref else dt
    return None


_DEADLINE_IN_TEXT = re.compile(
    r"(?:in\s+)?\d+(?:\.\d+)?\s*h(?:ours?|rs?)?(?:\s*\d+\s*m(?:in(?:ute)?s?)?)?\b"
    r"|(?:in\s+)?\d+\s*m(?:in(?:ute)?s?)?\b"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b"
    r"|\b\d{1,2}:\d{2}\b", re.I)


def find_deadline(text: str | None, ref: datetime | None = None) -> datetime | None:
    """A deadline written anywhere in a sentence ('... by 5pm, done when ...')."""
    if not text:
        return None
    if (whole := parse_deadline(text, ref)) is not None:
        return whole
    for m in _DEADLINE_IN_TEXT.finditer(text):
        if (dt := parse_deadline(m.group(0), ref)) is not None:
            return dt
    return None


def deadline_of(plan: dict | None) -> datetime | None:
    if not plan or not plan.get("deadline"):
        return None
    return datetime.fromisoformat(plan["deadline"])


def minutes_left(plan: dict | None, ref: datetime | None = None) -> int | None:
    dl = deadline_of(plan)
    if dl is None:
        return None
    return int((dl - (ref or now())).total_seconds() // 60)


def budget_minutes(mins_left: int) -> int:
    return max(0, int(mins_left * (1 - BUFFER)))


def fmt_minutes(m: int) -> str:
    sign, m = ("-" if m < 0 else ""), abs(m)
    return f"{sign}{m // 60}h{m % 60:02d}m" if m >= 60 else f"{sign}{m}m"


def current_step(plan: dict | None) -> tuple[int, dict] | tuple[None, None]:
    if not plan:
        return None, None
    for i, s in enumerate(plan.get("steps", [])):
        if s["id"] == plan.get("current_step_id"):
            return i, s
    return None, None


def clock_line(plan: dict | None, ref: datetime | None = None) -> str:
    """⏱ 14:05 · deadline 17:00 · 2h55m left · Step 2/4 'Monthly total report' est 40m"""
    ref = ref or now()
    parts = [f"⏱ {ref:%H:%M}"]
    dl = deadline_of(plan)
    if dl:
        left = minutes_left(plan, ref)
        parts.append(f"deadline {dl.astimezone(ref.tzinfo):%H:%M}")
        parts.append(f"{fmt_minutes(left)} left" if left >= 0 else f"OVER by {fmt_minutes(-left)}")
    i, step = current_step(plan)
    if step:
        parts.append(f"Step {i + 1}/{len(plan['steps'])} '{step['title']}' est {step.get('est_min', '?')}m")
    elif plan and plan.get("steps") and all(s["status"] in ("done", "parked") for s in plan["steps"]):
        parts.append("all steps done")
    return " · ".join(parts)


def fmt_ts(ts) -> str:
    """Mongo timestamp (naive UTC) → local HH:MM."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(now().tzinfo).strftime("%H:%M")
