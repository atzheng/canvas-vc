"""Explorer UI read-only guarantees (CONTRACTS.md section 13)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import canvas_sync
from canvas_sync.ui.server import dispatch
from tests.helpers.ui_repo import COURSE_YAML, make_repo

SRC = Path(canvas_sync.__file__).parent
FORBIDDEN_IMPORTS = (
    "canvas_sync.remote.http",
    "httpx",
    "requests",
    "canvas_sync.apply",
    "canvas_sync.snapshot",
)
# Widening this list must be called out in the PR body.
GITOPS_READ_ALLOWLIST = {
    "load_course_at",
    "ahead_behind",
    "read_tree_files",
    "head_commit",
    "ref_commit",
    "operation_in_progress",
    "conflict_marker_paths",
    "changed_paths",
    "last_fetch_time",
    "repo_root",  # read-only: git rev-parse --show-toplevel
}


def ui_files() -> list[Path]:
    return [*sorted((SRC / "ui").rglob("*.py")), SRC / "commands" / "ui.py"]


def _is_forbidden(mod: str) -> bool:
    return any(mod == f or mod.startswith(f + ".") for f in FORBIDDEN_IMPORTS)


def test_scan_finds_files() -> None:
    names = {p.name for p in ui_files()}
    assert {"server.py", "layout.py", "state.py", "overview.py", "ui.py"} <= names


def test_no_forbidden_imports_or_gitops_writes() -> None:
    problems: list[str] = []
    for path in ui_files():
        tree = ast.parse(path.read_text())
        aliases = {"gitops"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if _is_forbidden(a.name):
                        problems.append(f"{path.name}: imports {a.name}")
                    if a.name == "canvas_sync.gitops" and a.asname:
                        aliases.add(a.asname)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if _is_forbidden(mod):
                    problems.append(f"{path.name}: imports {mod}")
                for a in node.names:
                    if _is_forbidden(f"{mod}.{a.name}"):
                        problems.append(f"{path.name}: imports {mod}.{a.name}")
                    if mod == "canvas_sync" and a.name == "gitops" and a.asname:
                        aliases.add(a.asname)
                    if mod == "canvas_sync.gitops" and a.name not in GITOPS_READ_ALLOWLIST:
                        problems.append(f"{path.name}: uses gitops.{a.name}")
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in aliases
                and node.attr not in GITOPS_READ_ALLOWLIST
            ):
                problems.append(f"{path.name}:{node.lineno}: uses gitops.{node.attr}")
    assert not problems, problems


def test_roster_cache_names_never_shown(tmp_path: Path) -> None:
    sentinel = "Zebulon Quxley"
    root = make_repo(tmp_path, {"course.yaml": COURSE_YAML})
    cache = root / ".canvas" / "roster-cache.json"
    cache.parent.mkdir()
    cache.write_text(json.dumps({"students": {sentinel: 1}, "sections": {}, "groups": {}}))
    for url in ("/", "/modules", "/objects", "/nope"):
        assert sentinel not in dispatch(root, url).body.decode()
