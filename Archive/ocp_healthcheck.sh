#!/usr/bin/env bash
# =============================================================================
# OCP Cluster Health Check Script
# Usage: ./ocp_healthcheck.sh -f <login_commands_file> [-o <output_dir>]
# Login file format (one per line):  oc login <api_url> --token=<token>
#                                 or oc login <api_url> -u <user> -p <pass>
#
# Optional env-var overrides:
#   DISK_THRESHOLD          Disk % that triggers FAIL              (default: 80)
#   RESTART_WARN_THRESHOLD  Restart count that triggers WARN       (default: 10)
#   RESTART_FAIL_THRESHOLD  Restart count that triggers FAIL       (default: 50)
#   POD_AGE_MIN_WARN        Pod age (minutes) below which is WARN  (default: 5)
#   POD_AGE_MIN_FAIL        Pod age (minutes) below which is FAIL  (default: 2)
# =============================================================================

set -euo pipefail

# ──────────────────────────── defaults ────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUTPUT_DIR="${SCRIPT_DIR}/ocp_healthcheck_${TIMESTAMP}"
LOGIN_FILE=""


# ──────────────────────────── colours ─────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'


# ──────────────────────────── usage ───────────────────────────────────────────
usage() {
  echo -e "${BOLD}Usage:${RESET} $0 -f <login_file> [-o <output_dir>]"
  echo ""
  echo " -f Path to file containing 'oc login ...' commands (one per line)"
  echo " -o Output directory (default: ./ocp_healthcheck_<timestamp>)"
  echo ""
  echo "Login file example:"
  echo " oc login https://api.cluster1.example.com:6443 --token=sha256~xxxx"
  echo " oc login https://api.cluster2.example.com:6443 -u admin -p secret"
  exit 1
}


# ──────────────────────────── arg parsing ─────────────────────────────────────
while getopts ":f:o:h" opt; do
  case $opt in
    f) LOGIN_FILE="$OPTARG" ;;
    o) OUTPUT_DIR="$OPTARG" ;;
    h) usage ;;
    *) usage ;;
  esac
done

[[ -z "$LOGIN_FILE" ]] && { echo -e "${RED}ERROR:${RESET} -f <login_file> is required."; usage; }
[[ ! -f "$LOGIN_FILE" ]] && { echo -e "${RED}ERROR:${RESET} Login file '$LOGIN_FILE' not found."; exit 1; }
command -v oc &>/dev/null || { echo -e "${RED}ERROR:${RESET} 'oc' CLI not found in PATH."; exit 1; }

mkdir -p "$OUTPUT_DIR"


# ──────────────────────────── global log ──────────────────────────────────────
GLOBAL_LOG="${OUTPUT_DIR}/commands_executed.log"
SUMMARY_FILE="${OUTPUT_DIR}/healthcheck_summary.txt"

log_cmd() {
  # log_cmd <label> <command_string> [output_var]
  local label="$1"; local cmd="$2"
  echo "──────────────────────────────────────────" >> "$GLOBAL_LOG"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [${CLUSTER_NAME:-GLOBAL}] ${label}" >> "$GLOBAL_LOG"
  echo "CMD: ${cmd}" >> "$GLOBAL_LOG"
  eval "$cmd" >> "$GLOBAL_LOG" 2>&1 || true
  echo "" >> "$GLOBAL_LOG"
}


# ──────────────────────────── result tracking ─────────────────────────────────
PASS=0; FAIL=0

pass() { echo -e " ${GREEN}[PASS]${RESET} $*"; echo " [PASS] $*" >> "$CLUSTER_REPORT"; ((PASS++)) || true; }
fail() { echo -e " ${RED}[FAIL]${RESET} $*"; echo " [FAIL] $*" >> "$CLUSTER_REPORT"; ((FAIL++)) || true; }
warn() { echo -e " ${YELLOW}[WARN]${RESET} $*"; echo " [WARN] $*" >> "$CLUSTER_REPORT"; }
info() { echo -e " ${CYAN}[INFO]${RESET} $*"; echo " [INFO] $*" >> "$CLUSTER_REPORT"; }
section() {
  echo -e "${BOLD}$*${RESET}"
  echo "" >> "$CLUSTER_REPORT"
  echo "-> $*" >> "$CLUSTER_REPORT"
}


# =============================================================================
# HEALTH CHECK FUNCTIONS
# =============================================================================

# ─────────────────────────── 1. Cluster version & API ─────────────────────────
check_cluster_version() {
  section "Cluster Version & API Server"
  local cmd="oc version"
  log_cmd "Cluster Version" "$cmd"
  local out; out=$(eval "$cmd" 2>&1) || true
  
  local server_ver; server_ver=$(echo "$out" | grep -i "server version" | awk '{print $NF}') || true
  [[ -n "$server_ver" ]] && pass "API Server reachable – Server Version: ${server_ver}" || fail "Could not retrieve server version (API unreachable?)"
  
  local cmd2="oc get clusterversion version -o jsonpath='{.status.conditions[?(@.type==\"Available\")].status}'"
  log_cmd "ClusterVersion Available condition" "$cmd2"
  local av; av=$(eval "$cmd2" 2>&1) || true
  [[ "$av" == "'True'" || "$av" == "True" ]] && pass "ClusterVersion Available=True" || fail "ClusterVersion Available=${av}"
  
  local cmd3="oc get clusterversion version -o jsonpath='{.status.conditions[?(@.type==\"Progressing\")].status}'"
  log_cmd "ClusterVersion Progressing condition" "$cmd3"
  local prog; prog=$(eval "$cmd3" 2>&1) || true
  [[ "$prog" == "'False'" || "$prog" == "False" ]] && pass "ClusterVersion Progressing=False (no upgrade in progress)" || warn "ClusterVersion Progressing=${prog} (upgrade may be running)"
  
  local cmd4="oc get clusterversion version -o jsonpath='{.status.conditions[?(@.type==\"Degraded\")].status}{.status.conditions[?(@.type==\"Failing\")].status}'"
  log_cmd "ClusterVersion Degraded condition" "$cmd4"
  local deg; deg=$(eval "$cmd4" 2>&1) || true
  [[ "$deg" == "'False'" || "$deg" == "False" ]] && pass "ClusterVersion Degraded=False" || fail "ClusterVersion Degraded=${deg}"
}


