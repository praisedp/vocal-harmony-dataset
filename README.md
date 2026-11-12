# Spotify Charts Historical CSV Downloader

A robust, modular Python Playwright automation suite for downloading historical [Spotify Charts](https://charts.spotify.com) weekly CSV files backwards from a start date down to December 2016, with automatic session persistence, Rich terminal progress, and automatic resume from `completed.txt`.

Built following the [Google Chrome Browser Automation Guide](https://app.notion.com/p/Google-Chrome-Browser-Automation-Guide-4163ce5b6bf98180b757f7f9e430f103).

---

## Key Features

1. **Fully Configurable via [config.json](file:///Users/praise/Developer/spotify-charts/config.json):**
   - Easily change `start_date`, `stop_date`, `chart_type`, wait times, and folders without editing Python code.
2. **Auto-Resume via `completed.txt`:**
   - Every completed date is recorded immediately in `completed.txt`.
   - You can stop the runner anytime (`Ctrl+C`). When rerun, it checks the oldest completed date and automatically resumes from the week before it.
3. **Rich Terminal Interface:**
   - Real-time progress bar showing current active week, completed count, remaining count, elapsed time, and ETA.
4. **Stealth & Session Persistence:**
   - Reuses `.chrome-automation-profile/` with installed Chrome and anti-detection flags (`--disable-blink-features=AutomationControlled`, masked `navigator.webdriver`).
5. **Fast & Resilient Timing:**
   - Waits 1s after page load before triggering download.
   - Proceeds immediately to the next date the instant the CSV download finishes.
   - Waits 3s on failure before proceeding to avoid hammering the site.

---

## File Structure

| File | Purpose |
| --- | --- |
| [config.json](file:///Users/praise/Developer/spotify-charts/config.json) | Central settings file (`start_date`, `stop_date`, wait delays, directories). |
| [config.py](file:///Users/praise/Developer/spotify-charts/config.py) | Python configuration loader and fallback defaults. |
| [main.py](file:///Users/praise/Developer/spotify-charts/main.py) | **Batch Runner:** Orchestrates navigation, downloads, Rich progress bar, and auto-resume. |
| [date_utils.py](file:///Users/praise/Developer/spotify-charts/date_utils.py) | **Date & Resume Logic:** Calculates weekly reverse dates, tracks `completed.txt`, and computes remaining dates. |
| [downloader.py](file:///Users/praise/Developer/spotify-charts/downloader.py) | **CSV Downloader:** Locates download button, handles Playwright download events, and saves to `downloads/`. |
| [chart_page.py](file:///Users/praise/Developer/spotify-charts/chart_page.py) | **Single-Date Page Handler:** Navigates to a specific date URL and verifies readiness. |
| [browser.py](file:///Users/praise/Developer/spotify-charts/browser.py) | **Browser Lifecycle:** Manages persistent Chrome context and downloads configuration. |
| [login.py](file:///Users/praise/Developer/spotify-charts/login.py) | Interactive utility to sign in or refresh your Spotify session if needed. |

---

## Configuration Settings ([config.json](file:///Users/praise/Developer/spotify-charts/config.json))

```json
{
  "start_date": "2026-11-05",
  "stop_date": "2016-12-01",
  "chart_type": "regional-global-weekly",
  "page_load_wait_seconds": 1.0,
  "failure_retry_wait_seconds": 3.0,
  "downloads_dir": "downloads",
  "completed_file": "completed.txt",
  "headless": false,
  "profile_dir": ".chrome-automation-profile",
  "download_timeout_ms": 25000
}
```

---

## How to Run

### 1. Run the Batch Downloader
Starts downloading from `start_date` (`2026-11-05`), stepping backwards week-by-week:

```bash
uv run python main.py
```

### 2. Pausing & Resuming
- Press **`Ctrl+C`** in the terminal anytime to pause or cancel.
- Run `uv run python main.py` again. The script inspects `completed.txt`, identifies the oldest completed week, and resumes immediately from the next week backwards.

### 3. Command-Line Overrides (Optional)

```bash
# Override start date on the fly
uv run python main.py --start-date 2026-10-22

# Run headless in the background
uv run python main.py --headless
```

### 4. Standalone Helpers

```bash
# Preview date calculations
uv run python date_utils.py 2026-11-05 --preview 10

# Test downloading a single date
uv run python downloader.py 2026-11-05

# Refresh session if cookies ever expire
uv run python login.py
```
