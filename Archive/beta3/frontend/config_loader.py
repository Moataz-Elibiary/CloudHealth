"""Frontend wrapper around the shared inventory/config loader."""

from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = BASE_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.inventory import InventoryLoader  # noqa: E402


class ConfigLoader(InventoryLoader):
    """Expose the backend inventory loader to the frontend package."""

