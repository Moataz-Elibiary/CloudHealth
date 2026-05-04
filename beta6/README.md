# CloudHealth

**A fully headless cluster health-check tool for central Linux servers.**

CloudHealth runs entirely from a single central Linux server that SSHes directly to every bastion. There is no browser UI, no WebSocket streaming, no bootstrapper, and no code pushed to any bastion. Everything runs in one place, on one schedule.

---

## What you get

- **65+ built-in health checks** across OpenShift (OCP), Cisco VIM (CVIM), and physical hosts
- **Full HTML report** written locally at the end of every run, with command detail panels
- **Email-friendly report** for sharing summaries
- **Run history** in a single central SQLite database — browse past runs without re-running
- **Pre-flight validation** — SSH + auth + CLI availability checks before committing to a full run
- **Per-cluster threshold overrides** in the inventory file
- **Configurable check filters** — run only the check categories you care about
- **Parallel execution** — multiple clusters and multiple nodes checked simultaneously
- **Graceful cancellation** — SIGINT / SIGTERM saves partial results cleanly
- **Log rotation** — keeps the newest N system, command, and host logs automatically
- **Report rotation** — keeps the newest N HTML reports automatically

---

## Quick start

### 1. Install dependencies

```bash
pip install paramiko PyYAML
```

### 2. Set up configuration

```bash
mkdir -p ~/cloud_health/{config,reports,logs,db}
cp config/config.yaml   ~/cloud_health/config/config.yaml
cp config/inventory.yaml ~/cloud_health/config/inventory.yaml
```

Edit `config.yaml` for your paths and thresholds, then populate `inventory.yaml` with your clusters.

### 3. Run

```bash
python run.py
```

---

## CLI reference

```
python run.py [options]
```

### Input / output

| Flag | Description |
|---|---|
| `-c, --config PATH` | Path to `config.yaml` (default: `~/cloud_health/config/config.yaml`) |
| `-i, --inventory PATH` | Path to `inventory.yaml` (overrides the path in config.yaml) |
| `-o, --output DIR` | Directory to write HTML reports (overrides `output_dir` in config.yaml) |

### Cluster selection

| Flag | Description |
|---|---|
| `--clusters A,B,C` | Run only the named clusters (comma-separated) |
| `--failed-only` | Re-run only clusters that had failures in the previous run |

### Check filtering

| Flag | Description |
|---|---|
| `--check-types ocp,host` | Limit to specific cluster types (`ocp`, `cvim`, `host`) |
| `--ocp-checks nodes,etcd` | Run only these OCP check categories (comma-separated) |
| `--cvim-checks ceph,vms` | Run only these CVIM check categories |
| `--host-checks disk,memory` | Run only these host check categories |

### Run control

| Flag | Description |
|---|---|
| `--preflight-only` | Run pre-flight validation only; do not start health checks |
| `--ignore-preflight` | Proceed even if pre-flight fails for some clusters |
| `--dry-run` | Regenerate HTML reports from the last DB run without SSHing anywhere |
| `--parallel N` | Override `parallel_limit` from config.yaml |

### Information

| Flag | Description |
|---|---|
| `--list-history [N]` | Print the last N runs from the history DB (default: 20) |
| `--version` | Print the tool version and exit |
| `--verbose` | Enable verbose logging (SSH commands, raw output) |

### Examples

```bash
# Full run against all enabled clusters
python run.py

# Run only OCP clusters, only node and etcd checks
python run.py --check-types ocp --ocp-checks nodes,etcd

# Re-run only the clusters that failed last time
python run.py --failed-only

# Validate connectivity to all clusters without running checks
python run.py --preflight-only

# Regenerate the HTML reports from the last run (no SSH)
python run.py --dry-run

# Limit concurrency for a congested network
python run.py --parallel 2

# Check specific clusters with verbose output
python run.py --clusters prod-ocp-east,prod-cvim-west --verbose
```

---

## Configuration (`config.yaml`)

All keys are optional — defaults are shown below.

