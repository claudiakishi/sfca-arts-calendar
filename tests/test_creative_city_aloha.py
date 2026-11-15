"""Unit tests for The Creative City and AlohaCalendar sources (offline fixtures)."""

import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import fetch_and_build as fb  # noqa: E402

TCC_FIXTURE = HERE / "fixtures" / "thecreativecity-events.json"
ALOHA_FIXTURE = HERE / "fixtures" / "alohacalendar-events.json"
TODAY = dt.date(2026, 11, 14)


def _by_title(events, needle):
    matches = [e for e in events if needle.lower() in e.title.lower()]
    assert len(matches) == 1, f"{needle!r}: {[e.title for e in events]}"
    return matches[0]


class ParseCreativeCityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        payload = json.loads(TCC_FIXTURE.read_text(encoding="utf-8"))
        cls.events = fb.parse_creative_city(payload, TODAY)

    def test_single_day_event_has_exact_hst_time(self):
        ev = _by_title(self.events, "Lucy Darling")
        self.assertEqual(ev.date_start, "2026-11-20")
        self.assertEqual(ev.time_display, "7:30 PM - 8:30 PM HST")
        self.assertEqual(ev.dt_start_utc, "2026-11-21T05:30:00Z")
        self.assertEqual(ev.venue, "Hawaii Theatre")
        self.assertEqual(ev.provider, "The Creative City")
        self.assertTrue(ev.sources[0].url.startswith(
            "https://www.thecreativecity.org/events/"))

    def test_multi_day_run_is_a_date_span_without_time(self):
        ev = _by_title(self.events, "Peter Pan Goes Wrong")
        self.assertEqual((ev.date_start, ev.date_end),
                         ("2026-11-12", "2026-11-29"))
        self.assertEqual(ev.date_display, "November 12 – 29, 2026")
        self.assertIsNone(ev.time_display)
        self.assertIsNone(ev.dt_start_utc)

    def test_long_running_series_kept_and_flagged_recurring(self):
        ev = _by_title(self.events, "HoMA Nights")
        self.assertLess(ev.date_start, TODAY.isoformat())
        self.assertTrue(ev.recurring)
        # Floats into the current month instead of its 2026-01 start month.
        self.assertEqual(ev.month_key("2026-11"), "2026-11")

    def test_ending_today_kept(self):
        ev = _by_title(self.events, "David Takagi")
        self.assertEqual(ev.date_end, "2026-11-14")

    def test_beyond_site_window_dropped(self):
        titles = [e.title for e in self.events]
        self.assertFalse(any("Shakespeare in Love" in t for t in titles))

    def test_missing_upcoming_raises(self):
        with self.assertRaises(KeyError):
            fb.parse_creative_city({"items": []}, TODAY)


class ParseAlohaCalendarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = json.loads(ALOHA_FIXTURE.read_text(encoding="utf-8"))["events"]
        cls.events = fb.parse_alohacalendar(raw, TODAY)

    def test_fake_z_times_are_read_as_local_wall_time(self):
        ev = _by_title(self.events, "Princeville")
        self.assertEqual(ev.date_start, "2026-11-17")
        self.assertEqual(ev.time_display, "5:00 PM - 6:30 PM HST")
        self.assertEqual(ev.dt_start_utc, "2026-11-18T03:00:00Z")
        self.assertEqual(ev.dt_end_utc, "2026-11-18T04:30:00Z")
        self.assertEqual(ev.venue, "Princeville Community Center (Kauai)")

    def test_repeat_showings_collapse_into_one_span(self):
        ev = _by_title(self.events, "Book of Mormon")
        self.assertTrue(ev.recurring)
        self.assertEqual((ev.date_start, ev.date_end),
                         ("2026-11-25", "2026-12-06"))
        self.assertTrue(ev.date_display.startswith("11 dates, "))
        self.assertIsNone(ev.dt_start_utc)

    def test_markets_are_dropped(self):
        self.assertFalse(any("Farmers Market" in e.title for e in self.events))

    def test_ticket_link_kept(self):
        ev = _by_title(self.events, "According to Coyote")
        self.assertEqual(ev.links[0]["text"], "Event details (AlohaCalendar)")
        self.assertIn("Tickets", [link["text"] for link in ev.links])

    def test_distinct_same_day_listings_never_self_merge(self):
        evs = [e for e in self.events if "UNDER/GROUND" in e.title]
        self.assertEqual(len(evs), 2)
        merged, n = fb.merge_events(evs)
        self.assertEqual((len(merged), n), (2, 0))

    def test_merges_with_another_source(self):
        aloha = _by_title(self.events, "According to Coyote")
        other = fb.Event(title="According to Coyote", description="",
                         category="Theatre", provider="The Creative City",
                         date_start=aloha.date_start)
        merged, n = fb.merge_events([aloha, other])
        self.assertEqual((len(merged), n), (1, 1))


class FetchAlohaPagesTest(unittest.TestCase):
    def _page(self, day, n, prefix):
        return {"events": [{"id": f"{prefix}{i}", "startDate": f"{day}T00:00:00.000Z"}
                           for i in range(n)]}

    def test_pages_forward_by_date_until_a_short_page(self):
        pages = [self._page("2026-11-20", 50, "a"),
                 self._page("2026-11-20", 10, "b")]
        with mock.patch.object(fb, "_fetch_json",
                               side_effect=[(200, p) for p in pages]) as fj:
            got = fb._fetch_aloha_pages(dt.date(2026, 11, 14),
                                        dt.date(2027, 1, 31))
        self.assertEqual(len(got), 60)
        self.assertIn("from=2026-11-20", fj.call_args_list[1].args[0])

    def test_full_page_stuck_on_one_day_steps_past_it(self):
        pages = [self._page("2026-11-14", 50, "a"),
                 self._page("2026-11-15", 3, "b")]
        with mock.patch.object(fb, "_fetch_json",
                               side_effect=[(200, p) for p in pages]) as fj:
            fb._fetch_aloha_pages(dt.date(2026, 11, 14), dt.date(2027, 1, 31))
        self.assertIn("from=2026-11-15", fj.call_args_list[1].args[0])

    def test_http_error_raises(self):
        with mock.patch.object(fb, "_fetch_json", return_value=(503, None)):
            with self.assertRaises(ValueError):
                fb._fetch_aloha_pages(dt.date(2026, 11, 14),
                                      dt.date(2027, 1, 31))


if __name__ == "__main__":
    unittest.main()
