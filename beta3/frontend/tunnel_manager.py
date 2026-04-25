"""SSH tunnel helpers for forwarding local ports to remote backend workers."""

from __future__ import annotations

import asyncio
import posixpath
import select
import socketserver
import threading
from pathlib import Path

import paramiko


class _ForwardServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _ForwardHandler(socketserver.BaseRequestHandler):
    chain_host = "127.0.0.1"
    chain_port = 0
    ssh_transport = None

    def handle(self):
        channel = self.ssh_transport.open_channel(
            "direct-tcpip",
            (self.chain_host, self.chain_port),
            self.request.getpeername(),
        )
        if channel is None:
            return

        try:
            while True:
                readers, _, _ = select.select([self.request, channel], [], [])
                if self.request in readers:
                    payload = self.request.recv(1024)
                    if not payload:
                        break
                    channel.sendall(payload)
                if channel in readers:
                    payload = channel.recv(1024)
                    if not payload:
                        break
                    self.request.sendall(payload)
        finally:
            channel.close()
            self.request.close()


class TunnelHandle:
    """Wrapper around an SSH client and its local port forward server."""

    def __init__(self, client: paramiko.SSHClient, server: _ForwardServer, thread: threading.Thread):
        self.client = client
        self.server = server
        self.thread = thread

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.client.close()

    def __getattr__(self, name: str):
        return getattr(self.client, name)


def _start_forward_server(local_port: int, transport, remote_port: int):
    handler = type(
        "_PortForwardHandler",
        (_ForwardHandler,),
        {"chain_port": remote_port, "ssh_transport": transport},
    )
    server = _ForwardServer(("127.0.0.1", local_port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class TunnelManager:
    """Creates SSH connections and local TCP tunnels to remote backend workers."""

    def __init__(self):
        self._handles = []

    async def connect_and_tunnel(
        self,
        cluster_name: str,
        host: str,
        username: str,
        password: str = None,
        key_path: str = None,
        remote_port: int = 8100,
        local_port: int = 8100,
    ) -> TunnelHandle:
        handle = await asyncio.to_thread(
            self._connect_and_tunnel_sync,
            host,
            username,
            password,
            key_path,
            remote_port,
            local_port,
        )
        self._handles.append(handle)
        return handle

    def _connect_and_tunnel_sync(
        self,
        host: str,
        username: str,
        password: str,
        key_path: str,
        remote_port: int,
        local_port: int,
    ):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {
            "hostname": host,
            "username": username,
            "timeout": 15,
            "banner_timeout": 15,
            "auth_timeout": 15,
        }
        if key_path:
            connect_kwargs["key_filename"] = str(Path(key_path).expanduser())
        if password:
            connect_kwargs["password"] = password
        if not password and not key_path:
            connect_kwargs["look_for_keys"] = True
        client.connect(**connect_kwargs)
        server, thread = _start_forward_server(local_port, client.get_transport(), remote_port)
        return TunnelHandle(client, server, thread)

    async def close(self, handle: TunnelHandle):
        await asyncio.to_thread(handle.close)
        if handle in self._handles:
            self._handles.remove(handle)


async def sftp_push_backend(ssh_handle: TunnelHandle, local_backend_dir: str, remote_dir: str):
    await asyncio.to_thread(_sftp_push_backend_sync, ssh_handle.client, local_backend_dir, remote_dir)


def _sftp_push_backend_sync(client: paramiko.SSHClient, local_backend_dir: str, remote_dir: str):
    local_root = Path(local_backend_dir)
    sftp = client.open_sftp()

    def ensure_remote_dir(path: str):
        current = ""
        for chunk in path.strip("/").split("/"):
            current = posixpath.join(current, chunk)
            remote_path = "/" + current
            try:
                sftp.stat(remote_path)
            except OSError:
                sftp.mkdir(remote_path)

    def push_dir(local_path: Path, remote_path: str):
        ensure_remote_dir(remote_path)
        for entry in local_path.iterdir():
            if entry.name in {"__pycache__", ".pytest_cache"} or entry.suffix == ".pyc":
                continue
            remote_entry = posixpath.join(remote_path, entry.name)
            if entry.is_dir():
                push_dir(entry, remote_entry)
            else:
                sftp.put(str(entry), remote_entry)

    try:
        push_dir(local_root, remote_dir)
    finally:
        sftp.close()
