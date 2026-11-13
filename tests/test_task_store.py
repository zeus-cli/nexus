"""Testes de persistência segura de tarefas suspensas."""

from __future__ import annotations

from nexus_core.task_store import TaskStore


def test_empty_key_is_treated_as_unencrypted(tmp_path):
    store = TaskStore(tmp_path / "tasks", encryption_key="")
    store.save_task("task1", {"state": "waiting"})
    assert store.load_task("task1") == {"state": "waiting"}


def test_saved_task_is_private_and_atomic(tmp_path):
    store = TaskStore(tmp_path / "tasks", encryption_key="segredo")
    store.save_task("task2", {"state": "waiting"})

    path = tmp_path / "tasks" / "task2.json"
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "tasks" / "task2.tmp").exists()
    assert store.load_task("task2") == {"state": "waiting"}


def test_checksum_failure_discards_corrupted_task(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    store.save_task("task3", {"state": "waiting"})

    path = tmp_path / "tasks" / "task3.json"
    path.write_text(path.read_text(encoding="utf-8").replace("waiting", "tampered"),
                    encoding="utf-8")
    assert store.load_task("task3") is None
    assert not path.exists()


def test_crypto_backend_failure_is_treated_as_corruption(tmp_path):
    """Falha do backend de criptografia não pode escapar de ``load_task``."""
    store = TaskStore(tmp_path / "tasks", encryption_key="segredo")
    store.save_task("task4", {"state": "waiting"})

    class BrokenFernet:
        def decrypt(self, token: bytes) -> bytes:
            raise RuntimeError("backend de criptografia indisponível")

    store._crypto._fernet = BrokenFernet()
    path = tmp_path / "tasks" / "task4.json"

    assert store.load_task("task4") is None
    assert not path.exists()


__all__: list[str] = []
