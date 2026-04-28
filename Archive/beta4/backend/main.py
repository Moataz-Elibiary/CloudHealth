"""
Beta4 backend/main.py

Improvements over both betas:
  - Atomic lock: os.O_CREAT | os.O_EXCL (no TOCTOU race)
  - Dynamic port: --port arg, passed by frontend after socket.bind(0) picks it
  - Per-connection subscriber queues: _ACTIVE_SUBSCRIBERS set
  - Event history replay for reconnecting clients
  - Heartbeat watchdog kills process after timeout
  - atexit + SIGTERM/SIGINT cleanup
  - Cross-platform runtime dir via CLOUD_HEALTH_RUNTIME_DIR env var
  - CheckRunner receives subscriber_queue for per-item streaming
"""
from __future__ import annotations
import atexit
import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

BACKEND_DIR = Path(__file__).resolve().parent
VENDOR_DIR  = BACKEND_DIR / "vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from check_runner import CheckRunner


# ── Runtime directory ─────────────────────────────────────────────────────────

def _runtime_dir() -> Path:
    configured = os.environ.get("CLOUD_HEALTH_RUNTIME_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt":
        return Path(tempfile.gettempdir()) / "cloud_health"
    return Path("/tmp/cloud_health")


RUNTIME_DIR  = _runtime_dir()
LOCK_FILE    = RUNTIME_DIR / "hc.lock"
RESULTS_FILE = RUNTIME_DIR / "results.json"

# ── Global state ──────────────────────────────────────────────────────────────
_LOCK_ACQUIRED   = False
_LAST_ACTIVITY   = time.monotonic()
_CHECKS_RUNNING  = False
_LAST_RESULT     = None
_HEARTBEAT_TIMEOUT = 60.0
_EVENT_HISTORY:  list[dict] = []
# Per-connection subscriber queues — supports multiple simultaneous browser tabs
_ACTIVE_SUBSCRIBERS: set[asyncio.Queue] = set()
_RUN_TASK: asyncio.Task | None = None


# ── Lock helpers ──────────────────────────────────────────────────────────────

def _read_lock_payload() -> dict | None:
    try:
        if not LOCK_FILE.exists():
            return None
        payload = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, int):
            return {"pid": payload}
    except Exception:
        pass
    return None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_lock() -> tuple[bool, int | None]:
    """Atomic lock creation using O_CREAT|O_EXCL — no TOCTOU race."""
    global _LOCK_ACQUIRED
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    lock_data = json.dumps({
        "pid":       os.getpid(),
        "timestamp": datetime.now().isoformat(),
        "user":      os.environ.get("USER") or os.environ.get("USERNAME", ""),
    })
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(lock_data)
        _LOCK_ACQUIRED = True
        return True, None
    except FileExistsError:
        payload   = _read_lock_payload()
        owner_pid = payload.get("pid") if isinstance(payload, dict) else None
        if isinstance(owner_pid, str) and owner_pid.isdigit():
            owner_pid = int(owner_pid)
        if isinstance(owner_pid, int) and _pid_exists(owner_pid):
            return False, owner_pid
        # Stale lock — remove and retry once
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            return False, owner_pid
        return _acquire_lock()


def _release_lock():
    global _LOCK_ACQUIRED
    if not _LOCK_ACQUIRED:
        return
    try:
        payload = _read_lock_payload()
        if payload is None or payload.get("pid") == os.getpid():
            LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    _LOCK_ACQUIRED = False


def _cleanup():
    """Full cleanup: lock, results, log."""
    _release_lock()
    RESULTS_FILE.unlink(missing_ok=True)


# ── Results persistence ───────────────────────────────────────────────────────

def _write_results(summary: dict):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(summary, default=str), encoding="utf-8")


def _clear_results():
    global _LAST_RESULT, _EVENT_HISTORY
    _LAST_RESULT   = None
    _EVENT_HISTORY = []
    RESULTS_FILE.unlink(missing_ok=True)


def _load_results() -> dict | None:
    global _LAST_RESULT
    if _LAST_RESULT is not None:
        return _LAST_RESULT
    try:
        if not RESULTS_FILE.exists():
            return None
        payload = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            _LAST_RESULT = payload
            return payload
    except Exception:
        pass
    return None


# ── Broadcast to all subscribers ──────────────────────────────────────────────

async def _broadcast(message: dict, *, record: bool = True):
    """Send to every connected subscriber queue; prune stale ones."""
    if record:
        _EVENT_HISTORY.append(message)
    stale = []
    for q in list(_ACTIVE_SUBSCRIBERS):
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            stale.append(q)
    for q in stale:
        _ACTIVE_SUBSCRIBERS.discard(q)


# ── Check execution task ──────────────────────────────────────────────────────

