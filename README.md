# CloudHealth

**A cluster diagnostic and health-check tool with a clean web interface.**

CloudHealth runs a comprehensive set of health checks across one or many clusters in parallel and shows you the results live as they happen — passes, warnings, failures, and the underlying command output, all in one place. It supports OpenShift (OCP) clusters, Cisco VIM (CVIM) clouds, and the underlying physical hosts.

---

## What you get

- **65+ built-in health checks** spanning OpenShift, CVIM, and host-level diagnostics
- **Live streaming results** — see each check pass, warn, or fail the moment it completes
- **Full HTML report** at the end of every run, with detail panels for every command that ran
- **Email-friendly report** for sharing summaries with the team
- **Configurable thresholds** so warnings and failures match your environment's expectations
- **Multi-cluster, parallel** — check 10 clusters at once instead of one at a time

---

## Getting started

### 1. Launch CloudHealth

Run the bootstrapper. It opens a small browser window prompting for your access credentials, then takes you to the main CloudHealth interface in your default browser.

```
CloudHealth-Bootstrap.exe         (Windows)
./bootstrapper.py                 (Linux / Mac)
```

The first time you log in, you'll be asked whether to remember your credentials. Subsequent launches skip the credential prompt entirely.

### 2. The main interface

Once CloudHealth opens in your browser, you'll see two areas:

| Area | What it's for |
|---|---|
| **Sidebar (left)** | Pick which checks to run; tweak thresholds and parallelism |
| **Main panel (right)** | Live results, filters, and the link to the final report |

The sidebar has two tabs: **Test Cases** and **Configuration**.

---

## Using the Test Cases tab

This is where you choose what gets checked.

The checks are grouped into three categories:

- **OCP Checks (27)** — OpenShift cluster health: nodes, operators, etcd, storage, pods, certificates, networking, alerts, etc.
- **CVIM Checks (19)** — Cisco VIM cloud health: hypervisors, networking, volumes, OpenStack services, RabbitMQ, MariaDB, Ceph, and more
- **Host Checks (19)** — Physical host diagnostics: CPU, memory, disk, NTP, kernel messages, firmware, NUMA, hugepages, SELinux, firewall, etc.

Click any group header to expand it. Each row inside has a checkbox — tick the ones you want to include in this run.

> **Tip:** Each group also has quick "Select all" / "Select none" links in the header. Use them to flip an entire category on or off in one click.

When you're ready, click the big **🚀 Run Health Check** button at the bottom of the sidebar.

---

## Using the Configuration tab

This tab lets you adjust how the checks behave. Changes are saved when you click **Save Config** and persist across runs.

### Parallelism

| Setting | What it controls |
|---|---|
| **Parallel Limit** | Maximum number of clusters checked simultaneously (default: 5) |
| **Max Nodes** | Maximum number of physical nodes checked simultaneously per cluster (default: 10) |
| **SSH Timeout** | Seconds before an unresponsive SSH connection gives up |
| **CMD Timeout** | Seconds before a slow command gives up |

### Thresholds

These determine when a check is reported as a warning vs. a failure.

| Setting | Description |
|---|---|
| **Disk %** | Disk usage above this percentage is flagged |
| **Mem Warn %** / **Mem Fail %** | Memory usage thresholds for warning and failure |
| **Swap Warn %** | Swap usage above this is flagged |
| **Load Warn ×** / **Load Fail ×** | Load average ratio (load ÷ CPU count) thresholds |

### Paths

| Setting | Description |
|---|---|
| **Output Dir** | Where final HTML reports are saved on your machine |
| **Inventory File** | Path to the Excel file that lists your clusters |

> **Per-cluster overrides:** If a specific cluster needs different thresholds, you can set them directly in the inventory file (per-cluster columns). Any value set there overrides the global default.

---

## The inventory file

CloudHealth reads the list of clusters to check from an Excel file (`inventory.xlsx` by default). The file has two sheets:

### `Clusters` sheet

One row per cluster. Required columns:

| Column | Description |
|---|---|
| `cluster_name` | Friendly name (shown in reports and the UI) |
| `type` | `ocp` or `cvim` |
| `installer_ip` | IP or hostname of the cluster's bastion / installer node |
| `ssh_user` | SSH username for the bastion |
| `ssh_pass` *or* `ssh_key` | Either a password or a path to a private key |
| `enabled` | `yes` or `no` — disable a cluster without deleting the row |

Optional per-cluster threshold columns: `disk_threshold`, `mem_used_pct_warn`, `mem_used_pct_fail`, `load_ratio_warn`, `load_ratio_fail`, `swap_used_pct_warn`.

### `Nodes` sheet (optional, only for host checks)

One row per physical host you want host-level checks against. Columns:

| Column | Description |
|---|---|
| `cluster_name` | Must match a cluster_name from the Clusters sheet |
| `node_ip` | IP or hostname |
| `ssh_user` | SSH username |
| `ssh_pass` *or* `ssh_key` | Authentication |

---

## Reading the live results

Once you click Run, the main panel populates in real time. Each cluster gets its own card. The header shows the cluster name and current status (Running, Pass, Warn, Fail), and inside the card you'll see one section per check group with the individual results streaming in.

### Status icons

| Icon | Meaning |
|---|---|
| ✓ **PASS** | Check completed and the value is healthy |
| ⚠ **WARN** | Check completed but the value is concerning (above warn threshold, but not critical) |
| ✕ **FAIL** | Check completed and the value is critical |
| ✕ **ERROR** | Check could not complete (SSH problem, command failure, missing dependency) |
| ○ **INFO** | Informational — useful to know but not a problem |
| – **SKIP** | Check was skipped (not applicable to this cluster) |

