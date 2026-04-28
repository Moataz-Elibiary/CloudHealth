"""
Beta4 CheckRunner

Key fix over both betas:
  Sections are wired (_queue, _cluster_name) BEFORE each check function
  is called, not after run() returns. This ensures every sec.pass_()/fail_()
  call inside the check function pushes to the queue immediately.

Architecture: one CheckRunner per cluster per WebSocket connection.
The on_headline and on_result callbacks are async; the runner awaits them.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional, Set

from result import ClusterResult, SectionResult
from ssh_client import LocalClient, SSHClient
from engine.inventory import AppSettings, ClusterConfig, NodeConfig

from checks.cvim_checks import CVIMHealthChecker
from checks.host_checks  import HostHealthChecker
from checks.ocp_checks   import OCPHealthChecker


class _NullConsole:
    """No-op console used when no rich/curses UI is attached (backend mode)."""
    def section_start(self, name): pass
    def section_done(self, sec): pass

HOST_CHECK_IDS = {
    "host", "uptime", "os_info", "cpu", "memory", "disk", "ecc",
    "host_network", "bond", "sriov", "kernel_msgs", "services",
    "ntp", "pcie", "firmware", "numa", "hugepages", "selinux",
    "firewall", "ports",
}


class CheckRunner:
    """
    Orchestrates health checks on a single cluster from the bastion.
    Accepts async callbacks for headline messages and completed section dicts.
    """

    def __init__(
        self,
        cluster_config: dict,
        app_settings:   dict,
        on_headline:    Optional[Callable] = None,
        on_result:      Optional[Callable] = None,
        # Per-connection queue — wired into each Section for item-level streaming
        subscriber_queue: Optional[asyncio.Queue] = None,
    ):
        self.cluster          = ClusterConfig.from_dict(cluster_config)
        self.app              = AppSettings.from_dict(app_settings)
        self.on_headline      = on_headline
        self.on_result        = on_result
        self.enabled_checks   = self.app.enabled_checks
        self.subscriber_queue = subscriber_queue
        self.client           = LocalClient(
            self.cluster.installer_ip, self.cluster.ssh_user)
        self.log = logging.getLogger(f"cloud_health.{self.cluster.name}")
        self.con = _NullConsole()

    def _wire(self, sec: SectionResult) -> SectionResult:
        """Wire queue, cluster name, event loop, and commands logger onto a section BEFORE running fn()."""
        sec._queue           = self.subscriber_queue
        sec._cluster_name    = self.cluster.name
        sec._loop            = asyncio.get_running_loop()
        sec._commands_logger = logging.getLogger(
            f"commands.{self.cluster.name}.{sec.name}")
        return sec

    async def run(self) -> ClusterResult:
        res = ClusterResult(
            cluster_name = self.cluster.name,
            cluster_type = self.cluster.type,
        )

        if self.on_headline:
            await self.on_headline(
                f"Starting diagnostics for {self.cluster.name}...")

        checker = self._build_checker()

        # Run cluster-level checks (OCP or CVIM)
        # Each check function receives an already-wired Section.
        sections = await self._run_checker(checker)
        for sec in sections:
            res.sections.append(sec)
            if self.on_result:
                await self.on_result(self._serialize_section(sec))

        # Auto-discover nodes if none specified
        if not self.cluster.nodes and hasattr(checker, "discover_nodes"):
            discovered = await checker.discover_nodes()
            if discovered:
                default_user = "core" if self.cluster.type == "ocp" else "root"
                self.cluster.nodes = [
                    NodeConfig(ip=ip, username=default_user)
                    for ip in discovered
                ]
                if self.on_headline:
                    await self.on_headline(
                        f"Discovered {len(self.cluster.nodes)} node(s) "
                        f"for host checks on {self.cluster.name}.")

        # Host-level checks (real SSH per node)
        if self.cluster.nodes and self._should_run_host_checks():
            host_checker = HostHealthChecker(
                nodes        = self.cluster.nodes,
                app          = self.app,
                cluster_name = self.cluster.name,
                subscriber_queue = self.subscriber_queue,
            )
            host_sections = await host_checker.run()
            for sec in host_sections:
                res.sections.append(sec)
                if self.on_result:
                    await self.on_result(self._serialize_section(sec))

        res.end_time = datetime.now()
        return res

    async def _run_checker(self, checker) -> list:
        """
        Run the checker, wiring each SectionResult BEFORE fn() executes.
        This replaces the post-run monkey-patching antipattern from Beta2.
        """
        # Inject our wiring hook into the checker before run()
        checker._wire_section = self._wire
        return await checker.run()

    def _build_checker(self):
        if self.cluster.type == "ocp":
            return OCPHealthChecker(
                ssh     = self.client,
                app     = self.app,
                cluster = self.cluster,
                logger  = self.log,
                console = self.con,
            )
        if self.cluster.type == "cvim":
            return CVIMHealthChecker(
                ssh     = self.client,
                app     = self.app,
                cluster = self.cluster,
                logger  = self.log,
                console = self.con,
            )
        raise ValueError(f"Unsupported cluster type: {self.cluster.type!r}")

    def _should_run_host_checks(self) -> bool:
        if self.enabled_checks is None:
            return True
        return any(c in HOST_CHECK_IDS for c in self.enabled_checks)

    def _serialize_section(self, sec: SectionResult) -> dict:
        return {
            "name":     sec.name,
            "category": sec.category,
            "status":   sec.status.value,
            "checks": [
                {
                    "message": item.message,
                    "status":  item.status.value,
                    "detail":  item.detail,
                    "command": item.command,
                }
                for item in sec.checks
            ],
        }
