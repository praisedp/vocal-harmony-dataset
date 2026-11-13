"""Regression checks for incremental CSV updates; uses only temporary datasets."""

import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rich.console import Console

import build_master as builder


class MasterDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.master = self.root / "data" / "master.csv"
        console = patch.object(builder, "console", Console(file=io.StringIO()))
        console.start()
        self.addCleanup(console.stop)

    def row(self, uri="spotify:track:one", artist="Kate Bush", title="Running Up That Hill"):
        return dict(zip(builder.CSV_FIELDNAMES, ["1", uri, artist, title, "Label", "1", "4", "2", "57221016"]))

    def write_csv(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=builder.CSV_FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)

    def run_builder(self):
        return builder.build_master_dataset(self.downloads, self.master, preview_count=0)

    def test_incremental_update_and_repeat_run_leave_existing_bytes_unchanged(self):
        self.write_csv(self.downloads / "week1.csv", [self.row(), self.row()])
        self.assertEqual(len(self.run_builder()), 1)
        original = self.master.read_bytes()
        self.assertEqual(self.run_builder(), [])
        self.assertEqual(self.master.read_bytes(), original)
        self.write_csv(self.downloads / "week2.csv", [self.row(), self.row("spotify:track:two", title="New Song")])
        self.assertEqual(len(self.run_builder()), 1)
        self.assertTrue(self.master.read_bytes().startswith(original))
        self.assertEqual(len(builder.read_chart_rows(self.master)), 2)
        updated = self.master.read_bytes()
        self.assertEqual(self.run_builder(), [])
        self.assertEqual(self.master.read_bytes(), updated)

    def test_uri_and_normalized_artist_title_both_detect_duplicates(self):
        rows = [self.row(), self.row(title="Alternate Title"),
                self.row("spotify:track:alternate", "  KATE   BUSH ", "running up that hill"),
                self.row("", "Kate Bush", "Running Up That Hill")]
        self.write_csv(self.downloads / "week.csv", rows)
        self.assertEqual(len(self.run_builder()), 1)

    def test_only_visible_csv_files_are_read_and_quoted_text_round_trips(self):
        row = self.row(artist="Bad Bunny, Chencho Corleone", title='Tití, "Me"\nPreguntó')
        self.write_csv(self.downloads / "week.CSV", [row])
        for name in ("document", "duplicate.doc", ".hidden.csv", "partial.csv.tmp"):
            self.write_csv(self.downloads / name, [self.row("spotify:track:ignored", title="Ignored")])
        self.assertEqual(len(self.run_builder()), 1)
        self.assertEqual(builder.read_chart_rows(self.master)[0], row)

    def test_invalid_input_file_is_rejected_without_accepting_partial_rows(self):
        self.write_csv(self.downloads / "broken.csv", [self.row()])
        with (self.downloads / "broken.csv").open("a") as handle:
            handle.write("2,too,few,fields\n")
        (self.downloads / "unrelated.csv").write_text("wrong,header\nx,y\n")
        self.write_csv(self.downloads / "valid.csv", [self.row("spotify:track:valid", title="Valid")])
        self.assertEqual([row["uri"] for row in self.run_builder()], ["spotify:track:valid"])

    def test_invalid_existing_master_is_never_modified(self):
        self.master.parent.mkdir()
        self.master.write_text("wrong,header\nx,y\n")
        original = self.master.read_bytes()
        self.write_csv(self.downloads / "week.csv", [self.row()])
        with self.assertRaises(ValueError):
            self.run_builder()
        self.assertEqual(self.master.read_bytes(), original)

    def test_empty_master_and_missing_final_newline(self):
        self.master.parent.mkdir()
        self.master.touch()
        self.write_csv(self.downloads / "week1.csv", [self.row()])
        self.assertEqual(len(self.run_builder()), 1)
        self.master.write_bytes(self.master.read_bytes().rstrip(b"\r\n"))
        self.write_csv(self.downloads / "week2.csv", [self.row("spotify:track:two", title="Second Song")])
        self.assertEqual(len(self.run_builder()), 1)
        self.assertEqual(len(builder.read_chart_rows(self.master)), 2)

    def test_failed_publish_preserves_master_and_removes_temporary_file(self):
        self.write_csv(self.master, [self.row()])
        original = self.master.read_bytes()
        with patch.object(builder.os, "replace", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                builder.append_atomically(self.master, [self.row("spotify:track:two")])
        self.assertEqual(self.master.read_bytes(), original)
        self.assertEqual(list(self.master.parent.iterdir()), [self.master])


if __name__ == "__main__":
    unittest.main()
