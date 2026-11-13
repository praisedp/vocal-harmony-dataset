"""Master Dataset Builder & Deduplicator for Spotify Charts.

Scans the downloads/ folder for all weekly chart CSV files, extracts unique tracks
(deduplicating by Spotify URI and artist + track name), and updates a consolidated
master.csv inside a dedicated data/ directory.

Designed to be run repeatedly:
- Reads existing data/master.csv to preserve already collected tracks
- Only appends genuinely new tracks on each run
- Strictly ignores non-CSV document files and temporary artifacts in downloads/
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Set, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = PROJECT_DIR / "downloads"
DEFAULT_OUTPUT_FILE = PROJECT_DIR / "data/master.csv"

CSV_FIELDNAMES = [
    "rank",
    "uri",
    "artist_names",
    "track_name",
    "source",
    "peak_rank",
    "previous_rank",
    "weeks_on_chart",
    "streams",
]


def name_key(artist: str, track: str) -> Tuple[str, str]:
    """Ignore case and repeated whitespace, preserving accents and punctuation."""
    return tuple(" ".join(value.split()).casefold() for value in (artist, track))


def read_chart_rows(path: Path, *, master: bool = False) -> List[Dict[str, str]]:
    """Validate a whole file before accepting any of its rows."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or set(fields) != set(CSV_FIELDNAMES):
            raise ValueError(f"{path.name}: expected columns {', '.join(CSV_FIELDNAMES)}")
        if master and fields != CSV_FIELDNAMES:
            raise ValueError(f"{path.name}: master columns are in an unexpected order")
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path.name}: malformed row near line {reader.line_num}")
            rows.append(row)
        return rows


