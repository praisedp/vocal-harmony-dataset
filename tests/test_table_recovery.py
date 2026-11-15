"""Deterministic timing checks for table refresh recovery; no browser or sleeps."""

import io
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
for _folder in ("scraper", "pipeline"):
    _path = str(PROJECT_DIR / _folder)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import chart_page


class TableRecoveryTests(unittest.TestCase):
    def make_page(self, *, ready_at=None, reload_seconds=0, first_reload_error=False):
        clock = [0.0]
        refreshes = []
        page = MagicMock()
        page.locator.return_value.is_visible.return_value = False
        page.is_closed.return_value = False
        page.url = chart_page.build_chart_url("2026-11-05")
        page.title.return_value = "Spotify Charts"
        page.goto.return_value.status = 200
        page.reload.return_value.status = 200

        def wait_for(**kwargs):
            end = clock[0] + kwargs["timeout"] / 1000
            if ready_at is not None and ready_at <= end:
                clock[0] = max(clock[0], ready_at)
                return
            clock[0] = end
            raise chart_page.PlaywrightTimeoutError("table not attached")

        def reload(**kwargs):
            refreshes.append(clock[0])
            allowed = kwargs["timeout"] / 1000
            clock[0] += min(reload_seconds, allowed)
            if reload_seconds >= allowed:
                raise chart_page.PlaywrightTimeoutError("reload timeout")
            if first_reload_error and len(refreshes) == 1:
                raise chart_page.PlaywrightError("net::ERR_INTERNET_DISCONNECTED")
            return page.reload.return_value

        page.locator.return_value.first.wait_for.side_effect = wait_for
        page.reload.side_effect = reload
        return page, clock, refreshes

    def run_recovery(self, **kwargs):
        page, clock, refreshes = self.make_page(**kwargs)
        with redirect_stdout(io.StringIO()), patch.object(chart_page.time, "monotonic", side_effect=lambda: clock[0]):
            result = chart_page.wait_for_chart_table(page, "table")
        return result, clock[0], refreshes, page

    def test_missing_table_gets_exactly_25_seconds_and_four_refreshes(self):
        result, elapsed, refreshes, _ = self.run_recovery()
        self.assertFalse(result[0])
        self.assertEqual(elapsed, 25)
        self.assertEqual(refreshes, [5, 10, 15, 20])

    def test_existing_table_returns_immediately_without_refreshing(self):
        result, elapsed, refreshes, _ = self.run_recovery(ready_at=0)
        self.assertTrue(result[0])
        self.assertEqual(elapsed, 0)
        self.assertEqual(refreshes, [])

    def test_recovery_stops_as_soon_as_table_appears_after_refresh(self):
        result, elapsed, refreshes, _ = self.run_recovery(ready_at=12)
        self.assertEqual(result, (True, 200))
        self.assertEqual(elapsed, 12)
        self.assertEqual(refreshes, [5, 10])

    def test_reload_timeouts_count_toward_the_same_deadline(self):
        result, elapsed, refreshes, page = self.run_recovery(reload_seconds=7)
        self.assertFalse(result[0])
        self.assertEqual(elapsed, 25)
        self.assertEqual(refreshes, [5, 10, 15, 20])
        self.assertTrue(all(call.kwargs["timeout"] <= 5000 for call in page.reload.call_args_list))

    def test_transient_refresh_network_error_can_recover(self):
        result, elapsed, refreshes, _ = self.run_recovery(
            ready_at=12, reload_seconds=1, first_reload_error=True)
        self.assertEqual(result, (True, 200))
        self.assertEqual(elapsed, 12)
        self.assertEqual(refreshes, [5, 10])

    def test_fresh_and_failed_navigation_both_get_25_second_table_window(self):
        for navigation_timeout in (25000, 5000):
            with self.subTest(navigation_timeout=navigation_timeout):
                page, clock, refreshes = self.make_page()
                with redirect_stdout(io.StringIO()), patch.object(chart_page.time, "monotonic", side_effect=lambda: clock[0]):
                    result = chart_page.open_chart_for_date(page, "2026-11-05", timeout_ms=navigation_timeout)
                self.assertFalse(result["table_ready"])
                self.assertEqual(clock[0], 25)
                self.assertEqual(refreshes, [5, 10, 15, 20])
                self.assertEqual(page.goto.call_args.kwargs["timeout"], navigation_timeout)

    def test_timed_out_initial_navigation_still_enters_table_recovery(self):
        page, clock, refreshes = self.make_page(ready_at=12)

        def failed_navigation(*args, **kwargs):
            clock[0] += 5
            raise chart_page.PlaywrightTimeoutError("initial navigation timeout")

        page.goto.side_effect = failed_navigation
        with redirect_stdout(io.StringIO()), patch.object(chart_page.time, "monotonic", side_effect=lambda: clock[0]):
            result = chart_page.open_chart_for_date(page, "2026-11-05", timeout_ms=5000)
        self.assertTrue(result["table_ready"])
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(refreshes, [10])
        self.assertEqual(clock[0], 12)

    def test_refresh_success_replaces_initial_error_http_status(self):
        page, clock, refreshes = self.make_page(ready_at=7)
        page.goto.return_value.status = 503
        with redirect_stdout(io.StringIO()), patch.object(chart_page.time, "monotonic", side_effect=lambda: clock[0]):
            result = chart_page.open_chart_for_date(page, "2026-11-05")
        self.assertTrue(result["table_ready"])
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(refreshes, [5])

    def test_explicit_unavailable_page_stops_immediately_without_refresh(self):
        page, clock, refreshes = self.make_page()
        page.locator.return_value.is_visible.return_value = True
        with redirect_stdout(io.StringIO()), patch.object(chart_page.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(chart_page.ChartUnavailableError):
                chart_page.open_chart_for_date(page, "2026-11-05")
        self.assertEqual(clock[0], 0)
        self.assertEqual(refreshes, [])

    def test_unavailable_message_appearing_during_wait_prevents_refresh(self):
        page, clock, refreshes = self.make_page(ready_at=2)
        page.locator.return_value.is_visible.side_effect = lambda: clock[0] >= 2
        with redirect_stdout(io.StringIO()), patch.object(chart_page.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(chart_page.ChartUnavailableError):
                chart_page.wait_for_chart_table(page, "table")
        self.assertEqual(clock[0], 2)
        self.assertEqual(refreshes, [])

    def test_stale_previous_country_error_does_not_create_a_cutoff(self):
        page, _, _ = self.make_page()
        page.url = chart_page.build_chart_url("2026-11-05", "regional-co-weekly")
        page.locator.return_value.is_visible.return_value = True
        page.goto.side_effect = chart_page.PlaywrightError("net::ERR_INTERNET_DISCONNECTED")
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as error:
                chart_page.open_chart_for_date(page, "2026-11-05", "regional-au-weekly")
        self.assertNotIsInstance(error.exception, chart_page.ChartUnavailableError)


if __name__ == "__main__":
    unittest.main()
