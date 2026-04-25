# ⚡ ClusterPulse — Universal Cluster Health Check

A Python-based health-check tool for **OCP (OpenShift)** and **CVIM (Cisco VIM / OpenStack)** clusters.  
Runs from any Windows or Linux machine — no agents, no cluster-side installation required.  
Connects via SSH, runs checks in **parallel**, and produces a premium **interactive HTML report** plus an **email-ready HTML** version.

---

## Installation

```bash
# Python 3.9+ required
pip install -r requirements.txt
```

**Windows users:** works identically — use `python` instead of `python3`.

---

## Quick Start

```bash
# 1. Fill in config/inventory.xlsx  (Clusters + Nodes sheets)
# 2. Review / adjust config/config.yaml  (thresholds, parallelism, etc.)
# 3. Run:
python clusterpulse.py
```

Results appear in a timestamped `results/YYYYMMDD_HHMMSS/` directory.

---

## File Layout

```
clusterpulse/
├── clusterpulse.py          ← entry point
├── requirements.txt
├── config/
│   ├── config.yaml          ← all tunable parameters  ← EDIT THIS
│   └── inventory.xlsx       ← cluster & node inventory ← EDIT THIS
├── core/
│   ├── config.py            ← YAML + Excel loader, dataclasses
│   ├── engine.py            ← async parallel orchestrator
│   ├── ssh_client.py        ← paramiko async SSH wrapper
│   ├── result.py            ← ClusterResult / Section / CheckItem
│   ├── reporter_console.py  ← rich live console output
│   └── reporter_html.py     ← HTML report (browser + email)
└── checks/
    ├── ocp_checks.py        ← 27 OCP check categories
    ├── cvim_checks.py       ← 19 CVIM check categories
    └── host_checks.py       ← 19 physical-host check categories
```

---

## Configuration

### `config/config.yaml` — Program Parameters

All tunable settings live here. CLI flags override these values.

```yaml
inventory_path: "config/inventory.xlsx"
output_dir:     "results"

parallelism:
  max_parallel_clusters: 5    # clusters checked simultaneously
  max_parallel_nodes:    10   # nodes SSH-checked in parallel (per cluster)
  ssh_timeout:           30   # seconds
  cmd_timeout:           60   # seconds

thresholds:
  disk_pct:            80    # disk % that triggers FAIL
  restart_warn:        10    # pod restart WARN threshold
  restart_fail:        50    # pod restart FAIL threshold
  pod_age_min_warn_m:   5    # pod age (min) WARN
  pod_age_min_fail_m:   2    # pod age (min) FAIL
  cert_warn_days:       30   # cert expiry WARN
  load_ratio_warn:     1.0   # load/CPU ratio WARN
  load_ratio_fail:     2.0   # load/CPU ratio FAIL
  mem_used_pct_warn:   80
  mem_used_pct_fail:   90
  swap_used_pct_warn:  50

reports:
  html:          true        # interactive browser report
  email_friendly: true       # inline-styled email HTML
```

### `config/inventory.xlsx` — Cluster Inventory

Three sheets:

| Sheet | Purpose |
|---|---|
| **Clusters** | One row per cluster — credentials, type, thresholds |
| **Nodes** | One row per node — optional, nodes auto-discovered if omitted |
| **Instructions** | Built-in quick reference |

**Clusters sheet required columns:**

| Column | Description |
|---|---|
| `cluster_name` | Unique name (used in reports) |
| `type` | `ocp` or `cvim` |
| `enabled` | `TRUE` / `FALSE` — skip without deleting row |
| `installer_host` | Bastion IP for OCP; installer IP for CVIM |
| `ssh_username` | SSH username |
| `ssh_password` | SSH password (or leave blank + use `ssh_private_key`) |
| `ssh_private_key` | Path to private key file |
| `api_url` | OCP API URL e.g. `https://api.cluster.example.com:6443` |
| `api_token` | OCP service account token (`sha256~...`) |

All threshold columns (`disk_threshold`, `restart_warn`, etc.) are **optional** — blank = use `config.yaml` global value.

---

## CLI Reference

