
"""Base types for the NEXUS Automation Engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

class RiskLevel(Enum):
    LOW = auto()
    MEDIUM = auto()
    HIGH = auto()
    CRITICAL = auto()

class AccessLevel(Enum):
    READ = auto()
    WRITE = auto()

class ExecutionMode(Enum):
    HOST = auto()
    SANDBOX = auto()

@dataclass(frozen=True, slots=True)
class Tool:
    id: str
    version: str
    description: str
    risk: RiskLevel
    access: AccessLevel
    execution_mode: ExecutionMode = ExecutionMode.HOST
    idempotent: bool = True
    timeout_s: float = 10.0
    argv_template: tuple[str, ...] = field(default_factory=tuple)
    required_autonomy: int = 1
