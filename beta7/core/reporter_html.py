"""
Premium HTML Report Generator for CloudHealth.
Design: Refined industrial — charcoal + amber accent, monospaced precision.
Dual-mode: interactive browser report + email-safe inline-style version.
Filters work correctly: only show matching items, not just highlight them.
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from core.result import ClusterResult, SectionResult, Status
Section = SectionResult


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))

_STATUS_COLOR = {
    Status.PASS:  "#22c55e",
    Status.FAIL:  "#ef4444",
    Status.WARN:  "#f59e0b",
    Status.INFO:  "#60a5fa",
    Status.SKIP:  "#6b7280",
    Status.ERROR: "#ef4444",
}
_STATUS_BG = {
    Status.PASS:  "rgba(34,197,94,.12)",
    Status.FAIL:  "rgba(239,68,68,.14)",
    Status.WARN:  "rgba(245,158,11,.12)",
    Status.INFO:  "rgba(96,165,250,.10)",
    Status.SKIP:  "rgba(107,114,128,.10)",
    Status.ERROR: "rgba(239,68,68,.14)",
}
_STATUS_ICON = {
    Status.PASS:  "✓",
    Status.FAIL:  "✕",
    Status.WARN:  "⚠",
    Status.INFO:  "○",
    Status.SKIP:  "–",
    Status.ERROR: "✕",
}
_STATUS_LABEL = {
    Status.PASS:  "PASS",
    Status.FAIL:  "FAIL",
    Status.WARN:  "WARN",
    Status.INFO:  "INFO",
    Status.SKIP:  "SKIP",
    Status.ERROR: "ERROR",
}


def _badge_inline(status: Status, small: bool = False) -> str:
    """Inline-styled badge for email compatibility."""
    c  = _STATUS_COLOR[status]
    bg = _STATUS_BG[status]
    ic = _STATUS_ICON[status]
    lb = _STATUS_LABEL[status]
    fs = "10px" if small else "11px"
    pad = "2px 7px" if small else "3px 10px"
    return (
        f'<span style="display:inline-flex;align-items:center;gap:4px;'
        f'background:{bg};color:{c};border:1px solid {c}40;'
        f'border-radius:4px;padding:{pad};font-size:{fs};font-weight:700;'
        f'font-family:\'JetBrains Mono\',\'Fira Code\',monospace;'
        f'white-space:nowrap;letter-spacing:.4px">'
        f'{ic} {lb}</span>'
    )


# ══════════════════════════════════════════════════════════════════════════════
#  CSS (browser interactive version)
# ══════════════════════════════════════════════════════════════════════════════

_CSS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap');

:root {
  --bg0:    #0f1117;
  --bg1:    #171923;
  --bg2:    #1e2333;
  --bg3:    #252d40;
  --bg4:    #2d3654;
  --border: #2a3350;
  --border2:#354060;
  --text:   #e8edf8;
  --muted:  #7a8aaa;
  --dim:    #4a5570;
  --pass:   #22c55e;
  --fail:   #ef4444;
  --warn:   #f59e0b;
  --info:   #60a5fa;
  --skip:   #6b7280;
  --amber:  #f59e0b;
  --mono:   'IBM Plex Mono', 'JetBrains Mono', 'Fira Code', monospace;
  --sans:   'IBM Plex Sans', system-ui, sans-serif;
  --radius: 6px;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: var(--sans);
  background: var(--bg0);
  color: var(--text);
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

/* ─── HEADER ──────────────────────────────────────────────────────────────── */
.cp-header {
  background: var(--bg1);
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 200;
}
.cp-header-inner {
  max-width: 1400px; margin: 0 auto;
  padding: 0 32px;
  display: flex; align-items: stretch; gap: 0;
}
.cp-logo {
  display: flex; align-items: center; gap: 12px;
  padding: 18px 32px 18px 0;
  border-right: 1px solid var(--border);
  flex-shrink: 0;
}
.cp-logo-icon {
  width: 36px; height: 36px;
  background: linear-gradient(135deg, #f59e0b, #ef4444);
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  font-size: 18px; line-height: 1;
}
.cp-logo-text {
  font-family: var(--mono);
  font-size: 16px; font-weight: 600;
  letter-spacing: -.3px;
  color: var(--text);
}
.cp-logo-sub { font-size: 10px; color: var(--muted); letter-spacing: .5px; text-transform: uppercase; }

.cp-meta {
  display: flex; align-items: center; gap: 28px;
  padding: 18px 32px;
  flex: 1;
}
.cp-meta-item { display: flex; flex-direction: column; gap: 2px; }
.cp-meta-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .6px; }
.cp-meta-value { font-family: var(--mono); font-size: 13px; font-weight: 500; }

.cp-overall {
  display: flex; align-items: center;
  padding: 18px 0 18px 28px;
  margin-left: auto;
  border-left: 1px solid var(--border);
  padding-left: 32px;
  gap: 12px;
  flex-shrink: 0;
}
.cp-overall-badge {
  font-family: var(--mono);
  font-size: 13px; font-weight: 700;
  padding: 6px 18px;
  border-radius: 4px;
  letter-spacing: .8px;
}
.cp-overall-pass { background: rgba(34,197,94,.15); color: var(--pass); border: 1px solid rgba(34,197,94,.4); }
.cp-overall-fail { background: rgba(239,68,68,.15); color: var(--fail); border: 1px solid rgba(239,68,68,.4); }
.cp-overall-warn { background: rgba(245,158,11,.15); color: var(--warn); border: 1px solid rgba(245,158,11,.4); }
.cp-overall-error{ background: rgba(239,68,68,.15); color: var(--fail); border: 1px solid rgba(239,68,68,.4); }

/* ─── SCOREBOARD ──────────────────────────────────────────────────────────── */
.cp-scoreboard {
  max-width: 1400px; margin: 0 auto;
  padding: 24px 32px 0;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}
.cp-score-card {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 20px;
  position: relative; overflow: hidden;
}
.cp-score-card::before {
  content: '';
  position: absolute; top: 0; left: 0; right: 0; height: 2px;
}
.cp-score-card.sc-pass::before  { background: var(--pass); }
.cp-score-card.sc-fail::before  { background: var(--fail); }
.cp-score-card.sc-warn::before  { background: var(--warn); }
.cp-score-card.sc-info::before  { background: var(--info); }
.cp-score-card.sc-neutral::before { background: var(--dim); }
.cp-score-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .6px; margin-bottom: 6px; }
.cp-score-value { font-family: var(--mono); font-size: 28px; font-weight: 600; line-height: 1; }
.cp-score-card.sc-pass .cp-score-value  { color: var(--pass); }
.cp-score-card.sc-fail .cp-score-value  { color: var(--fail); }
.cp-score-card.sc-warn .cp-score-value  { color: var(--warn); }
.cp-score-card.sc-info .cp-score-value  { color: var(--info); }
.cp-score-card.sc-neutral .cp-score-value { color: var(--text); }
.cp-score-sub { font-size: 11px; color: var(--muted); margin-top: 4px; }

/* ─── TOOLBAR ─────────────────────────────────────────────────────────────── */
.cp-toolbar {
  max-width: 1400px; margin: 0 auto;
  padding: 20px 32px 16px;
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
}
.cp-toolbar-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-right: 4px; }

.cp-filter-btn {
  font-family: var(--mono);
  font-size: 11px; font-weight: 600; letter-spacing: .4px;
  padding: 5px 14px;
  border-radius: 20px;
  border: 1px solid var(--border2);
  background: var(--bg2);
  color: var(--muted);
  cursor: pointer; transition: all .15s;
  display: inline-flex; align-items: center; gap: 6px;
}
.cp-filter-btn:hover { border-color: var(--amber); color: var(--amber); }
.cp-filter-btn.active { background: var(--amber); border-color: var(--amber); color: #0f1117; }
.cp-filter-btn .dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: currentColor;
}

.cp-action-btn {
  font-family: var(--mono);
  font-size: 11px; padding: 5px 13px;
  border-radius: 4px;
  border: 1px solid var(--border2);
  background: transparent; color: var(--muted);
  cursor: pointer; transition: all .15s;
}
.cp-action-btn:hover { border-color: var(--info); color: var(--info); }

.cp-search {
  font-family: var(--mono);
  font-size: 12px; padding: 6px 14px;
  background: var(--bg2); border: 1px solid var(--border2);
  border-radius: 4px; color: var(--text);
  width: 260px; outline: none;
  margin-left: auto;
  transition: border-color .15s;
}
.cp-search:focus { border-color: var(--info); }
.cp-search::placeholder { color: var(--dim); }

/* ─── MAIN CONTENT ────────────────────────────────────────────────────────── */
.cp-content {
  max-width: 1400px; margin: 0 auto;
  padding: 0 32px 60px;
}

/* ─── CLUSTER CARD ────────────────────────────────────────────────────────── */
.cp-cluster {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 10px;
  overflow: hidden;
  transition: border-color .2s;
}
.cp-cluster:hover { border-color: var(--border2); }

.cp-cluster-header {
  display: flex; align-items: center; gap: 12px;
  padding: 14px 18px;
  background: var(--bg1);
  cursor: pointer;
  list-style: none;
  transition: background .15s;
  user-select: none;
}
.cp-cluster-header::-webkit-details-marker { display: none; }
.cp-cluster-header::before {
  content: "▶";
  font-size: 9px; color: var(--dim);
  transition: transform .2s; flex-shrink: 0;
}
details[open] > .cp-cluster-header::before { transform: rotate(90deg); }
.cp-cluster-header:hover { background: var(--bg2); }

.cp-cluster-type {
  font-family: var(--mono); font-size: 10px; font-weight: 600;
  letter-spacing: 1.5px; text-transform: uppercase;
  padding: 2px 8px; border-radius: 3px;
  background: var(--bg3); color: var(--info);
  border: 1px solid var(--border2);
}
.cp-cluster-name {
  font-size: 14px; font-weight: 600;
  letter-spacing: -.2px;
}
.cp-cluster-env {
  font-size: 11px; color: var(--muted);
  font-style: italic;
}
.cp-cluster-counts {
  margin-left: auto;
  display: flex; align-items: center; gap: 16px;
}
.cp-cnt {
  font-family: var(--mono); font-size: 12px; font-weight: 600;
  display: flex; align-items: center; gap: 4px;
}
.cp-cnt.pass { color: var(--pass); }
.cp-cnt.fail { color: var(--fail); }
.cp-cnt.warn { color: var(--warn); }
.cp-dur { font-family: var(--mono); font-size: 11px; color: var(--dim); }
.cp-cluster-ts { font-family: var(--mono); font-size: 10px; color: var(--dim); }

.cp-cluster-body {
  background: var(--bg0);
  padding: 10px 14px 14px;
}

/* ─── SECTION ─────────────────────────────────────────────────────────────── */
.cp-section {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 6px;
  overflow: hidden;
}
/* left accent bar based on worst status */
.cp-section.ws-FAIL  { border-left: 3px solid var(--fail); }
.cp-section.ws-ERROR { border-left: 3px solid var(--fail); }
.cp-section.ws-WARN  { border-left: 3px solid var(--warn); }
.cp-section.ws-PASS  { border-left: 3px solid var(--pass); }
.cp-section.ws-INFO  { border-left: 3px solid var(--info); }
.cp-section.ws-SKIP  { border-left: 3px solid var(--dim);  }

.cp-section-header {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 14px;
  background: var(--bg1);
  cursor: pointer; list-style: none;
  transition: background .15s;
  user-select: none;
}
.cp-section-header::-webkit-details-marker { display: none; }
.cp-section-header::before {
  content: "▶"; font-size: 8px; color: var(--dim);
  transition: transform .2s; flex-shrink: 0;
}
details[open] > .cp-section-header::before { transform: rotate(90deg); }
.cp-section-header:hover { background: var(--bg2); }

.cp-section-name {
  font-family: var(--mono); font-size: 12px; font-weight: 500;
  letter-spacing: .2px;
}
.cp-section-counts {
  margin-left: auto; display: flex; gap: 12px; align-items: center;
  font-family: var(--mono); font-size: 11px; font-weight: 600;
}
.cp-section-dur { font-family: var(--mono); font-size: 10px; color: var(--dim); }

.cp-section-body { padding: 10px 14px 14px; background: var(--bg0); }

/* ─── CHECK ITEMS ─────────────────────────────────────────────────────────── */
.cp-items { list-style: none; display: flex; flex-direction: column; gap: 3px; }

.cp-item {
  display: flex; flex-direction: column; gap: 4px;
  padding: 7px 12px;
  border-radius: 4px;
  border: 1px solid transparent;
  font-size: 13px;
  transition: background .1s;
}
.cp-item:hover { background: var(--bg2) !important; }

.cp-item.st-PASS  { background: rgba(34,197,94,.06);  border-color: rgba(34,197,94,.15); }
.cp-item.st-FAIL  { background: rgba(239,68,68,.08);  border-color: rgba(239,68,68,.25); }
.cp-item.st-ERROR { background: rgba(239,68,68,.08);  border-color: rgba(239,68,68,.25); }
.cp-item.st-WARN  { background: rgba(245,158,11,.07); border-color: rgba(245,158,11,.22); }
.cp-item.st-INFO  { background: rgba(96,165,250,.06); border-color: rgba(96,165,250,.15); }
.cp-item.st-SKIP  { background: rgba(107,114,128,.06);border-color: rgba(107,114,128,.15); opacity:.7; }

.cp-item-row { display: flex; align-items: flex-start; gap: 10px; }
.cp-item-msg { flex: 1; line-height: 1.45; color: var(--text); }

.cp-item-cmd {
  font-family: var(--mono); font-size: 10px; color: var(--info);
  opacity: .65; margin-top: 2px;
}
.cp-item-cmd::before { content: "$ "; }

.cp-item-detail {
  font-family: var(--mono); font-size: 11px; color: var(--muted);
  background: var(--bg1); border: 1px solid var(--border);
  padding: 8px 12px; border-radius: 4px;
  white-space: pre; overflow-x: auto;
  max-height: 280px; overflow-y: auto;
  margin-top: 4px; line-height: 1.5;
}

/* ─── RAW LOG ─────────────────────────────────────────────────────────────── */
.cp-raw { margin-top: 10px; }
.cp-raw summary {
  font-family: var(--mono); font-size: 11px; color: var(--dim);
  cursor: pointer; list-style: none; padding: 4px 6px;
  display: inline-flex; align-items: center; gap: 6px;
  border-radius: 4px; transition: color .15s;
}
.cp-raw summary::-webkit-details-marker { display: none; }
.cp-raw summary:hover { color: var(--muted); }
.cp-raw pre {
  font-family: var(--mono); font-size: 11px; color: #9ab;
  background: var(--bg1); border: 1px solid var(--border);
  padding: 12px 14px; border-radius: 4px; margin-top: 6px;
  white-space: pre; overflow-x: auto;
  max-height: 500px; overflow-y: auto; line-height: 1.55;
}

/* ─── LOGIN ERROR ─────────────────────────────────────────────────────────── */
.cp-login-err {
  padding: 14px 18px;
  font-family: var(--mono); font-size: 12px;
  color: var(--fail); background: rgba(239,68,68,.08);
}

/* ─── BADGE ───────────────────────────────────────────────────────────────── */
.badge {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 9px; border-radius: 4px;
  font-family: var(--mono); font-size: 11px; font-weight: 700;
  white-space: nowrap; letter-spacing: .4px; border: 1px solid;
}
.badge-PASS  { color: var(--pass); background: rgba(34,197,94,.12);  border-color: rgba(34,197,94,.35); }
.badge-FAIL  { color: var(--fail); background: rgba(239,68,68,.12);  border-color: rgba(239,68,68,.35); }
.badge-ERROR { color: var(--fail); background: rgba(239,68,68,.12);  border-color: rgba(239,68,68,.35); }
.badge-WARN  { color: var(--warn); background: rgba(245,158,11,.12); border-color: rgba(245,158,11,.35); }
.badge-INFO  { color: var(--info); background: rgba(96,165,250,.10); border-color: rgba(96,165,250,.30); }
.badge-SKIP  { color: var(--skip); background: rgba(107,114,128,.10);border-color: rgba(107,114,128,.3); }

/* ─── SCROLLBAR ───────────────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg1); }
::-webkit-scrollbar-thumb { background: var(--bg4); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--dim); }

/* ─── UTILITY ─────────────────────────────────────────────────────────────── */
.hidden { display: none !important; }

/* ─── PRINT / EMAIL styles ────────────────────────────────────────────────── */
@media print {
  .cp-header { position: static; }
  .cp-toolbar { display: none; }
  .cp-search { display: none; }
  details { open: true; }
  details[open] .cp-section-body,
  details[open] .cp-cluster-body { display: block; }
}
"""

