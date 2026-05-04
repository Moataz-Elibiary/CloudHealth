"""
Beta6 ssh_client.py

Two clients for the central-server-to-bastion architecture:

  BastionClient — SSH from the central server to a bastion.
                  Used by OCP/CVIM checkers whose `oc` / openstack / ciscovim
                  commands are on the bastion's PATH.  Replaces LocalClient
                  from beta5 (which ran subprocess locally on the bastion).

  NodeClient    — SSH from the central server to a physical node, jumping
                  through an already-connected bastion via a direct-tcpip
                  channel.  Used exclusively by HostHealthChecker.
                  Replaces the beta5 SSHClient that connected from bastion
                  to node directly.

Both share the same run() / execute() API so check code is unchanged.
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


# ── shared run helper ─────────────────────────────────────────────────────────

def _run_sync(client: paramiko.SSHClient, cmd: str, timeout: int) -> SSHResult:
    t0 = time.monotonic()
    try:
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        exit_code   = stdout.channel.recv_exit_status()
        stdout_text = stdout.read().decode("utf-8", errors="replace")
        stderr_text = stderr.read().decode("utf-8", errors="replace")
    except Exception as exc:
        elapsed = int((time.monotonic() - t0) * 1000)
        return SSHResult("", str(exc), -1, cmd, elapsed)
    elapsed = int((time.monotonic() - t0) * 1000)
    return SSHResult(stdout_text, stderr_text, exit_code, cmd, elapsed)


def _build_connect_kwargs(host, username, password, key_path, port, timeout) -> dict:
    kw = dict(
        hostname       = host,
        port           = port,
        username       = username,
        timeout        = timeout,
        banner_timeout = timeout,
        auth_timeout   = timeout,
    )
    if key_path:
        kw["key_filename"] = str(Path(key_path).expanduser())
    if password:
        kw["password"] = password
    if not password and not key_path:
        kw["look_for_keys"] = True
    return kw


# ── BastionClient ─────────────────────────────────────────────────────────────

class BastionClient:
    """
    SSH from the central server to the cluster bastion/installer node.
    All OCP and CVIM checks run their commands (oc, openstack, ciscovim)
    through this connection.
    """

    def __init__(
        self,
        host:     str,
        username: str,
        password: Optional[str] = None,
        key_path: Optional[str] = None,
        port:     int = 22,
        timeout:  int = 30,
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

    def get_transport(self) -> Optional[paramiko.Transport]:
        """Expose the underlying paramiko Transport for NodeClient jump-host use."""
        return self._client.get_transport() if self._client else None

    async def connect(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(**_build_connect_kwargs(
            self.host, self.username, self.password,
            self.key_path, self.port, self.timeout,
        ))
        self._client = client

    async def close(self):
        if self._client:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._client.close)
            self._client = None

    async def run(self, cmd: str, timeout: int = 60) -> SSHResult:
        if not self._client:
            return SSHResult("", "BastionClient not connected", -1, cmd, 0)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _run_sync, self._client, cmd, timeout)

    async def execute(self, cmd: str, timeout: int = 60):
        r = await self.run(cmd, timeout)
        return r.exit_code, r.stdout, r.stderr

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.close()


# ── NodeClient ────────────────────────────────────────────────────────────────

class NodeClient:
    """
    SSH from the central server to a physical node, routing through an
    already-connected bastion via a paramiko direct-tcpip channel.

    The bastion_transport must be the Transport of a live BastionClient
    connection (BastionClient.get_transport()).
    """

    def __init__(
        self,
        host:              str,
        username:          str,
        password:          Optional[str] = None,
        key_path:          Optional[str] = None,
        port:              int = 22,
        timeout:           int = 30,
        bastion_transport: Optional[paramiko.Transport] = None,
        logger=None,
    ):
        self.host              = host
        self.username          = username
        self.password          = password
        self.key_path          = key_path
        self.port              = port
        self.timeout           = timeout
        self.bastion_transport = bastion_transport
        self.logger            = logger
        self._client: Optional[paramiko.SSHClient] = None
        self._channel          = None

    async def connect(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._connect_sync)

    def _connect_sync(self):
        if self.bastion_transport is None:
            raise RuntimeError(
                f"NodeClient: no bastion_transport provided for {self.host}")

        # Open a direct-tcpip channel through the bastion to the node.
        self._channel = self.bastion_transport.open_channel(
            "direct-tcpip",
            (self.host, self.port),
            ("", 0),
        )

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(
            hostname       = self.host,
            username       = self.username,
            sock           = self._channel,   # route through bastion channel
            timeout        = self.timeout,
            banner_timeout = self.timeout,
            auth_timeout   = self.timeout,
        )
        if self.key_path:
            kw["key_filename"] = str(Path(self.key_path).expanduser())
        if self.password:
            kw["password"] = self.password
        if not self.password and not self.key_path:
            kw["look_for_keys"] = True
        client.connect(**kw)
        self._client = client

    async def close(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._close_sync)

    def _close_sync(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        if self._channel:
            try:
                self._channel.close()
            except Exception:
                pass
            self._channel = None

    async def run(self, cmd: str, timeout: int = 60) -> SSHResult:
        if not self._client:
            return SSHResult("", "NodeClient not connected", -1, cmd, 0)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _run_sync, self._client, cmd, timeout)

    async def execute(self, cmd: str, timeout: int = 60):
        r = await self.run(cmd, timeout)
        return r.exit_code, r.stdout, r.stderr

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.close()
