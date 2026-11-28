"""Git plumbing via subprocess (CONTRACTS.md section 11).

Safety rules for this module:

- every call is an argument list (never a shell) run as ``git -C <root>``;
- inherited ``GIT_DIR``/``GIT_INDEX_FILE``/``GIT_WORK_TREE`` etc. are dropped, so a
  call can never be redirected at another repository or the user's index;
- trees are built in a throwaway index file (``GIT_INDEX_FILE``), so the user's
  index and working tree are never touched;
- only :func:`merge` and :func:`fast_forward` change the working tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from canvas_sync.errors import CanvasSyncError

if TYPE_CHECKING:
    from canvas_sync.model.course import Course

REMOTE_REF = "refs/remotes/canvas/main"
BLOB_DIR = "canvas-blobs"

_DEFAULT_IDENT = {
    "GIT_AUTHOR_NAME": "canvas-sync",
    "GIT_AUTHOR_EMAIL": "canvas-sync@localhost",
    "GIT_COMMITTER_NAME": "canvas-sync",
    "GIT_COMMITTER_EMAIL": "canvas-sync@localhost",
}
_STRIPPED_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)
_FILE_MODE = "100644"


class GitError(CanvasSyncError):
    """A git command failed."""


class RefMoved(GitError):
    """compare-and-swap on a ref failed because the ref no longer has the expected value."""


@dataclass(frozen=True)
class BlobRef:
    """A file in a tree whose contents live in the BlobStore (``.git/canvas-blobs/``)."""

    sha256: str


def _env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_MERGE_AUTOEDIT"] = "no"
    if extra:
        env.update(extra)
    return env


def _run(
    root: Path,
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    ok: Sequence[int] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        input=stdin,
        capture_output=True,
        env=_env(env),
        check=False,
    )
    if proc.returncode not in ok:
        msg = proc.stderr.decode(errors="replace").strip().splitlines()
        raise GitError(f"git {args[0]} failed: {msg[-1] if msg else proc.returncode}")
    return proc


def _out(
    root: Path,
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> str:
    return _run(root, args, env=env, stdin=stdin).stdout.decode().strip()


def repo_root(start: Path) -> Path:
    """Top level of the work tree containing ``start``."""
    proc = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        env=_env(),
        check=False,
    )
    if proc.returncode != 0:
        raise GitError(f"{start}: not inside a git repository")
    return Path(proc.stdout.decode().strip())


def git_common_dir(root: Path) -> Path:
    """The shared ``.git`` directory (same for all worktrees)."""
    p = Path(_out(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]))
    return p


def blob_dir(root: Path) -> Path:
    return git_common_dir(root) / BLOB_DIR


_SHA256 = re.compile(r"[0-9a-f]{64}")


def blob_path(root: Path, sha256: str) -> Path:
    if not _SHA256.fullmatch(sha256):
        raise ValueError(f"not a sha256 hex digest: {sha256!r}")
    return blob_dir(root) / sha256[:2] / sha256


def _rev(root: Path, rev: str) -> str | None:
    proc = _run(root, ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], ok=(0, 1))
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


def head_commit(root: Path) -> str | None:
    return _rev(root, "HEAD")


def ref_commit(root: Path, ref: str = REMOTE_REF) -> str | None:
    return _rev(root, ref)


def tree_of(root: Path, commit: str) -> str:
    return _out(root, ["rev-parse", "--verify", f"{commit}^{{tree}}"])


def _hash_blob(root: Path, data: bytes) -> str:
    return _out(root, ["hash-object", "-w", "--no-filters", "--stdin"], stdin=data)


def write_tree(root: Path, files: Mapping[str, bytes | BlobRef]) -> str:
    """Write blobs and nested trees for ``files`` (tree path -> content); returns the tree id.

    Uses a temporary index, so the repository's own index and work tree are untouched.
    """
    entries: list[bytes] = []
    for path in sorted(files):
        if not path or path.startswith("/") or ".." in path.split("/") or "\0" in path:
            raise ValueError(f"bad tree path: {path!r}")
        content = files[path]
        if isinstance(content, BlobRef):
            src = blob_path(root, content.sha256)
            try:
                content = src.read_bytes()
            except FileNotFoundError:
                raise GitError(
                    f"{path}: blob {content.sha256[:12]} missing from blob store"
                ) from None
        sha = _hash_blob(root, content)
        entries.append(f"{_FILE_MODE} {sha}\t{path}".encode() + b"\0")
    with tempfile.TemporaryDirectory(prefix="canvas-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _run(
            root,
            ["update-index", "--add", "-z", "--index-info"],
            env=env,
            stdin=b"".join(entries),
        )
        return _out(root, ["write-tree"], env=env)


def commit_tree(root: Path, tree: str, parents: list[str], message: str) -> str:
    args = ["commit-tree", tree]
    for p in parents:
        args += ["-p", p]
    ident = {k: v for k, v in _DEFAULT_IDENT.items() if k not in os.environ}
    return _out(root, args, env=ident, stdin=message.encode() + b"\n")


def update_ref(root: Path, ref: str, new: str, old: str | None) -> None:
    """Compare-and-swap: set ``ref`` to ``new`` only if it currently equals ``old``
    (``None``: only if it does not exist). Raises :class:`RefMoved` otherwise."""
    expected = old if old is not None else "0" * len(new)
    proc = _run(
        root,
        ["update-ref", "-m", "canvas-sync", ref, new, expected],
        ok=tuple(range(256)),
    )
    if proc.returncode == 0:
        return
    err = proc.stderr.decode(errors="replace").strip()
    if "but expected" in err or "reference already exists" in err or "unable to resolve" in err:
        raise RefMoved(f"{ref} changed while it was being updated; try again")
    raise GitError(f"git update-ref failed: {err.splitlines()[-1] if err else proc.returncode}")


def read_file_at(root: Path, commit: str, path: str) -> bytes | None:
    """Contents of one file in a commit, or None if the commit has no such file."""
    if not _run(root, ["ls-tree", "-z", commit, "--", path]).stdout:
        return None
    return _run(root, ["cat-file", "blob", f"{commit}:{path}"]).stdout


def is_ancestor(root: Path, a: str, b: str) -> bool:
    return _run(root, ["merge-base", "--is-ancestor", a, b], ok=(0, 1)).returncode == 0


def unmerged_paths(root: Path) -> list[str]:
    out = _run(root, ["ls-files", "-u", "-z"]).stdout.decode()
    paths = {rec.split("\t", 1)[1] for rec in out.split("\0") if "\t" in rec}
    return sorted(paths)


def conflict_marker_paths(root: Path) -> list[str]:
    """Tracked text files whose work-tree content has a conflict marker line."""
    proc = _run(
        root,
        ["grep", "-I", "-l", "-z", "-E", "^(<<<<<<< |>>>>>>> )"],
        ok=(0, 1),
    )
    return sorted(p for p in proc.stdout.decode().split("\0") if p)


def is_clean(root: Path) -> bool:
    """No staged or unstaged changes to tracked files. Untracked files are not counted."""
    return not _run(
        root, ["--no-optional-locks", "status", "--porcelain", "--untracked-files=no"]
    ).stdout.strip()


def _tree_blobs(root: Path, commit: str) -> list[tuple[str, str]]:
    """(path, blob id) of every regular blob in ``commit``'s tree (symlinks skipped)."""
    listing = _run(root, ["ls-tree", "-r", "-z", "--full-tree", commit]).stdout.decode()
    out: list[tuple[str, str]] = []
    for rec in listing.split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind == "blob" and mode != "120000":
            out.append((path, sha))
    return out


