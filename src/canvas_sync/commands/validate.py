"""canvas validate."""

from __future__ import annotations

from typing import Annotated

import typer


def validate(
    fix: Annotated[bool, typer.Option("--fix", help="Normalize files to canonical form")] = False,
) -> None:
    """Offline checks: frontmatter, references, dates, roster names."""
    raise NotImplementedError
