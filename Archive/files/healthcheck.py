#!/usr/bin/env python3
"""
ClusterPulse - Universal Cluster Health Check Tool
Supports CVIM (OpenStack) and OCP (OpenShift) clusters
"""

import asyncio
import argparse
import sys
import os
from pathlib import Path

from core.config import Config
from core.engine import HealthCheckEngine
from core.reporter import HTMLReporter, ConsoleReporter
from core.logger import setup_logger


def parse_args():
    parser = argparse.ArgumentParser(
        description="ClusterPulse - Universal Cluster Health Check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run checks on all clusters defined in inventory
  python healthcheck.py -i inventory.yaml

  # Run only on specific cluster type
  python healthcheck.py -i inventory.yaml --type ocp

  # Custom output directory
  python healthcheck.py -i inventory.yaml -o /tmp/results

  # Run specific check categories only
  python healthcheck.py -i inventory.yaml --checks nodes,pods,ceph

  # Increase parallelism
  python healthcheck.py -i inventory.yaml --parallel 10
        """
    )
    parser.add_argument(
        "-i", "--inventory",
        required=True,
        help="Path to inventory YAML file (clusters, credentials, etc.)"
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output directory for reports (default: ./results_<timestamp>)"
    )
    parser.add_argument(
        "--type",
        choices=["ocp", "cvim", "all"],
        default="all",
        help="Cluster type to check (default: all)"
    )
    parser.add_argument(
        "--checks",
        default=None,
        help="Comma-separated list of check categories to run (default: all)"
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=5,
        help="Max parallel cluster connections (default: 5)"
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="Skip HTML report generation"
    )
    parser.add_argument(
        "--ssh-timeout",
        type=int,
        default=30,
        help="SSH connection timeout in seconds (default: 30)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show verbose output including all command outputs"
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    # Setup output directory
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output) if args.output else Path(f"results_{ts}")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Setup logging
    log_file = output_dir / "healthcheck.log"
    logger = setup_logger(log_file, verbose=args.verbose)

    logger.info(f"ClusterPulse Health Check starting — output: {output_dir}")

    # Load inventory
    try:
        config = Config(args.inventory)
    except Exception as e:
        print(f"[ERROR] Failed to load inventory: {e}")
        sys.exit(1)

    # Filter cluster types
    clusters = config.get_clusters(cluster_type=args.type if args.type != "all" else None)
    if not clusters:
        print("[ERROR] No clusters found matching the specified criteria.")
        sys.exit(1)

    # Parse check categories
    check_filter = None
    if args.checks:
        check_filter = [c.strip() for c in args.checks.split(",")]

    # Console reporter (live progress)
    console = ConsoleReporter(verbose=args.verbose)

    # Run health checks
    engine = HealthCheckEngine(
        clusters=clusters,
        output_dir=output_dir,
        logger=logger,
        console=console,
        max_parallel=args.parallel,
        ssh_timeout=args.ssh_timeout,
        check_filter=check_filter,
    )

    results = await engine.run()

    # Generate HTML report
    if not args.no_html:
        html_reporter = HTMLReporter(results, output_dir)
        html_file = html_reporter.generate()
        console.print_final_summary(results, html_file)
    else:
        console.print_final_summary(results, None)

    # Exit code based on failures
    total_fail = sum(r.fail_count for r in results)
    sys.exit(1 if total_fail > 0 else 0)


if __name__ == "__main__":
    asyncio.run(main())
