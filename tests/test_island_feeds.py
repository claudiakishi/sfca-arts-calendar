"""Unit tests for island assignment and the per-island ICS feeds."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import fetch_and_build as fb  # noqa: E402


def _ev(title="Show", provider="SFCA", venue=None, description="",
        date_start="2026-11-20"):
    return fb.Event(title=title, description=description, category="Arts",
                    provider=provider, venue=venue, date_start=date_start)


class AssignIslandTest(unittest.TestCase):
    def test_single_island_provider(self):
        self.assertEqual(fb.assign_island(_ev(provider="The Creative City")),
                         "oahu")
        self.assertEqual(fb.assign_island(_ev(provider="Volcano Art Center")),
                         "hawaii-island")

    def test_venue_parenthetical_beats_place_names(self):
        # "Waimea" is on two islands; "Hawai‘i State Library" is on Oʻahu.
        self.assertEqual(fb.assign_island(_ev(
            provider="Libraries", venue="Waimea Public Library (Kaua‘i)")),
            "kauai")
        self.assertEqual(fb.assign_island(_ev(
            provider="Libraries",
            venue="Hawai‘i State Library Public Library (O‘ahu)")), "oahu")
        self.assertEqual(fb.assign_island(_ev(
            provider="Libraries", venue="Pāhoa Public Library (Hawai‘i)")),
            "hawaii-island")
        self.assertEqual(fb.assign_island(_ev(
            provider="AlohaCalendar", venue="Imiloa (Big Island)")),
            "hawaii-island")
        self.assertEqual(fb.assign_island(_ev(
            provider="Libraries", venue="Lāna‘i Public Library (Lāna‘i)")),
            "maui-county")

    def test_place_names_in_prose(self):
        cases = {
            "“16th Annual Abstract Only!” at Wailoa Center": "hawaii-island",
            "“LUMINOSITY: The Legacy” at Viewpoints Gallery Maui": "maui-county",
            "Welcome Home Kōlea Festival at Ala Moana Beach Park": "oahu",
            "Concert at the Kailua-Kona pier": "hawaii-island",
        }
        for title, island in cases.items():
            with self.subTest(title=title):
                self.assertEqual(fb.assign_island(_ev(title=title)), island)

    def test_state_name_alone_is_not_an_island(self):
        self.assertIsNone(fb.assign_island(
            _ev(title="“Hawaiʻi’s Woodshow: Na Laʻau O Hawaiʻi”")))

    def test_two_counties_in_one_field_is_ambiguous(self):
        self.assertIsNone(fb.assign_island(
            _ev(title="Touring show: Hilo and Lihue")))


class IslandFeedsTest(unittest.TestCase):
    def test_writes_one_feed_per_island_with_only_its_events(self):
        evs = [_ev("Oahu show", provider="Capitol Modern"),
               _ev("Kona show", venue="Kona Library (Hawai‘i)"),
               _ev("Somewhere", venue=None)]
        for ev in evs:
            ev.island = fb.assign_island(ev)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(fb, "SITE_DIR", Path(tmp)):
            count = fb.write_ics(evs, "now")
            files = {p.name: p.read_text(encoding="utf-8")
                     for p in Path(tmp).iterdir()}
        self.assertEqual(count, 3)
        self.assertEqual(set(files), {"calendar.ics"} | {
            f"calendar-{k}.ics" for k in fb.ISLAND_FEEDS})
        self.assertIn("SUMMARY:Oahu show", files["calendar-oahu.ics"])
        self.assertNotIn("Kona show", files["calendar-oahu.ics"])
        self.assertIn("SUMMARY:Kona show", files["calendar-hawaii-island.ics"])
        self.assertNotIn("BEGIN:VEVENT", files["calendar-kauai.ics"])
        self.assertIn("X-WR-CALNAME:Hawaiʻi Arts & Culture – Kauaʻi",
                      files["calendar-kauai.ics"])
        # Unknown-island events are only in the statewide feed.
        self.assertIn("SUMMARY:Somewhere", files["calendar.ics"])
        self.assertFalse(any("Somewhere" in t for n, t in files.items()
                             if n != "calendar.ics"))


if __name__ == "__main__":
    unittest.main()
