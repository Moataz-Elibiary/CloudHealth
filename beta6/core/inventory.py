"""
Beta6 inventory.py — YAML-based cluster/node configuration.

Replaces the Excel/pandas loader from beta5.
Inventory is now a plain YAML file (inventory.yaml) on the central server.
pandas and openpyxl are no longer required.
"""
from __future__ import annotations
import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set


def _expand(path: Optional[str]) -> Optional[str]:
    """Expand ~ and env vars in a path string."""
    return str(Path(path).expanduser()) if path else None


@dataclass
class NodeConfig:
    ip:       str
    username: str
    password: Optional[str] = None
    key_path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "NodeConfig":
        return cls(
            ip       = data.get("ip", ""),
            username = data.get("username", data.get("ssh_user", "")),
            password = data.get("password", data.get("ssh_pass")),
            key_path = _expand(data.get("key_path", data.get("ssh_key"))),
        )

    def to_dict(self) -> dict:
        return {"ip": self.ip, "username": self.username,
                "password": self.password, "key_path": self.key_path}


@dataclass
class ClusterConfig:
    name:         str
    type:         str
    installer_ip: str
    ssh_user:     str
    ssh_pass:     Optional[str] = None
    ssh_key:      Optional[str] = None
    nodes:        List[NodeConfig] = field(default_factory=list)
    enabled:      bool = True
    # Per-cluster threshold overrides (None = use global AppSettings value)
    disk_threshold:     Optional[int]   = None
    mem_used_pct_warn:  Optional[int]   = None
    mem_used_pct_fail:  Optional[int]   = None
    load_ratio_warn:    Optional[float] = None
    load_ratio_fail:    Optional[float] = None
    swap_used_pct_warn: Optional[int]   = None

    @classmethod
    def from_dict(cls, data: dict) -> "ClusterConfig":
        nodes = [NodeConfig.from_dict(n) for n in data.get("nodes", [])]
        return cls(
            name         = data.get("name", ""),
            type         = data.get("type", "").lower(),
            installer_ip = data.get("installer_ip", ""),
            ssh_user     = data.get("ssh_user", ""),
            ssh_pass     = data.get("ssh_pass"),
            ssh_key      = _expand(data.get("ssh_key")),
            nodes        = nodes,
            enabled      = data.get("enabled", True),
            disk_threshold     = data.get("disk_threshold"),
            mem_used_pct_warn  = data.get("mem_used_pct_warn"),
            mem_used_pct_fail  = data.get("mem_used_pct_fail"),
            load_ratio_warn    = data.get("load_ratio_warn"),
            load_ratio_fail    = data.get("load_ratio_fail"),
            swap_used_pct_warn = data.get("swap_used_pct_warn"),
        )

    def to_dict(self) -> dict:
        return {
            "name":         self.name,
            "type":         self.type,
            "installer_ip": self.installer_ip,
            "ssh_user":     self.ssh_user,
            "ssh_pass":     self.ssh_pass,
            "ssh_key":      self.ssh_key,
            "nodes":        [n.to_dict() for n in self.nodes],
            "enabled":      self.enabled,
            "disk_threshold":     self.disk_threshold,
            "mem_used_pct_warn":  self.mem_used_pct_warn,
            "mem_used_pct_fail":  self.mem_used_pct_fail,
            "load_ratio_warn":    self.load_ratio_warn,
            "load_ratio_fail":    self.load_ratio_fail,
            "swap_used_pct_warn": self.swap_used_pct_warn,
        }


