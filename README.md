# CloudHealth

**A cluster diagnostic and health-check tool with a clean web interface.**

CloudHealth runs a comprehensive set of health checks across one or many clusters in parallel and shows you the results live as they happen — passes, warnings, failures, and the underlying command output, all in one place. It supports OpenShift (OCP) clusters, Cisco VIM (CVIM) clouds, and the underlying physical hosts.

---

## What you get

- **65+ built-in health checks** spanning OpenShift, CVIM, and host-level diagnostics
- **Live streaming results** — see each check pass, warn, or fail the moment it completes
- **Full HTML report** at the end of every run, with detail panels for every command that ran
- **Run history** — every run is persisted on each cluster's bastion; browse and replay past results without re-running
- **Diff highlighting** — new failures and resolved issues are marked automatically against the previous run
- **Per-cluster selection** — pick exactly which clusters to include; quickly re-run only the ones that failed last time
- **Pre-flight checks** — validate SSH connectivity, auth, and Python availability across all selected clusters before committing to a full run
- **Cancel / Stop** — abort an in-progress run cleanly; partial results are saved to history with status CANCELLED
- **Email-friendly report** for sharing summaries with the team
- **Configurable thresholds** so warnings and failures match your environment's expectations
- **Multi-cluster, parallel** — check 10 clusters at once instead of one at a time

---

## Getting started

### 1. Launch CloudHealth

**Windows:** Double-click `CloudHealth-Bootstrap.exe`.

> **SmartScreen warning:** Because the exe is not code-signed, Windows will show "Windows protected your PC" on first launch. Click **More info → Run anyway** to proceed. This is a one-time prompt.

**Linux / Mac:**
```
										   
./bootstrapper.py
```

The bootstrapper opens a small browser window prompting for your access credentials, then takes you to the main CloudHealth interface in your default browser.

The first time you log in, you'll be asked whether to remember your credentials. Subsequent launches skip the credential prompt entirely. Credentials are stored encrypted at `~/Documents/cloud_health/credentials.cache`.

### 2. Pre-flight (automatic)

Before every run, CloudHealth automatically validates that it can reach each selected cluster and that the bastion has Python available. You'll see a table like this:

| Cluster | Reachable | Auth | Python | Backend Version | Status |
|---|---|---|---|---|---|
| prod-east | ✓ | ✓ | 3.11.2 | 4.1.0 | OK |
| prod-west | ✓ | ✗ | — | — | FAIL |

If any cluster fails pre-flight, the run is blocked by default. You can:
- Fix the issue and click **🛫 Run Pre-flight Only** to re-check without starting a full run
- Tick **Ignore failures and proceed anyway** to run against only the clusters that passed
- Tick **Skip pre-flight** to bypass validation entirely (power users)

Pre-flight results are saved to a local audit database at `~/Documents/cloud_health/db/preflight.db`.

### 3. The main interface

Once CloudHealth opens in your browser, you'll see two areas:

| Area | What it's for |
|---|---|
| **Sidebar (left)** | Pick clusters and checks, tweak settings, browse run history |
| **Main panel (right)** | Live results, filters, and the link to the final report |

The sidebar has three tabs: **Test Cases**, **Configuration**, and **History**.

---

## Selecting clusters

At the top of the **Test Cases** tab you'll see a list of all enabled clusters from your inventory, each with a checkbox and a type badge (OCP / CVIM).

| Button | What it does |
|---|---|
| **All** | Select every cluster |
| **None** | Deselect every cluster |
| **Failed** | Select only clusters that had failures in the last run (appears after a run with failures) |

This lets you quickly re-run just the clusters that need attention without touching the rest.

---

## Choosing checks

Below the cluster list, the checks are grouped into three categories:

- **OCP Checks (27)** — OpenShift cluster health: nodes, operators, etcd, storage, pods, certificates, networking, alerts, etc.
- **CVIM Checks (19)** — Cisco VIM cloud health: hypervisors, networking, volumes, OpenStack services, RabbitMQ, MariaDB, Ceph, and more
- **Host Checks (19)** — Physical host diagnostics: CPU, memory, disk, NTP, kernel messages, firmware, NUMA, hugepages, SELinux, firewall, etc.

