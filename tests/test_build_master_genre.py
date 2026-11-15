"""Offline regression tests for incremental Last.fm genre enrichment."""

from __future__ import annotations

import csv
import io
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

import build_master as builder
import build_master_genre as enricher


class FakeLastFmClient(enricher.LastFmClient):
    """Deterministic offline stub simulating Last.fm endpoint responses."""

    def __init__(self, *, fail_once_track: str | None = None) -> None:
        super().__init__("test-key", min_interval_sec=0.0)
        self.fail_once_track = fail_once_track
        self._failed_already = False
        self.calls: list[tuple[str, str, str]] = []

    def call(self, method: str, **params: str) -> dict:
        artist = params.get("artist", "")
        track = params.get("track", "")
        self.calls.append((method, artist, track))

        if (
            self.fail_once_track
            and method == "track.getInfo"
            and track == self.fail_once_track
            and not self._failed_already
        ):
            self._failed_already = True
            raise enricher.LastFmTransientError("simulated timeout")

        if method == "track.getInfo":
            return {
                "track": {
                    "name": track,
                    "mbid": f"mbid-track-{track}",
                    "url": f"https://www.last.fm/music/{artist}/_/{track}",
                    "duration": "200000",
                    "streamable": {"#text": "0", "fulltrack": "0"},
                    "listeners": "1000",
                    "playcount": "5000",
                    "album": {
                        "title": "Sample Album",
                        "artist": artist,
                        "mbid": "mbid-album-1",
                        "url": "https://www.last.fm/album/1",
                        "image": [{"#text": "https://img/album.png", "size": "extralarge"}],
                    },
                    "toptags": {"tag": [{"name": "pop"}]},
                    "wiki": {
                        "published": "01 Jan 2025, 00:00",
                        "summary": f"Summary for {track} Read more on Last.fm.",
                        "content": f"Full content for {track}",
                    },
                }
            }
        if method == "track.getTopTags":
            return {"toptags": {"tag": [{"name": "pop", "count": 100}, {"name": "synthpop", "count": 80}]}}
        if method == "track.getSimilar":
            return {
                "similartracks": {
                    "track": [{"name": "Similar Song", "match": 0.9, "artist": {"name": artist}}]
                }
            }
        if method == "artist.getInfo":
            return {
                "artist": {
                    "name": artist,
                    "mbid": f"mbid-artist-{artist}",
                    "url": f"https://www.last.fm/music/{artist}",
                    "ontour": "1",
                    "stats": {"listeners": "50000", "playcount": "250000"},
                    "image": [{"#text": "https://img/artist.png", "size": "mega"}],
                    "similar": {"artist": [{"name": "Peer Artist"}]},
                    "tags": {"tag": [{"name": "pop"}]},
                    "bio": {
                        "published": "01 Jan 2020, 00:00",
                        "summary": f"Bio for {artist}",
                        "content": f"Full bio for {artist}",
                    },
                }
            }
        if method == "artist.getTopTags":
            return {"toptags": {"tag": [{"name": "pop", "count": 100}, {"name": "dance", "count": 60}]}}
        return {}


class MasterGenreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.master = self.root / "data" / "master.csv"
        self.output = self.root / "data" / "master_genre.csv"
        self.env = self.root / ".env"
        self.env.write_text("LASTFM_API_KEY=dummy-key\n", encoding="utf-8")

        console_patch = patch.object(enricher, "console", Console(file=io.StringIO()))
        console_patch.start()
        self.addCleanup(console_patch.stop)

    def master_row(self, rank: str, uri: str, artist: str, title: str) -> dict[str, str]:
        return dict(
            zip(
                builder.CSV_FIELDNAMES,
                [rank, uri, artist, title, "Label", "1", "2", "3", "100000"],
            )
        )

    def write_master(self, rows: list[dict[str, str]]) -> None:
        self.master.parent.mkdir(parents=True, exist_ok=True)
        with self.master.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=builder.CSV_FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)

    def read_output(self) -> list[dict[str, str]]:
        with self.output.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_initial_run_and_resume_skip_completed_rows(self) -> None:
        self.write_master(
            [
                self.master_row("1", "spotify:track:1", "Artist A", "Song One"),
                self.master_row("2", "spotify:track:2", "Artist B", "Song Two"),
                self.master_row("3", "spotify:track:3", "Artist C", "Song Three"),
            ]
        )

        first_client = FakeLastFmClient()
        res1 = enricher.build_master_genre(
            self.master, self.output, self.env, limit=2, client=first_client
        )
        self.assertEqual(res1["enriched_now"], 2)
        self.assertEqual(res1["total_saved"], 2)
        self.assertEqual(len(self.read_output()), 2)

        second_client = FakeLastFmClient()
        res2 = enricher.build_master_genre(
            self.master, self.output, self.env, limit=None, client=second_client
        )
        self.assertEqual(res2["existing_count"], 2)
        self.assertEqual(res2["enriched_now"], 1)
        self.assertEqual(res2["total_saved"], 3)

        track_info_calls = [c for c in second_client.calls if c[0] == "track.getInfo"]
        self.assertEqual(track_info_calls, [("track.getInfo", "Artist C", "Song Three")])

        saved_rows = self.read_output()
        self.assertEqual([r["uri"] for r in saved_rows], ["spotify:track:1", "spotify:track:2", "spotify:track:3"])
        self.assertEqual(saved_rows[0]["genre_source"], "track+artist")
        self.assertEqual(saved_rows[0]["genres_all"], "pop | synthpop | dance")

    def test_artist_cache_seeded_from_existing_master_genre(self) -> None:
        self.write_master(
            [
                self.master_row("1", "spotify:track:1", "The Weeknd", "Blinding Lights"),
                self.master_row("2", "spotify:track:2", "The Weeknd, Ariana Grande", "Save Your Tears"),
            ]
        )

        first_client = FakeLastFmClient()
        enricher.build_master_genre(
            self.master, self.output, self.env, limit=1, client=first_client
        )

        second_client = FakeLastFmClient()
        res2 = enricher.build_master_genre(
            self.master, self.output, self.env, limit=None, client=second_client
        )
        self.assertEqual(res2["enriched_now"], 1)
        self.assertEqual(res2["artist_cache_hits"], 1)

        artist_calls = [c for c in second_client.calls if c[0].startswith("artist.")]
        self.assertEqual(artist_calls, [])
        rows = self.read_output()
        self.assertEqual(rows[1]["lfm_artist_name"], "The Weeknd")
        self.assertEqual(rows[1]["artist_tags_names"], "pop | dance")

    def test_transient_error_is_not_saved_and_retried_next_run(self) -> None:
        self.write_master(
            [
                self.master_row("1", "spotify:track:1", "Artist A", "Flaky Song"),
                self.master_row("2", "spotify:track:2", "Artist B", "Good Song"),
            ]
        )

        failing_client = FakeLastFmClient(fail_once_track="Flaky Song")
        res1 = enricher.build_master_genre(
            self.master, self.output, self.env, client=failing_client
        )
        self.assertEqual(res1["transient_failures"], 1)
        self.assertEqual(res1["enriched_now"], 1)
        self.assertEqual([r["uri"] for r in self.read_output()], ["spotify:track:2"])

        retry_client = FakeLastFmClient()
        res2 = enricher.build_master_genre(
            self.master, self.output, self.env, client=retry_client
        )
        self.assertEqual(res2["transient_failures"], 0)
        self.assertEqual(res2["enriched_now"], 1)
        self.assertEqual(
            [r["uri"] for r in self.read_output()],
            ["spotify:track:2", "spotify:track:1"],
        )

    def test_invalid_existing_master_genre_is_never_modified(self) -> None:
        self.write_master([self.master_row("1", "spotify:track:1", "Artist A", "Song One")])
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text("wrong,header\nx,y\n", encoding="utf-8")
        original = self.output.read_bytes()

        with self.assertRaises(ValueError):
            enricher.build_master_genre(
                self.master, self.output, self.env, client=FakeLastFmClient()
            )
        self.assertEqual(self.output.read_bytes(), original)

    def test_primary_artist_and_track_candidate_cleaning(self) -> None:
        self.assertEqual(
            enricher.extract_primary_artist("Justin Bieber, Daniel Caesar, GIVĒON"),
            "Justin Bieber",
        )
        self.assertEqual(
            enricher.extract_primary_artist("Tyler, The Creator, Kali Uchis"),
            "Tyler, The Creator",
        )
        self.assertEqual(
            enricher.extract_primary_artist("Earth, Wind & Fire"),
            "Earth, Wind & Fire",
        )
        self.assertEqual(
            enricher.track_candidates("Peaches (feat. Daniel Caesar & Giveon)"),
            ["Peaches (feat. Daniel Caesar & Giveon)", "Peaches"],
        )
        self.assertEqual(
            enricher.track_candidates("Hotel California - 2013 Remaster"),
            ["Hotel California - 2013 Remaster", "Hotel California"],
        )
        self.assertEqual(
            enricher.track_candidates("Friday (feat. Mufasa & Hypeman) - Dopamine Re-Edit"),
            [
                "Friday (feat. Mufasa & Hypeman) - Dopamine Re-Edit",
                "Friday - Dopamine Re-Edit",
                "Friday",
            ],
        )


if __name__ == "__main__":
    unittest.main()
