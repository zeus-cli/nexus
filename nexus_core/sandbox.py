
"""Secure Sandbox Executor utilizing Bubblewrap (bwrap) for process isolation."""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .recorder import EventRecorder


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    memory_max: str | None = None # e.g., "512M"
    cpu_quota: str | None = None  # e.g., "50%"
    pids_max: int | None = None

@dataclass(frozen=True, slots=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    interrupted: bool = False
    reason: str | None = None

class SandboxError(RuntimeError):
    """Base error for sandbox operations."""

class SandboxExecutor:
    """Manages isolated process execution using bubblewrap and optional systemd-run for resource limits."""

    def __init__(
        self,
        recorder: EventRecorder,
        workspace_root: str | None = None,
    ) -> None:
        self._recorder = recorder
        self._workspace_root = Path(workspace_root or "/tmp/nexus-sandbox")
        self._workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    async def execute(
        self,
        argv: tuple[str, ...],
        env: dict[str, str] | None = None,
        timeout_s: float = 10.0,
        mounts: list[tuple[str, str]] | None = None,
        limits: ResourceLimits | None = None,
    ) -> SandboxResult:
        """Executes a command inside a hardened bubblewrap sandbox with optional resource limits."""
        
        if not argv:
            raise ValueError("argv cannot be empty")

        exec_id = uuid.uuid4().hex
        session_dir = self._workspace_root / exec_id
        session_dir.mkdir(mode=0o700)
        
        try:
            # 1. Build Bwrap arguments
            bwrap_args = [
                "/usr/bin/bwrap",
                "--unshare-all",
                "--uid", "0",
                "--gid", "0",
                "--ro-bind", "/usr", "/usr",
                "--ro-bind", "/bin", "/bin",
                "--ro-bind", "/lib", "/lib",
                "--ro-bind", "/lib64", "/lib64",
                "--ro-bind", "/etc", "/etc",
                "--tmpfs", "/tmp",
                "--proc", "/proc",
                "--dev", "/dev",
                "--bind", str(session_dir), "/workspace",
                "--chdir", "/workspace",
                "--clearenv",
            ]
            
            if mounts:
                for host, sand in mounts:
                    bwrap_args.extend(["--bind", host, sand])
            
            env_vars = {"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"}
            if env:
                env_vars.update(env)
            
            for k, v in env_vars.items():
                bwrap_args.extend(["--setenv", k, v])
            
            bwrap_args.extend(argv)
            
            # 2. Wrap in systemd-run for resource governance if limits are provided
            final_argv = bwrap_args
            if limits:
                systemd_args = [
                    "/usr/bin/systemd-run",
                    "--user",
                    "--scope",
                    "--quiet",
                ]
                if limits.memory_max:
                    systemd_args.extend(["--property", f"MemoryMax={limits.memory_max}"])
                if limits.cpu_quota:
                    systemd_args.extend(["--property", f"CPUQuota={limits.cpu_quota}"])
                if limits.pids_max:
                    systemd_args.extend(["--property", f"TasksMax={limits.pids_max}"])
                
                final_argv = systemd_args + bwrap_args

            # 3. Execution
            started = time.monotonic()
            process = await asyncio.create_subprocess_exec(
                *final_argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_s
                )
                exit_code = process.returncode or 0
                interrupted = False
                reason = None
            except TimeoutError:
                process.kill()
                await process.wait()
                stdout_bytes, stderr_bytes = await process.communicate()
                exit_code = -1
                interrupted = True
                reason = "timeout"
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                stdout_bytes, stderr_bytes = await process.communicate()
                exit_code = -1
                interrupted = True
                reason = "cancelled"
                raise
            
            duration_ms = int((time.monotonic() - started) * 1000)
            
            MAX_OUTPUT_BYTES = 16384
            stdout = stdout_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            stderr = stderr_bytes[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            truncated = len(stdout_bytes) > MAX_OUTPUT_BYTES or len(stderr_bytes) > MAX_OUTPUT_BYTES
            
            return SandboxResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                truncated=truncated,
                interrupted=interrupted,
                reason=reason,
            )
            
        finally:
            try:
                shutil.rmtree(session_dir)
            except Exception:
                pass

    async def cleanup_root(self) -> None:
        """Wipes all sandbox temporary data."""
        if self._workspace_root.exists():
            shutil.rmtree(self._workspace_root)
            self._workspace_root.mkdir(mode=0o700)
