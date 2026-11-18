"""Offline regression tests for the Last.fm track-tagged snowball sampler."""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
for _folder in ("scraper", "pipeline"):
    _path = str(PROJECT_DIR / _folder)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from rich.console import Console

import build_master_genre as enricher
import snowball_lastfm as snowball


class FakeSnowballClient(snowball.SnowballLastFmClient):
    """Deterministic offline Last.fm stub for snowball sampler tests."""

    def __init__(
        self,
        *,
        track_tags: dict[tuple[str, str], list[dict]] | None = None,
        track_similar: dict[tuple[str, str], list[dict]] | None = None,
        artist_top_tracks: dict[str, dict[int, list[dict]]] | None = None,
        canonical_track_names: dict[tuple[str, str], str] | None = None,
        fail_once_track: str | None = None,
    ) -> None:
        super().__init__("test-key", min_interval_sec=0.0)
        self.track_tags = track_tags or {}
        self.track_similar = track_similar or {}
        self.artist_top_tracks = artist_top_tracks or {}
        self.canonical_track_names = canonical_track_names or {}
        self.fail_once_track = fail_once_track
        self._failed_once = False
        self.calls: list[tuple[str, str, str, str]] = []

    def call(self, method: str, **params: str) -> dict:
        artist = params.get("artist", "")
        track = params.get("track", "")
        page = params.get("page", "")
        self.calls.append((method, artist, track, page))

        if (
            self.fail_once_track
            and method == "track.getTopTags"
            and track == self.fail_once_track
            and not self._failed_once
        ):
            self._failed_once = True
            raise enricher.LastFmTransientError("simulated timeout")

        if method == "track.getTopTags":
            tags = self.track_tags.get((artist.casefold(), track.casefold()), [])
            return {"toptags": {"tag": tags}} if tags else {"toptags": {"tag": []}}

        if method == "track.getInfo":
            canon_track = self.canonical_track_names.get(
                (artist.casefold(), track.casefold()), track
            )
            return {
                "track": {
                    "name": canon_track,
                    "mbid": f"mbid-track-{canon_track}",
                    "duration": "210000",
                    "listeners": "500000",
                    "playcount": "4000000",
                    "artist": {"name": artist},
                    "album": {"title": "Studio Album", "artist": artist},
                }
            }

        if method == "track.getSimilar":
            sims = self.track_similar.get((artist.casefold(), track.casefold()), [])
            return {"similartracks": {"track": sims}}

        if method == "artist.getInfo":
            return {
                "artist": {
                    "name": artist,
                    "mbid": f"mbid-artist-{artist}",
                    "stats": {"listeners": "1200000", "playcount": "50000000"},
                    "similar": {"artist": [{"name": "Peer Artist"}]},
                    "tags": {"tag": [{"name": "pop"}]},
                }
            }

        if method == "artist.getTopTags":
            return {
                "toptags": {
                    "tag": [
                        {"name": "pop", "count": 100},
                        {"name": "indie", "count": 40},
                    ]
                }
            }

        if method == "artist.getTopTracks":
            pages = self.artist_top_tracks.get(artist.casefold(), {})
            page_num = int(page or "1")
            items = pages.get(page_num, [])
            total_pages = str(max(pages.keys()) if pages else 1)
            return {
                "toptracks": {
                    "track": items,
                    "@attr": {"page": str(page_num), "totalPages": total_pages},
                }
            }

        return {}


def make_lastfm_row(
    *,
    track: str,
    artist: str,
    track_tag_count: str = "0",
    track_tags_names: str = "",
    track_tags_all: str = "",
    artist_tag_count: str = "2",
    artist_tags_names: str = "pop | rock",
    artist_tags_all: str = "pop (100) | rock (50)",
    similar_tracks: str = "",
    similar_artists: str = "Other Artist",
) -> dict[str, str]:
    row = {col: "" for col in snowball.LASTFM_FIELDNAMES}
    row.update(
        {
            "lfm_track_name": track,
            "lfm_artist_name": artist,
            "genre_source": "track+artist" if int(track_tag_count) > 0 else "artist",
            "genres_all": track_tags_names or artist_tags_names,
            "track_tag_count": track_tag_count,
            "track_tags_all": track_tags_all,
            "track_tags_names": track_tags_names,
            "artist_tag_count": artist_tag_count,
            "artist_tags_all": artist_tags_all,
            "artist_tags_names": artist_tags_names,
            "track_listeners": "500000",
            "track_playcount": "3000000",
            "artist_listeners": "1000000",
            "artist_playcount": "25000000",
            "similar_tracks": similar_tracks,
            "similar_artists": similar_artists,
        }
    )
    return row


def write_lastfm_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=snowball.LASTFM_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in snowball.LASTFM_FIELDNAMES})


