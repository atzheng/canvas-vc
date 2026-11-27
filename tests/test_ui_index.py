"""Reference index (CONTRACTS.md section 13): pure logic on small invented courses."""

from __future__ import annotations

from canvas_sync.config import CourseConfig
from canvas_sync.model.announcement import Announcement
from canvas_sync.model.assignment import Assignment
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import FileEntry, FilesMeta
from canvas_sync.model.modules import Module, ModuleItem
from canvas_sync.model.page import Page
from canvas_sync.model.roster import Roster
from canvas_sync.ui.index import Index, Link, build_index


def make_course(
    pages: dict[str, str] | None = None,
    assignments: dict[str, str] | None = None,
    announcements: dict[str, str] | None = None,
    files: tuple[str, ...] = (),
    modules: list[Module] | None = None,
    html_pages: dict[str, str] | None = None,
) -> Course:
    cfg = CourseConfig(
        canvas_url="https://canvas.example.edu",  # pyright: ignore[reportArgumentType]
        course_id=1,
        timezone="America/Vancouver",
    )
    objs: dict[ObjectKey, CanvasObject] = {}
    for n, b in (pages or {}).items():
        objs[ObjectKey(Kind.PAGE, f"pages/{n}.md")] = Page(title=n, body=b)
    for n, b in (html_pages or {}).items():
        objs[ObjectKey(Kind.PAGE, f"pages/{n}.md")] = Page(title=n, body=b, body_format="html")
    for n, b in (assignments or {}).items():
        objs[ObjectKey(Kind.ASSIGNMENT, f"assignments/{n}.md")] = Assignment(title=n, body=b)
    for n, b in (announcements or {}).items():
        objs[ObjectKey(Kind.ANNOUNCEMENT, f"announcements/{n}.md")] = Announcement(title=n, body=b)
    entries = {f"files/{f}": FileEntry(path=f) for f in files}
    for p, e in entries.items():
        objs[ObjectKey(Kind.FILE, p)] = e
    return Course(cfg, objs, FilesMeta(files=entries), modules or [], Roster())


def targets(idx: Index, src: str) -> list[tuple[str, str, bool]]:
    return [(lk.target, lk.kind, lk.broken) for lk in idx.outbound.get(src, ())]


def test_resolved_link_and_backlink() -> None:
    c = make_course(pages={"a": "see [b](b.md)", "b": "hi"})
    idx = build_index(c)
    assert targets(idx, "pages/a.md") == [("pages/b.md", "link", False)]
    assert [lk.source for lk in idx.backlinks["pages/b.md"]] == ["pages/a.md"]
    assert idx.broken == ()
    assert idx.orphans == ("pages/a.md",)  # b is linked from a; a is linked from nowhere


def test_broken_link_keeps_raw_href() -> None:
    c = make_course(pages={"a": "[x](nope.md) and [y](../../escape.md)"})
    idx = build_index(c)
    assert [lk.target for lk in idx.broken] == ["nope.md", "../../escape.md"]
    assert all(lk.broken for lk in idx.outbound["pages/a.md"])
    assert idx.backlinks == {}


def test_anchor_and_query_stripped() -> None:
    c = make_course(pages={"a": "[x](b.md#sec) [y](b.md?x=1#z) [z](#top)", "b": ""})
    idx = build_index(c)
    assert targets(idx, "pages/a.md") == [
        ("pages/b.md", "link", False),
        ("pages/b.md", "link", False),
    ]
    assert len(idx.backlinks["pages/b.md"]) == 2


def test_external_and_anchor_only_skipped() -> None:
    c = make_course(
        pages={"a": "[x](https://e.com) [m](mailto:a@b.c) [t](#top) [r](/courses/1/pages/x)"}
    )
    assert build_index(c).outbound == {}


