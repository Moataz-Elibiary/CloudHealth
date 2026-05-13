"""
Beta6 history_db.py — central SQLite database on the server.

Single DB for all clusters (replaces per-bastion DBs from beta5).
Writes directly — no SSH inline scripts, no source field.
"""
from __future__ import annotations
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("cloudhealth.history_db")

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
    status        TEXT    NOT NULL DEFAULT 'UNKNOWN'
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

CREATE INDEX IF NOT EXISTS idx_runs_started    ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_cr_run          ON cluster_results(run_id, cluster_name);
CREATE INDEX IF NOT EXISTS idx_chk_run_cluster ON check_results(run_id, cluster_name);
CREATE INDEX IF NOT EXISTS idx_chk_status      ON check_results(run_id, cluster_name, status);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    p = Path(db_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(_DDL)
        conn.commit()
    finally:
        conn.close()


def write_run(
    db_path:     str,
    run_id:      str,
    user:        str,
    started_at:  datetime,
    finished_at: datetime,
    summaries:   List[Dict[str, Any]],   # list of ClusterResult.to_dict()
    status:      str = "COMPLETED",
    max_runs:    int = 200,
) -> None:
    """Persist a full run (all clusters) in a single transaction, then prune."""
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs "
                "(run_id,user,started_at,finished_at,cluster_count,status) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, user,
                 started_at.isoformat(), finished_at.isoformat(),
                 len(summaries), status),
            )
            for summary in summaries:
                cname = summary.get("cluster_name", "")
                conn.execute(
                    "INSERT INTO cluster_results "
                    "(run_id,cluster_name,cluster_type,pass_count,fail_count,"
                    " warn_count,duration_s,status,login_success) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        run_id, cname,
                        summary.get("cluster_type", ""),
                        summary.get("pass_count",    0),
                        summary.get("fail_count",    0),
                        summary.get("warn_count",    0),
                        summary.get("duration_s"),
                        summary.get("overall_status", "UNKNOWN"),
                        1 if summary.get("login_success", True) else 0,
                    ),
                )
                for sec in summary.get("sections", []):
                    for idx, item in enumerate(sec.get("checks", [])):
                        conn.execute(
                            "INSERT INTO check_results "
                            "(run_id,cluster_name,section_name,message_index,"
                            " status,message,detail,command) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (
                                run_id, cname,
                                sec.get("name", ""), idx,
                                item.get("status",  ""),
                                item.get("message", ""),
                                item.get("detail"),
                                item.get("command"),
                            ),
                        )
        _prune(conn, max_runs)
    except Exception:
        log.exception("history_db.write_run failed for run_id=%s", run_id)
        raise
    finally:
        conn.close()


def _prune(conn: sqlite3.Connection, max_runs: int) -> None:
    old = conn.execute(
        "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT -1 OFFSET ?",
        (max_runs,),
    ).fetchall()
    if old:
        conn.executemany("DELETE FROM runs WHERE run_id = ?",
                         [(r["run_id"],) for r in old])
        conn.commit()
        log.info("history_db: pruned %d old run(s)", len(old))


def get_runs(db_path: str, limit: int = 30) -> List[Dict[str, Any]]:
    """Return summary rows for the most recent runs, newest first."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT run_id,user,started_at,finished_at,cluster_count,status "
            "FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_run(db_path: str, run_id: str) -> Optional[Dict[str, Any]]:
    """Return a run summary with its cluster_results rows."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
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
    db_path:       str,
    cluster_name:  str,
    before_run_id: str,
) -> List[Dict[str, Any]]:
    """Return check statuses from the last completed run before before_run_id.

    Format: [{"section_name": str, "message_index": int, "status": str}, ...]
    Used by HTMLReporter to build NEW/RESOLVED diff badges.
    """
    conn = _connect(db_path)
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
            return []
        rows = conn.execute(
            "SELECT section_name, message_index, status "
            "FROM check_results WHERE run_id = ? AND cluster_name = ?",
            (prev["run_id"], cluster_name),
        ).fetchall()
        return [
            {"section_name":   r["section_name"],
             "message_index":  r["message_index"],
             "status":         r["status"]}
            for r in rows
        ]
    finally:
        conn.close()


def get_failed_clusters(db_path: str) -> List[str]:
    """Return cluster names that failed (fail_count > 0 or login failed) in the last completed run."""
    conn = _connect(db_path)
    try:
        run = conn.execute(
            "SELECT run_id FROM runs WHERE status = 'COMPLETED' "
            "ORDER BY started_at DESC LIMIT 1",
        ).fetchone()
        if not run:
            return []
        rows = conn.execute(
            "SELECT cluster_name FROM cluster_results "
            "WHERE run_id = ? AND (fail_count > 0 OR login_success = 0)",
            (run["run_id"],),
        ).fetchall()
        return [r["cluster_name"] for r in rows]
    finally:
        conn.close()
