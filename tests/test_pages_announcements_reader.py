"""remote/pages.py and remote/announcements.py readers against FakeCanvas."""

from __future__ import annotations

from pathlib import Path

from canvas_sync.config import CourseConfig
from canvas_sync.ignore import IgnoreRules
from canvas_sync.model.announcement import Announcement
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import FilesMeta
from canvas_sync.model.page import Page
from canvas_sync.model.roster import Roster, RosterCache
from canvas_sync.remote import announcements, pages
from canvas_sync.remote.context import FetchContext
from canvas_sync.remote.paths import slugify, unique_path
from canvas_sync.render.bodies import Bodies
from canvas_sync.snapshot import BlobStore
from tests.fake_canvas.core import FakeCanvas
from tests.fake_canvas.discussion_topics import add_announcement, add_topic
from tests.fake_canvas.pages import add_page, rename_page


def make_cfg() -> CourseConfig:
    return CourseConfig(
        canvas_url="https://canvas.test",  # pyright: ignore[reportArgumentType]
        course_id=101,
        timezone="America/Vancouver",
    )


def course(objs: dict[str, CanvasObject]) -> Course:
    cfg = make_cfg()
    return Course(
        config=cfg,
        objects={o.key(p): o for p, o in objs.items()},
        files_meta=FilesMeta(),
        modules=[],
        roster=Roster(sections=[], groups_category=None),
    )


def ctx_for(
    tmp_path: Path,
    previous: dict[str, CanvasObject] | None = None,
    bodies: Bodies | None = None,
    ignore: list[str] | None = None,
) -> FetchContext:
    cfg = make_cfg()
    return FetchContext(
        config=cfg,
        tz=cfg.tz,
        bodies=bodies or Bodies(),
        previous=course(previous) if previous is not None else None,
        roster=RosterCache(),
        ignore=IgnoreRules(lines=ignore or []),
        blob_store=BlobStore(tmp_path),
    )


def test_slugify_and_collisions() -> None:
    assert slugify("Week 1: Intro & Setup!") == "week-1-intro-setup"
    assert slugify("Café Menu") == "cafe-menu"
    assert slugify("???") == "untitled"
    taken = {"pages/a.md", "pages/a-2.md"}
    assert unique_path("pages", "A", taken) == "pages/a-3.md"


def test_new_pages_get_slugified_paths(tmp_path: Path) -> None:
    fake = FakeCanvas()
    add_page(fake, "Syllabus", "<p>Hello <strong>there</strong></p>", front_page=True)
    add_page(
        fake,
        "Syllabus!",
        "<p>Other</p>",
        url="syllabus-x",
        published=False,
        publish_at="2026-09-08T16:00:00Z",
    )
    got = pages.read(fake.client("read"), ctx_for(tmp_path))
    assert sorted(got) == ["pages/syllabus-2.md", "pages/syllabus.md"]
    first = got["pages/syllabus.md"]
    assert isinstance(first, Page)
    assert first.canvas_id == "syllabus"
    assert first.front_page and first.published
    assert first.body == "Hello **there**"
    second = got["pages/syllabus-2.md"]
    assert isinstance(second, Page)
    assert second.published is False
    assert second.publish_at is not None and second.publish_at.hour == 9
    assert fake.writes() == []


def test_unchanged_html_reuses_markdown_byte_identical(tmp_path: Path) -> None:
    fake = FakeCanvas()
    p = add_page(fake, "Syllabus", "<p>rendered by md2html</p>")
    md = "Original   *markdown*  that pandoc would not reproduce\n\n* a\n* b"
    prev = Page(canvas_id="syllabus", title="Syllabus", published=True, body=md)
    bodies = Bodies()
    bodies.record("pages/syllabus.md", md, p["body"])
    got = pages.read(fake.client("read"), ctx_for(tmp_path, {"pages/syllabus.md": prev}, bodies))
    page = got["pages/syllabus.md"]
    assert isinstance(page, Page) and page.body == md


def test_ui_edited_html_is_converted(tmp_path: Path) -> None:
    fake = FakeCanvas()
    p = add_page(fake, "Syllabus", "<p>rendered</p>")
    prev = Page(canvas_id="syllabus", title="Syllabus", published=True, body="rendered")
    bodies = Bodies()
    bodies.record("pages/syllabus.md", "rendered", p["body"])
    p["body"] = "<p>edited in the <em>UI</em></p>"
    got = pages.read(fake.client("read"), ctx_for(tmp_path, {"pages/syllabus.md": prev}, bodies))
    page = got["pages/syllabus.md"]
    assert isinstance(page, Page) and page.body == "edited in the *UI*"


