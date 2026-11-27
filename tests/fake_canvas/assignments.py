"""FakeCanvas behaviour for assignments: GET assignments, groups and overrides.

State lives in ``fake.state("assignments")``: ``assignments`` (``{id: dict}``), ``overrides``
(``{assignment id: [dict]}``) and ``groups`` (``{id: dict}``). Seed with ``add_assignment``,
``add_override`` and ``add_assignment_group``; fields follow the Canvas API
(``name``, ``points_possible``, ``assignment_group_id``, ``only_visible_to_overrides``,
``due_at`` ..., override ``course_section_id`` / ``group_id`` / ``student_ids``).
``GET /assignments`` honours ``include[]=overrides``; assignment rows carry
``has_submitted_submissions`` / ``graded_submissions_exist`` derived from ``fake.submissions``.
Writes: ``POST/PUT/DELETE /assignments`` (bodies as ``{"assignment": {...}}`` or flat) and
``POST /assignment_groups``; there is no group DELETE. A PUT carrying ``assignment_overrides`` is
refused (the tool must never replace the whole override set), as is unpublishing an assignment
with submissions (Canvas answers 400). ``DELETE`` removes only the assignment record:
``fake.submissions`` is never touched here (real Canvas soft-deletes them, which the tool must
never cause), so tests assert on ``fake.log`` that no DELETE reached such an assignment.
``set_late_policy`` seeds ``GET /late_policy``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tests.fake_canvas.core import FakeCanvas, Req


def _store(fake: FakeCanvas) -> dict[str, Any]:
    st = fake.state("assignments")
    st.setdefault("assignments", {})
    st.setdefault("overrides", {})
    st.setdefault("groups", {})
    return st


def add_assignment_group(fake: FakeCanvas, name: str, **fields: Any) -> dict[str, Any]:
    groups: dict[int, dict[str, Any]] = _store(fake)["groups"]
    group: dict[str, Any] = {
        "id": fake.next_id(),
        "name": name,
        "position": len(groups) + 1,
        "group_weight": 0,
        **fields,
    }
    groups[group["id"]] = group
    return group


def add_assignment(fake: FakeCanvas, name: str, **fields: Any) -> dict[str, Any]:
    st = _store(fake)
    items: dict[int, dict[str, Any]] = st["assignments"]
    a: dict[str, Any] = {
        "id": fake.next_id(),
        "course_id": fake.course_id,
        "name": name,
        "description": "",
        "points_possible": None,
        "assignment_group_id": None,
        "submission_types": ["none"],
        "allowed_extensions": [],
        "grading_type": "points",
        "omit_from_final_grade": False,
        "published": True,
        "unlock_at": None,
        "due_at": None,
        "lock_at": None,
        "only_visible_to_overrides": False,
        "has_overrides": False,
        **fields,
    }
    items[a["id"]] = a
    return a


def add_override(fake: FakeCanvas, assignment_id: int, **fields: Any) -> dict[str, Any]:
    """Seed an override: pass one of ``course_section_id``, ``group_id``, ``student_ids``."""
    st = _store(fake)
    ov: dict[str, Any] = {
        "id": fake.next_id(),
        "assignment_id": assignment_id,
        "title": fields.pop("title", "override"),
        "due_at": None,
        "unlock_at": None,
        "lock_at": None,
        **fields,
    }
    st["overrides"].setdefault(assignment_id, []).append(ov)
    st["assignments"][assignment_id]["has_overrides"] = True
    return ov


def set_late_policy(fake: FakeCanvas, enabled: bool) -> None:
    _store(fake)["late_policy"] = {
        "late_submission_deduction_enabled": enabled,
        "late_submission_deduction": 10.0,
    }


_WRITABLE = (
    "name",
    "description",
    "points_possible",
    "assignment_group_id",
    "submission_types",
    "allowed_extensions",
    "grading_type",
    "omit_from_final_grade",
    "published",
    "unlock_at",
    "due_at",
    "lock_at",
    "only_visible_to_overrides",
)


def register(fake: FakeCanvas) -> None:
    from tests.fake_canvas.core import CanvasError, FakeCanvasError  # core imports this module

    def not_found() -> CanvasError:
        return CanvasError(404, "The specified resource does not exist.")

    def flagged(a: dict[str, Any]) -> dict[str, Any]:
        subs = fake.submissions.get(a["id"], [])
        out = dict(a)
        out["has_submitted_submissions"] = any(
            s.get("workflow_state") in ("submitted", "graded") for s in subs
        )
        out["graded_submissions_exist"] = any(s.get("workflow_state") == "graded" for s in subs)
        return out

    def with_overrides(a: dict[str, Any]) -> dict[str, Any]:
        out = flagged(a)
        out["overrides"] = [dict(o) for o in _store(fake)["overrides"].get(a["id"], [])]
        return out

    def list_assignments(q: Req) -> list[dict[str, Any]]:
        rows = list(_store(fake)["assignments"].values())
        if "overrides" in q.values("include"):
            return [with_overrides(a) for a in rows]
        return [flagged(a) for a in rows]

    def one(q: Req) -> dict[str, Any]:
        a = _store(fake)["assignments"].get(int(q.match.group(1)))
        if a is None:
            raise not_found()
        return with_overrides(a) if "overrides" in q.values("include") else flagged(a)

    def overrides(q: Req) -> list[dict[str, Any]]:
        aid = int(q.match.group(1))
        if aid not in _store(fake)["assignments"]:
            raise not_found()
        return [dict(o) for o in _store(fake)["overrides"].get(aid, [])]

    def groups(q: Req) -> list[dict[str, Any]]:
        return [dict(g) for g in _store(fake)["groups"].values()]

    def body_of(q: Req) -> dict[str, Any]:
        inner = q.body.get("assignment", q.body)
        if not isinstance(inner, dict):
            raise CanvasError(400, "bad body")
        body: dict[str, Any] = dict(inner)  # pyright: ignore[reportUnknownArgumentType]
        if "assignment_overrides" in body or "assignment_overrides" in q.body:
            raise FakeCanvasError("assignment_overrides would replace the whole override set")
        unknown = set(body) - set(_WRITABLE)
        if unknown:
            raise FakeCanvasError(f"fake does not model assignment fields {sorted(unknown)}")
        if isinstance(body.get("description"), str):
            body["description"] = fake.sanitize(body["description"])
        return body

    def create(q: Req) -> dict[str, Any]:
        body = body_of(q)
        if not body.get("name"):
            raise CanvasError(400, "name is required")
        if body.get("assignment_group_id") not in (None, *_store(fake)["groups"]):
            raise CanvasError(400, "invalid assignment group")
        fields = {"published": False, **body}
        a = add_assignment(fake, fields.pop("name"), **fields)
        return flagged(a)

    def put(q: Req) -> dict[str, Any]:
        a = _store(fake)["assignments"].get(int(q.match.group(1)))
        if a is None:
            raise not_found()
        body = body_of(q)
        if (
            body.get("published") is False
            and a["published"]
            and flagged(a)["has_submitted_submissions"]
        ):
            raise CanvasError(400, "Cannot unpublish assignment with student submissions")
        a.update(body)
        return flagged(a)

    def delete(q: Req) -> dict[str, Any]:
        st = _store(fake)
        a = st["assignments"].pop(int(q.match.group(1)), None)
        if a is None:
            raise not_found()
        st["overrides"].pop(a["id"], None)
        return a  # fake.submissions is deliberately left alone

    def create_group(q: Req) -> dict[str, Any]:
        name = q.body.get("name") or q.body.get("assignment_group", {}).get("name")
        if not name:
            raise CanvasError(400, "name is required")
        return add_assignment_group(fake, name)

    def late_policy(q: Req) -> dict[str, Any]:
        lp = _store(fake).get("late_policy")
        if lp is None:
            raise not_found()
        return {"late_policy": lp}

    fake.route("POST", r"/courses/{c}/assignments", create)
    fake.route("PUT", r"/courses/{c}/assignments/(\d+)", put)
    fake.route("DELETE", r"/courses/{c}/assignments/(\d+)", delete)
    fake.route("POST", r"/courses/{c}/assignment_groups", create_group)
    fake.route("GET", r"/courses/{c}/late_policy", late_policy)
    fake.route("GET", r"/courses/{c}/assignments", list_assignments)
    fake.route("GET", r"/courses/{c}/assignments/(\d+)", one)
    fake.route("GET", r"/courses/{c}/assignments/(\d+)/overrides", overrides)
    fake.route("GET", r"/courses/{c}/assignment_groups", groups)
