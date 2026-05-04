"""
Beta6 preflight.py — SSH connectivity and CLI availability check.

For each enabled cluster we verify:
  1. SSH socket reachable and authentication succeeds
  2. Required CLI tool is available on the bastion:
       OCP  → `oc version`
       CVIM → `openstack --version`

Python3 check is removed — no code is pushed to bastions in beta6.
Backend version check is removed — no backend process on bastions.

Results are logged and returned as a list of PreflightResult.
On_result callback is synchronous (no WebSocket streaming).
"""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

import paramiko

log = logging.getLogger("cloudhealth.preflight")

SSH_TIMEOUT_S = 10.0
CMD_TIMEOUT_S = 10.0

_CLI_PROBE = {
    "ocp":  "oc version --client 2>/dev/null || oc version 2>/dev/null | head -2",
    "cvim": "openstack --version 2>/dev/null || echo 'openstack not found'",
}


@dataclass
class PreflightResult:
    cluster_name: str
    cluster_type: str
    installer_ip: str
    timestamp:    str           # ISO-8601 UTC
    reachable:    bool
    auth_ok:      bool
    cli_ready:    bool
    cli_version:  Optional[str] = None
    duration_ms:  int = 0
    status:       str = "ERROR"  # OK | FAIL | ERROR
    error:        Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def run_preflight(
    clusters,
    parallel_limit: int,
    on_result: Optional[Callable[["PreflightResult"], None]] = None,
) -> List[PreflightResult]:
    """
    Blocking wrapper — runs async preflight in a new event loop.
    Called from run.py before the main health check run.
    """
    return asyncio.run(_run_preflight_async(clusters, parallel_limit, on_result))


async def _run_preflight_async(
    clusters,
    parallel_limit: int,
    on_result: Optional[Callable] = None,
) -> List[PreflightResult]:
    sem = asyncio.Semaphore(max(1, parallel_limit))

    async def _one(cluster) -> PreflightResult:
        async with sem:
            r = await asyncio.to_thread(_check_one_cluster, cluster)
            if on_result is not None:
                try:
                    on_result(r)
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
        cli_ready    = False,
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
            r.reachable = True
            r.error  = f"SSH authentication failed: {e}"
            r.status = "FAIL"
            return r
        except (paramiko.SSHException, OSError) as e:
            r.error  = f"SSH connection failed: {e}"
            r.status = "FAIL"
            return r

        # CLI availability check
        ctype = r.cluster_type.lower()
        if ctype not in _CLI_PROBE:
            r.error  = f"Unknown cluster type '{ctype}' — no preflight probe defined"
            r.status = "FAIL"
            return r
        probe = _CLI_PROBE[ctype]
        try:
            _, stdout, _ = client.exec_command(probe, timeout=CMD_TIMEOUT_S)
            out = stdout.read().decode("utf-8", errors="replace").strip()
            ec  = stdout.channel.recv_exit_status()
        except Exception as e:
            r.error  = f"CLI probe failed: {e}"
            r.status = "FAIL"
            return r

        if ec == 0 and out and "not found" not in out.lower():
            r.cli_ready  = True
            r.cli_version = out.splitlines()[0][:120] if out else None
            r.status = "OK"
        else:
            cli_name = "oc" if ctype == "ocp" else "openstack"
            r.error  = f"{cli_name} CLI unavailable on bastion (exit={ec}): {out[:100]!r}"
            r.status = "FAIL"

        return r

    finally:
        try:
            client.close()
        except Exception:
            pass
        r.duration_ms = int((time.monotonic() - started) * 1000)
