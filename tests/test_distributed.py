"""Testes dos componentes de Distributed & Adaptive Ecosystem (Fase 7)."""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from nexus_core.distributed_node import (
    DistributedNode,
    NodeResources,
    NodeRole,
    NodeStatus,
)
from nexus_core.distributed_node_manager import NodeManager
from nexus_core.distributed_resources import (
    DistributedResourceMonitor,
    ResourceMonitor,
)
from nexus_core.distributed_scheduler import (
    DistributedTaskScheduler,
    TaskPriority,
    ScheduledTaskStatus,
)
from nexus_core.experience_consolidation import (
    ExperienceConsolidationEngine,
    ExperienceStatus,
    ExperienceType,
)
from nexus_core.self_improvement import (
    SelfImprovementEngine,
    ImprovementStatus,
    ImprovementType,
)


# --------------------------------------------------------------------------- #
# Distributed Node
# --------------------------------------------------------------------------- #

def test_node_resources_ratios():
    res = NodeResources(
        cpu_cores=4,
        cpu_percent=50.0,
        ram_total_mb=8192.0,
        ram_available_mb=4096.0,
        ram_percent=50.0,
        gpu_available=True,
        gpu_memory_mb=8192.0,
        disk_free_mb=100000.0,
        disk_total_mb=500000.0,
        network_mbps=1000.0,
        updated_at=100.0,
    )
    assert res.ram_free_ratio == 0.5
    assert res.cpu_free_ratio == 0.5


def test_distributed_node_healthy():
    res = NodeResources(
        cpu_cores=2,
        cpu_percent=30.0,
        ram_total_mb=4096.0,
        ram_available_mb=2048.0,
        ram_percent=50.0,
        gpu_available=False,
        gpu_memory_mb=None,
        disk_free_mb=50000.0,
        disk_total_mb=200000.0,
        network_mbps=None,
        updated_at=time.time(),
    )
    node = DistributedNode(
        node_id="n-1",
        hostname="host-1",
        address="127.0.0.1:8000",
        role=NodeRole.WORKER,
        status=NodeStatus.ACTIVE,
        resources=res,
        last_heartbeat=time.time(),
    )
    assert node.is_healthy is True
    assert node.can_accept_task(1000.0) is True
    assert node.can_accept_task(5000.0) is False  # RAM insuficiente


def test_node_not_healthy_without_heartbeat():
    res = NodeResources(
        cpu_cores=2, cpu_percent=10.0, ram_total_mb=4096.0,
        ram_available_mb=2048.0, ram_percent=50.0, gpu_available=False,
        gpu_memory_mb=None, disk_free_mb=50000.0, disk_total_mb=200000.0,
        network_mbps=None, updated_at=100.0,
    )
    node = DistributedNode(
        node_id="n-1", hostname="h", address="a", role=NodeRole.WORKER,
        status=NodeStatus.ACTIVE, resources=res, last_heartbeat=None,
    )
    assert node.is_healthy is False


# --------------------------------------------------------------------------- #
# Node Manager
# --------------------------------------------------------------------------- #

def test_node_manager_register_and_heartbeat():
    mgr = NodeManager(heartbeat_timeout_s=10.0)
    nid = mgr.register_node(
        hostname="host-1", address="127.0.0.1:8000",
        role=NodeRole.WORKER, capabilities=("agent.run",),
    )
    node = mgr.get_node(nid)
    assert node is not None
    assert node.status == NodeStatus.ACTIVE
    assert node.last_heartbeat is not None

    # Atualiza recursos.
    res = NodeResources(
        cpu_cores=2, cpu_percent=20.0, ram_total_mb=4096.0,
        ram_available_mb=2048.0, ram_percent=50.0, gpu_available=False,
        gpu_memory_mb=None, disk_free_mb=50000.0, disk_total_mb=200000.0,
        network_mbps=None, updated_at=100.0,
    )
    assert mgr.heartbeat(nid, resources=res) is True
    assert mgr.get_node(nid).resources is res
    assert mgr.count_active() == 1


