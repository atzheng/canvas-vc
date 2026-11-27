"""Announcements reader and writer (CONTRACTS.md section 8)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from canvas_sync.dates import from_canvas, to_canvas
from canvas_sync.errors import CanvasSyncError
from canvas_sync.model.announcement import Announcement
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.plan.announcements import PlacedAnnouncement
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.remote.http import CanvasClient
from canvas_sync.remote.paths import previous_objects, read_body, render_body, unique_path

FOLDER = "announcements"


def _plan(
    client: CanvasClient, ctx: FetchContext
) -> tuple[list[dict[str, Any]], dict[int, str], set[int], dict[str, CanvasObject]]:
    """(topics by id, id -> path, ids matched to a previous object, previous)."""
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
    matched = set(assigned)
    # Kept paths are all taken before new ones are named; a path whose topic is gone is free.
    for t in topics:
        if int(t["id"]) not in assigned:
            path = unique_path(FOLDER, str(t.get("title") or "announcement"), taken)
            assigned[int(t["id"])] = path
            taken.add(path)
    return topics, assigned, matched, previous


def assign_paths(client: CanvasClient, ctx: FetchContext) -> dict[int, str]:
    """Topic id -> tree path for every announcement on Canvas (one list call).

    fetch_course uses it in its id/path pre-pass. ``read`` computes the same assignment.
    """
    return _plan(client, ctx)[1]


def read(
    client: CanvasClient,
    ctx: FetchContext,
    *,
    paths: Mapping[tuple[Kind, int | str], str] | None = None,
) -> dict[str, CanvasObject]:
    """Announcements keyed by tree path (GET only).

    Announcements are discussion topics listed with ``only_announcements=true``; the body
    is ``message`` and ``post_at`` is ``delayed_post_at``. Objects in ``ctx.previous`` keep
    their path (matched by ``canvas_id``, the topic id, which survives renames); new ones
    get ``announcements/<slugify(title)>.md`` (see ``assign_paths``). Ignored paths are
    dropped. ``paths`` is the complete ``(kind, id) -> tree path`` map from fetch_course;
    links resolve through it when given.
    """
    topics, assigned, matched, previous = _plan(client, ctx)
    read_ids: dict[tuple[Kind, int | str], str] = {
        (Kind.ANNOUNCEMENT, i): p for i, p in assigned.items()
    }
    out: dict[str, CanvasObject] = {}
    for t in topics:
        tid = int(t["id"])
        path = assigned[tid]
        if ctx.ignore.ignored(path):
            continue
        prev = previous.get(path) if tid in matched else None
        body, fmt = read_body(
            ctx,
            path,
            str(t.get("message") or ""),
            prev.body if isinstance(prev, Announcement) else None,
            prev.body_format if isinstance(prev, Announcement) else None,
            read_ids,
            paths,
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


def _as_ann(obj: CanvasObject) -> Announcement:
    if not isinstance(obj, Announcement):
        raise CanvasSyncError(f"expected an announcement, got {type(obj).__name__}")
    return obj


def _key_path(obj: Announcement) -> str:
    return obj.key_path if isinstance(obj, PlacedAnnouncement) else ""


def _fields(
    ctx: ApplyContext, obj: Announcement, before: Announcement | None, key_path: str
) -> dict[str, Any]:
    """Topic fields to send: all for a create, changed ones for an update (null to clear)."""
    out: dict[str, Any] = {}
    if before is None or obj.title != before.title:
        out["title"] = obj.title
    if before is None or (obj.body, obj.body_format) != (before.body, before.body_format):
        out["message"] = render_body(ctx, key_path, obj.body, obj.body_format)
    if before is None or obj.published != before.published:
        out["published"] = obj.published
    if obj.post_at != (before.post_at if before else None):
        out["delayed_post_at"] = to_canvas(obj.post_at)
    return out


def _record(ctx: ApplyContext, obj: Announcement, key_path: str, row: Any, sent: bool) -> None:
    if not key_path or not sent or obj.body_format == "html" or not isinstance(row, dict):
        return
    html = row.get("message")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if isinstance(html, str):
        ctx.bodies.record(key_path, obj.body, html)


def create(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> int | str:
    """POST the announcement; returns the topic id.

    Without a future ``post_at`` this posts at once and emails students; the differ's
    NOTIFIES_STUDENTS risk and the guard (``apply.needs_guard``) hold it back unless
    ``--confirm-notify`` was given.
    """
    ann = _as_ann(obj)
    key_path = _key_path(ann)
    body = {"is_announcement": True, **_fields(ctx, ann, None, key_path)}
    row = client.post(f"/courses/{ctx.config.course_id}/discussion_topics", json=body)
    new_id = int(row["id"])
    if key_path:
        ctx.ids[ObjectKey(Kind.ANNOUNCEMENT, key_path)] = new_id
    _record(ctx, ann, key_path, row, True)
    return new_id


def _put(client: CanvasClient, ctx: ApplyContext, old: Announcement, fields: dict[str, Any]) -> Any:
    if old.canvas_id is None:
        raise CanvasSyncError(f"announcement {old.title!r} has no Canvas id")
    return client.put(
        f"/courses/{ctx.config.course_id}/discussion_topics/{int(old.canvas_id)}", json=fields
    )


def update(
    client: CanvasClient, before: CanvasObject, after: CanvasObject, ctx: ApplyContext
) -> None:
    """PUT only the changed fields (also used for UNPUBLISH: ``published: false``)."""
    old, new = _as_ann(before), _as_ann(after)
    key_path = _key_path(new)
    fields = _fields(ctx, new, old, key_path)
    if not fields:
        return
    row = _put(client, ctx, old, fields)
    _record(ctx, new, key_path, row, "message" in fields)


def delete(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> None:
    """Announcements are never deleted (the allowlist refuses it): unpublish instead."""
    ann = _as_ann(obj)
    _put(client, ctx, ann, {"published": False})
