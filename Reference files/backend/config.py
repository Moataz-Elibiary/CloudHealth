"""
Backend config dataclasses.
The backend receives its AppConfig as JSON from the frontend
via the WebSocket start_checks message. No YAML loading on bastion.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SSHCred:
    username:    str
    password:    Optional[str] = None
    private_key: Optional[str] = None
    passphrase:  Optional[str] = None
    port:        int = 22

    def __post_init__(self):
        if not self.password and not self.private_key:
            raise ValueError("SSH credential needs 'password' or 'private_key'.")

    @classmethod
    def from_dict(cls, d: dict) -> "SSHCred":
        return cls(
            username    = d["username"],
            password    = d.get("password"),
            private_key = d.get("private_key"),
            passphrase  = d.get("passphrase"),
            port        = int(d.get("port", 22)),
        )


@dataclass
class APICred:
    token:      Optional[str] = None
    username:   Optional[str] = None
    password:   Optional[str] = None
    verify_ssl: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "APICred":
        return cls(
            token      = d.get("token"),
            username   = d.get("username"),
            password   = d.get("password"),
            verify_ssl = d.get("verify_ssl", True),
        )


@dataclass
class ClusterConfig:
    name:             str
    cluster_type:     str
    environment:      str = ""
    description:      str = ""
    installer_host:   Optional[str] = None
    api_url:          Optional[str] = None
    nodes:            List[str] = field(default_factory=list)
    ssh_cred:         Optional[SSHCred] = None
    api_cred:         Optional[APICred] = None
    disk_threshold:          Optional[int]   = None
    restart_warn_threshold:  Optional[int]   = None
    restart_fail_threshold:  Optional[int]   = None
    pod_age_min_warn:        Optional[int]   = None
    pod_age_min_fail:        Optional[int]   = None
    tags:             Dict[str, str] = field(default_factory=dict)
    enabled:          bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "ClusterConfig":
        ssh_d  = d.get("ssh_cred")
        api_d  = d.get("api_cred")
        return cls(
            name           = d["name"],
            cluster_type   = d["cluster_type"],
            environment    = d.get("environment", ""),
            description    = d.get("description", ""),
            installer_host = d.get("installer_host"),
            api_url        = d.get("api_url"),
            nodes          = d.get("nodes", []),
            ssh_cred       = SSHCred.from_dict(ssh_d) if ssh_d else None,
            api_cred       = APICred.from_dict(api_d) if api_d else None,
            disk_threshold         = d.get("disk_threshold"),
            restart_warn_threshold = d.get("restart_warn_threshold"),
            restart_fail_threshold = d.get("restart_fail_threshold"),
            pod_age_min_warn       = d.get("pod_age_min_warn"),
            pod_age_min_fail       = d.get("pod_age_min_fail"),
            tags           = d.get("tags", {}),
        )


@dataclass
class AppConfig:
    """Sent from frontend → backend in the start_checks WS message."""
    inventory_path:        str  = ""
    max_parallel_clusters: int  = 5
    max_parallel_nodes:    int  = 10
    ssh_timeout:           int  = 30
    cmd_timeout:           int  = 60
    enabled_checks:        Optional[List[str]] = None
    disk_threshold:         int   = 80
    restart_warn_threshold: int   = 10
    restart_fail_threshold: int   = 50
    pod_age_min_warn:       int   = 5
    pod_age_min_fail:       int   = 2
    cert_warn_days:         int   = 30
    load_ratio_warn:        float = 1.0
    load_ratio_fail:        float = 2.0
    mem_used_pct_warn:      int   = 80
    mem_used_pct_fail:      int   = 90
    swap_used_pct_warn:     int   = 50
    clusters:               List[ClusterConfig] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig":
        cfg = cls(
            max_parallel_clusters = d.get("max_parallel_clusters", 5),
            max_parallel_nodes    = d.get("max_parallel_nodes",    10),
            ssh_timeout           = d.get("ssh_timeout",           30),
            cmd_timeout           = d.get("cmd_timeout",           60),
            enabled_checks        = d.get("enabled_checks"),
            disk_threshold        = d.get("disk_threshold",        80),
            restart_warn_threshold= d.get("restart_warn_threshold",10),
            restart_fail_threshold= d.get("restart_fail_threshold",50),
            pod_age_min_warn      = d.get("pod_age_min_warn",       5),
            pod_age_min_fail      = d.get("pod_age_min_fail",       2),
            cert_warn_days        = d.get("cert_warn_days",        30),
            load_ratio_warn       = d.get("load_ratio_warn",      1.0),
            load_ratio_fail       = d.get("load_ratio_fail",      2.0),
            mem_used_pct_warn     = d.get("mem_used_pct_warn",    80),
            mem_used_pct_fail     = d.get("mem_used_pct_fail",    90),
            swap_used_pct_warn    = d.get("swap_used_pct_warn",   50),
        )
        cfg.clusters = [ClusterConfig.from_dict(c) for c in d.get("clusters", [])]
        return cfg


def resolve_threshold(cluster_val: Optional[int], global_val: int) -> int:
    return cluster_val if cluster_val is not None else global_val
