"""Shared append-only multi-CSV catalog store and deduplication engine for YouTube Music.

Used by both `collect_ytmusic_search.py` (Location x Genre x Era matrix search)
and `collect_ytmusic_moods.py` (`get_mood_categories` browse harvester) so that:
- All 8 CSV tables in `data/ytmusic/` share an identical, Ctrl+C-safe append-only format.
- Playlists (`RDCLAK5uy_...`), albums (`MPREb_...`), and tracks (`video_id`) already
  recorded in `playlists.csv`, `albums.csv`, and `tracks.csv` are loaded on startup
  and never re-fetched across queries, restarts, or scripts.
- Related playlists and albums returned inside `get_playlist(..., related=True)` and
  `get_album(...)` responses are saved to `related_candidates.csv` for future reference
  without being recursively scraped.
"""

from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_DIR / "data/ytmusic"
DEFAULT_STATE_DIR = PROJECT_DIR / "state/ytmusic"

SEARCH_HITS_FIELDS = [
    "recorded_at",
    "location",
    "genre",
    "era",
    "search_filter",
    "query",
    "search_rank",
    "item_type",
    "item_id",
    "item_title",
    "item_artists",
    "item_year",
    "is_official_editorial",
    "raw_json",
]

MOOD_HITS_FIELDS = [
    "recorded_at",
    "location",
    "section",
    "category_title",
    "category_params",
    "shelf_title",
    "era",
    "shelf_rank",
    "item_type",
    "item_id",
    "item_title",
    "item_subtitle",
    "is_official_editorial",
    "raw_json",
]

PLAYLISTS_FIELDS = [
    "playlist_id",
    "title",
    "description",
    "author_name",
    "author_id",
    "year",
    "track_count_reported",
    "track_count_fetched",
    "duration",
    "duration_seconds",
    "views",
    "privacy",
    "first_fetched_via",
    "fetched_at",
    "raw_json",
]

PLAYLIST_TRACKS_FIELDS = [
    "playlist_id",
    "track_rank",
    "video_id",
    "title",
    "artists",
    "artist_ids",
    "album",
    "album_id",
    "duration",
    "duration_seconds",
    "video_type",
    "is_explicit",
    "is_available",
]

ALBUMS_FIELDS = [
    "album_id",
    "audio_playlist_id",
    "title",
    "album_type",
    "year",
    "artists",
    "artist_ids",
    "track_count_reported",
    "track_count_fetched",
    "duration",
    "duration_seconds",
    "is_explicit",
    "description",
    "first_fetched_via",
    "fetched_at",
    "raw_json",
]

ALBUM_TRACKS_FIELDS = [
    "album_id",
    "album_title",
    "album_year",
    "track_position",
    "track_number",
    "video_id",
    "title",
    "artists",
    "artist_ids",
    "duration",
    "duration_seconds",
    "video_type",
    "is_explicit",
    "is_available",
    "views",
    "credits_browse_id",
]

TRACKS_FIELDS = [
    "video_id",
    "url",
    "ytm_url",
    "title",
    "artists",
    "artist_ids",
    "artists_json",
    "album",
    "album_id",
    "duration",
    "duration_seconds",
    "video_type",
    "is_explicit",
    "is_available",
    "year",
    "views",
    "track_number",
    "credits_browse_id",
    "set_video_id",
    "like_status",
    "in_library",
    "pinned_to_listen_again",
    "feedback_tokens_json",
    "listen_again_feedback_tokens_json",
    "community_vote_status_json",
    "thumbnails_json",
    "first_source_type",
    "first_source_id",
    "first_seen_at",
    "raw_json",
]

RELATED_CANDIDATES_FIELDS = [
    "recorded_at",
    "parent_type",
    "parent_id",
    "parent_title",
    "relation_kind",
    "related_type",
    "related_id",
    "related_title",
    "related_artists_or_author",
    "related_year",
    "related_is_explicit",
    "is_official_editorial",
    "raw_json",
]


class TimeoutSession(requests.Session):
    """Requests session enforcing a default 30-second HTTP timeout."""

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        return super().request(*args, **kwargs)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def to_json_str(value: Any) -> str:
    """Serialize any value compactly to JSON, or return empty string if None."""
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_playlist_id(playlist_id: Optional[str]) -> str:
    """Normalize a YTM playlist/browse ID by stripping a leading 'VL' prefix."""
    raw = str(playlist_id or "").strip()
    if raw.startswith("VL"):
        return raw[2:]
    return raw


