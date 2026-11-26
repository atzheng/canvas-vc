"""Assignment, Override and Roster models: validation, canonical order, fixed point."""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from canvas_sync.config import CourseConfig
from canvas_sync.dates import parse_local
from canvas_sync.errors import ValidationError
from canvas_sync.model.assignment import ASSIGNMENT_SPEC, Assignment
from canvas_sync.model.base import CanvasObject
from canvas_sync.model.overrides import Override, sort_overrides
from canvas_sync.model.roster import Roster, RosterCache, check_override_targets
from tests.helpers.fixed_point import assert_spec_fixed_point, assert_spec_normalizes

CFG = CourseConfig.model_validate(
    {
        "canvas_url": "https://canvas.example.edu",
        "course_id": 1,
        "timezone": "America/Vancouver",
    }
)


def dt(s: str) -> datetime:
    return parse_local(s, CFG.tz, CFG.defaults.assignment.due_time)


OVERRIDES = [
    Override(section="BA2", due=dt("2026-09-23 23:59")),
    Override(section="BA1", due=dt("2026-09-24 23:59:59")),
    Override(group="Team 4", due=dt("2026-09-24")),
    Override(group="Team 1", lock=dt("2026-09-30")),
    Override(students=["zed", "amy"], title="Accommodations", due=dt("2026-09-26")),
    Override(students=["bob"], unlock=dt("2026-09-01 08:00")),
]


def make(**kw: Any) -> Assignment:
    return Assignment(canvas_id=7, title="Homework 2", points=10, body="Do it.", **kw)


def test_fixed_point_with_overrides_in_shuffled_order(tmp_path: Path) -> None:
    expected = None
    for seed in range(10):
        ov = list(OVERRIDES)
        random.Random(seed).shuffle(ov)
        a = make(due=dt("2026-09-22"), unlock=dt("2026-09-15 09:00"), overrides=ov)
        objs: dict[str, CanvasObject] = {"assignments/hw2.md": a}
        out = assert_spec_fixed_point(ASSIGNMENT_SPEC, objs, CFG, tmp_path)
        if expected is None:
            expected = out
        assert out == expected


def test_canonical_output() -> None:
    a = make(due=dt("2026-09-22"), overrides=OVERRIDES)
    out = ASSIGNMENT_SPEC.dump({"assignments/hw2.md": a}, CFG)["assignments/hw2.md"].decode()
    assert out == (
        "---\n"
        "canvas_id: 7\n"
        "title: Homework 2\n"
        "points: 10\n"
        "due: 2026-09-22 23:59\n"
        "overrides:\n"
        "  - section: BA1\n"
        "    due: 2026-09-24 23:59:59\n"
        "  - section: BA2\n"
        "    due: 2026-09-23 23:59\n"
        "  - group: Team 1\n"
        "    lock: 2026-09-30 23:59\n"
        "  - group: Team 4\n"
        "    due: 2026-09-24 23:59\n"
        "  - students: [amy, zed]\n"
        "    title: Accommodations\n"
        "    due: 2026-09-26 23:59\n"
        "  - students: [bob]\n"
        "    unlock: 2026-09-01 08:00\n"
        "---\n\nDo it.\n"
    )


def test_normalizes_messy_input(tmp_path: Path) -> None:
    raw = (
        "﻿---\r\ntitle: Homework 2\r\ndue: 2026-09-22\r\nunlock: 2026-09-15\r\n"
        "canvas_id: 7\r\npublished: null\r\npoints: 10.0\r\n"
        "overrides:\r\n  - due: 2026-09-23\r\n    students: [zed, amy]\r\n"
        "  - lock: 2026-09-30\r\n    section: BA2\r\n---\r\n\r\nDo it.  "
    )
    expected = (
        "---\ncanvas_id: 7\ntitle: Homework 2\npoints: 10\nunlock: 2026-09-15 09:00\n"
        "due: 2026-09-22 23:59\noverrides:\n  - section: BA2\n    lock: 2026-09-30 23:59\n"
        "  - students: [amy, zed]\n    due: 2026-09-23 23:59\n---\n\nDo it.\n"
    )
    assert_spec_normalizes(
        ASSIGNMENT_SPEC,
        {"assignments/hw2.md": raw},
        {"assignments/hw2.md": expected},
        CFG,
        tmp_path,
    )


def test_absent_dates_stay_none(tmp_path: Path) -> None:
    root = tmp_path / "t"
    (root / "assignments").mkdir(parents=True)
    (root / "assignments" / "a.md").write_text("---\ntitle: A\noverrides:\n  - section: S\n---\n")
    a = ASSIGNMENT_SPEC.load(root, CFG)["assignments/a.md"]
    assert isinstance(a, Assignment)
    assert a.due is None and a.unlock is None and a.lock is None
    assert a.overrides[0].due is None and a.points is None


def test_overrides_only_fixed_point(tmp_path: Path) -> None:
    a = make(assign_to_overrides_only=True, overrides=OVERRIDES)
    assert_spec_fixed_point(ASSIGNMENT_SPEC, {"assignments/x.md": a}, CFG, tmp_path)


