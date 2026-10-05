"""Unit tests for the Volcano Art Center RSS source (offline fixture)."""

import datetime as dt
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import fetch_and_build as fb  # noqa: E402

FIXTURE = HERE / "fixtures" / "volcanoartcenter-feed.xml"
TODAY = dt.date(2026, 10, 5)


def _by_title(events, needle):
    matches = [e for e in events if needle.lower() in e.title.lower()]
    assert len(matches) == 1, f"{needle!r}: {[e.title for e in events]}"
    return matches[0]


class ParseVolcanoArtCenterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = fb.parse_volcano_art_center(
            FIXTURE.read_text(encoding="utf-8"), TODAY)

    def test_single_date_from_title_with_ordinal(self):
        ev = _by_title(self.events, "Bob Dylan")
        self.assertEqual(ev.date_start, "2026-10-10")
        self.assertEqual(ev.date_end, "2026-10-10")
        self.assertEqual(ev.provider, "Volcano Art Center")
        self.assertEqual(ev.category, "Concert")
        self.assertEqual(ev.sources[0].url,
                         "https://volcanoartcenter.org/"
                         "concert-a-tribute-to-bob-dylan-by-the-ing-crowd/")
        self.assertEqual(ev.uid_seed, "vac|88080")

    def test_range_from_description_using_through(self):
        ev = _by_title(self.events, "The Shape of Water")
        self.assertEqual(ev.date_start, "2026-10-03")
        self.assertEqual(ev.date_end, "2026-11-15")
        self.assertEqual(ev.category, "Exhibits")
        self.assertIn("Hawaiʻi Volcanoes National Park", ev.venue)

    def test_range_in_title_with_html_entities(self):
        ev = _by_title(self.events, "MOKUHANGA")
        self.assertEqual(ev.date_start, "2026-10-24")
        self.assertEqual(ev.date_end, "2026-11-14")
        self.assertIn("–", ev.title)  # &#8211; decoded
        self.assertNotIn("&#", ev.title)

    def test_year_rolls_forward_past_pubdate(self):
        # Posted July 2026 for a January-February workshop -> 2027.
        ev = _by_title(self.events, "Stained Glass")
        self.assertEqual(ev.date_start, "2027-01-23")
        self.assertEqual(ev.date_end, "2027-02-13")

    def test_still_running_range_is_kept(self):
        ev = _by_title(self.events, "Handbuilding with Clay")
        self.assertEqual(ev.date_start, "2026-09-06")
        self.assertEqual(ev.date_end, "2026-10-25")

    def test_undated_and_past_posts_are_dropped(self):
        titles = " ".join(e.title for e in self.events)
        self.assertNotIn("Worth Of Water", titles)  # 2023 post
        for ev in self.events:
            self.assertIsNotNone(ev.date_start, ev.title)
            self.assertGreaterEqual(ev.date_end, TODAY.isoformat(), ev.title)

    def test_description_strips_wordpress_boilerplate(self):
        ev = _by_title(self.events, "Bob Dylan")
        self.assertNotIn("appeared first on", ev.description)
        self.assertNotIn("<p>", ev.description)
        self.assertTrue(ev.description.startswith("You are invited"))

    def test_niaulani_venue(self):
        ev = _by_title(self.events, "VAC Voices")
        self.assertEqual(ev.date_start, "2026-10-08")
        self.assertIn("Niaulani", ev.venue)

    def test_sorted_by_date(self):
        starts = [e.date_start for e in self.events]
        self.assertEqual(starts, sorted(starts))

    def test_malformed_feed_raises(self):
        with self.assertRaises(Exception):
            fb.parse_volcano_art_center("<html>not a feed", TODAY)

    def test_empty_channel_returns_no_events(self):
        xml = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        self.assertEqual(fb.parse_volcano_art_center(xml, TODAY), [])


class NormalizeDateTextTest(unittest.TestCase):
    def test_ordinals_and_through(self):
        self.assertEqual(
            fb._vac_normalize_dates("Saturdays October 24th through November 14th"),
            "Saturdays October 24 - November 14")


if __name__ == "__main__":
    unittest.main()
