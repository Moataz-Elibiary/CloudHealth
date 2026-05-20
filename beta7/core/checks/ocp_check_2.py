"""OCP Health Checks — comprehensive 20+ categories via oc CLI on bastion SSH."""
from __future__ import annotations
import asyncio, json, re
from datetime import datetime
from typing import List
import logging

from core.inventory import ClusterConfig, AppSettings as AppConfig, resolve_threshold
from core.result import SectionResult as Section, Status
from core.ssh_client import SSHClient


# ── Expected operator list (DOCX §"Verify necessary Operators are installed") ──
REQUIRED_OPERATORS = [
    "odf-operator",
    "ocs-operator",
    "mcg-operator",
    "odf-csi-addons-operator",
    "openshift-logging",
    "loki-operator",
    "local-storage-operator",
    # Hub-cluster-only operators (checked but flagged as informational if absent)
    "advanced-cluster-management",
    "multicluster-engine",
    "quay-operator",
    "openshift-gitops-operator",
    "cert-manager-operator",
    "topology-aware-lifecycle-manager",
    "cincinnati-operator",          # OpenShift Update Service
]

# Operators that are only required on the hub cluster
HUB_ONLY_OPERATORS = {
    "advanced-cluster-management",
    "multicluster-engine",
    "quay-operator",
    "openshift-gitops-operator",
    "cert-manager-operator",
    "topology-aware-lifecycle-manager",
    "cincinnati-operator",
}


