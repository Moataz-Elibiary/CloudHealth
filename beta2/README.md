# CloudHealth

**CloudHealth** is a high-performance, multi-cluster diagnostic tool designed for Cloud environments. It provides deep observability into OpenShift (OCP) and VIM (CVIM) clusters, as well as host-level health checks for physical nodes.

## 🚀 Key Features

- **Dual Mode Interface**: Run diagnostics from a sleek, interactive Web UI or via the traditional CLI.
- **Deep OCP Diagnostics**: 27 check categories covering Operators, etcd, Storage, Networking, and RBAC.
- **VIM Support**: 19 specialized checks for OpenStack-based cloud environments.
- **Host-Level Audit**: Parallelized SSH-based checks for hardware, OS, and kernel health.
- **Real-time Streaming**: Watch diagnostic progress live via WebSockets.
- **Automated Reporting**: Generates beautiful HTML and plain-text reports for every run.

## 🛠 Installation

1. **Clone the repository**:
   ```bash
   git clone <repo-url>
   cd HealthCheck
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Inventory**:
   Edit `inputs/inventory.xlsx` to add your cluster details (IPs, Credentials, etc.).

## 📖 Usage

### CLI Mode (Default)
Simply run the main script to start a traditional CLI check:
```bash
# Run all checks for all clusters
python healthcheck.py

# Run specific checks (e.g., nodes and etcd)
python healthcheck.py --checks nodes,etcd --verbose
```

### Web UI Mode
Launch the interactive browser-based control center:
```bash
python healthcheck.py --web
```
*Accessible at `http://127.0.0.1:8000`*

## 📂 Project Structure

- `healthcheck.py`: Unified entry point (CLI/UI).
- `web/`: FastAPI backend and Welcome Page.
- `core/`: Multi-threaded engine, inventory loader, and SSH client.
- `checks/`: Logic for OCP, CVIM, and Host-level health checks.
- `reports/`: HTML and Text report generation templates.
- `inputs/`: Place your `inventory.xlsx` here.
- `outputs/`: Generated reports and logs.

## ⚙️ Configuration
Global thresholds (disk, memory, load) and parallelism settings can be tuned in `config.yaml`.

---
© 2026 CloudHealth
