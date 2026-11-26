"""Plan core and apply engine, with dummy differs and writers (no real types)."""

from __future__ import annotations

import dataclasses
import io
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from rich.console import Console

from canvas_sync.apply import ApplyResult, apply
from canvas_sync.config import CourseConfig
from canvas_sync.errors import CanvasAPIError, GuardBlocked
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course
from canvas_sync.model.files import FilesMeta
from canvas_sync.model.page import Page
from canvas_sync.model.roster import Roster, RosterCache
from canvas_sync.plan.core import (
    Action,
    Operation,
    Plan,
    Risk,
    RiskKind,
    changes_between,
    diff,
)
from canvas_sync.remote.context import ApplyContext
from canvas_sync.remote.http import CanvasClient

CFG = CourseConfig.model_validate(
    {"canvas_url": "https://canvas.example.edu", "course_id": 1, "timezone": "UTC"}
)


def course(objs: dict[ObjectKey, CanvasObject] | None = None) -> Course:
    return Course(CFG, objs or {}, FilesMeta(), [], Roster())


def key(kind: Kind, path: str) -> ObjectKey:
    return ObjectKey(kind, path)


def op(kind: Kind, path: str, action: Action, risks: tuple[Risk, ...] = ()) -> Operation:
    obj = Page(title=path)
    return Operation(
        key(kind, path),
        action,
        None if action in (Action.CREATE, Action.UPLOAD) else obj,
        None if action is Action.DELETE else obj,
        {},
        risks,
    )


class FakeGuard:
    def __init__(self) -> None:
        self.seen: list[str] = []
        self.block: str | None = None
        self.block_on_call: int | None = None

    def assert_allowed(self, o: Operation, confirmed: set[str]) -> None:
        self.seen.append(o.key.path)
        if o.key.path == self.block or len(self.seen) == self.block_on_call:
            raise GuardBlocked("blocked")


