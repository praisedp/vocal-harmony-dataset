"""Configuration loader for Spotify Charts batch automation.

Loads parameters from config.json with robust fallbacks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE_PATH = Path("config.json").resolve()


@dataclass
class ScraperConfig:
    start_date: str = "2026-11-05"
    stop_date: str = "2016-12-01"
    chart_type: str = "regional-global-weekly"
    page_load_wait_seconds: float = 1.0
    failure_retry_wait_seconds: float = 3.0
    downloads_dir: Path = Path("downloads").resolve()
    completed_file: Path = Path("completed.txt").resolve()
    headless: bool = False
    profile_dir: Path = Path(".chrome-automation-profile").resolve()
    download_timeout_ms: int = 25000

    @classmethod
    def load(cls, config_path: Path = CONFIG_FILE_PATH) -> ScraperConfig:
        """Load configuration from a JSON file, filling missing values with defaults."""
        data = {}
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"[!] Warning: Could not parse {config_path} ({e}), using default settings.")

        downloads_dir = Path(data.get("downloads_dir", "downloads")).resolve()
        completed_file = Path(data.get("completed_file", "completed.txt")).resolve()
        profile_dir = Path(data.get("profile_dir", ".chrome-automation-profile")).resolve()

        return cls(
            start_date=str(data.get("start_date", "2026-11-05")),
            stop_date=str(data.get("stop_date", "2016-12-01")),
            chart_type=str(data.get("chart_type", "regional-global-weekly")),
            page_load_wait_seconds=float(data.get("page_load_wait_seconds", 1.0)),
            failure_retry_wait_seconds=float(data.get("failure_retry_wait_seconds", 3.0)),
            downloads_dir=downloads_dir,
            completed_file=completed_file,
            headless=bool(data.get("headless", False)),
            profile_dir=profile_dir,
            download_timeout_ms=int(data.get("download_timeout_ms", 25000)),
        )


def get_config(config_path: Path = CONFIG_FILE_PATH) -> ScraperConfig:
    return ScraperConfig.load(config_path)