def _read_blobs(root: Path, wanted: Sequence[tuple[str, str]]) -> dict[str, bytes]:
    if not wanted:
        return {}
    data = _run(
        root, ["cat-file", "--batch"], stdin="".join(f"{s}\n" for _, s in wanted).encode()
    ).stdout
    out: dict[str, bytes] = {}
    pos = 0
    for path, _ in wanted:
        end = data.index(b"\n", pos)
        _sha, _type, size = data[pos:end].decode().split()
        start = end + 1
        out[path] = data[start : start + int(size)]
        pos = start + int(size) + 1
    return out


def read_tree_files(
    root: Path, commit: str, paths: Collection[str] | None = None
) -> dict[str, bytes]:
    """Contents of the regular files in ``commit`` (only ``paths`` when given)."""
    wanted = [(p, s) for p, s in _tree_blobs(root, commit) if paths is None or p in paths]
    return _read_blobs(root, wanted)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
SHA_CACHE = "canvas-sha256-cache.json"
_CHUNK = 1 << 20


def _sha_cache_path(root: Path) -> Path:
    return Path(_out(root, ["rev-parse", "--path-format=absolute", "--git-path", SHA_CACHE]))


def _load_sha_cache(path: Path) -> dict[str, str]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    items = cast("dict[object, object]", raw)
    return {
        k: v
        for k, v in items.items()
        if isinstance(k, str) and isinstance(v, str) and _SHA256_RE.fullmatch(v)
    }


def _save_sha_cache(path: Path, cache: Mapping[str, str]) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:  # a cache that cannot be written is only a slower next run
        tmp.unlink(missing_ok=True)


def _stream_blob_sha256(root: Path, blob: str) -> str:
    """sha256 of one blob's contents, streamed from ``git cat-file`` in chunks."""
    h = hashlib.sha256()
    proc = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "blob", blob],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
    )
    assert proc.stdout is not None and proc.stderr is not None
    while chunk := proc.stdout.read(_CHUNK):
        h.update(chunk)
    err = proc.stderr.read()
    if proc.wait() != 0:
        lines = err.decode(errors="replace").strip().splitlines()
        raise GitError(f"git cat-file failed: {lines[-1] if lines else proc.returncode}")
    return h.hexdigest()


