"""Canonical YAML and frontmatter (CONTRACTS.md section 5).

Git-style merging only works if fetch writes byte-stable text, so output is
produced by a small hand-written emitter rather than a general YAML dumper:
every formatting decision (key order, quoting, list style, line endings) is
made here and nowhere else.

Output rules
------------
* block style, 2-space indent, LF, UTF-8, one trailing ``\\n``;
* keys in ``key_order``, then the remaining keys sorted; ``None`` values in a
  mapping are omitted (the caller already dropped course defaults);
* strings are plain only when that is provably safe (see ``_plain_ok``),
  otherwise double-quoted; multi-line strings use a ``|`` block scalar when
  that is lossless, else double-quoted with escapes;
* lists of at most 4 single-line scalars that fit in 80 columns use flow
  style, every other list is one item per line;
* datetimes are written with ``format_local``; integral floats as ints.

Typing note: ruamel.yaml has no usable type information, so every call into
it is confined to ``load_yaml`` and ``_parse_scalar`` below, each with one
targeted pyright ignore. python-frontmatter is *not* used: splitting the
frontmatter here keeps the CRLF/BOM/``---``-in-body rules in one tested place.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from functools import lru_cache
from typing import Any, cast

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from canvas_sync.dates import format_local
from canvas_sync.errors import ValidationError

__all__ = [
    "dump_frontmatter",
    "dump_yaml",
    "load_frontmatter",
    "load_yaml",
    "normalize_body",
    "normalize_newlines",
]

_FLOW_MAX_ITEMS = 4
_FLOW_MAX_COLUMNS = 80

# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------


def normalize_newlines(text: str) -> str:
    """Drop a leading BOM and convert CRLF / lone CR to LF."""
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


_LEADING_BLANK_LINES = re.compile(r"\A(?:[ \t]*\n)+")


def normalize_body(body: str) -> str:
    """Canonical body: LF, no leading blank lines, no trailing whitespace."""
    return _LEADING_BLANK_LINES.sub("", normalize_newlines(body)).rstrip()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_yaml(text: str, source: str = "<yaml>") -> object:
    """Parse YAML text tolerantly (BOM, CRLF). Raises ValidationError on bad YAML.

    Returns plain Python data (dict/list/str/int/float/bool/None, plus
    ``date``/``datetime`` for unquoted timestamps). An empty document is None.
    """
    try:
        # ruamel.yaml is untyped: its load() returns Any with unknown members.
        return YAML(typ="safe").load(normalize_newlines(text))  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    except YAMLError as e:
        first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        raise ValidationError(f"{source}: invalid YAML: {first}") from None


def load_frontmatter(text: str, source: str = "<frontmatter>") -> tuple[dict[str, Any], str]:
    """Split ``---`` frontmatter from the body and parse it.

    Tolerates a BOM, CRLF and a missing trailing newline. Only the *first*
    closing ``---`` line ends the frontmatter, so the body may contain
    ``---``. A file that does not start with ``---`` has empty metadata and
    the whole text as its body. Raises ValidationError for an unterminated or
    non-mapping frontmatter block.
    """
    text = normalize_newlines(text)
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return {}, normalize_body(text)
    for end in range(1, len(lines)):
        if lines[end].rstrip() == "---":
            break
    else:
        raise ValidationError(f"{source}: frontmatter is not closed by a '---' line")
    raw = load_yaml("\n".join(lines[1:end]), source)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValidationError(f"{source}: frontmatter must be a mapping")
    meta = cast("dict[Any, Any]", raw)
    for k in meta:
        if not isinstance(k, str):
            raise ValidationError(f"{source}: frontmatter key {k!r} is not a string")
    return cast("dict[str, Any]", meta), normalize_body("\n".join(lines[end + 1 :]))


# ---------------------------------------------------------------------------
# Scalar formatting
# ---------------------------------------------------------------------------

# YAML 1.1 words that older parsers read as bool/null; quoted so output is
# unambiguous for any consumer, not just ruamel (YAML 1.2).
_RESERVED_WORDS = frozenset(
    {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~", "<<", "="}
)
_NUMBERISH = re.compile(
    r"""^[-+]?(?:
        [0-9][0-9_]*(?::[0-5]?[0-9])+(?:\.[0-9_]*)?   # 1.1 sexagesimal
      | (?:[0-9][0-9_]*)?\.?[0-9_]*(?:[eE][-+]?[0-9]+)?  # ints / floats, with _
      | 0[xXoObB][0-9a-fA-F_]+
      | \.(?:inf|Inf|INF|nan|NaN|NAN)
      )$""",
    re.VERBOSE,
)
_FLOW_UNSAFE = frozenset(",[]{}:")


@lru_cache(maxsize=4096)
def _parses_as_same_str(s: str) -> bool:
    """True if ``s`` written plain is read back by YAML 1.2 as exactly ``s``."""
    try:
        got = YAML(typ="safe").load(s)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    except YAMLError:
        return False
    return isinstance(got, str) and got == s


def _plain_ok(s: str, *, in_flow: bool) -> bool:
    """Can ``s`` be written without quotes and be read back as the same str?"""
    if not s or s != s.strip(" ") or not s.isprintable():
        return False
    first = s[0]
    if not (first.isalnum() or first == "_"):
        return False
    if "#" in s:
        return False
    # ": " and a trailing ":" end a key; a lone ":x" is fine in block context.
    if ": " in s or s.endswith(":"):
        return False
    if in_flow and any(c in _FLOW_UNSAFE for c in s):
        return False
    if s.lower() in _RESERVED_WORDS or _NUMBERISH.match(s):
        return False
    return _parses_as_same_str(s)


def _escape_char(c: str) -> str:
    o = ord(c)
    if c == "\\":
        return "\\\\"
    if c == '"':
        return '\\"'
    if c == "\n":
        return "\\n"
    if c == "\t":
        return "\\t"
    if c.isprintable() and c != "﻿":
        return c
    if o <= 0xFF:
        return f"\\x{o:02x}"
    if o <= 0xFFFF:
        return f"\\u{o:04x}"
    return f"\\U{o:08x}"


def _double_quote(s: str) -> str:
    return '"' + "".join(_escape_char(c) for c in s) + '"'


def _block_scalar_ok(s: str) -> bool:
    """Can a multi-line string be written as a ``|`` block scalar losslessly?"""
    if "\n" not in s or s.endswith("\n\n") or s.startswith(("\n", " ")):
        return False
    if any(not c.isprintable() and c != "\n" for c in s):
        return False
    return s.strip() != "" and all(line == line.rstrip(" ") for line in s.split("\n"))


def _scalar_lines(s: str, content_indent: int, *, in_flow: bool = False) -> list[str]:
    """Lines for a string value: one line, or a block scalar header plus content."""
    if "\n" in s:
        if not _block_scalar_ok(s):
            return [_double_quote(s)]
        chomp = "|" if s.endswith("\n") else "|-"
        body = s[:-1] if s.endswith("\n") else s
        pad = " " * content_indent
        return [chomp] + [(pad + line) if line else "" for line in body.split("\n")]
    return [s if _plain_ok(s, in_flow=in_flow) else _double_quote(s)]


def _inline_scalar(v: object, *, in_flow: bool = False) -> str | None:
    """Single-line text for a non-string scalar or string, None if not a scalar."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(int(v))
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValidationError(f"cannot write non-finite number {v!r}")
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            raise ValidationError(f"cannot write naive datetime {v!r}; dates must be zone-aware")
        return format_local(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, str):
        s = str(v)
        if "\n" in s:
            return None
        return s if _plain_ok(s, in_flow=in_flow) else _double_quote(s)
    return None


