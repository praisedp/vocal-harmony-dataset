"""Utility script for manual Spotify login and initial session setup.

Run this whenever you need to log in or refresh your saved Spotify credentials:
    uv run python login.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from browser import DEFAULT_PROFILE_DIR, get_browser_session

LOGIN_URL = "https://charts.spotify.com"


def interactive_login(profile_dir: Path = DEFAULT_PROFILE_DIR) -> None:
    print(f"[*] Opening Spotify Charts for interactive login...")
    print(f"[*] Profile directory: {profile_dir}")

    with get_browser_session(profile_dir=profile_dir, headless=False) as (context, page):
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print("\n" + "=" * 60)
        print("[!] Log in to Spotify in the browser window.")
        print("    When finished, press [ENTER] in this terminal to save and exit.")
        print("=" * 60 + "\n")

        try:
            if sys.stdin.isatty():
                input(">> Press [ENTER] after logging in: ")
            else:
                for _ in range(60):
                    if not context.pages or context.pages[0].is_closed():
                        break
                    time.sleep(1)
        except (KeyboardInterrupt, EOFError):
            print("\n[*] Exiting...")

    print("[✓] Session state successfully persisted.")


if __name__ == "__main__":
    interactive_login()