```yaml
# Parallelism
parallel_limit:     5      # max clusters checked simultaneously
max_parallel_nodes: 10     # max nodes checked per cluster simultaneously
ssh_timeout:        30     # SSH connection timeout (seconds)
cmd_timeout:        60     # per-command timeout (seconds)

# Paths (~ is expanded at runtime)
output_dir:     ~/cloud_health/reports
log_dir:        ~/cloud_health/logs
db_path:        ~/cloud_health/db/history.db
inventory_file: ~/cloud_health/config/inventory.yaml

# Log and report retention
max_log_files:    5        # newest N log files to keep per prefix
max_report_files: 10       # newest N HTML report files to keep

# History
history_max_runs: 200      # runs to retain in the SQLite database

# Thresholds (global defaults; override per-cluster in inventory.yaml)
thresholds:
  disk_percent:  85        # disk_threshold
  mem_warn:      80        # mem_used_pct_warn
  mem_fail:      95        # mem_used_pct_fail
  load_warn:     2.0       # load_ratio_warn (load ÷ CPU count)
  load_fail:     5.0       # load_ratio_fail
  swap_warn:     30        # swap_used_pct_warn

# OCP-specific thresholds
cert_warn_days:          30   # warn if TLS certificate expires within N days
restart_warn_threshold:   5   # pod restart count for WARN
restart_fail_threshold:  20   # pod restart count for FAIL
pod_age_min_warn:         5   # warn if pod is younger than N minutes
pod_age_min_fail:         2   # fail if pod is younger than N minutes

# Check filters — uncomment to restrict which categories run
# enabled_ocp_checks:
#   - nodes
#   - etcd
#   - pods

# enabled_cvim_checks:
#   - hypervisors
#   - ceph

# enabled_host_checks:
#   - disk
#   - memory
#   - services
```

---

## Inventory (`inventory.yaml`)

```yaml
clusters:
  - name: prod-ocp-east
    type: ocp                          # ocp or cvim
    installer_ip: 10.0.1.10            # bastion / installer IP
    ssh_user: core
    ssh_key: ~/.ssh/id_rsa             # or ssh_pass: "password"
    enabled: true

    # Optional per-cluster threshold overrides
    disk_threshold: 90
    mem_used_pct_warn: 85

    # Optional: list nodes explicitly for host checks
    # If omitted, nodes are discovered automatically from the cluster
    nodes:
      - ip: 10.0.1.11
        username: core
        key_path: ~/.ssh/id_rsa
      - ip: 10.0.1.12
        username: core
        key_path: ~/.ssh/id_rsa

  - name: prod-cvim-west
    type: cvim
    installer_ip: 10.0.2.10
    ssh_user: root
    ssh_pass: "s3cret"
    enabled: true

  - name: staging-ocp
    type: ocp
    installer_ip: 10.0.3.10
    ssh_user: core
    ssh_key: ~/.ssh/staging_key
    enabled: false                     # disabled — skipped at load time
```

### Per-cluster threshold overrides

Any threshold you set on a cluster overrides the global `config.yaml` value for that cluster only.

| Key | Description |
|---|---|
| `disk_threshold` | Disk usage % threshold |
| `mem_used_pct_warn` | Memory warn % |
| `mem_used_pct_fail` | Memory fail % |
| `load_ratio_warn` | Load ratio warn (load ÷ CPUs) |
| `load_ratio_fail` | Load ratio fail |
| `swap_used_pct_warn` | Swap usage warn % |

---

## Available checks

### OCP checks (27)

`nodes`, `node_conditions`, `etcd`, `etcd_members`, `pods`, `operators`,
`clusterversion`, `alerts`, `certs`, `pvc`, `storage`, `network`,
`dns`, `ingress`, `machine_config`, `node_resources`, `events`,
`api_server`, `scheduler`, `controller_manager`, `image_registry`,
`monitoring`, `logging`, `auth`, `rbac`, `quota`, `upgrade`

### CVIM checks (19)

`hypervisors`, `vms`, `networks`, `volumes`, `images`, `compute`,
`identity`, `nova`, `neutron`, `cinder`, `glance`, `keystone`,
`rabbitmq`, `mariadb`, `ceph`, `ceph_osd`, `haproxy`, `nfv`, `placement`

### Host checks (19)

`uptime`, `os_info`, `cpu`, `memory`, `disk`, `ecc`,
`host_network`, `bond`, `sriov`, `kernel_msgs`, `services`,
`ntp`, `pcie`, `firmware`, `numa`, `hugepages`, `selinux`,
`firewall`, `ports`

---

## Status icons

| Status | Meaning |
|---|---|
| **PASS** | Check completed; value is healthy |
| **WARN** | Check completed; value is concerning but not critical |
| **FAIL** | Check completed; value is critical |
| **ERROR** | Check could not complete (SSH failure, missing command) |
| **INFO** | Informational — not a problem |
| **SKIP** | Not applicable to this cluster type |

---

## How it works

```
┌─────────────────────────────────────────────────────────────┐
│  CENTRAL LINUX SERVER                                        │
│                                                              │
│  run.py                                                      │
│   │                                                          │
│   ├── Pre-flight: SSH to each bastion, probe CLI             │
│   │                                                          │
│   ├── Per cluster (parallel, up to parallel_limit):          │
│   │    CheckRunner                                           │
│   │     │                                                    │
│   │     ├── BastionClient ──SSH──> Bastion                   │
│   │     │    OCP checks:   oc ...                            │
│   │     │    CVIM checks:  openstack / ciscovim ...          │
│   │     │                                                    │
│   │     └── HostHealthChecker                                │
│   │          NodeClient ──ProxyJump──> each node             │
│   │          (parallel, up to max_parallel_nodes)            │
│   │                                                          │
│   ├── Write run to history DB (SQLite)                       │
│   ├── Generate HTML report + email report                    │
│   └── Rotate old logs and reports                            │
└─────────────────────────────────────────────────────────────┘
```

