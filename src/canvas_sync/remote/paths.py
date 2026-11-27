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

SLUG_MAX = 80


def slugify(title: str) -> str:
    """NFKD -> ASCII -> lower -> runs of non-alphanumerics to ``-`` -> strip -> first 80
    characters; ``untitled`` when nothing is left."""
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:SLUG_MAX] or "untitled"


def unique_path(folder: str, title: str, taken: Collection[str]) -> str:
    """``<folder>/<slug>.md``, or ``<slug>-2.md``, ``-3`` ... until not in ``taken``
    (compared case-insensitively, as case-insensitive file systems would collide)."""
    slug = slugify(title)
    seen = {t.casefold() for t in taken}
    path = f"{folder}/{slug}.md"
    n = 2
    while path.casefold() in seen:
        path = f"{folder}/{slug}-{n}.md"
        n += 1
    return path


def previous_objects(ctx: FetchContext, kind: Kind) -> dict[str, CanvasObject]:
    """Tree path -> previous object of ``kind`` (empty without a previous tree)."""
    if ctx.previous is None:
        return {}
    return {k.path: o for k, o in ctx.previous.objects.items() if k.kind == kind}


PathMap = Mapping[tuple[Kind, int | str], str]


def link_context(
    ctx: FetchContext,
    from_path: str,
    read: PathMap,
    paths: PathMap | None = None,
) -> LinkContext:
    """LinkContext for converting the body at ``from_path``.

    With ``paths`` (the complete ``(kind, id) -> tree path`` map from fetch_course) ids
    resolve through it alone. Otherwise through the previous tree plus ``read`` (objects
    of the kind being read, which win): previous objects of a kind present in ``read``
    that are not in ``read`` no longer exist remotely and are dropped.
    """
    by_id: dict[tuple[Kind, int | str], str] = {}
    by_path: dict[str, int | str] = {}
    if paths is not None:
        read = paths
    elif ctx.previous is not None:
        kinds = {k for k, _ in read}
        for key, obj in ctx.previous.objects.items():
            if obj.canvas_id is not None and key.kind not in kinds:
                by_id[(key.kind, obj.canvas_id)] = key.path
                by_path[key.path] = obj.canvas_id
    by_id.update(read)
    for (_, cid), path in read.items():
        by_path[path] = cid

    def ids(key: ObjectKey) -> int | str | None:
        return by_path.get(key.path)

    def path_for(kind: Kind, cid: int | str) -> str | None:
        return by_id.get((kind, cid))

    return LinkContext(course_id=ctx.config.course_id, from_path=from_path, ids=ids, paths=path_for)


def read_body(
    ctx: FetchContext,
    path: str,
    html: str,
    previous_md: str | None,
    previous_format: str | None,
    read: PathMap,
    paths: PathMap | None = None,
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
    return normalize_body(html_to_md(html, link_context(ctx, path, read, paths))), "md"
