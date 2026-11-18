"""Indefinite, resumable Last.fm snowball sampler for data/master_lastfm.csv.

Starts from tracks in data/master_lastfm.csv that already have track-level genre
tags (track_tag_count > 0), then continuously expands via:
1. Each track-tagged song's similar_tracks (and track.getSimilar on new hits)
2. Each new artist's catalog via paginated artist.getTopTracks (albums & singles)

Candidate tracks are gated by track.getTopTags first: only tracks with at least
one track-level tag (track_tag_count > 0) are fully enriched and appended to
data/master_lastfm.csv. Untagged candidates and already-expanded artists are
recorded in data/snowball_state.json so Ctrl+C and restarts resume cleanly.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional, Sequence, Set, Tuple

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

_PIPELINE_DIR = str(Path(__file__).resolve().parent)
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

from build_master import name_key
from build_master_genre import (
    ARTIST_COLUMNS,
    DEFAULT_ENV_PATH,
    LastFmClient,
    LastFmFatalError,
    LastFmTransientError,
    format_tags,
    load_api_key,
    merge_genre_tags,
    normalize_tag_list,
    track_candidates,
)

console = Console()

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LASTFM_CSV = PROJECT_DIR / "data/master_lastfm.csv"
DEFAULT_STATE_JSON = PROJECT_DIR / "data/snowball_state.json"

LASTFM_FIELDNAMES = [
    "lfm_track_name",
    "lfm_artist_name",
    "lfm_track_mbid",
    "lfm_artist_mbid",
    "genre_source",
    "genres_all",
    "track_tag_count",
    "track_tags_all",
    "track_tags_names",
    "artist_tag_count",
    "artist_tags_all",
    "artist_tags_names",
    "duration_ms",
    "track_listeners",
    "track_playcount",
    "artist_listeners",
    "artist_playcount",
    "album_title",
    "album_artist",
    "similar_tracks",
    "similar_artists",
]

LASTFM_ARTIST_COLUMNS = [
    "lfm_artist_name",
    "lfm_artist_mbid",
    "artist_listeners",
    "artist_playcount",
    "similar_artists",
    "artist_tag_count",
    "artist_tags_all",
    "artist_tags_names",
]

SIMILAR_SCORE_SUFFIX_RE = re.compile(r"\s+\([0-9.]+\)\s*$")


def artist_key(artist: str) -> str:
    """Normalize an artist name for case- and whitespace-insensitive deduplication."""
    return " ".join(artist.strip().split()).casefold()


def parse_similar_tracks_cell(cell: str) -> List[Tuple[str, str]]:
    """Parse 'Artist - Track (score) | ...' into unique (artist, track) pairs."""
    if not cell or not cell.strip():
        return []

    parsed: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for raw_part in cell.split(" | "):
        part = raw_part.strip()
        if not part:
            continue
        cleaned = SIMILAR_SCORE_SUFFIX_RE.sub("", part).strip()
        if " - " not in cleaned:
            continue
        sim_artist, sim_track = cleaned.split(" - ", 1)
        sim_artist = sim_artist.strip()
        sim_track = sim_track.strip()
        if not sim_artist or not sim_track:
            continue
        key = name_key(sim_artist, sim_track)
        if key not in seen:
            seen.add(key)
            parsed.append((sim_artist, sim_track))
    return parsed


def format_similar_tracks(similar_tracks: Sequence[dict]) -> str:
    """Format Last.fm track.getSimilar entries into 'Artist - Track (match) | ...'."""
    formatted: List[str] = []
    for sim in similar_tracks:
        if not isinstance(sim, dict):
            continue
        artist_obj = sim.get("artist")
        if isinstance(artist_obj, dict):
            sim_artist = str(artist_obj.get("name") or "").strip()
        else:
            sim_artist = str(artist_obj or "").strip()
        sim_name = str(sim.get("name") or "").strip()
        sim_match = str(sim.get("match") or "").strip()
        label = f"{sim_artist} - {sim_name}" if sim_artist else sim_name
        if sim_match and label:
            label = f"{label} ({sim_match})"
        if label:
            formatted.append(label)
    return " | ".join(formatted)


def load_existing_lastfm_csv(
    path: Path,
) -> Tuple[List[Dict[str, str]], Set[Tuple[str, str]], Dict[str, Dict[str, str]]]:
    """Validate and load rows, seen track keys, and cached artist metadata."""
    rows: List[Dict[str, str]] = []
    seen_tracks: Set[Tuple[str, str]] = set()
    artist_cache: Dict[str, Dict[str, str]] = {}

    if not path.exists() or not path.stat().st_size:
        return rows, seen_tracks, artist_cache

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        fields = reader.fieldnames or []
        if fields != LASTFM_FIELDNAMES:
            raise ValueError(
                f"{path.name}: unexpected columns; expected {', '.join(LASTFM_FIELDNAMES)}"
            )
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path.name}: malformed row near line {reader.line_num}")
            rows.append(row)

            artist = (row.get("lfm_artist_name") or "").strip()
            track = (row.get("lfm_track_name") or "").strip()
            if artist and track:
                seen_tracks.add(name_key(artist, track))

            if artist and (
                (row.get("artist_tags_names") or "").strip()
                or (row.get("artist_listeners") or "").strip()
                or (row.get("similar_artists") or "").strip()
            ):
                cached = {col: (row.get(col) or "").strip() for col in ARTIST_COLUMNS}
                for cache_id in {artist.casefold(), artist_key(artist)}:
                    if cache_id and cache_id not in artist_cache:
                        artist_cache[cache_id] = dict(cached)

    return rows, seen_tracks, artist_cache


def load_snowball_state(path: Path) -> Tuple[Set[str], Set[Tuple[str, str]]]:
    """Load expanded artists and rejected untagged track keys from JSON state."""
    if not path.exists() or not path.stat().st_size:
        return set(), set()

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name}: state JSON must be an object")

    raw_artists = payload.get("expanded_artists") or []
    raw_rejected = payload.get("rejected_tracks") or []

    expanded_artists = {
        artist_key(str(item)) for item in raw_artists if str(item).strip()
    }
    rejected_tracks: Set[Tuple[str, str]] = set()
    for pair in raw_rejected:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            a_norm, t_norm = name_key(str(pair[0]), str(pair[1]))
            if a_norm and t_norm:
                rejected_tracks.add((a_norm, t_norm))

    return expanded_artists, rejected_tracks


def save_snowball_state(
    path: Path,
    expanded_artists: Set[str],
    rejected_tracks: Set[Tuple[str, str]],
) -> None:
    """Atomically write snowball checkpoint state to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "expanded_artists": sorted(expanded_artists),
        "rejected_tracks": [list(pair) for pair in sorted(rejected_tracks)],
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def append_lastfm_row(output_path: Path, row: Dict[str, str]) -> None:
    """Append one 21-column Last.fm row to master_lastfm.csv and fsync immediately."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not output_path.exists() or output_path.stat().st_size == 0

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=LASTFM_FIELDNAMES)
    if write_header:
        writer.writeheader()
    writer.writerow({col: row.get(col, "") for col in LASTFM_FIELDNAMES})
    payload = buffer.getvalue().encode("utf-8")

    if not write_header:
        with output_path.open("rb") as existing:
            existing.seek(-1, os.SEEK_END)
            if existing.read(1) not in (b"\n", b"\r"):
                payload = b"\n" + payload

    with output_path.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


class SnowballLastFmClient(LastFmClient):
    """Last.fm client extended with artist catalog discovery and gated enrichment."""

    def get_similar_tracks(
        self, artist: str, track_name: str, limit: int = 20
    ) -> List[dict]:
        for candidate in track_candidates(track_name):
            data = self.call(
                "track.getSimilar",
                artist=artist,
                track=candidate,
                autocorrect="1",
                limit=str(limit),
            )
            sim = (
                data.get("similartracks")
                if isinstance(data.get("similartracks"), dict)
                else {}
            )
            raw = sim.get("track")
            if isinstance(raw, dict):
                raw = [raw]
            if isinstance(raw, list) and raw:
                return [item for item in raw if isinstance(item, dict) and item.get("name")]
        return []

    def get_artist_top_tracks(
        self,
        artist: str,
        *,
        min_listeners: int = 5000,
        max_pages: int = 10,
        page_limit: int = 100,
    ) -> List[Tuple[str, str]]:
        """Fetch an artist's popular tracks (albums + singles) down to min_listeners."""
        collected: List[Tuple[str, str]] = []
        seen: Set[Tuple[str, str]] = set()

        for page in range(1, max_pages + 1):
            data = self.call(
                "artist.getTopTracks",
                artist=artist,
                autocorrect="1",
                limit=str(page_limit),
                page=str(page),
            )
            toptracks = (
                data.get("toptracks")
                if isinstance(data.get("toptracks"), dict)
                else {}
            )
            raw_tracks = toptracks.get("track")
            if isinstance(raw_tracks, dict):
                raw_tracks = [raw_tracks]
            if not isinstance(raw_tracks, list) or not raw_tracks:
                break

            hit_floor = False
            for item in raw_tracks:
                if not isinstance(item, dict):
                    continue
                t_name = str(item.get("name") or "").strip()
                if not t_name:
                    continue

                artist_obj = item.get("artist")
                if isinstance(artist_obj, dict):
                    item_artist = str(artist_obj.get("name") or artist).strip()
                else:
                    item_artist = str(artist_obj or artist).strip()
                if not item_artist:
                    item_artist = artist.strip()

                try:
                    listeners = int(str(item.get("listeners") or "0").strip() or "0")
                except ValueError:
                    listeners = 0

                if listeners < min_listeners:
                    hit_floor = True
                    break

                key = name_key(item_artist, t_name)
                if key not in seen:
                    seen.add(key)
                    collected.append((item_artist, t_name))

            if hit_floor:
                break

            attr = (
                toptracks.get("@attr")
                if isinstance(toptracks.get("@attr"), dict)
                else {}
            )
            try:
                total_pages = int(str(attr.get("totalPages") or "0").strip() or "0")
            except ValueError:
                total_pages = 0
            if total_pages and page >= total_pages:
                break

        return collected

    def enrich_candidate_if_tagged(
        self,
        artist: str,
        track_name: str,
        *,
        similar_limit: int = 20,
        seen_tracks: Set[Tuple[str, str]],
    ) -> Tuple[Optional[Dict[str, str]], str, bool]:
        """Gate on track.getTopTags, then fetch full 21-col row if tagged.

        Returns (row_or_none, status, artist_cache_hit) where status is one of
        'added', 'untagged', or 'already_seen'.
        """
        t_tags = self.get_track_tags(artist, track_name)
        if not t_tags:
            return None, "untagged", False

        t_info = self.get_track_info(artist, track_name)
        lfm_track_name = str(t_info.get("name") or track_name).strip()

        info_artist_obj = t_info.get("artist")
        if isinstance(info_artist_obj, dict):
            canonical_artist = str(info_artist_obj.get("name") or artist).strip()
        else:
            canonical_artist = str(info_artist_obj or artist).strip()
        if not canonical_artist:
            canonical_artist = artist.strip()

        if name_key(canonical_artist, lfm_track_name) in seen_tracks:
            return None, "already_seen", False

        artist_fields, cache_hit = self.get_artist_fields(canonical_artist)
        final_artist = (artist_fields.get("lfm_artist_name") or canonical_artist).strip()

        if name_key(final_artist, lfm_track_name) in seen_tracks:
            return None, "already_seen", cache_hit

        t_similar = self.get_similar_tracks(
            final_artist, lfm_track_name, limit=similar_limit
        )
        album = t_info.get("album") if isinstance(t_info.get("album"), dict) else {}

        t_tags_all, t_tags_names = format_tags(t_tags)
        track_tag_list = [
            str(t["name"]).strip() for t in t_tags if str(t.get("name") or "").strip()
        ]
        artist_tag_list = [
            tag.strip()
            for tag in artist_fields.get("artist_tags_names", "").split("|")
            if tag.strip()
        ]
        genre_source, genres_all = merge_genre_tags(track_tag_list, artist_tag_list)

        row: Dict[str, str] = {
            "lfm_track_name": lfm_track_name,
            "lfm_artist_name": final_artist,
            "lfm_track_mbid": str(t_info.get("mbid") or "").strip(),
            "lfm_artist_mbid": artist_fields.get("lfm_artist_mbid", ""),
            "genre_source": genre_source,
            "genres_all": genres_all,
            "track_tag_count": str(len(t_tags)),
            "track_tags_all": t_tags_all,
            "track_tags_names": t_tags_names,
            "artist_tag_count": artist_fields.get("artist_tag_count", "0"),
            "artist_tags_all": artist_fields.get("artist_tags_all", ""),
            "artist_tags_names": artist_fields.get("artist_tags_names", ""),
            "duration_ms": str(t_info.get("duration") or "").strip(),
            "track_listeners": str(t_info.get("listeners") or "").strip(),
            "track_playcount": str(t_info.get("playcount") or "").strip(),
            "artist_listeners": artist_fields.get("artist_listeners", ""),
            "artist_playcount": artist_fields.get("artist_playcount", ""),
            "album_title": str(album.get("title") or "").strip(),
            "album_artist": str(album.get("artist") or "").strip(),
            "similar_tracks": format_similar_tracks(t_similar),
            "similar_artists": artist_fields.get("similar_artists", ""),
        }
        return row, "added", cache_hit


