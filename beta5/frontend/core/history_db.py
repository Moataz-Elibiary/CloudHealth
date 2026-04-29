"""
P3.1 — SQLite run history database.

DB path: ~/Documents/cloud_health/db/history.db
Schema : runs / cluster_results / check_results
Pruning: retain last history_max_runs completed runs (config-driven, default 200).
"""
from __future__ import annotations
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("frontend.history_db")

DB_PATH = Path.home() / "Documents" / "cloud_health" / "db" / "history.db"

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT    UNIQUE NOT NULL,
    user          TEXT    NOT NULL DEFAULT '',
    started_at    TEXT    NOT NULL,
    finished_at   TEXT    NOT NULL,
    cluster_count INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'UNKNOWN',
    source        TEXT    NOT NULL DEFAULT 'ui'
);

CREATE TABLE IF NOT EXISTS cluster_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT    NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    cluster_name  TEXT    NOT NULL,
    cluster_type  TEXT    NOT NULL DEFAULT '',
    pass_count    INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    warn_count    INTEGER NOT NULL DEFAULT 0,
    duration_s    REAL,
    status        TEXT    NOT NULL DEFAULT 'UNKNOWN',
    login_success INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS check_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT    NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    cluster_name  TEXT    NOT NULL,
    section_name  TEXT    NOT NULL,
    message_index INTEGER NOT NULL,
    status        TEXT    NOT NULL,
    message       TEXT    NOT NULL DEFAULT '',
    detail        TEXT,
    command       TEXT
);

-- Pre-flight audit log — one row per cluster per preflight run.
-- preflight_run_id is a separate uuid so standalone preflights (no health
-- check after) still get their own record in the audit trail.
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

CREATE INDEX IF NOT EXISTS idx_runs_started    ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_cr_run          ON cluster_results(run_id, cluster_name);
CREATE INDEX IF NOT EXISTS idx_chk_run_cluster ON check_results(run_id, cluster_name);
CREATE INDEX IF NOT EXISTS idx_chk_status      ON check_results(run_id, cluster_name, status);
CREATE INDEX IF NOT EXISTS idx_pf_runs_started ON preflight_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pf_res_id       ON preflight_results(preflight_id, cluster_name);
"""


# ── Connection ────────────────────────────────────────────────────────────────

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


# ── Write ─────────────────────────────────────────────────────────────────────

def write_run(
    run_id:      str,
    user:        str,
    started_at:  datetime,
    finished_at: datetime,
    results:     List[Dict[str, Any]],
    status:      str = "COMPLETED",
    source:      str = "ui",
    max_runs:    int = 200,
) -> None:
    """Persist a completed run in a single transaction, then prune old runs."""
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs "
                "(run_id,user,started_at,finished_at,cluster_count,status,source) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, user, started_at.isoformat(), finished_at.isoformat(),
                 len(results), status, source),
            )
            for r in results:
                conn.execute(
                    "INSERT INTO cluster_results "
                    "(run_id,cluster_name,cluster_type,pass_count,fail_count,"
                    " warn_count,duration_s,status,login_success) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        r.get("cluster_name", ""),
                        r.get("cluster_type", ""),
                        r.get("pass_count", 0),
                        r.get("fail_count", 0),
                        r.get("warn_count", 0),
                        r.get("duration_s"),
                        r.get("overall_status", "UNKNOWN"),
                        int(r.get("login_success", True)),
                    ),
                )
                for sec in r.get("sections", []):
                    for idx, item in enumerate(sec.get("checks", [])):
                        conn.execute(
                            "INSERT INTO check_results "
                            "(run_id,cluster_name,section_name,message_index,"
                            " status,message,detail,command) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (
                                run_id,
                                r.get("cluster_name", ""),
                                sec.get("name", ""),
                                idx,
                                item.get("status", ""),
                                item.get("message", ""),
                                item.get("detail"),
                                item.get("command"),
                            ),
                        )
        _prune(conn, max_runs)
    except Exception:
        log.exception("history_db.write_run failed for run_id=%s", run_id)
    finally:
        conn.close()


def _prune(conn: sqlite3.Connection, max_runs: int) -> None:
    """Delete the oldest runs beyond max_runs (cascade removes child rows)."""
    old = conn.execute(
        "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT -1 OFFSET ?",
        (max_runs,),
    ).fetchall()
    if old:
        conn.executemany(
            "DELETE FROM runs WHERE run_id = ?",
            [(r["run_id"],) for r in old],
        )
        conn.commit()
        log.info("history_db: pruned %d old run(s)", len(old))


def write_preflight(
    preflight_id: str,
    started_at:   datetime,
    rows:         List[Dict[str, Any]],
) -> None:
    """Persist a preflight audit record in a single transaction.

    rows is a list of PreflightResult.to_dict() dicts.
    """
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
    """Return a preflight run summary with its per-cluster result rows."""
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


# ── Read ──────────────────────────────────────────────────────────────────────

def get_runs(limit: int = 30) -> List[Dict[str, Any]]:
    """Return summary rows for the most recent runs, newest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT run_id,user,started_at,finished_at,cluster_count,status,source "
            "FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Return a run summary with its cluster_results rows."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        clusters = conn.execute(
            "SELECT * FROM cluster_results WHERE run_id = ? ORDER BY cluster_name",
            (run_id,),
        ).fetchall()
        result["clusters"] = [dict(c) for c in clusters]
        return result
    finally:
        conn.close()


def get_previous_checks(
    cluster_name:  str,
    before_run_id: str,
) -> Dict[Tuple[str, int], str]:
    """
    Return the check statuses from the most recent COMPLETED run for
    cluster_name that started before before_run_id.

    Returns {(section_name, message_index): status_str}.
    Used by the diff engine in the reporter.
    """
    conn = _connect()
    try:
        prev = conn.execute(
            """
            SELECT r.run_id FROM runs r
            JOIN  cluster_results cr ON cr.run_id = r.run_id
            WHERE cr.cluster_name = ?
              AND r.status        = 'COMPLETED'
              AND r.started_at    < (
                      SELECT started_at FROM runs WHERE run_id = ?
                  )
            ORDER BY r.started_at DESC LIMIT 1
            """,
            (cluster_name, before_run_id),
        ).fetchone()
        if not prev:
            return {}
        rows = conn.execute(
            "SELECT section_name,message_index,status "
            "FROM check_results WHERE run_id = ? AND cluster_name = ?",
            (prev["run_id"], cluster_name),
        ).fetchall()
        return {(r["section_name"], r["message_index"]): r["status"] for r in rows}
    finally:
        conn.close()
