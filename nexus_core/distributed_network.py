"""Distributed Network — comunicação remota entre nós do cluster NEXUS.

Responsabilidade:
* Servidor WebSocket que aceita conexões de outros nós do cluster.
* Cliente WebSocket que se conecta a nós remotos.
* Propagação de eventos entre nós via WebSocket.
* Heartbeat remoto para detecção de falha de nós remotos.
* Serialização compatível com o protocolo de eventos do NEXUS.

Princípios:
* Comunicação via WebSocket (compatível com ambiente local-first).
* Reconexão automática em caso de falha de conexão.
* Mensagens são JSON por linha, compatíveis com o protocolo existente.
* Cada nó mantém conexões ativas para outros nós conhecidos.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import websockets

from .events import Event, parse_timestamp
from .distributed_node import DistributedNode, NodeRole, NodeStatus


@dataclass(frozen=True, slots=True)
class RemoteNodeInfo:
    """Informações de um nó remoto conhecido."""

    node_id: str
    address: str  # ws://host:port
    role: NodeRole
    connected: bool = False
    last_heartbeat: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class NetworkError(Exception):
    """Erro de comunicação de rede."""


class ConnectionError(NetworkError):
    """Falha ao conectar a um nó remoto."""


class DistributedNetworkServer:
    """Servidor WebSocket que aceita conexões de outros nós."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8766,
        event_handler: Callable[[Event], Any] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.event_handler = event_handler
        self._server: asyncio.AbstractServer | None = None
        self._clients: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._running = False

    async def start(self) -> None:
        """Inicia o servidor WebSocket."""
        self._server = await websockets.serve(self._handle_client, self.host, self.port)
        self._running = True

    async def stop(self) -> None:
        """Para o servidor WebSocket."""
        self._running = False
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        async with self._lock:
            for client in list(self._clients.values()):
                try:
                    await client.close()
                except Exception:
                    pass
            self._clients.clear()

    async def _handle_client(self, websocket: Any) -> None:
        """Trata uma conexão de cliente WebSocket."""
        node_id: str | None = None
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    await self._send_json(websocket, {"type": "error", "message": "invalid json"})
                    continue

                msg_type = data.get("type")
                if msg_type == "hello":
                    node_id = data.get("node_id", uuid.uuid4().hex)
                    async with self._lock:
                        self._clients[node_id] = websocket
                    await self._send_json(
                        websocket,
                        {
                            "type": "hello_ack",
                            "node_id": node_id,
                            "status": "connected",
                        },
                    )
                elif msg_type == "event":
                    event = self._event_from_message(data)
                    if self.event_handler is not None:
                        if asyncio.iscoroutinefunction(self.event_handler):
                            await self.event_handler(event)
                        else:
                            self.event_handler(event)
                    await self._send_json(
                        websocket,
                        {
                            "type": "event_ack",
                            "event_id": event.event_id,
                            "status": "received",
                        },
                    )
                elif msg_type == "heartbeat":
                    node_id = data.get("node_id")
                    await self._send_json(
                        websocket,
                        {
                            "type": "heartbeat_ack",
                            "node_id": node_id,
                            "timestamp": time.time(),
                        },
                    )
                elif msg_type == "goodbye":
                    break
                else:
                    await self._send_json(websocket, {"type": "error", "message": f"unknown type: {msg_type}"})
        except Exception:
            pass
        finally:
            if node_id is not None:
                async with self._lock:
                    self._clients.pop(node_id, None)

    async def _send_json(self, websocket: Any, data: Mapping[str, Any]) -> None:
        await websocket.send(json.dumps(data, separators=(",", ":")))

    @staticmethod
    def _event_from_message(data: Mapping[str, Any]) -> Event:
        values: dict[str, Any] = {
            "kind": data.get("kind", "unknown"),
            "source": data.get("source", "remote"),
            "payload": data.get("payload", {}),
            "severity": data.get("severity", "info"),
            "event_id": data.get("event_id", uuid.uuid4().hex),
            "correlation_id": data.get("correlation_id"),
            "dedup": bool(data.get("dedup", True)),
        }
        if data.get("created_at"):
            values["created_at"] = parse_timestamp(
                data["created_at"], "event.created_at"
            )
        return Event(**values)

    @property
    def connected_clients(self) -> int:
        return len(self._clients)

    @property
    def is_running(self) -> bool:
        return self._running


