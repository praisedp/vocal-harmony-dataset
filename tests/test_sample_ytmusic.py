"""Offline checks for sampling and saving YouTube Music search evidence."""

import argparse
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pipeline import sample_ytmusic as sampler


class SampleYouTubeMusicTests(unittest.TestCase):
    def test_unique_reproducible_sample_and_oversize_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "master.csv"
            source.write_text("uri,artist_names,track_name\na,A,One\na,A,One\nb,B,Two\n")
            first = sampler.select_sample(source, 2, 42)
            self.assertEqual(first, sampler.select_sample(source, 2, 42))
            self.assertEqual(len({row["uri"] for row in first}), 2)
            with self.assertRaises(ValueError):
                sampler.select_sample(source, 3, 42)

    def test_missing_fields_are_blank_and_false_is_preserved(self):
        source = {"uri": "a", "artist_names": "A", "track_name": "One"}
        row = sampler.candidate_row(source, "A One", 1, {"videoId": "v", "isExplicit": False})
        self.assertEqual(row["duration_seconds"], "")
        self.assertEqual(row["is_explicit"], False)
        self.assertEqual(row["is_available"], "")

    def test_raw_evidence_survives_empty_results_and_query_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "master.csv"
            source.write_text("uri,artist_names,track_name\na,A,One\nb,B,Two\nc,C,Three\n")
            output = Path(folder) / "results"
            args = argparse.Namespace(input=source, count=3, seed=42, output_dir=output,
                                      location="LK", limit=1, delay=0)
            with patch.object(sampler, "YTMusic") as client, redirect_stdout(io.StringIO()):
                client.return_value.search.side_effect = [
                    [{"videoId": "v1", "title": "Candidate"}, {"videoId": "v2"}],
                    [], RuntimeError("simulated API failure"),
                ]
                summary = sampler.run(args)
            self.assertEqual(summary["statuses"], {"found": 1, "no_results": 1, "error": 1})
            records = [json.loads(line) for line in (output / "raw.jsonl").read_text().splitlines()]
            self.assertEqual(len(records), 3)
            self.assertEqual(len(records[0]["results"]), 2)
            with (output / "candidates.csv").open() as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 1)
            with self.assertRaises(FileExistsError):
                sampler.run(args)


if __name__ == "__main__":
    unittest.main()