def test_sort_overrides() -> None:
    assert [o.target() for o in sort_overrides(OVERRIDES)] == [
        ("section", ("BA1",)),
        ("section", ("BA2",)),
        ("group", ("Team 1",)),
        ("group", ("Team 4",)),
        ("students", ("amy", "zed")),
        ("students", ("bob",)),
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, r"exactly one of section, group, students \(found: none\)"),
        ({"section": "A", "group": "G"}, "found: section, group"),
        ({"section": "A", "students": ["x"]}, "found: section, students"),
        ({"students": []}, "must not be empty"),
        ({"students": ["x", "x"]}, "more than once"),
    ],
)
def test_invalid_override(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Override(**kwargs)


def test_duplicate_section_rejected() -> None:
    with pytest.raises(ValidationError, match="section 'BA1' appears in more than one"):
        make(overrides=[Override(section="BA1"), Override(section="BA1", due=dt("2026-09-01"))])


def test_duplicate_group_rejected() -> None:
    with pytest.raises(ValidationError, match="group 'G' appears in more than one"):
        make(overrides=[Override(group="G"), Override(group="G")])


@pytest.mark.parametrize("field", ["due", "unlock", "lock"])
def test_overrides_only_excludes_base_dates(field: str) -> None:
    with pytest.raises(ValidationError, match=f"assign_to_overrides_only.*{field}"):
        make(assign_to_overrides_only=True, **{field: dt("2026-09-01")})


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "---\ntitle: A\noverrides:\n  - due: 2026-09-01\n---\n",
            "assignments/a.md: .*exactly one",
        ),
        ("---\ntitle: A\noverrides: [x]\n---\n", "override 1 must be a mapping"),
        ("---\ntitle: A\noverrides: x\n---\n", "overrides must be a list"),
        ("---\ntitle: A\nbogus: 1\n---\n", "assignments/a.md: bogus"),
        ("---\ntitle: A\ndue: nonsense\n---\n", "invalid date"),
        (
            "---\ntitle: A\nassign_to_overrides_only: true\ndue: 2026-09-01\n---\n",
            "assignments/a.md: .*must be absent",
        ),
    ],
)
def test_load_errors(tmp_path: Path, text: str, message: str) -> None:
    root = tmp_path / "t"
    (root / "assignments").mkdir(parents=True)
    (root / "assignments" / "a.md").write_text(text)
    with pytest.raises(ValidationError, match=message):
        ASSIGNMENT_SPEC.load(root, CFG)


def test_students_sorted_on_construction() -> None:
    assert Override(students=["c", "a", "b"]).students == ["a", "b", "c"]


def test_roster() -> None:
    assert Roster(sections=["BA1", "BA2"]).groups_category is None
    with pytest.raises(ValidationError, match="'BA1' is listed more than once"):
        Roster(sections=["BA1", "BA1"])


def test_roster_cache_json_roundtrip() -> None:
    c = RosterCache(students={"zed": 2, "amy": 1}, sections={"BA1": 10}, groups={})
    text = c.to_json()
    assert text.index('"groups"') < text.index('"sections"') < text.index('"students"')
    assert text.index('"amy"') < text.index('"zed"')
    assert RosterCache.from_json(text) == c
    with pytest.raises(ValidationError, match="roster-cache.json"):
        RosterCache.from_json("{not json")


def test_check_override_targets() -> None:
    roster = Roster(sections=["BA1"])
    cache = RosterCache(students={"amy": 1}, groups={"Team 1": 3}, sections={"BA2": 5})
    ok = [Override(section="BA1"), Override(section="BA2"), Override(group="Team 1")]
    assert check_override_targets([*ok, Override(students=["amy"])], roster, cache, "a.md") == []
    bad = [Override(section="X"), Override(group="Y"), Override(students=["amy", "who"])]
    assert check_override_targets(bad, roster, cache, "assignments/a.md") == [
        "assignments/a.md: unknown section 'X'",
        "assignments/a.md: unknown group 'Y'",
        "assignments/a.md: unknown student 'who'",
    ]


def test_student_in_two_sets_rejected() -> None:
    sets = [Override(students=["amy", "bob"]), Override(students=["amy", "zed"])]
    with pytest.raises(ValidationError, match="student 'amy' appears in more than one"):
        make(overrides=sets)


def test_identical_student_sets_rejected() -> None:
    with pytest.raises(ValidationError, match="student 'amy' appears in more than one"):
        make(overrides=[Override(students=["amy"]), Override(students=["amy"], title="x")])


def test_target_distinguishes_comma_names() -> None:
    assert Override(students=["a,b"]).target() != Override(students=["a", "b"]).target()


def test_sort_is_input_order_independent() -> None:
    same = [
        Override(section="S", due=dt("2026-09-02")),
        Override(section="S", due=dt("2026-09-01")),
        Override(section="S"),
        Override(section="S", lock=dt("2026-09-01")),
    ]
    first = sort_overrides(same)
    assert first[0].due is None and first[0].lock is None
    for seed in range(10):
        shuffled = list(same)
        random.Random(seed).shuffle(shuffled)
        assert sort_overrides(shuffled) == first


def test_bad_date_error_names_file_key_and_index(tmp_path: Path) -> None:
    root = tmp_path / "t"
    (root / "assignments").mkdir(parents=True)
    f = root / "assignments" / "a.md"
    f.write_text("---\ntitle: A\ndue: notadate\n---\n")
    with pytest.raises(ValidationError, match=r"^assignments/a.md: due: invalid date 'notadate'"):
        ASSIGNMENT_SPEC.load(root, CFG)
    f.write_text("---\ntitle: A\noverrides:\n  - section: S\n  - section: T\n    lock: zz\n---\n")
    with pytest.raises(ValidationError, match=r"^assignments/a.md: override 2: lock: invalid date"):
        ASSIGNMENT_SPEC.load(root, CFG)
    f.write_text("---\ntitle: A\noverrides:\n  - section: S\n  - bogus: 1\n---\n")
    with pytest.raises(ValidationError, match=r"assignments/a.md: overrides.2"):
        ASSIGNMENT_SPEC.load(root, CFG)
