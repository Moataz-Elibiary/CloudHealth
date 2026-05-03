#!/usr/bin/env python3
"""CloudHealth Beta 5 — entry point."""
from __future__ import annotations
import argparse, logging, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "frontend"))
sys.path.insert(0, str(Path(__file__).parent))


def main():
    p = argparse.ArgumentParser(prog="cloudhealth",
                                description="CloudHealth Beta5 — Universal Cluster Health Check")
    p.add_argument("-i","--inventory", default=None)
    p.add_argument("-c","--config",    default=None)
    p.add_argument("-o","--output",    default=None)
    p.add_argument("--type",           default=None, choices=["ocp","cvim"])
    p.add_argument("--checks",         default=None, help="Comma-separated check categories")
    p.add_argument("--parallel",       type=int, default=None)
    p.add_argument("--ssh-timeout",    type=int, default=None, dest="ssh_timeout")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    from frontend.core.config import load_app_config
    try:
        app_config = load_app_config(
            config_path    = args.config,
            inventory_path = args.inventory,
            output_dir     = args.output,
            cluster_type   = args.type,
            checks         = args.checks,
            max_parallel   = args.parallel,
            ssh_timeout    = args.ssh_timeout,
        )
    except FileNotFoundError as e:
        print(f"\n[ERROR] {e}\n"); sys.exit(1)

    if not app_config.clusters:
        print("[ERROR] No enabled clusters found in inventory."); sys.exit(1)

    from frontend.app import start
    start(app_config)


if __name__ == "__main__":
    main()
