"""Spotify Chart page handler.

Handles constructing chart URLs, navigating to a specific chart date,
and basic DOM readiness verification. Can be used as an imported module
or run directly from CLI to test a single date.
"""

from __future__ import annotations

import argparse
import math
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError

from downloader import DEFAULT_DOWNLOADS_DIR, download_chart_csv

BASE_VIEW_URL = "https://charts.spotify.com/charts/view"
DEFAULT_CHART_TYPE = "regional-global-weekly"
TABLE_TIMEOUT_MS = 25000
TABLE_REFRESH_INTERVAL_MS = 5000
CHART_NOT_FOUND_SELECTOR = '[data-encore-id="emptyState"] h3:text-is("Couldn\'t find this chart")'


class ChartUnavailableError(RuntimeError):
    """Spotify explicitly displayed its unavailable-chart empty state."""


def check_chart_unavailable(page: Page, expected_url: str | None = None) -> None:
    if page.locator(CHART_NOT_FOUND_SELECTOR).is_visible():
        if expected_url is not None and urlsplit(page.url).path.rstrip("/") != urlsplit(expected_url).path.rstrip("/"):
            raise RuntimeError(f"Unavailable page is for a different region/week: {page.url}")
        raise ChartUnavailableError("Spotify displayed: Couldn't find this chart")


def wait_for_chart_table(page: Page, selector: str, expected_url: str | None = None) -> tuple[bool, int | None]:
    """Give the table 25 seconds, refreshing at 5, 10, 15 and 20 seconds.

    Reload time counts toward the same deadline. A successful table load stops
    recovery immediately; transient reload errors do not reset the deadline.
    """
    deadline = time.monotonic() + TABLE_TIMEOUT_MS / 1000
    interval = TABLE_REFRESH_INTERVAL_MS / 1000
    next_refresh = deadline - TABLE_TIMEOUT_MS / 1000 + interval
    last_http_status = None
    while True:
        check_chart_unavailable(page, expected_url)
        now = time.monotonic()
        if now >= deadline:
            return False, last_http_status
        wait_seconds = min(next_refresh, deadline) - now
        if wait_seconds > 0:
            try:
                # Wake for either outcome, so an explicit unavailable message
                # does not consume the 25-second refresh window.
                page.locator(f"{selector}, {CHART_NOT_FOUND_SELECTOR}").first.wait_for(
                    state="visible", timeout=max(1, math.ceil(wait_seconds * 1000))
                )
                check_chart_unavailable(page, expected_url)
                return True, last_http_status
            except PlaywrightTimeoutError:
                check_chart_unavailable(page, expected_url)
                pass
        now = time.monotonic()
        if now >= deadline:
            return False, last_http_status
        if now >= next_refresh:
            # Commit waits only for navigation to begin; the table wait below
            # handles hydration. Bound reloads by the next refresh/deadline.
            reload_budget = min(next_refresh + interval, deadline) - now
            print("[*] Chart table is still loading; refreshing the page...")
            try:
                response = page.reload(
                    wait_until="commit", timeout=max(1, math.ceil(reload_budget * 1000))
                )
                if response is not None:
                    last_http_status = response.status
            except PlaywrightError as err:
                if page.is_closed():
                    raise
                print(f"[!] Refresh did not finish; continuing within the table deadline: {err}")
            next_refresh += interval


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
    timeout_ms: int = 25000,
) -> dict:
    """Navigate page to the specified chart date and verify readiness.

    Returns a status dictionary with details about the load.
    """
    target_url = build_chart_url(date_str, chart_type)
    print(f"[*] Navigating to: {target_url} (timeout: {timeout_ms/1000:.1f}s)")

    http_status = None
    try:
        response = page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        http_status = response.status if response else None
    except PlaywrightError as err:
        if page.is_closed():
            raise
        # A timed-out/offline navigation still gets the same table recovery
        # window, including previously failed weeks with a 5-second goto limit.
        print(f"[!] Navigation did not finish; trying table refresh recovery: {err}")

    check_chart_unavailable(page, target_url)

    # Wait for dynamic table / chart content to attach if present
    table_found = False
    if wait_for_selector:
        table_found, refreshed_status = wait_for_chart_table(page, wait_for_selector, target_url)
        if refreshed_status is not None:
            http_status = refreshed_status

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
    page_timeout_ms: int = 25000,
    download_timeout_ms: int = 25000,
) -> tuple[dict, Path]:
    """Navigate to a chart date, ensure readiness, wait, and download the CSV."""
    status = open_chart_for_date(
        page=page,
        date_str=date_str,
        chart_type=chart_type,
        timeout_ms=page_timeout_ms,
    )

    expected_path = urlsplit(build_chart_url(date_str, chart_type)).path.rstrip("/")
    if urlsplit(status["url"]).path.rstrip("/") != expected_path:
        raise RuntimeError(f"Chart redirected away from requested region/week: {status['url']}")
    if status["http_status"] is not None and status["http_status"] >= 400:
        raise RuntimeError(f"Chart returned HTTP {status['http_status']}")
    if not status["table_ready"]:
        raise RuntimeError("Requested chart table did not load within 25 seconds, including refreshes")

    # Wait configured seconds after page load before triggering download
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    check_chart_unavailable(page, build_chart_url(date_str, chart_type))

    filename = target_filename or f"{chart_type}-{date_str}.csv"
    saved_path = download_chart_csv(
        page=page,
        target_filename=filename,
        output_dir=output_dir,
        timeout_ms=download_timeout_ms,
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
