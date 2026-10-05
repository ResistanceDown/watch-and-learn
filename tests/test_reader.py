"""Portable regression checks; all network and Whisper calls are mocked."""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/watch/scripts"))
import watch
from download import _pick_video, fetch_captions, resolve_local
from frames import extract, extract_at_timestamps, parse_time
from transcribe import filter_range, parse_vtt


class CaptionTest(unittest.TestCase):
    def test_short_timestamps_entities_settings_comments_and_repeated_speech(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captions.vtt"
            path.write_text("\ufeffWEBVTT\n\nNOTE an ignored example\n"
                            "00:00.000 --> 00:01.000\nIgnore me\n\n"
                            "1\n00:01.000 --> 00:02.000 align:start\n<c>Fish &amp; chips</c>\n\n"
                            "00:02.000 --> 00:03.000\nFish &amp; chips\n\n"
                            "00:05.000 --> 00:06.000\nFish &amp; chips\n\n"
                            "01:00:00.000 --> 01:00:01.000\nLater\n", encoding="utf-8")
            segments = parse_vtt(str(path))
        self.assertEqual([(s["start"], s["end"], s["text"]) for s in segments],
                         [(1, 3, "Fish & chips"), (5, 6, "Fish & chips"), (3600, 3601, "Later")])

    def test_range_excludes_touching_cues(self):
        cues = [{"start": 0, "end": 1}, {"start": 1, "end": 2}, {"start": 2, "end": 3}]
        self.assertEqual(filter_range(cues, 1, 2), [cues[1]])

    def test_selected_caption_language_is_preferred(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "video.mp4").touch()
            (root / "video.en.vtt").touch()
            (root / "video.fr.vtt").touch()
            picked = resolve_local(str(root / "video.mp4"), caption_language="fr")
            self.assertEqual(Path(picked["subtitle_path"]).name, "video.fr.vtt")

    def test_download_is_isolated_and_language_reaches_yt_dlp(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("download.shutil.which", return_value="yt-dlp"), \
             patch("download.subprocess.run") as run:
            fetch_captions("https://example.com/video", Path(directory), caption_language="fr")
        cmd = run.call_args.args[0]
        self.assertIn("--ignore-config", cmd)
        self.assertIn("--no-plugin-dirs", cmd)
        self.assertEqual(cmd[cmd.index("--sub-langs") + 1], "fr.*")
        self.assertEqual(cmd[cmd.index("--playlist-items") + 1], "1")
        self.assertIsNotNone(run.call_args.kwargs.get("timeout"))

    def test_empty_or_partial_download_is_not_video_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "video.mp4").touch()
            (root / "video.webm.part").write_bytes(b"partial")
            (root / "video.f136.mp4").write_bytes(b"unmerged video stream")
            self.assertIsNone(_pick_video(root))


class ReaderTest(unittest.TestCase):
    def test_invalid_times_are_rejected(self):
        for value in ("nan", "inf", "-1", "1:60", "1:00:99", "1.5:00", "0:-1", "1e308:00", "oops"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                parse_time(value)
        self.assertEqual(parse_time("1:02:03.5"), 3723.5)

    def test_bad_flags_fail_before_network_or_output_creation(self):
        cases = [("--end", "0"), ("--end", "-1"), ("--fps", "nan"), ("--fps", "0"),
                 ("--resolution", "0"), ("--timestamps", "nan"), ("--max-frames", "0"),
                 ("--caption-language", "all,-live_chat"), ("--ocr-language", "../eng"),
                 ("--start", "1e308:00")]
        with tempfile.TemporaryDirectory() as directory:
            for flags in cases:
                dest = Path(directory) / "unused"
                with self.subTest(flags=flags), patch("watch.fetch_captions") as fetch, \
                     patch("sys.argv", ["watch", "https://example.com/video", *flags, "--out-dir", str(dest)]), \
                     redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    watch.main()
                fetch.assert_not_called()
                self.assertFalse(dest.exists())

    def test_caption_only_url_does_not_require_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitle = root / "captions.vtt"
            subtitle.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nHello\n", encoding="utf-8")
            evidence = dict(subtitle_path=str(subtitle), info={"duration": 2}, video_path=None)
            out = root / "evidence"
            with patch("sys.argv", ["watch", "https://example.com/video", "--detail", "transcript",
                                    "--out-dir", str(out)]), patch("watch.fetch_captions", return_value=evidence), \
                 patch("watch.download") as download, patch("watch.get_metadata") as probe, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(watch.main(), 0)
            download.assert_not_called()
            probe.assert_not_called()
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["transcript"][0]["text"], "Hello")
            self.assertTrue((out / "report.md").is_file())

    def test_failed_media_download_preserves_caption_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitle = root / "captions.vtt"
            subtitle.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nHello\n", encoding="utf-8")
            evidence = dict(subtitle_path=str(subtitle), info={"duration": 2}, video_path=None)
            out = root / "evidence"
            with patch("sys.argv", ["watch", "https://example.com/video", "--screen-text", "--out-dir", str(out)]), \
                 patch("watch.fetch_captions", return_value=evidence), \
                 patch("watch.download", side_effect=SystemExit("network unavailable")), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(watch.main(), 2)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["transcript"][0]["text"], "Hello")
            self.assertTrue(report["errors"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_empty_transcript_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "skills/watch/scripts/watch.py"),
                                     str(ROOT / "tests/screen-text.mp4"), "--detail", "transcript",
                                     "--no-whisper", "--out-dir", directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            report = json.loads((Path(directory) / "report.json").read_text())
            self.assertEqual(report["status"], "partial")
            self.assertFalse(report["transcript"])

    @unittest.skipUnless(all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "tesseract")), "Media tools required")
    def test_focused_ocr_is_clamped_and_images_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "skills/watch/scripts/watch.py"),
                                     str(ROOT / "tests/screen-text.mp4"), "--screen-text", "--start", "2",
                                     "--end", "100", "--no-whisper", "--out-dir", directory],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((Path(directory) / "report.json").read_text())
            self.assertEqual(report["range"], {"start": 2, "end": 4})
            self.assertEqual([s["text"] for s in report["screen_text"]], ["FRAME TWO"])
            self.assertTrue(all(2 <= f["timestamp_seconds"] < 4 and Path(f.get("path") or f["frame"]).is_file()
                                for f in [*report["frames"], *report["screen_text"]]))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_explicit_whisper_request_is_partial_if_cli_is_missing(self):
        real_which = shutil.which
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "audio.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=c=black:s=160x120:r=10:d=2", "-f", "lavfi", "-i",
                            "anullsrc=r=16000:cl=mono", "-t", "2", "-c:v", "mpeg4", "-c:a", "aac",
                            str(video)], check=True)
            model = root / "model.pt"
            model.touch()
            out = root / "evidence"
            with patch("sys.argv", ["watch", str(video), "--whisper-model", str(model),
                                    "--out-dir", str(out)]), redirect_stdout(io.StringIO()), \
                 patch("watch.shutil.which", side_effect=lambda name: None if name == "whisper" else real_which(name)):
                self.assertEqual(watch.main(), 2)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["status"], "partial")
            self.assertTrue(report["frames"])
            self.assertFalse(report["transcript"])
            self.assertTrue(any("Requested Whisper" in e for e in report["errors"]))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_uniform_cap_spans_the_range_and_keeps_real_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            frames = extract(str(ROOT / "tests/screen-text.mp4"), Path(directory), fps=2,
                             max_frames=3, start_seconds=0, end_seconds=4)
            self.assertEqual(len(frames), 3)
            self.assertGreater(frames[-1]["timestamp_seconds"], 2)
            self.assertLess(frames[-1]["timestamp_seconds"], 4)
            self.assertTrue(all(Path(f["path"]).is_file() for f in frames))

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
    def test_cue_frames_record_decoded_times_and_respect_focus_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "sparse.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=160x120:r=0.2:d=20", "-c:v", "mpeg4", str(video)], check=True)
            cues = [0.2, 4, 5.2, 9]
            frames, _ = extract_at_timestamps(str(video), root / "frames", cues, end_seconds=20)
            self.assertEqual([f["timestamp_seconds"] for f in frames], [5, 5, 10, 10])
            self.assertEqual([f["requested_timestamp_seconds"] for f in frames], cues)
            focused, _ = extract_at_timestamps(str(video), root / "focused", cues, end_seconds=10)
            self.assertEqual([f["timestamp_seconds"] for f in focused], [5, 5])
            self.assertEqual(len(list((root / "focused").glob("cue_*.jpg"))), 2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_ocr_failure_keeps_transcript_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "input with spaces.mp4"
            shutil.copyfile(ROOT / "tests/screen-text.mp4", video)
            video.with_suffix(".vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nA caption\n")
            out = root / "evidence"
            with patch("sys.argv", ["watch", str(video), "--screen-text", "--out-dir", str(out)]), \
                 patch("watch.read_screen_text", side_effect=SystemExit("language not installed")), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(watch.main(), 2)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["transcript"][0]["text"], "A caption")
            self.assertTrue(report["frames"])
            self.assertEqual(report["status"], "partial")


if __name__ == "__main__":
    unittest.main()
