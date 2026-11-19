"""Unit tests for the multi-CSV YouTube Music search and mood collectors."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

PROJECT_DIR = Path(__file__).resolve().parent.parent
PIPELINE_DIR = PROJECT_DIR / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from collect_ytmusic_moods import run_mood_collection
from collect_ytmusic_search import run_collection
from ytmusic_catalog_store import (
    CatalogStore,
    is_official_featured_playlist,
    normalize_playlist_id,
    parse_duration_seconds,
)


def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class FakeYTMusic:
    """Deterministic fake YTMusic client tracking exact API call counts."""

    def __init__(self, location: str = "US") -> None:
        self.location = location
        self.search_calls: List[tuple[str, str, int]] = []
        self.playlist_calls: List[str] = []
        self.album_calls: List[str] = []
        self.browse_calls: List[Dict[str, Any]] = []

    def search(self, query: str, filter: str = "songs", limit: int = 20, **kwargs: Any) -> List[Dict[str, Any]]:
        self.search_calls.append((query, filter, limit))
        if filter == "featured_playlists":
            return [
                {
                    "title": "'60s & '70s Pop Classics",
                    "browseId": "VLRDCLAK5uy_SHARED_PL",
                    "author": "YouTube Music",
                    "itemCount": "2",
                    "resultType": "playlist",
                },
                {
                    "title": "User Community Playlist",
                    "browseId": "VLPL_USER_COMMUNITY_123",
                    "author": "Random User",
                    "itemCount": "50",
                    "resultType": "playlist",
                },
            ]
        if filter == "albums":
            return [
                {
                    "title": "Shared Classic Album",
                    "browseId": "MPREb_SHARED_ALB",
                    "type": "Album",
                    "artist": "The Classics",
                    "year": "1968",
                    "isExplicit": False,
                    "resultType": "album",
                }
            ]
        if filter == "songs":
            return [
                {
                    "videoId": "vid_direct_1",
                    "title": "Direct Search Song",
                    "artists": [{"name": "Direct Artist", "id": "UC_DIRECT"}],
                    "album": {"name": "Direct Album", "id": "MPREb_DIRECT"},
                    "duration": "3:20",
                    "duration_seconds": 200,
                    "videoType": "MUSIC_VIDEO_TYPE_ATV",
                    "isExplicit": False,
                    "isAvailable": True,
                    "resultType": "song",
                }
            ]
        return []

    def get_playlist(self, playlistId: str, limit: Any = None, related: bool = True) -> Dict[str, Any]:
        self.playlist_calls.append(playlistId)
        return {
            "id": playlistId,
            "title": f"Title for {playlistId}",
            "description": "Official editorial playlist",
            "author": {"name": "YouTube Music", "id": "UC_YTM"},
            "year": "2025",
            "trackCount": 2,
            "duration": "7:05",
            "duration_seconds": 425,
            "views": 12345,
            "privacy": "PUBLIC",
            "tracks": [
                {
                    "videoId": "vid_pl_1",
                    "title": "Playlist Track One",
                    "artists": [{"name": "Artist A", "id": "UC_A"}],
                    "album": {"name": "Album A", "id": "MPREb_A"},
                    "duration": "3:30",
                    "duration_seconds": 210,
                    "videoType": "MUSIC_VIDEO_TYPE_ATV",
                    "isExplicit": False,
                    "isAvailable": True,
                },
                {
                    "videoId": "vid_shared_overlap",
                    "title": "Shared Track Across Playlist and Album",
                    "artists": [{"name": "Artist Shared", "id": "UC_S"}],
                    "album": {"name": "Shared Classic Album", "id": "MPREb_SHARED_ALB"},
                    "duration": "3:35",
                    "duration_seconds": 215,
                    "videoType": "MUSIC_VIDEO_TYPE_ATV",
                    "isExplicit": True,
                    "isAvailable": True,
                },
            ],
            "related": [
                {
                    "title": "Related Editorial Playlist",
                    "playlistId": "RDCLAK5uy_RELATED_NEVER_FETCHED",
                    "description": "Playlist • YouTube Music",
                },
                {
                    "title": "Related User Playlist",
                    "playlistId": "PL_RELATED_USER_999",
                    "description": "Playlist • User",
                },
            ],
        }

    def get_album(self, browseId: str) -> Dict[str, Any]:
        self.album_calls.append(browseId)
        return {
            "title": "Shared Classic Album",
            "type": "Album",
            "audioPlaylistId": "OLAK5uy_AUDIO_SHARED",
            "year": "1968",
            "artists": [{"name": "The Classics", "id": "UC_CLASSICS"}],
            "trackCount": 2,
            "duration": "7:46",
            "duration_seconds": 466,
            "isExplicit": False,
            "description": "Landmark 1968 studio album",
            "tracks": [
                {
                    "videoId": "vid_shared_overlap",
                    "title": "Shared Track Across Playlist and Album",
                    "artists": [{"name": "Artist Shared", "id": "UC_S"}],
                    "album": "Shared Classic Album",
                    "duration": "3:35",
                    "duration_seconds": 215,
                    "trackNumber": 1,
                    "videoType": "MUSIC_VIDEO_TYPE_ATV",
                    "isExplicit": True,
                    "isAvailable": True,
                    "views": "5.2M",
                    "creditsBrowseId": "MPTC_SHARED",
                },
                {
                    "videoId": "vid_alb_2",
                    "title": "Album Deep Cut Two",
                    "artists": [{"name": "The Classics", "id": "UC_CLASSICS"}],
                    "album": "Shared Classic Album",
                    "duration": "4:11",
                    "duration_seconds": 251,
                    "trackNumber": 2,
                    "videoType": "MUSIC_VIDEO_TYPE_ATV",
                    "isExplicit": False,
                    "isAvailable": True,
                    "views": "820K",
                    "creditsBrowseId": "MPTC_ALB2",
                },
            ],
            "related_recommendations": [
                {
                    "title": "Recommended Similar Album",
                    "browseId": "MPREb_REL_REC_NEVER_FETCHED",
                    "artists": [{"name": "Similar Band", "id": "UC_SIM"}],
                    "year": "1970",
                    "isExplicit": False,
                }
            ],
            "other_versions": [
                {
                    "title": "Shared Classic Album (Deluxe)",
                    "browseId": "MPREb_OTHER_VER_NEVER_FETCHED",
                    "year": "2018",
                    "isExplicit": False,
                }
            ],
        }

    def get_mood_categories(self) -> Dict[str, List[Dict[str, str]]]:
        return {
            "Genres": [
                {"title": "Rock", "params": "PARAMS_ROCK"},
                {"title": "Decades", "params": "PARAMS_DECADES"},
            ]
        }

    def _send_request(self, endpoint: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self.browse_calls.append(body)
        params = body.get("params")
        if params == "PARAMS_ROCK":
            return {
                "contents": {
                    "singleColumnBrowseResultsRenderer": {
                        "tabs": [
                            {
                                "tabRenderer": {
                                    "content": {
                                        "sectionListRenderer": {
                                            "contents": [
                                                {
                                                    "musicCarouselShelfRenderer": {
                                                        "header": {
                                                            "musicCarouselShelfBasicHeaderRenderer": {
                                                                "title": {"runs": [{"text": "Songs"}]}
                                                            }
                                                        },
                                                        "contents": [
                                                            {
                                                                "musicResponsiveListItemRenderer": {
                                                                    "playlistItemData": {"videoId": "vid_mood_song_1"},
                                                                    "flexColumns": [
                                                                        {
                                                                            "musicResponsiveListItemFlexColumnRenderer": {
                                                                                "text": {"runs": [{"text": "Mood Shelf Song"}]}
                                                                            }
                                                                        },
                                                                        {
                                                                            "musicResponsiveListItemFlexColumnRenderer": {
                                                                                "text": {"runs": [{"text": "Mood Artist"}]}
                                                                            }
                                                                        },
                                                                    ],
                                                                }
                                                            }
                                                        ],
                                                    }
                                                },
                                                {
                                                    "musicCarouselShelfRenderer": {
                                                        "header": {
                                                            "musicCarouselShelfBasicHeaderRenderer": {
                                                                "title": {"runs": [{"text": "Featured playlists"}]}
                                                            }
                                                        },
                                                        "contents": [
                                                            {
                                                                "musicTwoRowItemRenderer": {
                                                                    "title": {"runs": [{"text": "Already Seen Playlist"}]},
                                                                    "subtitle": {"runs": [{"text": "YouTube Music"}]},
                                                                    "navigationEndpoint": {
                                                                        "browseEndpoint": {
                                                                            "browseId": "VLRDCLAK5uy_SHARED_PL",
                                                                            "browseEndpointContextSupportedConfigs": {
                                                                                "browseEndpointContextMusicConfig": {
                                                                                    "pageType": "MUSIC_PAGE_TYPE_PLAYLIST"
                                                                                }
                                                                            },
                                                                        }
                                                                    },
                                                                }
                                                            },
                                                            {
                                                                "musicTwoRowItemRenderer": {
                                                                    "title": {"runs": [{"text": "New Mood Playlist"}]},
                                                                    "subtitle": {"runs": [{"text": "YouTube Music"}]},
                                                                    "navigationEndpoint": {
                                                                        "browseEndpoint": {
                                                                            "browseId": "VLRDCLAK5uy_NEW_MOOD_PL",
                                                                            "browseEndpointContextSupportedConfigs": {
                                                                                "browseEndpointContextMusicConfig": {
                                                                                    "pageType": "MUSIC_PAGE_TYPE_PLAYLIST"
                                                                                }
                                                                            },
                                                                        }
                                                                    },
                                                                }
                                                            },
                                                        ],
                                                    }
                                                },
                                                {
                                                    "musicCarouselShelfRenderer": {
                                                        "header": {
                                                            "musicCarouselShelfBasicHeaderRenderer": {
                                                                "title": {"runs": [{"text": "Community playlists"}]}
                                                            }
                                                        },
                                                        "contents": [
                                                            {
                                                                "musicTwoRowItemRenderer": {
                                                                    "title": {"runs": [{"text": "Skipped Community"}]},
                                                                    "subtitle": {"runs": [{"text": "User"}]},
                                                                    "navigationEndpoint": {
                                                                        "browseEndpoint": {
                                                                            "browseId": "VLPL_SKIPPED_COMM"
                                                                        }
                                                                    },
                                                                }
                                                            }
                                                        ],
                                                    }
                                                },
                                                {
                                                    "musicCarouselShelfRenderer": {
                                                        "header": {
                                                            "musicCarouselShelfBasicHeaderRenderer": {
                                                                "title": {"runs": [{"text": "Albums"}]}
                                                            }
                                                        },
                                                        "contents": [
                                                            {
                                                                "musicTwoRowItemRenderer": {
                                                                    "title": {"runs": [{"text": "Shared Classic Album"}]},
                                                                    "subtitle": {"runs": [{"text": "Album • The Classics"}]},
                                                                    "navigationEndpoint": {
                                                                        "browseEndpoint": {
                                                                            "browseId": "MPREb_SHARED_ALB",
                                                                            "browseEndpointContextSupportedConfigs": {
                                                                                "browseEndpointContextMusicConfig": {
                                                                                    "pageType": "MUSIC_PAGE_TYPE_ALBUM"
                                                                                }
                                                                            },
                                                                        }
                                                                    },
                                                                }
                                                            }
                                                        ],
                                                    }
                                                },
                                            ]
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        if params == "PARAMS_DECADES":
            return {
                "contents": {
                    "singleColumnBrowseResultsRenderer": {
                        "tabs": [
                            {
                                "tabRenderer": {
                                    "content": {
                                        "sectionListRenderer": {
                                            "contents": [
                                                {
                                                    "musicCarouselShelfRenderer": {
                                                        "header": {
                                                            "musicCarouselShelfBasicHeaderRenderer": {
                                                                "title": {"runs": [{"text": "1980s"}]}
                                                            }
                                                        },
                                                        "contents": [
                                                            {
                                                                "musicTwoRowItemRenderer": {
                                                                    "title": {"runs": [{"text": "The Hits: '80s"}]},
                                                                    "subtitle": {"runs": [{"text": "YouTube Music"}]},
                                                                    "navigationEndpoint": {
                                                                        "browseEndpoint": {
                                                                            "browseId": "VLRDCLAK5uy_SHARED_PL",
                                                                            "browseEndpointContextSupportedConfigs": {
                                                                                "browseEndpointContextMusicConfig": {
                                                                                    "pageType": "MUSIC_PAGE_TYPE_PLAYLIST"
                                                                                }
                                                                            },
                                                                        }
                                                                    },
                                                                }
                                                            }
                                                        ],
                                                    }
                                                }
                                            ]
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        return {}


class TestYTMusicCollectors(unittest.TestCase):
    def test_helpers(self) -> None:
        self.assertEqual(normalize_playlist_id("VLRDCLAK5uy_123"), "RDCLAK5uy_123")
        self.assertEqual(normalize_playlist_id("RDCLAK5uy_123"), "RDCLAK5uy_123")
        self.assertTrue(is_official_featured_playlist("VLRDCLAK5uy_123"))
        self.assertFalse(is_official_featured_playlist("VLPL_user_123"))
        self.assertEqual(parse_duration_seconds(None, "4:11"), "251")
        self.assertEqual(parse_duration_seconds(215, "3:35"), "215")

    def test_multi_csv_dedup_across_queries_and_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            state_dir = Path(tmp) / "state"
            fake_clients: Dict[str, FakeYTMusic] = {}

            def factory(loc: str) -> FakeYTMusic:
                if loc not in fake_clients:
                    fake_clients[loc] = FakeYTMusic(loc)
                return fake_clients[loc]

            # 1. Run search matrix across 1 location (US), 2 genres (Pop, Blues), 2 eras (1960s, 1970s)
            # That is 4 (location, genre, era) pairs x 3 filters = 12 search steps.
            with CatalogStore(data_dir=data_dir, state_dir=state_dir, delay=0.0) as store:
                summary = run_collection(
                    store,
                    locations=("US",),
                    genres=("Pop", "Blues"),
                    eras=("1960s", "1970s"),
                    search_filters=("featured_playlists", "albums", "songs"),
                    limit=10,
                    yt_factory=factory,
                )

            self.assertEqual(summary["steps_executed"], 12)
            us_client = fake_clients["US"]

            # Even though 'RDCLAK5uy_SHARED_PL' was returned in all 4 featured_playlists searches,
            # get_playlist() must be called ONLY ONCE, and user playlist 'PL_USER_COMMUNITY_123' NEVER!
            self.assertEqual(us_client.playlist_calls, ["RDCLAK5uy_SHARED_PL"])
            # Even though 'MPREb_SHARED_ALB' was returned in all 4 album searches,
            # get_album() must be called ONLY ONCE!
            self.assertEqual(us_client.album_calls, ["MPREb_SHARED_ALB"])

            # Verify search_hits.csv kept all 16 hits (4 pairs * (2 playlists + 1 album + 1 song))
            search_hits = _read_csv_rows(data_dir / "search_hits.csv")
            self.assertEqual(len(search_hits), 16)
            pl_hits = [r for r in search_hits if r["item_id"] == "RDCLAK5uy_SHARED_PL"]
            self.assertEqual(len(pl_hits), 4)
            self.assertEqual(
                {(r["location"], r["genre"], r["era"]) for r in pl_hits},
                {
                    ("US", "Pop", "1960s"),
                    ("US", "Blues", "1960s"),
                    ("US", "Pop", "1970s"),
                    ("US", "Blues", "1970s"),
                },
            )

            # Verify playlists.csv and albums.csv each have 1 unique row
            self.assertEqual(len(_read_csv_rows(data_dir / "playlists.csv")), 1)
            self.assertEqual(len(_read_csv_rows(data_dir / "albums.csv")), 1)
            self.assertEqual(len(_read_csv_rows(data_dir / "playlist_tracks.csv")), 2)
            self.assertEqual(len(_read_csv_rows(data_dir / "album_tracks.csv")), 2)

            # Verify tracks.csv deduplicated 'vid_shared_overlap' across playlist & album:
            # unique tracks = vid_pl_1, vid_shared_overlap, vid_alb_2, vid_direct_1 (4 tracks)
            tracks = _read_csv_rows(data_dir / "tracks.csv")
            self.assertEqual({t["video_id"] for t in tracks}, {"vid_pl_1", "vid_shared_overlap", "vid_alb_2", "vid_direct_1"})

            # Verify related_candidates.csv saved the 2 related playlists + 2 related/other albums
            # WITHOUT ever calling get_playlist or get_album on them!
            related = _read_csv_rows(data_dir / "related_candidates.csv")
            self.assertEqual(
                {r["related_id"] for r in related},
                {
                    "RDCLAK5uy_RELATED_NEVER_FETCHED",
                    "PL_RELATED_USER_999",
                    "MPREb_REL_REC_NEVER_FETCHED",
                    "MPREb_OTHER_VER_NEVER_FETCHED",
                },
            )

            # 2. Re-running the exact same search grid must skip all 12 completed steps (0 new API calls)
            with CatalogStore(data_dir=data_dir, state_dir=state_dir, delay=0.0) as store2:
                summary2 = run_collection(
                    store2,
                    locations=("US",),
                    genres=("Pop", "Blues"),
                    eras=("1960s", "1970s"),
                    search_filters=("featured_playlists", "albums", "songs"),
                    limit=10,
                    yt_factory=factory,
                )
            self.assertEqual(summary2["steps_executed"], 0)
            self.assertEqual(summary2["already_completed_steps"], 12)

            # 3. Now run the separate mood collector (`collect_ytmusic_moods.py`) against the same data_dir!
            # It encounters 'RDCLAK5uy_SHARED_PL', 'MPREb_SHARED_ALB', and a brand-new 'RDCLAK5uy_NEW_MOOD_PL'.
            # It must skip 'RDCLAK5uy_SHARED_PL' and 'MPREb_SHARED_ALB' and ONLY fetch 'RDCLAK5uy_NEW_MOOD_PL'!
            with CatalogStore(data_dir=data_dir, state_dir=state_dir, delay=0.0) as store3:
                mood_summary = run_mood_collection(
                    store3,
                    locations=("US",),
                    yt_factory=factory,
                )
            self.assertEqual(mood_summary["categories_executed"], 2)
            self.assertEqual(mood_summary["playlists_fetched"], 1)
            self.assertEqual(mood_summary["playlists_skipped"], 2)  # skipped on Rock AND Decades!
            self.assertEqual(mood_summary["albums_fetched"], 0)
            self.assertEqual(mood_summary["albums_skipped"], 1)
            self.assertEqual(us_client.playlist_calls, ["RDCLAK5uy_SHARED_PL", "RDCLAK5uy_NEW_MOOD_PL"])
            self.assertEqual(us_client.album_calls, ["MPREb_SHARED_ALB"])

            # Verify mood_category_hits.csv recorded the Decades era='1980s' and skipped Community playlists
            mood_hits = _read_csv_rows(data_dir / "mood_category_hits.csv")
            self.assertEqual(len(mood_hits), 5)  # 1 song + 2 featured playlists + 1 album on Rock + 1 playlist on Decades
            decades_hit = [r for r in mood_hits if r["category_title"] == "Decades"][0]
            self.assertEqual(decades_hit["era"], "1980s")
            self.assertEqual(decades_hit["item_id"], "RDCLAK5uy_SHARED_PL")


if __name__ == "__main__":
    unittest.main()
