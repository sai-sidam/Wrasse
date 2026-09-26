"""Rich rendering. Session talks to a UI object so the eval can run headless."""
from __future__ import annotations

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .executor import ToolCall


class UI:
    def __init__(self, console: Console | None = None):
        self.console = console or Console()

    def info(self, text: str) -> None:
        self.console.print(f"[dim]{text}[/dim]")

    def warn(self, text: str) -> None:
        self.console.print(f"[yellow]⚠ {text}[/yellow]")

    def agent(self, text: str) -> None:
        if text:
            self.console.print(Panel(Markdown(text), title="agent", border_style="cyan", title_align="left"))

    def tool(self, call: ToolCall) -> None:
        arg = call.args.get("path") or call.args.get("args") or call.args.get("summary") or ""
        mark = "[red]✗[/red]" if call.is_error else "[green]✓[/green]"
        first = call.output.splitlines()[0] if call.output else ""
        self.console.print(f"  {mark} [bold]{call.name}[/bold] [dim]{arg}[/dim] [dim]→ {first[:100]}[/dim]")

    def ask(self, prompt: str) -> str:
        return self.console.input(prompt)

    # ---- plan / clock ----
    def clock(self, line: str) -> None:
        self.console.print(Text(line, style="bold magenta"))

    def rail(self, plan: dict, line: str, parked: int = 0) -> None:
        t = Text()
        for i, s in enumerate(plan["steps"]):
            icon, style = STATUS[s["status"]]
            if i:
                t.append("  ›  ", style="dim")
            t.append(f"{icon} {s['id'].upper()} {s['title']}", style=style)
        foot = Text(f"{line} · plan v{plan.get('version', 1)} · parked {parked}", style="dim")
        self.console.print(Panel(Group(t, foot), border_style="blue", padding=(0, 1)))

    def gap_card(self, basics: dict, gap: dict, mins_left: int, budget: int) -> None:
        body = Text()
        body.append(f"Goal: {basics['goal']}\nDone when: {basics['done_definition']}\n", style="bold")
        body.append(f"Time: {mins_left}m left → plan budget {budget}m (20% buffer)\n\n", style="dim")
        if gap["ask"]:
            body.append("ASK (these change the outcome)\n", style="bold yellow")
            for i, q in enumerate(gap["ask"], 1):
                body.append(f"  {i}. {q['question']}  ", style="yellow")
                body.append(f"[default: {q['default']}]\n", style="dim")
        if gap["assume"]:
            body.append("ASSUME\n", style="bold cyan")
            for a in gap["assume"]:
                body.append(f"  • {a}\n")
        if gap["ignore"]:
            body.append("IGNORE\n", style="bold dim")
            for a in gap["ignore"]:
                body.append(f"  • {a}\n", style="dim")
        body.append("\nAnswer in one line (e.g. '1: sqlite, 2: yes'). Blank = accept defaults.", style="italic")
        self.console.print(Panel(body, title="🐟 WRASSE: GAP CHECK", border_style="yellow", title_align="left"))

    def plan_card(self, draft: dict, budget: int, title: str = "🐟 WRASSE: PLAN",
                  diff: list[tuple[str, str]] | None = None) -> None:
        table = Table(box=None, show_header=True, header_style="bold")
        for col in ("#", "step", "size", "est", "scope", "why"):
            table.add_column(col, overflow="fold")
        for s in draft["steps"]:
            table.add_row(s["id"].upper(), s["title"], SIZE_ICON.get(s["size"], s["size"]), f"{s['est_min']}m",
                          ", ".join(s["files_scope"]), Text(s["why"], style="dim"))
        est = sum(s["est_min"] for s in draft["steps"] if s["status"] != "done")
        parts = [table, Text(f"Total {est}m of {budget}m budget" + ("  ⚠ over budget" if est > budget else ""),
                             style="red" if est > budget else "green")]
        if diff:
            d = Text("\nChanges\n", style="bold")
            for mark, line in diff:
                d.append(f"  {mark} {line}\n", style=DIFF_STYLE[mark])
            parts.append(d)
        if draft.get("cut"):
            c = Text("\nCut (and why)\n", style="bold")
            for x in draft["cut"]:
                c.append(f"  ✂ {x['item']}: ", style="red")
                c.append(f"{x['reason']}\n", style="dim")
            c.rstrip()
            parts.append(c)
        parts.append(Text("\nConfirm: y  ·  or type an edit instruction", style="italic"))
        self.console.print(Panel(Group(*parts), title=title, border_style="green", title_align="left"))

    def progress(self, text: str) -> None:
        self.console.print(Text(text, style="bold green"))

    def toast(self, text: str) -> None:
        self.console.print(Panel(Text(text, style="bold"), border_style="bright_cyan", expand=False))

    def back_to_plan(self, text: str) -> None:
        self.console.print(Text(f"↩ {text}", style="bold blue"))

    def detour_card(self, v: dict, link: str | None = None) -> None:
        imp, opts = v["impact"], v["options"]
        pushes = ", ".join(s.upper() for s in imp["delays_steps"]) or "nothing"
        t = Text()
        t.append("Asked: ", style="bold")
        t.append(f"{v['restatement']}\n")
        rel = v["related_to_goal"]
        t.append("Related to goal: ", style="bold")
        t.append(rel.capitalize(), style={"yes": "green", "partly": "yellow", "no": "red"}.get(rel, ""))
        t.append(f", {v['related_reason']}\n" if v["related_reason"] else "\n")
        if link:
            t.append(f"Linked: {link}\n", style="magenta")
        t.append("Impact: ", style="bold")
        t.append(f"pushes {pushes} ~{imp['est_min']}m · touches ~{len(imp['files_likely'])} files · "
                 f"risk: {imp['risk']}\n\n")
        for key in ("now", "later", "skip"):
            t.append(f"{key.capitalize() + ':':<7}", style="bold")
            t.append(f"+ {opts[key]['pro']}", style="green")
            t.append("  ")
            t.append(f"− {opts[key]['con']}\n", style="red")
        t.append("\nRecommendation: ", style="bold")
        t.append(v["recommendation"].upper(), style="bold reverse")
        t.append(f", {v['recommendation_reason']}")
        if v.get("rule_applied"):
            t.append(f"  [rule applied: \"{v['rule_applied']}\"]", style="cyan")
        t.append("\n\nReply: now / later / skip", style="italic")
        self.console.print(Panel(t, title="🐟 WRASSE: DETOUR CHECK", border_style="magenta", title_align="left"))

    def scope_card(self, path: str, step: dict | None) -> bool:
        label = f"Step {step['id'].upper()}'s scope ({step['size']})" if step else "the plan's scope"
        scope = ", ".join((step or {}).get("files_scope") or [])
        body = Text()
        body.append(f"{path}", style="bold")
        body.append(f" is outside {label}.\n")
        body.append(f"In scope: {scope}", style="dim")
        self.console.print(Panel(body, title="🐟 WRASSE: SCOPE CHECK", border_style="red", title_align="left"))
        return self.ask("Expand scope? y/n › ").strip().lower() in ("y", "yes")