### Filtering

Above the cluster cards there's a filter bar: **All / Failures / Warnings / Passed / Info**. Use it to focus on what matters. "Failures" is the most useful — it hides everything that passed and shows you only what needs attention.

### Live scoreboard

At the very top you'll see a running tally: total clusters, sections completed, passed, warnings, and failures. This updates every time a new check result streams in.

### Expanding details

Click any check item to expand the **details panel**, which shows:

- The exact command that was run
- The full stdout and stderr output
- A timestamp

This is what you'll want when debugging a failure — every check is reproducible because every command is shown verbatim.

---

## When the run finishes

A green banner appears at the top of the main panel with two links:

- **Open Full Report** — A self-contained HTML report with every cluster, every section, every check item, and a clickable table of contents. You can save it, share it, archive it.
- **Email Version** — A simpler HTML view formatted to paste cleanly into email or messaging tools.

Both reports are also written to your configured **Output Dir** so you have local copies.

---

## How it works (high level)

When you click Run, CloudHealth connects securely to each cluster's bastion node, runs the checks you selected, and streams the results back to your browser as they complete. The same logic runs against every cluster in parallel, so total runtime is roughly the time of your slowest cluster, not the sum of all of them.

For host checks, CloudHealth additionally connects to each physical host listed in the inventory and runs the host-level diagnostics there. Host checks within a cluster also run in parallel up to the configured **Max Nodes** limit.

Nothing is installed permanently on your clusters. Diagnostics are scoped to the run that triggered them, and credentials never leave the machines that need them — your laptop only ever holds the credentials for the cluster bastions you've configured.

### Three-tier SSH architecture

CloudHealth uses a three-tier architecture to reach compute and storage nodes without exposing direct access from your laptop:

```
┌─────────────────┐    1× SSH tunnel     ┌─────────────────┐    N× parallel SSH   ┌──────────────┐
│  User's laptop  │ ─── per cluster ───→ │  Bastion (per   │ ──── sessions ─────→ │  Compute /   │
│   (frontend)    │ ←── WS over tunnel ─ │   cluster)      │ ←── (results) ─────── │  storage     │
│                 │                      │  + backend.py   │                      │  hosts       │
└─────────────────┘                      └─────────────────┘                      └──────────────┘
```

**How it works:**

1. **Tier 1 (Laptop → Bastion):** You connect once per cluster to its bastion / installer node. This connection stays open for the entire run and uses the credentials from your inventory file. Results stream back via WebSocket.

2. **Tier 2 (Bastion → Compute/Storage):** The bastion runs the backend Python engine, which initiates parallel SSH connections *from the bastion* to each physical host. These connections use host-specific credentials (if provided in the inventory) or fall back to the bastion credentials. All host diagnostics run on the bastion side.

3. **Tier 3 (Per-node checks):** Once connected to a host, multiple checks run concurrently on a single SSH session. For example, CPU, memory, disk, and NTP checks all run in parallel without needing separate connections.

**Concurrency control has three layers:**

- **Parallel Limit** — Controls how many clusters are checked simultaneously from your laptop (1× SSH tunnel per cluster)
- **Max Nodes** — Controls how many physical hosts are checked in parallel per cluster (N× SSH from bastion)
- **Per-node checks** — All checks for a single host run concurrently on its SSH connection (unbounded)

No scripts are installed on compute nodes. All diagnostics use one-off commands executed via SSH, with output parsed and returned to the bastion for formatting and streaming back to your browser.

---

## Troubleshooting

**The credential prompt opens but won't accept my password.**
You'll be re-prompted up to three times. After that, double-check the credentials with your administrator. Cached credentials are stored encrypted; if you ever need to clear them, delete the credentials cache file (your administrator can tell you the exact path).

**A cluster shows ERROR before any checks run.**
This usually means CloudHealth couldn't reach the cluster bastion or the SSH credentials in the inventory are wrong. Check that:
1. The bastion IP / hostname in the inventory is correct
2. Your SSH user has permission on that bastion
3. The bastion is reachable from your machine (try `ssh user@host` manually)

**The run hangs or takes much longer than expected.**
Lower the **Parallel Limit** and **Max Nodes** values in the Configuration tab. Some environments throttle parallel SSH connections, and a too-aggressive setting can cause queuing.

**A specific check fails but I think it's a false positive.**
Click the check to expand its details panel and read the actual command output. If the threshold is the issue (e.g., disk warning at 85% but you consider 90% normal for that cluster), set a per-cluster override in the inventory file instead of changing the global threshold.

**A check shows SKIP.**
The check determined it doesn't apply to this cluster (e.g., a CVIM check on an OCP cluster, or a SR-IOV check on a host without SR-IOV cards). This is expected behavior, not an error.

---

## Where things live on your machine

| What | Where |
|---|---|
| HTML reports | Your configured **Output Dir** (set in Configuration tab) |
| Cached credentials (encrypted) | Managed automatically; you don't need to touch this |
| Frontend logs | Saved automatically; ask your administrator if you need them for support |

---

## Getting help

If something looks wrong and you're stuck:

1. Open the Full Report and use the **Failures** filter to see only the problems.
2. Expand a failing check — the **command** field shows exactly what was run, and the **detail** field shows exactly what came back.
3. If you need to escalate, copy the cluster name, check name, command, and output. That's everything someone needs to diagnose the issue.
