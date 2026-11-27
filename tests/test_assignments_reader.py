"""remote/assignments.py reader against FakeCanvas."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from canvas_sync.config import CourseConfig
from canvas_sync.ignore import IgnoreRules
from canvas_sync.model.assignment import ASSIGNMENT_SPEC, Assignment
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import FilesMeta
from canvas_sync.model.roster import Roster, RosterCache
from canvas_sync.remote import assignments as remote
from canvas_sync.remote.context import FetchContext
from canvas_sync.render.bodies import Bodies
from canvas_sync.snapshot import BlobStore
from tests.fake_canvas.assignments import add_assignment, add_assignment_group, add_override
from tests.fake_canvas.core import FakeCanvas
from tests.helpers import guard_fake


def make_cfg() -> CourseConfig:
    return CourseConfig(
        canvas_url="https://canvas.test",  # pyright: ignore[reportArgumentType]
        course_id=101,
        timezone="America/Vancouver",
    )


def ctx_for(
    tmp_path: Path,
    *,
    previous: Course | None = None,
    roster: RosterCache | None = None,
    bodies: Bodies | None = None,
    ignore: list[str] | None = None,
) -> FetchContext:
    cfg = make_cfg()
    return FetchContext(
        config=cfg,
        tz=cfg.tz,
        bodies=bodies or Bodies(),
        previous=previous,
        roster=roster or RosterCache(),
        ignore=IgnoreRules(lines=ignore or []),
        blob_store=BlobStore(tmp_path),
    )


def previous_with(objs: dict[str, Assignment]) -> Course:
    return Course(
        config=make_cfg(),
        objects={ObjectKey(Kind.ASSIGNMENT, p): o for p, o in objs.items()},
        files_meta=FilesMeta(),
        modules=[],
        roster=Roster(),
    )


def dump(objs: dict[str, CanvasObject]) -> dict[str, str]:
    return {p: b.decode() for p, b in ASSIGNMENT_SPEC.dump(objs, make_cfg()).items()}


def test_overrides_of_all_kinds_round_trip_to_canonical_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCanvas()
    sec = fake.add_section("BA2")
    cat = fake.add_group_category("Teams")
    team = fake.add_group(cat["id"], "Team 4")
    jd = fake.add_user("Jane Doe", login_id="jdoe")
    asm = fake.add_user("Al Smith", login_id="asmith")
    ghost = fake.add_user("Gone Person", login_id="gone")
    grp = add_assignment_group(fake, "Homework")
    a = add_assignment(
        fake,
        "Homework 2",
        description="<p>Do it.</p>",
        points_possible=10.0,
        assignment_group_id=grp["id"],
        submission_types=["online_upload"],
        allowed_extensions=["pdf"],
        due_at="2026-09-23T06:59:00Z",
        unlock_at="2026-09-15T16:00:00Z",
    )
    add_override(fake, a["id"], course_section_id=sec["id"], due_at="2026-09-24T06:59:00Z")
    add_override(fake, a["id"], group_id=team["id"], due_at="2026-09-25T06:59:00Z")
    add_override(
        fake,
        a["id"],
        student_ids=[jd["id"], asm["id"], ghost["id"]],
        title="Accommodations",
        due_at="2026-09-27T06:59:00Z",
        lock_at="2026-09-29T06:59:00Z",
    )
    roster = RosterCache(
        students={"jdoe": jd["id"], "asmith": asm["id"]},
        sections={"BA2": sec["id"]},
        groups={"Team 4": team["id"]},
    )
    got = remote.read(fake.client(), ctx_for(tmp_path, roster=roster))

    assert list(got) == ["assignments/homework-2.md"]
    text = dump(got)["assignments/homework-2.md"]
    assert text == (
        "---\n"
        f"canvas_id: {a['id']}\n"
        "title: Homework 2\n"
        "points: 10\n"
        "group: Homework\n"
        "submission_types: [online_upload]\n"
        "allowed_extensions: [pdf]\n"
        "published: true\n"
        "unlock: 2026-09-15 09:00\n"
        "due: 2026-09-22 23:59\n"
        "overrides:\n"
        "  - section: BA2\n    due: 2026-09-23 23:59\n"
        "  - group: Team 4\n    due: 2026-09-24 23:59\n"
        f'  - students: [asmith, jdoe, "unknown:{ghost["id"]}"]\n'
        "    title: Accommodations\n"
        "    due: 2026-09-26 23:59\n"
        "    lock: 2026-09-28 23:59\n"
        "---\n\n"
        "Do it.\n"
    )
    err = capsys.readouterr().err
    assert "assignments/homework-2.md: override names 1 student(s) not in the roster cache" in err
    assert "Gone" not in err
    # canonical form is a fixed point of load
    for p, t in dump(got).items():
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text(t)
    reloaded = ASSIGNMENT_SPEC.load(tmp_path, make_cfg())
    assert dump(reloaded) == dump(got)


def test_only_visible_to_overrides_drops_base_dates(tmp_path: Path) -> None:
    fake = FakeCanvas()
    sec = fake.add_section("BA1")
    a = add_assignment(
        fake,
        "Lab",
        only_visible_to_overrides=True,
        due_at="2026-09-23T06:59:00Z",
    )
    add_override(fake, a["id"], course_section_id=sec["id"], due_at="2026-09-24T06:59:00Z")
    got = remote.read(
        fake.client(), ctx_for(tmp_path, roster=RosterCache(sections={"BA1": sec["id"]}))
    )
    obj = cast(Assignment, got["assignments/lab.md"])
    assert obj.assign_to_overrides_only and obj.due is None
    assert [o.section for o in obj.overrides] == ["BA1"]


def test_unknown_section_and_group_are_kept_not_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCanvas()
    a = add_assignment(fake, "Lab")
    add_override(fake, a["id"], course_section_id=555)
    add_override(fake, a["id"], group_id=666)
    got = remote.read(fake.client(), ctx_for(tmp_path))
    obj = cast(Assignment, got["assignments/lab.md"])
    assert [(o.section, o.group) for o in obj.overrides] == [
        ("unknown:555", None),
        (None, "unknown:666"),
    ]
    assert capsys.readouterr().err.count("warning:") == 2


def test_auto_student_title_is_dropped(tmp_path: Path) -> None:
    fake = FakeCanvas()
    a = add_assignment(fake, "Lab")
    add_override(fake, a["id"], student_ids=[1, 2], title="2 students")
    obj = cast(Assignment, remote.read(fake.client(), ctx_for(tmp_path))["assignments/lab.md"])
    assert obj.overrides[0].title is None
    assert obj.overrides[0].students == ["unknown:1", "unknown:2"]


def test_same_student_in_two_overrides_raises_without_naming_them(tmp_path: Path) -> None:
    fake = FakeCanvas()
    a = add_assignment(fake, "Lab")
    add_override(fake, a["id"], student_ids=[1, 2])
    add_override(fake, a["id"], student_ids=[2, 3])
    from canvas_sync.errors import ValidationError

    with pytest.raises(ValidationError, match=r"assignments/lab\.md: .*more than one") as e:
        remote.read(fake.client(), ctx_for(tmp_path))
    assert "unknown" not in str(e.value)


def test_paths_previous_wins_new_get_slug_with_suffix(tmp_path: Path) -> None:
    fake = FakeCanvas()
    old = add_assignment(fake, "Renamed on Canvas")
    add_assignment(fake, "Homework 1")
    add_assignment(fake, "Homework 1")
    add_assignment(fake, "Ünïcode & Stuff!")
    prev = previous_with({"assignments/hw1.md": Assignment(canvas_id=old["id"], title="Old")})
    got = remote.read(fake.client(), ctx_for(tmp_path, previous=prev))
    assert sorted(got) == [
        "assignments/homework-1-2.md",
        "assignments/homework-1.md",
        "assignments/hw1.md",
        "assignments/unicode-stuff.md",
    ]
    assert cast(Assignment, got["assignments/hw1.md"]).title == "Renamed on Canvas"


def test_ignored_and_unmanaged_kinds_are_skipped(tmp_path: Path) -> None:
    fake = FakeCanvas()
    add_assignment(fake, "Keep")
    add_assignment(fake, "Scratch")
    add_assignment(fake, "Quiz 1", submission_types=["online_quiz"], quiz_id=9)
    add_assignment(fake, "Topic", submission_types=["discussion_topic"])
    got = remote.read(fake.client(), ctx_for(tmp_path, ignore=["assignments/scratch"]))
    assert list(got) == ["assignments/keep.md"]


def test_body_reuses_previous_markdown_when_html_unchanged(tmp_path: Path) -> None:
    fake = FakeCanvas()
    html = "<p>Remote <b>html</b></p>"
    a = add_assignment(fake, "Hw", description=html)
    prev = previous_with(
        {"assignments/hw.md": Assignment(canvas_id=a["id"], title="Hw", body="My *own* markdown\n")}
    )
    bodies = Bodies()
    stored = fake.sanitize(html)
    a["description"] = stored
    bodies.record("assignments/hw.md", "My *own* markdown\n", stored)
    got = remote.read(fake.client(), ctx_for(tmp_path, previous=prev, bodies=bodies))
    assert cast(Assignment, got["assignments/hw.md"]).body == "My *own* markdown\n"


def test_body_converted_when_remote_changed_and_html_format_kept(tmp_path: Path) -> None:
    fake = FakeCanvas()
    a = add_assignment(fake, "Hw", description="<p>Hello <strong>there</strong></p>")
    b = add_assignment(fake, "Raw", description="<div class='x'>raw</div>")
    prev = previous_with(
        {
            "assignments/hw.md": Assignment(canvas_id=a["id"], title="Hw", body="stale\n"),
            "assignments/raw.md": Assignment(
                canvas_id=b["id"], title="Raw", body_format="html", body="old"
            ),
        }
    )
    got = remote.read(fake.client(), ctx_for(tmp_path, previous=prev))
    hw = cast(Assignment, got["assignments/hw.md"])
    assert hw.body == "Hello **there**\n" and hw.body_format == "md"
    raw = cast(Assignment, got["assignments/raw.md"])
    assert raw.body == "<div class='x'>raw</div>" and raw.body_format == "html"


def test_links_in_body_become_relative(tmp_path: Path) -> None:
    fake = FakeCanvas()
    a = add_assignment(fake, "Hw")
    b = add_assignment(
        fake, "Other", description=f'<p><a href="/courses/101/assignments/{a["id"]}">o</a></p>'
    )
    got = remote.read(fake.client(), ctx_for(tmp_path))
    assert cast(Assignment, got["assignments/other.md"]).body == "[o](hw.md)\n"
    assert b["id"]


def test_reader_only_issues_gets_and_guard_fake_still_works(tmp_path: Path) -> None:
    fake = FakeCanvas()
    guard_fake.install(fake)
    add_assignment(fake, "Hw")
    remote.read(fake.client("read"), ctx_for(tmp_path))
    assert {r.method for r in fake.log} == {"GET"}
    g = guard_fake.add_assignment(fake, name="Guarded")
    assert fake.client("read").get_one(f"/courses/101/assignments/{g['id']}")["name"] == "Guarded"
