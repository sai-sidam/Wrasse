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
