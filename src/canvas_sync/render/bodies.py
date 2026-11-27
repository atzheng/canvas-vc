""".canvas/bodies.json: {tree_path: {"html_sha256": str, "md_sha256": str}}."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

BODIES_FILE = ".canvas/bodies.json"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Bodies:
    def __init__(self, entries: dict[str, dict[str, str]] | None = None) -> None:
        self.entries: dict[str, dict[str, str]] = entries if entries is not None else {}

    @classmethod
    def load(cls, root: Path) -> Bodies:
        """Read ``root/.canvas/bodies.json``; missing or unreadable means empty."""
        try:
            raw = json.loads((root / BODIES_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        entries: dict[str, dict[str, str]] = {}
        for path, rec in cast("dict[str, Any]", raw).items():
            if not isinstance(rec, dict):
                continue
            h = cast("dict[str, Any]", rec).get("html_sha256")
            m = cast("dict[str, Any]", rec).get("md_sha256")
            if isinstance(h, str) and isinstance(m, str):
                entries[path] = {"html_sha256": h, "md_sha256": m}
        return cls(entries)

    def record(self, path: str, md: str, canvas_html: str) -> None:
        self.entries[path] = {"html_sha256": _sha(canvas_html), "md_sha256": _sha(md)}

    def matches(self, path: str, canvas_html: str) -> bool:
        """Remote HTML unchanged since our push?"""
        rec = self.entries.get(path)
        return rec is not None and rec["html_sha256"] == _sha(canvas_html)

    def md_matches(self, path: str, md: str) -> bool:
        """Is ``md`` the Markdown recorded for ``path`` (fetch sanity check)?"""
        rec = self.entries.get(path)
        return rec is not None and rec["md_sha256"] == _sha(md)

    def forget(self, path: str) -> None:
        self.entries.pop(path, None)

    def dump(self) -> bytes:
        """Canonical JSON, sorted keys."""
        text = json.dumps(self.entries, sort_keys=True, indent=2, ensure_ascii=False)
        return (text + "\n").encode("utf-8")
