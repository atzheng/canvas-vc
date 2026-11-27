"""Tree paths, body conversion and link contexts shared by the pages and announcements readers.

``slugify(title)`` names new remote objects (CONTRACTS.md section 8); ``unique_path``
adds ``-2``, ``-3``, ... on collision. Existing objects keep the path they have in
``ctx.previous``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Mapping

from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.serialize import normalize_body
from canvas_sync.remote.context import FetchContext
from canvas_sync.render.html2md import html_to_md
from canvas_sync.render.links import LinkContext


def slugify(title: str) -> str:
    """Lowercase ASCII words joined by ``-``; ``untitled`` when nothing is left."""
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "untitled"


def unique_path(folder: str, title: str, taken: Collection[str]) -> str:
    """``<folder>/<slug>.md``, or ``<slug>-2.md``, ``-3`` ... until not in ``taken``."""
    slug = slugify(title)
    path = f"{folder}/{slug}.md"
    n = 2
    while path in taken:
        path = f"{folder}/{slug}-{n}.md"
        n += 1
    return path


def previous_objects(ctx: FetchContext, kind: Kind) -> dict[str, CanvasObject]:
    """Tree path -> previous object of ``kind`` (empty without a previous tree)."""
    if ctx.previous is None:
        return {}
    return {k.path: o for k, o in ctx.previous.objects.items() if k.kind == kind}


def link_context(
    ctx: FetchContext, from_path: str, read: Mapping[tuple[Kind, int | str], str]
) -> LinkContext:
    """LinkContext for converting the body at ``from_path``.

    Canvas ids resolve through the previous tree plus ``read`` (objects read earlier in
    this fetch, ``(kind, canvas id) -> tree path``, which win).
    """
    by_id: dict[tuple[Kind, int | str], str] = {}
    by_path: dict[str, int | str] = {}
    if ctx.previous is not None:
        for key, obj in ctx.previous.objects.items():
            if obj.canvas_id is not None:
                by_id[(key.kind, obj.canvas_id)] = key.path
                by_path[key.path] = obj.canvas_id
    by_id.update(read)
    for (_, cid), path in read.items():
        by_path[path] = cid

    def ids(key: ObjectKey) -> int | str | None:
        return by_path.get(key.path)

    def paths(kind: Kind, cid: int | str) -> str | None:
        return by_id.get((kind, cid))

    return LinkContext(course_id=ctx.config.course_id, from_path=from_path, ids=ids, paths=paths)


def read_body(
    ctx: FetchContext,
    path: str,
    html: str,
    previous_md: str | None,
    previous_format: str | None,
    read: Mapping[tuple[Kind, int | str], str],
) -> tuple[str, str]:
    """``(body, body_format)`` for remote ``html`` at tree path ``path``.

    A previous ``body_format: html`` object keeps the HTML verbatim. Otherwise the
    previous Markdown is reused when ``ctx.bodies`` says the remote HTML is unchanged
    since our push, else the HTML is converted. Converted bodies are normalised like
    loaded files (``normalize_body``), so a fetched tree is already canonical.
    """
    if previous_format == "html":
        return normalize_body(html), "html"
    if previous_md is not None and ctx.bodies.matches(path, html):
        return previous_md, "md"
    if not html.strip():
        return "", "md"
    return normalize_body(html_to_md(html, link_context(ctx, path, read))), "md"
