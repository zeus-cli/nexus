"""Extração tolerante de JSON de respostas de LLM.

Modelos locais frequentemente devolvem o objeto JSON cercado por Markdown
(`` ```json ... ``` ``) ou por texto explicativo, mesmo quando ``json_mode``
está ativo. Fazer ``json.loads`` diretamente transforma uma resposta válida em
fallback silencioso (REPLAN/plano trivial). Este módulo centraliza a extração
sem relaxar a validação: o resultado precisa ser um objeto JSON.
"""

from __future__ import annotations

import json
from typing import Any


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def extract_json_object(text: str) -> dict[str, Any]:
    """Retorna o primeiro objeto JSON encontrado em ``text``.

    Aceita o objeto puro, cercado por fence Markdown ou embutido em prosa.
    Levanta ``ValueError`` quando nenhum objeto JSON válido existe.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("resposta do modelo vazia ou não textual")

    candidate = text.strip()
    fenced = _strip_code_fence(candidate)
    for payload in (fenced, candidate):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    # Procura o primeiro objeto completo, respeitando chaves aninhadas.
    decoder = json.JSONDecoder()
    start = candidate.find("{")
    while start != -1:
        try:
            parsed, _ = decoder.raw_decode(candidate[start:])
        except json.JSONDecodeError:
            start = candidate.find("{", start + 1)
            continue
        if isinstance(parsed, dict):
            return parsed
        start = candidate.find("{", start + 1)

    raise ValueError("nenhum objeto JSON válido encontrado na resposta do modelo")


__all__ = ["extract_json_object"]
