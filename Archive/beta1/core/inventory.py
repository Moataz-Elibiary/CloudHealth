"""Inventory loader with per-cluster thresholds and check filter support."""
import os
import yaml
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Set


@dataclass
class NodeConfig:
    """Configuration for a single host/node."""
    ip: str
    username: str
    password: Optional[str] = None
    key_path: Optional[str] = None


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


@dataclass
class AppSettings:
    """Global application settings with defaults."""
    parallel_limit: int = 5
    max_parallel_nodes: int = 10
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


class InventoryLoader:
    """
    Handles loading of global configuration (YAML) and inventory data (Excel).
    Supports per-cluster overrides for thresholds.
    """
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.raw_config = self._load_yaml(config_path)
        self.input_dir = self.raw_config.get('input_dir', './inputs')

    def _load_yaml(self, path: str) -> Dict:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path, 'r') as f:
            return yaml.safe_load(f) or {}

    def get_app_settings(self) -> AppSettings:
        """Build AppSettings from config.yaml."""
        cfg = self.raw_config
        thr = cfg.get('thresholds', {})
        s = AppSettings(
            parallel_limit     = cfg.get('parallel_limit', 5),
            max_parallel_nodes = cfg.get('max_parallel_nodes', 10),
            output_dir         = cfg.get('output_dir', './outputs'),
            input_dir          = cfg.get('input_dir', './inputs'),
            inventory_file     = cfg.get('inventory_file', 'inventory.xlsx'),
            ssh_timeout        = cfg.get('ssh_timeout', 30),
            cmd_timeout        = cfg.get('cmd_timeout', 60),
            disk_threshold     = thr.get('disk_percent', 85),
            mem_used_pct_warn  = thr.get('mem_warn', 80),
            mem_used_pct_fail  = thr.get('mem_fail', 95),
            load_ratio_warn    = thr.get('load_warn', 2.0),
            load_ratio_fail    = thr.get('load_fail', 5.0),
            swap_used_pct_warn = thr.get('swap_warn', 30),
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

        wb = pd.ExcelFile(excel_path)

        # Load Clusters
        sheet_name = 'Clusters' if 'Clusters' in wb.sheet_names else wb.sheet_names[0]
        df_clusters = pd.read_excel(wb, sheet_name=sheet_name)

        # Load Nodes (optional second sheet)
        nodes_map: Dict[str, List[NodeConfig]] = {}
        if 'Nodes' in wb.sheet_names:
            df_nodes = pd.read_excel(wb, sheet_name='Nodes')
            for _, row in df_nodes.iterrows():
                c_name = str(row.get('Cluster Name', ''))
                if c_name not in nodes_map:
                    nodes_map[c_name] = []
                nodes_map[c_name].append(NodeConfig(
                    ip=str(row.get('Node IP', row.get('IP', ''))),
                    username=str(row.get('SSH User', row.get('User', 'root'))),
                    password=str(row.get('SSH Pass', row.get('Pass', ''))),
                ))

        clusters = []
        for _, row in df_clusters.iterrows():
            cluster_name = str(row['Cluster Name'])

            # Skip disabled clusters
            enabled_val = row.get('Enabled', 'yes')
            if str(enabled_val).strip().lower() in ('no', 'false', '0', 'disabled'):
                continue

            # Use Nodes from sheet if available, otherwise fallback to comma-separated column
            node_configs = nodes_map.get(cluster_name, [])
            if not node_configs and 'Node IPs' in row and pd.notna(row.get('Node IPs')):
                node_ips = str(row['Node IPs']).split(',')
                node_configs = [
                    NodeConfig(
                        ip=ip.strip(),
                        username=str(row.get('Node User', 'root')),
                        password=str(row.get('Node Pass/Key', '')),
                    ) for ip in node_ips if ip.strip()
                ]

            cluster = ClusterConfig(
                name=cluster_name,
                type=str(row['Type']).lower(),
                installer_ip=str(row['Installer IP']),
                ssh_user=str(row['SSH User']),
                ssh_pass=str(row.get('SSH Pass/Key', '')),
                nodes=node_configs,
                # Per-cluster threshold overrides
                disk_threshold     = int(row['Disk Threshold'])     if pd.notna(row.get('Disk Threshold'))     else None,
                mem_used_pct_warn  = int(row['Mem Warn %'])         if pd.notna(row.get('Mem Warn %'))         else None,
                load_ratio_warn    = float(row['Load Warn Ratio'])  if pd.notna(row.get('Load Warn Ratio'))    else None,
            )
            clusters.append(cluster)

        return clusters


def resolve_threshold(cluster: ClusterConfig, attr: str, app: AppSettings):
    """Return per-cluster threshold if set, otherwise global default."""
    v = getattr(cluster, attr, None)
    return v if v is not None else getattr(app, attr)
