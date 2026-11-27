"""Explorer UI: files view and /files/raw serving safety. Offline."""

from __future__ import annotations

import hashlib
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from canvas_sync.ui.server import dispatch, make_server
from tests.helpers.ui_repo import COURSE_YAML, make_repo

META = """\
rules:
  - match: "*.pdf"
    hidden: true
files:
  lectures/01.pdf: {unlock: 2026-09-28 09:00}
"""
MODULES = "- name: Week 1\n  items:\n    - file: lectures/01.pdf\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
PAGE = "---\ntitle: Week 1\n---\n\n![p](../files/img/pic.png) [s](../files/lectures/01.pdf)\n"
FILES = {
    "course.yaml": COURSE_YAML,
    "files/_meta.yaml": META,
    "files/lectures/01.pdf": b"%PDF-1.4 one",
    "files/lectures/02.pdf": b"%PDF-1.4 two",
    "files/img/pic.png": PNG,
    "files/img/x.svg": b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    "files/note.html": b"<script>alert(1)</script>",
    "modules.yaml": MODULES,
    "pages/week1.md": PAGE,
    "assignments/hw3.md": "---\ntitle: HW3\n---\n\n[data](../files/note.html)\n",
}


def test_files_page_backlinks_and_unreferenced(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    r = dispatch(root, "/files")
    assert r.status == 200
    html = r.body.decode()
    assert "<strong>lectures/</strong>" in html and "<strong>img/</strong>" in html
    assert 'href="/files/raw/lectures/01.pdf"' in html
    assert 'href="/o/pages/week1.md"' in html
    assert 'href="/o/modules.yaml%23Week%201"' in html and "Module Week 1" in html
    assert 'href="/o/assignments/hw3.md"' in html
    assert "unreferenced" in html
    # 02.pdf and x.svg are linked from nowhere; the other three files are
    assert "5 files, 2 unreferenced" in html
    assert "unlock 2026-09-28 09:00" in html
    assert "hidden" in html and "<code>*.pdf</code>" in html
    assert '<img src="/files/raw/img/pic.png"' in html
    assert f"<code>{hashlib.sha256(b'%PDF-1.4 two').hexdigest()[:8]}</code>" in html


def test_files_page_escapes_names(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"course.yaml": COURSE_YAML, "files/<b>.txt": b"x"})
    html = dispatch(root, "/files").body.decode()
    assert "&lt;b&gt;.txt" in html and "<b>.txt" not in html


def test_files_empty(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"course.yaml": COURSE_YAML})
    r = dispatch(root, "/files")
    assert r.status == 200 and "No files" in r.body.decode()
    assert dispatch(root, "/files/raw/a.txt").status == 404
    assert dispatch(root, "/files/other").status == 404


def test_raw_serves_pdf_and_png_inline(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    r = dispatch(root, "/files/raw/lectures/01.pdf")
    assert r.status == 200 and r.body == b"%PDF-1.4 one"
    assert r.content_type == "application/pdf"
    h = dict(r.headers)
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Content-Disposition"].startswith("inline")
    assert dispatch(root, "/files/raw/img/pic.png").content_type == "image/png"


@pytest.mark.parametrize("p", ["img/x.svg", "note.html"])
def test_raw_active_content_is_attachment(tmp_path: Path, p: str) -> None:
    root = make_repo(tmp_path, FILES)
    r = dispatch(root, "/files/raw/" + p)
    assert r.status == 200
    assert r.content_type == "application/octet-stream"
    h = dict(r.headers)
    assert h["Content-Disposition"].startswith("attachment")
    assert h["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in h["Content-Security-Policy"]


@pytest.mark.parametrize(
    "target",
    [
        "/files/raw/../course.yaml",
        "/files/raw/%2e%2e/course.yaml",
        "/files/raw/..%2fcourse.yaml",
        "/files/raw/..%2F..%2Fetc/passwd",
        "/files/raw/%2fetc/passwd",
        "/files/raw//etc/passwd",
        "/files/raw/%2Fetc%2Fpasswd",
        "/files/raw/_meta.yaml",
        "/files/raw/lectures",
        "/files/raw/",
        "/files/raw/nope.pdf",
        "/files/raw/lectures/01.pdf%00",
        "/files/raw/lectures/..%2f..%2fcourse.yaml",
    ],
)
def test_raw_refuses_outside_paths(tmp_path: Path, target: str) -> None:
    root = make_repo(tmp_path, FILES)
    assert dispatch(root, target).status == 404


def test_raw_refuses_symlinks(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_text("top secret")
    root = make_repo(tmp_path, FILES)
    os.symlink(secret, root / "files" / "link.txt")  # file symlink out of the tree
    os.symlink(outside, root / "files" / "linkdir")  # directory symlink out of the tree
    os.symlink(root / "files" / "note.html", root / "files" / "inside.html")  # even inside
    for p in ("link.txt", "linkdir/s.txt", "inside.html"):
        r = dispatch(root, "/files/raw/" + p)
        assert r.status == 404, p
        assert b"top secret" not in r.body
    page = dispatch(root, "/files")
    assert page.status == 200 and b"top secret" not in page.body


def test_raw_refuses_symlinked_files_dir(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    (real / "lectures").mkdir(parents=True)
    (real / "lectures" / "01.pdf").write_bytes(b"x")
    files = {k: v for k, v in FILES.items() if not k.startswith("files/")}
    root = make_repo(tmp_path, files)
    os.symlink(real, root / "files")
    assert dispatch(root, "/files/raw/lectures/01.pdf").status == 404


def test_raw_missing_on_disk_entry(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    (root / "files" / "lectures" / "02.pdf").unlink()
    meta = META + "  lectures/02.pdf: {sha256: " + "a" * 64 + "}\n"
    (root / "files" / "_meta.yaml").write_text(meta)
    assert dispatch(root, "/files/raw/lectures/02.pdf").status == 404
    assert "missing on disk" in dispatch(root, "/files").body.decode()


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    root = make_repo(tmp_path, FILES)
    srv = make_server(root, 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_http_raw_headers_and_traversal(server: str) -> None:
    with urllib.request.urlopen(server + "/files/raw/lectures/01.pdf") as r:
        assert r.read() == b"%PDF-1.4 one"
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["Content-Type"] == "application/pdf"
    with urllib.request.urlopen(server + "/files") as r:
        assert r.status == 200
    for bad in ("/files/raw/..%2f..%2fetc/passwd", "/files/raw/%2e%2e/course.yaml"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(server + bad)
        assert e.value.code == 404
