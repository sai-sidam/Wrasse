"""Scripted live demo: one real Wrasse session, recorded.

Runs the full pipeline (gap check → plan → build → curveballs → detour cards → later → learned rule)
against the real models and database from the environment, and saves a colour recording
(demo.html, demo.txt) plus a summary of what landed in MongoDB.

    python eval/demo.py --out demo-recording
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from rich.console import Console  # noqa: E402
from rich.rule import Rule  # noqa: E402

from wrasse import db, llm  # noqa: E402
from wrasse import main as cli  # noqa: E402
from wrasse.session import Session, create_project  # noqa: E402
from wrasse.ui import UI  # noqa: E402

GOAL = ("Extend the expense tracker: add a category field and a monthly total report, plus CSV export if "
        "there is time. Deadline in 45 minutes. Done when the tests pass.")
BUILD_TURNS = [
    "",                                                    # continue Step 1
    "could we also add colored terminal output?",          # tempting feature → detour card
    "later",
    "does json.dump handle datetime objects?",             # a question is a question
    "what if we added user accounts?",                     # scope bomb → detour card
    "later",                                               # second 'later' → rule learning
    "",                                                    # back to building
    "",
]


class DemoUI(UI):
    """Real rich UI; answers scope cards with n instead of waiting on a keyboard."""

    def ask(self, prompt: str) -> str:
        self.console.print(f"{prompt}n")
        return "n"


def say(console: Console, msg: str) -> None:
    console.print(f"[bold green]you ›[/bold green] {msg if msg else '[dim](enter)[/dim]'}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="demo-recording")
    ap.add_argument("--project", default=f"demo-{time.strftime('%H%M%S')}")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    console = Console(record=True, force_terminal=True, width=110, color_system="truecolor")
    cli.console = console
    ui = DemoUI(console)
    console.print(cli.BANNER)
    console.print(f"[dim]models: {llm.SONNET} (executor/planner/rules) · {llm.HAIKU} (triage)[/dim]")

    create_project(args.project)
    session = Session(args.project, ui)
    console.print(f"[dim]project {args.project} · mode wrasse · workspace {session.workspace}[/dim]\n")
    session.intro()

    say(console, GOAL)
    session.handle(GOAL)
    for _ in range(4):                                     # drive the gap check to a confirmed plan
        state = db.get_state(args.project)
        if state.get("phase") != "gap_check":
            break
        stage = (state.get("gap") or {}).get("stage")
        msg = {"answers": "", "confirm": "y"}.get(stage, "Deadline in 45 minutes. Done when the tests pass.")
        say(console, msg)
        session.handle(msg)

    for msg in BUILD_TURNS:
        say(console, msg)
        session.handle(msg)

    console.print(Rule("wrasse rules"))
    cli.cmd_rules(SimpleNamespace(project=args.project))

    console.print(Rule("what is in MongoDB"))
    database = db.get_db()
    where = "in-memory (mock)" if os.getenv("WRASSE_DB") == "mock" else \
        f"MongoDB Atlas · db '{db.DB_NAME}' · {len(database.client.nodes) or 'connected'} node(s)"
    console.print(f"[bold]{where}[/bold]")
    for name in db.COLLECTIONS:
        n = database[name].count_documents({"project": args.project}) if name != "eval_runs" else \
            database[name].count_documents({})
        console.print(f"  {name:<10} {n:>4} documents")
    console.print(f"[dim]LLM calls {llm.USAGE.calls} · tokens {llm.USAGE.total}[/dim]")

    console.save_html(str(out / "demo.html"), clear=False)
    console.save_text(str(out / "demo.txt"))
    print(f"saved {out / 'demo.html'}")


if __name__ == "__main__":
    main()
