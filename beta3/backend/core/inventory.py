"""Inventory loader with per-cluster thresholds and check filter support."""
import os
import re
import yaml
import pandas as pd
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Set
from zipfile import BadZipFile


def _canonical_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


HEADER_ALIASES = {
    "clustername": "cluster_name",
    "cluster_name": "cluster_name",
    "type": "type",
    "clustertype": "type",
    "installerip": "installer_ip",
    "installerhost": "installer_ip",
    "sshuser": "ssh_user",
    "sshusername": "ssh_user",
    "sshpass": "ssh_pass",
    "sshpassword": "ssh_pass",
    "sshpasskey": "ssh_pass",
    "sshkey": "ssh_key",
    "sshprivatekey": "ssh_key",
    "enabled": "enabled",
    "nodeips": "node_ips",
    "nodeuser": "node_user",
    "nodeusername": "node_user",
    "nodepass": "node_pass",
    "nodepassword": "node_pass",
    "nodepasskey": "node_pass",
    "diskthreshold": "disk_threshold",
    "diskpercent": "disk_threshold",
    "memwarn": "mem_used_pct_warn",
    "memwarnpct": "mem_used_pct_warn",
    "memfail": "mem_used_pct_fail",
    "memfailpct": "mem_used_pct_fail",
    "loadwarnratio": "load_ratio_warn",
    "loadfailratio": "load_ratio_fail",
    "swapwarn": "swap_used_pct_warn",
    "swapwarnpct": "swap_used_pct_warn",
    "nodeip": "node_ip",
    "ip": "node_ip",
    "hostname": "node_ip",
    "user": "ssh_user",
    "pass": "ssh_pass",
    "keypath": "ssh_key",
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    for column in df.columns:
        canonical = HEADER_ALIASES.get(_canonical_header(column))
        if canonical:
            rename_map[column] = canonical
    return df.rename(columns=rename_map)


def _none_if_blank(value):
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text if text else None


def _int_or_none(value):
    text = _none_if_blank(value)
    if text is None:
        return None
    return int(float(text))


def _float_or_none(value):
    text = _none_if_blank(value)
    if text is None:
        return None
    return float(text)


@dataclass
class NodeConfig:
    """Configuration for a single host/node."""
    ip: str
    username: str
    password: Optional[str] = None
    key_path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            ip=data.get('ip', ''),
            username=data.get('username', ''),
            password=data.get('password'),
            key_path=data.get('key_path')
        )


@dataclass
class ClusterConfig:
    """Configuration for a cluster (OCP or CVIM) including connection and thresholds."""
    name: str
    type: str           # 'ocp' or 'cvim'
    installer_ip: str
    ssh_user: str
    ssh_pass: Optional[str] = None
    ssh_key: Optional[str] = None
    nodes: List[NodeConfig] = field(default_factory=list)
    enabled: bool = True
    # Per-cluster threshold overrides
    disk_threshold: Optional[int] = None
    mem_used_pct_warn: Optional[int] = None
    mem_used_pct_fail: Optional[int] = None
    load_ratio_warn: Optional[float] = None
    load_ratio_fail: Optional[float] = None
    swap_used_pct_warn: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict):
        nodes_data = data.get('nodes', [])
        nodes = [NodeConfig.from_dict(n) for n in nodes_data]
        return cls(
            name=data.get('name', ''),
            type=data.get('type', ''),
            installer_ip=data.get('installer_ip', ''),
            ssh_user=data.get('ssh_user', ''),
            ssh_pass=data.get('ssh_pass'),
            ssh_key=data.get('ssh_key'),
            nodes=nodes,
            enabled=data.get('enabled', True),
            disk_threshold=data.get('disk_threshold'),
            mem_used_pct_warn=data.get('mem_used_pct_warn'),
            mem_used_pct_fail=data.get('mem_used_pct_fail'),
            load_ratio_warn=data.get('load_ratio_warn'),
            load_ratio_fail=data.get('load_ratio_fail'),
            swap_used_pct_warn=data.get('swap_used_pct_warn')
        )


