"""Resumable Last.fm Genre & Metadata Enricher for Spotify Charts.

Reads unique tracks from data/master.csv, fetches all available per-track and
primary-artist metadata from the Last.fm API, and appends each completed track
row directly to data/master_genre.csv.

Designed to be interrupted (Ctrl+C) and re-run safely:
- Reads data/master_genre.csv on startup and skips tracks already enriched
- Seeds an in-memory artist cache from existing rows in data/master_genre.csv
  so already-fetched artists are not queried again across restarts
- Flushes and fsyncs each completed row to disk immediately
- Leaves transient network/rate-limit failures unwritten so they are retried
"""

from __future__ import annotations

import argparse
import csv
import html
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

_PIPELINE_DIR = str(Path(__file__).resolve().parent)
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

from build_master import CSV_FIELDNAMES as MASTER_FIELDNAMES
from build_master import name_key, read_chart_rows

console = Console()

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MASTER_CSV = PROJECT_DIR / "data/master.csv"
DEFAULT_OUTPUT_CSV = PROJECT_DIR / "data/master_genre.csv"
DEFAULT_ENV_PATH = PROJECT_DIR / ".env"

LASTFM_HOST = "ws.audioscrobbler.com"
LASTFM_PATH = "/2.0/"
USER_AGENT = "spotify-charts-genre-enricher/1.0"

ARTIST_COLUMNS = [
    "lfm_artist_name",
    "lfm_artist_mbid",
    "lfm_artist_url",
    "artist_ontour",
    "artist_listeners",
    "artist_playcount",
    "artist_image_url",
    "similar_artists",
    "artist_tags_info",
    "artist_bio_published",
    "artist_bio_summary",
    "artist_bio_content",
    "artist_tag_count",
    "artist_tags_all",
    "artist_tags_names",
]

ENRICHMENT_COLUMNS = [
    "primary_artist",
    "genre_source",
    "genres_all",
    "lfm_track_name",
    "lfm_track_mbid",
    "lfm_track_url",
    "duration_ms",
    "track_streamable",
    "track_fulltrack",
    "track_listeners",
    "track_playcount",
    "album_title",
    "album_artist",
    "album_mbid",
    "album_url",
    "album_image_url",
    "track_toptags_info",
    "track_wiki_published",
    "track_wiki_summary",
    "track_wiki_content",
    "track_tag_count",
    "track_tags_all",
    "track_tags_names",
    "similar_tracks",
    *ARTIST_COLUMNS,
]

GENRE_FIELDNAMES = [*MASTER_FIELDNAMES, *ENRICHMENT_COLUMNS]

KNOWN_COMMA_ARTISTS = (
    "Tyler, The Creator",
    "Earth, Wind & Fire",
    "Crosby, Stills, Nash & Young",
    "Crosby, Stills & Nash",
    "Blood, Sweat & Tears",
    "Emerson, Lake & Palmer",
)

