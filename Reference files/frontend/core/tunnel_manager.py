"""
Tunnel manager.
For each bastion in the inventory:
  1. Open SSH connection (port 22 only — no new firewall rules)
  2. Check backend version on bastion
  3. SFTP push backend files if version differs
  4. Launch backend via SSH exec (listens on localhost:PORT only)
  5. Create SSH local port-forward: localhost:LOCAL_PORT → bastion:localhost:PORT
  6. Return WebSocket URL ws://localhost:LOCAL_PORT/ws

All communication with the backend goes through the SSH tunnel on port 22.
No new ports are opened in any firewall.
"""
from __future__ import annotations
import asyncio
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import paramiko

from config import AppConfig, ClusterConfig, SSHCred

log = logging.getLogger("frontend.tunnel")

BACKEND_DIR_REMOTE = "/tmp/cloud_health/backend"
BACKEND_VERSION_REMOTE = f"{BACKEND_DIR_REMOTE}/version.txt"
BACKEND_MAIN_REMOTE    = f"{BACKEND_DIR_REMOTE}/main.py"

# Local backend source directory (relative to this file)
BACKEND_SRC = Path(__file__).parent.parent.parent / "backend"

# Port range for local tunnel endpoints (one per bastion)
LOCAL_PORT_START = 19000


@dataclass
class TunnelInfo:
    cluster_name:  str
    installer_host: str
    local_port:    int
    ws_url:        str
    ssh_client:    paramiko.SSHClient
    transport:     paramiko.Transport
    channel:       Optional[object] = None   # forward channel
    backend_pid:   Optional[int]    = None
    error:         Optional[str]    = None
    ready:         bool             = False


