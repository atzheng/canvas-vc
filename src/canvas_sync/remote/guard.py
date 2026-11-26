"""Submission guard (CONTRACTS.md section 10)."""

from __future__ import annotations

from canvas_sync.config import CourseConfig
from canvas_sync.plan.core import Operation, Risk
from canvas_sync.remote.http import CanvasClient


class SubmissionGuard:
    def __init__(self, client: CanvasClient, config: CourseConfig) -> None:
        self.client = client
        self.config = config

    def check(self, op: Operation) -> list[Risk]:
        """Re-fetch the assignment live and report submission-related risks."""
        raise NotImplementedError

    def assert_allowed(self, op: Operation, confirmed: set[str]) -> None:
        """Raises GuardBlocked."""
        raise NotImplementedError
