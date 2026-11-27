"""Push scenarios for the submission-safety suite (bead y01.4).

Each scenario takes a seeded ``Harness`` and returns a one-line outcome for the run report.
They run through the real ``run_push`` (``Harness.push``) and share one temp clone, in order.
``Harness.scenario()`` rolls the clone back after a refused push and, always, re-reads the
test student's submission on every seeded assignment and compares it to the baseline.

The same functions run offline against FakeCanvas (tests/test_safety_rehearsal.py), so the
logic is checked before anything touches the sandbox.
"""

from __future__ import annotations

from collections.abc import Callable

from canvas_sync.errors import GuardBlocked
from canvas_sync.remote import roster
from tests.integration.sandbox import ALL_FLAGS, Harness, Refused

GRADING = {"--confirm-grading-change"}
ACCESS = {"--confirm-access-removal"}


def _expect_blocked(h: Harness, what: str, **kw: object) -> str:
    try:
        h.push(**kw)
    except GuardBlocked as e:
        return f"{what}: refused ({str(e)[:90]})"
    raise AssertionError(f"{what}: push was NOT refused")


def _commit_edit(h: Harness, msg: str) -> None:
    h.commit(msg)


def _alive(h: Harness, *keys: str) -> None:
    for k in keys:
        assert h.assignment(k) is not None, f"assignment {k} no longer exists on Canvas"


def delete_everything_locally(h: Harness) -> str:
    """Every suite file removed, with no flags and with every confirm flag: refused both ways."""
    with h.scenario():
        for f in h.suite_files():
            (h.root / f).unlink()
        _commit_edit(h, "delete everything")
        a = _expect_blocked(h, "no flags")
        b = _expect_blocked(h, "all flags", confirmed=set(ALL_FLAGS))
        _alive(h, "ungraded", "graded", "access", "empty")
    return f"{a}; {b}"


def delete_each_submitted_assignment(h: Harness) -> str:
    out: list[str] = []
    for key in ("ungraded", "graded", "access"):
        with h.scenario():
            (h.root / h.path(key)).unlink()
            h.edit("empty", "safety test assignment", "safety test assignment, edited")
            _commit_edit(h, f"delete {key}")
            out.append(_expect_blocked(h, key, confirmed=set(ALL_FLAGS)))
            _alive(h, key, "empty")
            desc = (h.assignment("empty") or {}).get("description") or ""
            assert "edited" not in desc, "the refused push still applied another edit"
    return "; ".join(out)


def delete_everything_with_no_delete(h: Harness) -> str:
    """--no-delete keeps every deletion off Canvas; nothing is removed."""
    with h.scenario():
        for f in h.suite_files():
            (h.root / f).unlink()
        _commit_edit(h, "delete everything, no-delete")
        result = h.push(no_delete=True)
        assert result.ok
        _alive(h, "ungraded", "graded", "access", "empty")
    return "no-delete: nothing deleted"


def points_change_on_graded(h: Harness) -> str:
    """Refused without --confirm-grading-change; with it, the push goes through and the
    graded submission still holds its score. The points are restored afterwards."""
    with h.scenario():
        h.edit("graded", "points: 10", "points: 20")
        _commit_edit(h, "points up")
        a = _expect_blocked(h, "unconfirmed")
        assert (h.assignment("graded") or {}).get("points_possible") == 10
    with h.scenario():
        h.edit("graded", "points: 10", "points: 20")
        _commit_edit(h, "points up")
        result = h.push(confirmed=set(GRADING))
        assert result.ok, h.last_output
        assert (h.assignment("graded") or {}).get("points_possible") == 20
        h.assert_submissions_unchanged()
    with h.scenario():
        h.edit("graded", "points: 20", "points: 10")
        _commit_edit(h, "points restored")
        assert h.push(confirmed=set(GRADING)).ok, h.last_output
    return f"{a}; confirmed push kept the graded submission"


def grading_type_change_on_graded(h: Harness) -> str:
    with h.scenario():
        h.edit("graded", "published: true\n", "grading_type: percent\npublished: true\n")
        _commit_edit(h, "grading type")
        return _expect_blocked(h, "grading_type unconfirmed")


def points_change_on_ungraded(h: Harness) -> str:
    """No grade to recalculate: may or may not need a flag; either way nothing changes."""
    with h.scenario():
        h.edit("ungraded", "points: 10", "points: 12")
        _commit_edit(h, "points on ungraded")
        try:
            result = h.push()
            assert result.ok, h.last_output
            outcome = "pushed without a flag"
        except GuardBlocked:
            assert h.push(confirmed=set(ALL_FLAGS)).ok, h.last_output
            outcome = "needed a confirm flag"
    return f"points on ungraded submitted work: {outcome}"


