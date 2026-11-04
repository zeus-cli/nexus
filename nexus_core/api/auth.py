"""Autenticação local da API.

Modelo: um token local (arquivo 0600 ou ``NEXUS_API_TOKEN``) identifica o
usuário da máquina. O token **não** concede privilégio: cada rota exige um
escopo, e o escopo só é liberado se o nível de autonomia configurado permitir.

| escopo  | autonomia mínima |
|---------|------------------|
| read    | 0 (observação)   |
| write   | 1 (assistência)  |
| execute | 2 (execução controlada) |
"""

from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from ..config import AUTONOMY_LEVELS, Config

ENV_API_TOKEN = "NEXUS_API_TOKEN"
TOKEN_PREFIX = "nexus_"

#: escopo mínimo exigido por tipo de operação
SCOPE_REQUIREMENTS: dict[str, int] = {"read": 0, "write": 1, "execute": 2}


class AuthConfigError(Exception):
    """Token local ausente, ilegível ou inválido."""


@dataclass(frozen=True, slots=True)
class TokenInfo:
    token: str
    source: str
    path: Path | None
    created: bool = False


def resolve_token(config: Config) -> TokenInfo:
    """Obtém o token de ``NEXUS_API_TOKEN`` (se definido) ou do arquivo local."""
    env_token = os.environ.get(ENV_API_TOKEN, "").strip()
    if env_token:
        return TokenInfo(token=env_token, source="env", path=None)
    token, created = ensure_token_file(config.api.token_file)
    return TokenInfo(token=token, source="file", path=config.api.token_file, created=created)


def ensure_token_file(path: Path) -> tuple[str, bool]:
    """Lê o token local; cria com permissão 0600 se ainda não existir."""
    path = Path(path)
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise AuthConfigError(f"arquivo de token vazio: {path}")
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
        return token, False
    return rotate_token_file(path), True


def rotate_token_file(path: Path) -> str:
    """Gera um novo token, grava atomicamente com 0600 e o retorna."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"{token}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    path.chmod(0o600)
    return token


class Authenticator:
    def __init__(self, token: str, autonomy_level: int, *, source: str = "file") -> None:
        if not token:
            raise AuthConfigError("token da API vazio")
        if autonomy_level not in AUTONOMY_LEVELS:
            raise ValueError(f"autonomia inválida: {autonomy_level}")
        self._token = token
        self.autonomy_level = autonomy_level
        self.source = source

    def verify(self, presented: str | None) -> bool:
        if not presented:
            return False
        return hmac.compare_digest(self._token, presented.strip())

    def allows(self, scope: str) -> bool:
        required = SCOPE_REQUIREMENTS.get(scope)
        if required is None:
            raise ValueError(f"escopo desconhecido: {scope}")
        return self.autonomy_level >= required

    def denial_reason(self, scope: str) -> str:
        required = SCOPE_REQUIREMENTS[scope]
        return (
            f"escopo '{scope}' exige autonomia nível {required} "
            f"({AUTONOMY_LEVELS[required]}); configurado: nível {self.autonomy_level} "
            f"({AUTONOMY_LEVELS[self.autonomy_level]})"
        )