```
python clusterpulse.py [OPTIONS]

  -i, --inventory FILE    Path to inventory.xlsx
                          Default search: config/inventory.xlsx, inventory.xlsx
  -c, --config FILE       Path to config.yaml
                          Default search: config/config.yaml, clusterpulse.yaml,
                                         ~/.clusterpulse/config.yaml
  -o, --output DIR        Output directory (timestamped subfolder created inside)
      --type {ocp,cvim}   Check only this cluster type
      --checks CATS       Comma-separated categories (default: all)
      --parallel N        Max parallel cluster connections (overrides config)
      --ssh-timeout N     SSH timeout seconds (overrides config)
      --no-html           Skip HTML browser report
      --no-email          Skip email HTML report
  -v, --verbose           Show all command output in console
```

**Examples:**

```bash
# Default (uses config/config.yaml + config/inventory.xlsx)
python clusterpulse.py

# Custom paths
python clusterpulse.py -i /data/clusters.xlsx -c /etc/clusterpulse.yaml

# OCP only, 10 parallel
python clusterpulse.py --type ocp --parallel 10

# Only specific check categories
python clusterpulse.py --checks nodes,pods,ceph,host

# Custom output, verbose
python clusterpulse.py -o /reports/daily --verbose

# CVIM only, skip host checks, custom timeout
python clusterpulse.py --type cvim --checks hypervisors,network,volumes,ceph --ssh-timeout 60
```

---

## Output Files

Each run creates `results/YYYYMMDD_HHMMSS/`:

```
results/20240115_143022/
├── healthcheck_report.html      ← Interactive browser report  ← OPEN THIS
├── healthcheck_email.html       ← Email-ready HTML (paste into mail body)
├── clusterpulse.log             ← Full execution log (all SSH commands + output)
├── prod-ocp-dc1_report.txt      ← Per-cluster plain-text report
├── staging-ocp-dc2_report.txt
└── prod-cvim-siteA_report.txt
```

### HTML Report Features

- **Dark precision theme** — IBM Plex Mono/Sans, amber accents, charcoal background
- **Summary scoreboard** — cluster counts, total PASS/FAIL/WARN
- **Cluster blocks** — collapsible, auto-expanded when failures exist
- **Section blocks** — left accent bar by severity, per-section counts
- **Check items** — PASS/FAIL/WARN/INFO badges with optional command + detail
- **Raw log** — expandable per section
- **Filter bar** — Failures / Warnings / Passed / Info (hides non-matching items entirely)
- **Live search** — filters clusters, sections, and individual check items
- **Expand / Collapse All** buttons
- **Email version** — inline-styled, works in Outlook, Gmail, Apple Mail

---

## Check Categories

### OCP (27 categories)

| Flag | What is checked |
|---|---|
| `version` | ClusterVersion API reachability, Available / Progressing / Degraded conditions, upgrade channel |
| `operators` | All ClusterOperators Available=True, Degraded=False |
| `nodes` | Node Ready state, role counts (master/worker/infra) |
| `pressure` | MemoryPressure / DiskPressure / PIDPressure per node |
| `node_disk` | Disk utilization via `oc debug` (first 3 nodes) |
| `etcd` | etcd pods running, etcdctl endpoint health, leader status, backup CRs |
| `controlplane` | apiserver, controller-manager, kube-apiserver, kube-scheduler, authentication pods |
| `ceph` | ODF pods, Ceph HEALTH_OK/WARN/ERR, OSD up/in, PG state, StorageCluster CR |
| `pvcs` | PVCs Bound (not Lost/Pending), PV states |
| `storageclasses` | Default StorageClass defined, no duplicates |
| `pods` | All-namespace audit: status, restart count, pod age |
| `deployments` | Deployments + StatefulSets fully available |
| `daemonsets` | DaemonSets with full desired/ready coverage |
| `jobs` | Failed/incomplete Jobs, suspended CronJobs |
| `hpa` | HPAs at maximum replicas (capacity pressure indicator) |
| `network` | CNI operator, DNS operator + pods, OVN/SDN namespace, EgressIPs |
| `ingress` | IngressController Available condition, router pods healthy |
| `events` | Warning events cluster-wide (last hour) |
| `certs` | TLS secret expiry scan (warn <30d, fail if expired) |
| `mcp` | MachineConfigPools not degraded / not updating |
| `nodes_upgrade` | Node OS version consistency, cordoned/unschedulable nodes |
| `quota` | ResourceQuotas and LimitRanges inventory |
| `rbac` | Privileged SCC usage, cluster-admin ClusterRoleBinding count |
| `alerts` | Prometheus critical + warning firing alerts via Thanos, Alertmanager silences |
| `logging` | openshift-logging / Loki stack pod health |
| `imageregistry` | Image registry operator Available condition, management state |
| `backup` | EtcdBackup CR freshness |

