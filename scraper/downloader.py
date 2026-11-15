"""CSV Download handler for Spotify Charts.

Finds and triggers the Spotify Charts CSV download button on a page,
intercepts the download event via Playwright, and saves the file to
the designated downloads/ directory.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from chart_csv import validate_chart_csv

DEFAULT_DOWNLOADS_DIR = Path(__file__).resolve().parent.parent / "downloads"

# Ordered list of reliable CSS selectors for the CSV download button
CSV_BUTTON_SELECTORS = [
    'button[aria-labelledby="csv_download"]',
    'div[class*="CSVLink"] button',
    'button[data-encore-id="buttonTertiary"]:has(svg path[d*="M12 6.05"])',
]


def find_download_button(page: Page, timeout_ms: int = 10000):
    """Find the CSV download button using fallback selectors."""
    for selector in CSV_BUTTON_SELECTORS:
        loc = page.locator(selector).first
        try:
            loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except PlaywrightTimeoutError:
            continue
    raise RuntimeError(
        f"Could not find CSV download button on {page.url} using selectors: {CSV_BUTTON_SELECTORS}"
    )


def download_chart_csv(
    page: Page,
    target_filename: str | None = None,
    output_dir: Path = DEFAULT_DOWNLOADS_DIR,
    timeout_ms: int = 25000,
) -> Path:
    """Click the CSV download button, intercept the file download, and save it.

    Parameters:
        page: Active Playwright Page on a chart view.
        target_filename: Optional custom filename (e.g. 'regional-global-weekly-2026-11-05.csv').
                         If None, the server's suggested filename is used.
        output_dir: Destination folder path (defaults to ./downloads).
        timeout_ms: Maximum wait time in milliseconds for the download to start.

    Returns:
        Path to the saved CSV file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[*] Locating CSV download button on: {page.url}")
    btn = find_download_button(page, timeout_ms=timeout_ms)

    print("[*] Triggering download via Playwright expect_download()...")
    with page.expect_download(timeout=timeout_ms) as download_info:
        btn.click()

    download = download_info.value
    suggested_name = download.suggested_filename
    expected = re.fullmatch(r"(regional-[a-z]+-weekly)-(\d{4}-\d{2}-\d{2})\.csv", target_filename or "")
    if expected and suggested_name.startswith("regional-"):
        chart, date_str = expected.groups()
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", suggested_name)
        if not suggested_name.startswith(f"{chart}-") or not dates or dates[-1] != date_str:
            raise ValueError(f"Server offered a different chart/week: {suggested_name}")
    final_filename = target_filename if target_filename else suggested_name
    destination = output_dir / final_filename

    # Keep partially saved/invalid downloads out of the CSV input directory.
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".part", delete=False) as handle:
            temporary_path = Path(handle.name)
        download.save_as(str(temporary_path))
        validate_chart_csv(temporary_path)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    if destination.exists():
        file_size = destination.stat().st_size
        print(f"[✓] Download complete! Saved to: {destination} ({file_size:,} bytes)")
    else:
        raise FileNotFoundError(f"Failed to save download to {destination}")

    return destination


def main():
    """CLI test for downloading a single chart date."""
    from browser import get_browser_session
    from chart_page import build_chart_url

    parser = argparse.ArgumentParser(
        description="Download CSV for a specific Spotify Charts date."
    )
    parser.add_argument(
        "date",
        help="Chart date in YYYY-MM-DD format (e.g. 2026-11-05)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_DOWNLOADS_DIR,
        help="Directory to save downloaded CSV files (default: downloads/)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode",
    )
    args = parser.parse_args()

    url = build_chart_url(args.date)
    print(f"[*] Opening chart for {args.date} and downloading CSV...")

    with get_browser_session(headless=args.headless) as (context, page):
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        saved_file = download_chart_csv(
            page=page,
            target_filename=f"regional-global-weekly-{args.date}.csv",
            output_dir=args.output_dir,
        )
        print(f"[✓] Done: {saved_file}")


if __name__ == "__main__":
    main()
