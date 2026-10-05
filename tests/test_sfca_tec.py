"""Tests for the SFCA /events/ source (The Events Calendar REST API).

Fixture tests/fixtures/sfca_tec_events.json holds one real event captured from
https://sfca.hawaii.gov/wp-json/tribe/events/v1/events (the board meeting)
plus two synthetic events: an all-day multi-day event with a venue object and
HTML in its title/description, and one with hide_from_listings set.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_and_build as fb  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sfca_tec_events.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class ParseSfcaTecTest(unittest.TestCase):
    def setUp(self):
        self.events = fb.parse_sfca_tec(load_fixture())
        self.by_title = {e.title: e for e in self.events}

    def test_hidden_events_are_skipped(self):
        self.assertEqual(len(self.events), 2)
        self.assertNotIn("Hidden internal event", self.by_title)

    def test_timed_event_uses_utc_instants_and_hst_display(self):
        ev = self.by_title["SFCA Board of Commissioners general meeting (10/16)"]
        self.assertEqual(ev.provider, fb.SFCA_TEC_NAME)
        self.assertEqual(ev.date_start, "2026-10-16")
        self.assertEqual(ev.date_end, "2026-10-16")
        self.assertEqual(ev.dt_start_utc, "2026-10-16T21:00:00Z")
        self.assertEqual(ev.dt_end_utc, "2026-10-16T23:00:00Z")
        self.assertEqual(ev.date_display, "October 16, 2026")
        self.assertEqual(ev.time_display, "11:00 AM - 1:00 PM HST")
        self.assertEqual(ev.category, "Public Meeting")
        self.assertIsNone(ev.venue)
        self.assertEqual(ev.uid_seed, "sfca-tec|27815")
        url = ("https://sfca.hawaii.gov/event/"
               "sfca-board-of-commissioners-general-meeting-10-16/")
        self.assertEqual(ev.sources[0].url, url)
        self.assertEqual(ev.sources[0].name, fb.SFCA_TEC_NAME)
        self.assertEqual(ev.links[0]["href"], url)

    def test_all_day_event_is_dated_but_untimed(self):
        ev = self.by_title["Art in Public Places – Open House & Tour"]
        self.assertEqual(ev.date_start, "2026-11-06")
        self.assertEqual(ev.date_end, "2026-11-08")
        self.assertIsNone(ev.dt_start_utc)
        self.assertIsNone(ev.time_display)
        self.assertEqual(ev.date_display, "November 6 – 8, 2026")
        self.assertEqual(ev.venue, "No. 1 Capitol District Building")
        self.assertEqual(ev.category, "Community Event")

    def test_description_is_plain_text(self):
        ev = self.by_title["Art in Public Places – Open House & Tour"]
        self.assertEqual(ev.description, "Join the APP team for a tour. Free.")

    def test_event_website_becomes_extra_link(self):
        ev = self.by_title["Art in Public Places – Open House & Tour"]
        hrefs = [l["href"] for l in ev.links]
        self.assertIn("https://sfca.hawaii.gov/app/", hrefs)

    def test_empty_or_malformed_payload_yields_no_events(self):
        self.assertEqual(fb.parse_sfca_tec({}), [])
        self.assertEqual(fb.parse_sfca_tec({"events": [{"title": "x"}]}), [])

    def test_events_group_into_their_month(self):
        months, undated = fb.group_by_month(self.events, "2026-10")
        keys = [k for k, _, _ in months]
        self.assertEqual(keys, ["2026-10", "2026-11"])
        self.assertEqual(undated, [])


class CollectSfcaTecTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name)
        self._orig_raw = fb.RAW_DIR
        fb.RAW_DIR = self.raw

    def tearDown(self):
        fb.RAW_DIR = self._orig_raw
        self.tmp.cleanup()

    def test_offline_reads_archive(self):
        (self.raw / f"{fb.SFCA_TEC_SLUG}.json").write_text(
            FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
        events, ps = fb.collect_sfca_tec(offline=True)
        self.assertEqual(len(events), 2)
        self.assertEqual(ps.status, "ok")
        self.assertEqual(ps.name, fb.SFCA_TEC_NAME)

    def test_offline_without_archive_is_error_not_crash(self):
        events, ps = fb.collect_sfca_tec(offline=True)
        self.assertEqual(events, [])
        self.assertEqual(ps.status, "error")

    def test_online_follows_pagination_and_archives(self):
        page1 = load_fixture()
        page1["next_rest_url"] = fb.SFCA_TEC_API + "?page=2"
        page2 = {"events": [], "total": 3, "total_pages": 2}
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return 200, (page1 if len(calls) == 1 else page2)

        events, ps = fb.collect_sfca_tec(offline=False, fetch_json=fake_fetch)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], fb.SFCA_TEC_API + "?page=2")
        self.assertEqual(len(events), 2)
        self.assertEqual(ps.status, "ok")
        archived = json.loads(
            (self.raw / f"{fb.SFCA_TEC_SLUG}.json").read_text("utf-8"))
        self.assertEqual(len(archived["events"]), 3)

    def test_online_failure_falls_back_to_archive(self):
        (self.raw / f"{fb.SFCA_TEC_SLUG}.json").write_text(
            FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

        def fake_fetch(url):
            return 429, None

        events, ps = fb.collect_sfca_tec(offline=False, fetch_json=fake_fetch)
        self.assertEqual(len(events), 2)
        self.assertEqual(ps.status, "partial")

    def test_online_failure_without_archive_is_error(self):
        def fake_fetch(url):
            raise fb.requests.RequestException("boom")

        events, ps = fb.collect_sfca_tec(offline=False, fetch_json=fake_fetch)
        self.assertEqual(events, [])
        self.assertEqual(ps.status, "error")


if __name__ == "__main__":
    unittest.main()
