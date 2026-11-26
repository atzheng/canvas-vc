from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from canvas_sync.errors import ValidationError
from canvas_sync.model.serialize import (
    dump_frontmatter,
    dump_yaml,
    load_frontmatter,
    load_yaml,
    normalize_body,
)
from tests.helpers.fixed_point import assert_deterministic_across_hash_seeds

VAN = ZoneInfo("America/Vancouver")


def d(data: dict[str, Any], order: list[str] | None = None) -> str:
    return dump_yaml(data, order or [])


# ---------------------------------------------------------------- quoting


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("Homework 1", "Homework 1"),
        ("Hello, world", "Hello, world"),
        ("Week 1 \u2013 Linear models", "Week 1 \u2013 Linear models"),
        ("caf\u00e9", "caf\u00e9"),
        ("https://example.com/a?b=c", "https://example.com/a?b=c"),
        ("2026-09-08 09:00", "2026-09-08 09:00"),  # same text as a datetime
        ("1st", "1st"),
        # must be quoted, or they would come back as non-strings / break the line
        ("yes", '"yes"'),
        ("No", '"No"'),
        ("on", '"on"'),
        ("OFF", '"OFF"'),
        ("true", '"true"'),
        ("null", '"null"'),
        ("~", '"~"'),
        ("", '""'),
        ("123", '"123"'),
        ("007", '"007"'),
        ("1.5", '"1.5"'),
        ("1e3", '"1e3"'),
        ("1_000", '"1_000"'),
        ("0x1F", '"0x1F"'),
        ("12:30", '"12:30"'),
        (".inf", '".inf"'),
        ("2026-09-08", '"2026-09-08"'),
        ("2026-09-08 09:00:00", '"2026-09-08 09:00:00"'),
        ("a: b", '"a: b"'),
        ("ends:", '"ends:"'),
        ("a #b", '"a #b"'),
        ("C#", '"C#"'),
        ("- item", '"- item"'),
        ("-", '"-"'),
        ("?x", '"?x"'),
        ("*x", '"*x"'),
        ("&x", '"&x"'),
        ("!x", '"!x"'),
        ("@x", '"@x"'),
        ("%x", '"%x"'),
        ("[x]", '"[x]"'),
        ("{x}", '"{x}"'),
        ("|", '"|"'),
        (">", '">"'),
        ("'q'", "\"'q'\""),
        ('"q"', '"\\"q\\""'),
        (" lead", '" lead"'),
        ("trail ", '"trail "'),
        ("tab\there", '"tab\\there"'),
        ("nbsp\u00a0x", '"nbsp\\xa0x"'),
        ("bom\ufeffx", '"bom\\ufeffx"'),
        ("ls\u2028x", '"ls\\u2028x"'),
        ("nel\x85x", '"nel\\x85x"'),
        ("cr\rx", '"cr\\x0dx"'),
        ("\U0001f600", '"\U0001f600"'),
        ("---", '"---"'),
        ("...", '"..."'),
    ],
)
def test_scalar_quoting(value: str, text: str) -> None:
    out = d({"k": value})
    assert out == f"k: {text}\n"
    assert load_yaml(out) == {"k": value}


def test_other_scalar_types() -> None:
    dt = datetime(2026, 9, 8, 9, 0, tzinfo=VAN)
    out = d({"a": True, "b": False, "c": 3, "d": 10.0, "e": 2.5, "f": dt, "g": None})
    assert out == "a: true\nb: false\nc: 3\nd: 10\ne: 2.5\nf: 2026-09-08 09:00\n"
    assert d({"f": datetime(2026, 9, 8, 23, 59, 59, tzinfo=VAN)}) == "f: 2026-09-08 23:59:59\n"
    with pytest.raises(ValidationError):
        d({"f": datetime(2026, 9, 8, 9, 0)})
    with pytest.raises(ValidationError):
        d({"f": float("nan")})
    with pytest.raises(TypeError):
        d({"f": object()})


