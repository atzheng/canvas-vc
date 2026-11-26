"""model/files.py: effective settings, glob rules, minimal canonical _meta.yaml."""

from __future__ import annotations

import warnings
from datetime import datetime
from pathlib import Path

import pytest

from canvas_sync.config import CourseConfig
from canvas_sync.errors import ValidationError
from canvas_sync.model.files import (
    FILE_SPEC,
    FileEntry,
    FileRule,
    FileSettings,
    FilesMeta,
    glob_match,
)
from tests.helpers.fixed_point import assert_files_meta_fixed_point, write_files

FIXTURE = Path(__file__).parent / "fixtures" / "trees" / "files"


@pytest.fixture
def cfg() -> CourseConfig:
    return CourseConfig(
        canvas_url="https://canvas.example.edu",  # pyright: ignore[reportArgumentType]
        course_id=1,
        timezone="America/Vancouver",
    )


def dt(cfg: CourseConfig, y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=cfg.tz)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("*.pdf", "a.pdf", True),
        ("*.pdf", "x/y/a.pdf", True),  # no slash: any depth
        ("*.pdf", "a.pdfx", False),
        ("lectures/*.pdf", "lectures/01.pdf", True),
        ("lectures/*.pdf", "x/lectures/01.pdf", False),  # anchored
        ("lectures/*.pdf", "lectures/old/01.pdf", False),  # * does not cross /
        ("lectures/**/*.pdf", "lectures/01.pdf", True),
        ("lectures/**/*.pdf", "lectures/a/b/01.pdf", True),
        ("**/01.pdf", "01.pdf", True),
        ("**/01.pdf", "a/b/01.pdf", True),
        ("old/**", "old/a/b.pdf", True),
        ("old/**", "old", False),
        ("old", "old/a/b.pdf", True),  # directory selects contents
        ("old/", "old/a.pdf", True),
        ("old/", "x/old", False),  # trailing / needs a directory
        ("old", "older/a.pdf", False),
        ("hw?.pdf", "hw1.pdf", True),
        ("hw?.pdf", "hw10.pdf", False),
        ("hw[12].pdf", "hw2.pdf", True),
        ("hw[!12].pdf", "hw2.pdf", False),
        ("hw[!12].pdf", "hw3.pdf", True),
        ("/a.pdf", "a.pdf", True),
        ("/a.pdf", "x/a.pdf", False),  # leading / anchors
        ("a.pdf", "A.pdf", False),
        ("a[b", "a[b", True),  # unclosed class is literal
        ("a\\*b", "a*b", True),
        ("a\\*b", "axb", False),
        ("solutions/hw1-*", "solutions/hw1-solutions.ipynb", True),
    ],
)
def test_glob_match(pattern: str, path: str, expected: bool) -> None:
    assert glob_match(pattern, path) is expected


def test_effective_layers(cfg: CourseConfig) -> None:
    meta = FilesMeta(
        defaults=FileSettings(published=True, hidden=False),
        rules=[
            FileRule(match="lectures/*.pdf", settings=FileSettings(hidden=True)),
            FileRule(match="lectures/secret*", settings=FileSettings(published=False)),
            FileRule(match="lectures/01*", settings=FileSettings(hidden=False)),  # later wins
        ],
        files={
            "files/lectures/secret.pdf": FileEntry(
                path="lectures/secret.pdf", hidden=False, lock=dt(cfg, 2026, 12, 1, 9)
            )
        },
    )
    assert meta.effective("files/other.txt") == FileSettings(published=True, hidden=False)
    assert meta.effective("files/lectures/01.pdf").hidden is False
    assert meta.effective("files/lectures/02.pdf").hidden is True
    s = meta.effective("files/lectures/secret.pdf")
    assert (s.published, s.hidden, s.lock) == (False, False, dt(cfg, 2026, 12, 1, 9))
    assert s.unlock is None


def test_effective_empty() -> None:
    assert FilesMeta().effective("files/a.pdf") == FileSettings()


