"""
ClusterPulse Backend — runs temporarily on a bastion server.

Lifecycle:
  1. Frontend SFTPs this file + deps to /tmp/cloud_health/
  2. Frontend starts it via SSH exec: python3 /tmp/cloud_health/backend/main.py
  3. Backend listens on localhost only (never exposed externally)
  4. Frontend connects via SSH local port-forward (no new firewall rules)
  5. Frontend sends {action: start_checks, config: {...}}
  6. Backend streams results via WebSocket until all_done
  7. Frontend closes WebSocket → backend cleans up and exits
  8. Heartbeat: if no ping received for 60s → cleanup and exit automatically

Lock file: /tmp/cloud_health/check.lock  (contains PID + timestamp)
All temp files under: /tmp/cloud_health/
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

# Add backend dir to path so imports work when run from /tmp/cloud_health/backend/
sys.path.insert(0, str(Path(__file__).parent))

from config import AppConfig, ClusterConfig
from check_runner import run_cluster
from result import ClusterResult

# ── Constants ─────────────────────────────────────────────────────────────────
WORK_DIR       = Path("/tmp/cloud_health")
LOCK_FILE      = WORK_DIR / "check.lock"
RESULTS_FILE   = WORK_DIR / "results.json"
LOG_FILE       = WORK_DIR / "backend.log"
HEARTBEAT_TIMEOUT = 60   # seconds — exit if no ping received
PORT           = int(os.environ.get("CP_PORT", "8765"))

# ── Logging ───────────────────────────────────────────────────────────────────
WORK_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("backend")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="ClusterPulse Backend")

# Shared state
_results:         list[dict]   = []
_last_heartbeat:  float        = time.monotonic()
_checks_running:  bool         = False


# ── Lock file helpers ─────────────────────────────────────────────────────────

def _write_lock():
    LOCK_FILE.write_text(json.dumps({
        "pid":   os.getpid(),
        "start": datetime.now().isoformat(),
    }))
    log.info(f"Lock file written: {LOCK_FILE}")


def _check_lock() -> Optional[str]:
    """Return error message if a live lock exists, else None."""
    if not LOCK_FILE.exists():
        return None
    try:
        data = json.loads(LOCK_FILE.read_text())
        pid  = data.get("pid", 0)
        start= data.get("start", "unknown")
        # Check if that PID is still alive
        try:
            os.kill(pid, 0)   # signal 0 = existence check, no kill
            return f"Health check already running (PID {pid}, started {start}). Try again in a few minutes."
        except ProcessLookupError:
            # PID is dead — stale lock, remove it
            log.warning(f"Removing stale lock file (PID {pid} not running)")
            LOCK_FILE.unlink(missing_ok=True)
            return None
    except (json.JSONDecodeError, KeyError):
        LOCK_FILE.unlink(missing_ok=True)
        return None


def _cleanup():
    """Remove all temp files and lock."""
    log.info("Cleaning up /tmp/cloud_health/")
    for f in [LOCK_FILE, RESULTS_FILE, LOG_FILE]:
        f.unlink(missing_ok=True)


# ── Heartbeat watchdog ────────────────────────────────────────────────────────

async def _heartbeat_watchdog():
    """Shutdown if no heartbeat ping received within HEARTBEAT_TIMEOUT seconds."""
    global _last_heartbeat
    while True:
        await asyncio.sleep(10)
        elapsed = time.monotonic() - _last_heartbeat
        if elapsed > HEARTBEAT_TIMEOUT:
            log.warning(f"Heartbeat timeout ({elapsed:.0f}s) — shutting down")
            _cleanup()
            os.kill(os.getpid(), signal.SIGTERM)


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global _last_heartbeat, _checks_running

    await ws.accept()
    log.info("WebSocket client connected")

    # Check for existing lock
    lock_err = _check_lock()
    if lock_err:
        await ws.send_json({"type": "error", "message": lock_err})
        await ws.close()
        return

    # Ready handshake
    await ws.send_json({"type": "ready", "message": "Backend ready"})

    # Shared queue — checkers push here, sender task drains here
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    # ── WebSocket sender task ─────────────────────────────────────────────────
    async def sender():
        """Drain the queue and forward every message to the frontend."""
        while True:
            msg = await queue.get()
            if msg is None:   # sentinel — done
                break
            try:
                await ws.send_json(msg)
            except Exception as e:
                log.warning(f"WS send failed: {e}")
            queue.task_done()

    sender_task = asyncio.create_task(sender())

    try:
        # ── Message loop ─────────────────────────────────────────────────────
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=HEARTBEAT_TIMEOUT + 5)
            except asyncio.TimeoutError:
                log.warning("Receive timeout — client gone")
                break
            except WebSocketDisconnect:
                log.info("WebSocket disconnected")
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            action = msg.get("action", "")

            # ── Ping / heartbeat ──────────────────────────────────────────────
            if action == "ping":
                _last_heartbeat = time.monotonic()
                await ws.send_json({"type": "pong"})
                continue

            # ── Start checks ──────────────────────────────────────────────────
            if action == "start_checks":
                if _checks_running:
                    await ws.send_json({"type": "error", "message": "Checks already running"})
                    continue

                config_dict = msg.get("config", {})
                try:
                    app_cfg = AppConfig.from_dict(config_dict)
                except Exception as e:
                    await ws.send_json({"type": "error", "message": f"Bad config: {e}"})
                    continue

                _write_lock()
                _checks_running = True
                _last_heartbeat = time.monotonic()

                await ws.send_json({
                    "type":    "checks_started",
                    "clusters": len(app_cfg.clusters),
                })

                # Run all clusters, collect results, stream via queue
                all_results: list[dict] = []
                sem = asyncio.Semaphore(app_cfg.max_parallel_clusters)

                async def _run_one(cluster: ClusterConfig):
                    async with sem:
                        result = await run_cluster(cluster, app_cfg, queue)
                        all_results.append(result.to_dict())

                await asyncio.gather(*[_run_one(c) for c in app_cfg.clusters])

                # Persist full results to file (resume on reconnect)
                RESULTS_FILE.write_text(json.dumps(all_results, default=str))

                # Signal sender task to flush and stop
                await queue.put(None)
                await sender_task

                _checks_running = False
                LOCK_FILE.unlink(missing_ok=True)

                await ws.send_json({
                    "type":    "all_done",
                    "results": all_results,
                })
                log.info("All checks complete — results sent")

            # ── Resume (client reconnected mid-run or post-run) ───────────────
            elif action == "get_results":
                if RESULTS_FILE.exists():
                    try:
                        results = json.loads(RESULTS_FILE.read_text())
                        await ws.send_json({"type": "all_done", "results": results})
                    except Exception as e:
                        await ws.send_json({"type": "error", "message": f"Could not read results: {e}"})
                elif _checks_running:
                    await ws.send_json({"type": "checks_in_progress"})
                else:
                    await ws.send_json({"type": "no_results"})

    finally:
        # Ensure sender task is cancelled if we exit loop early
        if not sender_task.done():
            await queue.put(None)
            try:
                await asyncio.wait_for(sender_task, timeout=3)
            except asyncio.TimeoutError:
                sender_task.cancel()

        if not _checks_running:
            # Clean exit — remove all temp files
            _cleanup()
            log.info("Backend exiting cleanly")
            # Schedule shutdown
            asyncio.get_event_loop().call_later(1, lambda: os.kill(os.getpid(), signal.SIGTERM))


# ── Health probe (optional — for debugging) ───────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "pid": os.getpid()}


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(_heartbeat_watchdog())
    log.info(f"Backend started — listening on localhost:{PORT}")


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="127.0.0.1",   # localhost only — never exposed externally
        port=PORT,
        log_level="warning",
    )
