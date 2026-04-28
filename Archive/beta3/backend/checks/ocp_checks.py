"""OCP Health Checks — comprehensive OpenShift diagnostics (27 categories).
Ported from CloudHealth with full production logic.
"""
from __future__ import annotations
import asyncio
import re
import json
from datetime import datetime
from typing import List, Optional, Set

from core.inventory import ClusterConfig, AppSettings
from core.models import SectionResult, Status
from core.ssh import SSHClient


class OCPHealthChecker:
    """Full OCP diagnostics — 27 check categories."""

    def __init__(self, ssh: SSHClient, app: AppSettings,
                 cluster: ClusterConfig, logger=None, console=None,
                 enabled_checks: Optional[Set[str]] = None):
        self.ssh     = ssh
        self.app     = app
        self.cluster = cluster
        self.log     = logger
        self.con     = console
        self.enabled = enabled_checks

    def _should(self, cat: str) -> bool:
        return self.enabled is None or cat in self.enabled

    async def _lc(self, sec: SectionResult, cmd: str, timeout: int = 60):
        """Run command with logging and section log append."""
        r = await self.ssh.run(cmd, timeout=timeout)
        sec.append_log(f"$ {cmd}\n{r.stdout}{r.stderr}\n")
        return r

    def _lines(self, out: str) -> List[str]:
        return [l for l in out.splitlines() if l.strip()]

    # ── section runner ────────────────────────────────────────────────────────

    async def run(self) -> List[SectionResult]:
        """
        Executes all enabled OCP diagnostic checks.
        Iterates through 27 categories and runs the associated methods.
        """
        checks = [
            ("version",       "OCP Version & API",               self._check_version),
            ("operators",     "Cluster Operators",                self._check_operators),
            ("nodes",         "Node Status",                     self._check_nodes),
            ("pressure",      "Resource Pressure",               self._check_pressure),
            ("etcd",          "etcd Health",                     self._check_etcd),
            ("controlplane",  "Control Plane Pods",              self._check_controlplane),
            ("ceph",          "Storage (Ceph/ODF)",              self._check_ceph),
            ("pvcs",          "PVC / PV Status",                 self._check_pvcs),
            ("storageclasses","Storage Classes",                 self._check_storageclasses),
            ("pods",          "Pods & Restarts Audit",           self._check_pods),
            ("deployments",   "Deployments & StatefulSets",      self._check_deployments),
            ("daemonsets",    "DaemonSets",                      self._check_daemonsets),
            ("jobs",          "Failed Jobs & CronJobs",          self._check_jobs),
            ("hpa",           "HPA Capacity",                    self._check_hpa),
            ("network",       "Network / CNI / DNS",             self._check_network),
            ("ingress",       "Ingress & Routes",                self._check_ingress),
            ("events",        "Events Warning Scan",             self._check_events),
            ("certs",         "Certificate Expiry",              self._check_certs),
            ("mcp",           "MachineConfigPools",              self._check_mcp),
            ("nodeupgrade",   "Node OS & Upgrade",               self._check_node_upgrade),
            ("quotas",        "Resource Quotas",                 self._check_quotas),
            ("rbac",          "RBAC / SCC Audit",                self._check_rbac),
            ("alerts",        "Prometheus Alerts",               self._check_alerts),
            ("logging",       "Cluster Logging",                 self._check_logging),
            ("registry",      "Image Registry",                  self._check_registry),
            ("etcdbackup",    "ETCD Backup Freshness",           self._check_etcd_backup),
            ("clusternetwork","Cluster Network Policy",          self._check_cluster_network),
        ]
        sections = []
        for cat, name, fn in checks:
            if not self._should(cat):
                continue
            sec = SectionResult(name, cat, start_time=datetime.now())
            if self.con:
                self.con.section_start(name)
            try:
                await fn(sec)
            except Exception as e:
                sec.error(f"Check raised exception: {e}")
                if self.log:
                    self.log.exception(f"[{self.cluster.name}] {cat} exception")
            sec.end_time = datetime.now()
            if self.con:
                self.con.section_done(sec)
            sections.append(sec)
        return sections

    async def discover_nodes(self) -> List[str]:
        """Auto-discover node InternalIPs using 'oc get nodes'."""
        r = await self.ssh.run(

            "oc get nodes -o jsonpath='{range .items[*]}"
            "{.status.addresses[?(@.type==\"InternalIP\")].address}{\"\\n\"}"
            "{end}' 2>/dev/null", timeout=30)
        return [l.strip() for l in self._lines(r.out) if l.strip()]

    # ══════════════════════════════════════════════════════════════════════════
    #  1. Version & API
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_version(self, sec):
        r = await self._lc(sec, "oc version")
        if r.exit_code != 0:
            sec.fail("Cannot reach API server", command="oc version")
            return
        m = re.search(r"Server Version:\s+(\S+)", r.stdout)
        ver = m.group(1) if m else "Unknown"
        sec.pass_(f"Running OCP {ver}")

        r2 = await self._lc(sec, "oc get clusterversion version -o json 2>/dev/null")
        try:
            cv = json.loads(r2.stdout)
            for cond in cv.get("status", {}).get("conditions", []):
                t, s = cond.get("type"), cond.get("status")
                if t == "Available":
                    (sec.pass_ if s == "True" else sec.fail)(
                        f"ClusterVersion Available: {s}")
                if t == "Degraded":
                    (sec.pass_ if s == "False" else sec.fail)(
                        f"ClusterVersion Degraded: {s}")
                if t == "Progressing" and s == "True":
                    sec.warn(f"Cluster upgrade in progress: {cond.get('message', '')[:80]}")
            channel = cv.get("spec", {}).get("channel", "unknown")
            sec.info(f"Update channel: {channel}")
        except (json.JSONDecodeError, KeyError):
            pass

    # ══════════════════════════════════════════════════════════════════════════
    #  2. Cluster Operators
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_operators(self, sec):
        r = await self._lc(sec, "oc get clusteroperators -o json 2>/dev/null")
        try:
            data = json.loads(r.stdout)
            items = data.get("items", [])
            degraded = []
            unavailable = []
            for op in items:
                name = op["metadata"]["name"]
                conditions = {c["type"]: c["status"]
                              for c in op.get("status", {}).get("conditions", [])}
                if conditions.get("Degraded") == "True":
                    degraded.append(name)
                if conditions.get("Available") == "False":
                    unavailable.append(name)
            if degraded:
                sec.fail(f"{len(degraded)} operator(s) degraded: {', '.join(degraded)}")
            if unavailable:
                sec.fail(f"{len(unavailable)} operator(s) unavailable: {', '.join(unavailable)}")
            if not degraded and not unavailable:
                sec.pass_(f"All {len(items)} cluster operators healthy")
        except (json.JSONDecodeError, KeyError):
            sec.error("Could not parse cluster operators")

    # ══════════════════════════════════════════════════════════════════════════
    #  3. Node Status
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_nodes(self, sec):
        r = await self._lc(sec, "oc get nodes -o json 2>/dev/null")
        try:
            data = json.loads(r.stdout)
            nodes = data.get("items", [])
            ready = []
            not_ready = []
            for n in nodes:
                name = n["metadata"]["name"]
                roles = [k.replace("node-role.kubernetes.io/", "")
                         for k in n.get("metadata", {}).get("labels", {})
                         if k.startswith("node-role.kubernetes.io/")]
                conditions = {c["type"]: c["status"]
                              for c in n.get("status", {}).get("conditions", [])}
                if conditions.get("Ready") == "True":
                    ready.append(name)
                else:
                    not_ready.append(f"{name} ({','.join(roles)})")
            if not_ready:
                sec.fail(f"{len(not_ready)} node(s) NOT Ready: {', '.join(not_ready)}")
            sec.pass_(f"{len(ready)}/{len(nodes)} nodes Ready")
            # Role breakdown
            sec.info(f"Node roles: {len(nodes)} total")
        except (json.JSONDecodeError, KeyError):
            sec.error("Could not parse node data")

    # ══════════════════════════════════════════════════════════════════════════
    #  4. Resource Pressure
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_pressure(self, sec):
        r = await self._lc(sec, "oc get nodes -o json 2>/dev/null")
        pressure_found = []
        try:
            data = json.loads(r.stdout)
            for n in data.get("items", []):
                name = n["metadata"]["name"]
                for cond in n.get("status", {}).get("conditions", []):
                    if (cond["type"] in ("MemoryPressure", "DiskPressure",
                                         "PIDPressure") and
                            cond["status"] == "True"):
                        pressure_found.append(f"{name}/{cond['type']}")
        except (json.JSONDecodeError, KeyError):
            pass
        if pressure_found:
            sec.fail(f"Pressure detected: {', '.join(pressure_found)}")
        else:
            sec.pass_("No resource pressure detected on any node")

    # ══════════════════════════════════════════════════════════════════════════
    #  5. etcd Health
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_etcd(self, sec):
        r = await self._lc(sec,
            "oc get pods -n openshift-etcd -l app=etcd --no-headers")
        running = r.stdout.count("Running")
        total = len([l for l in r.stdout.strip().split('\n') if l.strip()])
        (sec.pass_ if running == total and total > 0 else sec.fail)(
            f"etcd pods: {running}/{total} Running")
        # Endpoint health
        ep = await self._lc(sec,
            "oc exec -n openshift-etcd -c etcd "
            "$(oc get pods -n openshift-etcd -l app=etcd -o name | head -1) "
            "-- etcdctl endpoint health --cluster 2>/dev/null", timeout=30)
        if "unhealthy" in ep.stdout.lower():
            sec.fail("Unhealthy etcd endpoints detected",
                     detail=ep.stdout[:300])
        elif ep.stdout.strip():
            sec.pass_("etcd endpoints healthy")
        # Leader status
        ld = await self._lc(sec,
            "oc exec -n openshift-etcd -c etcd "
            "$(oc get pods -n openshift-etcd -l app=etcd -o name | head -1) "
            "-- etcdctl endpoint status --cluster -w table 2>/dev/null",
            timeout=30)
        if ld.stdout.strip():
            sec.info("etcd cluster status", detail=ld.stdout[:500])

    # ══════════════════════════════════════════════════════════════════════════
    #  6. Control Plane Pods
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_controlplane(self, sec):
        namespaces = [
            "openshift-apiserver", "openshift-kube-apiserver",
            "openshift-authentication", "openshift-kube-controller-manager",
            "openshift-kube-scheduler", "openshift-controller-manager",
        ]
        for ns in namespaces:
            r = await self._lc(sec, f"oc get pods -n {ns} --no-headers")
            bad = [l for l in self._lines(r.stdout)
                   if "Running" not in l and "Completed" not in l]
            (sec.pass_ if not bad else sec.fail)(
                f"{ns}: {'All pods healthy' if not bad else f'{len(bad)} pod(s) unhealthy'}")

    # ══════════════════════════════════════════════════════════════════════════
    #  7. Storage (Ceph/ODF) — deep
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_ceph(self, sec):
        ns_found = None
        for ns in ["openshift-storage", "rook-ceph"]:
            c = await self.ssh.run(f"oc get ns {ns} 2>/dev/null")
            if c.exit_code == 0:
                ns_found = ns
                break
        if not ns_found:
            sec.skip("Ceph/ODF not detected")
            return
        r = await self._lc(sec, f"oc get pods -n {ns_found} --no-headers")
        bad = [l for l in self._lines(r.stdout)
               if "Running" not in l and "Completed" not in l]
        (sec.pass_ if not bad else sec.warn)(f"{ns_found}: storage pods check")
        # Ceph health via toolbox
        tb = await self._lc(sec,
            f"oc exec -n {ns_found} "
            f"$(oc get pods -n {ns_found} -l app=rook-ceph-tools -o name "
            f"2>/dev/null | head -1) -- ceph status 2>/dev/null", timeout=30)
        for keyword, fn in [("HEALTH_OK", sec.pass_),
                            ("HEALTH_WARN", sec.warn),
                            ("HEALTH_ERR", sec.fail)]:
            if keyword in tb.stdout:
                fn(f"Ceph: {keyword}", detail=tb.stdout[:500])
                break
        # OSD info
        osd_m = re.search(r"osd:.*", tb.stdout)
        if osd_m:
            sec.info(f"Ceph OSD: {osd_m.group().strip()}")
        # StorageCluster CR
        sc = await self._lc(sec,
            f"oc get storagecluster -n {ns_found} -o json 2>/dev/null")
        try:
            scr = json.loads(sc.stdout)
            items = scr.get("items", [])
            for item in items:
                phase = item.get("status", {}).get("phase", "Unknown")
                sec.info(f"StorageCluster phase: {phase}")
        except (json.JSONDecodeError, KeyError):
            pass

    # ══════════════════════════════════════════════════════════════════════════
    #  8. PVC / PV Status
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_pvcs(self, sec):
        r = await self._lc(sec,
            "oc get pvc -A -o json 2>/dev/null")
        try:
            data = json.loads(r.stdout)
            items = data.get("items", [])
            pending = [f"{i['metadata']['namespace']}/{i['metadata']['name']}"
                       for i in items if i.get("status", {}).get("phase") == "Pending"]
            lost = [f"{i['metadata']['namespace']}/{i['metadata']['name']}"
                    for i in items if i.get("status", {}).get("phase") == "Lost"]
            if lost:
                sec.fail(f"{len(lost)} PVC(s) Lost", detail="\n".join(lost[:10]))
            if pending:
                sec.warn(f"{len(pending)} PVC(s) Pending", detail="\n".join(pending[:10]))
            if not pending and not lost:
                sec.pass_(f"All {len(items)} PVCs Bound")
        except (json.JSONDecodeError, KeyError):
            sec.warn("Could not parse PVC data")

    # ══════════════════════════════════════════════════════════════════════════
    #  9. Storage Classes
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_storageclasses(self, sec):
        r = await self._lc(sec, "oc get storageclass --no-headers 2>/dev/null")
        lines = self._lines(r.stdout)
        default = [l for l in lines if "(default)" in l]
        sec.info(f"{len(lines)} storage class(es), "
                 f"{len(default)} default")
        if not lines:
            sec.warn("No storage classes defined")
        elif not default:
            sec.warn("No default storage class set")
        else:
            sec.pass_(f"Default storage class: {default[0].split()[0]}")

    # ══════════════════════════════════════════════════════════════════════════
    #  10. Pods & Restarts Audit
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_pods(self, sec):
        r = await self._lc(sec, "oc get pods -A --no-headers 2>/dev/null")
        lines = self._lines(r.stdout)
        restarts_fail = []
        status_fail = []
        for l in lines:
            parts = l.split()
            if len(parts) < 5:
                continue
            ns, pod, status, restarts = parts[0], parts[1], parts[3], parts[4]
            try:
                rc = int(restarts)
                if rc > 50:
                    restarts_fail.append(f"{ns}/{pod}({rc})")
            except ValueError:
                pass
            if status not in ("Running", "Completed", "Succeeded"):
                status_fail.append(f"{ns}/{pod}({status})")
        (sec.pass_ if not status_fail else sec.fail)(
            f"{len(status_fail)} pods in bad state" if status_fail
            else "All pods Running/Completed",
            detail="\n".join(status_fail[:20]))
        (sec.pass_ if not restarts_fail else sec.warn)(
            f"{len(restarts_fail)} pods with high restarts" if restarts_fail
            else "No pods with high restarts",
            detail="\n".join(restarts_fail[:20]))

    # ══════════════════════════════════════════════════════════════════════════
    #  11. Deployments & StatefulSets
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_deployments(self, sec):
        r = await self._lc(sec,
            "oc get deployments -A -o json 2>/dev/null")
        try:
            data = json.loads(r.stdout)
            items = data.get("items", [])
            unhealthy = []
            for d in items:
                name = f"{d['metadata']['namespace']}/{d['metadata']['name']}"
                desired = d.get("spec", {}).get("replicas", 0)
                ready = d.get("status", {}).get("readyReplicas", 0)
                if ready < desired:
                    unhealthy.append(f"{name} ({ready}/{desired})")
            if unhealthy:
                sec.fail(f"{len(unhealthy)} deployment(s) under-replicated",
                         detail="\n".join(unhealthy[:15]))
            else:
                sec.pass_(f"All {len(items)} deployments at desired replicas")
        except (json.JSONDecodeError, KeyError):
            sec.warn("Could not parse deployment data")
        # StatefulSets
        r2 = await self._lc(sec,
            "oc get statefulsets -A -o json 2>/dev/null")
        try:
            data2 = json.loads(r2.stdout)
            items2 = data2.get("items", [])
            unhealthy2 = []
            for s in items2:
                name = f"{s['metadata']['namespace']}/{s['metadata']['name']}"
                desired = s.get("spec", {}).get("replicas", 0)
                ready = s.get("status", {}).get("readyReplicas", 0)
                if ready < desired:
                    unhealthy2.append(f"{name} ({ready}/{desired})")
            if unhealthy2:
                sec.fail(f"{len(unhealthy2)} statefulset(s) under-replicated",
                         detail="\n".join(unhealthy2[:15]))
            else:
                sec.pass_(f"All {len(items2)} statefulsets healthy")
        except (json.JSONDecodeError, KeyError):
            pass

    # ══════════════════════════════════════════════════════════════════════════
    #  12. DaemonSets
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_daemonsets(self, sec):
        r = await self._lc(sec, "oc get daemonsets -A -o json 2>/dev/null")
        try:
            data = json.loads(r.stdout)
            items = data.get("items", [])
            mismatched = []
            for ds in items:
                name = f"{ds['metadata']['namespace']}/{ds['metadata']['name']}"
                desired = ds.get("status", {}).get("desiredNumberScheduled", 0)
                ready = ds.get("status", {}).get("numberReady", 0)
                if ready < desired:
                    mismatched.append(f"{name} ({ready}/{desired})")
            if mismatched:
                sec.fail(f"{len(mismatched)} daemonset(s) not fully ready",
                         detail="\n".join(mismatched[:15]))
            else:
                sec.pass_(f"All {len(items)} daemonsets at full capacity")
        except (json.JSONDecodeError, KeyError):
            sec.warn("Could not parse daemonset data")

    # ══════════════════════════════════════════════════════════════════════════
    #  13. Failed Jobs & CronJobs
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_jobs(self, sec):
        r = await self._lc(sec,
            "oc get jobs -A --no-headers 2>/dev/null | "
            "awk '$3==0 && $4>0{print $1\"/\"$2}' | head -20")
        failed = self._lines(r.stdout)
        (sec.pass_ if not failed else sec.warn)(
            f"{len(failed)} failed job(s)" if failed else "No failed jobs",
            detail="\n".join(failed[:15]))
        # CronJob check
        r2 = await self._lc(sec,
            "oc get cronjobs -A --no-headers 2>/dev/null | wc -l")
        try:
            cnt = int(r2.out.strip())
            sec.info(f"{cnt} CronJob(s) configured")
        except ValueError:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    #  14. HPA Capacity
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_hpa(self, sec):
        r = await self._lc(sec, "oc get hpa -A -o json 2>/dev/null")
        try:
            data = json.loads(r.stdout)
            items = data.get("items", [])
            maxed_out = []
            for h in items:
                name = f"{h['metadata']['namespace']}/{h['metadata']['name']}"
                curr = h.get("status", {}).get("currentReplicas", 0)
                mx = h.get("spec", {}).get("maxReplicas", 0)
                if curr >= mx and mx > 0:
                    maxed_out.append(f"{name} ({curr}/{mx})")
            if maxed_out:
                sec.warn(f"{len(maxed_out)} HPA(s) at max capacity",
                         detail="\n".join(maxed_out[:10]))
            else:
                sec.pass_(f"All {len(items)} HPAs within capacity")
        except (json.JSONDecodeError, KeyError):
            sec.info("No HPAs or could not parse HPA data")

    # ══════════════════════════════════════════════════════════════════════════
    #  15. Network / CNI / DNS
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_network(self, sec):
        # SDN/OVN operator
        r = await self._lc(sec,
            "oc get network.config cluster -o jsonpath='{.status.networkType}' "
            "2>/dev/null")
        sec.info(f"CNI plugin: {r.out.strip() or 'unknown'}")
        # DNS pods
        r2 = await self._lc(sec,
            "oc get pods -n openshift-dns --no-headers 2>/dev/null")
        bad = [l for l in self._lines(r2.stdout)
               if "Running" not in l]
        (sec.pass_ if not bad else sec.fail)(
            f"DNS: {'All pods healthy' if not bad else f'{len(bad)} pod(s) unhealthy'}")
        # CoreDNS test
        r3 = await self._lc(sec,
            "oc exec -n openshift-dns "
            "$(oc get pods -n openshift-dns -o name | head -1) "
            "-- nslookup kubernetes.default.svc.cluster.local 2>/dev/null",
            timeout=15)
        if "Address" in r3.stdout:
            sec.pass_("CoreDNS resolution working")
        elif r3.stdout.strip():
            sec.warn("CoreDNS resolution may have issues",
                     detail=r3.stdout[:200])

    # ══════════════════════════════════════════════════════════════════════════
    #  16. Ingress & Routes
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_ingress(self, sec):
        r = await self._lc(sec,
            "oc get ingresscontroller -n openshift-ingress-operator "
            "-o json 2>/dev/null")
        try:
            data = json.loads(r.stdout)
            for ic in data.get("items", []):
                name = ic["metadata"]["name"]
                conditions = {c["type"]: c["status"]
                              for c in ic.get("status", {}).get("conditions", [])}
                avail = conditions.get("Available", "Unknown")
                degrad = conditions.get("Degraded", "Unknown")
                if avail == "True" and degrad == "False":
                    sec.pass_(f"IngressController '{name}': healthy")
                else:
                    sec.fail(f"IngressController '{name}': "
                             f"Available={avail}, Degraded={degrad}")
        except (json.JSONDecodeError, KeyError):
            sec.warn("Could not parse IngressController status")
        # Route count
        r2 = await self._lc(sec,
            "oc get routes -A --no-headers 2>/dev/null | wc -l")
        try:
            sec.info(f"Total routes: {int(r2.out.strip())}")
        except ValueError:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    #  17. Events Warning Scan
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_events(self, sec):
        r = await self._lc(sec,
            "oc get events -A --field-selector type=Warning "
            "--no-headers 2>/dev/null | tail -30")
        lines = self._lines(r.stdout)
        (sec.pass_ if not lines else sec.warn)(
            f"Found {len(lines)} warning events",
            detail="\n".join(lines[:20]))

    # ══════════════════════════════════════════════════════════════════════════
    #  18. Certificate Expiry (REAL — not a stub)
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_certs(self, sec):
        r = await self._lc(sec,
            "oc get secrets -A -o json 2>/dev/null | "
            "python3 -c \""
            "import json,sys,base64,subprocess,datetime\n"
            "data=json.load(sys.stdin)\n"
            "now=datetime.datetime.utcnow()\n"
            "for s in data.get('items',[]):\n"
            "  if s.get('type')!='kubernetes.io/tls': continue\n"
            "  ns=s['metadata']['namespace']; nm=s['metadata']['name']\n"
            "  cert_data=s.get('data',{}).get('tls.crt','')\n"
            "  if not cert_data: continue\n"
            "  try:\n"
            "    raw=base64.b64decode(cert_data)\n"
            "    p=subprocess.run(['openssl','x509','-noout','-enddate'],\n"
            "      input=raw,capture_output=True)\n"
            "    line=p.stdout.decode().strip()\n"
            "    exp=datetime.datetime.strptime(line.split('=',1)[1],'%b %d %H:%M:%S %Y GMT')\n"
            "    days=(exp-now).days\n"
            "    if days<30: print(f'{ns}/{nm}: expires in {days} days')\n"
            "  except: pass\n"
            "\" 2>/dev/null | head -20", timeout=120)
        lines = self._lines(r.stdout)
        if lines:
            critical = [l for l in lines if "expires in -" in l or
                        any(f"expires in {d} days" in l
                            for d in range(-999, 1))]
            warning = [l for l in lines if l not in critical]
            if critical:
                sec.fail(f"{len(critical)} certificate(s) EXPIRED",
                         detail="\n".join(critical[:10]))
            if warning:
                sec.warn(f"{len(warning)} certificate(s) expiring within 30 days",
                         detail="\n".join(warning[:10]))
        else:
            sec.pass_("No certificates expiring within 30 days")

    # ══════════════════════════════════════════════════════════════════════════
    #  19. MachineConfigPools
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_mcp(self, sec):
        r = await self._lc(sec, "oc get mcp -o json 2>/dev/null")
        try:
            data = json.loads(r.stdout)
            for pool in data.get("items", []):
                name = pool["metadata"]["name"]
                conditions = {c["type"]: c["status"]
                              for c in pool.get("status", {}).get("conditions", [])}
                degraded = conditions.get("Degraded", "False")
                updating = conditions.get("Updating", "False")
                if degraded == "True":
                    sec.fail(f"MCP '{name}': Degraded")
                elif updating == "True":
                    sec.warn(f"MCP '{name}': Updating in progress")
                else:
                    sec.pass_(f"MCP '{name}': healthy")
        except (json.JSONDecodeError, KeyError):
            sec.warn("Could not parse MCP data")

    # ══════════════════════════════════════════════════════════════════════════
    #  20. Node OS & Upgrade
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_node_upgrade(self, sec):
        r = await self._lc(sec,
            "oc get nodes -o json 2>/dev/null")
        try:
            data = json.loads(r.stdout)
            versions = set()
            for n in data.get("items", []):
                v = n.get("status", {}).get("nodeInfo", {}).get(
                    "kubeletVersion", "unknown")
                versions.add(v)
            if len(versions) > 1:
                sec.warn(f"Mixed kubelet versions detected: {', '.join(versions)}")
            else:
                sec.pass_(f"All nodes on kubelet {', '.join(versions)}")
        except (json.JSONDecodeError, KeyError):
            sec.warn("Could not parse node version data")

    # ══════════════════════════════════════════════════════════════════════════
    #  21. Resource Quotas
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_quotas(self, sec):
        r = await self._lc(sec,
            "oc get resourcequotas -A --no-headers 2>/dev/null | head -20")
        lines = self._lines(r.stdout)
        sec.info(f"{len(lines)} resource quota(s) defined")
        if not lines:
            sec.info("No resource quotas configured")

    # ══════════════════════════════════════════════════════════════════════════
    #  22. RBAC / SCC Audit
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_rbac(self, sec):
        r = await self._lc(sec,
            "oc get clusterrolebindings -o json 2>/dev/null | "
            "python3 -c \""
            "import json,sys\n"
            "data=json.load(sys.stdin)\n"
            "for b in data.get('items',[]):\n"
            "  for s in b.get('subjects',[]):\n"
            "    if s.get('kind')=='Group' and s.get('name')=='system:authenticated':\n"
            "      role=b.get('roleRef',{}).get('name','')\n"
            "      if 'cluster-admin' in role or 'edit' in role:\n"
            "        print(f\\\"Wide binding: {b['metadata']['name']} -> {role}\\\")\n"
            "\" 2>/dev/null | head -10")
        lines = self._lines(r.stdout)
        if lines:
            sec.warn(f"{len(lines)} overly-wide RBAC binding(s)",
                     detail="\n".join(lines[:10]))
        else:
            sec.pass_("No overly-wide RBAC bindings detected")
        # SCC audit
        r2 = await self._lc(sec,
            "oc get scc --no-headers 2>/dev/null | wc -l")
        try:
            sec.info(f"Security Context Constraints: {int(r2.out.strip())}")
        except ValueError:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    #  23. Prometheus Alerts
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_alerts(self, sec):
        r = await self._lc(sec,
            "oc exec -n openshift-monitoring "
            "$(oc get pods -n openshift-monitoring -l app.kubernetes.io/name=prometheus "
            "-o name 2>/dev/null | head -1) -c prometheus "
            "-- wget -qO- http://localhost:9090/api/v1/alerts 2>/dev/null",
            timeout=30)
        try:
            data = json.loads(r.stdout)
            alerts = data.get("data", {}).get("alerts", [])
            firing = [a for a in alerts if a.get("state") == "firing"]
            critical = [a for a in firing
                        if a.get("labels", {}).get("severity") == "critical"]
            warning = [a for a in firing
                       if a.get("labels", {}).get("severity") == "warning"]
            if critical:
                sec.fail(f"{len(critical)} critical alert(s) firing",
                         detail="\n".join(
                             a["labels"].get("alertname", "?")
                             for a in critical[:15]))
            if warning:
                sec.warn(f"{len(warning)} warning alert(s) firing",
                         detail="\n".join(
                             a["labels"].get("alertname", "?")
                             for a in warning[:15]))
            if not critical and not warning:
                sec.pass_(f"No critical/warning alerts firing "
                          f"({len(alerts)} total alerts)")
        except (json.JSONDecodeError, KeyError):
            sec.warn("Could not query Prometheus alerts")
        # Alertmanager silences
        r2 = await self._lc(sec,
            "oc exec -n openshift-monitoring "
            "$(oc get pods -n openshift-monitoring "
            "-l app.kubernetes.io/name=alertmanager -o name 2>/dev/null | head -1) "
            "-c alertmanager -- wget -qO- "
            "http://localhost:9093/api/v2/silences 2>/dev/null", timeout=20)
        try:
            silences = json.loads(r2.stdout)
            active = [s for s in silences if s.get("status", {}).get("state") == "active"]
            if active:
                sec.info(f"{len(active)} active Alertmanager silence(s)")
        except (json.JSONDecodeError, KeyError):
            pass

    # ══════════════════════════════════════════════════════════════════════════
    #  24. Cluster Logging
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_logging(self, sec):
        r = await self._lc(sec,
            "oc get pods -n openshift-logging --no-headers 2>/dev/null")
        if r.exit_code != 0 or not r.stdout.strip():
            sec.skip("Cluster logging not deployed")
            return
        bad = [l for l in self._lines(r.stdout)
               if "Running" not in l and "Completed" not in l]
        (sec.pass_ if not bad else sec.warn)(
            f"Logging: {'All pods healthy' if not bad else f'{len(bad)} pod(s) unhealthy'}")

    # ══════════════════════════════════════════════════════════════════════════
    #  25. Image Registry
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_registry(self, sec):
        r = await self._lc(sec,
            "oc get configs.imageregistry.operator.openshift.io cluster "
            "-o json 2>/dev/null")
        try:
            data = json.loads(r.stdout)
            mgmt = data.get("spec", {}).get("managementState", "Unknown")
            storage = data.get("spec", {}).get("storage", {})
            storage_type = list(storage.keys())[0] if storage else "none"
            sec.info(f"Image Registry: {mgmt}, storage: {storage_type}")
            if mgmt == "Removed":
                sec.warn("Image registry is Removed")
            elif mgmt == "Managed":
                sec.pass_("Image registry is Managed")
        except (json.JSONDecodeError, KeyError, IndexError):
            sec.warn("Could not parse image registry config")

    # ══════════════════════════════════════════════════════════════════════════
    #  26. ETCD Backup Freshness
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_etcd_backup(self, sec):
        r = await self._lc(sec,
            "oc get etcdbackups -A --no-headers 2>/dev/null || "
            "oc get cronjobs -n openshift-etcd --no-headers 2>/dev/null || "
            "echo 'none'")
        if "none" in r.stdout or not r.stdout.strip():
            sec.warn("No etcd backup CRs or CronJobs found")
        else:
            sec.info("etcd backup configuration detected",
                     detail=r.stdout[:300])
            sec.pass_("etcd backup mechanism in place")

    # ══════════════════════════════════════════════════════════════════════════
    #  27. Cluster Network Policy
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_cluster_network(self, sec):
        r = await self._lc(sec,
            "oc get networkpolicies -A --no-headers 2>/dev/null | wc -l")
        try:
            cnt = int(r.out.strip())
            sec.info(f"NetworkPolicies: {cnt} defined")
            if cnt == 0:
                sec.warn("No NetworkPolicies defined — all traffic allowed")
            else:
                sec.pass_(f"{cnt} NetworkPolicy rules in place")
        except ValueError:
            sec.info("Could not query NetworkPolicies")