# ══════════════════════════════════════════════════════════════════════════════
#  JS (filter logic — correct: hides non-matching items entirely)
# ══════════════════════════════════════════════════════════════════════════════

_JS = """
'use strict';

// ── filter state ──────────────────────────────────────────────────────────
let activeFilter = 'all';
let searchQuery  = '';

function applyFilters() {
  const clusters = document.querySelectorAll('.cp-cluster');

  clusters.forEach(cluster => {
    let clusterVisible = false;
    const sections = cluster.querySelectorAll('.cp-section');

    sections.forEach(section => {
      let sectionVisible = false;
      const items = section.querySelectorAll('.cp-item');

      items.forEach(item => {
        const st = item.dataset.status || '';
        const txt = item.textContent.toLowerCase();

        // status filter
        let statusOk = (activeFilter === 'all') ||
          (activeFilter === 'fail' && (st === 'FAIL' || st === 'ERROR')) ||
          (activeFilter === 'warn' && (st === 'FAIL' || st === 'ERROR' || st === 'WARN')) ||
          (activeFilter === 'pass' && st === 'PASS') ||
          (activeFilter === 'info' && (st === 'INFO' || st === 'SKIP'));

        // search filter
        let searchOk = !searchQuery || txt.includes(searchQuery);

        if (statusOk && searchOk) {
          item.classList.remove('hidden');
          sectionVisible = true;
        } else {
          item.classList.add('hidden');
        }
      });

      // also check section name / raw log for search
      if (!sectionVisible && searchQuery) {
        const sname = (section.querySelector('.cp-section-name') || {}).textContent || '';
        if (sname.toLowerCase().includes(searchQuery)) sectionVisible = true;
      }

      if (sectionVisible) {
        section.classList.remove('hidden');
        clusterVisible = true;
        // auto-open sections with visible failing items
        if (activeFilter !== 'all' || searchQuery) {
          section.open = true;
        }
      } else {
        section.classList.add('hidden');
      }
    });

    // cluster-level login-error row
    const loginErr = cluster.querySelector('.cp-login-err');
    if (loginErr) {
      const txt = cluster.textContent.toLowerCase();
      clusterVisible = (!searchQuery || txt.includes(searchQuery));
    }

    if (clusterVisible) {
      cluster.classList.remove('hidden');
      if (activeFilter !== 'all' || searchQuery) cluster.open = true;
    } else {
      cluster.classList.add('hidden');
    }
  });
}

function setFilter(mode, btn) {
  activeFilter = mode;
  document.querySelectorAll('.cp-filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  applyFilters();
}

function doSearch(val) {
  searchQuery = val.toLowerCase().trim();
  applyFilters();
}

function expandAll() {
  document.querySelectorAll('details').forEach(d => d.open = true);
}
function collapseAll() {
  document.querySelectorAll('details').forEach(d => d.open = false);
}

// open clusters with failures by default
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.cp-cluster').forEach(cluster => {
    const hasFail = cluster.querySelector('.cp-item.st-FAIL, .cp-item.st-ERROR');
    if (hasFail) { cluster.open = true; }
  });
});
"""

