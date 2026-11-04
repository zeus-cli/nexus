
"""Persistence layer for suspended cognitive tasks with security hardening."""

import json
import os
import hashlib
import shutil
from pathlib import Path
from typing import Any, Optional

class TaskStore:
    """Stores and retrieves state of suspended tasks with integrity and permission checks."""

    def __init__(self, store_dir: str = "/home/nexus/Documentos/nexus/data/tasks"):
        self._store_dir = Path(store_dir)
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
        
        data_str = json.dumps(state, indent=2)
        wrapped = {
            "checksum": self._calculate_checksum(data_str),
            "data": state
        }
        
        with open(temp_path, "w") as f:
            json.dump(wrapped, f, indent=2)
        
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
                wrapped = json.load(f)
            
            data_str = json.dumps(wrapped["data"], indent=2)
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
        import time
        now = time.time()
        count = 0
        for path in self._store_dir.glob("*.json"):
            if now - path.stat().st_mtime > (retention_days * 86400):
                path.unlink()
                count += 1
        return count
