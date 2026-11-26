"""gitops, snapshot, BlobStore and IgnoreRules against throwaway repos."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from canvas_sync import gitops
from canvas_sync.config import CourseConfig
from canvas_sync.gitops import BlobRef, GitError, RefMoved
from canvas_sync.ignore import IgnoreRules
from canvas_sync.model.course import Course
from canvas_sync.model.files import FileEntry, FilesMeta
from canvas_sync.model.roster import Roster
from canvas_sync.render.bodies import Bodies
from canvas_sync.snapshot import BODIES_PATH, BlobStore, snapshot

CONFIG = CourseConfig.model_validate(
    {"canvas_url": "https://canvas.example.edu", "course_id": 123, "timezone": "UTC"}
)
HAND_EDITED = """\
# my course, hand edited
canvas_url: https://canvas.example.edu
course_id: 123
timezone: UTC
"""


class FakeBodies(Bodies):
    def __init__(self, data: bytes = b"{}\n") -> None:
        self.data = data

    def dump(self) -> bytes:
        return self.data


@pytest.fixture(autouse=True)
def _git_env(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Test Person")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.invalid")
        monkeypatch.setenv(f"GIT_{who}_DATE", "2024-01-01T00:00:00+00:00")


def run(root: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    run(root, "init", "-q", "-b", "main")
    run(root, "config", "core.hooksPath", "/dev/null")
    run(root, "config", "commit.gpgsign", "false")
    return root


def commit_file(root: Path, name: str, text: str, msg: str = "c") -> str:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    run(root, "add", name)
    run(root, "commit", "-q", "-m", msg)
    return run(root, "rev-parse", "HEAD")


def course(files: dict[str, FileEntry] | None = None, config: CourseConfig = CONFIG) -> Course:
    return Course(config, {}, FilesMeta(files=files or {}), [], Roster())


# ---- gitops -----------------------------------------------------------------------


def test_head_and_ref_commit(repo: Path) -> None:
    assert gitops.head_commit(repo) is None
    assert gitops.ref_commit(repo) is None
    c = commit_file(repo, "a.txt", "a")
    assert gitops.head_commit(repo) == c
    assert gitops.repo_root(repo).resolve() == repo.resolve()
    with pytest.raises(GitError):
        gitops.repo_root(repo.parent)


def test_write_tree_nested_binary_and_user_index_untouched(repo: Path) -> None:
    commit_file(repo, "keep.txt", "k")
    (repo / "staged.txt").write_text("s")
    run(repo, "add", "staged.txt")
    before = run(repo, "status", "--porcelain")
    binary = bytes(range(256)) + b"\r\n\x00end"
    tree = gitops.write_tree(repo, {"a/b/c.bin": binary, "a/x.txt": b"x\n", "top.txt": b"t"})
    assert run(repo, "status", "--porcelain") == before
    assert not (repo / "a").exists()
    c = gitops.commit_tree(repo, tree, [], "m")
    files = gitops.read_tree_files(repo, c)
    assert files == {"a/b/c.bin": binary, "a/x.txt": b"x\n", "top.txt": b"t"}
    assert run(repo, "ls-tree", "-d", tree).endswith("\ta")


def test_write_tree_is_deterministic_and_resolves_blobrefs(repo: Path) -> None:
    store = BlobStore(repo)
    sha = store.put(b"pdf bytes")
    assert sha == hashlib.sha256(b"pdf bytes").hexdigest()
    assert store.get(sha) == b"pdf bytes"
    assert store.put(b"pdf bytes") == sha
    t1 = gitops.write_tree(repo, {"files/a.pdf": BlobRef(sha), "z": b"1"})
    t2 = gitops.write_tree(repo, {"z": b"1", "files/a.pdf": b"pdf bytes"})
    assert t1 == t2
    with pytest.raises(GitError):
        gitops.write_tree(repo, {"f": BlobRef("0" * 64)})
    with pytest.raises(ValueError):
        gitops.write_tree(repo, {"../x": b""})


def test_blobstore_rejects_bad_digest(repo: Path) -> None:
    with pytest.raises(ValueError):
        BlobStore(repo).path_for("../../etc/passwd")
    with pytest.raises(FileNotFoundError):
        BlobStore(repo).get("a" * 64)
    assert BlobStore(repo).path_for("ab" + "c" * 62).parent.parent.name == "canvas-blobs"


def test_update_ref_compare_and_swap(repo: Path) -> None:
    a = commit_file(repo, "a", "1")
    b = commit_file(repo, "a", "2")
    ref = gitops.REMOTE_REF
    gitops.update_ref(repo, ref, a, None)
    assert gitops.ref_commit(repo) == a
    with pytest.raises(RefMoved):
        gitops.update_ref(repo, ref, b, None)  # exists already
    with pytest.raises(RefMoved):
        gitops.update_ref(repo, ref, b, b)  # wrong old value
    assert gitops.ref_commit(repo) == a
    gitops.update_ref(repo, ref, b, a)
    assert gitops.ref_commit(repo) == b


def test_is_ancestor(repo: Path) -> None:
    a = commit_file(repo, "a", "1")
    b = commit_file(repo, "a", "2")
    assert gitops.is_ancestor(repo, a, b)
    assert not gitops.is_ancestor(repo, b, a)
    assert gitops.is_ancestor(repo, a, a)


def _conflict(repo: Path) -> None:
    commit_file(repo, "pages/p.md", "base\n")
    commit_file(repo, "other.md", "o\n")
    run(repo, "checkout", "-q", "-b", "theirs")
    theirs = commit_file(repo, "pages/p.md", "theirs\n")
    run(repo, "checkout", "-q", "main")
    run(repo, "reset", "-q", "--hard", "HEAD")
    commit_file(repo, "pages/p.md", "ours\n")
    run(repo, "update-ref", gitops.REMOTE_REF, theirs)


def test_merge_conflict_detection(repo: Path) -> None:
    _conflict(repo)
    assert gitops.is_clean(repo)
    assert gitops.merge(repo) != 0
    assert gitops.unmerged_paths(repo) == ["pages/p.md"]
    assert gitops.conflict_marker_paths(repo) == ["pages/p.md"]
    assert not gitops.is_clean(repo)
    (repo / "pages/p.md").write_text("resolved\n")
    assert gitops.conflict_marker_paths(repo) == []
    run(repo, "add", "pages/p.md")
    assert gitops.unmerged_paths(repo) == []
    assert not gitops.is_clean(repo)  # merge still staged
    run(repo, "commit", "-q", "--no-edit")
    assert gitops.is_clean(repo)


def test_is_clean_ignores_untracked_but_sees_edits(repo: Path) -> None:
    commit_file(repo, "a", "1")
    (repo / "new.txt").write_text("x")
    assert gitops.is_clean(repo)
    (repo / "a").write_text("2")
    assert not gitops.is_clean(repo)


def test_merge_clean(repo: Path) -> None:
    commit_file(repo, "a", "1")
    run(repo, "checkout", "-q", "-b", "theirs")
    theirs = commit_file(repo, "b", "2")
    run(repo, "checkout", "-q", "main")
    commit_file(repo, "c", "3")
    run(repo, "update-ref", gitops.REMOTE_REF, theirs)
    assert gitops.merge(repo) == 0
    assert (repo / "b").exists() and (repo / "c").exists()
    assert gitops.is_ancestor(repo, theirs, "HEAD")


def test_rebase(repo: Path) -> None:
    commit_file(repo, "a", "1")
    run(repo, "checkout", "-q", "-b", "theirs")
    theirs = commit_file(repo, "b", "2")
    run(repo, "checkout", "-q", "main")
    commit_file(repo, "c", "3")
    run(repo, "update-ref", gitops.REMOTE_REF, theirs)
    assert gitops.merge(repo, rebase=True) == 0
    assert gitops.is_ancestor(repo, theirs, "HEAD")


def test_fast_forward(repo: Path) -> None:
    a = commit_file(repo, "a", "1")
    run(repo, "checkout", "-q", "-b", "theirs")
    b = commit_file(repo, "a", "2")
    run(repo, "checkout", "-q", "main")
    run(repo, "update-ref", gitops.REMOTE_REF, b)
    assert gitops.fast_forward(repo)
    assert gitops.head_commit(repo) == b
    assert (repo / "a").read_text() == "2"
    # diverged: refuses and changes nothing
    run(repo, "reset", "-q", "--hard", a)
    own = commit_file(repo, "own", "x")
    assert not gitops.fast_forward(repo)
    assert gitops.head_commit(repo) == own
    assert not gitops.fast_forward(repo, "refs/remotes/canvas/nope")


def test_load_course_at(repo: Path) -> None:
    commit_file(repo, "course.yaml", HAND_EDITED)
    c = gitops.load_course_at(repo, "HEAD")
    assert c.config == CONFIG


# ---- snapshot ----------------------------------------------------------------------


def test_snapshot_first_then_unchanged_is_none(repo: Path) -> None:
    store = BlobStore(repo)
    c1 = snapshot(repo, course(), FakeBodies(), store, "canvas fetch: 1 changed")
    assert c1 is not None and gitops.ref_commit(repo) == c1
    files = gitops.read_tree_files(repo, c1)
    assert BODIES_PATH in files and "course.yaml" in files
    assert snapshot(repo, course(), FakeBodies(), store, "again") is None
    assert gitops.ref_commit(repo) == c1
    c2 = snapshot(repo, course(), FakeBodies(b'{"a": 1}\n'), store, "bodies changed")
    assert c2 is not None
    assert run(repo, "rev-parse", f"{c2}^") == c1
    assert gitops.head_commit(repo) is None  # user's branch untouched
    assert not (repo / ".canvas").exists()


def test_snapshot_blobs(repo: Path) -> None:
    store = BlobStore(repo)
    data = bytes(range(256)) * 3
    sha = store.put(data)
    entry = FileEntry(path="files/lec/01.pdf", canvas_id=5, sha256=sha)
    c = snapshot(repo, course({"files/lec/01.pdf": entry}), FakeBodies(), store, "m")
    assert c is not None
    assert gitops.read_tree_files(repo, c)["files/lec/01.pdf"] == data
    # missing from the store and from the previous snapshot: error, ref unchanged
    bad = FileEntry(path="files/x.pdf", canvas_id=6, sha256="1" * 64)
    with pytest.raises(Exception, match="blob store"):
        snapshot(repo, course({"files/x.pdf": bad}), FakeBodies(), store, "m")
    assert gitops.ref_commit(repo) == c
    # present in the previous snapshot: restored into the store
    store.path_for(sha).unlink()
    assert snapshot(repo, course({"files/lec/01.pdf": entry}), FakeBodies(), store, "m") is None
    assert store.get(sha) == data


def test_snapshot_extra_parent(repo: Path) -> None:
    store = BlobStore(repo)
    head = commit_file(repo, "mine", "x")
    c1 = snapshot(repo, course(), FakeBodies(), store, "fetch")
    assert c1 is not None
    c2 = snapshot(repo, course(), FakeBodies(), store, "canvas push: 3 operations", head)
    assert c2 is not None and c2 != c1  # same tree, still a commit
    assert run(repo, "rev-list", "--parents", "-n1", c2).split()[1:] == [c1, head]
    assert gitops.tree_of(repo, c2) == gitops.tree_of(repo, c1)
    assert gitops.is_ancestor(repo, head, c2)
    assert gitops.fast_forward(repo)  # the user's branch can follow


def test_snapshot_does_not_touch_worktree_or_index(repo: Path) -> None:
    commit_file(repo, "course.yaml", HAND_EDITED)
    (repo / "wip.md").write_text("unsaved work")
    run(repo, "add", "wip.md")
    status = run(repo, "status", "--porcelain")
    snapshot(repo, course(), FakeBodies(), BlobStore(repo), "m")
    assert run(repo, "status", "--porcelain") == status
    assert (repo / "wip.md").read_text() == "unsaved work"


def test_snapshot_keeps_hand_edited_course_yaml(repo: Path) -> None:
    head = commit_file(repo, "course.yaml", HAND_EDITED)
    c = snapshot(repo, course(), FakeBodies(), BlobStore(repo), "m")
    assert c is not None
    assert gitops.read_tree_files(repo, c)["course.yaml"] == HAND_EDITED.encode()
    # so merging canvas/main into the user's branch produces no course.yaml change
    assert gitops.merge(repo) == 0
    assert (repo / "course.yaml").read_text() == HAND_EDITED
    assert gitops.is_ancestor(repo, head, "HEAD")


def test_snapshot_dumps_course_yaml_when_config_differs(repo: Path) -> None:
    commit_file(repo, "course.yaml", HAND_EDITED.replace("123", "999"))
    c = snapshot(repo, course(), FakeBodies(), BlobStore(repo), "m")
    assert c is not None
    got = gitops.read_tree_files(repo, c)["course.yaml"].decode()
    assert "course_id: 123" in got and "hand edited" not in got


# ---- ignore ------------------------------------------------------------------------


def rules(*lines: str) -> IgnoreRules:
    return IgnoreRules(None, list(lines))


def test_ignore_basics() -> None:
    r = rules("# c", "", "*.pdf", "!keep.pdf", "/top.md", "files/old/", "pages/*/draft*", "**/tmp")
    assert r.ignored("files/a.pdf")
    assert not r.ignored("files/keep.pdf")
    assert r.ignored("top.md") and not r.ignored("pages/top.md")
    assert r.ignored("files/old/x/y.txt") and not r.ignored("files/older/x.txt")
    assert r.ignored("pages/a/draft1.md") and not r.ignored("pages/a/b/draft1.md")
    assert r.ignored("a/b/tmp") and r.ignored("tmp/x") and not r.ignored("tmpx")
    assert not r.ignored("pages/hello.md")


def test_ignore_dir_only_and_parent_wins() -> None:
    r = rules("build/", "!build/keep.txt", "foo")
    assert not r.ignored("build")  # a file named build is not a directory
    assert r.ignored("build/keep.txt")  # cannot re-include inside an ignored dir
    assert r.ignored("x/foo/bar")


def test_ignore_wildcards() -> None:
    r = rules("a/**/z", "q?.md", "[ab].md", "assignments/hw[0-9].md", "x/**")
    assert r.ignored("a/z") and r.ignored("a/b/c/z") and not r.ignored("a/zz")
    assert r.ignored("qa.md") and not r.ignored("qab.md") and not r.ignored("q/.md")
    assert r.ignored("a.md") and r.ignored("b.md") and not r.ignored("c.md")
    assert r.ignored("assignments/hw3.md") and not r.ignored("assignments/hwx.md")
    assert r.ignored("x/y/z")


def test_ignore_spec_form_matches_markdown_paths() -> None:
    r = rules("pages/instructor-scratch", "files/old-term/**")
    assert r.ignored("pages/instructor-scratch.md")
    assert r.ignored("files/old-term/a/b.pdf")
    assert not r.ignored("files/new-term/a.pdf")


def test_ignore_load_and_add(tmp_path: Path) -> None:
    assert not IgnoreRules.load(tmp_path).ignored("a.md")
    (tmp_path / ".canvasignore").write_text("*.tmp")  # no trailing newline
    r = IgnoreRules.load(tmp_path)
    r.add("assignments/hw #1 [x].md")
    r.add("assignments/hw #1 [x].md")  # idempotent
    assert r.ignored("assignments/hw #1 [x].md")
    assert not r.ignored("assignments/hw #1 x.md")
    text = (tmp_path / ".canvasignore").read_text()
    assert text == "*.tmp\n/assignments/hw #1 \\[x\\].md\n"
    assert IgnoreRules.load(tmp_path).ignored("assignments/hw #1 [x].md")
