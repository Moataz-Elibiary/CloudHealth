"""Host-level health checks — SSH directly to physical nodes (RHEL/RHCOS)."""
from __future__ import annotations
import asyncio, re
from datetime import datetime
from typing import List
import logging

from config import SSHCred, AppConfig
from result import Section, Status
from ssh_client import SSHClient


class HostHealthChecker:
    def __init__(self, nodes: List[str], ssh_cred: SSHCred, app: AppConfig,
                 logger: logging.Logger, console, cluster_name: str = ""):
        self.nodes   = nodes
        self.cred    = ssh_cred
        self.app     = app
        self.log     = logger
        self.con     = console
        self.cluster = cluster_name

    # ── parallel runner ───────────────────────────────────────────────────────

    async def run(self) -> List[Section]:
        sem   = asyncio.Semaphore(self.app.max_parallel_nodes)
        tasks = [self._check_node_guarded(n, sem) for n in self.nodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        sections = []
        for node, res in zip(self.nodes, results):
            if isinstance(res, Exception):
                s = Section(f"Host: {node}", "host", start_time=datetime.now())
                s.error(f"Exception: {res}")
                s.end_time = datetime.now()
                sections.append(s)
            else:
                sections.append(res)
        return sections

    async def _check_node_guarded(self, node: str, sem: asyncio.Semaphore) -> Section:
        async with sem:
            return await self._check_node(node)

    # ── per-node ──────────────────────────────────────────────────────────────

    async def _check_node(self, node: str) -> Section:
        sec = Section(f"Host: {node}", "host", start_time=datetime.now())
        self.con.section_start(f"Host: {node}")
        ssh = SSHClient(node, self.cred, timeout=self.app.ssh_timeout, logger=self.log)
        try:
            await ssh.connect()
        except Exception as e:
            sec.fail(f"SSH connection failed: {e}")
            sec.end_time = datetime.now()
            self.con.section_done(sec)
            return sec
        try:
            # Run all host checks concurrently on this node
            await asyncio.gather(
                self._uptime(ssh, sec),
                self._os_info(ssh, sec),
                self._cpu(ssh, sec),
                self._memory(ssh, sec),
                self._disk(ssh, sec),
                self._ecc(ssh, sec),
                self._network_interfaces(ssh, sec),
                self._bond_status(ssh, sec),
                self._sriov(ssh, sec),
                self._kernel_messages(ssh, sec),
                self._systemd_services(ssh, sec),
                self._ntp(ssh, sec),
                self._pcie_errors(ssh, sec),
                self._firmware_versions(ssh, sec),
                self._numa_topology(ssh, sec),
                self._hugepages(ssh, sec),
                self._selinux(ssh, sec),
                self._firewall(ssh, sec),
                self._open_ports(ssh, sec),
            )
        except Exception as e:
            sec.error(f"Host check error: {e}")
        finally:
            await ssh.close()
        sec.end_time = datetime.now()
        self.con.section_done(sec)
        return sec

    async def _r(self, ssh: SSHClient, sec: Section, cmd: str, timeout: int = 30):
        r = await ssh.run(cmd, timeout=timeout)
        sec.append_log(f"$ {cmd}\n{r.stdout}{r.stderr}\n")
        return r

    # ══════════════════════════════════════════════════════════════════════════
    #  Uptime & load average
    # ══════════════════════════════════════════════════════════════════════════
    async def _uptime(self, ssh, sec):
        r = await self._r(ssh, sec, "uptime")
        if r.exit_code != 0:
            sec.fail("Cannot get uptime"); return
        load_m = re.search(r"load average[s]?:\s+([\d.]+),\s*([\d.]+),\s*([\d.]+)", r.stdout)
        cpu_r  = await ssh.run("nproc 2>/dev/null || echo 1", timeout=10)
        try:
            cpus = int(cpu_r.out.strip())
        except Exception:
            cpus = 1
        if load_m:
            l1, l5, l15 = float(load_m.group(1)), float(load_m.group(2)), float(load_m.group(3))
            ratio = l1 / cpus
            msg = f"Load avg: {l1}/{l5}/{l15} on {cpus} CPU(s) — ratio {ratio:.2f}"
            if ratio >= self.app.load_ratio_fail:
                sec.fail(msg)
            elif ratio >= self.app.load_ratio_warn:
                sec.warn(msg)
            else:
                sec.pass_(msg)
        else:
            sec.info(f"Uptime: {r.out.strip()[:80]}")

    # ══════════════════════════════════════════════════════════════════════════
    #  OS / kernel info
    # ══════════════════════════════════════════════════════════════════════════
    async def _os_info(self, ssh, sec):
        r = await self._r(ssh, sec,
            "cat /etc/redhat-release 2>/dev/null || cat /etc/os-release 2>/dev/null | head -3")
        r2 = await self._r(ssh, sec, "uname -r")
        if r.out or r2.out:
            sec.info(f"OS: {r.out.strip()[:80]} | Kernel: {r2.out.strip()}")

    # ══════════════════════════════════════════════════════════════════════════
    #  CPU info & throttling
    # ══════════════════════════════════════════════════════════════════════════
    async def _cpu(self, ssh, sec):
        r = await self._r(ssh, sec,
            "grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | xargs")
        model = r.out.strip() if r.out else "unknown"
        r2 = await self._r(ssh, sec, "nproc --all 2>/dev/null; lscpu 2>/dev/null | grep -E 'Socket|Core|Thread' | head -3")
        sec.info(f"CPU: {model} | {r2.out.strip()[:120]}")
        # Thermal throttling events
        r3 = await self._r(ssh, sec,
            "grep -r 'throttled' /sys/devices/system/cpu/cpu*/thermal_throttle/ 2>/dev/null | "
            "awk -F: '{sum+=$NF} END{print sum+0}'")
        try:
            throttled = int(r3.out.strip())
            if throttled > 0:
                sec.warn(f"CPU thermal throttling events: {throttled}")
        except ValueError:
            pass
        # CPU frequency scaling governor
        r4 = await self._r(ssh, sec,
            "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo 'unavailable'")
        if "performance" not in r4.out and "unavailable" not in r4.out:
            sec.warn(f"CPU governor: {r4.out.strip()} (expected 'performance' for NFV workloads)")
        elif "performance" in r4.out:
            sec.pass_(f"CPU governor: performance")

    # ══════════════════════════════════════════════════════════════════════════
    #  Memory
    # ══════════════════════════════════════════════════════════════════════════
    async def _memory(self, ssh, sec):
        r = await self._r(ssh, sec, "free -m")
        mem_line = next((l for l in r.stdout.splitlines() if l.startswith("Mem:")), None)
        if mem_line:
            parts = mem_line.split()
            # total used free shared buff/cache available
            if len(parts) >= 7:
                try:
                    total_mb = int(parts[1]); used_mb = int(parts[2]); avail_mb = int(parts[6])
                    used_pct = round(used_mb * 100 / total_mb) if total_mb else 0
                    msg = (f"RAM: {total_mb//1024}G total, {used_mb//1024}G used ({used_pct}%), "
                           f"{avail_mb//1024}G available")
                    if used_pct >= self.app.mem_used_pct_fail:
                        sec.fail(msg)
                    elif used_pct >= self.app.mem_used_pct_warn:
                        sec.warn(msg)
                    else:
                        sec.pass_(msg)
                except (ValueError, ZeroDivisionError):
                    sec.info(f"Memory: {mem_line.strip()}")
        swap_line = next((l for l in r.stdout.splitlines() if l.startswith("Swap:")), None)
        if swap_line:
            parts = swap_line.split()
            if len(parts) >= 3:
                try:
                    total_s = int(parts[1]); used_s = int(parts[2])
                    if total_s > 0:
                        pct_s = round(used_s * 100 / total_s)
                        (sec.warn(f"Swap {used_s}MB/{total_s}MB used ({pct_s}%)")
                         if pct_s >= self.app.swap_used_pct_warn else
                         sec.pass_(f"Swap: {pct_s}% used"))
                except (ValueError, ZeroDivisionError):
                    pass
        # OOM kills
        r2 = await self._r(ssh, sec,
            "journalctl -k --since='24 hours ago' 2>/dev/null | grep -c 'oom_kill\\|Out of memory' || "
            "grep -c 'Out of memory\\|oom_kill' /var/log/messages 2>/dev/null || echo 0", timeout=20)
        try:
            oom_cnt = int(r2.out.strip())
            if oom_cnt > 0:
                sec.warn(f"OOM kill event(s) in last 24h: {oom_cnt}")
        except ValueError:
            pass
        # Memory NUMA balance
        r3 = await self._r(ssh, sec, "numactl --hardware 2>/dev/null | head -8 || true")
        if r3.out.strip():
            sec.info("NUMA hardware", detail=r3.out.strip()[:400])

    # ══════════════════════════════════════════════════════════════════════════
    #  Disk utilization
    # ══════════════════════════════════════════════════════════════════════════
    async def _disk(self, ssh, sec):
        thr = self.app.disk_threshold
        r = await self._r(ssh, sec,
            "df -h --output=source,pcent,target 2>/dev/null | "
            "grep -vE '^tmpfs|^devtmpfs|^Filesystem|^overlay|^shm'")
        ok = True
        detail_rows = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line: continue
            parts = line.split()
            if len(parts) < 3: continue
            try:
                pct = int(parts[1].rstrip("%"))
            except ValueError:
                continue
            detail_rows.append(f"{parts[0]:<40} {parts[1]:>5}  {parts[2]}")
            if pct >= thr:
                sec.fail(f"Disk {parts[2]} at {pct}% (threshold: {thr}%)", command="df -h")
                ok = False
        if ok:
            sec.pass_(f"All disk mounts below {thr}%", detail="\n".join(detail_rows[:20]))
        # Inode usage
        r2 = await self._r(ssh, sec,
            "df -i 2>/dev/null | grep -vE '^tmpfs|^devtmpfs|^Filesystem' | "
            "awk '$5~/[0-9]+%/{gsub(/%/,\"\",$5); if($5+0>=90) print}'")
        if r2.out.strip():
            sec.warn("High inode usage detected", detail=r2.out.strip()[:300])
        # Disk I/O wait
        r3 = await self._r(ssh, sec,
            "iostat -x 1 2 2>/dev/null | awk '/Device/{ok=1} ok && /[a-z]/{print}' | tail -10 || true")
        if r3.out.strip():
            high_wait = [l for l in r3.out.splitlines() if l.strip() and
                         len(l.split()) > 10 and float(l.split()[-1] if l.split()[-1].replace('.','').isdigit() else 0) > 20]
            if high_wait:
                sec.warn("High disk I/O await detected", detail="\n".join(high_wait[:5]))
        # SMART status for physical drives
        r4 = await self._r(ssh, sec,
            "for d in $(lsblk -d -n -o NAME 2>/dev/null | grep -E '^sd|^nvme' | head -4); do "
            "  smartctl -H /dev/$d 2>/dev/null | grep -E 'overall-health|PASSED|FAILED' | "
            "  awk -v d=$d '{print d\": \"$0}'; "
            "done || true", timeout=30)
        if r4.out.strip():
            failed = [l for l in r4.out.splitlines() if "FAILED" in l.upper()]
            if failed:
                sec.fail("SMART health FAILED for drive(s)", detail="\n".join(failed))
            else:
                sec.pass_("SMART health: PASSED for all checked drives", detail=r4.out.strip()[:300])

    # ══════════════════════════════════════════════════════════════════════════
    #  ECC memory errors
    # ══════════════════════════════════════════════════════════════════════════
    async def _ecc(self, ssh, sec):
        # Uncorrectable errors (UE) — critical
        r_ue = await self._r(ssh, sec,
            "for f in /sys/devices/system/edac/mc/mc*/ue_count 2>/dev/null; do "
            "  [[ -f $f ]] && echo \"$f: $(cat $f 2>/dev/null || echo 0)\"; done || echo 'no_edac'")
        # Correctable errors (CE) — warning
        r_ce = await self._r(ssh, sec,
            "for f in /sys/devices/system/edac/mc/mc*/ce_count 2>/dev/null; do "
            "  [[ -f $f ]] && echo \"$f: $(cat $f 2>/dev/null || echo 0)\"; done")
        # edac-util summary
        r_edac = await self._r(ssh, sec, "edac-util -s 0 2>/dev/null || true")
        # dmesg MCE events
        r_mce = await self._r(ssh, sec,
            "journalctl -k --since='7 days ago' 2>/dev/null | "
            "grep -iE 'machine check|mce|corrected memory|uncorrected memory|DIMM|ECC' | "
            "tail -10 || dmesg -T 2>/dev/null | "
            "grep -iE 'machine check|mce|DIMM|ECC' | tail -10 || true", timeout=20)

        node_ok = True
        # Parse UE
        for line in r_ue.stdout.splitlines():
            if "no_edac" in line: break
            m = re.search(r":\s*(\d+)$", line.strip())
            if m and int(m.group(1)) > 0:
                sec.fail(f"Uncorrectable ECC error: {line.strip()}")
                node_ok = False
        # Parse CE
        for line in r_ce.stdout.splitlines():
            m = re.search(r":\s*(\d+)$", line.strip())
            if m and int(m.group(1)) > 0:
                sec.warn(f"Correctable ECC errors: {line.strip()} (monitor)")
        # edac-util
        if r_edac.out and "error" in r_edac.out.lower():
            sec.warn("edac-util reported issues", detail=r_edac.out[:200])
        # MCE dmesg
        if r_mce.out.strip():
            sec.warn("MCE/ECC messages in kernel log", detail=r_mce.out.strip()[:400])
        if node_ok:
            sec.pass_("No uncorrectable ECC errors detected")

    # ══════════════════════════════════════════════════════════════════════════
    #  Network interfaces
    # ══════════════════════════════════════════════════════════════════════════
    async def _network_interfaces(self, ssh, sec):
        r = await self._r(ssh, sec, "ip -brief link show 2>/dev/null")
        if r.exit_code != 0:
            r = await self._r(ssh, sec, "ip link show 2>/dev/null")
        lines = [l for l in r.stdout.splitlines() if l.strip() and "lo " not in l]
        down   = [l for l in lines if " DOWN " in l or "NO-CARRIER" in l]
        up     = [l for l in lines if " UP " in l]
        unknown= [l for l in lines if " UNKNOWN " in l and "loop" not in l.lower()]
        sec.info(f"Interfaces: {len(up)} UP, {len(down)} DOWN, {len(unknown)} UNKNOWN")
        if down:
            # Filter expected-down (e.g., VFs, dummy interfaces)
            unexpected = [l for l in down if not re.search(r"vf|dummy|tap|veth|vxlan", l, re.I)]
            if unexpected:
                sec.warn(f"{len(unexpected)} unexpected interface(s) DOWN",
                         detail="\n".join(unexpected[:10]))
            else:
                sec.pass_("All physical interfaces UP (virtual/VF interfaces may be DOWN as expected)")
        else:
            sec.pass_("All network interfaces UP")
        # Check interface errors
        r2 = await self._r(ssh, sec,
            "ip -s link show 2>/dev/null | awk '/^[0-9]/{iface=$2} "
            "/RX:/{getline; rx_err=$3} /TX:/{getline; tx_err=$3; "
            "if(rx_err+0>0||tx_err+0>0) print iface\" RX_ERR:\"rx_err\" TX_ERR:\"tx_err}' | "
            "grep -v 'lo:' | head -10 || true", timeout=15)
        if r2.out.strip():
            sec.warn("Interface errors detected", detail=r2.out.strip()[:300])

    # ══════════════════════════════════════════════════════════════════════════
    #  Bond status
    # ══════════════════════════════════════════════════════════════════════════
    async def _bond_status(self, ssh, sec):
        r = await self._r(ssh, sec, "ls /proc/net/bonding/ 2>/dev/null || echo 'no_bonds'")
        if "no_bonds" in r.out or not r.out.strip():
            sec.info("No bond interfaces configured"); return
        bonds = [b.strip() for b in r.out.splitlines() if b.strip()]
        for bond in bonds:
            r2 = await self._r(ssh, sec, f"cat /proc/net/bonding/{bond} 2>/dev/null")
            if not r2.out: continue
            mode_m  = re.search(r"Bonding Mode:\s+(.+)", r2.out)
            mode    = mode_m.group(1).strip() if mode_m else "unknown"
            active  = re.findall(r"Slave Interface:\s+(\S+)", r2.out)
            mii_down= re.findall(r"MII Status:\s+down", r2.out)
            link_failures = re.findall(r"Link Failure Count:\s+(\d+)", r2.out)
            total_failures = sum(int(x) for x in link_failures)
            if mii_down:
                sec.fail(f"Bond {bond} ({mode}): {len(mii_down)}/{len(active)} slave(s) MII down",
                         detail=r2.out[:600])
            elif total_failures > 0:
                sec.warn(f"Bond {bond} ({mode}): {len(active)} slaves active, "
                         f"{total_failures} historical link failure(s)")
            else:
                sec.pass_(f"Bond {bond} ({mode}): {len(active)} slave(s) all UP, no failures")

    # ══════════════════════════════════════════════════════════════════════════
    #  SR-IOV
    # ══════════════════════════════════════════════════════════════════════════
    async def _sriov(self, ssh, sec):
        r = await self._r(ssh, sec,
            "for d in $(ls /sys/class/net/ 2>/dev/null); do "
            "  f=/sys/class/net/$d/device/sriov_numvfs; "
            "  [[ -f $f ]] && vfs=$(cat $f) && [[ $vfs -gt 0 ]] && "
            "  echo \"$d: $vfs VFs\"; "
            "done || true")
        if r.out.strip():
            total_vfs = sum(int(m) for m in re.findall(r"(\d+) VFs", r.out))
            sec.info(f"SR-IOV: {total_vfs} total Virtual Functions", detail=r.out.strip()[:300])
            # Check VF state
            r2 = await self._r(ssh, sec,
                "ip link show 2>/dev/null | grep -A1 'vf ' | head -30 || true")
            if r2.out.strip():
                disabled = len(re.findall(r"link-state disable", r2.out))
                if disabled:
                    sec.warn(f"SR-IOV: {disabled} VF(s) with link-state disabled")
                else:
                    sec.pass_(f"SR-IOV: all {total_vfs} VFs active")

    # ══════════════════════════════════════════════════════════════════════════
    #  Kernel messages
    # ══════════════════════════════════════════════════════════════════════════
    async def _kernel_messages(self, ssh, sec):
        r = await self._r(ssh, sec,
            "journalctl -k -p 0..4 --since='24 hours ago' 2>/dev/null | "
            "grep -vE 'audit|ACPI.*Information|microcode|pcie_bw|BERT' | tail -20 || "
            "dmesg -T 2>/dev/null | grep -iE 'error|crit|emerg|alert|warn' | "
            "grep -vE 'audit|acpi|microcode' | tail -20 || true", timeout=25)
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        critical = [l for l in lines if re.search(r"emerg|alert|crit|panic|oops|BUG:", l, re.I)]
        errors   = [l for l in lines if re.search(r"\berr\b|\berror\b", l, re.I)]
        if critical:
            sec.fail(f"{len(critical)} critical kernel message(s) in last 24h",
                     detail="\n".join(critical[:10]))
        elif errors:
            sec.warn(f"{len(errors)} kernel error message(s) in last 24h",
                     detail="\n".join(errors[:10]))
        else:
            sec.pass_("No critical kernel messages in last 24h")

    # ══════════════════════════════════════════════════════════════════════════
    #  Systemd services
    # ══════════════════════════════════════════════════════════════════════════
    async def _systemd_services(self, ssh, sec):
        r = await self._r(ssh, sec,
            "systemctl list-units --state=failed --no-legend --no-pager 2>/dev/null | head -20 || true")
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        if lines:
            sec.fail(f"{len(lines)} failed systemd service(s)", detail="\n".join(lines[:15]))
        else:
            sec.pass_("No failed systemd services")
        # Check critical services running
        for svc in ("sshd", "chronyd", "NetworkManager"):
            r2 = await ssh.run(f"systemctl is-active {svc} 2>/dev/null", timeout=10)
            state = r2.out.strip()
            if state != "active":
                sec.warn(f"Service {svc}: {state}")

    # ══════════════════════════════════════════════════════════════════════════
    #  NTP
    # ══════════════════════════════════════════════════════════════════════════
    async def _ntp(self, ssh, sec):
        r = await self._r(ssh, sec,
            "chronyc tracking 2>/dev/null || timedatectl status 2>/dev/null | "
            "grep -E 'NTP|synchronized|Time zone' | head -5 || true")
        if not r.out.strip():
            sec.warn("Cannot determine NTP status"); return
        if re.search(r"synchronized.*yes|NTP service: active|System time.*offset", r.out, re.I):
            # Check offset
            offset_m = re.search(r"System time\s*offset\s*:\s*([\d.]+)", r.out)
            if offset_m:
                offset_ms = float(offset_m.group(1)) * 1000
                (sec.warn(f"NTP offset high: {offset_ms:.1f}ms") if offset_ms > 100 else
                 sec.pass_(f"NTP synchronized, offset: {offset_ms:.2f}ms"))
            else:
                sec.pass_(f"NTP synchronized")
        elif re.search(r"synchronized.*no|NTP service: inactive", r.out, re.I):
            sec.fail("NTP not synchronized", detail=r.out.strip()[:200])
        else:
            sec.info(f"NTP status: {r.out.strip()[:120]}")

    # ══════════════════════════════════════════════════════════════════════════
    #  PCIe errors
    # ══════════════════════════════════════════════════════════════════════════
    async def _pcie_errors(self, ssh, sec):
        r = await self._r(ssh, sec,
            "dmesg -T 2>/dev/null | grep -iE 'pcie.*error|aer.*error|pci.*error|nmi.*error' | "
            "grep -iv 'corrected\\|informational' | tail -10 || true")
        if r.out.strip():
            sec.warn("PCIe/AER error messages detected", detail=r.out.strip()[:400])
        else:
            sec.pass_("No uncorrected PCIe/AER errors in kernel log")

    # ══════════════════════════════════════════════════════════════════════════
    #  Firmware versions
    # ══════════════════════════════════════════════════════════════════════════
    async def _firmware_versions(self, ssh, sec):
        r = await self._r(ssh, sec,
            "dmidecode -t bios 2>/dev/null | grep -E 'Vendor|Version|Release Date' | head -3 || true")
        if r.out.strip():
            sec.info(f"BIOS/Firmware: {' | '.join(l.strip() for l in r.out.strip().splitlines()[:3])}")
        # NIC firmware
        r2 = await self._r(ssh, sec,
            "for d in $(ls /sys/class/net/ | grep -vE '^lo|^vir|^veth|^docker' | head -4); do "
            "  ethtool -i $d 2>/dev/null | grep -E 'driver|firmware-version' | "
            "  awk -v if=$d '{print if\": \"$0}'; "
            "done | head -12 || true", timeout=20)
        if r2.out.strip():
            sec.info("NIC drivers/firmware", detail=r2.out.strip()[:400])

    # ══════════════════════════════════════════════════════════════════════════
    #  NUMA topology
    # ══════════════════════════════════════════════════════════════════════════
    async def _numa_topology(self, ssh, sec):
        r = await self._r(ssh, sec,
            "numactl --hardware 2>/dev/null | grep -E 'available|node [0-9]+ cpus|node [0-9]+ size' | head -8 || true")
        if r.out.strip():
            nodes_m = re.search(r"available:\s*(\d+)\s*node", r.out)
            n_nodes = int(nodes_m.group(1)) if nodes_m else 0
            sec.info(f"NUMA: {n_nodes} NUMA node(s)", detail=r.out.strip()[:300])

    # ══════════════════════════════════════════════════════════════════════════
    #  Hugepages
    # ══════════════════════════════════════════════════════════════════════════
    async def _hugepages(self, ssh, sec):
        r = await self._r(ssh, sec,
            "grep -E 'HugePages_Total|HugePages_Free|Hugepagesize' /proc/meminfo 2>/dev/null || true")
        if not r.out.strip():
            sec.info("Hugepages: not configured"); return
        total_m = re.search(r"HugePages_Total:\s+(\d+)", r.out)
        free_m  = re.search(r"HugePages_Free:\s+(\d+)",  r.out)
        size_m  = re.search(r"Hugepagesize:\s+(\S+)", r.out)
        if total_m and free_m and size_m:
            total = int(total_m.group(1)); free = int(free_m.group(1))
            used  = total - free
            sz    = size_m.group(1)
            if total > 0:
                pct = round(used * 100 / total)
                msg = f"Hugepages ({sz}): {used}/{total} used ({pct}%), {free} free"
                (sec.warn(msg) if pct > 95 else sec.pass_(msg))
            else:
                sec.info("Hugepages configured but none allocated")

    # ══════════════════════════════════════════════════════════════════════════
    #  SELinux
    # ══════════════════════════════════════════════════════════════════════════
    async def _selinux(self, ssh, sec):
        r = await self._r(ssh, sec, "getenforce 2>/dev/null || echo 'Disabled'")
        state = r.out.strip()
        if state == "Enforcing":
            sec.pass_("SELinux: Enforcing")
        elif state == "Permissive":
            sec.warn("SELinux: Permissive (not enforcing)")
        else:
            sec.info(f"SELinux: {state}")

    # ══════════════════════════════════════════════════════════════════════════
    #  Firewall
    # ══════════════════════════════════════════════════════════════════════════
    async def _firewall(self, ssh, sec):
        r = await self._r(ssh, sec,
            "firewall-cmd --state 2>/dev/null || systemctl is-active firewalld 2>/dev/null || "
            "iptables -L INPUT --line-numbers 2>/dev/null | head -5 || echo 'unknown'", timeout=10)
        state = r.out.strip().lower()
        if "running" in state or "active" in state:
            sec.info("Firewall: running")
        elif "not running" in state or "inactive" in state:
            sec.info("Firewall: not running")
        else:
            sec.info(f"Firewall state: {state[:60]}")

    # ══════════════════════════════════════════════════════════════════════════
    #  Open ports (listening services audit)
    # ══════════════════════════════════════════════════════════════════════════
    async def _open_ports(self, ssh, sec):
        r = await self._r(ssh, sec,
            "ss -tlnp 2>/dev/null | grep LISTEN | awk '{print $4, $6}' | "
            "grep -vE '127.0.0.1|::1|%lo' | sort -t: -k2 -n | head -20 || "
            "netstat -tlnp 2>/dev/null | grep LISTEN | head -20 || true", timeout=15)
        if r.out.strip():
            sec.info("Listening TCP ports", detail=r.out.strip()[:500])
