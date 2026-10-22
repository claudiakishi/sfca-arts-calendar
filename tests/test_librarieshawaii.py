"""Unit tests for the Hawaiʻi State Public Libraries (The Events Calendar) source."""
from __future__ import annotations

import datetime as dt
import json
import itertools
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import fetch_and_build as fb  # noqa: E402

FIXTURE = HERE / "fixtures" / "librarieshawaii-events.json"


def load_fixture() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["events"]


def by_id(events: list[dict], event_id: int) -> dict:
    return next(e for e in events if e["id"] == event_id)


class ArtsFilterTest(unittest.TestCase):
    def setUp(self):
        self.raw = load_fixture()

    def test_keeps_arts_and_culture_programs(self):
        for eid in (267143, 282199, 284820, 288756, 900001):
            with self.subTest(eid=eid):
                self.assertTrue(fb.is_arts_event(by_id(self.raw, eid)))

    def test_drops_kids_games_and_plain_movie_screenings(self):
        # 10035715 Family Story Time (storytime-for-kids), 284786 Mahjong and
        # 283951 Chess (games), 10038425 Movie Monday and 282845 Keiki Movie
        # Monday (films only / keiki), 286762 craft (no arts category).
        for eid in (10035715, 284786, 283951, 10038425, 282845, 286762):
            with self.subTest(eid=eid):
                self.assertFalse(fb.is_arts_event(by_id(self.raw, eid)))

    def test_current_author_talk_and_legacy_archive_category_are_kept(self):
        for slug in ("author-talk", "author-reading"):
            ev = dict(by_id(self.raw, 267143), categories=[{"slug": slug}])
            self.assertTrue(fb.is_arts_event(ev))

    def test_keiki_title_is_excluded_even_with_arts_category(self):
        ev = dict(by_id(self.raw, 267143), title="Keiki Writing Circle")
        self.assertFalse(fb.is_arts_event(ev))


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.events = {e.uid_seed: e for e in fb.parse_librarieshawaii(load_fixture())}

    def get(self, eid: int, date: str) -> "fb.Event":
        return self.events[f"hspl|{eid}|{date}"]

    def test_only_arts_events_are_returned(self):
        ids = sorted(int(k.split("|")[1]) for k in self.events)
        self.assertEqual(ids, [267143, 282199, 284820, 288756, 900001, 900002])

    def test_timed_event_fields(self):
        ev = self.get(288756, "2026-10-05")
        self.assertEqual(ev.title, "Movie Monday – The Librarians (2025)")
        self.assertEqual(ev.provider, fb.LIB_PROVIDER)
        self.assertEqual(ev.date_start, "2026-10-05")
        self.assertEqual(ev.date_end, "2026-10-05")
        self.assertEqual(ev.time_display, "4:00 PM - 6:00 PM HST")
        self.assertEqual(ev.dt_start_utc, "2026-10-06T02:00:00Z")
        self.assertEqual(ev.dt_end_utc, "2026-10-06T04:00:00Z")
        self.assertEqual(ev.venue, "Mountain View Public Library (Hawai‘i)")
        self.assertEqual(ev.sources[0].name, fb.LIB_PROVIDER)
        self.assertTrue(ev.sources[0].url.startswith(
            "https://www.librarieshawaii.org/event/"))
        self.assertEqual(ev.links[0]["href"], ev.sources[0].url)

    def test_description_is_plain_text_without_boilerplate(self):
        desc = self.get(288756, "2026-10-05").description
        self.assertIn("Every Story Matters", desc)
        self.assertNotIn("<p>", desc)
        self.assertNotIn("auxiliary aid", desc)
        self.assertLessEqual(len(desc), fb.LIB_DESC_MAX + 1)

    def test_all_day_multi_day_event(self):
        ev = self.get(900001, "2026-10-10")
        self.assertEqual(ev.date_start, "2026-10-10")
        self.assertEqual(ev.date_end, "2026-10-24")
        self.assertIsNone(ev.time_display)
        self.assertIsNone(ev.dt_start_utc)
        self.assertEqual(ev.date_display, "October 10 – 24, 2026")

    def test_uid_is_stable(self):
        a = fb.parse_librarieshawaii(load_fixture())
        b = fb.parse_librarieshawaii(load_fixture())
        self.assertEqual([e.uid() for e in a], [e.uid() for e in b])

    def test_results_sorted_by_date(self):
        dates = [e.date_start for e in fb.parse_librarieshawaii(load_fixture())]
        self.assertEqual(dates, sorted(dates))