Click any group header to expand it. Each row inside has a checkbox — tick the ones you want to include in this run.

> **Tip:** Each group also has quick "Select all" / "Select none" links in the header.

When you're ready, click the big **🚀 Run Health Check** button at the bottom of the sidebar.

---

## Cancelling a run

Click **⏹ Stop** (visible only while a run is active) to abort cleanly. CloudHealth:

1. Sends a cancel signal to every active bastion backend
2. Closes all SSH tunnels
3. Saves whatever partial results were collected on the bastion with status **CANCELLED**
4. Marks the run in the History tab with a distinct CANCELLED badge

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

## Using the History tab

The **History** tab shows the last 30 runs — timestamp, user, cluster count, pass/fail/warn totals, and status (including CANCELLED).

History is stored on each bastion at `/opt/cloud_health/db/history.db` and streamed to the frontend at the end of every run. Click **↻ Refresh** to re-fetch from all bastions over SSH.

Click any run to expand a per-cluster summary panel showing P/F/W counts without re-running.

---

## Diff highlighting in reports

When you open a full report after a run, CloudHealth automatically compares every check result against the previous successful run for that cluster. Changes are highlighted inline:

| Badge | Meaning |
|---|---|
| **NEW** (red) | This failure or warning did not exist in the previous run |
| **RESOLVED** (strikethrough) | This failure existed in the previous run and is now gone |

A **"What's Changed"** banner at the top of the report summarises the diff: _"3 new failures since [timestamp], 1 resolved, 2 new warnings."_

Cancelled runs are skipped as a comparison baseline — only completed runs are used for diff.

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

Once you click Run, the main panel populates in real time. Each cluster gets its own card. The header shows the cluster name and current status (Running, Pass, Warn, Fail, Cancelled), and inside the card you'll see one section per check group with the individual results streaming in.

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

- **Open Full Report** — A self-contained HTML report with every cluster, every section, every check item, diff badges, and a clickable table of contents. You can save it, share it, archive it.
- **Email Version** — A simpler HTML view formatted to paste cleanly into email or messaging tools.

Both reports are also written to your configured **Output Dir** so you have local copies.

---

## How it works (high level)

When you click Run, CloudHealth:

1. Runs **pre-flight** — validates SSH + auth + Python on each selected cluster in parallel
2. **SFTP-pushes** the backend engine to each bastion (only when the version has changed)
3. **Launches** the backend on each bastion and establishes a WebSocket tunnel back to your browser
4. **Streams** results live as each check completes
5. **Saves** run history to the bastion's SQLite DB, generates a diff against the previous run, and writes the HTML report to your machine

Total runtime is roughly the time of your slowest cluster, not the sum of all of them.

### Three-tier SSH architecture

																															  

```
┌─────────────────┐    1× SSH tunnel     ┌─────────────────┐    N× parallel SSH   ┌──────────────┐
│  User's laptop  │ ─── per cluster ───→ │  Bastion (per   │ ──── sessions ─────→ │  Compute /   │
│   (frontend)    │ ←── WS over tunnel ─ │   cluster)      │ ←── (results) ────── │  storage     │
│                 │                      │  + backend.py   │                      │  hosts       │
└─────────────────┘                      └─────────────────┘                      └──────────────┘
```

1. **Tier 1 (Laptop → Bastion):** One SSH tunnel per cluster, open for the entire run. Results stream back via WebSocket.
2. **Tier 2 (Bastion → Compute/Storage):** The bastion's backend engine opens parallel SSH sessions to physical hosts. All host diagnostics run on the bastion side.
3. **Tier 3 (Per-node checks):** Multiple checks run concurrently on a single SSH session to each node.

**Concurrency layers:**
- **Parallel Limit** — clusters checked simultaneously (1× tunnel per cluster)
- **Max Nodes** — hosts checked per cluster in parallel (N× SSH from bastion)
- **Per-node checks** — all checks on a single host run concurrently (unbounded)

