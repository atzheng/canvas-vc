"""Endpoints canvas-sync may call (CONTRACTS.md section 7).

Never widen this without saying why in the PR body. Stub: empty until the
allowlist bead fills it in, so every call is refused.
"""

from __future__ import annotations

ALLOWED: list[tuple[str, str]] = []  # (METHOD, path regex anchored at both ends)