# ══════════════════════════════════════════════════════════════════════════════
#  HTML builder helpers
# ══════════════════════════════════════════════════════════════════════════════

def _badge(status: Status) -> str:
    ic = _STATUS_ICON[status]
    lb = _STATUS_LABEL[status]
    return f'<span class="badge badge-{status.value}">{ic} {lb}</span>'


def _render_item(item, idx: int, section_id: str,
                  diff_marker: Optional[str] = None) -> str:
    st    = item.status
    cls   = f"cp-item st-{st.value}"
    badge = _badge(st)
    msg   = _esc(item.message)

    diff_badge = ""
    extra_cls  = ""
    if diff_marker == "NEW":
        diff_badge = ('<span style="margin-left:6px;padding:1px 7px;font-size:.65rem;'
                      'font-weight:700;border-radius:3px;background:rgba(239,68,68,.18);'
                      'color:#ef4444;border:1px solid #ef444460;letter-spacing:.05em">NEW</span>')
        extra_cls  = " diff-new"
    elif diff_marker == "RESOLVED":
        diff_badge = ('<span style="margin-left:6px;padding:1px 7px;font-size:.65rem;'
                      'font-weight:700;border-radius:3px;background:rgba(34,197,94,.15);'
                      'color:#22c55e;border:1px solid #22c55e60;letter-spacing:.05em">'
                      'RESOLVED</span>')
        extra_cls  = " diff-resolved"
        msg        = f'<span style="text-decoration:line-through;opacity:.6">{msg}</span>'

    cmd_html = ""
    if item.command:
        cmd_html = f'<div class="cp-item-cmd">{_esc(item.command)}</div>'

    detail_html = ""
    if item.detail and item.detail.strip():
        detail_html = f'<pre class="cp-item-detail">{_esc(item.detail.strip())}</pre>'

    return (
        f'<li class="{cls}{extra_cls}" data-status="{st.value}" id="{section_id}_i{idx}">'
        f'  <div class="cp-item-row">{badge}'
        f'    <span class="cp-item-msg">{msg}</span>'
        f'    {diff_badge}'
        f'  </div>'
        f'  {cmd_html}{detail_html}'
        f'</li>'
    )