FEAT_RE = re.compile(r"\s*[\(\[](?:feat\.|ft\.|with)\s+[^\)\]]+[\)\]]", re.IGNORECASE)
VERSION_PAREN_RE = re.compile(
    r"\s*[\(\[][^\)\]]*(?:remix|remaster(?:ed)?|radio edit|version)[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
VERSION_DASH_RE = re.compile(
    r"\s+-\s+.*?(?:remix|remaster(?:ed)?|radio edit|version|edit).*$",
    re.IGNORECASE,
)
TAG_HTML_RE = re.compile(r"<[^>]+>")

TRANSIENT_LFM_ERRORS = {8, 11, 16, 29}
FATAL_LFM_ERRORS = {10, 26}


class LastFmTransientError(Exception):
    """Raised when a Last.fm request fails due to transient rate-limit or network errors."""


class LastFmFatalError(Exception):
    """Raised when Last.fm rejects the API key (invalid or suspended)."""


def load_api_key(env_path: Path = DEFAULT_ENV_PATH) -> str:
    """Load LASTFM_API_KEY from .env or process environment."""
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "LASTFM_API_KEY":
                token = value.strip().strip('"').strip("'")
                if token:
                    return token
    env_token = os.environ.get("LASTFM_API_KEY", "").strip()
    if env_token:
        return env_token
    raise RuntimeError(f"LASTFM_API_KEY not found in {env_path} or environment")


def extract_primary_artist(artist_names: str) -> str:
    """Return the lead artist while preserving well-known comma-containing artist names."""
    stripped = artist_names.strip()
    if not stripped:
        return ""
    lowered = stripped.casefold()
    for known in KNOWN_COMMA_ARTISTS:
        if lowered.startswith(known.casefold()):
            end_idx = len(known)
            if len(stripped) == end_idx or stripped[end_idx] in (",", " "):
                return stripped[:end_idx]
    return stripped.split(",", 1)[0].strip()


def track_candidates(track_name: str) -> List[str]:
    """Generate deduplicated track title variants from exact to stripped."""
    raw = track_name.strip()
    if not raw:
        return []

    without_feat = FEAT_RE.sub("", raw).strip()
    without_version = VERSION_DASH_RE.sub(
        "", VERSION_PAREN_RE.sub("", without_feat)
    ).strip()

    candidates: List[str] = []
    for candidate in (raw, without_feat, without_version):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def clean_text(text: Optional[str]) -> str:
    """Strip HTML tags, Last.fm boilerplate links, and collapse whitespace."""
    if not text:
        return ""
    unescaped = TAG_HTML_RE.sub("", html.unescape(text))
    unescaped = unescaped.replace("Read more on Last.fm.", "").strip()
    return " ".join(unescaped.split())


def pick_image(images: object) -> str:
    """Select the highest-resolution non-empty image URL from a Last.fm image list."""
    if not isinstance(images, list):
        return ""
    by_size: Dict[str, str] = {}
    for item in images:
        if isinstance(item, dict):
            url = str(item.get("#text") or "").strip()
            if url:
                by_size[str(item.get("size") or "")] = url
    for size in ("mega", "extralarge", "large", "medium", "small", ""):
        if size in by_size:
            return by_size[size]
    return ""


def normalize_tag_list(raw_tags: object) -> List[Dict[str, object]]:
    """Coerce Last.fm tag container (dict, list, or empty) into a list of tag dicts."""
    if isinstance(raw_tags, dict):
        raw_tags = [raw_tags]
    if not isinstance(raw_tags, list):
        return []
    return [t for t in raw_tags if isinstance(t, dict) and str(t.get("name") or "").strip()]


def format_tags(tags: Sequence[Dict[str, object]]) -> Tuple[str, str]:
    """Return ('name (count) | ...', 'name | ...') formatted strings for tag dicts."""
    with_counts: List[str] = []
    names_only: List[str] = []
    for tag in tags:
        name = str(tag.get("name") or "").strip()
        if not name:
            continue
        names_only.append(name)
        count = tag.get("count")
        with_counts.append(f"{name} ({count})" if count is not None else name)
    return " | ".join(with_counts), " | ".join(names_only)


def merge_genre_tags(
    track_tags: Sequence[str], artist_tags: Sequence[str]
) -> Tuple[str, str]:
    """Determine genre_source and deduplicated 'genres_all' string."""
    has_track = bool(track_tags)
    has_artist = bool(artist_tags)
    if has_track and has_artist:
        genre_source = "track+artist"
    elif has_track:
        genre_source = "track"
    elif has_artist:
        genre_source = "artist"
    else:
        genre_source = "none"

    seen: Set[str] = set()
    merged: List[str] = []
    for tag in [*track_tags, *artist_tags]:
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            merged.append(tag)
    return genre_source, " | ".join(merged)


class LastFmClient:
    """Rate-limited, keep-alive Last.fm API client with artist caching."""

    def __init__(
        self,
        api_key: str,
        *,
        min_interval_sec: float = 0.22,
        max_retries: int = 4,
        timeout: float = 15.0,
        artist_cache: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> None:
        self.api_key = api_key
        self.min_interval_sec = min_interval_sec
        self.max_retries = max_retries
        self.timeout = timeout
        self.artist_cache: Dict[str, Dict[str, str]] = (
            dict(artist_cache) if artist_cache is not None else {}
        )
        self._last_call = 0.0
        self._conn: Optional[http.client.HTTPSConnection] = None

    def __enter__(self) -> "LastFmClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    def _get_conn(self) -> http.client.HTTPSConnection:
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(LASTFM_HOST, timeout=self.timeout)
        return self._conn

    def _reset_conn(self) -> None:
        self.close()

    def call(self, method: str, **params: str) -> dict:
        """Execute one Last.fm GET request with pacing, keep-alive reuse, and retries."""
        query = urllib.parse.urlencode(
            {"method": method, "api_key": self.api_key, "format": "json", **params}
        )
        request_target = f"{LASTFM_PATH}?{query}"
        headers = {"User-Agent": USER_AGENT, "Connection": "keep-alive"}

        last_reason = "unknown error"
        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.min_interval_sec:
                time.sleep(self.min_interval_sec - elapsed)
            self._last_call = time.monotonic()

            try:
                conn = self._get_conn()
                conn.request("GET", request_target, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
            except (http.client.HTTPException, OSError, TimeoutError) as err:
                last_reason = f"{type(err).__name__}: {err}"
                self._reset_conn()
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LastFmTransientError(last_reason) from err

            if resp.status == 429 or resp.status >= 500:
                last_reason = f"HTTP {resp.status}"
                self._reset_conn()
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LastFmTransientError(last_reason)

            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as err:
                last_reason = f"Invalid JSON: {err}"
                self._reset_conn()
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LastFmTransientError(last_reason) from err

            if not isinstance(data, dict):
                return {}

            error_code = data.get("error")
            if error_code is not None:
                try:
                    code_int = int(error_code)
                except (TypeError, ValueError):
                    code_int = -1
                message = str(data.get("message") or f"Last.fm error {error_code}")
                if code_int in FATAL_LFM_ERRORS:
                    raise LastFmFatalError(message)
                if code_int == 6:
                    return {}
                if code_int in TRANSIENT_LFM_ERRORS or resp.status >= 400:
                    last_reason = f"Last.fm error {code_int}: {message}"
                    if attempt < self.max_retries:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise LastFmTransientError(last_reason)
                return {}

            return data

        raise LastFmTransientError(last_reason)

    def get_track_info(self, artist: str, track_name: str) -> dict:
        for candidate in track_candidates(track_name):
            data = self.call("track.getInfo", artist=artist, track=candidate, autocorrect="1")
            track_obj = data.get("track")
            if isinstance(track_obj, dict) and track_obj:
                return track_obj
        return {}

    def get_track_tags(self, artist: str, track_name: str) -> List[Dict[str, object]]:
        for candidate in track_candidates(track_name):
            data = self.call("track.getTopTags", artist=artist, track=candidate, autocorrect="1")
            toptags = data.get("toptags") if isinstance(data.get("toptags"), dict) else {}
            tags = normalize_tag_list(toptags.get("tag"))
            if tags:
                return tags
        return []

    def get_similar_tracks(self, artist: str, track_name: str) -> List[dict]:
        for candidate in track_candidates(track_name):
            data = self.call(
                "track.getSimilar", artist=artist, track=candidate, autocorrect="1", limit="5"
            )
            sim = data.get("similartracks") if isinstance(data.get("similartracks"), dict) else {}
            raw = sim.get("track")
            if isinstance(raw, dict):
                raw = [raw]
            if isinstance(raw, list) and raw:
                return [item for item in raw if isinstance(item, dict) and item.get("name")]
        return []

    def get_artist_info(self, artist: str) -> dict:
        data = self.call("artist.getInfo", artist=artist, autocorrect="1")
        artist_obj = data.get("artist")
        return artist_obj if isinstance(artist_obj, dict) else {}

    def get_artist_tags(self, artist: str) -> List[Dict[str, object]]:
        data = self.call("artist.getTopTags", artist=artist, autocorrect="1")
        toptags = data.get("toptags") if isinstance(data.get("toptags"), dict) else {}
        return normalize_tag_list(toptags.get("tag"))

    def get_artist_fields(self, primary_artist: str) -> Tuple[Dict[str, str], bool]:
        """Fetch or reuse cached artist enrichment fields. Returns (fields, cache_hit)."""
        empty_fields = {col: "" for col in ARTIST_COLUMNS}
        empty_fields["artist_tag_count"] = "0"
        if not primary_artist:
            return empty_fields, True

        cache_key = primary_artist.casefold()
        if cache_key in self.artist_cache:
            return dict(self.artist_cache[cache_key]), True

        a_info = self.get_artist_info(primary_artist)
        a_tags = self.get_artist_tags(primary_artist)

        stats = a_info.get("stats") if isinstance(a_info.get("stats"), dict) else {}
        bio = a_info.get("bio") if isinstance(a_info.get("bio"), dict) else {}

        sim_container = a_info.get("similar") if isinstance(a_info.get("similar"), dict) else {}
        sim_raw = sim_container.get("artist")
        if isinstance(sim_raw, dict):
            sim_raw = [sim_raw]
        similar_artists = " | ".join(
            str(item.get("name") or "").strip()
            for item in (sim_raw if isinstance(sim_raw, list) else [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        )

        tags_container = a_info.get("tags") if isinstance(a_info.get("tags"), dict) else {}
        artist_info_tags = normalize_tag_list(tags_container.get("tag"))
        artist_tags_info = " | ".join(str(t["name"]).strip() for t in artist_info_tags)

        a_tags_all, a_tags_names = format_tags(a_tags)

        fields = {
            "lfm_artist_name": str(a_info.get("name") or "").strip(),
            "lfm_artist_mbid": str(a_info.get("mbid") or "").strip(),
            "lfm_artist_url": str(a_info.get("url") or "").strip(),
            "artist_ontour": str(a_info.get("ontour") or "").strip(),
            "artist_listeners": str(stats.get("listeners") or "").strip(),
            "artist_playcount": str(stats.get("playcount") or "").strip(),
            "artist_image_url": pick_image(a_info.get("image")),
            "similar_artists": similar_artists,
            "artist_tags_info": artist_tags_info,
            "artist_bio_published": str(bio.get("published") or "").strip(),
            "artist_bio_summary": clean_text(bio.get("summary")),
            "artist_bio_content": clean_text(bio.get("content")),
            "artist_tag_count": str(len(a_tags)),
            "artist_tags_all": a_tags_all,
            "artist_tags_names": a_tags_names,
        }
        self.artist_cache[cache_key] = dict(fields)
        return fields, False

    def enrich_row(self, master_row: Dict[str, str]) -> Tuple[Dict[str, str], bool]:
        """Fetch all track and artist Last.fm metadata for one master.csv row."""
        artist_names = (master_row.get("artist_names") or "").strip()
        track_name = (master_row.get("track_name") or "").strip()
        primary_artist = extract_primary_artist(artist_names)

        t_info: dict = {}
        t_tags: List[Dict[str, object]] = []
        t_similar: List[dict] = []
        if primary_artist and track_name:
            t_info = self.get_track_info(primary_artist, track_name)
            t_tags = self.get_track_tags(primary_artist, track_name)
            t_similar = self.get_similar_tracks(primary_artist, track_name)

        artist_fields, artist_cache_hit = self.get_artist_fields(primary_artist)

        streamable_raw = t_info.get("streamable")
        if isinstance(streamable_raw, dict):
            track_streamable = str(streamable_raw.get("#text") or "").strip()
            track_fulltrack = str(streamable_raw.get("fulltrack") or "").strip()
        else:
            track_streamable = str(streamable_raw or "").strip()
            track_fulltrack = ""

        album = t_info.get("album") if isinstance(t_info.get("album"), dict) else {}
        wiki = t_info.get("wiki") if isinstance(t_info.get("wiki"), dict) else {}

        info_toptags = t_info.get("toptags") if isinstance(t_info.get("toptags"), dict) else {}
        track_toptags_info = " | ".join(
            str(t["name"]).strip() for t in normalize_tag_list(info_toptags.get("tag"))
        )

        t_tags_all, t_tags_names = format_tags(t_tags)

        sim_tracks_formatted: List[str] = []
        for sim in t_similar:
            sim_artist = (
                str(sim.get("artist", {}).get("name") or "").strip()
                if isinstance(sim.get("artist"), dict)
                else str(sim.get("artist") or "").strip()
            )
            sim_name = str(sim.get("name") or "").strip()
            sim_match = str(sim.get("match") or "").strip()
            label = f"{sim_artist} - {sim_name}" if sim_artist else sim_name
            if sim_match:
                label = f"{label} ({sim_match})"
            if label:
                sim_tracks_formatted.append(label)

        track_tag_list = [str(t["name"]).strip() for t in t_tags if str(t.get("name") or "").strip()]
        artist_tag_list = [
            tag.strip() for tag in artist_fields["artist_tags_names"].split("|") if tag.strip()
        ]
        genre_source, genres_all = merge_genre_tags(track_tag_list, artist_tag_list)

        enriched: Dict[str, str] = {col: (master_row.get(col) or "").strip() for col in MASTER_FIELDNAMES}
        enriched.update(
            {
                "primary_artist": primary_artist,
                "genre_source": genre_source,
                "genres_all": genres_all,
                "lfm_track_name": str(t_info.get("name") or "").strip(),
                "lfm_track_mbid": str(t_info.get("mbid") or "").strip(),
                "lfm_track_url": str(t_info.get("url") or "").strip(),
                "duration_ms": str(t_info.get("duration") or "").strip(),
                "track_streamable": track_streamable,
                "track_fulltrack": track_fulltrack,
                "track_listeners": str(t_info.get("listeners") or "").strip(),
                "track_playcount": str(t_info.get("playcount") or "").strip(),
                "album_title": str(album.get("title") or "").strip(),
                "album_artist": str(album.get("artist") or "").strip(),
                "album_mbid": str(album.get("mbid") or "").strip(),
                "album_url": str(album.get("url") or "").strip(),
                "album_image_url": pick_image(album.get("image")),
                "track_toptags_info": track_toptags_info,
                "track_wiki_published": str(wiki.get("published") or "").strip(),
                "track_wiki_summary": clean_text(wiki.get("summary")),
                "track_wiki_content": clean_text(wiki.get("content")),
                "track_tag_count": str(len(t_tags)),
                "track_tags_all": t_tags_all,
                "track_tags_names": t_tags_names,
                "similar_tracks": " | ".join(sim_tracks_formatted),
                **artist_fields,
            }
        )
        return enriched, artist_cache_hit


def load_existing_genre_master(
    path: Path,
) -> Tuple[Set[str], Set[Tuple[str, str]], Dict[str, Dict[str, str]], int]:
    """Validate and load completed track keys and artist cache from master_genre.csv."""
    completed_uris: Set[str] = set()
    completed_names: Set[Tuple[str, str]] = set()
    artist_cache: Dict[str, Dict[str, str]] = {}

    if not path.exists() or not path.stat().st_size:
        return completed_uris, completed_names, artist_cache, 0

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        fields = reader.fieldnames or []
        if fields != GENRE_FIELDNAMES:
            raise ValueError(
                f"{path.name}: unexpected columns; expected {', '.join(GENRE_FIELDNAMES)}"
            )
        row_count = 0
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path.name}: malformed row near line {reader.line_num}")
            row_count += 1
            uri = (row.get("uri") or "").strip()
            artist = (row.get("artist_names") or "").strip()
            track = (row.get("track_name") or "").strip()
            if uri:
                completed_uris.add(uri)
            if artist and track:
                completed_names.add(name_key(artist, track))

            primary = (row.get("primary_artist") or "").strip()
            if primary and (
                (row.get("lfm_artist_name") or "").strip()
                or (row.get("artist_tags_names") or "").strip()
            ):
                cache_key = primary.casefold()
                if cache_key not in artist_cache:
                    artist_cache[cache_key] = {
                        col: (row.get(col) or "").strip() for col in ARTIST_COLUMNS
                    }

    return completed_uris, completed_names, artist_cache, row_count


def append_genre_row(output_path: Path, row: Dict[str, str]) -> None:
    """Append one enriched row to master_genre.csv and fsync immediately."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not output_path.exists() or output_path.stat().st_size == 0

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=GENRE_FIELDNAMES)
    if write_header:
        writer.writeheader()
    writer.writerow({col: row.get(col, "") for col in GENRE_FIELDNAMES})
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


def is_completed_track(
    row: Dict[str, str],
    completed_uris: Set[str],
    completed_names: Set[Tuple[str, str]],
) -> bool:
    uri = (row.get("uri") or "").strip()
    artist = (row.get("artist_names") or "").strip()
    track = (row.get("track_name") or "").strip()
    if uri and uri in completed_uris:
        return True
    if artist and track and name_key(artist, track) in completed_names:
        return True
    return False


def build_master_genre(
    master_path: Path = DEFAULT_MASTER_CSV,
    output_path: Path = DEFAULT_OUTPUT_CSV,
    env_path: Path = DEFAULT_ENV_PATH,
    *,
    limit: Optional[int] = None,
    min_interval_sec: float = 0.22,
    client: Optional[LastFmClient] = None,
) -> Dict[str, int]:
    """Enrich tracks from master.csv into master_genre.csv with resume support."""
    master_path = Path(master_path).resolve()
    output_path = Path(output_path).resolve()

    if not master_path.exists():
        raise FileNotFoundError(f"Master CSV '{master_path}' does not exist.")

    master_rows = read_chart_rows(master_path, master=True)
    completed_uris, completed_names, seeded_artists, existing_count = (
        load_existing_genre_master(output_path)
    )

    scoped_rows = master_rows[:limit] if limit is not None else master_rows
    pending_rows = [
        row
        for row in scoped_rows
        if not is_completed_track(row, completed_uris, completed_names)
    ]

    console.print("=" * 68)
    console.print("[bold green]Spotify Charts Last.fm Genre & Metadata Enricher[/bold green]")
    console.print(f"[*] Source Master CSV:   {master_path} ({len(master_rows):,} tracks)")
    console.print(f"[*] Output Genre CSV:    {output_path}")
    console.print(f"[*] Already Enriched:    [green]{existing_count:,}[/green] tracks")
    console.print(f"[*] Seeded Artist Cache: [cyan]{len(seeded_artists):,}[/cyan] artists")
    if limit is not None:
        console.print(f"[*] Scope Limit:         first [cyan]{limit:,}[/cyan] master rows")
    console.print(f"[*] Pending This Run:    [yellow]{len(pending_rows):,}[/yellow] tracks")
    console.print("=" * 68)

    if not pending_rows:
        console.print("\n[bold green]✓ master_genre.csv is already up to date![/bold green]")
        return {
            "master_total": len(master_rows),
            "existing_count": existing_count,
            "pending_count": 0,
            "enriched_now": 0,
            "transient_failures": 0,
            "artist_cache_hits": 0,
            "interrupted": 0,
            "total_saved": existing_count,
        }

    owns_client = client is None
    if client is None:
        api_key = load_api_key(env_path)
        active_client = LastFmClient(
            api_key,
            min_interval_sec=min_interval_sec,
            artist_cache=seeded_artists,
        )
    else:
        for key, value in seeded_artists.items():
            client.artist_cache.setdefault(key, value)
        active_client = client

    enriched_now = 0
    transient_failures = 0
    artist_cache_hits = 0
    interrupted = False

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("Enriching tracks", total=len(pending_rows))
            for row in pending_rows:
                artist = (row.get("artist_names") or "").strip()
                track = (row.get("track_name") or "").strip()
                progress.update(task_id, description=f"{artist} — {track}"[:60])

                try:
                    enriched_row, cache_hit = active_client.enrich_row(row)
                except LastFmTransientError as err:
                    transient_failures += 1
                    console.print(
                        f"[yellow]Warning: transient Last.fm failure for "
                        f"{artist} - {track} ({err}); skipping so next run retries.[/yellow]"
                    )
                    progress.update(task_id, advance=1)
                    continue

                append_genre_row(output_path, enriched_row)
                uri = (row.get("uri") or "").strip()
                if uri:
                    completed_uris.add(uri)
                if artist and track:
                    completed_names.add(name_key(artist, track))
                if cache_hit:
                    artist_cache_hits += 1
                enriched_now += 1
                progress.update(task_id, advance=1)
    except KeyboardInterrupt:
        interrupted = True
        console.print(
            "\n[yellow]Interrupted (Ctrl+C). Progress saved through the last completed track.[/yellow]"
        )
    finally:
        if owns_client:
            active_client.close()

    total_saved = existing_count + enriched_now
    summary = Table(title="Last.fm Genre Enrichment Summary", border_style="cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")
    summary.add_row("Tracks in master.csv", f"{len(master_rows):,}")
    summary.add_row("Previously Saved in master_genre.csv", f"{existing_count:,}")
    summary.add_row("Enriched in This Run", f"[bold green]{enriched_now:,}[/bold green]")
    summary.add_row("Artist Cache Hits", f"{artist_cache_hits:,}")
    if transient_failures:
        summary.add_row("Transient Failures (Retryable)", f"[yellow]{transient_failures:,}[/yellow]")
    summary.add_row("Total Saved in master_genre.csv", f"[bold cyan]{total_saved:,}[/bold cyan]")

    console.print()
    console.print(summary)

    return {
        "master_total": len(master_rows),
        "existing_count": existing_count,
        "pending_count": len(pending_rows),
        "enriched_now": enriched_now,
        "transient_failures": transient_failures,
        "artist_cache_hits": artist_cache_hits,
        "interrupted": 1 if interrupted else 0,
        "total_saved": total_saved,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Incrementally enrich data/master.csv with Last.fm track & artist metadata into data/master_genre.csv."
    )
    parser.add_argument(
        "--master",
        type=Path,
        default=DEFAULT_MASTER_CSV,
        help=f"Input master CSV path (default: {DEFAULT_MASTER_CSV})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_CSV,
        help=f"Output enriched CSV path (default: {DEFAULT_OUTPUT_CSV})",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help=f"Path to .env containing LASTFM_API_KEY (default: {DEFAULT_ENV_PATH})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on the first N rows of master.csv to process",
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
        result = build_master_genre(
            master_path=args.master,
            output_path=args.output,
            env_path=args.env,
            limit=args.limit,
            min_interval_sec=args.min_interval,
        )
    except LastFmFatalError as err:
        console.print(f"[bold red]Fatal Last.fm API error: {err}[/bold red]")
        sys.exit(1)
    if result["interrupted"]:
        sys.exit(130)
