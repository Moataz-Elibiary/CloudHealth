"""
Beta4 ssh_client.py

Two clients:
  LocalClient  — runs commands locally on the bastion via asyncio.create_subprocess_shell.
                 Used by OCP/CVIM checkers whose `oc` / openstack / ciscovim commands
                 are already on the bastion's PATH.

  SSHClient    — real paramiko SSH from the bastion to individual compute/storage nodes.
                 Used exclusively by HostHealthChecker for per-node checks.

This split is the correct architecture: cluster-level checks are local,
host-level checks require real SSH to each node from the bastion.
"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import paramiko


@dataclass
class SSHResult:
    stdout:    str
    stderr:    str
    exit_code: int
    command:   str = ""
    duration_ms: int = 0

    @property
    def out(self) -> str:
        return self.stdout.strip()

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def combined(self) -> str:
        parts = [self.stdout.strip(), self.stderr.strip()]
        return "\n".join(p for p in parts if p)


# ── LocalClient: runs on the bastion via subprocess ──────────────────────────

class LocalClient:
    """
    Executes commands directly on the bastion machine.
    connect() and close() are no-ops — kept for API compatibility with SSHClient.
    """

    def __init__(self, host: str = "", user: str = "",
                 pwd: str = None, key_path: str = None, timeout: int = 30):
        self.host    = host
        self.user    = user
        self.timeout = timeout

    async def connect(self):
        return None

    async def close(self):
        return None

    async def run(self, cmd: str, timeout: int = 60) -> SSHResult:
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout)
            elapsed = int((time.monotonic() - t0) * 1000)
            return SSHResult(
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                proc.returncode,
                cmd, elapsed,
            )
        except asyncio.TimeoutError:
            elapsed = int((time.monotonic() - t0) * 1000)
            return SSHResult("", "Command timed out", -1, cmd, elapsed)
        except Exception as e:
            elapsed = int((time.monotonic() - t0) * 1000)
            return SSHResult("", str(e), -1, cmd, elapsed)

    async def execute(self, cmd: str, timeout: int = 60):
        r = await self.run(cmd, timeout)
        return r.exit_code, r.stdout, r.stderr

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


# ── SSHClient: real paramiko for node-level checks ───────────────────────────

class SSHClient:
    """
    Async-compatible SSH client using Paramiko in a thread pool.
    Used by HostHealthChecker to SSH from the bastion to individual nodes.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str = None,
        key_path: str = None,
        port: int = 22,
        timeout: int = 30,
        logger=None,
    ):
        self.host     = host
        self.username = username
        self.password = password
        self.key_path = key_path
        self.port     = port
        self.timeout  = timeout
        self.logger   = logger
        self._client: Optional[paramiko.SSHClient] = None

    async def connect(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(
            hostname     = self.host,
            port         = self.port,
            username     = self.username,
            timeout      = self.timeout,
            banner_timeout = self.timeout,
            auth_timeout = self.timeout,
        )
        if self.key_path:
            kwargs["key_filename"] = str(Path(self.key_path).expanduser())
        if self.password:
            kwargs["password"] = self.password
        if not self.password and not self.key_path:
            kwargs["look_for_keys"] = True
        client.connect(**kwargs)
        self._client = client

    async def close(self):
        if self._client:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._client.close)
            self._client = None

    async def run(self, cmd: str, timeout: int = 60) -> SSHResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._run_sync, cmd, timeout)

    def _run_sync(self, cmd: str, timeout: int) -> SSHResult:
        if not self._client:
            raise RuntimeError("SSH not connected")
        t0 = time.monotonic()
        try:
            _, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
            exit_code   = stdout.channel.recv_exit_status()
            stdout_text = stdout.read().decode("utf-8", errors="replace")
            stderr_text = stderr.read().decode("utf-8", errors="replace")
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return SSHResult("", str(exc), -1, cmd, elapsed)
        elapsed = int((time.monotonic() - t0) * 1000)
        return SSHResult(stdout_text, stderr_text, exit_code, cmd, elapsed)

    async def execute(self, command: str, timeout: int = 60):
        result = await self.run(command, timeout)
        return result.exit_code, result.stdout, result.stderr

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.close()