def _render_section(s: Section, s_idx: int, c_idx: int,
                     diff_map: Optional[Dict[int, str]] = None) -> str:
    sid  = f"c{c_idx}_s{s_idx}"
    ws   = s.status
    dur  = f'<span class="cp-section-dur">{s.duration_s:.1f}s</span>' if s.duration_s else ""
    cnts = (f'<span class="cp-cnt pass">{s.pass_count}✓</span>'
            f'<span class="cp-cnt fail">{s.fail_count}✕</span>'
            f'<span class="cp-cnt warn">{s.warn_count}⚠</span>')

    diff_map = diff_map or {}
    active_items   = []
    resolved_items = []
    for i, item in enumerate(s.checks):
        marker = diff_map.get(i)
        if marker == "RESOLVED":
            resolved_items.append(_render_item(item, i, sid, diff_marker="RESOLVED"))
        else:
            active_items.append(_render_item(item, i, sid, diff_marker=marker))

    items_html = "\n".join(active_items)

    resolved_html = ""
    if resolved_items:
        resolved_html = (
            f'<details class="cp-resolved" open>'
            f'<summary style="color:var(--pass,#22c55e);font-size:.75rem;'
            f'font-weight:600;padding:.3rem 0;cursor:pointer">'
            f'✓ Resolved ({len(resolved_items)})</summary>'
            f'<ul class="cp-items">{"".join(resolved_items)}</ul>'
            f'</details>'
        )

    raw_html = ""
    if s.raw_log.strip():
        raw_html = (
            f'<details class="cp-raw"><summary>📋 Raw command log</summary>'
            f'<pre>{_esc(s.raw_log.strip())}</pre></details>'
        )

    auto_open = 'open' if ws in (Status.FAIL, Status.ERROR, Status.WARN) else ''

    return f"""
    <details class="cp-section ws-{ws.value}" id="{sid}" {auto_open}>
      <summary class="cp-section-header">
        {_badge(ws)}
        <span class="cp-section-name">{_esc(s.name)}</span>
        <span class="cp-section-counts">{cnts} {dur}</span>
      </summary>
      <div class="cp-section-body">
        <ul class="cp-items">{items_html}</ul>
        {resolved_html}
        {raw_html}
      </div>
    </details>"""


