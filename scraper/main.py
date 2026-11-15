"""Download Global, then every selected country, with independent durable progress."""

from __future__ import annotations

import argparse
import csv
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
import time

from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, BarColumn, MofNCompleteColumn, TextColumn,
    TimeElapsedColumn, TimeRemainingColumn,
)
from rich.table import Table

from browser import get_browser_session
from chart_csv import validate_chart_csv
from chart_page import ChartUnavailableError, open_and_download_chart
from config import CONFIG_FILE_PATH, ScraperConfig, get_config
from date_utils import (
    generate_weekly_dates_backwards,
    load_completed_dates,
    load_failed_dates,
    parse_date,
    record_completed_date,
    record_failed_date,
    remove_failed_date,
    save_dates,
    sync_failed_dates,
)
from regions import GLOBAL_CHART, REGIONS, region_name, resolve_chart_types

console = Console()


@dataclass
class RegionPlan:
    config: ScraperConfig
    all_dates: list[str]
    completed: set[str]
    retries: list[str]
    remaining: list[str]
    skipped: set[str] = field(default_factory=set)
    cutoff: str | None = None

    @property
    def outstanding(self) -> int:
        return len(self.all_dates) - len(self.completed) - len(self.skipped)


def cutoff_file(config: ScraperConfig) -> Path:
    return config.state_dir / config.chart_type / "unavailable_from.txt"


def load_cutoff(config: ScraperConfig) -> str | None:
    dates = load_completed_dates(cutoff_file(config))
    if not dates:
        return None
    if len(dates) != 1:
        raise ValueError(f"Expected one cutoff date in {cutoff_file(config)}")
    return parse_date(dates[0]).isoformat()


def config_for_region(config: ScraperConfig, chart_type: str) -> ScraperConfig:
    """Keep legacy Global progress intact; countries never share date ledgers."""
    if chart_type == GLOBAL_CHART:
        completed_file, failed_file = config.completed_file, config.failed_file
    else:
        directory = config.state_dir / chart_type
        completed_file, failed_file = directory / "completed.txt", directory / "failed.txt"
    return replace(config, chart_type=chart_type, chart_types=None,
                   completed_file=completed_file, failed_file=failed_file)


def prepare_region(config: ScraperConfig, *, reconcile: bool = True) -> RegionPlan:
    """Check every requested week and its CSV, repairing progress after interruptions.

    Valid CSVs saved before a crash are recovered. Missing/corrupt CSVs previously
    marked complete become pending again. Read-only status uses reconcile=False.
    """
    dates = generate_weekly_dates_backwards(config.start_date, config.stop_date)
    scope = set(dates)
    cutoff = load_cutoff(config)
    recorded = load_completed_dates(config.completed_file)
    completed = set()
    for date_str in dates:
        path = config.downloads_dir / f"{config.chart_type}-{date_str}.csv"
        if not path.is_file():
            continue
        try:
            validate_chart_csv(path)
        except (OSError, UnicodeError, ValueError, csv.Error):
            continue
        completed.add(date_str)

    # Keep valid older CSVs as completed. A saved cutoff excludes missing weeks
    # at/before the observed unavailable page, rather than claiming downloads.
    skipped = {date_str for date_str in dates
               if cutoff is not None and date_str <= cutoff and date_str not in completed}

    invalid_records = (set(recorded) & scope) - completed
    recovered = [date_str for date_str in dates if date_str in completed and date_str not in recorded]
    if reconcile:
        if invalid_records or recovered:
            save_dates(config.completed_file,
                       [date_str for date_str in recorded if date_str not in invalid_records] + recovered)
        for date_str in dates:
            if date_str in invalid_records and date_str not in skipped:
                record_failed_date(config.failed_file, date_str)
        failed = set(sync_failed_dates(config.failed_file, config.completed_file))
    else:
        failed = set(load_failed_dates(config.failed_file)) | invalid_records
    retries = [date_str for date_str in dates
               if date_str in failed and date_str not in completed and date_str not in skipped]
    remaining = [date_str for date_str in dates
                 if date_str not in completed and date_str not in failed and date_str not in skipped]
    return RegionPlan(config, dates, completed, retries, remaining, skipped, cutoff)


