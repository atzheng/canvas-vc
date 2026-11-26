"""Announcements reader and writer (CONTRACTS.md section 8)."""

from __future__ import annotations

from canvas_sync.model.base import CanvasObject
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.remote.http import CanvasClient


def read(client: CanvasClient, ctx: FetchContext) -> dict[str, CanvasObject]:
    raise NotImplementedError


def create(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> int | str:
    """Returns the new canvas_id."""
    raise NotImplementedError


def update(
    client: CanvasClient, before: CanvasObject, after: CanvasObject, ctx: ApplyContext
) -> None:
    """Only changed fields are sent."""
    raise NotImplementedError


def delete(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> None:
    """Goes through the submission guard."""
    raise NotImplementedError