class OCPHealthChecker:
    def __init__(self, cluster: ClusterConfig, app: AppConfig,
                 ssh: SSHClient, logger: logging.Logger, console):
        self.c   = cluster
        self.app = app
        self.ssh = ssh
        self.log = logger
        self.con = console
        self._nodes: List[str] = []

    # ── helpers ───────────────────────────────────────────────────────────────

    def _thr(self, attr: str):
        v = getattr(self.c, attr, None)
        return v if v is not None else getattr(self.app, attr)

    async def _oc(self, cmd: str, timeout: int = None) -> 'CmdResult':
        t = timeout or self.app.cmd_timeout
        r = await self.ssh.run(f"oc {cmd}", timeout=t)
        self.log.debug(f"[{self.c.name}] oc {cmd[:70]} rc={r.exit_code}")
        return r

    async def _lc(self, sec: Section, cmd: str, timeout: int = None) -> 'CmdResult':
        r = await self.ssh.run(cmd, timeout=timeout or self.app.cmd_timeout)
        sec.append_log(f"$ {cmd}\n{r.stdout}{r.stderr}\n")
        return r

    def _lines(self, out: str) -> List[str]:
        return [l for l in out.splitlines() if l.strip()]

    def _should(self, cat: str) -> bool:
        return self.app.enabled_ocp_checks is None or cat in self.app.enabled_ocp_checks

    # ── section runner ────────────────────────────────────────────────────────

    async def run(self) -> List[Section]:
        checks = [
            ("version",        "Cluster Version & API Server",              self._check_version),
            ("operators",      "Cluster Operators",                          self._check_operators),
            ("req_operators",  "Required Operators Inventory",               self._check_required_operators),
            ("nodes",          "Node Status",                                self._check_nodes),
            ("pressure",       "Node Resource Pressure",                     self._check_pressure),
            ("node_disk",      "Node Disk Utilization (via debug)",          self._check_node_disk),
            ("cpu_usage",      "CPU & Memory Usage (adm top)",               self._check_cpu_usage),
            ("etcd",           "etcd Health",                                self._check_etcd),
            ("etcd_encrypt",   "etcd Encryption Status",                     self._check_etcd_encryption),
            ("controlplane",   "Control-Plane Pods",                         self._check_controlplane),
            ("ceph",           "Ceph / ODF Storage",                         self._check_ceph),
            ("odf_capacity",   "ODF Raw Storage Capacity (Prometheus)",      self._check_odf_raw_capacity),
            ("multus",         "Multus CNI — ODF Network Interfaces",        self._check_multus_cni),
            ("pvcs",           "Persistent Volume Claims",                   self._check_pvcs),
            ("storageclasses", "Storage Classes",                            self._check_storageclasses),
            ("pods",           "Cluster-Wide Pod Audit",                     self._check_pods),
            ("deployments",    "Deployments & StatefulSets",                 self._check_deployments),
            ("daemonsets",     "DaemonSets",                                 self._check_daemonsets),
            ("jobs",           "Failed Jobs & CronJobs",                     self._check_jobs),
            ("hpa",            "Horizontal Pod Autoscalers",                 self._check_hpa),
            ("network",        "Network / CNI / DNS",                        self._check_network),
            ("ingress",        "Ingress / Routes",                           self._check_ingress),
            ("events",         "Warning Events",                             self._check_events),
            ("certs",          "Certificate Expiry",                         self._check_certs),
            ("tls_issuer",     "TLS Certificate Issuer (API & Ingress)",     self._check_tls_issuer),
            ("audit_logs",     "API Audit Logs",                             self._check_audit_logs),
            ("mcp",            "MachineConfigPool Status",                   self._check_mcp),
            ("nodes_upgrade",  "Node OS & Upgrade Status",                   self._check_node_os),
            ("quota",          "Resource Quotas & LimitRanges",              self._check_quotas),
            ("rbac",           "RBAC & Security (SCC audit)",                self._check_rbac),
            ("alerts",         "Prometheus Firing Alerts",                   self._check_alerts),
            ("logging",        "Cluster Logging / Loki Stack",               self._check_logging),
            ("imageregistry",  "Image Registry",                             self._check_imageregistry),
            ("backup",         "ETCD Backup Freshness",                      self._check_etcd_backup),
        ]
        sections = []
        for cat, name, fn in checks:
            if not self._should(cat):
                continue
            sec = Section(name, cat, start_time=datetime.now())
            if hasattr(self, '_wire_section'):
                sec = self._wire_section(sec)
            self.con.section_start(name)
            try:
                await fn(sec)
            except Exception as e:
                sec.error(f"Check raised exception: {e}")
                self.log.exception(f"[{self.c.name}] {cat} exception")
            sec.end_time = datetime.now()
            self.con.section_done(sec)
            sections.append(sec)
        return sections

    async def discover_nodes(self) -> List[str]:
        r = await self._oc("get nodes --no-headers -o custom-columns=NAME:.metadata.name")
        self._nodes = [l.strip() for l in self._lines(r.out)]
        return self._nodes

    # ══════════════════════════════════════════════════════════════════════════
    #  1. Cluster version
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_version(self, sec: Section):
        r = await self._lc(sec, "oc version")
        if r.exit_code != 0:
            sec.fail("Cannot reach API server", detail=r.stderr, command="oc version"); return
        m = re.search(r"Server Version:\s+(\S+)", r.stdout)
        if m:
            sec.pass_(f"API Server reachable — Version: {m.group(1)}", command="oc version")
        else:
            sec.fail("Cannot parse server version", detail=r.out)
        for cond, want, bad in [
            ("Available",   "True",  "Available=False — cluster may be degraded"),
            ("Progressing", "False", "Cluster is still progressing/upgrading"),
            ("Degraded",    "False", "ClusterVersion is Degraded"),
        ]:
            cmd = f"oc get clusterversion version -o jsonpath='{{.status.conditions[?(@.type==\"{cond}\")].status}}'"
            r2  = await self._lc(sec, cmd)
            val = r2.out.strip("'")
            if val == want:
                sec.pass_(f"ClusterVersion {cond}={want}", command=cmd)
            elif val:
                sec.fail(f"ClusterVersion {cond}={val} — {bad}", command=cmd)
            else:
                sec.warn(f"Could not read ClusterVersion/{cond}", command=cmd)
        # channel & desired version
        r3 = await self._oc("get clusterversion version -o jsonpath='{.spec.channel} {.status.desired.version}'")
        if r3.out:
            sec.info(f"Upgrade channel: {r3.out.strip()}")

    # ══════════════════════════════════════════════════════════════════════════
    #  2. Cluster operators
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_operators(self, sec: Section):
        r = await self._lc(sec, "oc get clusteroperators --no-headers")
        lines = self._lines(r.out)
        total = len(lines)
        degraded = []
        for l in lines:
            parts = l.split()
            # NAME VERSION AVAILABLE PROGRESSING DEGRADED
            if len(parts) >= 5 and (parts[2] == "False" or parts[4] == "True"):
                degraded.append(l.strip())
        if not degraded:
            sec.pass_(f"All {total} cluster operators healthy")
        else:
            sec.fail(f"{len(degraded)}/{total} operators degraded/unavailable",
                     detail="\n".join(degraded))

    # ══════════════════════════════════════════════════════════════════════════
    #  NEW #3 — Required Operators Inventory
    #  Verifies all expected operators from the NRFU document are installed
    #  and reports hub-only operators separately as informational.
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_required_operators(self, sec: Section):
        # Fetch all subscriptions cluster-wide (name + namespace)
        r = await self._lc(sec,
            "oc get subs -A --no-headers "
            "-o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,PKG:.spec.name,STATE:.status.state",
            timeout=60)
        lines = self._lines(r.out)
        if not lines:
            sec.warn("No Subscriptions found — cannot verify operator inventory"); return

        # Build a flat set of installed package names (lowercase for matching)
        installed: dict[str, str] = {}   # pkg_name → "NS/NAME (state)"
        for line in lines:
            parts = line.split()
            if len(parts) >= 3:
                ns, name, pkg = parts[0], parts[1], parts[2]
                state = parts[3] if len(parts) > 3 else "unknown"
                installed[pkg.lower()] = f"{ns}/{name} [{state}]"

        sec.info(f"Total subscriptions found: {len(installed)}")

        missing_required = []
        missing_hub_only = []
        found = []

        for op in REQUIRED_OPERATORS:
            # Flexible match: check if any installed key contains the operator token
            match = next((k for k in installed if op in k or k in op), None)
            if match:
                found.append(f"  ✔  {op:50s}  →  {installed[match]}")
            elif op in HUB_ONLY_OPERATORS:
                missing_hub_only.append(op)
            else:
                missing_required.append(op)

        # Report found operators
        if found:
            sec.info(f"{len(found)} required operator(s) present",
                     detail="\n".join(found))

        # Hub-only operators not found → informational only
        if missing_hub_only:
            sec.info(
                f"{len(missing_hub_only)} hub-only operator(s) not found "
                f"(expected only on hub cluster)",
                detail="\n".join(f"  -  {op}" for op in missing_hub_only))

        # Core operators missing → fail
        if missing_required:
            sec.fail(
                f"{len(missing_required)} required operator(s) NOT installed",
                detail="\n".join(f"  ✘  {op}" for op in missing_required))
        else:
            sec.pass_("All core required operators are installed")

    # ══════════════════════════════════════════════════════════════════════════
    #  3. Nodes
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_nodes(self, sec: Section):
        r = await self._lc(sec, "oc get nodes --no-headers -o wide")
        lines = self._lines(r.out)
        total = len(lines)
        not_ready = [l for l in lines if not re.search(r'\bReady\b', l.split()[1] if len(l.split())>1 else "")]
        if not not_ready:
            sec.pass_(f"All {total} nodes in Ready state")
        else:
            sec.fail(f"{len(not_ready)}/{total} nodes NOT Ready", detail="\n".join(not_ready))
        # roles
        for role in ("master", "worker", "infra"):
            r2 = await self._oc(f"get nodes --no-headers -l node-role.kubernetes.io/{role} 2>/dev/null | wc -l")
            cnt = r2.out.strip()
            if cnt and cnt != "0":
                sec.info(f"Role '{role}': {cnt} node(s)")
        self._nodes = [l.split()[0] for l in lines if l.split()]

    # ══════════════════════════════════════════════════════════════════════════
    #  4. Resource pressure
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_pressure(self, sec: Section):
        r = await self._lc(sec, "oc get nodes -o json", timeout=90)
        if r.exit_code != 0:
            sec.warn("Cannot retrieve node JSON", detail=r.stderr); return
        try:
            data = json.loads(r.stdout)
            bad  = []
            for n in data.get("items", []):
                name = n["metadata"]["name"]
                for cond in n["status"].get("conditions", []):
                    if cond["type"] in ("MemoryPressure","DiskPressure","PIDPressure") and cond["status"]=="True":
                        bad.append(f"{name} — {cond['type']}")
            if not bad:
                sec.pass_("No MemoryPressure / DiskPressure / PIDPressure on any node")
            else:
                sec.fail(f"Pressure conditions on {len(bad)} node(s)", detail="\n".join(bad))
        except json.JSONDecodeError:
            sec.warn("Could not parse node JSON")

    # ══════════════════════════════════════════════════════════════════════════
    #  5. Node disk (via oc debug — best-effort, OCP nodes)
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_node_disk(self, sec: Section):
        thr = self._thr("disk_threshold")
        nodes = self._nodes or await self.discover_nodes()
        if not nodes:
            sec.skip("No nodes discovered for disk check"); return
        for node in nodes[:3]:
            cmd = (f"oc debug node/{node} --quiet -- chroot /host "
                   f"df -h --output=source,pcent,target 2>/dev/null | grep -v tmpfs | grep -v devtmpfs || true")
            r = await self._lc(sec, cmd, timeout=60)
            if not r.out:
                sec.warn(f"Could not get disk info for {node} via oc debug"); continue
            ok = True
            for line in self._lines(r.out):
                parts = line.split()
                if len(parts) < 3: continue
                try:
                    pct = int(parts[1].rstrip("%"))
                except ValueError:
                    continue
                if pct >= thr:
                    sec.fail(f"{node}: {parts[2]} at {pct}% (>{thr}%)", command=cmd)
                    ok = False
            if ok:
                sec.pass_(f"{node}: all mounts below {thr}%")
        if len(nodes) > 3:
            sec.info(f"Remaining {len(nodes)-3} nodes checked via direct SSH (host checks)")

    # ══════════════════════════════════════════════════════════════════════════
    #  NEW #2 — CPU & Memory Usage (oc adm top)
    #  Reports cluster-wide resource consumption for nodes and pods.
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_cpu_usage(self, sec: Section):
        # ── Node-level utilisation ─────────────────────────────────────────
        r_nodes = await self._lc(sec, "oc adm top nodes 2>/dev/null || true", timeout=60)
        node_lines = self._lines(r_nodes.out)
        if node_lines:
            sec.info(f"Node resource utilisation ({len(node_lines)-1} nodes)",
                     detail="\n".join(node_lines))
            # Warn on any node with CPU% or MEM% ≥ 85
            high_cpu  = []
            high_mem  = []
            for line in node_lines[1:]:          # skip header
                parts = line.split()
                # NAME  CPU(cores)  CPU%  MEMORY(bytes)  MEMORY%
                if len(parts) < 5: continue
                try:
                    cpu_pct = int(parts[2].rstrip("%"))
                    mem_pct = int(parts[4].rstrip("%"))
                    if cpu_pct >= 85:
                        high_cpu.append(f"{parts[0]}: CPU {cpu_pct}%")
                    if mem_pct >= 85:
                        high_mem.append(f"{parts[0]}: MEM {mem_pct}%")
                except ValueError:
                    continue
            if high_cpu:
                sec.warn(f"{len(high_cpu)} node(s) with CPU ≥ 85%",
                         detail="\n".join(high_cpu))
            else:
                sec.pass_("All nodes below 85% CPU utilisation")
            if high_mem:
                sec.warn(f"{len(high_mem)} node(s) with Memory ≥ 85%",
                         detail="\n".join(high_mem))
            else:
                sec.pass_("All nodes below 85% Memory utilisation")
        else:
            sec.warn("Could not retrieve node top data (metrics-server may not be ready)")

        # ── Pod-level aggregated sum ──────────────────────────────────────
        r_pods = await self._lc(sec,
            "oc adm top pod --all-namespaces --sum 2>/dev/null | tail -3 || true",
            timeout=60)
        if r_pods.out:
            sec.info("Pod CPU/Memory summary (cluster-wide --sum)",
                     detail=r_pods.out.strip())
        else:
            sec.warn("Could not retrieve pod top summary")

    # ══════════════════════════════════════════════════════════════════════════
    #  6. etcd
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_etcd(self, sec: Section):
        r = await self._lc(sec, "oc get pods -n openshift-etcd -l app=etcd --no-headers -o wide")
        lines = self._lines(r.out)
        total = len(lines)
        bad   = [l for l in lines if "Running" not in l]
        if total == 0:
            sec.warn("No etcd pods found")
        elif not bad:
            sec.pass_(f"All {total} etcd pods Running")
        else:
            sec.fail(f"{len(bad)}/{total} etcd pods not Running", detail="\n".join(bad))
        # etcdctl health
        r2 = await self._lc(sec, "oc get pods -n openshift-etcd -l app=etcd --no-headers "
                                  "-o custom-columns=NAME:.metadata.name | head -1")
        pod = r2.out.strip()
        if pod:
            r3 = await self._lc(sec,
                f"oc exec -n openshift-etcd {pod} -c etcd -- "
                f"etcdctl endpoint health --cluster 2>/dev/null || true", timeout=30)
            if "healthy: true" in r3.stdout:
                sec.pass_("etcd endpoints healthy (etcdctl)")
            elif "unhealthy" in r3.stdout:
                sec.fail("etcd endpoint unhealthy", detail=r3.out[:500])
            # etcd leader check
            r4 = await self._lc(sec,
                f"oc exec -n openshift-etcd {pod} -c etcd -- "
                f"etcdctl endpoint status --cluster -w table 2>/dev/null || true", timeout=30)
            if r4.out:
                sec.info("etcd endpoint status", detail=r4.out[:800])
        # etcd backup CR
        r5 = await self._oc("get etcdbackup -A --no-headers 2>/dev/null | head -5 || true")
        if r5.out:
            sec.info("Recent etcd backups", detail=r5.out[:400])

    # ══════════════════════════════════════════════════════════════════════════
    #  NEW #8 — etcd Encryption Status
    #  Confirms EncryptionCompleted on openshiftapiserver, kubeapiserver,
    #  and oauthserver (DOCX §"ETCD Encryption Check").
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_etcd_encryption(self, sec: Section):
        resources = [
            ("openshiftapiserver",  "OpenShift API Server"),
            ("kubeapiserver",       "Kube API Server"),
            ("authentication",      "OAuth Server"),
        ]
        all_ok = True
        for res, label in resources:
            cmd = (f"oc get {res} -o jsonpath='"
                   f"{{range .items[0].status.conditions"
                   f"[?(@.type==\"Encrypted\")]}}{{.reason}}|{{.message}}{{end}}'  "
                   f"2>/dev/null || true")
            r = await self._lc(sec, cmd, timeout=30)
            raw = r.out.strip().strip("'")
            if not raw:
                sec.warn(f"{label}: could not read Encrypted condition"); all_ok = False; continue

            reason, _, message = raw.partition("|")
            if reason == "EncryptionCompleted":
                sec.pass_(f"{label}: EncryptionCompleted — {message[:120]}")
            elif reason in ("EncryptionInProgress", "MigratingResources"):
                sec.warn(f"{label}: encryption in progress — {message[:120]}"); all_ok = False
            else:
                sec.fail(f"{label}: encryption NOT complete — reason={reason} {message[:120]}")
                all_ok = False

        if all_ok:
            sec.pass_("etcd encryption is complete across all API servers")

    # ══════════════════════════════════════════════════════════════════════════
    #  7. Control-plane pods
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_controlplane(self, sec: Section):
        nss = ["openshift-apiserver","openshift-controller-manager",
               "openshift-kube-apiserver","openshift-kube-controller-manager",
               "openshift-kube-scheduler","openshift-authentication"]
        for ns in nss:
            r = await self._lc(sec, f"oc get pods -n {ns} --no-headers")
            lines = self._lines(r.out)
            total = len(lines)
            bad   = [l for l in lines if not re.search(r"Running|Completed|Succeeded", l)]
            if total == 0:
                sec.warn(f"{ns}: no pods found")
            elif not bad:
                sec.pass_(f"{ns}: {total} pod(s) healthy")
            else:
                sec.fail(f"{ns}: {len(bad)}/{total} pods unhealthy", detail="\n".join(bad))

    # ══════════════════════════════════════════════════════════════════════════
    #  8. Ceph / ODF
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_ceph(self, sec: Section):
        odf_ns = None
        for ns in ("openshift-storage","rook-ceph"):
            r = await self.ssh.run(f"oc get ns {ns} 2>/dev/null", timeout=15)
            if r.exit_code == 0:
                odf_ns = ns; break
        if not odf_ns:
            sec.skip("No Ceph/ODF namespace found"); return
        sec.info(f"ODF namespace: {odf_ns}")
        r = await self._lc(sec, f"oc get pods -n {odf_ns} --no-headers -o wide")
        lines = self._lines(r.out)
        total = len(lines)
        bad   = [l for l in lines if not re.search(r"Running|Completed|Succeeded", l)]
        sec.pass_(f"All {total} ODF pods healthy") if not bad else sec.fail(f"{len(bad)}/{total} ODF pods unhealthy", detail="\n".join(bad[:15]))
        for comp in ("rook-ceph-mon","rook-ceph-mgr","rook-ceph-osd","rook-ceph-mds","csi-cephfsplugin","csi-rbdplugin","noobaa"):
            r2 = await self.ssh.run(f"oc get pods -n {odf_ns} --no-headers | grep {comp}", timeout=20)
            pods = self._lines(r2.out)
            if not pods: continue
            bad2 = [p for p in pods if not re.search(r"Running|Completed|Succeeded", p)]
            sec.pass_(f"{comp}: {len(pods)} pod(s) Running") if not bad2 else sec.fail(f"{comp}: {len(bad2)}/{len(pods)} unhealthy", detail="\n".join(bad2))
        tb = await self.ssh.run(f"oc get pods -n {odf_ns} --no-headers | grep -E 'rook-ceph-tools|toolbox' | grep Running | head -1 | awk '{{print $1}}'", timeout=20)
        toolbox = tb.out.strip()
        if toolbox:
            for cmd, label in [
                (f"oc exec -n {odf_ns} {toolbox} -- ceph status","ceph status"),
                (f"oc exec -n {odf_ns} {toolbox} -- ceph osd status","osd status"),
                (f"oc exec -n {odf_ns} {toolbox} -- ceph df","ceph df"),
                (f"oc exec -n {odf_ns} {toolbox} -- ceph pg stat","pg stat"),
            ]:
                r3 = await self._lc(sec, cmd, timeout=40)
                if "HEALTH_OK" in r3.stdout:
                    sec.pass_("Ceph cluster: HEALTH_OK")
                elif "HEALTH_WARN" in r3.stdout:
                    sec.warn("Ceph cluster: HEALTH_WARN", detail=r3.out[:400])
                elif "HEALTH_ERR" in r3.stdout:
                    sec.fail("Ceph cluster: HEALTH_ERR", detail=r3.out[:400])
                if label == "osd status":
                    down = len(re.findall(r"\bdown\b|\bout\b", r3.stdout, re.I))
                    (sec.pass_("All Ceph OSDs up") if not down else sec.fail(f"{down} OSD(s) down/out", detail=r3.out[:300]))
                if label == "pg stat":
                    if re.search(r"degraded|incomplete|inconsistent|stale", r3.stdout, re.I):
                        sec.fail("Ceph PGs have issues", detail=r3.out[:300])
                    else:
                        sec.pass_("Ceph PGs healthy")
        sc_r = await self._oc(f"get storagecluster -n {odf_ns} -o jsonpath='{{.items[0].status.phase}}'")
        ph = sc_r.out.strip("'")
        (sec.pass_(f"StorageCluster: {ph}") if ph == "Ready" else sec.warn(f"StorageCluster: {ph or 'unknown'}"))

    # ══════════════════════════════════════════════════════════════════════════
    #  NEW #1 — ODF Raw Storage Capacity via Prometheus
    #  Queries odf_system_raw_capacity_used_bytes and
    #  odf_system_raw_capacity_total_bytes (DOCX §"Verify Used raw storage capacity").
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_odf_raw_capacity(self, sec: Section):
        # Resolve Prometheus route
        r_route = await self._lc(sec,
            "oc get route prometheus-k8s -n openshift-monitoring "
            "--no-headers -o custom-columns=HOST:.spec.host 2>/dev/null || true",
            timeout=20)
        prom_host = r_route.out.strip()
        if not prom_host:
            sec.warn("Cannot resolve prometheus-k8s route — skipping ODF capacity check"); return

        token_cmd = "oc whoami -t 2>/dev/null"
        r_tok = await self._lc(sec, token_cmd, timeout=15)
        token = r_tok.out.strip()
        if not token:
            sec.warn("Cannot obtain bearer token — skipping ODF capacity check"); return

        prom_base = f"https://{prom_host}/api/v1/query"
        all_ok = True

        for metric, label in [
            ("odf_system_raw_capacity_total_bytes", "Total raw capacity"),
            ("odf_system_raw_capacity_used_bytes",  "Used raw capacity"),
        ]:
            cmd = (f'curl -skH "Authorization: Bearer {token}" '
                   f'"{prom_base}?query={metric}" | python3 -c '
                   f'"import sys,json; d=json.load(sys.stdin); '
                   f'r=d.get(\'data\',{{}}).get(\'result\',[{}]); '
                   f'v=r[0].get(\'value\',[None,\'N/A\'])[1] if r else \'N/A\'; '
                   f'print(v)"')
            r = await self._lc(sec, cmd, timeout=30)
            raw_val = r.out.strip()
            if raw_val and raw_val != "N/A":
                try:
                    bytes_val = float(raw_val)
                    gib = bytes_val / (1024 ** 3)
                    sec.info(f"{label}: {gib:.1f} GiB ({bytes_val:.0f} bytes)")
                except ValueError:
                    sec.info(f"{label}: {raw_val}")
            else:
                sec.warn(f"Could not retrieve metric: {metric}"); all_ok = False

        # Compute usage percentage if both values retrieved
        used_cmd = (f'curl -skH "Authorization: Bearer {token}" '
                    f'"{prom_base}?query=odf_system_raw_capacity_used_bytes+%2F+'
                    f'odf_system_raw_capacity_total_bytes+*+100" | python3 -c '
                    f'"import sys,json; d=json.load(sys.stdin); '
                    f'r=d.get(\'data\',{{}}).get(\'result\',[{{}}]); '
                    f'v=r[0].get(\'value\',[None,\'N/A\'])[1] if r else \'N/A\'; print(v)"')
        r_pct = await self._lc(sec, used_cmd, timeout=30)
        pct_val = r_pct.out.strip()
        try:
            pct = float(pct_val)
            if pct >= 85:
                sec.fail(f"ODF raw capacity usage: {pct:.1f}% — critically high (≥85%)")
                all_ok = False
            elif pct >= 75:
                sec.warn(f"ODF raw capacity usage: {pct:.1f}% — approaching threshold (≥75%)")
            else:
                sec.pass_(f"ODF raw capacity usage: {pct:.1f}% — within acceptable range")
        except (ValueError, TypeError):
            sec.warn(f"Could not compute ODF capacity percentage (raw: '{pct_val}')")

    # ══════════════════════════════════════════════════════════════════════════
    #  NEW #4 — Multus CNI — ODF Network Interfaces
    #  Confirms OSD pods have ocs-cluster and ocs-public Multus interfaces
    #  attached (DOCX §"Attach multiple interfaces to ODF to check Multus CNI").
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_multus_cni(self, sec: Section):
        # Detect ODF namespace
        odf_ns = None
        for ns in ("openshift-storage", "rook-ceph"):
            r = await self.ssh.run(f"oc get ns {ns} 2>/dev/null", timeout=15)
            if r.exit_code == 0:
                odf_ns = ns; break
        if not odf_ns:
            sec.skip("No ODF namespace found — skipping Multus CNI check"); return

        # Confirm StorageCluster is using multus provider
        r_sc = await self._lc(sec,
            f"oc get storagecluster ocs-storagecluster -n {odf_ns} "
            f"-o jsonpath='{{.spec.network.provider}}' 2>/dev/null || true",
            timeout=20)
        provider = r_sc.out.strip().strip("'")
        if provider != "multus":
            sec.warn(f"StorageCluster network provider is '{provider or 'unset'}' — expected 'multus'")
        else:
            sec.pass_("StorageCluster network provider: multus")

        # Check network-status annotation on a sample OSD pod
        r_osd = await self._lc(sec,
            f"oc get pods -n {odf_ns} -l app=rook-ceph-osd --no-headers "
            f"-o custom-columns=NAME:.metadata.name 2>/dev/null | head -3 || true",
            timeout=20)
        osd_pods = [p.strip() for p in self._lines(r_osd.out)]
        if not osd_pods:
            sec.warn("No rook-ceph-osd pods found — cannot verify Multus interfaces"); return

        expected_nets = {"ovn-kubernetes", f"{odf_ns}/ocs-cluster", f"{odf_ns}/ocs-public"}
        annotation_key = r"k8s\.v1\.cni\.cncf\.io/network-status"

        for pod in osd_pods:
            r_ann = await self._lc(sec,
                f"oc get pod {pod} -n {odf_ns} "
                f"-o jsonpath='{{.metadata.annotations.k8s\\.v1\\.cni\\.cncf\\.io/network-status}}' "
                f"2>/dev/null || true",
                timeout=20)
            raw = r_ann.out.strip().strip("'")
            if not raw:
                sec.warn(f"{pod}: network-status annotation not found"); continue
            try:
                interfaces = json.loads(raw)
                attached_names = {iface.get("name", "") for iface in interfaces}
                missing_nets = expected_nets - attached_names
                if missing_nets:
                    sec.fail(
                        f"{pod}: missing expected Multus network(s): {', '.join(missing_nets)}",
                        detail=f"Attached: {', '.join(sorted(attached_names))}")
                else:
                    # Report IPs for each expected interface
                    detail_lines = []
                    for iface in interfaces:
                        name = iface.get("name", "?")
                        ips  = ", ".join(iface.get("ips", []))
                        iface_name = iface.get("interface", "?")
                        detail_lines.append(f"  {name} ({iface_name}): {ips or 'no IP'}")
                    sec.pass_(f"{pod}: all expected Multus interfaces present",
                               detail="\n".join(detail_lines))
            except json.JSONDecodeError:
                sec.warn(f"{pod}: could not parse network-status annotation",
                         detail=raw[:300])

    # ══════════════════════════════════════════════════════════════════════════
    #  9. PVCs
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_pvcs(self, sec: Section):
        r = await self._lc(sec, "oc get pvc -A --no-headers")
        lines = self._lines(r.out)
        total = len(lines)
        lost  = [l for l in lines if re.search(r"\bLost\b|\bPending\b", l)]
        sec.pass_(f"All {total} PVCs Bound") if not lost else sec.fail(f"{len(lost)}/{total} PVCs Lost/Pending", detail="\n".join(lost[:20]))
        r2 = await self._lc(sec, "oc get pv --no-headers | grep -vE 'Bound|Released' | head -10 || true")
        if r2.out:
            sec.warn("PVs in non-Bound state", detail=r2.out[:400])

    # ══════════════════════════════════════════════════════════════════════════
    #  10. Storage classes
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_storageclasses(self, sec: Section):
        r = await self._lc(sec, "oc get sc --no-headers")
        lines = self._lines(r.out)
        if not lines:
            sec.warn("No StorageClasses found"); return
        sec.info(f"{len(lines)} StorageClass(es) available", detail="\n".join(lines))
        default = [l for l in lines if "(default)" in l]
        if len(default) == 1:
            sec.pass_(f"Default StorageClass: {default[0].split()[0]}")
        elif len(default) == 0:
            sec.warn("No default StorageClass defined")
        else:
            sec.warn(f"Multiple default StorageClasses: {len(default)} — may cause issues")

    # ══════════════════════════════════════════════════════════════════════════
    #  11. Pods audit
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_pods(self, sec: Section):
        rw = self._thr("restart_warn_threshold")
        rf = self._thr("restart_fail_threshold")
        aw = self._thr("pod_age_min_warn")
        af = self._thr("pod_age_min_fail")
        sec.info(f"Thresholds — restarts WARN≥{rw} FAIL≥{rf} | age WARN<{aw}m FAIL<{af}m")
        r = await self._lc(sec, "oc get pods -A --no-headers", timeout=120)
        lines = self._lines(r.out)
        total = bad_st = r_warn = r_fail = age_warn = age_fail = 0
        flagged = []

        def age_min(a: str) -> int:
            d = int(m.group(1)) if (m := re.search(r"(\d+)d", a)) else 0
            h = int(m.group(1)) if (m := re.search(r"(\d+)h", a)) else 0
            mi= int(m.group(1)) if (m := re.search(r"(\d+)m", a)) else 0
            return d*1440 + h*60 + mi

        for line in lines:
            parts = line.split()
            if len(parts) < 5: continue
            ns, name, ready, status, restarts = parts[0], parts[1], parts[2], parts[3], parts[4]
            age = parts[5] if len(parts) > 5 else "0m"
            total += 1
            flags = []; sev = "ok"
            if status not in ("Running","Completed","Succeeded"):
                flags.append(f"status:{status}"); bad_st += 1; sev = "fail"
            try:
                rc = int(re.match(r"^\d+", restarts).group())
            except Exception:
                rc = 0
            if rc >= rf:
                flags.append(f"restarts:{rc}[≥{rf}]"); r_fail += 1; sev = "fail"
            elif rc >= rw:
                flags.append(f"restarts:{rc}[≥{rw}]"); r_warn += 1
                if sev == "ok": sev = "warn"
            if status not in ("Completed","Succeeded"):
                am = age_min(age)
                if am < af:
                    flags.append(f"age:{age}[<{af}m]"); age_fail += 1; sev = "fail"
                elif am < aw:
                    flags.append(f"age:{age}[<{aw}m]"); age_warn += 1
                    if sev == "ok": sev = "warn"
            if flags:
                flagged.append((sev, ns, name, age, rc, ", ".join(flags)))

        sec.info(f"Total pods scanned: {total}")
        (sec.pass_ if bad_st == 0 else sec.fail)(
            f"Pod status — {total-bad_st}/{total} Running/Completed/Succeeded" if bad_st else f"All {total} pods Running/Completed/Succeeded")
        (sec.fail if r_fail > 0 else (sec.warn if r_warn > 0 else sec.pass_))(
            f"Restarts — {r_fail} CRITICAL ≥{rf}, {r_warn} HIGH ≥{rw}" if r_fail or r_warn else f"Restarts — all below {rw}")
        (sec.fail if age_fail > 0 else (sec.warn if age_warn > 0 else sec.pass_))(
            f"Pod age — {age_fail} very young (<{af}m), {age_warn} recently started (<{aw}m)" if age_fail or age_warn else f"Pod age — all stable")
        if flagged:
            hdr = f"{'SEV':<6} {'NAMESPACE/POD':<52} {'FLAGS':<38} {'AGE':<7} REST\n" + "-"*110
            rows_txt = "\n".join(f"{s.upper():<6} {ns+'/'+nm:<52} {fl:<38} {ag:<7} {rc}"
                                  for s,ns,nm,ag,rc,fl in flagged[:60])
            sec.info("Flagged pods", detail=hdr+"\n"+rows_txt)

    # ══════════════════════════════════════════════════════════════════════════
    #  12. Deployments & StatefulSets
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_deployments(self, sec: Section):
        for kind in ("deployment","statefulset"):
            r = await self._lc(sec, f"oc get {kind} -A --no-headers")
            lines = self._lines(r.out)
            bad = []
            for l in lines:
                parts = l.split()
                if len(parts) >= 4:
                    ready_str = parts[2]
                    if "/" in ready_str:
                        ready, desired = ready_str.split("/", 1)
                        try:
                            if int(ready) < int(desired): bad.append(l.strip())
                        except ValueError:
                            pass
            total = len(lines)
            if not bad:
                sec.pass_(f"{kind.capitalize()}s: all {total} healthy")
            else:
                sec.fail(f"{kind.capitalize()}s: {len(bad)}/{total} not fully available",
                         detail="\n".join(bad[:20]))

    # ══════════════════════════════════════════════════════════════════════════
    #  13. DaemonSets
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_daemonsets(self, sec: Section):
        r = await self._lc(sec, "oc get daemonset -A --no-headers")
        lines = self._lines(r.out)
        bad = []
        for l in lines:
            parts = l.split()
            if len(parts) >= 6:
                try:
                    desired = int(parts[2]); ready = int(parts[4])
                    if ready < desired: bad.append(l.strip())
                except ValueError:
                    pass
        sec.pass_(f"All {len(lines)} DaemonSets have full coverage") if not bad else \
            sec.fail(f"{len(bad)}/{len(lines)} DaemonSets with missing pods", detail="\n".join(bad[:20]))

    # ══════════════════════════════════════════════════════════════════════════
    #  14. Jobs & CronJobs
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_jobs(self, sec: Section):
        r = await self._lc(sec, "oc get jobs -A --no-headers 2>/dev/null | grep -v ' 1/1 ' | head -20 || true")
        lines = self._lines(r.out)
        if not lines:
            sec.pass_("No failed/incomplete Jobs found")
        else:
            sec.warn(f"{len(lines)} Job(s) not complete", detail="\n".join(lines[:15]))
        r2 = await self._lc(sec, "oc get cronjob -A --no-headers 2>/dev/null | head -20 || true")
        if r2.out:
            suspended = [l for l in self._lines(r2.out) if "True" in l.split()[2:4]]
            sec.info(f"CronJobs: {len(self._lines(r2.out))} total, {len(suspended)} suspended")

    # ══════════════════════════════════════════════════════════════════════════
    #  15. HPA
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_hpa(self, sec: Section):
        r = await self._lc(sec, "oc get hpa -A --no-headers 2>/dev/null || true")
        lines = self._lines(r.out)
        if not lines:
            sec.info("No HorizontalPodAutoscalers defined"); return
        at_max = []
        for l in lines:
            parts = l.split()
            if len(parts) >= 7:
                try:
                    current = int(parts[6]); maxr = int(parts[5])
                    if current >= maxr: at_max.append(l.strip())
                except (ValueError, IndexError):
                    pass
        sec.info(f"{len(lines)} HPA(s) configured")
        if at_max:
            sec.warn(f"{len(at_max)} HPA(s) at max replicas — may be capacity constrained",
                     detail="\n".join(at_max))
        else:
            sec.pass_("No HPAs at maximum replica count")

    # ══════════════════════════════════════════════════════════════════════════
    #  16. Network / CNI / DNS
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_network(self, sec: Section):
        r = await self._lc(sec, "oc get co network --no-headers 2>/dev/null || true")
        if r.out and "True" in r.out:
            sec.pass_("Network cluster operator: Available")
        elif r.out:
            sec.warn("Network cluster operator state unexpected", detail=r.out)
        r2 = await self._lc(sec, "oc get co dns --no-headers 2>/dev/null || true")
        if r2.out:
            parts = r2.out.split()
            avail = parts[2] if len(parts)>2 else "?"
            (sec.pass_ if avail == "True" else sec.warn)(f"DNS operator Available={avail}")
        r3 = await self._lc(sec, "oc get pods -n openshift-dns --no-headers 2>/dev/null | head -5")
        lines = self._lines(r3.out)
        bad = [l for l in lines if "Running" not in l]
        (sec.pass_(f"openshift-dns: {len(lines)} pod(s) Running") if not bad else
         sec.fail(f"openshift-dns: {len(bad)} pods not Running", detail="\n".join(bad)))
        for ns in ("openshift-sdn","openshift-ovn-kubernetes"):
            r4 = await self.ssh.run(f"oc get pods -n {ns} --no-headers 2>/dev/null | wc -l", timeout=15)
            cnt = r4.out.strip()
            if cnt and cnt != "0":
                sec.info(f"{ns}: {cnt} pod(s)")
        r5 = await self._lc(sec, "oc get egressip -A --no-headers 2>/dev/null | head -5 || true")
        if r5.out:
            sec.info(f"EgressIPs configured", detail=r5.out[:200])

    # ══════════════════════════════════════════════════════════════════════════
    #  17. Ingress / Routes
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_ingress(self, sec: Section):
        r = await self._lc(sec, "oc get ingresscontroller -n openshift-ingress-operator --no-headers 2>/dev/null || true")
        lines = self._lines(r.out)
        if not lines:
            sec.warn("No IngressControllers found"); return
        for l in lines:
            parts = l.split()
            name = parts[0] if parts else "?"
            r2 = await self._oc(f"get ingresscontroller {name} -n openshift-ingress-operator "
                                  f"-o jsonpath='{{.status.conditions[?(@.type==\"Available\")].status}}'")
            avail = r2.out.strip("'")
            (sec.pass_ if avail == "True" else sec.fail)(
                f"IngressController '{name}' Available={avail}")
        r3 = await self._lc(sec, "oc get pods -n openshift-ingress --no-headers 2>/dev/null")
        lines3 = self._lines(r3.out)
        bad = [l for l in lines3 if "Running" not in l]
        (sec.pass_(f"openshift-ingress: {len(lines3)} router pod(s) Running") if not bad else
         sec.fail(f"openshift-ingress: {len(bad)} pods not Running", detail="\n".join(bad)))

    # ══════════════════════════════════════════════════════════════════════════
    #  18. Warning events
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_events(self, sec: Section):
        r = await self._lc(sec, "oc get events -A --field-selector type=Warning --no-headers 2>/dev/null | "
                                 "sort -k1,1 -k6,6rn | head -30 || true", timeout=30)
        lines = self._lines(r.out)
        if not lines:
            sec.pass_("No Warning events across all namespaces")
        else:
            sec.warn(f"{len(lines)} Warning event(s)", detail="\n".join(lines[:25]))

    # ══════════════════════════════════════════════════════════════════════════
    #  19. Certificates — expiry
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_certs(self, sec: Section):
        warn_days = self.app.cert_warn_days
        r = await self._lc(sec, "oc get secret -A --field-selector type=kubernetes.io/tls "
                                 "--no-headers -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name",
                            timeout=30)
        lines = self._lines(r.out)
        any_issue = False
        for line in lines[:80]:
            parts = line.split()
            if len(parts) < 2: continue
            ns, name = parts[0], parts[1]
            r2 = await self.ssh.run(
                f"oc get secret -n {ns} {name} -o jsonpath='{{.data.tls\\.crt}}' 2>/dev/null | "
                f"base64 -d 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null", timeout=15)
            if r2.exit_code != 0 or not r2.out: continue
            r3 = await self.ssh.run(
                f"echo '{r2.out.strip()}' | awk -F= '{{print $2}}' | "
                f"xargs -I{{}} sh -c 'echo $(( ($(date -d \"{{}}\" +%s 2>/dev/null || date -jf \"%b %d %T %Y %Z\" \"{{}}\" +%s 2>/dev/null) - $(date +%s)) / 86400 ))'",
                timeout=10)
            try:
                days = int(r3.out.strip())
            except Exception:
                continue
            if days < 0:
                sec.fail(f"EXPIRED: {ns}/{name} (expired {abs(days)}d ago)"); any_issue = True
            elif days < warn_days:
                sec.warn(f"Expiring soon: {ns}/{name} — {days}d remaining"); any_issue = True
        if not any_issue:
            sec.pass_(f"No certificates expiring within {warn_days}d (checked {len(lines)} TLS secrets)")

    # ══════════════════════════════════════════════════════════════════════════
    #  NEW #9 — TLS Certificate Issuer Verification (API & Ingress)
    #  Validates the issuer CN of the API and Ingress endpoints matches
    #  the expected cert-manager CA (DOCX §"TLS Certificates are replaced
    #  for API and Ingress").
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_tls_issuer(self, sec: Section):
        # Resolve API and Ingress hostnames from the cluster itself
        r_api = await self._lc(sec,
            "oc get infrastructure cluster -o jsonpath='{.status.apiServerURL}' 2>/dev/null || true",
            timeout=20)
        api_url = r_api.out.strip().strip("'")

        r_ing = await self._lc(sec,
            "oc get ingresscontroller default -n openshift-ingress-operator "
            "-o jsonpath='{.status.domain}' 2>/dev/null || true",
            timeout=20)
        ingress_domain = r_ing.out.strip().strip("'")
        # Build a sample app route hostname
        ingress_host = f"test.{ingress_domain}" if ingress_domain else ""

        endpoints: list[tuple[str, str]] = []
        if api_url:
            endpoints.append(("API", api_url))
        if ingress_host:
            endpoints.append(("Ingress", f"https://{ingress_host}"))

        if not endpoints:
            sec.warn("Could not determine API/Ingress URLs — skipping issuer check"); return

        expected_issuer_hint = getattr(self.app, "expected_tls_issuer", None)

        for label, url in endpoints:
            cmd = (f"curl -vk --max-time 10 {url} 2>&1 | "
                   f"grep -E 'issuer|subject|expire|SSL' | head -10 || true")
            r = await self._lc(sec, cmd, timeout=30)
            output = r.out.strip()
            if not output:
                sec.warn(f"{label} ({url}): no TLS details returned from curl"); continue

            # Extract issuer line
            issuer_match = re.search(r"issuer\s*:(.*)", output, re.IGNORECASE)
            issuer_str   = issuer_match.group(1).strip() if issuer_match else "unknown"

            sec.info(f"{label} TLS details", detail=output[:600])

            if expected_issuer_hint:
                if expected_issuer_hint.lower() in issuer_str.lower():
                    sec.pass_(f"{label}: issuer matches expected CA — {issuer_str[:120]}")
                else:
                    sec.fail(
                        f"{label}: issuer mismatch — got '{issuer_str[:120]}', "
                        f"expected to contain '{expected_issuer_hint}'")
            else:
                # No expected issuer configured — report and pass informational
                sec.info(f"{label} certificate issuer: {issuer_str[:120]}")
                sec.pass_(f"{label}: TLS certificate present and accessible")

    # ══════════════════════════════════════════════════════════════════════════
    #  NEW #5 — API Audit Logs Verification
    #  Confirms audit logs are generated and rotating on all master nodes
    #  for openshift-apiserver, kube-apiserver, and oauth-apiserver
    #  (DOCX §"Check and verify all important log files generated").
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_audit_logs(self, sec: Section):
        log_paths = [
            ("openshift-apiserver", "OpenShift API Server"),
            ("kube-apiserver",      "Kube API Server"),
            ("oauth-apiserver",     "OAuth API Server"),
        ]

        for path, label in log_paths:
            cmd = f"oc adm node-logs --role=master --path={path}/ 2>/dev/null | head -20 || true"
            r = await self._lc(sec, cmd, timeout=60)
            lines = self._lines(r.out)
            if not lines:
                sec.fail(f"{label}: no audit log files found via 'oc adm node-logs'"); continue

            # Count unique nodes and rotated log files
            nodes_seen: set[str] = set()
            audit_files = []
            rotated_files = []
            for line in lines:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    node, filename = parts
                    nodes_seen.add(node)
                    audit_files.append(filename.strip())
                    if re.search(r"audit-\d{4}-\d{2}-\d{2}T", filename):
                        rotated_files.append(f"{node}: {filename.strip()}")

            sec.pass_(
                f"{label}: audit logs present on {len(nodes_seen)} master node(s), "
                f"{len(audit_files)} file(s), {len(rotated_files)} rotated",
                detail="\n".join(lines[:10]))

            if not rotated_files:
                sec.warn(f"{label}: no rotated audit log files detected — "
                         f"rotation may not be configured or logs are very fresh")

        # Infrastructure / journal logs (master nodes)
        r_j = await self._lc(sec,
            "oc adm node-logs --role=master --path=journal 2>/dev/null | head -5 || true",
            timeout=60)
        if self._lines(r_j.out):
            sec.pass_("Master node journal logs accessible via 'oc adm node-logs'")
        else:
            sec.warn("Master node journal logs not accessible")

        # Container logs
        r_c = await self._lc(sec,
            "oc adm node-logs --role=master --path=containers 2>/dev/null | head -5 || true",
            timeout=60)
        if self._lines(r_c.out):
            sec.pass_("Master node container logs accessible via 'oc adm node-logs'")
        else:
            sec.warn("Master node container logs not accessible")

    # ══════════════════════════════════════════════════════════════════════════
    #  20. MachineConfigPool
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_mcp(self, sec: Section):
        r = await self._lc(sec, "oc get mcp --no-headers")
        lines = self._lines(r.out)
        bad = []
        for l in lines:
            parts = l.split()
            if len(parts) >= 5 and (parts[3] == "True" or parts[4] == "True"):
                bad.append(l.strip())
        sec.pass_(f"All {len(lines)} MCPs healthy") if not bad else \
            sec.fail(f"{len(bad)}/{len(lines)} MCPs degraded/updating", detail="\n".join(bad))

    # ══════════════════════════════════════════════════════════════════════════
    #  21. Node OS & upgrade
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_node_os(self, sec: Section):
        r = await self._lc(sec, "oc get nodes -o jsonpath='{range .items[*]}{.metadata.name} "
                                 "{.status.nodeInfo.osImage} {.status.nodeInfo.kernelVersion}\\n{end}'")
        lines = self._lines(r.out)
        os_versions = {}
        for l in lines:
            parts = l.split(None, 2)
            if len(parts) >= 2:
                os_versions.setdefault(parts[1], []).append(parts[0])
        if len(os_versions) == 1:
            os_ver = list(os_versions.keys())[0]
            sec.pass_(f"All {len(lines)} nodes on same OS: {os_ver}")
        elif os_versions:
            sec.warn(f"Nodes on mixed OS versions — possible upgrade in progress",
                     detail="\n".join(f"{os}: {', '.join(nodes)}" for os, nodes in os_versions.items()))
        r2 = await self._lc(sec, "oc get nodes --no-headers | grep SchedulingDisabled || true")
        if r2.out:
            sec.warn("Node(s) with SchedulingDisabled (cordoned)", detail=r2.out[:300])
        else:
            sec.pass_("No cordoned/unschedulable nodes")

    # ══════════════════════════════════════════════════════════════════════════
    #  22. Resource quotas
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_quotas(self, sec: Section):
        r = await self._lc(sec, "oc get resourcequota -A --no-headers 2>/dev/null | head -20 || true")
        lines = self._lines(r.out)
        if not lines:
            sec.info("No ResourceQuotas defined"); return
        sec.info(f"{len(lines)} ResourceQuota(s) defined")
        r2 = await self._lc(sec, "oc get limitrange -A --no-headers 2>/dev/null | wc -l || true")
        sec.info(f"LimitRanges: {r2.out.strip()}")

    # ══════════════════════════════════════════════════════════════════════════
    #  23. RBAC / SCC
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_rbac(self, sec: Section):
        r = await self._lc(sec, "oc get pods -A -o jsonpath='{range .items[?(@.metadata.annotations.openshift\\.io/scc==\"privileged\")]}{.metadata.namespace}/{.metadata.name}\\n{end}' 2>/dev/null | head -20 || true")
        lines = self._lines(r.out)
        if lines:
            sec.warn(f"{len(lines)} pod(s) running with privileged SCC",
                     detail="\n".join(lines[:15]))
        else:
            sec.info("No pods using privileged SCC (or SCC annotation not set)")
        r2 = await self._lc(sec, "oc get clusterrolebindings -o jsonpath='{range .items[?(@.roleRef.name==\"cluster-admin\")]}{.metadata.name}\\n{end}' 2>/dev/null | wc -l || true")
        cnt = r2.out.strip()
        sec.info(f"cluster-admin ClusterRoleBindings: {cnt}")

    # ══════════════════════════════════════════════════════════════════════════
    #  24. Prometheus alerts
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_alerts(self, sec: Section):
        r = await self.ssh.run(
            "oc get pods -n openshift-monitoring --no-headers 2>/dev/null | grep thanos-query | grep Running | head -1 | awk '{print $1}'", timeout=20)
        thanos = r.out.strip()
        if thanos:
            r2 = await self._lc(sec,
                f"oc exec -n openshift-monitoring {thanos} -c thanos-query -- "
                f"wget -qO- 'http://localhost:10902/api/v1/alerts' 2>/dev/null || true", timeout=30)
            try:
                data = json.loads(r2.stdout)
                all_alerts = data.get("data", {}).get("alerts", [])
                firing = [a for a in all_alerts if a.get("state") == "firing"]
                critical = [a for a in firing if a.get("labels", {}).get("severity") == "critical"]
                warning  = [a for a in firing if a.get("labels", {}).get("severity") == "warning"]
                sec.info(f"Firing alerts: {len(firing)} total ({len(critical)} critical, {len(warning)} warning)")
                if critical:
                    detail = "\n".join(f"{a['labels'].get('alertname','?')} [{a['labels'].get('namespace','cluster')}]" for a in critical[:20])
                    sec.fail(f"{len(critical)} CRITICAL alert(s) firing", detail=detail)
                elif warning:
                    detail = "\n".join(f"{a['labels'].get('alertname','?')} [{a['labels'].get('namespace','cluster')}]" for a in warning[:20])
                    sec.warn(f"{len(warning)} WARNING alert(s) firing", detail=detail)
                else:
                    sec.pass_("No critical/warning alerts firing")
            except (json.JSONDecodeError, KeyError):
                sec.warn("Could not parse Thanos alerts", detail=r2.out[:200])
        else:
            sec.warn("Thanos query pod not found in openshift-monitoring")
        r3 = await self.ssh.run(
            "oc get pods -n openshift-monitoring --no-headers 2>/dev/null | grep alertmanager-main | grep Running | head -1 | awk '{print $1}'", timeout=20)
        am = r3.out.strip()
        if am:
            r4 = await self._lc(sec,
                f"oc exec -n openshift-monitoring {am} -c alertmanager -- "
                f"amtool silence query --alertmanager.url=http://localhost:9093 2>/dev/null | head -10 || true", timeout=20)
            silences = self._lines(r4.out)
            if len(silences) > 1:
                sec.warn(f"{len(silences)-1} active silence(s) in Alertmanager")

    # ══════════════════════════════════════════════════════════════════════════
    #  25. Cluster Logging
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_logging(self, sec: Section):
        for ns in ("openshift-logging", "openshift-operators-redhat"):
            r = await self.ssh.run(f"oc get ns {ns} 2>/dev/null", timeout=10)
            if r.exit_code == 0:
                r2 = await self._lc(sec, f"oc get pods -n {ns} --no-headers 2>/dev/null | head -15")
                lines = self._lines(r2.out)
                bad = [l for l in lines if not re.search(r"Running|Completed|Succeeded", l)]
                (sec.pass_(f"{ns}: {len(lines)} pod(s) healthy") if not bad else
                 sec.fail(f"{ns}: {len(bad)} pods not healthy", detail="\n".join(bad)))
                return
        sec.skip("No logging namespace found (openshift-logging / openshift-operators-redhat)")

    # ══════════════════════════════════════════════════════════════════════════
    #  26. Image registry
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_imageregistry(self, sec: Section):
        r = await self._lc(sec, "oc get co image-registry --no-headers 2>/dev/null || true")
        if not r.out:
            sec.warn("image-registry cluster operator not found"); return
        parts = r.out.split()
        avail = parts[2] if len(parts) > 2 else "?"
        (sec.pass_ if avail == "True" else sec.fail)(f"Image registry Available={avail}", detail=r.out)
        r2 = await self._oc("get configs.imageregistry.operator.openshift.io/cluster "
                             "-o jsonpath='{.spec.managementState}' 2>/dev/null || true")
        if r2.out:
            sec.info(f"Image registry management state: {r2.out.strip()}")

    # ══════════════════════════════════════════════════════════════════════════
    #  27. ETCD Backup
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_etcd_backup(self, sec: Section):
        r = await self._lc(sec, "oc get etcdbackup -A --no-headers --sort-by=.metadata.creationTimestamp 2>/dev/null | tail -5 || true")
        if not r.out:
            sec.warn("No EtcdBackup resources found (backup may not be configured via operator)")
            return
        lines = self._lines(r.out)
        sec.info(f"Recent etcd backups: {len(lines)}", detail="\n".join(lines))
        r2 = await self.ssh.run("oc get etcdbackup -A -o jsonpath='{range .items[*]}{.status.conditions[?(@.type==\"BackupCompleted\")].status} {.metadata.creationTimestamp}\\n{end}' 2>/dev/null || true", timeout=20)
        completed = [l for l in self._lines(r2.out) if "True" in l]
        (sec.pass_(f"etcd backup completed successfully ({len(completed)} recorded)") if completed else
         sec.warn("No completed EtcdBackup found"))