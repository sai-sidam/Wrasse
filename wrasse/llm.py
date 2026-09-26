"""Anthropic wrapper: one client, token accounting, and a JSON-mode helper
with schema validation + retry."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import anthropic
from dotenv import load_dotenv

load_dotenv()

# Override for gateways with their own model names (e.g. OpenRouter): WRASSE_MODEL / WRASSE_TRIAGE_MODEL.
SONNET = os.getenv("WRASSE_MODEL") or "claude-sonnet-5"             # executor, planner, rule reflection
HAIKU = os.getenv("WRASSE_TRIAGE_MODEL") or "claude-haiku-4-5"      # triage (speed)

_client = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, usage) -> None:
        self.calls += 1
        if usage is not None:
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def reset(self) -> None:
        self.input_tokens = self.output_tokens = self.calls = 0


USAGE = Usage()


class LLMError(RuntimeError):
    pass


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def set_client(client) -> None:
    """Swap the client (tests inject a fake)."""
    global _client
    _client = client


def create(*, model: str, system: str, messages: list, max_tokens: int = 16000,
           tools: list | None = None, timeout: float | None = None):
    """Single Messages API call with usage accounting."""
    client = get_client()
    if timeout is not None and hasattr(client, "with_options"):
        client = client.with_options(timeout=timeout, max_retries=0)
    kwargs: dict[str, Any] = dict(model=model, system=system, messages=messages, max_tokens=max_tokens)
    if tools:
        kwargs["tools"] = tools
    resp = client.messages.create(**kwargs)
    USAGE.add(getattr(resp, "usage", None))
    return resp


def endpoint() -> str:
    """Where calls go and how they authenticate, for `wrasse check` (never prints secrets)."""
    base = os.getenv("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    if os.getenv("ANTHROPIC_API_KEY"):
        auth = "ANTHROPIC_API_KEY"
    elif os.getenv("ANTHROPIC_AUTH_TOKEN"):
        auth = "ANTHROPIC_AUTH_TOKEN (bearer)"
    else:
        auth = "none found"
    return f"{base} · auth: {auth}"


def ping(model: str) -> str:
    """One tiny call; returns the reply text or raises."""
    resp = create(model=model, system="Reply with exactly: OK", max_tokens=20,
                  messages=[{"role": "user", "content": "ping"}], timeout=60)
    return text_of(resp) or f"(empty reply, stop_reason={resp.stop_reason})"


def text_of(resp) -> str:
    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text").strip()


# ---- JSON mode -------------------------------------------------------------

def extract_json(text: str) -> Any:
    """Parse the first JSON object in text (tolerates ```json fences and prose)."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
          "integer": int, "number": (int, float), "null": type(None)}


def validate(value: Any, schema: dict, path: str = "$") -> list[str]:
    """Minimal JSON-schema check: type, enum, required, properties, items."""
    errs: list[str] = []
    types = schema.get("type")
    if types:
        types = types if isinstance(types, list) else [types]
        ok = any(isinstance(value, _TYPES[t]) and not (t in ("integer", "number") and isinstance(value, bool))
                 for t in types)
        if not ok:
            return [f"{path}: expected {'/'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errs.append(f"{path}: missing '{key}'")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errs += validate(value[key], sub, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errs += validate(item, schema["items"], f"{path}[{i}]")
    return errs


def json_call(*, model: str, system: str, prompt: str, schema: dict, retries: int = 2,
              max_tokens: int = 4000, timeout: float | None = None) -> dict:
    """Ask for strict JSON matching schema. Re-prompts with the errors on failure."""
    sys_prompt = (f"{system}\n\nRespond with ONLY a single JSON object (no prose, no code fences) "
                  f"matching this JSON schema:\n{json.dumps(schema)}")
    messages: list[dict] = [{"role": "user", "content": prompt}]
    last_err = ""
    for _ in range(retries + 1):
        resp = create(model=model, system=sys_prompt, messages=messages, max_tokens=max_tokens, timeout=timeout)
        raw = text_of(resp)
        try:
            obj = extract_json(raw)
            errs = validate(obj, schema)
            if not errs:
                return obj
            last_err = "; ".join(errs[:8])
        except ValueError as e:
            last_err = f"invalid JSON: {e}"
        messages += [{"role": "assistant", "content": raw or "(empty)"},
                     {"role": "user", "content": f"That was not valid. {last_err}. Reply with corrected JSON only."}]
    raise LLMError(f"json_call failed after {retries + 1} attempts: {last_err}")
