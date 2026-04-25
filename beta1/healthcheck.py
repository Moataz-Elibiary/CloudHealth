"""CloudHealth Beta 2 — Cloud Health Check Tool.
Dual mode:
  - Default: runs the traditional CLI health check
  - --web: launches the Welcome Page UI in your browser
"""
import asyncio
import argparse
import sys
import os
import webbrowser
import threading

from core.inventory import InventoryLoader
from core.engine import HealthCheckEngine
from reports.html_reporter import HTMLReporter
from reports.console_reporter import ConsoleReporter
from logger import setup_logger, CommandLogger


# ══════════════════════════════════════════════════════════════════════════════
#  CLI MODE
# ══════════════════════════════════════════════════════════════════════════════

async def run_cli(args):
    """Traditional CLI health check execution."""
    # 1. Load config
    try:
        loader = InventoryLoader(args.config)
        app = loader.get_app_settings()

        output_dir = args.output_dir or app.output_dir
        os.makedirs(output_dir, exist_ok=True)

        inventory = args.inventory or app.inventory_file
        clusters = loader.load_inventory(inventory)
    except Exception as e:
        print(f"Initialization Error: {e}")
        sys.exit(1)

    if not clusters:
        print("No clusters found in inventory. Exiting.")
        sys.exit(1)

    # 2. Setup Logger & Console
    logger, log_path = setup_logger(output_dir)
    cmd_logger = CommandLogger(logger)
    console = ConsoleReporter(verbose=args.verbose)

    console.console.print(
        f"\n[bold cyan]╔══════════════════════════════════════════╗[/bold cyan]")
    console.console.print(
        f"[bold cyan]║   CloudHealth  · Beta 2                  ║[/bold cyan]")
    console.console.print(
        f"[bold cyan]║   Cloud Health Check Tool                ║[/bold cyan]")
    console.console.print(
        f"[bold cyan]╚══════════════════════════════════════════╝[/bold cyan]\n")
    console.console.print(f"  Clusters: [white]{len(clusters)}[/white]")
    console.console.print(f"  Logs:     [dim]{log_path}[/dim]")
    console.console.print(f"  Output:   [dim]{output_dir}[/dim]")

    # 3. Parse check filter
    enabled_checks = None
    if args.checks:
        enabled_checks = set(c.strip() for c in args.checks.split(","))
        console.console.print(
            f"  Checks:   [yellow]{', '.join(sorted(enabled_checks))}[/yellow]")

    # 4. Run Engine
    engine = HealthCheckEngine(
        clusters=clusters, app=app, logger=logger, console=console,
        enabled_checks=enabled_checks,
    )
    results = await engine.run()

    # 5. Generate Reports
    reporter = HTMLReporter(results, output_dir)
    report_path = reporter.generate()
    text_path = reporter.generate_text()

    # 6. Final Summary
    console.final_summary(results, html_file=report_path)

    if text_path:
        console.console.print(
            f"[bold]Text Report -> [white]{text_path}[/white][/bold]")


# ══════════════════════════════════════════════════════════════════════════════
#  WEB UI MODE
# ══════════════════════════════════════════════════════════════════════════════

def launch_web_ui(host="127.0.0.1", port=8000):
    """Start the FastAPI server and open the Welcome Page in the browser."""
    import uvicorn
    from web.server import app as web_app

    url = f"http://{host}:{port}"
    print(f"\n  CloudHealth - Welcome Page")
    print(f"  -----------------------------")
    print(f"  Opening -> {url}")
    print(f"  Press Ctrl+C to stop the server\n")

    # Open browser after a short delay (gives server time to start)
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(web_app, host=host, port=port, log_level="warning")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="CloudHealth Beta 2 — Cloud Health Check Tool")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to global config.yaml")
    parser.add_argument("-i", "--inventory",
                        help="Filename or path of inventory excel file (overrides config.yaml)")
    parser.add_argument("-o", "--output_dir",
                        help="Override output directory (overrides config.yaml)")
    parser.add_argument("--checks",
                        help="Comma-separated list of check categories to run "
                             "(e.g. nodes,pods,etcd,host). Default: all")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show detailed per-check output in console")
    parser.add_argument("--web", action="store_true",
                        help="Launch the interactive Web UI")
    parser.add_argument("--port", type=int, default=8000,
                        help="Web UI server port (default: 8000)")

    args = parser.parse_args()

    if args.web:
        # Launch the Welcome Page
        launch_web_ui(port=args.port)
    else:
        # Run traditional CLI
        asyncio.run(run_cli(args))


if __name__ == "__main__":
    main()