### CVIM (19 categories)

| Flag | What is checked |
|---|---|
| `hypervisors` | Hypervisor count UP vs configured, per-HV down/disabled, cluster vCPU/RAM stats |
| `network` | Neutron agent alive count vs required (hvs×2+12), dead agents listed |
| `volumes` | Cinder services UP+enabled count, volume status summary |
| `compute_svc` | Nova compute service state per host |
| `identity` | Keystone token issue, disabled endpoints, region count |
| `image_svc` | Glance image count and activation state |
| `cloudpulse` | Cloudpulse test result pass/fail |
| `vms` | VM count by status (ACTIVE / SHUTOFF / ERROR) |
| `vm_errors` | VMs in ERROR state detail, active task states |
| `rabbitmq` | `rabbit_api.py` functional tests or `rabbitmqctl cluster_status` partition check |
| `mariadb` | Galera wsrep cluster size, wsrep_ready, wsrep_local_state_comment |
| `memcached` | Memcached uptime via stats protocol |
| `containers` | Podman/Docker container running vs systemd desired per node type |
| `ceph` | `ceph -s` HEALTH_OK/WARN/ERR, OSD line, client I/O, usage |
| `ceph_pools` | Pool list, PG state (degraded/incomplete/stale), OSD tree down count |
| `ovs` | OVS bridge inventory via `ovs-vsctl show`, OVS version |
| `haproxy` | HAProxy config check, VIP detection, stats socket |
| `nfs` | NFS exports + active mounts |
| `installer` | CVIM version, `ciscovim mgmt-node-health`, recent error logs, openrc presence |

### Host (19 sub-checks, category flag: `host`)

| Sub-check | What is checked |
|---|---|
| Uptime / Load | Load average vs CPU count ratio — WARN/FAIL thresholds |
| OS info | RHEL/RHCOS release + kernel version |
| CPU | Model, socket/core/thread topology, thermal throttling events, scaling governor |
| Memory | RAM used %, available, OOM kill events last 24h, NUMA hardware |
| Disk | All mounts vs threshold, inode usage, iostat await, SMART health |
| ECC errors | Uncorrectable (FAIL) + correctable (WARN) via EDAC sysfs, edac-util, dmesg MCE |
| Network interfaces | UP/DOWN state, interface RX/TX errors |
| Bond status | Slave MII state, historical link failure count per bond |
| SR-IOV | VF count per PF, VF link-state disabled check |
| Kernel messages | journalctl -k critical/error last 24h, filtered for benign patterns |
| Systemd services | `systemctl --state=failed`, critical services (sshd/chrony/NM) active |
| NTP | chronyc tracking offset, timedatectl sync state |
| PCIe / AER | Uncorrected PCIe/AER errors in dmesg |
| Firmware | BIOS vendor/version/date, NIC driver + firmware-version |
| NUMA topology | NUMA node count and CPU/memory layout |
| Hugepages | Total/free/used hugepages + size, WARN if >95% used |
| SELinux | Enforcing / Permissive / Disabled |
| Firewall | firewalld / iptables state |
| Open ports | Listening TCP ports (non-localhost) |

---

## Authentication Notes

- Uses **paramiko** for SSH — no OpenSSH binary required, works on Windows
- SSH host key verification is **disabled by default** (AutoAddPolicy) for lab environments  
  → For production: replace `AutoAddPolicy` in `core/ssh_client.py` with known_hosts verification
- Support both **password** and **private key** auth per cluster
- OCP API token stored in inventory — use a read-only service account in production:

```bash
oc create serviceaccount clusterpulse -n default
oc adm policy add-cluster-role-to-user view -z clusterpulse -n default
oc serviceaccounts get-token clusterpulse -n default
```

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | All checks passed (no FAIL results) |
| `1` | One or more FAIL results found |

Suitable for use in CI/CD pipelines, cron jobs, and monitoring scripts.

---

## Security

- Store `inventory.xlsx` with `chmod 600` or equivalent Windows ACL
- Prefer SSH private keys over passwords
- Consider HashiCorp Vault or Ansible Vault for secrets management at scale
- The tool does **not** modify any cluster state — all operations are read-only
