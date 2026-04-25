import asyncio
from datetime import datetime
from typing import Callable

# Internal Modular Imports
from result import ClusterResult, SectionResult
from ssh_client import LocalClient
from core.inventory import AppSettings, ClusterConfig, NodeConfig

from checks.cvim_checks import CVIMHealthChecker
from checks.host_checks import HostHealthChecker
from checks.ocp_checks import OCPHealthChecker

HOST_CHECK_IDS = {
    "host",
    "uptime",
    "os_info",
    "cpu",
    "memory",
    "disk",
    "ecc",
    "host_network",
    "bond",
    "sriov",
    "kernel_msgs",
    "services",
    "ntp",
    "pcie",
    "firmware",
    "numa",
    "hugepages",
    "selinux",
    "firewall",
    "ports",
}


class CheckRunner:
    """Orchestrates health checks on a single cluster locally from the bastion."""
    
    def __init__(self, cluster_config: dict, app_settings: dict, 
                 on_headline: Callable[[str], None] = None,
                 on_result: Callable[[dict], None] = None):
        
        # Rehydrate dataclasses from dictionaries
        self.cluster = ClusterConfig.from_dict(cluster_config)
        self.app = AppSettings.from_dict(app_settings)
        self.on_headline = on_headline
        self.on_result = on_result
        self.enabled_checks = self.app.enabled_checks
        self.client = LocalClient(self.cluster.installer_ip, self.cluster.ssh_user)

    async def run(self) -> ClusterResult:
        res = ClusterResult(
            cluster_name=self.cluster.name,
            cluster_type=self.cluster.type,
        )
        
        if self.on_headline:
            await self.on_headline(f"Starting diagnostics for {self.cluster.name}...")

        checker = self._build_checker()
        sections = await checker.run()
        
        for sec in sections:
            res.sections.append(sec)
            if self.on_result:
                await self.on_result(self._serialize_section(sec))

        if not self.cluster.nodes and hasattr(checker, "discover_nodes"):
            discovered_nodes = await checker.discover_nodes()
            if discovered_nodes:
                default_user = "core" if self.cluster.type == "ocp" else "root"
                self.cluster.nodes = [NodeConfig(ip=ip, username=default_user) for ip in discovered_nodes]
                if self.on_headline:
                    await self.on_headline(
                        f"Discovered {len(self.cluster.nodes)} node(s) for host checks on {self.cluster.name}."
                    )

        if self.cluster.nodes and self._should_run_host_checks():
            host_checker = HostHealthChecker(
                nodes=self.cluster.nodes,
                app=self.app,
                cluster_name=self.cluster.name,
            )
            host_sections = await host_checker.run()
            for sec in host_sections:
                res.sections.append(sec)
                if self.on_result:
                    await self.on_result(self._serialize_section(sec))

        res.end_time = datetime.now()
        return res

    def _build_checker(self):
        if self.cluster.type == "ocp":
            return OCPHealthChecker(
                ssh=self.client,
                app=self.app,
                cluster=self.cluster,
                enabled_checks=self.enabled_checks,
            )
        if self.cluster.type == "cvim":
            return CVIMHealthChecker(
                ssh=self.client,
                app=self.app,
                cluster=self.cluster,
                enabled_checks=self.enabled_checks,
            )
        raise ValueError(f"Unsupported cluster type: {self.cluster.type}")

    def _should_run_host_checks(self) -> bool:
        if self.enabled_checks is None:
            return True
        return any(check in HOST_CHECK_IDS for check in self.enabled_checks)

    def _serialize_section(self, sec: SectionResult):
        return {
            "name": sec.name,
            "category": sec.category,
            "status": sec.status.value,
            "checks": [
                {
                    "message": item.message,
                    "status": item.status.value,
                    "detail": item.detail,
                    "command": item.command,
                }
                for item in sec.checks
            ],
        }
