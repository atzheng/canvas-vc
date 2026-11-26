"""Git plumbing via subprocess (CONTRACTS.md section 11)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from canvas_sync.model.course import Course

REMOTE_REF = "refs/remotes/canvas/main"


@dataclass(frozen=True)
class BlobRef:
    """A file in a tree whose contents live in the BlobStore."""

    sha256: str


def repo_root(start: Path) -> Path:
    raise NotImplementedError


def head_commit(root: Path) -> str | None:
    raise NotImplementedError


def ref_commit(root: Path, ref: str = REMOTE_REF) -> str | None:
    raise NotImplementedError


def write_tree(root: Path, files: Mapping[str, bytes | BlobRef]) -> str:
    raise NotImplementedError


def commit_tree(root: Path, tree: str, parents: list[str], message: str) -> str:
    raise NotImplementedError


def update_ref(root: Path, ref: str, new: str, old: str | None) -> None:
    raise NotImplementedError


def is_ancestor(root: Path, a: str, b: str) -> bool:
    raise NotImplementedError


def unmerged_paths(root: Path) -> list[str]:
    raise NotImplementedError


def conflict_marker_paths(root: Path) -> list[str]:
    raise NotImplementedError


def is_clean(root: Path) -> bool:
    raise NotImplementedError


def read_tree_files(root: Path, commit: str) -> dict[str, bytes]:
    raise NotImplementedError


def load_course_at(root: Path, commit: str) -> Course:
    raise NotImplementedError


def merge(root: Path, ref: str = REMOTE_REF, rebase: bool = False) -> int:
    raise NotImplementedError


def fast_forward(root: Path, ref: str = REMOTE_REF) -> bool:
    raise NotImplementedError