# ─────────────────────────── 2. Cluster Operators ─────────────────────────────
check_cluster_operators() {
  section "Cluster Operators"
  local cmd="oc get clusteroperators"
  log_cmd "All Cluster Operators" "$cmd"
  local degraded; degraded=$(oc get clusteroperators --no-headers 2>/dev/null | awk '$3=="False" || $4=="True" || $5=="True"' | wc -l) || degraded=0
  local total; total=$(oc get clusteroperators --no-headers 2>/dev/null | wc -l) || total=0
  
  if [[ "$degraded" -eq 0 ]]; then
    pass "All ${total} cluster operators are healthy (Available=True, Degraded=False)"
  else
    fail "${degraded}/${total} cluster operators are degraded or unavailable"
    oc get clusteroperators --no-headers 2>/dev/null | awk '$4=="True" || $5=="False" {print " ",$0}' >> "$CLUSTER_REPORT" || true
  fi
}


# ─────────────────────────── 3. Node Status ───────────────────────────────────
check_nodes() {
  section "Node Status"
  local cmd="oc get nodes -o wide"
  log_cmd "Node list" "$cmd"
  
  local not_ready; not_ready=$(oc get nodes --no-headers 2>/dev/null | grep -v "  Ready  " | wc -l) || not_ready=0
  local total; total=$(oc get nodes --no-headers 2>/dev/null | wc -l) || total=0
  
  if [[ "$not_ready" -eq 0 ]]; then
    pass "All ${total} nodes are in Ready state"
  else
    fail "${not_ready}/${total} nodes are NOT Ready"
    oc get nodes --no-headers 2>/dev/null | grep -v "  Ready  " | awk '{print "    "$0}' >> "$CLUSTER_REPORT" || true
  fi
  
  # Node roles summary
  local masters; masters=$(oc get nodes --no-headers -l node-role.kubernetes.io/master 2>/dev/null | wc -l) || masters=0
  local workers; workers=$(oc get nodes --no-headers -l node-role.kubernetes.io/worker 2>/dev/null | wc -l) || workers=0
  info "Node count – Masters: ${masters} Workers: ${workers}"
}