def is_official_featured_playlist(playlist_id: Optional[str]) -> bool:
    """Return True if the playlist ID belongs to an official YTM editorial playlist (RDCLAK5uy_)."""
    return normalize_playlist_id(playlist_id).startswith("RDCLAK5uy_")


def normalize_album_id(album_id: Optional[str]) -> str:
    """Strip whitespace and return the album browseId."""
    return str(album_id or "").strip()


def is_valid_album_id(album_id: Optional[str]) -> bool:
    """Return True if the album ID has the standard MPREb_ prefix."""
    return normalize_album_id(album_id).startswith("MPRE")


def parse_duration_seconds(duration_seconds: Any, duration_text: Any = None) -> str:
    """Return duration in integer seconds as a string, parsing MM:SS or HH:MM:SS if needed."""
    if duration_seconds is not None and str(duration_seconds).strip() != "":
        try:
            return str(int(float(str(duration_seconds).strip())))
        except (TypeError, ValueError):
            pass
    text = str(duration_text or "").strip()
    if not text or ":" not in text:
        return ""
    parts = text.split(":")
    try:
        nums = [int(p.strip()) for p in parts]
    except ValueError:
        return ""
    if len(nums) == 2:
        return str(nums[0] * 60 + nums[1])
    if len(nums) == 3:
        return str(nums[0] * 3600 + nums[1] * 60 + nums[2])
    return ""


def extract_artists(obj: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Optional[str]]]]:
    """Extract ('; '-joined artist names, '; '-joined artist IDs, normalized list) from a YTM item."""
    raw_artists = obj.get("artists")
    normalized: List[Dict[str, Optional[str]]] = []
    if isinstance(raw_artists, list):
        for item in raw_artists:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                aid = str(item.get("id") or "").strip() or None
                if name or aid:
                    normalized.append({"name": name, "id": aid})
            elif isinstance(item, str) and item.strip():
                normalized.append({"name": item.strip(), "id": None})
    elif isinstance(raw_artists, str) and raw_artists.strip():
        normalized.append({"name": raw_artists.strip(), "id": None})

    if not normalized:
        single_artist = obj.get("artist")
        if isinstance(single_artist, str) and single_artist.strip():
            normalized.append({"name": single_artist.strip(), "id": None})
        elif isinstance(single_artist, dict):
            name = str(single_artist.get("name") or "").strip()
            aid = str(single_artist.get("id") or "").strip() or None
            if name or aid:
                normalized.append({"name": name, "id": aid})

    names = "; ".join(a["name"] for a in normalized if a.get("name"))
    ids = "; ".join((a["id"] or "") for a in normalized) if any(a.get("id") for a in normalized) else ""
    return names, ids, normalized


def extract_track_album(
    track: Dict[str, Any],
    fallback_title: str = "",
    fallback_id: str = "",
) -> Tuple[str, str]:
    """Return (album_title, album_id) from a track dict (handles both dict and plain string album)."""
    raw_album = track.get("album")
    if isinstance(raw_album, dict):
        title = str(raw_album.get("name") or "").strip() or fallback_title
        aid = str(raw_album.get("id") or "").strip() or fallback_id
        return title, aid
    if isinstance(raw_album, str):
        return raw_album.strip() or fallback_title, fallback_id
    return fallback_title, fallback_id


def extract_author(playlist: Dict[str, Any]) -> Tuple[str, str]:
    """Return (author_name, author_id) from a playlist dict."""
    author = playlist.get("author")
    if isinstance(author, dict):
        return str(author.get("name") or "").strip(), str(author.get("id") or "").strip()
    if isinstance(author, list):
        names = [str(a.get("name") or "").strip() for a in author if isinstance(a, dict) and a.get("name")]
        ids = [str(a.get("id") or "").strip() for a in author if isinstance(a, dict) and a.get("id")]
        return "; ".join(names), "; ".join(ids)
    if isinstance(author, str):
        return author.strip(), ""
    return "", ""