def download_region(page, plan: RegionPlan) -> RegionPlan:
    """Work newest to oldest; stop at an explicit unavailable-chart page."""
    config = plan.config
    name = region_name(config.chart_type)
    console.print(f"\n[bold cyan]{name} ({config.chart_type})[/bold cyan]: "
                  f"{len(plan.completed)}/{len(plan.all_dates)} verified; "
                  f"{len(plan.retries)} retries, {len(plan.remaining)} other missing weeks")
    console.print(f"Failure records: {config.failed_file}")
    retry_dates = set(plan.retries)
    pending_dates = retry_dates | set(plan.remaining)
    queue = [(date_str, date_str in retry_dates) for date_str in plan.all_dates
             if date_str in pending_dates]
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                  MofNCompleteColumn(), TimeElapsedColumn(), TimeRemainingColumn(),
                  console=console) as progress:
        task = progress.add_task(name, total=len(queue))
        for date_str, retry in queue:
            progress.update(task, description=f"{name}: {date_str}" + (" (retry)" if retry else ""))
            # Write BEFORE navigation: Ctrl+C or a crash keeps the in-flight week retryable.
            record_failed_date(config.failed_file, date_str)
            try:
                _, saved_file = open_and_download_chart(
                    page=page,
                    date_str=date_str,
                    chart_type=config.chart_type,
                    output_dir=config.downloads_dir,
                    wait_seconds=config.page_load_wait_seconds,
                    page_timeout_ms=int(1000 * (config.retry_page_timeout_seconds if retry
                                               else config.standard_page_timeout_seconds)),
                    download_timeout_ms=config.download_timeout_ms,
                )
                expected = config.downloads_dir / f"{config.chart_type}-{date_str}.csv"
                if saved_file.resolve() != expected.resolve():
                    raise ValueError(f"Unexpected saved CSV path: {saved_file}")
                validate_chart_csv(saved_file)
            except ChartUnavailableError:
                # Save before leaving the country, so reruns never regenerate
                # the same unavailable date and older years from the full range.
                save_dates(cutoff_file(config), [date_str])
                remove_failed_date(config.failed_file, date_str)
                progress.console.print(
                    f"{name}: chart unavailable at {date_str}. Saved cutoff; "
                    "skipping this date and older weeks and moving to the next location.",
                    markup=False,
                )
                break
            except Exception as err:
                progress.console.print(f"Failed {name} {date_str}: {err}", markup=False)
                if config.failure_retry_wait_seconds > 0:
                    time.sleep(config.failure_retry_wait_seconds)
            else:
                # Ledger failures must stop the run, rather than masquerade as download errors.
                record_completed_date(config.completed_file, date_str)
                remove_failed_date(config.failed_file, date_str)
                plan.completed.add(date_str)
                progress.console.print(f"✓ {name} {date_str}: {saved_file.name}", markup=False)
            progress.advance(task)
    # Re-audit files and ledgers; attempted weeks are never confused with completed weeks.
    return prepare_region(config)


def print_summary(plans: list[RegionPlan]) -> None:
    table = Table(title="Verified chart coverage")
    for title in ("Location", "CSV weeks", "Weeks in scope", "Cutoff skips", "Failed / interrupted", "Outstanding"):
        table.add_column(title)
    for plan in plans:
        table.add_row(region_name(plan.config.chart_type), str(len(plan.completed)),
                      str(len(plan.all_dates)), str(len(plan.skipped)),
                      str(len(plan.retries)), str(plan.outstanding))
    console.print(table)
    outstanding = sum(plan.outstanding for plan in plans)
    if outstanding:
        console.print(f"[yellow]{outstanding:,} region/weeks remain outstanding. "
                      "Rerun to retry; each region keeps its own failure records.[/yellow]")
    else:
        skipped_count = sum(len(plan.skipped) for plan in plans)
        console.print(f"[green]All non-skipped region/weeks have validated CSVs. "
                      f"{skipped_count:,} weeks excluded by saved availability cutoffs.[/green]")


def run_batch_downloader(config: ScraperConfig, *, status_only: bool = False) -> list[RegionPlan]:
    charts = resolve_chart_types(list(config.chart_types) if config.chart_types else config.chart_type)
    console.print(f"{len(charts)} locations, {config.start_date} back to {config.stop_date}; "
                  "Global first when selected.")
    plans = []
    # Reuse one authenticated browser for all countries. No browser opens for status
    # or when all selected downloads are already verified on disk.
    with ExitStack() as stack:
        page = None
        for index, chart in enumerate(charts, start=1):
            console.print(f"[{index}/{len(charts)}] Checking {region_name(chart)}...")
            plan = prepare_region(config_for_region(config, chart), reconcile=not status_only)
            if plan.outstanding and not status_only:
                if page is None:
                    _, page = stack.enter_context(get_browser_session(
                        profile_dir=config.profile_dir, downloads_dir=config.downloads_dir,
                        headless=config.headless))
                plan = download_region(page, plan)
            plans.append(plan)
    print_summary(plans)
    return plans


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_FILE_PATH)
    parser.add_argument("--start-date", help="Override newest chart week (YYYY-MM-DD)")
    parser.add_argument("--stop-date", help="Override earliest chart week (YYYY-MM-DD)")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--regions", nargs="+", help="all, or codes such as global co au")
    parser.add_argument("--list-regions", action="store_true", help="List locations without opening Chrome")
    parser.add_argument("--status", action="store_true", help="Read-only CSV coverage audit; no Chrome or state writes")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.list_regions:
        for code, name in REGIONS.items():
            console.print(f"{code:6} {name:20} regional-{code}-weekly")
        return 0
    try:
        config = get_config(args.config)
        if args.start_date:
            config.start_date = args.start_date
        if args.stop_date:
            config.stop_date = args.stop_date
        if args.headless:
            config.headless = True
        if args.regions:
            config.chart_types = resolve_chart_types(args.regions)
        plans = run_batch_downloader(config, status_only=args.status)
        return 0 if args.status or all(not plan.outstanding for plan in plans) else 1
    except KeyboardInterrupt:
        console.print("\nPaused. Completed files and in-flight/failed weeks are saved per region. Rerun to resume.")
        return 130
    except Exception as err:
        console.print(f"\nStopped: {err}", markup=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
