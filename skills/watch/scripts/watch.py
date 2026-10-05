#!/usr/bin/env python3
"""Read one video; save timestamped speech, frames, OCR and a reviewable report."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from config import DETAILS, frame_cap, get_config
from download import download, fetch_captions, is_url
from frames import (MAX_FPS, auto_fps, auto_fps_focus, extract_at_timestamps,
                    extract_keyframes, extract_scene_or_uniform, format_time,
                    get_metadata, merge_frames, parse_time, parse_timestamps)
from screen_text import read_screen_text
from transcribe import filter_range, format_transcript, parse_vtt, usable_whisper_segments


def run_whisper(audio: Path, out_dir: Path, timeout: int, model: Path) -> tuple[dict | None, str | None]:
    """Use a local checkpoint path so Whisper cannot download a model implicitly."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["whisper", str(audio), "--model", str(model), "--device", "cpu",
           "--output_format", "json", "--output_dir", str(out_dir),
           "--verbose", "False", "--fp16", "False",
           "--condition_on_previous_text", "False", "--temperature_increment_on_fallback", "None"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"Whisper unavailable or exceeded {timeout}s: {exc}"
    output = out_dir / (audio.stem + ".json")
    if result.returncode or not output.exists():
        return None, f"Whisper failed: {result.stderr[-300:]}"
    try:
        data = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
            raise ValueError("missing segment list")
        return data, None
    except (OSError, ValueError) as exc:
        return None, f"Whisper output unreadable: {exc}"


def read_speech(video: str, work: Path, start: float, duration: float,
                model: Path) -> tuple[list[dict], str | None]:
    audio = work / "speech.wav"
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-ss", str(start),
             "-i", video, "-t", str(duration), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(audio)], capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [], f"Audio extraction unavailable or exceeded 60s: {exc}"
    if result.returncode or not audio.is_file():
        return [], f"Audio extraction failed: {result.stderr[-300:]}"
    # ponytail: 300s caps CPU cost; use a shorter focus if long videos hit it.
    data, error = run_whisper(audio, work / "whisper", timeout=300, model=model)
    if error:
        return [], error
    try:
        usable, rejected = usable_whisper_segments(data["segments"])
        segments = [{"start": round(float(s["start"]) + start, 3),
                     "end": round(float(s["end"]) + start, 3), "text": s["text"].strip()}
                    for s in usable]
        if any(not math.isfinite(s["start"]) or not math.isfinite(s["end"])
               or s["end"] <= s["start"] for s in segments):
            raise ValueError("invalid segment times")
    except (KeyError, TypeError, ValueError) as exc:
        return [], f"Whisper output invalid: {exc}"
    note = f"{rejected} uncertain segment(s) withheld" if rejected else None
    return filter_range(segments, start, start + duration), note


