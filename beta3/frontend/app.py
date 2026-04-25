import asyncio
import sys
import json
import socket
import webbrowser
import threading
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
import dataclasses

BASE_PATH = Path(__file__).resolve().parent
ROOT_DIR = BASE_PATH.parent
BACKEND_DIR = ROOT_DIR / "backend"
for candidate in (BASE_PATH, BACKEND_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

# Internal modular imports
from config_loader import ConfigLoader
from tunnel_manager import TunnelManager, sftp_push_backend
from ws_proxy import WSProxy
from report_generator import ReportGenerator

app = FastAPI(title="CloudHealth Beta 3 - Orchestrator")
app.mount("/static", StaticFiles(directory=str(BASE_PATH / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_PATH / "static"))

# Orchestration components
tunnel_mgr = TunnelManager()
proxy = WSProxy()
RUN_LOCK = asyncio.Lock()
LAST_RESULTS = []
LATEST_REPORT_PATH = None


async def _safe_send_json(websocket: WebSocket, payload: dict):
    try:
        await websocket.send_json(payload)
        return True
    except Exception:
        return False


def _resolve_local_key_path(key_path: str | None) -> str | None:
    if not key_path:
        return None
    candidate = Path(key_path).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT_DIR / candidate
    return str(candidate.resolve())

@app.get("/")
async def get_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/results")
async def api_results():
    return {"results": LAST_RESULTS, "latest_report": LATEST_REPORT_PATH}


@app.get("/report/latest", response_class=HTMLResponse)
async def report_latest():
    if LATEST_REPORT_PATH and Path(LATEST_REPORT_PATH).exists():
        return FileResponse(LATEST_REPORT_PATH)
    return HTMLResponse("<h2>No report generated yet. Run diagnostics first.</h2>")

@app.websocket("/ws/ui")
async def ui_websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_run = None
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            if message.get("action") == "start_all":
                if active_run and not active_run.done():
                    await _safe_send_json(
                        websocket,
                        {"type": "error", "message": "A diagnostics run is already active for this session."},
                    )
                    continue
                active_run = asyncio.create_task(_run_all_clusters_guarded(websocket))
    except WebSocketDisconnect:
        if active_run and not active_run.done():
            active_run.cancel()
        pass

async def _run_all_clusters_guarded(ui_ws: WebSocket):
    if RUN_LOCK.locked():
        await _safe_send_json(
            ui_ws,
            {"type": "error", "message": "Another diagnostics run is already in progress."},
        )
        return

    async with RUN_LOCK:
        await _safe_send_json(ui_ws, {"type": "run_state", "state": "started"})
        try:
            await run_all_clusters(ui_ws)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await _safe_send_json(ui_ws, {"type": "error", "message": str(exc)})
        finally:
            await _safe_send_json(ui_ws, {"type": "run_state", "state": "finished"})


async def run_all_clusters(ui_ws: WebSocket):
    global LAST_RESULTS, LATEST_REPORT_PATH
    LAST_RESULTS = []
    LATEST_REPORT_PATH = None
    config_path = ROOT_DIR / "config.yaml"
    loader = ConfigLoader(str(config_path))
    app_settings = loader.get_app_settings()
    inventory_name = app_settings.inventory_file or "inventory.xlsx"

    try:
        clusters = loader.load_inventory(inventory_name)
    except Exception as e:
        await ui_ws.send_json({"type": "error", "message": f"Inventory load failed for '{inventory_name}': {str(e)}"})
        return

    if not clusters:
        await ui_ws.send_json(
            {
                "type": "error",
                "message": f"No enabled clusters found in '{inventory_name}'.",
            }
        )
        return

    output_dir = Path(app_settings.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT_DIR / output_dir
    reporter = ReportGenerator(output_dir=str(output_dir))
    await ui_ws.send_json(
        {
            "type": "run_state",
            "state": "running",
            "cluster_count": len(clusters),
            "message": f"Orchestrating {len(clusters)} clusters...",
        }
    )
    
    # Run in parallel with a semaphore to avoid overloading the user machine
    semaphore = asyncio.Semaphore(app_settings.parallel_limit or 5)
    
    async def run_with_sem(cluster):
        async with semaphore:
            return await run_single_cluster(ui_ws, cluster, app_settings, reporter)

    tasks = [run_with_sem(cluster) for cluster in clusters]
    results = [result for result in await asyncio.gather(*tasks) if result]
    LAST_RESULTS = results

    if results:
        combined_report = reporter.generate_combined_report(results)
        LATEST_REPORT_PATH = str(Path(combined_report).resolve())
        await ui_ws.send_json({"type": "reports_ready", "path": LATEST_REPORT_PATH, "count": len(results)})

def _allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _probe_port(port: int):
    with socket.create_connection(("127.0.0.1", port), 1.0):
        return


async def _wait_for_port(port: int, timeout_s: float = 15.0):
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        try:
            await asyncio.to_thread(_probe_port, port)
            return
        except OSError:
            await asyncio.sleep(0.5)
    raise TimeoutError(f"Backend on local port {port} did not become ready within {timeout_s:.1f}s. Check bastion logs at /tmp/cloud_health/backend.log")


async def run_single_cluster(ui_ws: WebSocket, cluster, app_settings, reporter: ReportGenerator):
    cluster_name = cluster.name
    local_port = _allocate_local_port()
    ssh = None
    key_path = _resolve_local_key_path(cluster.ssh_key)
    
    try:
        await ui_ws.send_json({"type": "cluster_status", "cluster": cluster_name, "status": "Connecting"})
        
        # 1. Setup Tunnel
        ssh = await tunnel_mgr.connect_and_tunnel(
            cluster_name,
            cluster.installer_ip,
            cluster.ssh_user,
            cluster.ssh_pass,
            key_path=key_path,
            remote_port=app_settings.backend_port, local_port=local_port
        )
        
        # 2. Sync Backend
        await ui_ws.send_json({"type": "cluster_status", "cluster": cluster_name, "status": "Pushing Backend"})
        backend_local = ROOT_DIR / "backend"
        await sftp_push_backend(ssh, str(backend_local), "/tmp/cloud_health")
        
        # 3. Launch Backend
        # We try both python3 and python to maximize compatibility
        launch_cmd = (
            "mkdir -p /tmp/cloud_health && "
            f"nohup sh -c 'python3 /tmp/cloud_health/main.py --port {app_settings.backend_port} "
            f"|| python /tmp/cloud_health/main.py --port {app_settings.backend_port}' "
            "> /tmp/cloud_health/backend.log 2>&1 &"
        )
        await asyncio.to_thread(ssh.exec_command, launch_cmd)
        
        try:
            await _wait_for_port(local_port)
        except TimeoutError as te:
            # Check if it's already running (lock conflict)
            _, stdout, _ = ssh.exec_command("cat /tmp/cloud_health/hc.lock 2>/dev/null")
            lock_content = stdout.read().decode()
            if lock_content:
                raise Exception(f"Backend is already locked by another process: {lock_content}")
            raise te
        
        # 4. Proxy WebSocket
        await ui_ws.send_json({"type": "cluster_status", "cluster": cluster_name, "status": "RUNNING"})
        config_payload = {
            "cluster": dataclasses.asdict(cluster),
            "app": dataclasses.asdict(app_settings)
        }
        if isinstance(config_payload["app"].get("enabled_checks"), set):
            config_payload["app"]["enabled_checks"] = sorted(config_payload["app"]["enabled_checks"])
        summary = await proxy.proxy_cluster(ui_ws, local_port, cluster_name, config_payload)
        if summary:
            report_path = reporter.generate_html_report(cluster_name, summary)
            await ui_ws.send_json(
                {
                    "type": "report",
                    "cluster": cluster_name,
                    "path": str(Path(report_path).resolve()),
                }
            )
            return summary
        
    except Exception as e:
        await ui_ws.send_json({"type": "cluster_status", "cluster": cluster_name, "status": "ERROR"})
        await ui_ws.send_json({"type": "error", "cluster": cluster_name, "message": str(e)})
    finally:
        if ssh is not None:
            await tunnel_mgr.close(ssh)
    return None

if __name__ == "__main__":
    # Auto-open dashboard
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8080")).start()
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="error")