def _is_flow_list(seq: Sequence[Any]) -> list[str] | None:
    """Items as flow-style text if ``seq`` qualifies for flow style, else None."""
    if len(seq) > _FLOW_MAX_ITEMS:
        return None
    out: list[str] = []
    for item in seq:
        text = _inline_scalar(item, in_flow=True)
        if text is None:
            return None
        out.append(text)
    return out


# ---------------------------------------------------------------------------
# Block emitter
# ---------------------------------------------------------------------------

_Orders = Mapping[str, Sequence[str]]


def _ordered_keys(m: Mapping[str, Any], order: Sequence[str]) -> list[str]:
    for k in m:
        if not isinstance(k, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"YAML keys must be strings, got {k!r}")
    wanted = [k for k in order if k in m]
    seen = set(wanted)
    return wanted + sorted(k for k in m if k not in seen)


def _is_container(v: object) -> bool:
    return isinstance(v, Mapping) or (isinstance(v, Sequence) and not isinstance(v, str | bytes))


def _emit(value: Any, indent: int, order: Sequence[str], nested: _Orders) -> list[str]:
    """Block lines for a mapping or sequence whose first line starts at ``indent``."""
    if isinstance(value, Mapping):
        return _emit_mapping(cast("Mapping[str, Any]", value), indent, order, nested)
    return _emit_sequence(cast("Sequence[Any]", value), indent, order, nested)