class FakeWriter:
    def __init__(self, log: list[str], fail_on: str | None = None) -> None:
        self.log, self.fail_on, self.n = log, fail_on, 100

    def _maybe_fail(self, title: str) -> None:
        if title == self.fail_on:
            raise CanvasAPIError(500, "boom")

    def create(self, client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> int | str:
        title = cast(Page, obj).title
        self.log.append(f"create {title}")
        self._maybe_fail(title)
        self.n += 1
        return self.n

    def update(
        self, client: CanvasClient, before: CanvasObject, after: CanvasObject, ctx: ApplyContext
    ) -> None:
        title = cast(Page, after).title
        self.log.append(f"update {title}")
        self._maybe_fail(title)

    def delete(self, client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> None:
        title = cast(Page, obj).title
        self.log.append(f"delete {title}")
        self._maybe_fail(title)


def make_ctx(guard: FakeGuard) -> ApplyContext:
    return ApplyContext(
        config=CFG,
        tz=ZoneInfo("UTC"),
        roster=RosterCache(),
        ids={},
        guard=cast(Any, guard),
        blobs=cast(Any, None),
    )


def run(
    plan: Plan, *, confirmed: set[str] | None = None, fail_on: str | None = None
) -> tuple[ApplyResult, list[str], FakeGuard]:
    log: list[str] = []
    guard = FakeGuard()
    w = FakeWriter(log, fail_on)
    writers = {k: w for k in Kind}
    res = apply(
        plan, cast(Any, None), make_ctx(guard), confirmed=confirmed or set(), writers=writers
    )
    return res, log, guard


def render_text(plan: Plan) -> str:
    console = Console(width=200, file=io.StringIO(), record=True, color_system=None)
    console.print(plan.render())
    return console.export_text()


# ---- diff ----


def test_diff_concatenates_in_order_and_moves_deletes_last_reversed() -> None:
    def files(b: Course, t: Course) -> list[Operation]:
        return [
            op(Kind.FILE, "files/a.pdf", Action.DELETE),
            op(Kind.FILE, "files/b.pdf", Action.UPLOAD),
        ]

    def pages(b: Course, t: Course) -> list[Operation]:
        return [
            op(Kind.PAGE, "pages/p.md", Action.UPDATE),
            op(Kind.PAGE, "pages/q.md", Action.DELETE),
        ]

    def mods(b: Course, t: Course) -> list[Operation]:
        return [
            op(Kind.MODULE, "modules.yaml#M", Action.CREATE),
            op(Kind.MODULE, "modules.yaml#N", Action.DELETE),
        ]

    plan = diff(course(), course(), [files, pages, mods])
    assert [(o.key.path, o.action) for o in plan.ops] == [
        ("files/b.pdf", Action.UPLOAD),
        ("pages/p.md", Action.UPDATE),
        ("modules.yaml#M", Action.CREATE),
        ("modules.yaml#N", Action.DELETE),
        ("pages/q.md", Action.DELETE),
        ("files/a.pdf", Action.DELETE),
    ]


def test_diff_ignores_canvas_meta_paths() -> None:
    seen: list[list[str]] = []

    def differ(b: Course, t: Course) -> list[Operation]:
        seen.append([k.path for k in t.objects])
        return [
            op(Kind.PAGE, ".canvas/state.md", Action.CREATE),
            op(Kind.PAGE, "pages/a.md", Action.CREATE),
        ]

    t = course({key(Kind.PAGE, ".canvas/x.md"): Page(), key(Kind.PAGE, "pages/a.md"): Page()})
    plan = diff(course(), t, [differ])
    assert [o.key.path for o in plan.ops] == ["pages/a.md"]
    assert seen == [["pages/a.md"]]


def test_empty_plan() -> None:
    plan = diff(course(), course(), [lambda b, t: []])
    assert plan.is_empty()
    assert "Nothing" in render_text(plan)


def test_changes_between_treats_cleared_date_as_none_change() -> None:
    old = Page(title="x", publish_at=datetime(2026, 9, 1, 9, 0))
    new = Page(title="x", publish_at=None)
    assert changes_between(old, new) == {"publish_at": (old.publish_at, None)}
    assert changes_between(old, old) == {}


# ---- render ----


def test_render_style() -> None:
    risk = Risk(RiskKind.NOTIFIES_STUDENTS, "emails students now", "--confirm-notify")
    ops = [
        Operation(
            key(Kind.ASSIGNMENT, "assignments/hw2.md"),
            Action.UPDATE,
            Page(),
            Page(),
            {"due": (None, datetime(2026, 9, 23, 23, 59))},
        ),
        Operation(
            key(Kind.ANNOUNCEMENT, "announcements/hi.md"), Action.CREATE, None, Page(), {}, (risk,)
        ),
        op(Kind.PAGE, "pages/old.md", Action.DELETE),
    ]
    out = render_text(Plan(ops)).splitlines()
    assert out[0].startswith("~ assignment")
    assert "hw2" in out[0] and "due 2026-09-23 23:59" in out[0]
    assert out[1].startswith("+ announcement")
    assert "! emails students now (needs --confirm-notify)" in out[2]
    assert out[3].startswith("- page")


# ---- apply ----


def test_apply_runs_in_order_and_records_ids() -> None:
    plan = Plan(
        [
            op(Kind.PAGE, "pages/a.md", Action.CREATE),
            op(Kind.PAGE, "pages/b.md", Action.UPDATE),
            op(Kind.FILE, "files/c.pdf", Action.UPLOAD),
            op(Kind.PAGE, "pages/d.md", Action.DELETE),
        ]
    )
    res, log, _ = run(plan)
    assert res.error is None and res.failed is None
    assert log == [
        "create pages/a.md",
        "update pages/b.md",
        "create files/c.pdf",
        "delete pages/d.md",
    ]
    assert res.new_ids == {
        key(Kind.PAGE, "pages/a.md"): 101,
        key(Kind.FILE, "files/c.pdf"): 102,
    }
    assert len(res.done) == 4


def test_partial_failure_stops_and_never_retries() -> None:
    plan = Plan(
        [
            op(Kind.PAGE, "pages/a.md", Action.CREATE),
            op(Kind.PAGE, "pages/b.md", Action.CREATE),
            op(Kind.PAGE, "pages/c.md", Action.CREATE),
        ]
    )
    res, log, _ = run(plan, fail_on="pages/b.md")
    assert log == ["create pages/a.md", "create pages/b.md"]  # b sent once, c never
    assert isinstance(res.error, CanvasAPIError)
    assert res.failed is plan.ops[1]
    assert res.done == [plan.ops[0]]
    assert res.new_ids == {key(Kind.PAGE, "pages/a.md"): 101}


def test_unconfirmed_risk_blocks_before_any_write() -> None:
    risk = Risk(RiskKind.NOTIFIES_STUDENTS, "emails students", "--confirm-notify")
    plan = Plan(
        [
            op(Kind.PAGE, "pages/a.md", Action.CREATE),
            op(Kind.ANNOUNCEMENT, "announcements/n.md", Action.CREATE, (risk,)),
        ]
    )
    log: list[str] = []
    with pytest.raises(GuardBlocked):
        apply(
            plan,
            cast(Any, None),
            make_ctx(FakeGuard()),
            confirmed=set(),
            writers={k: FakeWriter(log) for k in Kind},
        )
    assert log == []


@pytest.mark.parametrize("flag", ["--confirm-notify", "--confirm-notify announcements/n.md"])
def test_confirmed_risk_proceeds(flag: str) -> None:
    risk = Risk(RiskKind.NOTIFIES_STUDENTS, "emails students", "--confirm-notify")
    plan = Plan([op(Kind.ANNOUNCEMENT, "announcements/n.md", Action.CREATE, (risk,))])
    res, log, _ = run(plan, confirmed={flag})
    assert res.error is None and log == ["create announcements/n.md"]


def test_risk_with_no_flag_does_not_block() -> None:
    risk = Risk(RiskKind.POSTED_ANNOUNCEMENT, "FYI", None)
    res, _, _ = run(Plan([op(Kind.PAGE, "pages/a.md", Action.UPDATE, (risk,))]))
    assert res.error is None


def _guard_plan() -> Plan:
    mod = dataclasses.replace(
        op(Kind.MODULE, "modules.yaml#M", Action.UPDATE), touches_assignment=True
    )
    return Plan(
        [
            op(Kind.PAGE, "pages/a.md", Action.CREATE),
            op(Kind.ASSIGNMENT, "assignments/hw1.md", Action.DELETE),
            mod,
            op(Kind.MODULE, "modules.yaml#N", Action.UPDATE),
        ]
    )


def test_guard_preflight_blocks_with_nothing_written() -> None:
    log: list[str] = []
    guard = FakeGuard()
    guard.block = "assignments/hw1.md"
    with pytest.raises(GuardBlocked):
        apply(
            _guard_plan(),
            cast(Any, None),
            make_ctx(guard),
            confirmed=set(),
            writers={k: FakeWriter(log) for k in Kind},
        )
    assert log == []


def test_guard_runs_twice_for_guarded_ops_only() -> None:
    log: list[str] = []
    guard = FakeGuard()
    res = apply(
        _guard_plan(),
        cast(Any, None),
        make_ctx(guard),
        confirmed=set(),
        writers={k: FakeWriter(log) for k in Kind},
    )
    assert res.error is None
    # hw1 (assignment) and module M (touches an assignment); module N is not guarded
    assert guard.seen == ["assignments/hw1.md", "modules.yaml#M"] * 2


def test_guard_just_in_time_block_returned_not_raised() -> None:
    log: list[str] = []
    guard = FakeGuard()
    guard.block_on_call = 3  # passes pre-flight, refuses the live check of hw1
    plan = _guard_plan()
    res = apply(
        plan,
        cast(Any, None),
        make_ctx(guard),
        confirmed=set(),
        writers={k: FakeWriter(log) for k in Kind},
    )
    assert isinstance(res.error, GuardBlocked)
    assert res.failed is plan.ops[1]
    assert log == ["create pages/a.md"]


def test_all_missing_confirmations_listed() -> None:
    r1 = Risk(RiskKind.GRADING_CHANGE, "points change", "--confirm-grading-change")
    r2 = Risk(RiskKind.NOTIFIES_STUDENTS, "emails students", "--confirm-notify")
    plan = Plan(
        [
            op(Kind.ASSIGNMENT, "assignments/a.md", Action.UPDATE, (r1,)),
            op(Kind.ANNOUNCEMENT, "announcements/n.md", Action.CREATE, (r2,)),
        ]
    )
    with pytest.raises(GuardBlocked) as e:
        apply(plan, cast(Any, None), make_ctx(FakeGuard()), confirmed=set(), writers={})
    assert "--confirm-grading-change" in str(e.value) and "--confirm-notify" in str(e.value)


def test_render_long_values_truncated_old_to_new() -> None:
    o = Operation(
        key(Kind.PAGE, "pages/a.md"),
        Action.UPDATE,
        Page(),
        Page(),
        {"body": ("o" * 50, "n" * 50)},
    )
    out = render_text(Plan([o]))
    assert "body " + "o" * 30 + "\u2026 \u2192 " + "n" * 30 + "\u2026" in out


def test_ctx_ids_filled_for_later_ops() -> None:
    seen: dict[ObjectKey, int | str] = {}

    class Peek(FakeWriter):
        def update(
            self,
            client: CanvasClient,
            before: CanvasObject,
            after: CanvasObject,
            ctx: ApplyContext,
        ) -> None:
            seen.update(ctx.ids)

    plan = Plan(
        [
            op(Kind.PAGE, "pages/a.md", Action.CREATE),
            op(Kind.MODULE, "modules.yaml#M", Action.UPDATE),
        ]
    )
    apply(
        plan,
        cast(Any, None),
        make_ctx(FakeGuard()),
        confirmed=set(),
        writers={Kind.PAGE: FakeWriter([]), Kind.MODULE: Peek([])},
    )
    assert seen == {key(Kind.PAGE, "pages/a.md"): 101}
