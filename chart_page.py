"""Spotify Chart page handler.

Handles constructing chart URLs, navigating to a specific chart date,
and basic DOM readiness verification. Can be used as an imported module
or run directly from CLI to test a single date.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from downloader import DEFAULT_DOWNLOADS_DIR, download_chart_csv

BASE_VIEW_URL = "https://charts.spotify.com/charts/view"
DEFAULT_CHART_TYPE = "regional-global-weekly"


def build_chart_url(date_str: str, chart_type: str = DEFAULT_CHART_TYPE) -> str:
    """Construct full Spotify Charts URL for a given date and chart type.

    Example:
        build_chart_url("2026-11-05")
        -> "https://charts.spotify.com/charts/view/regional-global-weekly/2026-11-05"
    """
    clean_date = date_str.strip().strip("/")
    clean_type = chart_type.strip().strip("/")
    return f"{BASE_VIEW_URL}/{clean_type}/{clean_date}"


def open_chart_for_date(
    page: Page,
    date_str: str,
    chart_type: str = DEFAULT_CHART_TYPE,
    wait_for_selector: str = "table, [role='table'], [data-testid='table']",
    timeout_ms: int = 30000,
) -> dict:
    """Navigate page to the specified chart date and verify readiness.

    Returns a status dictionary with details about the load.
    """
    target_url = build_chart_url(date_str, chart_type)
    print(f"[*] Navigating to: {target_url}")

    response = page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
    http_status = response.status if response else None

    # Wait for dynamic table / chart content to attach if present
    table_found = False
    if wait_for_selector:
        try:
            page.locator(wait_for_selector).first.wait_for(
                state="attached", timeout=8000
            )
            table_found = True
        except PlaywrightTimeoutError:
            table_found = False

    result = {
        "date": date_str,
        "chart_type": chart_type,
        "url": page.url,
        "http_status": http_status,
        "title": page.title(),
        "table_ready": table_found,
    }

    print(f"[✓] Loaded: {result['title']} (HTTP {http_status}, Table ready: {table_found})")
    return result


def open_and_download_chart(
    page: Page,
    date_str: str,
    chart_type: str = DEFAULT_CHART_TYPE,
    output_dir: Path = DEFAULT_DOWNLOADS_DIR,
    target_filename: str | None = None,
    wait_seconds: float = 1.0,
) -> tuple[dict, Path]:
    """Navigate to a chart date, ensure readiness, wait, and download the CSV."""
    status = open_chart_for_date(page, date_str=date_str, chart_type=chart_type)
    
    # Wait configured seconds after page load before triggering download
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    filename = target_filename or f"{chart_type}-{date_str}.csv"
    saved_path = download_chart_csv(
        page=page,
        target_filename=filename,
        output_dir=output_dir,
    )
    status["csv_path"] = str(saved_path)
    return status, saved_path


def main():
    """CLI entrypoint for testing a single date directly."""
    from browser import get_browser_session

    parser = argparse.ArgumentParser(
        description="Open Spotify Charts for a specific date."
    )
    parser.add_argument(
        "date",
        help="Date string in YYYY-MM-DD format (e.g. 2026-11-05)",
    )
    parser.add_argument(
        "--chart-type",
        default=DEFAULT_CHART_TYPE,
        help=f"Chart type identifier (default: {DEFAULT_CHART_TYPE})",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download CSV after opening chart",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=5.0,
        help="Seconds to wait on the page before closing (default: 5.0)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run in headless mode",
    )
    args = parser.parse_args()

    print(f"[*] Running single-date test for: {args.date}")
    with get_browser_session(headless=args.headless) as (context, page):
        if args.download:
            res, csv_file = open_and_download_chart(
                page=page,
                date_str=args.date,
                chart_type=args.chart_type,
            )
            print(f"[✓] Saved CSV: {csv_file}")
        else:
            res = open_chart_for_date(page, date_str=args.date, chart_type=args.chart_type)

        print(f"[*] Waiting {args.wait}s...")
        time.sleep(args.wait)
        print("[*] Done with date test.")


if __name__ == "__main__":
    main()
