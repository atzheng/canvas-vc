"""modules.yaml model: exclusivity, order, tolerance, fixed point."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from canvas_sync.config import CourseConfig, load_config
from canvas_sync.errors import ValidationError
from canvas_sync.model.base import CanvasObject
from canvas_sync.model.course import Course, dump_tree, load_tree
from canvas_sync.model.modules import (
    MODULE_SPEC,
    Module,
    ModuleItem,
    ModuleOverride,
    UnmanagedRef,
)
from tests.helpers.fixed_point import (
    assert_course_fixed_point,
    assert_spec_fixed_point,
    assert_spec_normalizes,
    write_files,
)

FIXTURE = Path(__file__).parent / "fixtures" / "trees" / "modules"


@pytest.fixture
def cfg() -> CourseConfig:
    return load_config(FIXTURE)


def _load(tmp_path: Path, text: str) -> dict[str, CanvasObject]:
    root = tmp_path / "t"
    write_files(
        root,
        {"course.yaml": (FIXTURE / "course.yaml").read_text(), "modules.yaml": text},
    )
    return MODULE_SPEC.load(root, load_config(root))


def test_fixture_is_canonical_and_fixed_point(tmp_path: Path, cfg: CourseConfig) -> None:
    objs = MODULE_SPEC.load(FIXTURE, cfg)
    assert list(objs) == [
        "modules.yaml#Week 1 – Linear models",
        "modules.yaml#Week 2",
        "modules.yaml#Week 2#2",
    ]
    out = assert_spec_fixed_point(MODULE_SPEC, objs, cfg, tmp_path)
    assert out["modules.yaml"] == (FIXTURE / "modules.yaml").read_bytes()


def test_course_fixed_point_and_keys(tmp_path: Path) -> None:
    course = load_tree(FIXTURE)
    assert [m.name for m in course.modules] == ["Week 1 – Linear models", "Week 2", "Week 2"]
    assert dump_tree(course)["modules.yaml"] == (FIXTURE / "modules.yaml").read_bytes()
    assert_course_fixed_point(course, tmp_path)


def test_item_with_two_types_or_none_raises() -> None:
    with pytest.raises(PydanticValidationError):
        ModuleItem(page="a", assignment="b")
    with pytest.raises(PydanticValidationError):
        ModuleItem(header="x", quiz=UnmanagedRef(canvas_id=1))
    with pytest.raises(PydanticValidationError):
        ModuleItem(title="only a title")
    with pytest.raises(PydanticValidationError):
        ModuleItem()


def test_load_two_types_or_none_is_validation_error(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=r"\[0\]\.items\[0\].*exactly one"):
        _load(tmp_path, "- name: A\n  items:\n    - page: a\n      file: b\n")
    with pytest.raises(ValidationError, match="exactly one"):
        _load(tmp_path, "- name: A\n  items:\n    - title: nothing\n")


@pytest.mark.parametrize(
    "item",
    [
        "page: /abs",
        "page: ../x",
        "page: a//b",
        "page: ''",
        "header: ''",
        "url: ftp://x",
        "url: not a url",
        "page: a\n      indent: -1",
        "quiz: 5",
        "quiz: {canvas_id: x}",
        "page: 12\n      canvas_id: abc",
        "bogus: 1",
    ],
)
def test_bad_item_shapes(tmp_path: Path, item: str) -> None:
    with pytest.raises(ValidationError, match="modules.yaml"):
        _load(tmp_path, f"- name: A\n  items:\n    - {item}\n")


def test_bad_module_shapes(tmp_path: Path) -> None:
    for text in ["name: A\n", "- just a string\n", "- {}\n", "- name: A\n  unlock: tomorrow\n"]:
        with pytest.raises(ValidationError, match="modules.yaml"):
            _load(tmp_path, text)
    with pytest.raises(ValidationError, match="duplicate canvas_id 7"):
        _load(tmp_path, "- name: A\n  canvas_id: 7\n- name: B\n  canvas_id: 7\n")


def test_override_needs_exactly_one_target() -> None:
    with pytest.raises(PydanticValidationError):
        ModuleOverride()
    with pytest.raises(PydanticValidationError):
        ModuleOverride(section="A", group="B")
    with pytest.raises(PydanticValidationError):
        ModuleOverride(students=[])
    assert ModuleOverride(students=["b", "a"]).students == ["a", "b"]


def test_overrides_canonical_order() -> None:
    m = Module(
        name="M",
        overrides=[
            ModuleOverride(students=["z"]),
            ModuleOverride(group="G"),
            ModuleOverride(section="B"),
            ModuleOverride(section="A"),
        ],
    )
    assert [o.section or o.group or o.students for o in m.overrides] == ["A", "B", "G", ["z"]]


def test_order_preserved_exactly(tmp_path: Path, cfg: CourseConfig) -> None:
    names = ["Zed", "Alpha", "Mid", "Alpha"]
    mods: dict[str, CanvasObject] = {}
    for i, n in enumerate(names):
        key = f"modules.yaml#{n}" + ("#2" if i == 3 else "")
        mods[key] = Module(name=n, items=[ModuleItem(header=h) for h in ["z", "a", "m", "a"]])
    out = assert_spec_fixed_point(MODULE_SPEC, mods, cfg, tmp_path)
    loaded = _load(tmp_path, out["modules.yaml"].decode())
    loaded_mods = [m for m in loaded.values() if isinstance(m, Module)]
    assert [m.name for m in loaded_mods] == names
    for m in loaded_mods:
        assert [i.header for i in m.items] == ["z", "a", "m", "a"]


def test_dump_follows_dict_order(cfg: CourseConfig) -> None:
    a, b = Module(name="A"), Module(name="B")
    ab = MODULE_SPEC.dump({"modules.yaml#A": a, "modules.yaml#B": b}, cfg)
    ba = MODULE_SPEC.dump({"modules.yaml#B": b, "modules.yaml#A": a}, cfg)
    assert ab["modules.yaml"] == b"- name: A\n- name: B\n"
    assert ba["modules.yaml"] == b"- name: B\n- name: A\n"


def test_normalizes_messy_input(tmp_path: Path, cfg: CourseConfig) -> None:
    raw = (
        "﻿- items:\r\n"
        "    - assignment: hw1\r\n"
        "      canvas_id: 9\r\n"
        "  unlock: 2026-09-08\r\n"
        "  name: Week 1\r\n"
        "  published: null\r\n"
        "  overrides:\r\n"
        "    - unlock: 2026-09-10\r\n"
        "      students: [b, a]\r\n"
        "    - unlock: 2026-09-09 10:30\r\n"
        "      section: BA2"
    )
    expected = (
        "- name: Week 1\n"
        "  unlock: 2026-09-08 09:00\n"
        "  overrides:\n"
        "    - section: BA2\n"
        "      unlock: 2026-09-09 10:30\n"
        "    - students: [a, b]\n"
        "      unlock: 2026-09-10 09:00\n"
        "  items:\n"
        "    - canvas_id: 9\n"
        "      assignment: hw1\n"
    )
    assert_spec_normalizes(
        MODULE_SPEC, {"modules.yaml": raw}, {"modules.yaml": expected}, cfg, tmp_path
    )


def test_absent_and_null_are_the_same(tmp_path: Path) -> None:
    a = _load(tmp_path, "- name: A\n")
    b = _load(tmp_path, "- name: A\n  unlock: null\n  published: ~\n  items:\n  overrides:\n")
    assert a == b


def test_no_modules_no_file(tmp_path: Path, cfg: CourseConfig) -> None:
    assert MODULE_SPEC.dump({}, cfg) == {}
    root = tmp_path / "empty"
    write_files(root, {"course.yaml": (FIXTURE / "course.yaml").read_text()})
    assert MODULE_SPEC.load(root, cfg) == {}
    assert _load(tmp_path, "") == {}
    assert _load(tmp_path, "[]\n") == {}
    base = load_tree(root)
    course = Course(base.config, {}, base.files_meta, [], base.roster)
    assert "modules.yaml" not in dump_tree(course)


def test_awkward_strings_round_trip(tmp_path: Path, cfg: CourseConfig) -> None:
    objs: dict[str, CanvasObject] = {
        "modules.yaml#yes: 1 # x": Module(
            name="yes: 1 # x",
            items=[
                ModuleItem(header="true"),
                ModuleItem(header="12"),
                ModuleItem(url="https://a.b/c?d=e#f", title="Title: with colon"),
            ],
        )
    }
    assert_spec_fixed_point(MODULE_SPEC, objs, cfg, tmp_path)
