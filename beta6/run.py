#!/usr/bin/env python3
"""
CloudHealth Beta 6 — headless CLI entry point.

Runs health checks from a central Linux server that SSHes to all bastions.
No frontend, no WebSocket, no browser — results go to DB + HTML report.

Usage examples:
  python3 run.py
  python3 run.py -i /path/to/inventory.yaml -o /tmp/reports
  python3 run.py --clusters cluster-ocp-prod-1,cluster-cvim-prod-1
  python3 run.py --failed-only
  python3 run.py --check-types ocp --ocp-checks nodes,etcd,pods
  python3 run.py --preflight-only
  python3 run.py --dry-run
  python3 run.py --list-history 20
"""
from __future__ import annotations
import argparse
import asyncio
import logging
import os
import signal
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Make core/ importable as a package
sys.path.insert(0, str(Path(__file__).parent))

from core.inventory import InventoryLoader, AppSettings
from core.preflight  import run_preflight, PreflightResult
from core.check_runner import CheckRunner
from core.history_db import (
    init_db, write_run, get_runs, get_previous_checks, get_failed_clusters,
)
from core.reporter_html import HTMLReporter

# ── Exit codes ────────────────────────────────────────────────────────────────
EXIT_OK          = 0   # all checks passed
EXIT_FAILURES    = 1   # run completed, one or more checks failed/warned
EXIT_PREFLIGHT   = 2   # run aborted — preflight failure
EXIT_CONFIG      = 3   # run aborted — config/inventory error
EXIT_CANCELLED   = 130 # run cancelled by SIGINT


# ── Run lock ──────────────────────────────────────────────────────────────────

def _acquire_lock(lock_path: Path) -> bool:
    """Atomic lock — returns False if another run is already in progress."""
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Check if the PID in the lock file is still alive
        try:
            pid = int(lock_path.read_text().strip())
            os.kill(pid, 0)
            return False           # process is alive — lock is valid
        except (ValueError, OSError):
            lock_path.unlink(missing_ok=True)   # stale lock
            return _acquire_lock(lock_path)


def _release_lock(lock_path: Path):
    lock_path.unlink(missing_ok=True)


# ── Logging setup ─────────────────────────────────────────────────────────────

