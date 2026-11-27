"""Modules outline (`/modules`): the course as students see it (UI.md section 2).

Reads ``Course.modules`` and the reference index only; no Canvas calls.
"""

from __future__ import annotations

from collections.abc import Mapping

from canvas_sync.dates import LocalDT, format_local
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.modules import Module, ModuleItem
from canvas_sync.ui.layout import Response, badge, esc, link, obj_href, page, table
from canvas_sync.ui.state import State


def module_keys(modules: list[Module]) -> list[tuple[str, Module]]:
    """``modules.yaml#<name>``, ``#<name>#2`` for repeats (same scheme as ``dump_tree``)."""
    taken: set[str] = set()
    out: list[tuple[str, Module]] = []
    for m in modules:
        key = f"modules.yaml#{m.name}"
        n = 2
        while key in taken:
            key = f"modules.yaml#{m.name}#{n}"
            n += 1
        taken.add(key)
        out.append((key, m))
    return out


def item_target(item: ModuleItem) -> str | None:
    """Tree path of the object a page/assignment/file item names."""
    if item.page is not None:
        return f"pages/{item.page}.md"
    if item.assignment is not None:
        return f"assignments/{item.assignment}.md"
    if item.file is not None:
        return f"files/{item.file}"
    return None


def _date(dt: LocalDT) -> str:
    return esc(format_local(dt))


def _pub_badge(value: bool | None, default: bool) -> str:
    on = default if value is None else value
    return badge("published", "ok") if on else badge("unpublished", "warning")


def _object(course: Course, path: str) -> CanvasObject | None:
    for kind in (Kind.PAGE, Kind.ASSIGNMENT, Kind.FILE):
        obj = course.objects.get(ObjectKey(kind, path))
        if obj is not None:
            return obj
    return None


def _item_row(course: Course, item: ModuleItem, broken: set[str]) -> str:
    default_pub = course.config.defaults.published
    kind = item.item_type
    target = item_target(item)
    obj = _object(course, target) if target else None
    title = item.title or ""
    when = ""
    pub = item.published
    if obj is not None and target is not None:
        title = title or str(getattr(obj, "title", "") or "")
        due: LocalDT | None = getattr(obj, "due", None) if kind == "assignment" else None
        at: LocalDT | None = getattr(obj, "publish_at", None) if kind == "page" else None
        if due:
            when = f"due {_date(due)}"
        elif at:
            when = f"publish {_date(at)}"
        if pub is None:
            if kind == "file":
                pub = course.files_meta.effective(target).published
            else:
                pub = getattr(obj, "published", None)
    where = ""
    if target is not None:
        where = esc(target) if target in broken else link(obj_href(target), target)
    elif item.url is not None:
        where = link(item.url, item.url)
    elif item.unmanaged is not None:
        title = title or item.unmanaged.type
        where = esc(item.unmanaged.type)
    elif item.quiz is not None:
        where = esc(f"quiz {item.quiz.canvas_id}")
    elif item.discussion is not None:
        where = esc(f"discussion {item.discussion.canvas_id}")
    if not title:
        title = str(item.header or item.url or target or "")
    flags = ""
    if target is not None and target in broken:
        flags = badge("missing target", "error")
    elif kind != "header" and (pub if pub is not None else default_pub) is False:
        flags = _pub_badge(pub, default_pub)
    pad = f' style="padding-left:{item.indent * 1.5}em"'
    return (
        f"<tr><td>{esc(kind)}</td><td{pad}>{esc(title)}</td><td>{where}</td>"
        f"<td>{when}</td><td>{flags}</td></tr>"
    )


def _module_block(
    course: Course, key: str, m: Module, names: Mapping[int, str], broken: set[str]
) -> str:
    meta = [_pub_badge(m.published, course.config.defaults.published)]
    if m.unlock:
        meta.append(f"unlocks {_date(m.unlock)}")
    if m.prerequisites:
        pre = ", ".join(esc(names.get(p, f"module {p} (not in tree)")) for p in m.prerequisites)
        meta.append(f"prerequisites: {pre}")
    if m.require_sequential_progress:
        meta.append("sequential progress")
    by_id = {it.canvas_id: it for it in m.items if it.canvas_id is not None}
    reqs: list[str] = []
    for r in m.completion_requirements:
        it = by_id.get(r.item_id)
        label = (it.title if it and it.title else None) or f"item {r.item_id}"
        score = f" {r.min_score:g}" if r.min_score is not None else ""
        reqs.append(esc(f"{r.type}{score}: {label}"))
    if reqs:
        meta.append("requirements: " + "; ".join(reqs))
    head = f"<h2>{link(obj_href(key), m.name)}</h2><p class=mute>{' &middot; '.join(meta)}</p>"
    if not m.items:
        return head + '<p class="mute">no items</p>'
    rows = "".join(_item_row(course, it, broken) for it in m.items)
    return head + table(["type", "title", "target", "date", ""], []).replace(
        "<tbody></tbody>", f"<tbody>{rows}</tbody>"
    )


def render(state: State, path: str, query: Mapping[str, list[str]]) -> Response:
    course = state.course
    if course is None:
        body = f'<h1>Modules</h1><p class="err">{esc(state.load_error)}</p>'
        return page("Modules", body, current="modules", config=None)
    idx = state.index()
    names = {m.canvas_id: m.name for m in course.modules if m.canvas_id is not None}
    body = "<h1>Modules</h1>"
    if not course.modules:
        body += '<p class="mute">no modules</p>'
    for key, m in module_keys(course.modules):
        broken = {lk.target for lk in idx.outbound.get(key, ()) if lk.broken}
        body += _module_block(course, key, m, names, broken)
    placed = {
        lk.target for links in idx.backlinks.values() for lk in links if lk.kind == "module_item"
    }
    # announcements can never be module items, so they are not "missing" from a module
    loose = sorted(
        k.path
        for k in course.objects
        if k.kind not in (Kind.MODULE, Kind.ANNOUNCEMENT) and k.path not in placed
    )
    body += "<h2>Not in any module</h2>"
    if loose:
        body += "<ul>" + "".join(f"<li>{link(obj_href(p), p)}</li>" for p in loose) + "</ul>"
    else:
        body += '<p class="mute">every object is in a module</p>'
    return page("Modules", body, current="modules", config=course.config)
