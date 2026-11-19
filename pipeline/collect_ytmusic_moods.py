"""Standalone `get_mood_categories()` browse harvester for YouTube Music.

Browses YouTube Music's `get_mood_categories()` (`Genres`, `Moods & moments`,
and the `Decades` shelves `1960s`–`2010s`) using raw `browse` requests (bypassing
`ytmusicapi` 1.12.3's broken `get_mood_playlists()`).

Shares `data/ytmusic/` and `state/ytmusic/` with `collect_ytmusic_search.py`:
- Logs every shelf item (`[0] Songs`, `[1] Featured playlists`, `[4] Albums`, and
  Decade playlist shelves) to `data/ytmusic/mood_category_hits.csv`.
- Checks `playlists.csv`, `albums.csv`, and `tracks.csv` (via `CatalogStore`) so any
  playlist (`RDCLAK5uy_...`), album (`MPREb_...`), or track (`video_id`) already
  fetched by `collect_ytmusic_search.py` is skipped without making duplicate API calls.
- Saves `related` playlists and `related_recommendations` albums to
  `related_candidates.csv` without diving into them.
"""

from __future__ import annotations

import argparse
import re
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
    is_official_featured_playlist,
    is_valid_album_id,
    normalize_album_id,
    normalize_playlist_id,
)

console = Console()

DECADE_RE = re.compile(r"^\d{4}s$", re.IGNORECASE)
SKIPPED_SHELF_TITLES = {"community playlists", "music videos", "videos"}


def _join_runs_text(runs: Any) -> str:
    if not isinstance(runs, list):
        return ""
    return "".join(str(r.get("text") or "") for r in runs if isinstance(r, dict)).strip()


