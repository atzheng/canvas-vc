"""Explorer UI: object lists and detail pages (CONTRACTS.md section 13). Offline."""

from __future__ import annotations

from pathlib import Path

from canvas_sync.ui.server import dispatch
from tests.helpers.ui_repo import COURSE_YAML, make_repo

HW1 = (
    "---\ncanvas_id: 456\ntitle: Homework 1\npoints: 20\nsubmission_types: [online_upload]\n"
    "due: 2026-10-02 23:59\n"
    "overrides:\n  - section: Section B\n    due: 2026-10-04 23:59\n"
    "  - students: [alias:ab12]\n    due: 2026-10-06 23:59\n---\n\n"
    "Read [the syllabus](../pages/syllabus.md) and [gone](../pages/gone.md).\n\n"
    "![data](../files/data.csv)\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))\n"
)
FILES = {
    "course.yaml": COURSE_YAML,
    "pages/syllabus.md": "---\ncanvas_id: syllabus\ntitle: Syllabus\npublished: true\n---\n\n"
    "See [HW1](../assignments/hw1.md).\n",
    "pages/raw.md": "---\ntitle: Raw\nbody_format: html\n---\n\n<script>alert(2)</script>\n",
    "assignments/hw1.md": HW1,
    "files/data.csv": b"a,b\n",
    "modules.yaml": "- name: Week 1\n  items:\n    - page: syllabus\n",
}


def get(root: Path, url: str) -> tuple[int, str]:
    r = dispatch(root, url)
    return r.status, r.body.decode()


def test_detail_assignment(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    status, html = get(root, "/o/assignments/hw1.md")
    assert status == 200
    assert "Homework 1" in html
    assert "https://canvas.example.edu/courses/123/assignments/456" in html
    assert "section &quot;Section B&quot;" in html and "students alias:ab12" in html
    assert 'href="/o/pages/syllabus.md"' in html  # relative link -> UI route
    assert 'src="/files/raw/data.csv"' in html
    assert "courses/123/pages" not in html
    assert "Broken links (1): ../pages/gone.md" in html
    assert "Links out (3)" in html
    assert "<script>alert(1)</script>" not in html  # raw HTML shown escaped
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert 'href="javascript:' not in html
    assert "never fetched" in html


def test_backlinks_and_module(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    _, html = get(root, "/o/pages/syllabus.md")
    assert "Linked from (2)" in html
    assert "Module Week 1 (module item)" in html and 'href="/o/modules.yaml%23Week%201"' in html
    status, html = get(root, "/o/modules.yaml%23Week%201")
    assert status == 200 and 'href="/o/pages/syllabus.md"' in html


def test_html_body_is_escaped_source(tmp_path: Path) -> None:
    _, html = get(make_repo(tmp_path, FILES), "/o/pages/raw.md")
    assert "<script>alert(2)" not in html
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html


def test_path_traversal_404(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    for url in (
        "/o/../course.yaml",
        "/o/pages/../course.yaml",
        "/o/%2e%2e/course.yaml",
        "/o/pages/%2e%2e/%2e%2e/etc/passwd",
        "/o//etc/passwd",
        "/o/course.yaml",
        "/o/pages/nope.md",
        "/o/pages/syllabus.md%00",
        "/o/pages",
        "/o",
    ):
        assert get(root, url)[0] == 404, url


def test_differs_state(tmp_path: Path) -> None:
    remote = {
        "course.yaml": COURSE_YAML,
        "pages/syllabus.md": FILES["pages/syllabus.md"],
        "assignments/hw1.md": HW1.replace("points: 20", "points: 10"),
    }
    root = make_repo(tmp_path, FILES, remote_files=remote)
    assert "unchanged vs canvas/main" in get(root, "/o/pages/syllabus.md")[1]
    assert "modified vs canvas/main (points)" in get(root, "/o/assignments/hw1.md")[1]
    assert "new vs canvas/main" in get(root, "/o/pages/raw.md")[1]
    assert "differs" in get(root, "/objects/assignments")[1]


def test_list_sort_and_flags(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    status, html = get(root, "/objects")
    assert status == 200 and "Pages</a> (2)" in html and "Assignments</a> (1)" in html
    assert "broken links" in html and "20.0 pts, online_upload" in html
    _, html = get(root, "/objects/pages?sort=title&dir=desc")
    assert html.index(">Syllabus<") < html.index(">Raw<")
    assert get(root, "/objects/bogus")[0] == 404
    assert get(root, "/objects/modules")[0] == 200


def test_titles_escaped(tmp_path: Path) -> None:
    files = {**FILES, "pages/x.md": "---\ntitle: <b>hi</b>\n---\n\nbody\n"}
    _, html = get(make_repo(tmp_path, files), "/o/pages/x.md")
    assert "<b>hi</b>" not in html and "&lt;b&gt;hi&lt;/b&gt;" in html
