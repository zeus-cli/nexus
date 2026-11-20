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


def test_checksum_failure_quarantines_corrupted_task(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    store.save_task("task3", {"state": "waiting"})

    path = tmp_path / "tasks" / "task3.json"
    path.write_text(path.read_text(encoding="utf-8").replace("waiting", "tampered"),
                    encoding="utf-8")
    assert store.load_task("task3") is None
    # O estado corrompido é preservado para diagnóstico, fora do caminho ativo.
    assert not path.exists()
    corrupt = list((tmp_path / "tasks").glob("task3.json.corrupt-*"))
    assert len(corrupt) == 1
    assert "tampered" in corrupt[0].read_text(encoding="utf-8")


def test_crypto_backend_failure_preserves_task_file(tmp_path):
    """Falha do backend de criptografia não pode destruir o estado suspenso."""
    store = TaskStore(tmp_path / "tasks", encryption_key="segredo")
    store.save_task("task4", {"state": "waiting"})

    class BrokenFernet:
        def decrypt(self, token: bytes) -> bytes:
            raise RuntimeError("backend de criptografia indisponível")

    store._crypto._fernet = BrokenFernet()
    path = tmp_path / "tasks" / "task4.json"

    assert store.load_task("task4") is None
    assert path.exists()


def test_wrong_key_preserves_task_for_later_recovery(tmp_path):
    path = tmp_path / "tasks"
    store = TaskStore(path, encryption_key="chave-correta")
    store.save_task("task5", {"state": "waiting"})

    wrong = TaskStore(path, encryption_key="chave-errada")
    assert wrong.load_task("task5") is None
    assert (path / "task5.json").exists()

    assert store.load_task("task5") == {"state": "waiting"}


def test_missing_key_preserves_encrypted_task_file(tmp_path):
    path = tmp_path / "tasks"
    encrypted_store = TaskStore(path, encryption_key="chave-correta")
    encrypted_store.save_task("task7", {"state": "waiting"})
    task_file = path / "task7.json"

    assert TaskStore(path).load_task("task7") is None
    assert task_file.exists()
    assert encrypted_store.load_task("task7") == {"state": "waiting"}


def test_cleanup_ignores_files_that_disappear_during_scan(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks")
    store.save_task("task6", {"state": "waiting"})
    removed = tmp_path / "tasks" / "task6.json"

    real_stat = type(removed).stat

    def vanishing_stat(self, *args, **kwargs):
        if self == removed:
            removed.unlink()
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(removed), "stat", vanishing_stat)
    assert store.cleanup(retention_days=0) == 0


__all__: list[str] = []
