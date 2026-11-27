"""Stop-gap files differ (bead rvk; replaced by orx.5)."""

from __future__ import annotations

import pytest

from canvas_sync.config import CourseConfig
from canvas_sync.errors import ValidationError
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import parse_files_meta
from canvas_sync.model.roster import Roster
from canvas_sync.plan.core import diff
from canvas_sync.plan.files import diff_type

CFG = CourseConfig.model_validate(
    {"canvas_url": "https://canvas.test", "course_id": 101, "timezone": "America/Vancouver"}
)
SHA_A, SHA_B = "a" * 64, "b" * 64
META = f"""\
defaults: {{published: true}}
rules:
  - {{match: "*.pdf", hidden: true}}
files:
  x.pdf: {{canvas_id: 1, sha256: "{SHA_A}"}}
  y.txt: {{canvas_id: 2, sha256: "{SHA_B}"}}
"""
MSG = "pushing file changes is not implemented yet"


def course(meta: str) -> Course:
    fm = parse_files_meta(meta, CFG)
    objs: dict[ObjectKey, CanvasObject] = {ObjectKey(Kind.FILE, p): e for p, e in fm.files.items()}
    return Course(CFG, objs, fm, [], Roster())


def test_identical_no_ops() -> None:
    assert diff_type(course(META), course(META)) == []
    assert diff(course(META), course(META)).ops == []


def test_canvas_id_only_change_ignored() -> None:
    assert diff_type(course(META), course(META.replace("canvas_id: 1", "canvas_id: 9"))) == []


def test_rule_edit_without_effective_change_ignored() -> None:
    other = META.replace('"*.pdf", hidden: true', '"x.pdf", hidden: true')
    assert diff_type(course(META), course(other)) == []


def test_add_and_delete() -> None:
    added = META + f'  z.pdf: {{canvas_id: 3, sha256: "{SHA_A}"}}\n'
    with pytest.raises(ValidationError) as e:
        diff_type(course(META), course(added))
    assert str(e.value) == f"files/z.pdf: {MSG}"
    with pytest.raises(ValidationError) as e:
        diff_type(course(added), course(META))
    assert str(e.value) == f"files/z.pdf: {MSG}"


def test_edit_hash() -> None:
    edited = META.replace(f'sha256: "{SHA_B}"', f'sha256: "{SHA_A}"')
    with pytest.raises(ValidationError) as e:
        diff_type(course(META), course(edited))
    assert str(e.value) == f"files/y.txt: {MSG}"


def test_settings_change_via_defaults_and_rules() -> None:
    with pytest.raises(ValidationError) as e:
        diff_type(course(META), course(META.replace("published: true", "published: false")))
    assert str(e.value).splitlines() == [f"files/x.pdf: {MSG}", f"files/y.txt: {MSG}"]
    with pytest.raises(ValidationError) as e:
        diff_type(course(META), course(META.replace("hidden: true", "hidden: false")))
    assert str(e.value).splitlines() == [f"files/x.pdf: {MSG}"]


def test_core_diff_raises_on_change() -> None:
    with pytest.raises(ValidationError):
        diff(course(META), course(META.replace("published: true", "published: false")))