def test_multiline_strings() -> None:
    assert d({"k": "a\nb"}) == "k: |-\n  a\n  b\n"
    assert d({"k": "a\n\nb\n"}) == "k: |\n  a\n\n  b\n"
    # not representable losslessly as a block scalar -> escaped double quotes
    for s in ["a \nb", " a\nb", "a\n\n", "\na", "a\tb\nc", "\n", "a\r\nb"]:
        out = d({"k": s})
        assert out.startswith('k: "')
        assert load_yaml(out) == {"k": s}
    for s in ["a\nb", "a\n\nb\n", "x\n  indented\ny", "caf\u00e9\nline"]:
        assert load_yaml(d({"k": s})) == {"k": s}
    assert dump_yaml(["a\nb"], []) == "- |-\n  a\n  b\n"


# ------------------------------------------------------------ structure


def test_key_order_and_sorted_rest() -> None:
    out = d({"z": 1, "b": 2, "a": 3, "title": "t", "canvas_id": 5}, ["canvas_id", "title"])
    assert out == "canvas_id: 5\ntitle: t\na: 3\nb: 2\nz: 1\n"


def test_nested_orders_and_sequences() -> None:
    data = [
        {
            "items": [{"title": "T", "page": "p", "canvas_id": 2}, {"header": "H"}],
            "name": "W1",
            "canvas_id": 1,
        }
    ]
    out = dump_yaml(
        data,
        ["canvas_id", "name", "items"],
        nested_orders={"items": ["canvas_id", "page", "header", "title"]},
    )
    assert out == (
        "- canvas_id: 1\n"
        "  name: W1\n"
        "  items:\n"
        "    - canvas_id: 2\n"
        "      page: p\n"
        "      title: T\n"
        "    - header: H\n"
    )
    assert load_yaml(out) == [
        {
            "canvas_id": 1,
            "name": "W1",
            "items": [{"canvas_id": 2, "page": "p", "title": "T"}, {"header": "H"}],
        }
    ]


def test_nested_mapping_in_item_and_none_dropped() -> None:
    out = d({"items": [{"quiz": {"canvas_id": 4321}, "canvas_id": 9, "x": None}]}, ["canvas_id"])
    assert out == "items:\n  - canvas_id: 9\n    quiz:\n      canvas_id: 4321\n"
    assert d({"a": {"b": None}}) == "a: {}\n"
    assert d({}) == "{}\n"
    assert dump_yaml([], []) == "[]\n"


def test_flow_vs_block_lists() -> None:
    assert d({"k": []}) == "k: []\n"
    assert d({"k": ["a", "b"]}) == "k: [a, b]\n"
    assert d({"k": ["a", "b", "c", "d"]}) == "k: [a, b, c, d]\n"
    assert d({"k": ["a", "b", "c", "d", "e"]}) == "k:\n  - a\n  - b\n  - c\n  - d\n  - e\n"
    long = ["x" * 30, "y" * 30, "z" * 30]
    assert d({"k": long}) == "k:\n" + "".join(f"  - {s}\n" for s in long)
    assert d({"k": ["a,b", "c: d", "[x]"]}) == 'k: ["a,b", "c: d", "[x]"]\n'
    assert d({"k": ["a\nb"]}) == "k:\n  - |-\n    a\n    b\n"
    assert d({"k": [["a"], ["b", "c"]]}) == "k:\n  - [a]\n  - [b, c]\n"
    assert d({"k": [{"a": 1}]}) == "k:\n  - a: 1\n"


def test_integral_and_float_values_load_back() -> None:
    for x in [10.0, 2.5, 1e22, 1.5e-7, 123456789.125, -3.0]:
        assert load_yaml(d({"k": x}))["k"] == x  # type: ignore[index]


def test_non_string_keys_rejected() -> None:
    with pytest.raises(TypeError):
        dump_yaml({1: "x"}, [])  # type: ignore[dict-item]


def test_output_independent_of_input_order() -> None:
    a = {"x": 1, "y": [{"q": 1, "p": 2}], "z": "s"}
    b = {"z": "s", "y": [{"p": 2, "q": 1}], "x": 1}
    assert d(a, ["x"]) == d(b, ["x"])


