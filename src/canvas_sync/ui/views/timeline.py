"""Timeline (`/timeline`): every dated thing in the course, by week (UI.md section 4)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from canvas_sync.commands.validate import date_problems
from canvas_sync.model.assignment import Assignment
from canvas_sync.model.base import Kind
from canvas_sync.model.course import Course
from canvas_sync.model.overrides import Override
from canvas_sync.ui.layout import Response, badge, error_page, esc, link, obj_href, page
from canvas_sync.ui.state import State

KINDS = ("assignment", "page", "announcement", "module", "file")
_KIND_OF = {
    Kind.ASSIGNMENT: "assignment",
    Kind.PAGE: "page",
    Kind.ANNOUNCEMENT: "announcement",
    Kind.MODULE: "module",
    Kind.FILE: "file",
}
# event label -> attribute name used by the validate date checks
_ATTR = {"publish": "publish_at", "post": "post_at"}
_ASSIGN = ("unlock", "due", "lock")


@dataclass(frozen=True)
class Event:
    when: datetime  # aware, in the course timezone
    field: str  # unlock, due, lock, publish, post
    kind: str  # one of KINDS
    path: str  # tree path, or "modules.yaml#<name>" for a module
    title: str
    target: str | None = None  # "section X" / "group X" / "students: a, b" for an override
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Timeline:
    events: list[Event]  # sorted by time
    undated: list[tuple[str, str, str]]  # (kind, path, title)


def _target(o: Override) -> str:
    kind, names = o.target()
    return f"{kind} {names[0]}" if kind != "students" else "students: " + ", ".join(names)


def _own(obj: object, fields: tuple[str, ...], kind: str, path: str, title: str) -> list[Event]:
    """Events for the date fields of ``obj``; flags come from validate's date checks."""
    problems = date_problems(obj)
    out: list[Event] = []
    for label in fields:
        attr = _ATTR.get(label, label)
        v = getattr(obj, attr, None)
        if isinstance(v, datetime):
            flags = tuple(p.message for p in problems if attr in p.fields)
            out.append(Event(v, label, kind, path, title, None, flags))
    return out


def _override_events(
    overrides: list[Override], base: object, kind: str, path: str, title: str
) -> list[Event]:
    base_lock = getattr(base, "lock", None)
    out: list[Event] = []
    for o in overrides:
        problems = date_problems(o)
        for label in _ASSIGN:
            v = getattr(o, label, None)
            if not isinstance(v, datetime):
                continue
            flags = [p.message for p in problems if label in p.fields]
            if label == "due" and isinstance(base_lock, datetime) and v > base_lock:
                flags.append("override due is after the base lock")
            out.append(Event(v, label, kind, path, title, _target(o), tuple(flags)))
    return out


def extract(course: Course) -> Timeline:
    """All dated events of the tree, and the objects that have no date at all. Pure."""
    events: list[Event] = []
    undated: list[tuple[str, str, str]] = []

    def add(found: list[Event], kind: str, path: str, title: str) -> None:
        events.extend(found)
        if not found:
            undated.append((kind, path, title))

    for key, obj in sorted(course.objects.items()):
        kind = _KIND_OF[key.kind]
        title = getattr(obj, "title", "") or key.path
        if key.kind is Kind.ASSIGNMENT and isinstance(obj, Assignment):
            found = _own(obj, _ASSIGN, kind, key.path, title)
            found += _override_events(obj.overrides, obj, kind, key.path, title)
        elif key.kind is Kind.PAGE:
            found = _own(obj, ("publish",), kind, key.path, title)
        elif key.kind is Kind.ANNOUNCEMENT:
            found = _own(obj, ("post",), kind, key.path, title)
        elif key.kind is Kind.FILE:
            eff = course.files_meta.effective(key.path)  # defaults, rules, entry
            found = _own(eff, ("unlock", "lock"), kind, key.path, key.path)
        else:
            continue
        add(found, kind, key.path, title)
    for mod in course.modules:
        path = f"modules.yaml#{mod.name}"
        found = _own(mod, ("unlock",), "module", path, mod.name)
        for o in mod.overrides:
            if o.unlock is not None:
                found.append(Event(o.unlock, "unlock", "module", path, mod.name, _target(o)))
        add(found, "module", path, mod.name)
    events.sort(key=lambda e: (e.when.timestamp(), e.path, e.field, e.target or ""))
    return Timeline(events, undated)


def by_week(events: list[Event]) -> list[tuple[date, list[Event]]]:
    """Group events by the Monday of their ISO week, read off each event's local
    (course timezone) wall-clock date, so a DST change never moves an event across weeks."""
    out: dict[date, list[Event]] = {}
    for e in events:
        local = e.when.date()
        out.setdefault(local - timedelta(days=local.weekday()), []).append(e)
    return sorted(out.items())


def _row(e: Event) -> str:
    what = e.title + (f" ({e.target})" if e.target else "")
    flags = "".join(f' <span class="warn">[!] {esc(f)}</span>' for f in e.flags)
    when = f"{e.when:%a %b} {e.when.day:>2} {e.when:%H:%M}"
    return (
        f"<tr><td>{esc(when)}</td><td>{badge(e.field)}</td><td>{esc(e.kind)}</td>"
        f"<td>{link(obj_href(e.path), what)}</td><td><code>{esc(e.path)}</code>{flags}</td></tr>"
    )


def _week_label(start: date) -> str:
    end = start + timedelta(days=6)
    return f"Week of {start:%b} {start.day} - {end:%b} {end.day}"


def render(state: State, path: str, query: Mapping[str, list[str]]) -> Response:
    c = state.course
    if c is None:
        return error_page(state.load_error or "course failed to load")
    want = (query.get("kind") or [""])[0]
    tl = extract(c)
    filt = " ".join([link("/timeline", "all")] + [link(f"/timeline?kind={k}", k) for k in KINDS])
    body = (
        f'<h1>Timeline</h1><p class="mute">course timezone: {esc(c.config.timezone)}</p>'
        f"<p>Filter: {filt}</p>"
    )
    events = [e for e in tl.events if not want or e.kind == want]
    if want and want not in KINDS:
        body += f'<p class="err">unknown kind {esc(want)}</p>'
        events = []
    for start, evs in by_week(events):
        body += f"<h2>{esc(_week_label(start))}</h2><table><tbody>"
        body += "".join(_row(e) for e in evs) + "</tbody></table>"
    if not events:
        body += '<p class="mute">No dated events.</p>'
    undated = [u for u in tl.undated if not want or u[0] == want]
    if undated:
        items = "".join(
            f"<li>{link(obj_href(p), t)} <code>{esc(p)}</code></li>" for _k, p, t in undated
        )
        body += (
            f"<details><summary>Undated: {len(undated)} objects</summary><ul>{items}</ul></details>"
        )
    return page("Timeline", body, current="timeline", config=c.config)