def test_image_and_file_links_and_parent_paths() -> None:
    c = make_course(
        pages={
            "a": (
                "![pic](../files/img/p.png)\n\n[doc](../files/notes%20v2.pdf)\n"
                "[bad](../files/no.pdf)"
            )
        },
        files=("img/p.png", "notes v2.pdf", "unused.txt"),
    )
    idx = build_index(c)
    assert targets(idx, "pages/a.md") == [
        ("files/img/p.png", "image", False),
        ("files/notes v2.pdf", "link", False),
        ("../files/no.pdf", "link", True),
    ]
    assert idx.unreferenced_files == ("files/unused.txt",)
    assert "files/img/p.png" in idx.backlinks
    assert idx.backlinks["files/img/p.png"][0].kind == "image"


def test_missing_file_target_is_reported_broken() -> None:
    c = make_course(pages={"a": "[d](../files/gone.pdf)"})
    idx = build_index(c)
    assert len(idx.broken) == 1
    assert idx.broken[0].broken


def test_raw_html_in_markdown_and_html_body() -> None:
    c = make_course(
        pages={"a": '<a href="b.md">x</a>\n\ntext <img src="../files/p.png"> end', "b": ""},
        html_pages={"h": '<p>\n<a href="b.md#q">y</a><img src="../files/p.png"></p>'},
        files=("p.png",),
    )
    idx = build_index(c)
    assert targets(idx, "pages/a.md") == [
        ("pages/b.md", "link", False),
        ("files/p.png", "image", False),
    ]
    assert targets(idx, "pages/h.md") == [
        ("pages/b.md", "link", False),
        ("files/p.png", "image", False),
    ]
    assert idx.outbound["pages/h.md"][0].line == 2
    assert idx.unreferenced_files == ()


def test_line_numbers() -> None:
    c = make_course(pages={"a": "intro\n\nline [x](b.md)\ncont [y](b.md)", "b": ""})
    lines = [lk.line for lk in build_index(c).outbound["pages/a.md"]]
    assert lines == [3, 4]


def test_backlinks_from_modules_and_orphans() -> None:
    mod = Module(
        name="Week 1",
        items=[
            ModuleItem(page="a"),
            ModuleItem(assignment="hw1"),
            ModuleItem(file="l/01.pdf"),
            ModuleItem(header="Hdr"),
            ModuleItem(page="ghost"),
        ],
    )
    c = make_course(
        pages={"a": "", "lonely": "", "linked": ""},
        assignments={"hw1": "[p](../pages/linked.md)", "hw2": ""},
        announcements={"n1": ""},
        files=("l/01.pdf", "l/02.pdf"),
        modules=[mod],
    )
    idx = build_index(c)
    assert [lk.source for lk in idx.backlinks["pages/a.md"]] == ["modules.yaml#Week 1"]
    assert idx.backlinks["pages/a.md"][0].kind == "module_item"
    assert "files/l/01.pdf" in idx.backlinks
    assert idx.orphans == ("announcements/n1.md", "assignments/hw2.md", "pages/lonely.md")
    assert idx.unreferenced_files == ("files/l/02.pdf",)
    assert idx.broken == (Link("modules.yaml#Week 1", "pages/ghost.md", "module_item", None, True),)


def test_duplicate_module_names_get_distinct_keys() -> None:
    c = make_course(
        pages={"a": "", "b": ""},
        modules=[
            Module(name="W", items=[ModuleItem(page="a")]),
            Module(name="W", items=[ModuleItem(page="b")]),
        ],
    )
    idx = build_index(c)
    assert idx.backlinks["pages/a.md"][0].source == "modules.yaml#W"
    assert idx.backlinks["pages/b.md"][0].source == "modules.yaml#W#2"


def test_self_link_does_not_prevent_orphan() -> None:
    c = make_course(pages={"a": "[me](a.md)"})
    idx = build_index(c)
    assert idx.orphans == ("pages/a.md",)
    assert idx.backlinks == {}


def test_deterministic_order() -> None:
    c = make_course(pages={"z": "[a](a.md)", "a": "[z](z.md)", "m": "[a](a.md)"})
    idx = build_index(c)
    assert list(idx.outbound) == sorted(idx.outbound)
    assert [lk.source for lk in idx.backlinks["pages/a.md"]] == ["pages/m.md", "pages/z.md"]