def test_hash_seed_independence() -> None:
    script = (
        "from canvas_sync.model.serialize import dump_yaml, dump_frontmatter\n"
        "data = {k: {j: {'b', 'a'} and ['x', 'y'] for j in 'qwerty'} for k in "
        "['delta','alpha','charlie','bravo','echo','foxtrot','golf']}\n"
        "data['set_keys'] = {w: 1 for w in {'one','two','three','four','five','six'}}\n"
        "print(dump_yaml(data, ['alpha']).encode().hex())\n"
        "print(dump_frontmatter(data['delta'], 'body', []).encode().hex())\n"
    )
    out = assert_deterministic_across_hash_seeds(script)
    assert len(out.splitlines()) == 2


# ----------------------------------------------------------- frontmatter


def test_frontmatter_dump_shape() -> None:
    assert dump_frontmatter(
        {"title": "T", "canvas_id": 1}, "Hello\n\n\n", ["canvas_id", "title"]
    ) == ("---\ncanvas_id: 1\ntitle: T\n---\n\nHello\n")
    assert dump_frontmatter({"title": "T"}, "", ["title"]) == "---\ntitle: T\n---\n"
    assert dump_frontmatter({"title": "T"}, " \n\t\n", ["title"]) == "---\ntitle: T\n---\n"
    assert dump_frontmatter({}, "x", []) == "---\n---\n\nx\n"
    assert dump_frontmatter({"a": None}, "x", []) == "---\n---\n\nx\n"


def test_frontmatter_body_trailing_whitespace_and_indent() -> None:
    out = dump_frontmatter({"t": 1}, "\n\n    code\nline  \n  \n\n", ["t"])
    assert out == "---\nt: 1\n---\n\n    code\nline\n"
    assert load_frontmatter(out)[1] == "    code\nline"


def test_load_frontmatter_basic_and_body_with_dashes() -> None:
    text = "---\ntitle: T\n---\n\nbody\n\n---\n\nmore: x\n---\n"
    meta, body = load_frontmatter(text)
    assert meta == {"title": "T"}
    assert body == "body\n\n---\n\nmore: x\n---"
    # round trips through dump
    again = dump_frontmatter(meta, body, ["title"])
    assert load_frontmatter(again) == (meta, body)
    assert dump_frontmatter(*load_frontmatter(again), ["title"]) == again


def test_body_starting_with_dashes_survives() -> None:
    out = dump_frontmatter({"t": 1}, "---\nfoo\n---\nbar", ["t"])
    assert load_frontmatter(out) == ({"t": 1}, "---\nfoo\n---\nbar")


def test_load_frontmatter_tolerant_input() -> None:
    canonical = "---\ntitle: T\n---\n\nline1\nline2\n"
    variants = [
        canonical,
        canonical.replace("\n", "\r\n"),
        "\ufeff" + canonical,
        "\ufeff" + canonical.replace("\n", "\r\n"),
        canonical.rstrip("\n"),  # no trailing newline
        canonical.replace("line1", "\n\nline1"),  # extra blank lines
        canonical.replace("---\n", "---  \n"),  # trailing spaces on delimiters
        canonical.replace("\n", "\r"),  # old Mac
    ]
    for v in variants:
        assert load_frontmatter(v) == ({"title": "T"}, "line1\nline2"), repr(v)


def test_load_frontmatter_no_frontmatter_and_empty() -> None:
    assert load_frontmatter("just text\n") == ({}, "just text")
    assert load_frontmatter("") == ({}, "")
    assert load_frontmatter("---\n---\n") == ({}, "")
    assert load_frontmatter("---\n---") == ({}, "")
    assert load_frontmatter("---\ntitle: T\n---") == ({"title": "T"}, "")


@pytest.mark.parametrize(
    "text",
    [
        "---\ntitle: T\nbody",
        "---\n- a\n- b\n---\n",
        "---\n1: x\n---\n",
        "---\na: [\n---\n",
        "---\na: 1\na: 2\n---\n",
    ],
)
def test_load_frontmatter_errors(text: str) -> None:
    with pytest.raises(ValidationError):
        load_frontmatter(text, "pages/x.md")


def test_error_mentions_source() -> None:
    with pytest.raises(ValidationError, match=r"pages/x\.md"):
        load_frontmatter("---\na: [\n---\n", "pages/x.md")


