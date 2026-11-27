"""canvas open: URL building (offline) and the --print flag. Invented ids only."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from canvas_sync.cli import app
from canvas_sync.commands.open import canvas_url_for
from canvas_sync.errors import ValidationError
from tests.helpers.fixed_point import write_files

BASE = "https://canvas.example.edu/courses/12345"
SHA = "a" * 64
COURSE = "canvas_url: https://canvas.example.edu\ncourse_id: 12345\ntimezone: America/Vancouver\n"
META = f"files:\n  lectures/01.pdf: {{canvas_id: 5551212, sha256: '{SHA}'}}\n"
MODULES = "- name: Week 1\n  canvas_id: 4433\n  items: []\n- name: Week 1\n  canvas_id: 4434\n  items: []\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_files(
        tmp_path,
        {
            "course.yaml": COURSE,
            "pages/syllabus.md": "---\ncanvas_id: syllabus-2\ntitle: Syllabus\n---\n\nhi\n",
            "pages/new.md": "---\ntitle: New\n---\n\nhi\n",
            "assignments/hw1.md": "---\ncanvas_id: 998877\ntitle: HW1\n---\n\nhi\n",
            "announcements/welcome.md": "---\ncanvas_id: 776655\ntitle: Welcome\n---\n\nhi\n",
            "files/lectures/01.pdf": "%PDF",
            "files/_meta.yaml": META,
            "modules.yaml": MODULES,
        },
    )
    return tmp_path


@pytest.mark.parametrize(
    ("path", "url"),
    [
        ("pages/syllabus.md", f"{BASE}/pages/syllabus-2"),
        ("assignments/hw1.md", f"{BASE}/assignments/998877"),
        ("announcements/welcome.md", f"{BASE}/discussion_topics/776655"),
        ("files/lectures/01.pdf", f"{BASE}/files/5551212"),
        ("modules.yaml#Week 1", f"{BASE}/modules#module_4433"),
        ("modules.yaml#Week 1#2", f"{BASE}/modules#module_4434"),
        ("modules.yaml", f"{BASE}/modules"),
        ("course.yaml", BASE),
        (".", BASE),
    ],
)
def test_urls(root: Path, path: str, url: str) -> None:
    assert canvas_url_for(root, path) == url


def test_absolute_path(root: Path) -> None:
    assert canvas_url_for(root, str(root / "assignments/hw1.md")) == f"{BASE}/assignments/998877"


@pytest.mark.parametrize(
    "path", ["pages/new.md", "pages/nope.md", "files/nope.pdf", "modules.yaml#Nope", "README.md"]
)
def test_errors(root: Path, path: str) -> None:
    with pytest.raises(ValidationError):
        canvas_url_for(root, path)


def test_cli_print(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(root)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    r = CliRunner().invoke(app, ["open", "--print", "assignments/hw1.md"])
    assert r.exit_code == 0
    assert r.output.strip() == f"{BASE}/assignments/998877"
    assert opened == []
    r = CliRunner().invoke(app, ["open", "assignments/hw1.md"])
    assert r.exit_code == 0
    assert opened == [f"{BASE}/assignments/998877"]


def test_cli_error(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(root)
    r = CliRunner().invoke(app, ["open", "--print", "pages/new.md"])
    assert r.exit_code == 1
    assert "not on Canvas yet" in r.output
