"""Engine — orchestrates parallel health checks across all clusters."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from core.config import AppConfig, ClusterConfig, resolve_threshold
from core.result import ClusterResult, Section, Status
from core.ssh_client import SSHClient
from core.reporter_console import ConsoleReporter
from checks.ocp_checks import OCPHealthChecker
from checks.cvim_checks import CVIMHealthChecker
from checks.host_checks import HostHealthChecker


class HealthCheckEngine:
    def __init__(self, app: AppConfig, console: ConsoleReporter, logger: logging.Logger):
        self.app     = app
        self.console = console
        self.log     = logger

    async def run(self) -> List[ClusterResult]:
        sem   = asyncio.Semaphore(self.app.max_parallel_clusters)
        tasks = [self._run_guarded(c, sem) for c in self.app.clusters]
        return await asyncio.gather(*tasks, return_exceptions=False)

    async def _run_guarded(self, cluster: ClusterConfig, sem: asyncio.Semaphore) -> ClusterResult:
        async with sem:
            return await self._run_cluster(cluster)

    async def _run_cluster(self, cluster: ClusterConfig) -> ClusterResult:
        result = ClusterResult(
            cluster_name  = cluster.name,
            cluster_type  = cluster.cluster_type,
            environment   = cluster.environment,
            description   = cluster.description,
            start_time    = datetime.now(),
        )
        self.console.cluster_start(cluster.name, cluster.cluster_type)
        self.log.info(f"[{cluster.name}] Starting health check")

        try:
            if cluster.cluster_type == "ocp":
                await self._run_ocp(cluster, result)
            elif cluster.cluster_type == "cvim":
                await self._run_cvim(cluster, result)
            else:
                result.login_success = False
                result.login_error   = f"Unknown cluster type: {cluster.cluster_type}"
        except Exception as e:
            self.log.exception(f"[{cluster.name}] Unhandled engine error: {e}")
            sec = Section("Engine Error", "engine")
            sec.error(f"Unhandled exception during checks: {e}")
            result.add_section(sec)

        result.end_time = datetime.now()
        self.console.cluster_done(result)
        self.log.info(
            f"[{cluster.name}] Done — PASS:{result.pass_count} "
            f"FAIL:{result.fail_count} WARN:{result.warn_count}"
        )
        self._write_text_report(cluster, result)
        return result

    # ── OCP ───────────────────────────────────────────────────────────────────

    async def _run_ocp(self, cluster: ClusterConfig, result: ClusterResult):
        if not cluster.ssh_cred:
            result.login_success = False
            result.login_error   = "No SSH credentials provided"
            return

        host = cluster.installer_host or (
            cluster.api_url.replace("https://","").split(":")[0] if cluster.api_url else None)
        if not host:
            result.login_success = False
            result.login_error   = "No installer_host or api_url specified"
            return

        ssh = SSHClient(host, cluster.ssh_cred, self.app.ssh_timeout, self.log)
        try:
            await ssh.connect()
            self.log.info(f"[{cluster.name}] SSH connected to {host}")
        except Exception as e:
            result.login_success = False
            result.login_error   = str(e)
            self.log.error(f"[{cluster.name}] SSH failed: {e}")
            return

        try:
            # OCP API checks
            checker = OCPHealthChecker(cluster, self.app, ssh, self.log, self.console)
            for sec in await checker.run():
                result.add_section(sec)

            # Discover nodes if not in inventory
            nodes = cluster.nodes or await checker.discover_nodes()

            # Host-level checks
            if nodes and self._should("host") and cluster.ssh_cred:
                hc = HostHealthChecker(nodes, cluster.ssh_cred, self.app,
                                       self.log, self.console, cluster.name)
                for sec in await hc.run():
                    result.add_section(sec)
        finally:
            await ssh.close()

    # ── CVIM ─────────────────────────────────────────────────────────────────

    async def _run_cvim(self, cluster: ClusterConfig, result: ClusterResult):
        if not cluster.ssh_cred:
            result.login_success = False
            result.login_error   = "No SSH credentials provided"
            return
        if not cluster.installer_host:
            result.login_success = False
            result.login_error   = "No installer_host specified for CVIM cluster"
            return

        ssh = SSHClient(cluster.installer_host, cluster.ssh_cred, self.app.ssh_timeout, self.log)
        try:
            await ssh.connect()
            self.log.info(f"[{cluster.name}] SSH connected to CVIM installer {cluster.installer_host}")
        except Exception as e:
            result.login_success = False
            result.login_error   = str(e)
            self.log.error(f"[{cluster.name}] SSH failed: {e}")
            return

        try:
            checker = CVIMHealthChecker(cluster, self.app, ssh, self.log, self.console)
            for sec in await checker.run():
                result.add_section(sec)

            nodes = cluster.nodes or await checker.discover_nodes()
            if nodes and self._should("host") and cluster.ssh_cred:
                hc = HostHealthChecker(nodes, cluster.ssh_cred, self.app,
                                       self.log, self.console, cluster.name)
                for sec in await hc.run():
                    result.add_section(sec)
        finally:
            await ssh.close()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _should(self, cat: str) -> bool:
        return self.app.enabled_checks is None or cat in self.app.enabled_checks

    def _write_text_report(self, cluster: ClusterConfig, result: ClusterResult):
        safe = cluster.name.replace("/","_").replace(":","_").replace(" ","_")
        path = self.app.output_dir / f"{safe}_report.txt"
        lines = [
            "="*72,
            f"  ClusterPulse Report — {result.cluster_name}",
            f"  Type   : {result.cluster_type.upper()}",
            f"  Date   : {result.start_time}",
            f"  Status : {result.overall_status.value}",
            f"  PASS:{result.pass_count}  FAIL:{result.fail_count}  WARN:{result.warn_count}",
            "="*72, "",
        ]
        for sec in result.sections:
            lines += [f"\n▶ {sec.name}  [{sec.worst_status.value}]", "-"*60]
            for item in sec.items:
                lines.append(f"  [{item.status.value:5}] {item.message}")
                if item.command:
                    lines.append(f"          CMD: {item.command}")
                if item.detail:
                    for dl in item.detail.strip().splitlines():
                        lines.append(f"              {dl}")
            if sec.raw_log.strip():
                lines += ["\n  --- Raw Log ---"]
                lines += [f"  {l}" for l in sec.raw_log.strip().splitlines()]
        lines += ["", "="*72]
        path.write_text("\n".join(lines), encoding="utf-8")
        self.log.info(f"[{cluster.name}] Text report: {path}")