def test_load_tolerates_date_only_and_returns_raw_values() -> None:
    meta, _ = load_frontmatter("---\ndue: 2026-09-15\nopen: 2026-09-15 08:30\n---\n")
    # raw: type beads convert with dates.parse_local (accepts date and str)
    assert str(meta["due"]) == "2026-09-15"
    assert str(meta["open"]) == "2026-09-15 08:30"


def test_normalize_body() -> None:
    assert normalize_body("\r\n\r\n a\r\nb \r\n\r\n") == " a\nb"


# ------------------------------------------------------------ properties

scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(),
)
keys = st.text(min_size=1)
values = st.recursive(
    scalars,
    lambda inner: st.one_of(st.lists(inner, max_size=6), st.dictionaries(keys, inner, max_size=5)),
    max_leaves=25,
)


def _strip_none(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _strip_none(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [_strip_none(x) for x in v]
    return v


@settings(max_examples=300, deadline=None)
@given(st.dictionaries(keys, values, max_size=5))
def test_property_roundtrip_and_fixed_point(data: dict[str, Any]) -> None:
    out = dump_yaml(data, [])
    loaded = load_yaml(out)
    expected = _strip_none(data)
    assert loaded == (expected or {})
    assert out.endswith("\n") and not out.endswith("\n\n") and "\r" not in out
    assert "\ufeff" not in out
    # dumping what we loaded gives the same text (canonical form is a fixed point)
    assert dump_yaml(loaded, []) == out  # type: ignore[arg-type]


@settings(max_examples=200, deadline=None)
@given(st.text(), st.dictionaries(keys, scalars, max_size=4))
def test_property_frontmatter_fixed_point(body: str, meta: dict[str, Any]) -> None:
    text = dump_frontmatter(meta, body, [])
    m2, b2 = load_frontmatter(text)
    assert m2 == _strip_none(meta)
    assert b2 == normalize_body(body)
    assert dump_frontmatter(m2, b2, []) == text


@settings(max_examples=100, deadline=None)
@given(st.text(), st.sampled_from(["\n", "\r\n"]), st.booleans(), st.booleans())
def test_property_line_ending_and_bom_tolerance(
    body: str, eol: str, bom: bool, final_nl: bool
) -> None:
    canonical = dump_frontmatter({"title": "T"}, body, ["title"])
    messy = canonical.replace("\n", eol)
    if not final_nl:
        messy = messy.rstrip("\r\n")
    if bom:
        messy = "\ufeff" + messy
    # body text with its own CR / CRLF is normalised the same way in both paths
    assert dump_frontmatter(*load_frontmatter(messy), ["title"]) == dump_frontmatter(
        *load_frontmatter(canonical), ["title"]
    )


# ------------------------------------------- regression: trailing block scalar


@pytest.mark.parametrize("value", ["0\n", "a\nb", "a\n\nb\n", "x\n  indented\ny\n"])
def test_multiline_value_in_last_and_middle_key(value: str) -> None:
    for data in ({"a": 1, "z": value}, {"a": value, "z": 1}, {"0": value}):
        assert load_yaml(dump_yaml(data, [])) == data
        meta, body = load_frontmatter(dump_frontmatter(data, "", []))
        assert (meta, body) == (data, "")
        meta, body = load_frontmatter(dump_frontmatter(data, "body", []))
        assert (meta, body) == (data, "body")


def test_regression_last_key_block_scalar_keeps_newline() -> None:
    assert load_frontmatter(dump_frontmatter({"0": "0\n"}, "", []))[0] == {"0": "0\n"}


def test_none_and_absent_are_the_same() -> None:
    assert dump_yaml({"due": None}, []) == dump_yaml({}, [])
    assert dump_frontmatter({"due": None}, "b", []) == dump_frontmatter({}, "b", [])
    assert dump_yaml({"a": {"due": None}}, []) == dump_yaml({"a": {}}, [])


def test_duplicate_key_error_names_key_and_source() -> None:
    with pytest.raises(ValidationError, match=r"^pages/x\.md: duplicate key \"?title\"?$"):
        load_frontmatter("---\ntitle: a\nx: 1\ntitle: b\n---\n", "pages/x.md")
