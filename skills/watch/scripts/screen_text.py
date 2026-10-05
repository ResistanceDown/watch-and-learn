"""Extract timestamped text from video frames with local Tesseract OCR."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from frames import extract, format_time


def _ocr_frame(frame: dict, language: str = "eng") -> str:
    try:
        result = subprocess.run(["tesseract", frame["path"], "stdout", "-l", language, "--psm", "11"],
                                capture_output=True, text=True, timeout=30,
                                env={**os.environ, "OMP_THREAD_LIMIT": "1"})
    except subprocess.TimeoutExpired:
        raise SystemExit(f"OCR timed out at {format_time(frame['timestamp_seconds'])}")
    if result.returncode:
        raise SystemExit(f"OCR failed at {format_time(frame['timestamp_seconds'])}: {result.stderr.strip()}")
    return " ".join(result.stdout.split())


def read_screen_text(video: str, work: Path, start: float | None, end: float | None,
                     duration: float, fps: float = 2.0, max_frames: int = 1200,
                     language: str = "eng") -> list[dict]:
    if shutil.which("tesseract") is None:
        raise SystemExit("tesseract is required for --screen-text")
    if duration <= 0:
        raise SystemExit("Cannot scan screen text without a known video duration")
    if fps <= 0 or max_frames < 1:
        raise SystemExit("OCR fps and frame cap must be positive")
    found: list[dict] = []
    previous = ""
    work.mkdir(parents=True, exist_ok=True)
    (work / "screen-text.json").write_text("[]\n", encoding="utf-8")
    window_start = start or 0.0
    window_end = end if end is not None else window_start + duration
    chunk_seconds = max_frames / fps
    chunk = 0
    while window_start < window_end:
        chunk_end = min(window_end, window_start + chunk_seconds)
        frames = extract(video, work / "ocr_frames" / f"{chunk:04d}", fps=fps,
                         resolution=1600, max_frames=max_frames,
                         start_seconds=window_start, end_seconds=chunk_end)
        # ponytail: four single-threaded processes cap CPU pressure; tune only from real captures.
        with ThreadPoolExecutor(max_workers=4) as pool:
            for offset in range(0, len(frames), 16):
                batch = frames[offset:offset + 16]
                for frame, value in zip(batch, pool.map(_ocr_frame, batch, [language] * len(batch))):
                    if value and value != previous:
                        found.append({"timestamp_seconds": frame["timestamp_seconds"],
                                      "text": value, "frame": frame["path"]})
                    else:
                        Path(frame["path"]).unlink()
                    previous = value
                checkpoint = work / "screen-text.json"
                pending = work / "screen-text.json.tmp"
                pending.write_text(json.dumps(found, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                pending.replace(checkpoint)
        window_start = chunk_end
        chunk += 1
    return found
