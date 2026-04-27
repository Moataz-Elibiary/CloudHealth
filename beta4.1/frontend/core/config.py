"""
Beta4 frontend/core/config.py

Uses Beta3's InventoryLoader (pandas + HEADER_ALIASES) exposed via
a thin ConfigLoader subclass.
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import List, Optional

BASE_DIR         = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR      = BASE_DIR / "backend"
BACKEND_CORE_DIR = BACKEND_DIR / "core"

# Add backend/core to path so 'inventory' is importable as a top-level module
# (avoids conflict with frontend/core which is also named 'core')
for _p in (str(BACKEND_DIR), str(BACKEND_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from inventory import (  # noqa: E402  — resolves to backend/core/inventory.py
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
        Path("cloudhealth.yaml"),
        Path.home() / ".cloudhealth" / "config.yaml",
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
