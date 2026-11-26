"""Page and Announcement TypeSpecs: fixtures, normalisation, validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from canvas_sync.config import CourseConfig
from canvas_sync.dates import parse_local
from canvas_sync.errors import ValidationError
from canvas_sync.model.announcement import ANNOUNCEMENT_SPEC, Announcement
from canvas_sync.model.base import CanvasObject, TypeSpec
from canvas_sync.model.page import PAGE_SPEC, Page
from tests.helpers.fixed_point import (
    assert_spec_fixed_point,
    assert_spec_normalizes,
    write_files,
)

FIXTURES = Path(__file__).parent / "fixtures" / "trees"


@pytest.fixture
def cfg() -> CourseConfig:
    return CourseConfig.model_validate(
        {
            "canvas_url": "https://canvas.example.edu",
            "course_id": 1,
            "timezone": "America/Vancouver",
        }
    )


def test_page_fixtures_fixed_point(cfg: CourseConfig, tmp_path: Path) -> None:
    objs = PAGE_SPEC.load(FIXTURES, cfg)
    assert set(objs) == {"pages/layout.md", "pages/syllabus.md"}
    out = assert_spec_fixed_point(PAGE_SPEC, objs, cfg, tmp_path)
    for rel, data in out.items():  # fixtures are already canonical
        assert data == (FIXTURES / rel).read_bytes()
    syl = objs["pages/syllabus.md"]
    assert isinstance(syl, Page) and syl.front_page and syl.canvas_id == "syllabus"


def test_announcement_fixtures_fixed_point(cfg: CourseConfig, tmp_path: Path) -> None:
    objs = ANNOUNCEMENT_SPEC.load(FIXTURES, cfg)
    assert set(objs) == {"announcements/draft.md", "announcements/welcome.md"}
    out = assert_spec_fixed_point(ANNOUNCEMENT_SPEC, objs, cfg, tmp_path)
    for rel, data in out.items():
        assert data == (FIXTURES / rel).read_bytes()
    draft = objs["announcements/draft.md"]
    assert isinstance(draft, Announcement) and draft.published is False and draft.canvas_id is None


def test_page_date_only_and_messy_input(cfg: CourseConfig, tmp_path: Path) -> None:
    raw = (
        "﻿---\r\ntitle: T\r\npublish_at: 2026-09-08\r\ncanvas_id: t\r\n"
        "front_page: false\r\n---\r\n\r\nHi  \r\n"
    )
    expected = "---\ncanvas_id: t\ntitle: T\npublish_at: 2026-09-08 09:00\n---\n\nHi\n"
    assert_spec_normalizes(PAGE_SPEC, {"pages/t.md": raw}, {"pages/t.md": expected}, cfg, tmp_path)


def test_announcement_date_only_and_defaults(cfg: CourseConfig, tmp_path: Path) -> None:
    raw = (
        "---\npost_at: 2026-09-08\npublished: true\nbody_format: md\n"
        "canvas_id: 5\ntitle: A\n---\nBody"
    )
    expected = "---\ncanvas_id: 5\ntitle: A\npost_at: 2026-09-08 09:00\n---\n\nBody\n"
    path = "announcements/a.md"
    assert_spec_normalizes(ANNOUNCEMENT_SPEC, {path: raw}, {path: expected}, cfg, tmp_path)


def test_null_equals_absent(cfg: CourseConfig, tmp_path: Path) -> None:
    raw = "---\ntitle: T\npublished: null\npublish_at:\n---\nx\n"
    expected = "---\ntitle: T\n---\n\nx\n"
    assert_spec_normalizes(PAGE_SPEC, {"pages/t.md": raw}, {"pages/t.md": expected}, cfg, tmp_path)


def test_page_published_not_filled_from_course_defaults(cfg: CourseConfig, tmp_path: Path) -> None:
    write_files(tmp_path, {"pages/t.md": "---\ntitle: T\n---\n"})
    page = PAGE_SPEC.load(tmp_path, cfg)["pages/t.md"]
    assert isinstance(page, Page) and page.published is None and page.publish_at is None


def test_publish_at_uses_unlock_time_default(cfg: CourseConfig, tmp_path: Path) -> None:
    write_files(tmp_path, {"pages/t.md": "---\npublish_at: 2026-09-08\n---\n"})
    page = PAGE_SPEC.load(tmp_path, cfg)["pages/t.md"]
    assert isinstance(page, Page)
    assert page.publish_at == parse_local("2026-09-08 09:00", cfg.tz, cfg.defaults.unlock_time)


@pytest.mark.parametrize(
    ("spec", "path"), [(PAGE_SPEC, "pages/x.md"), (ANNOUNCEMENT_SPEC, "announcements/x.md")]
)
def test_unknown_key_names_file_and_key(
    spec: TypeSpec, path: str, cfg: CourseConfig, tmp_path: Path
) -> None:
    write_files(tmp_path, {path: "---\ntitle: X\nbogus_key: 1\n---\nbody\n"})
    with pytest.raises(ValidationError) as ei:
        spec.load(tmp_path, cfg)
    assert path in str(ei.value) and "bogus_key" in str(ei.value)


def test_bad_value_names_file(cfg: CourseConfig, tmp_path: Path) -> None:
    write_files(tmp_path, {"pages/x.md": "---\nbody_format: rtf\n---\n"})
    with pytest.raises(ValidationError, match=r"pages/x\.md.*body_format"):
        PAGE_SPEC.load(tmp_path, cfg)
    write_files(tmp_path, {"announcements/x.md": "---\npost_at: soon\n---\n"})
    with pytest.raises(ValidationError):
        ANNOUNCEMENT_SPEC.load(tmp_path, cfg)


def test_numeric_slug_stays_string(cfg: CourseConfig, tmp_path: Path) -> None:
    objs: dict[str, CanvasObject] = {"pages/n.md": Page(canvas_id="2026", title="N", body="x")}
    out = assert_spec_fixed_point(PAGE_SPEC, objs, cfg, tmp_path)
    assert b'canvas_id: "2026"' in out["pages/n.md"]


def test_dump_independent_of_dict_order(cfg: CourseConfig) -> None:
    a: dict[str, CanvasObject] = {"pages/a.md": Page(title="A"), "pages/b.md": Page(title="B")}
    b = dict(reversed(list(a.items())))
    assert list(PAGE_SPEC.dump(a, cfg)) == list(PAGE_SPEC.dump(b, cfg))
    assert list(PAGE_SPEC.dump(a, cfg)) == ["pages/a.md", "pages/b.md"]


def test_missing_folder_loads_empty(cfg: CourseConfig, tmp_path: Path) -> None:
    assert PAGE_SPEC.load(tmp_path, cfg) == {}
    assert ANNOUNCEMENT_SPEC.load(tmp_path, cfg) == {}


@pytest.mark.parametrize(
    ("spec", "folder"), [(PAGE_SPEC, "pages"), (ANNOUNCEMENT_SPEC, "announcements")]
)
@pytest.mark.parametrize("name", ["old/x.md", "x.markdown", "x.MD", "x.txt"])
def test_unexpected_entries_rejected(
    spec: TypeSpec, folder: str, name: str, cfg: CourseConfig, tmp_path: Path
) -> None:
    write_files(tmp_path, {f"{folder}/{name}": "---\ntitle: X\n---\n"})
    with pytest.raises(ValidationError, match=rf"{folder}/{name.split('/')[0]}"):
        spec.load(tmp_path, cfg)


def test_hidden_entries_skipped(cfg: CourseConfig, tmp_path: Path) -> None:
    write_files(tmp_path, {"pages/.DS_Store": "x", "pages/.hidden/y.md": "z"})
    assert PAGE_SPEC.load(tmp_path, cfg) == {}


def test_duplicate_canvas_id_rejected(cfg: CourseConfig, tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "pages/a.md": "---\ncanvas_id: s\n---\n",
            "pages/b.md": "---\ncanvas_id: s\n---\n",
            "announcements/a.md": "---\ncanvas_id: 7\n---\n",
            "announcements/b.md": '---\ncanvas_id: "7"\n---\n',
        },
    )
    with pytest.raises(ValidationError, match=r"pages/b\.md.*pages/a\.md"):
        PAGE_SPEC.load(tmp_path, cfg)
    with pytest.raises(ValidationError, match=r"announcements/b\.md.*announcements/a\.md"):
        ANNOUNCEMENT_SPEC.load(tmp_path, cfg)


def test_announcement_canvas_id_string_coerced(cfg: CourseConfig, tmp_path: Path) -> None:
    write_files(tmp_path, {"announcements/a.md": '---\ncanvas_id: "776655"\n---\n'})
    assert ANNOUNCEMENT_SPEC.load(tmp_path, cfg)["announcements/a.md"].canvas_id == 776655
    write_files(tmp_path, {"announcements/a.md": "---\ncanvas_id: abc\n---\n"})
    with pytest.raises(ValidationError, match=r"announcements/a\.md"):
        ANNOUNCEMENT_SPEC.load(tmp_path, cfg)
