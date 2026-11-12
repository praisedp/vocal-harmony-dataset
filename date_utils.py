"""Date calculation utilities for Spotify Charts historical weekly ranges.

Provides functions to generate weekly date sequences backwards from a given
starting date down to a specified target date (e.g. December 2016).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Union

DEFAULT_STOP_DATE = "2016-12-01"


def parse_date(d: Union[str, date]) -> date:
    """Parse a date string (YYYY-MM-DD) or date object into a datetime.date."""
    if isinstance(d, date):
        return d
    return datetime.strptime(d.strip(), "%Y-%m-%d").date()


def generate_weekly_dates_backwards(
    start_date: Union[str, date],
    stop_date: Union[str, date] = DEFAULT_STOP_DATE,
    include_start: bool = True,
) -> list[str]:
    """Generate weekly dates stepping 7 days backwards from start_date down to stop_date.

    Parameters:
        start_date: Starting date (e.g. '2026-10-29' or date object).
        stop_date: Earliest date limit (e.g. '2016-12-01').
                   The sequence stops once dates go earlier than this date.
        include_start: Whether to include start_date as the first element.

    Returns:
        List of date strings formatted as 'YYYY-MM-DD' in reverse chronological order.

    Example:
        >>> generate_weekly_dates_backwards("2026-10-29", stop_date="2026-10-08")
        ['2026-10-29', '2026-10-22', '2026-10-15', '2026-10-08']
    """
    curr = parse_date(start_date)
    earliest = parse_date(stop_date)

    if curr < earliest:
        raise ValueError(
            f"start_date ({curr}) must be on or after stop_date ({earliest})"
        )

    dates: list[str] = []

    if include_start:
        dates.append(curr.isoformat())

    one_week = timedelta(days=7)
    curr -= one_week

    while curr >= earliest:
        dates.append(curr.isoformat())
        curr -= one_week

    return dates


def load_completed_dates(file_path: Union[str, Path]) -> list[str]:
    """Read completed dates from file, preserving file order."""
    path = Path(file_path)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def record_completed_date(file_path: Union[str, Path], date_str: str) -> None:
    """Append a completed date to the record file and flush immediately."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{date_str}\n")
        f.flush()


def get_remaining_dates(
    start_date: Union[str, date],
    stop_date: Union[str, date] = DEFAULT_STOP_DATE,
    completed_file: Union[str, Path] = "completed.txt",
) -> tuple[list[str], list[str], str | None]:
    """Calculate remaining weekly dates to process, taking completed.txt into account.

    Returns:
        tuple of (remaining_dates_to_process, already_completed_dates, oldest_completed_date)
    """
    completed = load_completed_dates(completed_file)
    completed_set = set(completed)

    if not completed:
        remaining = generate_weekly_dates_backwards(
            start_date=start_date, stop_date=stop_date
        )
        return remaining, [], None

    # Find the oldest (earliest in time) completed date
    oldest_completed = min(completed)
    oldest_dt = parse_date(oldest_completed)
    earliest_stop = parse_date(stop_date)

    # Next matching weekly date before the oldest completed
    resume_start = oldest_dt - timedelta(days=7)

    if resume_start < earliest_stop:
        return [], completed, oldest_completed

    candidates = generate_weekly_dates_backwards(
        start_date=resume_start, stop_date=stop_date
    )
    remaining = [d for d in candidates if d not in completed_set]
    return remaining, completed, oldest_completed



def main():
    parser = argparse.ArgumentParser(
        description="Generate weekly dates stepping backwards from a start date."
    )
    parser.add_argument(
        "start_date",
        nargs="?",
        default="2026-10-29",
        help="Starting date in YYYY-MM-DD format (default: 2026-10-29)",
    )
    parser.add_argument(
        "--stop-date",
        default=DEFAULT_STOP_DATE,
        help=f"Earliest stop date (default: {DEFAULT_STOP_DATE})",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=10,
        help="Number of dates to preview in console (default: 10, use 0 for all)",
    )
    parser.add_argument(
        "--output-file",
        type=str,
        default=None,
        help="Optional file path to save the full list (one date per line)",
    )
    args = parser.parse_args()

    date_list = generate_weekly_dates_backwards(
        start_date=args.start_date,
        stop_date=args.stop_date,
    )

    print("=" * 60)
    print(" Weekly Date Range Generator")
    print(f"[*] Start Date:    {args.start_date}")
    print(f"[*] Stop Date:     {args.stop_date}")
    print(f"[*] Total Weeks:   {len(date_list)}")
    print("=" * 60)

    if args.preview > 0 and len(date_list) > args.preview:
        print(f"\nFirst {args.preview} dates:")
        for d in date_list[:args.preview]:
            print(f"  • {d}")
        print("  ...")
        print("Last 5 dates:")
        for d in date_list[-5:]:
            print(f"  • {d}")
    else:
        print("\nAll dates:")
        for d in date_list:
            print(f"  • {d}")

    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as f:
            for d in date_list:
                f.write(f"{d}\n")
        print(f"\n[✓] Saved {len(date_list)} dates to: {args.output_file}")


if __name__ == "__main__":
    main()
