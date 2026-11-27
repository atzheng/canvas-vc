"""modules.yaml model: exclusivity, order, tolerance, fixed point."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError as PydanticValidationError

from canvas_sync.config import CourseConfig, load_config
from canvas_sync.errors import ValidationError
from canvas_sync.model.base import CanvasObject
from canvas_sync.model.course import Course, dump_tree, load_tree
from canvas_sync.model.modules import (
    MODULE_SPEC,
    CompletionRequirement,
    Module,
    ModuleItem,
    ModuleOverride,
    UnmanagedItem,
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


def test_override_rules() -> None:
    d = datetime(2026, 9, 9, 9, 0, tzinfo=ZoneInfo("America/Vancouver"))
    with pytest.raises(PydanticValidationError, match="unlock date"):
        ModuleOverride(section="A")
    with pytest.raises(PydanticValidationError, match="only support unlock"):
        ModuleOverride(section="A", unlock=d, title="t")
    with pytest.raises(PydanticValidationError, match="only support unlock"):
        ModuleOverride(section="A", unlock=d, due=d)
    with pytest.raises(ValidationError):
        ModuleOverride(unlock=d)
    with pytest.raises(ValidationError):
        ModuleOverride(section="A", group="B", unlock=d)
    assert ModuleOverride(students=["b", "a"], unlock=d).students == ["a", "b"]


def test_load_override_without_unlock(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=r"\[0\]\.overrides\[0\].*unlock"):
        _load(tmp_path, "- name: A\n  overrides:\n    - section: S\n")


def test_overrides_canonical_order() -> None:
    d = datetime(2026, 9, 9, 9, 0, tzinfo=ZoneInfo("America/Vancouver"))
    m = Module(
        name="M",
        overrides=[
            ModuleOverride(students=["z"], unlock=d),
            ModuleOverride(group="G", unlock=d),
            ModuleOverride(section="B", unlock=d),
            ModuleOverride(section="A", unlock=d),
        ],
    )
    assert [o.section or o.group or o.students for o in m.overrides] == ["A", "B", "G", ["z"]]


def test_null_indent_and_new_tab_mean_default(tmp_path: Path, cfg: CourseConfig) -> None:
    objs = _load(
        tmp_path, "- name: A\n  items:\n    - page: p\n      indent: null\n      new_tab: ~\n"
    )
    (m,) = objs.values()
    assert isinstance(m, Module)
    assert m.items[0].indent == 0
    assert m.items[0].new_tab is False
    assert MODULE_SPEC.dump(objs, cfg)["modules.yaml"] == b"- name: A\n  items:\n    - page: p\n"
    assert_spec_fixed_point(MODULE_SPEC, objs, cfg, tmp_path)


def test_bool_canvas_id_and_indent_rejected(tmp_path: Path) -> None:
    with pytest.raises(PydanticValidationError):
        Module(name="A", canvas_id=True)  # pyright: ignore[reportArgumentType]
    with pytest.raises(PydanticValidationError):
        ModuleItem(page="p", canvas_id=True)  # pyright: ignore[reportArgumentType]
    with pytest.raises(PydanticValidationError):
        UnmanagedRef(canvas_id=True)  # pyright: ignore[reportArgumentType]
    with pytest.raises(PydanticValidationError):
        ModuleItem(page="p", indent=True)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationError, match="modules.yaml"):
        _load(tmp_path, "- name: A\n  canvas_id: true\n")
    with pytest.raises(ValidationError, match="modules.yaml"):
        _load(tmp_path, "- name: A\n  items:\n    - page: p\n      canvas_id: false\n")


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


UNMANAGED_TEXT = """\
- canvas_id: 10
  name: Intro
- canvas_id: 11
  name: Week 1
  prerequisites: [10]
  require_sequential_progress: true
  completion_requirements:
    - item_id: 201
      type: must_view
    - item_id: 203
      type: min_score
      min_score: 80
  items:
    - canvas_id: 201
      page: syllabus
    - canvas_id: 202
      unmanaged:
        type: ExternalTool
        content_id: 777
        external_url: https://tool.example.org/launch
      title: Lab tool
      indent: 1
    - canvas_id: 203
      assignment: hw1
"""


def test_unmanaged_items_and_settings_round_trip(tmp_path: Path, cfg: CourseConfig) -> None:
    objs = _load(tmp_path, UNMANAGED_TEXT)
    m = list(objs.values())[1]
    assert isinstance(m, Module)
    assert m.prerequisites == [10]
    assert m.require_sequential_progress is True
    assert [r.item_id for r in m.completion_requirements] == [201, 203]
    assert m.items[1].item_type == "unmanaged"
    out = assert_spec_fixed_point(MODULE_SPEC, objs, cfg, tmp_path)
    assert out["modules.yaml"].decode() == UNMANAGED_TEXT


def test_unmanaged_item_validation() -> None:
    with pytest.raises(PydanticValidationError, match="canvas_id"):
        ModuleItem(unmanaged=UnmanagedItem(type="ExternalTool"))
    with pytest.raises(PydanticValidationError):
        ModuleItem(canvas_id=1, unmanaged=UnmanagedItem(type=" "))
    with pytest.raises(PydanticValidationError):
        ModuleItem(canvas_id=1, page="a", unmanaged=UnmanagedItem(type="X"))


def test_module_settings_validation(tmp_path: Path) -> None:
    with pytest.raises(PydanticValidationError, match="own prerequisite"):
        Module(name="A", canvas_id=5, prerequisites=[5])
    with pytest.raises(PydanticValidationError, match="duplicate prerequisite"):
        Module(name="A", prerequisites=[1, 1])
    with pytest.raises(PydanticValidationError, match="min_score"):
        CompletionRequirement(item_id=1, type="must_view", min_score=3)
    with pytest.raises(PydanticValidationError, match="duplicate completion"):
        Module(
            name="A",
            completion_requirements=[
                CompletionRequirement(item_id=1, type="must_view"),
                CompletionRequirement(item_id=1, type="must_submit"),
            ],
        )
    with pytest.raises(ValidationError, match=r"\[0\]\.prerequisites\[0\]"):
        _load(tmp_path, "- name: A\n  prerequisites:\n    - not-an-id\n")
    with pytest.raises(ValidationError, match="duplicate canvas_id 7"):
        _load(
            tmp_path,
            "- name: A\n  items:\n    - canvas_id: 7\n      unmanaged:\n"
            "        type: ExternalTool\n"
            "    - canvas_id: 7\n      header: h\n",
        )


def test_null_settings_mean_default(tmp_path: Path, cfg: CourseConfig) -> None:
    objs = _load(tmp_path, "- name: A\n  prerequisites:\n  completion_requirements:\n")
    assert MODULE_SPEC.dump(objs, cfg)["modules.yaml"] == b"- name: A\n"