class TunnelManager:
    def __init__(self, app: AppConfig, local_backend_version: str):
        self.app                  = app
        self.local_backend_version= local_backend_version.strip()
        self._tunnels: Dict[str, TunnelInfo] = {}
        self._port_counter        = LOCAL_PORT_START

    # ── Public API ────────────────────────────────────────────────────────────

    async def setup_all(self, clusters: List[ClusterConfig]) -> Dict[str, TunnelInfo]:
        """
        Set up SSH tunnels for all clusters in parallel.
        Returns map of cluster_name → TunnelInfo.
        Unreachable clusters get TunnelInfo with error set.
        """
        tasks = [self._setup_one(c) for c in clusters]
        infos = await asyncio.gather(*tasks, return_exceptions=True)
        for cluster, info in zip(clusters, infos):
            if isinstance(info, Exception):
                self._tunnels[cluster.name] = TunnelInfo(
                    cluster_name   = cluster.name,
                    installer_host = cluster.installer_host or "",
                    local_port     = 0,
                    ws_url         = "",
                    ssh_client     = None,
                    transport      = None,
                    error          = str(info),
                )
            else:
                self._tunnels[cluster.name] = info
        return self._tunnels

    async def teardown_all(self):
        """Stop all backend processes and close all SSH connections."""
        tasks = [self._teardown_one(info) for info in self._tunnels.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tunnels.clear()

    # ── Per-cluster setup ─────────────────────────────────────────────────────

    async def _setup_one(self, cluster: ClusterConfig) -> TunnelInfo:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._setup_sync, cluster)

    def _setup_sync(self, cluster: ClusterConfig) -> TunnelInfo:
        host = cluster.installer_host
        cred = cluster.ssh_cred
        if not host or not cred:
            raise ValueError(f"No installer_host or SSH credentials for {cluster.name}")

        local_port = self._next_port()
        log.info(f"[{cluster.name}] Connecting to {host}:{cred.port}")

        # ── SSH connect ───────────────────────────────────────────────────────
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kw = dict(
            hostname=host, port=cred.port, username=cred.username,
            timeout=self.app.ssh_timeout,
            banner_timeout=self.app.ssh_timeout,
            auth_timeout=self.app.ssh_timeout,
        )
        if cred.private_key:
            connect_kw["pkey"] = paramiko.RSAKey.from_private_key_file(
                cred.private_key, password=cred.passphrase)
        elif cred.password:
            connect_kw["password"] = cred.password
        client.connect(**connect_kw)
        transport = client.get_transport()

        # ── Check / push backend version ──────────────────────────────────────
        self._sync_backend(client, cluster.name)

        # ── Launch backend ────────────────────────────────────────────────────
        backend_port = self.app.backend_port
        cmd = (
            f"mkdir -p {BACKEND_DIR_REMOTE} && "
            f"CP_PORT={backend_port} python3 {BACKEND_MAIN_REMOTE} "
            f"> /tmp/cloud_health/backend.log 2>&1 & echo $!"
        )
        _, stdout, _ = client.exec_command(cmd, timeout=15)
        pid_str = stdout.read().decode().strip()
        try:
            backend_pid = int(pid_str)
        except ValueError:
            backend_pid = None
        log.info(f"[{cluster.name}] Backend started PID={backend_pid} on port {backend_port}")

        # Give the backend a moment to start
        time.sleep(2)

        # ── SSH local port-forward ────────────────────────────────────────────
        # Forwards localhost:local_port on user's machine → bastion:localhost:backend_port
        # This goes entirely over the existing SSH connection on port 22.
        self._start_forward(transport, local_port, backend_port, cluster.name)

        ws_url = f"ws://localhost:{local_port}/ws"
        log.info(f"[{cluster.name}] Tunnel ready — {ws_url}")

        return TunnelInfo(
            cluster_name   = cluster.name,
            installer_host = host,
            local_port     = local_port,
            ws_url         = ws_url,
            ssh_client     = client,
            transport      = transport,
            backend_pid    = backend_pid,
            ready          = True,
        )

    def _sync_backend(self, client: paramiko.SSHClient, cluster_name: str):
        """Push backend files to bastion if version differs."""
        sftp = client.open_sftp()
        try:
            # Read remote version
            try:
                with sftp.open(BACKEND_VERSION_REMOTE) as fh:
                    remote_ver = fh.read().decode().strip()
            except IOError:
                remote_ver = ""

            if remote_ver == self.local_backend_version:
                log.info(f"[{cluster_name}] Backend version matches ({remote_ver}) — skip push")
                return

            log.info(f"[{cluster_name}] Backend version mismatch "
                     f"(local={self.local_backend_version} remote={remote_ver}) — pushing")

            # Ensure remote dir exists
            self._sftp_mkdir(sftp, BACKEND_DIR_REMOTE)
            self._sftp_mkdir(sftp, f"{BACKEND_DIR_REMOTE}/checks")
            self._sftp_mkdir(sftp, f"{BACKEND_DIR_REMOTE}/vendor")

            # Push all backend files recursively
            self._sftp_push_dir(sftp, BACKEND_SRC, BACKEND_DIR_REMOTE, cluster_name)
            log.info(f"[{cluster_name}] Backend push complete")
        finally:
            sftp.close()

    def _sftp_push_dir(self, sftp, local_dir: Path, remote_dir: str, cluster_name: str):
        for item in local_dir.rglob("*"):
            if item.suffix == ".pyc" or "__pycache__" in str(item):
                continue
            rel    = item.relative_to(local_dir)
            remote = f"{remote_dir}/{str(rel).replace(os.sep, '/')}"
            if item.is_dir():
                self._sftp_mkdir(sftp, remote)
            else:
                log.debug(f"[{cluster_name}] SFTP put {item} → {remote}")
                sftp.put(str(item), remote)

    @staticmethod
    def _sftp_mkdir(sftp, path: str):
        try:
            sftp.mkdir(path)
        except IOError:
            pass  # already exists

    def _start_forward(self, transport: paramiko.Transport,
                       local_port: int, remote_port: int, cluster_name: str):
        """
        Start a background thread that accepts connections on localhost:local_port
        and tunnels them to bastion's localhost:remote_port via the SSH transport.
        """
        def _forward_thread():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", local_port))
            sock.listen(5)
            sock.settimeout(1)
            while True:
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    if not transport.is_active():
                        break
                    continue
                try:
                    chan = transport.open_channel(
                        "direct-tcpip",
                        ("127.0.0.1", remote_port),
                        ("127.0.0.1", local_port),
                    )
                except Exception as e:
                    log.warning(f"[{cluster_name}] Channel open failed: {e}")
                    conn.close()
                    continue
                t = threading.Thread(target=_forward_conn,
                                     args=(conn, chan), daemon=True)
                t.start()
            sock.close()

        t = threading.Thread(target=_forward_thread, daemon=True)
        t.start()

    # ── Per-cluster teardown ──────────────────────────────────────────────────

    async def _teardown_one(self, info: TunnelInfo):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._teardown_sync, info)

    def _teardown_sync(self, info: TunnelInfo):
        if not info.ssh_client:
            return
        # Kill backend process if still running
        if info.backend_pid:
            try:
                info.ssh_client.exec_command(
                    f"kill {info.backend_pid} 2>/dev/null || true", timeout=5)
            except Exception:
                pass
        # Close SSH connection
        try:
            info.ssh_client.close()
        except Exception:
            pass
        log.info(f"[{info.cluster_name}] Tunnel closed")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _next_port(self) -> int:
        p = self._port_counter
        self._port_counter += 1
        return p


def _forward_conn(local_sock, chan):
    """Bidirectional pipe between a local socket and a paramiko channel."""
    import select
    try:
        while True:
            r, _, _ = select.select([local_sock, chan], [], [], 1)
            if local_sock in r:
                data = local_sock.recv(1024)
                if not data:
                    break
                chan.send(data)
            if chan in r:
                data = chan.recv(1024)
                if not data:
                    break
                local_sock.send(data)
    except Exception:
        pass
    finally:
        local_sock.close()
        chan.close()
