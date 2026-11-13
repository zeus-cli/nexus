"""Tool Registry — catálogo padronizado de ferramentas operacionais.

Responsabilidade: manter o registro central de todas as ferramentas disponíveis
para execução governada, com metadados padronizados (risco, acesso, idempotência,
timeout, limites de recurso) e validação de parâmetros.

Regra de projeto: nenhuma ferramenta executa comandos de shell arbitrários gerados
por modelos; todas usam argumentos estruturados com esquema explícito.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Mapping


class ToolRiskLevel(Enum):
    LOW = auto()       # Automático dentro do escopo aprovado
    MEDIUM = auto()    # Autorização dependente de política
    HIGH = auto()      # Aprovação humana obrigatória
    CRITICAL = auto()  # Negado por padrão


class ToolAccessLevel(Enum):
    READ = auto()
    WRITE = auto()
    EXECUTE = auto()
    ADMIN = auto()


class ToolExecutionMode(Enum):
    HOST = auto()      # Execução direta no host (somente leitura / baixo risco)
    SANDBOX = auto()   # Execução isolada em sandbox (bubblewrap)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Especificação padronizada de uma ferramenta."""

    tool_id: str
    version: str
    description: str
    risk: ToolRiskLevel
    access: ToolAccessLevel
    execution_mode: ToolExecutionMode = ToolExecutionMode.HOST
    idempotent: bool = True
    timeout_s: float = 10.0
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    required_capabilities: tuple[str, ...] = ()
    resource_limits: dict[str, Any] = field(default_factory=dict)
    reversible: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "version": self.version,
            "description": self.description,
            "risk": self.risk.name,
            "access": self.access.name,
            "execution_mode": self.execution_mode.name,
            "idempotent": self.idempotent,
            "timeout_s": self.timeout_s,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "required_capabilities": list(self.required_capabilities),
            "resource_limits": dict(self.resource_limits),
            "reversible": self.reversible,
            "metadata": dict(self.metadata),
        }


class ToolRegistryError(Exception):
    """Erro no registro de ferramentas."""


class ToolNotFound(ToolRegistryError):
    """Ferramenta não encontrada no registro."""


class ToolRegistry:
    """Registro central de ferramentas com validação de metadados."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}

    def register(self, spec: ToolSpec, handler: Callable[..., Any] | None = None) -> None:
        """Registra uma ferramenta e (opcionalmente) seu handler de execução."""
        if spec.tool_id in self._tools:
            raise ToolRegistryError(f"tool already registered: {spec.tool_id}")
        self._tools[spec.tool_id] = spec
        if handler is not None:
            self._handlers[spec.tool_id] = handler

    def unregister(self, tool_id: str) -> bool:
        """Remove uma ferramenta do registro."""
        if tool_id not in self._tools:
            return False
        del self._tools[tool_id]
        self._handlers.pop(tool_id, None)
        return True

    def get(self, tool_id: str) -> ToolSpec | None:
        return self._tools.get(tool_id)

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def list_tools_dict(self) -> list[dict[str, Any]]:
        return [spec.as_dict() for spec in self._tools.values()]

    def has_handler(self, tool_id: str) -> bool:
        return tool_id in self._handlers

    def get_handler(self, tool_id: str) -> Callable[..., Any] | None:
        return self._handlers.get(tool_id)

    def validate_input(self, tool_id: str, params: Mapping[str, Any]) -> None:
        """Valida parâmetros de entrada contra o esquema da ferramenta."""
        spec = self._tools.get(tool_id)
        if spec is None:
            raise ToolNotFound(f"tool not found: {tool_id}")
        schema = spec.input_schema
        if not schema:
            return  # sem esquema definido: aceita qualquer parâmetro
        required = schema.get("required", [])
        for key in required:
            if key not in params:
                raise ValueError(f"missing required parameter: {key}")
        properties = schema.get("properties", {})
        for key, value in params.items():
            if key in properties:
                expected_type = properties[key].get("type")
                if expected_type == "string" and not isinstance(value, str):
                    raise ValueError(f"parameter '{key}' must be a string")
                if expected_type == "integer" and (
                    not isinstance(value, int) or isinstance(value, bool)
                ):
                    raise ValueError(f"parameter '{key}' must be an integer")
                if expected_type == "boolean" and not isinstance(value, bool):
                    raise ValueError(f"parameter '{key}' must be a boolean")


__all__ = [
    "ToolRegistry",
    "ToolSpec",
    "ToolRiskLevel",
    "ToolAccessLevel",
    "ToolExecutionMode",
    "ToolRegistryError",
    "ToolNotFound",
]