# ─────────────────────────── 4. Node Resource Pressure ───────────────────────
check_node_pressure() {
  section "Node Resource Pressure (Memory / Disk / PID)"
  log_cmd "Node conditions" "oc get nodes -o json"

  local mem_pressure; mem_pressure=$(oc get nodes -o json 2>/dev/null | python3 -c "
import json,sys
data=json.load(sys.stdin)
bad=[]
for n in data.get('items',[]):
    name=n['metadata']['name']
    for c in n['status'].get('conditions',[]):
        if c['type'] in ('MemoryPressure','DiskPressure','PIDPressure') and c['status']=='True':
            bad.append(f\"{name}/{c['type']}\")
print('\n'.join(bad))
" 2>/dev/null) || mem_pressure=""

  if [[ -z "$mem_pressure" ]]; then
    pass "No MemoryPressure / DiskPressure / PIDPressure on any node"
  else
    fail "Pressure conditions detected on nodes:"
    echo "$mem_pressure" | while read -r line; do
      echo " $line" >> "$CLUSTER_REPORT"
      echo -e " ${RED}$line${RESET}"
    done
  fi
}


# ─────────────────────────── 5. Node Disk Utilization ─────────────────────────
check_node_disk() {
  section "Node Disk Utilization"
  local DISK_THRESHOLD=${DISK_THRESHOLD:-80}

  local nodes; nodes=$(oc get nodes --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null) || nodes=""
  [[ -z "$nodes" ]] && { warn "Could not retrieve node list for disk check"; return; }

  while IFS= read -r node; do
    [[ -z "$node" ]] && continue
    log_cmd "Disk usage on node ${node}" "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 core@${node} df -h"

    local df_out
    df_out=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes \
      "core@${node}" "df -h" 2>/dev/null) || df_out=""

    if [[ -z "$df_out" ]]; then
      warn "Node ${node} - Could not retrieve disk info via SSH (check key-based auth)"
      continue
    fi

    # Write full df table to report
    echo "    [Node: ${node}]" >> "$CLUSTER_REPORT"
    echo "$df_out" | awk '{print "    "$0}' >> "$CLUSTER_REPORT"
    echo "" >> "$CLUSTER_REPORT"

    local node_ok=1
    while IFS= read -r line; do
      # Skip header, tmpfs, devtmpfs
      echo "$line" | grep -qE "^Filesystem|tmpfs|devtmpfs" && continue
      local pct; pct=$(echo "$line" | awk '{print $5}' | tr -d '%') || continue
      local mnt; mnt=$(echo "$line" | awk '{print $6}')
      [[ ! "$pct" =~ ^[0-9]+$ ]] && continue
      if [[ "$pct" -ge "$DISK_THRESHOLD" ]]; then
        fail "Node ${node} - ${mnt} at ${pct}% (threshold: ${DISK_THRESHOLD}%)"
        node_ok=0
      fi
    done <<< "$df_out"

    [[ "$node_ok" -eq 1 ]] && pass "Node ${node} - All mounts below ${DISK_THRESHOLD}%"
  done <<< "$nodes"
}


# ─────────────────────────── 6. Node Memory ECC Errors ───────────────────────
check_node_ecc() {
  section "Node Memory ECC Errors"
  
  local nodes; nodes=$(oc get nodes --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null) || nodes=""
  [[ -z "$nodes" ]] && { warn "Could not retrieve node list for ECC check"; return; }
  
  local any_fail=0
  while IFS= read -r node; do
    [[ -z "$node" ]] && continue
    log_cmd "ECC errors on node ${node}" \
      "oc debug node/${node} -- chroot /host bash -c 'cat /sys/devices/system/edac/mc/mc*/ce_count 2>/dev/null; cat /sys/devices/system/edac/mc/mc*/ue_count 2>/dev/null; edac-util -s 0 2>/dev/null || true'"
	
	# Correctable Errors
    local ce_out
    ce_out=$(timeout 60 oc debug "node/${node}" -- chroot /host bash -c 'for f in /sys/devices/system/edac/mc/mc*/ce_count; do echo "$f: $(cat $f 2>/dev/null)"; done' 2>/dev/null) || ce_out=""
    
	# Uncorrectable Errors
    local ue_out
    ue_out=$(timeout 60 oc debug "node/${node}" -- chroot /host bash -c 'for f in /sys/devices/system/edac/mc/mc*/ue_count; do echo "$f: $(cat $f 2>/dev/null)"; done' 2>/dev/null) || ue_out=""
    
	# Check edac-util if available
    local edac_out
    edac_out=$(timeout 60 oc debug "node/${node}" -- chroot /host bash -c 'edac-util -s 0 2>/dev/null || echo "edac-util not available"' 2>/dev/null) || edac_out=""
    
	local node_ok=1
    
	# Parse UE counts (critical)
    if [[ -n "$ue_out" ]]; then
      while IFS= read -r line; do
        local count; count=$(echo "$line" | awk -F': ' '{print $2}')
        if [[ "$count" =~ ^[0-9]+$ ]] && [[ "$count" -gt 0 ]]; then
          fail "Node ${node} – Uncorrectable ECC errors: ${line}"
          any_fail=1; node_ok=0
        fi
      done <<< "$ue_out"
    fi
    
	# Parse CE counts (warning)
    if [[ -n "$ce_out" ]]; then
      while IFS= read -r line; do
        local count; count=$(echo "$line" | awk -F': ' '{print $2}')
        if [[ "$count" =~ ^[0-9]+$ ]] && [[ "$count" -gt 0 ]]; then
          warn "Node ${node} – Correctable ECC errors: ${line} (monitor closely)"
        fi
      done <<< "$ce_out"
    fi
    
	# edac-util summary
    if echo "$edac_out" | grep -qi "error"; then
      warn "Node ${node} – edac-util reported: $(echo "$edac_out" | head -3)"
    fi
	
    [[ "$node_ok" -eq 1 ]] && pass "Node ${node} – No uncorrectable ECC errors found"
  done <<< "$nodes"
}


# ─────────────────────────── 7. etcd Health ───────────────────────────────────
check_etcd() {
  section "etcd Health"
  local cmd="oc get pods -n openshift-etcd -l app=etcd -o wide"
  log_cmd "etcd pods" "$cmd"
  
  local not_running; not_running=$(oc get pods -n openshift-etcd -l app=etcd --no-headers 2>/dev/null | grep -v "Running" | wc -l) || not_running=0
  local total; total=$(oc get pods -n openshift-etcd -l app=etcd --no-headers 2>/dev/null | wc -l) || total=0
  
  [[ "$not_running" -eq 0 && "$total" -gt 0 ]] && pass "All ${total} etcd pods are Running" || fail "${not_running}/${total} etcd pods not in Running state"
  
  # etcd member health via etcdctl (best-effort)
  local etcd_pod; etcd_pod=$(oc get pods -n openshift-etcd -l app=etcd --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | head -1) || etcd_pod=""
  
  if [[ -n "$etcd_pod" ]]; then
    local cmd2="oc exec -n openshift-etcd ${etcd_pod} -c etcd -- etcdctl endpoint health --cluster 2>&1"
    log_cmd "etcd endpoint health" "$cmd2"
    local health_out; health_out=$(eval "$cmd2" 2>/dev/null) || health_out=""
    if echo "$health_out" | grep -q "healthy: true"; then
      pass "etcd endpoints are healthy"
    elif [[ -n "$health_out" ]]; then
      fail "etcd endpoint health issues detected"
      echo "$health_out" | head -10 | awk '{print " "$0}' >> "$CLUSTER_REPORT"
    fi
  fi
}


# ─────────────────────────── 8. Control-Plane Pods ────────────────────────────
check_controlplane_pods() {
  section "Control-Plane Critical Pods"
  local namespaces=(
    "openshift-apiserver"
    "openshift-controller-manager"
    "openshift-kube-apiserver"
    "openshift-kube-controller-manager"
    "openshift-kube-scheduler"
  )
  for ns in "${namespaces[@]}"; do
    log_cmd "Pods in ${ns}" "oc get pods -n ${ns} -o wide"
    local not_ok; not_ok=$(oc get pods -n "$ns" --no-headers 2>/dev/null | grep -Ev "Running|Completed|Succeeded" | wc -l) || not_ok=0
    local total; total=$(oc get pods -n "$ns" --no-headers 2>/dev/null | wc -l) || total=0
    [[ "$not_ok" -eq 0 && "$total" -gt 0 ]] && pass "${ns}: all ${total} pods healthy" || { [[ "$total" -eq 0 ]] && warn "${ns}: no pods found" || fail "${ns}: ${not_ok}/${total} pods not healthy"; }
  done
}


# ─────────────────────────── 9. Ceph / ODF Storage ───────────────────────────
check_ceph() {
  section "Ceph / OpenShift Data Foundation (ODF) Storage"

  # Detect ODF namespace (rook-ceph or openshift-storage)
  local ODF_NS=""
  for ns in openshift-storage rook-ceph; do
    if oc get ns "$ns" &>/dev/null; then
      ODF_NS="$ns"; break
    fi
  done

  if [[ -z "$ODF_NS" ]]; then
    warn "No Ceph/ODF namespace found (openshift-storage / rook-ceph). Skipping Ceph checks."
    return
  fi
  info "ODF namespace detected: ${ODF_NS}"

  # ── 9a. All pods in ODF namespace ──
  log_cmd "All ODF pods" "oc get pods -n ${ODF_NS} -o wide"
  local not_ok; not_ok=$(oc get pods -n "$ODF_NS" --no-headers 2>/dev/null | grep -Ev "Running|Completed|Succeeded" | wc -l) || not_ok=0
  local total; total=$(oc get pods -n "$ODF_NS" --no-headers 2>/dev/null | wc -l) || total=0
  [[ "$not_ok" -eq 0 && "$total" -gt 0 ]] && pass "All ${total} ODF pods are healthy" || fail "${not_ok}/${total} ODF pods NOT healthy"

  # ── 9b. Key Ceph component pods ──
  local components=(
    "rook-ceph-mon"
    "rook-ceph-mgr"
    "rook-ceph-osd"
    "rook-ceph-mds"
    "rook-ceph-rgw"
    "rook-ceph-crashcollector"
    "nooba"
    "csi-cephfsplugin"
    "csi-rbdplugin"
    "ocs-operator"
    "odf-operator"
  )
  for comp in "${components[@]}"; do
    local pods; pods=$(oc get pods -n "$ODF_NS" --no-headers 2>/dev/null | grep "$comp") || pods=""
    [[ -z "$pods" ]] && continue
    local bad; bad=$(echo "$pods" | grep -Ev "Running|Completed|Succeeded" | wc -l) || bad=0
    local cnt; cnt=$(echo "$pods" | wc -l)
    [[ "$bad" -eq 0 ]] && pass "${comp}: ${cnt} pod(s) Running" || fail "${comp}: ${bad}/${cnt} pods NOT Running"
  done

  # ── 9c. Ceph cluster health via toolbox ──
  local toolbox; toolbox=$(oc get pods -n "$ODF_NS" --no-headers 2>/dev/null \
    | grep -E "rook-ceph-tools|rook-ceph-toolbox" | grep Running | head -1 | awk '{print $1}') || toolbox=""

  if [[ -n "$toolbox" ]]; then
    log_cmd "ceph status" "oc exec -n ${ODF_NS} ${toolbox} -- ceph status"
    local ceph_status; ceph_status=$(oc exec -n "$ODF_NS" "$toolbox" -- ceph status 2>/dev/null) || ceph_status=""
    if echo "$ceph_status" | grep -q "HEALTH_OK"; then
      pass "Ceph cluster health: HEALTH_OK"
    elif echo "$ceph_status" | grep -q "HEALTH_WARN"; then
      warn "Ceph cluster health: HEALTH_WARN"
      echo "$ceph_status" | grep "HEALTH_WARN" | awk '{print "       "$0}' >> "$CLUSTER_REPORT"
    elif echo "$ceph_status" | grep -q "HEALTH_ERR"; then
      fail "Ceph cluster health: HEALTH_ERR"
      echo "$ceph_status" | grep -A5 "HEALTH_ERR" | awk '{print "       "$0}' >> "$CLUSTER_REPORT"
    else
      warn "Could not parse Ceph health output"
    fi

    # OSD status
    log_cmd "ceph osd status" "oc exec -n ${ODF_NS} ${toolbox} -- ceph osd status"
    local osd_out; osd_out=$(oc exec -n "$ODF_NS" "$toolbox" -- ceph osd status 2>/dev/null) || osd_out=""
    local osds_down; osds_down=$(echo "$osd_out" | grep -c "down\|out") || osds_down=0
    [[ "$osds_down" -eq 0 ]] && pass "All Ceph OSDs are up" || fail "${osds_down} Ceph OSD(s) are down/out"

    # Ceph df
    log_cmd "ceph df" "oc exec -n ${ODF_NS} ${toolbox} -- ceph df"
    local ceph_df; ceph_df=$(oc exec -n "$ODF_NS" "$toolbox" -- ceph df 2>/dev/null) || ceph_df=""
    local raw_used; raw_used=$(echo "$ceph_df" | grep -i "total used" | awk '{print $3}') || raw_used=""
    [[ -n "$raw_used" ]] && info "Ceph raw usage: ${raw_used}"

    # PG health
    log_cmd "ceph pg stat" "oc exec -n ${ODF_NS} ${toolbox} -- ceph pg stat"
    local pg_out; pg_out=$(oc exec -n "$ODF_NS" "$toolbox" -- ceph pg stat 2>/dev/null) || pg_out=""
    if echo "$pg_out" | grep -qE "degraded|incomplete|inconsistent"; then
      fail "Ceph PGs have issues: ${pg_out}"
    else
      pass "Ceph PG status looks healthy"
    fi
  else
    warn "Ceph toolbox pod not found or not Running – skipping ceph status checks"
  fi

  # ── 9d. StorageCluster CR ──
  log_cmd "StorageCluster status" "oc get storagecluster -n ${ODF_NS}"
  local sc_phase; sc_phase=$(oc get storagecluster -n "$ODF_NS" -o jsonpath='{.items[0].status.phase}' 2>/dev/null) || sc_phase=""
  [[ "$sc_phase" == "Ready" ]] && pass "StorageCluster phase: Ready" \
    || { [[ -n "$sc_phase" ]] && fail "StorageCluster phase: ${sc_phase}" || warn "StorageCluster not found or phase unknown"; }

  # ── 9e. CephBlockPool & CephFilesystem ──
  for crd in cephblockpool cephfilesystem; do
    log_cmd "${crd} status" "oc get ${crd} -n ${ODF_NS}"
    local phase; phase=$(oc get "$crd" -n "$ODF_NS" -o jsonpath='{.items[*].status.phase}' 2>/dev/null) || phase=""
    if [[ -z "$phase" ]]; then
      warn "${crd}: not found or no instances"
    elif echo "$phase" | grep -qiv "Ready\|Connected"; then
      fail "${crd} phase(s): ${phase}"
    else
      pass "${crd} phase(s): ${phase}"
    fi
  done
}


# ─────────────────────────── 10. PVC Health ───────────────────────────────────
check_pvcs() {
  section "Persistent Volume Claims (PVCs)"
  log_cmd "All PVCs" "oc get pvc -A"
  local lost; lost=$(oc get pvc -A --no-headers 2>/dev/null | grep -E "Lost|Pending" | wc -l) || lost=0
  local total; total=$(oc get pvc -A --no-headers 2>/dev/null | wc -l) || total=0
  [[ "$lost" -eq 0 ]] && pass "All ${total} PVCs are Bound" || { fail "${lost}/${total} PVCs in Lost/Pending state"
  oc get pvc -A --no-headers 2>/dev/null | grep -E "Lost|Pending" | awk '{print " "$0}' >> "$CLUSTER_REPORT" || true; }
}


# ─────────────────────────── helper: convert age string to minutes ─────────────
age_to_minutes() {
  # Handles: 5d, 3h, 10m, 2d3h, 1h30m, 45m, 10s (seconds rounded to 0)
  local age="$1"
  local d=0 h=0 m=0
  [[ "$age" =~ ([0-9]+)d ]] && d="${BASH_REMATCH[1]}"
  [[ "$age" =~ ([0-9]+)h ]] && h="${BASH_REMATCH[1]}"
  [[ "$age" =~ ([0-9]+)m ]] && m="${BASH_REMATCH[1]}"
  echo $(( d*1440 + h*60 + m ))
}

# ─────────────────────────── 11. Cluster-Wide Pod Audit ───────────────────────
check_all_pods() {
  section "Cluster-Wide Pod Audit (All Namespaces - Status / Age / Restarts)"

  local RESTART_WARN=${RESTART_WARN_THRESHOLD:-10}
  local RESTART_FAIL=${RESTART_FAIL_THRESHOLD:-50}
  local AGE_MIN_WARN=${POD_AGE_MIN_WARN:-5}    # pods younger than this (minutes) -> WARN
  local AGE_MIN_FAIL=${POD_AGE_MIN_FAIL:-2}    # pods younger than this (minutes) -> FAIL

  info "Thresholds: restarts WARN>=${RESTART_WARN} FAIL>=${RESTART_FAIL} | age WARN<${AGE_MIN_WARN}m FAIL<${AGE_MIN_FAIL}m"

  log_cmd "All pods cluster-wide" "oc get pods -A -owide"

  local pod_out; pod_out=$(oc get pods -A -owide --no-headers 2>/dev/null) || pod_out=""
  if [[ -z "$pod_out" ]]; then
    warn "Could not retrieve cluster-wide pod list"
    return
  fi

  # Save raw output to report
  echo "" >> "$CLUSTER_REPORT"
  echo " [Raw: oc get pods -A -owide]" >> "$CLUSTER_REPORT"
  oc get pods -A -owide 2>/dev/null | awk '{print " "$0}' >> "$CLUSTER_REPORT" || true
  echo "" >> "$CLUSTER_REPORT"

  local total=0 bad_status=0 r_warn=0 r_fail=0 age_warn=0 age_fail=0
  local flagged_pods=()

  while read -r ns name ready status restarts age rest; do
    [[ -z "$name" ]] && continue
    ((total++)) || true

    local flags=() severity="ok"

    # 1. STATUS — anything that is not Running/Completed/Succeeded is a problem
    if [[ "$status" != "Running" && "$status" != "Completed" && "$status" != "Succeeded" ]]; then
      flags+=("status:${status}")
      ((bad_status++)) || true
      severity="fail"
    fi

    # 2. RESTART COUNT — strip trailing annotation like "(2h ago)" that oc adds
    local rcount; rcount=$(echo "$restarts" | grep -oP '^\d+') || rcount=0
    if [[ "$rcount" =~ ^[0-9]+$ ]]; then
      if [[ "$rcount" -ge "$RESTART_FAIL" ]]; then
        flags+=("restarts:${rcount}[FAIL>=${RESTART_FAIL}]")
        ((r_fail++)) || true
        severity="fail"
      elif [[ "$rcount" -ge "$RESTART_WARN" ]]; then
        flags+=("restarts:${rcount}[WARN>=${RESTART_WARN}]")
        ((r_warn++)) || true
        [[ "$severity" == "ok" ]] && severity="warn"
      fi
    fi

    # 3. AGE — skip Completed/Succeeded (short-lived jobs are expected to be young)
    if [[ "$status" != "Completed" && "$status" != "Succeeded" ]]; then
      local age_mins; age_mins=$(age_to_minutes "$age")
      if [[ "$age_mins" -lt "$AGE_MIN_FAIL" ]]; then
        flags+=("age:${age}[FAIL<${AGE_MIN_FAIL}m]")
        ((age_fail++)) || true
        severity="fail"
      elif [[ "$age_mins" -lt "$AGE_MIN_WARN" ]]; then
        flags+=("age:${age}[WARN<${AGE_MIN_WARN}m]")
        ((age_warn++)) || true
        [[ "$severity" == "ok" ]] && severity="warn"
      fi
    fi

    if [[ "${#flags[@]}" -gt 0 ]]; then
      local flag_str; flag_str=$(IFS=', '; echo "${flags[*]}")
      flagged_pods+=("${severity}|${ns}|${name}|${age}|${rcount}|${flag_str}")
    fi
  done <<< "$pod_out"

  # ── Sub-check results ───────────────────────────────────────────────────────
  info "Total pods scanned: ${total}"

  if [[ "$bad_status" -eq 0 ]]; then
    pass "Pod Status   -> All ${total} pods are Running/Completed/Succeeded"
  else
    fail "Pod Status   -> ${bad_status}/${total} pods in error state"
  fi

  if [[ "$r_fail" -gt 0 ]]; then
    fail "Pod Restarts -> ${r_fail} pod(s) with CRITICAL restart count (>=${RESTART_FAIL})"
  elif [[ "$r_warn" -gt 0 ]]; then
    warn "Pod Restarts -> ${r_warn} pod(s) with HIGH restart count (>=${RESTART_WARN})"
  else
    pass "Pod Restarts -> All pods below restart warn threshold (${RESTART_WARN})"
  fi

  if [[ "$age_fail" -gt 0 ]]; then
    fail "Pod Age      -> ${age_fail} pod(s) younger than ${AGE_MIN_FAIL}m (likely just restarted)"
  elif [[ "$age_warn" -gt 0 ]]; then
    warn "Pod Age      -> ${age_warn} pod(s) younger than ${AGE_MIN_WARN}m (recently started)"
  else
    pass "Pod Age      -> All pods older than ${AGE_MIN_WARN}m"
  fi

  # ── Flagged pods detail table ───────────────────────────────────────────────
  if [[ "${#flagged_pods[@]}" -gt 0 ]]; then
    echo "" >> "$CLUSTER_REPORT"
    echo " +- Flagged Pods -------------------------------------------------------------------" >> "$CLUSTER_REPORT"
    printf " | %-6s  %-36s  %-28s  %-6s  %-8s  %-s\n" \
      "SEV" "NAMESPACE/POD" "FLAGS" "AGE" "RESTARTS" "" >> "$CLUSTER_REPORT"
    echo " +----------------------------------------------------------------------------------" >> "$CLUSTER_REPORT"

    for entry in "${flagged_pods[@]}"; do
      IFS='|' read -r sev fns fpod fage frcount fflags <<< "$entry"
      local marker
      case "$sev" in
        fail) marker="[FAIL]" ;;
        warn) marker="[WARN]" ;;
        *)    marker="[INFO]" ;;
      esac
      printf " | %-6s  %-36s  %-28s  %-6s  %-8s\n" \
        "$marker" "${fns}/${fpod}" "$fflags" "$fage" "$frcount" >> "$CLUSTER_REPORT"
    done

    echo " +----------------------------------------------------------------------------------" >> "$CLUSTER_REPORT"
    echo "" >> "$CLUSTER_REPORT"

    # Console output — capped at 30
    echo -e "\n ${BOLD}Flagged pods (first 30):${RESET}"
    local shown=0
    for entry in "${flagged_pods[@]}"; do
      [[ "$shown" -ge 30 ]] && { echo -e " ... and more — see report for full list."; break; }
      IFS='|' read -r sev fns fpod fage frcount fflags <<< "$entry"
      case "$sev" in
        fail) echo -e "  ${RED}[FAIL]${RESET} ${fns}/${fpod}  age=${fage}  restarts=${frcount}  -> ${fflags}" ;;
        warn) echo -e "  ${YELLOW}[WARN]${RESET} ${fns}/${fpod}  age=${fage}  restarts=${frcount}  -> ${fflags}" ;;
        *)    echo -e "  ${CYAN}[INFO]${RESET} ${fns}/${fpod}  age=${fage}  restarts=${frcount}  -> ${fflags}" ;;
      esac
      ((shown++)) || true
    done
  else
    info "No flagged pods across all ${total} pods"
  fi

  # ── CSV inventory ───────────────────────────────────────────────────────────
  local POD_CSV="${OUTPUT_DIR}/${CLUSTER_NAME//[^a-zA-Z0-9._-]/_}_pods_inventory.csv"
  echo "namespace,pod_name,status,restarts,age,flags" > "$POD_CSV"
  for entry in "${flagged_pods[@]}"; do
    IFS='|' read -r sev fns fpod fage frcount fflags <<< "$entry"
    echo "\"${fns}\",\"${fpod}\",\"${sev}\",${frcount},\"${fage}\",\"${fflags}\""
  done >> "$POD_CSV"
  info "Flagged pods CSV: ${POD_CSV}"
}

