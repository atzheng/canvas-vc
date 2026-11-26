""".canvas/bodies.json: {tree_path: {"html_sha256": str, "md_sha256": str}}."""

from __future__ import annotations

from pathlib import Path


class Bodies:
    @classmethod
    def load(cls, root: Path) -> Bodies:
        raise NotImplementedError

    def record(self, path: str, md: str, canvas_html: str) -> None:
        raise NotImplementedError

    def matches(self, path: str, canvas_html: str) -> bool:
        """Remote HTML unchanged since our push?"""
        raise NotImplementedError

    def dump(self) -> bytes:
        """Canonical JSON, sorted keys."""
        raise NotImplementedError
