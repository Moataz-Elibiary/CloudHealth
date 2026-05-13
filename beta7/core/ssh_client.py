"""
Beta7 ssh_client.py — subprocess SSH wrappers (no paramiko).

SSHClient: runs commands on a remote host via the system ssh binary.
           Pass jump_host="user@bastion_ip" to route through a bastion
           (equivalent to ssh -J), used for node checks.

No persistent connection — every run() spawns a fresh ssh process.
connect() and close() are no-ops kept for API compatibility.
"""
from __future__ import annotations
import asyncio
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class SSHResult:
    stdout:      str
    stderr:      str
    exit_code:   int
    command:     str = ""
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


def _build_argv(
    host:      str,
    username:  str,
    key_path:  Optional[str],
    port:      int,
    timeout:   int,
    jump_host: Optional[str],
) -> List[str]:
    argv = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
        "-p", str(port),
    ]
    if key_path:
        argv += ["-i", str(Path(key_path).expanduser())]
    if jump_host:
        argv += ["-J", jump_host]
    argv.append(f"{username}@{host}")
    return argv


class SSHClient:
    """
    Thin wrapper around the system ssh binary.
    Each run() call spawns a new ssh subprocess — no persistent connection.
    """

    def __init__(
        self,
        host:      str,
        username:  str,
        key_path:  Optional[str] = None,
        port:      int = 22,
        timeout:   int = 30,
        jump_host: Optional[str] = None,
        logger=None,
    ):
        self.host      = host
        self.username  = username
        self.key_path  = key_path
        self.port      = port
        self.timeout   = timeout
        self.jump_host = jump_host
        self.logger    = logger

    def _run_sync(self, cmd: str, timeout: int) -> SSHResult:
        argv = _build_argv(
            self.host, self.username, self.key_path,
            self.port, self.timeout, self.jump_host,
        ) + [cmd]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            elapsed = int((time.monotonic() - t0) * 1000)
            return SSHResult(proc.stdout, proc.stderr, proc.returncode, cmd, elapsed)
        except subprocess.TimeoutExpired:
            elapsed = int((time.monotonic() - t0) * 1000)
            return SSHResult("", f"Command timed out after {timeout}s", -1, cmd, elapsed)
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            return SSHResult("", str(exc), -1, cmd, elapsed)

    async def run(self, cmd: str, timeout: int = 60) -> SSHResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._run_sync, cmd, timeout)

    async def execute(self, cmd: str, timeout: int = 60):
        r = await self.run(cmd, timeout)
        return r.exit_code, r.stdout, r.stderr

    async def connect(self):
        """No-op — no persistent connection needed."""

    async def close(self):
        """No-op — no persistent connection to close."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


# Aliases kept for import compatibility across checker modules
BastionClient = SSHClient
NodeClient    = SSHClient
