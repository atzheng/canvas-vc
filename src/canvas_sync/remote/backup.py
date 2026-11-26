"""Pre-push backup (CONTRACTS.md section 10)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from canvas_sync.config import CourseConfig
from canvas_sync.remote.http import CanvasClient


def backup(client: CanvasClient, config: CourseConfig, assignment_ids: Iterable[int]) -> Path:
    """Write ~/.canvas-sync/backups/<course_id>/<UTC timestamp>.json (GET only)."""
    raise NotImplementedError
