# Spotify Charts Historical CSV Downloader

A modular Python Playwright automation suite for downloading historical [Spotify Charts](https://charts.spotify.com) weekly CSV files for Global and all 73 countries/markets in the supplied dropdown. It processes Global first, then each country in dropdown order, with independent progress, validation and retries.

Built following the [Google Chrome Browser Automation Guide](https://app.notion.com/p/Google-Chrome-Browser-Automation-Guide-4163ce5b6bf98180b757f7f9e430f103).

---

## Key Features

1. **Fully Configurable via [config.json](file:///Users/praise/Developer/spotify-charts/config.json):**
   - Change `start_date`, `stop_date`, `chart_types`, wait times, and folders without editing Python code. `chart_types: "all"` selects all 74 locations; a list such as `["global", "co", "au"]` selects a subset. Older configurations containing only `chart_type` still select one chart.
2. **Failed Date Retries (`state/failed.txt`):**
   - Any date that encounters a download error or fails to load is automatically recorded in `state/failed.txt`.
   - On every rerun, failed dates are retried alongside other missing weeks in newest-to-oldest order. This avoids hitting an old unavailable retry before collecting newer charts.
   - Retries use a reduced 5.0s navigation timeout (`retry_page_timeout_seconds`), while downloads retain their own timeout. These are per-operation limits, not a five-second total attempt limit.
   - After initial navigation, both fresh and failed weeks get a separate 25-second table-loading window. While the table is missing, the page refreshes at 5, 10, 15 and 20 seconds; reload time counts toward that same window. Navigation timeouts/network errors also enter this recovery window. Recovery stops as soon as the table appears.
   - When a failed date succeeds on retry, it is added to `state/completed.txt` and removed from `state/failed.txt`.
3. **Auto-Resume via `state/completed.txt`:**
   - Global keeps `state/completed.txt` and `state/failed.txt`. Each country uses `state/<chart-type>/completed.txt` and `failed.txt`, so Global dates never suppress country downloads.
   - Every run checks every week in scope, filling gaps above any saved availability cutoff rather than jumping past the oldest completed date. Completed records count only when a valid CSV exists. Missing/corrupt files are retried; valid files saved before a crash are recovered.
   - An in-flight date is recorded as failed before navigation, then cleared only after a valid CSV is saved and completion is recorded. `Ctrl+C` stops the whole run and leaves it resumable.
4. **Rich Terminal Interface:**
   - Real-time progress bar showing current active week, completed count, remaining count, elapsed time, and ETA.
5. **Stealth & Session Persistence:**
   - Reuses `.chrome-automation-profile/` with installed Chrome and anti-detection flags (`--disable-blink-features=AutomationControlled`, masked `navigator.webdriver`).
6. **Fast & Resilient Timing:**
   - Waits 1s after page load before triggering download.
   - Proceeds immediately to the next date the instant the CSV download finishes.
   - Waits 3s on failure before proceeding to avoid hammering the site.
7. **Verified Coverage:**
   - Reports validated CSV weeks, failed weeks, and outstanding weeks separately per region. Failed dates never count as complete. After attempting a region's range, the scraper continues to the next country even if failures remain; a rerun revisits those failures.
   - When Spotify displays the exact "Couldn't find this chart" heading in its empty-state panel, that location stops immediately and the next location begins. The date is saved to `state/<chart-type>/unavailable_from.txt`. Missing weeks at that date and earlier are skipped on future runs, including old failed dates; existing valid older CSVs are preserved. Coverage reports cutoff skips separately from completed downloads and failures.
   - This stopping policy follows the observed unavailable page; it does not establish the country's true chart launch date, since Spotify's message can also mean a chart was taken down. Timeouts, network errors and generic HTTP errors do not create cutoffs. Short charts and gaps in ranks are allowed, as seen in existing exports. Validation confirms a usable chart file, not that Spotify supplied all 200 entries.
   - To deliberately recheck older dates, remove that location's `unavailable_from.txt` before restarting. Ordinary failures above the cutoff remain retryable.
   - Files are named `downloads/<chart-type>-YYYY-MM-DD.csv`, preventing country/date collisions and remaining compatible with the master builder. Downloads are validated and atomically published; malformed files, HTTP errors, redirects to another week/region, and mismatched server chart filenames are rejected.

---

## File Structure

| File | Purpose |
| --- | --- |
| [scraper/regions.py](file:///Users/praise/Developer/spotify-charts/scraper/regions.py) | All 74 dropdown locations, their names, country codes and weekly chart identifiers. |
| [scraper/chart_csv.py](file:///Users/praise/Developer/spotify-charts/scraper/chart_csv.py) | Downloaded chart header, row and rank validation. |
| [pipeline/build_master.py](file:///Users/praise/Developer/spotify-charts/pipeline/build_master.py) | **Dataset Aggregator & Deduplicator:** Scans `downloads/*.csv`, filters duplicates by Spotify URI, and builds/updates `data/master.csv` incrementally. |
| [pipeline/build_master_genre.py](file:///Users/praise/Developer/spotify-charts/pipeline/build_master_genre.py) | **Last.fm Genre & Metadata Enricher:** Incrementally enriches `data/master.csv` into `data/master_genre.csv` with resumable artist caching. |
| [config.json](file:///Users/praise/Developer/spotify-charts/config.json) | Central settings file (`start_date`, `stop_date`, wait delays, directories). |
| [scraper/config.py](file:///Users/praise/Developer/spotify-charts/scraper/config.py) | Python configuration loader and fallback defaults. |
| [scraper/main.py](file:///Users/praise/Developer/spotify-charts/scraper/main.py) | **Batch Runner:** Global then country-by-country, one shared authenticated browser, independent retries and coverage audit. |
| [scraper/date_utils.py](file:///Users/praise/Developer/spotify-charts/scraper/date_utils.py) | **Date & State Logic:** Calculates weekly reverse dates, tracks `state/completed.txt` and `state/failed.txt`, and computes remaining dates. |
| [scraper/downloader.py](file:///Users/praise/Developer/spotify-charts/scraper/downloader.py) | **CSV Downloader:** Locates download button, handles Playwright download events, and saves to `downloads/`. |
| [scraper/chart_page.py](file:///Users/praise/Developer/spotify-charts/scraper/chart_page.py) | **Single-Date Page Handler:** Navigates to a specific date URL with configurable timeouts and verifies readiness. |
| [scraper/browser.py](file:///Users/praise/Developer/spotify-charts/scraper/browser.py) | **Browser Lifecycle:** Manages persistent Chrome context and downloads configuration. |
| [scraper/login.py](file:///Users/praise/Developer/spotify-charts/scraper/login.py) | Interactive utility to sign in or refresh your Spotify session if needed. |
| `tests/` | Offline `unittest` regression suite (`test_scraper.py`, `test_table_recovery.py`, `test_build_master.py`, `test_build_master_genre.py`). |
| `data/samples/` | Sample Last.fm output CSVs (`sample_genres.csv`, `sample_100_full.csv`). |

---

## Configuration Settings ([config.json](file:///Users/praise/Developer/spotify-charts/config.json))

```json
{
  "start_date": "2026-11-05",
  "stop_date": "2016-12-01",
  "chart_type": "regional-global-weekly",
  "chart_types": "all",
  "state_dir": "state",
  "page_load_wait_seconds": 1.0,
  "failure_retry_wait_seconds": 3.0,
  "retry_page_timeout_seconds": 5.0,
  "standard_page_timeout_seconds": 25.0,
  "downloads_dir": "downloads",
  "completed_file": "state/completed.txt",
  "failed_file": "state/failed.txt",
  "headless": false,
  "profile_dir": ".chrome-automation-profile",
  "download_timeout_ms": 25000
}
```

---

## How to Run

### 1. Run the Batch Downloader
If an older scraper is running, stop it with `Ctrl+C` first, then restart with the command below. Existing progress and downloads are reused. The configured range has 519 weeks per location (38,406 region/weeks across all 74); not all locations necessarily have data that far back.

```bash
uv run python scraper/main.py
```

### 2. Execution Flow
For Global, then each country:

1. Audit all requested weeks against the saved CSVs and that region's ledgers.
2. Attempt missing and failed weeks in one newest-to-oldest sequence, respecting any saved cutoff.
3. Stop the location on the explicit unavailable-chart panel, save its cutoff and move to the next location. Other failures stay recorded and do not stop the sequence. Final coverage separates cutoff skips from outstanding failures.

The process exits with code 1 if non-skipped requested weeks remain outstanding, 130 when interrupted, and 0 when all non-skipped weeks are verified. Read-only `--status` exits with 0 after reporting coverage, including cutoff skips.

### 3. Pausing & Resuming
- Press **`Ctrl+C`** in the terminal anytime to pause or cancel.
- Run `uv run python scraper/main.py` again. It checks all selected regions and skips verified CSVs, retries failures within each region, and fills every remaining gap.
- Run one scraper at a time; the Chrome profile and date ledgers are shared.

### 4. Build / Update Master Dataset (`data/master.csv`)
Consolidates all downloaded weekly CSVs into a single deduplicated dataset, ignoring temporary documents and only adding new tracks on subsequent runs:

```bash
uv run python pipeline/build_master.py
```

The script runs independently of the downloader and saves to `data/master.csv`.
Default paths are relative to the project root, so it also works when launched from
another directory. It reads only visible files ending in `.csv` (case insensitive),
ignoring extensionless documents and temporary files.

Duplicates are identified by Spotify URI **or** the same artist/title pair, ignoring
case and repeated whitespace. This also merges alternate Spotify IDs for the same
artist/title; distinct recordings with identical artist/title text are therefore
treated as one song. Punctuation and accents are preserved.

Existing master rows stay unchanged. Only new songs are added; rerunning without
new songs leaves the file untouched. Following the original builder's format,
`rank` is a sequential catalog number, not a weekly chart position. Other values
come from the first occurrence found in filename order; they are not historical
aggregates. Invalid input CSVs are reported and skipped; an invalid existing master
stops the update. Updates are published atomically to protect the master if writing
fails. Run one builder at a time.

```bash
# Optional input/output locations and preview size
uv run python pipeline/build_master.py --input-dir downloads --output data/master.csv --preview 10

# Enrich tracks with Last.fm genre & metadata into data/master_genre.csv (resumable)
uv run python pipeline/build_master_genre.py

# Verify incremental updates using temporary test data
uv run python -m unittest tests/test_build_master.py tests/test_build_master_genre.py
```

### YouTube Music discovery sample

Search for five song candidates per track for 100 random entries from `data/master.csv`:

```bash
uv run python pipeline/sample_ytmusic.py
```

Defaults: seed `42`, country context `LK`, one-second spacing, no login. Each run
creates a new `data/samples/ytmusic_<timestamp>/` containing `sample.csv`,
`candidates.csv`, `raw.jsonl` and `summary.json`. Candidate rank is search order,
not a verified match. Missing metadata stays blank. Responses are flushed after
each query, and failures are recorded. This only discovers songs; it downloads no
audio. Use `--count`, `--seed`, `--limit`, `--location`, `--delay` or `--output-dir`
to customize. An explicit output directory must not already exist.

### 5. Command-Line Overrides (Optional)

```bash
# Override start date on the fly
uv run python scraper/main.py --start-date 2026-10-22

# Run headless in the background
uv run python scraper/main.py --headless

# Select just Global, Colombia and Australia
uv run python scraper/main.py --regions global co au

# List all dropdown locations
uv run python scraper/main.py --list-regions

# Audit all configured locations without opening Chrome or modifying state
uv run python scraper/main.py --status

# Run offline regression tests (no Chrome or network)
uv run python -m unittest discover -s tests -v
```

### 6. Standalone Helpers

```bash
# Preview date calculations
uv run python scraper/date_utils.py 2026-11-05 --preview 10

# Test downloading a single date
uv run python scraper/downloader.py 2026-11-05

# Refresh session if cookies ever expire
uv run python scraper/login.py
```