@dataclass
class AppSettings:
    parallel_limit:        int   = 5
    max_parallel_nodes:    int   = 10
    ssh_timeout:           int   = 30
    cmd_timeout:           int   = 60
    output_dir:            str   = "~/cloud_health/reports"
    log_dir:               str   = "~/cloud_health/logs"
    db_path:               str   = "~/cloud_health/db/history.db"
    inventory_file:        str   = "~/cloud_health/config/inventory.yaml"
    max_log_files:         int   = 5
    history_max_runs:      int   = 200
    disk_threshold:        int   = 85
    mem_used_pct_warn:     int   = 80
    mem_used_pct_fail:     int   = 95
    load_ratio_warn:       float = 2.0
    load_ratio_fail:       float = 5.0
    swap_used_pct_warn:    int   = 30
    cert_warn_days:        int   = 30
    restart_warn_threshold:int   = 5
    restart_fail_threshold:int   = 20
    pod_age_min_warn:      int   = 5
    pod_age_min_fail:      int   = 2
    enabled_checks:        Optional[Set[str]] = None

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        thr = data.get("thresholds", {})
        ec  = data.get("enabled_checks")
        return cls(
            parallel_limit        = data.get("parallel_limit",        5),
            max_parallel_nodes    = data.get("max_parallel_nodes",    10),
            ssh_timeout           = data.get("ssh_timeout",           30),
            cmd_timeout           = data.get("cmd_timeout",           60),
            output_dir            = data.get("output_dir",            "~/cloud_health/reports"),
            log_dir               = data.get("log_dir",               "~/cloud_health/logs"),
            db_path               = data.get("db_path",               "~/cloud_health/db/history.db"),
            inventory_file        = data.get("inventory_file",        "~/cloud_health/config/inventory.yaml"),
            max_log_files         = data.get("max_log_files",         5),
            history_max_runs      = data.get("history_max_runs",      200),
            disk_threshold        = thr.get("disk_percent",           thr.get("disk_threshold",      85)),
            mem_used_pct_warn     = thr.get("mem_warn",               thr.get("mem_used_pct_warn",   80)),
            mem_used_pct_fail     = thr.get("mem_fail",               thr.get("mem_used_pct_fail",   95)),
            load_ratio_warn       = thr.get("load_warn",              thr.get("load_ratio_warn",    2.0)),
            load_ratio_fail       = thr.get("load_fail",              thr.get("load_ratio_fail",    5.0)),
            swap_used_pct_warn    = thr.get("swap_warn",              thr.get("swap_used_pct_warn",  30)),
            cert_warn_days        = data.get("cert_warn_days",        30),
            restart_warn_threshold= data.get("restart_warn_threshold", 5),
            restart_fail_threshold= data.get("restart_fail_threshold", 20),
            pod_age_min_warn      = data.get("pod_age_min_warn",       5),
            pod_age_min_fail      = data.get("pod_age_min_fail",       2),
            enabled_checks        = set(str(i).strip() for i in ec if str(i).strip()) if ec else None,
        )

    def to_dict(self) -> dict:
        return {
            "parallel_limit":        self.parallel_limit,
            "max_parallel_nodes":    self.max_parallel_nodes,
            "ssh_timeout":           self.ssh_timeout,
            "cmd_timeout":           self.cmd_timeout,
            "output_dir":            self.output_dir,
            "log_dir":               self.log_dir,
            "db_path":               self.db_path,
            "inventory_file":        self.inventory_file,
            "max_log_files":         self.max_log_files,
            "history_max_runs":      self.history_max_runs,
            "disk_threshold":        self.disk_threshold,
            "mem_used_pct_warn":     self.mem_used_pct_warn,
            "mem_used_pct_fail":     self.mem_used_pct_fail,
            "load_ratio_warn":       self.load_ratio_warn,
            "load_ratio_fail":       self.load_ratio_fail,
            "swap_used_pct_warn":    self.swap_used_pct_warn,
            "cert_warn_days":        self.cert_warn_days,
            "restart_warn_threshold":self.restart_warn_threshold,
            "restart_fail_threshold":self.restart_fail_threshold,
            "pod_age_min_warn":      self.pod_age_min_warn,
            "pod_age_min_fail":      self.pod_age_min_fail,
            "enabled_checks":        sorted(self.enabled_checks) if self.enabled_checks else None,
        }


class InventoryLoader:
    """Loads config.yaml and inventory.yaml from the central server."""

    def __init__(self, config_path: str):
        self.config_path = str(Path(config_path).expanduser().resolve())
        self.config_dir  = Path(self.config_path).parent
        self.raw_config  = self._load_yaml(self.config_path)

    def _load_yaml(self, path: str) -> dict:
        p = Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"File not found: {p}")
        with open(p) as f:
            return yaml.safe_load(f) or {}

    def get_app_settings(self) -> AppSettings:
        return AppSettings.from_dict(self.raw_config)

    def load_inventory(self, inventory_path: str) -> List[ClusterConfig]:
        p = Path(inventory_path).expanduser()
        if not p.is_absolute():
            p = (self.config_dir / inventory_path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"Inventory not found: {p}")

        data = self._load_yaml(str(p))
        clusters_raw = data.get("clusters", [])
        if not isinstance(clusters_raw, list):
            raise ValueError(f"inventory.yaml must have a top-level 'clusters' list")

        clusters: List[ClusterConfig] = []
        for entry in clusters_raw:
            if not isinstance(entry, dict):
                continue
            enabled_val = entry.get("enabled", True)
            if str(enabled_val).strip().lower() in ("no", "false", "0", "disabled"):
                continue
            clusters.append(ClusterConfig.from_dict(entry))
        return clusters


def resolve_threshold(cluster: ClusterConfig, attr: str, app: AppSettings):
    """Return per-cluster override if set, otherwise fall back to global AppSettings."""
    v = getattr(cluster, attr, None)
    return v if v is not None else getattr(app, attr)
