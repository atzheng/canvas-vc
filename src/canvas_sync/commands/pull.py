"""canvas pull."""

from __future__ import annotations

from typing import Annotated

import typer


def pull(
    rebase: Annotated[bool, typer.Option("--rebase", help="Rebase instead of merge")] = False,
) -> None:
    """Fetch, then merge (or rebase onto) canvas/main."""
    raise NotImplementedError
