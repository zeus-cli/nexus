from __future__ import annotations

import asyncio
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from nexus_core.app import bootstrap, health, serve


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        config_dir = self.root / "config"
        config_dir.mkdir(parents=True)
        self.config_path = config_dir / "nexus.toml"
        self.config_path.write_text(
            textwrap.dedent(
                """
                [paths]
                data_dir = "data"

                [logging]
                level = "DEBUG"
                json = true

                [events]
                dedup_window_hours = 0

                [safety]
                autonomy_level = 1
                """
            ),
            encoding="utf-8",
        )
        self.runtime = bootstrap(self.config_path, console_logs=False)
        self.addCleanup(self.runtime.close)

    def test_bootstrap_creates_database_and_log(self) -> None:
        self.assertTrue(self.runtime.config.paths.database.is_file())
        self.assertTrue(self.runtime.config.paths.log_file.is_file())
        self.assertEqual(self.runtime.database.schema_version(), 3)

    def test_health_reports_operational_state(self) -> None:
        report = health(self.runtime)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["autonomy"], {"level": 1, "name": "assistance"})
        self.assertEqual(report["database"]["integrity"], "ok")
        self.assertTrue(all(check["ok"] for check in report["checks"]))

    def test_serve_records_full_lifecycle(self) -> None:
        asyncio.run(serve(self.runtime, interval=0.01, iterations=2))

        kinds = [event.kind for event in self.runtime.store.query(oldest_first=True)]
        self.assertEqual(kinds[0], "system.started")
        self.assertEqual(kinds[-1], "system.stopped")
        self.assertEqual(kinds.count("system.heartbeat"), 2)

        status = health(self.runtime)
        self.assertEqual(status["database"]["events"], 4)
        self.assertGreaterEqual(status["bus"]["published"], 4)
        self.assertFalse((self.runtime.config.paths.data_dir / "run" / "sentinel.sock").exists())

    def test_serve_rejects_invalid_arguments(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(serve(self.runtime, interval=0))
        with self.assertRaises(ValueError):
            asyncio.run(serve(self.runtime, iterations=0))

    def test_sentinel_health_tracks_live_socket_and_resets_on_shutdown(self) -> None:
        async def exercise() -> None:
            task = asyncio.create_task(serve(self.runtime, interval=60))
            socket_path = self.runtime.config.paths.data_dir / "run" / "sentinel.sock"
            for _ in range(100):
                if socket_path.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(socket_path.exists())
            self.assertFalse(health(self.runtime)["sentinel"]["connected"])

            reader, writer = await asyncio.open_unix_connection(socket_path)
            writer.write(
                json.dumps(
                    {
                        "source": "sentinel_rs",
                        "kind": "perception.system.sample",
                        "payload": {"process_count": 2},
                        "dedup": True,
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()
            self.assertTrue(json.loads(await reader.readline())["ok"])
            writer.close()
            await writer.wait_closed()
            self.assertTrue(health(self.runtime)["sentinel"]["connected"])

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(health(self.runtime)["sentinel"]["connected"])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