# ─────────────────────────── 12. Warning Events ───────────────────────────────
check_events() {
  section "Cluster Warning Events (last 1h)"
  log_cmd "Warning events" "oc get events -A --field-selector type=Warning"
  local warn_count; warn_count=$(oc get events -A --field-selector type=Warning --no-headers 2>/dev/null | wc -l) || warn_count=0
  
  if [[ "$warn_count" -eq 0 ]]; then
    pass "No Warning events found across all namespaces"
  else
    warn "${warn_count} Warning event(s) found (top 20 shown in report)"
    oc get events -A --field-selector type=Warning --no-headers 2>/dev/null | head -20 | awk '{print "    "$0}' >> "$CLUSTER_REPORT" || true
  fi
}


# ─────────────────────────── 13. Certificate Expiry ──────────────────────────
check_certificates() {
  section "API Server Certificate Expiry"
  log_cmd "API server secrets (tls)" "oc get secret -n openshift-kube-apiserver -o json | python3 -c 'import json,sys,base64,subprocess; ...'"

  local warn_days=30
  local any_expiring=0
  while IFS= read -r line; do
    local secret_name ns expiry_date expiry_ts now_ts days_left
    ns=$(echo "$line" | awk '{print $1}')
    secret_name=$(echo "$line" | awk '{print $2}')
	
    local cert_data
    cert_data=$(oc get secret -n "$ns" "$secret_name" -o jsonpath='{.data.tls\.crt}' 2>/dev/null | base64 -d 2>/dev/null) || cert_data=""
    [[ -z "$cert_data" ]] && continue
	
    expiry_date=$(echo "$cert_data" | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2) || expiry_date=""
    [[ -z "$expiry_date" ]] && continue
	
    expiry_ts=$(date -d "$expiry_date" +%s 2>/dev/null) || continue
    now_ts=$(date +%s)
    days_left=$(( (expiry_ts - now_ts) / 86400 ))
	
    if [[ "$days_left" -lt 0 ]]; then
      fail "Certificate EXPIRED: ${ns}/${secret_name} (expired ${days_left#-} days ago)"
      any_expiring=1
    elif [[ "$days_left" -lt "$warn_days" ]]; then
      warn "Certificate expiring soon: ${ns}/${secret_name} – ${days_left} days remaining"
      any_expiring=1
    fi
  done < <(oc get secret -A --field-selector type=kubernetes.io/tls --no-headers 2>/dev/null | awk '{print $1,$2}' | head -50)
  
  [[ "$any_expiring" -eq 0 ]] && pass "No certificates expiring within ${warn_days} days (checked top 50 TLS secrets)"
  log_cmd "TLS secrets list" "oc get secret -A --field-selector type=kubernetes.io/tls"
}


