""".canvasignore matching (CONTRACTS.md section 11)."""

from __future__ import annotations

from pathlib import Path


class IgnoreRules:
    @classmethod
    def load(cls, root: Path) -> IgnoreRules:
        raise NotImplementedError

    def ignored(self, path: str) -> bool:
        raise NotImplementedError

    def add(self, path: str) -> None:
        raise NotImplementedError