async def _run_checks_task(config: dict, subscriber_queue: asyncio.Queue):
    global _CHECKS_RUNNING, _LAST_RESULT, _RUN_TASK, _LAST_ACTIVITY, _HEARTBEAT_TIMEOUT
    try:
        app_settings = config.get("app", {})
        ht = app_settings.get("heartbeat_timeout")
        if isinstance(ht, (int, float)):
            _HEARTBEAT_TIMEOUT = float(ht)

        async def on_headline(msg: str):
            await _broadcast({"type": "headline", "message": msg})

        async def on_result(data: dict):
            await _broadcast({"type": "result", "data": data})

        runner = CheckRunner(
            cluster_config   = config.get("cluster", {}),
            app_settings     = app_settings,
            on_headline      = on_headline,
            on_result        = on_result,
            subscriber_queue = subscriber_queue,
        )
        result  = await runner.run()
        summary = result.to_dict()
        _LAST_RESULT = summary
        _write_results(summary)
        await _broadcast({"type": "all_done", "summary": summary}, record=False)
        _LAST_ACTIVITY = time.monotonic()
    except Exception as exc:
        await _broadcast({"type": "error", "message": str(exc)}, record=False)
    finally:
        _CHECKS_RUNNING = False
        _RUN_TASK       = None


# ── Heartbeat watchdog ────────────────────────────────────────────────────────

async def _heartbeat_monitor():
    while True:
        await asyncio.sleep(5)
        elapsed = time.monotonic() - _LAST_ACTIVITY
        if elapsed > _HEARTBEAT_TIMEOUT:
            print(f"[beta4] Heartbeat timeout ({_HEARTBEAT_TIMEOUT}s). Shutting down.",
                  file=sys.stderr)
            _release_lock()
            os.kill(os.getpid(), signal.SIGINT)
            break


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    monitor = asyncio.create_task(_heartbeat_monitor())
    try:
        yield
    finally:
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass
        _release_lock()


app = FastAPI(title="CloudHealth Beta4 Backend", lifespan=lifespan)


def _handle_shutdown(*_):
    _release_lock()
    sys.exit(0)


atexit.register(_release_lock)
for _sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(_sig, _handle_shutdown)


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global _LAST_ACTIVITY, _CHECKS_RUNNING, _RUN_TASK

    await websocket.accept()
    _LAST_ACTIVITY = time.monotonic()

    # One queue per connection — supports multiple tabs simultaneously
    sub_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
    _ACTIVE_SUBSCRIBERS.add(sub_queue)

    async def sender():
        while True:
            msg = await sub_queue.get()
            if msg is None:
                break
            try:
                await websocket.send_json(msg)
            except Exception:
                break

    sender_task = asyncio.create_task(sender())

    try:
        # Ready handshake
        await websocket.send_json({
            "type":        "ready",
            "running":     _CHECKS_RUNNING,
            "has_results": _load_results() is not None,
        })

        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=70)
            except asyncio.TimeoutError:
                break

            _LAST_ACTIVITY = time.monotonic()

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            action = message.get("action")

            # ── Ping ──────────────────────────────────────────────────────────
            if action == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            # ── Get results (reconnect path) ──────────────────────────────────
            if action == "get_results":
                summary = _load_results()
                if summary is not None:
                    # Already done — send the full result immediately
                    await websocket.send_json({"type": "all_done", "summary": summary})
                elif _CHECKS_RUNNING:
                    # Mid-run — send current state then replay history
                    await websocket.send_json({"type": "checks_in_progress"})
                    for event in list(_EVENT_HISTORY):
                        await websocket.send_json(event)
                else:
                    # Nothing here — caller should send start_checks
                    await websocket.send_json({"type": "no_results"})
                continue

            # ── Start checks ──────────────────────────────────────────────────
            if action in {"start", "start_checks"}:
                if _CHECKS_RUNNING:
                    await websocket.send_json({
                        "type": "error", "message": "Checks already running"})
                    continue

                config       = message.get("config", {})
                app_settings = config.get("app", {})
                if not config.get("cluster") or not app_settings:
                    await websocket.send_json({
                        "type": "error", "message": "Invalid config payload"})
                    continue

                _clear_results()
                _CHECKS_RUNNING = True
                await websocket.send_json({"type": "checks_started"})
                _RUN_TASK = asyncio.create_task(
                    _run_checks_task(config, sub_queue))
                continue

    except WebSocketDisconnect:
        pass
    finally:
        _ACTIVE_SUBSCRIBERS.discard(sub_queue)
        if not sender_task.done():
            await sub_queue.put(None)
            try:
                await asyncio.wait_for(sender_task, timeout=2)
            except (asyncio.TimeoutError, Exception):
                sender_task.cancel()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()

    acquired, owner_pid = _acquire_lock()
    if not acquired:
        print(f"[beta4] Backend already running (PID {owner_pid}).",
              file=sys.stderr)
        sys.exit(1)

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="error")
