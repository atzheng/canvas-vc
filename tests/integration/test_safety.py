"""Submission-safety suite on the sandbox course (bead canvas-sync-y01.4, P0).

Seeds a Student View test-student submission on assignments the suite itself creates
(``zz-canvas-sync-safety-*``), runs every push scenario through the real ``run_push`` in a temp
clone scoped to those assignments, and asserts every submission is unchanged afterwards.

RUN BY THE OWNER / COORDINATOR ONLY (see the PR body for the run sheet). It skips unless::

    CANVAS_SANDBOX_URL=https://canvas.ubc.ca  CANVAS_SANDBOX_COURSE=198014
    CANVAS_SAFETY_OK=1  CANVAS_TOKEN=...

and refuses any other course id. Offline tests of the guards and a FakeCanvas rehearsal of the
same scenarios live in tests/test_safety_rehearsal.py.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from typing import Any, Literal

import pytest

from canvas_sync.remote.http import CanvasClient
from tests.integration.sandbox import (
    Harness,
    RawSeeder,
    Refused,
    SuiteClient,
    check_course_unpublished,
    check_env,
    check_no_stale_state,
    new_workdir,
    save_state,
    state_path,
)
from tests.integration.scenarios import SCENARIOS

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def suite() -> Iterator[Harness]:
    env = dict(os.environ)
    try:
        url, course = check_env(env)
        state = state_path(env)
        check_no_stale_state(state)
    except Refused as e:
        pytest.skip(str(e))

    # The token is read where it is used and kept in no local, so --showlocals cannot show it.
    def make(
        mode: Literal["read", "write"], recorded: set[int], students: set[int]
    ) -> CanvasClient:
        return SuiteClient(
            url, os.environ["CANVAS_TOKEN"], course_id=course, mode=mode, recorded=recorded,
            students=students,
        )  # fmt: skip

    created: dict[int, str] = {}

    def on_record(aid: int, name: str) -> None:
        created[aid] = name
        save_state(state, created)  # written before anything else happens to the object

    h = Harness(
        make_client=make,
        seeder_factory=lambda sid, rec: RawSeeder(
            url, os.environ["CANVAS_TOKEN"], course, sid, rec
        ),
        url=url,
        course_id=course,
        workdir=new_workdir(),
        on_record=on_record,
        check_course=lambda c: check_course_unpublished(c.get_one(f"/courses/{course}")),
    )
    try:
        h.seed()
        h.clone()
        h.notes["scenarios"] = {}
        yield h
    finally:
        try:
            # The report holds counts and outcomes only. It is written outside the clone.
            report = state.parent / "safety-suite-report.json"
            report.write_text(json.dumps(h.notes, indent=2, default=str))
            print(f"\nsafety suite report: {report}")
            print(json.dumps(h.notes, indent=2, default=str))
            h.finish(state)
        finally:
            h.read.close()
            h.write.close()
            # The clone's roster cache can hold real students' refs: never leave it behind.
            shutil.rmtree(h.workdir, ignore_errors=True)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__)
def test_scenario_leaves_submissions_unchanged(suite: Harness, scenario: Any) -> None:
    outcome = scenario(suite)
    suite.notes["scenarios"][scenario.__name__] = outcome
    # A scenario may declare the hidden state it is expected to leave (override_removal:
    # Canvas hides the submission of a student who lost access until access_restored).
    suite.assert_submissions_unchanged(hidden=getattr(scenario, "leaves_hidden", ()))
    # No scenario may leave the temp clone pointing at objects outside the suite.
    assert all(f.startswith("assignments/zz-canvas-sync-safety-") for f in suite.suite_files())


def test_findings_are_recorded(suite: Harness) -> None:
    """The two bead NOTES questions and the build version; values go to the report."""
    for key in ("q1_submissions_listing", "q2_student_in_two_overrides", "canvas_version"):
        assert key in suite.notes
    assert (
        suite.notes["q1_submissions_listing"]["single_get_works"]
        or suite.notes["q1_submissions_listing"]["default_includes_test_student"]
    ), "no way to read the test student's submission"
