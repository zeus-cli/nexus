"""Testes da extração tolerante de JSON de respostas de LLM."""

from __future__ import annotations

import pytest

from nexus_core.llm_router.json_utils import extract_json_object


def test_plain_object() -> None:
    assert extract_json_object('{"decision": "SUCCESS"}') == {"decision": "SUCCESS"}


def test_markdown_fenced_object() -> None:
    text = '```json\n{"steps": [{"type": "reason"}]}\n```'
    assert extract_json_object(text) == {"steps": [{"type": "reason"}]}


def test_object_embedded_in_prose() -> None:
    text = 'Aqui está o resultado:\n{"decision": "RETRY", "justification": "falhou"}\nFim.'
    assert extract_json_object(text) == {
        "decision": "RETRY",
        "justification": "falhou",
    }


def test_nested_braces_are_respected() -> None:
    text = 'texto {"a": {"b": 1}, "c": [{"d": 2}]} resto'
    assert extract_json_object(text) == {"a": {"b": 1}, "c": [{"d": 2}]}


@pytest.mark.parametrize("text", ["", "   ", "sem json", "[1, 2, 3]", "```\n```"])
def test_invalid_payloads_raise(text: str) -> None:
    with pytest.raises(ValueError):
        extract_json_object(text)


__all__: list[str] = []