def test_dump_is_minimal(cfg: CourseConfig) -> None:
    meta = FilesMeta(
        defaults=FileSettings(published=True),
        rules=[FileRule(match="*.pdf", settings=FileSettings(hidden=True))],
        files={
            # settings equal to what the rules give: only id + hash are written
            "files/b.pdf": FileEntry(
                canvas_id=2, path="b.pdf", sha256="bb", published=True, hidden=True
            ),
            "files/a.txt": FileEntry(
                canvas_id=1, path="a.txt", sha256="aa", hidden=True, stable_link=True
            ),
        },
    )
    out = FILE_SPEC.dump_files_meta(meta, cfg)["files/_meta.yaml"].decode()
    assert out == (
        "defaults:\n"
        "  published: true\n"
        "rules:\n"
        '  - match: "*.pdf"\n'
        "    hidden: true\n"
        "files:\n"
        "  a.txt:\n"
        "    canvas_id: 1\n"
        "    sha256: aa\n"
        "    hidden: true\n"
        "    stable_link: true\n"
        "  b.pdf:\n"
        "    canvas_id: 2\n"
        "    sha256: bb\n"
    )


def test_dump_order_independent(cfg: CourseConfig) -> None:
    e1 = FileEntry(canvas_id=1, path="a.pdf")
    e2 = FileEntry(canvas_id=2, path="b.pdf")
    m1 = FilesMeta(files={"files/a.pdf": e1, "files/b.pdf": e2})
    m2 = FilesMeta(files={"files/b.pdf": e2, "files/a.pdf": e1})
    assert FILE_SPEC.dump_files_meta(m1, cfg) == FILE_SPEC.dump_files_meta(m2, cfg)


def test_empty_meta_writes_nothing(cfg: CourseConfig, tmp_path: Path) -> None:
    assert FILE_SPEC.dump_files_meta(FilesMeta(), cfg) == {}
    assert_files_meta_fixed_point(FILE_SPEC, FilesMeta(), cfg, tmp_path)  # type: ignore[arg-type]


def test_fixed_point_full(cfg: CourseConfig, tmp_path: Path) -> None:
    meta = FilesMeta(
        defaults=FileSettings(published=True, unlock=dt(cfg, 2026, 9, 1, 9)),
        rules=[
            FileRule(match="lectures/*.pdf", settings=FileSettings(hidden=True)),
            FileRule(match="solutions/**", settings=FileSettings(published=False)),
            FileRule(match="*.yaml: x", settings=FileSettings()),
        ],
        files={
            "files/lectures/01.pdf": FileEntry(
                canvas_id=5551212, path="lectures/01.pdf", sha256="ab" * 32
            ),
            "files/solutions/hw1.ipynb": FileEntry(
                canvas_id=9,
                path="solutions/hw1.ipynb",
                sha256="cd" * 32,
                published=True,
                lock=dt(cfg, 2026, 12, 20, 23, 59),
                stable_link=True,
            ),
            "files/published": FileEntry(canvas_id=3, path="published"),
            "files/empty.txt": FileEntry(path="empty.txt"),
        },
    )
    assert_files_meta_fixed_point(FILE_SPEC, meta, cfg, tmp_path)  # type: ignore[arg-type]


def test_load_fixture_normalizes(cfg: CourseConfig, tmp_path: Path) -> None:
    meta = FILE_SPEC.load_files_meta(FIXTURE, cfg)
    assert meta.defaults.published is True
    assert meta.rules[1].settings.unlock == dt(cfg, 2026, 9, 19, 9)  # date-only -> unlock_time
    e = meta.files["files/solutions/hw1-solutions.ipynb"]
    assert (e.canvas_id, e.path) == (5551299, "solutions/hw1-solutions.ipynb")
    assert e.lock == dt(cfg, 2026, 12, 20, 23, 59)
    text = FILE_SPEC.dump_files_meta(meta, cfg)["files/_meta.yaml"].decode()
    # the entry's unlock equals what the rule gives, so it is dropped; hidden: false differs
    assert text.count("unlock") == 1
    assert "hidden: false" in text
    assert text.index("lectures/01") < text.index("solutions/hw1-solutions")
    # the redundant entry setting is not minimal, so check the fixed point on the normalized form
    write_files(tmp_path, {"files/_meta.yaml": text})
    normalized = FILE_SPEC.load_files_meta(tmp_path, cfg)
    assert_files_meta_fixed_point(FILE_SPEC, normalized, cfg, tmp_path)  # type: ignore[arg-type]


def test_load_tolerates_crlf_and_missing_file(cfg: CourseConfig, tmp_path: Path) -> None:
    assert FILE_SPEC.load_files_meta(tmp_path, cfg) == FilesMeta()
    write_files(tmp_path, {"files/_meta.yaml": "defaults:\r\n  published: false\r\nfiles:\r\n"})
    assert FILE_SPEC.load_files_meta(tmp_path, cfg).defaults.published is False


