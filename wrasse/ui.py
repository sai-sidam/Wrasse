"""Rich rendering. Session talks to a UI object so the eval can run headless."""
from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

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
