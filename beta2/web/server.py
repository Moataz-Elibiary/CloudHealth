"""FastAPI backend for CloudHealth Welcome Page.
Serves the UI, provides config REST API, and streams diagnostics via WebSocket.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# ── Ensure project root is importable ─────────────────────────────────────────
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.inventory import InventoryLoader
from core.engine import HealthCheckEngine
from core.models import Status
from reports.html_reporter import HTMLReporter
from reports.console_reporter import ConsoleReporter
from logger import setup_logger

# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(title="CloudHealth")

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")
WELCOME_HTML = os.path.join(os.path.dirname(__file__), "welcome.html")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "outputs")

# Ensure outputs exists for mounting
os.makedirs(OUTPUT_DIR, exist_ok=True)
app.mount("/reports", StaticFiles(directory=OUTPUT_DIR), name="reports")

# ── Check category registry ──────────────────────────────────────────────────
CHECK_CATEGORIES = {
    "OCP Checks (27)": [
        {"id": "version",       "label": "OCP Version & API"},
        {"id": "operators",     "label": "Cluster Operators"},
        {"id": "nodes",         "label": "Node Status"},
        {"id": "pressure",      "label": "Resource Pressure"},
        {"id": "etcd",          "label": "etcd Health"},
        {"id": "controlplane",  "label": "Control Plane Pods"},
        {"id": "ceph",          "label": "Storage (Ceph/ODF)"},
        {"id": "pvcs",          "label": "PVC / PV Status"},
        {"id": "storageclasses","label": "Storage Classes"},
        {"id": "pods",          "label": "Pods & Restarts Audit"},
        {"id": "deployments",   "label": "Deployments & StatefulSets"},
        {"id": "daemonsets",    "label": "DaemonSets"},
        {"id": "jobs",          "label": "Failed Jobs & CronJobs"},
        {"id": "hpa",           "label": "HPA Capacity"},
        {"id": "network",       "label": "Network / CNI / DNS"},
        {"id": "ingress",       "label": "Ingress & Routes"},
        {"id": "events",        "label": "Events Warning Scan"},
        {"id": "certs",         "label": "Certificate Expiry"},
        {"id": "mcp",           "label": "MachineConfigPools"},
        {"id": "nodeupgrade",   "label": "Node OS & Upgrade"},
        {"id": "quotas",        "label": "Resource Quotas"},
        {"id": "rbac",          "label": "RBAC / SCC Audit"},
        {"id": "alerts",        "label": "Prometheus Alerts"},
        {"id": "logging",       "label": "Cluster Logging"},
        {"id": "registry",      "label": "Image Registry"},
        {"id": "etcdbackup",    "label": "ETCD Backup Freshness"},
        {"id": "clusternetwork","label": "Cluster Network Policy"},
    ],
    "CVIM Checks (19)": [
        {"id": "hypervisors",  "label": "Hypervisor Status"},
        {"id": "cvim_network", "label": "Network Agents"},
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
        {"id": "cvim_ceph",    "label": "Ceph Storage Status"},
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


# ── Helper: strip Rich markup ────────────────────────────────────────────────
def _strip_rich(text: str) -> str:
    return re.sub(r'\[/?[^\]]*\]', '', text).replace("\r", "").strip()


# ── WebSocket Reporter ───────────────────────────────────────────────────────
class WSReporter(ConsoleReporter):
    """
    Streams diagnostic events to a WebSocket client.
    Overrides ConsoleReporter to send JSON-formatted messages instead of printing to stdout.
    """

    def __init__(self, ws: WebSocket):
        super().__init__(verbose=False)
        self.ws = ws
        self._loop = asyncio.get_event_loop()

    async def _ws_send(self, msg: str):
        try:
            await self.ws.send_text(json.dumps({"type": "log", "message": msg}))
        except Exception:
            pass

    def _send(self, msg: str):
        asyncio.ensure_future(self._ws_send(msg))

    def cluster_start(self, name, ctype):
        self._send(f"▶ {ctype.upper()} · {name}")

    def section_start(self, name):
        self._send(f"  ├─ {name} …")

    def section_done(self, s):
        icon = "✔" if s.fail_count == 0 else "✘"
        self._send(f"  ├─ {icon} {s.name}  (P:{s.pass_count} F:{s.fail_count} W:{s.warn_count})")

    def cluster_done(self, r):
        icon = "✔" if r.overall_status == "PASS" else "✘"
        dur = f" ({r.duration_s:.0f}s)" if r.duration_s else ""
        self._send(f"  └─ {icon} {r.cluster_name} — {r.overall_status}{dur}")


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serves the main Welcome Page UI."""
    with open(WELCOME_HTML, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/config")
async def get_config():
    """Return current config.yaml as JSON."""
    with open(CONFIG_PATH, "r") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


@app.post("/api/config")
async def save_config(payload: dict):
    """Write updated config back to config.yaml."""
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(payload, f, default_flow_style=False, sort_keys=False)
    return {"status": "saved"}


@app.get("/api/checks")
async def get_checks():
    """Return all available check categories grouped by type."""
    return CHECK_CATEGORIES


@app.websocket("/ws/run")
async def ws_run(ws: WebSocket):
    """
    WebSocket endpoint that:
    1. Receives run options (selected checks).
    2. Initializes the engine.
    3. Streams progress logs back to the client.
    4. Sends a final summary and report URL upon completion.
    """
    await ws.accept()

    try:
        # Receive run options from client
        raw = await ws.receive_text()
        opts = json.loads(raw)
        enabled_checks = set(opts.get("checks", [])) or None

        # Load config & inventory
        loader = InventoryLoader(CONFIG_PATH)
        settings = loader.get_app_settings()

        output_dir = settings.output_dir
        os.makedirs(output_dir, exist_ok=True)
        logger, _ = setup_logger(output_dir)

        clusters = loader.load_inventory(settings.inventory_file)
        await ws.send_text(json.dumps({
            "type": "log",
            "message": f"📋 Loaded {len(clusters)} cluster(s) from inventory"
        }))

        # Run engine with WS reporter
        reporter = WSReporter(ws)
        engine = HealthCheckEngine(
            clusters=clusters, app=settings, logger=logger,
            console=reporter, enabled_checks=enabled_checks,
        )
        results = await engine.run()

        # Generate reports
        html_reporter = HTMLReporter(results, output_dir)
        report_path = html_reporter.generate()
        html_reporter.generate_text()

        report_url = f"/reports/{os.path.basename(report_path)}"

        # Summary
        total_pass = sum(r.pass_count for r in results)
        total_fail = sum(r.fail_count for r in results)
        total_warn = sum(r.warn_count for r in results)

        await ws.send_text(json.dumps({
            "type": "done",
            "report_url": report_url,
            "report_path": os.path.abspath(report_path),
            "summary": {
                "clusters": len(results),
                "pass": total_pass, "fail": total_fail, "warn": total_warn,
            },
        }))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        await ws.send_text(json.dumps({"type": "error", "message": str(e)}))
    finally:
        try:
            await ws.close()
        except Exception:
            pass