def test_html_body_format_is_kept_verbatim(tmp_path: Path) -> None:
    fake = FakeCanvas()
    add_page(fake, "Layout", '<div style="text-align:center">x</div>')
    prev = Page(canvas_id="layout", title="Layout", published=True, body_format="html", body="old")
    got = pages.read(fake.client("read"), ctx_for(tmp_path, {"pages/layout.md": prev}))
    page = got["pages/layout.md"]
    assert isinstance(page, Page)
    assert page.body_format == "html"
    assert page.body == '<div style="text-align:center">x</div>'


def test_renamed_page_keeps_tree_path(tmp_path: Path) -> None:
    fake = FakeCanvas()
    p = add_page(fake, "Course Info", "<p>x</p>")
    prev = Page(canvas_id="course-info", title="Course Info", published=True, body="x")
    rename_page(fake, p["page_id"], "Information")
    new = add_page(fake, "Brand New", "<p>n</p>")
    got = pages.read(fake.client("read"), ctx_for(tmp_path, {"pages/info-page.md": prev}))
    kept = got["pages/info-page.md"]
    assert isinstance(kept, Page)
    assert kept.canvas_id == "information" and kept.title == "Information"
    other = got["pages/brand-new.md"]
    assert isinstance(other, Page) and other.canvas_id == new["url"]
    assert len(got) == 2


def test_page_deleted_on_canvas_is_absent(tmp_path: Path) -> None:
    fake = FakeCanvas()
    add_page(fake, "Keep", "<p>k</p>")
    prev = Page(canvas_id="gone", title="Gone", published=True, body="g")
    got = pages.read(fake.client("read"), ctx_for(tmp_path, {"pages/gone.md": prev}))
    assert sorted(got) == ["pages/keep.md"]


def test_ignored_pages_are_dropped(tmp_path: Path) -> None:
    fake = FakeCanvas()
    add_page(fake, "Secret", "<p>s</p>")
    add_page(fake, "Open", "<p>o</p>")
    got = pages.read(fake.client("read"), ctx_for(tmp_path, ignore=["pages/secret.md"]))
    assert sorted(got) == ["pages/open.md"]


def test_page_links_become_relative(tmp_path: Path) -> None:
    fake = FakeCanvas()
    home = add_page(fake, "Home")
    home["body"] = '<p><a href="/courses/101/pages/other">other</a></p>\n'
    add_page(fake, "Other", "<p>o</p>")
    got = pages.read(fake.client("read"), ctx_for(tmp_path))
    page = got["pages/home.md"]
    assert isinstance(page, Page) and page.body == "[other](other.md)"


def test_announcements(tmp_path: Path) -> None:
    fake = FakeCanvas()
    add_topic(fake, "A discussion", "<p>not an announcement</p>")
    a = add_announcement(fake, "Welcome!", "<p>hi</p>", delayed_post_at="2026-09-01T16:00:00Z")
    b = add_announcement(fake, "Welcome", "<p>again</p>", published=False)
    got = announcements.read(fake.client("read"), ctx_for(tmp_path))
    assert sorted(got) == ["announcements/welcome-2.md", "announcements/welcome.md"]
    first = got["announcements/welcome.md"]
    assert isinstance(first, Announcement)
    assert first.canvas_id == a["id"] and first.body == "hi"
    assert first.post_at is not None and first.post_at.hour == 9
    second = got["announcements/welcome-2.md"]
    assert isinstance(second, Announcement)
    assert second.canvas_id == b["id"] and second.published is False
    assert fake.writes() == []
    assert any(r.params.get("only_announcements") == "true" for r in fake.requests("GET"))  # pyright: ignore[reportUnknownMemberType]


def test_renamed_announcement_keeps_path_and_body(tmp_path: Path) -> None:
    fake = FakeCanvas()
    a = add_announcement(fake, "Renamed on Canvas", "<p>rendered</p>")
    md = "Kept   markdown"
    prev = Announcement(canvas_id=a["id"], title="Old title", body=md)
    bodies = Bodies()
    bodies.record("announcements/old.md", md, a["message"])
    got = announcements.read(
        fake.client("read"), ctx_for(tmp_path, {"announcements/old.md": prev}, bodies)
    )
    assert list(got) == ["announcements/old.md"]
    ann = got["announcements/old.md"]
    assert isinstance(ann, Announcement)
    assert ann.title == "Renamed on Canvas" and ann.body == md