def call_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = 2,
    base_backoff: float = 1.5,
    **kwargs: Any,
) -> Any:
    """Invoke a YTMusic method with exponential backoff on transient failures."""
    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as err:  # noqa: BLE001
            last_err = err
            if attempt < max_retries:
                time.sleep(base_backoff * (attempt + 1))
    raise last_err  # type: ignore[misc]


def _load_csv_column_set(csv_path: Path, column_name: str, normalizer: Optional[Callable[[str], str]] = None) -> Set[str]:
    """Read an existing CSV file and return the set of non-empty values in `column_name`."""
    values: Set[str] = set()
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return values
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            val = str(row.get(column_name) or "").strip()
            if val:
                values.add(normalizer(val) if normalizer else val)
    return values


def _load_ledger_lines(path: Path) -> Set[str]:
    """Read a line-delimited state ledger into a set of stripped non-empty lines."""
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


class _CsvAppender:
    """Line-flushed, fsynced append-only CSV writer that auto-writes headers on new files."""

    def __init__(self, path: Path, fieldnames: Sequence[str]) -> None:
        self.path = path
        self.fieldnames = list(fieldnames)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not self.path.exists() or self.path.stat().st_size == 0
        self._handle = self.path.open("a", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._handle, fieldnames=self.fieldnames)
        if needs_header:
            self._writer.writeheader()
            self.flush()

    def write_row(self, row: Dict[str, Any]) -> None:
        clean = {k: ("" if row.get(k) is None else str(row.get(k))) for k in self.fieldnames}
        self._writer.writerow(clean)

    def write_rows(self, rows: Iterable[Dict[str, Any]]) -> int:
        count = 0
        for row in rows:
            self.write_row(row)
            count += 1
        if count:
            self.flush()
        return count

    def flush(self) -> None:
        self._handle.flush()
        try:
            os.fsync(self._handle.fileno())
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._handle.close()


