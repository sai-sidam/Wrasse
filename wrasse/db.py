"""MongoDB Atlas client + collection helpers. Atlas is the store for everything."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

DB_NAME = "wrasse"
COLLECTIONS = ("plans", "state", "messages", "events", "parked", "edits", "rules", "eval_runs")

_db = None


def now() -> datetime:
    return datetime.now(timezone.utc)


def _connect():
    if os.getenv("WRASSE_DB") == "mock":
        import mongomock

        return mongomock.MongoClient()[DB_NAME]
    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set. Put it in .env (or set WRASSE_DB=mock for offline dev).")
    from pymongo import MongoClient

    client = MongoClient(uri, serverSelectionTimeoutMS=8000, appname="wrasse")
    return client[DB_NAME]


def get_db():
    global _db
    if _db is None:
        _db = _connect()
        ensure_indexes(_db)
    return _db


def set_db(db) -> None:
    """Swap the database (tests use a mongomock db)."""
    global _db
    _db = db
    if db is not None:
        ensure_indexes(db)


def ensure_indexes(db) -> None:
    db.plans.create_index("project", unique=True)
    db.state.create_index("project", unique=True)
    for name in ("messages", "events", "parked", "edits"):
        db[name].create_index([("project", 1), ("ts", 1)])
    db.rules.create_index([("project", 1), ("active", 1)])


def col(name: str):
    assert name in COLLECTIONS, name
    return get_db()[name]


def _clean(doc: dict | None) -> dict | None:
    if doc is not None:
        doc.pop("_id", None)
    return doc


# ---- plans -----------------------------------------------------------------

def get_plan(project: str) -> dict | None:
    return _clean(col("plans").find_one({"project": project}))


def save_plan(plan: dict) -> None:
    doc = {k: v for k, v in plan.items() if k != "_id"}
    col("plans").replace_one({"project": plan["project"]}, doc, upsert=True)


# ---- state -----------------------------------------------------------------

def get_state(project: str) -> dict | None:
    return _clean(col("state").find_one({"project": project}))


def set_state(project: str, **fields: Any) -> dict:
    col("state").update_one({"project": project}, {"$set": fields}, upsert=True)
    return get_state(project)


# ---- messages --------------------------------------------------------------

def add_message(project: str, role: str, content: str, **extra: Any) -> None:
    col("messages").insert_one({"project": project, "ts": now(), "role": role, "content": content, **extra})


def get_messages(project: str, limit: int | None = None) -> list[dict]:
    cur = col("messages").find({"project": project}).sort("ts", 1)
    msgs = [_clean(m) for m in cur]
    return msgs[-limit:] if limit else msgs


# ---- events / parked / edits / rules --------------------------------------

def log_event(project: str, **fields: Any):
    doc = {"project": project, "ts": now(), "user_decision": None, "decision_ts": None, **fields}
    return col("events").insert_one(doc).inserted_id


def update_event(event_id, **fields: Any) -> None:
    col("events").update_one({"_id": event_id}, {"$set": fields})


def add_parked(project: str, **fields: Any):
    return col("parked").insert_one({"project": project, "ts": now(), **fields}).inserted_id


def get_parked(project: str) -> list[dict]:
    return list(col("parked").find({"project": project}).sort("ts", 1))


def log_edit(project: str, file: str, step_id: str | None, in_scope: bool, allowed: bool) -> None:
    col("edits").insert_one({"project": project, "ts": now(), "file": file, "step_id": step_id,
                             "in_scope": in_scope, "allowed": allowed})


def active_rules(project: str) -> list[dict]:
    return list(col("rules").find({"project": project, "active": True}).sort("confidence", -1))


def list_projects() -> list[str]:
    return sorted(s["project"] for s in col("state").find({}, {"project": 1}))


def delete_project(project: str) -> None:
    for name in COLLECTIONS:
        if name != "eval_runs":
            col(name).delete_many({"project": project})