def read_lastfm_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class SnowballLastFmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.csv_path = self.root / "data" / "master_lastfm.csv"
        self.state_path = self.root / "data" / "snowball_state.json"
        self.env_path = self.root / ".env"
        self.env_path.write_text("LASTFM_API_KEY=dummy-key\n", encoding="utf-8")

        console_patch = patch.object(snowball, "console", Console(file=io.StringIO()))
        console_patch.start()
        self.addCleanup(console_patch.stop)

    def test_parse_similar_tracks_cell_handles_scores_and_hyphens(self) -> None:
        cell = (
            "Arctic Monkeys - 505 (0.963855) | "
            "Artist 2 - Track - 2011 Remaster (0.85) | "
            "ARCTIC MONKEYS - 505 (0.50) | "
            "MalformedEntryWithoutDash | "
            "MIIA - Dynasty"
        )
        parsed = snowball.parse_similar_tracks_cell(cell)
        self.assertEqual(
            parsed,
            [
                ("Arctic Monkeys", "505"),
                ("Artist 2", "Track - 2011 Remaster"),
                ("MIIA", "Dynasty"),
            ],
        )

    def test_seed_only_queues_track_tagged_rows_and_skips_untagged_candidates(self) -> None:
        seed_tagged = make_lastfm_row(
            track="Tagged Seed",
            artist="Seed Artist",
            track_tag_count="2",
            track_tags_names="rock | indie",
            track_tags_all="rock (100) | indie (60)",
            similar_tracks="New Artist - Untagged Candidate (0.91)",
        )
        seed_untagged = make_lastfm_row(
            track="Untagged Existing",
            artist="Untagged Artist",
            track_tag_count="0",
            similar_tracks="Ignored Artist - Ignored Song (0.99)",
        )
        write_lastfm_csv(self.csv_path, [seed_tagged, seed_untagged])

        client = FakeSnowballClient(
            track_tags={},  # "Untagged Candidate" has 0 track tags
            artist_top_tracks={
                "seed artist": {
                    1: [
                        {"name": "Tagged Seed", "listeners": "500000"},
                        {"name": "Artist Side B", "listeners": "12000"},
                    ]
                }
            },
        )

        result = snowball.run_snowball(
            csv_path=self.csv_path,
            state_path=self.state_path,
            env_path=self.env_path,
            min_track_listeners=5000,
            client=client,
        )

        # Should not expand Untagged Artist or check Ignored Song.
        artists_queried = {c[1].casefold() for c in client.calls}
        self.assertNotIn("untagged artist", artists_queried)
        self.assertNotIn("ignored artist", artists_queried)

        # Untagged Candidate & Artist Side B had 0 tags: gated at track.getTopTags only.
        info_calls = [c for c in client.calls if c[0] == "track.getInfo"]
        similar_calls = [c for c in client.calls if c[0] == "track.getSimilar"]
        self.assertEqual(info_calls, [])
        self.assertEqual(similar_calls, [])

        self.assertEqual(result["tracks_added"], 0)
        self.assertEqual(result["tracks_rejected"], 2)
        self.assertEqual(result["artists_expanded"], 1)

        rows_after = read_lastfm_csv(self.csv_path)
        self.assertEqual(len(rows_after), 2)

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["expanded_artists"], ["seed artist"])
        self.assertIn(["new artist", "untagged candidate"], state["rejected_tracks"])
        self.assertIn(["seed artist", "artist side b"], state["rejected_tracks"])

    def test_recursive_snowball_appends_tagged_hits_and_expands_new_artists(self) -> None:
        seed = make_lastfm_row(
            track="Origin Song",
            artist="Origin Artist",
            track_tag_count="1",
            track_tags_names="synthpop",
            track_tags_all="synthpop (100)",
            similar_tracks="Discovered Artist - Tagged Hit (0.88)",
        )
        write_lastfm_csv(self.csv_path, [seed])

        client = FakeSnowballClient(
            track_tags={
                ("discovered artist", "tagged hit"): [
                    {"name": "synthpop", "count": 95},
                    {"name": "electropop", "count": 50},
                ],
                ("discovered artist", "album deep cut"): [
                    {"name": "dream pop", "count": 80},
                ],
            },
            track_similar={
                ("discovered artist", "tagged hit"): [
                    {
                        "name": "Final Hop",
                        "match": 0.77,
                        "artist": {"name": "Third Artist"},
                    }
                ]
            },
            artist_top_tracks={
                "origin artist": {1: [{"name": "Origin Song", "listeners": "800000"}]},
                "discovered artist": {
                    1: [
                        {"name": "Tagged Hit", "listeners": "400000"},
                        {"name": "Album Deep Cut", "listeners": "18000"},
                    ]
                },
                "third artist": {1: []},
            },
        )

        result = snowball.run_snowball(
            csv_path=self.csv_path,
            state_path=self.state_path,
            env_path=self.env_path,
            min_track_listeners=5000,
            similar_limit=20,
            client=client,
        )

        self.assertEqual(result["tracks_added"], 2)
        self.assertEqual(result["artists_expanded"], 2)

        rows = read_lastfm_csv(self.csv_path)
        self.assertEqual(len(rows), 3)
        self.assertEqual(list(rows[1].keys()), snowball.LASTFM_FIELDNAMES)
        self.assertEqual(rows[1]["lfm_track_name"], "Tagged Hit")
        self.assertEqual(rows[1]["lfm_artist_name"], "Discovered Artist")
        self.assertEqual(rows[1]["track_tag_count"], "2")
        self.assertEqual(rows[1]["similar_tracks"], "Third Artist - Final Hop (0.77)")
        self.assertEqual(rows[2]["lfm_track_name"], "Album Deep Cut")
        self.assertEqual(rows[2]["lfm_artist_name"], "Discovered Artist")

        # Verify track.getSimilar was requested with limit=20 (client saw it, and Third Artist Hop was queued).
        self.assertIn(("third artist", "final hop"), {
            (c[1].casefold(), c[2].casefold())
            for c in client.calls
            if c[0] == "track.getTopTags"
        })

    def test_artist_top_tracks_stops_paginating_below_listener_floor(self) -> None:
        client = FakeSnowballClient(
            artist_top_tracks={
                "popular artist": {
                    1: [
                        {"name": "Hit One", "listeners": "100000"},
                        {"name": "Hit Two", "listeners": "6000"},
                        {"name": "Below Floor", "listeners": "4999"},
                    ],
                    2: [
                        {"name": "Should Not Fetch Page 2", "listeners": "4000"},
                    ],
                }
            }
        )

        tracks = client.get_artist_top_tracks(
            "Popular Artist", min_listeners=5000, max_pages=5, page_limit=3
        )
        self.assertEqual(
            tracks,
            [("Popular Artist", "Hit One"), ("Popular Artist", "Hit Two")],
        )
        pages_called = [c[3] for c in client.calls if c[0] == "artist.getTopTracks"]
        self.assertEqual(pages_called, ["1"])

    def test_resume_from_state_makes_zero_duplicate_api_calls(self) -> None:
        seed = make_lastfm_row(
            track="Origin Song",
            artist="Origin Artist",
            track_tag_count="1",
            track_tags_names="pop",
            track_tags_all="pop (100)",
            similar_tracks="Peer Artist - Untagged Song (0.80)",
        )
        write_lastfm_csv(self.csv_path, [seed])

        first_client = FakeSnowballClient(
            artist_top_tracks={
                "origin artist": {
                    1: [{"name": "Origin B-Side", "listeners": "10000"}]
                }
            }
        )
        first_result = snowball.run_snowball(
            csv_path=self.csv_path,
            state_path=self.state_path,
            env_path=self.env_path,
            min_track_listeners=5000,
            client=first_client,
        )
        self.assertGreater(len(first_client.calls), 0)
        self.assertEqual(first_result["tracks_rejected"], 2)

        # Second run with identical CSV and saved state must make zero API calls.
        second_client = FakeSnowballClient(
            artist_top_tracks={
                "origin artist": {
                    1: [{"name": "Origin B-Side", "listeners": "10000"}]
                }
            }
        )
        second_result = snowball.run_snowball(
            csv_path=self.csv_path,
            state_path=self.state_path,
            env_path=self.env_path,
            min_track_listeners=5000,
            client=second_client,
        )
        self.assertEqual(second_client.calls, [])
        self.assertEqual(second_result["tracks_checked"], 0)
        self.assertEqual(second_result["artists_expanded"], 0)

    def test_artist_cache_seeded_from_existing_csv_avoids_artist_refetch(self) -> None:
        seed = make_lastfm_row(
            track="Known Song",
            artist="Cached Artist",
            track_tag_count="1",
            track_tags_names="rock",
            track_tags_all="rock (100)",
            similar_tracks="Cached Artist - Second Song (0.92)",
        )
        write_lastfm_csv(self.csv_path, [seed])

        client = FakeSnowballClient(
            track_tags={
                ("cached artist", "second song"): [{"name": "rock", "count": 90}],
            },
            artist_top_tracks={"cached artist": {1: []}},
        )
        result = snowball.run_snowball(
            csv_path=self.csv_path,
            state_path=self.state_path,
            env_path=self.env_path,
            min_track_listeners=5000,
            client=client,
        )

        self.assertEqual(result["tracks_added"], 1)
        self.assertEqual(result["artist_cache_hits"], 1)
        artist_api_calls = [
            c for c in client.calls if c[0] in ("artist.getInfo", "artist.getTopTags")
        ]
        self.assertEqual(artist_api_calls, [])


if __name__ == "__main__":
    unittest.main()
