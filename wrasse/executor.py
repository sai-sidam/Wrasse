"""Tool-use agent loop. The same loop runs in wrasse and naive mode; only the
system context, the tool set and the guard differ."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from . import llm
from .tools import ToolBox

MAX_STEPS = 25

BASE_SYSTEM = """You are a coding agent working inside a sandboxed project workspace.
Use the tools to inspect and change files. Keep changes small and focused.
Run the tests after you change code. Write complete file contents with write_file.
When you finish, reply with a short summary of what you did."""

READ_ONLY_NOTE = """This turn is READ-ONLY. Answer the user's question (you may read files),
but do not change anything. Write tools are not available."""


@dataclass
class ToolCall:
    name: str
    args: dict
    output: str
    is_error: bool


@dataclass
class ExecResult:
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    steps: int = 0
    stop: str = "end_turn"        # end_turn | max_steps | interrupted | max_tokens | refusal | error


def _history_messages(history: list[dict]) -> list[dict]:
    """Chat history (role/content text) → valid alternating Messages API turns."""
    out: list[dict] = []
    for m in history:
        role = "assistant" if m["role"] == "assistant" else "user"
        text = (m.get("content") or "").strip()
        if not text:
            continue
        if not out and role == "assistant":
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    return out


def run(message: str, box: ToolBox, *, context: str = "", history: list[dict] | None = None,
        read_only: bool = False, max_steps: int = MAX_STEPS, model: str = llm.SONNET,
        on_tool: Callable[[ToolCall], None] | None = None,
        should_stop: Callable[[], bool] | None = None) -> ExecResult:
    system = BASE_SYSTEM + (f"\n\n{context}" if context else "") + (f"\n\n{READ_ONLY_NOTE}" if read_only else "")
    messages = _history_messages(history or [])
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] += "\n\n" + message
    else:
        messages.append({"role": "user", "content": message})

    result = ExecResult(text="")
    texts: list[str] = []
    for step in range(max_steps):
        result.steps = step + 1
        try:
            resp = llm.create(model=model, system=system, messages=messages, tools=box.specs(read_only))
        except Exception as e:  # never crash the session on an API failure
            result.stop, result.text = "error", f"(executor error: {e})"
            return result
        texts.append(llm.text_of(resp))
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            result.stop = resp.stop_reason or "end_turn"
            break

        tool_results = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            args = block.input if isinstance(block.input, dict) else json.loads(block.input or "{}")
            output, is_error = box.call(block.name, args, read_only=read_only)
            call = ToolCall(block.name, args, output, is_error)
            result.calls.append(call)
            if on_tool:
                on_tool(call)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": output, "is_error": is_error})
        messages.append({"role": "user", "content": tool_results})
        if should_stop and should_stop():
            result.stop = "interrupted"
            break
    else:
        result.stop = "max_steps"

    result.text = "\n\n".join(t for t in texts if t).strip()
    return result
