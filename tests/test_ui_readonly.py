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
    "log_commits",  # read-only: git --no-optional-locks log --name-only
    "CommitInfo",  # result dataclass of log_commits
    "repo_root",  # read-only: git rev-parse --show-toplevel
}


def ui_files() -> list[Path]:
    return [*sorted((SRC / "ui").rglob("*.py")), SRC / "commands" / "ui.py"]


def _is_forbidden(mod: str) -> bool:
    return any(mod == f or mod.startswith(f + ".") for f in FORBIDDEN_IMPORTS)


def test_scan_finds_files() -> None:
    names = {p.name for p in ui_files()}
    assert {"server.py", "layout.py", "state.py", "overview.py", "ui.py"} <= names


FORBIDDEN_MODULES = ("subprocess", "shutil", "tempfile")
WRITE_MODES = set("wax+")


def scan_source(source: str, name: str = "x.py") -> list[str]:
    problems: list[str] = []
    tree = ast.parse(source)
    aliases = {"gitops"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if _is_forbidden(a.name) or a.name.split(".")[0] in FORBIDDEN_MODULES:
                    problems.append(f"{name}: imports {a.name}")
                if a.name == "canvas_sync.gitops" and a.asname:
                    aliases.add(a.asname)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if _is_forbidden(mod) or mod.split(".")[0] in FORBIDDEN_MODULES:
                problems.append(f"{name}: imports {mod}")
            for a in node.names:
                if _is_forbidden(f"{mod}.{a.name}"):
                    problems.append(f"{name}: imports {mod}.{a.name}")
                if mod == "canvas_sync" and a.name == "gitops" and a.asname:
                    aliases.add(a.asname)
                if mod == "canvas_sync.gitops" and a.name not in GITOPS_READ_ALLOWLIST:
                    problems.append(f"{name}: uses gitops.{a.name}")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
            and node.attr not in GITOPS_READ_ALLOWLIST
        ):
            problems.append(f"{name}:{node.lineno}: uses gitops.{node.attr}")
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr in ("write_text", "write_bytes"):
                problems.append(f"{name}:{node.lineno}: calls {fn.attr}")
            is_open = (isinstance(fn, ast.Name) and fn.id == "open") or (
                isinstance(fn, ast.Attribute)
                and fn.attr == "open"
                and not (isinstance(fn.value, ast.Name) and fn.value.id == "webbrowser")
            )
            if is_open:
                modes = [a for a in node.args[1:2]] + [
                    k.value for k in node.keywords if k.arg == "mode"
                ]
                if isinstance(fn, ast.Attribute) and node.args[:1]:
                    modes = [node.args[0]] + [k.value for k in node.keywords if k.arg == "mode"]
                for m in modes:
                    if not (isinstance(m, ast.Constant) and isinstance(m.value, str)):
                        problems.append(f"{name}:{node.lineno}: open() with non-literal mode")
                    elif WRITE_MODES & set(m.value):
                        problems.append(f"{name}:{node.lineno}: open() for writing")
    return problems


def test_no_forbidden_imports_or_gitops_writes() -> None:
    problems: list[str] = []
    for path in ui_files():
        problems += scan_source(path.read_text(), path.name)
    assert not problems, problems


def test_scan_catches_violations() -> None:
    bad = [
        "import subprocess",
        "from shutil import rmtree",
        "import tempfile",
        "import httpx",
        "from canvas_sync import gitops\ngitops.update_ref",
        "open('f', 'w')",
        "open('f', mode='ab')",
        "open('f', 'r+')",
        "p.open('w')",
        "p.write_text('x')",
        "p.write_bytes(b'x')",
    ]
    for src in bad:
        assert scan_source(src), src
    assert not scan_source("open('f')\nopen('f', 'rb')\np.read_text()")


def test_host_header_checked(tmp_path: Path) -> None:
    import threading
    import urllib.error
    import urllib.request

    from canvas_sync.ui.server import host_ok, make_server

    assert host_ok("127.0.0.1") and host_ok("localhost:8765") and host_ok("127.0.0.1:1")
    assert not host_ok(None) and not host_ok("evil.example") and not host_ok("evil.example:80")
    assert not host_ok("127.0.0.1.evil.example")
    root = make_repo(tmp_path, {"course.yaml": COURSE_YAML})
    srv = make_server(root, 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/"
        assert urllib.request.urlopen(url).status == 200
        req = urllib.request.Request(url, headers={"Host": "evil.example"})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_overview_does_not_touch_git_dir(tmp_path: Path) -> None:
    import os

    root = make_repo(tmp_path, {"course.yaml": COURSE_YAML})
    os.utime(root / "course.yaml", (1, 1))  # stat-dirty index entry
    snap = {
        p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (root / ".git").rglob("*") if p.is_file()
    }
    assert dispatch(root, "/").status == 200
    after = {
        p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (root / ".git").rglob("*") if p.is_file()
    }
    assert after == snap


def test_roster_cache_names_never_shown(tmp_path: Path) -> None:
    sentinel = "Zebulon Quxley"
    root = make_repo(tmp_path, {"course.yaml": COURSE_YAML})
    cache = root / ".canvas" / "roster-cache.json"
    cache.parent.mkdir()
    cache.write_text(json.dumps({"students": {sentinel: 1}, "sections": {}, "groups": {}}))
    for url in ("/", "/modules", "/objects", "/nope"):
        assert sentinel not in dispatch(root, url).body.decode()
