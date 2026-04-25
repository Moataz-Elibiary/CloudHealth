"""Async SSH client wrapper with typed CmdResult and per-command timeouts."""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import paramiko


@dataclass
class CmdResult:
    """Structured result from a remote SSH command."""
    command:     str
    stdout:      str
    stderr:      str
    exit_code:   int
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()

    @property
    def combined(self) -> str:
        parts = [self.stdout.strip(), self.stderr.strip()]
        return "\n".join(p for p in parts if p)


class SSHClient:
    """Async-compatible SSH client using paramiko via thread-pool executor."""

    def __init__(self, host: str, username: str, password: str = None,
                 key_path: str = None, port: int = 22, timeout: int = 30,
                 logger=None):
        self.host     = host
        self.username = username
        self.password = password
        self.key_path = key_path
        self.port     = port
        self.timeout  = timeout
        self.logger   = logger
        self._client: Optional[paramiko.SSHClient] = None

    # ── connection ────────────────────────────────────────────────────────────

    async def connect(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(
            hostname=self.host,
            port=self.port,
            username=self.username,
            timeout=self.timeout,
            banner_timeout=self.timeout,
            auth_timeout=self.timeout,
        )
        if self.key_path:
            kw["pkey"] = paramiko.RSAKey.from_private_key_file(self.key_path)
        elif self.password:
            kw["password"] = self.password
        else:
            kw["look_for_keys"] = True
        c.connect(**kw)
        self._client = c

    async def close(self):
        if self._client:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._client.close)
            self._client = None

    # ── execution ─────────────────────────────────────────────────────────────

    async def run(self, cmd: str, timeout: int = 60) -> CmdResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._run_sync, cmd, timeout)

    def _run_sync(self, cmd: str, timeout: int) -> CmdResult:
        if not self._client:
            raise RuntimeError("SSH not connected")
        t0 = time.monotonic()
        try:
            _, out, err = self._client.exec_command(cmd, timeout=timeout)
            rc   = out.channel.recv_exit_status()
            sout = out.read().decode("utf-8", errors="replace")
            serr = err.read().decode("utf-8", errors="replace")
        except Exception as e:
            elapsed = int((time.monotonic() - t0) * 1000)
            return CmdResult(cmd, "", str(e), -1, elapsed)
        return CmdResult(cmd, sout, serr, rc, int((time.monotonic() - t0) * 1000))

    # Legacy compatibility: returns tuple (exit_code, stdout, stderr)
    async def execute(self, command: str, timeout: int = 60):
        r = await self.run(command, timeout)
        return r.exit_code, r.stdout, r.stderr

    # ── context manager ───────────────────────────────────────────────────────

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.close()
