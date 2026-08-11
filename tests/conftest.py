"""
Shared pytest setup.

Points the app at a throwaway database BEFORE any app module is imported,
so running the test suite never touches the real chess_tracker.db (which
has real personal game history in it). This has to happen at module import
time, before pytest collects test files that `import db` / `import config`
etc., which is exactly what conftest.py guarantees.
"""

import os
import sys
import tempfile
from pathlib import Path

_TEST_DB_DIR = tempfile.mkdtemp(prefix="chess_tracker_test_")
os.environ["DB_PATH"] = str(Path(_TEST_DB_DIR) / "test_chess_tracker.db")

APP_DIR = Path(__file__).parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

FIXTURES_DIR = Path(__file__).parent / "fixtures"
