# CloudHealth

**A cluster diagnostic and health-check tool with a clean web interface.**

CloudHealth runs a comprehensive set of health checks across one or many clusters in parallel and shows you the results live as they happen — passes, warnings, failures, and the underlying command output, all in one place. It supports OpenShift (OCP) clusters, Cisco VIM (CVIM) clouds, and the underlying physical hosts.

---

## Table of Contents

1. [What you get](#what-you-get)
2. [Architecture overview](#architecture-overview)
3. [Installation — first-time setup](#installation--first-time-setup)
   - [Step 1 — Prerequisites](#step-1--prerequisites)
   - [Step 2 — Set up the source server](#step-2--set-up-the-source-server)
   - [Step 3 — Bundle dependencies (vendor wheels)](#step-3--bundle-dependencies-vendor-wheels)
   - [Step 4 — Fill in the inventory file](#step-4--fill-in-the-inventory-file)
   - [Step 5 — Distribute the bootstrapper to users](#step-5--distribute-the-bootstrapper-to-users)
   - [Step 6 — First-time user launch](#step-6--first-time-user-launch)
4. [Updating the code](#updating-the-code)
5. [What each machine needs](#what-each-machine-needs)
6. [Getting started (end users)](#getting-started-end-users)
7. [Selecting clusters](#selecting-clusters)
8. [Choosing checks](#choosing-checks)
9. [Using the Configuration tab](#using-the-configuration-tab)
10. [Using the History tab](#using-the-history-tab)
11. [Diff highlighting in reports](#diff-highlighting-in-reports)
12. [The inventory file](#the-inventory-file)
13. [Reading the live results](#reading-the-live-results)
14. [When the run finishes](#when-the-run-finishes)
15. [How it works (high level)](#how-it-works-high-level)
16. [Where things live](#where-things-live)
17. [Troubleshooting](#troubleshooting)

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

## Architecture overview

CloudHealth has three tiers:

```
┌─────────────────┐    SSH (source server)    ┌──────────────────────┐
│  User's laptop  │ ─── bootstrapper pulls ──→│  Source server       │
│                 │     program on startup     │  /opt/cloud_health/  │
│  [frontend]     │                            └──────────────────────┘
│  browser UI     │
│                 │    SSH tunnel (per cluster) ┌──────────────────┐   SSH   ┌──────────┐
│                 │ ──────────────────────────→ │  Cluster bastion │ ──────→ │  Nodes   │
│                 │ ←── WebSocket (results) ─── │  [backend]       │         │          │
└─────────────────┘                             └──────────────────┘         └──────────┘
```

| Component | Runs on | Role |
|---|---|---|
| **Bootstrapper** | User's laptop | One-time exe the user launches. Connects to the source server, pulls the program if a newer version is available, installs deps, then starts the frontend. |
| **Source server** | A central bastion you control | Holds the authoritative copy of the program at `/opt/cloud_health/`. All users pull from here. This is how updates are pushed to everyone at once. |
| **Frontend** | User's laptop (localhost) | FastAPI + WebSocket server opened in the user's browser. Reads the inventory, handles the UI, and orchestrates all cluster connections. |
| **Backend** | Each cluster's bastion | Pushed automatically by the frontend when a run starts (only if the version changed). Runs the actual health checks and streams results back. |

**Key point about dependencies:** The source server must contain a pre-built `vendor/` directory of Python wheel files. The bootstrapper installs deps from those wheels using `pip install --no-index` — no internet access is required on the user's machine or on any bastion.

---

## Installation — first-time setup

### Step 1 — Prerequisites

| Machine | What it needs |
|---|---|
| **Source server** | Python 3.6+, SSH server (sshd), write access to `/opt/cloud_health/` |
| **Cluster bastions** | Python 3.6+, SSH server, access to the cluster's nodes and API (no other software needed — the backend is pushed automatically) |
| **User's laptop** | Python 3.6+ (if running `bootstrapper.py` directly), or nothing at all (if using the compiled `.exe`). Internet access is not required after first setup. |

### Step 2 — Set up the source server

The source server is the single machine that holds the authoritative copy of the program. Every user's bootstrapper connects here on launch to check whether their local copy is up to date.

**On the source server,** create the directory and copy the program files:

```bash
sudo mkdir -p /opt/cloud_health
sudo chown $USER /opt/cloud_health

# Copy the entire beta5 directory contents into /opt/cloud_health/
cp -r beta5/* /opt/cloud_health/
```

The result should look like this:

```
/opt/cloud_health/
├── main.py
├── version.txt          ← version string, e.g. "5.0.0"
├── requirements.txt
├── vendor/              ← pre-downloaded wheels (see Step 3)
├── config/
│   ├── config.yaml
│   └── inventory.xlsx   ← you'll fill this in (see Step 4)
├── frontend/
├── backend/
└── bootstrapper/
```

Set the version string:

```bash
echo "5.0.0" > /opt/cloud_health/version.txt
```

### Step 3 — Bundle dependencies (vendor wheels)

The `vendor/` directory contains all Python packages needed to run the frontend (on users' laptops) and the backend (on bastions). It must exist on the source server before any user can launch the tool.

**On the source server** (must have internet access for this step only):

```bash
cd /opt/cloud_health

# Download all required wheels into vendor/
pip download \
  fastapi \
  "uvicorn[standard]" \
  websockets \
  paramiko \
  PyYAML \
  pandas \
  openpyxl \
  cryptography \
  rich \
  -d vendor/
```

This downloads `.whl` files for all dependencies and their transitive deps into `vendor/`. After this step the source server no longer needs internet access, and neither does anything that installs from it.

> **Why wheels instead of pip install?**
> Cluster bastions and users' laptops are often on isolated networks with no internet access. By pre-downloading wheels and installing with `pip install --no-index --find-links vendor/`, the tool works entirely offline. The bootstrapper does this automatically; you just need to make sure `vendor/` exists on the source server.

Verify the directory is populated:

```bash
ls /opt/cloud_health/vendor/*.whl | head
# Should list many .whl files
```

### Step 4 — Fill in the inventory file

The inventory file tells CloudHealth which clusters to check. It is an Excel file at `/opt/cloud_health/config/inventory.xlsx`. You can edit it with LibreOffice or Excel.

The file has two sheets. See [The inventory file](#the-inventory-file) section below for full column reference.

**Minimal example for `Clusters` sheet:**

| cluster_name | type | installer_ip | ssh_user | ssh_pass | enabled |
|---|---|---|---|---|---|
| prod-east | ocp | 10.0.1.5 | cloud-user | s3cret | yes |
| staging | cvim | 10.0.2.10 | root | p@ssword | yes |

> The inventory file lives on the source server and is pulled to users' laptops along with the rest of the program. Every time you update the inventory, bump `version.txt` (see [Updating the code](#updating-the-code)) so users get the new file automatically on next launch.

### Step 5 — Distribute the bootstrapper to users

Users only ever need the bootstrapper — it handles everything else automatically.

**Option A — Compiled exe (Windows, recommended)**

Build the exe once on a Windows machine that has PyInstaller and the same Python version:

```bash
pip install pyinstaller cryptography paramiko
pyinstaller --onefile bootstrapper/bootstrapper.py --name CloudHealth-Bootstrap
```

The resulting `dist/CloudHealth-Bootstrap.exe` is the only file users need. Send it to them however you share files (SharePoint, email, USB). They double-click it and it handles the rest.

> **SmartScreen warning:** Because the exe is not code-signed, Windows shows "Windows protected your PC" on first launch. Users click **More info → Run anyway**. This is a one-time prompt per machine.

**Option B — Python script (Linux / Mac)**

Copy `bootstrapper/bootstrapper.py` to the user's machine and ensure `cryptography` and `paramiko` are installed:

```bash
pip install cryptography paramiko
python3 bootstrapper.py
```

### Step 6 — First-time user launch

When a user runs the bootstrapper for the first time:

1. A browser window opens prompting for the **source server IP, username, and password** (the SSH credentials for `/opt/cloud_health/` on the source server — not cluster credentials).
2. The bootstrapper SSHes to the source server and compares `version.txt`.
3. Since no local copy exists, it SFTPs the entire `/opt/cloud_health/` to `~/Documents/cloud_health/program/` on the user's machine.
4. It installs Python packages from `vendor/` using `pip install --no-index`.
5. It launches `main.py`, which opens the CloudHealth UI in the browser.

From the second launch onwards, the credential prompt is skipped (credentials are cached, encrypted, at `~/Documents/cloud_health/credentials.cache`). The version check still runs — if the version matches, launch takes a few seconds; if the source server has a newer version, it syncs first.

---

## Updating the code

To push a new version to all users:

1. **Edit the program files** on the source server (or copy a new build):
   ```bash
   cp -r beta5/* /opt/cloud_health/
   ```

2. **Re-bundle dependencies** if `requirements.txt` changed:
   ```bash
   rm -rf /opt/cloud_health/vendor/
   pip download -r /opt/cloud_health/requirements.txt -d /opt/cloud_health/vendor/
   ```

3. **Bump the version number** — this is the trigger that tells every user's bootstrapper to sync:
   ```bash
   echo "5.1.0" > /opt/cloud_health/version.txt
   ```

That's it. The next time any user launches CloudHealth, the bootstrapper sees the version mismatch, pulls the new files, re-installs deps, and launches the updated program — automatically, with no action required from the user.

> **Bastion backend updates happen automatically too.** The backend that runs on cluster bastions is pushed directly from the user's laptop during each run (if the version on the bastion doesn't match). You do not need to manually update anything on bastions.

---

## What each machine needs

### Source server

- Python 3.6 or newer
- `sshd` running and reachable from users' laptops
- `/opt/cloud_health/` populated as described above (program files + `vendor/` + `version.txt`)
- No internet access required after the initial `pip download`

### Cluster bastions

- Python 3.6 or newer (check: `python3 --version`)
- `sshd` running and reachable from users' laptops
- SSH access from the bastion to the cluster's nodes (for host checks)
- `oc` CLI available (for OCP clusters)
- `openstack` CLI available (for CVIM clusters)
- Write access to `/tmp/cloud_health/` (the backend is pushed here)
- **No pre-installed Python packages required** — the backend vendor wheels are pushed alongside the code

### User's laptop

- Python 3.6 or newer (if running `bootstrapper.py` directly)
- No special setup needed beyond the bootstrapper file
- SSH access to the source server (port 22 by default, configurable in the login prompt)

---

## Getting started (end users)

### 1. Launch CloudHealth

**Windows:** Double-click `CloudHealth-Bootstrap.exe`.

**Linux / Mac:**
```bash
python3 bootstrapper.py
```

The bootstrapper opens a browser window prompting for your source server credentials (the IP address, username, and password your administrator gave you). After a successful login, the CloudHealth UI opens automatically.

The first time you log in, you can tick **Remember credentials** to skip the prompt on future launches. Credentials are stored encrypted at `~/Documents/cloud_health/credentials.cache`.

### 2. Pre-flight (automatic)

Before every run, CloudHealth automatically validates that it can reach each selected cluster and that the bastion has Python available. You'll see a table like this:

| Cluster | Reachable | Auth | Python | Backend Version | Status |
|---|---|---|---|---|---|
| prod-east | ✓ | ✓ | 3.11.2 | 5.0.0 | OK |
| prod-west | ✓ | ✗ | — | — | FAIL |

If any cluster fails pre-flight, the run is blocked by default. You can:
- Fix the issue and click **🛫 Run Pre-flight Only** to re-check without starting a full run
- Tick **Ignore failures and proceed anyway** to run against only the clusters that passed
- Tick **Skip pre-flight** to bypass validation entirely (power users)

Pre-flight results are saved to a local audit database at `~/Documents/cloud_health/db/preflight.db`.

### 3. The main interface

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

---

## Choosing checks

Below the cluster list, the checks are grouped into three categories:

- **OCP Checks (27)** — OpenShift cluster health: nodes, operators, etcd, storage, pods, certificates, networking, alerts, etc.
- **CVIM Checks (19)** — Cisco VIM cloud health: hypervisors, networking, volumes, OpenStack services, RabbitMQ, MariaDB, Ceph, and more
- **Host Checks (19)** — Physical host diagnostics: CPU, memory, disk, NTP, kernel messages, firmware, NUMA, hugepages, SELinux, firewall, etc.

Click any group header to expand it. Each row inside has a checkbox — tick the ones you want to include in this run.

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

> **Per-cluster overrides:** If a specific cluster needs different thresholds, set them in the `Clusters` sheet of the inventory file. Any value set there overrides the global default.

---

## Using the History tab

The **History** tab shows the last 30 runs — timestamp, user, cluster count, pass/fail/warn totals, and status (including CANCELLED).

History is stored on each bastion at `/opt/cloud_health/db/history.db` and streamed to the frontend at the end of every run. Click **↻ Refresh** to re-fetch from all bastions over SSH.

Click any run to expand a per-cluster summary panel showing P/F/W counts without re-running.

---

## Diff highlighting in reports

When you open a full report, CloudHealth compares every check result against the previous successful run for that cluster:

| Badge | Meaning |
|---|---|
| **NEW** (red) | This failure or warning did not exist in the previous run |
| **RESOLVED** (strikethrough) | This failure existed in the previous run and is now gone |

A **"What's Changed"** banner at the top of the report summarises the diff. Cancelled runs are skipped — only completed runs are used as a comparison baseline.

---

## The inventory file

CloudHealth reads the list of clusters from an Excel file (`config/inventory.xlsx` by default). The file has two sheets.

### `Clusters` sheet

One row per cluster. Required columns:

| Column | Description |
|---|---|
| `cluster_name` | Friendly name (shown in reports and the UI) |
| `type` | `ocp` or `cvim` |
| `installer_ip` | IP or hostname of the cluster's bastion / installer node |
| `ssh_user` | SSH username for the bastion |
| `ssh_pass` *or* `ssh_key` | Either a plaintext password or a path to a private key file |
| `enabled` | `yes` or `no` — disable a cluster without deleting the row |

Optional per-cluster threshold overrides (any omitted column falls back to the global value in `config.yaml`):

| Column | Description |
|---|---|
| `disk_threshold` | Override global `disk_percent` threshold |
| `mem_used_pct_warn` | Override global `mem_warn` threshold |
| `mem_used_pct_fail` | Override global `mem_fail` threshold |
| `load_ratio_warn` | Override global `load_warn` threshold |
| `load_ratio_fail` | Override global `load_fail` threshold |
| `swap_used_pct_warn` | Override global `swap_warn` threshold |

### `Nodes` sheet (optional, only needed for host checks)

One row per physical host you want host-level checks against:

| Column | Description |
|---|---|
| `cluster_name` | Must exactly match a `cluster_name` from the Clusters sheet |
| `node_ip` | IP or hostname of the physical host |
| `ssh_user` | SSH username for the host |
| `ssh_pass` *or* `ssh_key` | Authentication for the host |

---

## Reading the live results

Once you click Run, the main panel populates in real time. Each cluster gets its own card.

### Status icons

| Icon | Meaning |
|---|---|
| ✓ **PASS** | Check completed and the value is healthy |
| ⚠ **WARN** | Check completed but the value is concerning |
| ✕ **FAIL** | Check completed and the value is critical |
| ✕ **ERROR** | Check could not complete (SSH problem, command failure, missing dependency) |
| ○ **INFO** | Informational — useful but not a problem |
| – **SKIP** | Check was skipped (not applicable to this cluster type) |

### Filtering

Above the cluster cards: **All / Failures / Warnings / Passed / Info**. "Failures" is the most useful during triage.

### Expanding details

Click any check item to see the exact command that ran, the full stdout/stderr output, and a timestamp. Every check is fully reproducible because every command is shown verbatim.

---

## When the run finishes

A green banner appears with two links:

- **Open Full Report** — Self-contained HTML with every cluster, section, check item, diff badges, and a clickable table of contents.
- **Email Version** — Simpler HTML formatted to paste cleanly into email or messaging tools.

Both are also written to your configured **Output Dir**.

---

## How it works (high level)

When you click Run, CloudHealth:

1. Runs **pre-flight** — validates SSH + auth + Python on each selected cluster in parallel
2. **SSHes into each bastion** and SFTP-pushes the backend (only if the bastion's version doesn't match)
3. **Installs dependencies** on the bastion from the pushed `vendor/` wheels (offline, no internet)
4. **Launches the backend** on each bastion and establishes a WebSocket tunnel back to your browser
5. **Streams results** live as each check completes
6. **Saves** run history to the bastion's SQLite DB, generates a diff against the previous run, and writes the HTML report to your machine

Total runtime is roughly the time of your slowest cluster, not the sum of all of them.

### Concurrency layers

- **Parallel Limit** — clusters checked simultaneously
- **Max Nodes** — hosts checked per cluster in parallel
- **Per-node checks** — all checks on a single host run concurrently

No scripts are installed permanently on any node. All diagnostics run as one-off SSH commands.

---

## Where things live

### On the source server

| What | Path |
|---|---|
| Program files | `/opt/cloud_health/` |
| Version file | `/opt/cloud_health/version.txt` |
| Vendor wheels | `/opt/cloud_health/vendor/` |
| Inventory file | `/opt/cloud_health/config/inventory.xlsx` |
| Config file | `/opt/cloud_health/config/config.yaml` |

### On the user's laptop

| What | Path |
|---|---|
| Synced program files | `~/Documents/cloud_health/program/` |
| Credentials cache (encrypted) | `~/Documents/cloud_health/credentials.cache` |
| Salt (encryption key material) | `~/Documents/cloud_health/.salt` |
| Version tracker | `~/Documents/cloud_health/version.txt` |
| Pre-flight audit database | `~/Documents/cloud_health/db/preflight.db` |
| HTML reports | Your configured **Output Dir** (default: `./outputs/`) |

> **Upgrading from an earlier beta?** Beta 5 uses `~/Documents/cloud_health/` exclusively. If you were using `~/.cloud_health/` before, re-enter your credentials once after upgrading.

### On each cluster bastion

| What | Path |
|---|---|
| Run history database | `/opt/cloud_health/db/history.db` |
| Backend engine (pushed per run) | `/tmp/cloud_health/` |
| Vendor wheels (pushed with backend) | `/tmp/cloud_health/vendor/` |
| System log | `/tmp/cloud_health/log/system_YYYYMMDD_HHMMSS.log` |
| Per-check command log | `/tmp/cloud_health/log/commands_YYYYMMDD_HHMMSS.log` |
| Host check log | `/tmp/cloud_health/log/hosts_YYYYMMDD_HHMMSS.log` |
| Run lock file | `/tmp/cloud_health/hc.lock` |

---

## Troubleshooting

**The credential prompt opens but won't accept my password.**
You'll be re-prompted up to three times. After three failures the bootstrapper exits. Double-check your source server SSH credentials with your administrator. To force a fresh prompt on the next launch, delete the cache file: `~/Documents/cloud_health/credentials.cache`.

**Pre-flight fails for a cluster.**
Check the Status column. Common causes:
- **Not reachable** — bastion IP is wrong or network is blocked; try `ssh user@<ip>` manually
- **Auth failed** — wrong credentials in the inventory file
- **python3 unavailable** — Python 3 is not installed on that bastion

Tick **Ignore failures and proceed anyway** to skip the failing clusters and continue with the rest.

**A cluster shows BUSY before any checks run.**
Another CloudHealth run is already active on that bastion (the conflicting PID and start time are shown). Wait for it to finish, or use **Stop** on the active session.

**A cluster shows ERROR at startup.**
Connection or backend launch failed. Expand the error message for details. The most common cause is an SSH auth failure — verify the `ssh_user` and `ssh_pass`/`ssh_key` columns in the inventory.

**The push step fails with "requirements.txt or vendor/ missing".**
The source server is missing the `vendor/` directory. Run the `pip download` command in [Step 3](#step-3--bundle-dependencies-vendor-wheels) on the source server.

**The run hangs or takes much longer than expected.**
Lower **Parallel Limit** and **Max Nodes** in the Configuration tab. Some environments throttle parallel SSH connections and an aggressive setting can cause queuing or timeouts.

**A specific check fails but I think it's a false positive.**
Click the check to expand its details panel and read the actual command output. If the threshold is the issue, set a per-cluster override in the inventory file rather than changing the global default in `config.yaml`.

**A check shows SKIP.**
The check doesn't apply to this cluster type (e.g., a CVIM check on an OCP cluster). This is expected — not an error.

**Windows SmartScreen blocks the .exe on first launch.**
Click **More info**, then **Run anyway**. The exe is not code-signed but is safe. This prompt appears only once per machine.

---

## Getting help

1. Open the Full Report and use the **Failures** filter to see only the problems.
2. Expand a failing check — the **command** field shows exactly what was run, and the **detail** field shows what came back.
3. Check the **History** tab to compare against previous runs — the diff badges tell you whether a failure is new or pre-existing.
4. If you need to escalate, copy the cluster name, check name, command, and output.
