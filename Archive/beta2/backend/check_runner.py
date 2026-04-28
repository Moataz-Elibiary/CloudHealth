"""
Backend check runner.
Runs all checks for a single cluster, streams results into the asyncio.Queue.
Each Section is wired to the queue so every pass_/fail_/warn_/info_ call
pushes a WS message without any change to the check function logic.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime
from typing import Optional

from config import AppConfig, ClusterConfig, resolve_threshold
from result import ClusterResult, Section, Status
from ssh_client import SSHClient
from checks.ocp_checks import OCPHealthChecker
from checks.cvim_checks import CVIMHealthChecker
from checks.host_checks import HostHealthChecker


log = logging.getLogger("backend.runner")


def _wire_section(sec: Section, queue: asyncio.Queue, cluster_name: str) -> Section:
    """Wire a Section to the shared queue so adders stream WS messages."""
    sec._queue        = queue
    sec._cluster_name = cluster_name
    return sec


async def run_cluster(
    cluster:      ClusterConfig,
    app:          AppConfig,
    queue:        asyncio.Queue,
    console_stub  = None,          # kept for checker API compatibility
) -> ClusterResult:
    """
    Run all health checks for one cluster.
    Every check result is pushed to `queue` in real time.
    Returns the complete ClusterResult when done.
    """
    result = ClusterResult(
        cluster_name = cluster.name,
        cluster_type = cluster.cluster_type,
        environment  = cluster.environment,
        description  = cluster.description,
        start_time   = datetime.now(),
    )

    # Announce cluster start to frontend
    await queue.put({
        "type":        "cluster_start",
        "cluster":     cluster.name,
        "cluster_type": cluster.cluster_type,
        "environment": cluster.environment,
    })

    try:
        if cluster.cluster_type == "ocp":
            await _run_ocp(cluster, app, result, queue)
        elif cluster.cluster_type == "cvim":
            await _run_cvim(cluster, app, result, queue)
        else:
            result.login_success = False
            result.login_error   = f"Unknown cluster type: {cluster.cluster_type}"
    except Exception as e:
        log.exception(f"[{cluster.name}] Unhandled runner error")
        sec = _wire_section(Section("Engine Error", "engine", start_time=datetime.now()), queue, cluster.name)
        sec.error(f"Unhandled exception: {e}")
        sec.end_time = datetime.now()
        result.add_section(sec)

    result.end_time = datetime.now()

    # Announce cluster done
    await queue.put({
        "type":        "cluster_done",
        "cluster":     cluster.name,
        "overall":     result.overall_status.value,
        "pass_count":  result.pass_count,
        "fail_count":  result.fail_count,
        "warn_count":  result.warn_count,
        "duration_s":  result.duration_s,
        "login_success": result.login_success,
        "login_error":   result.login_error,
    })
    return result


# ── OCP ───────────────────────────────────────────────────────────────────────

async def _run_ocp(cluster: ClusterConfig, app: AppConfig, result: ClusterResult, queue: asyncio.Queue):
    if not cluster.ssh_cred:
        result.login_success = False
        result.login_error   = "No SSH credentials provided"
        return

    host = cluster.installer_host or (
        cluster.api_url.replace("https://", "").split(":")[0] if cluster.api_url else None)
    if not host:
        result.login_success = False
        result.login_error   = "No installer_host or api_url specified"
        return

    ssh = SSHClient(host, cluster.ssh_cred, app.ssh_timeout, log)
    try:
        await ssh.connect()
    except Exception as e:
        result.login_success = False
        result.login_error   = str(e)
        return

    try:
        checker = OCPHealthChecker(cluster, app, ssh, log, _QueueConsole(queue, cluster.name))
        # Monkey-patch section factory so every new Section is queue-wired
        original_run = checker.run

        async def patched_run():
            sections = await original_run()
            for sec in sections:
                sec._queue        = queue
                sec._cluster_name = cluster.name
            return sections

        checker.run = patched_run
        for sec in await checker.run():
            result.add_section(sec)

        nodes = cluster.nodes or await checker.discover_nodes()
        if nodes and _should(app, "host"):
            hc = HostHealthChecker(nodes, cluster.ssh_cred, app, log,
                                   _QueueConsole(queue, cluster.name), cluster.name)
            for sec in await hc.run():
                sec._queue = queue
                sec._cluster_name = cluster.name
                result.add_section(sec)
    finally:
        await ssh.close()


# ── CVIM ──────────────────────────────────────────────────────────────────────

async def _run_cvim(cluster: ClusterConfig, app: AppConfig, result: ClusterResult, queue: asyncio.Queue):
    if not cluster.ssh_cred:
        result.login_success = False
        result.login_error   = "No SSH credentials provided"
        return
    if not cluster.installer_host:
        result.login_success = False
        result.login_error   = "No installer_host specified for CVIM cluster"
        return

    ssh = SSHClient(cluster.installer_host, cluster.ssh_cred, app.ssh_timeout, log)
    try:
        await ssh.connect()
    except Exception as e:
        result.login_success = False
        result.login_error   = str(e)
        return

    try:
        checker = CVIMHealthChecker(cluster, app, ssh, log, _QueueConsole(queue, cluster.name))
        for sec in await checker.run():
            sec._queue = queue
            sec._cluster_name = cluster.name
            result.add_section(sec)

        nodes = cluster.nodes or await checker.discover_nodes()
        if nodes and _should(app, "host"):
            hc = HostHealthChecker(nodes, cluster.ssh_cred, app, log,
                                   _QueueConsole(queue, cluster.name), cluster.name)
            for sec in await hc.run():
                sec._queue = queue
                sec._cluster_name = cluster.name
                result.add_section(sec)
    finally:
        await ssh.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _should(app: AppConfig, cat: str) -> bool:
    return app.enabled_checks is None or cat in app.enabled_checks


class _QueueConsole:
    """Minimal console stub that pushes section_start/done to the queue."""

    def __init__(self, queue: asyncio.Queue, cluster: str):
        self._q = queue
        self._c = cluster

    def section_start(self, name: str):
        try:
            self._q.put_nowait({
                "type":    "section_start",
                "cluster": self._c,
                "section": name,
            })
        except asyncio.QueueFull:
            pass

    def section_done(self, sec: Section):
        try:
            self._q.put_nowait({
                "type":       "section_done",
                "cluster":    self._c,
                "section":    sec.name,
                "category":   sec.category,
                "worst":      sec.worst_status.value,
                "pass_count": sec.pass_count,
                "fail_count": sec.fail_count,
                "warn_count": sec.warn_count,
                "duration_s": sec.duration_s,
            })
        except asyncio.QueueFull:
            pass

    def cluster_start(self, *a, **kw): pass
    def cluster_done(self,  *a, **kw): pass
    def final_summary(self, *a, **kw): pass
