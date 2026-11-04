from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from nexus_core.config import (
    ENV_DATA_DIR,
    ENV_LOG_LEVEL,
    ConfigError,
    config_from_mapping,
    find_project_root,
    load_config,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def write_project(root: Path, body: str) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "nexus.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class DefaultConfigTest(unittest.TestCase):
    def test_repository_config_is_valid(self) -> None:
        config = load_config(PROJECT_ROOT / "config" / "nexus.toml")
        self.assertEqual(config.environment, "production")
        self.assertEqual(config.safety.autonomy_level, 0)
        self.assertEqual(config.autonomy_name, "observation")
        self.assertEqual(config.paths.database, PROJECT_ROOT / "data" / "databases" / "zeus_events.db")
        self.assertEqual(config.paths.log_file, PROJECT_ROOT / "data" / "logs" / "nexus.log")
        self.assertEqual(config.api.host, "127.0.0.1")
        self.assertEqual(config.events.retention_days, 0)

    def test_project_root_discovery_stops_at_config_dir(self) -> None:
        self.assertEqual(find_project_root(Path(__file__)), PROJECT_ROOT)


class OverrideTest(unittest.TestCase):
    def test_env_data_dir_relocates_default_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_project(root, "[paths]\ndata_dir = 'data'\n")
            with mock.patch.dict(os.environ, {ENV_DATA_DIR: str(root / "override")}, clear=False):
                config = load_config(path)
            self.assertEqual(config.paths.data_dir, root / "override")
            self.assertEqual(
                config.paths.database, root / "override" / "databases" / "zeus_events.db"
            )
            self.assertEqual(config.paths.log_file, root / "override" / "logs" / "nexus.log")

    def test_explicit_database_path_wins_over_env(self) -> None:
        data = {
            "paths": {"database": "custom/events.db", "data_dir": "data"},
            "events": {"retention_days": 7},
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = config_from_mapping(data, source=Path(tmp) / "config" / "nexus.toml")
        self.assertEqual(config.paths.database, Path(tmp).resolve() / "custom" / "events.db")
        self.assertEqual(config.events.retention_days, 7)

    def test_env_log_level_applies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_project(Path(tmp), "[logging]\nlevel = 'INFO'\n")
            with mock.patch.dict(os.environ, {ENV_LOG_LEVEL: "debug"}, clear=False):
                config = load_config(path)
        self.assertEqual(config.logging.level, "DEBUG")


class ValidationTest(unittest.TestCase):
    def assert_invalid(self, body: str, fragment: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_project(Path(tmp), body)
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
        self.assertIn(fragment, str(ctx.exception))

    def test_unknown_top_level_key(self) -> None:
        self.assert_invalid("[banco]\nhost = 'x'\n", "desconhecidas")

    def test_unknown_section_key(self) -> None:
        self.assert_invalid("[events]\nretention_days = 30\nretencao = 5\n", "desconhecidas")

    def test_autonomy_level_out_of_range(self) -> None:
        self.assert_invalid("[safety]\nautonomy_level = 9\n", "entre 0 e 4")

    def test_port_out_of_range(self) -> None:
        self.assert_invalid("[api]\nport = 99999\n", "api.port")

    def test_non_loopback_api_host_is_rejected(self) -> None:
        self.assert_invalid('[api]\nhost = "0.0.0.0"\n', "loopback")

    def test_enabled_remote_provider_requires_https(self) -> None:
        self.assert_invalid(
            """
            [[llm.providers]]
            name = "remote"
            kind = "ollama"
            base_url = "http://models.example/v1"
            models = ["model"]
            enabled = true
            authorized = true
            """,
            "HTTPS",
        )

    def test_non_finite_llm_timeout_is_rejected(self) -> None:
        self.assert_invalid(
            """
            [[llm.providers]]
            name = "local"
            kind = "ollama"
            base_url = "http://127.0.0.1:11434"
            models = ["model"]
            enabled = true
            authorized = true
            timeout_s = nan
            """,
            "finito",
        )

    def test_invalid_log_level(self) -> None:
        self.assert_invalid("[logging]\nlevel = 'TRACE'\n", "logging.level")

    def test_non_integer_retention(self) -> None:
        self.assert_invalid("[events]\nretention_days = '90'\n", "events.retention_days")

    def test_missing_config_file(self) -> None:
        with self.assertRaises(ConfigError):
            load_config("/caminho/inexistente/nexus.toml")


if __name__ == "__main__":
    unittest.main()
