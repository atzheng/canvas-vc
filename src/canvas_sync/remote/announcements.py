"""Announcements reader and writer (CONTRACTS.md section 8)."""

from __future__ import annotations

from typing import Any

from canvas_sync.dates import from_canvas
from canvas_sync.model.announcement import Announcement
from canvas_sync.model.base import CanvasObject, Kind
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.remote.http import CanvasClient
from canvas_sync.remote.paths import previous_objects, read_body, unique_path

FOLDER = "announcements"


def read(client: CanvasClient, ctx: FetchContext) -> dict[str, CanvasObject]:
    """Announcements keyed by tree path (GET only).

    Announcements are discussion topics listed with ``only_announcements=true``; the body
    is ``message`` and ``post_at`` is ``delayed_post_at``. Objects in ``ctx.previous`` keep
    their path (matched by ``canvas_id``, which is the topic id and survives renames);
    new ones get ``announcements/<slugify(title)>.md`` (``-2``, ``-3`` on collision).
    Ignored paths are dropped.
    """
    listing: list[dict[str, Any]] = client.get(
        f"/courses/{ctx.config.course_id}/discussion_topics",
        params={"only_announcements": "true"},
    )
    topics = sorted(listing, key=lambda t: int(t["id"]))
    previous = previous_objects(ctx, Kind.ANNOUNCEMENT)
    by_id = {o.canvas_id: p for p, o in previous.items() if o.canvas_id is not None}

    assigned: dict[int, str] = {}
    taken: set[str] = set()
    for t in topics:
        path = by_id.get(int(t["id"]))
        if path is not None:
            assigned[int(t["id"])] = path
            taken.add(path)
    # Kept paths are all taken before new ones are named; a path whose topic is gone is free.
    for t in topics:
        if int(t["id"]) not in assigned:
            path = unique_path(FOLDER, str(t.get("title") or "announcement"), taken)
            assigned[int(t["id"])] = path
            taken.add(path)

    read_ids: dict[tuple[Kind, int | str], str] = {
        (Kind.ANNOUNCEMENT, i): p for i, p in assigned.items()
    }
    out: dict[str, CanvasObject] = {}
    for t in topics:
        tid = int(t["id"])
        path = assigned[tid]
        if ctx.ignore.ignored(path):
            continue
        prev = previous.get(path)
        body, fmt = read_body(
            ctx,
            path,
            str(t.get("message") or ""),
            prev.body if isinstance(prev, Announcement) else None,
            prev.body_format if isinstance(prev, Announcement) else None,
            read_ids,
        )
        out[path] = Announcement(
            canvas_id=tid,
            title=str(t.get("title") or ""),
            post_at=from_canvas(t.get("delayed_post_at"), ctx.tz),
            published=bool(t.get("published", True)),
            body_format="html" if fmt == "html" else "md",
            body=body,
        )
    return out


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
