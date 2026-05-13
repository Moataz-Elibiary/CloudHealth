"""
Beta7 preflight.py — SSH connectivity and CLI availability check.

For each enabled cluster we verify:
  1. SSH reachable and authentication succeeds (key-based)
  2. Required CLI tool is available on the bastion:
       OCP  → `oc version`
       CVIM → `openstack --version`

Uses the system ssh binary via subprocess — no paramiko dependency.
"""
from __future__ import annotations
import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger("cloudhealth.preflight")

SSH_TIMEOUT = 10   # seconds for connection
CMD_TIMEOUT = 15   # seconds for CLI probe command

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
    """Blocking wrapper — runs async preflight in a new event loop."""
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


def _base_argv(cluster, timeout: int) -> List[str]:
    argv = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "PasswordAuthentication=no",
        "-p", "22",
    ]
    if cluster.ssh_key:
        argv += ["-i", str(Path(cluster.ssh_key).expanduser())]
    argv.append(f"{cluster.ssh_user}@{cluster.installer_ip}")
    return argv


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

    try:
        base_argv = _base_argv(cluster, SSH_TIMEOUT)

        # Step 1 — SSH reachability + auth
        try:
            probe = subprocess.run(
                base_argv + ["echo __ok__"],
                capture_output=True, text=True, timeout=SSH_TIMEOUT + 2,
            )
        except subprocess.TimeoutExpired:
            r.error  = f"SSH connection timed out after {SSH_TIMEOUT}s"
            r.status = "FAIL"
            return r
        except Exception as exc:
            r.error  = f"SSH subprocess error: {exc}"
            r.status = "ERROR"
            return r

        if probe.returncode != 0:
            stderr = probe.stderr.strip()
            if "Permission denied" in stderr or "publickey" in stderr:
                r.reachable = True
                r.error  = f"SSH authentication failed: {stderr[:120]}"
            else:
                r.error  = f"SSH connection failed (exit={probe.returncode}): {stderr[:120]}"
            r.status = "FAIL"
            return r

        r.reachable = True
        r.auth_ok   = True

        # Step 2 — CLI availability
        ctype = r.cluster_type.lower()
        if ctype not in _CLI_PROBE:
            r.error  = f"Unknown cluster type '{ctype}' — no preflight probe defined"
            r.status = "FAIL"
            return r

        try:
            cli_probe = subprocess.run(
                base_argv + [_CLI_PROBE[ctype]],
                capture_output=True, text=True, timeout=CMD_TIMEOUT + 2,
            )
        except subprocess.TimeoutExpired:
            r.error  = f"CLI probe timed out after {CMD_TIMEOUT}s"
            r.status = "FAIL"
            return r
        except Exception as exc:
            r.error  = f"CLI probe subprocess error: {exc}"
            r.status = "ERROR"
            return r

        out = cli_probe.stdout.strip()
        ec  = cli_probe.returncode
        if ec == 0 and out and "not found" not in out.lower():
            r.cli_ready   = True
            r.cli_version = out.splitlines()[0][:120]
            r.status      = "OK"
        else:
            cli_name = "oc" if ctype == "ocp" else "openstack"
            r.error  = f"{cli_name} CLI unavailable on bastion (exit={ec}): {out[:100]!r}"
            r.status = "FAIL"

        return r

    finally:
        r.duration_ms = int((time.monotonic() - started) * 1000)
