"""canvas validate: validate_tree, fix_tree and the CLI. Offline; invented people only."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from canvas_sync.cli import app
from canvas_sync.commands.validate import Issue, fix_tree, has_errors, validate_tree
from canvas_sync.errors import ValidationError
from canvas_sync.model.roster import RosterCache
from tests.helpers.fixed_point import write_files

COURSE = "canvas_url: https://canvas.example.edu\ncourse_id: 1\ntimezone: America/Vancouver\n"
CACHE = RosterCache(students={"pat": 1, "sam": 2}, sections={"BA1": 10}, groups={"Team A": 20})


def tree(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    write_files(tmp_path, {"course.yaml": COURSE, **(files or {})})
    return tmp_path


def msgs(root: Path, cache: RosterCache | None = CACHE) -> list[str]:
    return [str(i) for i in validate_tree(root, cache=cache)]


def snapshot(root: Path) -> dict[Path, bytes]:
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_clean_tree(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "pages/a.md": "---\ntitle: A\n---\n\nSee [b](b.md) and [f](../files/x.pdf).\n",
            "pages/b.md": "---\ntitle: B\n---\n\nhi\n",
            "files/x.pdf": "%PDF",
            "modules.yaml": "- name: W1\n  items:\n    - page: a\n    - file: x.pdf\n",
        },
    )
    assert msgs(root) == []


def test_missing_cache_is_warning(tmp_path: Path) -> None:
    root = tree(tmp_path)
    issues = validate_tree(root)
    assert [i.severity for i in issues] == ["warning"]
    assert not has_errors(issues)
    assert str(issues[0]).startswith(".canvas/roster-cache.json: ")


def test_schema_errors_all_listed(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "pages/a.md": "---\ntitle: A\nbogus: 1\n---\n",
            "pages/b.md": "---\ntitle: B\npublish_at: nonsense\n---\n",
            "pages/c.md": "---\ntitle: C\n---\nfine\n",
        },
    )
    out = msgs(root)
    assert any(m.startswith("pages/a.md: ") and "bogus" in m for m in out)
    assert any(m.startswith("pages/b.md: ") for m in out)
    assert not any(m.startswith("pages/c.md") for m in out)


def test_duplicate_canvas_id(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {"pages/a.md": "---\ncanvas_id: s\n---\n", "pages/b.md": "---\ncanvas_id: s\n---\n"},
    )
    assert any("duplicate canvas_id" in m for m in msgs(root))


def test_bad_config(tmp_path: Path) -> None:
    write_files(tmp_path, {"course.yaml": "course_id: x\n"})
    out = msgs(tmp_path)
    assert len(out) == 1 and out[0].startswith("course.yaml: ")


def test_conflict_markers(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "pages/a.md": "---\ntitle: A\n---\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> other\n",
            "modules.yaml": "<<<<<<< HEAD\n- name: A\n=======\n- name: B\n>>>>>>> o\n",
            "pages/ok.md": "---\ntitle: ok\n---\n",
        },
    )
    out = msgs(root)
    assert "pages/a.md: unresolved git conflict markers" in out
    assert "modules.yaml: unresolved git conflict markers" in out
    assert len(out) == 2


def test_broken_refs(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "pages/a.md": "---\ntitle: A\n---\n[x](nope.md) ![i](../files/no.png) [ok](a.md)\n",
            "modules.yaml": (
                "- name: W1\n  items:\n    - page: ghost\n    - assignment: hw9\n"
                "    - file: z.pdf\n"
            ),
        },
    )
    out = msgs(root)
    assert sum(m.startswith("pages/a.md: broken link") for m in out) == 2
    assert any("page 'ghost'" in m for m in out)
    assert any("assignment 'hw9'" in m for m in out)
    assert any("file 'z.pdf'" in m for m in out)
    assert all(m.startswith(("pages/a.md: ", "modules.yaml: ")) for m in out)


def test_front_page_at_most_one(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "pages/a.md": "---\nfront_page: true\n---\n",
            "pages/b.md": "---\nfront_page: true\n---\n",
        },
    )
    assert msgs(root) == ["pages/b.md: front_page: true is also set on pages/a.md"]


HW = "---\ntitle: HW\noverrides:\n{ov}---\n"


def test_roster_names(tmp_path: Path) -> None:
    ov = (
        "  - section: BA9\n    due: 2025-01-01\n"
        "  - group: Team Z\n    due: 2025-01-01\n"
        "  - students: [pat, zed]\n    due: 2025-01-01\n"
    )
    root = tree(
        tmp_path, {"assignments/hw.md": HW.format(ov=ov), "roster.yaml": "sections: [BA1]\n"}
    )
    out = msgs(root)
    assert "assignments/hw.md: unknown section 'BA9'" in out
    assert "assignments/hw.md: unknown group 'Team Z'" in out
    assert "assignments/hw.md: unknown student 'zed'" in out
    assert len(out) == 3


def test_roster_without_cache_checks_only_listed_sections(tmp_path: Path) -> None:
    ov = "  - section: BA9\n    due: 2025-01-01\n  - group: G\n    due: 2025-01-01\n"
    root = tree(
        tmp_path, {"assignments/hw.md": HW.format(ov=ov), "roster.yaml": "sections: [BA1]\n"}
    )
    out = [i for i in validate_tree(root) if i.severity == "error"]
    assert [str(i) for i in out] == ["assignments/hw.md: unknown section 'BA9'"]


def test_override_rules(tmp_path: Path) -> None:
    ov = "  - section: BA1\n    due: 2025-01-01\n  - section: BA1\n    due: 2025-01-02\n"
    ov2 = "  - section: BA1\n    due: 2025-01-01\n    group: Team A\n"
    root = tree(
        tmp_path, {"assignments/a.md": HW.format(ov=ov), "assignments/b.md": HW.format(ov=ov2)}
    )
    out = msgs(root)
    assert any(m.startswith("assignments/a.md: ") and "BA1" in m for m in out)
    assert any(m.startswith("assignments/b.md: ") for m in out)


def test_date_order(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {"assignments/a.md": "---\nunlock: 2025-03-02\nlock: 2025-03-01\ndue: 2025-03-05\n---\n"},
    )
    issues = validate_tree(root, cache=CACHE)
    assert Issue("assignments/a.md", "unlock is after lock") in issues
    assert Issue("assignments/a.md", "due is after lock", "warning") in issues


def test_ambiguous_time_warns(tmp_path: Path) -> None:
    root = tree(tmp_path, {"assignments/a.md": "---\ndue: 2025-11-02 01:30\n---\n"})
    issues = validate_tree(root, cache=CACHE)
    assert [i.severity for i in issues] == ["warning"]


def test_canvas_dir_ignored(tmp_path: Path) -> None:
    root = tree(tmp_path, {".canvas/bodies.json": "{ not json", ".canvas/pages/x.md": "<<<<<<<\n"})
    assert msgs(root) == []


MESSY = "---\ntitle:   Hello\npublished: yes\n---\n\n\nBody  \r\n"


def test_fix_idempotent_and_canonical(tmp_path: Path) -> None:
    root = tree(
        tmp_path, {"pages/a.md": MESSY, "modules.yaml": "- {name: W, items: [{page: a}]}\n"}
    )
    changed = fix_tree(root)
    assert "pages/a.md" in changed and "modules.yaml" in changed
    after = snapshot(root)
    assert fix_tree(root) == []
    assert snapshot(root) == after
    assert (
        root / "pages/a.md"
    ).read_bytes() == b"---\ntitle: Hello\npublished: true\n---\n\nBody\n"
    assert msgs(root) == []


def test_fix_refuses_conflicts_and_bad_files(tmp_path: Path) -> None:
    root = tree(tmp_path, {"pages/a.md": "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> o\n"})
    before = snapshot(root)
    with pytest.raises(ValidationError):
        fix_tree(root)
    assert snapshot(root) == before
    root2 = tree(tmp_path / "b", {"pages/a.md": "---\nbogus: 1\n---\n"})
    with pytest.raises(ValidationError):
        fix_tree(root2)


def test_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tree(tmp_path, {"pages/a.md": "---\nbogus: 1\n---\n"})
    monkeypatch.chdir(root)
    res = CliRunner().invoke(app, ["validate"])
    assert res.exit_code == 1
    assert "pages/a.md: " in res.output
    write_files(root, {"pages/a.md": MESSY})
    res = CliRunner().invoke(app, ["validate", "--fix"])
    assert res.exit_code == 0, res.output
    assert "pages/a.md: rewritten in canonical form" in res.output
    assert "warning: .canvas/roster-cache.json" in res.output


@pytest.mark.parametrize(
    "rel",
    [
        "roster.yaml",
        "modules.yaml",
        "files/_meta.yaml",
        "assignments/a.md",
        "announcements/a.md",
        "pages/a.md",
    ],
)
def test_non_utf8_is_an_issue_not_a_crash(tmp_path: Path, rel: str) -> None:
    root = tree(tmp_path)
    write_files(root, {rel: b"\xff\xfe\x00bad\x80"})
    out = [i for i in validate_tree(root, cache=CACHE) if i.severity == "error"]
    assert out and all(i.path == rel for i in out)
    with pytest.raises(ValidationError):
        fix_tree(root)


def test_conflicted_course_yaml(tmp_path: Path) -> None:
    write_files(tmp_path, {"course.yaml": "<<<<<<< HEAD\na: 1\n=======\na: 2\n>>>>>>> o\n"})
    assert msgs(tmp_path) == ["course.yaml: unresolved git conflict markers"]


def test_lone_marker_in_code_block_is_not_a_conflict(tmp_path: Path) -> None:
    body = "---\ntitle: A\n---\n\n```\n<<<<<<< HEAD\n```\n"
    root = tree(tmp_path, {"pages/a.md": body})
    assert msgs(root) == []


def test_fix_warns_when_dropping_yaml_comments(tmp_path: Path) -> None:
    root = tree(tmp_path, {"modules.yaml": "# my notes\n- {name: W}\n"})
    seen: list[str] = []
    fix_tree(root, warn=seen.append)
    assert seen == ["modules.yaml: YAML comments are dropped when rewriting to canonical form"]
