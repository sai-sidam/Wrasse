import os
from types import SimpleNamespace

import mongomock
import pytest

os.environ.setdefault("WRASSE_DB", "mock")

from wrasse import db, llm  # noqa: E402


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(name, args, id_="t1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def response(*blocks, stop=None):
    has_tool = any(b.type == "tool_use" for b in blocks)
    return SimpleNamespace(content=list(blocks), stop_reason=stop or ("tool_use" if has_tool else "end_turn"),
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class FakeClient:
    """Scripted Anthropic client: returns queued responses in order and records requests."""

    def __init__(self, responses=()):
        self.queue = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if not self.queue:
            raise AssertionError("FakeClient ran out of scripted responses")
        r = self.queue.pop(0)
        return r(kwargs) if callable(r) else r

    def with_options(self, **_):
        return self


@pytest.fixture(autouse=True)
def mock_db():
    db.set_db(mongomock.MongoClient()["wrasse"])
    yield db.get_db()
    db.set_db(None)


@pytest.fixture
def fake_llm():
    client = FakeClient()
    llm.set_client(client)
    llm.USAGE.reset()
    yield client
    llm.set_client(None)


@pytest.fixture
def workspaces(tmp_path, monkeypatch):
    monkeypatch.setenv("WRASSE_WORKSPACES", str(tmp_path / "workspaces"))
    return tmp_path / "workspaces"