def _value_after_key(
    prefix: str, v: Any, indent: int, order: Sequence[str], nested: _Orders, key: str | None
) -> list[str]:
    """Lines for ``prefix`` (``"k:"`` or ``"-"``) followed by value ``v``."""
    child_order = nested.get(key, order) if key is not None else order
    if isinstance(v, str):
        first, *rest = _scalar_lines(str(v), indent + 2)
        return [f"{prefix} {first}", *rest]
    if isinstance(v, Mapping):
        m = cast("Mapping[str, Any]", v)
        if not any(x is not None for x in m.values()):
            return [f"{prefix} {{}}"]
        return [prefix, *_emit_mapping(m, indent + 2, child_order, nested)]
    if _is_container(v):
        seq = cast("Sequence[Any]", v)
        if not seq:
            return [f"{prefix} []"]
        flow = _is_flow_list(seq)
        if flow is not None:
            line = f"{prefix} [{', '.join(flow)}]"
            if len(" " * indent + line) <= _FLOW_MAX_COLUMNS:
                return [line]
        return [prefix, *_emit_sequence(seq, indent + 2, child_order, nested)]
    text = _inline_scalar(v)
    if text is None:
        raise TypeError(f"cannot serialize {type(v).__name__} value {v!r}")
    return [f"{prefix} {text}"]


def _quote_key(k: str) -> str:
    return k if _plain_ok(k, in_flow=False) else _double_quote(k)


def _emit_mapping(
    m: Mapping[str, Any], indent: int, order: Sequence[str], nested: _Orders
) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    for k in _ordered_keys(m, order):
        v = m[k]
        if v is None:
            continue
        sub = _value_after_key(f"{_quote_key(k)}:", v, indent, order, nested, k)
        lines.extend(pad + line if i == 0 else line for i, line in enumerate(sub))
    return lines


def _emit_sequence(
    seq: Sequence[Any], indent: int, order: Sequence[str], nested: _Orders
) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    for item in seq:
        if isinstance(item, Mapping) and any(
            x is not None for x in cast("Mapping[str, Any]", item).values()
        ):
            # "- " + first key line; remaining keys align under it at indent + 2.
            sub = _emit_mapping(cast("Mapping[str, Any]", item), indent + 2, order, nested)
            sub[0] = pad + "- " + sub[0][indent + 2 :]
            lines.extend(sub)
        elif isinstance(item, Sequence) and not isinstance(item, str | bytes) and _is_nonflow(item):
            sub = _emit_sequence(cast("Sequence[Any]", item), indent + 2, order, nested)
            sub[0] = pad + "- " + sub[0][indent + 2 :]
            lines.extend(sub)
        else:
            sub = _value_after_key("-", item, indent, order, nested, None)
            lines.extend(pad + line if i == 0 else line for i, line in enumerate(sub))
    return lines


def _is_nonflow(item: Any) -> bool:
    seq = cast("Sequence[Any]", item)
    return bool(seq) and _is_flow_list(seq) is None


def dump_yaml(
    data: Mapping[str, Any] | Sequence[Any],
    key_order: Sequence[str],
    *,
    nested_orders: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """Canonical YAML text for ``data`` (see module docstring for the rules).

    ``key_order`` orders the keys of every mapping. ``nested_orders`` overrides
    it for mappings found under a given key name, e.g. ``{"items": ITEM_ORDER}``
    for a module list, so each level can have its own order. Keys in neither
    are sorted after the listed ones.
    """
    nested = nested_orders or {}
    lines = _emit(data, 0, key_order, nested)
    if not lines:
        return "{}\n" if isinstance(data, Mapping) else "[]\n"
    return "\n".join(lines) + "\n"


def dump_frontmatter(
    meta: Mapping[str, Any],
    body: str,
    key_order: Sequence[str],
    *,
    nested_orders: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """``---`` frontmatter, a blank line, then the normalised body.

    The body is passed through ``normalize_body`` so that
    ``dump(load(dump(x))) == dump(x)``. An empty body leaves out the blank
    line and the body entirely; empty metadata gives ``---\\n---\\n``.
    """
    lines = _emit_mapping(meta, 0, key_order, nested_orders or {})
    out = "---\n" + "".join(line + "\n" for line in lines) + "---\n"
    body = normalize_body(body)
    if body:
        out += "\n" + body + "\n"
    return out
