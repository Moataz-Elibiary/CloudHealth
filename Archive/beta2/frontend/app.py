"""
ClusterPulse Frontend — local FastAPI server.

Runs on the user's machine at http://localhost:8080
Serves the browser UI and acts as the orchestration layer:
  - Manages SSH tunnels to each bastion (via tunnel_manager)
  - Proxies WebSocket messages between browser and each backend (via ws_proxy)
  - Streams live progress to the browser
  - Generates the final HTML report when all checks complete
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))

from core.config import AppConfig, load_app_config
from core.tunnel_manager import TunnelManager
from core.ws_proxy import WSProxyOrchestrator
from core.result import ClusterResult
from core.reporter_html import HTMLReporter

log = logging.getLogger("frontend.app")

STATIC_DIR   = Path(__file__).parent / "static"
FRONTEND_PORT= 8080
VERSION_FILE = Path(__file__).parent.parent / "version.txt"


def get_version() -> str:
    return VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "dev"


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="ClusterPulse Frontend")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Global state — set by start() before uvicorn launches
_app_config: Optional[AppConfig] = None
_output_dir: Optional[Path]      = None
_results:    List[dict]          = []


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main dashboard page."""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/report", response_class=HTMLResponse)
async def report():
    """Serve the final report page."""
    report_path = _output_dir / "healthcheck_report.html" if _output_dir else None
    if report_path and report_path.exists():
        return FileResponse(str(report_path))
    return HTMLResponse("<h2>No report generated yet. Run a health check first.</h2>")


@app.get("/api/config")
async def api_config():
    """Return sanitised config to the browser UI (no credentials)."""
    if not _app_config:
        return {"error": "not initialised"}
    return {
        "version":  get_version(),
        "clusters": [
            {
                "name":        c.name,
                "type":        c.cluster_type,
                "environment": c.environment,
                "description": c.description,
                "host":        c.installer_host or "",
            }
            for c in _app_config.clusters
        ],
        "thresholds": {
            "disk_threshold":         _app_config.disk_threshold,
            "restart_warn_threshold": _app_config.restart_warn_threshold,
            "restart_fail_threshold": _app_config.restart_fail_threshold,
        },
    }


@app.get("/api/results")
async def api_results():
    """Return last completed results (for page reload)."""
    return {"results": _results}


@app.websocket("/ws/live")
async def ws_live(browser_ws: WebSocket):
    """
    Browser connects here to receive live streaming health check updates.
    Frontend proxies messages from all backend WebSockets to this single endpoint.
    """
    global _results
    await browser_ws.accept()
    log.info("Browser connected to live WebSocket")

    if not _app_config:
        await browser_ws.send_json({"type": "error", "message": "Frontend not initialised"})
        return

    # ── Setup SSH tunnels to all bastions ─────────────────────────────────────
    local_backend_version = (
        (Path(__file__).parent.parent / "backend" / "version.txt").read_text().strip()
        if (Path(__file__).parent.parent / "backend" / "version.txt").exists()
        else "0.0.0"
    )
    tm = TunnelManager(_app_config, local_backend_version)

    await browser_ws.send_json({
        "type":    "setup_start",
        "message": f"Setting up connections to {len(_app_config.clusters)} cluster(s)…",
    })

    tunnels = await tm.setup_all(_app_config.clusters)

    ready_count = sum(1 for t in tunnels.values() if t.ready)
    await browser_ws.send_json({
        "type":        "setup_done",
        "ready":       ready_count,
        "total":       len(tunnels),
        "message":     f"{ready_count}/{len(tunnels)} cluster(s) connected",
    })

    if ready_count == 0:
        await browser_ws.send_json({
            "type":    "error",
            "message": "No clusters reachable. Check inventory and network.",
        })
        return

    # ── Proxy — backend → browser ─────────────────────────────────────────────
    backend_config = _app_config.to_backend_dict()
    orchestrator   = WSProxyOrchestrator(tunnels, backend_config)

    collected_results: List[dict] = []

    async def _forward_to_browser():
        """Drain orchestrator queue and send each message to the browser."""
        while True:
            try:
                msg = await asyncio.wait_for(
                    orchestrator.browser_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # Send keepalive so browser knows we're alive
                try:
                    await browser_ws.send_json({"type": "keepalive"})
                except Exception:
                    return
                continue

            if msg is None:
                break

            # Collect final results when they arrive
            if msg.get("type") == "all_done" and "results" in msg:
                collected_results.extend(msg["results"])

            try:
                await browser_ws.send_json(msg)
            except WebSocketDisconnect:
                log.info("Browser disconnected during streaming")
                return

            if msg.get("type") == "all_clusters_done":
                break

    # Run proxy and forward concurrently
    proxy_task   = asyncio.create_task(orchestrator.run_all())
    forward_task = asyncio.create_task(_forward_to_browser())
    await asyncio.gather(proxy_task, forward_task, return_exceptions=True)

    # ── Generate reports ──────────────────────────────────────────────────────
    if collected_results:
        _results = collected_results
        _generate_reports(collected_results)
        await browser_ws.send_json({
            "type":        "reports_ready",
            "report_url":  "/report",
            "email_url":   "/report?format=email",
        })

    # ── Teardown tunnels ──────────────────────────────────────────────────────
    await tm.teardown_all()
    log.info("All tunnels closed")


@app.get("/report")
async def serve_report(format: str = "html"):
    if not _output_dir:
        return HTMLResponse("<h2>No report yet.</h2>")
    fname = "healthcheck_email.html" if format == "email" else "healthcheck_report.html"
    path  = _output_dir / fname
    if path.exists():
        return FileResponse(str(path), media_type="text/html")
    return HTMLResponse("<h2>Report not found.</h2>")


# ── Report generation ─────────────────────────────────────────────────────────

def _generate_reports(results_dicts: List[dict]):
    global _output_dir
    from core.result import ClusterResult
    results = [ClusterResult.from_dict(d) for d in results_dicts]
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = (_app_config.output_dir / ts) if _app_config and _app_config.output_dir else Path(f"results_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)
    _output_dir = out_dir
    reporter = HTMLReporter(results, out_dir)
    reporter.generate()
    if _app_config and _app_config.email_friendly:
        reporter.generate_email()
    log.info(f"Reports saved to {out_dir}")


# ── Entry point ───────────────────────────────────────────────────────────────

def start(app_config: AppConfig):
    """Called by the bootstrapper / main script after version sync."""
    global _app_config
    _app_config = app_config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Open browser after a short delay to let uvicorn start
    def _open_browser():
        import time; time.sleep(1.5)
        webbrowser.open(f"http://localhost:{FRONTEND_PORT}")

    import threading
    threading.Thread(target=_open_browser, daemon=True).start()

    uvicorn.run(app, host="127.0.0.1", port=FRONTEND_PORT, log_level="warning")
