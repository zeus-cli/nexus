
import pytest
import os
import tempfile
from pathlib import Path
from nexus_core.app import bootstrap, NexusRuntime

@pytest.fixture
def temp_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        config_dir = root / "config"
        config_dir.mkdir()
        config_file = config_dir / "nexus.toml"
        
        config_text = (
            "[paths]\n"
            "data_dir = \"data\"\n"
            "database = \"data/nexus_events.db\"\n"
            "log_file = \"data/nexus.log\"\n\n"
            "[safety]\n"
            "autonomy_level = 1\n\n"
            "[events]\n"
            "dedup_window_hours = 24\n"
            "max_payload_bytes = 16384\n"
            "retention_days = 7\n"
            "query_limit = 100\n\n"
            "[llm]\n"
            "max_context_tokens = 4096\n"
            "failure_threshold = 3\n"
            "cooldown_s = 60\n"
            "max_retries = 2\n"
            "providers = []\n"
            "chain = []\n\n"
            "[logging]\n"
            "level = \"INFO\"\n"
        )
        config_file.write_text(config_text)
        (root / "data").mkdir()
        yield config_file

@pytest.mark.asyncio
async def test_bootstrap_success(temp_config):
    runtime = bootstrap(config_path=temp_config)
    assert isinstance(runtime, NexusRuntime)
    assert runtime.config is not None
    await runtime.aclose()

@pytest.mark.asyncio
async def test_bootstrap_invalid_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "invalid.toml"
        config_file.write_text("invalid = config")
        with pytest.raises(Exception):
            bootstrap(config_path=config_file)