@dataclass
class AppSettings:
    """Global application settings with defaults."""
    parallel_limit: int = 5
    max_parallel_nodes: int = 10
    backend_port: int = 8100
    heartbeat_timeout: int = 60
    output_dir: str = "./outputs"
    input_dir: str = "./inputs"
    inventory_file: str = "inventory.xlsx"
    ssh_timeout: int = 30
    cmd_timeout: int = 60
    # Global thresholds
    disk_threshold: int = 85
    mem_used_pct_warn: int = 80
    mem_used_pct_fail: int = 95
    load_ratio_warn: float = 2.0
    load_ratio_fail: float = 5.0
    swap_used_pct_warn: int = 30
    # Check filtering (None = run all)
    enabled_checks: Optional[Set[str]] = None
    verbose: bool = False

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            parallel_limit=data.get('parallel_limit', 5),
            max_parallel_nodes=data.get('max_parallel_nodes', 10),
            backend_port=data.get('backend_port', 8100),
            heartbeat_timeout=data.get('heartbeat_timeout', 60),
            output_dir=data.get('output_dir', './outputs'),
            input_dir=data.get('input_dir', './inputs'),
            inventory_file=data.get('inventory_file', 'inventory.xlsx'),
            ssh_timeout=data.get('ssh_timeout', 30),
            cmd_timeout=data.get('cmd_timeout', 60),
            disk_threshold=data.get('disk_threshold', 85),
            mem_used_pct_warn=data.get('mem_used_pct_warn', 80),
            mem_used_pct_fail=data.get('mem_used_pct_fail', 95),
            load_ratio_warn=data.get('load_ratio_warn', 2.0),
            load_ratio_fail=data.get('load_ratio_fail', 5.0),
            swap_used_pct_warn=data.get('swap_used_pct_warn', 30),
            enabled_checks=set(data['enabled_checks']) if data.get('enabled_checks') else None,
            verbose=data.get('verbose', False)
        )


