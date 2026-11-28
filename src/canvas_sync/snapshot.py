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


def _course_yaml(root: Path, course: Course, dumped: bytes, prev: str | None) -> bytes:
    """Reuse an existing course.yaml's bytes when it parses to the same config.

    ``dump_tree`` writes only non-default fields and drops comments, so using its output
    would make merges show a spurious course.yaml diff. Source, in order: the previous
    canvas/main (so a comment-only edit by the user never makes snapshot() see a change);
    on the first snapshot, the user's HEAD; else the canonical dump.
    """
    commit = prev or gitops.head_commit(root)
    if commit is not None:
        old = gitops.read_file_at(root, commit, CONFIG_PATH)
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

    course.yaml bytes are reused from the previous canvas/main (first snapshot: HEAD)
    when they parse to the same config; see ``_course_yaml``.
    """
    prev = gitops.ref_commit(root)
    dumped = dump_tree(course)
    files: dict[str, bytes | BlobRef] = dict(dumped)
    files[CONFIG_PATH] = _course_yaml(root, course, dumped[CONFIG_PATH], prev)
    files[BODIES_PATH] = bodies.dump()

    # blobs missing from the store fall back to the previous snapshot (e.g. fresh machine,
    # file not re-downloaded); read only those files from it, in one call
    missing = {
        p for p, e in course.files_meta.files.items() if e.sha256 and not blobs.has(e.sha256)
    }
    prev_files = gitops.read_tree_files(root, prev, missing) if prev is not None and missing else {}
    for path, entry in course.files_meta.files.items():
        if not entry.sha256:
            continue
        if path in missing:
            old = prev_files.get(path)
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
