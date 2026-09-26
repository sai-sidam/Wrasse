"""Sandbox tools the executor can call. Every path is confined to the workspace.

Enforcement beyond the sandbox boundary lives in guard.py and is plugged in via
the `guard` hook, so naive mode can run the same tools with no guard.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

MAX_READ = 20_000
MAX_TEST_OUTPUT = 4_000
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}

TOOL_SPECS = [
    {"name": "list_dir", "description": "List files under a directory of the workspace (recursive).",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Workspace-relative directory. Default '.'"}}}},
    {"name": "read_file", "description": "Read a UTF-8 text file from the workspace.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Create or overwrite a workspace file with the full new contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "run_tests", "description": "Run the workspace test suite with pytest and return the output.",
     "input_schema": {"type": "object", "properties": {
         "args": {"type": "string", "description": "Optional extra pytest args, e.g. a test file path."}}}},
    {"name": "mark_step_done", "description": "Mark the current plan step done. Run tests first; they must pass.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
]
WRITE_TOOLS = {"write_file", "mark_step_done"}


class Guard(Protocol):
    def before_write(self, path: str) -> str | None: ...   # error message or None
    def before_done(self, box: "ToolBox") -> str | None: ...


class SandboxError(ValueError):
    pass


@dataclass
class ToolBox:
    root: Path
    guard: Guard | None = None
    on_write: Callable[[str], None] | None = None
    on_step_done: Callable[[str], str | None] | None = None   # returns a progress note
    write_seq: int = 0                 # bumps on every successful write
    tests_seq: int = -1                # write_seq at the last test run
    tests_passed: bool = False
    files_written: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.root = Path(self.root).resolve()

    # -- helpers --
    def resolve(self, rel: str) -> Path:
        p = (self.root / (rel or ".")).resolve()
        if p != self.root and self.root not in p.parents:
            raise SandboxError(f"'{rel}' is outside the workspace")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def specs(self, read_only: bool = False) -> list[dict]:
        return [t for t in TOOL_SPECS if not (read_only and t["name"] in WRITE_TOOLS)]

    @property
    def tests_green_since_write(self) -> bool:
        return self.tests_passed and self.tests_seq == self.write_seq

    # -- dispatch --
    def call(self, name: str, args: dict, read_only: bool = False) -> tuple[str, bool]:
        """Run a tool. Returns (output, is_error)."""
        if read_only and name in WRITE_TOOLS:
            return f"{name} is disabled: this turn is read-only (answer the question, do not change files).", True
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return f"unknown tool {name}", True
        try:
            return fn(**(args or {}))
        except SandboxError as e:
            return str(e), True
        except TypeError as e:
            return f"bad arguments for {name}: {e}", True
        except OSError as e:
            return f"{name} failed: {e}", True

    # -- tools --
    def t_list_dir(self, path: str = ".") -> tuple[str, bool]:
        base = self.resolve(path)
        if not base.is_dir():
            return f"not a directory: {path}", True
        files = [self.rel(p) for p in sorted(base.rglob("*"))
                 if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(self.root).parts)]
        return "\n".join(files) or "(empty)", False

    def t_read_file(self, path: str) -> tuple[str, bool]:
        p = self.resolve(path)
        if not p.is_file():
            return f"no such file: {path}", True
        text = p.read_text(encoding="utf-8", errors="replace")
        return (text[:MAX_READ] + "\n...[truncated]") if len(text) > MAX_READ else text, False

    def t_write_file(self, path: str, content: str) -> tuple[str, bool]:
        p = self.resolve(path)
        rel = self.rel(p)
        if self.guard and (err := self.guard.before_write(rel)):
            return err, True
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self.write_seq += 1
        self.files_written.append(rel)
        if self.on_write:
            self.on_write(rel)
        return f"wrote {rel} ({len(content)} chars)", False

    def t_run_tests(self, args: str = "") -> tuple[str, bool]:
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args.split()]
        try:
            proc = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, timeout=120)
            out, code = proc.stdout + proc.stderr, proc.returncode
        except subprocess.TimeoutExpired:
            out, code = "pytest timed out after 120s", 1
        self.tests_seq = self.write_seq
        self.tests_passed = code == 0
        if len(out) > MAX_TEST_OUTPUT:
            out = "...[truncated]\n" + out[-MAX_TEST_OUTPUT:]
        return f"exit code {code} ({'PASS' if code == 0 else 'FAIL'})\n{out}", code != 0

    def t_mark_step_done(self, summary: str) -> tuple[str, bool]:
        if self.guard and (err := self.guard.before_done(self)):
            return err, True
        note = self.on_step_done(summary) if self.on_step_done else None
        return note or "step marked done", False
