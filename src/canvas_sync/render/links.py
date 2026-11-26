"""Relative path <-> Canvas URL (CONTRACTS.md section 6)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from canvas_sync.model.base import Kind, ObjectKey


@dataclass
class LinkContext:
    course_id: int
    from_path: str  # tree path of the document being rendered
    ids: Callable[[ObjectKey], int | str | None]  # tree path -> canvas id
    paths: Callable[[Kind, int | str], str | None]  # canvas id -> tree path


def to_canvas_url(rel: str, ctx: LinkContext) -> str | None:
    raise NotImplementedError


def from_canvas_url(url: str, ctx: LinkContext) -> str | None:
    raise NotImplementedError
