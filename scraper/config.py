"""Configuration loader for Spotify Charts batch automation.

Loads parameters from config.json with robust fallbacks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from regions import resolve_chart_types

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE_PATH = PROJECT_DIR / "config.json"


@dataclass
class ScraperConfig:
    start_date: str = "2026-11-05"
    stop_date: str = "2016-12-01"
    chart_type: str = "regional-global-weekly"
    chart_types: tuple[str, ...] | None = None
    state_dir: Path = PROJECT_DIR / "state"
    page_load_wait_seconds: float = 1.0
    failure_retry_wait_seconds: float = 3.0
    retry_page_timeout_seconds: float = 5.0
    standard_page_timeout_seconds: float = 25.0
    downloads_dir: Path = PROJECT_DIR / "downloads"
    completed_file: Path = PROJECT_DIR / "state/completed.txt"
    failed_file: Path = PROJECT_DIR / "state/failed.txt"
    headless: bool = False
    profile_dir: Path = PROJECT_DIR / ".chrome-automation-profile"
    download_timeout_ms: int = 25000

    @classmethod
    def load(cls, config_path: Path = CONFIG_FILE_PATH) -> ScraperConfig:
        """Load configuration from a JSON file, filling missing values with defaults."""
        data = {}
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                raise ValueError(f"Could not read configuration {config_path}: {e}") from e

        def configured_path(key: str, default: str) -> Path:
            return (config_path.resolve().parent / data.get(key, default)).resolve()

        downloads_dir = configured_path("downloads_dir", "downloads")
        completed_file = configured_path("completed_file", "state/completed.txt")
        failed_file = configured_path("failed_file", "state/failed.txt")
        profile_dir = configured_path("profile_dir", ".chrome-automation-profile")

        return cls(
            start_date=str(data.get("start_date", "2026-11-05")),
            stop_date=str(data.get("stop_date", "2016-12-01")),
            chart_type=str(data.get("chart_type", "regional-global-weekly")),
            chart_types=resolve_chart_types(data.get("chart_types", data.get("chart_type", "global"))),
            state_dir=configured_path("state_dir", "state"),
            page_load_wait_seconds=float(data.get("page_load_wait_seconds", 1.0)),
            failure_retry_wait_seconds=float(data.get("failure_retry_wait_seconds", 3.0)),
            retry_page_timeout_seconds=float(data.get("retry_page_timeout_seconds", 5.0)),
            standard_page_timeout_seconds=float(data.get("standard_page_timeout_seconds", 25.0)),
            downloads_dir=downloads_dir,
            completed_file=completed_file,
            failed_file=failed_file,
            headless=bool(data.get("headless", False)),
            profile_dir=profile_dir,
            download_timeout_ms=int(data.get("download_timeout_ms", 25000)),
        )


def get_config(config_path: Path = CONFIG_FILE_PATH) -> ScraperConfig:
    return ScraperConfig.load(config_path)