def _build_section_diff_map(
    section_name: str,
    checks:       list,
    prev_checks:  Dict,
) -> Dict[int, str]:
    """Return {message_index: "NEW"|"RESOLVED"} for one section."""
    dm: Dict[int, str] = {}
    problem = {Status.FAIL.value, Status.ERROR.value, Status.WARN.value}
    for idx, item in enumerate(checks):
        prev_status = prev_checks.get((section_name, idx))
        curr_status = item.status.value if hasattr(item.status, "value") else str(item.status)
        if prev_status is None:
            if curr_status in problem:
                dm[idx] = "NEW"
        else:
            if prev_status in problem and curr_status not in problem:
                dm[idx] = "RESOLVED"
            elif prev_status not in problem and curr_status in problem:
                dm[idx] = "NEW"
    return dm


def _render_cluster(r: ClusterResult, c_idx: int,
                    prev_checks: Optional[Dict] = None) -> str:
    cid  = f"cluster_{c_idx}"
    ws   = r.overall_status
    ts   = r.start_time.strftime("%Y-%m-%d %H:%M:%S") if r.start_time else ""
    dur  = f'{r.duration_s:.0f}s' if r.duration_s else "—"
    env  = f'<span class="cp-cluster-env">{_esc(r.environment)}</span>' if r.environment else ""

    if not r.login_success:
        return f"""
  <details class="cp-cluster" id="{cid}">
    <summary class="cp-cluster-header">
      {_badge(Status.ERROR)}
      <span class="cp-cluster-type">{r.cluster_type.upper()}</span>
      <span class="cp-cluster-name">{_esc(r.cluster_name)}</span>
      {env}
    </summary>
    <div class="cp-login-err">
      ✕ SSH/Login failed — {_esc(r.login_error)}
    </div>
  </details>"""

    cnts = (f'<span class="cp-cnt pass">{r.pass_count}✓</span>'
            f'<span class="cp-cnt fail">{r.fail_count}✕</span>'
            f'<span class="cp-cnt warn">{r.warn_count}⚠</span>')

    sections_html = "\n".join(
        _render_section(
            s, i, c_idx,
            diff_map=(_build_section_diff_map(s.name, s.checks, prev_checks)
                      if prev_checks else None),
        )
        for i, s in enumerate(r.sections)
    )

    auto_open = 'open' if r.fail_count > 0 else ''

    return f"""
  <details class="cp-cluster" id="{cid}" {auto_open}>
    <summary class="cp-cluster-header">
      {_badge(ws)}
      <span class="cp-cluster-type">{r.cluster_type.upper()}</span>
      <span class="cp-cluster-name">{_esc(r.cluster_name)}</span>
      {env}
      <span class="cp-cluster-counts">
        {cnts}
        <span class="cp-dur">{dur}</span>
      </span>
      <span class="cp-cluster-ts">{ts}</span>
    </summary>
    <div class="cp-cluster-body">
      {sections_html}
    </div>
  </details>"""