class MergeTest(unittest.TestCase):
    def test_other_source_cannot_bridge_distinct_library_sessions(self):
        ev = fb.parse_librarieshawaii([by_id(load_fixture(), 267143)])[0]
        later = fb.Event(**{**ev.__dict__, "uid_seed": ev.uid_seed + "|pm",
                            "dt_start_utc": "2026-10-06T03:00:00Z"})
        summary = fb.Event(**{**ev.__dict__, "provider": "SFCA",
                              "uid_seed": "sfca-summary"})
        for order in itertools.permutations([ev, later, summary]):
            with self.subTest(order=[e.uid_seed for e in order]):
                merged, num = fb.merge_events(list(order))
                self.assertEqual((len(merged), num), (2, 1))

    def test_two_library_sessions_on_same_day_stay_separate(self):
        ev = fb.parse_librarieshawaii([by_id(load_fixture(), 267143)])[0]
        later = fb.Event(**{**ev.__dict__, "uid_seed": ev.uid_seed + "|pm",
                            "dt_start_utc": "2026-10-06T03:00:00Z"})
        merged, num = fb.merge_events([ev, later])
        self.assertEqual((len(merged), num), (2, 0))


class UrlTest(unittest.TestCase):
    def test_query_targets_arts_categories_and_window(self):
        url = fb.tec_events_url(dt.date(2026, 10, 5), dt.date(2026, 12, 31), page=2)
        parsed = urlparse(url)
        self.assertEqual(parsed.netloc, "www.librarieshawaii.org")
        self.assertTrue(parsed.path.endswith("/wp-json/tribe/events/v1/events"))
        q = parse_qs(parsed.query)
        self.assertEqual(q["start_date"], ["2026-10-05"])
        self.assertEqual(q["end_date"], ["2026-12-31"])
        self.assertEqual(q["page"], ["2"])
        self.assertEqual(set(q["categories"][0].split(",")), set(fb.LIB_ARTS_CATEGORIES))

    def test_live_query_uses_current_author_category(self):
        # The public API rejects the retired author-reading slug (HTTP 400).
        query = parse_qs(urlparse(fb.tec_events_url(
            dt.date(2026, 10, 22), dt.date(2026, 12, 31))).query)
        self.assertIn("author-talk", query["categories"][0].split(","))
        self.assertNotIn("author-reading", query["categories"][0].split(","))

    def test_window_runs_to_end_of_sfca_horizon(self):
        start, end = fb.librarieshawaii_window(dt.date(2026, 11, 20))
        self.assertEqual(start, dt.date(2026, 11, 20))
        self.assertEqual(end, dt.date(2027, 1, 31))


class CollectOfflineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURE, self.tmp / f"{fb.LIB_SLUG}.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_offline_reads_archive_and_drops_past_events(self):
        with mock.patch.object(fb, "RAW_DIR", self.tmp):
            events, status = fb.collect_librarieshawaii(dt.date(2026, 10, 5), offline=True)
        self.assertEqual(status.status, "ok")
        titles = {e.title for e in events}
        self.assertNotIn("Writing Circle (past)", titles)
        self.assertIn("Lauhala Weaving Class", titles)
        self.assertEqual(len(events), 5)

    def test_offline_without_archive_is_an_error(self):
        empty = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(fb, "RAW_DIR", empty):
                events, status = fb.collect_librarieshawaii(dt.date(2026, 10, 5), offline=True)
        finally:
            shutil.rmtree(empty)
        self.assertEqual(events, [])
        self.assertEqual(status.status, "error")


if __name__ == "__main__":
    unittest.main()
