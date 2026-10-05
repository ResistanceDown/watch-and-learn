"""Run with: python3 -m unittest discover -s tests"""
import sys
import io
import json
from contextlib import redirect_stdout
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/watch/scripts"))
from download import download_url, fetch_captions, resolve_local
from transcribe import usable_whisper_segments
import watch


class LocalSidecarTest(unittest.TestCase):
    def test_sidecar_does_not_substitute_a_different_language(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mp4"
            video.touch()
            video.with_name("clip.en.vtt").touch()
            self.assertIsNone(resolve_local(str(video), caption_language="fr")["subtitle_path"])
            unlabelled = video.with_suffix(".vtt")
            unlabelled.touch()
            result = resolve_local(str(video), caption_language="fr")
            self.assertEqual(result["subtitle_path"], str(unlabelled.resolve()))
            self.assertIn("unverified", result["notes"][0])
            preferred = video.with_name("clip.FR-ca.vtt")
            preferred.touch()
            self.assertEqual(resolve_local(str(video), caption_language="fr")["subtitle_path"], str(preferred.resolve()))

    def test_matching_english_caption_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "video.mp4").touch()
            (root / "video.fr.vtt").touch()
            expected = root / "video.en.vtt"
            expected.touch()
            (root / "video-other.en.vtt").touch()
            self.assertEqual(resolve_local(str(root / "video.mp4"))["subtitle_path"], str(expected.resolve()))

    def test_stale_media_is_not_accepted_as_a_new_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / "video.mp4"
            stale.write_bytes(b"old video")
            with patch("download.shutil.which", return_value="yt-dlp"):
                with self.assertRaises(SystemExit):
                    fetch_captions("https://example.com/new", root)
                with self.assertRaises(SystemExit):
                    download_url("https://example.com/new", root)
            self.assertEqual(stale.read_bytes(), b"old video")
            stale.unlink()
            old_caption = root / "video.en.vtt"
            old_caption.write_text("old captions")
            with patch("download.shutil.which", return_value="yt-dlp"):
                with self.assertRaises(SystemExit):
                    download_url("https://example.com/new", root)
            self.assertEqual(old_caption.read_text(), "old captions")

    def test_watch_rejects_nonempty_work_directory_before_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "keep.txt"
            marker.write_text("old evidence")
            watch = Path(__file__).resolve().parents[1] / "skills/watch/scripts/watch.py"
            result = subprocess.run(
                [sys.executable, str(watch), "missing.mp4", "--out-dir", str(root)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be new or empty", result.stderr)
            self.assertEqual(marker.read_text(), "old evidence")

    def test_whisper_music_loop_is_not_reported_as_speech(self):
        segments = [
            {"text": "Today I will show you how to edit this video", "no_speech_prob": 0.01,
             "avg_logprob": -0.24},
            {"text": "2-3-4-4-4-4-4-4-4-4-4-4-4-4-4", "no_speech_prob": 0.58,
             "avg_logprob": -0.09},
            *[{"text": "Pt.", "no_speech_prob": 0.78, "avg_logprob": -0.48} for _ in range(5)],
        ]
        accepted, rejected = usable_whisper_segments(segments)
        self.assertEqual([segment["text"] for segment in accepted], [segments[0]["text"]])
        self.assertEqual(rejected, 6)

        repeated_speech = [
            {"text": "Click OK.", "no_speech_prob": 0.01, "avg_logprob": -0.2}
            for _ in range(4)
        ]
        self.assertEqual(usable_whisper_segments(repeated_speech), (repeated_speech, 0))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_long_uncaptioned_video_reaches_full_transcription(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "color=c=black:s=160x120:r=1:d=240", "-f", "lavfi", "-i",
                 "anullsrc=r=16000:cl=mono", "-t", "240", "-c:v", "mpeg4", "-c:a", "aac",
                 str(video)], check=True,
            )
            model = root / "model.pt"
            model.touch()
            data = {"segments": [{"start": 60, "end": 65, "text": "Useful instruction",
                                  "no_speech_prob": 0.01, "avg_logprob": -0.1}]}
            output = io.StringIO()
            with patch("sys.argv", ["watch", str(video), "--detail", "transcript",
                                    "--whisper-model", str(model), "--out-dir", str(root / "report")]), \
                 patch("watch.run_whisper", return_value=(data, None)) as reader, \
                 patch("watch.shutil.which", return_value="available"), redirect_stdout(output):
                self.assertEqual(watch.main(), 0)
            reader.assert_called_once()
            self.assertIn("[01:00] Useful instruction", output.getvalue())
            report = json.loads((root / "report/report.json").read_text())
            self.assertEqual(report["transcript"][0]["start"], 60)



if __name__ == "__main__":
    unittest.main()
