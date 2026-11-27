"""Object lists (`/objects[/<kind>]`) and detail pages (`/o/<tree path>`), UI.md section 3.

Read-only and offline: bodies render with ``render.md2html`` (raw HTML disabled, relative links
rewritten to UI routes), URL paths are looked up as keys of the loaded course and never joined to
the file system, and the roster cache is never read (overrides show names as written in the tree).
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast
from urllib.parse import quote, unquote, urlsplit

from markdown_it.token import Token

from canvas_sync.dates import format_local
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import FileEntry
from canvas_sync.model.modules import Module
from canvas_sync.model.overrides import Override
from canvas_sync.render.links import LinkContext, is_relative_ref, resolve_relative
from canvas_sync.render.md2html import _make_md  # pyright: ignore[reportPrivateUsage]
from canvas_sync.ui.index import Index, Link
from canvas_sync.ui.index import _module_keys as module_keys  # pyright: ignore[reportPrivateUsage]
from canvas_sync.ui.layout import Response, badge, esc, link, not_found, obj_href, page, table
from canvas_sync.ui.state import State

KINDS: dict[str, tuple[Kind, str]] = {
    "pages": (Kind.PAGE, "Pages"),
    "assignments": (Kind.ASSIGNMENT, "Assignments"),
    "announcements": (Kind.ANNOUNCEMENT, "Announcements"),
    "files": (Kind.FILE, "Files"),
    "modules": (Kind.MODULE, "Modules"),
}
_CANVAS_SEGMENT = {
    Kind.PAGE: "pages",
    Kind.ASSIGNMENT: "assignments",
    Kind.ANNOUNCEMENT: "discussion_topics",
    Kind.FILE: "files",
    Kind.MODULE: "modules",
}
# Raw HTML in a body cannot run (the markdown renderer has it disabled); this is a second layer.
_CSP = (
    "Content-Security-Policy",
    "default-src 'none'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; base-uri 'none'",
)


# ---- pure helpers -----------------------------------------------------------------------------


def fmt_value(v: object) -> str:
    """Plain text for a frontmatter value (dates in course local time)."""
    if v is None:
        return "-"
    if isinstance(v, datetime):
        return format_local(v)
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (list, tuple)):
        items = list(cast("list[object]", v))
        return ", ".join(fmt_value(x) for x in items) or "-"
    return str(v)


def _dump(obj: CanvasObject) -> dict[str, Any]:
    return obj.model_dump()


def diff_fields(local: CanvasObject, remote: CanvasObject) -> list[str]:
    """Names of the fields that differ, in field order."""
    a, b = _dump(local), _dump(remote)
    return [k for k in a if a[k] != b.get(k)]


def diff_state(local: CanvasObject, remote: Course | None, key: str) -> tuple[str, list[str]]:
    """("new" | "modified" | "unchanged" | "unknown", changed fields) against canvas/main."""
    if remote is None:
        return "unknown", []
    other: CanvasObject | None
    if key.startswith("modules.yaml#"):
        other = dict(module_keys(remote.modules)).get(key)
    else:
        other = next((o for k, o in remote.objects.items() if k.path == key), None)
    if other is None:
        return "new", []
    changed = diff_fields(local, other)
    return ("modified", changed) if changed else ("unchanged", [])


def _published(course: Course, kind: Kind, path: str, value: bool | None) -> bool:
    if kind is Kind.FILE:
        value = course.files_meta.effective(path).published
    return course.config.defaults.published if value is None else value


def canvas_url(course: Course, kind: Kind, obj: CanvasObject) -> str | None:
    """Plain Canvas URL of an object that has an id (the UI never requests it)."""
    if obj.canvas_id is None:
        return None
    base = str(course.config.canvas_url).rstrip("/")
    root = f"{base}/courses/{course.config.course_id}/{_CANVAS_SEGMENT[kind]}"
    if kind is Kind.MODULE:
        return root
    return f"{root}/{quote(str(obj.canvas_id), safe='')}"


def _key_date(obj: CanvasObject) -> datetime | None:
    for f in ("due", "publish_at", "post_at", "unlock"):
        v = getattr(obj, f, None)
        if isinstance(v, datetime):
            return v
    return None


def _dates(obj: CanvasObject) -> str:
    parts = [
        f"{f} {format_local(v)}"
        for f in ("unlock", "due", "lock", "publish_at", "post_at")
        if isinstance(v := getattr(obj, f, None), datetime)
    ]
    return esc(", ".join(parts)) if parts else "-"


# ---- body rendering ---------------------------------------------------------------------------


def _render_math_code(self: Any, tokens: Sequence[Token], idx: int, options: Any, env: Any) -> str:
    return f"<code>{esc(tokens[idx].content.strip())}</code>"


def render_body(course: Course, path: str, body: str) -> str:
    """Markdown body to HTML for the UI: no raw HTML, relative links point at UI routes."""
    exists = {k.path for k in course.objects}
    seen: list[str] = []

    def ids(key: ObjectKey) -> int | None:
        seen.append(key.path)
        return 0 if key.path in exists else None

    # md2html's own pass (Canvas URLs) stays a no-op because no object has an id there; the
    # rule added below resolves targets itself and rewrites them to UI routes.
    noop = LinkContext(course.config.course_id, path, lambda _k: None, lambda _k, _i: None)
    md = _make_md(noop, [], [])
    # Math would be <img src="/equation_images/..."> (a 404 here): show the LaTeX as code.
    for math_rule in ("math_inline", "math_inline_double", "math_block"):
        md.add_render_rule(math_rule, _render_math_code)
    md.options["html"] = False  # raw <script>/<img onerror> in a body is shown as text
    ctx = LinkContext(course.config.course_id, path, ids, lambda _k, _i: None)

    def ui_href(href: str, image: bool) -> str | None:
        seen.clear()
        res = resolve_relative(href, ctx, image=image)
        if res.status != "ok" or not seen:
            return None
        target = seen[-1]
        if image:
            if not target.startswith("files/"):
                return None
            return "/files/raw/" + quote(target.removeprefix("files/"), safe="/")
        frag = unquote(urlsplit(href).fragment)
        return obj_href(target) + (f"#{quote(frag, safe='')}" if frag else "")

    def walk(tokens: list[Token]) -> None:
        for t in tokens:
            attr = "href" if t.type == "link_open" else "src" if t.type == "image" else None
            if attr is not None:
                v = t.attrGet(attr)
                if isinstance(v, str) and is_relative_ref(v):
                    new = ui_href(v, attr == "src")
                    if new is None:
                        t.attrSet(attr, "#")
                        t.attrSet("class", "broken")
                    else:
                        t.attrSet(attr, new)
            if t.children:
                walk(t.children)

    def rule(state: Any) -> None:
        walk(state.tokens)

    md.core.ruler.push("ui_links", rule)
    return md.render(body)


# ---- list -------------------------------------------------------------------------------------


def _rows(course: Course) -> list[tuple[str, Kind, CanvasObject]]:
    out: list[tuple[str, Kind, CanvasObject]] = []
    for key in sorted(course.objects):
        out.append((key.path, key.kind, course.objects[key]))
    for path, m in module_keys(course.modules):
        out.append((path, Kind.MODULE, m))
    return out


def _title(path: str, obj: CanvasObject) -> str:
    if isinstance(obj, Module):
        return obj.name
    if isinstance(obj, FileEntry):
        return posixpath.basename(path)
    return str(getattr(obj, "title", "") or posixpath.basename(path))


def _flags(path: str, kind: Kind, index: Index, differs: str) -> str:
    out: list[str] = []
    if kind is not Kind.MODULE:
        back = index.backlinks.get(path, ())
        if path in index.orphans:
            out.append(badge("orphan", "warning"))
        elif kind is not Kind.FILE and not any(b.kind == "module_item" for b in back):
            out.append(badge("not in a module"))
    if any(lk.broken for lk in index.outbound.get(path, ())):
        out.append(badge("broken links", "error"))
    if differs in ("new", "modified"):
        out.append(badge("differs" if differs == "modified" else "new"))
    return " ".join(out) or "-"


def _extra(obj: CanvasObject) -> str:
    pts = fmt_value(getattr(obj, "points", None))
    sub = fmt_value(getattr(obj, "submission_types", []))
    return esc(f"{pts} pts, {sub}")


def _sort_link(base: str, name: str, col: str, sort: str, desc: bool) -> str:
    flip = "desc" if sort == col and not desc else "asc"
    return link(f"{base}?sort={col}&dir={flip}", name)


def render_list(state: State, course: Course, which: str, query: Mapping[str, list[str]]) -> str:
    sort = (query.get("sort") or [""])[0]
    desc = (query.get("dir") or [""])[0] == "desc"
    index = state.index()
    remote = state.remote_course()
    selected = [which] if which else list(KINDS)
    body = ["<h1>Objects</h1>"]
    for slug in selected:
        kind, label = KINDS[slug]
        rows = [r for r in _rows(course) if r[1] is kind]
        if sort == "title":
            rows.sort(key=lambda r: _title(r[0], r[2]).lower(), reverse=desc)
        elif sort == "date":
            dated = [(r, d) for r in rows if (d := _key_date(r[2])) is not None]
            undated = [r for r in rows if _key_date(r[2]) is None]
            dated.sort(key=lambda rd: rd[1], reverse=desc)
            rows = [r for r, _ in dated] + undated
        base = f"/objects/{slug}" if which else "/objects"

        headers: list[str] = [
            _sort_link(base, "title", "title", sort, desc),
            "path",
            "pub",
            _sort_link(base, "dates", "date", sort, desc),
        ]
        if kind is Kind.ASSIGNMENT:
            headers.append("points, submission")
        headers.append("flags")
        trs: list[list[str]] = []
        for path, k, obj in rows:
            pub = _published(course, k, path, getattr(obj, "published", None))
            d, _ = diff_state(obj, remote, path)
            row = [
                link(obj_href(path), _title(path, obj)),
                esc(path),
                "yes" if pub else "no",
                _dates(obj) if k is not Kind.FILE else "-",
            ]
            if kind is Kind.ASSIGNMENT:
                row.append(_extra(obj))
            row.append(_flags(path, k, index, d))
            trs.append(row)
        body.append(f"<h2>{link(f'/objects/{slug}', label)} ({len(rows)})</h2>")
        body.append(table(headers, trs))
    return "".join(body)


# ---- detail -----------------------------------------------------------------------------------


def _override_rows(overrides: list[Override]) -> list[list[str]]:
    rows: list[list[str]] = []
    for o in overrides:
        if o.section is not None:
            who = f'section "{o.section}"'
        elif o.group is not None:
            who = f'group "{o.group}"'
        else:
            who = "students " + ", ".join(o.students or [])
        dates = ", ".join(
            f"{f} {format_local(v)}"
            for f in ("unlock", "due", "lock")
            if isinstance(v := getattr(o, f), datetime)
        )
        rows.append([esc(who), esc(o.title or "-"), esc(dates or "-")])
    return rows


def _src_label(src: str) -> str:
    if src.startswith("modules.yaml#"):
        return f"Module {src.removeprefix('modules.yaml#')} (module item)"
    return src


def _out_row(lk: Link, course: Course) -> list[str]:
    where = lk.kind + ("" if lk.line is None else f" line {lk.line}")
    if lk.broken:
        return [esc(lk.target), badge("broken", "error"), esc(where)]
    tgt = next((o for k, o in course.objects.items() if k.path == lk.target), None)
    status = badge("no canvas id yet") if tgt is not None and tgt.canvas_id is None else "ok"
    return [link(obj_href(lk.target), lk.target), status, esc(where)]


def _back_row(lk: Link) -> list[str]:
    where = lk.kind + ("" if lk.line is None else f" line {lk.line}")
    return [link(obj_href(lk.source), _src_label(lk.source)), esc(where)]


def _find(course: Course, path: str) -> tuple[Kind, CanvasObject] | None:
    for key, obj in course.objects.items():
        if key.path == path:
            return key.kind, obj
    for mpath, m in module_keys(course.modules):
        if mpath == path:
            return Kind.MODULE, m
    return None


def _module_items(m: Module, course: Course) -> str:
    exists = {k.path for k in course.objects}
    rows: list[list[str]] = []
    for it in m.items:
        t = it.item_type
        v = getattr(it, t)
        target = {
            "page": f"pages/{v}.md",
            "assignment": f"assignments/{v}.md",
            "file": f"files/{v}",
        }.get(t)
        label = it.title or (v if isinstance(v, str) else t)
        cell = link(obj_href(target), str(label)) if target in exists else esc(label)
        rows.append([esc(t), cell])
    return "<h2>Items</h2>" + table(["type", "item"], rows) if rows else ""


def render_detail(state: State, course: Course, path: str) -> Response:
    found = _find(course, path)
    if found is None:
        return not_found(f"no such object: {path}")
    kind, obj = found
    index = state.index()
    title = _title(path, obj)
    out = [
        f"<h1>{esc(title)}</h1>"
        f"<p class=mute>{esc(kind.value)} &middot; <code>{esc(path)}</code></p>"
    ]
    url = canvas_url(course, kind, obj)
    shown = link(url, url) if url else "<span class=mute>not on Canvas yet</span>"
    out.append(f"<p>Canvas: {shown}</p>")

    d, fields = diff_state(obj, state.remote_course(), path)
    if d == "unknown":
        st = "canvas/main: never fetched"
    elif d == "modified":
        st = f"modified vs canvas/main ({', '.join(fields)})"
    else:
        st = f"{d} vs canvas/main"
    pub = _published(course, kind, path, getattr(obj, "published", None))
    out.append(
        f"<p>State: {badge('published' if pub else 'unpublished')} "
        f"{badge(st, 'warning' if d in ('new', 'modified') else '')}</p>"
    )

    skip = {"body", "overrides", "items"}
    fm = [
        [esc(k), esc(fmt_value(v))]
        for k, v in _dump(obj).items()
        if k not in skip and v is not None and v != []
    ]
    out.append("<h2>Frontmatter</h2>" + table(["field", "value"], fm))

    overrides = cast("list[Override]", getattr(obj, "overrides", []))
    if overrides:
        out.append(
            "<h2>Overrides</h2>" + table(["target", "title", "dates"], _override_rows(overrides))
        )
    if isinstance(obj, Module):
        out.append(_module_items(obj, course))

    body = getattr(obj, "body", None)
    if isinstance(body, str):
        out.append("<h2>Body</h2>")
        if getattr(obj, "body_format", "md") == "html":
            out.append(
                f"<p class=mute>HTML body, shown as source.</p><pre><code>{esc(body)}</code></pre>"
            )
        else:
            out.append(f'<div class="body">{render_body(course, path, body)}</div>')

    outb = index.outbound.get(path, ())
    back = index.backlinks.get(path, ())
    out.append(f"<h2>Links out ({len(outb)})</h2>")
    out.append(
        table(["target", "status", "kind"], [_out_row(lk, course) for lk in outb])
        if outb
        else "<p class=mute>none</p>"
    )
    out.append(f"<h2>Linked from ({len(back)})</h2>")
    out.append(
        table(["source", "kind"], [_back_row(lk) for lk in back])
        if back
        else "<p class=mute>none</p>"
    )
    broken = [lk.target for lk in outb if lk.broken]
    if broken:
        names = ", ".join(esc(b) for b in broken)
        out.append(f'<p class="bad">Broken links ({len(broken)}): {names}</p>')

    r = page(title, "".join(out), current="objects", config=course.config)
    return Response(r.status, r.body, r.content_type, (_CSP,))


# ---- entry points -----------------------------------------------------------------------------


def render(state: State, path: str, query: Mapping[str, list[str]]) -> Response:
    """``/objects[/<kind>]`` (path is the kind or "")."""
    if state.course is None:
        err = f'<p class="err">{esc(state.load_error)}</p>'
        return page("Objects", err, current="objects", config=None)
    if path and path not in KINDS:
        return not_found("unknown object kind")
    return page(
        "Objects",
        render_list(state, state.course, path, query),
        current="objects",
        config=state.course.config,
    )


def render_object(state: State, path: str, query: Mapping[str, list[str]]) -> Response:
    """``/o/<tree path>``: the path is only ever a key lookup, never a file system path."""
    if state.course is None:
        err = f'<p class="err">{esc(state.load_error)}</p>'
        return page("Object", err, current="objects", config=None)
    return render_detail(state, state.course, path)
