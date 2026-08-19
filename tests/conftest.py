"""
Shared pytest setup.

Points the app at a throwaway database BEFORE any app module is imported,
so running the test suite never touches the real chess_tracker.db (which
has real personal game history in it). This has to happen at module import
time, before pytest collects test files that `import db` / `import config`
etc., which is exactly what conftest.py guarantees.
"""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TEST_DB_DIR = tempfile.mkdtemp(prefix="chess_tracker_test_")
os.environ["DB_PATH"] = str(Path(_TEST_DB_DIR) / "test_chess_tracker.db")
# Not a pytest fixture (nothing here can be — this all has to run at plain
# module-import time, before pytest even starts collecting), so cleanup
# can't be a fixture teardown either: register a plain interpreter-exit
# hook instead. Without it, every local `pytest` run left an orphaned
# chess_tracker_test_* directory under the OS temp folder.
atexit.register(shutil.rmtree, _TEST_DB_DIR, ignore_errors=True)

APP_DIR = Path(__file__).parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

FIXTURES_DIR = Path(__file__).parent / "fixtures"
