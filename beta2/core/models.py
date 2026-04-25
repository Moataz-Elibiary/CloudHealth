"""Result data structures for CloudHealth Beta 2."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class Status(str, Enum):
    PASS  = "PASS"
    FAIL  = "FAIL"
    WARN  = "WARN"
    INFO  = "INFO"
    SKIP  = "SKIP"
    ERROR = "ERROR"


@dataclass
class CheckItem:
    """A single check finding within a section."""
    status:  Status
    message: str
    name:    str = ""
    detail:  str = ""
    command: str = ""

    @property
    def is_problem(self) -> bool:
        return self.status in (Status.FAIL, Status.ERROR, Status.WARN)


@dataclass
class SectionResult:
    """A logical group of related checks (e.g. 'etcd Health')."""
    name:       str
    category:   str = ""
    checks:     List[CheckItem] = field(default_factory=list)
    raw_log:    str = ""
    start_time: Optional[datetime] = None
    end_time:   Optional[datetime] = None

    # ── convenience adders (match CloudHealth API for easy porting) ──
    def pass_(self, msg: str, detail: str = "", command: str = ""):
        self.checks.append(CheckItem(Status.PASS, msg, detail=detail, command=command))

    def fail(self, msg: str, detail: str = "", command: str = ""):
        self.checks.append(CheckItem(Status.FAIL, msg, detail=detail, command=command))

    def warn(self, msg: str, detail: str = "", command: str = ""):
        self.checks.append(CheckItem(Status.WARN, msg, detail=detail, command=command))

    def info(self, msg: str, detail: str = "", command: str = ""):
        self.checks.append(CheckItem(Status.INFO, msg, detail=detail, command=command))

    def skip(self, msg: str):
        self.checks.append(CheckItem(Status.SKIP, msg))

    def error(self, msg: str, detail: str = ""):
        self.checks.append(CheckItem(Status.ERROR, msg, detail=detail))

    def append_log(self, text: str):
        self.raw_log += text + "\n"

    # ── aggregates ──
    @property
    def pass_count(self): return sum(1 for i in self.checks if i.status == Status.PASS)
    @property
    def fail_count(self): return sum(1 for i in self.checks if i.status == Status.FAIL)
    @property
    def warn_count(self): return sum(1 for i in self.checks if i.status in (Status.WARN, Status.ERROR))

    @property
    def status(self) -> Status:
        statuses = {i.status for i in self.checks}
        for s in (Status.FAIL, Status.ERROR, Status.WARN, Status.PASS, Status.INFO, Status.SKIP):
            if s in statuses:
                return s
        return Status.INFO

    @property
    def duration_s(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None


@dataclass
class ClusterResult:
    """Top-level result for an entire cluster."""
    cluster_name:  str
    cluster_type:  str
    environment:   str = ""
    sections:      List[SectionResult] = field(default_factory=list)
    login_success: bool = True
    login_error:   str  = ""
    start_time:    Optional[datetime] = None
    end_time:      Optional[datetime] = None

    @property
    def pass_count(self): return sum(s.pass_count for s in self.sections)
    @property
    def fail_count(self): return sum(s.fail_count for s in self.sections)
    @property
    def warn_count(self): return sum(s.warn_count for s in self.sections)

    @property
    def overall_status(self) -> str:
        if not self.login_success: return "ERROR"
        if self.fail_count > 0:   return "FAIL"
        if self.warn_count > 0:   return "WARN"
        return "PASS"

    @property
    def duration_s(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None
