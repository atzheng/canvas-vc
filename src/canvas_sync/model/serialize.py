"""Canonical YAML and frontmatter (CONTRACTS.md section 5)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def dump_yaml(data: Mapping[str, Any] | Sequence[Any], key_order: Sequence[str]) -> str:
    raise NotImplementedError


def dump_frontmatter(meta: Mapping[str, Any], body: str, key_order: Sequence[str]) -> str:
    raise NotImplementedError


def load_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    raise NotImplementedError
