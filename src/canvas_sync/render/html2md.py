"""Canvas HTML -> Markdown via pandoc (CONTRACTS.md section 6).

Pandoc runs with a fixed reader/writer (``html`` -> ``gfm``, no wrapping,
default extensions of those formats), so output does not drift with pandoc's
defaults. Around it:

* Canvas equation images become ``$..$`` (inline) or ``$$..$$`` (when the
  image is alone in its paragraph / block, as md2html writes display math).
* ``<a href>`` / ``<img src>`` Canvas URLs of this course become relative
  paths.
* Elements pandoc would drop (``iframe``, ``video``, ``audio``, ``object``,
  ``embed``, ``svg``, ``script``, ``style``) are kept verbatim as raw HTML.

Both substitutions go through alphanumeric placeholder tokens so pandoc cannot
escape or reflow them.
"""

from __future__ import annotations

import re
from html import escape, unescape
from urllib.parse import unquote

import pypandoc  # pyright: ignore[reportMissingTypeStubs]

from canvas_sync.render.links import LinkContext, from_canvas_url

PANDOC_FROM = "html"
PANDOC_TO = "gfm"
PANDOC_ARGS = ("--wrap=none", "--eol=lf")

_RAW_TAGS = "iframe|video|audio|object|embed|svg|script|style"
_RAW_RE = re.compile(rf"<({_RAW_TAGS})\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_EQ_IMG = r"<img\b[^>]*\bclass=[\"'][^\"']*\bequation_image\b[^>]*>"
_EQ_IMG_RE = re.compile(_EQ_IMG, re.IGNORECASE)
# a paragraph (or the span md2html emits for inline $$..$$) holding only one equation image
_EQ_BLOCK_RE = re.compile(
    rf"<(p|span)\b[^>]*>(?:\s|&nbsp;)*({_EQ_IMG})(?:\s|&nbsp;)*</\1\s*>", re.IGNORECASE
)
# md2html's pygments output (inline styles); the language is not recoverable, so it
# becomes a plain fenced block (and then stays one: a fixed point).
_HIGHLIGHT_RE = re.compile(
    r"<div\b[^>]*\bclass=[\"']highlight[\"'][^>]*>\s*<pre\b[^>]*>(.*?)</pre>\s*</div>",
    re.IGNORECASE | re.DOTALL,
)
# empty spacer paragraphs the Rich Content Editor leaves around (<p>&nbsp;</p>)
_EMPTY_P_RE = re.compile(r"<p\b[^>]*>(?:\s|&nbsp;|\u00a0)*</p>", re.IGNORECASE)
_LANG_CLASS_RE = re.compile(r"(<(?:pre|code)\b[^>]*\bclass=[\"'][^\"']*?)\blanguage-", re.I)
_ANY_TAG_RE = re.compile(r"<[^>]*>")
_TAG_RE = re.compile(r"<(a|img)\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""\b(href|src)=(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf"""\b{name}=(["'])(.*?)\1""", tag, re.IGNORECASE | re.DOTALL)
    return unescape(m.group(2)) if m else None


def equation_latex(img: str) -> str:
    """LaTeX of a Canvas equation image tag."""
    v = _attr(img, "data-equation-content")
    if v and v.strip():
        return v.strip()
    alt = _attr(img, "alt")
    if alt and alt.startswith("LaTeX:"):
        return alt[len("LaTeX:") :].strip()
    src = _attr(img, "src") or ""
    tail = src.rsplit("/equation_images/", 1)[-1].split("?", 1)[0]
    return unquote(unquote(tail)).strip()


def _rewrite_links(html: str, ctx: LinkContext) -> str:
    def tag(m: re.Match[str]) -> str:
        want = "src" if m.group(1).lower() == "img" else "href"

        def attr(a: re.Match[str]) -> str:
            if a.group(1).lower() != want:
                return a.group(0)
            rel = from_canvas_url(unescape(a.group(3)), ctx)
            if rel is None:
                return a.group(0)
            return f"{a.group(1)}={a.group(2)}{escape(rel, quote=True)}{a.group(2)}"

        return _ATTR_RE.sub(attr, m.group(0))

    return _TAG_RE.sub(tag, html)


_NBSP_EDGE_RE = re.compile("^\u00a0+|\u00a0+$")


def _protect_nbsp(md: str) -> str:
    """Markdown trims Unicode whitespace at paragraph edges; keep edge nbsp as an entity."""
    out: list[str] = []
    in_fence = False
    for line in md.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence:
            line = _NBSP_EDGE_RE.sub(lambda m: "&nbsp;" * len(m.group(0)), line)
        out.append(line)
    return "\n".join(out)


def _plain_code(m: re.Match[str]) -> str:
    text = unescape(_ANY_TAG_RE.sub("", m.group(1)))
    return f"<pre><code>{escape(text, quote=False)}</code></pre>"


def _display(latex: str) -> str:
    return f"$$\n{latex}\n$$" if "\n" in latex else f"$${latex}$$"


def html_to_md(html: str, link_ctx: LinkContext) -> str:
    prefix = "CSYNCTOKEN"
    while prefix in html:
        prefix += "X"
    latexes: list[str] = []
    raws: list[str] = []

    def eq_block(m: re.Match[str]) -> str:
        latexes.append(equation_latex(m.group(2)))
        return f"<p>{prefix}D{len(latexes) - 1}Z</p>"

    def eq_inline(m: re.Match[str]) -> str:
        latexes.append(equation_latex(m.group(0)))
        return f"{prefix}I{len(latexes) - 1}Z"

    def raw(m: re.Match[str]) -> str:
        raws.append(m.group(0))
        return f"{prefix}R{len(raws) - 1}Z"

    # Raw elements first so their content is never touched. One alone in a <p>
    # or on its own line is its own paragraph (else it would glue to the
    # neighbouring text on a second pass).
    work = _RAW_RE.sub(raw, html)
    tok = rf"{prefix}R\d+Z"
    work = re.sub(rf"<p\b[^>]*>\s*({tok})\s*</p>", r"<p>\1</p>", work)
    work = re.sub(rf"(?m)^[ \t]*({tok})[ \t]*$", r"<p>\1</p>", work)
    work = _HIGHLIGHT_RE.sub(_plain_code, work)
    work = _EMPTY_P_RE.sub("", work)
    work = _LANG_CLASS_RE.sub(r"\1", work)
    work = _rewrite_links(work, link_ctx)
    work = _EQ_BLOCK_RE.sub(eq_block, work)
    work = _EQ_IMG_RE.sub(eq_inline, work)

    out: str = pypandoc.convert_text(  # pyright: ignore[reportUnknownMemberType]
        work, PANDOC_TO, format=PANDOC_FROM, extra_args=list(PANDOC_ARGS)
    )

    out = re.sub(rf"{prefix}D(\d+)Z", lambda m: _display(latexes[int(m.group(1))]), out)
    out = re.sub(rf"{prefix}I(\d+)Z", lambda m: f"${latexes[int(m.group(1))]}$", out)
    out = re.sub(rf"{prefix}R(\d+)Z", lambda m: raws[int(m.group(1))], out)
    out = _protect_nbsp(out.strip("\n"))
    return out + "\n" if out else ""
