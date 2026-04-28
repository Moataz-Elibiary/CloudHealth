from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime
from enum import Enum

class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    INFO = "INFO"
    SKIP = "SKIP"
    ERROR = "ERROR"
    RUNNING = "RUNNING"

@dataclass
class CheckItem:
    status: Status
    message: str
    name: str = ""
    detail: Optional[str] = None
    command: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def is_problem(self) -> bool:
        return self.status in (Status.FAIL, Status.ERROR, Status.WARN)

@dataclass
class SectionResult:
    name: str
    category: str = ""
    checks: List[CheckItem] = field(default_factory=list)
    raw_log: str = ""
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    def append_log(self, text: str):
        self.raw_log += text + "\n"

    def pass_(self, msg: str, detail: str = "", command: str = ""):
        self.checks.append(CheckItem(Status.PASS, msg, detail=detail, command=command))

    def fail(self, msg: str, detail: str = "", command: str = ""):
        self.checks.append(CheckItem(Status.FAIL, msg, detail=detail, command=command))

    def warn(self, msg: str, detail: str = "", command: str = ""):
        self.checks.append(CheckItem(Status.WARN, msg, detail=detail, command=command))

    def info(self, msg: str, detail: str = "", command: str = ""):
        self.checks.append(CheckItem(Status.INFO, msg, detail=detail, command=command))

    def skip(self, msg: str, detail: str = ""):
        self.checks.append(CheckItem(Status.SKIP, msg, detail=detail))

    def error(self, msg: str, detail: str = ""):
        self.checks.append(CheckItem(Status.ERROR, msg, detail=detail))

    @property
    def pass_count(self) -> int:
        return sum(1 for item in self.checks if item.status == Status.PASS)

    @property
    def fail_count(self) -> int:
        return sum(1 for item in self.checks if item.status == Status.FAIL)

    @property
    def warn_count(self) -> int:
        return sum(1 for item in self.checks if item.status in (Status.WARN, Status.ERROR))

    @property
    def status(self) -> Status:
        statuses = {item.status for item in self.checks}
        for candidate in (Status.FAIL, Status.ERROR, Status.WARN, Status.PASS, Status.INFO, Status.SKIP):
            if candidate in statuses:
                return candidate
        return Status.INFO

    @property
    def duration_s(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None

@dataclass
class ClusterResult:
    cluster_name: str
    cluster_type: str = ""
    environment: str = ""
    description: str = ""
    sections: List[SectionResult] = field(default_factory=list)
    login_success: bool = True
    login_error: str = ""
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None

    @property
    def pass_count(self) -> int:
        return sum(section.pass_count for section in self.sections)

    @property
    def fail_count(self) -> int:
        return sum(section.fail_count for section in self.sections)

    @property
    def warn_count(self) -> int:
        return sum(section.warn_count for section in self.sections)

    @property
    def overall_status(self) -> Status:
        if not self.login_success:
            return Status.ERROR
        if self.fail_count > 0:
            return Status.FAIL
        if self.warn_count > 0:
            return Status.WARN
        return Status.PASS

    @property
    def duration_s(self) -> Optional[float]:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None

    def to_dict(self) -> Dict[str, object]:
        def serialize_check(item: CheckItem) -> Dict[str, object]:
            return {
                "status": item.status.value,
                "message": item.message,
                "name": item.name,
                "detail": item.detail,
                "command": item.command,
                "timestamp": item.timestamp.isoformat(),
            }

        def serialize_section(section: SectionResult) -> Dict[str, object]:
            return {
                "name": section.name,
                "category": section.category,
                "status": section.status.value,
                "checks": [serialize_check(item) for item in section.checks],
                "raw_log": section.raw_log,
                "pass_count": section.pass_count,
                "fail_count": section.fail_count,
                "warn_count": section.warn_count,
                "start_time": section.start_time.isoformat() if section.start_time else None,
                "end_time": section.end_time.isoformat() if section.end_time else None,
                "duration_s": section.duration_s,
            }

        return {
            "cluster_name": self.cluster_name,
            "cluster_type": self.cluster_type,
            "environment": self.environment,
            "description": self.description,
            "sections": [serialize_section(section) for section in self.sections],
            "login_success": self.login_success,
            "login_error": self.login_error,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_s": self.duration_s,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "warn_count": self.warn_count,
            "overall_status": self.overall_status.value,
        }
