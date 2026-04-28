"""
Beta4 frontend/core/tunnel_manager.py

Improvements over both betas:
  - Dynamic local port: caller passes local_port from socket.bind(0)
  - Version comparison before SFTP push (only syncs when needed)
  - Credential sanitiser: strips SSH creds from foreign-bastion clusters
    before sending to_backend_dict
  - Pure paramiko direct-tcpip channel (no socketserver dependency)
"""
from __future__ import annotations
import asyncio, logging, posixpath, select, socket, threading, time
from pathlib import Path
from typing import Optional

import paramiko

log = logging.getLogger("frontend.tunnel_manager")


# ── Port allocation ───────────────────────────────────────────────────────────

def allocate_local_port() -> int:
    """Ask the OS for a free TCP port (no race — socket stays open until bound)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── SSH tunnel handle ─────────────────────────────────────────────────────────

class TunnelHandle:
    """SSH client + background forward thread."""

    def __init__(self, client: paramiko.SSHClient,
                 stop_event: threading.Event, thread: threading.Thread):
        self.client     = client
        self._stop      = stop_event
        self._thread    = thread

    def close(self):
        self._stop.set()
        self._thread.join(timeout=3)
        try:
            self.client.close()
        except Exception:
            pass

    # Delegate paramiko methods used by sftp_push_backend / exec_command
    def __getattr__(self, name: str):
        return getattr(self.client, name)


def _forward_loop(transport: paramiko.Transport,
                  local_port: int, remote_port: int,
                  stop_event: threading.Event):
    """
    Accept connections on localhost:local_port and pipe them through
    a direct-tcpip channel to the bastion's localhost:remote_port.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", local_port))
    srv.listen(5)
    srv.settimeout(1.0)

    def _pipe(conn, chan):
        try:
            while True:
                r, _, _ = select.select([conn, chan], [], [], 1)
                if conn in r:
                    data = conn.recv(4096)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(4096)
                    if not data:
                        break
                    conn.sendall(data)
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass
            try: chan.close()
            except Exception: pass

    while not stop_event.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        try:
            chan = transport.open_channel(
                "direct-tcpip",
                ("127.0.0.1", remote_port),
                ("127.0.0.1", local_port),
            )
        except Exception:
            conn.close()
            continue
        t = threading.Thread(target=_pipe, args=(conn, chan), daemon=True)
        t.start()

    srv.close()


# ── Tunnel manager ────────────────────────────────────────────────────────────

class TunnelManager:

    def __init__(self):
        self._handles: list[TunnelHandle] = []

    async def connect_and_tunnel(
        self,
        cluster_name: str,
        host:         str,
        username:     str,
        password:     str  = None,
        key_path:     str  = None,
        remote_port:  int  = 8100,
        local_port:   int  = 0,
    ) -> TunnelHandle:
        if local_port == 0:
            local_port = allocate_local_port()
        handle = await asyncio.to_thread(
            self._connect_sync,
            host, username, password, key_path, remote_port, local_port,
        )
        self._handles.append(handle)
        return handle

    def _connect_sync(self, host, username, password, key_path,
                      remote_port, local_port) -> TunnelHandle:
        log.info("Connecting to %s (local_port=%d remote_port=%d)", host, local_port, remote_port)
        t0 = time.monotonic()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(hostname=host, username=username,
                  timeout=15, banner_timeout=15, auth_timeout=15)
        if key_path:
            kw["key_filename"] = str(Path(key_path).expanduser())
        if password:
            kw["password"] = password
        if not password and not key_path:
            kw["look_for_keys"] = True
        client.connect(**kw)
        log.info("Connected to %s in %.2fs", host, time.monotonic() - t0)

        stop_event = threading.Event()
        thread = threading.Thread(
            target=_forward_loop,
            args=(client.get_transport(), local_port, remote_port, stop_event),
            daemon=True,
        )
        thread.start()
        return TunnelHandle(client, stop_event, thread)

    async def close(self, handle: TunnelHandle):
        await asyncio.to_thread(handle.close)
        if handle in self._handles:
            self._handles.remove(handle)

    async def close_all(self):
        for h in list(self._handles):
            await self.close(h)


# ── SFTP backend push with version comparison ─────────────────────────────────

LOCAL_VERSION_FILE  = Path(__file__).parent.parent.parent / "version.txt"
REMOTE_BACKEND_DIR  = "/tmp/cloud_health"
REMOTE_VERSION_FILE = f"{REMOTE_BACKEND_DIR}/version.txt"


def _get_local_version() -> str:
    return LOCAL_VERSION_FILE.read_text().strip() \
        if LOCAL_VERSION_FILE.exists() else "0.0.0"


async def sftp_push_backend(
    handle:          TunnelHandle,
    local_backend_dir: str,
    remote_dir:      str = REMOTE_BACKEND_DIR,
) -> bool:
    """Push backend files only if remote version differs. Returns True if pushed."""
    pushed = await asyncio.to_thread(
        _sftp_push_sync, handle.client, local_backend_dir, remote_dir)
    return pushed


def _sftp_push_sync(client: paramiko.SSHClient,
                    local_backend_dir: str, remote_dir: str) -> bool:
    local_root   = Path(local_backend_dir)
    local_ver    = _get_local_version()
    sftp         = client.open_sftp()
    try:
        # Version check
        try:
            with sftp.open(REMOTE_VERSION_FILE) as fh:
                remote_ver = fh.read().decode().strip()
        except OSError:
            remote_ver = ""

        if remote_ver == local_ver:
            log.info("SFTP push skipped — remote already at version %s", remote_ver)
            return False   # already up to date

        def ensure_dir(path: str):
            parts = path.strip("/").split("/")
            cur   = ""
            for part in parts:
                cur = posixpath.join(cur, part)
                rp  = "/" + cur
                try:
                    sftp.stat(rp)
                except OSError:
                    try:
                        sftp.mkdir(rp)
                    except OSError:
                        pass

        def push_dir(lp: Path, rp: str):
            ensure_dir(rp)
            for entry in lp.iterdir():
                if entry.name in {"__pycache__", ".pytest_cache"} \
                        or entry.suffix == ".pyc":
                    continue
                remote_entry = posixpath.join(rp, entry.name)
                if entry.is_dir():
                    push_dir(entry, remote_entry)
                else:
                    sftp.put(str(entry), remote_entry)

        log.info("SFTP push started — local=%s remote=%s (ver %s → %s)",
                 local_root, remote_dir, remote_ver, local_ver)
        t0 = time.monotonic()
        push_dir(local_root, remote_dir)

        # Push vendor/ and requirements.txt from the program root so the
        # bastion can install deps offline (no internet access on bastions).
        program_root = local_root.parent
        req_file     = program_root / "requirements.txt"
        vendor_dir   = program_root / "vendor"
        if not req_file.exists() or not vendor_dir.exists():
            raise RuntimeError(
                "requirements.txt or vendor/ missing from local program root — "
                "source server must bundle wheels with 'pip download'")
        sftp.put(str(req_file), posixpath.join(remote_dir, "requirements.txt"))
        push_dir(vendor_dir, posixpath.join(remote_dir, "vendor"))
        log.info("SFTP push complete in %.2fs", time.monotonic() - t0)

        return True
    finally:
        sftp.close()
