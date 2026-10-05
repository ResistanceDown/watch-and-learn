#!/usr/bin/env python3
"""Parse a WebVTT subtitle file into a clean, timestamped transcript.

YouTube auto-subs emit rolling-duplicate cues (each line appears 2-3 times as it
scrolls). We dedupe consecutive identical cues and merge their time ranges.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path


TS_RE = re.compile(r"((?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
                   r"((?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3})")
TAG_RE = re.compile(r"<[^>]+>")


def _to_seconds(stamp: str) -> float:
    result = 0.0
    for part in stamp.replace(",", ".").split(":"):
        result = result * 60 + float(part)
    return result


def parse_vtt(path: str) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = text.splitlines()

    segments: list[dict] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().split(" ", 1)[0] in {"NOTE", "STYLE", "REGION"}:
            while i < len(lines) and lines[i].strip():
                i += 1
            continue
        match = TS_RE.match(lines[i])
        if not match:
            i += 1
            continue

        start, end = map(_to_seconds, match.groups())
        i += 1

        cue_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            cleaned = html.unescape(TAG_RE.sub("", lines[i])).strip()
            if cleaned:
                cue_lines.append(cleaned)
            i += 1

        cue_text = " ".join(cue_lines).strip()
        if cue_text and end > start:
            segments.append({"start": round(start, 2), "end": round(end, 2), "text": cue_text})
        i += 1

    return _dedupe(segments)


def _dedupe(segments: list[dict]) -> list[dict]:
    """Collapse rolling duplicates common in YouTube auto-subs."""
    out: list[dict] = []
    for seg in segments:
        adjacent = out and seg["start"] <= out[-1]["end"]
        if adjacent and seg["text"] == out[-1]["text"]:
            out[-1]["end"] = max(out[-1]["end"], seg["end"])
            continue
        if adjacent and seg["text"].startswith(out[-1]["text"] + " "):
            out[-1]["text"] = seg["text"]
            out[-1]["end"] = max(out[-1]["end"], seg["end"])
            continue
        out.append(seg)
    return out


def filter_range(
    segments: list[dict],
    start_seconds: float | None,
    end_seconds: float | None,
) -> list[dict]:
    """Return segments overlapping [start, end); retain their original cue times."""
    if start_seconds is None and end_seconds is None:
        return segments
    lo = start_seconds if start_seconds is not None else float("-inf")
    hi = end_seconds if end_seconds is not None else float("inf")
    return [seg for seg in segments if seg["end"] > lo and seg["start"] < hi]


def format_transcript(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        start = int(seg["start"])
        hours, rest = divmod(start, 3600)
        stamp = (f"[{hours:02d}:{rest // 60:02d}:{rest % 60:02d}]" if hours
                 else f"[{rest // 60:02d}:{rest % 60:02d}]")
        lines.append(f"{stamp} {seg['text']}")
    return "\n".join(lines)


def usable_whisper_segments(segments: list[dict]) -> tuple[list[dict], int]:
    """Exclude low-confidence and repetitive Whisper output from the evidence report."""
    accepted: list[dict] = []
    for seg in segments:
        normalized = str(seg.get("text", "")).strip().casefold()
        words = re.findall(r"\w+", normalized)
        repetitive = len(words) >= 8 and len(set(words)) / len(words) < 0.25
        if (not words or repetitive
                or float(seg.get("no_speech_prob", 0)) > 0.6
                or float(seg.get("avg_logprob", 0)) < -1.2):
            continue
        accepted.append(seg)
    return accepted, len(segments) - len(accepted)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: transcribe.py <vtt-path>", file=sys.stderr)
        raise SystemExit(2)
    print(format_transcript(parse_vtt(sys.argv[1])))
