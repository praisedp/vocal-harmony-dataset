"""Resumable (Location x Genre x Era) matrix harvester for YouTube Music (Option B).

Iterates across Locations (US, GB, AU, CA), Genres (14 citation-backed categories),
and Eras (ALL, 1960s..2020s), querying `yt.search()` with:
  1. filter="featured_playlists" -> logs hits to `search_hits.csv`, fetches each
     official `RDCLAK5uy_...` playlist at most once into `playlists.csv`,
     `playlist_tracks.csv`, `tracks.csv`, and saves related playlists to
     `related_candidates.csv` without diving into them.
  2. filter="albums" -> logs hits to `search_hits.csv`, fetches each `MPREb_...`
     album at most once into `albums.csv`, `album_tracks.csv`, `tracks.csv`, and
     saves related/other-version albums to `related_candidates.csv`.
  3. filter="songs" -> logs hits to `search_hits.csv` and appends new `video_id`s
     to `tracks.csv`.

Safe to interrupt (Ctrl+C) and resume anytime.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from ytmusicapi import YTMusic

_PIPELINE_DIR = str(Path(__file__).resolve().parent)
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

from ytmusic_catalog_store import (
    DEFAULT_DATA_DIR,
    DEFAULT_STATE_DIR,
    CatalogStore,
    TimeoutSession,
    call_with_retry,
    extract_artists,
    is_official_featured_playlist,
    is_valid_album_id,
    normalize_album_id,
    normalize_playlist_id,
)

console = Console()

DEFAULT_LOCATIONS: Tuple[str, ...] = ("US", "GB", "AU", "CA")

DEFAULT_GENRES: Tuple[str, ...] = (
    "Pop",
    "Rock",
    "Hip-Hop",
    "Dance & Electronic",
    "Latin",
    "R&B & Soul",
    "Country",
    "Reggae",
    "Jazz",
    "Folk & Acoustic",
    "Metal",
    "Christian & Gospel",
    "Blues",
    "Indie & Alternative",
)

DEFAULT_ERAS: Tuple[str, ...] = (
    "ALL",
    "1960s",
    "1970s",
    "1980s",
    "1990s",
    "2000s",
    "2010s",
    "2020s",
)

DEFAULT_FILTERS: Tuple[str, ...] = (
    "featured_playlists",
    "albums",
    "songs",
)


def build_query(genre: str, era: str) -> str:
    """Construct the search query string for a (genre, era) pair."""
    clean_genre = genre.strip()
    clean_era = era.strip()
    if not clean_era or clean_era.upper() == "ALL":
        return clean_genre
    return f"{clean_era} {clean_genre}"


def make_step_key(location: str, genre: str, era: str, search_filter: str) -> str:
    """Return the unique ledger key for one (location, genre, era, search_filter) query step."""
    return f"{location.strip()}|{genre.strip()}|{era.strip()}|{search_filter.strip()}"


def build_step_plan(
    locations: Sequence[str],
    genres: Sequence[str],
    eras: Sequence[str],
    search_filters: Sequence[str],
) -> List[Tuple[str, str, str, str]]:
    """Order steps with era outermost so `ALL` completes across all locations/genres first."""
    steps: List[Tuple[str, str, str, str]] = []
    for era in eras:
        for location in locations:
            for genre in genres:
                for s_filter in search_filters:
                    steps.append((location, genre, era, s_filter))
    return steps


def execute_search_step(
    yt: Any,
    store: CatalogStore,
    *,
    location: str,
    genre: str,
    era: str,
    search_filter: str,
    limit: int,
) -> Dict[str, int]:
    """Run one search query step, log hits, fetch new containers, and mark the step done."""
    step_key = make_step_key(location, genre, era, search_filter)
    query = build_query(genre, era)

    stats = {
        "search_hits": 0,
        "playlists_fetched": 0,
        "playlists_skipped": 0,
        "albums_fetched": 0,
        "albums_skipped": 0,
        "new_tracks": 0,
    }

    store.pace()
    try:
        results = call_with_retry(
            yt.search,
            query,
            filter=search_filter,
            limit=limit,
            max_retries=store.max_retries,
        )
    except Exception as err:  # noqa: BLE001
        store.record_failure(f"search_{search_filter}", step_key, err)
        return stats

    if not isinstance(results, list):
        results = []

    candidates = results[:limit]
    for rank, item in enumerate(candidates, 1):
        if not isinstance(item, dict):
            continue

        if search_filter == "featured_playlists":
            raw_pid = str(item.get("browseId") or item.get("playlistId") or "").strip()
            norm_pid = normalize_playlist_id(raw_pid)
            is_official = is_official_featured_playlist(norm_pid)
            author = str(item.get("author") or "").strip()
            store.record_search_hit(
                location=location,
                genre=genre,
                era=era,
                search_filter=search_filter,
                query=query,
                search_rank=rank,
                item_type="playlist",
                item_id=norm_pid,
                item_title=str(item.get("title") or "").strip(),
                item_artists=author,
                item_year=str(item.get("year") or "").strip(),
                is_official_editorial=is_official,
                raw_obj=item,
            )
            stats["search_hits"] += 1

            if is_official and norm_pid:
                status, _, added_tracks = store.process_playlist_if_new(
                    yt,
                    norm_pid,
                    source_label=f"search:{step_key}",
                )
                if status == "fetched":
                    stats["playlists_fetched"] += 1
                    stats["new_tracks"] += added_tracks
                elif status == "skipped_existing":
                    stats["playlists_skipped"] += 1

        elif search_filter == "albums":
            raw_aid = str(item.get("browseId") or "").strip()
            norm_aid = normalize_album_id(raw_aid)
            artist_names, _, _ = extract_artists(item)
            store.record_search_hit(
                location=location,
                genre=genre,
                era=era,
                search_filter=search_filter,
                query=query,
                search_rank=rank,
                item_type="album",
                item_id=norm_aid,
                item_title=str(item.get("title") or "").strip(),
                item_artists=artist_names,
                item_year=str(item.get("year") or "").strip(),
                is_official_editorial=False,
                raw_obj=item,
            )
            stats["search_hits"] += 1

            if is_valid_album_id(norm_aid):
                status, _, added_tracks = store.process_album_if_new(
                    yt,
                    norm_aid,
                    source_label=f"search:{step_key}",
                )
                if status == "fetched":
                    stats["albums_fetched"] += 1
                    stats["new_tracks"] += added_tracks
                elif status == "skipped_existing":
                    stats["albums_skipped"] += 1

        elif search_filter == "songs":
            vid = str(item.get("videoId") or "").strip()
            artist_names, _, _ = extract_artists(item)
            store.record_search_hit(
                location=location,
                genre=genre,
                era=era,
                search_filter=search_filter,
                query=query,
                search_rank=rank,
                item_type="song",
                item_id=vid,
                item_title=str(item.get("title") or "").strip(),
                item_artists=artist_names,
                item_year=str(item.get("year") or "").strip(),
                is_official_editorial=False,
                raw_obj=item,
            )
            stats["search_hits"] += 1

            if vid and store.record_track_if_new(
                item,
                source_type="search_songs",
                source_id=step_key,
            ):
                stats["new_tracks"] += 1

    store.mark_query_completed(step_key)
    return stats


def run_collection(
    store: CatalogStore,
    *,
    locations: Sequence[str] = DEFAULT_LOCATIONS,
    genres: Sequence[str] = DEFAULT_GENRES,
    eras: Sequence[str] = DEFAULT_ERAS,
    search_filters: Sequence[str] = DEFAULT_FILTERS,
    limit: int = 20,
    max_steps: int = 0,
    yt_factory: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Run the matrix search loop with resume support and return summary metrics."""
    if yt_factory is None:
        def default_factory(loc: str) -> Any:
            return YTMusic(location=loc, requests_session=TimeoutSession())
        yt_factory = default_factory

    all_steps = build_step_plan(locations, genres, eras, search_filters)
    pending_steps = [
        step
        for step in all_steps
        if make_step_key(*step) not in store.completed_queries
    ]

    if max_steps > 0:
        target_steps = pending_steps[:max_steps]
    else:
        target_steps = pending_steps

    totals = {
        "total_grid_steps": len(all_steps),
        "already_completed_steps": len(all_steps) - len(pending_steps),
        "steps_executed": 0,
        "search_hits": 0,
        "playlists_fetched": 0,
        "playlists_skipped": 0,
        "albums_fetched": 0,
        "albums_skipped": 0,
        "new_tracks": 0,
        "interrupted": False,
    }

    if not target_steps:
        console.print("[green]All requested (Location x Genre x Era x Filter) steps are already completed![/green]")
        return totals

    clients: Dict[str, Any] = {}
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Harvesting YTM Search Matrix", total=len(target_steps))
        try:
            for location, genre, era, search_filter in target_steps:
                if location not in clients:
                    clients[location] = yt_factory(location)
                yt = clients[location]

                progress.update(
                    task,
                    description=(
                        f"[cyan]{location}[/cyan] | [bold]{genre}[/bold] | "
                        f"[yellow]{era}[/yellow] | {search_filter}"
                    ),
                )
                step_stats = execute_search_step(
                    yt,
                    store,
                    location=location,
                    genre=genre,
                    era=era,
                    search_filter=search_filter,
                    limit=limit,
                )
                totals["steps_executed"] += 1
                for k in (
                    "search_hits",
                    "playlists_fetched",
                    "playlists_skipped",
                    "albums_fetched",
                    "albums_skipped",
                    "new_tracks",
                ):
                    totals[k] += step_stats[k]
                progress.advance(task)
        except KeyboardInterrupt:
            totals["interrupted"] = True
            console.print("\n[yellow]Interrupted by user (Ctrl+C). All completed items are saved and flushed.[/yellow]")

    totals["total_unique_playlists"] = len(store.seen_playlists)
    totals["total_unique_albums"] = len(store.seen_albums)
    totals["total_unique_tracks"] = len(store.seen_tracks)
    return totals


