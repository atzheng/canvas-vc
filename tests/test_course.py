"""load_tree / dump_tree driven by the registry, with a test-only Page TypeSpec.

The spec below is also a worked example for type beads: one file per object,
frontmatter via ``model.serialize``, dates via ``dates``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from canvas_sync import model
from canvas_sync.config import CourseConfig
from canvas_sync.dates import format_local, parse_local
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey, kind_for_path
from canvas_sync.model.course import Course, dump_tree, load_tree
from canvas_sync.model.page import KEY_ORDER, Page
from canvas_sync.model.roster import Roster
from canvas_sync.model.serialize import dump_frontmatter, load_frontmatter
from tests.helpers.fixed_point import (
    assert_course_fixed_point,
    assert_spec_fixed_point,
    assert_spec_normalizes,
    write_files,
)

COURSE_YAML = """\
canvas_url: https://canvas.example.edu
course_id: 123456
timezone: America/Vancouver
"""


class _PageSpec:
    kind = Kind.PAGE
    folder = "pages"

    def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]:
        out: dict[str, CanvasObject] = {}
        for p in sorted((root / self.folder).glob("*.md")):
            meta, body = load_frontmatter(p.read_text(encoding="utf-8"), p.name)
            if meta.get("publish_at") is not None:
                meta["publish_at"] = parse_local(
                    meta["publish_at"], cfg.tz, cfg.defaults.unlock_time
                )
            out[f"{self.folder}/{p.name}"] = Page(body=body, **meta)
        return out

    def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for path, obj in objs.items():
            page = cast(Page, obj)
            meta = page.model_dump(exclude={"body"}, exclude_defaults=True)
            files[path] = dump_frontmatter(meta, page.body, KEY_ORDER).encode()
        return files


@pytest.fixture(autouse=True)
def page_spec(monkeypatch: pytest.MonkeyPatch) -> Iterator[_PageSpec]:
    spec = _PageSpec()
    monkeypatch.setitem(model.REGISTRY, Kind.PAGE, spec)
    yield spec


def tree(tmp_path: Path, files: dict[str, str]) -> Path:
    write_files(tmp_path / "t", {"course.yaml": COURSE_YAML, **files})
    return tmp_path / "t"


def test_load_dump_roundtrip(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "pages/syllabus.md": "---\ncanvas_id: syllabus\ntitle: Syllabus\n---\n\nHello\n",
            "roster.yaml": "sections: [BA1, BA2]\n",
        },
    )
    course = load_tree(root)
    assert course.roster == Roster(sections=["BA1", "BA2"])
    key = ObjectKey(Kind.PAGE, "pages/syllabus.md")
    assert set(course.objects) == {key}
    assert isinstance(course.objects[key], Page)
    files = assert_course_fixed_point(course, tmp_path)
    assert files["course.yaml"] == COURSE_YAML.encode()
    assert files["roster.yaml"] == b"sections: [BA1, BA2]\n"
    assert list(files) == sorted(files)


def test_tolerant_load_normalizes(tmp_path: Path) -> None:
    messy = (
        "﻿---\r\ntitle: Syllabus\r\npublish_at: 2026-09-08\r\ncanvas_id: syllabus\r\n"
        "published: false\r\n---\r\n\r\n\r\nHello  \r\nWorld"
    )
    root = tree(tmp_path, {"pages/syllabus.md": messy})
    out = dump_tree(load_tree(root))
    assert out["pages/syllabus.md"] == (
        b"---\ncanvas_id: syllabus\ntitle: Syllabus\npublished: false\n"
        b"publish_at: 2026-09-08 09:00\n---\n\nHello  \nWorld\n"
    )


def test_course_yaml_canonical(tmp_path: Path) -> None:
    messy = (
        "﻿defaults:\r\n  unlock_time: '09:00'\r\n  published: false\r\n  assignment:\r\n"
        "    due_time: '23:59'\r\n    submission_types: [online_upload]\r\n"
        "timezone: America/Vancouver\r\ncourse_id: 123456\r\ncanvas_url: https://canvas.example.edu/"
    )
    root = tmp_path / "t"
    write_files(root, {"course.yaml": messy})
    assert dump_tree(load_tree(root))["course.yaml"] == COURSE_YAML.encode()

    write_files(
        root,
        {
            "course.yaml": COURSE_YAML
            + "defaults:\n  published: true\n  assignment:\n    due_time: '17:00'\n"
        },
    )
    out = dump_tree(load_tree(root))["course.yaml"].decode()
    assert out.endswith('defaults:\n  published: true\n  assignment:\n    due_time: "17:00"\n')
    assert_course_fixed_point(load_tree(root), tmp_path)


def test_empty_roster_not_written(tmp_path: Path) -> None:
    files = dump_tree(load_tree(tree(tmp_path, {})))
    assert set(files) == {"course.yaml"}


def test_fixed_point_and_object_order_independent(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {f"pages/{n}.md": f"---\ntitle: {n}\n---\n\nbody {n}\n" for n in ["b", "a", "c"]},
    )
    course = load_tree(root)
    reordered = Course(
        course.config,
        dict(reversed(list(course.objects.items()))),
        course.files_meta,
        course.modules,
        course.roster,
    )
    assert dump_tree(course) == dump_tree(reordered)
    assert_course_fixed_point(course, tmp_path)


def test_spec_helpers_work_for_a_type(tmp_path: Path, page_spec: _PageSpec) -> None:
    cfg = load_tree(tree(tmp_path, {})).config
    pages: dict[str, CanvasObject] = {
        "pages/a.md": Page(title="A: the first", body="x\n\n---\n\ny"),
        "pages/b.md": Page(canvas_id="b", title="yes", published=True, body=""),
    }
    assert_spec_fixed_point(page_spec, pages, cfg, tmp_path)
    assert_spec_normalizes(
        page_spec,
        {"pages/a.md": "---\r\ntitle: Z\r\n---\r\nbody"},
        {"pages/a.md": "---\ntitle: Z\n---\n\nbody\n"},
        cfg,
        tmp_path,
    )


def test_fixed_point_helper_catches_instability(tmp_path: Path, page_spec: _PageSpec) -> None:
    class Unstable(_PageSpec):
        def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]:
            return {p: b"---\ntitle: T\n---\n\nbody\n\n\n" for p in objs}

    cfg = load_tree(tree(tmp_path, {})).config
    with pytest.raises(AssertionError):
        assert_spec_fixed_point(
            Unstable(), {"pages/a.md": Page(title="T", body="body")}, cfg, tmp_path
        )


def test_dates_survive_roundtrip(tmp_path: Path) -> None:
    root = tree(tmp_path, {"pages/p.md": "---\ntitle: P\npublish_at: 2026-09-08 23:59:59\n---\n"})
    course = load_tree(root)
    page = cast(Any, course.objects[ObjectKey(Kind.PAGE, "pages/p.md")])
    assert format_local(page.publish_at) == "2026-09-08 23:59:59"
    assert_course_fixed_point(course, tmp_path)


def test_object_key_and_kind_for_path() -> None:
    assert Page().key("pages/x.md") == ObjectKey(Kind.PAGE, "pages/x.md")
    assert kind_for_path("assignments/a.md") is Kind.ASSIGNMENT
    assert kind_for_path("announcements/a.md") is Kind.ANNOUNCEMENT
    assert kind_for_path("files/a/b.pdf") is Kind.FILE
    assert kind_for_path("modules.yaml#Week 1") is Kind.MODULE
    with pytest.raises(ValueError):
        kind_for_path("nope/x.md")


def test_modules_get_unique_keys_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from canvas_sync.model.modules import Module

    seen: dict[str, CanvasObject] = {}

    class _ModSpec:
        kind = Kind.MODULE
        folder = "modules"

        def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]:
            return {"modules.yaml#B": Module(name="B"), "modules.yaml#A": Module(name="A")}

        def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]:
            seen.update(objs)
            return {"modules.yaml": b"x\n"}

    monkeypatch.setitem(model.REGISTRY, Kind.MODULE, _ModSpec())
    course = load_tree(tree(tmp_path, {}))
    assert [m.name for m in course.modules] == ["B", "A"]  # load order kept
    dup = Course(
        course.config, {}, course.files_meta, [Module(name="A"), Module(name="A")], Roster()
    )
    dump_tree(dup)
    assert list(seen) == ["modules.yaml#A", "modules.yaml#A#2"]


def test_two_specs_writing_same_path_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Clash(_PageSpec):
        kind = Kind.ANNOUNCEMENT
        folder = "announcements"

        def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]:
            return {"course.yaml": b"x\n"}

    monkeypatch.setitem(model.REGISTRY, Kind.ANNOUNCEMENT, _Clash())
    with pytest.raises(ValueError, match="course.yaml"):
        dump_tree(load_tree(tree(tmp_path, {})))
