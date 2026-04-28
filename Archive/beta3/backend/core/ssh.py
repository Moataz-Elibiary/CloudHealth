"""Async SSH client wrapper with typed results for node-level checks."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import paramiko


@dataclass
class CmdResult:
    command: str
    stdout: str
    stderr: str
    exit_code: int
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
        return "\n".join(part for part in parts if part)


class SSHClient:
    """Async-compatible SSH client using Paramiko in a thread pool."""

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
        self.host = host
        self.username = username
        self.password = password
        self.key_path = key_path
        self.port = port
        self.timeout = timeout
        self.logger = logger
        self._client: Optional[paramiko.SSHClient] = None

    async def connect(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(
            hostname=self.host,
            port=self.port,
            username=self.username,
            timeout=self.timeout,
            banner_timeout=self.timeout,
            auth_timeout=self.timeout,
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

    async def run(self, cmd: str, timeout: int = 60) -> CmdResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._run_sync, cmd, timeout)

    def _run_sync(self, cmd: str, timeout: int) -> CmdResult:
        if not self._client:
            raise RuntimeError("SSH not connected")

        started = time.monotonic()
        try:
            _, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            stdout_text = stdout.read().decode("utf-8", errors="replace")
            stderr_text = stderr.read().decode("utf-8", errors="replace")
        except Exception as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            return CmdResult(cmd, "", str(exc), -1, elapsed)

        elapsed = int((time.monotonic() - started) * 1000)
        return CmdResult(cmd, stdout_text, stderr_text, exit_code, elapsed)

    async def execute(self, command: str, timeout: int = 60):
        result = await self.run(command, timeout)
        return result.exit_code, result.stdout, result.stderr

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.close()
