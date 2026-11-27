"""Submission-safety suite: sandbox guards, fenced clients and the harness (bead y01.4).

This is test infrastructure under ``tests/integration``; nothing here is imported by ``src/``.
It is run by the owner and the coordinator only (see the PR body for the run sheet).

Layers of protection, in the order they act:

1. ``check_env``: refuses unless the URL, the course id and the explicit opt-in variable are
   exactly the sandbox values. Any other course id is refused, whatever else is set.
2. ``check_course_unpublished``: refuses to run if the sandbox course is published (real
   students could see the suite's assignments).
3. ``check_scope``: before every push, the plan (computed offline from git) may only touch
   ``assignments/zz-canvas-sync-safety-*`` objects; anything else aborts the test before a write.
4. ``SuiteClient``: the write client used for every push refuses any write whose path is not
   under an assignment id this run created and recorded.
5. ``RawSeeder``: the only code that calls endpoints outside ``remote/allowlist.py`` (to submit
   as, and grade, the Student View test student). It is fenced to exact paths, recorded
   assignment ids and the single test-student id, and never follows redirects.
6. Cleanup deletes only recorded ids whose live name still starts with the suite prefix.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from rich.console import Console

from canvas_sync import gitops
from canvas_sync.commands.diff import build_plan
from canvas_sync.commands.fetch import committed_inputs, run_fetch
from canvas_sync.commands.push import PushResult, run_push
from canvas_sync.config import CourseConfig, load_config
from canvas_sync.errors import CanvasAPIError, CanvasSyncError
from canvas_sync.model.base import Kind
from canvas_sync.model.roster import RosterCache
from canvas_sync.plan.core import Plan
from canvas_sync.remote import roster
from canvas_sync.remote.http import CanvasClient

REQUIRED_URL = "https://canvas.ubc.ca"
REQUIRED_COURSE = "198014"
OPT_IN_VAR = "CANVAS_SAFETY_OK"
PREFIX = "zz-canvas-sync-safety"
TEST_STUDENT_REF = "zz-test-student"  # roster alias for the Student View student (never real)
SCOPE_PREFIX = f"assignments/{PREFIX}-"
TIMEZONE = "America/Vancouver"
ALL_FLAGS = {"--confirm-grading-change", "--confirm-access-removal", "--confirm-notify"}


class Refused(Exception):
    """A guard refused; nothing was written."""


class ScopeViolation(Refused):
    """A push or write would touch something that is not a suite-created object."""


# ---- guards (offline-testable) ------------------------------------------------------------


def check_env(env: dict[str, str]) -> tuple[str, int]:
    """Hard-coded sandbox check. Returns (base_url, course_id) or raises ``Refused``."""
    if env.get(OPT_IN_VAR) != "1":
        raise Refused(f"refusing: set {OPT_IN_VAR}=1 (this writes to a real Canvas course)")
    if env.get("CANVAS_SANDBOX_URL", "").rstrip("/") != REQUIRED_URL:
        raise Refused(f"refusing: CANVAS_SANDBOX_URL must be {REQUIRED_URL}")
    if env.get("CANVAS_SANDBOX_COURSE", "") != REQUIRED_COURSE:
        raise Refused(f"refusing: CANVAS_SANDBOX_COURSE must be {REQUIRED_COURSE}")
    if not env.get("CANVAS_TOKEN"):
        raise Refused("refusing: CANVAS_TOKEN is not set")
    return REQUIRED_URL, int(REQUIRED_COURSE)


def check_course_unpublished(course: dict[str, Any]) -> None:
    if course.get("workflow_state") == "available":
        raise Refused("refusing: the sandbox course is published; unpublish it first")


def assert_recorded(recorded: set[int], ident: int) -> None:
    """Cleanup (and every write) may only touch ids this run recorded."""
    if ident not in recorded:
        raise Refused(f"refusing: assignment {ident} was not created by this run")


def assert_suite_name(name: str | None, ident: int) -> None:
    if not (name or "").startswith(PREFIX):
        raise Refused(f"refusing: assignment {ident} is not named {PREFIX}*")


def student_refs(student_id: int) -> set[str]:
    """The only student refs an override may name: the test student."""
    return {TEST_STUDENT_REF, f"unknown:{student_id}"}


def check_scope(plan: Plan, student_id: int) -> None:
    """Every operation must be on a suite assignment file, and every override audience (old
    or new) must be the Student View test student alone: no section, group or other student.
    Anything else aborts the test before a write."""
    bad = [
        f"{op.action.name} {op.key.kind.name} {op.key.path}"
        for op in plan.ops
        if op.key.kind is not Kind.ASSIGNMENT or not op.key.path.startswith(SCOPE_PREFIX)
    ]
    if bad:
        raise ScopeViolation("push would touch non-suite objects: " + "; ".join(bad[:5]))
    allowed = student_refs(student_id)
    for op in plan.ops:
        for obj in (op.before, op.after):
            for o in getattr(obj, "overrides", None) or []:
                if o.section is not None or o.group is not None:
                    raise ScopeViolation(f"{op.key.path}: override targets a section or group")
                if not o.students or not set(o.students) <= allowed:
                    raise ScopeViolation(
                        f"{op.key.path}: override targets someone but the test student"
                    )


def check_override_payload(path: str, json: Any, students: set[int]) -> None:
    """A write to an override may only name the test student(s) in ``students``."""
    if "/overrides" not in path:
        if isinstance(json, dict) and "assignment_overrides" in json.get("assignment", {}):  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            raise ScopeViolation("refusing: assignment write carries an override set")
        return
    body: Any = json.get("assignment_override") if isinstance(json, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if body is None:
        return  # DELETE, or a write with no body
    if not isinstance(body, dict):
        raise ScopeViolation("refusing: malformed override payload")
    if body.get("course_section_id") or body.get("group_id"):  # pyright: ignore[reportUnknownMemberType]
        raise ScopeViolation("refusing: override targets a section or group")
    ids: Any = body.get("student_ids")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    is_create = path.endswith("/overrides")
    if ids is None and not is_create:
        return  # a date-only update of an existing override (audience unchanged)
    if not isinstance(ids, list) or not ids or not {int(i) for i in ids} <= students:  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        raise ScopeViolation("refusing: override audience is not the test student alone")


class SuiteClient(CanvasClient):
    """A write client that only writes under assignments this run recorded."""

    def __init__(
        self, *args: Any, recorded: set[int], students: set[int] | None = None, **kw: Any
    ) -> None:
        super().__init__(*args, **kw)
        self.recorded = recorded
        self.students: set[int] = students if students is not None else set()
        self._fence_rx = re.compile(
            rf"(?:/api/v1)?/courses/{self.course_id}/assignments/(\d+)(?:/overrides(?:/\d+)?)?"
        )

    def _fence(self, method: str, path: str) -> None:
        m = self._fence_rx.fullmatch(path)
        if m is None:
            raise ScopeViolation(f"refusing {method} {path}: not under a suite assignment")
        assert_recorded(self.recorded, int(m.group(1)))

    def create_assignment(self, body: dict[str, Any]) -> dict[str, Any]:
        """The one write outside the fence (a create has no id yet); the new id is recorded."""
        path = f"/courses/{self.course_id}/assignments"
        row = super()._write("POST", path, None, {"assignment": body})
        self.recorded.add(int(row["id"]))
        return row

    def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self._fence("DELETE", path)
        return super().delete(path, params)

    def _write(self, method: str, path: str, data: Any, json: Any) -> Any:
        self._fence(method, path)
        check_override_payload(path, json, self.students)
        return super()._write(method, path, data, json)


# ---- seeding: the test-only raw client ----------------------------------------------------


class Seeder(Protocol):
    def submit_text(self, assignment_id: int, body: str) -> str:
        """Submit as the test student. Returns the method used ('submission' or a fallback)."""
        ...

    def grade(self, assignment_id: int, score: float) -> None: ...

    def version(self) -> str: ...


class RawSeeder:
    """Calls endpoints that are NOT on ``remote/allowlist.py`` (CLAUDE.md allows network
    use under tests/integration only): submit as the Student View test student (masquerade
    ``as_user_id``), teacher comment/grade on that student, test-student lookup and the build
    version. Every call is checked against an exact fence; redirects are never followed.
    """

    def __init__(
        self, base_url: str, token: str, course_id: int, student_id: int, recorded: set[int]
    ) -> None:
        self._base, self._token = base_url.rstrip("/"), token
        self.course_id, self.student_id, self.recorded = course_id, student_id, recorded
        self._http = httpx.Client(follow_redirects=False, timeout=60.0)

    def __repr__(self) -> str:  # never the token
        return f"RawSeeder({self._base!r}, course={self.course_id})"

    def _fenced(self, method: str, path: str, params: dict[str, Any]) -> None:
        c, s = self.course_id, self.student_id
        m = re.fullmatch(rf"/api/v1/courses/{c}/assignments/(\d+)/submissions(?:/{s})?", path)
        if method == "POST" and m and path.endswith("/submissions"):
            assert_recorded(self.recorded, int(m.group(1)))
            if str(params.get("as_user_id")) != str(s):
                raise ScopeViolation("raw submit must masquerade as the test student only")
            return
        if method == "PUT" and m and path.endswith(f"/submissions/{s}"):
            assert_recorded(self.recorded, int(m.group(1)))
            return
        if method == "GET" and path == "/health_check.json":
            return
        raise ScopeViolation(f"raw client refuses {method} {path}")

    def _call(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, data: Any = None
    ) -> httpx.Response:
        params = params or {}
        self._fenced(method, path, params)
        headers = {} if path == "/health_check.json" else {"Authorization": f"Bearer {self._token}"}
        return self._http.request(
            method, self._base + path, params=params, data=data, headers=headers
        )

    def submit_text(self, assignment_id: int, body: str) -> str:
        path = f"/api/v1/courses/{self.course_id}/assignments/{assignment_id}/submissions"
        r = self._call(
            "POST",
            path,
            params={"as_user_id": self.student_id},
            data={"submission[submission_type]": "online_text_entry", "submission[body]": body},
        )
        if r.status_code in (200, 201):
            return "submission (as_user_id)"
        # Fallback: a teacher comment on the test student. The guard counts comments as work.
        r2 = self._call(
            "PUT",
            f"{path}/{self.student_id}",
            data={"comment[text_comment]": f"{PREFIX} seeded comment"},
        )
        if r2.status_code not in (200, 201):
            raise Refused(
                f"could not seed a submission (submit {r.status_code}, comment {r2.status_code})"
            )
        return f"comment only (submit as_user_id failed with HTTP {r.status_code})"

    def grade(self, assignment_id: int, score: float) -> None:
        path = f"/api/v1/courses/{self.course_id}/assignments/{assignment_id}/submissions"
        r = self._call(
            "PUT", f"{path}/{self.student_id}", data={"submission[posted_grade]": str(score)}
        )
        if r.status_code not in (200, 201):
            raise Refused(f"could not grade the test student (HTTP {r.status_code})")

    def version(self) -> str:
        try:
            r = self._call("GET", "/health_check.json")
            body = r.json() if r.status_code == 200 else {}
            keys = ("canvas_build", "revision", "version", "canvas_version")
            found = {k: body[k] for k in keys if k in body}
            return json.dumps(found or {"http": r.status_code})
        except (httpx.HTTPError, ValueError) as e:
            return f"unknown ({type(e).__name__})"


# ---- state file and cleanup ---------------------------------------------------------------


def save_state(path: Path, created: dict[int, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"assignments": {str(k): v for k, v in created.items()}}))


def load_state(path: Path) -> dict[int, str]:
    raw = json.loads(path.read_text())
    return {int(k): str(v) for k, v in raw.get("assignments", {}).items()}


def check_no_stale_state(path: Path) -> None:
    """A state file left by a crashed run means orphaned suite assignments: clean up first."""
    if path.exists():
        raise Refused(
            f"refusing: {path} exists (an earlier run did not finish); "
            "run `python -m tests.integration.sandbox cleanup` first"
        )


@dataclass
class CleanupResult:
    deleted: list[int] = field(default_factory=list[int])
    gone: list[int] = field(default_factory=list[int])  # confirmed 404 / already deleted
    failed: dict[int, str] = field(default_factory=dict[int, str])  # still need cleaning

    @property
    def unresolved(self) -> dict[int, str]:
        return self.failed


def cleanup(
    client: SuiteClient, created: dict[int, str], log: Callable[[str], None]
) -> CleanupResult:
    """Delete recorded suite assignments (prefix verified live).

    Only a 404 (or a ``deleted`` workflow state) counts as already gone; any other error
    leaves the id in ``failed`` so its state-file entry is kept. A misnamed or unrecorded id
    raises ``Refused`` (nothing further is deleted).

    Their test-student submissions go with them: that is the suite's own data. This is the one
    place in the repo that deletes an assignment with a submission, by design and for suite
    objects only.
    """
    res = CleanupResult()
    for aid in sorted(created):
        assert_recorded(client.recorded, aid)
        base = f"/courses/{client.course_id}/assignments/{aid}"
        try:
            live = client.get_one(base)
        except CanvasAPIError as e:
            if e.status == 404:
                res.gone.append(aid)
                log(f"assignment {aid}: already gone (404)")
            else:
                res.failed[aid] = f"read failed: HTTP {e.status}"
                log(f"NOT CLEANED assignment {aid}: read failed with HTTP {e.status}")
            continue
        except CanvasSyncError as e:
            res.failed[aid] = f"read failed: {type(e).__name__}"
            log(f"NOT CLEANED assignment {aid}: read failed ({type(e).__name__})")
            continue
        assert_suite_name(live.get("name"), aid)
        if live.get("workflow_state") == "deleted":
            res.gone.append(aid)
            continue
        try:
            client.delete(base)
        except CanvasSyncError as e:
            res.failed[aid] = f"delete failed: {type(e).__name__}"
            log(f"NOT CLEANED assignment {aid}: delete failed ({type(e).__name__})")
            continue
        res.deleted.append(aid)
        log(f"deleted assignment {aid}")
    return res


def settle_state(state: Path, created: dict[int, str], res: CleanupResult) -> None:
    """Keep only unresolved ids in the state file; remove it when all are resolved. Raises
    ``Refused`` (loudly) if anything is left."""
    if not res.failed:
        state.unlink(missing_ok=True)
        return
    save_state(state, {i: created[i] for i in res.failed})
    ids = ", ".join(str(i) for i in sorted(res.failed))
    raise Refused(
        f"CLEANUP INCOMPLETE: assignments {ids} remain in the sandbox course; "
        f"state kept in {state}; run `python -m tests.integration.sandbox cleanup`"
    )


# ---- the harness --------------------------------------------------------------------------

_SUB_FIELDS = (
    "user_id", "workflow_state", "attempt", "submission_type", "body", "url", "submitted_at",
    "score", "entered_score", "grade", "entered_grade", "excused",
)  # fmt: skip


def normalize_submission(row: dict[str, Any]) -> dict[str, Any]:
    """The fields that must never change; comments and attachments by id and text."""
    out: dict[str, Any] = {k: row.get(k) for k in _SUB_FIELDS}
    out["comments"] = sorted(
        (str(c.get("id")), c.get("comment")) for c in row.get("submission_comments") or []
    )
    out["attachments"] = sorted(str(a.get("id")) for a in row.get("attachments") or [])
    return out


@dataclass
class Harness:
    """One temp clone of the sandbox course scoped to the suite's own assignments."""

    make_client: Callable[[Literal["read", "write"], set[int], set[int]], CanvasClient]
    seeder_factory: Callable[[int, set[int]], Seeder]
    url: str
    course_id: int
    workdir: Path
    on_record: Callable[[int, str], None] = lambda _i, _n: None
    log: Callable[[str], None] = print
    check_course: Callable[[CanvasClient], None] = lambda _c: None
    recorded: set[int] = field(default_factory=set[int])
    students: set[int] = field(default_factory=set[int])  # the test student, once found
    created: dict[int, str] = field(default_factory=dict[int, str])
    ids: dict[str, int] = field(default_factory=dict[str, int])
    student_id: int = 0
    baseline: dict[int, dict[str, Any]] = field(default_factory=dict[int, dict[str, Any]])
    notes: dict[str, Any] = field(default_factory=dict[str, Any])
    seed_methods: dict[str, str] = field(default_factory=dict[str, str])
    root: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root = self.workdir / "course"
        self.read = self.make_client("read", self.recorded, self.students)
        self.write = self.make_client("write", self.recorded, self.students)

    # -- Canvas helpers --------------------------------------------------------------------

    def _list_subs(self, aid: int, **extra: Any) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"include[]": ["submission_comments"], **extra}
        rows = self.read.get(f"/courses/{self.course_id}/assignments/{aid}/submissions", params)
        return list(rows)

    def snapshot(self, aid: int) -> dict[str, Any]:
        """The test student's submission on ``aid``: the single GET, else the list row."""
        base = f"/courses/{self.course_id}/assignments/{aid}/submissions"
        try:
            row = self.read.get_one(
                f"{base}/{self.student_id}", {"include[]": ["submission_comments"]}
            )
        except Exception:
            rows = [r for r in self._list_subs(aid) if r.get("user_id") == self.student_id]
            if not rows:
                raise Refused(f"cannot read the test student's submission on {aid}") from None
            row = rows[0]
        return normalize_submission(row)

    def assignment(self, key: str) -> dict[str, Any] | None:
        try:
            return self.read.get_one(f"/courses/{self.course_id}/assignments/{self.ids[key]}")
        except CanvasSyncError:
            return None

    def find_test_student(self) -> int:
        """The Student View student; its enrollment must be StudentViewEnrollment."""
        rows = self.read.get(
            f"/courses/{self.course_id}/users",
            {"enrollment_type[]": ["student_view"], "include[]": ["enrollments", "test_student"]},
        )
        rows = [
            r
            for r in rows
            if r.get("is_test_student")
            or any(e.get("type") == "StudentViewEnrollment" for e in r.get("enrollments") or [])
        ]
        if len(rows) == 1:
            return int(rows[0]["id"])
        if len(rows) > 1:
            raise Refused("more than one Student View student found")
        raise Refused(
            "no Student View test student in the course: open Student View once "
            "(course Home > Student View) and run again"
        )

    # -- setup -----------------------------------------------------------------------------

    def create_assignment(self, key: str, **fields: Any) -> int:
        name = f"{PREFIX}-{key}"
        body: dict[str, Any] = {
            "name": name,
            "submission_types": ["online_text_entry"],
            "points_possible": 10,
            "published": True,
            "description": f"<p>{PREFIX} test assignment; safe to delete.</p>",
            **fields,
        }
        assert isinstance(self.write, SuiteClient)
        row = self.write.create_assignment(body)
        aid = int(row["id"])
        self.recorded.add(aid)
        self.created[aid] = name
        self.ids[key] = aid
        self.on_record(aid, name)
        self.log(f"created assignment {aid} ({name})")
        return aid

    def seed(self) -> None:
        """Create the four suite assignments, seed test-student work, record baselines."""
        self.check_course(self.read)
        self.student_id = self.find_test_student()
        self.students.add(self.student_id)
        due = "2027-12-31T07:59:00Z"
        self.create_assignment("ungraded", due_at=due)
        self.create_assignment("graded", due_at=due)
        self.create_assignment("access")  # no base due, so it can become overrides-only
        self.create_assignment("empty", due_at=due)
        seeder = self.seeder_factory(self.student_id, self.recorded)
        self.notes["canvas_version"] = seeder.version()
        for key in ("ungraded", "graded", "access"):
            self.seed_methods[key] = seeder.submit_text(self.ids[key], f"{PREFIX} test text")
        seeder.grade(self.ids["graded"], 8)
        self.probe_questions()
        for key in ("ungraded", "graded", "access"):
            self.baseline[self.ids[key]] = self.snapshot(self.ids[key])
            assert (
                self.baseline[self.ids[key]]["comments"]
                or self.baseline[self.ids[key]]["submission_type"]
                or self.baseline[self.ids[key]]["score"] is not None
            ), f"seeding {key} left no work"
        self.notes["seed_methods"] = dict(self.seed_methods)

    def probe_questions(self) -> None:
        """Record the answers to the two NOTES questions (counts only, no student data)."""
        c, aid = self.course_id, self.ids["ungraded"]
        default = self._list_subs(aid)
        every = self._list_subs(aid, **{"student_ids[]": ["all"]})
        ts = self.student_id
        enr = self.read.get(
            f"/courses/{c}/enrollments",
            {"state[]": ["inactive", "completed"], "type[]": ["StudentEnrollment"]},
        )
        off = {int(e["user_id"]) for e in enr}
        self.notes["q1_submissions_listing"] = {
            "default_rows": len(default),
            "all_rows": len(every),
            "default_includes_test_student": any(r.get("user_id") == ts for r in default),
            "all_includes_test_student": any(r.get("user_id") == ts for r in every),
            "inactive_or_concluded_student_enrollments": len(off),
            "inactive_or_concluded_in_default_rows": sum(r.get("user_id") in off for r in default),
            "inactive_or_concluded_in_all_rows": sum(r.get("user_id") in off for r in every),
            "single_get_works": self._single_get_ok(aid),
            "has_submitted_submissions_flag": bool(
                (self.assignment("ungraded") or {}).get("has_submitted_submissions")
            ),
        }
        # Q2: one student in two overrides of the same assignment. Probed on 'empty'.
        eaid = self.ids["empty"]
        base = f"/courses/{c}/assignments/{eaid}"
        made: list[int] = []
        verdict = "accepted"
        try:
            for title in ("a", "b"):
                row = self.write.post(
                    f"{base}/overrides",
                    json={
                        "assignment_override": {"student_ids": [ts], "title": f"{PREFIX} {title}"}
                    },
                )
                made.append(int(row["id"]))
        except CanvasSyncError as e:
            verdict = f"rejected second override: {type(e).__name__} {str(e)[:100]}"
        finally:
            for oid in made:
                self.write.delete(f"{base}/overrides/{oid}")
        self.notes["q2_student_in_two_overrides"] = verdict

    def _single_get_ok(self, aid: int) -> bool:
        try:
            self.read.get_one(
                f"/courses/{self.course_id}/assignments/{aid}/submissions/{self.student_id}"
            )
            return True
        except Exception:
            return False

    # -- the clone -------------------------------------------------------------------------

    def canvasignore(self) -> str:
        """Ignore every managed object except the suite's own assignments."""
        return (
            "# canvas-sync safety suite: only zz-canvas-sync-safety-* assignments are managed\n"
            "/pages/\n/announcements/\n/files/\n/modules.yaml\n/discussions/\n"
            f"/assignments/*\n!/assignments/{PREFIX}-*\n"
        )

    def clone(self) -> None:
        root = self.root
        root.mkdir(parents=True)
        self._git("init", "--quiet", "--initial-branch=main")
        (root / ".canvasignore").write_text(self.canvasignore(), encoding="utf-8")
        cache = RosterCache(students={TEST_STUDENT_REF: self.student_id})
        roster.write_cache(root, cache)  # untracked
        config = CourseConfig.model_validate(
            {"canvas_url": self.url, "course_id": self.course_id, "timezone": TIMEZONE}
        )
        run_fetch(root, self.read, config)
        if not gitops.fast_forward(root):
            raise Refused("could not check out canvas/main in the temp clone")
        (root / ".gitignore").write_text(f"{roster.CACHE_PATH}\n{roster.ALIASES_PATH}\n")
        self.commit("suite: .gitignore")
        self.config = load_config(root)
        self.minimal_roster()

    def minimal_roster(self) -> None:
        """Replace the fetched roster cache (it lists real students) with the test student
        alone, under the `unknown:<id>` ref that fetch writes for it. Never printed."""
        cache = RosterCache(students={f"unknown:{self.student_id}": self.student_id})
        roster.write_cache(self.root, cache)

    def _git(self, *args: str) -> str:
        cfg = [
            "-c",
            "user.name=suite",
            "-c",
            "user.email=suite@localhost",
            "-c",
            "commit.gpgsign=false",
        ]
        p = subprocess.run(
            ["git", "-C", str(self.root), *cfg, *args], capture_output=True, text=True, check=False
        )
        if p.returncode != 0:
            raise Refused(f"git {args[0]} failed: {p.stderr.strip()[:200]}")
        return p.stdout

    def commit(self, msg: str) -> None:
        self._git("add", "-A")
        self._git("commit", "--quiet", "--allow-empty", "-m", msg)

    def path(self, key: str) -> str:
        return f"assignments/{PREFIX}-{key}.md"

    def suite_files(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.root))
            for p in (self.root / "assignments").glob(f"{PREFIX}-*.md")
        )

    def edit(self, key: str, old: str, new: str) -> None:
        p = self.root / self.path(key)
        text = p.read_text(encoding="utf-8")
        if old not in text:
            raise Refused(f"{self.path(key)}: expected {old!r} in the file")
        p.write_text(text.replace(old, new, 1), encoding="utf-8")

    # -- pushing ---------------------------------------------------------------------------

    def plan(self) -> Plan:
        head, main = gitops.head_commit(self.root), gitops.ref_commit(self.root)
        _, ignore = committed_inputs(self.root, self.config)
        return build_plan(self.root, ignore, head=head, main=main).plan

    def push(self, **kw: Any) -> PushResult:
        """check_scope first (offline), then the real ``run_push``. Commit before calling."""
        check_scope(self.plan(), self.student_id)
        self.minimal_roster()
        kw.setdefault("yes", True)
        buf = io.StringIO()
        con = Console(file=buf, width=200, force_terminal=False)
        self.last_output = ""
        try:
            return run_push(self.root, self.write, self.config, console=con, **kw)
        finally:
            self.last_output = buf.getvalue()

    last_output: str = ""

    @contextmanager
    def scenario(self) -> Iterator[None]:
        """Roll the clone back to its start unless the push really changed Canvas; then check
        every seeded submission against its baseline."""
        start = gitops.head_commit(self.root)
        main_before = gitops.ref_commit(self.root)
        try:
            yield
        finally:
            if gitops.ref_commit(self.root) == main_before and start:
                self._git("reset", "--hard", "--quiet", start)
            self.assert_submissions_unchanged()

    def assert_submissions_unchanged(self) -> None:
        for aid, before in self.baseline.items():
            now = self.snapshot(aid)
            assert now == before, f"submission on assignment {aid} changed: {before} -> {now}"

    def finish(self, state_file: Path | None = None) -> CleanupResult:
        """Delete the suite's assignments; keep the state file for any that could not be."""
        assert isinstance(self.write, SuiteClient)
        res = cleanup(self.write, self.created, self.log)
        if state_file is not None:
            settle_state(state_file, self.created, res)
        return res


def new_workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="canvas-safety-"))


# ---- state file location and manual cleanup CLI -------------------------------------------


def state_path(env: dict[str, str]) -> Path:
    default = Path.home() / ".canvas-sync" / "safety-suite-state.json"
    return (
        Path(env["CANVAS_SAFETY_STATE"]).expanduser() if env.get("CANVAS_SAFETY_STATE") else default
    )


def main(argv: list[str]) -> int:
    """``python -m tests.integration.sandbox cleanup``: delete leftovers recorded in the state
    file of an interrupted run (same guards as the suite's own cleanup)."""
    import os

    if argv != ["cleanup"]:
        print("usage: python -m tests.integration.sandbox cleanup")
        return 2
    env = dict(os.environ)
    url, course = check_env(env)
    path = state_path(env)
    if not path.exists():
        print(f"nothing to clean: {path} does not exist")
        return 0
    created = load_state(path)
    recorded = set(created)
    c = SuiteClient(url, env["CANVAS_TOKEN"], course_id=course, mode="write", recorded=recorded)
    try:
        res = cleanup(c, created, print)
    finally:
        c.close()
    try:
        settle_state(path, created, res)
    except Refused as e:
        print(e)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
