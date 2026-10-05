#!/usr/bin/env python3
"""Download a video via yt-dlp, or resolve a local file path.

Also fetches subtitles (manual first, then auto-generated) in VTT format so
transcribe.py can parse them without needing Whisper.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}


def is_url(source: str) -> bool:
    if source.startswith("-"):
        return False
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def resolve_local(path: str, caption_language: str = "en") -> dict:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise SystemExit(f"File not found: {p}")
    if p.suffix.lower() not in VIDEO_EXTS:
        print(
            f"[watch] warning: {p.suffix} is not a known video extension, proceeding anyway",
            file=sys.stderr,
        )
    subtitle = _pick_subtitle(p.parent, p.stem, caption_language)
    return {
        "video_path": str(p),
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": {"title": p.name, "url": str(p)},
        "downloaded": False,
        "notes": (["Local caption sidecar has no language tag; its language is unverified"]
                  if subtitle and subtitle.stem == p.stem else []),
    }


def _pick_subtitle(out_dir: Path, stem: str = "video", language: str = "en") -> Path | None:
    candidates = sorted(p for p in out_dir.iterdir() if p.is_file() and p.suffix.lower() == ".vtt"
                        and (p.stem == stem or p.name.startswith(stem + ".")))
    if not candidates:
        return None
    preferred = [
        c for c in candidates
        if c.name.lower() == f"{stem}.{language}.vtt".lower()
        or c.name.lower().startswith(f"{stem}.{language}-".lower())
    ]
    return preferred[0] if preferred else next((c for c in candidates if c.stem == stem), None)


def _pick_video(out_dir: Path) -> Path | None:
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus"):
        candidate = out_dir / f"video{ext}"
        if candidate.is_file() and candidate.stat().st_size:
            return candidate
    for candidate in out_dir.glob("video.*"):
        if (candidate.stem == "video" and candidate.is_file() and candidate.stat().st_size
                and candidate.suffix.lower() in VIDEO_EXTS):
            return candidate
    return None


def _download_command(url: str, out_dir: Path, language: str) -> list[str]:
    """Share isolation, language selection and single-video limits across both paths."""
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is required for URLs; install it for your platform and add it to PATH")

    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise SystemExit(f"Download directory must be new or empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")
    return [
        "yt-dlp",
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-cache-dir",
        "--socket-timeout", "30",
        "--retries", "2",
        "--fragment-retries", "2",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", f"{language}.*",
        "--sub-format", "vtt",
        "--no-playlist",
        "--playlist-items", "1",
        "-o", output_template,
        "--",
        url,
    ]


def fetch_captions(url: str, out_dir: Path, caption_language: str = "en") -> dict:
    """Fetch metadata and VTT captions without downloading video."""
    cmd = _download_command(url, out_dir, caption_language)
    cmd.insert(1, "--skip-download")
    try:
        subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr, timeout=180)
    except subprocess.TimeoutExpired:
        print("[watch] caption lookup exceeded 180s; checking any retained captions", file=sys.stderr)
    subtitle = _pick_subtitle(out_dir, language=caption_language)
    info = _read_info(out_dir / "video.info.json", url)
    return {
        "video_path": None,
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": False,
    }


def _read_info(info_path: Path, url: str) -> dict:
    info: dict = {}
    if info_path.exists():
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
            info = {
                "title": raw.get("title"),
                "uploader": raw.get("uploader") or raw.get("channel"),
                "duration": raw.get("duration"),
                "url": raw.get("webpage_url") or url,
            }
        except Exception as exc:
            print(f"[watch] info.json parse failed: {exc}", file=sys.stderr)
            info = {"url": url}
    return info


def download_url(
    url: str,
    out_dir: Path,
    audio_only: bool = False,
    caption_language: str = "en",
) -> dict:
    cmd = _download_command(url, out_dir, caption_language)
    fmt = "ba/bestaudio" if audio_only else "bv*[height<=720]+ba/b[height<=720]/bv+ba/b"
    cmd[1:1] = ["-N", "4", "-f", fmt, "--merge-output-format", "mp4"]

    # yt-dlp may exit non-zero if a subtitle variant fails (e.g. 429) even when
    # the video itself downloaded fine. Treat "video file present" as success.
    try:
        result = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr, timeout=900)
    except subprocess.TimeoutExpired:
        raise SystemExit("Video download exceeded 900s; partial files are retained")
    video = _pick_video(out_dir)
    if video is None:
        raise SystemExit(
            f"yt-dlp did not produce a video file in {out_dir} (exit {result.returncode})"
        )

    subtitle = _pick_subtitle(out_dir, language=caption_language)
    info = _read_info(out_dir / "video.info.json", url)

    return {
        "video_path": str(video),
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": True,
    }


def download(
    source: str,
    out_dir: Path,
    audio_only: bool = False,
    caption_language: str = "en",
) -> dict:
    if is_url(source):
        return download_url(source, out_dir, audio_only=audio_only, caption_language=caption_language)
    return resolve_local(source, caption_language=caption_language)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: download.py <url-or-path> <out-dir>", file=sys.stderr)
        raise SystemExit(2)
    result = download(sys.argv[1], Path(sys.argv[2]))
    print(json.dumps(result, indent=2))
