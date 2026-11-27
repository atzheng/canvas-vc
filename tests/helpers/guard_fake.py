"""Minimal assignment routes for guard/backup tests (installed in front of any others).

Assignments live in ``fake.state("guard_assignments")``. Deleting one removes only the
assignment record; ``fake.submissions`` is deliberately never touched here (a real
Canvas soft-deletes them with the assignment, which the tool must never cause).
"""

from __future__ import annotations

from typing import Any, cast

from tests.fake_canvas.core import CanvasError, FakeCanvas, Req


def _assignments(fake: FakeCanvas) -> dict[int, dict[str, Any]]:
    return cast("dict[int, dict[str, Any]]", fake.state("guard_assignments"))


def _overrides(fake: FakeCanvas) -> dict[int, list[dict[str, Any]]]:
    return cast("dict[int, list[dict[str, Any]]]", fake.state("guard_overrides"))


def install(fake: FakeCanvas) -> None:
    store = _assignments(fake)
    overrides = _overrides(fake)
    fake.state("guard_late_policy")

    def one(q: Req) -> dict[str, Any]:
        found = store.get(int(q.match.group(1)))
        if found is None:
            raise CanvasError(404, "The specified resource does not exist.")
        return found

    def put(q: Req) -> dict[str, Any]:
        a = one(q)
        a.update(q.body)
        return a

    def delete(q: Req) -> dict[str, Any]:
        a = one(q)
        del store[a["id"]]
        return a

    def late(q: Req) -> dict[str, Any]:
        lp = fake.state("guard_late_policy").get("policy")
        if lp is None:
            raise CanvasError(404, "The specified resource does not exist.")
        return {"late_policy": lp}

    routes: list[tuple[str, str, Any]] = [
        ("GET", r"/courses/{c}/assignments/(\d+)", one),
        ("PUT", r"/courses/{c}/assignments/(\d+)", put),
        ("DELETE", r"/courses/{c}/assignments/(\d+)", delete),
        (
            "GET",
            r"/courses/{c}/assignments/(\d+)/overrides",
            lambda q: list(overrides.get(int(q.match.group(1)), [])),
        ),
        ("GET", r"/courses/{c}/late_policy", late),
    ]
    for method, pattern, handler in routes:
        fake.route(method, pattern, handler)
        fake._routes.insert(0, fake._routes.pop())  # pyright: ignore[reportPrivateUsage]


def add_assignment(fake: FakeCanvas, **fields: Any) -> dict[str, Any]:
    aid = fake.next_id()
    a: dict[str, Any] = {
        "id": aid,
        "name": "Homework",
        "points_possible": 10,
        "grading_type": "points",
        "published": True,
        "has_submitted_submissions": False,
        "graded_submissions_exist": False,
        "only_visible_to_overrides": False,
        "group_category_id": None,
        **fields,
    }
    _assignments(fake)[aid] = a
    return a


def set_late_policy(fake: FakeCanvas, enabled: bool) -> None:
    fake.state("guard_late_policy")["policy"] = {
        "late_submission_deduction_enabled": enabled,
        "late_submission_deduction": 10.0,
    }


def add_override(fake: FakeCanvas, assignment_id: int, **fields: Any) -> dict[str, Any]:
    ov = {"id": fake.next_id(), "assignment_id": assignment_id, **fields}
    _overrides(fake).setdefault(assignment_id, []).append(ov)
    return ov