def test_node_manager_detect_stale():
    mgr = NodeManager(heartbeat_timeout_s=0.1)
    nid = mgr.register_node(hostname="h", address="a")
    import time
    time.sleep(0.15)
    affected = mgr.detect_stale_nodes()
    assert nid in affected
    assert mgr.get_node(nid).status == NodeStatus.UNREACHABLE


def test_node_manager_cluster_summary():
    mgr = NodeManager()
    mgr.register_node(hostname="h1", address="a1", role=NodeRole.WORKER)
    mgr.register_node(hostname="h2", address="a2", role=NodeRole.CONTROLLER)
    summary = mgr.cluster_summary()
    assert summary["total_nodes"] == 2
    assert summary["active_nodes"] == 2
    assert summary["roles"]["WORKER"] == 1
    assert summary["roles"]["CONTROLLER"] == 1


# --------------------------------------------------------------------------- #
# Distributed Resources
# --------------------------------------------------------------------------- #

def test_distributed_resource_monitor_cluster_view():
    mgr = NodeManager()
    mgr.register_node(hostname="n1", address="a1", role=NodeRole.WORKER)
    mgr.register_node(hostname="n2", address="a2", role=NodeRole.WORKER)
    res1 = NodeResources(
        cpu_cores=4, cpu_percent=20.0, ram_total_mb=8192.0,
        ram_available_mb=4096.0, ram_percent=50.0, gpu_available=False,
        gpu_memory_mb=None, disk_free_mb=50000.0, disk_total_mb=200000.0,
        network_mbps=None, updated_at=100.0,
    )
    res2 = NodeResources(
        cpu_cores=8, cpu_percent=10.0, ram_total_mb=16384.0,
        ram_available_mb=8192.0, ram_percent=50.0, gpu_available=True,
        gpu_memory_mb=8192.0, disk_free_mb=100000.0, disk_total_mb=500000.0,
        network_mbps=None, updated_at=100.0,
    )
    mgr.heartbeat("n1" if mgr.get_node("n1") else list(mgr.list_nodes())[0].node_id, resources=res1)
    # Encontra o nó correto pelo endereço.
    nodes = mgr.list_nodes()
    mgr.heartbeat(nodes[0].node_id, resources=res1)
    mgr.heartbeat(nodes[1].node_id, resources=res2)

    monitor = DistributedResourceMonitor(mgr)
    view = monitor.get_cluster_view()
    assert view.node_count == 2
    assert view.total_cpu_cores == 12
    assert view.nodes_with_gpu == 1
    assert view.total_gpu_memory_mb == 8192.0


def test_resource_monitor_find_best_node():
    mgr = NodeManager()
    mgr.register_node(hostname="n1", address="a1", role=NodeRole.WORKER)
    mgr.register_node(hostname="n2", address="a2", role=NodeRole.WORKER)
    nodes = mgr.list_nodes()
    res1 = NodeResources(
        cpu_cores=2, cpu_percent=10.0, ram_total_mb=4096.0,
        ram_available_mb=3000.0, ram_percent=25.0, gpu_available=False,
        gpu_memory_mb=None, disk_free_mb=50000.0, disk_total_mb=200000.0,
        network_mbps=None, updated_at=100.0,
    )
    res2 = NodeResources(
        cpu_cores=4, cpu_percent=10.0, ram_total_mb=8192.0,
        ram_available_mb=1000.0, ram_percent=88.0, gpu_available=False,
        gpu_memory_mb=None, disk_free_mb=50000.0, disk_total_mb=200000.0,
        network_mbps=None, updated_at=100.0,
    )
    mgr.heartbeat(nodes[0].node_id, resources=res1)
    mgr.heartbeat(nodes[1].node_id, resources=res2)

    monitor = DistributedResourceMonitor(mgr)
    best = monitor.find_best_node_for_task(required_ram_mb=2000.0)
    assert best == nodes[0].node_id  # n1 tem mais RAM livre

    # GPU requerida e nenhum tem GPU -> None.
    assert monitor.find_best_node_for_task(require_gpu=True) is None


