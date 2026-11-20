"""Catálogo de escopos e papéis de identidade do NEXUS (T8).

A fonte da verdade são ``contracts/identity/scopes.json`` e
``contracts/identity/roles.json``. O mesmo catálogo será consumido pelo daemon
Rust (T8.2); por isso os arquivos são versionados e validados aqui antes de
qualquer uso.

Regras centrais:

* escopo desconhecido é erro, nunca ignorado;
* o papel ``owner`` cobre todos os escopos built-in, mas por lista explícita —
  um escopo novo não é concedido automaticamente;
* ``family``, ``guest`` e ``service`` têm escopos proibidos verificados;
* narrowing só pode reduzir: pedir escopo fora do papel é escalada de
  privilégio, exceto quando a coleção pedida é vazia (reduzir a nada).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

IDENTITY_ROOT = Path(__file__).resolve().parent.parent / "contracts" / "identity"
SCOPES_PATH = IDENTITY_ROOT / "scopes.json"
ROLES_PATH = IDENTITY_ROOT / "roles.json"

OWNER_ROLE = "owner"
REQUIRED_ROLE_IDS = ("owner", "family", "guest", "service")

SCOPE_ID_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+$")
ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

#: Escopos que um papel built-in nunca pode receber. Regras terminadas em ``.``
#: são prefixos; as demais são escopos exatos.
FORBIDDEN_SCOPES: dict[str, tuple[str, ...]] = {
    "family": (
        "identity.principals.",
        "identity.devices.",
        "identity.pairing.",
        "security.approvals.read.all",
        "security.approvals.decide.others",
        "memory.read.all",
        "memory.delete.any",
        "agents.manage",
        "tools.execute",
        "llm.remote",
        "system.backup",
        "system.admin",
    ),
    "guest": (
        "identity.principals.",
        "identity.devices.",
        "identity.pairing.",
        "security.",
        "agents.",
        "tools.write",
        "tools.execute",
        "memory.read.shared",
        "memory.write.shared",
        "memory.read.all",
        "memory.delete.any",
        "llm.remote",
        "system.",
    ),
    "service": (
        "identity.",
        "security.",
        "agents.",
        "llm.remote",
    ),
}


class IdentityContractError(ValueError):
    """Catálogo de identidade ausente, malformado ou inconsistente."""


class UnknownScopeError(IdentityContractError):
    """Escopo referenciado que não existe no catálogo."""

    def __init__(self, scope: str, context: str | None = None) -> None:
        self.scope = scope
        self.context = context
        detail = f" em {context}" if context else ""
        super().__init__(f"escopo desconhecido{detail}: {scope!r}")


class UnknownRoleError(IdentityContractError):
    """Papel referenciado que não existe no catálogo."""

    def __init__(self, role_id: str) -> None:
        self.role_id = role_id
        super().__init__(f"papel desconhecido: {role_id!r}")


class PrivilegeEscalationError(IdentityContractError):
    """Narrowing pediu escopo que o papel não concede."""

    def __init__(self, role_id: str, scopes: Iterable[str]) -> None:
        self.role_id = role_id
        self.scopes = frozenset(scopes)
        listed = ", ".join(sorted(self.scopes))
        super().__init__(f"escopos fora do papel {role_id!r}: {listed}")


@dataclass(frozen=True, slots=True)
class Scope:
    """Um escopo atômico do catálogo."""

    id: str
    category: str
    description: str


@dataclass(frozen=True, slots=True)
class Role:
    """Um papel com o conjunto exato de escopos concedidos."""

    id: str
    name: str
    description: str
    builtin: bool
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class IdentityCatalog:
    """Catálogo validado e imutável de escopos e papéis."""

    catalog_version: str
    contract_version: str
    scopes: Mapping[str, Scope]
    roles: Mapping[str, Role]

    @property
    def scope_ids(self) -> frozenset[str]:
        return frozenset(self.scopes)

    @property
    def role_ids(self) -> frozenset[str]:
        return frozenset(self.roles)

    def scope(self, scope_id: str) -> Scope:
        try:
            return self.scopes[scope_id]
        except KeyError:
            raise UnknownScopeError(scope_id) from None

    def role(self, role_id: str) -> Role:
        try:
            return self.roles[role_id]
        except KeyError:
            raise UnknownRoleError(role_id) from None

    def scopes_for_role(self, role_id: str) -> frozenset[str]:
        return self.role(role_id).scopes


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IdentityContractError(f"{context} deve ser um objeto JSON")
    return value


def _string(
    mapping: Mapping[str, Any],
    key: str,
    context: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = mapping.get(key)
    if not isinstance(value, str):
        raise IdentityContractError(f"{context}.{key} deve ser uma string")
    if not allow_empty and not value.strip():
        raise IdentityContractError(f"{context}.{key} não pode ser vazio")
    return value


def _version(mapping: Mapping[str, Any], key: str, context: str) -> str:
    value = _string(mapping, key, context)
    if not VERSION_RE.fullmatch(value):
        raise IdentityContractError(
            f"{context}.{key} deve ser semver (ex.: 1.0.0), recebido {value!r}"
        )
    return value


def _scope_entries(raw: object) -> dict[str, Scope]:
    if not isinstance(raw, list) or not raw:
        raise IdentityContractError("scopes deve ser uma lista não vazia")

    scopes: dict[str, Scope] = {}
    for index, item in enumerate(raw):
        context = f"scopes[{index}]"
        entry = _mapping(item, context)
        scope_id = _string(entry, "id", context)
        category = _string(entry, "category", context)
        description = _string(entry, "description", context)

        if not SCOPE_ID_RE.fullmatch(scope_id):
            raise IdentityContractError(
                f"{context}.id inválido: {scope_id!r} (esperado "
                "'categoria.nome' em minúsculas)"
            )
        if scope_id in scopes:
            raise IdentityContractError(f"escopo duplicado: {scope_id!r}")
        if scope_id.split(".", 1)[0] != category:
            raise IdentityContractError(
                f"{context}.category {category!r} não corresponde ao prefixo "
                f"de {scope_id!r}"
            )
        scopes[scope_id] = Scope(
            id=scope_id,
            category=category,
            description=description,
        )
    return scopes


def _role_entries(raw: object, scopes: Mapping[str, Scope]) -> dict[str, Role]:
    if not isinstance(raw, list) or not raw:
        raise IdentityContractError("roles deve ser uma lista não vazia")

    roles: dict[str, Role] = {}
    for index, item in enumerate(raw):
        context = f"roles[{index}]"
        entry = _mapping(item, context)
        role_id = _string(entry, "id", context)
        name = _string(entry, "name", context)
        description = _string(entry, "description", context)
        builtin = entry.get("builtin")
        if not isinstance(builtin, bool):
            raise IdentityContractError(f"{context}.builtin deve ser booleano")

        if not ROLE_ID_RE.fullmatch(role_id):
            raise IdentityContractError(f"{context}.id inválido: {role_id!r}")
        if role_id in roles:
            raise IdentityContractError(f"papel duplicado: {role_id!r}")

        raw_scopes = entry.get("scopes")
        if not isinstance(raw_scopes, list) or not raw_scopes:
            raise IdentityContractError(
                f"{context}.scopes deve ser uma lista não vazia"
            )
        granted: set[str] = set()
        for raw_scope in raw_scopes:
            if not isinstance(raw_scope, str) or not raw_scope.strip():
                raise IdentityContractError(
                    f"{context}.scopes contém um valor inválido"
                )
            if raw_scope not in scopes:
                raise UnknownScopeError(raw_scope, f"papel {role_id!r}")
            if raw_scope in granted:
                raise IdentityContractError(
                    f"escopo duplicado {raw_scope!r} no papel {role_id!r}"
                )
            granted.add(raw_scope)

        roles[role_id] = Role(
            id=role_id,
            name=name,
            description=description,
            builtin=builtin,
            scopes=frozenset(granted),
        )
    return roles


def _check_required_roles(roles: Mapping[str, Role]) -> None:
    missing = [role_id for role_id in REQUIRED_ROLE_IDS if role_id not in roles]
    if missing:
        raise IdentityContractError(
            "papéis built-in ausentes: " + ", ".join(sorted(missing))
        )
    for role_id in REQUIRED_ROLE_IDS:
        if not roles[role_id].builtin:
            raise IdentityContractError(
                f"papel obrigatório {role_id!r} deve ter builtin=true"
            )


def _check_owner_coverage(
    scopes: Mapping[str, Scope], roles: Mapping[str, Role]
) -> None:
    missing = sorted(set(scopes) - roles[OWNER_ROLE].scopes)
    if missing:
        raise IdentityContractError(
            "owner não cobre todos os escopos; faltam: " + ", ".join(missing)
        )


def _forbidden_hits(scope: str, rules: Iterable[str]) -> bool:
    return any(
        scope == rule or (rule.endswith(".") and scope.startswith(rule))
        for rule in rules
    )


def _check_forbidden_scopes(roles: Mapping[str, Role]) -> None:
    for role_id, rules in FORBIDDEN_SCOPES.items():
        role = roles.get(role_id)
        if role is None:
            continue
        hits = sorted(scope for scope in role.scopes if _forbidden_hits(scope, rules))
        if hits:
            raise IdentityContractError(
                f"papel {role_id!r} contém escopos proibidos: " + ", ".join(hits)
            )


def build_catalog(
    scopes_document: Mapping[str, Any],
    roles_document: Mapping[str, Any],
) -> IdentityCatalog:
    """Valida os dois documentos e devolve o catálogo imutável.

    Levanta :class:`IdentityContractError` (ou uma subclasse) em qualquer
    inconsistência. Não ignora escopos desconhecidos nem papéis ausentes.
    """

    scopes_doc = _mapping(scopes_document, "scopes.json")
    roles_doc = _mapping(roles_document, "roles.json")

    catalog_version = _version(scopes_doc, "catalog_version", "scopes.json")
    contract_version = _version(scopes_doc, "contract_version", "scopes.json")
    if _version(roles_doc, "catalog_version", "roles.json") != catalog_version:
        raise IdentityContractError(
            "scopes.json e roles.json têm catalog_version diferentes"
        )
    if _version(roles_doc, "contract_version", "roles.json") != contract_version:
        raise IdentityContractError(
            "scopes.json e roles.json têm contract_version diferentes"
        )

    scopes = _scope_entries(scopes_doc.get("scopes"))
    roles = _role_entries(roles_doc.get("roles"), scopes)
    _check_required_roles(roles)
    _check_owner_coverage(scopes, roles)
    _check_forbidden_scopes(roles)

    return IdentityCatalog(
        catalog_version=catalog_version,
        contract_version=contract_version,
        scopes=MappingProxyType(dict(scopes)),
        roles=MappingProxyType(dict(roles)),
    )


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as error:
        raise IdentityContractError(f"não foi possível ler {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise IdentityContractError(f"JSON inválido em {path}: {error}") from error
    return _mapping(document, str(path))


def load_catalog(
    scopes_path: Path | str = SCOPES_PATH,
    roles_path: Path | str = ROLES_PATH,
) -> IdentityCatalog:
    """Carrega e valida o catálogo versionado do disco."""

    return build_catalog(_read_json(Path(scopes_path)), _read_json(Path(roles_path)))


def scopes_for_role(catalog: IdentityCatalog, role_id: str) -> frozenset[str]:
    """Escopos concedidos pelo papel, sem narrowing."""

    return catalog.scopes_for_role(role_id)


def narrow_scopes(
    catalog: IdentityCatalog,
    role_id: str,
    requested: Iterable[str] | None = None,
) -> frozenset[str]:
    """Aplica narrowing explícito sobre os escopos de um papel.

    ``None`` mantém todos os escopos do papel; uma coleção vazia reduz a nada;
    qualquer escopo desconhecido ou fora do papel falha. Nunca há união.
    """

    granted = catalog.scopes_for_role(role_id)
    if requested is None:
        return granted
    if isinstance(requested, str):
        raise IdentityContractError(
            "requested deve ser uma coleção de escopos, não uma string"
        )

    requested_ids = frozenset(requested)
    unknown = sorted(requested_ids - catalog.scope_ids)
    if unknown:
        raise UnknownScopeError(unknown[0], "narrowing")

    escalated = requested_ids - granted
    if escalated:
        raise PrivilegeEscalationError(role_id, escalated)
    return requested_ids