# ══════════════════════════════════════════════════════════════════════════════
#  Top-level scorer
# ══════════════════════════════════════════════════════════════════════════════

def _scoreboard(results: List[ClusterResult], ts: str) -> str:
    total_pass  = sum(r.pass_count for r in results)
    total_fail  = sum(r.fail_count for r in results)
    total_warn  = sum(r.warn_count for r in results)
    cl_fail     = sum(1 for r in results if r.fail_count > 0 or not r.login_success)
    cl_warn     = sum(1 for r in results if r.warn_count > 0 and r.fail_count == 0)
    cl_ok       = len(results) - cl_fail - cl_warn

    cards = [
        ("CLUSTERS",      str(len(results)), "", "sc-neutral"),
        ("HEALTHY",       str(cl_ok),        "clusters",  "sc-pass"),
        ("CRITICAL",      str(cl_fail),      "clusters",  "sc-fail"),
        ("WARNINGS",      str(cl_warn),      "clusters",  "sc-warn"),
        ("TOTAL PASS",    str(total_pass),   "checks",    "sc-pass"),
        ("TOTAL FAIL",    str(total_fail),   "checks",    "sc-fail"),
        ("TOTAL WARN",    str(total_warn),   "checks",    "sc-warn"),
    ]
    html = '<div class="cp-scoreboard">'
    for label, val, sub, cls in cards:
        sub_html = f'<div class="cp-score-sub">{sub}</div>' if sub else ""
        html += (
            f'<div class="cp-score-card {cls}">'
            f'  <div class="cp-score-label">{label}</div>'
            f'  <div class="cp-score-value">{val}</div>'
            f'  {sub_html}'
            f'</div>'
        )
    html += '</div>'
    return html


# ══════════════════════════════════════════════════════════════════════════════
#  Full page assembler
# ══════════════════════════════════════════════════════════════════════════════

def _diff_banner(results: List[ClusterResult],
                  diff_data: Dict[str, Dict]) -> str:
    """Build the 'What's Changed' banner summarising new/resolved items."""
    new_fail = new_warn = resolved = 0
    problem  = {Status.FAIL.value, Status.ERROR.value, Status.WARN.value}
    for r in results:
        prev = diff_data.get(r.cluster_name, {})
        if not prev:
            continue
        for s in r.sections:
            for idx, item in enumerate(s.checks):
                prev_st = prev.get((s.name, idx))
                curr_st = item.status.value
                if prev_st is None and curr_st in problem:
                    if curr_st == Status.WARN.value:
                        new_warn += 1
                    else:
                        new_fail += 1
                elif prev_st in problem and curr_st not in problem:
                    resolved += 1
                elif prev_st not in problem and curr_st in problem:
                    if curr_st == Status.WARN.value:
                        new_warn += 1
                    else:
                        new_fail += 1
    if new_fail == 0 and new_warn == 0 and resolved == 0:
        return ""
    parts = []
    if new_fail:
        parts.append(f'<span style="color:#ef4444">▲ {new_fail} new failure{"s" if new_fail!=1 else ""}</span>')
    if new_warn:
        parts.append(f'<span style="color:#f59e0b">▲ {new_warn} new warning{"s" if new_warn!=1 else ""}</span>')
    if resolved:
        parts.append(f'<span style="color:#22c55e">▼ {resolved} resolved</span>')
    return (
        '<div style="margin:0 0 1rem;padding:.75rem 1.25rem;border-radius:6px;'
        'background:rgba(96,165,250,.08);border:1px solid rgba(96,165,250,.25);'
        'font-size:.82rem;display:flex;gap:1.5rem;align-items:center;flex-wrap:wrap">'
        '<span style="font-weight:700;color:#60a5fa">⟳ What\'s Changed</span>'
        + "  ".join(parts)
        + "</div>"
    )


class HTMLReporter:
    def __init__(self, results: List[ClusterResult], output_dir: Path,
                 diff_data: Optional[Dict[str, Dict]] = None):
        self.results    = results
        self.output_dir = output_dir
        self.diff_data  = diff_data or {}

    def generate(self) -> Path:
        ts      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tp      = sum(r.pass_count for r in self.results)
        tf      = sum(r.fail_count for r in self.results)
        tw      = sum(r.warn_count for r in self.results)
        overall = "FAIL" if tf > 0 else ("WARN" if tw > 0 else "PASS")
        ov_cls  = f"cp-overall-{overall.lower()}"

        scoreboard    = _scoreboard(self.results, ts)
        diff_banner   = _diff_banner(self.results, self.diff_data) if self.diff_data else ""
        clusters_html = "\n".join(
            _render_cluster(r, i, prev_checks=self.diff_data.get(r.cluster_name))
            for i, r in enumerate(self.results)
        )

        # filter buttons with counts
        n_fail = sum(1 for r in self.results for s in r.sections for item in s.checks
                     if item.status in (Status.FAIL, Status.ERROR))
        n_warn = sum(1 for r in self.results for s in r.sections for item in s.checks
                     if item.status == Status.WARN)
        n_pass = sum(1 for r in self.results for s in r.sections for item in s.checks
                     if item.status == Status.PASS)
        n_info = sum(1 for r in self.results for s in r.sections for item in s.checks
                     if item.status in (Status.INFO, Status.SKIP))

        meta_clusters = f"{len(self.results)} cluster{'s' if len(self.results)!=1 else ''}"

        page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CloudHealth Report — {ts}</title>