# --------------------------------------------------------------------------- #
# Distributed Task Scheduler
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_scheduler_submit_and_assign():
    mgr = NodeManager()
    mgr.register_node(hostname="n1", address="a1", role=NodeRole.WORKER)
    res = NodeResources(
        cpu_cores=2, cpu_percent=10.0, ram_total_mb=4096.0,
        ram_available_mb=3000.0, ram_percent=25.0, gpu_available=False,
        gpu_memory_mb=None, disk_free_mb=50000.0, disk_total_mb=200000.0,
        network_mbps=None, updated_at=100.0,
    )
    mgr.heartbeat(mgr.list_nodes()[0].node_id, resources=res)
    monitor = DistributedResourceMonitor(mgr)
    sched = DistributedTaskScheduler(mgr, monitor)

    tid = sched.submit_task(
        description="tarefa de teste",
        input={"x": 1},
        required_ram_mb=500.0,
        priority=TaskPriority.HIGH,
    )
    assert tid is not None

    assigned = await sched.schedule_next()
    assert assigned == tid
    task = sched.get_task(tid)
    assert task.status == ScheduledTaskStatus.ASSIGNED
    assert task.assigned_node_id is not None


def test_scheduler_list_pending():
    mgr = NodeManager()
    monitor = DistributedResourceMonitor(mgr)
    sched = DistributedTaskScheduler(mgr, monitor)
    sched.submit_task(description="t1", input={})
    sched.submit_task(description="t2", input={})
    pending = sched.list_pending()
    assert len(pending) == 2


@pytest.mark.asyncio
async def test_scheduler_complete_and_fail():
    mgr = NodeManager()
    mgr.register_node(hostname="n1", address="a1", role=NodeRole.WORKER)
    res = NodeResources(
        cpu_cores=2, cpu_percent=10.0, ram_total_mb=4096.0,
        ram_available_mb=3000.0, ram_percent=25.0, gpu_available=False,
        gpu_memory_mb=None, disk_free_mb=50000.0, disk_total_mb=200000.0,
        network_mbps=None, updated_at=100.0,
    )
    mgr.heartbeat(mgr.list_nodes()[0].node_id, resources=res)
    monitor = DistributedResourceMonitor(mgr)
    sched = DistributedTaskScheduler(mgr, monitor)

    tid = sched.submit_task(description="t", input={})
    await sched.schedule_next()
    await sched.mark_started(tid)
    await sched.complete_task(tid, result={"ok": True})
    task = sched.get_task(tid)
    assert task.status == ScheduledTaskStatus.COMPLETED
    assert task.result == {"ok": True}


@pytest.mark.asyncio
async def test_scheduler_failed_task_is_rescheduled_and_assignable():
    mgr = NodeManager()
    mgr.register_node(hostname="n1", address="a1", role=NodeRole.WORKER)
    res = NodeResources(
        cpu_cores=2, cpu_percent=10.0, ram_total_mb=4096.0,
        ram_available_mb=3000.0, ram_percent=25.0, gpu_available=False,
        gpu_memory_mb=None, disk_free_mb=50000.0, disk_total_mb=200000.0,
        network_mbps=None, updated_at=time.time(),
    )
    mgr.heartbeat(mgr.list_nodes()[0].node_id, resources=res)
    monitor = DistributedResourceMonitor(mgr)
    sched = DistributedTaskScheduler(mgr, monitor)

    tid = sched.submit_task(description="t", input={}, max_attempts=5)
    assert await sched.schedule_next() == tid
    await sched.mark_started(tid)

    assert await sched.fail_task(tid, "boom") is True
    failed = sched.get_task(tid)
    assert failed.status == ScheduledTaskStatus.RESCHEDULED
    assert failed.attempts == 1
    assert tid in {task.task_id for task in sched.list_pending()}

    assert await sched.schedule_next() == tid
    assert sched.get_task(tid).status == ScheduledTaskStatus.ASSIGNED


