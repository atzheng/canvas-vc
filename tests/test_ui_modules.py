"""Explorer UI: modules outline. Offline."""

from __future__ import annotations

from pathlib import Path

from canvas_sync.ui.server import dispatch
from tests.helpers.ui_repo import COURSE_YAML, make_repo

MODULES = """\
- canvas_id: 10
  name: Week <1>
  published: true
  unlock: 2026-09-21
  items:
    - page: intro
    - assignment: hw1
      indent: 1
    - header: Further <b>reading</b>
    - url: https://example.org/x?a=1&b=2
      title: Boyd
      indent: 2
    - file: lec/01.pdf
    - page: gone
    - unmanaged: {type: ExternalTool, external_url: "https://t.example"}
      canvas_id: 77
      title: Gradescope
- canvas_id: 11
  name: Week 2
  published: false
  prerequisites: [10]
"""

FILES = {
    "course.yaml": COURSE_YAML,
    "modules.yaml": MODULES,
    "pages/intro.md": "---\ntitle: Intro\npublished: true\n---\n\nhi\n",
    "pages/loose.md": "---\ntitle: Loose\n---\n\nhi\n",
    "assignments/hw1.md": "---\ntitle: HW1\ndue: 2026-10-02 23:59\n---\n\nbody\n",
    "files/lec/01.pdf": b"%PDF",
    "files/other.txt": b"x",
    "announcements/news.md": "---\ntitle: News\n---\n\nhi\n",
}


def test_modules_outline(tmp_path: Path) -> None:
    html = dispatch(make_repo(tmp_path, FILES), "/modules").body.decode()
    assert "Week &lt;1&gt;" in html and "Week <1>" not in html
    assert "unlocks 2026-09-21" in html
    assert "prerequisites: Week &lt;1&gt;" in html
    assert html.index("Week &lt;1&gt;") < html.index("Week 2")
    # order of items, indent, subheader, external URL, unmanaged, missing target
    order = ["Intro", "HW1", "Further &lt;b&gt;reading", "Boyd", "01.pdf", "Gradescope"]
    pos = [html.index(s) for s in order]
    assert pos == sorted(pos)
    assert "padding-left:1.5em" in html and "padding-left:3.0em" in html
    assert "<b>reading" not in html
    assert '<a href="https://example.org/x?a=1&amp;b=2">' in html
    assert "<td>header</td>" in html and "<td>unmanaged</td>" in html
    assert "due 2026-10-02 23:59" in html
    assert 'href="/o/pages/intro.md"' in html and 'href="/o/files/lec/01.pdf"' in html
    assert "missing target" in html and 'href="/o/pages/gone.md"' not in html
    assert "pages/gone.md" in html


def test_not_in_any_module_matches_index(tmp_path: Path) -> None:
    root = make_repo(tmp_path, FILES)
    html = dispatch(root, "/modules").body.decode()
    tail = html.split("Not in any module")[1]
    assert "pages/loose.md" in tail and "files/other.txt" in tail
    assert "pages/intro.md" not in tail and "assignments/hw1.md" not in tail
    assert "files/lec/01.pdf" not in tail
    assert "announcements/news.md" not in tail


def _sub(tmp_path: Path) -> Path:
    (tmp_path / "x").mkdir()
    return tmp_path / "x"


def test_no_modules_and_bad_tree(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"course.yaml": COURSE_YAML})
    assert "no modules" in dispatch(root, "/modules").body.decode()
    root2 = make_repo(_sub(tmp_path), {"course.yaml": COURSE_YAML, "modules.yaml": "- 1\n"})
    r = dispatch(root2, "/modules")
    assert r.status == 200 and "modules.yaml" in r.body.decode()
