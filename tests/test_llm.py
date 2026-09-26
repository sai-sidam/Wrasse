import pytest

from wrasse import llm
from conftest import response, text_block

SCHEMA = {"type": "object", "required": ["kind", "n"],
          "properties": {"kind": {"type": "string", "enum": ["a", "b"]}, "n": {"type": "integer"}}}


def test_extract_json_variants():
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('Sure! {"a": {"b": [1]}} trailing') == {"a": {"b": [1]}}
    with pytest.raises(ValueError):
        llm.extract_json("nope")


def test_validate():
    assert llm.validate({"kind": "a", "n": 1}, SCHEMA) == []
    errs = llm.validate({"kind": "c", "n": True}, SCHEMA)
    assert any("not in" in e for e in errs) and any("integer" in e for e in errs)
    assert llm.validate({}, SCHEMA) == ["$: missing 'kind'", "$: missing 'n'"]


def test_json_call_retries_then_succeeds(fake_llm):
    fake_llm.queue = [response(text_block("not json")), response(text_block('{"kind": "z", "n": 1}')),
                      response(text_block('{"kind": "b", "n": 2}'))]
    assert llm.json_call(model=llm.HAIKU, system="s", prompt="p", schema=SCHEMA) == {"kind": "b", "n": 2}
    last = fake_llm.requests[-1]["messages"]
    assert last[-1]["role"] == "user" and "not in" in last[-1]["content"]
    assert llm.USAGE.calls == 3 and llm.USAGE.total == 45


def test_json_call_gives_up(fake_llm):
    fake_llm.queue = [response(text_block("x"))] * 2
    with pytest.raises(llm.LLMError):
        llm.json_call(model=llm.HAIKU, system="s", prompt="p", schema=SCHEMA, retries=1)


def test_models_overridable_by_env(monkeypatch):
    import importlib
    monkeypatch.setenv("WRASSE_MODEL", "anthropic/some-sonnet")
    monkeypatch.setenv("WRASSE_TRIAGE_MODEL", "anthropic/some-haiku")
    try:
        mod = importlib.reload(llm)
        assert (mod.SONNET, mod.HAIKU) == ("anthropic/some-sonnet", "anthropic/some-haiku")
    finally:
        monkeypatch.delenv("WRASSE_MODEL")
        monkeypatch.delenv("WRASSE_TRIAGE_MODEL")
        importlib.reload(llm)


def test_endpoint_never_shows_secrets(monkeypatch):
    monkeypatch.delenv("WRASSE_AUTH_TOKEN", raising=False)     # the environment may set Wrasse's own gateway
    monkeypatch.delenv("WRASSE_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://openrouter.ai/api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-or-secret")
    out = llm.endpoint()
    assert out == "https://openrouter.ai/api · auth: ANTHROPIC_AUTH_TOKEN (bearer)" and "secret" not in out


def test_check_command(fake_llm, capsys, monkeypatch):
    from wrasse.main import main
    monkeypatch.setenv("WRASSE_DB", "mock")
    fake_llm.queue = [response(text_block("OK")), response(text_block("OK"))]
    main(["check"])
    out = capsys.readouterr().out
    assert out.count("✓") == 2 and "mock" in out
    assert [r["model"] for r in fake_llm.requests] == [llm.SONNET, llm.HAIKU]


def test_wrasse_gateway_vars_take_priority(monkeypatch):
    monkeypatch.setenv("WRASSE_BASE_URL", "https://openrouter.ai/api")
    monkeypatch.setenv("WRASSE_AUTH_TOKEN", "sk-or-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-sent")
    llm.set_client(None)
    try:
        c = llm.get_client()
        assert str(c.base_url).startswith("https://openrouter.ai/api")
        assert c.auth_headers == {"Authorization": "Bearer sk-or-test"}      # no Anthropic key leaks
        assert llm.endpoint() == "https://openrouter.ai/api · auth: WRASSE_AUTH_TOKEN (bearer)"
    finally:
        llm.set_client(None)
