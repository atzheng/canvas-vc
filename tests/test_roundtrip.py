"""html_to_md, Bodies, and the md -> html -> md round-trip property."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from canvas_sync.model.base import Kind
from canvas_sync.render.bodies import Bodies
from canvas_sync.render.html2md import html_to_md
from canvas_sync.render.links import LinkContext
from canvas_sync.render.md2html import md_to_html

FIXTURES = Path(__file__).parent / "fixtures" / "html"

TREE: dict[str, tuple[Kind, int | str]] = {
    "pages/syllabus.md": (Kind.PAGE, "syllabus"),
    "pages/a.md": (Kind.PAGE, "a-page"),
    "assignments/hw1.md": (Kind.ASSIGNMENT, 11),
    "announcements/n.md": (Kind.ANNOUNCEMENT, 22),
    "files/lectures/01 intro.pdf": (Kind.FILE, 33),
    "files/map.png": (Kind.FILE, 34),
}


def ctx(from_path: str = "pages/a.md") -> LinkContext:
    return LinkContext(
        course_id=7,
        from_path=from_path,
        ids=lambda k: TREE[k.path][1] if k.path in TREE else None,
        paths=lambda kind, i: next((p for p, (k, v) in TREE.items() if k == kind and v == i), None),
    )


def once(md: str) -> str:
    return html_to_md(md_to_html(md, ctx()), ctx())


CORPUS = [
    "Plain paragraph.\n",
    "# Title\n\nSome *em*, **strong**, `code` and ~~strike~~.\n",
    "- a\n- b\n  - c\n\n1. one\n2. two\n",
    "3. three\n4. four\n",
    "> quote\n>\n> more\n",
    "| A | B |\n|---|---|\n| 1 | 2 |\n",
    "```python\nx = 1\nprint(x)\n```\n",
    "```\nplain fence\n```\n",
    "Inline $x^2$ and $a_i + b$ math.\n",
    "$$\n\\int_0^1 f(x) dx\n$$\n",
    "Costs $5 and $10 here.\n",
    "Escaped \\$ dollar and \\* star and a_b_c.\n",
    "See [syllabus](syllabus.md) and [hw](../assignments/hw1.md#top).\n",
    "[file](../files/lectures/01%20intro.pdf) ![img](../files/map.png)\n",
    "[ext](https://example.com/x?a=1&b=2)\n",
    "line one  \nline two\n",
    "---\n\ntext\n",
    'Raw <span style="color: red;">red</span> text.\n',
    '<div class="box">\n\nboxed\n\n</div>\n',
    '<iframe src="https://example.com/v" width="300"></iframe>\n',
    "Footnote[^1].\n\n[^1]: The note.\n",
    "Unicode caf\u00e9 \u2014 \u201cquotes\u201d and 'apos' -- dashes...\n",
    "Special chars: <, >, &, # not heading, 1\\. not list, | pipe\n",
    "![alt](https://example.com/i.png)\n",
    "&nbsp; entity &amp; and &lt;tag&gt;\n",
]


@pytest.mark.parametrize("md", CORPUS)
def test_corpus_idempotent(md: str) -> None:
    first = once(md)
    assert once(first) == first


@pytest.mark.parametrize("name", ["rce_syllabus.html", "rce_assignment.html"])
def test_rce_fixtures_stable(name: str) -> None:
    md1 = html_to_md((FIXTURES / name).read_text(encoding="utf-8"), ctx())
    md2 = once(md1)  # may lose code languages (pygments output carries none) ...
    assert once(md2) == md2  # ... but is a fixed point from there on


RCE_EDGE_CASES = [
    '<p><a href="/courses/7/files/33/download?wrap=1">f</a> '
    '<a href="/courses/7/files/33?wrap=1">g</a></p>',
    '<pre class="language-python"><code>x = 1\nprint("`` ticks")\n</code></pre>',
    "<p>a<br><br><br>b</p>",
    "<p>&amp;nbsp; and &amp;amp; and `&amp;lt;` literal</p>",
    "<p>&nbsp;lead and trail&nbsp;</p>",
    "<ul><li>item<pre><code>in list\n</code></pre></li></ul>",
]


@pytest.mark.parametrize("html", RCE_EDGE_CASES)
def test_rce_edge_cases_multipass(html: str) -> None:
    """html -> md -> html -> md -> html -> md: stable from the first md on."""
    md1 = html_to_md(html, ctx())
    md2 = once(md1)
    md3 = once(md2)
    assert md1 == md2 == md3


def test_literal_entity_text_stays_literal() -> None:
    md = html_to_md("<p>&amp;nbsp;</p>", ctx())
    assert "\u00a0" not in once(md)
    assert "&amp;nbsp;" in md_to_html(md, ctx())


def test_pandoc_version_matches_pin() -> None:
    from canvas_sync.render import html2md

    html2md.check_pandoc_version.cache_clear()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        html2md.check_pandoc_version()  # fails clearly if pandoc major.minor drifted


def test_pandoc_version_mismatch_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    from canvas_sync.render import html2md

    monkeypatch.setattr(html2md.pypandoc, "get_pandoc_version", lambda: "9.9.1")
    html2md.check_pandoc_version.cache_clear()
    with pytest.warns(RuntimeWarning, match="pandoc 9.9.1"):
        html2md.check_pandoc_version()
    html2md.check_pandoc_version.cache_clear()


def test_rce_syllabus_content_kept() -> None:
    md = html_to_md((FIXTURES / "rce_syllabus.html").read_text(encoding="utf-8"), ctx())
    for needle in (
        "Course Syllabus",
        "full syllabus",
        "Mon/Wed",
        "Fridays",
        "Late work loses 10% per day.",
        "Quiz",
        "Exam",
        "Course map",
        "Intro video",
        "m-abc",
        "A quote",
        "Line one",
        "line two",
        'print("hi")',
        "Boxed",
    ):
        assert needle in md, needle
    assert "$E = mc^2$" in md or "$$E = mc^2$$" in md
    assert "$$E = mc^2$$" in md
    assert "Inline $x^2$ math" in md
    assert "../files/lectures/01%20intro.pdf" in md
    assert "../assignments/hw1.md" in md
    assert "../files/map.png" in md
    assert "<iframe" in md
    assert 'href="/courses/7/' not in md
    assert "https://school.instructure.com/courses/" not in md
    assert "equation_image" not in md


def test_rce_assignment_content_kept() -> None:
    md = html_to_md((FIXTURES / "rce_assignment.html").read_text(encoding="utf-8"), ctx())
    assert r"$\sum_{i=1}^n i = n(n+1)/2$" in md
    assert "(syllabus.md)" in md
    assert "Part A" in md and "Part B" in md
    assert "<video" in md and "v.mp4" in md
    assert "https://example.com/x?a=1&b=2" in md or "a=1&amp;b=2" in md


def test_unknown_canvas_links_left_alone() -> None:
    html = '<p><a href="/courses/7/assignments/999">x</a> <a href="/courses/8/pages/a">y</a></p>'
    md = html_to_md(html, ctx())
    assert "/courses/7/assignments/999" in md
    assert "/courses/8/pages/a" in md


def test_equation_from_src_only() -> None:
    html = '<p><img class="equation_image" src="/equation_images/x%255E2" alt=""></p>'
    assert html_to_md(html, ctx()).strip() == "$$x^2$$"


def test_placeholder_collision() -> None:
    assert "CSYNCTOKEN" in html_to_md("<p>CSYNCTOKEN</p>", ctx())


def test_empty() -> None:
    assert html_to_md("", ctx()) == ""


# --- hypothesis -------------------------------------------------------------

_word = st.text(
    alphabet=st.sampled_from(list("abcxyzABC019 _*#>-+.,:;!?()[]<>&|~`'\"\\$=/")),
    min_size=1,
    max_size=12,
)
_plain = st.from_regex(r"[A-Za-z][A-Za-z0-9 ,.]{0,15}", fullmatch=True)
_inline = st.one_of(
    _word,
    _plain.map(lambda s: f"*{s.strip() or 'x'}*"),
    _plain.map(lambda s: f"**{s.strip() or 'x'}**"),
    _plain.map(lambda s: f"`{s.strip() or 'x'}`"),
    _plain.map(lambda s: f"[{s.strip() or 'x'}](https://example.com/{s.strip()[:3]})"),
    _plain.map(lambda s: f"[{s.strip() or 'x'}](../assignments/hw1.md)"),
    st.sampled_from(["$x^2$", "$a_i+b$", "$5 and $10", "\\$", "<b>b</b>", "~~s~~"]),
)
_para = st.lists(_inline, min_size=1, max_size=5).map(" ".join)
_block = st.one_of(
    _para,
    _para.map(lambda s: "# " + s),
    _para.map(lambda s: "> " + s),
    st.lists(_para, min_size=1, max_size=3).map(lambda xs: "\n".join(f"- {x}" for x in xs)),
    st.lists(_para, min_size=1, max_size=3).map(
        lambda xs: "\n".join(f"{i + 1}. {x}" for i, x in enumerate(xs))
    ),
    _plain.map(lambda s: f"```py\n{s}\n```"),
    st.sampled_from(["$$\nx^2\n$$", "| a | b |\n|---|---|\n| 1 | 2 |", "---"]),
)
_doc = st.lists(_block, min_size=1, max_size=4).map(lambda bs: "\n\n".join(bs) + "\n")


@settings(
    max_examples=25,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(_doc)
def test_roundtrip_property(md: str) -> None:
    first = once(md)
    assert once(first) == first


# --- bodies.json ------------------------------------------------------------


def test_bodies_record_matches() -> None:
    b = Bodies()
    assert not b.matches("pages/a.md", "<p>x</p>")
    b.record("pages/a.md", "x\n", "<p>x</p>")
    assert b.matches("pages/a.md", "<p>x</p>")
    assert not b.matches("pages/a.md", "<p>x </p>")
    assert not b.matches("pages/b.md", "<p>x</p>")
    assert b.md_matches("pages/a.md", "x\n")
    assert not b.md_matches("pages/a.md", "y\n")


def test_bodies_dump_canonical_and_load(tmp_path: Path) -> None:
    a = Bodies()
    a.record("pages/z.md", "z", "<p>z</p>")
    a.record("pages/a.md", "a", "<p>a</p>")
    b = Bodies()
    b.record("pages/a.md", "a", "<p>a</p>")
    b.record("pages/z.md", "z", "<p>z</p>")
    assert a.dump() == b.dump()
    data = json.loads(a.dump())
    assert list(data) == ["pages/a.md", "pages/z.md"]
    assert set(data["pages/a.md"]) == {"html_sha256", "md_sha256"}
    assert a.dump().endswith(b"\n")

    (tmp_path / ".canvas").mkdir()
    (tmp_path / ".canvas" / "bodies.json").write_bytes(a.dump())
    loaded = Bodies.load(tmp_path)
    assert loaded.dump() == a.dump()
    assert loaded.matches("pages/z.md", "<p>z</p>")


def test_bodies_load_missing_or_bad(tmp_path: Path) -> None:
    assert Bodies.load(tmp_path).dump() == b"{}\n"
    (tmp_path / ".canvas").mkdir()
    (tmp_path / ".canvas" / "bodies.json").write_text("not json")
    assert Bodies.load(tmp_path).entries == {}
