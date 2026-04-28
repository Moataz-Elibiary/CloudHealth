"""CVIM Health Checks — comprehensive OpenStack/VIM diagnostics (19 categories).
Ported from CloudHealth with full production logic.
"""
from __future__ import annotations
import asyncio
import re
from datetime import datetime
from typing import List, Optional, Set

from core.inventory import ClusterConfig, AppSettings
from core.models import SectionResult, Status
from core.ssh import SSHClient


class CVIMHealthChecker:
    """Full CVIM diagnostics — 19 check categories."""

    def __init__(self, ssh: SSHClient, app: AppSettings,
                 cluster: ClusterConfig, logger=None, console=None,
                 enabled_checks: Optional[Set[str]] = None):
        self.ssh     = ssh
        self.app     = app
        self.cluster = cluster
        self.log     = logger
        self.con     = console
        self.enabled = enabled_checks

    def _os(self, cmd: str) -> str:
        return f"source /root/openstack-configs/openrc 2>/dev/null; {cmd}"

    def _should(self, cat: str) -> bool:
        return self.enabled is None or cat in self.enabled

    async def _lc(self, sec: SectionResult, cmd: str, timeout: int = 60):
        r = await self.ssh.run(cmd, timeout=timeout)
        sec.append_log(f"$ {cmd}\n{r.stdout}{r.stderr}\n")
        return r

    def _lines(self, out: str) -> List[str]:
        return [l for l in out.splitlines() if l.strip()]

    # ── section runner ────────────────────────────────────────────────────────

    async def run(self) -> List[SectionResult]:
        checks = [
            ("hypervisors",  "Hypervisor Status",              self._check_hypervisors),
            ("network",      "Network Agents",                 self._check_network_agents),
            ("volumes",      "Volume Services (Cinder/Ceph)",  self._check_volume_services),
            ("compute_svc",  "Compute Services (Nova)",        self._check_compute_services),
            ("identity",     "Identity Services (Keystone)",   self._check_identity),
            ("image_svc",    "Image Service (Glance)",         self._check_image_service),
            ("cloudpulse",   "Cloudpulse Health",              self._check_cloudpulse),
            ("vms",          "VM (Nova) Status",               self._check_vms),
            ("vm_errors",    "VM Error Audit",                 self._check_vm_errors),
            ("rabbitmq",     "RabbitMQ Health",                self._check_rabbitmq),
            ("mariadb",      "MariaDB / Galera Cluster",       self._check_mariadb),
            ("memcached",    "Memcached Status",               self._check_memcached),
            ("containers",   "Container Status on Nodes",      self._check_containers),
            ("ceph",         "Ceph Storage Status",            self._check_ceph),
            ("ceph_pools",   "Ceph Pool Health",               self._check_ceph_pools),
            ("ovs",          "OVS / Networking Status",        self._check_ovs),
            ("haproxy",      "HAProxy / VIP Status",           self._check_haproxy),
            ("nfs",          "NFS / External Storage",         self._check_nfs),
            ("installer",    "CVIM Installer Status",          self._check_installer),
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
        """Auto-discover node IPs from CVIM installer."""
        r = await self.ssh.run(
            "ciscovim list-nodes 2>/dev/null | awk '{print $2}' | "
            "grep -v Name | grep -v '^$'")
        return [l.strip() for l in self._lines(r.out) if l.strip()]

    # ══════════════════════════════════════════════════════════════════════════
    #  1. Hypervisors
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_hypervisors(self, sec):
        r_cfg = await self._lc(sec,
            "ciscovim list-nodes 2>/dev/null | grep -c compute || echo 0")
        r_up = await self._lc(sec, self._os(
            "openstack hypervisor list -f value -c 'State' -c 'Status' "
            "2>/dev/null | grep -c 'up enabled' || echo 0"))
        r_all = await self._lc(sec, self._os(
            "openstack hypervisor list -f value "
            "-c 'Hypervisor Hostname' -c 'State' -c 'Status' "
            "-c 'vCPUs' -c 'Memory MB Used' 2>/dev/null"))
        try:
            cfg = int(r_cfg.out.strip())
            up = int(r_up.out.strip())
        except ValueError:
            sec.error("Could not parse hypervisor counts")
            return
        (sec.pass_ if up >= cfg else sec.fail)(f"Hypervisors UP: {up}/{cfg}")
        lines = self._lines(r_all.out)
        down = [l for l in lines
                if "down" in l.lower() or "disabled" in l.lower()]
        if down:
            sec.fail(f"{len(down)} hypervisor(s) down/disabled",
                     detail="\n".join(down))
        if lines:
            sec.info("Hypervisor list", detail="\n".join(lines[:30]))
        # Stats
        r_stats = await self._lc(sec, self._os(
            "openstack hypervisor stats show -f value "
            "-c 'vcpus' -c 'vcpus_used' -c 'memory_mb' -c 'memory_mb_used' "
            "2>/dev/null | paste - - - -"))
        if r_stats.out:
            sec.info(f"Cluster resources: {r_stats.out.strip()}")

    # ══════════════════════════════════════════════════════════════════════════
    #  2. Network Agents
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_network_agents(self, sec):
        r_cfg = await self.ssh.run(
            "ciscovim list-nodes 2>/dev/null | grep -c compute || echo 0")
        try:
            hv = int(r_cfg.out.strip())
        except ValueError:
            hv = 0
        required = hv * 2 + 12
        r = await self._lc(sec, self._os(
            "openstack network agent list -f value "
            "-c 'Agent Type' -c 'Host' -c 'Alive' -c 'State' 2>/dev/null"))
        lines = self._lines(r.out)
        alive = [l for l in lines
                 if ":-)  UP" in l or "True  UP" in l or "True UP" in l]
        dead = [l for l in lines if "XXX" in l or "False" in l]
        (sec.pass_ if len(alive) >= required else sec.fail)(
            f"Network agents alive: {len(alive)}/{required} required")
        if dead:
            sec.fail(f"{len(dead)} agent(s) dead/down",
                     detail="\n".join(dead[:15]))

    # ══════════════════════════════════════════════════════════════════════════
    #  3. Volume Services (Cinder)
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_volume_services(self, sec):
        r = await self._lc(sec, self._os(
            "openstack volume service list -f value "
            "-c 'Binary' -c 'Host' -c 'Status' -c 'State' 2>/dev/null"))
        lines = self._lines(r.out)
        up = [l for l in lines
              if "up" in l.lower() and "enabled" in l.lower()]
        down = [l for l in lines if "down" in l.lower()]
        disabled = [l for l in lines
                    if "disabled" in l.lower() and "up" in l.lower()]
        (sec.pass_ if len(up) >= 4 else sec.fail)(
            f"Volume services UP+enabled: {len(up)}/4 minimum")
        if down:
            sec.fail(f"{len(down)} volume service(s) DOWN",
                     detail="\n".join(down))
        if disabled:
            sec.warn(f"{len(disabled)} service(s) disabled",
                     detail="\n".join(disabled[:10]))
        # Volume usage
        r2 = await self._lc(sec, self._os(
            "openstack volume list --all-projects -f value -c 'Status' "
            "2>/dev/null | sort | uniq -c | sort -rn | head -10"))
        if r2.out:
            sec.info("Volume status summary", detail=r2.out[:400])

    # ══════════════════════════════════════════════════════════════════════════
    #  4. Nova Compute Services
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_compute_services(self, sec):
        r = await self._lc(sec, self._os(
            "openstack compute service list -f value "
            "-c 'Binary' -c 'Host' -c 'Status' -c 'State' 2>/dev/null"))
        lines = self._lines(r.out)
        up = [l for l in lines
              if "enabled" in l.lower() and "up" in l.lower()]
        down = [l for l in lines if "down" in l.lower()]
        disabled = [l for l in lines if "disabled" in l.lower()]
        (sec.pass_ if not down else sec.fail)(
            f"Compute services: {len(up)} up, {len(down)} down, "
            f"{len(disabled)} disabled")
        if down:
            sec.fail(f"{len(down)} compute service(s) down",
                     detail="\n".join(down[:15]))

    # ══════════════════════════════════════════════════════════════════════════
    #  5. Keystone Identity
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_identity(self, sec):
        r = await self._lc(sec, self._os(
            "openstack token issue -f value -c 'id' 2>/dev/null | "
            "head -c 20 || echo FAIL"))
        if "FAIL" in r.out or r.exit_code != 0:
            sec.fail("Keystone token issue failed",
                     detail=r.stderr[:300])
        else:
            sec.pass_("Keystone: token issue successful")
        # Disabled endpoints
        r2 = await self._lc(sec, self._os(
            "openstack endpoint list -f value "
            "-c 'Service Name' -c 'Interface' -c 'Enabled' 2>/dev/null | "
            "awk '$3==\"False\"' | head -10 || true"))
        disabled = self._lines(r2.out)
        if disabled:
            sec.warn(f"{len(disabled)} disabled endpoint(s)",
                     detail="\n".join(disabled))
        else:
            sec.pass_("All service endpoints enabled")

    # ══════════════════════════════════════════════════════════════════════════
    #  6. Glance Image Service
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_image_service(self, sec):
        r = await self._lc(sec, self._os(
            "openstack image list -f value -c 'Status' 2>/dev/null | "
            "sort | uniq -c | sort -rn | head -5"))
        if r.exit_code != 0:
            sec.fail("Cannot reach image service (Glance)",
                     detail=r.stderr[:300])
            return
        lines = self._lines(r.out)
        if not lines:
            sec.warn("No images found in Glance")
        else:
            total = sum(int(l.split()[0]) for l in lines
                        if l.split()[0].isdigit())
            active = next((int(l.split()[0]) for l in lines
                           if "active" in l.lower()), 0)
            (sec.pass_ if active > 0 else sec.warn)(
                f"Glance: {total} images ({active} active)")

    # ══════════════════════════════════════════════════════════════════════════
    #  7. Cloudpulse Health
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_cloudpulse(self, sec):
        r = await self._lc(sec,
            "cloudpulse result 2>/dev/null || true", timeout=90)
        if not r.out.strip():
            sec.warn("Cloudpulse returned no output (may not be configured)")
            return
        failed = [l for l in self._lines(r.out)
                  if l.strip() and not re.search(
                      r"success|running|testtype|\+|\-\-", l, re.I)]
        (sec.pass_("Cloudpulse: all tests success/running") if not failed
         else sec.fail(f"Cloudpulse: {len(failed)} failed item(s)",
                       detail="\n".join(failed[:20])))

    # ══════════════════════════════════════════════════════════════════════════
    #  8. VM (Nova) Status
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_vms(self, sec):
        r = await self._lc(sec, self._os(
            "openstack server list --all-projects -f value -c 'Status' "
            "2>/dev/null | sort | uniq -c | sort -rn"), timeout=120)
        lines = self._lines(r.out)
        if not lines:
            sec.warn("No VMs found or cannot query Nova")
            return
        total = sum(int(l.split()[0]) for l in lines if l.split()[0].isdigit())
        active = next((int(l.split()[0]) for l in lines
                       if "ACTIVE" in l), 0)
        error = next((int(l.split()[0]) for l in lines
                      if "ERROR" in l), 0)
        shutoff = next((int(l.split()[0]) for l in lines
                        if "SHUTOFF" in l), 0)
        sec.info(f"VM inventory: {total} total — {active} ACTIVE, "
                 f"{shutoff} SHUTOFF, {error} ERROR")
        if error > 0:
            sec.fail(f"{error} VM(s) in ERROR state")
        else:
            sec.pass_(f"All {active} active VMs running")

    # ══════════════════════════════════════════════════════════════════════════
    #  9. VM Error Audit
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_vm_errors(self, sec):
        r = await self._lc(sec, self._os(
            "openstack server list --all-projects --status ERROR -f value "
            "-c 'Name' -c 'ID' -c 'Host' -c 'Status' 2>/dev/null | "
            "head -25"), timeout=120)
        lines = self._lines(r.out)
        if not lines:
            sec.pass_("No VMs in ERROR state")
        else:
            sec.fail(f"{len(lines)} VM(s) in ERROR state",
                     detail="\n".join(lines))
        # Task states
        r2 = await self._lc(sec, self._os(
            "openstack server list --all-projects -f value "
            "-c 'Name' -c 'Status' -c 'Task State' 2>/dev/null | "
            "grep -v ' None$' | grep -v 'ACTIVE  None' | head -15 "
            "|| true"), timeout=60)
        tasks = self._lines(r2.out)
        if tasks:
            sec.info(f"{len(tasks)} VM(s) with active task states",
                     detail="\n".join(tasks[:10]))

    # ══════════════════════════════════════════════════════════════════════════
    #  10. RabbitMQ (deep)
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_rabbitmq(self, sec):
        # Try rabbit_api.py first
        r0 = await self._lc(sec,
            "ls /root/installer-*/tools/rabbit_api.py 2>/dev/null | head -1")
        script = r0.out.strip()
        if script:
            r = await self._lc(sec,
                f"python3 {script} 2>/dev/null | grep CHECK | "
                f"grep -v '^7.'", timeout=60)
            lines = self._lines(r.out)
            passed = [l for l in lines if "PASSED" in l]
            failed = [l for l in lines if "PASSED" not in l]
            if failed:
                sec.fail(
                    f"RabbitMQ: {len(passed)} passed, {len(failed)} FAILED",
                    detail="\n".join(failed))
            else:
                sec.pass_(
                    f"RabbitMQ: all {len(passed)} functional checks passed")
            return
        # Fallback: rabbitmqctl
        r2 = await self._lc(sec,
            "docker exec rabbitmq rabbitmqctl cluster_status 2>/dev/null || "
            "podman exec rabbitmq rabbitmqctl cluster_status 2>/dev/null || "
            "true", timeout=30)
        if not r2.out:
            sec.warn("RabbitMQ status inaccessible")
            return
        if "running_nodes" in r2.out:
            nodes_m = re.findall(r"rabbit@\S+", r2.out)
            partitions = ("partitions" in r2.out and
                          "[]" not in r2.out.split("partitions")[1][:50])
            if partitions:
                sec.fail("RabbitMQ network partition detected!",
                         detail=r2.out[:500])
            else:
                sec.pass_(
                    f"RabbitMQ cluster: {len(nodes_m)} node(s), no partitions")
        else:
            sec.warn("Could not parse RabbitMQ cluster status",
                     detail=r2.out[:300])

    # ══════════════════════════════════════════════════════════════════════════
    #  11. MariaDB / Galera (deep)
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_mariadb(self, sec):
        r = await self._lc(sec,
            "docker exec mariadb mysql -u root "
            "-e 'SHOW STATUS LIKE \"wsrep%\";' 2>/dev/null || "
            "podman exec mariadb mysql -u root "
            "-e 'SHOW STATUS LIKE \"wsrep%\";' 2>/dev/null || true",
            timeout=30)
        if not r.out:
            sec.warn("MariaDB/Galera status inaccessible")
            return
        cluster_size = re.search(r"wsrep_cluster_size\s+(\d+)", r.out)
        ready = re.search(r"wsrep_ready\s+(\w+)", r.out)
        state = re.search(r"wsrep_local_state_comment\s+(\w+)", r.out)
        connected = re.search(r"wsrep_connected\s+(\w+)", r.out)
        if cluster_size:
            cnt = int(cluster_size.group(1))
            (sec.pass_ if cnt >= 3 else sec.fail)(
                f"Galera cluster size: {cnt}")
        if ready:
            (sec.pass_ if ready.group(1) == "ON" else sec.fail)(
                f"Galera wsrep_ready: {ready.group(1)}")
        if state:
            (sec.pass_ if state.group(1) == "Synced" else sec.warn)(
                f"Galera state: {state.group(1)}")

    # ══════════════════════════════════════════════════════════════════════════
    #  12. Memcached Status
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_memcached(self, sec):
        r = await self._lc(sec,
            "echo 'stats' | nc -w 2 localhost 11211 2>/dev/null | "
            "grep -E 'uptime|curr_connections|version' | head -5 || "
            "docker exec memcached sh -c "
            "'echo stats | nc -w 2 localhost 11211' 2>/dev/null | head -5 || "
            "true", timeout=15)
        if r.out and "uptime" in r.out:
            uptime_m = re.search(r"uptime\s+(\d+)", r.out)
            if uptime_m:
                uptime_h = int(uptime_m.group(1)) // 3600
                (sec.pass_ if uptime_h > 0 else sec.warn)(
                    f"Memcached running, uptime: {uptime_h}h",
                    detail=r.out[:200])
        else:
            sec.warn("Memcached status check inconclusive")

    # ══════════════════════════════════════════════════════════════════════════
    #  13. Container Status per Node Type
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_containers(self, sec):
        for node_type in ("control", "compute", "storage"):
            r_nodes = await self.ssh.run(
                f"ciscovim list-nodes 2>/dev/null | grep {node_type} | "
                f"awk '{{print $2}}'")
            nodes = [l.strip() for l in self._lines(r_nodes.out)]
            if not nodes:
                sec.skip(f"No {node_type} nodes found via VIM")
                continue
            for node in nodes:
                cmd = (
                    f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "
                    f"-o BatchMode=yes {node} "
                    f"'docker ps -a 2>/dev/null | grep -c Up || echo 0'")
                r = await self._lc(sec, cmd, timeout=30)
                try:
                    count = int(r.out.strip())
                    (sec.pass_ if count > 0 else sec.warn)(
                        f"{node} ({node_type}): {count} containers running")
                except ValueError:
                    sec.warn(f"{node}: could not retrieve container info",
                             detail=r.out[:100])

    # ══════════════════════════════════════════════════════════════════════════
    #  14. Ceph Storage Status (deep)
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_ceph(self, sec):
        r = None
        for cmd in [
            "rgac 'cephmon ceph -s' 2>/dev/null",
            "ssh -o StrictHostKeyChecking=no cephmon 'ceph -s' 2>/dev/null",
            "docker exec ceph_mon_0 ceph -s 2>/dev/null",
            "podman exec ceph_mon_0 ceph -s 2>/dev/null",
        ]:
            r = await self._lc(sec, cmd, timeout=30)
            if r.out.strip() and ("HEALTH" in r.out or
                                  "cluster" in r.out.lower()):
                break
        if not r or not r.out.strip():
            sec.warn("Ceph -s output unavailable")
            return
        for keyword, fn in [("HEALTH_OK", sec.pass_),
                            ("HEALTH_WARN", sec.warn),
                            ("HEALTH_ERR", sec.fail)]:
            if keyword in r.out:
                fn(f"Ceph cluster: {keyword}", detail=r.out[:500])
                break
        else:
            sec.warn("Could not determine Ceph health",
                     detail=r.out[:300])
        # OSD summary
        osd_m = re.search(r"osd:.*", r.out)
        if osd_m:
            sec.info(f"Ceph OSD: {osd_m.group().strip()}")
        # Client I/O
        client_m = re.search(r"client:.*", r.out)
        if client_m:
            sec.info(f"Ceph I/O: {client_m.group().strip()}")

    # ══════════════════════════════════════════════════════════════════════════
    #  15. Ceph Pool Health
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_ceph_pools(self, sec):
        r = None
        for cmd in [
            "rgac 'cephmon ceph osd pool ls detail' 2>/dev/null",
            "ssh -o StrictHostKeyChecking=no cephmon "
            "'ceph osd pool ls detail' 2>/dev/null",
        ]:
            r = await self._lc(sec, cmd, timeout=30)
            if r.out.strip():
                break
        if not r or not r.out.strip():
            sec.skip("Ceph pool detail unavailable")
            return
        pools = re.findall(r"pool\s+\d+\s+'(\S+)'", r.out)
        sec.info(f"{len(pools)} Ceph pool(s): {', '.join(pools[:10])}")
        # PG status
        r2 = None
        for cmd in [
            "rgac 'cephmon ceph pg stat' 2>/dev/null",
            "ssh -o StrictHostKeyChecking=no cephmon "
            "'ceph pg stat' 2>/dev/null",
        ]:
            r2 = await self._lc(sec, cmd, timeout=20)
            if r2.out.strip():
                break
        if r2 and r2.out:
            if re.search(
                    r"degraded|incomplete|inconsistent|stale|undersized",
                    r2.out, re.I):
                sec.fail("Ceph PG issues detected",
                         detail=r2.out[:400])
            else:
                sec.pass_(f"Ceph PGs healthy: {r2.out.strip()[:120]}")
        # OSD tree
        r3 = None
        for cmd in [
            "rgac 'cephmon ceph osd tree' 2>/dev/null | grep -v 'WEIGHT'",
            "ssh -o StrictHostKeyChecking=no cephmon "
            "'ceph osd tree' 2>/dev/null | grep -v WEIGHT",
        ]:
            r3 = await self._lc(sec, cmd, timeout=25)
            if r3.out.strip():
                break
        if r3 and r3.out:
            down = len(re.findall(r"\bdown\b", r3.out, re.I))
            (sec.fail(f"{down} OSD(s) down in osd tree",
                      detail=r3.out[:600]) if down
             else sec.pass_("All OSDs UP in OSD tree"))

    # ══════════════════════════════════════════════════════════════════════════
    #  16. OVS / Networking
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_ovs(self, sec):
        r = await self._lc(sec,
            "docker exec neutron_ovs_agent ovs-vsctl show 2>/dev/null | "
            "head -20 || "
            "podman exec neutron_ovs_agent ovs-vsctl show 2>/dev/null | "
            "head -20 || "
            "ovs-vsctl show 2>/dev/null | head -20 || true", timeout=20)
        if r.out.strip():
            bridges = re.findall(r"Bridge\s+\"?(\S+?)\"?", r.out)
            sec.info(
                f"OVS bridges: {', '.join(bridges)}" if bridges
                else "OVS running (no bridges?)")
        else:
            sec.warn("OVS vsctl inaccessible")
        # Version
        r2 = await self._lc(sec,
            "ovs-vsctl get Open_vSwitch . ovs_version 2>/dev/null || "
            "docker exec neutron_ovs_agent ovs-vsctl get Open_vSwitch . "
            "ovs_version 2>/dev/null || true", timeout=10)
        if r2.out.strip():
            sec.pass_(f"OVS version: {r2.out.strip()}")

    # ══════════════════════════════════════════════════════════════════════════
    #  17. HAProxy / VIP
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_haproxy(self, sec):
        r = await self._lc(sec,
            "docker exec haproxy_config haproxy -c "
            "-f /etc/haproxy/haproxy.cfg 2>/dev/null | head -3 || "
            "podman exec haproxy_config haproxy -c "
            "-f /etc/haproxy/haproxy.cfg 2>/dev/null | head -3 || true",
            timeout=20)
        if r.out:
            (sec.pass_("HAProxy config check OK") if "OK" in r.out
             else sec.warn("HAProxy config output", detail=r.out))
        # VIP check
        r2 = await self._lc(sec,
            "ip addr show | grep -E 'inet .*/32|inet .*/24' | "
            "grep secondary 2>/dev/null || "
            "ip addr show | grep -i vip 2>/dev/null || true", timeout=10)
        if r2.out:
            sec.info("VIP addresses detected", detail=r2.out[:300])
        # HAProxy stats
        r3 = await self._lc(sec,
            "echo 'show info' | socat stdio "
            "/var/run/haproxy/admin.sock 2>/dev/null | "
            "grep -E 'Version|Uptime|MaxConn|CurrConns' | head -6 "
            "|| true", timeout=10)
        if r3.out:
            sec.info("HAProxy stats", detail=r3.out[:300])

    # ══════════════════════════════════════════════════════════════════════════
    #  18. NFS / External Storage
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_nfs(self, sec):
        r = await self._lc(sec,
            "showmount -e localhost 2>/dev/null | head -10 || "
            "cat /etc/exports 2>/dev/null | grep -v '^#' | head -10 || true",
            timeout=15)
        if r.out.strip():
            sec.info("NFS exports configured", detail=r.out[:400])
        else:
            sec.skip("No NFS exports detected on installer node")
        # Mount health
        r2 = await self._lc(sec,
            "mount | grep nfs | head -10 || true", timeout=10)
        if r2.out:
            sec.info("NFS mounts active", detail=r2.out[:300])

    # ══════════════════════════════════════════════════════════════════════════
    #  19. CVIM Installer Status
    # ══════════════════════════════════════════════════════════════════════════
    async def _check_installer(self, sec):
        # CVIM version
        r = await self._lc(sec,
            "ciscovim --version 2>/dev/null || "
            "cat /root/installer-*/version.txt 2>/dev/null | head -2 || true")
        if r.out:
            sec.info(f"CVIM version: {r.out.strip()[:100]}")
        # Management status
        r2 = await self._lc(sec,
            "ciscovim mgmt-node-health 2>/dev/null | head -20 || true",
            timeout=30)
        if r2.out:
            if "healthy" in r2.out.lower() or "ok" in r2.out.lower():
                sec.pass_("CVIM management node healthy")
            else:
                sec.warn("CVIM management node status",
                         detail=r2.out[:400])
        # Recent error logs
        r3 = await self._lc(sec,
            "find /var/log/mercury* /var/log/cvim* 2>/dev/null "
            "-name '*.log' -newer /tmp -mmin -60 | "
            "xargs grep -l 'ERROR\\|FATAL\\|CRITICAL' 2>/dev/null | "
            "head -5 || true", timeout=20)
        if r3.out.strip():
            sec.warn("Recent error log entries found in CVIM logs",
                     detail=r3.out[:300])
        else:
            sec.pass_(
                "No recent error/fatal entries in CVIM logs (last 60 min)")
        # openrc file
        r4 = await self._lc(sec,
            "test -f /root/openstack-configs/openrc && echo 'FOUND' "
            "|| echo 'MISSING'")
        (sec.pass_("openrc credentials file present") if "FOUND" in r4.out
         else sec.warn("openrc file not found"))
