"""Assignments differ and writer, end to end against FakeCanvas with seeded submissions.

Covers the SPEC risk-table rows about deletion, groups, grading_type/points and late policy.
Every test that touches an assignment with submissions ends with ``assert_submissions_intact``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from canvas_sync.apply import ApplyResult, apply
from canvas_sync.config import CourseConfig
from canvas_sync.errors import CanvasSyncError, GuardBlocked, ValidationError
from canvas_sync.ignore import IgnoreRules
from canvas_sync.model.assignment import ASSIGNMENT_SPEC, Assignment
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import FilesMeta
from canvas_sync.model.overrides import Override
from canvas_sync.model.roster import Roster, RosterCache
from canvas_sync.plan.assignments import AssignmentGroup, diff_type
from canvas_sync.plan.core import Action, Operation, Plan, RiskKind, diff
from canvas_sync.remote import assignments as remote
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.remote.guard import CONFIRM_GRADING, SubmissionGuard
from canvas_sync.render.bodies import Bodies
from canvas_sync.snapshot import BlobStore
from tests.fake_canvas.assignments import (
    add_assignment,
    add_assignment_group,
    add_override,
    set_late_policy,
)
from tests.fake_canvas.core import FakeCanvas

CFG = CourseConfig.model_validate(
    {"canvas_url": "https://canvas.test", "course_id": 101, "timezone": "UTC"}
)
PREFIX = "assignments/"


def make_course(objs: dict[str, Assignment]) -> Course:
    return Course(
        CFG, {ObjectKey(Kind.ASSIGNMENT, p): o for p, o in objs.items()}, FilesMeta(), [], Roster()
    )


def asg(**fields: Any) -> Assignment:
    return Assignment(**{"title": "HW", "points": 10.0, "published": True, **fields})


def plan_of(base: Course, target: Course) -> Plan:
    return diff(base, target, [diff_type])  # the other types' differs are not built yet


def ops_of(base: dict[str, Assignment], target: dict[str, Assignment]) -> list[Operation]:
    return diff_type(make_course(base), make_course(target))


# ---- differ (pure) --------------------------------------------------------------------


def test_identical_trees_have_no_ops() -> None:
    a = asg(canvas_id=1, group="Homework", due=None)
    assert ops_of({"assignments/a.md": a}, {"assignments/a.md": a}) == []


def test_unknown_refs_unchanged_produce_no_ops() -> None:
    ov = [
        Override(students=["unknown:55", "jdoe"], due=None),
        Override(section="unknown:7"),
        Override(group="unknown:9"),
    ]
    a = asg(canvas_id=1, overrides=ov)
    assert ops_of({"assignments/a.md": a}, {"assignments/a.md": a.model_copy()}) == []


def test_override_change_is_refused_at_plan_time() -> None:
    old = asg(canvas_id=1)
    new = old.model_copy(update={"overrides": [Override(section="BA2")]})
    with pytest.raises(ValidationError, match=r"assignments/a.md: pushing override changes"):
        ops_of({"assignments/a.md": old}, {"assignments/a.md": new})
    with pytest.raises(ValidationError, match="override changes"):
        ops_of({}, {"assignments/n.md": asg(overrides=[Override(section="BA2")])})


def test_duplicate_canvas_id_in_target_is_refused() -> None:
    a = asg(canvas_id=1)
    with pytest.raises(ValidationError, match=r"assignments/b.md.*assignments/a.md"):
        ops_of({"assignments/a.md": a}, {"assignments/a.md": a, "assignments/b.md": a})


def test_canvas_normalised_values_do_not_diff() -> None:
    old = asg(canvas_id=1, submission_types=["none"], points=0.0)
    new = asg(canvas_id=1, submission_types=[], points=None)
    assert ops_of({"assignments/a.md": old}, {"assignments/a.md": new}) == []


def test_id_missing_from_base_is_not_recreated_or_deleted() -> None:
    # Canvas deleted it, or turned it into a quiz: nothing to do, in either direction.
    assert ops_of({}, {"assignments/a.md": asg(canvas_id=99)}) == []


def test_rename_with_same_id_is_no_op_and_stripped_id_is_an_update() -> None:
    a = asg(canvas_id=1)
    assert ops_of({"assignments/a.md": a}, {"assignments/b.md": a}) == []
    stripped = asg(title="Renamed")
    (op,) = ops_of({"assignments/a.md": a}, {"assignments/a.md": stripped})
    assert op.action is Action.UPDATE and op.before is a
    assert set(op.changes) == {"title"}


def test_create_with_new_group_emits_group_first_and_existing_group_none() -> None:
    old = {"assignments/a.md": asg(canvas_id=1, group="Homework")}
    new = {
        **old,
        "assignments/b.md": asg(title="B", group="Labs"),
        "assignments/c.md": asg(title="C", group="Homework"),
        "assignments/d.md": asg(title="D", group="Labs"),
    }
    ops = ops_of(old, new)
    assert [(o.action, type(o.after).__name__) for o in ops[:2]] == [
        (Action.CREATE, "AssignmentGroup"),
        (Action.CREATE, "Assignment"),
    ]
    groups = [o for o in ops if isinstance(o.after, AssignmentGroup)]
    assert [g.after.name for g in groups if isinstance(g.after, AssignmentGroup)] == ["Labs"]
    assert not any(o.action is Action.DELETE for o in ops)


def test_moving_to_a_new_group_creates_it() -> None:
    old = {"assignments/a.md": asg(canvas_id=1, group="Homework")}
    new = {"assignments/a.md": asg(canvas_id=1, group="Labs")}
    ops = ops_of(old, new)
    assert [type(o.after).__name__ for o in ops] == ["AssignmentGroup", "Assignment"]


def test_grading_risks_are_static_and_other_changes_have_none() -> None:
    old = asg(canvas_id=1)
    (pts,) = ops_of(
        {"assignments/a.md": old}, {"assignments/a.md": old.model_copy(update={"points": 20.0})}
    )
    (gt,) = ops_of(
        {"assignments/a.md": old},
        {"assignments/a.md": old.model_copy(update={"grading_type": "pass_fail"})},
    )
    (title,) = ops_of(
        {"assignments/a.md": old}, {"assignments/a.md": old.model_copy(update={"title": "X"})}
    )
    for op in (pts, gt):
        assert [r.kind for r in op.risks] == [RiskKind.GRADING_CHANGE]
        assert op.risks[0].needs_confirm == CONFIRM_GRADING
    assert title.risks == ()


def test_deleting_every_assignment_emits_only_assignment_deletes() -> None:
    old = {f"assignments/{n}.md": asg(canvas_id=i, group="G") for i, n in enumerate("abc", 1)}
    ops = ops_of(old, {})
    assert {o.action for o in ops} == {Action.DELETE}
    assert not any(type(o.before).__name__ == "AssignmentGroup" for o in ops)


# ---- harness over FakeCanvas ----------------------------------------------------------


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.fake = FakeCanvas(course_id=101)
        self.client = self.fake.client()
        self.tmp = tmp_path
        self.guard = SubmissionGuard(self.client, CFG)

    def ctx(self) -> ApplyContext:
        return ApplyContext(
            config=CFG, tz=CFG.tz, roster=RosterCache(), ids={}, guard=self.guard,
            blobs=BlobStore(self.tmp),
        )  # fmt: skip

    def fetch(self, previous: Course | None = None, bodies: Bodies | None = None) -> Course:
        ctx = FetchContext(
            config=CFG, tz=CFG.tz, bodies=bodies or Bodies(), previous=previous,
            roster=RosterCache(), ignore=IgnoreRules(lines=[]), blob_store=BlobStore(self.tmp),
        )  # fmt: skip
        objs = remote.read(self.client, ctx)
        return make_course({p: a for p, a in objs.items() if isinstance(a, Assignment)})

    def push(
        self, base: Course, target: Course, confirmed: set[str] | None = None
    ) -> tuple[Plan, ApplyResult, ApplyContext]:
        plan = plan_of(base, target)
        ctx = self.ctx()
        return plan, apply(plan, self.client, ctx, confirmed=confirmed or set()), ctx

    def writes(self, method: str | None = None) -> list[Any]:
        return [r for r in self.fake.writes() if method is None or r.method == method]

    def assignment(self, aid: int) -> dict[str, Any]:
        return self.fake.state("assignments")["assignments"][aid]

    def with_submissions(self, name: str, *, graded: bool = True, **fields: Any) -> int:
        a = add_assignment(self.fake, name, points_possible=10.0, **fields)
        self.fake.seed_submissions(
            a["id"],
            [
                {"user_id": 7, "score": 8.0, "grade": "8", "workflow_state": "graded"}
                if graded
                else {"user_id": 7, "workflow_state": "submitted"},
                {"user_id": 8, "workflow_state": "submitted"},
            ],
        )
        return a["id"]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def without(course: Course, *paths: str) -> Course:
    return dataclasses.replace(
        course, objects={k: v for k, v in course.objects.items() if k.path not in paths}
    )


def edit(course: Course, path: str, **fields: Any) -> Course:
    key = ObjectKey(Kind.ASSIGNMENT, path)
    objs: dict[ObjectKey, CanvasObject] = dict(course.objects)
    objs[key] = objs[key].model_copy(update=fields)
    return dataclasses.replace(course, objects=objs)


# ---- deletion ---------------------------------------------------------------------


def test_deleting_everything_locally_destroys_no_submission(env: Env) -> None:
    graded = env.with_submissions("Graded", graded=True)
    submitted = env.with_submissions("Submitted only", graded=False, has_overrides=False)
    empty = add_assignment(env.fake, "Empty")["id"]
    base = env.fetch()
    snapshot = env.fake.snapshot_submissions()

    with pytest.raises(GuardBlocked, match="submissions"):
        env.push(base, make_course({}))

    # The pre-flight pass refuses the whole push: nothing was written, nothing deleted.
    assert env.writes() == []
    assert [r for r in env.fake.log if r.method == "DELETE"] == []
    assert {graded, submitted, empty} <= set(env.fake.state("assignments")["assignments"])
    env.fake.assert_submissions_intact(snapshot)


def test_only_submission_free_assignments_are_deleted(env: Env) -> None:
    env.with_submissions("Keep me")
    empty = add_assignment(env.fake, "Empty")["id"]
    base = env.fetch()
    snapshot = env.fake.snapshot_submissions()
    keep = next(p for p, a in base.objects.items() if a.canvas_id != empty).path
    plan, result, _ = env.push(
        base, without(base, *[k.path for k in base.objects if k.path != keep])
    )
    assert result.error is None and len(plan.ops) == 1
    assert [r.path for r in env.writes("DELETE")] == [f"/courses/101/assignments/{empty}"]
    assert empty not in env.fake.state("assignments")["assignments"]
    env.fake.assert_submissions_intact(snapshot)


def test_direct_delete_writer_cannot_bypass_the_guard(env: Env) -> None:
    aid = env.with_submissions("Graded")
    obj = Assignment(canvas_id=aid, title="Graded")
    snapshot = env.fake.snapshot_submissions()
    with pytest.raises(GuardBlocked):
        remote.delete(env.client, obj, env.ctx())
    assert [r for r in env.fake.log if r.method == "DELETE"] == []
    assert aid in env.fake.state("assignments")["assignments"]
    env.fake.assert_submissions_intact(snapshot)


def test_stale_snapshot_does_not_fool_the_guard(env: Env) -> None:
    """The plan was made when the assignment had no submissions; one arrives before apply."""
    a = add_assignment(env.fake, "Late arrival")
    base = env.fetch()
    plan = plan_of(base, make_course({}))
    env.fake.seed_submissions(a["id"], [{"user_id": 3, "workflow_state": "submitted"}])
    snapshot = env.fake.snapshot_submissions()
    with pytest.raises(GuardBlocked):
        apply(plan, env.client, env.ctx(), confirmed=set())
    assert [r for r in env.fake.log if r.method == "DELETE"] == []
    env.fake.assert_submissions_intact(snapshot)


def test_assignment_groups_are_never_deleted(env: Env) -> None:
    g = add_assignment_group(env.fake, "Homework")
    add_assignment(env.fake, "Empty", assignment_group_id=g["id"])
    base = env.fetch()
    env.push(base, make_course({}))
    assert [r for r in env.fake.log if "assignment_groups" in r.path and r.method != "GET"] == []
    assert g["id"] in env.fake.state("assignments")["groups"]
    with pytest.raises(GuardBlocked):
        remote.delete(env.client, AssignmentGroup(name="Homework"), env.ctx())


# ---- grading change, late policy, unpublish -----------------------------------------


def test_points_change_needs_confirmation_and_keeps_submissions(env: Env) -> None:
    aid = env.with_submissions("Graded")
    base = env.fetch()
    path = next(iter(base.objects)).path
    target = edit(base, path, points=100.0)
    snapshot = env.fake.snapshot_submissions()

    plan = plan_of(base, target)
    assert plan.ops[0].risks[0].kind is RiskKind.GRADING_CHANGE
    with pytest.raises(GuardBlocked, match="confirm-grading-change"):
        apply(plan, env.client, env.ctx(), confirmed=set())
    assert env.writes() == [] and env.assignment(aid)["points_possible"] == 10.0

    result = apply(plan, env.client, env.ctx(), confirmed={CONFIRM_GRADING})
    assert result.error is None
    assert env.assignment(aid)["points_possible"] == 100.0
    env.fake.assert_submissions_intact(snapshot)


def test_grading_type_change_needs_confirmation(env: Env) -> None:
    aid = env.with_submissions("Graded")
    base = env.fetch()
    target = edit(base, next(iter(base.objects)).path, grading_type="pass_fail")
    with pytest.raises(GuardBlocked):
        env.push(base, target)
    assert env.assignment(aid)["grading_type"] == "points"
    _, result, _ = env.push(base, target, {f"{CONFIRM_GRADING} {next(iter(base.objects)).path}"})
    assert result.error is None and env.assignment(aid)["grading_type"] == "pass_fail"


def test_late_policy_makes_due_date_change_need_confirmation(env: Env) -> None:
    aid = env.with_submissions("Graded", due_at="2026-09-23T06:59:00Z")
    base = env.fetch()
    path = next(iter(base.objects)).path
    obj = base.objects[ObjectKey(Kind.ASSIGNMENT, path)]
    assert isinstance(obj, Assignment) and obj.due is not None
    target = edit(base, path, due=obj.due.replace(day=30))
    set_late_policy(env.fake, True)
    snapshot = env.fake.snapshot_submissions()

    plan = plan_of(base, target)
    assert plan.ops[0].risks == ()  # the differ cannot see the late policy
    with pytest.raises(GuardBlocked, match="late policy"):
        apply(plan, env.client, env.ctx(), confirmed=set())
    assert env.writes() == [] and env.assignment(aid)["due_at"] == "2026-09-23T06:59:00Z"

    result = apply(plan, env.client, env.ctx(), confirmed={CONFIRM_GRADING})
    assert result.error is None and env.assignment(aid)["due_at"] == "2026-09-30T06:59:00Z"
    env.fake.assert_submissions_intact(snapshot)


def test_due_date_change_without_late_policy_needs_no_confirmation(env: Env) -> None:
    aid = env.with_submissions("Graded", due_at="2026-09-23T06:59:00Z")
    base = env.fetch()
    path = next(iter(base.objects)).path
    obj = base.objects[ObjectKey(Kind.ASSIGNMENT, path)]
    assert isinstance(obj, Assignment) and obj.due is not None
    _, result, _ = env.push(base, edit(base, path, due=obj.due.replace(day=1)))
    assert result.error is None and env.assignment(aid)["due_at"] == "2026-09-01T06:59:00Z"


def test_unpublishing_an_assignment_with_submissions_is_refused(env: Env) -> None:
    aid = env.with_submissions("Graded")
    base = env.fetch()
    target = edit(base, next(iter(base.objects)).path, published=False)
    snapshot = env.fake.snapshot_submissions()
    with pytest.raises(GuardBlocked, match="unpublish"):
        env.push(base, target)
    assert env.writes() == [] and env.assignment(aid)["published"] is True
    env.fake.assert_submissions_intact(snapshot)


# ---- writer behaviour --------------------------------------------------------------


def test_update_sends_only_changed_fields_and_never_assignment_overrides(env: Env) -> None:
    a = add_assignment(env.fake, "HW", points_possible=10.0, due_at="2026-09-23T06:59:00Z")
    sec = env.fake.add_section("BA2")
    add_override(env.fake, a["id"], course_section_id=sec["id"], due_at="2026-09-24T06:59:00Z")
    add_override(env.fake, a["id"], noop_id=3)  # mastery path: not mirrored
    base = env.fetch()
    path = next(iter(base.objects)).path
    # Only the title changes; nothing else (and no override) may be sent.
    _, result, _ = env.push(base, edit(base, path, title="HW 1"))
    assert result.error is None
    (put,) = env.writes("PUT")
    assert put.body == {"assignment": {"name": "HW 1"}}
    # A cleared date is an explicit null.
    base = env.fetch(previous=base)
    _, result, _ = env.push(base, edit(base, path, due=None))
    assert result.error is None
    assert env.writes("PUT")[-1].body == {"assignment": {"due_at": None}}
    assert env.assignment(a["id"])["due_at"] is None
    assert all("assignment_overrides" not in str(w.body) for w in env.writes())


def test_override_change_plus_new_group_writes_nothing(env: Env) -> None:
    add_assignment(env.fake, "HW", points_possible=10.0)
    env.fake.add_section("BA2")
    base = env.fetch()
    path = next(iter(base.objects)).path
    target = edit(base, path, title="Changed", overrides=[Override(section="BA2")])
    new = Assignment(title="New", group="Brand new group")
    target = dataclasses.replace(
        target, objects={**target.objects, ObjectKey(Kind.ASSIGNMENT, "assignments/new.md"): new}
    )
    with pytest.raises(ValidationError, match="override changes"):
        env.push(base, target)
    assert env.writes() == []


def test_writer_refuses_override_change_as_second_line_of_defence(env: Env) -> None:
    a = add_assignment(env.fake, "HW")
    before = Assignment(canvas_id=a["id"], title="HW")
    after = before.model_copy(update={"overrides": [Override(section="BA2")]})
    with pytest.raises(CanvasSyncError):
        remote.update(env.client, before, after, env.ctx())
    assert env.writes() == []


def test_create_uses_created_group_and_defaults(env: Env) -> None:
    base = env.fetch()
    new = Assignment(
        title="New HW", points=5.0, group="Labs", body="Do it.", submission_types=["online_upload"]
    )
    target = make_course({"assignments/new-hw.md": new})
    plan, result, ctx = env.push(base, target)
    assert result.error is None and [type(o.after).__name__ for o in plan.ops] == [
        "AssignmentGroup",
        "Assignment",
    ]
    groups = env.fake.state("assignments")["groups"]
    (gid,) = [g for g, row in groups.items() if row["name"] == "Labs"]
    (aid,) = env.fake.state("assignments")["assignments"]
    row = env.assignment(aid)
    assert row["assignment_group_id"] == gid and row["points_possible"] == 5.0
    assert row["published"] is False  # config default
    assert "Do it." in row["description"]
    assert result.new_ids[ObjectKey(Kind.ASSIGNMENT, "assignments/new-hw.md")] == aid


def test_existing_empty_group_is_reused_not_duplicated(env: Env) -> None:
    g = add_assignment_group(env.fake, "Labs")
    base = env.fetch()
    target = make_course({"assignments/x.md": Assignment(title="X", group="Labs")})
    _, result, _ = env.push(base, target)
    assert result.error is None
    assert [r for r in env.writes("POST") if "assignment_groups" in r.path] == []
    (aid,) = env.fake.state("assignments")["assignments"]
    assert env.assignment(aid)["assignment_group_id"] == g["id"]


def test_duplicate_group_names_are_refused_not_guessed(env: Env) -> None:
    add_assignment_group(env.fake, "Labs")
    add_assignment_group(env.fake, "Labs")
    a = add_assignment(env.fake, "HW")
    base = env.fetch()
    path = next(iter(base.objects)).path
    # Moving into the duplicated name is ambiguous.
    target = edit(base, path, group="Labs")
    plan = plan_of(base, target)
    result = apply(plan, env.client, env.ctx(), confirmed=set())
    assert isinstance(result.error, ValidationError) and "more than once" in str(result.error)
    assert env.assignment(a["id"])["assignment_group_id"] is None
    # An unchanged group on an update is not looked up at all (resolved by id on Canvas).
    assert env.writes("POST") == []


# ---- push, then fetch is a no-op --------------------------------------------------


def test_push_then_fetch_is_a_no_op(env: Env) -> None:
    g = add_assignment_group(env.fake, "Homework")
    keep = add_assignment(
        env.fake, "Old", points_possible=10.0, assignment_group_id=g["id"],
        submission_types=["online_upload"], description="<p>Old body</p>",
        due_at="2026-09-23T06:59:00Z",
    )  # fmt: skip
    env.fake.seed_submissions(
        keep["id"], [{"user_id": 7, "score": 9.0, "workflow_state": "graded"}]
    )
    base = env.fetch()
    path = next(iter(base.objects)).path
    snapshot = env.fake.snapshot_submissions()

    updated = edit(base, path, title="Old renamed", points=12.0, group="Labs", body="New body")
    new = Assignment(
        title="Brand New", points=3.0, group="Labs", submission_types=["online_upload"],
        body="Fresh text.", allowed_extensions=["pdf"], published=True,
    )  # fmt: skip
    target = dataclasses.replace(
        updated,
        objects={**updated.objects, ObjectKey(Kind.ASSIGNMENT, "assignments/brand-new.md"): new},
    )
    _, result, ctx = env.push(base, target, {CONFIRM_GRADING})
    assert result.error is None, result.error

    # What the tool's push writes back: new ids in the tree and the body record.
    new_id = ctx.ids[ObjectKey(Kind.ASSIGNMENT, "assignments/brand-new.md")]
    objs = {k.path: v for k, v in target.objects.items() if isinstance(v, Assignment)}
    objs["assignments/brand-new.md"] = new.model_copy(update={"canvas_id": new_id})
    local = make_course(objs)
    bodies = Bodies()
    for p, a in objs.items():
        html = env.assignment(int(a.canvas_id or 0))["description"]
        bodies.record(p, a.body, html)

    remote_after = env.fetch(previous=local, bodies=bodies)
    assert plan_empty(local, remote_after)

    def dumped(c: Course) -> dict[str, bytes]:
        return ASSIGNMENT_SPEC.dump({k.path: v for k, v in c.objects.items()}, CFG)

    assert dumped(local) == dumped(remote_after)
    env.fake.assert_submissions_intact(snapshot)


def plan_empty(local: Course, remote_course: Course) -> bool:
    plan = plan_of(remote_course, local)
    assert plan.is_empty(), [(o.key.path, o.action, o.changes) for o in plan.ops]
    return True
