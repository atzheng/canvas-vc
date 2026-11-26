"""Run a Plan against Canvas (CONTRACTS.md section 9)."""

from __future__ import annotations

from dataclasses import dataclass

from canvas_sync.model.base import ObjectKey
from canvas_sync.plan.core import Operation, Plan
from canvas_sync.remote.context import ApplyContext
from canvas_sync.remote.http import CanvasClient


@dataclass
class ApplyResult:
    done: list[Operation]
    failed: Operation | None
    error: Exception | None
    new_ids: dict[ObjectKey, int | str]  # to write back into the tree


def apply(
    plan: Plan, client: CanvasClient, ctx: ApplyContext, *, confirmed: set[str]
) -> ApplyResult:
    """Stop at the first failure; never retry a write that is not idempotent."""
    raise NotImplementedError
