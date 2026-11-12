"""Main Spotify Charts Batch CSV Downloader.

Features:
- Configurable via config.json (start_date, stop_date, wait intervals, directories)
- Resumes automatically from completed.txt (finds the oldest completed date and continues backwards)
- Real-time terminal progress using the Rich library (week in progress, completed, remaining)
- Immediate progression to next date upon download completion, 3s pause on failure
- Graceful cancellation handling (Ctrl+C preserves state for resumption)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from browser import get_browser_session
from chart_page import open_and_download_chart
from config import ScraperConfig, get_config
from date_utils import (
    generate_weekly_dates_backwards,
    get_remaining_dates,
    record_completed_date,
)

console = Console()


def display_welcome_banner(
    config: ScraperConfig,
    total_dates: int,
    completed_dates_count: int,
    remaining_dates_count: int,
    oldest_completed: str | None,
    next_resume_date: str | None,
) -> None:
    """Print a clean Rich panel summarizing the configuration and resume status."""
    info_table = Table.grid(padding=(0, 2))
    info_table.add_column(style="bold cyan")
    info_table.add_column()

    info_table.add_row("Start Date:", config.start_date)
    info_table.add_row("Earliest Date (Stop):", config.stop_date)
    info_table.add_row("Chart Type:", config.chart_type)
    info_table.add_row("Page Load Wait:", f"{config.page_load_wait_seconds}s")
    info_table.add_row("Failure Pause:", f"{config.failure_retry_wait_seconds}s")
    info_table.add_row("Downloads Folder:", str(config.downloads_dir))
    info_table.add_row("Completed Records:", str(config.completed_file))
    info_table.add_row("Total Weeks in Scope:", str(total_dates))
    info_table.add_row("Already Completed:", f"[green]{completed_dates_count}[/green]")
    info_table.add_row("Remaining to Download:", f"[yellow]{remaining_dates_count}[/yellow]")

    if oldest_completed:
        info_table.add_row("Oldest Completed in File:", f"[magenta]{oldest_completed}[/magenta]")
        info_table.add_row("Resuming From:", f"[bold green]{next_resume_date}[/bold green]")
    else:
        info_table.add_row("Starting Fresh From:", f"[bold green]{config.start_date}[/bold green]")

    panel = Panel(
        info_table,
        title="[bold green]Spotify Charts Historical CSV Downloader[/bold green]",
        subtitle="[dim]Press Ctrl+C anytime to pause. Progress is saved automatically.[/dim]",
        border_style="bright_blue",
    )
    console.print(panel)


def run_batch_downloader(config: ScraperConfig) -> None:
    config.downloads_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate full scope of dates from start down to stop
    all_dates = generate_weekly_dates_backwards(
        start_date=config.start_date,
        stop_date=config.stop_date,
    )
    total_dates = len(all_dates)

    # 2. Check completed.txt to determine resume point
    remaining_dates, completed_dates, oldest_completed = get_remaining_dates(
        start_date=config.start_date,
        stop_date=config.stop_date,
        completed_file=config.completed_file,
    )

    completed_count = len(completed_dates)
    remaining_count = len(remaining_dates)
    next_resume_date = remaining_dates[0] if remaining_dates else None

    # 3. Display summary banner
    display_welcome_banner(
        config=config,
        total_dates=total_dates,
        completed_dates_count=completed_count,
        remaining_dates_count=remaining_count,
        oldest_completed=oldest_completed,
        next_resume_date=next_resume_date,
    )

    if not remaining_dates:
        console.print(
            "\n[bold green]All dates in the configured range have already been downloaded![/bold green]"
        )
        return

    # 4. Initialize Rich Progress
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=30),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TextColumn("[yellow]({task.fields[remaining]} remaining)[/yellow]"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )

    task_id = progress.add_task(
        description=f"Week: {remaining_dates[0]}",
        total=total_dates,
        completed=completed_count,
        remaining=remaining_count,
    )

    successful_runs = 0
    failed_runs = 0

    try:
        with progress:
            with get_browser_session(
                profile_dir=config.profile_dir,
                downloads_dir=config.downloads_dir,
                headless=config.headless,
            ) as (context, page):

                for idx, date_str in enumerate(remaining_dates, start=1):
                    # Update progress description to current active week
                    progress.update(
                        task_id,
                        description=f"Week: [bold white]{date_str}[/bold white]",
                        remaining=len(remaining_dates) - (idx - 1),
                    )

                    try:
                        # 1) Open chart page
                        # 2) Wait 1s (config.page_load_wait_seconds)
                        # 3) Trigger download and wait for finish
                        status, saved_file = open_and_download_chart(
                            page=page,
                            date_str=date_str,
                            chart_type=config.chart_type,
                            output_dir=config.downloads_dir,
                            wait_seconds=config.page_load_wait_seconds,
                        )

                        # Record completed date immediately
                        record_completed_date(config.completed_file, date_str)
                        successful_runs += 1

                        file_size = saved_file.stat().st_size if saved_file.exists() else 0
                        progress.console.print(
                            f"[bold green]✓[/bold green] [{date_str}] Downloaded: [green]{saved_file.name}[/green] ({file_size:,} bytes)"
                        )

                        # Update progress bar
                        progress.update(
                            task_id,
                            advance=1,
                            remaining=len(remaining_dates) - idx,
                        )

                    except Exception as err:
                        failed_runs += 1
                        progress.console.print(
                            f"[bold red]✗[/bold red] [{date_str}] Failed: {err}. "
                            f"Waiting {config.failure_retry_wait_seconds}s before next..."
                        )
                        time.sleep(config.failure_retry_wait_seconds)

    except KeyboardInterrupt:
        console.print(
            "\n[bold yellow]Paused by user (Ctrl+C).[/bold yellow] "
            "All downloaded dates are saved in completed.txt. Rerun anytime to resume exactly where you left off!"
        )
    except Exception as err:
        console.print(f"\n[bold red]Unexpected error during batch run: {err}[/bold red]")

    # 5. Final summary
    summary_table = Table(title="Batch Run Summary", border_style="cyan")
    summary_table.add_column("Category", style="bold")
    summary_table.add_column("Count", justify="right")

    summary_table.add_row("Total Weeks in Scope", str(total_dates))
    summary_table.add_row("Downloaded in this session", f"[green]{successful_runs}[/green]")
    summary_table.add_row("Failed in this session", f"[red]{failed_runs}[/red]" if failed_runs else "0")
    summary_table.add_row("Total Recorded Completed", str(completed_count + successful_runs))
    summary_table.add_row("Remaining Outstanding", str(max(0, total_dates - (completed_count + successful_runs))))

    console.print()
    console.print(summary_table)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Spotify Charts Batch Downloader with auto-resume and Rich progress."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Path to custom config.json file (default: config.json)",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="Override start date (e.g. 2026-11-05)",
    )
    parser.add_argument(
        "--stop-date",
        default=None,
        help="Override earliest stop date (e.g. 2016-12-01)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    config = get_config(args.config)

    # Command-line overrides if provided
    if args.start_date:
        config.start_date = args.start_date
    if args.stop_date:
        config.stop_date = args.stop_date
    if args.headless:
        config.headless = True

    run_batch_downloader(config)


if __name__ == "__main__":
    main()
