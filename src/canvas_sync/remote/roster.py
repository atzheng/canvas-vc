"""Roster reader (CONTRACTS.md section 8). Read-only: no create/update/delete."""

from __future__ import annotations

from canvas_sync.model.roster import Roster
from canvas_sync.remote.context import FetchContext
from canvas_sync.remote.http import CanvasClient


def read(client: CanvasClient, ctx: FetchContext) -> Roster:
    raise NotImplementedError
