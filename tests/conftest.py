import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "worker"), str(ROOT / "bench")]

# Both services read these at import time. Nothing here connects until a test asks it to.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("DATABASE_URL", os.getenv("TEST_DATABASE_URL", "postgresql://localhost/test"))
os.environ.setdefault("PANEL_PASSWORD", "test-password-123")


def load_module(name: str, path: Path):
    """worker/main.py and panel/main.py share a file name, so load each under its own module name."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
