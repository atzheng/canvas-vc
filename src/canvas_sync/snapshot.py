"""Course -> commit on canvas/main (CONTRACTS.md section 11)."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from canvas_sync import gitops
from canvas_sync.config import CourseConfig
from canvas_sync.errors import CanvasSyncError, ValidationError
from canvas_sync.gitops import BlobRef
from canvas_sync.model.course import Course, dump_tree
from canvas_sync.model.serialize import load_yaml
from canvas_sync.render.bodies import Bodies

BODIES_PATH = ".canvas/bodies.json"
CONFIG_PATH = "course.yaml"


class BlobStore:
    """Content-addressed file store in ``.git/canvas-blobs/`` (shared by all worktrees)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, sha256: str) -> Path:
        return gitops.blob_path(self.root, sha256)

    def has(self, sha256: str) -> bool:
        return self.path_for(sha256).is_file()

    def put(self, content: bytes) -> str:
        sha = hashlib.sha256(content).hexdigest()
        dest = self.path_for(sha)
        if dest.is_file():
            return sha
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            os.replace(tmp, dest)  # atomic: readers never see a partial blob
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha

    def get(self, sha256: str) -> bytes:
        """Raises FileNotFoundError if absent."""
        return self.path_for(sha256).read_bytes()


def _config_of(data: bytes) -> CourseConfig | None:
    try:
        raw = load_yaml(data.decode("utf-8"), CONFIG_PATH)
        return CourseConfig.model_validate(raw)
    except (UnicodeDecodeError, ValidationError, PydanticValidationError):
        return None


def _course_yaml(root: Path, course: Course, dumped: bytes, candidates: list[str]) -> bytes:
    """Keep the user's hand-edited course.yaml when it parses to the same config.

    ``dump_tree`` writes only non-default fields and drops comments, so using its output
    would make every merge show a spurious course.yaml diff (and could replace the user's
    comments). The canonical dump is used only when no candidate commit has a course.yaml
    with an equal parsed config.
    """
    for commit in candidates:
        try:
            old = gitops.read_tree_files(root, commit).get(CONFIG_PATH)
        except gitops.GitError:
            continue
        if old is not None and _config_of(old) == course.config:
            return old
    return dumped


def snapshot(
    root: Path,
    course: Course,
    bodies: Bodies,
    blobs: BlobStore,
    message: str,
    extra_parent: str | None = None,
) -> str | None:
    """Commit ``course`` to ``refs/remotes/canvas/main``; returns the commit id.

    Returns None, creating nothing, when the tree equals canvas/main's and there is no
    ``extra_parent``. Never touches the work tree or the user's index.

    course.yaml comes from the user's side where possible (HEAD, then the previous
    canvas/main) when it parses to the same config; see ``_course_yaml``.
    """
    prev = gitops.ref_commit(root)
    dumped = dump_tree(course)
    files: dict[str, bytes | BlobRef] = dict(dumped)
    candidates = [c for c in (gitops.head_commit(root), prev) if c]
    files[CONFIG_PATH] = _course_yaml(root, course, dumped[CONFIG_PATH], candidates)
    files[BODIES_PATH] = bodies.dump()

    prev_files: dict[str, bytes] | None = None
    for path, entry in course.files_meta.files.items():
        if not entry.sha256:
            continue
        if not blobs.has(entry.sha256):
            # fall back to the previous snapshot (e.g. fresh machine, file not re-downloaded)
            if prev is not None and prev_files is None:
                prev_files = gitops.read_tree_files(root, prev)
            old = (prev_files or {}).get(path)
            if old is None or hashlib.sha256(old).hexdigest() != entry.sha256:
                raise CanvasSyncError(f"{path}: contents not in the blob store; fetch it again")
            blobs.put(old)
        files[path] = BlobRef(entry.sha256)

    tree = gitops.write_tree(root, files)
    if extra_parent is None and prev is not None and gitops.tree_of(root, prev) == tree:
        return None
    parents = [p for p in (prev, extra_parent) if p]
    if len(parents) == 2 and parents[0] == parents[1]:
        parents = parents[:1]
    commit = gitops.commit_tree(root, tree, parents, message)
    gitops.update_ref(root, gitops.REMOTE_REF, commit, prev)
    return commit
