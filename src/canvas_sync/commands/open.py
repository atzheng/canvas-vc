"""canvas open."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer


def open_(
    path: Annotated[Path, typer.Argument(help="Tree path of the object")],
) -> None:
    """Open the object on Canvas."""
    raise NotImplementedError