def load_course_at(
    root: Path, commit: str, skip_files: Callable[[str], bool] | None = None
) -> Course:
    """``load_tree`` over a commit, without reading ``files/**`` blobs more than once.

    Only non-``files/`` blobs and ``files/_meta.yaml`` are written to a temp dir. Each other
    ``files/`` blob gets its sha256 from a cache keyed by git blob id (``.git/
    canvas-sha256-cache.json``; a miss streams that blob and records it). ``skip_files``
    (tree path -> bool) omits files whose bytes need not be read (e.g. ignored paths).
    """
    from canvas_sync.model.course import load_tree

    small: list[tuple[str, str]] = []
    big: list[tuple[str, str]] = []
    for path, sha in _tree_blobs(root, commit):
        if path.startswith("files/") and path != "files/_meta.yaml":
            if path.rsplit("/", 1)[-1] != ".DS_Store" and not (skip_files and skip_files(path)):
                big.append((path, sha))
        else:
            small.append((path, sha))
    cache_path = _sha_cache_path(root)
    cache = _load_sha_cache(cache_path)
    dirty = False
    hashes: dict[str, str] = {}
    for path, blob in big:
        digest = cache.get(blob)
        if digest is None:
            digest = cache[blob] = _stream_blob_sha256(root, blob)
            dirty = True
        hashes[path] = digest
    if dirty:
        _save_sha_cache(cache_path, cache)
    with tempfile.TemporaryDirectory(prefix="canvas-tree-") as tmp:
        base = Path(tmp)
        for path, content in _read_blobs(root, small).items():
            dest = base / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        return load_tree(base, file_hashes=hashes)


def merge_with_output(root: Path, ref: str = REMOTE_REF, rebase: bool = False) -> tuple[int, str]:
    """:func:`merge`, also returning git's message: the first non-empty stderr line, else the
    last stdout line, else ``""`` (for a one-line report)."""
    args = ["rebase", ref] if rebase else ["merge", "--no-edit", "--allow-unrelated-histories", ref]
    proc = _run(root, args, ok=tuple(range(256)))
    err = [ln.strip() for ln in proc.stderr.decode(errors="replace").splitlines() if ln.strip()]
    out = [ln.strip() for ln in proc.stdout.decode(errors="replace").splitlines() if ln.strip()]
    msg = err[0] if err else out[-1] if out else ""
    # git lists the files it refuses to overwrite as tab-indented lines.
    listed = [
        ln.strip()
        for ln in proc.stderr.decode(errors="replace").splitlines()
        if ln.startswith(("\t", " " * 4)) and ln.strip()
    ]
    if listed:
        msg += f" [{', '.join(listed)}]"
    return proc.returncode, msg