def unpublish_with_submission(h: Harness) -> str:
    """A hard block: no flag overrides it."""
    with h.scenario():
        h.edit("ungraded", "published: true", "published: false")
        _commit_edit(h, "unpublish")
        r = _expect_blocked(h, "unpublish", confirmed=set(ALL_FLAGS))
        a = h.assignment("ungraded")
        assert a is not None and a.get("published") is True
    return r


def submission_types_change(h: Harness) -> str:
    with h.scenario():
        h.edit("ungraded", "[online_text_entry]", "[online_upload]")
        _commit_edit(h, "submission types")
        try:
            ok = h.push().ok
        except GuardBlocked:
            ok = h.push(confirmed=set(ALL_FLAGS)).ok
        assert ok, h.last_output
    with h.scenario():
        h.edit("ungraded", "[online_upload]", "[online_text_entry]")
        _commit_edit(h, "submission types restored")
        assert h.push(confirmed=set(ALL_FLAGS)).ok, h.last_output
    return "submission_types change pushed; submission kept"


def body_and_title_edit(h: Harness) -> str:
    with h.scenario():
        h.edit("graded", "safety test assignment", "safety test assignment, revised")
        h.edit("ungraded", "safety test assignment", "safety test assignment, revised")
        _commit_edit(h, "bodies")
        assert h.push().ok, h.last_output
    return "body edits on submitted assignments pushed"


def _section_name(h: Harness) -> str:
    names = sorted(roster.load_cache(h.root).sections)
    if not names:
        raise Refused("the course has no sections to use as an override audience")
    return names[0]


def _access_on(h: Harness, section: str) -> None:
    """'access' becomes overrides-only, with a single section override (the section is only
    an audience on this suite assignment). The test student is not a StudentEnrollment, so the
    guard sees a submitter that loses 'Everyone else'."""
    h.edit(
        "access",
        "published: true\n",
        f"published: true\nassign_to_overrides_only: true\noverrides:\n  - section: {section!r}\n",
    )


def _access_off(h: Harness) -> None:
    p = h.root / h.path("access")
    lines = p.read_text(encoding="utf-8").split("\n")
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith("assign_to_overrides_only:"):
            continue
        if line.startswith("overrides:"):
            skipping = True
            continue
        if skipping and (line.startswith((" ", "- "))):
            continue
        skipping = False
        out.append(line)
    p.write_text("\n".join(out), encoding="utf-8")


def access_removal(h: Harness) -> str:
    """Turning on 'overrides only' removes 'Everyone else': a submitter loses access.
    Refused without --confirm-access-removal; with it the push may proceed, and the
    submission is still intact (the autouse comparison in ``h.scenario()``)."""
    section = _section_name(h)
    with h.scenario():
        _access_on(h, section)
        _commit_edit(h, "overrides only")
        a = _expect_blocked(h, "access removal unconfirmed")
        live = h.assignment("access") or {}
        assert not live.get("only_visible_to_overrides")
    with h.scenario():
        _access_on(h, section)
        _commit_edit(h, "overrides only")
        result = h.push(confirmed=set(ACCESS) | GRADING)
        b = "confirmed push applied" if result.ok else f"push stopped: {h.last_output[-100:]}"
    return f"{a}; {b}"


def override_removal(h: Harness) -> str:
    """Remove the override (and then the overrides-only flag): submissions stay. Whether a
    flag is needed is recorded, not prescribed."""
    live = h.assignment("access") or {}
    if not live.get("only_visible_to_overrides"):
        return "skipped: access_removal did not apply the override"
    with h.scenario():
        _access_off(h)
        _commit_edit(h, "remove override")
        try:
            ok = h.push().ok
            how = "no flag needed"
        except GuardBlocked:
            ok = h.push(confirmed=set(ALL_FLAGS)).ok
            how = "needed a confirm flag"
        assert ok, h.last_output
    return f"override removal pushed ({how})"


def delete_only_the_empty_assignment(h: Harness) -> str:
    """The control: an assignment with no submissions may be deleted, and the submitted ones
    are untouched. Runs last (it removes 'empty')."""
    with h.scenario():
        (h.root / h.path("empty")).unlink()
        _commit_edit(h, "delete empty")
        result = h.push()
        assert result.ok, h.last_output
        assert (
            h.assignment("empty") is None
            or (h.assignment("empty") or {}).get("workflow_state") == "deleted"
        )
        _alive(h, "ungraded", "graded", "access")
    return "assignment without submissions deleted; the others untouched"


SCENARIOS: list[Callable[[Harness], str]] = [
    delete_everything_locally,
    delete_each_submitted_assignment,
    delete_everything_with_no_delete,
    points_change_on_graded,
    grading_type_change_on_graded,
    points_change_on_ungraded,
    unpublish_with_submission,
    submission_types_change,
    body_and_title_edit,
    access_removal,
    override_removal,
    delete_only_the_empty_assignment,
]
