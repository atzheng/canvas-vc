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
* Relative links/images become Canvas URLs; unresolved ones are left
  unchanged and reported by ``md_to_html_report``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape
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

from canvas_sync.render.links import LinkContext, is_relative_ref, to_canvas_url


@dataclass
class RenderResult:
    html: str
    missing_links: list[str] = field(default_factory=lambda: list[str]())  # unmapped targets


def equation_img(latex: str) -> str:
    """Canvas RCE equation markup; src carries the double-encoded LaTeX."""
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


def _make_md(ctx: LinkContext, missing: list[str]) -> MarkdownIt:
    md = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    md.use(footnote_plugin)
    md.use(dollarmath_plugin, allow_space=False, allow_digits=True, double_inline=True)

    md.add_render_rule("fence", _render_fence)
    md.add_render_rule("math_inline", _render_math_inline)
    md.add_render_rule("math_inline_double", _render_math_inline_double)
    md.add_render_rule("math_block", _render_math_block)

    def rewrite(tok: Token, attr: str, image: bool) -> None:
        href = tok.attrGet(attr)
        if not isinstance(href, str) or not is_relative_ref(href):
            return
        new = to_canvas_url(href, ctx, image=image)
        if new is None:
            missing.append(href)
        else:
            tok.attrSet(attr, new)

    def walk(tokens: list[Token]) -> None:
        for t in tokens:
            if t.type == "link_open":
                rewrite(t, "href", False)
            elif t.type == "image":
                rewrite(t, "src", True)
            if t.children:
                walk(t.children)

    def rule(state: Any) -> None:
        walk(state.tokens)

    md.core.ruler.push("canvas_links", rule)
    return md


def md_to_html_report(md: str, link_ctx: LinkContext) -> RenderResult:
    missing: list[str] = []
    html = _make_md(link_ctx, missing).render(md)
    return RenderResult(html=html, missing_links=sorted(set(missing)))


def md_to_html(md: str, link_ctx: LinkContext) -> str:
    return md_to_html_report(md, link_ctx).html
