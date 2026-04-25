"""Result data structures for ClusterPulse health checks."""
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
    status:  Status
    message: str
    detail:  str = ""
    command: str = ""


@dataclass
class Section:
    name:       str
    category:   str
    items:      List[CheckItem] = field(default_factory=list)
    raw_log:    str = ""
    start_time: Optional[datetime] = None
    end_time:   Optional[datetime] = None

    # ── convenience adders ────────────────────────────────────────────────────
    def pass_(self, msg: str, detail: str = "", command: str = ""):
        self.items.append(CheckItem(Status.PASS,  msg, detail, command))
    def fail(self, msg: str, detail: str = "", command: str = ""):
        self.items.append(CheckItem(Status.FAIL,  msg, detail, command))
    def warn(self, msg: str, detail: str = "", command: str = ""):
        self.items.append(CheckItem(Status.WARN,  msg, detail, command))
    def info(self, msg: str, detail: str = "", command: str = ""):
        self.items.append(CheckItem(Status.INFO,  msg, detail, command))
    def skip(self, msg: str):
        self.items.append(CheckItem(Status.SKIP,  msg))
    def error(self, msg: str, detail: str = ""):
        self.items.append(CheckItem(Status.ERROR, msg, detail))
    def append_log(self, text: str):
        self.raw_log += text + "\n"

    # ── aggregates ────────────────────────────────────────────────────────────
    @property
    def pass_count(self):  return sum(1 for i in self.items if i.status == Status.PASS)
    @property
    def fail_count(self):  return sum(1 for i in self.items if i.status == Status.FAIL)
    @property
    def warn_count(self):  return sum(1 for i in self.items if i.status in (Status.WARN, Status.ERROR))

    @property
    def worst_status(self) -> Status:
        statuses = {i.status for i in self.items}
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
    cluster_name:  str
    cluster_type:  str
    environment:   str = ""
    description:   str = ""
    sections:      List[Section] = field(default_factory=list)
    login_success: bool = True
    login_error:   str  = ""
    start_time:    Optional[datetime] = None
    end_time:      Optional[datetime] = None

    def add_section(self, s: Section):
        self.sections.append(s)

    @property
    def pass_count(self):  return sum(s.pass_count for s in self.sections)
    @property
    def fail_count(self):  return sum(s.fail_count for s in self.sections)
    @property
    def warn_count(self):  return sum(s.warn_count for s in self.sections)

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