def parse_responsive_song_item(renderer: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a `musicResponsiveListItemRenderer` song from a Genre Page `[0] Songs` shelf."""
    video_id = (
        str(renderer.get("playlistItemData", {}).get("videoId") or "").strip()
        or str(
            renderer.get("overlay", {})
            .get("musicItemThumbnailOverlayRenderer", {})
            .get("content", {})
            .get("musicPlayButtonRenderer", {})
            .get("playNavigationEndpoint", {})
            .get("watchEndpoint", {})
            .get("videoId")
            or ""
        ).strip()
    )

    flex_cols = renderer.get("flexColumns") if isinstance(renderer.get("flexColumns"), list) else []
    title = ""
    artists: List[Dict[str, Optional[str]]] = []
    album_obj: Optional[Dict[str, Optional[str]]] = None
    views = ""

    if len(flex_cols) >= 1 and isinstance(flex_cols[0], dict):
        runs0 = (
            flex_cols[0]
            .get("musicResponsiveListItemFlexColumnRenderer", {})
            .get("text", {})
            .get("runs", [])
        )
        title = _join_runs_text(runs0)

    if len(flex_cols) >= 2 and isinstance(flex_cols[1], dict):
        runs1 = (
            flex_cols[1]
            .get("musicResponsiveListItemFlexColumnRenderer", {})
            .get("text", {})
            .get("runs", [])
        )
        if isinstance(runs1, list):
            for run in runs1:
                if not isinstance(run, dict):
                    continue
                txt = str(run.get("text") or "").strip()
                if not txt or txt in ("•", ",", "&"):
                    continue
                nav_b = (
                    run.get("navigationEndpoint", {})
                    .get("browseEndpoint", {})
                )
                bid = str(nav_b.get("browseId") or "").strip() if isinstance(nav_b, dict) else ""
                if "views" in txt.casefold():
                    views = txt
                elif bid.startswith("MPRE"):
                    album_obj = {"name": txt, "id": bid}
                else:
                    artists.append({"name": txt, "id": bid or None})

    if len(flex_cols) >= 3 and isinstance(flex_cols[2], dict) and album_obj is None:
        runs2 = (
            flex_cols[2]
            .get("musicResponsiveListItemFlexColumnRenderer", {})
            .get("text", {})
            .get("runs", [])
        )
        if isinstance(runs2, list) and runs2 and isinstance(runs2[0], dict):
            alb_name = _join_runs_text(runs2)
            alb_id = str(
                runs2[0]
                .get("navigationEndpoint", {})
                .get("browseEndpoint", {})
                .get("browseId")
                or ""
            ).strip()
            if alb_name or alb_id:
                album_obj = {"name": alb_name, "id": alb_id or None}

    badges = renderer.get("badges") if isinstance(renderer.get("badges"), list) else []
    is_explicit = any(
        isinstance(b, dict)
        and "musicInlineBadgeRenderer" in b
        for b in badges
    )
    thumbs = (
        renderer.get("thumbnail", {})
        .get("musicThumbnailRenderer", {})
        .get("thumbnail", {})
        .get("thumbnails")
    )

    return {
        "videoId": video_id,
        "title": title,
        "artists": artists,
        "album": album_obj,
        "views": views,
        "isExplicit": is_explicit,
        "isAvailable": True,
        "thumbnails": thumbs,
    }


def parse_two_row_card(renderer: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """Extract `(item_type, item_id, title, subtitle)` from a `musicTwoRowItemRenderer`."""
    title = _join_runs_text(renderer.get("title", {}).get("runs", []))
    subtitle = _join_runs_text(renderer.get("subtitle", {}).get("runs", []))
    browse_ep = renderer.get("navigationEndpoint", {}).get("browseEndpoint", {})
    if not isinstance(browse_ep, dict):
        browse_ep = {}
    browse_id = str(browse_ep.get("browseId") or "").strip()
    page_type = str(
        browse_ep.get("browseEndpointContextSupportedConfigs", {})
        .get("browseEndpointContextMusicConfig", {})
        .get("pageType")
        or ""
    ).strip()

    if page_type == "MUSIC_PAGE_TYPE_ALBUM" or browse_id.startswith("MPRE"):
        return "album", normalize_album_id(browse_id), title, subtitle
    if (
        page_type == "MUSIC_PAGE_TYPE_PLAYLIST"
        or browse_id.startswith(("VL", "RDCL", "PL"))
    ):
        return "playlist", normalize_playlist_id(browse_id), title, subtitle

    watch_vid = str(
        renderer.get("navigationEndpoint", {})
        .get("watchEndpoint", {})
        .get("videoId")
        or ""
    ).strip()
    if watch_vid:
        return "video", watch_vid, title, subtitle
    return "unknown", browse_id, title, subtitle


def extract_category_shelves(browse_response: Dict[str, Any]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Extract `[(shelf_title, items), ...]` from a raw `FEmusic_moods_and_genres_category` response."""
    sections = (
        browse_response.get("contents", {})
        .get("singleColumnBrowseResultsRenderer", {})
        .get("tabs", [{}])[0]
        .get("tabRenderer", {})
        .get("content", {})
        .get("sectionListRenderer", {})
        .get("contents", [])
    )
    if not isinstance(sections, list):
        return []

    shelves: List[Tuple[str, List[Dict[str, Any]]]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        if "musicCarouselShelfRenderer" in section:
            carousel = section["musicCarouselShelfRenderer"]
            hdr_runs = (
                carousel.get("header", {})
                .get("musicCarouselShelfBasicHeaderRenderer", {})
                .get("title", {})
                .get("runs", [])
            )
            shelf_title = _join_runs_text(hdr_runs)
            items = carousel.get("contents", [])
            if isinstance(items, list):
                shelves.append((shelf_title, items))
        elif "gridRenderer" in section:
            grid = section["gridRenderer"]
            hdr_runs = (
                grid.get("header", {})
                .get("gridHeaderRenderer", {})
                .get("title", {})
                .get("runs", [])
            )
            shelf_title = _join_runs_text(hdr_runs) or "Grid"
            items = grid.get("items", [])
            if isinstance(items, list):
                shelves.append((shelf_title, items))
    return shelves


def infer_era(category_title: str, shelf_title: str) -> str:
    """Return a decade label (e.g. `'1980s'`) if either shelf_title or category_title is a decade."""
    for candidate in (shelf_title.strip(), category_title.strip()):
        if DECADE_RE.match(candidate):
            return candidate
    return ""


def process_mood_category(
    yt: Any,
    store: CatalogStore,
    *,
    location: str,
    section: str,
    category_title: str,
    category_params: str,
) -> Dict[str, int]:
    """Browse one mood/genre category page, log shelf items, and fetch new playlists/albums."""
    mood_key = f"{location}|{section}|{category_title}|{category_params}"
    stats = {
        "mood_hits": 0,
        "playlists_fetched": 0,
        "playlists_skipped": 0,
        "albums_fetched": 0,
        "albums_skipped": 0,
        "new_tracks": 0,
    }

    store.pace()
    try:
        resp = call_with_retry(
            yt._send_request,
            "browse",
            {"browseId": "FEmusic_moods_and_genres_category", "params": category_params},
            max_retries=store.max_retries,
        )
    except Exception as err:  # noqa: BLE001
        store.record_failure("mood_category", mood_key, err)
        return stats

    if not isinstance(resp, dict):
        return stats

    shelves = extract_category_shelves(resp)
    for shelf_title, items in shelves:
        if shelf_title.strip().casefold() in SKIPPED_SHELF_TITLES:
            continue
        era = infer_era(category_title, shelf_title)

        for rank, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue

            if "musicResponsiveListItemRenderer" in item:
                renderer = item["musicResponsiveListItemRenderer"]
                song_dict = parse_responsive_song_item(renderer)
                vid = str(song_dict.get("videoId") or "").strip()
                artist_str = "; ".join(
                    a["name"] for a in song_dict.get("artists", []) if a.get("name")
                )
                store.record_mood_hit(
                    location=location,
                    section=section,
                    category_title=category_title,
                    category_params=category_params,
                    shelf_title=shelf_title,
                    era=era,
                    shelf_rank=rank,
                    item_type="song",
                    item_id=vid,
                    item_title=str(song_dict.get("title") or "").strip(),
                    item_subtitle=artist_str,
                    is_official_editorial=False,
                    raw_obj=item,
                )
                stats["mood_hits"] += 1
                if vid and store.record_track_if_new(
                    song_dict,
                    source_type="mood_category_songs",
                    source_id=mood_key,
                ):
                    stats["new_tracks"] += 1

            elif "musicTwoRowItemRenderer" in item:
                renderer = item["musicTwoRowItemRenderer"]
                item_type, item_id, card_title, card_sub = parse_two_row_card(renderer)
                if item_type in ("video", "unknown") or not item_id:
                    continue

                is_official = item_type == "playlist" and is_official_featured_playlist(item_id)
                store.record_mood_hit(
                    location=location,
                    section=section,
                    category_title=category_title,
                    category_params=category_params,
                    shelf_title=shelf_title,
                    era=era,
                    shelf_rank=rank,
                    item_type=item_type,
                    item_id=item_id,
                    item_title=card_title,
                    item_subtitle=card_sub,
                    is_official_editorial=is_official,
                    raw_obj=item,
                )
                stats["mood_hits"] += 1

                if item_type == "playlist" and is_official:
                    status, _, added = store.process_playlist_if_new(
                        yt,
                        item_id,
                        source_label=f"mood:{mood_key}",
                    )
                    if status == "fetched":
                        stats["playlists_fetched"] += 1
                        stats["new_tracks"] += added
                    elif status == "skipped_existing":
                        stats["playlists_skipped"] += 1

                elif item_type == "album" and is_valid_album_id(item_id):
                    status, _, added = store.process_album_if_new(
                        yt,
                        item_id,
                        source_label=f"mood:{mood_key}",
                    )
                    if status == "fetched":
                        stats["albums_fetched"] += 1
                        stats["new_tracks"] += added
                    elif status == "skipped_existing":
                        stats["albums_skipped"] += 1

    store.mark_mood_completed(mood_key)
    return stats


def run_mood_collection(
    store: CatalogStore,
    *,
    locations: Sequence[str] = ("US",),
    category_filter: Optional[Sequence[str]] = None,
    max_categories: int = 0,
    yt_factory: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    """Harvest `get_mood_categories()` across requested locations, deduplicating against existing CSVs."""
    if yt_factory is None:
        def default_factory(loc: str) -> Any:
            return YTMusic(location=loc, requests_session=TimeoutSession())
        yt_factory = default_factory

    allowed_titles = {c.strip().casefold() for c in category_filter if c.strip()} if category_filter else None

    totals = {
        "categories_discovered": 0,
        "already_completed_categories": 0,
        "categories_executed": 0,
        "mood_hits": 0,
        "playlists_fetched": 0,
        "playlists_skipped": 0,
        "albums_fetched": 0,
        "albums_skipped": 0,
        "new_tracks": 0,
        "interrupted": False,
    }

    pending_tasks: List[Tuple[str, Any, str, str, str]] = []
    for location in locations:
        yt = yt_factory(location)
        store.pace()
        mood_cats = call_with_retry(yt.get_mood_categories, max_retries=store.max_retries)
        if not isinstance(mood_cats, dict):
            continue
        for section, cats in mood_cats.items():
            if not isinstance(cats, list):
                continue
            for cat in cats:
                if not isinstance(cat, dict):
                    continue
                c_title = str(cat.get("title") or "").strip()
                c_params = str(cat.get("params") or "").strip()
                if not c_title or not c_params:
                    continue
                if allowed_titles and c_title.casefold() not in allowed_titles:
                    continue
                totals["categories_discovered"] += 1
                mood_key = f"{location}|{section}|{c_title}|{c_params}"
                if mood_key in store.completed_moods:
                    totals["already_completed_categories"] += 1
                else:
                    pending_tasks.append((location, yt, section, c_title, c_params))

    target_tasks = pending_tasks[:max_categories] if max_categories > 0 else pending_tasks
    if not target_tasks:
        console.print("[green]All requested mood/genre categories are already completed![/green]")
        return totals

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Harvesting YTM Mood/Genre Pages", total=len(target_tasks))
        try:
            for location, yt, section, c_title, c_params in target_tasks:
                progress.update(
                    task,
                    description=f"[cyan]{location}[/cyan] | {section} | [bold]{c_title}[/bold]",
                )
                c_stats = process_mood_category(
                    yt,
                    store,
                    location=location,
                    section=section,
                    category_title=c_title,
                    category_params=c_params,
                )
                totals["categories_executed"] += 1
                for k in (
                    "mood_hits",
                    "playlists_fetched",
                    "playlists_skipped",
                    "albums_fetched",
                    "albums_skipped",
                    "new_tracks",
                ):
                    totals[k] += c_stats[k]
                progress.advance(task)
        except KeyboardInterrupt:
            totals["interrupted"] = True
            console.print("\n[yellow]Interrupted by user (Ctrl+C). Completed items are flushed.[/yellow]")

    totals["total_unique_playlists"] = len(store.seen_playlists)
    totals["total_unique_albums"] = len(store.seen_albums)
    totals["total_unique_tracks"] = len(store.seen_tracks)
    return totals


def print_summary(totals: Dict[str, Any], data_dir: Path) -> None:
    table = Table(title="YouTube Music Mood/Genre Categories Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right", style="bold")

    table.add_row("Categories discovered", f"{totals.get('categories_discovered', 0):,}")
    table.add_row("Previously completed categories", f"{totals.get('already_completed_categories', 0):,}")
    table.add_row("Categories executed this run", f"{totals.get('categories_executed', 0):,}")
    table.add_row("Shelf items logged this run", f"{totals.get('mood_hits', 0):,}")
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
        default=["US"],
        help="Comma-separated YTM location codes (default: US)",
    )
    parser.add_argument(
        "--categories",
        type=parse_csv_list,
        default=None,
        help="Optional comma-separated subset of category titles (e.g. 'Rock,Jazz,Decades')",
    )
    parser.add_argument("--delay", type=float, default=0.4, help="Seconds between API calls (default: 0.4)")
    parser.add_argument("--max-retries", type=int, default=2, help="Retries per failed request (default: 2)")
    parser.add_argument(
        "--max-categories",
        type=int,
        default=0,
        help="Max categories to process before exiting (0 = all pending)",
    )
    args = parser.parse_args()

    with CatalogStore(
        data_dir=args.data_dir,
        state_dir=args.state_dir,
        delay=args.delay,
        max_retries=args.max_retries,
    ) as store:
        totals = run_mood_collection(
            store,
            locations=args.locations,
            category_filter=args.categories,
            max_categories=args.max_categories,
        )
        print_summary(totals, args.data_dir)

    return 130 if totals.get("interrupted") else 0


if __name__ == "__main__":
    raise SystemExit(main())