class CatalogStore:
    """Shared multi-CSV append-only catalog and state ledger manager."""

    def __init__(
        self,
        data_dir: Path = DEFAULT_DATA_DIR,
        state_dir: Path = DEFAULT_STATE_DIR,
        *,
        delay: float = 0.4,
        max_retries: int = 2,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.state_dir = Path(state_dir)
        self.delay = max(0.0, float(delay))
        self.max_retries = max(0, int(max_retries))

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.search_hits_path = self.data_dir / "search_hits.csv"
        self.mood_hits_path = self.data_dir / "mood_category_hits.csv"
        self.playlists_path = self.data_dir / "playlists.csv"
        self.playlist_tracks_path = self.data_dir / "playlist_tracks.csv"
        self.albums_path = self.data_dir / "albums.csv"
        self.album_tracks_path = self.data_dir / "album_tracks.csv"
        self.tracks_path = self.data_dir / "tracks.csv"
        self.related_candidates_path = self.data_dir / "related_candidates.csv"

        self.completed_queries_path = self.state_dir / "completed_queries.txt"
        self.completed_moods_path = self.state_dir / "completed_moods.txt"
        self.completed_playlists_path = self.state_dir / "completed_playlists.txt"
        self.completed_albums_path = self.state_dir / "completed_albums.txt"
        self.failed_items_path = self.state_dir / "failed_items.txt"

        # Seed in-memory deduplication sets from existing CSVs + state ledgers
        self.seen_playlists: Set[str] = (
            _load_csv_column_set(self.playlists_path, "playlist_id", normalize_playlist_id)
            | {normalize_playlist_id(x) for x in _load_ledger_lines(self.completed_playlists_path)}
        )
        self.seen_albums: Set[str] = (
            _load_csv_column_set(self.albums_path, "album_id", normalize_album_id)
            | {normalize_album_id(x) for x in _load_ledger_lines(self.completed_albums_path)}
        )
        self.seen_tracks: Set[str] = _load_csv_column_set(self.tracks_path, "video_id")
        self.completed_queries: Set[str] = _load_ledger_lines(self.completed_queries_path)
        self.completed_moods: Set[str] = _load_ledger_lines(self.completed_moods_path)

        # Open CSV appenders
        self._search_hits_csv = _CsvAppender(self.search_hits_path, SEARCH_HITS_FIELDS)
        self._mood_hits_csv = _CsvAppender(self.mood_hits_path, MOOD_HITS_FIELDS)
        self._playlists_csv = _CsvAppender(self.playlists_path, PLAYLISTS_FIELDS)
        self._playlist_tracks_csv = _CsvAppender(self.playlist_tracks_path, PLAYLIST_TRACKS_FIELDS)
        self._albums_csv = _CsvAppender(self.albums_path, ALBUMS_FIELDS)
        self._album_tracks_csv = _CsvAppender(self.album_tracks_path, ALBUM_TRACKS_FIELDS)
        self._tracks_csv = _CsvAppender(self.tracks_path, TRACKS_FIELDS)
        self._related_csv = _CsvAppender(self.related_candidates_path, RELATED_CANDIDATES_FIELDS)

        self._last_api_call = 0.0

    def __enter__(self) -> "CatalogStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        for appender in (
            self._search_hits_csv,
            self._mood_hits_csv,
            self._playlists_csv,
            self._playlist_tracks_csv,
            self._albums_csv,
            self._album_tracks_csv,
            self._tracks_csv,
            self._related_csv,
        ):
            appender.close()

    def pace(self) -> None:
        """Enforce minimum delay between live YouTube Music API calls."""
        if self.delay <= 0:
            return
        elapsed = time.monotonic() - self._last_api_call
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_api_call = time.monotonic()

    @staticmethod
    def _append_ledger_line(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line.strip() + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass

    def mark_query_completed(self, step_key: str) -> None:
        if step_key not in self.completed_queries:
            self.completed_queries.add(step_key)
            self._append_ledger_line(self.completed_queries_path, step_key)

    def mark_mood_completed(self, mood_key: str) -> None:
        if mood_key not in self.completed_moods:
            self.completed_moods.add(mood_key)
            self._append_ledger_line(self.completed_moods_path, mood_key)

    def record_failure(self, item_type: str, item_id: str, error: Exception) -> None:
        stamp = utc_now_iso()
        msg = f"{stamp}\t{item_type}\t{item_id}\t{type(error).__name__}: {error}"
        self._append_ledger_line(self.failed_items_path, msg)

    def record_search_hit(
        self,
        *,
        location: str,
        genre: str,
        era: str,
        search_filter: str,
        query: str,
        search_rank: int,
        item_type: str,
        item_id: str,
        item_title: str,
        item_artists: str,
        item_year: str,
        is_official_editorial: bool,
        raw_obj: Dict[str, Any],
    ) -> None:
        self._search_hits_csv.write_row(
            {
                "recorded_at": utc_now_iso(),
                "location": location,
                "genre": genre,
                "era": era,
                "search_filter": search_filter,
                "query": query,
                "search_rank": search_rank,
                "item_type": item_type,
                "item_id": item_id,
                "item_title": item_title,
                "item_artists": item_artists,
                "item_year": item_year,
                "is_official_editorial": is_official_editorial,
                "raw_json": to_json_str(raw_obj),
            }
        )
        self._search_hits_csv.flush()

    def record_mood_hit(
        self,
        *,
        location: str,
        section: str,
        category_title: str,
        category_params: str,
        shelf_title: str,
        era: str,
        shelf_rank: int,
        item_type: str,
        item_id: str,
        item_title: str,
        item_subtitle: str,
        is_official_editorial: bool,
        raw_obj: Dict[str, Any],
    ) -> None:
        self._mood_hits_csv.write_row(
            {
                "recorded_at": utc_now_iso(),
                "location": location,
                "section": section,
                "category_title": category_title,
                "category_params": category_params,
                "shelf_title": shelf_title,
                "era": era,
                "shelf_rank": shelf_rank,
                "item_type": item_type,
                "item_id": item_id,
                "item_title": item_title,
                "item_subtitle": item_subtitle,
                "is_official_editorial": is_official_editorial,
                "raw_json": to_json_str(raw_obj),
            }
        )
        self._mood_hits_csv.flush()

    def record_track_if_new(
        self,
        track: Dict[str, Any],
        *,
        source_type: str,
        source_id: str,
        fallback_album_title: str = "",
        fallback_album_id: str = "",
        fallback_year: str = "",
    ) -> bool:
        """Write a track row to `tracks.csv` iff its `video_id` has not been recorded yet."""
        video_id = str(track.get("videoId") or "").strip()
        if not video_id or video_id in self.seen_tracks:
            return False

        artist_names, artist_ids, artists_norm = extract_artists(track)
        album_title, album_id = extract_track_album(
            track,
            fallback_title=fallback_album_title,
            fallback_id=fallback_album_id,
        )
        dur_text = str(track.get("duration") or track.get("length") or "").strip()
        dur_sec = parse_duration_seconds(track.get("duration_seconds"), dur_text)
        thumbs = track.get("thumbnails") if track.get("thumbnails") is not None else track.get("thumbnail")

        self._tracks_csv.write_row(
            {
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "ytm_url": f"https://music.youtube.com/watch?v={video_id}",
                "title": str(track.get("title") or "").strip(),
                "artists": artist_names,
                "artist_ids": artist_ids,
                "artists_json": to_json_str(artists_norm),
                "album": album_title,
                "album_id": album_id,
                "duration": dur_text,
                "duration_seconds": dur_sec,
                "video_type": str(track.get("videoType") or "").strip(),
                "is_explicit": "" if track.get("isExplicit") is None else str(bool(track.get("isExplicit"))),
                "is_available": "" if track.get("isAvailable") is None else str(bool(track.get("isAvailable"))),
                "year": str(track.get("year") or fallback_year or "").strip(),
                "views": str(track.get("views") or "").strip(),
                "track_number": "" if track.get("trackNumber") is None else str(track.get("trackNumber")),
                "credits_browse_id": str(track.get("creditsBrowseId") or "").strip(),
                "set_video_id": str(track.get("setVideoId") or "").strip(),
                "like_status": str(track.get("likeStatus") or "").strip(),
                "in_library": "" if track.get("inLibrary") is None else str(bool(track.get("inLibrary"))),
                "pinned_to_listen_again": (
                    ""
                    if track.get("pinnedToListenAgain") is None
                    else str(bool(track.get("pinnedToListenAgain")))
                ),
                "feedback_tokens_json": to_json_str(track.get("feedbackTokens")),
                "listen_again_feedback_tokens_json": to_json_str(track.get("listenAgainFeedbackTokens")),
                "community_vote_status_json": to_json_str(track.get("communityVoteStatus")),
                "thumbnails_json": to_json_str(thumbs),
                "first_source_type": source_type,
                "first_source_id": source_id,
                "first_seen_at": utc_now_iso(),
                "raw_json": to_json_str(track),
            }
        )
        self._tracks_csv.flush()
        self.seen_tracks.add(video_id)
        return True

    def process_playlist_if_new(
        self,
        yt: Any,
        raw_playlist_id: str,
        *,
        source_label: str,
    ) -> Tuple[str, int, int]:
        """Fetch and save a playlist at most once.

        Returns `(status, tracks_in_playlist, new_unique_tracks_added)` where `status`
        is one of `'fetched'`, `'skipped_existing'`, or `'failed'`.
        """
        playlist_id = normalize_playlist_id(raw_playlist_id)
        if not playlist_id:
            return "failed", 0, 0
        if playlist_id in self.seen_playlists:
            return "skipped_existing", 0, 0

        self.pace()
        try:
            pl = call_with_retry(
                yt.get_playlist,
                playlist_id,
                limit=None,
                related=True,
                max_retries=self.max_retries,
            )
        except Exception as err:  # noqa: BLE001
            self.record_failure("playlist", playlist_id, err)
            return "failed", 0, 0

        if not isinstance(pl, dict):
            return "failed", 0, 0

        fetched_at = utc_now_iso()
        pl_title = str(pl.get("title") or "").strip()
        author_name, author_id = extract_author(pl)
        tracks = pl.get("tracks") if isinstance(pl.get("tracks"), list) else []

        # Strip heavy `tracks` list from playlists.csv raw_json so tracks aren't duplicated in two huge blobs
        pl_meta = {k: v for k, v in pl.items() if k != "tracks"}
        self._playlists_csv.write_row(
            {
                "playlist_id": playlist_id,
                "title": pl_title,
                "description": str(pl.get("description") or "").strip(),
                "author_name": author_name,
                "author_id": author_id,
                "year": str(pl.get("year") or "").strip(),
                "track_count_reported": "" if pl.get("trackCount") is None else str(pl.get("trackCount")),
                "track_count_fetched": str(len(tracks)),
                "duration": str(pl.get("duration") or "").strip(),
                "duration_seconds": parse_duration_seconds(pl.get("duration_seconds"), pl.get("duration")),
                "views": "" if pl.get("views") is None else str(pl.get("views")),
                "privacy": str(pl.get("privacy") or "").strip(),
                "first_fetched_via": source_label,
                "fetched_at": fetched_at,
                "raw_json": to_json_str(pl_meta),
            }
        )
        self._playlists_csv.flush()

        pt_rows: List[Dict[str, Any]] = []
        new_tracks = 0
        for rank, track in enumerate(tracks, 1):
            if not isinstance(track, dict):
                continue
            vid = str(track.get("videoId") or "").strip()
            artist_names, artist_ids, _ = extract_artists(track)
            album_title, album_id = extract_track_album(track)
            dur_text = str(track.get("duration") or track.get("length") or "").strip()
            dur_sec = parse_duration_seconds(track.get("duration_seconds"), dur_text)
            pt_rows.append(
                {
                    "playlist_id": playlist_id,
                    "track_rank": rank,
                    "video_id": vid,
                    "title": str(track.get("title") or "").strip(),
                    "artists": artist_names,
                    "artist_ids": artist_ids,
                    "album": album_title,
                    "album_id": album_id,
                    "duration": dur_text,
                    "duration_seconds": dur_sec,
                    "video_type": str(track.get("videoType") or "").strip(),
                    "is_explicit": "" if track.get("isExplicit") is None else str(bool(track.get("isExplicit"))),
                    "is_available": "" if track.get("isAvailable") is None else str(bool(track.get("isAvailable"))),
                }
            )
            if vid and self.record_track_if_new(
                track,
                source_type="playlist",
                source_id=playlist_id,
                fallback_year=str(pl.get("year") or "").strip(),
            ):
                new_tracks += 1

        self._playlist_tracks_csv.write_rows(pt_rows)

        # Save related playlists for future consideration WITHOUT diving into them
        related_list = pl.get("related") if isinstance(pl.get("related"), list) else []
        rel_rows: List[Dict[str, Any]] = []
        for rel in related_list:
            if not isinstance(rel, dict):
                continue
            rel_id = normalize_playlist_id(rel.get("playlistId") or rel.get("browseId"))
            if not rel_id:
                continue
            rel_rows.append(
                {
                    "recorded_at": fetched_at,
                    "parent_type": "playlist",
                    "parent_id": playlist_id,
                    "parent_title": pl_title,
                    "relation_kind": "playlist_related",
                    "related_type": "playlist",
                    "related_id": rel_id,
                    "related_title": str(rel.get("title") or "").strip(),
                    "related_artists_or_author": str(rel.get("description") or "").strip(),
                    "related_year": "",
                    "related_is_explicit": "",
                    "is_official_editorial": is_official_featured_playlist(rel_id),
                    "raw_json": to_json_str(rel),
                }
            )
        self._related_csv.write_rows(rel_rows)

        self.seen_playlists.add(playlist_id)
        self._append_ledger_line(self.completed_playlists_path, playlist_id)
        return "fetched", len(pt_rows), new_tracks

    def process_album_if_new(
        self,
        yt: Any,
        raw_album_id: str,
        *,
        source_label: str,
    ) -> Tuple[str, int, int]:
        """Fetch and save an album at most once.

        Returns `(status, tracks_in_album, new_unique_tracks_added)` where `status`
        is one of `'fetched'`, `'skipped_existing'`, or `'failed'`.
        """
        album_id = normalize_album_id(raw_album_id)
        if not is_valid_album_id(album_id):
            return "failed", 0, 0
        if album_id in self.seen_albums:
            return "skipped_existing", 0, 0

        self.pace()
        try:
            alb = call_with_retry(
                yt.get_album,
                album_id,
                max_retries=self.max_retries,
            )
        except Exception as err:  # noqa: BLE001
            self.record_failure("album", album_id, err)
            return "failed", 0, 0

        if not isinstance(alb, dict):
            return "failed", 0, 0

        fetched_at = utc_now_iso()
        alb_title = str(alb.get("title") or "").strip()
        alb_year = str(alb.get("year") or "").strip()
        artist_names, artist_ids, _ = extract_artists(alb)
        tracks = alb.get("tracks") if isinstance(alb.get("tracks"), list) else []

        alb_meta = {k: v for k, v in alb.items() if k != "tracks"}
        self._albums_csv.write_row(
            {
                "album_id": album_id,
                "audio_playlist_id": str(alb.get("audioPlaylistId") or "").strip(),
                "title": alb_title,
                "album_type": str(alb.get("type") or "").strip(),
                "year": alb_year,
                "artists": artist_names,
                "artist_ids": artist_ids,
                "track_count_reported": "" if alb.get("trackCount") is None else str(alb.get("trackCount")),
                "track_count_fetched": str(len(tracks)),
                "duration": str(alb.get("duration") or "").strip(),
                "duration_seconds": parse_duration_seconds(alb.get("duration_seconds"), alb.get("duration")),
                "is_explicit": "" if alb.get("isExplicit") is None else str(bool(alb.get("isExplicit"))),
                "description": str(alb.get("description") or "").strip(),
                "first_fetched_via": source_label,
                "fetched_at": fetched_at,
                "raw_json": to_json_str(alb_meta),
            }
        )
        self._albums_csv.flush()

        at_rows: List[Dict[str, Any]] = []
        new_tracks = 0
        for pos, track in enumerate(tracks, 1):
            if not isinstance(track, dict):
                continue
            vid = str(track.get("videoId") or "").strip()
            t_artists, t_artist_ids, _ = extract_artists(track)
            if not t_artists:
                t_artists, t_artist_ids = artist_names, artist_ids
            dur_text = str(track.get("duration") or track.get("length") or "").strip()
            dur_sec = parse_duration_seconds(track.get("duration_seconds"), dur_text)
            at_rows.append(
                {
                    "album_id": album_id,
                    "album_title": alb_title,
                    "album_year": alb_year,
                    "track_position": pos,
                    "track_number": "" if track.get("trackNumber") is None else str(track.get("trackNumber")),
                    "video_id": vid,
                    "title": str(track.get("title") or "").strip(),
                    "artists": t_artists,
                    "artist_ids": t_artist_ids,
                    "duration": dur_text,
                    "duration_seconds": dur_sec,
                    "video_type": str(track.get("videoType") or "").strip(),
                    "is_explicit": "" if track.get("isExplicit") is None else str(bool(track.get("isExplicit"))),
                    "is_available": "" if track.get("isAvailable") is None else str(bool(track.get("isAvailable"))),
                    "views": str(track.get("views") or "").strip(),
                    "credits_browse_id": str(track.get("creditsBrowseId") or "").strip(),
                }
            )
            if vid and self.record_track_if_new(
                track,
                source_type="album",
                source_id=album_id,
                fallback_album_title=alb_title,
                fallback_album_id=album_id,
                fallback_year=alb_year,
            ):
                new_tracks += 1

        self._album_tracks_csv.write_rows(at_rows)

        # Save related_recommendations & other_versions albums to related_candidates.csv without diving in
        rel_rows: List[Dict[str, Any]] = []
        for rel_key, rel_kind in (
            ("related_recommendations", "album_related_recommendations"),
            ("other_versions", "album_other_versions"),
        ):
            items = alb.get(rel_key) if isinstance(alb.get(rel_key), list) else []
            for rel in items:
                if not isinstance(rel, dict):
                    continue
                rel_id = normalize_album_id(rel.get("browseId"))
                if not rel_id:
                    continue
                r_artists, _, _ = extract_artists(rel)
                rel_rows.append(
                    {
                        "recorded_at": fetched_at,
                        "parent_type": "album",
                        "parent_id": album_id,
                        "parent_title": alb_title,
                        "relation_kind": rel_kind,
                        "related_type": "album",
                        "related_id": rel_id,
                        "related_title": str(rel.get("title") or "").strip(),
                        "related_artists_or_author": r_artists,
                        "related_year": str(rel.get("year") or "").strip(),
                        "related_is_explicit": (
                            "" if rel.get("isExplicit") is None else str(bool(rel.get("isExplicit")))
                        ),
                        "is_official_editorial": False,
                        "raw_json": to_json_str(rel),
                    }
                )
        self._related_csv.write_rows(rel_rows)

        self.seen_albums.add(album_id)
        self._append_ledger_line(self.completed_albums_path, album_id)
        return "fetched", len(at_rows), new_tracks
