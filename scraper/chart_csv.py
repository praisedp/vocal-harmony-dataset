"""Validate downloaded chart CSVs before counting a week as complete."""

import csv
from pathlib import Path

CHART_FIELDS = {
    "rank", "uri", "artist_names", "track_name", "source", "peak_rank",
    "previous_rank", "weeks_on_chart", "streams",
}


def validate_chart_csv(path: Path) -> int:
    """Return the row count, rejecting empty, malformed, or non-chart downloads.

    Some historical charts have fewer than 200 entries. A short chart is not
    automatically a failed download; no exact 200-row assumption is made.
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        fields = reader.fieldnames or []
        if len(fields) != len(CHART_FIELDS) or set(fields) != CHART_FIELDS:
            raise ValueError(f"Invalid Spotify chart CSV header: {path.name}")
        ranks = set()
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed chart row near line {reader.line_num}")
            rank = int(row["rank"])
            if rank < 1 or rank > 200 or rank in ranks:
                raise ValueError(f"Invalid or duplicate chart rank: {rank}")
            if not row["uri"].startswith("spotify:track:") or not row["track_name"].strip():
                raise ValueError(f"Missing track identity near line {reader.line_num}")
            for field in ("peak_rank", "previous_rank", "weeks_on_chart", "streams"):
                int(row[field])
            ranks.add(rank)
        # Existing Spotify exports can have omitted ranks (e.g. 199 rows with
        # ranks 1..200 except 179). Gaps alone cannot establish truncation.
        if not ranks:
            raise ValueError(f"Empty chart: {path.name}")
        return len(ranks)
