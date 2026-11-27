"""plan/files.py differ (bead orx.5)."""

from __future__ import annotations

import pytest
from rich.console import Console

from canvas_sync.config import CourseConfig
from canvas_sync.errors import ValidationError
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import parse_files_meta
from canvas_sync.model.roster import Roster
from canvas_sync.plan.core import Action, RiskKind, diff
from canvas_sync.plan.files import PlacedFile, diff_type

CFG = CourseConfig.model_validate(
    {"canvas_url": "https://canvas.test", "course_id": 101, "timezone": "America/Vancouver"}
)
SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64
Y_LINE = f'  y.txt: {{canvas_id: 2, sha256: "{SHA_B}"}}\n'
META = f"""\
defaults: {{published: true}}
rules:
  - {{match: "*.pdf", hidden: true}}
files:
  x.pdf: {{canvas_id: 1, sha256: "{SHA_A}"}}
{Y_LINE}"""


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


def test_content_change_is_upload_with_id_risk() -> None:
    (op,) = diff_type(course(META), course(META.replace(SHA_A, SHA_C)))
    assert op.action is Action.UPLOAD and op.key.path == "files/x.pdf"
    assert op.changes == {"content": ("aaaaaaaa", "cccccccc")}
    assert [r.kind for r in op.risks] == [RiskKind.FILE_ID_CHANGE]
    assert op.risks[0].needs_confirm is None
    assert isinstance(op.before, PlacedFile) and isinstance(op.after, PlacedFile)
    assert op.before.canvas_id == 1 and op.after.canvas_id == 1


def test_settings_only_change_is_update_without_risk() -> None:
    (op,) = diff_type(course(META), course(META.replace("hidden: true", "hidden: false")))
    assert op.action is Action.UPDATE and op.risks == ()
    assert op.changes == {"hidden": (True, False)}
    assert isinstance(op.after, PlacedFile) and op.after.effective.hidden is False


def test_per_file_entry_override_is_a_change() -> None:
    other = META.replace("canvas_id: 2,", "canvas_id: 2, published: false,")
    (op,) = diff_type(course(META), course(other))
    assert op.key.path == "files/y.txt" and op.changes == {"published": (True, False)}


def test_content_and_settings_together_is_one_upload() -> None:
    other = META.replace(SHA_A, SHA_C).replace("hidden: true", "hidden: false")
    ops = diff_type(course(META), course(other))
    assert [(o.key.path, o.action) for o in ops] == [("files/x.pdf", Action.UPLOAD)]
    assert set(ops[0].changes) == {"content", "hidden"}


def test_new_file_is_create_without_id() -> None:
    added = META + f'  z.pdf: {{canvas_id: 3, sha256: "{SHA_A}"}}\n'
    (op,) = diff_type(course(META), course(added))
    assert op.action is Action.CREATE and op.before is None
    assert op.after is not None and op.after.canvas_id is None  # never trust an id for a new path
    assert op.changes["content"] == (None, "aaaaaaaa")
    assert op.changes["hidden"] == (False, True)  # the *.pdf rule
    assert op.risks == ()


def test_new_file_without_content_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"files/z\.pdf: new file has no content"):
        diff_type(course(META), course(META + "  z.pdf: {}\n"))


def test_deleted_file_is_delete() -> None:
    (op,) = diff_type(course(META), course(META.replace(Y_LINE, "")))
    assert op.action is Action.DELETE and op.key.path == "files/y.txt" and op.after is None
    assert op.before is not None and op.before.canvas_id == 2


def test_delete_of_never_uploaded_entry_is_nothing() -> None:
    base = META + f'  z.pdf: {{sha256: "{SHA_A}"}}\n'
    assert diff_type(course(base), course(META)) == []


def test_move_is_refused() -> None:
    moved = META.replace("x.pdf: {canvas_id: 1", "sub/x.pdf: {canvas_id: 1")
    with pytest.raises(ValidationError, match=r"files/sub/x\.pdf: moving or renaming"):
        diff_type(course(META), course(moved))


def test_plan_render_shows_risk() -> None:
    plan = diff(course(META), course(META.replace(SHA_A, SHA_C)))
    con = Console(width=200, record=True, force_terminal=False)
    con.print(plan.render())
    text = con.export_text()
    assert "x.pdf" in text and "new Canvas file id" in text
