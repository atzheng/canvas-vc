"""Files (`/files`, `/files/raw/<path>`): the `files/` tree with settings and backlinks
(UI.md section 5, CONTRACTS.md section 13).

Raw bytes are served only for keys of the loaded course's ``files/`` entries, never for a
path joined from the URL, and only from regular, non-symlink files inside ``<root>/files``.
"""

from __future__ import annotations

import mimetypes
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from canvas_sync.dates import format_local
from canvas_sync.model.course import Course
from canvas_sync.model.files import FileEntry, glob_match
from canvas_sync.ui.index import Link
from canvas_sync.ui.layout import (
    Response,
    badge,
    error_page,
    esc,
    link,
    not_found,
    obj_href,
    page,
)
from canvas_sync.ui.state import State

_PREFIX = "files/"
# Served inline (browsers render these without running script). Everything else, SVG and
# HTML included, is an attachment, so nothing from files/ runs in the UI origin.
_PREVIEW = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_INLINE = _PREVIEW | {"application/pdf"}


def _mime(rel: str) -> str:
    return mimetypes.guess_type(rel, strict=False)[0] or "application/octet-stream"


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
    return f"{n} B"  # unreachable


def _raw_href(rel: str) -> str:
    return "/files/raw/" + quote(rel, safe="/")


def _disk_file(root: Path, rel: str) -> Path | None:
    """The regular, non-symlink file ``files/<rel>`` under ``root``, or None.

    No component below ``files/`` may be a symlink (a linked directory could lead outside),
    and the resolved path must stay inside the resolved ``files/``.
    """
    base = root / "files"
    if base.is_symlink() or not base.is_dir():
        return None
    p = base
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            return None
        p = p / part
        if p.is_symlink():
            return None
    if not p.is_file():
        return None
    if not p.resolve().is_relative_to(base.resolve()):
        return None
    return p


def _lookup(course: Course, rel: str) -> str | None:
    """The key (relative to ``files/``) in the loaded course that ``rel`` names."""
    for cand in (rel, unicodedata.normalize("NFC", rel)):
        if _PREFIX + cand in course.files_meta.files:
            return cand
    return None