<style>{_CSS}</style>
</head>
<body>

<!-- ── HEADER ─────────────────────────────────────────────────────────── -->
<header class="cp-header">
  <div class="cp-header-inner">
    <div class="cp-logo">
      <div class="cp-logo-icon">⚡</div>
      <div>
        <div class="cp-logo-text">CloudHealth</div>
        <div class="cp-logo-sub">Health Report</div>
      </div>
    </div>
    <div class="cp-meta">
      <div class="cp-meta-item">
        <span class="cp-meta-label">Generated</span>
        <span class="cp-meta-value">{ts}</span>
      </div>
      <div class="cp-meta-item">
        <span class="cp-meta-label">Scope</span>
        <span class="cp-meta-value">{meta_clusters}</span>
      </div>
      <div class="cp-meta-item">
        <span class="cp-meta-label">Checks</span>
        <span class="cp-meta-value" style="color:var(--pass)">{tp} pass</span>
      </div>
      <div class="cp-meta-item">
        <span class="cp-meta-label">Failures</span>
        <span class="cp-meta-value" style="color:var(--fail)">{tf} fail</span>
      </div>
    </div>
    <div class="cp-overall">
      <span class="cp-overall-badge {ov_cls}">{overall}</span>
    </div>
  </div>
</header>

<!-- ── SCOREBOARD ────────────────────────────────────────────────────── -->
{scoreboard}

<!-- ── TOOLBAR ───────────────────────────────────────────────────────── -->
<div class="cp-toolbar">
  <span class="cp-toolbar-label">Filter</span>
  <button class="cp-filter-btn active" onclick="setFilter('all',this)">
    <span class="dot"></span> All
  </button>
  <button class="cp-filter-btn" onclick="setFilter('fail',this)"
          style="color:var(--fail);border-color:rgba(239,68,68,.4)">
    <span class="dot" style="background:var(--fail)"></span> Failures ({n_fail})
  </button>
  <button class="cp-filter-btn" onclick="setFilter('warn',this)"
          style="color:var(--warn);border-color:rgba(245,158,11,.4)">
    <span class="dot" style="background:var(--warn)"></span> Warnings ({n_warn})
  </button>
  <button class="cp-filter-btn" onclick="setFilter('pass',this)"
          style="color:var(--pass);border-color:rgba(34,197,94,.4)">
    <span class="dot" style="background:var(--pass)"></span> Passed ({n_pass})
  </button>
  <button class="cp-filter-btn" onclick="setFilter('info',this)"
          style="color:var(--info);border-color:rgba(96,165,250,.4)">
    <span class="dot" style="background:var(--info)"></span> Info ({n_info})
  </button>
  <button class="cp-action-btn" onclick="expandAll()">Expand All</button>
  <button class="cp-action-btn" onclick="collapseAll()">Collapse All</button>
  <input class="cp-search" type="text" placeholder="🔍 Search…"
         oninput="doSearch(this.value)">
</div>

<!-- ── CLUSTERS ──────────────────────────────────────────────────────── -->
<div class="cp-content" id="cp-clusters">
{diff_banner}
{clusters_html}
</div>

