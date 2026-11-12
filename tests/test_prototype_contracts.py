"""Exercise the offline handoff examples, without private runtime dependencies."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import wave

from examples.prototype_contracts import FIXTURES, sample_decision, write_synthetic_song


class PrototypeContractTests(unittest.TestCase):
    def test_sample_decision_contains_independent_rows_and_legal_cells(self):
        decision = sample_decision()
        self.assertEqual(len(decision.legal_actions), 25)
        for action in decision.legal_actions:
            self.assertEqual(decision.state["board"][action["row"]][action["col"]], 0)
        decision.state["board"][0][0] = 1
        self.assertEqual(decision.state["board"][1][0], 0)

    def test_drawing_fixture_uses_pixel_coordinates_within_declared_rect(self):
        drawing = json.loads((FIXTURES / "drawing.json").read_text(encoding="utf-8"))
        x0, y0, x1, y1 = drawing["draw_rect"]
        self.assertLessEqual(x1, drawing["canvas_width"])
        self.assertLessEqual(y1, drawing["canvas_height"])
        for stroke in drawing["strokes"]:
            self.assertGreaterEqual(len(stroke), 2)
            for x, y in stroke:
                self.assertTrue(x0 <= x <= x1 and y0 <= y <= y1)

    def test_synthetic_song_has_aligned_tracks_but_is_not_enabled(self):
        with tempfile.TemporaryDirectory() as folder:
            song = write_synthetic_song(Path(folder))
            self.assertFalse(song.enabled)
            for path in (song.assets.master, song.assets.vocal, song.assets.instrumental):
                with wave.open(str(path), "rb") as audio:
                    self.assertEqual((audio.getframerate(), audio.getnframes(),
                                      audio.getnchannels(), audio.getsampwidth()), (24000, 24000, 1, 2))
            self.assertTrue(song.assets.lyrics.is_file())
            self.assertFalse(song.assets.translation.exists())  # optional for single subtitles
            rows = json.loads((Path(folder) / "singing_catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(rows["songs"][0]["id"], song.id)

    def test_cli_works_without_site_packages(self):
        result = subprocess.run([sys.executable, "-X", "utf8", "-S", "-m",
                                 "examples.prototype_contracts"],
                                cwd=Path(__file__).resolve().parents[1],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Contract examples complete", result.stdout)

    def test_existing_catalogue_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = Path(folder) / "singing_catalog.json"
            catalog.write_text("keep existing catalogue", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty output directory"):
                write_synthetic_song(Path(folder))
            self.assertEqual(catalog.read_text(encoding="utf-8"), "keep existing catalogue")
            self.assertEqual(list(Path(folder).iterdir()), [catalog])


if __name__ == "__main__":
    unittest.main()