# ─────────────────────────── 14. MachineConfigPool ───────────────────────────
check_mcp() {
  section "MachineConfigPool Status"
  log_cmd "MachineConfigPools" "oc get mcp"
  local degraded; degraded=$(oc get mcp --no-headers 2>/dev/null | awk '$4=="True" || $5=="True"' | wc -l) || degraded=0
  local total; total=$(oc get mcp --no-headers 2>/dev/null | wc -l) || total=0
  [[ "$degraded" -eq 0 ]] && pass "All ${total} MachineConfigPools healthy (not degraded / not updating)" || fail "${degraded}/${total} MCPs are Degraded or Updating"
}


# ─────────────────────────── 15. Alertmanager / Firing Alerts ─────────────────
check_alerts() {
  section "Prometheus Firing Alerts"
  log_cmd "Firing alerts" "oc get prometheusrule -A"
  
  local alertmanager_pod; alertmanager_pod=$(oc get pods -n openshift-monitoring --no-headers 2>/dev/null | grep "alertmanager-main" | grep Running | head -1 | awk '{print $1}') || alertmanager_pod=""
  
  if [[ -n "$alertmanager_pod" ]]; then
    local cmd="oc exec -n openshift-monitoring ${alertmanager_pod} -c alertmanager -- amtool --alertmanager.url=http://127.0.0.1:9093 alert -o json 2>/dev/null"
    log_cmd "Active alerts via amtool" "$cmd"
    local alerts; alerts=$(eval "$cmd" 2>/dev/null | python3 -c "import json,sys; a=json.load(sys.stdin); print(len(a),'alerts firing')" 2>/dev/null) || alerts=""
    [[ -n "$alerts" ]] && info "Alertmanager: ${alerts}" || warn "Could not query alertmanager"
  else
    warn "alertmanager-main pod not found or not Running"
  fi
  
  # Check for Critical alerts via Thanos/Prometheus API
  local thanos_pod; thanos_pod=$(oc get pods -n openshift-monitoring --no-headers 2>/dev/null | grep "thanos-querier" | grep Running | head -1 | awk '{print $1}') || thanos_pod=""
  if [[ -n "$thanos_pod" ]]; then
    local critical_cmd="oc exec -n openshift-monitoring ${thanos_pod} -c thanos-query -- wget -qO- 'http://localhost:9090/api/v1/alerts' 2>/dev/null"
    log_cmd "Critical alerts via Thanos" "$critical_cmd"
    local crit; crit=$(eval "$critical_cmd" 2>/dev/null | python3 -c "import json,sys
try:
  d=json.load(sys.stdin)
  alerts=[a for a in d.get('data',{}).get('alerts',[]) if a.get('labels',{}).get('severity')=='critical' and a.get('state')=='firing']
  print(len(alerts))
except: 
  print(-1)" 2>/dev/null) || crit=-1
    if [[ "$crit" -eq 0 ]]; then
      pass "No critical alerts firing"
    elif [[ "$crit" -gt 0 ]]; then
      fail "${crit} critical alert(s) currently firing"
    else
      warn "Could not determine number of firing alerts"
    fi
  fi
}


# ─────────────────────────── MAIN PER-CLUSTER FUNCTION ────────────────────────
run_cluster_checks() {
  local login_cmd="$1"
  CLUSTER_NAME="$(echo "$login_cmd" | grep -oP 'https?://\S+' | head -1 | sed 's|https://||;s|/.*||')"
  [[ -z "$CLUSTER_NAME" ]] && CLUSTER_NAME="cluster_$(date +%s)"
  
  CLUSTER_REPORT="${OUTPUT_DIR}/${CLUSTER_NAME//[^a-zA-Z0-9._-]/_}_healthcheck.txt"
  local CLUSTER_PASS=0 CLUSTER_FAIL=0
  
  echo ""
  echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════${RESET}"
  echo -e "${BOLD}${CYAN} Cluster: ${CLUSTER_NAME}${RESET}"
  echo -e "${BOLD}${CYAN}════════════════════════════════════════════════════${RESET}"
  
  {
    echo "════════════════════════════════════════════════════"
    echo " OCP Health Check Report"
    echo " Cluster : ${CLUSTER_NAME}"
    echo " Date : $(date)"
    echo "════════════════════════════════════════════════════"
  } > "$CLUSTER_REPORT"
  
  # ── Login ──────────────────────────────────────────────────────────────────
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] LOGIN: ${login_cmd}" >> "$GLOBAL_LOG"
  if eval "$login_cmd" >> "$GLOBAL_LOG" 2>&1; then
    echo -e " ${GREEN}[PASS]${RESET} Login successful"
    echo " [PASS] Login to ${CLUSTER_NAME} successful" >> "$CLUSTER_REPORT"
  else
    echo -e " ${RED}[FAIL]${RESET} Login FAILED – skipping cluster"
    echo " [FAIL] Login to ${CLUSTER_NAME} FAILED – all checks skipped" >> "$CLUSTER_REPORT"
    echo "" >> "$SUMMARY_FILE"
    echo "CLUSTER: ${CLUSTER_NAME} -> LOGIN FAILED" >> "$SUMMARY_FILE"
    return
  fi
  
  # ── Run all checks ─────────────────────────────────────────────────────────
  check_cluster_version
  check_cluster_operators
  check_nodes
  check_node_pressure
  check_node_disk
  check_node_ecc
  check_etcd
  check_controlplane_pods
  check_ceph
  check_pvcs
  check_all_pods
  check_events
  check_certificates
  check_mcp
  check_alerts
  
  # ── Logout ─────────────────────────────────────────────────────────────────
  log_cmd "Logout" "oc logout"
  oc logout >> "$GLOBAL_LOG" 2>&1 || true
  
  # ── Cluster summary ────────────────────────────────────────────────────────
  local c_pass; c_pass=$(grep -c "^\s*\[PASS\]" "$CLUSTER_REPORT") || c_pass=0
  local c_fail; c_fail=$(grep -c "^\s*\[FAIL\]" "$CLUSTER_REPORT") || c_fail=0
  local c_warn; c_warn=$(grep -c "^\s*\[WARN\]" "$CLUSTER_REPORT") || c_warn=0
  
  {
    echo ""
    echo "════════════════════════════════════════════════════"
    echo " CLUSTER SUMMARY"
    echo " PASS : ${c_pass}"
    echo " FAIL : ${c_fail}"
    echo " WARN : ${c_warn}"
    echo "════════════════════════════════════════════════════"
  } >> "$CLUSTER_REPORT"
  
  echo ""
  # echo -e " Cluster summary -> ${GREEN}PASS: ${c_pass}${RESET}  ${RED}FAIL: ${c_fail}${RESET}  ${YELLOW}WARN: ${c_warn}${RESET}"
  echo " Report saved: ${CLUSTER_REPORT}"
  
  # Append to global summary
  printf "%-40s ${GREEN}PASS${RESET}: %-5s ${RED}FAIL${RESET}: %-5s ${YELLOW}WARN${RESET}: %-5s\n" "${CLUSTER_NAME}" "${c_pass}" "${c_fail}" "${c_warn}" >> "$SUMMARY_FILE"
  
  PASS=$((PASS + c_pass)); FAIL=$((FAIL + c_fail))
}


