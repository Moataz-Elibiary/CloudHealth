"""
Beta7 CheckRunner — headless, no WebSocket streaming.

Runs all checks for one cluster synchronously (from the caller's perspective).
Results are collected in memory and returned as a ClusterResult.

Uses system ssh subprocess (SSHClient) instead of paramiko.
Node checks jump through the bastion via ssh -J (no persistent transport needed).
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime
from core.result import ClusterResult, SectionResult
from core.ssh_client import SSHClient
from core.inventory import AppSettings, ClusterConfig, NodeConfig

from core.checks.cvim_checks import CVIMHealthChecker
from core.checks.host_checks  import HostHealthChecker
from core.checks.ocp_checks   import OCPHealthChecker


class _NullConsole:
    def section_start(self, name): pass
    def section_done(self, sec):   pass


HOST_CHECK_IDS = {
    "host", "uptime", "os_info", "cpu", "memory", "disk", "ecc",
    "host_network", "bond", "sriov", "kernel_msgs", "services",
    "ntp", "pcie", "firmware", "numa", "hugepages", "selinux",
    "firewall", "ports",
}


class CheckRunner:
    """
    Orchestrates health checks on a single cluster from the central server.
    Runs OCP/CVIM checks via ssh to the bastion, then runs host checks via
    ssh -J bastion to each node.
    """

    def __init__(
        self,
        cluster_config: dict,
        app_settings:   dict,
    ):
        self.cluster = ClusterConfig.from_dict(cluster_config)
        self.app     = AppSettings.from_dict(app_settings)
        self.log     = logging.getLogger(f"cloudhealth.{self.cluster.name}")
        self.con     = _NullConsole()

        self.bastion = SSHClient(
            host     = self.cluster.installer_ip,
            username = self.cluster.ssh_user,
            key_path = self.cluster.ssh_key,
            timeout  = self.app.ssh_timeout,
            logger   = self.log,
        )
        # jump_host string used by NodeClient to reach nodes through the bastion
        self.jump_host = f"{self.cluster.ssh_user}@{self.cluster.installer_ip}"

    def _wire(self, sec: SectionResult) -> SectionResult:
        sec._commands_logger = logging.getLogger(
            f"commands.{self.cluster.name}.{sec.name}")
        return sec

    async def run(self) -> ClusterResult:
        res = ClusterResult(
            cluster_name = self.cluster.name,
            cluster_type = self.cluster.type,
        )

        # Verify bastion is reachable before running any checks
        self.log.info("Verifying SSH to %s (%s)", self.cluster.name, self.cluster.installer_ip)
        probe = await self.bastion.run("echo __ok__", timeout=self.app.ssh_timeout)
        if not probe.ok:
            self.log.error("SSH to %s failed: %s", self.cluster.installer_ip, probe.stderr)
            res.login_success = False
            res.login_error   = probe.stderr.strip() or f"exit={probe.exit_code}"
            res.end_time      = datetime.now()
            return res

        try:
            checker = self._build_checker()
            self.log.info("Running %s checks on %s", self.cluster.type.upper(), self.cluster.name)
            sections = await self._run_checker(checker)
            for sec in sections:
                res.sections.append(sec)

            # Auto-discover nodes if none specified in inventory
            if not self.cluster.nodes and hasattr(checker, "discover_nodes"):
                discovered = await checker.discover_nodes()
                if discovered:
                    default_user = "core" if self.cluster.type == "ocp" else "root"
                    self.cluster.nodes = [
                        NodeConfig(ip=ip, username=default_user)
                        for ip in discovered
                    ]
                    self.log.info("Discovered %d node(s) for host checks on %s",
                                  len(self.cluster.nodes), self.cluster.name)

            # Host checks via ssh -J bastion → node
            if self.cluster.nodes and self._should_run_host_checks():
                host_checker = HostHealthChecker(
                    nodes        = self.cluster.nodes,
                    app          = self.app,
                    cluster_name = self.cluster.name,
                    cluster      = self.cluster,
                    jump_host    = self.jump_host,
                    logger       = self.log,
                    console      = self.con,
                )
                host_sections = await host_checker.run()
                for sec in host_sections:
                    res.sections.append(sec)

        except asyncio.CancelledError:
            self.log.warning("Run cancelled for %s — saving partial results", self.cluster.name)
            raise

        res.end_time = datetime.now()
        self.log.info(
            "%s done — %d pass, %d fail, %d warn",
            self.cluster.name, res.pass_count, res.fail_count, res.warn_count,
        )
        return res

    async def _run_checker(self, checker) -> list:
        checker._wire_section = self._wire
        return await checker.run()

    def _build_checker(self):
        if self.cluster.type == "ocp":
            return OCPHealthChecker(
                ssh     = self.bastion,
                app     = self.app,
                cluster = self.cluster,
                logger  = self.log,
                console = self.con,
            )
        if self.cluster.type == "cvim":
            return CVIMHealthChecker(
                ssh     = self.bastion,
                app     = self.app,
                cluster = self.cluster,
                logger  = self.log,
                console = self.con,
            )
        raise ValueError(f"Unsupported cluster type: {self.cluster.type!r}")

    def _should_run_host_checks(self) -> bool:
        ec = self.app.enabled_host_checks
        if ec is None:
            return True
        return any(c in HOST_CHECK_IDS for c in ec)

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
