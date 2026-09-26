"""CLI entry point.

    wrasse new <project> [--mode wrasse|naive]
    wrasse resume <project> [--mode wrasse|naive]
    wrasse rules [project]
    wrasse eval
"""
from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table

from . import clock, db, rules
from .session import Session, create_project
from .ui import UI

console = Console()
BANNER = "[bold cyan]🐟 WRASSE[/bold cyan] [dim]· your AI is the shark, Wrasse keeps it clean[/dim]"


def chat(session: Session) -> None:
    console.print(BANNER)
    console.print(f"[dim]project {session.project} · mode {session.mode} · workspace {session.workspace}[/dim]")
    console.print("[dim]Type a message. Empty line or 'go' continues. 'quit' exits.[/dim]\n")
    session.intro()
    while True:
        try:
            msg = console.input("[bold green]you ›[/bold green] ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if msg.strip().lower() in ("quit", "exit", ":q"):
            break
        try:
            session.handle(msg)
        except KeyboardInterrupt:
            console.print("[yellow]interrupted[/yellow]")
        except Exception as e:  # the session must never crash the REPL
            console.print(f"[red]error: {e}[/red]")


def cmd_new(args) -> None:
    ws = create_project(args.project, mode=args.mode)
    console.print(f"[green]created[/green] {args.project} → {ws}")
    chat(Session(args.project, UI(console), mode=args.mode))


def cmd_resume(args) -> None:
    chat(Session(args.project, UI(console), mode=args.mode))


def cmd_rules(args) -> None:
    projects = [args.project] if args.project else db.list_projects()
    shown = 0
    for project in projects:
        found = rules.all_rules(project)
        if not found:
            continue
        shown += 1
        table = Table(title=f"🧠 Rules learned · {project}", show_lines=True, title_justify="left")
        for col in ("rule", "conf", "status", "evidence"):
            table.add_column(col, overflow="fold")
        events = {e["_id"]: e for e in db.col("events").find({"project": project})}
        for r in found:
            ev = [events.get(i) for i in r["evidence_event_ids"]]
            evidence = "\n".join(
                f"{clock.fmt_ts(e['ts'])} {(e.get('verdict') or {}).get('restatement') or e['prompt']} → {e['user_decision']}"
                for e in ev if e)
            table.add_row(r["text"], str(r["confidence"]),
                          "[green]active[/green]" if r["active"] else "[dim]retired[/dim]", evidence)
        console.print(table)
    if not shown:
        console.print("[dim]No rules learned yet. Wrasse learns from your now/later/skip and scope y/n decisions.[/dim]")


def cmd_eval(args) -> None:
    import importlib.util

    from .session import REPO_ROOT
    spec = importlib.util.spec_from_file_location("run_eval", REPO_ROOT / "eval" / "run_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main(["--modes", args.modes])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="wrasse", description="An agent harness that holds the plan.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("new", cmd_new), ("resume", cmd_resume)):
        p = sub.add_parser(name)
        p.add_argument("project")
        p.add_argument("--mode", choices=["wrasse", "naive"], default=None if name == "resume" else "wrasse")
        p.set_defaults(fn=fn)
    p = sub.add_parser("rules")
    p.add_argument("project", nargs="?")
    p.set_defaults(fn=cmd_rules)
    p = sub.add_parser("eval")
    p.add_argument("--modes", default="naive,wrasse")
    p.set_defaults(fn=cmd_eval)
    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except (FileExistsError, KeyError, RuntimeError) as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