def test_null_means_absent(cfg: CourseConfig, tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {"files/_meta.yaml": "defaults:\n  published: null\nfiles:\n  a.pdf: {canvas_id: null}\n"},
    )
    meta = FILE_SPEC.load_files_meta(tmp_path, cfg)
    assert meta.defaults == FileSettings()
    assert meta.files["files/a.pdf"].canvas_id is None


@pytest.mark.parametrize(
    "text",
    [
        "- a\n",
        "bogus: 1\n",
        "defaults:\n  colour: red\n",
        "defaults:\n  published: maybe\n",
        "rules:\n  - hidden: true\n",
        "rules: x\n",
        "rules:\n  - match: a\n    canvas_id: 1\n",
        "files:\n  a.pdf: {canvas_id: x}\n",
        "files:\n  ../a.pdf: {}\n",
        "files:\n  a.pdf: {lock: nonsense}\n",
        "files:\n  a.pdf: {nope: 1}\n",
    ],
)
def test_invalid_meta(cfg: CourseConfig, tmp_path: Path, text: str) -> None:
    write_files(tmp_path, {"files/_meta.yaml": text})
    with pytest.raises(ValidationError, match="_meta.yaml"):
        FILE_SPEC.load_files_meta(tmp_path, cfg)


def test_typespec_load_dump(cfg: CourseConfig, tmp_path: Path) -> None:
    objs = {
        "files/a.pdf": FileEntry(
            canvas_id=1,
            path="a.pdf",
            sha256="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
    }
    write_files(tmp_path, FILE_SPEC.dump(objs, cfg))  # type: ignore[arg-type]
    assert FILE_SPEC.load(tmp_path, cfg) == objs


def test_dump_rejects_non_tree_key(cfg: CourseConfig) -> None:
    with pytest.raises(ValueError, match="tree path"):
        FILE_SPEC.dump_files_meta(FilesMeta(files={"a.pdf": FileEntry()}), cfg)


def _load(cfg: CourseConfig, tmp_path: Path, text: str) -> FilesMeta:
    write_files(tmp_path, {"files/_meta.yaml": text})
    return FILE_SPEC.load_files_meta(tmp_path, cfg)


HEX = "a" * 64


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "files:\n  a.pdf: {canvas_id: 5}\n  b.pdf: {canvas_id: 5}\n",
            r"files\.b\.pdf: duplicate canvas_id 5 \(also in a\.pdf\)",
        ),
        ("files:\n  a.pdf: {canvas_id: -1}\n", "canvas_id"),
        ("files:\n  a.pdf: {sha256: ABC}\n", "sha256"),
        (f"files:\n  a.pdf: {{sha256: {'A' * 64}}}\n", "sha256"),
        (f"files:\n  a.pdf: {{sha256: {HEX}0}}\n", "sha256"),
        ("files:\n  A.pdf: {}\n  a.pdf: {}\n", "case-insensitive"),
        ('files:\n  "a\\\\b.pdf": {}\n', "invalid file path"),
        ("files:\n  sub/_meta.yaml: {}\n", "invalid file path"),
        ("files:\n  _meta.yaml: {}\n", "invalid file path"),
        ("rules:\n  - match: '[z-a]'\n", r"rules\[0\]: match: invalid glob '\[z-a\]'"),
        ("rules:\n  - match: ok\n  - match: '[a--b]'\n", r"rules\[1\]: match: invalid glob"),
        ("defaults:\n  lock: true\n", "lock"),
    ],
)
def test_more_invalid_meta(cfg: CourseConfig, tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _load(cfg, tmp_path, text)


def test_valid_sha_and_zero_id(cfg: CourseConfig, tmp_path: Path) -> None:
    meta = _load(cfg, tmp_path, f"files:\n  a.pdf: {{canvas_id: 0, sha256: {HEX}}}\n")
    assert meta.files["files/a.pdf"].sha256 == HEX


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("[]a]", "]", True),
        ("[]a]", "a", True),
        ("[]a]", "b", False),
        ("[!]a]", "b", True),
        ("[!]a]", "]", False),
        ("[[a]", "[", True),
        ("[[a]", "a", True),
        ("[a&&b]", "&", True),
        ("[a&&b]", "b", True),
        ("[a-c]", "b", True),
        ("[a-]", "-", True),
        ("a[/]b", "a/b", False),
    ],
)
def test_glob_class_edge_cases(pattern: str, path: str, expected: bool) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert glob_match(pattern, path) is expected