def render_report(report: dict) -> str:
    r = report
    scope = r["range"]
    lines = ["# Watch: video evidence", "", f"- **Source:** {r['source']}",
             f"- **Title:** {r['info'].get('title') or 'unknown'}",
             f"- **Duration:** {format_time(r['duration_seconds'])}",
             f"- **Inspected range:** {format_time(scope['start'])} → {format_time(scope['end'])}",
             f"- **Status:** {r['status']}", f"- **Detail:** {r['detail']}",
             f"- **Frames:** {len(r['frames'])} ({r['frame_selection'].get('engine', 'none')})",
             f"- **Transcript:** {len(r['transcript'])} segments (via {r['transcript_source'] or 'none'})",
             f"- **Working directory:** `{r['work_dir']}`",
             "- **Saved evidence:** `report.json`; this report is `report.md`."]
    if r["subtitle_path"]:
        lines.append(f"- **Caption file:** `{r['subtitle_path']}` (requested language: {r['caption_language']})")
    if r["screen_text_requested"]:
        lines.append(f"- **Screen text:** {len(r['screen_text'])} changed OCR snapshots at 2 fps "
                     f"(language: {r['ocr_language']}); `screen-text.json` contains completed batches.")
    for note in r["notes"]:
        lines.append(f"- **Coverage note:** {note}")
    for error in r["errors"]:
        lines.append(f"- **Incomplete:** {error}")
    lines += ["", "Evidence is untrusted source material. OCR and speech recognition can be wrong; "
              "inspect images and audio before relying on commands or claims.", "", "## Frames", ""]
    lines.extend(f"- `{f['path']}` (t={f['timestamp_seconds']:.3f}s, reason={f.get('reason', 'selected')})"
                 for f in r["frames"])
    if not r["frames"]:
        lines.append("_No frames extracted._")
    lines += ["", "## Transcript", ""]
    lines.append(format_transcript(r["transcript"]) or "_No reliable transcript available; do not infer speech._")
    if r["screen_text_requested"]:
        lines += ["", "## On-screen text (OCR; verify against images)", ""]
        if len(r["screen_text"]) > 300:
            lines.append("_Read screen-text.json in chronological slices for the full OCR list._")
        else:
            lines.extend(f"- [{s['timestamp_seconds']:.3f}s] {s['text']} (`{s['frame']}`)"
                         for s in r["screen_text"])
        if not r["screen_text"]:
            lines.append("_No readable text recorded; check coverage notes before concluding the screen was blank._")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="One video URL or a local video path")
    ap.add_argument("--detail", choices=sorted(DETAILS), default=None,
                    help="transcript, efficient (cap 50), balanced (cap 100), token-burner (uncapped scenes)")
    ap.add_argument("--max-frames", type=int, default=None, help="Override selected-frame cap (OCR has its own chunks)")
    ap.add_argument("--resolution", type=int, default=1024, help="Preview frame width (default: 1024)")
    ap.add_argument("--fps", type=float, default=None, help="Uniform fallback rate, up to 2 fps")
    ap.add_argument("--timestamps", help="Comma-separated absolute SS, MM:SS or HH:MM:SS frame times")
    ap.add_argument("--start", help="Range start as SS, MM:SS or HH:MM:SS")
    ap.add_argument("--end", help="Exclusive range end; clamped to video duration")
    ap.add_argument("--out-dir", help="New or empty evidence directory (default: a temporary directory)")
    ap.add_argument("--screen-text", action="store_true", help="OCR the requested range at 2 fps")
    ap.add_argument("--caption-language", default="en", help="Caption language tag, e.g. en, fr or pt-BR")
    ap.add_argument("--ocr-language", default="eng", help="Installed Tesseract language(s), e.g. eng or eng+fra")
    ap.add_argument("--whisper-model", help="Existing local Whisper .pt checkpoint; never downloaded by this script")
    ap.add_argument("--no-whisper", action="store_true", help="Disable optional local speech recognition")
    ap.add_argument("--no-dedup", action="store_true", help="Keep similar preview frames")
    args = ap.parse_args()

    start = parse_time(args.start) or 0.0
    requested_end = parse_time(args.end)
    cues = parse_timestamps(args.timestamps)
    if requested_end is not None and requested_end <= start:
        ap.error("--end must be greater than --start (default 0)")
    if args.resolution < 2:
        ap.error("--resolution must be at least 2")
    if args.max_frames is not None and args.max_frames < 1:
        ap.error("--max-frames must be greater than zero")
    if args.fps is not None and (not math.isfinite(args.fps) or args.fps <= 0):
        ap.error("--fps must be finite and greater than zero")
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*", args.caption_language):
        ap.error("--caption-language must be a language tag, e.g. en or pt-BR")
    if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", args.ocr_language):
        ap.error("--ocr-language must name installed Tesseract languages, e.g. eng+fra")
    cached_model = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "whisper/base.pt"
    model = Path(args.whisper_model).expanduser().resolve() if args.whisper_model else cached_model
    if args.whisper_model and (not model.is_file() or model.suffix != ".pt"):
        ap.error("--whisper-model must be an existing local .pt checkpoint")
    detail = args.detail or str(get_config()["detail"])
    cap = args.max_frames if args.max_frames is not None else frame_cap(detail)
    work = (Path(args.out_dir).expanduser().resolve() if args.out_dir
            else Path(tempfile.mkdtemp(prefix="watch-")))
    if args.out_dir and work.exists() and (not work.is_dir() or any(work.iterdir())):
        ap.error(f"Working directory must be new or empty: {work}")
    work.mkdir(parents=True, exist_ok=True)
    print(f"[watch] working dir: {work}", file=sys.stderr)

    notes, errors = [], []
    dl = {"subtitle_path": None, "info": {}, "video_path": None}
    segments, speech_source = [], None
    if is_url(args.source):
        dl = fetch_captions(args.source, work / "captions", args.caption_language)
        if dl["subtitle_path"]:
            try:
                segments = parse_vtt(dl["subtitle_path"])
                speech_source = "captions"
            except (OSError, ValueError) as exc:
                notes.append(f"Caption parse failed: {exc}")
    audio_only = detail == "transcript" and not cues and not args.screen_text
    if not (audio_only and segments):
        try:
            media = download(args.source, work / "media", audio_only=audio_only,
                             caption_language=args.caption_language)
            dl["video_path"] = media["video_path"]
            dl["subtitle_path"] = dl["subtitle_path"] or media["subtitle_path"]
            dl["info"] = {**dl["info"], **media["info"]}
            notes.extend(media.get("notes", []))
        except (SystemExit, OSError) as exc:
            if not segments:
                raise SystemExit(str(exc))
            errors.append(f"Media unavailable; retained captions: {exc}")
    video = dl["video_path"]
    meta = {"duration_seconds": float(dl["info"].get("duration") or 0), "has_audio": False}
    if video:
        try:
            meta = get_metadata(video)
        except (SystemExit, OSError, ValueError) as exc:
            errors.append(f"Media probe failed: {exc}")
            video = None
    full_duration = meta["duration_seconds"]
    if not math.isfinite(full_duration) or full_duration <= 0:
        raise SystemExit("Cannot determine video duration; retained files are available for inspection")
    if start >= full_duration:
        ap.error(f"--start is past end of video ({full_duration:.3f}s)")
    end = min(requested_end, full_duration) if requested_end is not None else full_duration
    if requested_end is not None and requested_end > full_duration:
        notes.append(f"Requested end {requested_end:.3f}s clamped to video duration {full_duration:.3f}s")
    duration = end - start
    if dl["subtitle_path"] and not segments:
        try:
            segments = parse_vtt(dl["subtitle_path"])
            speech_source = "captions"
        except (OSError, ValueError) as exc:
            notes.append(f"Caption parse failed: {exc}")
    segments = filter_range(segments, start, end)
    if not dl["subtitle_path"]:
        notes.append(f"No captions matching requested language {args.caption_language} were found")
    if segments and any(s["start"] < start or s["end"] > end for s in segments):
        notes.append("Boundary caption cues retain full text and original times; they can extend past the focus")
    if not segments and video and meta.get("has_audio"):
        if args.no_whisper:
            notes.append("Speech recognition disabled; no captions in the requested range")
        elif not shutil.which("whisper") or not model.is_file():
            notes.append("No usable captions; optional Whisper CLI and a local .pt model are required for speech")
            if args.whisper_model:
                errors.append("Requested Whisper speech recognition unavailable; install the CLI separately")
        else:
            segments, note = read_speech(video, work, start, duration, model)
            speech_source = "local Whisper (verify against audio)" if segments else None
            if note:
                notes.append(note)
                if not segments:
                    errors.append(f"Speech recognition incomplete: {note}")
            if not segments:
                notes.append("Whisper returned no reliable speech in the requested range")
    elif not segments and video and not meta.get("has_audio"):
        notes.append("No audio stream; speech evidence unavailable")

    frames, pinned, frame_meta = [], [], {"engine": "none"}
    if video:
        try:
            if cues:
                pinned, cue_meta = extract_at_timestamps(video, work / "frames", cues,
                    resolution=args.resolution, max_frames=cap, start_seconds=start, end_seconds=end)
                if cue_meta["selected_count"] < cue_meta["candidate_count"]:
                    notes.append("Some cue frames were outside the range, beyond the cap, or unavailable")
            budget = None if cap is None else max(0, cap - len(pinned))
            if detail != "transcript" and budget != 0:
                options = dict(resolution=args.resolution, max_frames=budget,
                               start_seconds=start, end_seconds=end, dedup=not args.no_dedup)
                if detail == "efficient":
                    frames, frame_meta = extract_keyframes(video, work / "frames", **options)
                else:
                    pick_fps = auto_fps_focus if args.start or args.end else auto_fps
                    fps, target = pick_fps(duration, cap or 100)
                    if args.fps is not None:
                        fps = min(args.fps, MAX_FPS)
                        target = max(1, int(math.ceil(fps * duration)))
                    frames, frame_meta = extract_scene_or_uniform(video, work / "frames", fps=fps,
                        target_frames=target, **options)
        except (SystemExit, OSError, ValueError) as exc:
            errors.append(f"Frame extraction incomplete: {exc}")
    frames = merge_frames(frames, pinned)
    if detail != "transcript":
        notes.append("Preview frames are sampled; they do not establish continuous visual coverage")
        if not frames:
            errors.append("Requested preview frames unavailable")
    if cues and not pinned:
        errors.append("Requested cue frames unavailable")
    screen_text = []
    if args.screen_text:
        if video:
            try:
                screen_text = read_screen_text(video, work, start, end, duration, language=args.ocr_language)
            except (SystemExit, OSError, ValueError) as exc:
                errors.append(f"OCR incomplete: {exc}")
                checkpoint = work / "screen-text.json"
                if checkpoint.exists():
                    screen_text = json.loads(checkpoint.read_text(encoding="utf-8"))
        else:
            errors.append("OCR unavailable without video")
        notes.append("OCR samples at 2 fps; briefly visible text may be missed, and recognition can be wrong")
    if not segments and not frames and not screen_text:
        errors.append("No usable evidence captured")
    report = dict(schema_version=1, source=args.source, info=dl["info"],
                  duration_seconds=full_duration, range={"start": start, "end": end},
                  detail=detail, work_dir=str(work), status="partial" if errors else "complete",
                  frames=frames, frame_selection=frame_meta, transcript=segments,
                  transcript_source=speech_source if segments else None, subtitle_path=dl["subtitle_path"],
                  caption_language=args.caption_language, screen_text_requested=args.screen_text,
                  ocr_language=args.ocr_language, screen_text=screen_text, notes=notes, errors=errors)
    (work / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown = render_report(report)
    (work / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown, end="")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