@pytest.mark.asyncio
async def test_scheduler_node_unreachable_preserves_attempts():
    mgr = NodeManager()
    mgr.register_node(hostname="n1", address="a1", role=NodeRole.WORKER)
    res = NodeResources(
        cpu_cores=2, cpu_percent=10.0, ram_total_mb=4096.0,
        ram_available_mb=3000.0, ram_percent=25.0, gpu_available=False,
        gpu_memory_mb=None, disk_free_mb=50000.0, disk_total_mb=200000.0,
        network_mbps=None, updated_at=time.time(),
    )
    node_id = mgr.list_nodes()[0].node_id
    mgr.heartbeat(node_id, resources=res)
    monitor = DistributedResourceMonitor(mgr)
    sched = DistributedTaskScheduler(mgr, monitor)

    tid = sched.submit_task(description="t", input={}, max_attempts=5)
    assert await sched.schedule_next() == tid
    await sched.mark_started(tid)
    assert await sched.fail_task(tid, "boom") is True

    assert await sched.schedule_next() == tid
    await sched.mark_started(tid)
    requeued = sched.handle_node_unreachable(node_id)

    assert requeued == [tid]
    task = sched.get_task(tid)
    assert task.status == ScheduledTaskStatus.PENDING
    assert task.assigned_node_id is None
    assert task.attempts == 2


def test_scheduler_node_unreachable_requeue():
    mgr = NodeManager()
    mgr.register_node(hostname="n1", address="a1", role=NodeRole.WORKER)
    res = NodeResources(
        cpu_cores=2, cpu_percent=10.0, ram_total_mb=4096.0,
        ram_available_mb=3000.0, ram_percent=25.0, gpu_available=False,
        gpu_memory_mb=None, disk_free_mb=50000.0, disk_total_mb=200000.0,
        network_mbps=None, updated_at=time.time(),
    )
    mgr.heartbeat(mgr.list_nodes()[0].node_id, resources=res)
    monitor = DistributedResourceMonitor(mgr)
    sched = DistributedTaskScheduler(mgr, monitor)
    sched.submit_task(description="t", input={})
    asyncio.run(sched.schedule_next())
    requeued = sched.handle_node_unreachable(mgr.list_nodes()[0].node_id)
    assert len(requeued) == 1
    task = sched.get_task(requeued[0])
    assert task.status == ScheduledTaskStatus.PENDING
    assert task.assigned_node_id is None
    assert task.attempts == 1


# --------------------------------------------------------------------------- #
# Experience Consolidation
# --------------------------------------------------------------------------- #

def test_experience_add_and_validate():
    eng = ExperienceConsolidationEngine()
    rid = eng.add_experience(
        experience_type=ExperienceType.TASK_SUCCESS,
        title="tarefa de uptime",
        description="execução bem-sucedida",
        source_node_id="n-1",
        source_task_id="t-1",
        context={"cmd": "uptime"},
        outcome={"exit_code": 0},
        confidence=0.9,
        tags=("uptime", "success"),
    )
    record = eng.get_record(rid)
    assert record is not None
    assert record.status == ExperienceStatus.PROPOSED
    assert record.is_reusable() is False  # ainda não validada

    assert eng.validate_experience(rid, validated_by="operador") is True
    record = eng.get_record(rid)
    assert record.status == ExperienceStatus.VALIDATED
    assert record.is_reusable() is True


def test_experience_reject_low_confidence():
    eng = ExperienceConsolidationEngine(min_confidence_for_validation=0.7)
    rid = eng.add_experience(
        experience_type=ExperienceType.TASK_FAILURE,
        title="falha",
        description="falhou",
        source_node_id="n-1",
        confidence=0.3,
    )
    assert eng.validate_experience(rid, validated_by="op") is False  # confiança baixa


