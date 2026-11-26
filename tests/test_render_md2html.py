from __future__ import annotations

from canvas_sync.model.base import Kind, ObjectKey
from canvas_sync.render.links import LinkContext, from_canvas_url, to_canvas_url
from canvas_sync.render.md2html import md_to_html, md_to_html_report

TREE: dict[str, tuple[Kind, int | str]] = {
    "pages/syl.md": (Kind.PAGE, "syllabus"),
    "pages/a.md": (Kind.PAGE, "a-page"),
    "assignments/hw1.md": (Kind.ASSIGNMENT, 11),
    "announcements/n.md": (Kind.ANNOUNCEMENT, 22),
    "files/lectures/01 intro.pdf": (Kind.FILE, 33),
}


def ctx(from_path: str = "pages/a.md") -> LinkContext:
    return LinkContext(
        course_id=7,
        from_path=from_path,
        ids=lambda k: TREE[k.path][1] if k.path in TREE else None,
        paths=lambda kind, i: next((p for p, (k, v) in TREE.items() if k == kind and v == i), None),
    )


def test_all_url_forms_round_trip() -> None:
    for from_path in ("pages/a.md", "assignments/hw1.md", "files/x.md"):
        c = ctx(from_path)
        for path in TREE:
            for image in (False, True):
                if path.startswith("files/") is False and image:
                    continue
                import posixpath

                rel = posixpath.relpath(path, posixpath.dirname(from_path))
                url = to_canvas_url(rel, c, image=image)
                assert url is not None
                assert from_canvas_url(url, c) in (rel, rel.replace(" ", "%20"))


def test_url_forms() -> None:
    c = ctx()
    assert to_canvas_url("syl.md", c) == "/courses/7/pages/syllabus"
    assert to_canvas_url("../assignments/hw1.md", c) == "/courses/7/assignments/11"
    assert to_canvas_url("../announcements/n.md", c) == "/courses/7/discussion_topics/22"
    f = "../files/lectures/01%20intro.pdf"
    assert to_canvas_url(f, c) == "/courses/7/files/33?wrap=1"
    assert to_canvas_url(f, c, image=True) == "/courses/7/files/33/preview"
    assert to_canvas_url("../assignments/hw1.md#top", c) == "/courses/7/assignments/11#top"
    assert from_canvas_url("https://x.instructure.com/courses/7/assignments/11", c) == (
        "../assignments/hw1.md"
    )


def test_unmappable() -> None:
    c = ctx()
    assert to_canvas_url("../assignments/nope.md", c) is None
    assert to_canvas_url("https://example.com", c) is None
    assert to_canvas_url("../../etc/x.md", c) is None
    assert from_canvas_url("/courses/8/assignments/11", c) is None
    assert from_canvas_url("/courses/7/assignments/99", c) is None
    assert from_canvas_url("/courses/7/quizzes/1", c) is None


def test_links_rewritten_and_missing_reported() -> None:
    md = "[hw](../assignments/hw1.md) ![i](../files/lectures/01%20intro.pdf) [x](gone.md) [e](https://e.com)"
    r = md_to_html_report(md, ctx())
    assert 'href="/courses/7/assignments/11"' in r.html
    assert 'src="/courses/7/files/33/preview"' in r.html
    assert 'href="gone.md"' in r.html
    assert r.missing_links == ["gone.md"]


def test_math() -> None:
    html = md_to_html(r"inline $x^2$ and `$a$`" + "\n\n$$\\frac{1}{2}$$\n", ctx())
    assert 'class="equation_image"' in html
    assert 'data-equation-content="x^2"' in html
    assert 'src="/equation_images/x%255E2"' in html
    assert 'data-equation-content="\\frac{1}{2}"' in html
    assert "<code>$a$</code>" in html
    assert html.count("<img ") == 2


def test_currency_not_math() -> None:
    html = md_to_html("It costs $5 and $10.", ctx())
    assert "equation_image" not in html
    assert "$5 and $10" in html


def test_code_not_math() -> None:
    html = md_to_html("```\n$x$\n```\n", ctx())
    assert "equation_image" not in html


def test_code_highlight_inline_styles() -> None:
    html = md_to_html("```python\ndef f():\n    return 1\n```\n", ctx())
    assert "style=" in html
    assert "<style" not in html


def test_unknown_lang_plain() -> None:
    assert md_to_html("```nosuchlang\na<b\n```\n", ctx()) == "<pre><code>a&lt;b\n</code></pre>\n"


def test_table_strike_footnote() -> None:
    html = md_to_html("|a|b|\n|-|-|\n|1|2|\n\n~~s~~ n[^1]\n\n[^1]: foot\n", ctx())
    assert "<table>" in html and "<del>" in html or "<s>" in html
    assert 'href="#fn1"' in html and 'id="fn1"' in html


def test_deterministic() -> None:
    md = "a[^1] $x$\n\n```py\n1\n```\n\n[^1]: f\n"
    assert md_to_html(md, ctx()) == md_to_html(md, ctx())


def test_objectkey_used() -> None:
    seen: list[ObjectKey] = []
    c = ctx()
    c.ids = lambda k: seen.append(k)  # type: ignore[assignment,return-value]
    to_canvas_url("../assignments/hw1.md", c)
    assert seen == [ObjectKey(Kind.ASSIGNMENT, "assignments/hw1.md")]
