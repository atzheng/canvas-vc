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
    """Pair previous pages whose slug is gone with remote pages renamed on Canvas.

    The page model keeps the slug as ``canvas_id`` and has no page id, so a renamed page
    is found by asking Canvas for the old slug (it redirects to the page) and comparing
    ``page_id``s. Fills ``claimed`` (new slug -> previous tree path).
    """
    taken = set(claimed.values())
    orphans = {
        path: str(o.canvas_id)
        for path, o in previous.items()
        if o.canvas_id is not None and path not in taken and str(o.canvas_id) not in remote
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
    for path, obj in previous.items():
        if obj.canvas_id is not None and str(obj.canvas_id) in remote:
            claimed[str(obj.canvas_id)] = path
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
    use it. Pages in ``ctx.previous`` keep their tree path, matched by slug and, when
    the slug changed on Canvas, by ``page_id`` (see ``_match_renamed``). New pages get
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
            title=str(full.get("title") or ""),
            published=bool(full.get("published")),
            publish_at=from_canvas(full.get("publish_at"), ctx.tz),
            front_page=bool(full.get("front_page")),
            body_format="html" if fmt == "html" else "md",
            body=body,
        )
    return out


def _slug_path(ctx: ApplyContext, slug: str) -> str:
    return f"/courses/{ctx.config.course_id}/pages/{quote(slug, safe='')}"


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
    slug = str(old.canvas_id)
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
    client.delete(_slug_path(ctx, str(page.canvas_id)))


def _as_page(obj: CanvasObject) -> Page:
    if not isinstance(obj, Page):
        raise CanvasSyncError(f"expected a page, got {type(obj).__name__}")
    return obj
