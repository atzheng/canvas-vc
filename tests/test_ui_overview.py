"""Explorer UI: overview page, server routing, read-only behaviour. Offline."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from canvas_sync.cli import app
from canvas_sync.ui.layout import Response
from canvas_sync.ui.server import dispatch, make_server
from tests.helpers.ui_repo import COURSE_YAML, git, make_repo

FILES = {
    "course.yaml": COURSE_YAML,
    "pages/a.md": "---\ntitle: <script>x</script>\npublished: true\n---\n\nhi\n",
    "pages/b.md": "---\ntitle: B\npublished: false\n---\n\nhi\n",
    "assignments/hw1.md": "---\ntitle: HW1\n---\n\nbody\n",
}


def html_of(r: Response) -> str:
    return r.body.decode()


def test_overview_never_fetched(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    r = dispatch(root, "/")
    assert r.status == 200
    html = html_of(r)
    assert "never fetched" in html and "canvas fetch" in html
    assert "https://canvas.example.edu" in html and "America/New_York" in html
    assert "<td>Pages</td><td>2</td><td>1</td><td>1</td>" in html
    for label in ("Overview", "Modules", "Objects", "Timeline", "Files", "Changes"):
        assert f">{label}</a>" in html


def test_overview_sync(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES, remote_files={"course.yaml": COURSE_YAML})
    (root / "pages/b.md").write_text("---\ntitle: B2\n---\n\nhi\n")
    html = html_of(dispatch(root, "/"))
    assert "never fetched" not in html
    assert "1 commit not pushed, 1 behind canvas/main" in html
    assert "Working tree: 1 changed file" in html


def test_overview_conflict_markers(tmp_path: Path) -> None:
    files = dict(FILES)
    files["pages/c.md"] = "---\ntitle: C\n---\n\n<<<<<<< a\nx\n=======\ny\n>>>>>>> b\n"
    html = html_of(dispatch(make_repo(tmp_path, files), "/"))
    assert "conflict markers in: pages/c.md" in html


def test_validate_issues_listed_with_path(tmp_path: Path) -> None:
    files = dict(FILES)
    files["pages/bad.md"] = "---\ntitle: T\nbogus: 1\n---\n\nhi\n"
    html = html_of(dispatch(make_repo(tmp_path, files), "/"))
    assert "pages/bad.md" in html
    assert "<td>error</td>" not in html  # severity is a badge
    assert "badge error" in html


def test_broken_tree_renders_message(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"course.yaml": "not: [valid"})
    r = dispatch(root, "/")
    assert r.status == 200
    assert "course.yaml" in html_of(r)


def test_tree_text_is_escaped(tmp_path: Path) -> None:
    files = dict(FILES)
    files["pages/<script>.md"] = "---\ntitle: T\n---\n\nhi\n"
    html = html_of(dispatch(make_repo(tmp_path, files), "/"))
    assert "<script>" not in html


def test_not_a_git_repo(tmp_path: Path) -> None:
    (tmp_path / "course.yaml").write_text(COURSE_YAML)
    r = dispatch(tmp_path, "/")
    assert r.status == 500 and "not a git repository" in html_of(r)


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    root = make_repo(tmp_path, FILES, remote_files={"course.yaml": COURSE_YAML})
    server = make_server(root, 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", root
    server.shutdown()
    server.server_close()


def fetch(url: str, method: str = "GET") -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def state_of(root: Path) -> tuple[str, str, bool]:
    return (
        git(root, "status", "--porcelain"),
        git(root, "for-each-ref"),
        (root / ".canvas").exists(),
    )


def test_http(served: tuple[str, Path]) -> None:
    base, root = served
    before = state_of(root)
    assert fetch(base + "/")[0] == 200
    s, css = fetch(base + "/static/ui.css")
    assert s == 200 and b"--bg" in css
    assert fetch(base + "/nope")[0] == 404
    assert fetch(base + "/o/../x")[0] == 404
    assert fetch(base + "/files/raw/..%2f..%2fetc/passwd")[0] == 404
    assert fetch(base + "/", "POST")[0] == 405
    assert state_of(root) == before


def test_cli_ui_help() -> None:
    res = CliRunner().invoke(app, ["ui", "--help"])
    assert res.exit_code == 0 and "--no-open" in res.output
