"""Ensure scraper and pipeline modules are importable from tests."""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
for _folder in ("scraper", "pipeline"):
    _path = str(PROJECT_DIR / _folder)
    if _path not in sys.path:
        sys.path.insert(0, _path)
