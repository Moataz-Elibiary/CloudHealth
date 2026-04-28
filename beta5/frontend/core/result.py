"""
Proxy to backend/result.py — the canonical result model lives there.
Bootstraps sys.path so this import works even before config.py is loaded.
"""
import sys
from pathlib import Path

_backend = Path(__file__).resolve().parent.parent.parent / "backend"
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

from result import (  # noqa: F401, E402
    CheckItem, ClusterResult, SectionResult, Status,
)
