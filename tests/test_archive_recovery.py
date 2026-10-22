"""A failed archive refresh must preserve the last complete snapshot."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import fetch_and_build as fb


class ArchiveRecoveryTest(unittest.TestCase):
    def test_partial_write_preserves_archive_and_fallback_for_both_sources(self):
        cases = [
            (fb.LIB_SLUG + ".json", fb.collect_librarieshawaii),
            (fb.VAC_SLUG + ".xml", fb.collect_volcano_art_center),
        ]
        write_text = Path.write_text
        for filename, collect in cases:
            with self.subTest(source=filename), tempfile.TemporaryDirectory() as tmp:
                raw_dir = Path(tmp)
                archive = raw_dir / filename
                before = (HERE / "fixtures" / filename).read_text(encoding="utf-8")
                archive.write_text(before, encoding="utf-8")

                def fail_after_partial_write(path, text, **kwargs):
                    write_text(path, text[:12], **kwargs)
                    raise OSError("disk full after partial write")

                raw = (json.loads(before)["events"] if filename.endswith("json") else [])
                with mock.patch.object(fb, "RAW_DIR", raw_dir), mock.patch.object(
                        fb, "_fetch_tec_pages", return_value=raw), mock.patch.object(
                        fb, "fetch", return_value=(200, before)), mock.patch.object(
                        Path, "write_text", fail_after_partial_write):
                    events, status = collect(dt.date(2026, 10, 5), False)
                self.assertEqual(status.status, "partial")
                self.assertGreater(len(events), 0)
                self.assertEqual(archive.read_text(encoding="utf-8"), before)
                with mock.patch.object(fb, "RAW_DIR", raw_dir):
                    offline_events, offline_status = collect(dt.date(2026, 10, 5), True)
                self.assertEqual(offline_status.status, "ok")
                self.assertEqual(len(offline_events), len(events))
                self.assertEqual(list(raw_dir.iterdir()), [archive])
