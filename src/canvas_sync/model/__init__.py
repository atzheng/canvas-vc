"""Registers every TypeSpec. Type beads never edit this file (CONTRACTS.md section 4)."""

from __future__ import annotations

from canvas_sync.model.announcement import ANNOUNCEMENT_SPEC
from canvas_sync.model.assignment import ASSIGNMENT_SPEC
from canvas_sync.model.base import Kind, TypeSpec
from canvas_sync.model.files import FILE_SPEC
from canvas_sync.model.modules import MODULE_SPEC
from canvas_sync.model.page import PAGE_SPEC

REGISTRY: dict[Kind, TypeSpec] = {
    Kind.PAGE: PAGE_SPEC,
    Kind.ASSIGNMENT: ASSIGNMENT_SPEC,
    Kind.ANNOUNCEMENT: ANNOUNCEMENT_SPEC,
    Kind.FILE: FILE_SPEC,
    Kind.MODULE: MODULE_SPEC,
}
