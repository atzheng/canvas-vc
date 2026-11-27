"""Operation, Plan, Risk and ordering (CONTRACTS.md section 9).

Type differs plug in through ``DIFFER_MODULES``: each ``plan/<type>.py``
exposes ``diff_type(base, target) -> list[Operation]`` and is imported lazily
by name, so type beads never edit this file. ``diff`` also accepts an explicit
list of differs (used by tests).
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from rich.console import Group, RenderableType
from rich.text import Text

from canvas_sync.model.base import CanvasObject, Kind, ObjectKey
from canvas_sync.model.course import Course


class Action(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    UNPUBLISH = "unpublish"
    UPLOAD = "upload"
    REORDER = "reorder"


class RiskKind(StrEnum):
    HAS_SUBMISSIONS = "has_submissions"
    GRADING_CHANGE = "grading_change"
    LATE_POLICY_REGRADE = "late_policy_regrade"
    ACCESS_REMOVED = "access_removed"
    POSTED_ANNOUNCEMENT = "posted_announcement"
    FILE_ID_CHANGE = "file_id_change"
    NOTIFIES_STUDENTS = "notifies_students"  # announcement posted now: needs --confirm-notify


@dataclass(frozen=True)
class Risk:
    kind: RiskKind
    message: str
    needs_confirm: str | None  # flag name required to proceed, e.g. "--confirm-grading-change"


@dataclass(frozen=True)
class Operation:
    key: ObjectKey
    action: Action
    before: CanvasObject | None
    after: CanvasObject | None
    changes: dict[str, tuple[Any, Any]]  # field -> (old, new), for display
    risks: tuple[Risk, ...] = ()
    touches_assignment: bool = False  # module ops: published/item change on an assignment item


Differ = Callable[[Course, Course], list[Operation]]

# Differ modules in apply order (CONTRACTS.md section 9). The assignments
# differ covers assignment groups, assignments and their overrides; the
# modules differ covers modules, then module items.
DIFFER_MODULES: tuple[str, ...] = (
    "canvas_sync.plan.files",
    "canvas_sync.plan.pages",
    "canvas_sync.plan.assignments",
    "canvas_sync.plan.announcements",
    "canvas_sync.plan.modules",
)

META_PREFIX = ".canvas/"


def default_differs() -> list[Differ]:
    return [importlib.import_module(name).diff_type for name in DIFFER_MODULES]


def changes_between(
    before: CanvasObject, after: CanvasObject, *, exclude: Iterable[str] = ("canvas_id",)
) -> dict[str, tuple[Any, Any]]:
    """Helper for differs: ``{field: (old, new)}`` for fields that differ.

    Absent and ``None`` are the same thing in a model (CONTRACTS.md section 5),
    so a cleared date shows up as ``(old, None)``; writers must send an
    explicit null for it.
    """
    skip = set(exclude)
    out: dict[str, tuple[Any, Any]] = {}
    for name in type(after).model_fields:
        if name in skip:
            continue
        old, new = getattr(before, name, None), getattr(after, name, None)
        if old != new:
            out[name] = (old, new)
    return out


def _is_meta(path: str) -> bool:
    return path == META_PREFIX.rstrip("/") or path.startswith(META_PREFIX)


def _without_meta(course: Course) -> Course:
    if not any(_is_meta(k.path) for k in course.objects):
        return course
    objs = {k: v for k, v in course.objects.items() if not _is_meta(k.path)}
    return dataclasses.replace(course, objects=objs)


@dataclass
class Plan:
    ops: list[Operation]  # in apply order

    def render(self) -> RenderableType:
        """One line per operation, in the SPEC example style, plus risk lines."""
        if not self.ops:
            return Text("Nothing to push.")
        noun_w = max(len(_noun(op)) for op in self.ops)
        label_w = max(len(_label(op.key)) for op in self.ops)
        lines: list[Text] = []
        for op in self.ops:
            sym, style = _SYMBOL[op.action]
            line = Text()
            line.append(f"{sym} ", style=style)
            line.append(f"{_noun(op):<{noun_w}}  ")
            line.append(f"{_label(op.key):<{label_w}}", style="bold")
            summary = _changes_text(op)
            if summary:
                line.append(f"  {summary}")
            line.append(f"  ({op.key.path})", style="dim")
            lines.append(line)
            for risk in op.risks:
                note = Text(f"    ! {risk.message}", style="red")
                if risk.needs_confirm:
                    note.append(f" (needs {risk.needs_confirm})")
                lines.append(note)
        return Group(*lines)

    def is_empty(self) -> bool:
        return not self.ops


def diff(base: Course, target: Course, differs: Sequence[Differ] | None = None) -> Plan:
    """base = canvas/main tree, target = HEAD tree.

    Concatenates the per-type differs in apply order. DELETE operations are
    moved to the end, in reverse order, so dependants go before what they
    depend on. Paths under ``.canvas/`` are ignored.
    """
    base, target = _without_meta(base), _without_meta(target)
    ops: list[Operation] = []
    deletes: list[Operation] = []
    for differ in default_differs() if differs is None else differs:
        for op in differ(base, target):
            if _is_meta(op.key.path):
                continue
            (deletes if op.action is Action.DELETE else ops).append(op)
    ops.extend(reversed(deletes))
    return Plan(ops)


def unconfirmed_risks(plan: Plan, confirmed: set[str]) -> list[tuple[Operation, Risk]]:
    """Risks that need a confirmation flag that was not given.

    A flag counts as given if ``confirmed`` has the bare flag name
    (``--confirm-grading-change``) or the flag scoped to the object's tree path
    (``--confirm-grading-change assignments/hw1.md``).
    """
    out: list[tuple[Operation, Risk]] = []
    for op in plan.ops:
        for risk in op.risks:
            flag = risk.needs_confirm
            if flag is None or flag in confirmed or f"{flag} {op.key.path}" in confirmed:
                continue
            out.append((op, risk))
    return out


_SYMBOL = {
    Action.CREATE: ("+", "green"),
    Action.UPLOAD: ("+", "green"),
    Action.UPDATE: ("~", "yellow"),
    Action.REORDER: ("~", "yellow"),
    Action.DELETE: ("-", "red"),
    Action.UNPUBLISH: ("-", "red"),
}
_NOUN = {
    "Override": "override",
    "ModuleItem": "item",
    "PlacedItem": "item",
    "AssignmentGroup": "group",
}


def _noun(op: Operation) -> str:
    obj = op.after or op.before
    if obj is not None and type(obj).__name__ in _NOUN:
        return _NOUN[type(obj).__name__]
    return op.key.kind.value


def _label(key: ObjectKey) -> str:
    if key.kind is Kind.MODULE and "#" in key.path:
        return key.path.split("#", 1)[1]
    if key.kind is Kind.FILE:
        return key.path.removeprefix("files/")
    name = key.path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _fmt(v: Any) -> str:
    if v is None:
        return "(none)"
    if hasattr(v, "strftime"):
        return str(v.strftime("%Y-%m-%d %H:%M"))
    return str(v)


def _short(v: Any, n: int = 30) -> str:
    s = _fmt(v).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "\u2026"


def _change_text(field: str, old: Any, new: Any) -> str:
    long = len(_fmt(new)) > 40 or "\n" in _fmt(new) or len(_fmt(old)) > 40 or "\n" in _fmt(old)
    if long:
        return f"{field} {_short(old)} \u2192 {_short(new)}"
    return f"{field} {_fmt(new)}"


def _changes_text(op: Operation) -> str:
    if op.action is Action.UNPUBLISH:
        return "unpublish"
    if op.action is Action.DELETE:
        return ""
    return "  ".join(_change_text(f, old, new) for f, (old, new) in op.changes.items())
