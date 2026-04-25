"""Health Check Engine — parallel orchestration with login failure handling."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime
from typing import List, Optional, Set

from core.inventory import ClusterConfig, AppSettings, resolve_threshold
from core.models import ClusterResult, SectionResult, CheckItem, Status
from core.ssh import SSHClient
from checks.host_checks import HostHealthChecker
from checks.ocp_checks import OCPHealthChecker
from checks.cvim_checks import CVIMHealthChecker


class HealthCheckEngine:
    """
    Orchestrates the health check process across multiple clusters and nodes.
    Supports parallel execution and real-time reporting via a console or UI.
    """
    def __init__(self, clusters: List[ClusterConfig], app: AppSettings,
                 logger: logging.Logger, console=None,
                 enabled_checks: Optional[Set[str]] = None):
        """
        Initialize the engine.
        
        Args:
            clusters: List of cluster configurations to check.
            app: Global application settings.
            logger: System logger.
            console: Optional reporter (ConsoleReporter or WSReporter).
            enabled_checks: Optional filter for specific check IDs.
        """
        self.clusters = clusters
        self.app = app
        self.logger = logger
        self.console = console
        self.enabled_checks = enabled_checks
        self.semaphore = asyncio.Semaphore(app.parallel_limit)

    async def run(self) -> List[ClusterResult]:
        """Runs diagnostics on all clusters in parallel (respecting parallel_limit)."""
        tasks = [self._check_cluster_guarded(c) for c in self.clusters]
        return await asyncio.gather(*tasks)

    async def _check_cluster_guarded(self, cluster: ClusterConfig) -> ClusterResult:
        """Executes cluster check within the semaphore to control parallelism."""
        async with self.semaphore:
            return await self._check_cluster(cluster)

    async def _check_cluster(self, cluster: ClusterConfig) -> ClusterResult:
        """
        Core logic for a single cluster:
        1. Connects via SSH to the installer/bastion.
        2. Runs cluster-level diagnostics (OCP or CVIM).
        3. Discovers nodes if necessary.
        4. Runs host-level diagnostics on all nodes in parallel.
        """
        result = ClusterResult(

            cluster_name=cluster.name,
            cluster_type=cluster.type,
            start_time=datetime.now(),
        )
        if self.console:
            self.console.cluster_start(cluster.name, cluster.type)

        # ── Step 1: Connect to Installer/Bastion ──
        ssh = SSHClient(
            host=cluster.installer_ip,
            username=cluster.ssh_user,
            password=cluster.ssh_pass,
            key_path=cluster.ssh_key,
            timeout=self.app.ssh_timeout,
            logger=self.logger,
        )
        try:
            await ssh.connect()
        except Exception as e:
            result.login_success = False
            result.login_error = str(e)
            self.logger.error(f"[{cluster.name}] SSH login failed: {e}")
            sec = SectionResult("Login", "login", start_time=datetime.now())
            sec.fail(f"SSH connection to {cluster.installer_ip} failed: {e}")
            sec.end_time = datetime.now()
            if self.console:
                self.console.section_done(sec)
            result.sections.append(sec)
            result.end_time = datetime.now()
            if self.console:
                self.console.cluster_done(result)
            return result

        # ── Step 2: Cluster-level checks ──
        try:
            if cluster.type == 'ocp':
                checker = OCPHealthChecker(
                    ssh=ssh, app=self.app, cluster=cluster,
                    logger=self.logger, console=self.console,
                    enabled_checks=self.enabled_checks,
                )
                sections = await checker.run()
                result.sections.extend(sections)

                # Auto-discover nodes if none in inventory
                if not cluster.nodes:
                    discovered = await checker.discover_nodes()
                    if discovered:
                        self.logger.info(f"[{cluster.name}] Auto-discovered {len(discovered)} nodes")
                        from core.inventory import NodeConfig
                        cluster.nodes = [NodeConfig(ip=n, username='core') for n in discovered]

            elif cluster.type == 'cvim':
                checker = CVIMHealthChecker(
                    ssh=ssh, app=self.app, cluster=cluster,
                    logger=self.logger, console=self.console,
                    enabled_checks=self.enabled_checks,
                )
                sections = await checker.run()
                result.sections.extend(sections)

                # Auto-discover nodes if none in inventory
                if not cluster.nodes:
                    discovered = await checker.discover_nodes()
                    if discovered:
                        self.logger.info(f"[{cluster.name}] Auto-discovered {len(discovered)} nodes")
                        from core.inventory import NodeConfig
                        cluster.nodes = [NodeConfig(ip=n, username='root') for n in discovered]

        except Exception as e:
            self.logger.exception(f"[{cluster.name}] Cluster check error")
            sec = SectionResult("Cluster Check Error", "error", start_time=datetime.now())
            sec.error(f"Unexpected error: {e}")
            sec.end_time = datetime.now()
            result.sections.append(sec)

        await ssh.close()

        # ── Step 3: Node-level host checks (parallel) ──
        if cluster.nodes:
            host_checker = HostHealthChecker(
                nodes=cluster.nodes, app=self.app,
                logger=self.logger, console=self.console,
                cluster_name=cluster.name,
            )
            node_sections = await host_checker.run()
            result.sections.extend(node_sections)

        result.end_time = datetime.now()
        if self.console:
            self.console.cluster_done(result)
        return result
