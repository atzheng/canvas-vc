"""Run a Plan against Canvas (CONTRACTS.md section 9).

Writers are the ``remote/<type>.py`` modules (``create``/``update``/``delete``,
CONTRACTS.md section 8), found by ``op.key.kind`` through ``WRITER_MODULES``,
so type beads plug in without editing this file. ``apply`` accepts an explicit
``writers`` mapping (used by tests).

Action -> writer call:

- CREATE, or UPLOAD with no ``before``: ``create(client, after, ctx)`` -> new id
- UPDATE, UNPUBLISH, REORDER, or UPLOAD with a ``before``:
  ``update(client, before, after, ctx)``
- DELETE: ``delete(client, before, ctx)``

Writers must send an explicit null for any field in ``op.changes`` whose new
value is None (a cleared date).
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, cast

from canvas_sync.errors import GuardBlocked
from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.plan.core import Action, Operation, Plan, unconfirmed_risks
from canvas_sync.remote.context import ApplyContext
from canvas_sync.remote.http import CanvasClient


class Writer(Protocol):
    """Shape of a ``remote/<type>.py`` module (or any object with these)."""

    def create(self, client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> int | str: ...

    def update(
        self, client: CanvasClient, before: CanvasObject, after: CanvasObject, ctx: ApplyContext
    ) -> None: ...

    def delete(self, client: CanvasClient, obj: CanvasObject, ctx: ApplyContext) -> None: ...


WRITER_MODULES: dict[Kind, str] = {
    Kind.PAGE: "canvas_sync.remote.pages",
    Kind.ASSIGNMENT: "canvas_sync.remote.assignments",
    Kind.ANNOUNCEMENT: "canvas_sync.remote.announcements",
    Kind.FILE: "canvas_sync.remote.files",
    Kind.MODULE: "canvas_sync.remote.modules",
}


def needs_guard(op: Operation) -> bool:
    """Operations that go through the guard: assignment operations, module
    operations that touch an assignment item, and announcement creates/updates
    (defence in depth for the notify-students risk; the check uses no network)."""
    if op.key.kind is Kind.ANNOUNCEMENT:
        return op.action in (Action.CREATE, Action.UPDATE)
    return op.key.kind is Kind.ASSIGNMENT or op.touches_assignment


@dataclass
class ApplyResult:
    done: list[Operation]
    failed: Operation | None
    error: Exception | None
    new_ids: dict[ObjectKey, int | str] = field(
        default_factory=dict[ObjectKey, int | str]
    )  # to write back


def _writer(kind: Kind, writers: Mapping[Kind, Writer] | None) -> Writer:
    if writers is not None:
        return writers[kind]
    return cast(Writer, importlib.import_module(WRITER_MODULES[kind]))


def _run(op: Operation, w: Writer, client: CanvasClient, ctx: ApplyContext) -> int | str | None:
    if op.action is Action.DELETE:
        if op.before is None:
            raise ValueError(f"delete of {op.key.path} has no 'before' object")
        w.delete(client, op.before, ctx)
        return None
    if op.after is None:
        raise ValueError(f"{op.action} of {op.key.path} has no 'after' object")
    if op.action is Action.CREATE or (op.action is Action.UPLOAD and op.before is None):
        return w.create(client, op.after, ctx)
    if op.before is None:
        raise ValueError(f"{op.action} of {op.key.path} has no 'before' object")
    w.update(client, op.before, op.after, ctx)
    return None


def apply(
    plan: Plan,
    client: CanvasClient,
    ctx: ApplyContext,
    *,
    confirmed: set[str],
    writers: Mapping[Kind, Writer] | None = None,
) -> ApplyResult:
    """Stop at the first failure; never retry a write that is not idempotent.

    Raises ``GuardBlocked`` before any write if a risk needs a confirmation
    flag that is not in ``confirmed`` (all are listed), or if the guard
    refuses any guarded operation in the pre-flight pass. After that, any exception from the
    guard or a writer ends the run and is returned in ``ApplyResult.error``,
    with the operations already done and the ids of objects already created.
    A failed operation is never re-sent: the HTTP client retries only what is
    safe, and a create that errored may or may not have happened, so the next
    fetch decides.
    """
    missing = unconfirmed_risks(plan, confirmed)
    if missing:
        raise GuardBlocked(
            "; ".join(f"{o.key.path}: {r.message} (needs {r.needs_confirm})" for o, r in missing)
        )
    # Pre-flight: nothing is written if the guard refuses any operation.
    for op in plan.ops:
        if needs_guard(op):
            ctx.guard.assert_allowed(op, confirmed)

    result = ApplyResult(done=[], failed=None, error=None)
    for op in plan.ops:
        try:
            if needs_guard(op):  # just-in-time, for live state
                ctx.guard.assert_allowed(op, confirmed)
            before_id = ctx.ids.get(op.key)
            new_id = _run(op, _writer(op.key.kind, writers), client, ctx)
            if new_id is None:  # an update may change the id (a renamed page's slug)
                changed = ctx.ids.get(op.key)
                if changed is not None and changed != before_id and op.action is not Action.DELETE:
                    new_id = changed
        except Exception as e:
            result.failed, result.error = op, e
            return result
        if new_id is not None:
            ctx.ids[op.key] = new_id
            result.new_ids[op.key] = new_id
        result.done.append(op)
    return result
