
"""Persistence layer for suspended cognitive tasks with security hardening."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_TASKS_DIR = Path("data") / "tasks"


def _looks_like_encrypted_payload(raw: str) -> bool:
    """Identifies opaque base64 ciphertext when the decryption key is absent."""
    candidate = raw.strip()
    if not candidate:
        return False
    try:
        decoded = base64.b64decode(candidate, altchars=b"-_", validate=True)
    except (ValueError, base64.binascii.Error):
        return False
    return len(decoded) >= 16


class _TaskCrypto:
    """Criptografia opcional para estados de tarefa suspensa.

    Quando nenhuma chave é fornecida, os dados são gravados em texto claro
    (compatível com versões anteriores). Quando uma chave é fornecida, usa
    Fernet (cryptography) se disponível, caso contrário cifra XOR simples
    para proteger contra leitura casual do disco.
    """

    def __init__(self, key: str | None = None) -> None:
        # Uma chave vazia não pode operar o XOR (divisão por zero); trate-a
        # como ausência de criptografia.
        self._key = key or None
        self._fernet = None
        if self._key is not None:
            key = self._key
            try:
                from cryptography.fernet import Fernet

                derived = hashlib.sha256(key.encode()).digest()
                self._fernet = Fernet(
                    __import__("base64").urlsafe_b64encode(derived)
                )
            except ImportError:
                self._fernet = None

    def encrypt(self, data: str) -> str:
        if self._fernet is not None:
            return self._fernet.encrypt(data.encode()).decode()
        if self._key is not None:
            return self._xor(data)
        return data

    def decrypt(self, data: str) -> str:
        if self._fernet is not None:
            try:
                return self._fernet.decrypt(data.encode()).decode()
            except Exception as exc:  # falha de cripto vira estado inválido
                raise ValueError("falha ao decifrar estado da tarefa") from exc
        if self._key is not None:
            return self._xor(data, decrypt=True)
        return data

    def _xor(self, data: str, *, decrypt: bool = False) -> str:
        import base64

        key_bytes = self._key.encode()
        if decrypt:
            data_bytes = base64.urlsafe_b64decode(data.encode())
        else:
            data_bytes = data.encode()
        out = bytearray(len(data_bytes))
        for i, b in enumerate(data_bytes):
            out[i] = b ^ key_bytes[i % len(key_bytes)]
        if decrypt:
            return out.decode()
        return base64.urlsafe_b64encode(out).decode()


class TaskStore:
    """Stores and retrieves state of suspended tasks with integrity and permission checks."""

    def __init__(
        self,
        store_dir: str | Path = DEFAULT_TASKS_DIR,
        *,
        encryption_key: str | None = None,
    ):
        self._store_dir = Path(store_dir)
        self._crypto = _TaskCrypto(encryption_key)
        self._ensure_secure_dir()

    def _ensure_secure_dir(self) -> None:
        """Ensures the storage directory exists with restricted permissions (0700)."""
        if not self._store_dir.exists():
            self._store_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            os.chmod(self._store_dir, 0o700)

    def _calculate_checksum(self, data: str) -> str:
        """Calculates SHA-256 checksum of the data string."""
        return hashlib.sha256(data.encode()).hexdigest()

    def save_task(self, task_id: str, state: dict[str, Any]) -> None:
        """Saves task state atomically with checksum and restricted permissions."""
        if not task_id.isalnum():
            raise ValueError("Invalid task_id: must be alphanumeric")

        path = self._store_dir / f"{task_id}.json"
        temp_path = path.with_suffix(".tmp")

        data_str = json.dumps(state, indent=2, sort_keys=True)
        wrapped = {
            "checksum": self._calculate_checksum(data_str),
            "data": state,
            "encrypted": self._crypto._key is not None,
        }

        payload = json.dumps(wrapped, indent=2)
        if self._crypto._key is not None:
            payload = self._crypto.encrypt(payload)

        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(temp_path, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            # ``os.fdopen`` fecha o descritor; este chmod cobre um eventual
            # arquivo temporário pré-existente com permissões mais amplas.
            os.chmod(temp_path, 0o600)
        temp_path.replace(path)

    def _quarantine(self, path: Path) -> None:
        """Preserva um arquivo ilegível em vez de apagá-lo silenciosamente."""
        try:
            target = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
            path.replace(target)
            os.chmod(target, 0o600)
        except OSError:
            # Corrida com outro processo ou disco somente leitura: o importante
            # é nunca transformar uma falha de leitura em perda definitiva.
            pass

    def load_task(self, task_id: str) -> dict[str, Any] | None:
        """Loads task state and verifies integrity checksum."""
        if not task_id.isalnum():
            return None

        path = self._store_dir / f"{task_id}.json"
        if not path.exists():
            return None

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None

        if self._crypto._key is not None:
            try:
                raw = self._crypto.decrypt(raw)
            except (ValueError, UnicodeDecodeError):
                # Chave errada ou backend indisponível não é corrupção do
                # arquivo: preserve-o para quando a chave correta voltar.
                return None

        # O fallback XOR não é autenticado: sem uma chave correta não há como
        # distinguir "chave errada" de "arquivo corrompido". Nesse caso,
        # preserve o arquivo em vez de arriscar apagar estado recuperável.
        preserve_on_failure = self._crypto._key is not None and self._crypto._fernet is None

        try:
            wrapped = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Fernet and the legacy XOR fallback both produce opaque base64.
            # Without a key, invalid JSON may be recoverable ciphertext.
            if not preserve_on_failure and not (
                self._crypto._key is None and _looks_like_encrypted_payload(raw)
            ):
                self._quarantine(path)
            return None
        if not isinstance(wrapped, dict):
            if not preserve_on_failure:
                self._quarantine(path)
            return None

        if wrapped.get("encrypted", False) and self._crypto._key is None:
            # Configuração sem chave: o conteúdo pode estar íntegro, apenas
            # ilegível neste processo. Não destrua o estado.
            return None

        try:
            data = wrapped["data"]
            checksum = wrapped["checksum"]
            data_str = json.dumps(data, indent=2, sort_keys=True)
        except (KeyError, TypeError, ValueError):
            if not preserve_on_failure:
                self._quarantine(path)
            return None
        if not isinstance(data, dict) or not isinstance(checksum, str):
            if not preserve_on_failure:
                self._quarantine(path)
            return None
        if self._calculate_checksum(data_str) != checksum:
            if not preserve_on_failure:
                self._quarantine(path)
            return None
        return data

    def delete_task(self, task_id: str) -> None:
        if not task_id.isalnum():
            return
        path = self._store_dir / f"{task_id}.json"
        path.unlink(missing_ok=True)

    def list_suspended(self) -> list[str]:
        """Lists all currently suspended tasks."""
        return [p.stem for p in self._store_dir.glob("*.json")]

    def cleanup(self, retention_days: int = 7) -> int:
        """Purges stale task states based on file age."""
        now = time.time()
        count = 0
        for path in self._store_dir.glob("*.json"):
            try:
                stale = now - path.stat().st_mtime > (retention_days * 86400)
            except OSError:
                continue
            if stale:
                try:
                    path.unlink()
                except OSError:
                    continue
                count += 1
        return count