# =============================================================================
# ENTRY POINT
# =============================================================================

start_time="$(date +%d/%m/%Y_%H:%M:%S)"

{
  echo ""
  printf "%-40s ${GREEN}PASS        ${RESET}${RED}FAIL        ${RESET}${YELLOW}WARN${RESET}\n" "CLUSTER"
  printf '%.0s─' {1..80}; echo
} > "$SUMMARY_FILE"

echo ""
echo -e "${BOLD}OCP Health Check started at $start_time${RESET}"
echo -e "Output directory : ${OUTPUT_DIR}"
echo -e "Global log : ${GLOBAL_LOG}"

CLUSTER_COUNT=0
while IFS= read -r line || [[ -n "$line" ]]; do
  # Skip blank lines and comments
  [[ -z "${line// /}" || "$line" =~ ^# ]] && continue
  # Only process lines that start with 'oc login'
  [[ "$line" != oc\ login* ]] && { echo -e "${YELLOW} Skipping non-login line:${RESET} $line"; continue; }
  
  ((CLUSTER_COUNT++)) || true
  run_cluster_checks "$line"
done < "$LOGIN_FILE"

{
  printf '%.0s─' {1..80}; echo
} >> "$SUMMARY_FILE"

echo ""
echo ""
echo -e "${BOLD}══════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD} OCP Health Check – Summary${RESET}"
echo -e ""
printf "%-15s : Complete\n" " Health Check"
printf "%-15s : ${CLUSTER_COUNT}\n" " Total Clusters"
printf "%-15s : ${LOGIN_FILE}\n" " Input file"
printf "%-15s : $start_time\n" " Started Time"
printf "%-15s : $(date +%d/%m/%Y_%H:%M:%S)\n" " Finished Time"
echo -e ""
printf "%-15s : ${SUMMARY_FILE}\n" " Summary file"
printf "%-15s : ${GLOBAL_LOG}\n" " Command log"
echo -e "${BOLD}══════════════════════════════════════════════════════════════${RESET}"
echo ""
cat "$SUMMARY_FILE"
