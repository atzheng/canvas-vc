"""Progress hook for fetch/pull (bead vtr.4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from canvas_sync import progress as progress_mod
from canvas_sync.cli import app
from canvas_sync.commands import fetch as fetch_mod
from canvas_sync.commands.clone import clone_course
from canvas_sync.commands.fetch import run_fetch
from canvas_sync.config import load_config
from canvas_sync.progress import NullProgress, rich_progress
from tests.fake_canvas.assignments import add_assignment
from tests.fake_canvas.core import FakeCanvas
from tests.fake_canvas.pages import add_page


class Recording:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def phase(self, name: str, total: int | None = None) -> None:
        self.events.append(("phase", name, total))

    def advance(self, n: int = 1, label: str | None = None) -> None:
        self.events.append(("advance", n, label))

    def bytes(self, done: int, total: int) -> None:
        self.events.append(("bytes", done, total))


def make_fake() -> FakeCanvas:
    f = FakeCanvas()
    add_page(f, "Syllabus", "<p>hi</p>")
    add_page(f, "Other", "<p>o</p>")
    add_assignment(f, "Homework 1", points_possible=10)
    folder = next(iter(f.folders.values()))
    f.add_file(int(folder["id"]), "a.txt", b"12345")
    f.add_file(int(folder["id"]), "b.txt", b"1234567")
    return f


def fresh_repo(fake: FakeCanvas, tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    clone_course(fake.client("read"), "https://canvas.test", 101, root, "America/Vancouver")
    return root


def log(fake: FakeCanvas) -> list[tuple[str, str, str]]:
    return [(r.method, r.path, repr(sorted(r.params.items()))) for r in fake.log]


def test_progress_does_not_change_requests(tmp_path: Path) -> None:
    runs: list[list[tuple[str, str, str]]] = []
    for name, prog in (("a", NullProgress()), ("b", Recording())):
        fake = make_fake()
        root = fresh_repo(fake, tmp_path, name)
        mark = len(fake.log)
        run_fetch(root, fake.client("read"), load_config(root), prog)
        runs.append(log(fake)[mark:])
    assert runs[0] == runs[1] and runs[0]


def test_recording_sees_every_phase_with_totals(tmp_path: Path) -> None:
    fake = make_fake()
    root = fresh_repo(fake, tmp_path, "c")
    rec = Recording()
    run_fetch(root, fake.client("read"), load_config(root), rec)
    phases = [(e[1], e[2]) for e in rec.events if e[0] == "phase"]
    assert phases[0] == ("roster", None)
    for want in [("pages", 2), ("assignments", 1), ("announcements", 0), ("files", 2)]:
        assert want in phases
    assert ("modules", None) in phases
    byte_events = [e for e in rec.events if e[0] == "bytes"]
    assert byte_events[-1] == ("bytes", 12, 12)


def test_non_tty_is_quiet(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_fake()
    root = fresh_repo(fake, tmp_path, "d")
    with rich_progress() as p:  # pytest's stderr is not a TTY
        assert isinstance(p, NullProgress)
        run_fetch(root, fake.client("read"), load_config(root), p)
    assert capsys.readouterr().err == ""


def test_quiet_flag_suppresses_even_on_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = make_fake()
    root = fresh_repo(fake, tmp_path, "e")
    monkeypatch.setattr(fetch_mod, "get_token", lambda cfg: "t")
    monkeypatch.setattr(
        fetch_mod, "CanvasClient", lambda url, token, course_id, mode: fake.client(mode)
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr(progress_mod.sys.stderr, "isatty", lambda: True, raising=False)
    made: list[bool] = []
    real = progress_mod.RichProgress.__init__

    def spy(self: progress_mod.RichProgress, console: object) -> None:
        made.append(True)
        real(self, console)  # type: ignore[arg-type]

    monkeypatch.setattr(progress_mod.RichProgress, "__init__", spy)
    res = CliRunner().invoke(app, ["fetch", "--quiet"])
    assert res.exit_code == 0, res.output
    assert made == []


def test_rich_renderer_runs_on_tty(tmp_path: Path) -> None:
    import io

    from rich.console import Console

    fake = make_fake()
    root = fresh_repo(fake, tmp_path, "f")
    buf = io.StringIO()
    with progress_mod.RichProgress(Console(file=buf, force_terminal=True)) as p:
        run_fetch(root, fake.client("read"), load_config(root), p)