def raw(state: State, rel: str) -> Response:
    if state.course is None:
        return not_found()
    key = _lookup(state.course, rel)
    if key is None:
        return not_found()
    f = _disk_file(state.root, key)
    if f is None:
        return not_found()
    mime = _mime(key)
    inline = mime in _INLINE
    name = quote(key.rsplit("/", 1)[-1], safe="")
    headers = (
        ("X-Content-Type-Options", "nosniff"),
        ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox"),
        ("Content-Disposition", f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{name}"),
    )
    with open(f, "rb") as fh:
        data = fh.read()
    return Response(200, data, mime if inline else "application/octet-stream", headers)


def _date(dt: datetime) -> str:
    return format_local(dt)


def _settings(course: Course, tree_path: str, entry: FileEntry) -> str:
    eff = course.files_meta.effective(tree_path)
    published = course.config.defaults.published if eff.published is None else eff.published
    rel = tree_path.removeprefix(_PREFIX)
    parts = [badge("published" if published else "unpublished", "ok" if published else "warning")]
    if eff.hidden:
        parts.append(badge("hidden", "warning"))
    if isinstance(eff.unlock, datetime):
        parts.append(f"unlock {esc(_date(eff.unlock))}")
    if isinstance(eff.lock, datetime):
        parts.append(f"lock {esc(_date(eff.lock))}")
    if entry.stable_link:
        parts.append(badge("stable link"))
    rules = [r.match for r in course.files_meta.rules if glob_match(r.match, rel)]
    own = [f for f in ("published", "hidden", "unlock", "lock") if getattr(entry, f) is not None]
    src: list[str] = []
    if rules:
        src.append("rule " + ", ".join(f"<code>{esc(m)}</code>" for m in rules))
    if own:
        src.append("entry: " + esc(", ".join(own)))
    if src:
        parts.append(f"({'; '.join(src)})")
    return " ".join(parts)


def _backlinks(links: tuple[Link, ...]) -> str:
    seen: dict[str, str] = {}
    for lk in links:
        label = lk.source
        if lk.source.startswith("modules.yaml#"):
            label = "Module " + lk.source.split("#", 1)[1]
        seen.setdefault(lk.source, link(obj_href(lk.source), label))
    return "linked from: " + ", ".join(seen.values())


def _file_li(state: State, course: Course, tree_path: str, entry: FileEntry) -> str:
    rel = tree_path.removeprefix(_PREFIX)
    name = rel.rsplit("/", 1)[-1]
    disk = _disk_file(state.root, rel)
    if disk is None:
        size = badge("missing on disk", "warning")
        title = esc(name)
    else:
        size = esc(_size(disk.stat().st_size))
        title = link(_raw_href(rel), name)
    bl = state.index().backlinks.get(tree_path, ())
    used = _backlinks(bl) if bl else badge("unreferenced", "warning")
    sha = f"<code>{esc(entry.sha256[:8])}</code>" if entry.sha256 else "-"
    thumb = ""
    if disk is not None and _mime(rel) in _PREVIEW:
        thumb = f' <img src="{esc(_raw_href(rel))}" alt="" height="32" loading="lazy">'
    return (
        f"<li>{title}{thumb} &nbsp; {size} &nbsp; {sha} &nbsp; "
        f"{_settings(course, tree_path, entry)} &nbsp; {used}</li>"
    )


class _Dir:
    def __init__(self) -> None:
        self.dirs: dict[str, _Dir] = {}
        self.files: dict[str, FileEntry] = {}


def _tree(state: State, course: Course) -> str:
    top = _Dir()
    for tp, entry in sorted(course.files_meta.files.items()):
        node = top
        *dirs, _ = tp.removeprefix(_PREFIX).split("/")
        for d in dirs:
            node = node.dirs.setdefault(d, _Dir())
        node.files[tp] = entry

    def walk(node: _Dir) -> str:
        out = ["<ul>"]
        for name, sub in sorted(node.dirs.items()):
            out.append(f"<li><strong>{esc(name)}/</strong>{walk(sub)}</li>")
        for tp, entry in sorted(node.files.items()):
            out.append(_file_li(state, course, tp, entry))
        out.append("</ul>")
        return "".join(out)

    return walk(top)


def _rules(course: Course) -> str:
    items: list[str] = []
    for r in course.files_meta.rules:
        s = r.settings
        bits: list[str] = []
        if s.published is not None:
            bits.append(f"published {str(s.published).lower()}")
        if s.hidden is not None:
            bits.append(f"hidden {str(s.hidden).lower()}")
        if s.unlock is not None:
            bits.append("unlock " + (_date(s.unlock) if s.unlock else "none"))
        if s.lock is not None:
            bits.append("lock " + (_date(s.lock) if s.lock else "none"))
        items.append(f"<li><code>{esc(r.match)}</code> {esc(', '.join(bits) or '-')}</li>")
    if not items:
        return "<p>No rules in <code>files/_meta.yaml</code>.</p>"
    return "<h2>Rules (_meta.yaml)</h2><ul>" + "".join(items) + "</ul>"


def render(state: State, path: str, query: Mapping[str, list[str]]) -> Response:
    if path.startswith("raw/"):
        return raw(state, path[4:])
    if path:
        return not_found()
    course = state.course
    if course is None:
        return error_page(state.load_error or "course not loaded")
    n = len(course.files_meta.files)
    unref = len(state.index().unreferenced_files)
    body = (
        "<h1>Files</h1>"
        f"<p>{n} file{'' if n == 1 else 's'}, {unref} unreferenced</p>"
        + (_tree(state, course) if n else "<p>No files under <code>files/</code>.</p>")
        + _rules(course)
    )
    return page("Files", body, current="files", config=course.config)
