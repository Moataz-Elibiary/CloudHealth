"""
Beta4 frontend/app.py

Combines:
  - Beta3: /api/checks, /api/config GET+POST, check-selection sidebar support
  - Beta3: glassmorphism UI served from static/index.html
  - Our v2: rich WS message types (check_result, section_done, cluster_done)
  - Our v2: reporter_html.py premium report generation
  - Fixed: dynamic port allocation via allocate_local_port()
  - Fixed: credential sanitisation — only sends SSH creds to the matching bastion
"""
from __future__ import annotations
import asyncio, dataclasses, json, logging, os, sys, threading, uuid, webbrowser
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import uvicorn, yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

BASE_PATH   = Path(__file__).resolve().parent
ROOT_DIR    = BASE_PATH.parent
BACKEND_DIR = ROOT_DIR / "backend"
for _p in (BASE_PATH, BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.config        import ConfigLoader, load_app_config, _AppConfig
from core.tunnel_manager import TunnelManager, sftp_push_backend, allocate_local_port
from core.ws_proxy      import WSProxy
from core.reporter_html import HTMLReporter
from core.result        import ClusterResult

# ── Frontend logging ──────────────────────────────────────────────────────────

class _FrontendRunFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.run_id  = "-"
        self.user_id = "-"

    def filter(self, record):
        record.run_id  = self.run_id
        record.user_id = self.user_id
        return True

_FRONTEND_RUN_FILTER = _FrontendRunFilter()
log = logging.getLogger("frontend.app")


def _setup_frontend_logging(log_dir: Path, max_files: int = 5) -> None:
    """Rotate frontend-YYYYMMDD.log (one per day, keep last max_files days)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    old = sorted(log_dir.glob("frontend-*.log"), key=lambda p: p.stat().st_mtime)
    today_file = log_dir / f"frontend-{datetime.now().strftime('%Y%m%d')}.log"
    # Only count files that are NOT today's file toward the rotation limit
    old_not_today = [p for p in old if p != today_file]
    while len(old_not_today) >= max_files:
        old_not_today.pop(0).unlink(missing_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [run=%(run_id)s] [user=%(user_id)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    handler = logging.FileHandler(today_file, mode="a", encoding="utf-8")
    handler.setFormatter(fmt)
    handler.addFilter(_FRONTEND_RUN_FILTER)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="CloudHealth Beta4")
app.mount("/static", StaticFiles(directory=str(BASE_PATH / "static")), name="static")

tunnel_mgr = TunnelManager()
proxy      = WSProxy()
RUN_LOCK   = asyncio.Lock()
LAST_RESULTS: List[dict] = []
LATEST_REPORT_PATH: Optional[str] = None

VERSION_FILE = ROOT_DIR / "version.txt"

# ── Check categories (served to sidebar) ─────────────────────────────────────
CHECK_CATEGORIES = {
    "OCP Checks (27)": [
        {"id": "version",        "label": "OCP Version & API"},
        {"id": "operators",      "label": "Cluster Operators"},
        {"id": "nodes",          "label": "Node Status"},
        {"id": "pressure",       "label": "Resource Pressure"},
        {"id": "etcd",           "label": "etcd Health"},
        {"id": "controlplane",   "label": "Control Plane Pods"},
        {"id": "ceph",           "label": "Storage (Ceph/ODF)"},
        {"id": "pvcs",           "label": "PVC / PV Status"},
        {"id": "storageclasses", "label": "Storage Classes"},
        {"id": "pods",           "label": "Pods & Restarts Audit"},
        {"id": "deployments",    "label": "Deployments & StatefulSets"},
        {"id": "daemonsets",     "label": "DaemonSets"},
        {"id": "jobs",           "label": "Failed Jobs & CronJobs"},
        {"id": "hpa",            "label": "HPA Capacity"},
        {"id": "network",        "label": "Network / CNI / DNS"},
        {"id": "ingress",        "label": "Ingress & Routes"},
        {"id": "events",         "label": "Events Warning Scan"},
        {"id": "certs",          "label": "Certificate Expiry"},
        {"id": "mcp",            "label": "MachineConfigPools"},
        {"id": "nodeupgrade",    "label": "Node OS & Upgrade"},
        {"id": "quotas",         "label": "Resource Quotas"},
        {"id": "rbac",           "label": "RBAC / SCC Audit"},
        {"id": "alerts",         "label": "Prometheus Alerts"},
        {"id": "logging",        "label": "Cluster Logging"},
        {"id": "registry",       "label": "Image Registry"},
        {"id": "etcdbackup",     "label": "ETCD Backup Freshness"},
        {"id": "clusternetwork", "label": "Cluster Network Policy"},
    ],
    "CVIM Checks (19)": [
        {"id": "hypervisors",  "label": "Hypervisor Status"},
        {"id": "network",      "label": "Network Agents"},
        {"id": "volumes",      "label": "Volume Services (Cinder/Ceph)"},
        {"id": "compute_svc",  "label": "Compute Services (Nova)"},
        {"id": "identity",     "label": "Identity Services (Keystone)"},
        {"id": "image_svc",    "label": "Image Service (Glance)"},
        {"id": "cloudpulse",   "label": "Cloudpulse Health"},
        {"id": "vms",          "label": "VM (Nova) Status"},
        {"id": "vm_errors",    "label": "VM Error Audit"},
        {"id": "rabbitmq",     "label": "RabbitMQ Health"},
        {"id": "mariadb",      "label": "MariaDB / Galera Cluster"},
        {"id": "memcached",    "label": "Memcached Status"},
        {"id": "containers",   "label": "Container Status on Nodes"},
        {"id": "ceph",         "label": "Ceph Storage Status"},
        {"id": "ceph_pools",   "label": "Ceph Pool Health"},
        {"id": "ovs",          "label": "OVS / Networking Status"},
        {"id": "haproxy",      "label": "HAProxy / VIP Status"},
        {"id": "nfs",          "label": "NFS / External Storage"},
        {"id": "installer",    "label": "CVIM Installer Status"},
    ],
    "Host Checks (19)": [
        {"id": "uptime",       "label": "Uptime & Load Average"},
        {"id": "os_info",      "label": "OS & Kernel Info"},
        {"id": "cpu",          "label": "CPU Info & Throttling"},
        {"id": "memory",       "label": "Memory (RAM + Swap + OOM)"},
        {"id": "disk",         "label": "Disk Usage & SMART"},
        {"id": "ecc",          "label": "ECC Memory Errors"},
        {"id": "host_network", "label": "Network Interfaces"},
        {"id": "bond",         "label": "Bond Status"},
        {"id": "sriov",        "label": "SR-IOV"},
        {"id": "kernel_msgs",  "label": "Kernel Messages (dmesg)"},
        {"id": "services",     "label": "Systemd Services"},
        {"id": "ntp",          "label": "NTP Time Sync"},
        {"id": "pcie",         "label": "PCIe / AER Errors"},
        {"id": "firmware",     "label": "Firmware Versions"},
        {"id": "numa",         "label": "NUMA Topology"},
        {"id": "hugepages",    "label": "Hugepages"},
        {"id": "selinux",      "label": "SELinux Status"},
        {"id": "firewall",     "label": "Firewall Rules"},
        {"id": "ports",        "label": "Open Ports"},
    ],
}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(BASE_PATH / "static" / "index.html"))


@app.get("/report/latest", response_class=HTMLResponse)
async def report_latest():
    if LATEST_REPORT_PATH and Path(LATEST_REPORT_PATH).exists():
        return FileResponse(LATEST_REPORT_PATH)
    return HTMLResponse("<h2>No report generated yet.</h2>")


@app.get("/report/email", response_class=HTMLResponse)
async def report_email():
    if LATEST_REPORT_PATH:
        email_path = LATEST_REPORT_PATH.replace(
            "healthcheck_report.html", "healthcheck_email.html")
        if Path(email_path).exists():
            return FileResponse(email_path)
    return HTMLResponse("<h2>No email report found.</h2>")


@app.get("/api/results")
async def api_results():
    return {"results": LAST_RESULTS, "latest_report": LATEST_REPORT_PATH}


@app.get("/api/checks")
async def api_checks():
    return CHECK_CATEGORIES


@app.get("/api/config")
async def api_config_get():
    config_path = ROOT_DIR / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/config")
async def api_config_post(request: Request):
    try:
        new_config  = await request.json()
        config_path = ROOT_DIR / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(new_config, f, default_flow_style=False, sort_keys=False)
        return {"status": "success"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/version")
async def api_version():
    ver = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "dev"
    return {"version": ver}


# ── WebSocket UI endpoint ─────────────────────────────────────────────────────

@app.websocket("/ws/ui")
async def ui_websocket(websocket: WebSocket):
    await websocket.accept()
    active_run: Optional[asyncio.Task] = None
    try:
        while True:
            raw     = await websocket.receive_text()
            message = json.loads(raw)
            if message.get("action") == "start_all":
                if active_run and not active_run.done():
                    await _safe_send(websocket, {
                        "type":    "error",
                        "message": "A run is already active for this session.",
                    })
                    continue
                enabled = message.get("enabled_checks")
                active_run = asyncio.create_task(
                    _run_guarded(websocket, enabled))
    except WebSocketDisconnect:
        if active_run and not active_run.done():
            active_run.cancel()


async def _safe_send(ws: WebSocket, payload: dict) -> bool:
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        return False


async def _run_guarded(ui_ws: WebSocket, enabled_checks):
    if RUN_LOCK.locked():
        await _safe_send(ui_ws, {
            "type": "error",
            "message": "Another diagnostics run is already in progress.",
        })
        return
    async with RUN_LOCK:
        await _safe_send(ui_ws, {"type": "run_state", "state": "started"})
        try:
            await _run_all_clusters(ui_ws, enabled_checks)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await _safe_send(ui_ws, {"type": "error", "message": str(exc)})
        finally:
            await _safe_send(ui_ws, {"type": "run_state", "state": "finished"})


async def _run_all_clusters(ui_ws: WebSocket, enabled_checks=None):
    global LAST_RESULTS, LATEST_REPORT_PATH
    LAST_RESULTS        = []
    LATEST_REPORT_PATH  = None

    # Generate correlation IDs for this run
    run_id  = str(uuid.uuid4())
    try:
        user_id = os.getlogin()
    except Exception:
        user_id = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    _FRONTEND_RUN_FILTER.run_id  = run_id
    _FRONTEND_RUN_FILTER.user_id = user_id

    log.info("Run started — clusters initialising")

    config_path = ROOT_DIR / "config" / "config.yaml"
    if not config_path.exists():
        config_path = ROOT_DIR / "config.yaml"
    loader      = ConfigLoader(str(config_path))
    app_settings = loader.get_app_settings()

    if enabled_checks is not None:
        app_settings.enabled_checks = set(enabled_checks)

    inventory_name = app_settings.inventory_file or "inventory.xlsx"
    try:
        clusters = loader.load_inventory(inventory_name)
    except Exception as e:
        log.error("Inventory load failed for '%s': %s", inventory_name, e)
        await _safe_send(ui_ws, {
            "type":    "error",
            "message": f"Inventory load failed for '{inventory_name}': {e}",
        })
        return

    if not clusters:
        log.warning("No enabled clusters found in '%s'", inventory_name)
        await _safe_send(ui_ws, {
            "type":    "error",
            "message": f"No enabled clusters found in '{inventory_name}'.",
        })
        return

    output_dir = Path(app_settings.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT_DIR / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    log.info("Orchestrating %d cluster(s) [parallel_limit=%d]",
             len(clusters), app_settings.parallel_limit or 5)
    await _safe_send(ui_ws, {
        "type":          "run_state",
        "state":         "running",
        "cluster_count": len(clusters),
        "message":       f"Orchestrating {len(clusters)} cluster(s)…",
    })

    run_start = datetime.now()
    sem   = asyncio.Semaphore(app_settings.parallel_limit or 5)
    tasks = [_run_with_sem(sem, ui_ws, c, app_settings, output_dir, run_id, user_id)
             for c in clusters]
    results = [r for r in await asyncio.gather(*tasks) if r]
    LAST_RESULTS = results

    elapsed = (datetime.now() - run_start).total_seconds()
    log.info("All clusters finished in %.1fs — %d result(s) collected", elapsed, len(results))

    if results:
        reporter     = HTMLReporter([ClusterResult.from_dict(r) for r in results],
                                    output_dir)
        report_path  = reporter.generate()
        email_path   = reporter.generate_email()
        LATEST_REPORT_PATH = str(report_path.resolve())
        log.info("Report generated: %s", LATEST_REPORT_PATH)
        await _safe_send(ui_ws, {
            "type":   "reports_ready",
            "path":   LATEST_REPORT_PATH,
            "count":  len(results),
        })

    _FRONTEND_RUN_FILTER.run_id  = "-"
    _FRONTEND_RUN_FILTER.user_id = "-"


async def _run_with_sem(sem, ui_ws, cluster, app_settings, output_dir, run_id, user_id):
    async with sem:
        return await _run_single_cluster(ui_ws, cluster, app_settings, output_dir, run_id, user_id)


async def _run_single_cluster(ui_ws, cluster, app_settings, output_dir, run_id, user_id):
    name       = cluster.name
    local_port = allocate_local_port()
    ssh        = None
    t_start    = datetime.now()

    log.info("Cluster '%s' — starting", name)
    try:
        await _safe_send(ui_ws, {
            "type": "cluster_status", "cluster": name, "status": "Connecting"})

        # 1. SSH + tunnel
        key_path = _resolve_key(cluster.ssh_key)
        ssh = await tunnel_mgr.connect_and_tunnel(
            name, cluster.installer_ip, cluster.ssh_user,
            cluster.ssh_pass, key_path=key_path,
            remote_port=app_settings.backend_port, local_port=local_port,
        )

        # 1.5 Lock-state pre-check — bail out before SFTP if a live owner
        # already holds /tmp/cloud_health/hc.lock on the bastion. Surfaces a
        # clear "another run in progress" error instead of a silent backend
        # exit on launch.
        existing_lock = await _probe_lock_state(ssh)
        if existing_lock:
            owner_pid  = existing_lock.get("pid", "?")
            owner_user = existing_lock.get("user") or "?"
            owner_ts   = existing_lock.get("timestamp") or "?"
            raise RuntimeError(
                f"Another run is in progress on the bastion "
                f"(PID {owner_pid}, started {owner_ts} by {owner_user}). "
                f"Wait for it to finish, or remove /tmp/cloud_health/hc.lock "
                f"on the bastion if it is stale.")

        # 2. Push backend (version-aware)
        await _safe_send(ui_ws, {
            "type": "cluster_status", "cluster": name, "status": "Pushing Backend"})
        pushed = await sftp_push_backend(ssh, str(ROOT_DIR / "backend"))
        if pushed:
            await _safe_send(ui_ws, {
                "type": "cluster_status", "cluster": name,
                "status": "Installing Dependencies"})
            _, stdout, stderr = await asyncio.to_thread(
                ssh.exec_command,
                "python3 -m pip install --no-index "
                "--find-links /tmp/cloud_health/vendor/ "
                "-r /tmp/cloud_health/requirements.txt --quiet"
            )
            exit_code = await asyncio.to_thread(stdout.channel.recv_exit_status)
            if exit_code != 0:
                err = (await asyncio.to_thread(stderr.read)).decode().strip()
                raise RuntimeError(f"pip install on bastion failed: {err}")
            await _safe_send(ui_ws, {
                "type": "headline", "cluster": name,
                "message": "Backend updated and dependencies installed."})

        # 3. Launch backend on bastion
        launch_cmd = (
            f"mkdir -p /tmp/cloud_health/log && "
            f"nohup sh -c 'python3 /tmp/cloud_health/main.py "
            f"--port {app_settings.backend_port} "
            f"--max-log-files {app_settings.max_log_files} 2>&1' "
            f"> /tmp/cloud_health/backend.log 2>&1 &"
        )
        await asyncio.to_thread(ssh.exec_command, launch_cmd)

        # 4. Wait for backend to become ready
        try:
            await _wait_for_port(local_port)
        except TimeoutError as exc:
            # Read the backend log tail BEFORE killing the process — once we
            # pkill, anything useful in stderr may be gone.
            log_tail = await _read_backend_log_tail(ssh, lines=50)
            # Backend may have started and acquired the lock but is not
            # responding. Kill it and delete the lock while SSH is still open
            # so the next run is not blocked.
            await asyncio.to_thread(
                ssh.exec_command,
                "pkill -f '/tmp/cloud_health/main.py' 2>/dev/null; "
                "rm -f /tmp/cloud_health/hc.lock"
            )
            if log_tail:
                raise TimeoutError(
                    f"{exc}\n--- backend.log (last 50 lines) ---\n{log_tail}")
            raise

        # 5. Proxy WS — credential-sanitised payload
        await _safe_send(ui_ws, {
            "type": "cluster_status", "cluster": name, "status": "RUNNING"})

        config_payload = {
            "cluster": cluster.to_dict(),           # full creds for THIS bastion
            "app":     app_settings.to_dict(),
            "run_id":  run_id,
            "user_id": user_id,
        }

        summary = await proxy.proxy_cluster(
            ui_ws, local_port, name, config_payload)

        if summary:
            elapsed = (datetime.now() - t_start).total_seconds()
            log.info("Cluster '%s' — completed in %.1fs", name, elapsed)
            return summary

    except Exception as e:
        elapsed = (datetime.now() - t_start).total_seconds()
        log.error("Cluster '%s' — failed after %.1fs: %s", name, elapsed, e)
        await _safe_send(ui_ws, {
            "type": "cluster_status", "cluster": name, "status": "ERROR"})
        await _safe_send(ui_ws, {
            "type": "error", "cluster": name, "message": str(e)})
    finally:
        if ssh is not None:
            await tunnel_mgr.close(ssh)
    return None


def _resolve_key(key_path: Optional[str]) -> Optional[str]:
    if not key_path:
        return None
    p = Path(key_path).expanduser()
    if not p.is_absolute():
        p = ROOT_DIR / p
    return str(p.resolve())


async def _wait_for_port(port: int, timeout_s: float = 20.0):
    import socket as _socket
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        try:
            await asyncio.to_thread(
                lambda: _socket.create_connection(("127.0.0.1", port), 1.0).close())
            return
        except OSError:
            await asyncio.sleep(0.5)
    raise TimeoutError(
        f"Backend on port {port} did not become ready within {timeout_s:.0f}s. "
        f"Check /tmp/cloud_health/backend.log on the bastion.")


async def _probe_lock_state(ssh) -> Optional[dict]:
    """Read /tmp/cloud_health/hc.lock over the existing SSH session.

    Returns the lock payload dict if a live owner holds the lock, else None.
    A stale lock (owner PID no longer alive) is treated as no lock — the
    backend will clean it up itself on next launch.
    """
    try:
        _, stdout, _ = await asyncio.to_thread(
            ssh.exec_command, "cat /tmp/cloud_health/hc.lock 2>/dev/null")
        raw = (await asyncio.to_thread(stdout.read)).decode(errors="replace").strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        pid = payload.get("pid")
        if isinstance(pid, str) and pid.isdigit():
            pid = int(pid)
        if not isinstance(pid, int):
            return None
        _, stdout2, _ = await asyncio.to_thread(
            ssh.exec_command, f"kill -0 {pid} 2>/dev/null && echo alive || echo dead")
        status = (await asyncio.to_thread(stdout2.read)).decode(errors="replace").strip()
        if status != "alive":
            return None
        return payload
    except Exception:
        return None


async def _read_backend_log_tail(ssh, lines: int = 50) -> str:
    """Read the last N lines of /tmp/cloud_health/backend.log over SSH.

    Returns an empty string if the log is missing or unreadable.
    """
    try:
        _, stdout, _ = await asyncio.to_thread(
            ssh.exec_command,
            f"tail -n {int(lines)} /tmp/cloud_health/backend.log 2>/dev/null")
        return (await asyncio.to_thread(stdout.read)).decode(errors="replace").rstrip()
    except Exception:
        return ""


# ── Entry point ───────────────────────────────────────────────────────────────

def start(app_config=None):
    max_log_files = 5
    for _cp in (ROOT_DIR / "config" / "config.yaml", ROOT_DIR / "config.yaml"):
        if _cp.exists():
            try:
                _cfg = yaml.safe_load(_cp.read_text()) or {}
                max_log_files = int(_cfg.get("max_log_files", 5))
            except Exception:
                pass
            break
    log_dir = Path.home() / "Documents" / "cloud_health" / "log"
    _setup_frontend_logging(log_dir, max_files=max_log_files)
    threading.Timer(
        1.5, lambda: webbrowser.open("http://127.0.0.1:8080")).start()
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")


if __name__ == "__main__":
    start()
