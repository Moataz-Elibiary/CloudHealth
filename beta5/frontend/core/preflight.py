"""
Pre-flight credential / readiness validation (TODO P1.3).

For each enabled cluster we verify, in order:
  1. SSH socket reachable + authentication succeeds
  2. python3 is available on the bastion
  3. /opt/cloud_health/version.txt is readable (informational — absence is
     not fatal because the backend gets SFTP-pushed on first run)

Each cluster produces a PreflightResult row. The schema is intentionally
flat / dict-friendly so that P3.1 (history DB) can persist these rows
directly with no transformation — see PreflightResult.to_dict().
"""
from __future__ import annotations
import asyncio, logging, time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

import paramiko

log = logging.getLogger("frontend.preflight")

REMOTE_VERSION_PATH = "/opt/cloud_health/version.txt"
SSH_TIMEOUT_S = 8.0
CMD_TIMEOUT_S = 8.0


@dataclass
class PreflightResult:
    cluster_name:    str
    cluster_type:    str
    installer_ip:    str
    timestamp:       str           # ISO-8601 UTC, used in UI + history DB
    reachable:       bool
    auth_ok:         bool
    python_ready:    bool
    python_version:  Optional[str] = None
    backend_version: Optional[str] = None
    duration_ms:     int = 0
    status:          str = "ERROR"  # OK | FAIL | ERROR
    error:           Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# Type alias for the streaming callback used by the WS handler.
ResultCallback = Optional[Callable[[PreflightResult], Awaitable[None]]]


async def run_preflight(
    clusters,
    parallel_limit: int,
    on_result: ResultCallback = None,
) -> List[PreflightResult]:
    """Run preflight on all clusters in parallel, capped at parallel_limit.
    Each row is delivered via on_result the moment it completes, then the
    full ordered list is returned for callers that need it."""
    sem = asyncio.Semaphore(max(1, parallel_limit))

    async def _one(cluster) -> PreflightResult:
        async with sem:
            r = await asyncio.to_thread(_check_one_cluster, cluster)
            if on_result is not None:
                try:
                    await on_result(r)
                except Exception as e:
                    log.warning("preflight on_result callback failed: %s", e)
            return r

    return list(await asyncio.gather(*[_one(c) for c in clusters]))


def _check_one_cluster(cluster) -> PreflightResult:
    started = time.monotonic()
    r = PreflightResult(
        cluster_name = cluster.name,
        cluster_type = getattr(cluster, "type", "") or "",
        installer_ip = cluster.installer_ip,
        timestamp    = datetime.now(timezone.utc).isoformat(timespec="seconds"),
        reachable    = False,
        auth_ok      = False,
        python_ready = False,
    )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        kw = dict(
            hostname       = cluster.installer_ip,
            username       = cluster.ssh_user,
            timeout        = SSH_TIMEOUT_S,
            banner_timeout = SSH_TIMEOUT_S,
            auth_timeout   = SSH_TIMEOUT_S,
        )
        if cluster.ssh_key:
            kw["key_filename"] = str(Path(cluster.ssh_key).expanduser())
        if cluster.ssh_pass:
            kw["password"] = cluster.ssh_pass
        if not cluster.ssh_pass and not cluster.ssh_key:
            kw["look_for_keys"] = True

        try:
            client.connect(**kw)
            r.reachable = True
            r.auth_ok   = True
        except paramiko.AuthenticationException as e:
            # We got far enough to attempt auth, so the host was reachable.
            r.reachable = True
            r.error  = f"SSH authentication failed: {e}"
            r.status = "FAIL"
            return r
        except (paramiko.SSHException, OSError) as e:
            r.error  = f"SSH connection failed: {e}"
            r.status = "FAIL"
            return r

        # Python availability
        try:
            _, stdout, stderr = client.exec_command(
                "python3 --version 2>&1", timeout=CMD_TIMEOUT_S)
            out = stdout.read().decode().strip()
            ec  = stdout.channel.recv_exit_status()
        except Exception as e:
            r.error  = f"python3 check failed: {e}"
            r.status = "FAIL"
            return r

        if ec != 0 or not out:
            r.error  = f"python3 unavailable on bastion (exit={ec}): {out!r}"
            r.status = "FAIL"
            return r
        r.python_ready   = True
        r.python_version = out.replace("Python", "").strip() or out

        # Backend version (informational — missing is OK)
        try:
            _, stdout, _ = client.exec_command(
                f"cat {REMOTE_VERSION_PATH} 2>/dev/null", timeout=CMD_TIMEOUT_S)
            ver = stdout.read().decode().strip()
            ec  = stdout.channel.recv_exit_status()
        except Exception as e:
            r.error  = f"version probe failed: {e}"
            r.status = "FAIL"
            return r

        if ec == 0 and ver:
            r.backend_version = ver
            r.status = "OK"
        else:
            # First-run case — backend not yet pushed. Pass preflight but
            # surface the situation so the UI can hint at it.
            r.backend_version = None
            r.status = "OK"
            r.error  = f"{REMOTE_VERSION_PATH} not present yet — backend will be installed on first run"
        return r

    finally:
        try:
            client.close()
        except Exception:
            pass
        r.duration_ms = int((time.monotonic() - started) * 1000)