def run_snowball(
    csv_path: Path = DEFAULT_LASTFM_CSV,
    state_path: Path = DEFAULT_STATE_JSON,
    env_path: Path = DEFAULT_ENV_PATH,
    *,
    min_track_listeners: int = 5000,
    similar_limit: int = 20,
    max_artist_pages: int = 10,
    track_batch_size: int = 25,
    state_save_interval: int = 25,
    max_new_tracks: Optional[int] = None,
    min_interval_sec: float = 0.22,
    client: Optional[SnowballLastFmClient] = None,
) -> Dict[str, int]:
    """Run the indefinite Last.fm snowball loop over track-tagged seeds."""
    csv_path = Path(csv_path).resolve()
    state_path = Path(state_path).resolve()

    if not csv_path.exists():
        raise FileNotFoundError(f"Input CSV '{csv_path}' does not exist.")

    rows, seen_tracks, seeded_artists = load_existing_lastfm_csv(csv_path)
    expanded_artists, rejected_tracks = load_snowball_state(state_path)

    artist_queue: Deque[str] = deque()
    queued_artists: Set[str] = set()
    track_queue: Deque[Tuple[str, str]] = deque()
    queued_tracks: Set[Tuple[str, str]] = set()

    def enqueue_artist(name: str) -> None:
        cleaned = name.strip()
        if not cleaned:
            return
        key = artist_key(cleaned)
        if not key or key in expanded_artists or key in queued_artists:
            return
        queued_artists.add(key)
        artist_queue.append(cleaned)

    def enqueue_track(artist: str, track: str) -> None:
        a_clean = artist.strip()
        t_clean = track.strip()
        if not a_clean or not t_clean:
            return
        key = name_key(a_clean, t_clean)
        if key in seen_tracks or key in rejected_tracks or key in queued_tracks:
            return
        queued_tracks.add(key)
        track_queue.append((a_clean, t_clean))

    seed_rows = 0
    for row in rows:
        try:
            tag_count = int((row.get("track_tag_count") or "0").strip() or "0")
        except ValueError:
            tag_count = 0
        if tag_count <= 0:
            continue
        seed_rows += 1
        enqueue_artist(row.get("lfm_artist_name") or "")
        for sim_artist, sim_track in parse_similar_tracks_cell(
            row.get("similar_tracks") or ""
        ):
            enqueue_track(sim_artist, sim_track)

    console.print("=" * 72)
    console.print("[bold green]Last.fm Track-Tagged Snowball Sampler[/bold green]")
    console.print(f"[*] Target CSV:             {csv_path} ({len(rows):,} rows)")
    console.print(f"[*] State Checkpoint:       {state_path}")
    console.print(f"[*] Track-Tagged Seeds:     [cyan]{seed_rows:,}[/cyan] rows")
    console.print(f"[*] Seeded Artist Cache:    [cyan]{len(seeded_artists):,}[/cyan] keys")
    console.print(
        f"[*] Prior State:            expanded={len(expanded_artists):,} artists, "
        f"rejected={len(rejected_tracks):,} untagged tracks"
    )
    console.print(
        f"[*] Initial Queues:         artists=[yellow]{len(artist_queue):,}[/yellow], "
        f"tracks=[yellow]{len(track_queue):,}[/yellow]"
    )
    console.print(
        f"[*] Params:                 min_listeners>={min_track_listeners:,}, "
        f"similar_limit={similar_limit}, max_artist_pages={max_artist_pages}"
    )
    console.print("=" * 72)

    if not artist_queue and not track_queue:
        console.print("\n[bold green]✓ Nothing left in queues; snowball is exhausted![/bold green]")
        return {
            "existing_rows": len(rows),
            "seed_rows": seed_rows,
            "tracks_checked": 0,
            "tracks_added": 0,
            "tracks_rejected": 0,
            "tracks_already_seen": 0,
            "artists_expanded": 0,
            "artist_cache_hits": 0,
            "transient_failures": 0,
            "interrupted": 0,
            "total_rows": len(rows),
        }

    owns_client = client is None
    if client is None:
        api_key = load_api_key(env_path)
        active_client = SnowballLastFmClient(
            api_key,
            min_interval_sec=min_interval_sec,
            artist_cache=seeded_artists,
        )
    else:
        for key, value in seeded_artists.items():
            client.artist_cache.setdefault(key, value)
        active_client = client

    tracks_checked = 0
    tracks_added = 0
    tracks_rejected = 0
    tracks_already_seen = 0
    artists_expanded = 0
    artist_cache_hits = 0
    transient_failures = 0
    ops_since_save = 0
    interrupted = False

    def maybe_save_state(*, force: bool = False) -> None:
        nonlocal ops_since_save
        if force or ops_since_save >= state_save_interval:
            save_snowball_state(state_path, expanded_artists, rejected_tracks)
            ops_since_save = 0

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("Snowballing...", total=None)

            while artist_queue or track_queue:
                if max_new_tracks is not None and tracks_added >= max_new_tracks:
                    break

                processed_in_batch = 0
                while track_queue and processed_in_batch < track_batch_size:
                    if max_new_tracks is not None and tracks_added >= max_new_tracks:
                        break

                    cand_artist, cand_track = track_queue.popleft()
                    cand_key = name_key(cand_artist, cand_track)
                    queued_tracks.discard(cand_key)

                    if cand_key in seen_tracks or cand_key in rejected_tracks:
                        continue

                    processed_in_batch += 1
                    tracks_checked += 1
                    progress.update(
                        task_id,
                        description=(
                            f"[+{tracks_added:,} | rej={tracks_rejected:,} | "
                            f"Q: A={len(artist_queue):,} T={len(track_queue):,}] "
                            f"{cand_artist} — {cand_track}"
                        )[:95],
                    )

                    try:
                        enriched_row, status, cache_hit = (
                            active_client.enrich_candidate_if_tagged(
                                cand_artist,
                                cand_track,
                                similar_limit=similar_limit,
                                seen_tracks=seen_tracks,
                            )
                        )
                    except LastFmTransientError as err:
                        transient_failures += 1
                        console.print(
                            f"[yellow]Warning: transient failure checking "
                            f"{cand_artist} - {cand_track} ({err}); leaving uncached.[/yellow]"
                        )
                        continue

                    if status == "untagged":
                        rejected_tracks.add(cand_key)
                        tracks_rejected += 1
                        ops_since_save += 1
                    elif status == "already_seen":
                        seen_tracks.add(cand_key)
                        tracks_already_seen += 1
                    elif enriched_row is not None:
                        append_lastfm_row(csv_path, enriched_row)
                        seen_tracks.add(cand_key)
                        canon_key = name_key(
                            enriched_row["lfm_artist_name"],
                            enriched_row["lfm_track_name"],
                        )
                        seen_tracks.add(canon_key)
                        if cache_hit:
                            artist_cache_hits += 1
                        tracks_added += 1
                        ops_since_save += 1

                        enqueue_artist(enriched_row["lfm_artist_name"])
                        for sim_artist, sim_track in parse_similar_tracks_cell(
                            enriched_row.get("similar_tracks") or ""
                        ):
                            enqueue_track(sim_artist, sim_track)

                    maybe_save_state()

                if max_new_tracks is not None and tracks_added >= max_new_tracks:
                    break

                if artist_queue:
                    target_artist = artist_queue.popleft()
                    a_norm = artist_key(target_artist)
                    queued_artists.discard(a_norm)
                    if a_norm in expanded_artists:
                        continue

                    progress.update(
                        task_id,
                        description=(
                            f"[+{tracks_added:,} | rej={tracks_rejected:,} | "
                            f"Q: A={len(artist_queue):,} T={len(track_queue):,}] "
                            f"Expanding artist: {target_artist}"
                        )[:95],
                    )

                    try:
                        top_tracks = active_client.get_artist_top_tracks(
                            target_artist,
                            min_listeners=min_track_listeners,
                            max_pages=max_artist_pages,
                        )
                    except LastFmTransientError as err:
                        transient_failures += 1
                        console.print(
                            f"[yellow]Warning: transient failure expanding artist "
                            f"{target_artist} ({err}); will retry next run.[/yellow]"
                        )
                        continue

                    expanded_artists.add(a_norm)
                    artists_expanded += 1
                    ops_since_save += 1
                    for a_name, t_name in top_tracks:
                        enqueue_track(a_name, t_name)
                    maybe_save_state()

    except KeyboardInterrupt:
        interrupted = True
        console.print(
            "\n[yellow]Interrupted (Ctrl+C). Saving checkpoint state...[/yellow]"
        )
    finally:
        save_snowball_state(state_path, expanded_artists, rejected_tracks)
        if owns_client:
            active_client.close()

    total_rows = len(rows) + tracks_added
    summary = Table(title="Last.fm Snowball Summary", border_style="cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")
    summary.add_row("Initial Rows in master_lastfm.csv", f"{len(rows):,}")
    summary.add_row("Candidates Checked", f"{tracks_checked:,}")
    summary.add_row("New Track-Tagged Rows Added", f"[bold green]{tracks_added:,}[/bold green]")
    summary.add_row("Skipped (0 Track Tags)", f"{tracks_rejected:,}")
    summary.add_row("Skipped (Canonical Already Seen)", f"{tracks_already_seen:,}")
    summary.add_row("Artists Expanded (getTopTracks)", f"{artists_expanded:,}")
    summary.add_row("Artist Cache Hits", f"{artist_cache_hits:,}")
    summary.add_row(
        "Remaining Queues",
        f"artists={len(artist_queue):,}, tracks={len(track_queue):,}",
    )
    if transient_failures:
        summary.add_row("Transient Failures", f"[yellow]{transient_failures:,}[/yellow]")
    summary.add_row("Total Rows in master_lastfm.csv", f"[bold cyan]{total_rows:,}[/bold cyan]")

    console.print()
    console.print(summary)

    return {
        "existing_rows": len(rows),
        "seed_rows": seed_rows,
        "tracks_checked": tracks_checked,
        "tracks_added": tracks_added,
        "tracks_rejected": tracks_rejected,
        "tracks_already_seen": tracks_already_seen,
        "artists_expanded": artists_expanded,
        "artist_cache_hits": artist_cache_hits,
        "transient_failures": transient_failures,
        "interrupted": 1 if interrupted else 0,
        "total_rows": total_rows,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Indefinitely snowball data/master_lastfm.csv from track-tagged songs "
            "via similar tracks and artist catalogs."
        )
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_LASTFM_CSV,
        help=f"Target master_lastfm.csv path (default: {DEFAULT_LASTFM_CSV})",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_JSON,
        help=f"Resume state JSON path (default: {DEFAULT_STATE_JSON})",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help=f"Path to .env containing LASTFM_API_KEY (default: {DEFAULT_ENV_PATH})",
    )
    parser.add_argument(
        "--min-track-listeners",
        type=int,
        default=5000,
        help="Minimum Last.fm track listeners when paginating artist.getTopTracks (default: 5000)",
    )
    parser.add_argument(
        "--similar-limit",
        type=int,
        default=20,
        help="Number of similar tracks requested per new track (default: 20)",
    )
    parser.add_argument(
        "--max-artist-pages",
        type=int,
        default=10,
        help="Max pages (100 tracks/page) per artist.getTopTracks expansion (default: 10)",
    )
    parser.add_argument(
        "--track-batch-size",
        type=int,
        default=25,
        help="Tracks to check between artist catalog expansions (default: 25)",
    )
    parser.add_argument(
        "--max-new-tracks",
        type=int,
        default=None,
        help="Optional cap on new track-tagged rows to append before stopping (default: unlimited)",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=0.22,
        help="Minimum seconds between Last.fm HTTP requests (default: 0.22)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        result = run_snowball(
            csv_path=args.csv,
            state_path=args.state,
            env_path=args.env,
            min_track_listeners=args.min_track_listeners,
            similar_limit=args.similar_limit,
            max_artist_pages=args.max_artist_pages,
            track_batch_size=args.track_batch_size,
            max_new_tracks=args.max_new_tracks,
            min_interval_sec=args.min_interval,
        )
    except LastFmFatalError as err:
        console.print(f"[bold red]Fatal Last.fm API error: {err}[/bold red]")
        sys.exit(1)
    if result["interrupted"]:
        sys.exit(130)