<script>{_JS}</script>
</body>
</html>"""

        out = self.output_dir / "healthcheck_report.html"
        out.write_text(page, encoding="utf-8")
        return out

    # ── Email version ─────────────────────────────────────────────────────────
    def generate_email(self) -> Path:
        """Simpler inline-styled version safe for email clients."""
        ts      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tf      = sum(r.fail_count for r in self.results)
        tw      = sum(r.warn_count for r in self.results)
        overall = "FAIL" if tf > 0 else ("WARN" if tw > 0 else "PASS")
        ov_color= "#ef4444" if overall=="FAIL" else ("#f59e0b" if overall=="WARN" else "#22c55e")

        rows = ""
        for r in self.results:
            ws    = r.overall_status
            color = _STATUS_COLOR[ws]
            bg    = "rgba(239,68,68,.07)" if r.fail_count>0 else ("rgba(245,158,11,.07)" if r.warn_count>0 else "transparent")
            fail_sections = [s for s in r.sections if s.fail_count > 0 or s.warn_count > 0]

            detail = ""
            for s in fail_sections[:8]:
                for item in s.checks:
                    if item.status in (Status.FAIL, Status.ERROR, Status.WARN):
                        ic = _badge_inline(item.status, small=True)
                        detail += (
                            f'<tr><td style="padding:4px 12px 4px 32px;font-size:12px;'
                            f'font-family:monospace;color:#ccd;border-bottom:1px solid #2a3350">'
                            f'{ic}&nbsp; {_esc(item.message)}</td></tr>'
                        )

            rows += f"""
        <tr style="background:{bg}">
          <td style="padding:10px 16px;border-bottom:1px solid #2a3350;vertical-align:middle">
            {_badge_inline(ws)}
          </td>
          <td style="padding:10px 16px;border-bottom:1px solid #2a3350;font-weight:600;color:#e8edf8;font-size:13px;font-family:'IBM Plex Mono',monospace">
            {_esc(r.cluster_name)}
          </td>
          <td style="padding:10px 16px;border-bottom:1px solid #2a3350;font-size:11px;color:#7a8aaa;font-family:monospace;text-transform:uppercase;letter-spacing:1px">
            {r.cluster_type.upper()}
          </td>
          <td style="padding:10px 16px;border-bottom:1px solid #2a3350;font-family:monospace;font-size:12px">
            <span style="color:#22c55e">{r.pass_count}✓</span>&nbsp;
            <span style="color:#ef4444">{r.fail_count}✕</span>&nbsp;
            <span style="color:#f59e0b">{r.warn_count}⚠</span>
          </td>
          <td style="padding:10px 16px;border-bottom:1px solid #2a3350;font-size:11px;color:#7a8aaa;font-family:monospace">
            {f"{r.duration_s:.0f}s" if r.duration_s else "—"}
          </td>
        </tr>
        {detail}"""

        tp = sum(r.pass_count for r in self.results)
        tw2= sum(r.warn_count for r in self.results)

        email = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>CloudHealth Health Report</title></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:'IBM Plex Sans',Arial,sans-serif;color:#e8edf8">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0f1117;padding:32px 0">
  <tr><td align="center">
    <table width="860" cellpadding="0" cellspacing="0" style="max-width:860px;background:#171923;border:1px solid #2a3350;border-radius:8px;overflow:hidden">

      <!-- header -->
      <tr style="background:linear-gradient(135deg,#1e2333,#252d40)">
        <td style="padding:24px 32px">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td>
                <div style="font-family:'IBM Plex Mono',monospace;font-size:20px;font-weight:700;
                            background:linear-gradient(135deg,#f59e0b,#ef4444);
                            -webkit-background-clip:text;color:#f59e0b">⚡ CloudHealth</div>
                <div style="font-size:11px;color:#7a8aaa;letter-spacing:.5px;text-transform:uppercase;margin-top:2px">Health Check Report</div>
              </td>
              <td align="right">
                <div style="font-family:monospace;font-size:26px;font-weight:700;
                            padding:6px 20px;border-radius:6px;border:2px solid {ov_color}40;
                            background:{ov_color}18;color:{ov_color};letter-spacing:1px">{overall}</div>
              </td>
            </tr>
          </table>
        </td>
      </tr>

      <!-- meta row -->
      <tr style="background:#1e2333;border-bottom:1px solid #2a3350">
        <td style="padding:12px 32px">
          <table cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding-right:32px"><span style="font-size:10px;color:#7a8aaa;text-transform:uppercase;letter-spacing:.5px;display:block">Generated</span>
                <span style="font-family:monospace;font-size:12px">{ts}</span></td>
              <td style="padding-right:32px"><span style="font-size:10px;color:#7a8aaa;text-transform:uppercase;letter-spacing:.5px;display:block">Clusters</span>
                <span style="font-family:monospace;font-size:12px">{len(self.results)}</span></td>
              <td style="padding-right:32px"><span style="font-size:10px;color:#7a8aaa;text-transform:uppercase;letter-spacing:.5px;display:block">Total Pass</span>
                <span style="font-family:monospace;font-size:12px;color:#22c55e">{tp}</span></td>
              <td style="padding-right:32px"><span style="font-size:10px;color:#7a8aaa;text-transform:uppercase;letter-spacing:.5px;display:block">Total Fail</span>
                <span style="font-family:monospace;font-size:12px;color:#ef4444">{tf}</span></td>
              <td><span style="font-size:10px;color:#7a8aaa;text-transform:uppercase;letter-spacing:.5px;display:block">Total Warn</span>
                <span style="font-family:monospace;font-size:12px;color:#f59e0b">{tw2}</span></td>
            </tr>
          </table>
        </td>
      </tr>

      <!-- cluster table -->
      <tr><td style="padding:24px 32px">
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border:1px solid #2a3350;border-radius:6px;overflow:hidden">
          <tr style="background:#1e2333">
            <th style="padding:8px 16px;text-align:left;font-size:10px;color:#7a8aaa;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #2a3350">Status</th>
            <th style="padding:8px 16px;text-align:left;font-size:10px;color:#7a8aaa;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #2a3350">Cluster</th>
            <th style="padding:8px 16px;text-align:left;font-size:10px;color:#7a8aaa;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #2a3350">Type</th>
            <th style="padding:8px 16px;text-align:left;font-size:10px;color:#7a8aaa;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #2a3350">Results</th>
            <th style="padding:8px 16px;text-align:left;font-size:10px;color:#7a8aaa;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #2a3350">Duration</th>
          </tr>
          {rows}
        </table>
      </td></tr>

      <!-- footer -->
      <tr style="background:#1e2333;border-top:1px solid #2a3350">
        <td style="padding:14px 32px;font-size:11px;color:#4a5570;font-family:monospace;text-align:center">
          CloudHealth · Auto-generated health check report · {ts}
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""

        out = self.output_dir / "healthcheck_email.html"
        out.write_text(email, encoding="utf-8")
        return out
