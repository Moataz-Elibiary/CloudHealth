#!/usr/bin/env python3
"""
ClusterPulse — Universal Cluster Health Check
Supports OCP (OpenShift) and CVIM (Cisco VIM / OpenStack) clusters.
"""
import asyncio
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(
        prog="clusterpulse",
        description="ClusterPulse — Universal Cluster Health Check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python clusterpulse.py                              # use config/config.yaml + config/inventory.xlsx
  python clusterpulse.py -i my_inventory.xlsx         # custom inventory
  python clusterpulse.py -c custom_config.yaml        # custom config
  python clusterpulse.py --type ocp                   # OCP clusters only
  python clusterpulse.py --checks nodes,pods,ceph,host
  python clusterpulse.py --parallel 10 --ssh-timeout 60
  python clusterpulse.py -o /reports/2024-01-15
        """
    )
    p.add_argument("-i","--inventory",  default=None, help="Path to inventory.xlsx")
    p.add_argument("-c","--config",     default=None, help="Path to config.yaml")
    p.add_argument("-o","--output",     default=None, help="Output directory")
    p.add_argument("--type",            choices=["ocp","cvim","all"], default=None,
                   help="Filter cluster type (default: all)")
    p.add_argument("--checks",          default=None,
                   help="Comma-separated check categories to run (default: all)")
    p.add_argument("--parallel",        type=int, default=None,
                   help="Max parallel cluster connections (overrides config)")
    p.add_argument("--ssh-timeout",     type=int, default=None, dest="ssh_timeout",
                   help="SSH timeout in seconds (overrides config)")
    p.add_argument("--no-html",         action="store_true", help="Skip HTML report")
    p.add_argument("--no-email",        action="store_true", help="Skip email HTML report")
    p.add_argument("--verbose","-v",    action="store_true", help="Verbose console output")
    return p.parse_args()


async def main():
    args = parse_args()

    # ── Load config ───────────────────────────────────────────────────────────
    try:
        from core.config import load_app_config
        app = load_app_config(
            config_path    = args.config,
            inventory_path = args.inventory,
            output_dir     = args.output,
            cluster_type   = args.type if args.type != "all" else None,
            checks         = args.checks,
            max_parallel   = args.parallel,
            ssh_timeout    = args.ssh_timeout,
            verbose        = args.verbose,
        )
    except FileNotFoundError as e:
        print(f"\n[ERROR] {e}\n")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Failed to load configuration: {e}\n")
        sys.exit(1)

    if not app.clusters:
        print("[ERROR] No clusters found in inventory (check type filter and 'enabled' column).")
        sys.exit(1)

    # ── Setup output / logging ────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if app.output_dir and not str(app.output_dir).endswith(ts):
        out_dir = app.output_dir / ts if app.output_dir.name != ts else app.output_dir
    else:
        out_dir = Path(f"results_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)
    app.output_dir = out_dir

    log_file = out_dir / "clusterpulse.log"
    logger   = _setup_logger(log_file, app.verbose_console)

    from core.reporter_console import ConsoleReporter
    console = ConsoleReporter(verbose=app.verbose_console)

    logger.info(f"ClusterPulse starting — {len(app.clusters)} cluster(s) — output: {out_dir}")
    console.console.print(
        f"\n[bold cyan]⚡ ClusterPulse[/bold cyan]  "
        f"[dim]{len(app.clusters)} cluster(s) · max {app.max_parallel_clusters} parallel[/dim]\n"
    )

    # ── Run checks ────────────────────────────────────────────────────────────
    from core.engine import HealthCheckEngine
    engine  = HealthCheckEngine(app, console, logger)
    results = await engine.run()

    # ── Reports ───────────────────────────────────────────────────────────────
    html_file   = None
    email_file  = None

    if not args.no_html and app.html_report:
        from core.reporter_html import HTMLReporter
        reporter   = HTMLReporter(results, out_dir)
        html_file  = reporter.generate()
        if not args.no_email and app.email_friendly:
            email_file = reporter.generate_email()

    console.final_summary(results, html_file)

    if email_file:
        console.console.print(f"[dim]Email HTML  → {email_file}[/dim]")

    console.console.print(f"[dim]Text logs   → {out_dir}[/dim]")
    console.console.print(f"[dim]Full log    → {log_file}[/dim]\n")

    # Exit code
    total_fail = sum(r.fail_count for r in results)
    sys.exit(1 if total_fail > 0 else 0)


def _setup_logger(log_file: Path, verbose: bool) -> logging.Logger:
    logger = logging.getLogger("clusterpulse")
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-8s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.WARNING)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)
    return logger


if __name__ == "__main__":
    asyncio.run(main())
