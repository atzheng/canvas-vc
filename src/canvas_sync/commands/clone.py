"""canvas clone."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer


def clone(
    url: Annotated[str, typer.Argument(help="Canvas base URL")],
    course_id: Annotated[int, typer.Argument(help="Canvas course id")],
    directory: Annotated[Path | None, typer.Argument(help="Target directory")] = None,
) -> None:
    """Create the repo, fetch, and check out main."""
    raise NotImplementedError
