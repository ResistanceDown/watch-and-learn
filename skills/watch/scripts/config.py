"""Portable frame budgets; explicit flags take precedence over WATCH_DETAIL."""
from __future__ import annotations

import os

DEFAULT_DETAIL = "balanced"
DETAILS = {"transcript", "efficient", "balanced", "token-burner"}


def get_config() -> dict[str, str]:
    detail = os.environ.get("WATCH_DETAIL", DEFAULT_DETAIL)
    return {"detail": detail if detail in DETAILS else DEFAULT_DETAIL}


def frame_cap(detail: str) -> int | None:
    return {"efficient": 50, "balanced": 100, "token-burner": None, "transcript": None}.get(detail, 100)
