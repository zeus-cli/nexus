
"""Persistence layer for suspended cognitive tasks with security hardening."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_TASKS_DIR = Path("data") / "tasks"


class _TaskCrypto:
    """Criptografia opcional para estados de tarefa suspensa.

    Quando nenhuma chave é fornecida, os dados são gravados em texto claro
    (compatível com versões anteriores). Quando uma chave é fornecida, usa
    Fernet (cryptography) se disponível, caso contrário cifra XOR simples
    para proteger contra leitura casual do disco.
    """

    def __init__(self, key: Optional[str] = None) -> None:
        self._key = key
        self._fernet = None
        if key is not None:
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
            return self._fernet.decrypt(data.encode()).decode()
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
        encryption_key: Optional[str] = None,
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

        with open(temp_path, "w") as f:
            f.write(payload)

        os.chmod(temp_path, 0o600)
        temp_path.replace(path)

    def load_task(self, task_id: str) -> dict[str, Any] | None:
        """Loads task state and verifies integrity checksum."""
        if not task_id.isalnum():
            return None

        path = self._store_dir / f"{task_id}.json"
        if not path.exists():
            return None

        try:
            with open(path, "r") as f:
                raw = f.read()

            if self._crypto._key is not None:
                raw = self._crypto.decrypt(raw)

            wrapped = json.loads(raw)
            encrypted = wrapped.get("encrypted", False)

            if encrypted and self._crypto._key is None:
                raise ValueError("Task store encrypted but no decryption key provided")

            data_str = json.dumps(wrapped["data"], indent=2, sort_keys=True)
            if self._calculate_checksum(data_str) != wrapped["checksum"]:
                raise ValueError("Task store integrity check failed: checksum mismatch")

            return wrapped["data"]
        except (json.JSONDecodeError, KeyError, ValueError):
            path.unlink(missing_ok=True)
            return None

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
            if now - path.stat().st_mtime > (retention_days * 86400):
                path.unlink()
                count += 1
        return count