def append_atomically(output_path: Path, rows: List[Dict[str, str]]) -> None:
    """Publish the full update at once; leave the old master intact on failure."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=output_path.parent, delete=False) as handle:
            temporary_path = Path(handle.name)
            if output_path.exists() and output_path.stat().st_size:
                with output_path.open("rb") as existing:
                    shutil.copyfileobj(existing, handle)
                # A valid CSV need not have a final newline.
                with output_path.open("rb") as existing:
                    existing.seek(-1, os.SEEK_END)
                    if existing.read(1) not in (b"\n", b"\r"):
                        handle.write(b"\n")
        with temporary_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
            if not output_path.exists() or not output_path.stat().st_size:
                writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_existing_master(master_path: Path) -> Tuple[Set[str], Set[Tuple[str, str]], int, int]:
    """Load existing unique track URIs, artist+title keys, and highest rank from master.csv.

    Returns:
        Tuple of (existing_uris, existing_names, current_max_rank, row_count)
    """
    existing_uris: Set[str] = set()
    existing_names: Set[Tuple[str, str]] = set()
    max_rank = 0

    if not master_path.exists() or not master_path.stat().st_size:
        return existing_uris, existing_names, 0, 0

    rows = read_chart_rows(master_path, master=True)
    for row in rows:
        uri = row.get("uri", "").strip()
        artist = row.get("artist_names", "").strip().lower()
        track = row.get("track_name", "").strip().lower()

        if uri:
            existing_uris.add(uri)
        if artist and track:
            existing_names.add(name_key(artist, track))

        raw_rank = row.get("rank", "").strip()
        if raw_rank.isdigit():
            max_rank = max(max_rank, int(raw_rank))
        else:
            max_rank += 1

    return existing_uris, existing_names, max_rank, len(rows)


def build_master_dataset(
    downloads_dir: Path = DEFAULT_INPUT_DIR,
    output_path: Path = DEFAULT_OUTPUT_FILE,
    preview_count: int = 5,
) -> List[Dict[str, str]]:
    """Scan downloads/ for CSVs, deduplicate tracks, and append new ones to master.csv."""
    downloads_dir = Path(downloads_dir).resolve()
    output_path = Path(output_path).resolve()

    # 1. Discover all valid .csv files (ignoring non-csv documents, hidden files, temp files)
    if not downloads_dir.exists():
        raise FileNotFoundError(f"Downloads directory '{downloads_dir}' does not exist.")

    csv_files = sorted(
        [
            f
            for f in downloads_dir.iterdir()
            if f.is_file()
            and f.resolve() != output_path
            and f.name.lower().endswith(".csv")
            and not f.name.startswith(".")
        ]
    )

    # 2. Read existing master.csv (if present) to prevent re-adding tracks
    existing_uris, existing_names, current_max_rank, initial_master_count = (
        load_existing_master(output_path)
    )

    console.print("=" * 68)
    console.print("[bold green]Spotify Charts Dataset Aggregator & Deduplicator[/bold green]")
    console.print(f"[*] Input Directory:   {downloads_dir}")
    console.print(f"[*] Output Master CSV: {output_path}")
    console.print(f"[*] Valid CSVs Found:  [cyan]{len(csv_files)}[/cyan]")
    console.print(f"[*] Existing in Master:[green]{initial_master_count}[/green] tracks")
    console.print("=" * 68)

    if not csv_files:
        console.print("[yellow]No CSV files found in downloads/ to process.[/yellow]")
        return []

    # 3. Process CSV files and extract only new unique tracks
    new_tracks: List[Dict[str, str]] = []
    total_rows_scanned = 0
    duplicate_rows_skipped = 0
    corrupt_files = 0

    next_rank = current_max_rank + 1

    for file_path in csv_files:
        try:
            rows = read_chart_rows(file_path)
            for row in rows:
                total_rows_scanned += 1
                uri = (row.get("uri") or "").strip()
                artist = (row.get("artist_names") or "").strip()
                track = (row.get("track_name") or "").strip()

                # Skip empty rows
                if not uri and not (artist and track):
                    continue

                # Check for duplication: primary key is URI, fallback is (artist, track)
                is_duplicate = False
                if uri and uri in existing_uris:
                    is_duplicate = True
                elif artist and track and name_key(artist, track) in existing_names:
                    is_duplicate = True

                if is_duplicate:
                    duplicate_rows_skipped += 1
                    continue

                # New unique track!
                if uri:
                    existing_uris.add(uri)
                if artist and track:
                    existing_names.add(name_key(artist, track))

                clean_row = {
                    "rank": str(next_rank),
                    "uri": uri,
                    "artist_names": artist,
                    "track_name": track,
                    "source": (row.get("source") or "").strip(),
                    "peak_rank": (row.get("peak_rank") or "").strip(),
                    "previous_rank": (row.get("previous_rank") or "").strip(),
                    "weeks_on_chart": (row.get("weeks_on_chart") or "").strip(),
                    "streams": (row.get("streams") or "").strip(),
                }
                new_tracks.append(clean_row)
                next_rank += 1

        except (OSError, UnicodeError, csv.Error, ValueError) as err:
            corrupt_files += 1
            console.print(f"[dim red]Warning: Could not read {file_path.name}: {err}[/dim red]")

    # 4. Write / Append new tracks to master.csv
    if new_tracks:
        append_atomically(output_path, new_tracks)

    final_total_tracks = initial_master_count + len(new_tracks)

    # 5. Display Clean Summary Table
    summary_table = Table(title="Deduplication & Master Build Results", border_style="cyan")
    summary_table.add_column("Metric", style="bold")
    summary_table.add_column("Count", justify="right")

    summary_table.add_row("Weekly CSV Files Scanned", str(len(csv_files)))
    if corrupt_files:
        summary_table.add_row("Unreadable Files Skipped", f"[red]{corrupt_files}[/red]")
    summary_table.add_row("Total Chart Entries Examined", f"{total_rows_scanned:,}")
    summary_table.add_row("Duplicate Occurrences Filtered", f"[yellow]{duplicate_rows_skipped:,}[/yellow]")
    summary_table.add_row("Tracks Already in Master", f"{initial_master_count:,}")
    summary_table.add_row("Newly Discovered Unique Tracks", f"[bold green]{len(new_tracks):,}[/bold green]")
    summary_table.add_row("Total Unique Tracks in master.csv", f"[bold cyan]{final_total_tracks:,}[/bold cyan]")

    console.print()
    console.print(summary_table)

    # 6. Preview newly added rows if any
    if new_tracks and preview_count > 0:
        preview_table = Table(
            title=f"Sample of Newly Added Tracks (showing up to {preview_count})",
            border_style="green",
        )
        preview_table.add_column("Rank", justify="right", style="cyan")
        preview_table.add_column("Artist(s)", style="bold")
        preview_table.add_column("Track Title")
        preview_table.add_column("Spotify URI", style="dim")

        for sample in new_tracks[:preview_count]:
            preview_table.add_row(
                sample["rank"],
                sample["artist_names"],
                sample["track_name"],
                sample["uri"],
            )
        console.print()
        console.print(preview_table)

    if not new_tracks and corrupt_files:
        console.print("\n[yellow]No new tracks added. Review the unreadable-file warnings above.[/yellow]")
    elif not new_tracks:
        console.print(
            "\n[bold green]✓ master.csv is already up to date! No new songs found in downloads/.[/bold green]"
        )
    else:
        console.print(
            f"\n[bold green]✓ Successfully added {len(new_tracks):,} new unique tracks to: {output_path}[/bold green]"
        )

    return new_tracks


def parse_args():
    parser = argparse.ArgumentParser(
        description="Aggregate Spotify Charts CSVs into a deduplicated master dataset."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing weekly CSV files (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
        help=f"Output path for master CSV (default: {DEFAULT_OUTPUT_FILE})",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=5,
        help="Number of newly added tracks to preview in terminal (default: 5)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    build_master_dataset(
        downloads_dir=args.input_dir,
        output_path=args.output,
        preview_count=args.preview,
    )
