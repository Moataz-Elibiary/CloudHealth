"""
Beta6 result model.
Streaming queue removed — no WebSocket, no live push.
Results are collected in memory and written to DB + report at end of run.
"""
from __future__ import annotations
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

    # Commands logger wired by CheckRunner._wire() before each check runs.
    _commands_logger: Optional[logging.Logger] = field(default=None, repr=False, compare=False)

    # ── Adders ────────────────────────────────────────────────────────────────
    def pass_(self, msg: str, detail: str = "", command: str = ""):
        self.checks.append(CheckItem(Status.PASS, msg, detail or None, command))

    def fail(self, msg: str, detail: str = "", command: str = ""):
        self.checks.append(CheckItem(Status.FAIL, msg, detail or None, command))

    def warn(self, msg: str, detail: str = "", command: str = ""):
        self.checks.append(CheckItem(Status.WARN, msg, detail or None, command))

    def info(self, msg: str, detail: str = "", command: str = ""):
        self.checks.append(CheckItem(Status.INFO, msg, detail or None, command))

    def skip(self, msg: str, detail: str = ""):
        self.checks.append(CheckItem(Status.SKIP, msg, detail or None))

    def error(self, msg: str, detail: str = ""):
        self.checks.append(CheckItem(Status.ERROR, msg, detail or None))

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