class DistributedNetworkClient:
    """Cliente WebSocket que se conecta a nós remotos e propaga eventos."""

    def __init__(
        self,
        node_id: str,
        event_handler: Callable[[Event], Any] | None = None,
        reconnect_interval_s: float = 5.0,
        heartbeat_interval_s: float = 30.0,
    ) -> None:
        self.node_id = node_id
        self.event_handler = event_handler
        self.reconnect_interval_s = reconnect_interval_s
        self.heartbeat_interval_s = heartbeat_interval_s
        self._connections: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        for task in list(self._tasks.values()):
            task.cancel()
        async with self._lock:
            for conn in list(self._connections.values()):
                try:
                    await conn.close()
                except Exception:
                    pass
            self._connections.clear()

    async def connect(self, address: str) -> None:
        """Conecta a um nó remoto."""
        async with self._lock:
            if address in self._connections:
                return  # já conectado
            ws = None
            task = None
            try:
                ws = await websockets.connect(address)
                task = asyncio.create_task(self._receive_loop(address, ws))
                self._connections[address] = ws
                self._tasks[address] = task
                # Envia hello
                await self._send_json(
                    ws,
                    {
                        "type": "hello",
                        "node_id": self.node_id,
                        "timestamp": time.time(),
                    },
                )
            except BaseException as exc:
                # Uma falha no handshake não pode deixar a conexão (nem a
                # tarefa de recepção) registrada: o próximo connect retornaria
                # cedo e o nó ficaria permanentemente "conectado" sem sê-lo.
                if task is not None:
                    task.cancel()
                self._connections.pop(address, None)
                self._tasks.pop(address, None)
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                if isinstance(exc, Exception):
                    raise ConnectionError(
                        f"falha ao conectar a {address}: {exc}"
                    ) from exc
                raise

    async def disconnect(self, address: str) -> None:
        async with self._lock:
            if address in self._tasks:
                self._tasks[address].cancel()
                del self._tasks[address]
            if address in self._connections:
                try:
                    await self._connections[address].close()
                except Exception:
                    pass
                del self._connections[address]

    async def broadcast_event(self, event: Event) -> int:
        """Envia um evento para todos os nós conectados."""
        message = {
            "type": "event",
            "event_id": event.event_id,
            "kind": event.kind,
            "source": event.source,
            "severity": event.severity.value,
            "payload": event.payload,
            "correlation_id": event.correlation_id,
            "created_at": event.created_at.isoformat(),
            "dedup": event.dedup,
            "timestamp": time.time(),
            "sender_node_id": self.node_id,
        }
        sent = 0
        async with self._lock:
            connections = list(self._connections.values())
        for ws in connections:
            try:
                await ws.send(json.dumps(message, separators=(",", ":")))
                sent += 1
            except Exception:
                pass  # falha silenciosa; o receive_loop detectará desconexão
        return sent

    async def send_heartbeat(self, address: str) -> None:
        async with self._lock:
            ws = self._connections.get(address)
        if ws is None:
            return
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "heartbeat",
                        "node_id": self.node_id,
                        "timestamp": time.time(),
                    },
                    separators=(",", ":"),
                )
            )
        except Exception:
            pass

    async def _receive_loop(self, address: str, ws: Any) -> None:
        try:
            async for message in ws:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                msg_type = data.get("type")
                if msg_type == "hello_ack":
                    continue
                elif msg_type == "event_ack":
                    continue
                elif msg_type == "heartbeat_ack":
                    continue
                elif msg_type == "event":
                    event = self._event_from_message(data)
                    if self.event_handler is not None:
                        if asyncio.iscoroutinefunction(self.event_handler):
                            await self.event_handler(event)
                        else:
                            self.event_handler(event)
                elif msg_type == "error":
                    pass  # log
                elif msg_type == "goodbye":
                    break
        except Exception:
            pass
        finally:
            async with self._lock:
                # Só remove a conexão/tarefa que ainda pertence a este loop:
                # uma reconexão pode ter criado um novo par para o endereço.
                if self._connections.get(address) is ws:
                    self._connections.pop(address, None)
                if self._tasks.get(address) is asyncio.current_task():
                    self._tasks.pop(address, None)

    async def _send_json(self, websocket: Any, data: Mapping[str, Any]) -> None:
        await websocket.send(json.dumps(data, separators=(",", ":")))

    @staticmethod
    def _event_from_message(data: Mapping[str, Any]) -> Event:
        values: dict[str, Any] = {
            "kind": data.get("kind", "unknown"),
            "source": data.get("source", "remote"),
            "payload": data.get("payload", {}),
            "severity": data.get("severity", "info"),
            "event_id": data.get("event_id", uuid.uuid4().hex),
            "correlation_id": data.get("correlation_id"),
            "dedup": bool(data.get("dedup", True)),
        }
        if data.get("created_at"):
            values["created_at"] = parse_timestamp(
                data["created_at"], "event.created_at"
            )
        return Event(**values)

    @property
    def connected_count(self) -> int:
        return len(self._connections)


class DistributedNetwork:
    """Combina servidor e cliente de rede distribuída."""

    def __init__(
        self,
        node_id: str,
        host: str = "0.0.0.0",
        port: int = 8766,
        event_handler: Callable[[Event], Any] | None = None,
    ) -> None:
        self.node_id = node_id
        self.server = DistributedNetworkServer(
            host=host, port=port, event_handler=event_handler
        )
        self.client = DistributedNetworkClient(
            node_id=node_id, event_handler=event_handler
        )
        self._remote_nodes: dict[str, RemoteNodeInfo] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self.server.start()
        await self.client.start()

    async def stop(self) -> None:
        await self.client.stop()
        await self.server.stop()

    async def connect_to_node(self, address: str, role: NodeRole = NodeRole.WORKER) -> None:
        await self.client.connect(address)
        async with self._lock:
            self._remote_nodes[address] = RemoteNodeInfo(
                node_id=self.node_id,
                address=address,
                role=role,
                connected=True,
                last_heartbeat=time.time(),
            )

    async def broadcast_event(self, event: Event) -> int:
        return await self.client.broadcast_event(event)

    def connected_nodes_count(self) -> int:
        return self.client.connected_count

    @property
    def is_running(self) -> bool:
        return self.server.is_running


__all__ = [
    "DistributedNetwork",
    "DistributedNetworkServer",
    "DistributedNetworkClient",
    "RemoteNodeInfo",
    "NetworkError",
    "ConnectionError",
]
