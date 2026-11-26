"""fetch_course: calls each type's reader (CONTRACTS.md section 8)."""

from __future__ import annotations

from canvas_sync.model.course import Course
from canvas_sync.remote.context import FetchContext
from canvas_sync.remote.http import CanvasClient


def fetch_course(client: CanvasClient, ctx: FetchContext) -> Course:
    raise NotImplementedError
