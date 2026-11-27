"""Throwaway git repos for the explorer UI tests. Invented people only."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from canvas_sync import gitops
from tests.helpers.fixed_point import write_files

COURSE_YAML = "canvas_url: https://canvas.example.edu\ncourse_id: 123\ntimezone: America/New_York\n"

_CFG = (
    "-c",
    "user.name=Test Person",
    "-c",
    "user.email=t@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
)


def git(root: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(root), *_CFG, *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def make_repo(
    tmp_path: Path,
    files: Mapping[str, bytes | str],
    *,
    remote_files: Mapping[str, bytes | str] | None = None,
) -> Path:
    """A git repo at ``tmp_path/repo`` with one commit of ``files`` on ``main``.

    With ``remote_files`` a ``canvas/main`` ref holds a commit of those files (built with
    gitops.write_tree/commit_tree/update_ref), as a fetched repo that differs from HEAD.
    """
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write_files(root, files)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    if remote_files is not None:
        data = {p: c.encode() if isinstance(c, str) else c for p, c in remote_files.items()}
        tree = gitops.write_tree(root, data)
        commit = gitops.commit_tree(root, tree, [], "canvas fetch: test")
        gitops.update_ref(root, gitops.REMOTE_REF, commit, None)
    return root
