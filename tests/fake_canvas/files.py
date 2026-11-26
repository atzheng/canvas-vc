"""FakeCanvas behaviour for files. Reader and writer beads fill this in."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.fake_canvas.core import FakeCanvas


def register(fake: FakeCanvas) -> None:
    """Add this resource's request handlers to `fake.handlers`."""