No scripts are installed permanently on any node. All diagnostics run as one-off SSH commands.

---

## Where things live

### On your machine (user's laptop / Windows workstation)
																											   
																										 

| What | Path |
|---|---|
| Credentials cache (encrypted) | `~/Documents/cloud_health/credentials.cache` |
| Salt file (credential encryption key) | `~/Documents/cloud_health/.salt` |
| Version tracker | `~/Documents/cloud_health/version.txt` |
| Synced program files | `~/Documents/cloud_health/program/` |
| Pre-flight audit database | `~/Documents/cloud_health/db/preflight.db` |
| HTML reports | Your configured **Output Dir** (default: `./outputs/`) |
| Bootstrapper metadata | `~/Documents/cloud_health/.meta` |

> **Upgrading from an earlier beta?** Beta5 uses `~/Documents/cloud_health/` exclusively. If you were using `~/.cloud_health/` before, you will need to re-enter your credentials once after upgrading.

### On each cluster bastion

| What | Path |
|---|---|
| Run history database | `/opt/cloud_health/db/history.db` |
| Backend engine (temporary, per run) | `/tmp/cloud_health/` |
| Vendor wheels (offline pip) | `/tmp/cloud_health/vendor/` |
| System log | `/tmp/cloud_health/log/system_YYYYMMDD.log` |
| Per-check command log | `/tmp/cloud_health/log/commands_YYYYMMDD.log` |
| Host check log | `/tmp/cloud_health/log/hosts_YYYYMMDD.log` |
| Run lock file | `/tmp/cloud_health/hc.lock` |

### On the source / version server

| What | Path |
|---|---|
| Backend source + vendor wheels | `/opt/cloud_health/` |
| Version file | `/opt/cloud_health/version.txt` |
| Python dependency wheels (offline) | `/opt/cloud_health/vendor/` |

---

## Troubleshooting

**The credential prompt opens but won't accept my password.**
You'll be re-prompted up to three times. After that, the bootstrapper exits. Double-check your credentials with your administrator. The encrypted credentials cache is at `~/Documents/cloud_health/credentials.cache` — delete that file to force a fresh login prompt next launch.

**Pre-flight fails for a cluster.**
Check the Status column in the pre-flight table. Common causes:
- **Not reachable** — bastion IP wrong or network blocked; try `ssh user@host` manually
- **Auth failed** — wrong credentials in the inventory file
- **python3 unavailable** — Python 3 is not installed on that bastion

You can tick **Ignore failures and proceed anyway** to skip the failing clusters and continue with the rest.

**A cluster shows ERROR or BUSY before any checks run.**
BUSY means another CloudHealth run is already active on that bastion (the conflicting PID and start time are shown). Wait for it to finish, or use **Stop** on the active session. ERROR means the connection or backend launch failed — expand the error message for details.

**The run hangs or takes much longer than expected.**
Lower the **Parallel Limit** and **Max Nodes** values in the Configuration tab. Some environments throttle parallel SSH connections, and an aggressive setting can cause queuing or timeouts.

**A specific check fails but I think it's a false positive.**
Click the check to expand its details panel and read the actual command output. If the threshold is the issue, set a per-cluster override in the inventory file rather than changing the global default.

**A check shows SKIP.**
The check doesn't apply to this cluster type (e.g., a CVIM check on an OCP cluster, or SR-IOV on a host without SR-IOV cards). This is expected behaviour, not an error.

   

									

**Windows SmartScreen blocks the .exe on first launch.**
Click **More info**, then **Run anyway**. The exe is not code-signed but is safe to run. This prompt appears only once per machine.
																			
																						
																							

---

## Getting help

If something looks wrong and you're stuck:

1. Open the Full Report and use the **Failures** filter to see only the problems.
2. Expand a failing check — the **command** field shows exactly what was run, and the **detail** field shows exactly what came back.
3. Check the **History** tab to compare against previous runs — the diff badges tell you whether this failure is new or pre-existing.
4. If you need to escalate, copy the cluster name, check name, command, and output. That's everything someone needs to diagnose the issue.