def test_experience_reuse_counter():
    eng = ExperienceConsolidationEngine()
    rid = eng.add_experience(
        experience_type=ExperienceType.TASK_SUCCESS,
        title="t",
        description="d",
        source_node_id="n-1",
        confidence=0.9,
    )
    eng.validate_experience(rid, validated_by="op")
    assert eng.reuse_experience(rid) is True
    assert eng.get_record(rid).reuse_count == 1


def test_experience_detect_patterns():
    eng = ExperienceConsolidationEngine(min_recurrence_for_pattern=2)
    for i in range(3):
        eng.add_experience(
            experience_type=ExperienceType.TASK_SUCCESS,
            title=f"t{i}",
            description="d",
            source_node_id="n-1",
            confidence=0.8,
            tags=("uptime",),
        )
    eng.validate_experience(eng.list_records()[0].record_id, validated_by="op")
    # Valida todos para terem >= 2 validadas com tag.
    for rec in eng.list_records():
        eng.validate_experience(rec.record_id, validated_by="op")
    proposals = eng.detect_patterns()
    assert len(proposals) >= 1


# --------------------------------------------------------------------------- #
# Self Improvement
# --------------------------------------------------------------------------- #

def test_self_improvement_propose_and_approve():
    eng = SelfImprovementEngine()
    pid = eng.propose_improvement(
        improvement_type=ImprovementType.RESOURCE_TUNING,
        title="aumentar limite de RAM",
        description="aumentar RAM para tarefas pesadas",
        rationale="experiência validada mostrou necessidade",
        proposed_changes={"ram_limit_mb": 8192},
        affected_components=("scheduler",),
        risk_level="low",
    )
    prop = eng.get_proposal(pid)
    assert prop is not None
    assert prop.status == ImprovementStatus.PROPOSED
    assert prop.requires_human_approval is False  # baixo risco, não segurança

    assert eng.submit_for_review(pid, reviewer="op") is True
    assert eng.approve(pid, approver="op") is True
    prop = eng.get_proposal(pid)
    assert prop.status == ImprovementStatus.APPROVED

    assert eng.mark_applied(pid) is True
    assert eng.get_proposal(pid).status == ImprovementStatus.APPLIED


def test_self_improvement_security_policy_requires_approval():
    eng = SelfImprovementEngine()
    assert eng.security_policies_frozen() is True
    pid = eng.propose_improvement(
        improvement_type=ImprovementType.SECURITY_POLICY,
        title="mudar política",
        description="alterar política de segurança",
        rationale="motivo",
        proposed_changes={"allow_shell": True},
        affected_components=("execution_gate",),
        risk_level="critical",
    )
    prop = eng.get_proposal(pid)
    assert prop.requires_human_approval is True

    # Submete para revisão antes de aprovar.
    assert eng.submit_for_review(pid, reviewer="op") is True
    # Sem aprovador -> não aprova.
    assert eng.approve(pid, approver=None) is False
    # Com aprovador -> aprova.
    assert eng.approve(pid, approver="admin") is True


def test_self_improvement_rollback():
    eng = SelfImprovementEngine()
    pid = eng.propose_improvement(
        improvement_type=ImprovementType.RESOURCE_TUNING,
        title="t",
        description="d",
        rationale="r",
        proposed_changes={"x": 1},
        affected_components=("c",),
        risk_level="low",
    )
    eng.submit_for_review(pid, reviewer="op")
    eng.approve(pid, approver="op", rollback_plan={"undo": "set_x_to_0"})
    eng.mark_applied(pid)
    assert eng.rollback(pid, rolled_back_by="op") is True
    assert eng.get_proposal(pid).status == ImprovementStatus.ROLLED_BACK


def test_self_improvement_stats():
    eng = SelfImprovementEngine()
    eng.propose_improvement(
        improvement_type=ImprovementType.RESOURCE_TUNING,
        title="t1", description="d", rationale="r",
        proposed_changes={}, affected_components=(),
    )
    stats = eng.stats()
    assert stats["total_proposals"] == 1
    assert stats["security_policies_frozen"] is True


__all__ = []