def operation_in_progress(root: Path) -> str | None:
    """``"merge"`` or ``"rebase"`` if one is unfinished in this work tree, else None."""
    for name, what in (
        ("MERGE_HEAD", "merge"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
    ):
        if Path(_out(root, ["rev-parse", "--path-format=absolute", "--git-path", name])).exists():
            return what
    return None


def merge(root: Path, ref: str = REMOTE_REF, rebase: bool = False) -> int:
    """Run ``git merge`` (or ``rebase``) of ``ref`` in the work tree; returns the exit code.

    This is the user's own merge: it runs their hooks and honours their commit signing
    settings. (``commit_tree``, used for canvas/main snapshots, runs neither.)

    A conflict gives a non-zero code and leaves markers in the work tree, as git does.
    Unrelated histories are allowed for merge: the first fetch creates a root commit
    on canvas/main.
    """
    return merge_with_output(root, ref, rebase)[0]


def fast_forward(root: Path, ref: str = REMOTE_REF) -> bool:
    """``git merge --ff-only``; False (and nothing changed) if not possible."""
    if ref_commit(root, ref) is None:
        return False
    return _run(root, ["merge", "--ff-only", ref], ok=(0, 1, 128)).returncode == 0


def tree_with_carried_files(
    root: Path,
    new: str,
    old: str,
    managed: Collection[str] = (),
    preserve: Collection[str] = (),
) -> str | None:
    """Tree of commit ``new`` with some of the files of commit ``old`` put back.

    - files of ``old`` that ``new`` has no place for (paths in neither ``new`` nor
      ``managed``): ``.gitignore``, a README;
    - files in ``preserve`` that both commits have: ``old``'s blob wins, so content the
      user wrote (comments, formatting) survives although the paths are semantically equal.

    None if the result would equal ``new``'s tree. Built in a temporary index; the work
    tree and the user's index are untouched."""

    def listing(commit: str) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        for rec in _run(root, ["ls-tree", "-r", "-z", "--full-tree", commit]).stdout.split(b"\0"):
            if b"\t" not in rec:
                continue
            meta, path = rec.split(b"\t", 1)
            mode, kind, sha = meta.decode().split()
            if kind == "blob":
                out[path.decode()] = (mode, sha)
        return out

    have, was = listing(new), listing(old)
    keep: list[bytes] = []
    for path, (mode, sha) in was.items():
        if path in have:
            if path not in preserve or have[path][1] == sha:
                continue
        elif path in managed:
            continue
        keep.append(f"{mode} {sha}\t{path}".encode() + b"\0")
    if not keep:
        return None
    with tempfile.TemporaryDirectory(prefix="canvas-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _run(root, ["read-tree", new], env=env)
        _run(root, ["update-index", "--add", "-z", "--index-info"], env=env, stdin=b"".join(keep))
        return _out(root, ["write-tree"], env=env)


def restore_local_files(root: Path, before: str, ref: str = REMOTE_REF) -> bool:
    """After ``git rebase ref`` of a branch that was at ``before``: put back the local-only
    files (``.gitignore``, a README ...) that the rebase dropped.

    A rebase flattens merge commits, and ``canvas push`` carries those files in one. A file
    counts as local-only when ``before`` has it, HEAD lacks it, and it is not in the
    merge base of ``before`` and ``ref`` (so a file Canvas deleted stays deleted). They are
    added in a follow-up commit on top of HEAD. True if a commit was made; False if there
    was nothing to restore or git refused (e.g. an untracked file in the way)."""
    head = head_commit(root)
    base = _run(root, ["merge-base", before, ref], ok=(0, 1)).stdout.decode().strip()
    if head is None or not base:
        return False
    managed = _out(root, ["ls-tree", "-r", "--name-only", "--full-tree", base]).splitlines()
    tree = tree_with_carried_files(root, head, before, managed)
    if tree is None:
        return False
    commit = commit_tree(root, tree, [head], "canvas pull: keep local files")
    return fast_forward(root, commit)


def ahead_behind(root: Path, ref: str = REMOTE_REF) -> tuple[int, int]:
    """(commits in HEAD not in ``ref``, commits in ``ref`` not in HEAD); (0, 0) if either
    is missing."""
    if head_commit(root) is None or ref_commit(root, ref) is None:
        return 0, 0
    a, b = _out(root, ["rev-list", "--left-right", "--count", f"HEAD...{ref}"]).split()
    return int(a), int(b)


def changed_paths(root: Path) -> list[str]:
    """Tracked files with staged or unstaged changes (untracked files are not counted)."""
    out = _run(
        root, ["--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=no"]
    ).stdout.decode()
    recs = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(recs):
        rec = recs[i]
        i += 1
        if len(rec) > 3:
            paths.append(rec[3:])
            if rec[0] in "RC":  # a rename/copy record is followed by its source path
                i += 1
    return sorted(paths)


FETCH_TIME_FILE = "canvas-last-fetch"


def record_fetch_time(root: Path, when: str) -> None:
    """Remember (ISO 8601 UTC text) when the last fetch started; local cache only."""
    (git_common_dir(root) / FETCH_TIME_FILE).write_text(when + "\n", encoding="utf-8")


def last_fetch_time(root: Path) -> str | None:
    """The recorded start of the last fetch, else the commit time of canvas/main, else None."""
    try:
        text = (git_common_dir(root) / FETCH_TIME_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        return text
    commit = ref_commit(root)
    return _out(root, ["log", "-1", "--format=%cI", commit]) if commit else None


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    time: str  # committer time, ISO 8601
    subject: str
    paths: tuple[str, ...]  # paths changed by the commit (none for a merge commit)


def log_commits(
    root: Path, rev: str, *, exclude: str | None = None, limit: int = 10
) -> list[CommitInfo]:
    """Newest-first commits of ``rev`` (not reachable from ``exclude``) with changed paths.

    Read-only: one ``git log --name-only`` (``--no-optional-locks``); empty for a missing ``rev``.
    """
    args = ["--no-optional-locks", "-c", "core.quotepath=off", "log", f"--max-count={limit}"]
    args += ["--no-renames", "--name-only", "--format=%x01%H%x1f%cI%x1f%s", rev]
    if exclude:
        args.append(f"^{exclude}")
    args.append("--")
    out = _run(root, args).stdout.decode(errors="replace")
    commits: list[CommitInfo] = []
    for chunk in out.split("\x01")[1:]:
        head, _, rest = chunk.partition("\n")
        sha, time, subject = (head.split("\x1f") + ["", ""])[:3]
        paths = tuple(p for p in rest.split("\n") if p)
        commits.append(CommitInfo(sha, time, subject, paths))
    return commits
