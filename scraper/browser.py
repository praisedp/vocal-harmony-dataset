"""Browser context management for Spotify automation.

Handles launching the dedicated persistent Chrome context with stealth flags
and session preservation, as recommended in the Google Chrome Browser Automation Guide.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from playwright.sync_api import BrowserContext, Page, sync_playwright

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_DIR = PROJECT_DIR / ".chrome-automation-profile"
DEFAULT_DOWNLOADS_DIR = PROJECT_DIR / "downloads"


def launch_chrome_context(
    playwright,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    downloads_dir: Path = DEFAULT_DOWNLOADS_DIR,
    headless: bool = False,
    viewport: dict | None = None,
) -> BrowserContext:
    """Launch a persistent Chrome browser context with session persistence and downloads configured."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)

    if viewport is None:
        viewport = {"width": 1280, "height": 850}

    launch_kwargs = {
        "user_data_dir": str(profile_dir),
        "downloads_path": str(downloads_dir),
        "accept_downloads": True,
        "headless": headless,
        "viewport": viewport,
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    }

    try:
        context = playwright.chromium.launch_persistent_context(
            channel="chrome", **launch_kwargs
        )
    except Exception as err:
        print(f"[!] Warning: Could not launch system Chrome ({err}). Falling back to Chromium...")
        context = playwright.chromium.launch_persistent_context(**launch_kwargs)

    # Mask navigator.webdriver
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
            configurable: true
        });
        """
    )

    context.set_default_timeout(15000)
    context.set_default_navigation_timeout(45000)

    return context


@contextmanager
def get_browser_session(
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    downloads_dir: Path = DEFAULT_DOWNLOADS_DIR,
    headless: bool = False,
) -> Generator[tuple[BrowserContext, Page], None, None]:
    """Context manager that yields (context, page) and guarantees clean shutdown on exit."""
    with sync_playwright() as playwright:
        context = launch_chrome_context(
            playwright=playwright,
            profile_dir=profile_dir,
            downloads_dir=downloads_dir,
            headless=headless,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield context, page
        finally:
            context.close()
