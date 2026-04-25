"""
Shared result dataclasses.
Identical file lives in both backend/ and frontend/core/.
Serialised as JSON over the WebSocket protocol.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional, Callable, Awaitable


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

    def to_dict(self) -> dict:
        return {
            "status":  self.status.value,
            "message": self.message,
            "detail":  self.detail,
            "command": self.command,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CheckItem":
        return cls(
            status  = Status(d["status"]),
            message = d["message"],
            detail  = d.get("detail", ""),
            command = d.get("command", ""),
        )


@dataclass
class Section:
    name:       str
    category:   str
    items:      List[CheckItem] = field(default_factory=list)
    raw_log:    str = ""
    start_time: Optional[datetime] = None
    end_time:   Optional[datetime] = None

    # ── Queue hook — set by backend main before checks run ───────────────────
    # When set, every adder method pushes a WS message to the frontend in
    # addition to appending the item locally. Check code is unchanged.
    _queue: Optional[asyncio.Queue] = field(default=None, repr=False, compare=False)
    _cluster_name: str = field(default="", repr=False, compare=False)

    def _push(self, item: CheckItem):
        """Non-blocking push to the asyncio queue if one is wired up."""
        if self._queue is not None:
            try:
                self._queue.put_nowait({
                    "type":         "check_result",
                    "cluster":      self._cluster_name,
                    "section":      self.name,
                    "category":     self.category,
                    "status":       item.status.value,
                    "message":      item.message,
                    "detail":       item.detail,
                    "command":      item.command,
                })
            except asyncio.QueueFull:
                pass  # drop rather than block check execution

    # ── Convenience adders (unchanged API from original code) ────────────────
    def pass_(self, msg: str, detail: str = "", command: str = ""):
        item = CheckItem(Status.PASS,  msg, detail, command)
        self.items.append(item); self._push(item)

    def fail(self, msg: str, detail: str = "", command: str = ""):
        item = CheckItem(Status.FAIL,  msg, detail, command)
        self.items.append(item); self._push(item)

    def warn(self, msg: str, detail: str = "", command: str = ""):
        item = CheckItem(Status.WARN,  msg, detail, command)
        self.items.append(item); self._push(item)

    def info(self, msg: str, detail: str = "", command: str = ""):
        item = CheckItem(Status.INFO,  msg, detail, command)
        self.items.append(item); self._push(item)

    def skip(self, msg: str):
        item = CheckItem(Status.SKIP,  msg)
        self.items.append(item); self._push(item)

    def error(self, msg: str, detail: str = ""):
        item = CheckItem(Status.ERROR, msg, detail)
        self.items.append(item); self._push(item)

    def append_log(self, text: str):
        self.raw_log += text + "\n"

    # ── Aggregates ────────────────────────────────────────────────────────────
    @property
    def pass_count(self):
        return sum(1 for i in self.items if i.status == Status.PASS)

    @property
    def fail_count(self):
        return sum(1 for i in self.items if i.status == Status.FAIL)

    @property
    def warn_count(self):
        return sum(1 for i in self.items if i.status in (Status.WARN, Status.ERROR))

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

    def to_dict(self) -> dict:
        return {
            "name":       self.name,
            "category":   self.category,
            "items":      [i.to_dict() for i in self.items],
            "raw_log":    self.raw_log,
            "duration_s": self.duration_s,
            "worst_status": self.worst_status.value,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "warn_count": self.warn_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Section":
        s = cls(name=d["name"], category=d["category"])
        s.items   = [CheckItem.from_dict(i) for i in d.get("items", [])]
        s.raw_log = d.get("raw_log", "")
        return s


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

    def to_dict(self) -> dict:
        return {
            "cluster_name":  self.cluster_name,
            "cluster_type":  self.cluster_type,
            "environment":   self.environment,
            "description":   self.description,
            "login_success": self.login_success,
            "login_error":   self.login_error,
            "overall_status": self.overall_status.value,
            "pass_count":    self.pass_count,
            "fail_count":    self.fail_count,
            "warn_count":    self.warn_count,
            "duration_s":    self.duration_s,
            "sections":      [s.to_dict() for s in self.sections],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ClusterResult":
        r = cls(
            cluster_name  = d["cluster_name"],
            cluster_type  = d["cluster_type"],
            environment   = d.get("environment", ""),
            description   = d.get("description", ""),
            login_success = d.get("login_success", True),
            login_error   = d.get("login_error", ""),
        )
        r.sections = [Section.from_dict(s) for s in d.get("sections", [])]
        return r
