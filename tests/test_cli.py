from __future__ import annotations

import unittest
import json
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace

from nexus_core.app import bootstrap
from nexus_core.cli import _cmd_memory_export, build_parser


class CliParserTest(unittest.TestCase):
    def test_desktop_start_control(self) -> None:
        args = build_parser().parse_args(["desktop", "--no-start"])
        self.assertTrue(args.no_start)

    def test_config_option_is_accepted_before_and_after_subcommand(self) -> None:
        before = build_parser().parse_args(["--config", "config/nexus.toml", "serve"])
        after = build_parser().parse_args(["serve", "--config", "config/nexus.toml"])
        self.assertEqual(before.config, "config/nexus.toml")
        self.assertEqual(after.config, "config/nexus.toml")

    def test_config_option_is_accepted_after_nested_subcommand(self) -> None:
        args = build_parser().parse_args(
            ["llm", "models", "--config", "config/nexus.toml"]
        )
        self.assertEqual(args.config, "config/nexus.toml")

    def test_global_options_are_accepted_before_nested_subcommand(self) -> None:
        args = build_parser().parse_args(
            ["--config", "config/nexus.toml", "--json", "llm", "models"]
        )
        self.assertEqual(args.config, "config/nexus.toml")
        self.assertTrue(args.json)

    def test_json_option_is_accepted_after_subcommand(self) -> None:
        args = build_parser().parse_args(["status", "--json"])
        self.assertTrue(args.json)

    def test_json_option_is_accepted_after_nested_subcommand(self) -> None:
        args = build_parser().parse_args(["events", "list", "--json"])
        self.assertTrue(args.json)

    def test_logs_line_limit_option(self) -> None:
        args = build_parser().parse_args(["logs", "--lines", "25"])
        self.assertEqual(args.lines, 25)

    def test_task_provider_and_model_selection(self) -> None:
        args = build_parser().parse_args(
            ["task", "run", "--prompt", "hi", "--provider", "ollama-local", "--model", "gemma4:12b"]
        )
        self.assertEqual(args.provider, "ollama-local")
        self.assertEqual(args.model, "gemma4:12b")

    def test_memory_export_is_private_and_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
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
            runtime = bootstrap(config, console_logs=False)
            try:
                runtime.memory.create(kind="long_term", title="sample", content="private fact")
                destination = root / "memories.json"
                _cmd_memory_export(
                    runtime,
                    SimpleNamespace(to=str(destination), include_expired=False),
                )
                records = json.loads(destination.read_text(encoding="utf-8"))
                self.assertEqual(records[0]["content"], "private fact")
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
