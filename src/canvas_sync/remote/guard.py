"""Submission guard (CONTRACTS.md section 10), safety layer 2.

The guard never trusts the plan's snapshot: every call re-fetches the assignment
(and, where needed, its submissions, overrides and the late policy) from Canvas
with GET. If live state cannot be read, the operation is refused (fail closed).

Hard blocks raise ``GuardBlocked`` before ``confirmed`` is looked at, so no flag
can override them. Everything else is a ``Risk`` that needs its flag.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from canvas_sync.config import CourseConfig
from canvas_sync.errors import CanvasAPIError, GuardBlocked
from canvas_sync.model.base import Kind
from canvas_sync.model.overrides import Override
from canvas_sync.plan.core import Action, Operation, Risk, RiskKind
from canvas_sync.remote.http import CanvasClient

CONFIRM_GRADING = "--confirm-grading-change"
CONFIRM_ACCESS = "--confirm-access-removal"
CONFIRM_NOTIFY = "--confirm-notify"

_SUBMITTED_STATES = frozenset({"submitted", "graded", "pending_review"})

Row = dict[str, Any]


def _is_submitted(sub: Row) -> bool:
    """Student work exists: not just Canvas's per-student 'unsubmitted' placeholder."""
    return bool(
        sub.get("workflow_state") in _SUBMITTED_STATES
        or sub.get("submitted_at") is not None
        or sub.get("score") is not None
        or sub.get("grade") is not None
        or sub.get("excused")
    )


def _is_graded(sub: Row) -> bool:
    return bool(
        sub.get("workflow_state") == "graded"
        or sub.get("score") is not None
        or sub.get("grade") is not None
        or sub.get("excused")
    )


def is_group_op(op: Operation) -> bool:
    """An operation on an assignment group (duck-typed by class name, see CONTRACTS.md 10)."""
    return any(type(o).__name__ == "AssignmentGroup" for o in (op.before, op.after) if o)


def _assignment_id(op: Operation) -> int | None:
    for obj in (op.before, op.after):
        cid = getattr(obj, "canvas_id", None)
        if isinstance(cid, int) and not isinstance(cid, bool):
            return cid
    return None


def _field(obj: object | None, name: str) -> Any:
    return getattr(obj, name, None) if obj is not None else None


def _changed(op: Operation, *names: str) -> bool:
    """True if any field differs between before and after, or is listed in ``changes``."""
    for name in names:
        if name in op.changes and op.changes[name][0] != op.changes[name][1]:
            return True
        if (
            op.before is not None
            and op.after is not None
            and _field(op.before, name) != _field(op.after, name)
        ):
            return True
    return False


@dataclass
class _Evaluation:
    risks: list[Risk]
    blocks: list[str]


