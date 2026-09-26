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

from .session import Session, create_project
from .ui import UI

console = Console()
BANNER = "[bold cyan]🐟 WRASSE[/bold cyan] [dim]· your AI is the shark, Wrasse keeps it clean[/dim]"


def chat(session: Session) -> None:
    console.print(BANNER)
    console.print(f"[dim]project {session.project} · mode {session.mode} · workspace {session.workspace}[/dim]")
    console.print("[dim]Type a message. Empty line or 'go' continues. 'quit' exits.[/dim]\n")
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
    console.print("[dim]rule learning arrives in build step 4[/dim]")


def cmd_eval(args) -> None:
    console.print("[dim]eval arrives in build step 5[/dim]")


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
    sub.add_parser("eval").set_defaults(fn=cmd_eval)
    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except (FileExistsError, KeyError, RuntimeError) as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
