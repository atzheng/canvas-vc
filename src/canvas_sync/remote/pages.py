"""Pages reader and writer (CONTRACTS.md section 8)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from rich.console import Console

from canvas_sync.dates import from_canvas
from canvas_sync.errors import CanvasAPIError
from canvas_sync.model.base import CanvasObject, Kind
from canvas_sync.model.page import Page
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.remote.http import CanvasClient
from canvas_sync.remote.paths import previous_objects, read_body, unique_path

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


def read(client: CanvasClient, ctx: FetchContext) -> dict[str, CanvasObject]:
    """Pages keyed by tree path (GET only).

    ``Page.canvas_id`` is the page's current Canvas ``url`` slug; module items and links
    use it, so ``fetch_course`` can build its ``(Kind.PAGE, slug) -> ref`` map from the
    result. Pages in ``ctx.previous`` keep their tree path, matched by slug and, when
    the slug changed on Canvas, by ``page_id`` (see ``_match_renamed``). New pages get
    ``pages/<slugify(title)>.md`` (``-2``, ``-3`` on collision, also against kept paths).
    Pages matched by ``.canvasignore`` are dropped.
    """
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

    read_ids: dict[tuple[Kind, int | str], str] = {(Kind.PAGE, s): p for s, p in assigned.items()}
    out: dict[str, CanvasObject] = {}
    for slug, path in sorted(assigned.items(), key=lambda kv: kv[1]):
        if ctx.ignore.ignored(path):
            continue
        full = _fetch(client, ctx, slug)
        if full is None:
            _warn(f"{path}: page {slug} disappeared during fetch, skipped")
            continue
        prev = previous.get(path)
        body, fmt = read_body(
            ctx,
            path,
            str(full.get("body") or ""),
            prev.body if isinstance(prev, Page) else None,
            prev.body_format if isinstance(prev, Page) else None,
            read_ids,
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
