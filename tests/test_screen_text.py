"""Run with: python3 -m unittest discover -s tests -p test_screen_text.py"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/watch/scripts"))

from screen_text import read_screen_text


@unittest.skipUnless(all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "tesseract")),
                     "FFmpeg and Tesseract required")
class ScreenTextTest(unittest.TestCase):
    def test_text_across_ocr_chunks_is_retained(self):
        with tempfile.TemporaryDirectory() as output:
            items = read_screen_text(str(ROOT / "tests/screen-text.mp4"), Path(output),
                                     None, None, duration=4, fps=2, max_frames=4)
            self.assertEqual([item["text"] for item in items], ["FRAME ONE", "FRAME TWO"])
            self.assertEqual([round(item["timestamp_seconds"]) for item in items], [0, 2])
            self.assertTrue(all(Path(item["frame"]).exists() for item in items))

    def test_changes_in_visible_text_are_timestamped(self):
        with tempfile.TemporaryDirectory() as output:
            subprocess.run([sys.executable, str(ROOT / "skills/watch/scripts/watch.py"),
                            str(ROOT / "tests/screen-text.mp4"), "--screen-text",
                            "--detail", "transcript", "--out-dir", output],
                           check=True, capture_output=True, text=True)
            items = json.loads((Path(output) / "screen-text.json").read_text())
        self.assertEqual([item["text"] for item in items], ["FRAME ONE", "FRAME TWO"])
        self.assertEqual([round(item["timestamp_seconds"]) for item in items], [0, 2])

    def test_interrupted_ocr_keeps_completed_batch(self):
        def ocr(frame, language):
            if frame["timestamp_seconds"] >= 2:
                raise RuntimeError("interrupted")
            return "FIRST BATCH"

        with tempfile.TemporaryDirectory() as output:
            with patch("screen_text._ocr_frame", side_effect=ocr):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    read_screen_text(str(ROOT / "tests/screen-text.mp4"), Path(output),
                                     None, None, duration=4, fps=2, max_frames=4)
            saved = json.loads((Path(output) / "screen-text.json").read_text())
            self.assertEqual([item["text"] for item in saved], ["FIRST BATCH"])

    def test_direct_frame_export_preserves_nonempty_output(self):
        with tempfile.TemporaryDirectory() as output:
            marker = Path(output) / "frame_0001.jpg"
            marker.write_bytes(b"prior evidence")
            result = subprocess.run(
                [sys.executable, str(ROOT / "skills/watch/scripts/frames.py"),
                 str(ROOT / "tests/screen-text.mp4"), output],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be new or empty", result.stderr)
            self.assertEqual(marker.read_bytes(), b"prior evidence")
