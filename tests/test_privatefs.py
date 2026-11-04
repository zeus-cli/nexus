from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path

from nexus_core.config import LoggingConfig
from nexus_core.logging_setup import configure_logging
from nexus_core.memory.store import Database
from nexus_core.privatefs import (
    ensure_private_file,
    secure_runtime_data_directory,
)


class PrivateFilesystemTest(unittest.TestCase):
    def test_runtime_tree_permissions_are_restricted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            child = root / "databases"
            child.mkdir(parents=True, mode=0o755)
            os.chmod(root, 0o775)
            os.chmod(child, 0o775)
            root_file = root / "state"
            root_file.write_text("metadata", encoding="utf-8")
            os.chmod(root_file, 0o664)

            secure_runtime_data_directory(root)

            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(child.stat().st_mode & 0o777, 0o700)
            self.assertEqual(root_file.stat().st_mode & 0o777, 0o600)

    def test_private_file_creation_uses_owner_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.db"
            ensure_private_file(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_private_file_refuses_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_text("leave unchanged", encoding="utf-8")
            os.chmod(target, 0o644)
            link = root / "link"
            link.symlink_to(target)

            with self.assertRaises(PermissionError):
                ensure_private_file(link)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_database_and_wal_files_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "data" / "events.db"
            database = Database(database_path)
            try:
                database.connect()
                database.execute("CREATE TABLE sample (value TEXT)")
                database.execute("INSERT INTO sample(value) VALUES (?)", ("private",))
                self.assertEqual(database_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(database_path.parent.stat().st_mode & 0o777, 0o700)
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{database_path}{suffix}")
                    if sidecar.exists():
                        self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
            finally:
                database.close()

    def test_rotated_logs_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "logs" / "nexus.log"
            logger = configure_logging(
                LoggingConfig(max_bytes=1, backup_count=1), log_path, console=False
            )
            try:
                for index in range(3):
                    logger.info("private log entry %s", index)
                for handler in logger.handlers:
                    handler.flush()
                self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(Path(f"{log_path}.1").stat().st_mode & 0o777, 0o600)
            finally:
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()
