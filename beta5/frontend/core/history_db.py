"""
Frontend preflight audit database.

DB path: ~/Documents/cloud_health/db/history.db
Schema : preflight_runs / preflight_results
Purpose: audit trail for SSH pre-flight checks run before health checks.

Health-check run history is now owned by each bastion backend
(at /opt/cloud_health/db/history.db) and streamed to the frontend
via the all_done / cancelled WS events.
"""
from __future__ import annotations
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("frontend.history_db")

DB_PATH = Path.home() / "Documents" / "cloud_health" / "db" / "history.db"

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Pre-flight audit log — one row per cluster per preflight run.
CREATE TABLE IF NOT EXISTS preflight_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    preflight_id  TEXT    UNIQUE NOT NULL,
    started_at    TEXT    NOT NULL,
    total         INTEGER NOT NULL DEFAULT 0,
    passed        INTEGER NOT NULL DEFAULT 0,
    all_ok        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS preflight_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    preflight_id    TEXT    NOT NULL REFERENCES preflight_runs(preflight_id) ON DELETE CASCADE,
    cluster_name    TEXT    NOT NULL,
    cluster_type    TEXT    NOT NULL DEFAULT '',
    installer_ip    TEXT    NOT NULL DEFAULT '',
    timestamp       TEXT    NOT NULL,
    reachable       INTEGER NOT NULL DEFAULT 0,
    auth_ok         INTEGER NOT NULL DEFAULT 0,
    python_ready    INTEGER NOT NULL DEFAULT 0,
    python_version  TEXT,
    backend_version TEXT,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'ERROR',
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_pf_runs_started ON preflight_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pf_res_id       ON preflight_results(preflight_id, cluster_name);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(_DDL)
        conn.commit()
    finally:
        conn.close()


def write_preflight(
    preflight_id: str,
    started_at:   datetime,
    rows:         List[Dict[str, Any]],
) -> None:
    """Persist a preflight audit record. rows is PreflightResult.to_dict() list."""
    passed = sum(1 for r in rows if r.get("status") == "OK")
    all_ok = int(passed == len(rows) and len(rows) > 0)
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO preflight_runs "
                "(preflight_id, started_at, total, passed, all_ok) "
                "VALUES (?,?,?,?,?)",
                (preflight_id, started_at.isoformat(), len(rows), passed, all_ok),
            )
            for r in rows:
                conn.execute(
                    "INSERT INTO preflight_results "
                    "(preflight_id, cluster_name, cluster_type, installer_ip, "
                    " timestamp, reachable, auth_ok, python_ready, python_version, "
                    " backend_version, duration_ms, status, error) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        preflight_id,
                        r.get("cluster_name", ""),
                        r.get("cluster_type", ""),
                        r.get("installer_ip", ""),
                        r.get("timestamp", ""),
                        int(bool(r.get("reachable"))),
                        int(bool(r.get("auth_ok"))),
                        int(bool(r.get("python_ready"))),
                        r.get("python_version"),
                        r.get("backend_version"),
                        r.get("duration_ms", 0),
                        r.get("status", "ERROR"),
                        r.get("error"),
                    ),
                )
    except Exception:
        log.exception("history_db.write_preflight failed for preflight_id=%s", preflight_id)
    finally:
        conn.close()


def get_preflight_runs(limit: int = 30) -> List[Dict[str, Any]]:
    """Return summary rows for the most recent preflight runs, newest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT preflight_id, started_at, total, passed, all_ok "
            "FROM preflight_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_preflight_run(preflight_id: str) -> Optional[Dict[str, Any]]:
    """Return a preflight run with its per-cluster result rows."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM preflight_runs WHERE preflight_id = ?", (preflight_id,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        clusters = conn.execute(
            "SELECT * FROM preflight_results WHERE preflight_id = ? ORDER BY cluster_name",
            (preflight_id,),
        ).fetchall()
        result["clusters"] = [dict(c) for c in clusters]
        return result
    finally:
        conn.close()
