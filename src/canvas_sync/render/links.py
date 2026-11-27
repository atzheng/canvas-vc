"""Relative path <-> Canvas URL (CONTRACTS.md section 6).

URL forms (``c`` is the course id)::

    page          /courses/c/pages/<slug>
    assignment    /courses/c/assignments/<id>
    announcement  /courses/c/discussion_topics/<id>
    file link     /courses/c/files/<id>?wrap=1
    file image    /courses/c/files/<id>/preview

Relative paths are POSIX, relative to the directory of ``ctx.from_path``,
percent-encoded (``%20`` for spaces); a ``#fragment`` is carried through.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

from canvas_sync.model.base import Kind, ObjectKey, kind_for_path


@dataclass
class LinkContext:
    course_id: int
    from_path: str  # tree path of the document being rendered
    ids: Callable[[ObjectKey], int | str | None]  # tree path -> canvas id
    paths: Callable[[Kind, int | str], str | None]  # canvas id -> tree path
    # tree path -> does the local object exist (even without a canvas_id yet)?
    # None: unknown, so an id-less target is reported as missing, not unresolved.
    exists: Callable[[str], bool] | None = None


_FOLDER_URL = {
    Kind.PAGE: "pages",
    Kind.ASSIGNMENT: "assignments",
    Kind.ANNOUNCEMENT: "discussion_topics",
    Kind.FILE: "files",
}
_KIND_FOR_SEGMENT = {v: k for k, v in _FOLDER_URL.items()}
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def is_relative_ref(url: str) -> bool:
    """True for a link we treat as a local path (not absolute, scheme, anchor-only)."""
    return bool(url) and not url.startswith(("/", "#", "?")) and not _SCHEME.match(url)


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a relative link.

    ``status``: ``ok`` (url set), ``missing`` (no such local object) or
    ``unresolved`` (the object exists locally but has no Canvas id yet; push
    renders again after creating objects, so it gets filled in).
    """

    status: Literal["ok", "missing", "unresolved"]
    url: str | None = None


def resolve_relative(rel: str, ctx: LinkContext, *, image: bool = False) -> Resolution:
    if not is_relative_ref(rel):
        return Resolution("missing")
    parts = urlsplit(rel)
    frag = f"#{parts.fragment}" if parts.fragment else ""
    base = posixpath.dirname(ctx.from_path)
    target = posixpath.normpath(posixpath.join(base, unquote(parts.path)))
    if target.startswith("../") or target == "..":
        return Resolution("missing")
    try:
        kind = kind_for_path(target)
    except ValueError:
        return Resolution("missing")
    if kind not in _FOLDER_URL:
        return Resolution("missing")
    cid = ctx.ids(ObjectKey(kind, target))
    if cid is None:
        if ctx.exists is not None and ctx.exists(target):
            return Resolution("unresolved")
        return Resolution("missing")
    root = f"/courses/{ctx.course_id}/{_FOLDER_URL[kind]}/"
    query = parts.query
    if kind is Kind.PAGE:
        path = f"{root}{quote(str(cid), safe='')}"
    elif kind is Kind.FILE:
        if image:
            path = f"{root}{cid}/preview"
        else:
            path = f"{root}{cid}"
            query = "wrap=1" + (f"&{query}" if query else "")
    else:
        path = f"{root}{cid}"
    return Resolution("ok", path + (f"?{query}" if query else "") + frag)


def to_canvas_url(rel: str, ctx: LinkContext, *, image: bool = False) -> str | None:
    """Canvas URL for relative path ``rel``; None if missing or not yet created.

    ``image`` selects the ``/preview`` form for files (used for ``![](...)``).
    A ``?query`` and ``#fragment`` on ``rel`` are carried through.
    """
    return resolve_relative(rel, ctx, image=image).url


def from_canvas_url(url: str, ctx: LinkContext) -> str | None:
    """Relative path for a Canvas URL of this course; None if not ours/unknown."""
    parts = urlsplit(url.strip())
    if parts.scheme and parts.scheme not in ("http", "https"):
        return None
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 4 or segs[0] != "courses" or segs[1] != str(ctx.course_id):
        return None
    kind = _KIND_FOR_SEGMENT.get(segs[2])
    if kind is None:
        return None
    raw = segs[3]
    extra = segs[4:]
    if kind is Kind.FILE:
        if extra not in ([], ["preview"], ["download"]):
            return None
    elif extra:
        return None
    cid: int | str
    if kind is Kind.PAGE:
        cid = unquote(raw)
    elif raw.isdigit():
        cid = int(raw)
    else:
        return None
    tree = ctx.paths(kind, cid)
    if tree is None:
        return None
    base = posixpath.dirname(ctx.from_path) or "."
    rel = posixpath.relpath(tree, base)
    frag = f"#{parts.fragment}" if parts.fragment else ""
    query = parts.query
    if kind is Kind.FILE and extra != ["preview"]:
        # ``verifier`` is minted by Canvas (a fresh one whenever it rewrites a link after a
        # file replacement), never written by a person: keep it out of the Markdown.
        query = "&".join(
            q for q in query.split("&") if q and q != "wrap=1" and not q.startswith("verifier=")
        )
    return quote(rel, safe="/._-~") + (f"?{query}" if query else "") + frag
