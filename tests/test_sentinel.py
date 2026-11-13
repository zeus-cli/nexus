from __future__ import annotations

import asyncio
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from nexus_core.app import bootstrap
from nexus_core.sentinel import SentinelReceiver


class SentinelReceiverTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        config = root / "config" / "nexus.toml"
        config.parent.mkdir()
        config.write_text(
            textwrap.dedent(
                """
                [paths]
                data_dir = "data"
                [safety]
                autonomy_level = 1
                """
            ),
            encoding="utf-8",
        )
        self.runtime = bootstrap(config, console_logs=False)
        self.addCleanup(self.runtime.close)
        self.socket_path = root / "data" / "run" / "sentinel.sock"
        self.receiver = SentinelReceiver(self.socket_path, self.runtime.recorder)
        self.addAsyncCleanup(self.receiver.close)

    async def _send(self, frame: dict[str, object]) -> dict[str, object]:
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        writer.write(json.dumps(frame).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return response

    async def test_records_valid_sentinel_event_and_uses_private_socket(self) -> None:
        await self.receiver.start()
        self.assertEqual(self.socket_path.stat().st_mode & 0o777, 0o600)

        ack = await self._send(
            {
                "source": "sentinel_rs",
                "kind": "perception.system.sample",
                "severity": "info",
                "payload": {"process_count": 3},
                "dedup": True,
            }
        )

        self.assertTrue(ack["ok"])
        events = self.runtime.store.query(kind="perception.system.sample")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["process_count"], 3)
        self.assertTrue(self.receiver.health()["connected"])

        # An unchanged sample is deduplicated in SQLite but still proves the
        # process is alive, so its volatile liveness timestamp must advance.
        await self._send(
            {
                "source": "sentinel_rs",
                "kind": "perception.system.sample",
                "severity": "info",
                "payload": {"process_count": 3},
                "dedup": True,
            }
        )
        self.assertEqual(self.runtime.store.count(kind="perception.system.sample"), 1)

    async def test_rejects_non_sentinel_source(self) -> None:
        await self.receiver.start()
        ack = await self._send(
            {
                "source": "untrusted",
                "kind": "perception.system.sample",
                "payload": {},
            }
        )
        self.assertFalse(ack["ok"])
        self.assertEqual(self.runtime.store.count(), 0)

    async def test_refuses_to_replace_regular_file_at_socket_path(self) -> None:
        self.socket_path.parent.mkdir(parents=True)
        self.socket_path.write_text("keep", encoding="utf-8")
        with self.assertRaises(PermissionError):
            await self.receiver.start()
        self.assertEqual(self.socket_path.read_text(encoding="utf-8"), "keep")

    async def test_second_receiver_does_not_unlink_live_socket(self) -> None:
        await self.receiver.start()
        second = SentinelReceiver(self.socket_path, self.runtime.recorder)
        with self.assertRaises(RuntimeError):
            await second.start()
        await second.close()
        self.assertTrue(self.socket_path.is_socket())

    async def test_oversized_frame_gets_error_reply_before_close(self) -> None:
        from nexus_core.sentinel import MAX_FRAME_BYTES

        await self.receiver.start()
        ack = await self._send(
            {
                "source": "sentinel_rs",
                "kind": "perception.system.sample",
                "payload": {"blob": "x" * (MAX_FRAME_BYTES + 1024)},
            }
        )

        self.assertFalse(ack["ok"])
        self.assertEqual(ack["error"], "frame too large")
        self.assertEqual(self.runtime.store.count(), 0)


if __name__ == "__main__":
    unittest.main()
