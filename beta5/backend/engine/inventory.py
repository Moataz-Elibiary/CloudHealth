"""Beta5 core/inventory.py — Beta3's robust inventory loader verbatim."""
from __future__ import annotations
import os, re, yaml, pandas as pd
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Set
from zipfile import BadZipFile


def _canonical_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


HEADER_ALIASES = {
    "clustername": "cluster_name", "cluster_name": "cluster_name",
    "type": "type", "clustertype": "type",
    "installerip": "installer_ip", "installerhost": "installer_ip",
    "sshuser": "ssh_user", "sshusername": "ssh_user", "user": "ssh_user",
    "sshpass": "ssh_pass", "sshpassword": "ssh_pass", "sshpasskey": "ssh_pass", "pass": "ssh_pass",
    "sshkey": "ssh_key", "sshprivatekey": "ssh_key", "keypath": "ssh_key",
    "enabled": "enabled",
    "nodeips": "node_ips",
    "nodeuser": "node_user", "nodeusername": "node_user",
    "nodepass": "node_pass", "nodepassword": "node_pass", "nodepasskey": "node_pass",
    "diskthreshold": "disk_threshold", "diskpercent": "disk_threshold",
    "memwarn": "mem_used_pct_warn", "memwarnpct": "mem_used_pct_warn",
    "memfail": "mem_used_pct_fail", "memfailpct": "mem_used_pct_fail",
    "loadwarnratio": "load_ratio_warn", "loadfailratio": "load_ratio_fail",
    "swapwarn": "swap_used_pct_warn", "swapwarnpct": "swap_used_pct_warn",
    "nodeip": "node_ip", "ip": "node_ip", "hostname": "node_ip",
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    for col in df.columns:
        canonical = HEADER_ALIASES.get(_canonical_header(col))
        if canonical:
            rename_map[col] = canonical
    return df.rename(columns=rename_map)


def _none_if_blank(value):
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text if text else None


def _int_or_none(value):
    text = _none_if_blank(value)
    return int(float(text)) if text is not None else None


def _float_or_none(value):
    text = _none_if_blank(value)
    return float(text) if text is not None else None


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
            username = data.get("username", ""),
            password = data.get("password"),
            key_path = data.get("key_path"),
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
    # Per-cluster threshold overrides (None = use global)
    disk_threshold:    Optional[int]   = None
    mem_used_pct_warn: Optional[int]   = None
    mem_used_pct_fail: Optional[int]   = None
    load_ratio_warn:   Optional[float] = None
    load_ratio_fail:   Optional[float] = None
    swap_used_pct_warn:Optional[int]   = None

    @classmethod
    def from_dict(cls, data: dict) -> "ClusterConfig":
        nodes = [NodeConfig.from_dict(n) for n in data.get("nodes", [])]
        return cls(
            name         = data.get("name", ""),
            type         = data.get("type", ""),
            installer_ip = data.get("installer_ip", ""),
            ssh_user     = data.get("ssh_user", ""),
            ssh_pass     = data.get("ssh_pass"),
            ssh_key      = data.get("ssh_key"),
            nodes        = nodes,
            enabled      = data.get("enabled", True),
            disk_threshold    = data.get("disk_threshold"),
            mem_used_pct_warn = data.get("mem_used_pct_warn"),
            mem_used_pct_fail = data.get("mem_used_pct_fail"),
            load_ratio_warn   = data.get("load_ratio_warn"),
            load_ratio_fail   = data.get("load_ratio_fail"),
            swap_used_pct_warn= data.get("swap_used_pct_warn"),
        )

    def to_dict(self, *, sanitize: bool = False) -> dict:
        """Serialise for the WebSocket payload.
        sanitize=True strips SSH credentials (used when sending to foreign bastions).
        """
        return {
            "name":         self.name,
            "type":         self.type,
            "installer_ip": self.installer_ip,
            "ssh_user":     self.ssh_user,
            "ssh_pass":     None if sanitize else self.ssh_pass,
            "ssh_key":      None if sanitize else self.ssh_key,
            "nodes":        [n.to_dict() for n in self.nodes],
            "enabled":      self.enabled,
            "disk_threshold":    self.disk_threshold,
            "mem_used_pct_warn": self.mem_used_pct_warn,
            "mem_used_pct_fail": self.mem_used_pct_fail,
            "load_ratio_warn":   self.load_ratio_warn,
            "load_ratio_fail":   self.load_ratio_fail,
            "swap_used_pct_warn":self.swap_used_pct_warn,
        }


@dataclass
class AppSettings:
    parallel_limit:    int   = 5
    max_parallel_nodes:int   = 10
    backend_port:      int   = 8100
    heartbeat_timeout: int   = 60
    output_dir:        str   = "./outputs"
    inventory_file:    str   = "inventory.xlsx"
    ssh_timeout:       int   = 30
    cmd_timeout:       int   = 60
    disk_threshold:    int   = 85
    mem_used_pct_warn: int   = 80
    mem_used_pct_fail: int   = 95
    load_ratio_warn:   float = 2.0
    load_ratio_fail:   float = 5.0
    swap_used_pct_warn:int   = 30
    enabled_checks:           Optional[Set[str]] = None
    verbose:                  bool  = False
    max_log_files:            int   = 5
    history_max_runs:         int   = 200
    cert_warn_days:           int   = 30
    restart_warn_threshold:   int   = 5
    restart_fail_threshold:   int   = 20
    pod_age_min_warn:         int   = 5
    pod_age_min_fail:         int   = 2

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        ec = data.get("enabled_checks")
        return cls(
            parallel_limit    = data.get("parallel_limit",     5),
            max_parallel_nodes= data.get("max_parallel_nodes", 10),
            backend_port      = data.get("backend_port",       8100),
            heartbeat_timeout = data.get("heartbeat_timeout",  60),
            output_dir        = data.get("output_dir",         "./outputs"),
            inventory_file    = data.get("inventory_file",     "inventory.xlsx"),
            ssh_timeout       = data.get("ssh_timeout",        30),
            cmd_timeout       = data.get("cmd_timeout",        60),
            disk_threshold    = data.get("disk_threshold",     85),
            mem_used_pct_warn = data.get("mem_used_pct_warn",  80),
            mem_used_pct_fail = data.get("mem_used_pct_fail",  95),
            load_ratio_warn   = data.get("load_ratio_warn",    2.0),
            load_ratio_fail   = data.get("load_ratio_fail",    5.0),
            swap_used_pct_warn= data.get("swap_used_pct_warn", 30),
            enabled_checks           = set(ec) if ec else None,
            verbose                  = data.get("verbose",                  False),
            max_log_files            = data.get("max_log_files",            5),
            history_max_runs         = data.get("history_max_runs",         200),
            cert_warn_days           = data.get("cert_warn_days",           30),
            restart_warn_threshold   = data.get("restart_warn_threshold",   5),
            restart_fail_threshold   = data.get("restart_fail_threshold",   20),
            pod_age_min_warn         = data.get("pod_age_min_warn",         5),
            pod_age_min_fail         = data.get("pod_age_min_fail",         2),
        )

    def to_dict(self) -> dict:
        return {
            "parallel_limit":    self.parallel_limit,
            "max_parallel_nodes":self.max_parallel_nodes,
            "backend_port":      self.backend_port,
            "heartbeat_timeout": self.heartbeat_timeout,
            "output_dir":        self.output_dir,
            "inventory_file":    self.inventory_file,
            "ssh_timeout":       self.ssh_timeout,
            "cmd_timeout":       self.cmd_timeout,
            "disk_threshold":    self.disk_threshold,
            "mem_used_pct_warn": self.mem_used_pct_warn,
            "mem_used_pct_fail": self.mem_used_pct_fail,
            "load_ratio_warn":   self.load_ratio_warn,
            "load_ratio_fail":   self.load_ratio_fail,
            "swap_used_pct_warn":self.swap_used_pct_warn,
            # Always serialise as sorted list for JSON compatibility
            "enabled_checks":          sorted(self.enabled_checks) if self.enabled_checks else None,
            "verbose":                 self.verbose,
            "max_log_files":           self.max_log_files,
            "history_max_runs":        self.history_max_runs,
            "cert_warn_days":          self.cert_warn_days,
            "restart_warn_threshold":  self.restart_warn_threshold,
            "restart_fail_threshold":  self.restart_fail_threshold,
            "pod_age_min_warn":        self.pod_age_min_warn,
            "pod_age_min_fail":        self.pod_age_min_fail,
        }


class InventoryLoader:
    def __init__(self, config_path: str):
        self.config_path = str(Path(config_path).resolve())
        self.config_dir  = Path(self.config_path).parent
        self.raw_config  = self._load_yaml(self.config_path)

    def _load_yaml(self, path: str) -> dict:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config not found: {path}")
        with open(path) as f:
            return yaml.safe_load(f) or {}

    def get_app_settings(self) -> AppSettings:
        cfg = self.raw_config
        thr = cfg.get("thresholds", {})
        par = cfg.get("parallelism", {})
        ec  = cfg.get("enabled_checks")
        if ec:
            ec = set(str(i).strip() for i in ec if str(i).strip())
        return AppSettings(
            parallel_limit    = cfg.get("parallel_limit",     par.get("max_parallel_clusters", 5)),
            max_parallel_nodes= cfg.get("max_parallel_nodes", par.get("max_parallel_nodes",    10)),
            backend_port      = cfg.get("backend_port",       8100),
            heartbeat_timeout = cfg.get("heartbeat_timeout",  60),
            output_dir        = cfg.get("output_dir",         "./outputs"),
            inventory_file    = cfg.get("inventory_file",     "inventory.xlsx"),
            ssh_timeout       = cfg.get("ssh_timeout",        par.get("ssh_timeout", 30)),
            cmd_timeout       = cfg.get("cmd_timeout",        par.get("cmd_timeout", 60)),
            disk_threshold    = thr.get("disk_percent",       thr.get("disk_pct",           85)),
            mem_used_pct_warn = thr.get("mem_warn",           thr.get("mem_used_pct_warn",   80)),
            mem_used_pct_fail = thr.get("mem_fail",           thr.get("mem_used_pct_fail",   95)),
            load_ratio_warn   = thr.get("load_warn",          thr.get("load_ratio_warn",    2.0)),
            load_ratio_fail   = thr.get("load_fail",          thr.get("load_ratio_fail",    5.0)),
            swap_used_pct_warn= thr.get("swap_warn",          thr.get("swap_used_pct_warn",  30)),
            enabled_checks           = ec,
            max_log_files            = cfg.get("max_log_files",            5),
            history_max_runs         = cfg.get("history_max_runs",         200),
            cert_warn_days           = cfg.get("cert_warn_days",           30),
            restart_warn_threshold   = cfg.get("restart_warn_threshold",   5),
            restart_fail_threshold   = cfg.get("restart_fail_threshold",   20),
            pod_age_min_warn         = cfg.get("pod_age_min_warn",         5),
            pod_age_min_fail         = cfg.get("pod_age_min_fail",         2),
        )

    def load_inventory(self, excel_filename: str) -> List[ClusterConfig]:
        excel_path = (excel_filename if os.path.isabs(excel_filename)
                      else str(self.config_dir / excel_filename))
        if not os.path.exists(excel_path):
            raise FileNotFoundError(f"Inventory not found: {excel_path}")
        try:
            wb = pd.ExcelFile(excel_path)
        except BadZipFile as exc:
            raise ValueError(f"Not a valid .xlsx file: {excel_path}") from exc

        sheet = "Clusters" if "Clusters" in wb.sheet_names else wb.sheet_names[0]
        # Auto-detect header row: handles decorated Excel with banner rows above headers
        raw_df = pd.read_excel(wb, sheet_name=sheet, header=None)
        header_row = 0
        for i, row in raw_df.iterrows():
            for cell in row.values:
                if _canonical_header(str(cell)) in ("clustername", "cluster_name"):
                    header_row = int(i)
                    break
            if header_row:
                break
        df = _normalize_columns(pd.read_excel(wb, sheet_name=sheet, header=header_row))

        # Optional Nodes sheet
        nodes_map: Dict[str, List[NodeConfig]] = {}
        if "Nodes" in wb.sheet_names:
            df_n = _normalize_columns(pd.read_excel(wb, sheet_name="Nodes"))
            for _, row in df_n.iterrows():
                cname = _none_if_blank(row.get("cluster_name"))
                if not cname:
                    continue
                nodes_map.setdefault(cname, []).append(NodeConfig(
                    ip       = _none_if_blank(row.get("node_ip")) or "",
                    username = _none_if_blank(row.get("ssh_user")) or "root",
                    password = _none_if_blank(row.get("ssh_pass")),
                    key_path = _none_if_blank(row.get("ssh_key")),
                ))

        clusters: List[ClusterConfig] = []
        for _, row in df.iterrows():
            name = _none_if_blank(row.get("cluster_name"))
            if not name:
                continue
            enabled_val = row.get("enabled", "yes")
            if str(enabled_val).strip().lower() in ("no", "false", "0", "disabled"):
                continue

            node_configs = nodes_map.get(name, [])
            if not node_configs and pd.notna(row.get("node_ips")):
                node_configs = [
                    NodeConfig(
                        ip       = ip.strip(),
                        username = _none_if_blank(row.get("node_user")) or "root",
                        password = _none_if_blank(row.get("node_pass")),
                    )
                    for ip in str(row["node_ips"]).split(",") if ip.strip()
                ]

            clusters.append(ClusterConfig(
                name         = name,
                type         = (_none_if_blank(row.get("type")) or "").lower(),
                installer_ip = _none_if_blank(row.get("installer_ip")) or "",
                ssh_user     = _none_if_blank(row.get("ssh_user")) or "",
                ssh_pass     = _none_if_blank(row.get("ssh_pass")),
                ssh_key      = _none_if_blank(row.get("ssh_key")),
                nodes        = node_configs,
                disk_threshold    = _int_or_none(row.get("disk_threshold")),
                mem_used_pct_warn = _int_or_none(row.get("mem_used_pct_warn")),
                mem_used_pct_fail = _int_or_none(row.get("mem_used_pct_fail")),
                load_ratio_warn   = _float_or_none(row.get("load_ratio_warn")),
                load_ratio_fail   = _float_or_none(row.get("load_ratio_fail")),
                swap_used_pct_warn= _int_or_none(row.get("swap_used_pct_warn")),
            ))
        return clusters


def resolve_threshold(cluster: ClusterConfig, attr: str, app: AppSettings):
    v = getattr(cluster, attr, None)
    return v if v is not None else getattr(app, attr)
