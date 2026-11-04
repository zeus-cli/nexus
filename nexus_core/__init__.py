"""NEXUS — camada cognitiva local-first.

Fase 1 (Fundação): configuração central, logging estruturado, banco SQLite com
migrações versionadas, EventStore persistente e EventBus desacoplado.
Fase 2 (Backend): API local HTTP/WebSocket com autenticação por token e
permissões derivadas do nível de autonomia.
"""

__version__ = "0.2.0"
__all__ = ["__version__"]
