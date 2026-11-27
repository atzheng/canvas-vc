"""SubmissionGuard: one test per row of the SPEC risk table, plus bypass attempts.

The guard re-reads live state from FakeCanvas. The plan objects below deliberately
carry stale or empty data to show the guard does not trust them.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from canvas_sync.config import CourseConfig
from canvas_sync.errors import GuardBlocked
from canvas_sync.model.announcement import Announcement
from canvas_sync.model.assignment import Assignment
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.overrides import Override
from canvas_sync.plan.core import Action, Operation, Risk, RiskKind
from canvas_sync.remote.guard import (
    CONFIRM_ACCESS,
    CONFIRM_GRADING,
    CONFIRM_NOTIFY,
    SubmissionGuard,
)
from tests.fake_canvas.core import FakeCanvas
from tests.helpers import guard_fake as gf

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
KEY = ObjectKey(Kind.ASSIGNMENT, "assignments/hw1.md")
EVERY_FLAG = {CONFIRM_GRADING, CONFIRM_ACCESS, CONFIRM_NOTIFY, f"{CONFIRM_GRADING} {KEY.path}"}


class AssignmentGroup(CanvasObject):
    name: str = ""


@pytest.fixture
def fake() -> FakeCanvas:
    f = FakeCanvas(course_id=101)
    gf.install(f)
    return f


def make_guard(fake: FakeCanvas, **cfg: Any) -> SubmissionGuard:
    config = CourseConfig.model_validate(
        {"canvas_url": fake.base_url, "course_id": 101, "timezone": "UTC", **cfg}
    )
    return SubmissionGuard(fake.client(), config, now=lambda: NOW)


def op(
    action: Action,
    before: CanvasObject | None,
    after: CanvasObject | None,
    changes: dict[str, tuple[Any, Any]] | None = None,
    key: ObjectKey = KEY,
    risks: tuple[Risk, ...] = (),
) -> Operation:
    return Operation(key, action, before, after, changes or {}, risks)


def asg(aid: int, **fields: Any) -> Assignment:
    return Assignment(**{"canvas_id": aid, "title": "HW1", "points": 10, **fields})


def submit(fake: FakeCanvas, aid: int, user: int, score: float | None = None) -> None:
    sub: dict[str, Any] = {"user_id": user, "score": score}
    if score is not None:
        sub.update(workflow_state="graded", grade=str(score))
    fake.seed_submissions(aid, [sub])


def with_submissions(fake: FakeCanvas, flags: bool = True) -> int:
    a = gf.add_assignment(fake, has_submitted_submissions=flags)
    submit(fake, a["id"], 7, 8.0)
    return a["id"]


def delete_op(aid: int) -> Operation:
    # The plan's snapshot says nothing about submissions: the guard must look live.
    return op(Action.DELETE, asg(aid), None)


def no_writes(fake: FakeCanvas) -> None:
    assert fake.writes() == []


# ---- delete an assignment ---------------------------------------------------------


def test_delete_with_submissions_blocked_before_any_delete_reaches_canvas(
    fake: FakeCanvas,
) -> None:
    aid = with_submissions(fake)
    snap = fake.snapshot_submissions()
    guard = make_guard(fake)
    with pytest.raises(GuardBlocked, match="never deleted"):
        guard.assert_allowed(delete_op(aid), set())
    assert [e.method for e in fake.log if e.method == "DELETE"] == []
    no_writes(fake)
    fake.assert_submissions_intact(snap)
    assert aid in fake.state("guard_assignments")


@pytest.mark.parametrize(
    "confirmed",
    [
        set(),
        EVERY_FLAG,
        {"--force", "--yes", "--confirm-delete", "--confirm-grading-change assignments/hw1.md"},
        {"*", "all", ""},
    ],
)
def test_delete_block_cannot_be_bypassed_by_any_confirmed_flag(
    fake: FakeCanvas, confirmed: set[str]
) -> None:
    aid = with_submissions(fake)
    with pytest.raises(GuardBlocked):
        make_guard(fake).assert_allowed(delete_op(aid), confirmed)
    no_writes(fake)


def test_delete_blocked_when_assignment_flags_lag_but_submissions_exist(fake: FakeCanvas) -> None:
    aid = with_submissions(fake, flags=False)  # flags say no, the submissions list says yes
    with pytest.raises(GuardBlocked):
        make_guard(fake).assert_allowed(delete_op(aid), EVERY_FLAG)
    no_writes(fake)


def test_delete_ignores_unsubmitted_placeholders(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    fake.seed_submissions(
        a["id"], [{"user_id": 7, "workflow_state": "unsubmitted", "submitted_at": None, "attempt": None, "submission_type": None, "body": None}]
    )
    make_guard(fake).assert_allowed(delete_op(a["id"]), set())


def test_delete_without_submissions_is_allowed(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    guard = make_guard(fake)
    assert guard.check(delete_op(a["id"])) == []
    guard.assert_allowed(delete_op(a["id"]), set())


def test_delete_already_gone_is_not_blocked(fake: FakeCanvas) -> None:
    make_guard(fake).assert_allowed(delete_op(424242), set())


def test_check_reports_submissions_risk_without_confirm_flag(fake: FakeCanvas) -> None:
    aid = with_submissions(fake)
    risks = make_guard(fake).check(delete_op(aid))
    assert [r.kind for r in risks] == [RiskKind.HAS_SUBMISSIONS]
    assert risks[0].needs_confirm is None


def test_assert_allowed_is_idempotent(fake: FakeCanvas) -> None:
    aid = with_submissions(fake)
    guard = make_guard(fake)
    for _ in range(3):
        with pytest.raises(GuardBlocked):
            guard.assert_allowed(delete_op(aid), set())
    update = op(Action.UPDATE, asg(aid), asg(aid, points=20), {"points": (10, 20)})
    guard.assert_allowed(update, {CONFIRM_GRADING})
    guard.assert_allowed(update, {CONFIRM_GRADING})


def test_delete_with_unknown_assignment_id_is_blocked(fake: FakeCanvas) -> None:
    with pytest.raises(GuardBlocked, match="cannot tell"):
        make_guard(fake).assert_allowed(op(Action.DELETE, Assignment(), None), EVERY_FLAG)


# ---- delete an assignment group -----------------------------------------------------


@pytest.mark.parametrize("confirmed", [set(), EVERY_FLAG])
def test_group_delete_always_blocked_without_touching_canvas(
    fake: FakeCanvas, confirmed: set[str]
) -> None:
    group = AssignmentGroup(canvas_id=5, name="Homework")
    gop = op(Action.DELETE, group, None, key=ObjectKey(Kind.ASSIGNMENT, "assignments/_groups.yaml"))
    with pytest.raises(GuardBlocked, match="never deleted"):
        make_guard(fake).assert_allowed(gop, confirmed)
    assert fake.log == []


def test_group_create_and_update_are_not_blocked(fake: FakeCanvas) -> None:
    group = AssignmentGroup(canvas_id=5, name="Homework")
    key = ObjectKey(Kind.ASSIGNMENT, "assignments/_groups.yaml")
    make_guard(fake).assert_allowed(op(Action.CREATE, None, group, key=key), set())
    make_guard(fake).assert_allowed(op(Action.UPDATE, group, group, key=key), set())


# ---- group_category -----------------------------------------------------------------


def test_group_category_change_with_submissions_blocked(fake: FakeCanvas) -> None:
    aid = with_submissions(fake)
    change = op(Action.UPDATE, asg(aid), asg(aid), {"group_category": (None, "Teams")})
    with pytest.raises(GuardBlocked, match="group_category"):
        make_guard(fake).assert_allowed(change, EVERY_FLAG)
    no_writes(fake)


def test_group_category_change_without_submissions_allowed(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    change = op(Action.UPDATE, asg(a["id"]), asg(a["id"]), {"group_category": (None, "Teams")})
    make_guard(fake).assert_allowed(change, set())


# ---- grading_type / points ----------------------------------------------------------


@pytest.mark.parametrize(
    "field,old,new", [("points", 10, 20), ("grading_type", "points", "percent")]
)
def test_grading_change_needs_confirmation(
    fake: FakeCanvas, field: str, old: Any, new: Any
) -> None:
    aid = with_submissions(fake)
    change = op(Action.UPDATE, asg(aid), asg(aid, **{field: new}), {field: (old, new)})
    guard = make_guard(fake)
    risks = guard.check(change)
    assert [(r.kind, r.needs_confirm) for r in risks] == [
        (RiskKind.GRADING_CHANGE, CONFIRM_GRADING)
    ]
    assert "1 graded" in risks[0].message
    with pytest.raises(GuardBlocked, match="--confirm-grading-change"):
        guard.assert_allowed(change, set())
    with pytest.raises(GuardBlocked):
        guard.assert_allowed(change, {CONFIRM_ACCESS, CONFIRM_NOTIFY})
    guard.assert_allowed(change, {CONFIRM_GRADING})
    guard.assert_allowed(change, {f"{CONFIRM_GRADING} {KEY.path}"})
    with pytest.raises(GuardBlocked):  # scoped to some other object
        guard.assert_allowed(change, {f"{CONFIRM_GRADING} assignments/other.md"})
    no_writes(fake)


def test_grading_change_detected_from_objects_when_changes_empty(fake: FakeCanvas) -> None:
    aid = with_submissions(fake)
    with pytest.raises(GuardBlocked):
        make_guard(fake).assert_allowed(op(Action.UPDATE, asg(aid), asg(aid, points=99)), set())


def test_grading_change_with_no_graded_submissions_needs_nothing(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    fake.seed_submissions(a["id"], [{"user_id": 7, "workflow_state": "submitted"}])
    change = op(Action.UPDATE, asg(a["id"]), asg(a["id"], points=20), {"points": (10, 20)})
    make_guard(fake).assert_allowed(change, set())


def test_static_risk_on_the_operation_is_also_enforced(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    static = Risk(RiskKind.GRADING_CHANGE, "static", CONFIRM_GRADING)
    change = op(Action.UPDATE, asg(a["id"]), asg(a["id"], title="x"), risks=(static,))
    with pytest.raises(GuardBlocked):
        make_guard(fake).assert_allowed(change, set())
    make_guard(fake).assert_allowed(change, {CONFIRM_GRADING})


# ---- late policy --------------------------------------------------------------------

DUE = datetime(2026, 3, 10, 23, 59, tzinfo=UTC)
DUE2 = DUE + timedelta(days=3)


def due_change(aid: int) -> Operation:
    return op(Action.UPDATE, asg(aid, due=DUE), asg(aid, due=DUE2), {"due": (DUE, DUE2)})


def test_due_change_with_late_policy_on_needs_confirmation(fake: FakeCanvas) -> None:
    aid = with_submissions(fake)
    gf.set_late_policy(fake, True)
    guard = make_guard(fake)
    risks = guard.check(due_change(aid))
    assert [(r.kind, r.needs_confirm) for r in risks] == [
        (RiskKind.LATE_POLICY_REGRADE, CONFIRM_GRADING)
    ]
    assert "1 graded" in risks[0].message
    with pytest.raises(GuardBlocked):
        guard.assert_allowed(due_change(aid), set())
    guard.assert_allowed(due_change(aid), {CONFIRM_GRADING})
    # the late policy is read from the dedicated endpoint
    assert any(e.path == "/courses/101/late_policy" for e in fake.log)


def test_due_change_late_policy_off_or_absent_is_free(fake: FakeCanvas) -> None:
    aid = with_submissions(fake)
    guard = make_guard(fake)
    guard.assert_allowed(due_change(aid), set())  # no late policy at all (404)
    gf.set_late_policy(fake, False)
    guard.assert_allowed(due_change(aid), set())


def test_due_change_late_policy_on_but_nothing_graded_is_free(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    gf.set_late_policy(fake, True)
    make_guard(fake).assert_allowed(due_change(a["id"]), set())


def test_override_date_change_counts_as_due_change(fake: FakeCanvas) -> None:
    aid = with_submissions(fake)
    gf.set_late_policy(fake, True)
    sec = Override(section="BA2", due=DUE)
    sec2 = Override(section="BA2", due=DUE2)
    change = op(
        Action.UPDATE,
        asg(aid, overrides=[sec]),
        asg(aid, overrides=[sec2]),
        {"overrides": ([sec], [sec2])},
    )
    with pytest.raises(GuardBlocked, match="late policy"):
        make_guard(fake).assert_allowed(change, set())


# ---- access removed -------------------------------------------------------------------


def test_removing_student_override_with_submitter_needs_confirmation(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    fake.add_user("Pat Placeholder", login_id="pat")
    pat = next(iter(fake.users))
    submit(fake, a["id"], pat)
    gf.add_override(fake, a["id"], student_ids=[pat], title="1 student")
    removal = op(
        Action.UPDATE,
        asg(a["id"], overrides=[Override(students=["pat"])]),
        asg(a["id"]),
        {"overrides": ([Override(students=["pat"])], [])},
    )
    guard = make_guard(fake)
    risks = guard.check(removal)
    assert [(r.kind, r.needs_confirm) for r in risks] == [(RiskKind.ACCESS_REMOVED, CONFIRM_ACCESS)]
    assert "pat" not in risks[0].message.lower().replace("path", "")  # no student identifiers
    with pytest.raises(GuardBlocked, match="--confirm-access-removal"):
        guard.assert_allowed(removal, {CONFIRM_GRADING})
    guard.assert_allowed(removal, {CONFIRM_ACCESS})


def test_removing_override_of_student_who_has_not_submitted_is_free(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    fake.add_user("Quinn Placeholder", login_id="quinn")
    quinn = next(iter(fake.users))
    submit(fake, a["id"], quinn + 999)  # somebody else submitted
    gf.add_override(fake, a["id"], student_ids=[quinn])
    removal = op(
        Action.UPDATE,
        asg(a["id"], overrides=[Override(students=["quinn"])]),
        asg(a["id"]),
        {"overrides": ([Override(students=["quinn"])], [])},
    )
    make_guard(fake).assert_allowed(removal, set())


def test_kept_student_override_is_not_removal(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    fake.add_user("Pat Placeholder", login_id="pat")
    pat = next(iter(fake.users))
    submit(fake, a["id"], pat)
    gf.add_override(fake, a["id"], student_ids=[pat])
    kept = Override(students=["pat"], due=DUE)
    move = op(
        Action.UPDATE,
        asg(a["id"], overrides=[Override(students=["pat"])]),
        asg(a["id"], overrides=[kept]),
        {"overrides": ([Override(students=["pat"])], [kept])},
    )
    make_guard(fake).assert_allowed(move, set())


def test_removing_section_override_with_submitter_needs_confirmation(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    sec = fake.add_section("BA2")
    fake.add_user("Pat Placeholder", section_id=sec["id"])
    pat = next(iter(fake.users))
    submit(fake, a["id"], pat)
    gf.add_override(fake, a["id"], course_section_id=sec["id"])
    removal = op(
        Action.UPDATE,
        asg(a["id"], overrides=[Override(section="BA2")]),
        asg(a["id"]),
        {"overrides": ([Override(section="BA2")], [])},
    )
    with pytest.raises(GuardBlocked, match="lose access"):
        make_guard(fake).assert_allowed(removal, set())


def test_turning_on_assign_to_overrides_only_needs_confirmation(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    fake.add_user("Pat Placeholder")
    pat = next(iter(fake.users))
    submit(fake, a["id"], pat)
    change = op(
        Action.UPDATE,
        asg(a["id"]),
        asg(a["id"], assign_to_overrides_only=True),
        {"assign_to_overrides_only": (False, True)},
    )
    with pytest.raises(GuardBlocked, match="lose access"):
        make_guard(fake).assert_allowed(change, set())
    make_guard(fake).assert_allowed(change, {CONFIRM_ACCESS})


# ---- unpublish / submission_types ---------------------------------------------------


def test_unpublish_with_submissions_is_not_attempted(fake: FakeCanvas) -> None:
    aid = with_submissions(fake)
    unpub = op(Action.UNPUBLISH, asg(aid, published=True), asg(aid, published=False))
    with pytest.raises(GuardBlocked, match="unpublish"):
        make_guard(fake).assert_allowed(unpub, EVERY_FLAG)
    no_writes(fake)


def test_unpublish_without_submissions_is_allowed(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    unpub = op(Action.UNPUBLISH, asg(a["id"], published=True), asg(a["id"], published=False))
    make_guard(fake).assert_allowed(unpub, set())


def test_submission_types_change_warns_but_is_allowed(fake: FakeCanvas) -> None:
    aid = with_submissions(fake)
    change = op(
        Action.UPDATE,
        asg(aid, submission_types=["online_upload"]),
        asg(aid, submission_types=["online_text_entry"]),
        {"submission_types": (["online_upload"], ["online_text_entry"])},
    )
    guard = make_guard(fake)
    risks = guard.check(change)
    assert [(r.kind, r.needs_confirm) for r in risks] == [(RiskKind.HAS_SUBMISSIONS, None)]
    guard.assert_allowed(change, set())


# ---- announcements ------------------------------------------------------------------


def ann_op(before: Announcement | None, after: Announcement) -> Operation:
    key = ObjectKey(Kind.ANNOUNCEMENT, "announcements/a.md")
    return op(Action.CREATE if before is None else Action.UPDATE, before, after, key=key)


def test_announcement_posting_now_needs_notify_confirmation(fake: FakeCanvas) -> None:
    guard = make_guard(fake)
    now_post = ann_op(None, Announcement(title="Hi"))
    assert [r.kind for r in guard.check(now_post)] == [RiskKind.NOTIFIES_STUDENTS]
    with pytest.raises(GuardBlocked, match="--confirm-notify"):
        guard.assert_allowed(now_post, set())
    guard.assert_allowed(now_post, {CONFIRM_NOTIFY})
    assert fake.log == []  # announcements need no live reads


def test_announcement_with_future_post_at_or_unpublished_is_free(fake: FakeCanvas) -> None:
    guard = make_guard(fake)
    guard.assert_allowed(ann_op(None, Announcement(post_at=NOW + timedelta(days=1))), set())
    guard.assert_allowed(ann_op(None, Announcement(published=False)), set())


def test_editing_an_already_posted_announcement_does_not_renotify(fake: FakeCanvas) -> None:
    old = Announcement(canvas_id=3, post_at=NOW - timedelta(days=1))
    new = Announcement(canvas_id=3, post_at=NOW - timedelta(days=1), title="typo fixed")
    make_guard(fake).assert_allowed(ann_op(old, new), set())


# ---- fail closed, read-only, scope ---------------------------------------------------


def test_unreadable_live_state_refuses_the_operation(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    fake.fail(500, method="GET", path=r"/courses/101/assignments/\d+", times=None)
    with pytest.raises(GuardBlocked, match="cannot verify"):
        make_guard(fake).assert_allowed(delete_op(a["id"]), EVERY_FLAG)
    no_writes(fake)


def test_unreadable_submissions_refuse_the_delete(fake: FakeCanvas) -> None:
    a = gf.add_assignment(fake)
    fake.fail(500, method="GET", path=r"/courses/101/assignments/\d+/submissions", times=None)
    with pytest.raises(GuardBlocked, match="cannot verify"):
        make_guard(fake).assert_allowed(delete_op(a["id"]), EVERY_FLAG)


def test_guard_only_reads_and_stays_in_its_course(fake: FakeCanvas) -> None:
    aid = with_submissions(fake)
    gf.set_late_policy(fake, True)
    guard = make_guard(fake)
    for o in (delete_op(aid), due_change(aid)):
        with contextlib.suppress(GuardBlocked):
            guard.assert_allowed(o, set())
    assert fake.log and all(e.method == "GET" for e in fake.log)
    assert all(e.path.startswith("/courses/101/") for e in fake.log)


def test_guard_does_not_trust_the_plans_view_of_submissions(fake: FakeCanvas) -> None:
    """Snapshot said 'no submissions'; a student submitted since. Live state wins."""
    a = gf.add_assignment(fake, has_submitted_submissions=False)
    stale = delete_op(a["id"])
    make_guard(fake).assert_allowed(stale, set())  # nothing yet; now a student submits
    fake.state("guard_assignments")[a["id"]]["has_submitted_submissions"] = True
    with pytest.raises(GuardBlocked):
        make_guard(fake).assert_allowed(stale, set())


@pytest.mark.parametrize(
    "work",
    [
        {"submission_comments": [{"id": 1, "comment": "please resubmit"}]},
        {"attempt": 1},
        {"submission_type": "online_text_entry"},
        {"body": "<p>answer</p>"},
        {"url": "https://example.edu/x"},
        {"attachments": [{"id": 9}]},
    ],
)
def test_delete_blocked_by_any_kind_of_student_work(fake: FakeCanvas, work: dict[str, Any]) -> None:
    """Flags say no submissions; the only trace is a comment, attempt, body, ..."""
    a = gf.add_assignment(fake, has_submitted_submissions=False)
    placeholder = {"user_id": 7, "workflow_state": "unsubmitted", "submitted_at": None}
    fake.seed_submissions(a["id"], [{**placeholder, "score": None, "grade": None, **work}])
    with pytest.raises(GuardBlocked):
        make_guard(fake).assert_allowed(delete_op(a["id"]), EVERY_FLAG)
    assert any(
        e.params.get("include[]") == ["submission_comments"]
        or "submission_comments" in str(e.params)
        for e in fake.log
        if e.path.endswith("/submissions")
    )
    no_writes(fake)
