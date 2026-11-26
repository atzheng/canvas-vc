"""canvas push."""

from __future__ import annotations

from typing import Annotated

import typer


def push(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan only")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt")] = False,
    allow_dirty: Annotated[bool, typer.Option("--allow-dirty", help="Allow a dirty tree")] = False,
    no_delete: Annotated[
        bool, typer.Option("--no-delete", help="Turn deletions into unpublishes")
    ] = False,
) -> None:
    """Apply the local changes to Canvas."""
    raise NotImplementedError
