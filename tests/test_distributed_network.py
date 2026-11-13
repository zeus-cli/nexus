"""Testes de rede distribuída — WebSocket entre nós."""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import websockets

from nexus_core.distributed_network import (
    ConnectionError as NetworkConnectionError,
    DistributedNetwork,
    DistributedNetworkClient,
    DistributedNetworkServer,
    RemoteNodeInfo,
)
from nexus_core.distributed_node import NodeRole, NodeStatus
from nexus_core.events import Event, Severity


# --------------------------------------------------------------------------- #
# Distributed Network Server
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_server_start_stop():
    server = DistributedNetworkServer(host="127.0.0.1", port=0)
    await server.start()
    assert server.is_running
    actual_port = server._server.sockets[0].getsockname()[1]
    assert actual_port > 0
    await server.stop()
    assert not server.is_running


@pytest.mark.asyncio
async def test_server_receives_hello():
    server = DistributedNetworkServer(host="127.0.0.1", port=0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]

    received = []
    server.event_handler = lambda e: received.append(e)

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(
            json.dumps(
                {"type": "hello", "node_id": "node-1", "timestamp": 1.0}
            )
        )
        response = await ws.recv()
        data = json.loads(response)
        assert data["type"] == "hello_ack"
        assert data["status"] == "connected"

    await server.stop()


@pytest.mark.asyncio
async def test_server_receives_event():
    server = DistributedNetworkServer(host="127.0.0.1", port=0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]

    received = []
    server.event_handler = lambda e: received.append(e)

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(
            json.dumps(
                {"type": "hello", "node_id": "node-1"}
            )
        )
        await ws.recv()  # hello_ack

        created_at = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
        event = Event(
            kind="test.remote",
            source="remote",
            payload={"data": "hello"},
            created_at=created_at,
            dedup=False,
        )
        await ws.send(
            json.dumps(
                {
                    "type": "event",
                    "event_id": event.event_id,
                    "kind": event.kind,
                    "source": event.source,
                    "severity": event.severity.value,
                    "payload": event.payload,
                    "created_at": event.created_at.isoformat(),
                    "dedup": event.dedup,
                    "timestamp": 1.0,
                }
            )
        )
        response = await ws.recv()
        data = json.loads(response)
        assert data["type"] == "event_ack"
        assert data["event_id"] == event.event_id

        assert len(received) == 1
        assert received[0].kind == "test.remote"
        assert received[0].created_at == created_at
        assert received[0].dedup is False

    await server.stop()


# --------------------------------------------------------------------------- #
# Distributed Network Client
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_client_connect_and_broadcast():
    server = DistributedNetworkServer(host="127.0.0.1", port=0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]

    received_server = []
    server.event_handler = lambda e: received_server.append(e)

    client = DistributedNetworkClient(node_id="client-1")
    await client.start()
    await client.connect(f"ws://127.0.0.1:{port}")

    # Aguarda conexão ser estabelecida
    await asyncio.sleep(0.5)

    # Envia evento
    event = Event(kind="client.event", source="client-1", payload={"msg": "hi"})
    sent = await client.broadcast_event(event)
    assert sent >= 1

    await asyncio.sleep(0.5)
    assert len(received_server) >= 1
    assert received_server[0].kind == "client.event"

    await client.stop()
    await server.stop()


@pytest.mark.asyncio
async def test_client_connect_cleans_up_when_hello_fails(monkeypatch):
    class RejectingWebSocket:
        def __init__(self) -> None:
            self.closed = False

        async def send(self, payload: str) -> None:
            raise OSError("send failed")

        async def close(self) -> None:
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)

    sockets: list[RejectingWebSocket] = []

    async def fake_connect(address: str) -> RejectingWebSocket:
        ws = RejectingWebSocket()
        sockets.append(ws)
        return ws

    monkeypatch.setattr(
        "nexus_core.distributed_network.websockets.connect", fake_connect
    )
    client = DistributedNetworkClient(node_id="client-1")

    with pytest.raises(NetworkConnectionError):
        await client.connect("ws://127.0.0.1:1")

    assert client.connected_count == 0
    assert client._tasks == {}
    assert sockets[0].closed is True

    # Uma nova tentativa deve tentar conectar de novo, não retornar cedo.
    with pytest.raises(NetworkConnectionError):
        await client.connect("ws://127.0.0.1:1")
    assert len(sockets) == 2
    await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Distributed Network (combined)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_distributed_network_start_stop():
    network = DistributedNetwork(node_id="net-1", host="127.0.0.1", port=0)
    await network.start()
    assert network.is_running
    assert network.server.is_running
    await network.stop()
    assert not network.is_running


@pytest.mark.asyncio
async def test_distributed_network_connect_and_broadcast():
    # Dois nós se conectando
    net1 = DistributedNetwork(node_id="node-1", host="127.0.0.1", port=0)
    net2 = DistributedNetwork(node_id="node-2", host="127.0.0.1", port=0)

    await net1.start()
    await net2.start()

    port1 = net1.server._server.sockets[0].getsockname()[1]

    await net2.connect_to_node(f"ws://127.0.0.1:{port1}", role=NodeRole.WORKER)
    # Aguarda conexão ser estabelecida
    await asyncio.sleep(1.0)

    # Verifica que net2 tem pelo menos uma conexão ativa
    assert net2.connected_nodes_count() >= 1

    await net1.stop()
    await net2.stop()


# --------------------------------------------------------------------------- #
# Remote Node Info
# --------------------------------------------------------------------------- #

def test_remote_node_info():
    node = RemoteNodeInfo(
        node_id="n-1",
        address="127.0.0.1:8766",
        role=NodeRole.WORKER,
        connected=True,
    )
    assert node.node_id == "n-1"
    assert node.role == NodeRole.WORKER
    assert node.connected is True


__all__ = []
