"""Offline tests for the submission-safety suite (bead y01.4): guards and a full rehearsal of
every scenario against FakeCanvas. Nothing here touches the network."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import pytest

from canvas_sync.errors import CanvasSyncError
from canvas_sync.model.base import Kind, ObjectKey
from canvas_sync.plan.core import Action, Operation, Plan
from canvas_sync.remote.http import CanvasClient
from tests.fake_canvas.assignments import add_assignment
from tests.fake_canvas.core import FakeCanvas
from tests.integration import sandbox
from tests.integration.sandbox import (
    PREFIX,
    Harness,
    Refused,
    ScopeViolation,
    SuiteClient,
    assert_recorded,
    assert_suite_name,
    check_course_unpublished,
    check_env,
    check_scope,
)
from tests.integration.scenarios import SCENARIOS

GOOD = {
    "CANVAS_SANDBOX_URL": "https://canvas.ubc.ca",
    "CANVAS_SANDBOX_COURSE": "198014",
    "CANVAS_TOKEN": "x",
    "CANVAS_SAFETY_OK": "1",
}


# ---- guards ----------------------------------------------------------------------------------


def test_env_accepts_only_the_sandbox() -> None:
    assert check_env(dict(GOOD)) == ("https://canvas.ubc.ca", 198014)


@pytest.mark.parametrize(
    "change",
    [
        {"CANVAS_SANDBOX_COURSE": "151648"},
        {"CANVAS_SANDBOX_COURSE": "198015"},
        {"CANVAS_SANDBOX_COURSE": ""},
        {"CANVAS_SANDBOX_URL": "https://canvas.example.edu"},
        {"CANVAS_SAFETY_OK": ""},
        {"CANVAS_SAFETY_OK": "yes"},
        {"CANVAS_TOKEN": ""},
    ],
)
def test_env_refuses_everything_else(change: dict[str, str]) -> None:
    with pytest.raises(Refused):
        check_env({**GOOD, **change})


def test_published_course_refused() -> None:
    check_course_unpublished({"workflow_state": "unpublished"})
    with pytest.raises(Refused):
        check_course_unpublished({"workflow_state": "available"})


def test_recorded_and_name_checks() -> None:
    with pytest.raises(Refused):
        assert_recorded({1, 2}, 3)
    assert_recorded({1, 2}, 2)
    with pytest.raises(Refused):
        assert_suite_name("Homework 1", 5)
    assert_suite_name(f"{PREFIX}-graded", 5)


def _suite_client(fake: FakeCanvas, recorded: set[int]) -> SuiteClient:
    return SuiteClient(
        fake.base_url,
        "t",
        course_id=fake.course_id,
        mode="write",
        transport=fake,
        sleep=fake.sleeps.append,
        recorded=recorded,
    )


def test_cleanup_deletes_nothing_unrecorded_or_misnamed() -> None:
    fake = FakeCanvas()
    real = add_assignment(fake, "Homework 1")
    mine = add_assignment(fake, f"{PREFIX}-x")
    renamed = add_assignment(fake, "Homework 2")  # recorded, but its name is not ours
    client = _suite_client(fake, {mine["id"], renamed["id"]})
    with pytest.raises(Refused):
        sandbox.cleanup(client, {real["id"]: "x"}, lambda _m: None)
    with pytest.raises(Refused):
        sandbox.cleanup(client, {renamed["id"]: f"{PREFIX}-y"}, lambda _m: None)
    assert real["id"] in fake.state("assignments")["assignments"]
    assert renamed["id"] in fake.state("assignments")["assignments"]
    assert sandbox.cleanup(client, {mine["id"]: f"{PREFIX}-x"}, lambda _m: None) == [mine["id"]]


def test_suite_client_refuses_writes_outside_recorded_assignments() -> None:
    fake = FakeCanvas()
    real = add_assignment(fake, "Homework 1")
    mine = add_assignment(fake, f"{PREFIX}-x")
    client = _suite_client(fake, {mine["id"]})
    c = fake.course_id
    with pytest.raises(Refused):
        client.put(f"/courses/{c}/assignments/{real['id']}", json={"assignment": {"name": "z"}})
    with pytest.raises(Refused):
        client.delete(f"/courses/{c}/assignments/{real['id']}")
    with pytest.raises(Refused):
        client.post(f"/courses/{c}/pages", json={"wiki_page": {"title": "t"}})
    assert not [r for r in fake.log if r.method != "GET"]
    client.put(f"/courses/{c}/assignments/{mine['id']}", json={"assignment": {"name": "ok"}})


def test_scope_check_aborts_on_a_non_suite_object() -> None:
    def op(kind: Kind, path: str, action: Action = Action.UPDATE) -> Operation:
        return Operation(ObjectKey(kind, path), action, None, None, {})

    ok = op(Kind.ASSIGNMENT, f"assignments/{PREFIX}-a.md")
    check_scope(Plan([ok]))
    for bad in (
        op(Kind.ASSIGNMENT, "assignments/homework-1.md", Action.DELETE),
        op(Kind.PAGE, f"pages/{PREFIX}-a.md"),
    ):
        with pytest.raises(ScopeViolation):
            check_scope(Plan([ok, bad]))


def test_raw_seeder_fence() -> None:
    s = sandbox.RawSeeder("https://canvas.ubc.ca", "tok", 198014, 7, {11})
    assert "tok" not in repr(s)
    sub = "/api/v1/courses/198014/assignments/11/submissions"
    for method, path, params in [
        ("POST", "/api/v1/courses/198014/assignments/12/submissions", {"as_user_id": 7}),
        ("POST", sub, {"as_user_id": 8}),
        ("POST", sub, {}),
        ("PUT", f"{sub}/8", {}),
        ("PUT", "/api/v1/courses/151648/assignments/11/submissions/7", {}),
        ("DELETE", f"{sub}/7", {}),
        ("GET", "/api/v1/courses/198014/users/self", {}),
    ]:
        with pytest.raises(Refused):
            s._fenced(method, path, params)  # pyright: ignore[reportPrivateUsage]
    s._fenced("POST", sub, {"as_user_id": 7})  # pyright: ignore[reportPrivateUsage]
    s._fenced("PUT", f"{sub}/7", {})  # pyright: ignore[reportPrivateUsage]


# ---- rehearsal against FakeCanvas ------------------------------------------------------------


class FakeSeeder:
    def __init__(self, fake: FakeCanvas, student_id: int) -> None:
        self.fake, self.sid = fake, student_id

    def submit_text(self, assignment_id: int, body: str) -> str:
        self.fake.seed_submissions(
            assignment_id,
            [
                {
                    "user_id": self.sid,
                    "workflow_state": "submitted",
                    "attempt": 1,
                    "body": body,
                    "submission_type": "online_text_entry",
                    "submitted_at": "2027-01-01T00:00:00Z",
                }
            ],
        )
        return "submission (fake)"

    def grade(self, assignment_id: int, score: float) -> None:
        for s in self.fake.submissions[assignment_id]:
            if s["user_id"] == self.sid:
                s.update(workflow_state="graded", score=score, grade=str(score))

    def version(self) -> str:
        return "fake"


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCanvas:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    f = FakeCanvas(course_id=101)
    f.add_section("Default")
    f.add_user("Test Student", role="StudentViewEnrollment")
    return f


@pytest.fixture
def harness(fake: FakeCanvas, tmp_path: Path) -> Harness:
    def make(mode: Literal["read", "write"], recorded: set[int]) -> CanvasClient:
        return fake.client("read") if mode == "read" else _suite_client(fake, recorded)

    work = tmp_path / "work"
    work.mkdir()
    return Harness(
        make_client=make,
        seeder_factory=lambda sid, _rec: FakeSeeder(fake, sid),
        url=fake.base_url,
        course_id=101,
        workdir=work,
        log=lambda _m: None,
    )


def test_every_scenario_against_fake_canvas(fake: FakeCanvas, harness: Harness) -> None:
    real = add_assignment(fake, "Real Homework", description="<p>mine</p>", points_possible=5)
    harness.seed()
    harness.clone()
    assert harness.suite_files() and "assignments/real-homework.md" not in harness.suite_files()
    outcomes: dict[str, Any] = {fn.__name__: fn(harness) for fn in SCENARIOS}
    assert len(outcomes) == len(SCENARIOS)
    stored = fake.state("assignments")["assignments"]
    assert stored[real["id"]]["points_possible"] == 5
    assert all(str(real["id"]) not in r.path for r in fake.log if r.method != "GET")
    harness.finish()
    with pytest.raises(CanvasSyncError):
        harness.read.get_one(f"/courses/101/assignments/{harness.ids['graded']}")
