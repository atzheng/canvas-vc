"""Exception hierarchy (CONTRACTS.md section 9)."""

from __future__ import annotations


class CanvasSyncError(Exception):
    """Base class for errors the CLI shows as one line with exit code 1."""


class ConfigError(CanvasSyncError): ...


class ValidationError(CanvasSyncError): ...


class ForbiddenEndpoint(CanvasSyncError): ...


class GuardBlocked(CanvasSyncError): ...


class StaleRemote(CanvasSyncError): ...


class UnresolvedConflicts(CanvasSyncError): ...


class NotFastForward(CanvasSyncError): ...


class DirtyTree(CanvasSyncError): ...


class CanvasAPIError(CanvasSyncError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Canvas API error {status}: {body}")
        self.status = status
        self.body = body