def _setup_logging(log_dir: str, verbose: bool = False):
    p = Path(log_dir).expanduser()
    p.mkdir(parents=True, exist_ok=True)

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    sys_log  = p / f"system_{ts}.log"
    cmd_log  = p / f"commands_{ts}.log"

    level = logging.DEBUG if verbose else logging.INFO
    fmt   = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"

    # Root logger → system log + stderr
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fh = logging.FileHandler(sys_log, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(logging.Formatter(fmt))
    ch.setLevel(logging.INFO)
    root.addHandler(ch)

    # Commands logger → separate file, no propagation
    cmd_logger = logging.getLogger("commands")
    cmd_logger.setLevel(logging.DEBUG)
    cmd_logger.propagate = False
    cmd_logger.handlers.clear()
    cfh = logging.FileHandler(cmd_log, encoding="utf-8")
    cfh.setFormatter(logging.Formatter("%(asctime)s\n%(message)s"))
    cmd_logger.addHandler(cfh)

    # rotation is performed at end of run via _rotate_logs / _rotate_reports


def _rotate_logs(log_dir: Path, max_files: int):
    """Keep only the newest max_files of each log type."""
    if not max_files:
        return
    for prefix in ("system_", "commands_", "host-"):
        files = sorted(log_dir.glob(f"{prefix}*.log"),
                       key=lambda p: p.stat().st_mtime)
        while len(files) > max_files:
            files.pop(0).unlink(missing_ok=True)


def _rotate_reports(out_dir: Path, max_files: int):
    """Keep only the newest max_files of each report type (full + email)."""
    if not max_files:
        return
    for prefix in ("healthcheck_report_", "healthcheck_email_"):
        files = sorted(out_dir.glob(f"{prefix}*.html"),
                       key=lambda p: p.stat().st_mtime)
        while len(files) > max_files:
            files.pop(0).unlink(missing_ok=True)


# ── Argument parsing ──────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="CloudHealth Beta6 — headless cluster health check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Config & paths
    p.add_argument("-c", "--config",      metavar="PATH",
                   help="Config file (default: config/config.yaml)")
    p.add_argument("-i", "--inventory",   metavar="PATH",
                   help="Inventory YAML file (overrides config inventory_file)")
    p.add_argument("-o", "--output-dir",  metavar="PATH", dest="output_dir",
                   help="Report output directory (overrides config output_dir)")

    # Cluster selection
    p.add_argument("--clusters",          metavar="LIST",
                   help="Comma-separated cluster names to run (overrides inventory enabled flags)")
    p.add_argument("--failed-only",       action="store_true",
                   help="Run only clusters that failed in the last completed DB run")

    # Check selection
    p.add_argument("--check-types",       metavar="LIST",
                   help="Comma-separated cluster types to run: ocp,cvim (default: all)")
    p.add_argument("--ocp-checks",        metavar="LIST",
                   help="Specific OCP check names (default: all enabled in config)")
    p.add_argument("--cvim-checks",       metavar="LIST",
                   help="Specific CVIM check names (default: all enabled in config)")
    p.add_argument("--host-checks",       metavar="LIST",
                   help="Specific host check names (default: all enabled in config)")

    # Run behaviour
    p.add_argument("--preflight-only",    action="store_true",
                   help="Run SSH + CLI checks only, no health checks")
    p.add_argument("--ignore-preflight",  action="store_true",
                   help="Continue even if preflight checks fail")
    p.add_argument("--dry-run",           action="store_true",
                   help="Regenerate report from last DB run without running new checks")
    p.add_argument("--parallel",          type=int, metavar="N",
                   help="Override parallel_limit from config")

    # Info / utility
    p.add_argument("--list-history",      nargs="?", const=10, type=int, metavar="N",
                   help="Print last N runs from DB and exit (default: 10)")
    p.add_argument("--version",           action="store_true",
                   help="Print version and exit")
    p.add_argument("--verbose",           action="store_true",
                   help="Enable debug-level logging")

    return p


# ── Diff data builder ─────────────────────────────────────────────────────────

def _build_diff_data(db_path: str, run_id: str, cluster_names: list) -> dict:
    """Build {cluster_name: {(section_name, idx): status}} for diff badges."""
    diff_data = {}
    for cname in cluster_names:
        rows = get_previous_checks(db_path, cname, run_id)
        if rows:
            diff_data[cname] = {
                (r["section_name"], r["message_index"]): r["status"]
                for r in rows
            }
    return diff_data


# ── Dry-run report regeneration ───────────────────────────────────────────────

def _dry_run(db_path: str, output_dir: str) -> int:
    from core.result import ClusterResult
    log = logging.getLogger("cloudhealth")
    runs = get_runs(db_path, limit=1)
    if not runs:
        log.error("No completed runs found in DB for --dry-run")
        return EXIT_CONFIG

    last = runs[0]
    run_id = last["run_id"]
    log.info("Regenerating report from run %s (%s)", run_id, last["started_at"])

    from core.history_db import get_run
    run_data = get_run(db_path, run_id)
    if not run_data:
        log.error("Run %s not found in DB", run_id)
        return EXIT_CONFIG

    # Reconstruct ClusterResult objects from stored check_results
    import sqlite3
    from pathlib import Path as P
    conn = sqlite3.connect(str(P(db_path).expanduser()))
    conn.row_factory = sqlite3.Row
    results = []
    for cl in run_data.get("clusters", []):
        rows = conn.execute(
            "SELECT section_name, message_index, status, message, detail, command "
            "FROM check_results WHERE run_id=? AND cluster_name=? "
            "ORDER BY section_name, message_index",
            (run_id, cl["cluster_name"]),
        ).fetchall()
        from core.result import SectionResult, CheckItem, Status
        sections_map: dict = {}
        for r in rows:
            s = sections_map.setdefault(r["section_name"], SectionResult(r["section_name"]))
            s.checks.append(CheckItem(
                status  = Status(r["status"]),
                message = r["message"] or "",
                detail  = r["detail"],
                command = r["command"] or "",
            ))
        cr = ClusterResult(
            cluster_name  = cl["cluster_name"],
            cluster_type  = cl.get("cluster_type", ""),
            login_success = bool(cl.get("login_success", 1)),
        )
        cr.sections = list(sections_map.values())
        results.append(cr)
    conn.close()

    out = Path(output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    reporter = HTMLReporter(results, out)
    full  = reporter.generate()
    email = reporter.generate_email()
    log.info("Full report:  %s", full)
    log.info("Email report: %s", email)
    return EXIT_OK


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = _build_parser()
    args   = parser.parse_args()

    # ── version ────────────────────────────────────────────────────────────────
    if args.version:
        v = (Path(__file__).parent / "version.txt").read_text().strip()
        print(f"CloudHealth {v}")
        return EXIT_OK

    # ── load config ────────────────────────────────────────────────────────────
    DEFAULT_CONFIGS = [
        Path("config/config.yaml"),
        Path("~/cloud_health/config/config.yaml").expanduser(),
    ]
    cfg_path = args.config
    if not cfg_path:
        for p in DEFAULT_CONFIGS:
            if p.exists():
                cfg_path = str(p)
                break
    if not cfg_path:
        print(f"[ERROR] config.yaml not found. Tried: {[str(p) for p in DEFAULT_CONFIGS]}",
              file=sys.stderr)
        return EXIT_CONFIG

    try:
        loader = InventoryLoader(cfg_path)
        app    = loader.get_app_settings()
    except Exception as e:
        print(f"[ERROR] Failed to load config: {e}", file=sys.stderr)
        return EXIT_CONFIG

    # CLI overrides
    if args.output_dir:          app.output_dir     = args.output_dir
    if args.parallel is not None: app.parallel_limit = args.parallel
    if args.verbose:             app.verbose        = True

    # ── logging ────────────────────────────────────────────────────────────────
    _setup_logging(app.log_dir, verbose=app.verbose)
    log = logging.getLogger("cloudhealth")
    v   = (Path(__file__).parent / "version.txt").read_text().strip()
    log.info("CloudHealth %s starting", v)

    # ── --list-history ─────────────────────────────────────────────────────────
    if args.list_history is not None:
        try:
            init_db(app.db_path)
            runs = get_runs(app.db_path, limit=args.list_history)
        except Exception as e:
            log.error("DB error: %s", e)
            return EXIT_CONFIG
        if not runs:
            print("No runs found in history DB.")
            return EXIT_OK
        print(f"{'RUN ID':<38} {'STARTED':<22} {'STATUS':<12} {'CLUSTERS'}")
        print("-" * 90)
        for r in runs:
            print(f"{r['run_id']:<38} {r['started_at']:<22} {r['status']:<12} {r['cluster_count']}")
        return EXIT_OK

    # ── load inventory ─────────────────────────────────────────────────────────
    inv_path = args.inventory or app.inventory_file
    try:
        clusters = loader.load_inventory(inv_path)
    except Exception as e:
        log.error("Failed to load inventory: %s", e)
        return EXIT_CONFIG

    if not clusters:
        log.error("No enabled clusters found in inventory.")
        return EXIT_CONFIG

    # ── cluster filter ─────────────────────────────────────────────────────────
    if args.failed_only:
        try:
            init_db(app.db_path)
            failed_names = set(get_failed_clusters(app.db_path))
        except Exception as e:
            log.error("DB error when fetching failed clusters: %s", e)
            return EXIT_CONFIG
        if not failed_names:
            log.info("No failed clusters in last run — nothing to do.")
            return EXIT_OK
        clusters = [c for c in clusters if c.name in failed_names]
        log.info("--failed-only: %d cluster(s) selected", len(clusters))

    if args.clusters:
        names    = {n.strip() for n in args.clusters.split(",")}
        clusters = [c for c in clusters if c.name in names]
        if not clusters:
            log.error("None of the specified clusters found in inventory: %s", args.clusters)
            return EXIT_CONFIG

    if args.check_types:
        types    = {t.strip().lower() for t in args.check_types.split(",")}
        clusters = [c for c in clusters if c.type in types]
        if not clusters:
            log.error("No clusters of type(s): %s", args.check_types)
            return EXIT_CONFIG

    # ── enabled checks filter ──────────────────────────────────────────────────
    if args.ocp_checks:
        app.enabled_ocp_checks  = {c.strip() for c in args.ocp_checks.split(",")  if c.strip()}
    if args.cvim_checks:
        app.enabled_cvim_checks = {c.strip() for c in args.cvim_checks.split(",") if c.strip()}
    if args.host_checks:
        app.enabled_host_checks = {c.strip() for c in args.host_checks.split(",") if c.strip()}

    # ── --dry-run ──────────────────────────────────────────────────────────────
    if args.dry_run:
        try:
            init_db(app.db_path)
        except Exception as e:
            log.error("DB init failed: %s", e)
            return EXIT_CONFIG
        return _dry_run(app.db_path, app.output_dir)

    # ── init DB ────────────────────────────────────────────────────────────────
    try:
        init_db(app.db_path)
    except Exception as e:
        log.error("DB init failed: %s", e)
        return EXIT_CONFIG

    # ── run lock ───────────────────────────────────────────────────────────────
    lock_path = Path(app.log_dir).expanduser() / "run.lock"
    if not _acquire_lock(lock_path):
        log.error("Another CloudHealth run is already in progress (lock: %s)", lock_path)
        return EXIT_FAILURES

    # ── preflight ──────────────────────────────────────────────────────────────
    log.info("Running preflight checks on %d cluster(s)...", len(clusters))

    preflight_results = run_preflight(
        clusters       = clusters,
        parallel_limit = app.parallel_limit,
        on_result      = lambda r: log.info(
            "  [%s] %s — %s%s",
            "OK" if r.status == "OK" else "FAIL",
            r.cluster_name,
            (r.cli_version or "SSH OK") if r.status == "OK" else r.error,
            f" ({r.duration_ms}ms)" if r.duration_ms else "",
        ),
    )

    failed_pf = [r for r in preflight_results if r.status != "OK"]
    if failed_pf:
        for r in failed_pf:
            log.warning("Preflight FAIL: %s — %s", r.cluster_name, r.error)
        if not args.ignore_preflight:
            log.error(
                "%d/%d preflight check(s) failed. Use --ignore-preflight to proceed anyway.",
                len(failed_pf), len(clusters),
            )
            _release_lock(lock_path)
            return EXIT_PREFLIGHT
        log.warning("--ignore-preflight set — continuing despite preflight failures")

    if args.preflight_only:
        log.info("--preflight-only: skipping health checks.")
        _release_lock(lock_path)
        return EXIT_OK if not failed_pf else EXIT_PREFLIGHT

    # ── run health checks ─────────────────────────────────────────────────────
    run_id     = str(uuid.uuid4())
    started_at = datetime.now()
    results    = []
    cancelled  = False

    log.info("Starting health checks — run_id=%s", run_id)
    log.info("Clusters: %s", ", ".join(c.name for c in clusters))

    # SIGINT / SIGTERM → cancel gracefully
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _cancel_event = asyncio.Event()

    def _handle_signal(signum, frame):
        log.warning("Signal %s received — cancelling run...", signum)
        loop.call_soon_threadsafe(_cancel_event.set)

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    async def _run_all():
        nonlocal results, cancelled
        from core.result import ClusterResult
        sem = asyncio.Semaphore(app.parallel_limit)

        async def _one(cluster):
            async with sem:
                runner = CheckRunner(
                    cluster_config = cluster.to_dict(),
                    app_settings   = app.to_dict(),
                )
                try:
                    return await runner.run()
                except asyncio.CancelledError:
                    log.warning("Cluster run cancelled: %s", cluster.name)
                    return None
                except Exception as e:
                    log.error("Unhandled error on %s: %s", cluster.name, e, exc_info=True)
                    cr = ClusterResult(cluster_name=cluster.name, cluster_type=cluster.type)
                    cr.login_success = False
                    cr.login_error   = str(e)
                    cr.end_time      = datetime.now()
                    return cr

        tasks = [asyncio.create_task(_one(c)) for c in clusters]

        async def _watcher():
            """Cancel all cluster tasks as soon as a signal fires."""
            await _cancel_event.wait()
            log.warning("Cancellation event — stopping %d running task(s)…", len(tasks))
            for t in tasks:
                t.cancel()

        watcher = asyncio.create_task(_watcher())
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass

        if _cancel_event.is_set():
            cancelled = True

        results = [r for r in gathered
                   if isinstance(r, ClusterResult) and r is not None]

    try:
        loop.run_until_complete(_run_all())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
        signal.signal(signal.SIGINT,  signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    finished_at = datetime.now()
    status      = "CANCELLED" if cancelled else "COMPLETED"

    # ── persist to DB ─────────────────────────────────────────────────────────
    if results:
        summaries = [r.to_dict() for r in results]
        try:
            write_run(
                db_path     = app.db_path,
                run_id      = run_id,
                user        = os.environ.get("USER", os.environ.get("LOGNAME", "unknown")),
                started_at  = started_at,
                finished_at = finished_at,
                summaries   = summaries,
                status      = status,
                max_runs    = app.history_max_runs,
            )
            log.info("Results saved to DB (%s)", app.db_path)
        except Exception as e:
            log.error("Failed to save results to DB: %s", e)

    # ── generate HTML reports ──────────────────────────────────────────────────
    if results:
        out_dir = Path(app.output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        diff_data = _build_diff_data(app.db_path, run_id,
                                     [r.cluster_name for r in results])

        reporter = HTMLReporter(results, out_dir, diff_data=diff_data)

        # Timestamp-stamped filenames — generate to default name then rename
        ts         = started_at.strftime("%Y%m%d_%H%M%S")
        full_path  = out_dir / f"healthcheck_report_{ts}.html"
        email_path = out_dir / f"healthcheck_email_{ts}.html"

        reporter.generate().rename(full_path)
        reporter.generate_email().rename(email_path)

        _rotate_reports(out_dir, max_files=app.max_report_files)

        log.info("Full report:  %s", full_path)
        log.info("Email report: %s", email_path)

    # ── summary ────────────────────────────────────────────────────────────────
    total_pass = sum(r.pass_count for r in results)
    total_fail = sum(r.fail_count for r in results)
    total_warn = sum(r.warn_count for r in results)
    elapsed    = (finished_at - started_at).total_seconds()

    log.info(
        "Run %s in %.1fs — %d cluster(s), %d pass, %d fail, %d warn",
        status, elapsed, len(results), total_pass, total_fail, total_warn,
    )

    # ── log rotation ───────────────────────────────────────────────────────────
    _rotate_logs(Path(app.log_dir).expanduser(), max_files=app.max_log_files)

    _release_lock(lock_path)

    if cancelled:
        return EXIT_CANCELLED
    return EXIT_FAILURES if total_fail > 0 or total_warn > 0 else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