@pytest.mark.parametrize("pattern", ["[z-a]", "[a--b]"])
def test_glob_invalid_raises_value_error(pattern: str) -> None:
    with pytest.raises(ValueError, match="invalid glob"):
        glob_match(pattern, "a")


def test_no_date_overrides_rule(cfg: CourseConfig, tmp_path: Path) -> None:
    lock = dt(cfg, 2026, 12, 20, 23, 59)
    meta = FilesMeta(
        rules=[FileRule(match="solutions/**", settings=FileSettings(lock=lock))],
        files={
            "files/solutions/a.pdf": FileEntry(path="solutions/a.pdf", lock=False),
            "files/solutions/b.pdf": FileEntry(path="solutions/b.pdf"),
        },
    )
    assert meta.effective("files/solutions/a.pdf").lock is None
    assert meta.effective("files/solutions/b.pdf").lock == lock
    text = FILE_SPEC.dump_files_meta(meta, cfg)["files/_meta.yaml"].decode()
    assert "  solutions/a.pdf:\n    lock: false\n" in text
    assert text.count("false") == 1
    assert_files_meta_fixed_point(FILE_SPEC, meta, cfg, tmp_path)  # type: ignore[arg-type]
    (tmp_path / "x").mkdir()
    loaded = _load(cfg, tmp_path / "x", text)
    assert loaded.files["files/solutions/a.pdf"].lock is False


def test_no_date_in_rules_and_defaults(cfg: CourseConfig, tmp_path: Path) -> None:
    unlock = dt(cfg, 2026, 9, 1, 9)
    meta = FilesMeta(
        defaults=FileSettings(unlock=unlock),
        rules=[FileRule(match="early/**", settings=FileSettings(unlock=False))],
        files={"files/early/x.pdf": FileEntry(path="early/x.pdf", unlock=unlock)},
    )
    assert meta.effective("files/other.pdf").unlock == unlock
    assert meta.effective("files/early/y.pdf").unlock is None
    assert meta.effective("files/early/x.pdf").unlock == unlock  # entry re-sets a date
    assert_files_meta_fixed_point(FILE_SPEC, meta, cfg, tmp_path)  # type: ignore[arg-type]


def test_no_date_without_rule_is_not_written(cfg: CourseConfig) -> None:
    meta = FilesMeta(files={"files/a.pdf": FileEntry(path="a.pdf", canvas_id=1, lock=False)})
    assert "false" not in FILE_SPEC.dump_files_meta(meta, cfg)["files/_meta.yaml"].decode()


def test_effective_requires_prefix() -> None:
    with pytest.raises(ValueError, match="tree path"):
        FilesMeta().effective("a.pdf")


def test_nfc_normalization(cfg: CourseConfig, tmp_path: Path) -> None:
    nfd = "résumé.pdf"
    nfc = "résumé.pdf"
    assert glob_match(nfc, nfd) and glob_match(nfd, nfc)
    meta = _load(
        cfg,
        tmp_path,
        f'rules:\n  - match: "{nfc}"\n    hidden: true\nfiles:\n  "{nfd}": {{canvas_id: 4}}\n',
    )
    assert list(meta.files) == [f"files/{nfc}"]
    assert meta.effective(f"files/{nfd}").hidden is True
    assert meta.files[f"files/{nfc}"].path == nfc
    (tmp_path / "y").mkdir()
    assert_files_meta_fixed_point(FILE_SPEC, meta, cfg, tmp_path / "y")  # type: ignore[arg-type]


def test_nfc_collision_is_duplicate(cfg: CourseConfig, tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="collides"):
        _load(cfg, tmp_path, 'files:\n  "ré.pdf": {}\n  "ré.pdf": {}\n')


def test_redundant_setting_not_written_so_rule_edit_applies(cfg: CourseConfig) -> None:
    old = FilesMeta(
        rules=[FileRule(match="*.pdf", settings=FileSettings(hidden=True))],
        files={"files/a.pdf": FileEntry(path="a.pdf", canvas_id=1, hidden=True)},
    )
    text = FILE_SPEC.dump_files_meta(old, cfg)["files/_meta.yaml"].decode()
    assert text.count("hidden") == 1  # only the rule, not the entry
    new = FilesMeta(
        rules=[FileRule(match="*.pdf", settings=FileSettings(hidden=False))],
        files={"files/a.pdf": FileEntry(path="a.pdf", canvas_id=1)},
    )
    assert new.effective("files/a.pdf").hidden is False