def print_summary(totals: Dict[str, Any], data_dir: Path) -> None:
    table = Table(title="YouTube Music Search Matrix Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right", style="bold")

    table.add_row("Total grid steps", f"{totals.get('total_grid_steps', 0):,}")
    table.add_row("Previously completed steps", f"{totals.get('already_completed_steps', 0):,}")
    table.add_row("Steps executed this run", f"{totals.get('steps_executed', 0):,}")
    table.add_row("Search hits logged this run", f"{totals.get('search_hits', 0):,}")
    table.add_row("New playlists fetched (skipped existing)", f"{totals.get('playlists_fetched', 0):,} ({totals.get('playlists_skipped', 0):,})")
    table.add_row("New albums fetched (skipped existing)", f"{totals.get('albums_fetched', 0):,} ({totals.get('albums_skipped', 0):,})")
    table.add_row("New unique tracks added this run", f"{totals.get('new_tracks', 0):,}")
    table.add_row("Total unique playlists in catalog", f"{totals.get('total_unique_playlists', 0):,}")
    table.add_row("Total unique albums in catalog", f"{totals.get('total_unique_albums', 0):,}")
    table.add_row("Total unique tracks (video_id) in catalog", f"{totals.get('total_unique_tracks', 0):,}")
    table.add_row("Output directory", str(data_dir))
    console.print(table)


