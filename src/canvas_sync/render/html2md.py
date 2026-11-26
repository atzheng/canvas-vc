"""Canvas HTML -> Markdown via pandoc (CONTRACTS.md section 6)."""

from __future__ import annotations

from canvas_sync.render.links import LinkContext


def html_to_md(html: str, link_ctx: LinkContext) -> str:
    raise NotImplementedError
