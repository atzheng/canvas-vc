"""FakeCanvas behaviour for assignments: GET assignments, groups and overrides.

State lives in ``fake.state("assignments")``: ``assignments`` (``{id: dict}``), ``overrides``
(``{assignment id: [dict]}``) and ``groups`` (``{id: dict}``). Seed with ``add_assignment``,
``add_override`` and ``add_assignment_group``; fields follow the Canvas API
(``name``, ``points_possible``, ``assignment_group_id``, ``only_visible_to_overrides``,
``due_at`` ..., override ``course_section_id`` / ``group_id`` / ``student_ids``). Writes are not
modelled yet. ``GET /assignments`` honours ``include[]=overrides``. Submissions
(``fake.submissions``) are never touched here.
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


def register(fake: FakeCanvas) -> None:
    from tests.fake_canvas.core import CanvasError  # core imports this module

    def not_found() -> CanvasError:
        return CanvasError(404, "The specified resource does not exist.")

    def with_overrides(a: dict[str, Any]) -> dict[str, Any]:
        out = dict(a)
        out["overrides"] = [dict(o) for o in _store(fake)["overrides"].get(a["id"], [])]
        return out

    def list_assignments(q: Req) -> list[dict[str, Any]]:
        rows = list(_store(fake)["assignments"].values())
        if "overrides" in q.values("include"):
            return [with_overrides(a) for a in rows]
        return [dict(a) for a in rows]

    def one(q: Req) -> dict[str, Any]:
        a = _store(fake)["assignments"].get(int(q.match.group(1)))
        if a is None:
            raise not_found()
        return with_overrides(a) if "overrides" in q.values("include") else dict(a)

    def overrides(q: Req) -> list[dict[str, Any]]:
        aid = int(q.match.group(1))
        if aid not in _store(fake)["assignments"]:
            raise not_found()
        return [dict(o) for o in _store(fake)["overrides"].get(aid, [])]

    def groups(q: Req) -> list[dict[str, Any]]:
        return [dict(g) for g in _store(fake)["groups"].values()]

    fake.route("GET", r"/courses/{c}/assignments", list_assignments)
    fake.route("GET", r"/courses/{c}/assignments/(\d+)", one)
    fake.route("GET", r"/courses/{c}/assignments/(\d+)/overrides", overrides)
    fake.route("GET", r"/courses/{c}/assignment_groups", groups)
