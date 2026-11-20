from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from nexus_core.identity_contracts import (
    ROLES_PATH,
    SCOPES_PATH,
    IdentityCatalog,
    IdentityContractError,
    PrivilegeEscalationError,
    UnknownRoleError,
    UnknownScopeError,
    build_catalog,
    load_catalog,
    narrow_scopes,
    scopes_for_role,
)


@pytest.fixture()
def documents() -> tuple[dict[str, Any], dict[str, Any]]:
    scopes = json.loads(SCOPES_PATH.read_text(encoding="utf-8"))
    roles = json.loads(ROLES_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(scopes), copy.deepcopy(roles)


@pytest.fixture()
def catalog() -> IdentityCatalog:
    return load_catalog()


# -- catálogo versionado ----------------------------------------------------


def test_default_catalog_is_versioned_and_well_formed(
    catalog: IdentityCatalog,
) -> None:
    assert catalog.catalog_version == "1.0.0"
    assert catalog.contract_version == "1.0.0"
    assert len(catalog.scopes) == 28
    assert catalog.role_ids == {"owner", "family", "guest", "service"}
    for scope in catalog.scopes.values():
        assert scope.id.split(".", 1)[0] == scope.category
        assert scope.description.strip()
    for role in catalog.roles.values():
        assert role.description.strip()


def test_owner_covers_every_scope_explicitly(catalog: IdentityCatalog) -> None:
    owner = catalog.role("owner")
    assert owner.scopes == catalog.scope_ids
    assert len(owner.scopes) == len(catalog.scopes)


@pytest.mark.parametrize(
    ("role_id", "expected"),
    [("family", 14), ("guest", 6), ("service", 6)],
)
def test_builtin_role_size_is_pinned(
    catalog: IdentityCatalog, role_id: str, expected: int
) -> None:
    assert len(catalog.scopes_for_role(role_id)) == expected


def test_scopes_for_role_matches_catalog(catalog: IdentityCatalog) -> None:
    assert scopes_for_role(catalog, "guest") == frozenset(
        {
            "identity.self.read",
            "tools.read",
            "memory.read.self",
            "memory.write.self",
            "simulations.run",
            "llm.local",
        }
    )


# -- escopos proibidos por papel -------------------------------------------


def test_family_has_no_remote_llm_or_admin(catalog: IdentityCatalog) -> None:
    family = catalog.scopes_for_role("family")
    assert "llm.remote" not in family
    assert "system.admin" not in family
    assert "identity.principals.manage" not in family
    assert "security.approvals.decide.others" not in family


def test_guest_is_private_and_read_only(catalog: IdentityCatalog) -> None:
    guest = catalog.scopes_for_role("guest")
    assert "tools.read" in guest
    assert "tools.write" not in guest
    assert "tools.execute" not in guest
    assert "memory.read.shared" not in guest
    assert "memory.read.all" not in guest
    assert "security.approvals.decide.self" not in guest


def test_service_has_no_identity_or_approval_scopes(
    catalog: IdentityCatalog,
) -> None:
    service = catalog.scopes_for_role("service")
    assert not any(scope.startswith("identity.") for scope in service)
    assert not any(scope.startswith("security.") for scope in service)
    assert "llm.remote" not in service


# -- narrowing --------------------------------------------------------------


def test_narrow_none_keeps_all_role_scopes(catalog: IdentityCatalog) -> None:
    assert narrow_scopes(catalog, "family") == catalog.scopes_for_role("family")


def test_narrow_keeps_only_requested_subset(catalog: IdentityCatalog) -> None:
    result = narrow_scopes(
        catalog, "family", {"tools.read", "memory.read.shared"}
    )
    assert result == frozenset({"tools.read", "memory.read.shared"})


def test_narrow_empty_iterable_yields_no_scopes(catalog: IdentityCatalog) -> None:
    result = narrow_scopes(catalog, "family", [])
    assert result == frozenset()
    assert isinstance(result, frozenset)


def test_narrow_rejects_unknown_scope(catalog: IdentityCatalog) -> None:
    with pytest.raises(UnknownScopeError, match="tools.nuke"):
        narrow_scopes(catalog, "family", ["tools.nuke"])


def test_narrow_rejects_privilege_escalation(catalog: IdentityCatalog) -> None:
    with pytest.raises(PrivilegeEscalationError) as caught:
        narrow_scopes(catalog, "guest", ["tools.write"])
    assert caught.value.role_id == "guest"
    assert caught.value.scopes == frozenset({"tools.write"})


def test_narrow_rejects_bare_string(catalog: IdentityCatalog) -> None:
    with pytest.raises(IdentityContractError, match="não uma string"):
        narrow_scopes(catalog, "family", "tools.read")  # type: ignore[arg-type]


def test_unknown_role_is_rejected(catalog: IdentityCatalog) -> None:
    with pytest.raises(UnknownRoleError, match="root"):
        narrow_scopes(catalog, "root", [])


# -- mutações que devem falhar em build_catalog -----------------------------


def test_duplicate_scope_is_rejected(
    documents: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    scopes, roles = documents
    scopes["scopes"].append(dict(scopes["scopes"][0]))
    with pytest.raises(IdentityContractError, match="duplicado"):
        build_catalog(scopes, roles)


def test_scope_id_must_be_lowercase_dotted(
    documents: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    scopes, roles = documents
    target = next(scope for scope in scopes["scopes"] if scope["id"] == "tools.read")
    target["id"] = "Tools.Read"
    with pytest.raises(IdentityContractError, match="id inválido"):
        build_catalog(scopes, roles)


def test_scope_category_must_match_prefix(
    documents: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    scopes, roles = documents
    target = next(scope for scope in scopes["scopes"] if scope["id"] == "tools.read")
    target["category"] = "memory"
    with pytest.raises(IdentityContractError, match="category"):
        build_catalog(scopes, roles)


def test_role_with_unknown_scope_is_rejected(
    documents: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    scopes, roles = documents
    family = next(role for role in roles["roles"] if role["id"] == "family")
    family["scopes"].append("tools.nuke")
    with pytest.raises(UnknownScopeError, match="tools.nuke"):
        build_catalog(scopes, roles)


def test_owner_must_cover_every_scope(
    documents: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    scopes, roles = documents
    owner = next(role for role in roles["roles"] if role["id"] == "owner")
    owner["scopes"].remove("system.admin")
    with pytest.raises(IdentityContractError, match="system.admin"):
        build_catalog(scopes, roles)


def test_family_cannot_gain_remote_llm(
    documents: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    scopes, roles = documents
    family = next(role for role in roles["roles"] if role["id"] == "family")
    family["scopes"].append("llm.remote")
    with pytest.raises(IdentityContractError, match="proibidos"):
        build_catalog(scopes, roles)


def test_required_builtin_roles_must_exist(
    documents: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    scopes, roles = documents
    roles["roles"] = [role for role in roles["roles"] if role["id"] != "guest"]
    with pytest.raises(IdentityContractError, match="guest"):
        build_catalog(scopes, roles)


def test_catalog_versions_must_match(
    documents: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    scopes, roles = documents
    roles["catalog_version"] = "2.0.0"
    with pytest.raises(IdentityContractError, match="catalog_version"):
        build_catalog(scopes, roles)


def test_load_catalog_rejects_invalid_json(tmp_path: Path) -> None:
    bad = tmp_path / "scopes.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(IdentityContractError, match="JSON inválido"):
        load_catalog(bad, ROLES_PATH)


def test_load_catalog_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(IdentityContractError, match="não foi possível ler"):
        load_catalog(tmp_path / "missing.json", ROLES_PATH)
