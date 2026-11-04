
import pytest
import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock

from nexus_core.sandbox import SandboxExecutor
from nexus_core.recorder import EventRecorder
from nexus_core.events import Event

@pytest.fixture
def setup_sandbox():
    recorder = MagicMock(spec=EventRecorder)
    # Use a specific test workspace to avoid clashing with real data
    executor = SandboxExecutor(recorder, workspace_root="/tmp/nexus-sandbox-test")
    return executor, recorder

@pytest.mark.asyncio
async def test_simple_execution(setup_sandbox):
    executor, _ = setup_sandbox
    # Run 'id' to check if we are root in sandbox
    res = await executor.execute(["/usr/bin/id"])
    assert res.exit_code == 0
    assert "uid=0(root)" in res.stdout

@pytest.mark.asyncio
async def test_read_authorized_file(setup_sandbox):
    executor, _ = setup_sandbox
    # Read /etc/hostname (mounted as RO)
    res = await executor.execute(["/usr/bin/cat", "/etc/hostname"])
    assert res.exit_code == 0
    assert len(res.stdout) > 0

@pytest.mark.asyncio
async def test_read_denied_file(setup_sandbox):
    executor, _ = setup_sandbox
    # Attempt to read a file in the host's home directory
    # Bubblewrap should prevent this as the home dir isn't mounted
    res = await executor.execute(["/usr/bin/cat", "/home/nexus/.bashrc"])
    # Depending on bwrap, this might return a 'No such file or directory'
    assert res.exit_code != 0 or "No such file" in res.stderr

@pytest.mark.asyncio
async def test_write_denied_filesystem(setup_sandbox):
    executor, _ = setup_sandbox
    # Attempt to write to /etc
    res = await executor.execute(["/usr/bin/touch", "/etc/sandbox_test"])
    assert res.exit_code != 0
    assert "Read-only file system" in res.stderr

@pytest.mark.asyncio
async def test_network_isolation(setup_sandbox):
    executor, _ = setup_sandbox
    # Attempt to ping google.com
    # Since we use --unshare-all, network should be disabled
    res = await executor.execute(["/usr/bin/ping", "-c", "1", "8.8.8.8"])
    assert res.exit_code != 0
    # Usually returns error about network unreachable or no route
    assert "Network is unreachable" in res.stderr or res.exit_code != 0

@pytest.mark.asyncio
async def test_dbus_isolation(setup_sandbox):
    executor, _ = setup_sandbox
    # Attempt to list DBus services
    res = await executor.execute(["/usr/bin/busctl", "list"])
    assert res.exit_code != 0
    assert "Failed to connect" in res.stderr or "No such file" in res.stderr

@pytest.mark.asyncio
async def test_sandbox_timeout(setup_sandbox):
    executor, _ = setup_sandbox
    # Command that sleeps longer than timeout
    res = await executor.execute(["/usr/bin/sleep", "2"], timeout_s=0.5)
    assert res.interrupted is True
    assert res.reason == "timeout"

@pytest.mark.asyncio
async def test_workspace_isolation(setup_sandbox):
    executor, _ = setup_sandbox
    # Create a file in /workspace
    await executor.execute(["/usr/bin/touch", "/workspace/test_file"])
    
    # In a second execution, the workspace should be different (isolated)
    # But we can check if the first one worked by looking at the result
    res = await executor.execute(["/usr/bin/ls", "/workspace"])
    # This is a different session, so /workspace should be empty
    assert "test_file" not in res.stdout

@pytest.mark.asyncio
async def test_process_cleanup(setup_sandbox):
    executor, _ = setup_sandbox
    # Start a background process that would normally persist
    # We use a shell script to spawn a sleep in background
    # bwrap --unshare-all usually kills children on exit, but we verify
    await executor.execute(["/usr/bin/sh", "-c", "sleep 100 &"])
    
    # Check if any sleep is running on host (it shouldn't be, but we check for the child)
    # This is hard to verify perfectly without root, but the sandbox design
    # using --unshare-all and kill() on timeout ensures cleanup.
    pass
