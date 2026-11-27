"""Explorer UI timeline: event extraction, week grouping, page. Offline."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from canvas_sync.config import CourseConfig
from canvas_sync.dates import parse_local
from canvas_sync.model.announcement import Announcement
from canvas_sync.model.assignment import Assignment
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import FileEntry, FileRule, FileSettings, FilesMeta
from canvas_sync.model.modules import Module, ModuleOverride
from canvas_sync.model.overrides import Override
from canvas_sync.model.page import Page
from canvas_sync.model.roster import Roster
from canvas_sync.ui.server import dispatch
from canvas_sync.ui.views.timeline import by_week, extract
from tests.helpers.ui_repo import COURSE_YAML, make_repo

TZ = ZoneInfo("America/New_York")


def dt(s: str) -> datetime:
    return parse_local(s, TZ, time(0, 0))


def course(
    objs: dict[ObjectKey, CanvasObject],
    modules: list[Module] | None = None,
    meta: FilesMeta | None = None,
) -> Course:
    cfg = CourseConfig(
        canvas_url="https://canvas.example.edu",  # pyright: ignore[reportArgumentType]
        course_id=1,
        timezone="America/New_York",
    )
    return Course(cfg, objs, meta or FilesMeta(), modules or [], Roster())


def test_assignment_dates_and_overrides() -> None:
    a = Assignment(
        title="HW1",
        unlock=dt("2026-09-28 09:00"),
        due=dt("2026-10-02 23:59"),
        lock=dt("2026-10-04 23:59"),
        overrides=[
            Override(section="B", due=dt("2026-10-05 12:00")),
            Override(group="late", unlock=dt("2026-09-27 08:00")),
        ],
    )
    tl = extract(course({ObjectKey(Kind.ASSIGNMENT, "assignments/hw1.md"): a}))
    got = [(e.field, e.target) for e in tl.events]
    assert got == [
        ("unlock", "group late"),
        ("unlock", None),
        ("due", None),
        ("lock", None),
        ("due", "section B"),
    ]
    assert tl.undated == []
    late = tl.events[-1]
    assert late.flags == ("override due is after the base lock",)
    assert all(e.flags == () for e in tl.events[:-1])


def test_page_announcement_module_file_dates() -> None:
    b_zip = FileEntry(path="b.zip", lock=dt("2026-12-01 00:00"))
    objs: dict[ObjectKey, CanvasObject] = {
        ObjectKey(Kind.PAGE, "pages/p.md"): Page(title="P", publish_at=dt("2026-09-30 09:00")),
        ObjectKey(Kind.PAGE, "pages/q.md"): Page(title="Q"),
        ObjectKey(Kind.ANNOUNCEMENT, "announcements/n.md"): Announcement(
            title="N", post_at=dt("2026-10-01 10:00")
        ),
        ObjectKey(Kind.FILE, "files/a.pdf"): FileEntry(path="a.pdf"),
        ObjectKey(Kind.FILE, "files/b.zip"): b_zip,
    }
    meta = FilesMeta(
        rules=[FileRule(match="*.pdf", settings=FileSettings(unlock=dt("2026-10-10 08:00")))],
        files={"files/b.zip": b_zip},
    )
    mod = Module(
        name="Week 1",
        unlock=dt("2026-09-28 00:00"),
        overrides=[ModuleOverride(section="B", unlock=dt("2026-09-29 00:00"))],
    )
    tl = extract(course(objs, [mod], meta))
    got = [(e.kind, e.field, e.path, e.target) for e in tl.events]
    assert got == [
        ("module", "unlock", "modules.yaml#Week 1", None),
        ("module", "unlock", "modules.yaml#Week 1", "section B"),
        ("page", "publish", "pages/p.md", None),
        ("announcement", "post", "announcements/n.md", None),
        ("file", "unlock", "files/a.pdf", None),  # from the rule
        ("file", "lock", "files/b.zip", None),
    ]
    assert [u[1] for u in tl.undated] == ["pages/q.md"]


def test_flags_reuse_validate_checks() -> None:
    a = Assignment(title="H", unlock=dt("2026-10-05 09:00"), due=dt("2026-10-02 09:00"))
    tl = extract(course({ObjectKey(Kind.ASSIGNMENT, "assignments/h.md"): a}))
    by_field = {e.field: e.flags for e in tl.events}
    assert by_field["unlock"] == ("unlock is after due",)
    assert by_field["due"] == ("unlock is after due",)


def test_week_grouping_across_dst_change() -> None:
    # US fall-back is Sun 2026-11-01 at 02:00; Monday Nov 2 starts a new week in local time
    # even though the UTC offset changed.
    a = Assignment(
        title="H",
        unlock=dt("2026-10-26 00:00"),  # Monday, EDT
        due=dt("2026-11-01 23:59"),  # Sunday after fall-back, EST
        lock=dt("2026-11-02 00:30"),  # Monday, EST
    )
    tl = extract(course({ObjectKey(Kind.ASSIGNMENT, "assignments/h.md"): a}))
    weeks = by_week(tl.events)
    assert [(str(w), [e.field for e in evs]) for w, evs in weeks] == [
        ("2026-10-26", ["unlock", "due"]),
        ("2026-11-02", ["lock"]),
    ]


def test_week_grouping_spring_forward() -> None:
    a = Assignment(title="H", unlock=dt("2026-03-08 01:30"), due=dt("2026-03-09 00:00"))
    weeks = by_week(extract(course({ObjectKey(Kind.ASSIGNMENT, "assignments/h.md"): a})).events)
    assert [str(w) for w, _ in weeks] == ["2026-03-02", "2026-03-09"]


FILES = {
    "course.yaml": COURSE_YAML,
    "pages/a.md": "---\ntitle: <b>A</b>\npublish_at: 2026-09-30 09:00\n---\n\nhi\n",
    "pages/b.md": "---\ntitle: B\n---\n\nhi\n",
    "assignments/hw1.md": (
        "---\ntitle: HW1\nunlock: 2026-09-28 09:00\ndue: 2026-10-02 23:59\n"
        "lock: 2026-10-01 23:59\n"
        "overrides:\n- section: Sec B\n  due: 2026-10-03 12:00\n---\n\nbody\n"
    ),
}


def test_page_renders_weeks_flags_links_and_filter(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    r = dispatch(root, "/timeline")
    assert r.status == 200
    html = r.body.decode()
    assert "Week of Sep 28 - Oct 4" in html
    assert "America/New_York" in html
    assert "due is after lock" in html
    assert "override due is after the base lock" in html
    assert "HW1 (section Sec B)" in html
    assert 'href="/o/assignments/hw1.md"' in html
    assert "&lt;b&gt;A&lt;/b&gt;" in html and "<b>A</b>" not in html
    assert "Undated: 1 objects" in html and "pages/b.md" in html
    only = dispatch(root, "/timeline?kind=page").body.decode()
    assert "pages/a.md" in only and "assignments/hw1.md" not in only
    assert dispatch(root, "/timeline?kind=bogus").status == 200


def test_empty_course(tmp_path: Path) -> None:
    html = dispatch(make_repo(tmp_path, {"course.yaml": COURSE_YAML}), "/timeline").body.decode()
    assert "No dated events" in html
