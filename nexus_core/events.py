"""Modelo de evento do NEXUS.

Todo evento possui identidade estável (``event_id``), classificação por
``kind`` hierárquico (ex.: ``perception.fs.created``) e origem explícita.
``dedup_key`` permite idempotência: o mesmo fato reemitido dentro da janela de
deduplicação não é gravado duas vezes.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")


class Severity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    source: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    severity: Severity = Severity.INFO
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    correlation_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: ``False`` para fatos que podem se repetir legitimamente com payload
    #: idêntico (transições de ciclo de vida) e não devem ser deduplicados.
    dedup: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not KIND_PATTERN.match(self.kind):
            raise ValueError(
                f"kind inválido: {self.kind!r} (esperado minúsculas, dígitos, '_' e '.')"
            )
        if not isinstance(self.source, str) or not self.source or any(
            char.isspace() for char in self.source
        ):
            raise ValueError(f"source inválido: {self.source!r} (não vazio, sem espaços)")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload deve ser um mapeamento")
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id não vazio é obrigatório")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("created_at deve ser um datetime com timezone")
        if not isinstance(self.dedup, bool):
            raise ValueError("dedup deve ser booleano")

        object.__setattr__(self, "created_at", self.created_at.astimezone(timezone.utc))
        object.__setattr__(self, "payload", dict(self.payload))
        if not isinstance(self.severity, Severity):
            try:
                object.__setattr__(self, "severity", Severity(str(self.severity).lower()))
            except ValueError as exc:
                raise ValueError(f"severity inválida: {self.severity!r}") from exc

    def canonical_payload(self) -> str:
        """JSON canônico (chaves ordenadas) usado para persistência e dedup."""
        return json.dumps(
            dict(self.payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )

    def payload_size(self) -> int:
        return len(self.canonical_payload().encode("utf-8"))

    def dedup_key(self) -> str | None:
        """Chave de idempotência, ou ``None`` quando o evento não é deduplicável."""
        if not self.dedup:
            return None
        material = "\x1f".join(
            (self.kind, self.source, self.severity.value, self.canonical_payload())
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "source": self.source,
            "severity": self.severity.value,
            "payload": dict(self.payload),
            "correlation_id": self.correlation_id,
            "created_at": self.created_at.isoformat(),
            "dedup_key": self.dedup_key(),
        }


def parse_timestamp(value: Any, label: str = "timestamp") -> datetime:
    """Converte ISO-8601 (com ou sem timezone, ``Z`` aceito) em datetime UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} inválido: {value!r}") from exc
    else:
        raise ValueError(f"{label} inválido: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