class InventoryLoader:
    """
    Handles loading of global configuration (YAML) and inventory data (Excel).
    Supports per-cluster overrides for thresholds.
    """
    def __init__(self, config_path: str):
        self.config_path = str(Path(config_path).resolve())
        self.config_dir = Path(self.config_path).parent
        self.raw_config = self._load_yaml(self.config_path)
        configured_input_dir = Path(self.raw_config.get('input_dir', './inputs'))
        if not configured_input_dir.is_absolute():
            configured_input_dir = self.config_dir / configured_input_dir
        self.input_dir = str(configured_input_dir)

    def _load_yaml(self, path: str) -> Dict:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path, 'r') as f:
            return yaml.safe_load(f) or {}

    def get_app_settings(self) -> AppSettings:
        """Build AppSettings from config.yaml."""
        cfg = self.raw_config
        thr = cfg.get('thresholds', {})
        parallelism = cfg.get('parallelism', {})
        enabled_checks = cfg.get('enabled_checks')
        if enabled_checks:
            enabled_checks = set(str(item).strip() for item in enabled_checks if str(item).strip())
        s = AppSettings(
            parallel_limit     = cfg.get('parallel_limit', parallelism.get('max_parallel_clusters', 5)),
            max_parallel_nodes = cfg.get('max_parallel_nodes', parallelism.get('max_parallel_nodes', 10)),
            backend_port       = cfg.get('backend_port', 8100),
            heartbeat_timeout  = cfg.get('heartbeat_timeout', 60),
            output_dir         = cfg.get('output_dir', './outputs'),
            input_dir          = self.input_dir,
            inventory_file     = cfg.get('inventory_file', 'inventory.xlsx'),
            ssh_timeout        = cfg.get('ssh_timeout', parallelism.get('ssh_timeout', 30)),
            cmd_timeout        = cfg.get('cmd_timeout', parallelism.get('cmd_timeout', 60)),
            disk_threshold     = thr.get('disk_percent', thr.get('disk_pct', 85)),
            mem_used_pct_warn  = thr.get('mem_warn', thr.get('mem_used_pct_warn', 80)),
            mem_used_pct_fail  = thr.get('mem_fail', thr.get('mem_used_pct_fail', 95)),
            load_ratio_warn    = thr.get('load_warn', thr.get('load_ratio_warn', 2.0)),
            load_ratio_fail    = thr.get('load_fail', thr.get('load_ratio_fail', 5.0)),
            swap_used_pct_warn = thr.get('swap_warn', thr.get('swap_used_pct_warn', 30)),
            enabled_checks     = enabled_checks,
        )
        return s

    # Legacy compat
    def get_global_settings(self) -> Dict:
        s = self.get_app_settings()
        return {
            'parallel_limit': s.parallel_limit,
            'output_dir': s.output_dir,
            'thresholds': {
                'disk_percent': s.disk_threshold,
                'mem_warn': s.mem_used_pct_warn,
                'mem_fail': s.mem_used_pct_fail,
            },
        }

    def load_inventory(self, excel_filename: str) -> List[ClusterConfig]:
        # Try both absolute path and relative to input_dir
        if os.path.isabs(excel_filename) and os.path.exists(excel_filename):
            excel_path = excel_filename
        else:
            excel_path = os.path.join(self.input_dir, excel_filename)
        if not os.path.exists(excel_path):
            raise FileNotFoundError(f"Inventory file not found: {excel_path}")

        try:
            wb = pd.ExcelFile(excel_path)
        except BadZipFile as exc:
            raise ValueError(f"Inventory file is not a valid .xlsx workbook: {excel_path}") from exc

        # Load Clusters
        sheet_name = 'Clusters' if 'Clusters' in wb.sheet_names else wb.sheet_names[0]
        df_clusters = _normalize_columns(pd.read_excel(wb, sheet_name=sheet_name))

        # Load Nodes (optional second sheet)
        nodes_map: Dict[str, List[NodeConfig]] = {}
        if 'Nodes' in wb.sheet_names:
            df_nodes = _normalize_columns(pd.read_excel(wb, sheet_name='Nodes'))
            for _, row in df_nodes.iterrows():
                c_name = _none_if_blank(row.get('cluster_name'))
                if not c_name:
                    continue
                if c_name not in nodes_map:
                    nodes_map[c_name] = []
                nodes_map[c_name].append(NodeConfig(
                    ip=_none_if_blank(row.get('node_ip')) or "",
                    username=_none_if_blank(row.get('ssh_user')) or "root",
                    password=_none_if_blank(row.get('ssh_pass')),
                    key_path=_none_if_blank(row.get('ssh_key')),
                ))

        clusters = []
        for _, row in df_clusters.iterrows():
            cluster_name = _none_if_blank(row.get('cluster_name'))
            if not cluster_name:
                continue

            # Skip disabled clusters
            enabled_val = row.get('enabled', 'yes')
            if str(enabled_val).strip().lower() in ('no', 'false', '0', 'disabled'):
                continue

            # Use Nodes from sheet if available, otherwise fallback to comma-separated column
            node_configs = nodes_map.get(cluster_name, [])
            if not node_configs and pd.notna(row.get('node_ips')):
                node_ips = str(row['node_ips']).split(',')
                node_configs = [
                    NodeConfig(
                        ip=ip.strip(),
                        username=_none_if_blank(row.get('node_user')) or "root",
                        password=_none_if_blank(row.get('node_pass')),
                    ) for ip in node_ips if ip.strip()
                ]

            cluster = ClusterConfig(
                name=cluster_name,
                type=(_none_if_blank(row.get('type')) or "").lower(),
                installer_ip=_none_if_blank(row.get('installer_ip')) or "",
                ssh_user=_none_if_blank(row.get('ssh_user')) or "",
                ssh_pass=_none_if_blank(row.get('ssh_pass')),
                ssh_key=_none_if_blank(row.get('ssh_key')),
                nodes=node_configs,
                # Per-cluster threshold overrides
                disk_threshold     = _int_or_none(row.get('disk_threshold')),
                mem_used_pct_warn  = _int_or_none(row.get('mem_used_pct_warn')),
                mem_used_pct_fail  = _int_or_none(row.get('mem_used_pct_fail')),
                load_ratio_warn    = _float_or_none(row.get('load_ratio_warn')),
                load_ratio_fail    = _float_or_none(row.get('load_ratio_fail')),
                swap_used_pct_warn = _int_or_none(row.get('swap_used_pct_warn')),
            )
            clusters.append(cluster)

        return clusters


def resolve_threshold(cluster: ClusterConfig, attr: str, app: AppSettings):
    """Return per-cluster threshold if set, otherwise global default."""
    v = getattr(cluster, attr, None)
    return v if v is not None else getattr(app, attr)