1. **Pre-flight** — validates SSH + auth + CLI (oc / openstack / ciscovim) on each cluster before running any checks. Clusters that fail are skipped unless `--ignore-preflight` is set.
2. **BastionClient** — `paramiko.SSHClient` from the central server to the bastion. All OCP and CVIM commands (`oc`, `openstack`, `ciscovim`) run on the bastion through this connection.
3. **NodeClient** — `paramiko.SSHClient` that routes through the bastion's existing transport via a `direct-tcpip` channel (ProxyJump equivalent). Used exclusively for host checks. No second SSH binary needed.
4. **History** — every run is written to a single central SQLite DB (`~/cloud_health/db/history.db`). Use `--list-history` to browse past runs.
5. **Reports** — a full interactive HTML report and a simpler email-friendly version are written to `output_dir` at the end of every run.

No code is pushed to any bastion. No Python is required on bastions or nodes.

---

## File structure

```
├── run.py                          # Entry point and CLI
├── version.txt                     # Tool version
├── requirements.txt                # Python dependencies (paramiko, PyYAML)
├── config/
│   ├── config.yaml                 # App configuration
│   └── inventory.yaml              # Cluster + node inventory
└── core/
    ├── inventory.py                # YAML loader — AppSettings, ClusterConfig, NodeConfig
    ├── check_runner.py             # Orchestrates one cluster: bastion SSH + host ProxyJump
    ├── result.py                   # ClusterResult, SectionResult, CheckItem, Status
    ├── ssh_client.py               # BastionClient + NodeClient (paramiko wrappers)
    ├── history_db.py               # Central SQLite run history
    ├── preflight.py                # Pre-flight SSH + CLI probe per cluster
    ├── reporter_html.py            # HTML + email report generation
    └── checks/
        ├── ocp_checks.py           # 27 OpenShift health checks
        ├── cvim_checks.py          # 19 Cisco VIM health checks
        └── host_checks.py          # 19 physical host health checks
```

### Data written at runtime

```
~/cloud_health/
├── config/
│   ├── config.yaml
│   └── inventory.yaml
├── db/
│   └── history.db                  # Central run history (SQLite)
├── logs/
│   ├── system_YYYYMMDD_HHMMSS.log  # Per-run system log
│   ├── commands_YYYYMMDD_HHMMSS.log# Per-cluster SSH command log
│   └── host-YYYYMMDD_HHMMSS.log   # Per-node host check log
└── reports/
    ├── healthcheck_report_YYYYMMDD_HHMMSS.html   # Full interactive report
    └── healthcheck_email_YYYYMMDD_HHMMSS.html    # Email-friendly version
```

---

## Troubleshooting

**Pre-flight fails for a cluster.**
Check the pre-flight output table. Common causes:
- `Not reachable` — wrong IP or network blocked; test with `ssh user@host` manually
- `Auth failed` — wrong credentials in `inventory.yaml`
- `CLI not found` — `oc` or `openstack` is not on the bastion's PATH

Use `--ignore-preflight` to skip failing clusters and proceed with the rest.

**A cluster shows BUSY / locked.**
Another `run.py` process is already running (the PID and start time are printed). Wait for it to finish, or kill the process holding the lock. The lock file is at `<log_dir>/run.lock`.

**A specific check fails but looks like a false positive.**
The HTML report shows the exact command and its full output for every check. If the threshold is the issue, set a per-cluster override in `inventory.yaml` rather than changing the global default in `config.yaml`.

**The run is slow.**
Reduce `parallel_limit` and `max_parallel_nodes` in `config.yaml`. Some environments throttle parallel SSH connections. Alternatively, use `--host-checks` to narrow the host check categories to what you care about.

**Host checks fail with connection errors.**
Host checks route through the bastion via ProxyJump. If the bastion cannot reach a node IP, host checks for that node will fail with a connection error. Verify that the bastion can SSH to each node directly.

**A check shows SKIP.**
The check does not apply to this cluster type (e.g., a CVIM check on an OCP cluster, or SR-IOV on a host without SR-IOV cards). This is expected, not an error.

**Logs fill up the disk.**
Lower `max_log_files` and `max_report_files` in `config.yaml`. Log and report rotation runs automatically at the end of every run, keeping only the newest N files per category.

---

## Getting help

1. Run with `--verbose` to see every SSH command and its raw output.
2. Open the HTML report and look at the **detail** panel on any failing check — the exact command and output are shown verbatim.
3. Use `--list-history` to compare against previous runs.
4. To escalate: copy the cluster name, check name, command, and output. That is everything needed to diagnose the issue remotely.
