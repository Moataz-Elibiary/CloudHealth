# ⚡ ClusterPulse — Universal Cluster Health Check

A Python-based health check tool for **CVIM (OpenStack)** and **OCP (OpenShift)** clusters.  
Runs entirely from any machine — no agent installation on target clusters needed.  
Connects via SSH and/or API, runs checks in **parallel**, and generates a rich **HTML report**.

---

## Features

| Feature | Detail |
|---|---|
| **Multi-cluster** | Check dozens of clusters in one run |
| **Parallel execution** | Configurable parallelism (default: 5 clusters at once) |
| **Live console output** | Real-time PASS/FAIL/WARN per check section |
| **HTML report** | Collapsible sections, filterable, searchable |
| **Full command log** | Every SSH command + output logged to file |
| **OCP checks** | Version, Operators, Nodes, Pressure, etcd, Control-Plane, Ceph/ODF, PVCs, Pods, Events, Certs, MCP, Alerts |
| **CVIM checks** | Hypervisors, Network Agents, Volume Services, Cloudpulse, VMs, RabbitMQ, Containers, Ceph |
| **Host-level checks** | Uptime/Load, Disk, ECC errors, Memory/OOM, Network (bond/SR-IOV/NIC), Kernel errors, CPU, systemd services, NTP |
| **Flexible auth** | Password or SSH key, global credential refs |
| **Independent** | Runs from any machine/user — no cluster-side dependencies |

---

## Installation

```bash
# 1. Clone / copy this directory to your machine
cd clusterpulse/

# 2. Install Python dependencies (Python 3.9+ required)
pip install -r requirements.txt

# 3. Copy and fill in your inventory
cp inventory.yaml.example inventory.yaml
vi inventory.yaml
```

---

## Inventory File

The inventory YAML defines clusters and credentials. See `inventory.yaml.example` for a full annotated example.

**Minimum required per cluster:**

```yaml
clusters:
  - name: "my-ocp-cluster"
    type: ocp                             # "ocp" or "cvim"
    installer_host: "192.168.1.10"        # bastion/installer SSH target
    ssh_credentials:
      username: root
      password: "secret"                  # or use private_key: /path/to/key
    api_credentials:
      token: "sha256~yourtoken"           # for OCP: oc login token
```

**SSH to nodes (for host checks)** — either list them explicitly or they are auto-discovered:

```yaml
    nodes:
      - "192.168.1.100"
      - "192.168.1.101"
```

**Global credential references** (reuse one credential across clusters):

```yaml
credentials:
  my_key:
    type: ssh
    username: root
    private_key: /home/me/.ssh/id_rsa

clusters:
  - name: "cluster-A"
    ...
    ssh_credentials: my_key          # reference by name
```

---

## Usage

```bash
# Run all clusters in inventory
python healthcheck.py -i inventory.yaml

# Only OCP clusters
python healthcheck.py -i inventory.yaml --type ocp

# Only CVIM clusters
python healthcheck.py -i inventory.yaml --type cvim

# Run specific check categories (comma-separated)
python healthcheck.py -i inventory.yaml --checks nodes,pods,ceph,host

# Custom output directory
python healthcheck.py -i inventory.yaml -o /mnt/reports/2024-01-15

# Increase parallelism (10 clusters at a time)
python healthcheck.py -i inventory.yaml --parallel 10

# Verbose: show all command output in console
python healthcheck.py -i inventory.yaml --verbose

# Skip HTML report (text logs only)
python healthcheck.py -i inventory.yaml --no-html

# Increase SSH timeout
python healthcheck.py -i inventory.yaml --ssh-timeout 60
```

---

## Output

Each run creates a timestamped output directory:

```
results_20240115_143022/
├── healthcheck_report.html          # Main HTML report (open in browser)
├── healthcheck.log                  # Full execution log (all commands + outputs)
├── prod-ocp-cluster-01_report.txt   # Per-cluster plain-text report
├── prod-cvim-site-A_report.txt
└── ...
```

### HTML Report Features
- **Summary cards** — overall status, cluster counts, totals
- **Cluster blocks** — collapsible, auto-expanded if failures exist
- **Section blocks** — per-check-category with PASS/FAIL/WARN badges
- **Raw command log** — expandable per section
- **Filter bar** — show All / Failures Only / Warnings
- **Search** — live text search across all clusters and checks
- **Expand/Collapse All** buttons

---

## Check Categories

### OCP Checks (`--type ocp`)

| Category flag | Description |
|---|---|
| `version` | Cluster version, API reachability, Available/Progressing/Degraded conditions |
| `operators` | All ClusterOperators healthy |
| `nodes` | Node Ready state, master/worker counts |
| `pressure` | MemoryPressure / DiskPressure / PIDPressure per node |
| `etcd` | etcd pods running, etcdctl endpoint health |
| `controlplane` | apiserver, controller-manager, kube-scheduler pods |
| `ceph` | ODF/Ceph pods, ceph status, OSDs, PGs, StorageCluster CR |
| `pvcs` | PVCs not Lost/Pending |
| `pods` | All-namespace pod audit: status, restart counts, age |
| `events` | Warning events across all namespaces |
| `certs` | TLS certificate expiry (warn <30d, fail if expired) |
| `mcp` | MachineConfigPool not degraded/updating |
| `alerts` | Prometheus critical firing alerts via Thanos/Alertmanager |

### CVIM Checks (`--type cvim`)

| Category flag | Description |
|---|---|
| `hypervisors` | Hypervisor count UP vs configured |
| `network` | Network agent count UP vs expected |
| `volumes` | Cinder volume services UP |
| `cloudpulse` | Cloudpulse result status |
| `vms` | Nova VM active/non-active count |
| `rabbitmq` | RabbitMQ functional tests (rabbit_api.py or mgmt API) |
| `containers` | Podman container count per control/compute/storage node |
| `ceph` | Ceph cluster health, OSD tree |

### Host Checks (both cluster types)

| Category flag | Description |
|---|---|
| `host` | Uptime + load average, Disk utilization, RAM ECC errors, Memory/OOM, Network interfaces (bond/SR-IOV/NIC status), Kernel critical errors, CPU throttling, Failed systemd services, NTP sync |

---

## Security Notes

- **Credentials** are stored in the inventory YAML. Protect the file with appropriate permissions (`chmod 600 inventory.yaml`).
- The tool uses `paramiko` for SSH and does **not** require any agent on target machines.
- SSH host key checking is disabled by default for convenience in lab environments. For production, remove `AutoAddPolicy` in `core/ssh_client.py` and manage `known_hosts`.
- Consider using **SSH private keys** instead of passwords for production use.

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | All checks passed |
| `1` | One or more FAIL results found |

---

## Architecture

```
healthcheck.py          Entry point, arg parsing
core/
  config.py             Inventory YAML loader + ClusterConfig dataclasses
  engine.py             Async orchestrator — runs clusters in parallel
  ssh_client.py         Async SSH wrapper (paramiko + asyncio executor)
  result.py             Result dataclasses (ClusterResult, Section, CheckItem)
  reporter.py           Console (rich) + HTML report generation
  logger.py             File + console logging setup
checks/
  ocp_checks.py         All OCP/OpenShift check functions (13 categories)
  cvim_checks.py        All CVIM/OpenStack check functions (8 categories)
  host_checks.py        Physical host checks via direct SSH (8 categories)
```