def parse_csv_list(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument(
        "--locations",
        type=parse_csv_list,
        default=list(DEFAULT_LOCATIONS),
        help="Comma-separated YTM location codes (default: US,GB,AU,CA)",
    )
    parser.add_argument(
        "--genres",
        type=parse_csv_list,
        default=list(DEFAULT_GENRES),
        help="Comma-separated genres list",
    )
    parser.add_argument(
        "--eras",
        type=parse_csv_list,
        default=list(DEFAULT_ERAS),
        help="Comma-separated eras list (default: ALL,1960s..2020s)",
    )
    parser.add_argument(
        "--filters",
        type=parse_csv_list,
        default=list(DEFAULT_FILTERS),
        help="Comma-separated search filters (default: featured_playlists,albums,songs)",
    )
    parser.add_argument("--limit", type=int, default=20, help="Max search results per query (default: 20)")
    parser.add_argument("--delay", type=float, default=0.4, help="Seconds between API calls (default: 0.4)")
    parser.add_argument("--max-retries", type=int, default=2, help="Retries per failed request (default: 2)")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=0,
        help="Max query steps to execute before exiting (0 = run all pending steps)",
    )
    args = parser.parse_args()

    with CatalogStore(
        data_dir=args.data_dir,
        state_dir=args.state_dir,
        delay=args.delay,
        max_retries=args.max_retries,
    ) as store:
        totals = run_collection(
            store,
            locations=args.locations,
            genres=args.genres,
            eras=args.eras,
            search_filters=args.filters,
            limit=args.limit,
            max_steps=args.max_steps,
        )
        print_summary(totals, args.data_dir)

    return 130 if totals.get("interrupted") else 0


if __name__ == "__main__":
    raise SystemExit(main())
