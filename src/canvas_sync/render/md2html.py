"""Markdown -> Canvas HTML (CONTRACTS.md section 6).

Rules:

* CommonMark + tables, strikethrough, footnotes (ids by position: ``fn1``).
* Fenced code is highlighted by pygments with inline styles (Canvas strips
  ``<style>`` and classes).
* Math uses the dollarmath rules: ``$..$`` is math only if the character after
  the opening ``$`` and before the closing ``$`` is not whitespace and the
  closing ``$`` is not followed by a digit, so ``$5 and $10`` stays text.
  Code spans and blocks are never scanned. ``\\$`` is a literal dollar.
* Inline math becomes ``<img class="equation_image" ...>``; display math
  (``$$..$$``) is the same image alone in
  ``<p style="text-align: center;">``.
* Relative links/images (also ``<a href>``/``<img src>`` in raw HTML) become
  Canvas URLs, keeping ``?query`` and ``#fragment``. Unmappable ones are left
  unchanged and reported by ``md_to_html_report``: ``missing_links`` (no such
  local object) and ``unresolved_links`` (exists, no canvas_id yet).
* Latex is stripped of surrounding whitespace. ``$$a$$ text $$b$$`` at line
  start is ONE display block (CommonMark block rule); put each on its own
  line. Inside table cells a math ``|`` must be written ``\\|``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape, unescape
from typing import Any
from urllib.parse import quote

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import OptionsDict
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.footnote import footnote_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name  # pyright: ignore[reportUnknownVariableType]
from pygments.util import ClassNotFound

from canvas_sync.render.links import LinkContext, is_relative_ref, resolve_relative

_TAG_RE = re.compile(r"<(a|img)\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""\b(href|src)=(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)


@dataclass
class RenderResult:
    html: str
    missing_links: list[str] = field(default_factory=lambda: list[str]())  # no such local object
    unresolved_links: list[str] = field(default_factory=lambda: list[str]())  # no canvas_id yet


def equation_img(latex: str) -> str:
    """Canvas RCE equation markup; src carries the double-encoded LaTeX."""
    latex = latex.strip()
    src = "/equation_images/" + quote(quote(latex, safe=""), safe="")
    esc = escape(latex, quote=True)
    return (
        f'<img class="equation_image" title="{esc}" src="{src}" '
        f'alt="LaTeX: {esc}" data-equation-content="{esc}">'
    )


def _highlight(code: str, lang: str) -> str | None:
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        return None
    out: str = highlight(code, lexer, HtmlFormatter(noclasses=True, wrapcode=True))
    return out


def _render_fence(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: Any
) -> str:
    tok: Token = tokens[idx]
    info = tok.info.strip()
    if info:
        out = _highlight(tok.content, info.split(maxsplit=1)[0])
        if out is not None:
            return out
    return f"<pre><code>{escape(tok.content)}</code></pre>\n"


def _render_math_inline(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: Any
) -> str:
    return equation_img(tokens[idx].content)


def _render_math_inline_double(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: Any
) -> str:
    img = equation_img(tokens[idx].content)
    return f'<span style="display: block; text-align: center;">{img}</span>'


def _render_math_block(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: Any
) -> str:
    return f'<p style="text-align: center;">{equation_img(tokens[idx].content)}</p>\n'


def _make_md(ctx: LinkContext, missing: list[str], unresolved: list[str]) -> MarkdownIt:
    md = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    md.use(footnote_plugin)
    md.use(dollarmath_plugin, allow_space=False, allow_digits=False, double_inline=True)

    md.add_render_rule("fence", _render_fence)
    md.add_render_rule("math_inline", _render_math_inline)
    md.add_render_rule("math_inline_double", _render_math_inline_double)
    md.add_render_rule("math_block", _render_math_block)

    def resolve(href: str, image: bool) -> str | None:
        """New URL for a relative ref, recording missing/unresolved ones."""
        if not is_relative_ref(href):
            return None
        res = resolve_relative(href, ctx, image=image)
        if res.status == "missing":
            missing.append(href)
        elif res.status == "unresolved":
            unresolved.append(href)
        return res.url

    def rewrite(tok: Token, attr: str, image: bool) -> None:
        href = tok.attrGet(attr)
        if isinstance(href, str):
            new = resolve(href, image)
            if new is not None:
                tok.attrSet(attr, new)

    def rewrite_raw(html: str) -> str:
        def tag(m: re.Match[str]) -> str:
            is_img = m.group(1).lower() == "img"
            want = "src" if is_img else "href"

            def attr(a: re.Match[str]) -> str:
                if a.group(1).lower() != want:
                    return a.group(0)
                new = resolve(unescape(a.group(3)), is_img)
                if new is None:
                    return a.group(0)
                return f"{a.group(1)}={a.group(2)}{escape(new, quote=True)}{a.group(2)}"

            return _ATTR_RE.sub(attr, m.group(0))

        return _TAG_RE.sub(tag, html)

    def walk(tokens: list[Token]) -> None:
        for t in tokens:
            if t.type == "link_open":
                rewrite(t, "href", False)
            elif t.type == "image":
                rewrite(t, "src", True)
            elif t.type in ("html_block", "html_inline"):
                t.content = rewrite_raw(t.content)
            if t.children:
                walk(t.children)

    def rule(state: Any) -> None:
        walk(state.tokens)

    md.core.ruler.push("canvas_links", rule)
    return md


def md_to_html_report(md: str, link_ctx: LinkContext) -> RenderResult:
    missing: list[str] = []
    unresolved: list[str] = []
    html = _make_md(link_ctx, missing, unresolved).render(md)
    return RenderResult(
        html=html, missing_links=sorted(set(missing)), unresolved_links=sorted(set(unresolved))
    )


def md_to_html(md: str, link_ctx: LinkContext) -> str:
    return md_to_html_report(md, link_ctx).html