def test_object_key_kinds() -> None:
    assert ObjectKey(Kind.PAGE, "pages/a.md").kind == Kind.PAGE


def test_slugify_caps_length_and_unique_path_ignores_case() -> None:
    assert len(slugify("word " * 50)) <= 80
    assert unique_path("pages", "Intro", {"pages/INTRO.md"}) == "pages/intro-2.md"


def test_new_object_does_not_inherit_deleted_objects_body_format(tmp_path: Path) -> None:
    fake = FakeCanvas()
    add_page(fake, "Layout", "<p>fresh <em>page</em></p>")
    prev = Page(canvas_id="deleted", title="Layout", published=True, body_format="html", body="x")
    got = pages.read(fake.client("read"), ctx_for(tmp_path, {"pages/layout.md": prev}))
    page = got["pages/layout.md"]
    assert isinstance(page, Page)
    assert page.body_format == "md" and page.body == "fresh *page*"
    assert pages.assign_paths(fake.client("read"), ctx_for(tmp_path)) == {
        "layout": "pages/layout.md"
    }


def test_paths_map_overrides_previous_for_links(tmp_path: Path) -> None:
    fake = FakeCanvas()
    home = add_page(fake, "Home")
    home["body"] = '<p><a href="/courses/101/assignments/55">hw</a></p>\n'
    paths: dict[tuple[Kind, int | str], str] = {
        (Kind.PAGE, "home"): "pages/home.md",
        (Kind.ASSIGNMENT, 55): "assignments/hw-one.md",
    }
    got = pages.read(fake.client("read"), ctx_for(tmp_path), paths=paths)
    page = got["pages/home.md"]
    assert isinstance(page, Page) and page.body == "[hw](../assignments/hw-one.md)"


def test_stale_previous_ids_dropped_without_paths(tmp_path: Path) -> None:
    fake = FakeCanvas()
    home = add_page(fake, "Home")
    home["body"] = '<p><a href="/courses/101/pages/gone">g</a></p>\n'
    prev = Page(canvas_id="gone", title="Gone", published=True, body="g")
    got = pages.read(fake.client("read"), ctx_for(tmp_path, {"pages/gone.md": prev}))
    page = got["pages/home.md"]
    assert isinstance(page, Page) and "gone.md" not in page.body


def test_slug_reuse_keeps_path_bound_to_renamed_page(tmp_path: Path) -> None:
    fake = FakeCanvas()
    a = add_page(fake, "Intro", "<p>a</p>")
    prev = Page(canvas_id="intro", page_id=a["page_id"], title="Intro", published=True, body="a")
    rename_page(fake, a["page_id"], "Welcome")
    b = add_page(fake, "Intro", "<p>b</p>")  # a new page takes the freed slug
    assert b["url"] == "intro"
    got = pages.read(fake.client("read"), ctx_for(tmp_path, {"pages/intro.md": prev}))
    kept, new = got["pages/intro.md"], got["pages/intro-2.md"]
    assert isinstance(kept, Page) and isinstance(new, Page)
    assert (kept.canvas_id, kept.page_id, kept.body) == ("welcome", a["page_id"], "a")
    assert (new.canvas_id, new.page_id) == ("intro", b["page_id"])
    assert len(got) == 2


def test_deleted_page_id_does_not_match_reused_slug(tmp_path: Path) -> None:
    fake = FakeCanvas()
    prev = Page(canvas_id="intro", page_id=999, title="Intro", body="old")
    add_page(fake, "Intro", "<p>b</p>")
    got = pages.read(fake.client("read"), ctx_for(tmp_path, {"pages/intro.md": prev}))
    new = got["pages/intro.md"]
    assert isinstance(new, Page) and new.page_id != 999 and new.body == "b"


def test_legacy_file_without_page_id_matches_by_slug(tmp_path: Path) -> None:
    fake = FakeCanvas()
    a = add_page(fake, "Intro", "<p>a</p>")
    prev = Page(canvas_id="intro", title="Intro", published=True, body="a")
    got = pages.read(fake.client("read"), ctx_for(tmp_path, {"pages/x.md": prev}))
    kept = got["pages/x.md"]
    assert list(got) == ["pages/x.md"]
    assert isinstance(kept, Page) and kept.page_id == a["page_id"]
