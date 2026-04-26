"""
Beta4 frontend/core/config.py

Uses Beta3's InventoryLoader (pandas + HEADER_ALIASES) exposed via
a thin ConfigLoader subclass.

Key addition: to_backend_dict() sanitises SSH credentials for clusters
whose installer_ip does not match the current bastion being tunnelled —
sending all credentials to all bastions is a security hole both prior
betas shared.
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import List, Optional

BASE_DIR    = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = BASE_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.inventory import (  # noqa: E402
    InventoryLoader, AppSettings, ClusterConfig, NodeConfig
)


class ConfigLoader(InventoryLoader):
    """Frontend wrapper — identical to InventoryLoader, just named clearly."""
    pass


def load_app_config(
    config_path:    Optional[str] = None,
    inventory_path: Optional[str] = None,
    output_dir:     Optional[str] = None,
    cluster_type:   Optional[str] = None,
    checks:         Optional[str] = None,
    max_parallel:   Optional[int] = None,
    ssh_timeout:    Optional[int] = None,
) -> "AppConfig":
    """
    Convenience loader for the CLI entry point (main.py).
    Returns an AppConfig-like object wrapping AppSettings + clusters.
    """
    DEFAULT_CONFIGS = [
        Path("config/config.yaml"),
        Path("clusterpulse.yaml"),
        Path.home() / ".clusterpulse" / "config.yaml",
    ]
    DEFAULT_INVENTORIES = [
        Path("config/inventory.xlsx"),
        Path("inventory.xlsx"),
    ]

    def _find(explicit, defaults, label):
        if explicit:
            p = Path(explicit)
            if not p.exists():
                raise FileNotFoundError(f"{label} not found: {p}")
            return str(p)
        for p in defaults:
            if p.exists():
                return str(p)
        raise FileNotFoundError(
            f"{label} not found. Tried: {[str(d) for d in defaults]}")

    cfg_path = _find(config_path, DEFAULT_CONFIGS, "config.yaml")
    loader   = ConfigLoader(cfg_path)
    app      = loader.get_app_settings()

    # CLI overrides
    if output_dir:   app.output_dir        = output_dir
    if max_parallel: app.parallel_limit    = max_parallel
    if ssh_timeout:  app.ssh_timeout       = ssh_timeout
    if checks:       app.enabled_checks    = set(checks.split(","))

    inv_file = inventory_path or app.inventory_file
    clusters = loader.load_inventory(inv_file)

    if cluster_type:
        clusters = [c for c in clusters
                    if c.type.lower() == cluster_type.lower()]

    return _AppConfig(app, clusters)


class _AppConfig:
    """Thin wrapper so frontend app.py can treat the result uniformly."""

    def __init__(self, settings: AppSettings, clusters: List[ClusterConfig]):
        self._settings = settings
        self.clusters  = clusters

    def __getattr__(self, name):
        return getattr(self._settings, name)

    def to_backend_dict(self, current_bastion_ip: str = "") -> dict:
        """
        Serialise config for the WebSocket payload sent to one bastion.

        Credential sanitisation: only include ssh credentials for the cluster
        whose installer_ip matches current_bastion_ip. All other clusters have
        their passwords/keys stripped to prevent credential exposure.
        """
        settings_dict = self._settings.to_dict()
        clusters_out  = []
        for c in self.clusters:
            is_current = (not current_bastion_ip or
                          c.installer_ip == current_bastion_ip)
            clusters_out.append(c.to_dict(sanitize=not is_current))

        return {**settings_dict, "clusters": clusters_out}
