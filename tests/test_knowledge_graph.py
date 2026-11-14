"""Testes do Knowledge Graph persistente (T3).

Cobrem migração, CRUD de nós/arestas, proveniência, confiança, validade,
consultas determinísticas, travessia BFS com ciclos, cascade e o adapter
persistente do ``KnowledgeEngine``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nexus_core.knowledge_engine import (
    KnowledgeEngine,
    KnowledgeKind,
    KnowledgeSource,
)
from nexus_core.knowledge_graph import (
    KnowledgeGraph,
    KnowledgeGraphError,
)
from nexus_core.memory.store import Database


def future(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def past(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.fixture()
def database(tmp_path: Path):
    db = Database(tmp_path / "data" / "knowledge.db")
    db.connect()
    assert db.migrate() == [1, 2, 3, 4, 5]
    yield db
    db.close()


@pytest.fixture()
def graph(database: Database) -> KnowledgeGraph:
    return KnowledgeGraph(database)


def add_node(graph: KnowledgeGraph, node_id: str, **kwargs):
    defaults = {
        "node_type": "concept",
        "label": node_id,
        "kind": KnowledgeKind.ASSUMPTION,
        "source": KnowledgeSource.MODEL_INFERENCE,
        "confidence": 0.5,
    }
    defaults.update(kwargs)
    return graph.add_node(node_id=node_id, **defaults)


# --------------------------------------------------------------------------- #
# Migration / persistência
# --------------------------------------------------------------------------- #

def test_migration_creates_graph_tables_and_is_idempotent(database: Database) -> None:
    tables = {
        row["name"]
        for row in database.query(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"knowledge_nodes", "knowledge_edges"} <= tables
    assert database.schema_version() == 5
    assert database.migrate() == []
    assert database.pending_migrations() == []


def test_schema_rejects_invalid_confidence_even_without_graph(
    database: Database,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            "INSERT INTO knowledge_nodes "
            "(node_id, node_type, label, kind, source, confidence, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("bypass", "concept", "bypass", "FACT", "USER", 1.5, now, now),
        )


def test_graph_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "data" / "knowledge.db"
    first = Database(path)
    first.connect()
    first.migrate()
    graph = KnowledgeGraph(first)
    add_node(graph, "root", label="root node")
    graph.add_edge("root", "root", "self", edge_id="edge-1")
    first.close()

    second = Database(path)
    second.connect()
    try:
        assert second.migrate() == []
        reopened = KnowledgeGraph(second)
        node = reopened.get_node("root")
        assert node is not None
        assert node.label == "root node"
        assert reopened.get_edge("edge-1").source_id == "root"
    finally:
        second.close()


# --------------------------------------------------------------------------- #
# Nós
# --------------------------------------------------------------------------- #

def test_add_node_persists_provenance_and_enums_by_name(
    graph: KnowledgeGraph, database: Database
) -> None:
    node = graph.add_node(
        "server",
        "web-01",
        node_id="node-web-01",
        properties={"role": "frontend", "zone": "dmz"},
        kind=KnowledgeKind.FACT,
        source=KnowledgeSource.OBSERVATION,
        source_ref="event-123",
        confidence=0.75,
        metadata={"owner": "ops"},
    )
    assert node.kind is KnowledgeKind.FACT
    assert node.source is KnowledgeSource.OBSERVATION
    assert node.confidence == 0.75
    assert node.properties == {"role": "frontend", "zone": "dmz"}
    assert node.metadata == {"owner": "ops"}
    assert node.validation_evidence == ()

    row = database.query(
        "SELECT kind, source FROM knowledge_nodes WHERE node_id = ?", ("node-web-01",)
    )[0]
    assert row["kind"] == "FACT"
    assert row["source"] == "OBSERVATION"


def test_add_node_rejects_invalid_inputs(graph: KnowledgeGraph) -> None:
    with pytest.raises(ValueError):
        add_node(graph, "confidence-high", confidence=1.1)
    with pytest.raises(ValueError):
        add_node(graph, "confidence-low", confidence=-0.1)
    with pytest.raises(TypeError):
        add_node(graph, "confidence-bool", confidence=True)
    with pytest.raises(ValueError):
        add_node(graph, "bad-kind", kind="NOT_A_KIND")
    with pytest.raises(ValueError):
        add_node(graph, "bad-type", node_type="Bad Type")
    with pytest.raises(ValueError):
        add_node(graph, "bad id")
    with pytest.raises(TypeError):
        add_node(graph, "bad-properties", properties=["not", "object"])
    with pytest.raises(ValueError):
        add_node(graph, "nan-properties", properties={"value": float("nan")})
    with pytest.raises(ValueError):
        add_node(graph, "naive-expiry", expires_at="2026-01-01T00:00:00")


def test_add_duplicate_node_fails(graph: KnowledgeGraph) -> None:
    add_node(graph, "duplicated")
    with pytest.raises(KnowledgeGraphError):
        add_node(graph, "duplicated")


def test_get_and_update_node_replace_all_fields(graph: KnowledgeGraph) -> None:
    add_node(graph, "updated", label="old", properties={"a": 1}, metadata={"x": 1})
    updated = graph.update_node(
        "updated",
        node_type="service",
        label="new",
        properties={"b": 2},
        kind=KnowledgeKind.FACT,
        source=KnowledgeSource.USER,
        confidence=0.9,
        metadata={"y": 2},
        validated_by="operator",
        validation_evidence=("manual-review",),
    )
    assert updated.label == "new"
    assert updated.node_type == "service"
    assert updated.properties == {"b": 2}
    assert updated.metadata == {"y": 2}
    assert updated.validated_by == "operator"
    assert updated.validation_evidence == ("manual-review",)

    with pytest.raises(KeyError):
        graph.update_node("missing", node_type="service", label="x")


def test_list_nodes_filters_and_excludes_expired(graph: KnowledgeGraph) -> None:
    active = add_node(graph, "active", node_type="host", kind=KnowledgeKind.FACT)
    expired = add_node(
        graph, "expired", node_type="host", kind=KnowledgeKind.FACT, expires_at=past()
    )
    add_node(graph, "assumption", node_type="idea")

    assert [node.node_id for node in graph.list_nodes(node_type="host")] == [
        active.node_id
    ]
    assert [node.node_id for node in graph.list_nodes(kind=KnowledgeKind.FACT)] == [
        active.node_id
    ]
    assert {node.node_id for node in graph.list_nodes(include_expired=True)} == {
        "active",
        "expired",
        "assumption",
    }
    assert expired.node_id == "expired"


def test_list_nodes_limit_is_validated(graph: KnowledgeGraph) -> None:
    add_node(graph, "one")
    with pytest.raises(ValueError):
        graph.list_nodes(limit=0)
    with pytest.raises(ValueError):
        graph.list_nodes(limit=10_001)


def test_delete_node_cascades_edges(graph: KnowledgeGraph, database: Database) -> None:
    add_node(graph, "a")
    add_node(graph, "b")
    graph.add_edge("a", "b", "links", edge_id="edge-ab")
    assert graph.delete_node("a") is True
    assert graph.get_node("a") is None
    assert graph.get_edge("edge-ab") is None
    assert database.query("SELECT COUNT(*) FROM knowledge_edges")[0][0] == 0
    assert graph.delete_node("a") is False


# --------------------------------------------------------------------------- #
# Arestas
# --------------------------------------------------------------------------- #

def test_add_and_update_edge(graph: KnowledgeGraph) -> None:
    add_node(graph, "a")
    add_node(graph, "b")
    edge = graph.add_edge(
        "a",
        "b",
        "depends_on",
        edge_id="edge-ab",
        properties={"since": "2026"},
        kind=KnowledgeKind.HYPOTHESIS,
        source=KnowledgeSource.EXTERNAL,
        source_ref="doc-1",
        confidence=0.4,
        metadata={"note": "draft"},
    )
    assert edge.relation == "depends_on"
    assert edge.kind is KnowledgeKind.HYPOTHESIS
    assert edge.metadata == {"note": "draft"}

    updated = graph.update_edge(
        "edge-ab",
        source_id="b",
        target_id="a",
        relation="informs",
        kind=KnowledgeKind.FACT,
        source=KnowledgeSource.VALIDATED_EXPERIENCE,
        confidence=0.95,
        validated_by="operator",
        validation_evidence=("incident-42",),
    )
    assert updated.source_id == "b"
    assert updated.target_id == "a"
    assert updated.relation == "informs"
    assert updated.validated_by == "operator"
    assert updated.properties == {}
    assert updated.metadata == {}

    with pytest.raises(KeyError):
        graph.update_edge(
            "missing", source_id="a", target_id="b", relation="links"
        )


def test_add_edge_requires_existing_nodes_and_unique_id(graph: KnowledgeGraph) -> None:
    add_node(graph, "a")
    with pytest.raises(KnowledgeGraphError):
        graph.add_edge("a", "missing", "links")
    with pytest.raises(KnowledgeGraphError):
        graph.add_edge("missing", "a", "links")

    add_node(graph, "b")
    graph.add_edge("a", "b", "links", edge_id="edge-1")
    with pytest.raises(KnowledgeGraphError):
        graph.add_edge("a", "b", "links", edge_id="edge-1")


def test_list_edges_filters_and_expiration(graph: KnowledgeGraph) -> None:
    add_node(graph, "a")
    add_node(graph, "b")
    add_node(graph, "c")
    graph.add_edge("a", "b", "links", edge_id="edge-1")
    graph.add_edge("b", "c", "links", edge_id="edge-2", expires_at=past())
    graph.add_edge("a", "c", "informs", edge_id="edge-3")

    assert [edge.edge_id for edge in graph.list_edges(source_id="a")] == [
        "edge-3",
        "edge-1",
    ]
    assert [edge.edge_id for edge in graph.list_edges(relation="links")] == ["edge-1"]
    assert [edge.edge_id for edge in graph.list_edges(target_id="c")] == ["edge-3"]
    assert {edge.edge_id for edge in graph.list_edges(include_expired=True)} == {
        "edge-1",
        "edge-2",
        "edge-3",
    }
    with pytest.raises(ValueError):
        graph.list_edges(relation="Not Valid")


# --------------------------------------------------------------------------- #
# Consultas e travessia
# --------------------------------------------------------------------------- #

def test_neighbors_respects_direction_and_relation(graph: KnowledgeGraph) -> None:
    add_node(graph, "root")
    add_node(graph, "out")
    add_node(graph, "in")
    graph.add_edge("root", "out", "links", edge_id="edge-out")
    graph.add_edge("in", "root", "links", edge_id="edge-in")
    graph.add_edge("root", "in", "mentions", edge_id="edge-mention")

    assert [node.node_id for node in graph.neighbors("root", direction="out")] == [
        "out",
        "in",
    ]
    assert [node.node_id for node in graph.neighbors("root", direction="in")] == [
        "in"
    ]
    assert {
        node.node_id for node in graph.neighbors("root", direction="both")
    } == {"in", "out"}
    assert [
        node.node_id
        for node in graph.neighbors("root", relation="mentions", direction="out")
    ] == ["in"]
    with pytest.raises(ValueError):
        graph.neighbors("root", direction="sideways")


def test_traverse_is_breadth_first_and_prevents_cycles(graph: KnowledgeGraph) -> None:
    add_node(graph, "root")
    add_node(graph, "a")
    add_node(graph, "b")
    graph.add_edge("root", "b", "links", edge_id="edge-b")
    graph.add_edge("root", "a", "links", edge_id="edge-a")
    graph.add_edge("a", "b", "links", edge_id="edge-ab")
    graph.add_edge("b", "root", "links", edge_id="edge-cycle")

    paths = graph.traverse("root", max_depth=3)
    assert [path.node.node_id for path in paths] == ["root", "a", "b"]
    assert [path.depth for path in paths] == [0, 1, 1]
    assert paths[1].via_edge.edge_id == "edge-a"
    assert paths[2].parent_id == "root"

    limited = graph.traverse("root", max_depth=1, limit=2)
    assert [path.node.node_id for path in limited] == ["root", "a"]
    with pytest.raises(KeyError):
        graph.traverse("missing")


def test_traverse_filters_expired_nodes_and_edges(graph: KnowledgeGraph) -> None:
    add_node(graph, "root")
    add_node(graph, "active")
    add_node(graph, "expired-node", expires_at=past())
    add_node(graph, "expired-edge-target")
    graph.add_edge("root", "active", "links", edge_id="edge-active")
    graph.add_edge("root", "expired-node", "links", edge_id="edge-node")
    graph.add_edge(
        "root", "expired-edge-target", "links", edge_id="edge-expired", expires_at=past()
    )

    assert [path.node.node_id for path in graph.traverse("root")] == ["root", "active"]
    assert {
        path.node.node_id for path in graph.traverse("root", include_expired=True)
    } == {"root", "active", "expired-node", "expired-edge-target"}


def test_search_nodes_is_case_insensitive_and_filtered(graph: KnowledgeGraph) -> None:
    add_node(
        graph,
        "postgres",
        node_type="service",
        label="PostgreSQL Primary",
        properties={"region": "eu-west"},
    )
    add_node(graph, "redis", node_type="service", label="Redis Cache")
    add_node(graph, "note", node_type="idea", label="postgres migration plan")

    results = graph.search_nodes("postgres")
    assert {node.node_id for node in results} == {"postgres", "note"}
    assert [
        node.node_id for node in graph.search_nodes("postgres", node_type="service")
    ] == ["postgres"]
    assert graph.search_nodes("ab") == []
    with pytest.raises(ValueError):
        graph.search_nodes("   ")


# --------------------------------------------------------------------------- #
# Promoção e simulação
# --------------------------------------------------------------------------- #

def test_simulation_is_never_promoted_to_fact(graph: KnowledgeGraph) -> None:
    add_node(
        graph,
        "simulated",
        kind=KnowledgeKind.SIMULATION,
        source=KnowledgeSource.SIMULATION_OUTPUT,
        confidence=0.99,
    )
    assert graph.promote_node_to_fact("simulated", validated_by="operator") is False
    assert graph.get_node("simulated").kind is KnowledgeKind.SIMULATION

    graph.add_edge(
        "simulated",
        "simulated",
        "links",
        edge_id="edge-sim",
        kind=KnowledgeKind.SIMULATION,
        source=KnowledgeSource.SIMULATION_OUTPUT,
    )
    assert graph.promote_edge_to_fact("edge-sim", validated_by="operator") is False
    assert graph.get_edge("edge-sim").kind is KnowledgeKind.SIMULATION


def test_promotion_requires_explicit_validator_and_records_evidence(
    graph: KnowledgeGraph,
) -> None:
    add_node(graph, "candidate", kind=KnowledgeKind.HYPOTHESIS, confidence=0.3)
    with pytest.raises(TypeError):
        graph.promote_node_to_fact("candidate", validated_by=None)
    with pytest.raises(ValueError):
        graph.promote_node_to_fact("candidate", validated_by="  ")

    assert graph.promote_node_to_fact(
        "candidate", validated_by="operator", validation_evidence=("benchmark",)
    )
    promoted = graph.get_node("candidate")
    assert promoted.kind is KnowledgeKind.FACT
    assert promoted.validated_by == "operator"
    assert promoted.validation_evidence == ("benchmark",)
    assert promoted.confidence == 0.8
    assert graph.promote_node_to_fact("missing", validated_by="operator") is False


def test_mark_simulation_downgrades_unvalidated_but_not_validated_fact(
    graph: KnowledgeGraph,
) -> None:
    add_node(graph, "validated", kind=KnowledgeKind.FACT, validated_by="operator")
    assert graph.mark_node_as_simulation("validated") is False
    assert graph.get_node("validated").kind is KnowledgeKind.FACT

    add_node(graph, "unvalidated", kind=KnowledgeKind.FACT, confidence=0.9)
    assert graph.mark_node_as_simulation("unvalidated") is True
    downgraded = graph.get_node("unvalidated")
    assert downgraded.kind is KnowledgeKind.SIMULATION
    assert downgraded.source is KnowledgeSource.SIMULATION_OUTPUT
    assert downgraded.confidence == 0.6
    assert downgraded.metadata["flagged_as_simulation"] is True
    assert graph.mark_node_as_simulation("missing") is False


def test_edge_promotion_and_simulation_marking(graph: KnowledgeGraph) -> None:
    add_node(graph, "a")
    add_node(graph, "b")
    graph.add_edge("a", "b", "links", edge_id="edge-1", confidence=0.4)
    assert graph.promote_edge_to_fact("edge-1", validated_by="operator")
    edge = graph.get_edge("edge-1")
    assert edge.kind is KnowledgeKind.FACT
    assert edge.confidence == 0.8
    assert graph.mark_edge_as_simulation("edge-1") is False
    assert graph.promote_edge_to_fact("missing", validated_by="operator") is False


# --------------------------------------------------------------------------- #
# Estatísticas
# --------------------------------------------------------------------------- #

def test_stats_aggregate_nodes_edges_and_kinds(graph: KnowledgeGraph) -> None:
    add_node(graph, "fact", node_type="host", kind=KnowledgeKind.FACT, source=KnowledgeSource.USER)
    add_node(
        graph,
        "sim",
        node_type="simulation",
        kind=KnowledgeKind.SIMULATION,
        source=KnowledgeSource.SIMULATION_OUTPUT,
    )
    graph.add_edge("fact", "sim", "links", edge_id="edge-1")

    stats = graph.stats()
    assert stats["nodes"] == 2
    assert stats["edges"] == 1
    assert stats["by_kind"]["FACT"] == 1
    assert stats["by_kind"]["SIMULATION"] == 1
    assert stats["by_source"]["USER"] == 1
    assert stats["by_node_type"] == {"host": 1, "simulation": 1}
    assert stats["edge_kinds"]["ASSUMPTION"] == 1
    assert stats["by_relation"] == {"links": 1}


# --------------------------------------------------------------------------- #
# Adapter persistente do KnowledgeEngine
# --------------------------------------------------------------------------- #

def test_knowledge_engine_persists_records_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "data" / "knowledge.db"
    first = Database(path)
    first.connect()
    first.migrate()
    engine = KnowledgeEngine(store=KnowledgeGraph(first))
    record = engine.add(
        kind=KnowledgeKind.HYPOTHESIS,
        content="cache hit rate will exceed 90 percent",
        source=KnowledgeSource.MODEL_INFERENCE,
        source_ref="task-1",
        confidence=0.55,
        metadata={"task_id": "task-1"},
        expires_at=future(),
    )
    first.close()

    second = Database(path)
    second.connect()
    try:
        reopened = KnowledgeEngine(store=KnowledgeGraph(second))
        loaded = reopened.get(record.record_id)
        assert loaded is not None
        assert loaded.content == record.content
        assert loaded.kind is KnowledgeKind.HYPOTHESIS
        assert loaded.metadata == {"task_id": "task-1"}
        assert loaded.confidence == 0.55
    finally:
        second.close()


def test_knowledge_engine_persistent_operations(graph: KnowledgeGraph) -> None:
    engine = KnowledgeEngine(store=graph)
    record = engine.add(
        kind=KnowledgeKind.ASSUMPTION,
        content="disk latency is acceptable",
        source=KnowledgeSource.OBSERVATION,
        confidence=0.6,
    )
    assert engine.get(record.record_id).is_fact() is False
    assert [item.record_id for item in engine.list(kind=KnowledgeKind.ASSUMPTION)] == [
        record.record_id
    ]
    assert [item.record_id for item in engine.search("disk latency")] == [
        record.record_id
    ]
    assert engine.stats()["ASSUMPTION"] == 1
    assert engine.stats()["total"] == 1

    assert engine.validate_fact(
        record.record_id, validated_by="operator", evidence=("smartctl",)
    )
    validated = engine.get(record.record_id)
    assert validated.kind is KnowledgeKind.FACT
    assert validated.validated_by == "operator"
    assert engine.stats()["FACT"] == 1

    simulation = engine.add(
        kind=KnowledgeKind.SIMULATION,
        content="simulated disk failure",
        source=KnowledgeSource.SIMULATION_OUTPUT,
    )
    assert engine.validate_fact(simulation.record_id, validated_by="operator") is False
    assert engine.mark_simulation(record.record_id) is False
    assert engine.mark_simulation(simulation.record_id) is True

    assert engine.delete(simulation.record_id) is True
    assert engine.get(simulation.record_id) is None
    assert engine.delete("missing") is False
    assert engine.validate_fact("missing", validated_by="operator") is False


def test_knowledge_engine_persistent_search_ignores_other_node_types(
    graph: KnowledgeGraph,
) -> None:
    add_node(graph, "raw", label="knowledge record impostor", node_type="concept")
    engine = KnowledgeEngine(store=graph)
    assert engine.search("knowledge record impostor") == []
    assert engine.list() == []
    stats = engine.stats()
    assert stats["total"] == 0
    assert stats["ASSUMPTION"] == 0
    assert graph.node_stats(node_type="concept")["nodes"] == 1
    assert graph.node_stats(node_type="concept")["by_kind"]["ASSUMPTION"] == 1


def test_knowledge_engine_rejects_invalid_confidence_with_store(
    graph: KnowledgeGraph,
) -> None:
    engine = KnowledgeEngine(store=graph)
    with pytest.raises(ValueError):
        engine.add(
            kind=KnowledgeKind.FACT,
            content="invalid",
            source=KnowledgeSource.USER,
            confidence=2.0,
        )
    with pytest.raises(TypeError):
        engine.add(
            kind=KnowledgeKind.FACT,
            content="invalid",
            source=KnowledgeSource.USER,
            confidence=True,
        )
    with pytest.raises(TypeError):
        engine.add(
            kind=KnowledgeKind.FACT,
            content="invalid",
            source=KnowledgeSource.USER,
            confidence="0.5",
        )
    with pytest.raises(TypeError):
        engine.add(
            kind=KnowledgeKind.FACT,
            content=123,
            source=KnowledgeSource.USER,
            confidence=0.5,
        )
    assert graph.stats()["nodes"] == 0
