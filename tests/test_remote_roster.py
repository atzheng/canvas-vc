"""Roster reader against FakeCanvas (invented people only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from canvas_sync.config import CourseConfig
from canvas_sync.errors import ValidationError
from canvas_sync.model.course import Course
from canvas_sync.model.roster import Roster, RosterCache
from canvas_sync.remote import roster as reader
from canvas_sync.remote.context import FetchContext
from tests.fake_canvas import FakeCanvas

C = 101


def make_ctx(student_ref: str = "login_id", category: str | None = None) -> FetchContext:
    cfg = CourseConfig.model_validate(
        {
            "canvas_url": "https://canvas.test",
            "course_id": C,
            "timezone": "America/Vancouver",
            "student_ref": student_ref,
        }
    )
    previous = None
    if category is not None:
        previous = cast(Course, type("P", (), {"roster": Roster(groups_category=category)})())
    return FetchContext(
        config=cfg,
        tz=ZoneInfo("America/Vancouver"),
        bodies=cast(Any, None),
        previous=previous,
        roster=RosterCache(),
        ignore=cast(Any, None),
        blob_store=cast(Any, None),
    )


@pytest.fixture
def fake() -> FakeCanvas:
    f = FakeCanvas(course_id=C, max_per_page=2)
    sec2 = f.add_section("BA2")
    f.add_user("Ada Example", login_id="aexample", sis_user_id="10000001")
    f.add_user("Bo Sample", login_id="bsample", sis_user_id="10000002", section_id=sec2["id"])
    f.add_user("Cy Invented", login_id="cinvented", sis_user_id="10000003")
    f.add_user("Tess Teacher", role="TeacherEnrollment", login_id="tteacher")
    return f


def test_login_id_mode(fake: FakeCanvas) -> None:
    cache = reader.read(fake.client(), make_ctx())
    assert set(cache.students) == {"aexample", "bsample", "cinvented"}  # paginated, no teacher
    assert cache.students["bsample"] == next(
        u["id"] for u in fake.users.values() if u["login_id"] == "bsample"
    )
    assert set(cache.sections) == {"Section 001", "BA2"}
    assert fake.writes() == []


def test_sis_mode(fake: FakeCanvas) -> None:
    cache = reader.read(fake.client(), make_ctx("sis"))
    assert set(cache.students) == {"sis:10000001", "sis:10000002", "sis:10000003"}


def test_alias_mode(fake: FakeCanvas) -> None:
    aliases = {"student-01": "aexample", "student-02": "bsample", "student-09": "nobody"}
    cache = reader.read(fake.client(), make_ctx("alias"), aliases=aliases)
    assert set(cache.students) == {"student-01", "student-02"}  # cinvented has no alias
    blob = cache.to_json()
    assert "aexample" not in blob and "Ada" not in blob


def test_alias_mode_needs_aliases(fake: FakeCanvas) -> None:
    with pytest.raises(ValidationError, match="roster-aliases.yaml"):
        reader.read(fake.client(), make_ctx("alias"))


def test_alias_duplicate_login(fake: FakeCanvas) -> None:
    with pytest.raises(ValidationError, match="two aliases"):
        reader.read(fake.client(), make_ctx("alias"), aliases={"a": "x", "b": "x"})


def test_users_without_ref_are_skipped(fake: FakeCanvas) -> None:
    fake.add_user("Dee Nologin")  # login_id and sis id null
    cache = reader.read(fake.client(), make_ctx())
    assert len(cache.students) == 3
    assert len(reader.read(fake.client(), make_ctx("sis")).students) == 3


def test_inactive_students_excluded(fake: FakeCanvas) -> None:
    u = fake.add_user("Eve Dropped", login_id="edropped")
    for e in fake.enrollments.values():
        if e["user_id"] == u["id"]:
            e["enrollment_state"] = "inactive"
    assert "edropped" not in reader.read(fake.client(), make_ctx()).students


def test_duplicate_refs_rejected(fake: FakeCanvas) -> None:
    fake.add_user("Twin Example", login_id="aexample")
    with pytest.raises(ValidationError, match="share the same student ref"):
        reader.read(fake.client(), make_ctx())


def test_duplicate_section_names_rejected(fake: FakeCanvas) -> None:
    fake.add_section("BA2")
    with pytest.raises(ValidationError, match="two sections"):
        reader.read(fake.client(), make_ctx())


def test_groups_all_and_by_category(fake: FakeCanvas) -> None:
    teams = fake.add_group_category("Project Teams")
    labs = fake.add_group_category("Labs")
    t1 = fake.add_group(teams["id"], "Team 1")
    fake.add_group(teams["id"], "Team 2")
    fake.add_group(labs["id"], "Lab 1")
    fake.add_group(labs["id"], "Lab 2")
    everything = reader.read(fake.client(), make_ctx())
    assert set(everything.groups) == {"Team 1", "Team 2", "Lab 1", "Lab 2"}
    only = reader.read(fake.client(), make_ctx(category="Project Teams"))
    assert set(only.groups) == {"Team 1", "Team 2"}
    assert only.groups["Team 1"] == t1["id"]
    assert reader.read(fake.client(), make_ctx(category="Missing")).groups == {}


def test_cache_roundtrip_and_aliases_file(fake: FakeCanvas, tmp_path: Path) -> None:
    assert reader.load_cache(tmp_path) == RosterCache()
    cache = reader.read(fake.client(), make_ctx())
    path = reader.write_cache(tmp_path, cache)
    assert path == tmp_path / ".canvas" / "roster-cache.json"
    assert reader.load_cache(tmp_path) == cache

    assert reader.load_aliases(tmp_path) is None
    (tmp_path / "roster-aliases.yaml").write_text("student-01: aexample\n")
    assert reader.load_aliases(tmp_path) == {"student-01": "aexample"}
    (tmp_path / "roster-aliases.yaml").write_text("- a\n")
    with pytest.raises(ValidationError):
        reader.load_aliases(tmp_path)


def test_only_get_requests(fake: FakeCanvas) -> None:
    reader.read(fake.client("read"), make_ctx())
    assert {e.method for e in fake.log} == {"GET"}
