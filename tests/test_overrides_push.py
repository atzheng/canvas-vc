"""Assignment override push: differ, writer and guard, end to end against FakeCanvas.

Students are invented. Every test that touches an assignment with submissions ends with
``assert_submissions_intact``; a mastery-path (``noop_id``) override must survive every push.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from canvas_sync.apply import ApplyResult, apply
from canvas_sync.errors import GuardBlocked, ValidationError
from canvas_sync.ignore import IgnoreRules
from canvas_sync.model.assignment import ASSIGNMENT_SPEC, Assignment
from canvas_sync.model.course import Course
from canvas_sync.model.overrides import Override
from canvas_sync.model.roster import RosterCache
from canvas_sync.plan.assignments import CONFIRM_ACCESS, OverrideChange, diff_overrides
from canvas_sync.plan.core import Plan, RiskKind
from canvas_sync.remote import assignments as remote
from canvas_sync.remote.context import ApplyContext, FetchContext
from canvas_sync.remote.guard import SubmissionGuard
from canvas_sync.render.bodies import Bodies
from canvas_sync.snapshot import BlobStore
from tests.fake_canvas.assignments import add_assignment, add_override
from tests.fake_canvas.core import FakeCanvas
from tests.test_assignments_write import CFG, asg, edit, make_course, ops_of, plan_of

DUE = "2026-09-23T06:59:00Z"


class Env:
    """FakeCanvas with sections BA1/BA2, students ann/bob/cy (BA1, BA1, BA2) and a roster cache."""

    def __init__(self, tmp: Path, *, roster: bool = True) -> None:
        self.fake = FakeCanvas(course_id=101)
        self.client = self.fake.client()
        self.tmp = tmp
        self.ba1 = self.fake.add_section("BA1")["id"]
        self.ba2 = self.fake.add_section("BA2")["id"]
        self.users = {
            "ann": self.fake.add_user("Ann Test", section_id=self.ba1, login_id="ann")["id"],
            "bob": self.fake.add_user("Bob Test", section_id=self.ba1, login_id="bob")["id"],
            "cy": self.fake.add_user("Cy Test", section_id=self.ba2, login_id="cy")["id"],
        }
        cat = self.fake.add_group_category("Teams")["id"]
        self.team = self.fake.add_group(cat, "Team 1", [self.users["ann"]])["id"]
        self.cache = (
            RosterCache(
                students=dict(self.users),
                sections={"BA1": self.ba1, "BA2": self.ba2},
                groups={"Team 1": self.team},
            )
            if roster
            else RosterCache()
        )
        self.guard = SubmissionGuard(self.client, CFG)

    def ctx(self) -> ApplyContext:
        return ApplyContext(
            config=CFG, tz=CFG.tz, roster=self.cache, ids={}, guard=self.guard,
            blobs=BlobStore(self.tmp),
        )  # fmt: skip

    def fetch(self, previous: Course | None = None) -> Course:
        ctx = FetchContext(
            config=CFG, tz=CFG.tz, bodies=Bodies(), previous=previous, roster=self.cache,
            ignore=IgnoreRules(lines=[]), blob_store=BlobStore(self.tmp),
        )  # fmt: skip
        objs = remote.read(self.client, ctx)
        return make_course({p: a for p, a in objs.items() if isinstance(a, Assignment)})

    def push(
        self, base: Course, target: Course, confirmed: set[str] | None = None
    ) -> tuple[Plan, ApplyResult]:
        plan = plan_of(base, target)
        return plan, apply(plan, self.client, self.ctx(), confirmed=confirmed or set())

    def writes(self) -> list[Any]:
        return [r for r in self.fake.writes() if r.method != "GET"]

    def overrides(self, aid: int) -> list[dict[str, Any]]:
        return self.fake.state("assignments")["overrides"].get(aid, [])

    def assignment(self, aid: int) -> dict[str, Any]:
        return self.fake.state("assignments")["assignments"][aid]

    def submit(self, aid: int, *who: str) -> dict[int, list[dict[str, Any]]]:
        self.fake.seed_submissions(
            aid, [{"user_id": self.users[w], "workflow_state": "submitted"} for w in who]
        )
        return self.fake.snapshot_submissions()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def only_path(course: Course) -> str:
    return next(iter(course.objects)).path


def current(course: Course) -> Assignment:
    (obj,) = course.objects.values()
    assert isinstance(obj, Assignment)
    return obj


def base_assignment(env: Env, **fields: Any) -> dict[str, Any]:
    return add_assignment(env.fake, "HW", points_possible=10.0, due_at=DUE, **fields)


# ---- differ (pure) ---------------------------------------------------------------------


def kinds(changes: list[OverrideChange]) -> list[str]:
    return [c.kind for c in changes]


def test_diff_overrides_is_minimal_and_keyed_by_target() -> None:
    sec = Override(section="BA1")
    grp = Override(group="Team 1")
    assert diff_overrides([sec, grp], [grp, sec]) == []  # order is not significant
    moved = Override(section="BA2")
    assert kinds(diff_overrides([sec, grp], [moved, grp])) == ["add", "remove"]
    dated = Override(group="Team 1", due=None, title="ignored")
    assert diff_overrides([grp], [dated]) == []  # a group/section override has no custom title


def test_diff_overrides_student_sets_match_by_title_when_membership_changes() -> None:
    old = Override(students=["ann", "bob"], title="Accommodations")
    grown = Override(students=["ann", "bob", "cy"], title="Accommodations")
    (c,) = diff_overrides([old], [grown])
    assert (c.kind, c.before, c.after) == ("update", old, grown)
    # No title: paired by overlap into one update, so "ann" is never without an override.
    untitled = diff_overrides([Override(students=["ann"])], [Override(students=["ann", "bob"])])
    assert kinds(untitled) == ["update"]
    # No student in common: add first, then remove.
    apart = diff_overrides([Override(students=["ann"])], [Override(students=["cy"])])
    assert kinds(apart) == ["add", "remove"]


def test_diff_overrides_pairs_by_largest_overlap_deterministically() -> None:
    old = [Override(students=["ann", "bob", "cy"]), Override(students=["dee"])]
    new = [Override(students=["ann", "bob"]), Override(students=["dee", "cy"])]
    out = diff_overrides(old, new)
    assert [(c.before.students, c.after.students) for c in out if c.before and c.after] == [
        (["ann", "bob", "cy"], ["ann", "bob"]),  # shrinking set first
        (["dee"], ["cy", "dee"]),
    ]
    assert diff_overrides(list(reversed(old)), list(reversed(new))) == out


def test_diff_overrides_orders_shrinking_updates_adds_updates_removes() -> None:
    a, b, c = Override(section="BA1"), Override(section="BA2"), Override(group="Team 1")
    later = Override(section="BA1", due=asg(due="2026-10-01 12:00").due)
    assert kinds(diff_overrides([a, b], [later, c])) == ["add", "update", "remove"]


def test_static_access_risk_only_when_an_audience_drops_out() -> None:
    old = asg(canvas_id=1, overrides=[Override(section="BA1"), Override(students=["ann", "bob"])])
    path = "assignments/a.md"

    def risks(**fields: Any) -> list[RiskKind]:
        (op,) = ops_of({path: old}, {path: old.model_copy(update=fields)})
        return [r.kind for r in op.risks]

    assert risks(overrides=[*old.overrides, Override(section="BA2")]) == []
    assert risks(overrides=[Override(students=["ann", "bob"])]) == [RiskKind.ACCESS_REMOVED]
    assert risks(overrides=[Override(section="BA1"), Override(students=["ann"])]) == [
        RiskKind.ACCESS_REMOVED
    ]
    assert risks(assign_to_overrides_only=True, due=None) == [RiskKind.ACCESS_REMOVED]


# ---- writer: minimal calls ---------------------------------------------------------------


def test_add_section_override_is_one_post_and_leaves_others_alone(env: Env) -> None:
    a = base_assignment(env)
    keep = add_override(env.fake, a["id"], course_section_id=env.ba1, due_at="2026-09-24T06:59:00Z")
    base = env.fetch()
    path = only_path(base)
    ba2 = Override(section="BA2", due=current(base).due)
    target = edit(base, path, overrides=[*current(base).overrides, ba2])
    plan, result = env.push(base, target)
    assert result.error is None and plan.ops[0].risks == ()
    (post,) = env.writes()
    assert post.method == "POST" and post.path.endswith(f"/assignments/{a['id']}/overrides")
    assert post.body["assignment_override"]["course_section_id"] == env.ba2
    assert "assignment_overrides" not in str(post.body)
    assert next(o for o in env.overrides(a["id"]) if o["id"] == keep["id"]) == keep
    assert len(env.overrides(a["id"])) == 2


def test_date_change_is_one_put_with_explicit_null_for_cleared_dates(env: Env) -> None:
    a = base_assignment(env)
    ov = add_override(
        env.fake, a["id"], course_section_id=env.ba1, due_at="2026-09-24T06:59:00Z",
        lock_at="2026-09-30T06:59:00Z",
    )  # fmt: skip
    base = env.fetch()
    new = current(base).overrides[0].model_copy(update={"lock": None})
    _, result = env.push(base, edit(base, only_path(base), overrides=[new]))
    assert result.error is None
    (put,) = env.writes()
    assert put.method == "PUT" and put.path.endswith(f"/overrides/{ov['id']}")
    body = put.body["assignment_override"]
    assert body["lock_at"] is None and body["due_at"] == "2026-09-24T06:59:00Z"
    assert "course_section_id" not in body
    assert env.overrides(a["id"])[0]["lock_at"] is None


def test_adding_a_student_rewrites_the_whole_student_list(env: Env) -> None:
    a = base_assignment(env)
    ov = add_override(
        env.fake, a["id"], student_ids=[env.users["ann"]], title="Accommodations",
        due_at="2026-09-26T06:59:00Z",
    )  # fmt: skip
    base = env.fetch()
    cur = current(base)
    assert cur.overrides[0].students == ["ann"] and cur.overrides[0].title == "Accommodations"
    grown = cur.overrides[0].model_copy(update={"students": ["ann", "bob"]})
    plan, result = env.push(base, edit(base, only_path(base), overrides=[grown]))
    assert result.error is None and plan.ops[0].risks == ()
    (put,) = env.writes()
    assert put.path.endswith(f"/overrides/{ov['id']}")  # matched by title, not delete + create
    sent = put.body["assignment_override"]
    assert sorted(sent["student_ids"]) == sorted([env.users["ann"], env.users["bob"]])
    assert sent["title"] == "Accommodations"


def test_untitled_student_set_growth_is_one_put(env: Env) -> None:
    a = base_assignment(env)
    ov = add_override(env.fake, a["id"], student_ids=[env.users["ann"]], title="1 student")
    base = env.fetch()
    grown = Override(students=["ann", "bob"])
    _, result = env.push(base, edit(base, only_path(base), overrides=[grown]))
    assert result.error is None
    (put,) = env.writes()
    assert put.method == "PUT" and put.path.endswith(f"/overrides/{ov['id']}")
    assert "title" in put.body["assignment_override"]  # cleared explicitly, not an auto-title
    assert put.body["assignment_override"]["title"] is None
    (row,) = env.overrides(a["id"])
    assert sorted(row["student_ids"]) == sorted([env.users["ann"], env.users["bob"]])


def test_disjoint_student_sets_add_before_remove_and_failed_post_keeps_coverage(env: Env) -> None:
    a = base_assignment(env)
    add_override(env.fake, a["id"], student_ids=[env.users["ann"]], title="1 student")
    other = add_override(env.fake, a["id"], course_section_id=env.ba2)
    base = env.fetch()
    path = only_path(base)
    target = edit(base, path, overrides=[Override(section="BA2"), Override(students=["cy"])])
    del other
    _, result = env.push(base, target, {CONFIRM_ACCESS})
    assert result.error is None
    assert [w.method for w in env.writes()] == ["POST", "DELETE"]

    # A failing POST leaves the old override (and its student) untouched.
    env2 = Env(env.tmp / "again")
    b = base_assignment(env2)
    old = add_override(env2.fake, b["id"], student_ids=[env2.users["ann"]], title="1 student")
    base2 = env2.fetch()
    env2.fake.fail(method="POST", path=r".*/overrides", status=500)
    target2 = edit(base2, only_path(base2), overrides=[Override(students=["cy"])])
    _, failed = env2.push(base2, target2, {CONFIRM_ACCESS})
    assert failed.error is not None
    assert [o["id"] for o in env2.overrides(b["id"])] == [old["id"]]


def test_moving_a_student_between_sets_never_double_covers(env: Env) -> None:
    a = base_assignment(env)
    add_override(env.fake, a["id"], student_ids=[env.users["ann"], env.users["bob"]], title="A")
    add_override(env.fake, a["id"], student_ids=[env.users["cy"]], title="B")
    base = env.fetch()
    by_title = {o.title: o for o in current(base).overrides}
    moved = [
        by_title["A"].model_copy(update={"students": ["ann"]}),
        by_title["B"].model_copy(update={"students": ["bob", "cy"]}),
    ]
    _, result = env.push(base, edit(base, only_path(base), overrides=moved))
    assert result.error is None, result.error  # the fake rejects a student in two overrides
    assert [w.method for w in env.writes()] == ["PUT", "PUT"]


def test_ambiguous_live_overrides_are_refused_before_any_write(env: Env) -> None:
    a = base_assignment(env)
    add_override(env.fake, a["id"], course_section_id=env.ba1)
    base = env.fetch()
    add_override(env.fake, a["id"], course_section_id=env.ba1)  # a second one for the same section
    new = current(base).overrides[0].model_copy(update={"due": current(base).due})
    with pytest.raises(ValidationError, match=r"more than one Canvas override targets the same"):
        env.push(base, edit(base, only_path(base), overrides=[new]))
    assert env.writes() == []


def test_cleared_title_and_auto_titles_round_trip(env: Env) -> None:
    a = base_assignment(env)
    add_override(env.fake, a["id"], student_ids=[env.users["ann"]], title="Acc")
    base = env.fetch()
    cleared = current(base).overrides[0].model_copy(update={"title": None})
    local = edit(base, only_path(base), overrides=[cleared])
    _, result = env.push(base, local)
    assert result.error is None
    (put,) = env.writes()
    assert put.body["assignment_override"]["title"] is None
    assert env.overrides(a["id"])[0]["title"] == "1 students"  # Canvas's own title
    assert plan_of(env.fetch(previous=local), local).is_empty()
    # An auto-title typed into the file is never sent back as a custom title.
    auto = Override(students=["ann", "bob"], title="2 students")
    _, result = env.push(env.fetch(previous=local), edit(local, only_path(base), overrides=[auto]))
    assert result.error is None
    assert env.writes()[-1].body["assignment_override"]["title"] is None


def test_group_override_add_and_remove(env: Env) -> None:
    a = base_assignment(env)
    base = env.fetch()
    path = only_path(base)
    added = edit(base, path, overrides=[Override(group="Team 1", due=current(base).due)])
    _, result = env.push(base, added)
    assert result.error is None
    assert env.overrides(a["id"])[0]["group_id"] == env.team
    base2 = env.fetch(previous=added)
    _, result = env.push(base2, edit(base2, path, overrides=[]), {CONFIRM_ACCESS})
    assert result.error is None and env.overrides(a["id"]) == []


# ---- access removal ----------------------------------------------------------------------


def test_removing_a_student_who_submitted_needs_confirmation(env: Env) -> None:
    a = base_assignment(env)
    add_override(env.fake, a["id"], student_ids=[env.users["ann"], env.users["bob"]], title="Acc")
    snapshot = env.submit(a["id"], "ann")
    base = env.fetch()
    path = only_path(base)
    shrunk = current(base).overrides[0].model_copy(update={"students": ["bob"]})
    target = edit(base, path, overrides=[shrunk])

    with pytest.raises(GuardBlocked, match=r"access"):
        env.push(base, target)
    assert env.writes() == []
    both = sorted([env.users["ann"], env.users["bob"]])
    assert sorted(env.overrides(a["id"])[0]["student_ids"]) == both

    _, result = env.push(base, target, {CONFIRM_ACCESS})
    assert result.error is None
    assert env.overrides(a["id"])[0]["student_ids"] == [env.users["bob"]]
    env.fake.assert_submissions_intact(snapshot)


def test_guard_counts_submitters_losing_access_without_naming_them(env: Env) -> None:
    a = base_assignment(env)
    add_override(env.fake, a["id"], course_section_id=env.ba1)
    add_override(env.fake, a["id"], course_section_id=env.ba2)
    snapshot = env.submit(a["id"], "ann", "cy")
    base = env.fetch()
    cur = current(base)
    keep = [o for o in cur.overrides if o.section == "BA2"]
    target = edit(base, only_path(base), overrides=keep)
    op = plan_of(base, target).ops[0]
    risks = env.guard.check(op)
    assert [(r.kind, r.message.split(": ", 1)[1]) for r in risks] == [
        (RiskKind.ACCESS_REMOVED, "1 student(s) with submissions lose access")
    ]
    with pytest.raises(GuardBlocked):
        env.push(base, target)
    _, result = env.push(base, target, {CONFIRM_ACCESS})
    assert result.error is None
    assert [o["course_section_id"] for o in env.overrides(a["id"])] == [env.ba2]
    env.fake.assert_submissions_intact(snapshot)


def test_overrides_only_turned_on_writes_overrides_first_then_the_flag(env: Env) -> None:
    a = base_assignment(env)
    snapshot = env.submit(a["id"], "cy")  # a BA2 student loses "everyone else"
    base = env.fetch()
    path = only_path(base)
    target = edit(
        base, path, assign_to_overrides_only=True, due=None, overrides=[Override(section="BA1")]
    )
    with pytest.raises(GuardBlocked, match="access"):
        env.push(base, target)
    assert env.writes() == []
    _, result = env.push(base, target, {CONFIRM_ACCESS})
    assert result.error is None
    assert [w.method for w in env.writes()] == ["POST", "PUT"]
    assert env.assignment(a["id"])["only_visible_to_overrides"] is True
    assert env.assignment(a["id"])["due_at"] is None
    env.fake.assert_submissions_intact(snapshot)


# ---- unknown refs, mastery paths ---------------------------------------------------------


def test_unknown_students_are_preserved_and_not_flagged(tmp_path: Path) -> None:
    env = Env(tmp_path, roster=False)  # a cache that knows nobody: every ref is unknown:<id>
    a = base_assignment(env)
    ghost = 999_001
    add_override(env.fake, a["id"], student_ids=[env.users["ann"], ghost], title="Acc")
    snapshot = env.submit(a["id"], "ann")
    base = env.fetch()
    cur = current(base)
    assert cur.overrides[0].students == sorted([f"unknown:{env.users['ann']}", f"unknown:{ghost}"])
    # A date change on the override keeps both students and needs no confirmation.
    moved = cur.overrides[0].model_copy(update={"due": cur.due})
    _, result = env.push(base, edit(base, only_path(base), overrides=[moved]))
    assert result.error is None
    (put,) = env.writes()
    sent = put.body["assignment_override"]
    assert sorted(sent["student_ids"]) == sorted([env.users["ann"], ghost])
    env.fake.assert_submissions_intact(snapshot)


def test_unknown_section_refuses_before_any_write(tmp_path: Path) -> None:
    env = Env(tmp_path, roster=False)
    base_assignment(env)
    base = env.fetch()
    target = edit(base, only_path(base), title="Renamed", overrides=[Override(section="BA1")])
    with pytest.raises(ValidationError, match="unknown section 'BA1'"):
        env.push(base, target)
    assert env.writes() == []
    ghost = edit(base, only_path(base), overrides=[Override(students=["nobody"])])
    with pytest.raises(ValidationError) as exc:
        env.push(base, ghost)
    assert "nobody" not in str(exc.value) and env.writes() == []


def test_mastery_path_override_survives_every_push(env: Env) -> None:
    a = base_assignment(env)
    noop = add_override(env.fake, a["id"], noop_id=3)
    add_override(env.fake, a["id"], course_section_id=env.ba1)
    base = env.fetch()
    path = only_path(base)
    assert len(current(base).overrides) == 1  # the noop override is not mirrored
    cleared = edit(base, path, overrides=[], title="Renamed")
    _, result = env.push(base, cleared, {CONFIRM_ACCESS})
    assert result.error is None
    assert env.overrides(a["id"]) == [noop]
    base2 = env.fetch(previous=cleared)
    refilled = edit(base2, path, overrides=[Override(section="BA2")])
    _, result = env.push(base2, refilled)
    assert result.error is None and noop in env.overrides(a["id"])
    assert not [w for w in env.writes() if w.path.endswith(f"/{noop['id']}")]


# ---- create, round trip ------------------------------------------------------------------


def test_create_with_overrides_posts_assignment_then_overrides(env: Env) -> None:
    base = env.fetch()
    new = Assignment(
        title="New", points=5.0, published=True, assign_to_overrides_only=True,
        overrides=[Override(section="BA2"), Override(students=["bob"], title="Acc")],
    )  # fmt: skip
    plan, result = env.push(base, make_course({"assignments/new.md": new}))
    assert result.error is None and plan.ops[0].risks == ()
    assert [w.method for w in env.writes()] == ["POST", "POST", "POST"]
    first = env.writes()[0]
    assert "assignment_overrides" not in str(first.body)
    assert first.body["assignment"]["only_visible_to_overrides"] is True
    (aid,) = env.fake.state("assignments")["assignments"]
    assert {o.get("course_section_id") for o in env.overrides(aid)} == {env.ba2, None}


def test_push_then_fetch_is_a_no_op(env: Env) -> None:
    a = base_assignment(env)
    add_override(env.fake, a["id"], course_section_id=env.ba1, due_at="2026-09-24T06:59:00Z")
    add_override(env.fake, a["id"], student_ids=[env.users["ann"]], title="Acc")
    add_override(env.fake, a["id"], noop_id=9)
    snapshot = env.submit(a["id"], "ann", "cy")
    base = env.fetch()
    path = only_path(base)
    by_target = {o.target(): o for o in current(base).overrides}
    local_overrides = [
        by_target[("section", ("BA1",))].model_copy(update={"due": None}),
        Override(section="BA2"),
        Override(group="Team 1"),
        by_target[("students", ("ann",))].model_copy(update={"students": ["ann", "bob"]}),
    ]
    local = edit(base, path, overrides=local_overrides)
    _, result = env.push(base, local)
    assert result.error is None, result.error
    remote_after = env.fetch(previous=local)
    plan = plan_of(remote_after, local)
    assert plan.is_empty(), [(o.key.path, o.changes) for o in plan.ops]

    def dumped(c: Course) -> dict[str, bytes]:
        return ASSIGNMENT_SPEC.dump({k.path: v for k, v in c.objects.items()}, CFG)

    assert dumped(local) == dumped(remote_after)
    env.fake.assert_submissions_intact(snapshot)