STATUS = {"todo": ("○", "white"), "doing": ("◉", "bold yellow"), "done": ("✔", "green"),
          "parked": ("⏸", "dim")}
SIZE_ICON = {"cupcake": "🧁 cupcake", "layer": "🍰 layer", "wedding": "🎂 wedding"}
DIFF_STYLE = {"+": "green", "-": "red", "~": "yellow", "=": "dim"}


class NullUI(UI):
    """Headless UI for the eval: records lines instead of printing."""

    def __init__(self):
        super().__init__(Console(quiet=True))
        self.lines: list[str] = []

    def info(self, text): self.lines.append(text)
    def warn(self, text): self.lines.append("WARN " + text)
    def agent(self, text): self.lines.append("AGENT " + (text or ""))
    def tool(self, call): self.lines.append(f"TOOL {call.name} {call.args.get('path', '')}")
    def ask(self, prompt): return ""
    def clock(self, line): self.lines.append("CLOCK " + line)
    def rail(self, plan, line, parked=0): self.lines.append("RAIL " + line)
    def gap_card(self, basics, gap, mins_left, budget): self.lines.append(f"GAP {gap}")
    def plan_card(self, draft, budget, title="", diff=None): self.lines.append(f"PLAN {draft['steps']}")
    def progress(self, text): self.lines.append("PROGRESS " + text)
    def back_to_plan(self, text): self.lines.append("BACK " + text)
    def toast(self, text): self.lines.append("TOAST " + text)
    def detour_card(self, v, link=None): self.lines.append(f"DETOUR {v['restatement']} → {v['recommendation']}")

    def scope_card(self, path, step):
        self.lines.append(f"SCOPE {path}")
        return self.scope_answer == "y"

    scope_answer = "n"     # the eval auto-answers scope cards with n
