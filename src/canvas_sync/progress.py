"""Progress reporting shared by fetch and push (CONTRACTS.md section 1a).

Reporting only: callers never change what they request or return based on it.
``NullProgress`` is the default everywhere; ``rich_progress`` renders on stderr.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager
from typing import Protocol

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.progress import (
    Progress as RichBar,
)


class Progress(Protocol):
    def phase(self, name: str, total: int | None = None) -> None:
        """Start a phase (``total`` items, None when unknown); ends the previous one."""

    def advance(self, n: int = 1, label: str | None = None) -> None:
        """``n`` items of the current phase are done; ``label`` names the latest one."""

    def bytes(self, done: int, total: int) -> None:
        """Cumulative bytes transferred in the current phase, of ``total`` expected."""


class NullProgress:
    def phase(self, name: str, total: int | None = None) -> None:
        pass

    def advance(self, n: int = 1, label: str | None = None) -> None:
        pass

    def bytes(self, done: int, total: int) -> None:
        pass


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # unreachable


class RichProgress:
    """Renders phases as rich bars (count, elapsed, ETA; bytes in the phase line)."""

    def __init__(self, console: Console) -> None:
        self._bar = RichBar(
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.fields[bytes]}"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        )
        self._task: TaskID | None = None
        self._name = ""

    def __enter__(self) -> RichProgress:
        self._bar.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._bar.stop()

    def phase(self, name: str, total: int | None = None) -> None:
        if self._task is not None:
            self._bar.remove_task(self._task)
        self._name = name
        self._task = self._bar.add_task(name, total=total, bytes="")

    def advance(self, n: int = 1, label: str | None = None) -> None:
        if self._task is not None:
            desc = f"{self._name}: {label}" if label else self._name
            self._bar.update(self._task, advance=n, description=desc)

    def bytes(self, done: int, total: int) -> None:
        if self._task is not None:
            self._bar.update(self._task, bytes=f"{_human(done)}/{_human(total)}")


@contextmanager
def rich_progress(quiet: bool = False) -> Generator[Progress]:
    """A renderer on stderr when stderr is a TTY and not ``quiet``, else ``NullProgress``."""
    if quiet or not sys.stderr.isatty():
        yield NullProgress()
        return
    with RichProgress(Console(stderr=True)) as p:
        yield p
