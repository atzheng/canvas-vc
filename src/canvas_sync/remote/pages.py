"""Pages reader and writer (CONTRACTS.md section 8)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from rich.console import Console

from canvas_sync.dates import from_canvas, to_canvas
from canvas_sync.errors import CanvasAPIError, CanvasSyncError
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.page import Page
from canvas_sync.plan.pages import PlacedPage
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.remote.http import CanvasClient
from canvas_sync.remote.paths import previous_objects, read_body, render_body, unique_path

FOLDER = "pages"


def _warn(message: str) -> None:
    Console(stderr=True).print(f"warning: {message}", markup=False, highlight=False, soft_wrap=True)


def _fetch(client: CanvasClient, ctx: FetchContext, slug: str) -> dict[str, Any] | None:
    """One page by slug (Canvas also resolves a slug the page had before a rename)."""
    try:
        data = client.get_one(f"/courses/{ctx.config.course_id}/pages/{quote(slug, safe='')}")
    except CanvasAPIError as e:
        if e.status == 404:
            return None
        raise
    return data if isinstance(data, dict) else None  # pyright: ignore[reportUnknownVariableType]


def _match_renamed(
    client: CanvasClient,
    ctx: FetchContext,
    previous: dict[str, CanvasObject],
    claimed: dict[str, str],
    remote: dict[str, dict[str, Any]],
) -> None:
    """Pair previous pages that have no ``page_id`` and whose slug is gone with renamed pages.

    Only files written before ``page_id`` existed need this: Canvas redirects the old slug
    to the page, and the ``page_id``s are compared. Fills ``claimed`` (new slug -> path).
    """
    taken = set(claimed.values())
    orphans = {
        path: str(o.canvas_id)
        for path, o in previous.items()
        if isinstance(o, Page)
        and o.page_id is None
        and o.canvas_id is not None
        and path not in taken
        and str(o.canvas_id) not in remote
    }
    unclaimed = {s: r for s, r in remote.items() if s not in claimed}
    for path, old in sorted(orphans.items()):
        if not unclaimed:
            break
        found = _fetch(client, ctx, old)
        if found is None:
            continue
        for slug, raw in unclaimed.items():
            if raw.get("page_id") is not None and raw.get("page_id") == found.get("page_id"):
                claimed[slug] = path
                del unclaimed[slug]
                break


def assign_paths(client: CanvasClient, ctx: FetchContext) -> dict[str, str]:
    """Page url slug -> tree path for every page on Canvas (list calls, no bodies).

    fetch_course uses it in its id/path pre-pass. ``read`` computes the same assignment.
    """
    return _plan(client, ctx)[0]


def _plan(
    client: CanvasClient, ctx: FetchContext
) -> tuple[dict[str, str], dict[str, str], dict[str, CanvasObject]]:
    """(slug -> path, slug -> path for pages matched to a previous object, previous)."""
    cid = ctx.config.course_id
    listing: list[dict[str, Any]] = client.get(f"/courses/{cid}/pages")
    remote = {str(p["url"]): p for p in sorted(listing, key=lambda p: p.get("page_id") or 0)}
    previous = previous_objects(ctx, Kind.PAGE)

    claimed: dict[str, str] = {}  # slug -> tree path, existing objects
    by_pid = {p["page_id"]: s for s, p in remote.items() if p.get("page_id") is not None}
    for path, obj in sorted(previous.items()):  # stable page_id first: survives renames
        if isinstance(obj, Page) and obj.page_id in by_pid:
            claimed.setdefault(by_pid[obj.page_id], path)
    for path, obj in sorted(previous.items()):
        # A file with a page_id never matches by slug (the slug may belong to another page now).
        if isinstance(obj, Page) and obj.page_id is not None:
            continue
        slug = str(obj.canvas_id) if obj.canvas_id is not None else None
        if slug in remote and slug not in claimed and path not in claimed.values():
            claimed[slug] = path
    _match_renamed(client, ctx, previous, claimed, remote)

    taken = set(claimed.values())
    assigned: dict[str, str] = dict(claimed)
    for slug, raw in remote.items():
        if slug not in assigned:
            assigned[slug] = unique_path(FOLDER, str(raw.get("title") or slug), taken)
            taken.add(assigned[slug])
    return assigned, claimed, previous


def read(
    client: CanvasClient,
    ctx: FetchContext,
    *,
    paths: Mapping[tuple[Kind, int | str], str] | None = None,
) -> dict[str, CanvasObject]:
    """Pages keyed by tree path (GET only).

    ``Page.canvas_id`` is the page's current Canvas ``url`` slug; module items and links
    use it. ``Page.page_id`` is the stable Canvas id. Pages in ``ctx.previous`` keep their
    tree path, matched by ``page_id`` first, then (files without one) by slug and the
    old-slug redirect (see ``_match_renamed``). New pages get
    ``pages/<slugify(title)>.md`` (see ``assign_paths``). Pages matched by
    ``.canvasignore`` are dropped. ``paths`` is the complete ``(kind, id) -> tree path``
    map from fetch_course (pages by slug); links resolve through it when given.
    """
    assigned, claimed, previous = _plan(client, ctx)
    read_ids: dict[tuple[Kind, int | str], str] = {(Kind.PAGE, s): p for s, p in assigned.items()}
    out: dict[str, CanvasObject] = {}
    for slug, path in sorted(assigned.items(), key=lambda kv: kv[1]):
        if ctx.ignore.ignored(path):
            continue
        full = _fetch(client, ctx, slug)
        if full is None:
            _warn(f"{path}: page {slug} disappeared during fetch, skipped")
            continue
        prev = previous.get(path) if slug in claimed else None
        body, fmt = read_body(
            ctx,
            path,
            str(full.get("body") or ""),
            prev.body if isinstance(prev, Page) else None,
            prev.body_format if isinstance(prev, Page) else None,
            read_ids,
            paths,
        )
        out[path] = Page(
            canvas_id=slug,
            page_id=_int(full.get("page_id")),
            title=str(full.get("title") or ""),
            published=bool(full.get("published")),
            publish_at=from_canvas(full.get("publish_at"), ctx.tz),
            front_page=bool(full.get("front_page")),
            body_format="html" if fmt == "html" else "md",
            body=body,
        )
    return out


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _slug_path(ctx: ApplyContext, slug: str) -> str:
    return f"/courses/{ctx.config.course_id}/pages/{quote(slug, safe='')}"


def _verify(client: CanvasClient, ctx: ApplyContext, page: Page, path: str) -> str:
    """The slug to address ``page`` by, after checking it still is the same page.

    A slug can be reused by another page after a rename, so when the page has a
    ``page_id`` the GET must return that id; otherwise the write is refused.
    """
    slug = str(page.canvas_id)
    if page.page_id is None:
        return slug
    row = client.get_one(_slug_path(ctx, slug))
    got = row.get("page_id") if isinstance(row, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if got != page.page_id:
        raise CanvasSyncError(
            f"{path}: page slug {slug!r} now belongs to another page on Canvas; "
            "run `canvas fetch` and retry"
        )
    return slug


def _fields(ctx: ApplyContext, obj: Page, before: Page | None, key_path: str) -> dict[str, Any]:
    """The ``wiki_page`` fields to send: all set ones for a create, changed ones otherwise.

    A cleared ``publish_at`` is sent as an explicit null. ``published: None`` means no
    opinion: a create uses ``course.yaml`` defaults, an update leaves it alone.
    """
    out: dict[str, Any] = {}
    if before is None or obj.title != before.title:
        out["title"] = obj.title
    if before is None or (obj.body, obj.body_format) != (before.body, before.body_format):
        out["body"] = render_body(ctx, key_path, obj.body, obj.body_format)
    if before is None:
        published = obj.published
        out["published"] = ctx.config.defaults.published if published is None else published
        if obj.publish_at is not None:
            out["publish_at"] = to_canvas(obj.publish_at)
        if obj.front_page:
            out["front_page"] = True
        return out
    if obj.published is not None and obj.published != before.published:
        out["published"] = obj.published
    if obj.publish_at != before.publish_at:
        out["publish_at"] = to_canvas(obj.publish_at)
    if obj.front_page != before.front_page:
        out["front_page"] = obj.front_page
    return out


def _key_path(obj: Page) -> str:
    return obj.key_path if isinstance(obj, PlacedPage) else ""


def _record(ctx: ApplyContext, obj: Page, key_path: str, row: dict[str, Any], sent: bool) -> None:
    """Remember the Markdown for the HTML Canvas now holds, so fetch reuses it."""
    if not key_path or not sent or obj.body_format == "html":
        return
    html = row.get("body")
    if isinstance(html, str):
        ctx.bodies.record(key_path, obj.body, html)


def create(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> int | str:
    """POST the page; returns its url slug (a str: module items refer to pages by it)."""
    page = _as_page(obj)
    key_path = _key_path(page)
    c = ctx.config.course_id
    row = client.post(f"/courses/{c}/pages", json={"wiki_page": _fields(ctx, page, None, key_path)})
    slug = str(row["url"])
    if key_path:
        ctx.ids[ObjectKey(Kind.PAGE, key_path)] = slug
    _record(ctx, page, key_path, row, True)
    return slug


def update(
    client: CanvasClient, before: CanvasObject, after: CanvasObject, ctx: ApplyContext
) -> None:
    """PUT only the changed fields.

    Canvas changes a page's url slug when its title changes. The PUT response carries the
    new slug; it is put in ``ctx.ids`` under the page's key, and ``apply`` reports a
    changed id in ``ApplyResult.new_ids``, so push writes the new ``canvas_id`` back (and
    module refs, which use the slug, stay right).
    """
    old, new = _as_page(before), _as_page(after)
    key_path = _key_path(new)
    if old.canvas_id is None:
        raise CanvasSyncError(f"{key_path or new.title}: page has no Canvas id")
    fields = _fields(ctx, new, old, key_path)
    if not fields:
        return
    slug = _verify(client, ctx, old, key_path or new.title)
    row = client.put(_slug_path(ctx, slug), json={"wiki_page": fields})
    new_slug = str(row.get("url") or slug) if isinstance(row, dict) else slug  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    if key_path:
        ctx.ids[ObjectKey(Kind.PAGE, key_path)] = new_slug
    if isinstance(row, dict):
        _record(ctx, new, key_path, row, "body" in fields)  # pyright: ignore[reportUnknownArgumentType]


def delete(client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> None:
    """DELETE the page (pages carry no student work; the guard does not apply)."""
    page = _as_page(obj)
    if page.canvas_id is None:
        raise CanvasSyncError(f"page {page.title!r} has no Canvas id")
    client.delete(_slug_path(ctx, _verify(client, ctx, page, page.title)))


def _as_page(obj: CanvasObject) -> Page:
    if not isinstance(obj, Page):
        raise CanvasSyncError(f"expected a page, got {type(obj).__name__}")
    return obj