class SubmissionGuard:
    def __init__(
        self,
        client: CanvasClient,
        config: CourseConfig,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.client = client
        self.config = config
        self._now = now

    # ---- public API (CONTRACTS.md section 10) -------------------------------------

    def check(self, op: Operation) -> list[Risk]:
        """Re-fetch the assignment live and report submission-related risks.

        Returns live-derived risks only (not ``op.risks``). Raises ``GuardBlocked`` if
        live state cannot be read for an operation that needs it.
        """
        return self._evaluate(op).risks

    def assert_allowed(self, op: Operation, confirmed: set[str]) -> None:
        """Raises GuardBlocked. Idempotent: live GETs on every call, never a write."""
        ev = self._evaluate(op)
        if ev.blocks:
            raise GuardBlocked("; ".join(ev.blocks))  # before `confirmed` is consulted
        seen: set[tuple[RiskKind, str]] = set()
        for risk in [*op.risks, *ev.risks]:
            flag = risk.needs_confirm
            if flag is None or (risk.kind, flag) in seen:
                continue
            seen.add((risk.kind, flag))
            if flag in confirmed or f"{flag} {op.key.path}" in confirmed:
                continue
            raise GuardBlocked(f"{risk.message} (needs {flag})")

    # ---- evaluation ---------------------------------------------------------------

    def _evaluate(self, op: Operation) -> _Evaluation:
        path = op.key.path
        if is_group_op(op):
            if op.action is not Action.DELETE:
                return _Evaluation([], [])
            msg = f"{path}: assignment groups are never deleted (delete empty groups in Canvas)"
            return _Evaluation([Risk(RiskKind.HAS_SUBMISSIONS, msg, None)], [msg])
        if op.key.kind is Kind.ANNOUNCEMENT:
            return _Evaluation(self._announcement_risks(op), [])
        if op.key.kind is not Kind.ASSIGNMENT or op.action is Action.CREATE:
            return _Evaluation([], [])
        return self._assignment(op)

    def _announcement_risks(self, op: Operation) -> list[Risk]:
        if op.action not in (Action.CREATE, Action.UPDATE) or op.after is None:
            return []
        if self._posted_now(op.after) and not (op.before and self._posted_now(op.before)):
            msg = f"{op.key.path}: posts now and emails students; cannot be taken back"
            return [Risk(RiskKind.NOTIFIES_STUDENTS, msg, CONFIRM_NOTIFY)]
        return []

    def _posted_now(self, obj: object) -> bool:
        if not _field(obj, "published"):
            return False
        post_at = _field(obj, "post_at")
        return post_at is None or cast(datetime, post_at) <= self._now()

    def _assignment(self, op: Operation) -> _Evaluation:
        path = op.key.path
        aid = _assignment_id(op)
        if aid is None:
            return _Evaluation(
                [], [f"{path}: cannot tell which Canvas assignment this operation touches"]
            )
        c = self.config.course_id
        base = f"/courses/{c}/assignments/{aid}"
        live = self._get_optional(base, path)
        if live is None:  # already gone on Canvas: nothing of ours to destroy
            return _Evaluation([], [])

        risks: list[Risk] = []
        blocks: list[str] = []
        is_delete = op.action is Action.DELETE

        def block(msg: str) -> None:
            risks.append(Risk(RiskKind.HAS_SUBMISSIONS, msg, None))
            blocks.append(msg)

        subs_cache: list[Row] | None = None

        def submissions() -> list[Row]:
            nonlocal subs_cache
            if subs_cache is None:
                subs_cache = self._get_list(f"{base}/submissions", path)
            return subs_cache

        flagged = bool(
            live.get("has_submitted_submissions") or live.get("graded_submissions_exist")
        )
        unpublishing = op.action is Action.UNPUBLISH or (
            _field(op.after, "published") is False and bool(live.get("published"))
        )
        group_cat = _changed(op, "group_category", "group_category_id")
        sub_types = _changed(op, "submission_types")
        has_subs = flagged
        if not has_subs and (is_delete or unpublishing or group_cat or sub_types):
            has_subs = any(_is_submitted(s) for s in submissions())  # the flags can lag

        if has_subs:
            if is_delete:
                block(f"{path}: has student submissions and is never deleted; keep it")
                return _Evaluation(risks, blocks)
            if group_cat:
                block(f"{path}: group_category cannot change once submissions exist")
            if unpublishing:
                block(f"{path}: has submissions, Canvas refuses to unpublish it; not attempted")
            if sub_types:
                msg = f"{path}: submission_types changes; existing submissions are kept"
                risks.append(Risk(RiskKind.HAS_SUBMISSIONS, msg, None))
        if is_delete or op.action is Action.UNPUBLISH:
            return _Evaluation(risks, blocks)

        if _changed(op, "points", "grading_type"):
            n = sum(1 for s in submissions() if _is_graded(s))
            if n:
                msg = f"{path}: points/grading_type change affects {n} graded submission(s)"
                risks.append(Risk(RiskKind.GRADING_CHANGE, msg, CONFIRM_GRADING))
        if _changed(op, "due", "overrides") and self._late_policy_on(path):
            n = sum(1 for s in submissions() if _is_graded(s))
            if n:
                msg = (
                    f"{path}: late policy is on; the date change recalculates "
                    f"{n} graded submission(s)"
                )
                risks.append(Risk(RiskKind.LATE_POLICY_REGRADE, msg, CONFIRM_GRADING))
        if _changed(op, "overrides", "assign_to_overrides_only") and op.after is not None:
            n = self._access_removed(op, aid, live, submissions(), path)
            if n:
                msg = f"{path}: {n} student(s) with submissions lose access"
                risks.append(Risk(RiskKind.ACCESS_REMOVED, msg, CONFIRM_ACCESS))
        return _Evaluation(risks, blocks)

    # ---- access removal -----------------------------------------------------------

    def _access_removed(
        self, op: Operation, aid: int, live: Row, subs: list[Row], path: str
    ) -> int:
        """Number of submitters who may lose access, from live overrides.

        Conservative: a removed group override has unreadable membership, so every
        submitter not otherwise kept counts; unresolvable kept students (aliases) count
        as removed.
        """
        submitters = {
            int(s["user_id"]) for s in subs if _is_submitted(s) and s.get("user_id") is not None
        }
        if not submitters:
            return 0
        c = self.config.course_id
        live_overrides = self._get_list(f"/courses/{c}/assignments/{aid}/overrides", path)
        kept = cast("list[Override]", getattr(op.after, "overrides", None) or [])
        turned_on = bool(getattr(op.after, "assign_to_overrides_only", False)) and not live.get(
            "only_visible_to_overrides"
        )

        keep_sections = self._ids_by_name(f"/courses/{c}/sections", {o.section for o in kept}, path)
        keep_groups = self._ids_by_name(f"/courses/{c}/groups", {o.group for o in kept}, path)
        keep_users = self._student_ids({s for o in kept for s in (o.students or [])}, path)
        section_of: dict[int, int] = {}
        enrollments = self._get_list(
            f"/courses/{c}/enrollments", path, {"type[]": ["StudentEnrollment"]}
        )
        for e in enrollments:
            if e.get("user_id") is not None and e.get("course_section_id") is not None:
                section_of[int(e["user_id"])] = int(e["course_section_id"])

        def retained(uid: int) -> bool:
            return uid in keep_users or section_of.get(uid) in keep_sections

        affected: set[int] = set()
        if turned_on:  # "Everyone else" goes away
            affected |= {u for u in submitters if not retained(u)}
        for o in live_overrides:
            section_id, group_id = o.get("course_section_id"), o.get("group_id")
            if section_id is not None:
                if int(section_id) not in keep_sections:
                    affected |= {u for u in submitters if section_of.get(u) == int(section_id)}
            elif group_id is not None:
                if int(group_id) not in keep_groups:
                    affected |= {u for u in submitters if not retained(u)}
            else:
                students = {int(x) for x in cast("list[Any]", o.get("student_ids") or [])}
                affected |= {u for u in students & submitters if not retained(u)}
        return len(affected)

    def _ids_by_name(self, url: str, names: set[str | None], path: str) -> set[int]:
        wanted = {n for n in names if n is not None}
        if not wanted:
            return set()
        return {int(r["id"]) for r in self._get_list(url, path) if r.get("name") in wanted}

    def _student_ids(self, refs: set[str], path: str) -> set[int]:
        """Resolve kept student refs to ids (login_id or sis); aliases are unresolvable."""
        if not refs or self.config.student_ref == "alias":
            return set()
        users = self._get_list(
            f"/courses/{self.config.course_id}/users", path, {"enrollment_type[]": ["student"]}
        )
        out: set[int] = set()
        for u in users:
            ref = (
                u.get("login_id")
                if self.config.student_ref == "login_id"
                else (f"sis:{u['sis_user_id']}" if u.get("sis_user_id") else None)
            )
            if ref in refs:
                out.add(int(u["id"]))
        return out

    # ---- live reads (GET only; fail closed) ---------------------------------------

    def _late_policy_on(self, path: str) -> bool:
        body = self._get_optional(f"/courses/{self.config.course_id}/late_policy", path)
        if body is None:
            return False
        lp = cast("Row", body.get("late_policy", body))
        return bool(
            lp.get("late_submission_deduction_enabled")
            or lp.get("missing_submission_deduction_enabled")
        )

    def _get_optional(self, url: str, path: str) -> Row | None:
        """One object, or None on 404. Any other failure refuses the operation."""
        try:
            body = self.client.get_one(url)
        except CanvasAPIError as e:
            if e.status == 404:
                return None
            raise GuardBlocked(
                f"{path}: cannot verify live state (Canvas error {e.status})"
            ) from None
        except Exception as e:
            raise GuardBlocked(f"{path}: cannot verify live state ({type(e).__name__})") from None
        if not isinstance(body, dict):
            raise GuardBlocked(f"{path}: unexpected live response")
        return cast("Row", body)

    def _get_list(self, url: str, path: str, params: dict[str, Any] | None = None) -> list[Row]:
        try:
            body = self.client.get(url, params)
        except Exception as e:
            status = f" {e.status}" if isinstance(e, CanvasAPIError) else ""
            raise GuardBlocked(
                f"{path}: cannot verify live state ({type(e).__name__}{status})"
            ) from None
        if not isinstance(body, list):
            raise GuardBlocked(f"{path}: unexpected live response")
        return cast("list[Row]", body)
