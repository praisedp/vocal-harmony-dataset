"""Offline regression coverage for region isolation, resume, and download integrity."""

from contextlib import contextmanager
import csv
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
for _folder in ("scraper", "pipeline"):
    _path = str(PROJECT_DIR / _folder)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from rich.console import Console

from chart_csv import CHART_FIELDS, validate_chart_csv
import chart_page
from config import ScraperConfig
from date_utils import get_remaining_dates, load_completed_dates, load_failed_dates, save_dates
import downloader
import main
from regions import ALL_CHART_TYPES, GLOBAL_CHART, REGIONS, resolve_chart_types

DATES = ["2026-11-05", "2026-10-29", "2026-10-22"]


def write_chart(path, *, title="Song"):
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(rank="1", uri="spotify:track:example", artist_names="Artist",
               track_name=title, source="Label", peak_rank="1", previous_rank="-1",
               weeks_on_chart="1", streams="100")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted(CHART_FIELDS))
        writer.writeheader()
        writer.writerow(row)
    return path


class ScraperTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = ScraperConfig(
            start_date=DATES[0], stop_date=DATES[-1],
            downloads_dir=self.root / "downloads", state_dir=self.root / "state",
            completed_file=self.root / "completed.txt", failed_file=self.root / "failed.txt",
            profile_dir=self.root / "profile", failure_retry_wait_seconds=0,
            page_load_wait_seconds=0,
        )
        console = patch.object(main, "console", Console(file=io.StringIO()))
        console.start()
        self.addCleanup(console.stop)

    def csv_path(self, config, date):
        return config.downloads_dir / f"{config.chart_type}-{date}.csv"

    def fake_download(self, **kwargs):
        path = kwargs["output_dir"] / f'{kwargs["chart_type"]}-{kwargs["date_str"]}.csv'
        return {}, write_chart(path)

    def test_all_dropdown_locations_and_global_first(self):
        codes = "global ar au at by be bo br bg ca cl co cr cy cz dk do ec eg sv ee fi fr de gr gt hn hk hu is in id ie il it jp kz lv lt lu my mx ma nl nz ni ng no pk pa py pe ph pl pt ro sa sg sk za kr es se ch tw th tr ae ua gb uy us ve vn".split()
        self.assertEqual(list(REGIONS), codes)
        self.assertEqual(len(ALL_CHART_TYPES), 74)
        self.assertEqual(resolve_chart_types("all"), ALL_CHART_TYPES)
        self.assertEqual(resolve_chart_types(["co", "global", "au", "co"]),
                         (GLOBAL_CHART, "regional-co-weekly", "regional-au-weekly"))
        with self.assertRaises(ValueError):
            resolve_chart_types("../../unsafe")

    def test_config_all_and_legacy_single_chart(self):
        path = self.root / "config.json"
        path.write_text('{"chart_types":"all"}')
        config = ScraperConfig.load(path)
        self.assertEqual(config.chart_types, ALL_CHART_TYPES)
        self.assertEqual(config.downloads_dir, (self.root / "downloads").resolve())
        path.write_text('{"chart_type":"regional-co-weekly"}')
        self.assertEqual(ScraperConfig.load(path).chart_types, ("regional-co-weekly",))
        path.write_text('{broken')
        with self.assertRaises(ValueError):
            ScraperConfig.load(path)

    def test_global_ledgers_preserved_and_country_ledgers_isolated(self):
        global_config = main.config_for_region(self.config, GLOBAL_CHART)
        co = main.config_for_region(self.config, "regional-co-weekly")
        au = main.config_for_region(self.config, "regional-au-weekly")
        self.assertEqual(global_config.completed_file, self.config.completed_file)
        self.assertEqual(co.failed_file, self.root / "state/regional-co-weekly/failed.txt")
        save_dates(global_config.completed_file, DATES)
        self.assertEqual(load_completed_dates(co.completed_file), [])
        self.assertNotEqual(co.completed_file, au.completed_file)

    def test_resume_fills_gaps_before_oldest_completed_and_newer_scope(self):
        save_dates(self.config.completed_file, [DATES[-1]])
        remaining, _, _ = get_remaining_dates(DATES[0], DATES[-1], self.config.completed_file)
        self.assertEqual(remaining, DATES[:2])

    def test_reconcile_recovers_saved_csv_and_requeues_missing_or_corrupt_files(self):
        save_dates(self.config.completed_file, [DATES[0], DATES[-1]])
        write_chart(self.csv_path(self.config, DATES[0]))
        write_chart(self.csv_path(self.config, DATES[1]))
        self.csv_path(self.config, DATES[-1]).write_text("not,a,chart\n")
        plan = main.prepare_region(self.config)
        self.assertEqual(plan.completed, set(DATES[:2]))
        self.assertEqual(plan.retries, [DATES[-1]])
        self.assertEqual(set(load_completed_dates(self.config.completed_file)), set(DATES[:2]))
        self.assertEqual(load_failed_dates(self.config.failed_file), [DATES[-1]])

    def test_failed_dates_outside_range_are_preserved_but_not_requested(self):
        save_dates(self.config.failed_file, ["2016-01-01", DATES[1]])
        plan = main.prepare_region(self.config)
        self.assertEqual(plan.retries, [DATES[1]])
        self.assertEqual(plan.remaining, [DATES[0], DATES[-1]])
        self.assertIn("2016-01-01", load_failed_dates(self.config.failed_file))

    def test_each_country_runs_after_global_even_when_a_global_week_fails(self):
        self.config.chart_types = resolve_chart_types(["ar", "global", "au"])
        calls = []

        def download(**kwargs):
            calls.append((kwargs["chart_type"], kwargs["date_str"]))
            if calls[-1] == (GLOBAL_CHART, DATES[1]):
                raise RuntimeError("unavailable week")
            return self.fake_download(**kwargs)

        @contextmanager
        def browser(**kwargs):
            yield None, object()

        with patch.object(main, "get_browser_session", side_effect=browser) as session:
            with patch.object(main, "open_and_download_chart", side_effect=download):
                plans = main.run_batch_downloader(self.config)
        session.assert_called_once()
        self.assertEqual([chart for chart, date in calls],
                         [GLOBAL_CHART] * 3 + ["regional-ar-weekly"] * 3 + ["regional-au-weekly"] * 3)
        self.assertEqual([plan.outstanding for plan in plans], [1, 0, 0])
        self.assertEqual(load_failed_dates(self.config.failed_file), [DATES[1]])
        self.assertEqual(load_failed_dates(main.config_for_region(self.config, "regional-au-weekly").failed_file), [])

    def test_newest_first_with_retry_timeouts_and_no_repeat_downloads(self):
        save_dates(self.config.failed_file, [DATES[1]])
        plan = main.prepare_region(self.config)
        with patch.object(main, "open_and_download_chart", side_effect=self.fake_download) as download:
            result = main.download_region(object(), plan)
        self.assertEqual([call.kwargs["date_str"] for call in download.call_args_list],
                         DATES)
        self.assertEqual([call.kwargs["page_timeout_ms"] for call in download.call_args_list],
                         [25000, 5000, 25000])
        self.assertEqual(result.outstanding, 0)
        self.assertEqual(load_failed_dates(self.config.failed_file), [])
        with patch.object(main, "get_browser_session") as session:
            plans = main.run_batch_downloader(self.config)
        session.assert_not_called()
        self.assertEqual(plans[0].outstanding, 0)

    def test_ctrl_c_keeps_inflight_date_failed_and_does_not_mark_completed(self):
        plan = main.prepare_region(self.config)
        with patch.object(main, "open_and_download_chart", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.download_region(object(), plan)
        self.assertEqual(load_failed_dates(self.config.failed_file), [DATES[0]])
        self.assertEqual(load_completed_dates(self.config.completed_file), [])

    def test_interrupt_after_csv_save_is_recovered_without_redownload(self):
        def save_then_interrupt(**kwargs):
            self.fake_download(**kwargs)
            raise KeyboardInterrupt
        with patch.object(main, "open_and_download_chart", side_effect=save_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.download_region(object(), main.prepare_region(self.config))
        plan = main.prepare_region(self.config)
        self.assertIn(DATES[0], plan.completed)
        self.assertNotIn(DATES[0], plan.retries + plan.remaining)
        self.assertEqual(load_failed_dates(self.config.failed_file), [])

    def test_read_only_status_does_not_launch_browser_or_change_state(self):
        save_dates(self.config.completed_file, DATES)
        before = self.config.completed_file.read_bytes()
        with patch.object(main, "get_browser_session") as session:
            plans = main.run_batch_downloader(self.config, status_only=True)
        session.assert_not_called()
        self.assertEqual(self.config.completed_file.read_bytes(), before)
        self.assertFalse(self.config.failed_file.exists())
        self.assertEqual(plans[0].outstanding, 3)

    def test_bad_download_is_never_marked_completed(self):
        def download(**kwargs):
            path = self.csv_path(self.config, kwargs["date_str"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<html>error</html>")
            return {}, path
        with patch.object(main, "open_and_download_chart", side_effect=download):
            result = main.download_region(object(), main.prepare_region(self.config))
        self.assertEqual(result.outstanding, 3)
        self.assertEqual(load_completed_dates(self.config.completed_file), [])
        self.assertEqual(set(load_failed_dates(self.config.failed_file)), set(DATES))

    def test_unavailable_country_stops_at_boundary_and_next_country_still_runs(self):
        self.config.chart_types = resolve_chart_types(["co", "au"])
        calls = []

        def download(**kwargs):
            calls.append((kwargs["chart_type"], kwargs["date_str"]))
            if calls[-1] == ("regional-co-weekly", DATES[1]):
                raise chart_page.ChartUnavailableError("Couldn't find this chart")
            return self.fake_download(**kwargs)

        @contextmanager
        def browser(**kwargs):
            yield None, object()

        with patch.object(main, "get_browser_session", side_effect=browser):
            with patch.object(main, "open_and_download_chart", side_effect=download):
                plans = main.run_batch_downloader(self.config)
        self.assertEqual(calls, [("regional-co-weekly", DATES[0]), ("regional-co-weekly", DATES[1])]
                         + [("regional-au-weekly", date) for date in DATES])
        self.assertEqual(plans[0].skipped, set(DATES[1:]))
        self.assertEqual(plans[0].completed, {DATES[0]})
        self.assertEqual(plans[0].outstanding, 0)
        co = main.config_for_region(self.config, "regional-co-weekly")
        self.assertEqual(main.load_cutoff(co), DATES[1])
        self.assertEqual(load_failed_dates(co.failed_file), [])

        # A persisted boundary survives a rerun; no older dates are regenerated.
        with patch.object(main, "get_browser_session") as session:
            with patch.object(main, "open_and_download_chart") as download:
                main.run_batch_downloader(self.config)
        session.assert_not_called()
        download.assert_not_called()

    def test_saved_cutoff_filters_older_failures_but_preserves_valid_older_csvs(self):
        save_dates(main.cutoff_file(self.config), [DATES[1]])
        save_dates(self.config.failed_file, DATES)
        write_chart(self.csv_path(self.config, DATES[-1]))
        plan = main.prepare_region(self.config)
        self.assertEqual(plan.completed, {DATES[-1]})
        self.assertEqual(plan.skipped, {DATES[1]})
        self.assertEqual(plan.retries, [DATES[0]])
        self.assertEqual(plan.outstanding, 1)
        self.assertIn(DATES[1], load_failed_dates(self.config.failed_file))

    def test_network_failures_never_create_availability_cutoffs(self):
        with patch.object(main, "open_and_download_chart", side_effect=RuntimeError("network timeout")):
            result = main.download_region(object(), main.prepare_region(self.config))
        self.assertIsNone(result.cutoff)
        self.assertFalse(main.cutoff_file(self.config).exists())
        self.assertEqual(result.outstanding, 3)

    def test_downloader_atomically_publishes_valid_csv_and_preserves_old_file_on_error(self):
        page = MagicMock()
        download = MagicMock()
        download.suggested_filename = f"{GLOBAL_CHART}-{DATES[0]}.csv"
        page.expect_download.return_value.__enter__.return_value.value = download
        target = self.csv_path(self.config, DATES[0])
        write_chart(target, title="Original")
        before = target.read_bytes()
        download.save_as.side_effect = lambda path: Path(path).write_text("not a csv")
        with patch.object(downloader, "find_download_button"):
            with self.assertRaises(ValueError):
                downloader.download_chart_csv(page, target.name, target.parent)
            self.assertEqual(target.read_bytes(), before)
            download.save_as.side_effect = lambda path: write_chart(Path(path), title="Updated")
            self.assertEqual(downloader.download_chart_csv(page, target.name, target.parent), target)
        self.assertEqual(validate_chart_csv(target), 1)
        self.assertNotEqual(target.read_bytes(), before)
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_wrong_server_chart_filename_is_rejected(self):
        page = MagicMock()
        download = MagicMock()
        download.suggested_filename = f"regional-co-weekly-{DATES[0]}.csv"
        page.expect_download.return_value.__enter__.return_value.value = download
        with patch.object(downloader, "find_download_button"):
            with self.assertRaises(ValueError):
                downloader.download_chart_csv(page, f"{GLOBAL_CHART}-{DATES[0]}.csv", self.config.downloads_dir)
        download.save_as.assert_not_called()

    def test_short_chart_with_rank_gaps_is_accepted_but_duplicate_ranks_are_rejected(self):
        path = write_chart(self.root / "chart.csv")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows.append({**rows[0], "rank": "3", "uri": "spotify:track:other"})
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=sorted(CHART_FIELDS))
            writer.writeheader()
            writer.writerows(rows)
        self.assertEqual(validate_chart_csv(path), 2)
        with path.open("a", newline="") as handle:
            csv.DictWriter(handle, fieldnames=sorted(CHART_FIELDS)).writerow(rows[0])
        with self.assertRaises(ValueError):
            validate_chart_csv(path)

    def test_redirect_or_missing_table_is_rejected_before_download(self):
        status = dict(url=f"https://charts.spotify.com/charts/view/{GLOBAL_CHART}/{DATES[1]}",
                      http_status=200, table_ready=True)
        with patch.object(chart_page, "open_chart_for_date", return_value=status):
            with patch.object(chart_page, "download_chart_csv") as download:
                with self.assertRaises(RuntimeError):
                    chart_page.open_and_download_chart(object(), DATES[0], wait_seconds=0)
                download.assert_not_called()
        status["url"] = chart_page.build_chart_url(DATES[0])
        status["table_ready"] = False
        with patch.object(chart_page, "open_chart_for_date", return_value=status):
            with self.assertRaises(RuntimeError):
                chart_page.open_and_download_chart(object(), DATES[0], wait_seconds=0)


if __name__ == "__main__":
    unittest.main()
