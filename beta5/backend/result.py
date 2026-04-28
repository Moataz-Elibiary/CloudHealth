"""
Beta4 canonical result model.
Single source of truth — copied to frontend/core/result.py during SFTP push.
Section._push() provides per-check-item streaming via an asyncio.Queue.
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class Status(str, Enum):
    PASS  = "PASS"
    FAIL  = "FAIL"
    WARN  = "WARN"
    INFO  = "INFO"
    SKIP  = "SKIP"
    ERROR = "ERROR"
    RUNNING = "RUNNING"


@dataclass
class CheckItem:
    status:  Status
    message: str
    detail:  Optional[str] = None
    command: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def is_problem(self) -> bool:
        return self.status in (Status.FAIL, Status.ERROR, Status.WARN)

    def to_dict(self) -> dict:
        return {
            "status":    self.status.value,
            "message":   self.message,
            "detail":    self.detail,
            "command":   self.command,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CheckItem":
        return cls(
            status  = Status(d["status"]),
            message = d["message"],
            detail  = d.get("detail"),
            command = d.get("command", ""),
        )


@dataclass
class SectionResult:
    name:     str
    category: str = ""
    checks:   List[CheckItem] = field(default_factory=list)
    raw_log:  str = ""
    start_time: Optional[datetime] = None
    end_time:   Optional[datetime] = None

    # ── Queue hook: wired before fn() runs, not after ─────────────────────────
    # Set by CheckRunner._wire(sec) before each check function is called.
    # Every sec.pass_()/fail_()/warn_()/info_() call pushes to this queue
    # so individual check items stream to the browser in real time.
    _queue:           Optional[asyncio.Queue]             = field(default=None, repr=False, compare=False)
    _cluster_name:    str                                 = field(default="",   repr=False, compare=False)
    _loop:            Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False, compare=False)
    _commands_logger: Optional[logging.Logger]           = field(default=None, repr=False, compare=False)

    def _push(self, item: CheckItem):
        """Push to per-connection queue from a worker thread.

        Fast path: put_nowait when the queue has space.
        Slow path: if the queue is full (slow browser / slow tunnel), schedule
        a blocking put() on the event loop and wait up to 30 s.  This
        backpressures the check thread rather than silently losing data.
        Last resort: drop only if the loop is gone or the wait times out.
        """
        if self._queue is None:
            return
        msg = {
            "type":     "check_result",
            "cluster":  self._cluster_name,
            "section":  self.name,
            "category": self.category,
            "status":   item.status.value,
            "message":  item.message,
            "detail":   item.detail,
            "command":  item.command,
        }
        try:
            self._queue.put_nowait(msg)
            return
        except asyncio.QueueFull:
            pass
        # Queue full — backpressure via the event loop rather than dropping.
        if self._loop is not None and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._queue.put(msg), self._loop
                ).result(timeout=30)
            except Exception:
                pass  # loop stopped or timed out — drop as absolute last resort

    # ── Adders ────────────────────────────────────────────────────────────────
    def pass_(self, msg: str, detail: str = "", command: str = ""):
        item = CheckItem(Status.PASS,  msg, detail or None, command)
        self.checks.append(item); self._push(item)

    def fail(self, msg: str, detail: str = "", command: str = ""):
        item = CheckItem(Status.FAIL,  msg, detail or None, command)
        self.checks.append(item); self._push(item)

    def warn(self, msg: str, detail: str = "", command: str = ""):
        item = CheckItem(Status.WARN,  msg, detail or None, command)
        self.checks.append(item); self._push(item)

    def info(self, msg: str, detail: str = "", command: str = ""):
        item = CheckItem(Status.INFO,  msg, detail or None, command)
        self.checks.append(item); self._push(item)

    def skip(self, msg: str, detail: str = ""):
        item = CheckItem(Status.SKIP,  msg, detail or None)
        self.checks.append(item); self._push(item)

    def error(self, msg: str, detail: str = ""):
        item = CheckItem(Status.ERROR, msg, detail or None)
        self.checks.append(item); self._push(item)

    def append_log(self, text: str):
        self.raw_log += text + "\n"
        if self._commands_logger is not None:
            self._commands_logger.debug(text)

    # ── Aggregates ────────────────────────────────────────────────────────────
    @property
    def pass_count(self) -> int:
        return sum(1 for i in self.checks if i.status == Status.PASS)

    @property
    def fail_count(self) -> int:
        return sum(1 for i in self.checks if i.status == Status.FAIL)

    @property
    def warn_count(self) -> int:
        return sum(1 for i in self.checks if i.status in (Status.WARN, Status.ERROR))

    @property
    def status(self) -> Status:
        statuses = {i.status for i in self.checks}
        for candidate in (Status.FAIL, Status.ERROR, Status.WARN,
                          Status.PASS, Status.INFO, Status.SKIP):
            if candidate in statuses:
                return candidate
        return Status.INFO

    @property
    def duration_s(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None

    def to_dict(self) -> dict:
        return {
            "name":       self.name,
            "category":   self.category,
            "status":     self.status.value,
            "checks":     [i.to_dict() for i in self.checks],
            "raw_log":    self.raw_log,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "warn_count": self.warn_count,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time":   self.end_time.isoformat()   if self.end_time   else None,
            "duration_s": self.duration_s,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SectionResult":
        s = cls(name=d["name"], category=d.get("category", ""))
        s.checks  = [CheckItem.from_dict(i) for i in d.get("checks", [])]
        s.raw_log = d.get("raw_log", "")
        return s


@dataclass
class ClusterResult:
    cluster_name: str
    cluster_type: str = ""
    environment:  str = ""
    description:  str = ""
    sections:     List[SectionResult] = field(default_factory=list)
    login_success: bool = True
    login_error:   str  = ""
    start_time:    datetime = field(default_factory=datetime.now)
    end_time:      Optional[datetime] = None

    def add_section(self, s: SectionResult):
        self.sections.append(s)

    @property
    def pass_count(self) -> int:
        return sum(s.pass_count for s in self.sections)

    @property
    def fail_count(self) -> int:
        return sum(s.fail_count for s in self.sections)

    @property
    def warn_count(self) -> int:
        return sum(s.warn_count for s in self.sections)

    @property
    def overall_status(self) -> Status:
        if not self.login_success: return Status.ERROR
        if self.fail_count > 0:   return Status.FAIL
        if self.warn_count > 0:   return Status.WARN
        return Status.PASS

    @property
    def duration_s(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None

    def to_dict(self) -> dict:
        return {
            "cluster_name":   self.cluster_name,
            "cluster_type":   self.cluster_type,
            "environment":    self.environment,
            "description":    self.description,
            "sections":       [s.to_dict() for s in self.sections],
            "login_success":  self.login_success,
            "login_error":    self.login_error,
            "overall_status": self.overall_status.value,
            "pass_count":     self.pass_count,
            "fail_count":     self.fail_count,
            "warn_count":     self.warn_count,
            "start_time":     self.start_time.isoformat(),
            "end_time":       self.end_time.isoformat() if self.end_time else None,
            "duration_s":     self.duration_s,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ClusterResult":
        r = cls(
            cluster_name  = d["cluster_name"],
            cluster_type  = d.get("cluster_type", ""),
            environment   = d.get("environment", ""),
            description   = d.get("description", ""),
            login_success = d.get("login_success", True),
            login_error   = d.get("login_error", ""),
        )
        r.sections = [SectionResult.from_dict(s) for s in d.get("sections", [])]
        return r
