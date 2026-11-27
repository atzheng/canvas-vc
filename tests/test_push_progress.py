"""Progress hook for push (bead vtr.5): reporting only."""

from __future__ import annotations

import io
import re
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from canvas_sync.commands.clone import clone_course
from canvas_sync.commands.push import run_push
from canvas_sync.config import load_config
from canvas_sync.errors import GuardBlocked
from canvas_sync.progress import NullProgress, Progress
from tests.fake_canvas.assignments import add_assignment
from tests.fake_canvas.core import FakeCanvas
from tests.fake_canvas.pages import add_page
from tests.test_progress import Recording
from tests.test_push import commit, edit, write


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def build(tmp_path: Path, name: str, scenario: str) -> tuple[FakeCanvas, Path]:
    fake = FakeCanvas()
    hw = add_assignment(fake, "Homework 1", points_possible=10, has_submitted_submissions=True)
    fake.seed_submissions(
        hw["id"],
        [{"user_id": 7001, "workflow_state": "submitted", "score": 8, "attempt": 1}],
    )
    add_page(fake, "Syllabus", "<p>hi</p>")
    root = tmp_path / name
    clone_course(fake.client("read"), "https://canvas.test", 101, root, "America/Vancouver")
    if scenario == "guard":
        edit(root, "assignments/homework-1.md", "points: 10", "points: 99")
    else:
        write(root, "pages/a-first.md", "---\ntitle: A First\n---\nOne\n")
        write(root, "pages/b-second.md", "---\ntitle: B Second\n---\nTwo\n")
        (root / "files").mkdir(exist_ok=True)
        (root / "files/x.txt").write_bytes(b"12345")
    commit(root)
    if scenario == "fail":
        real = fake.handle_request
        state = {"n": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/pages"):
                state["n"] += 1
                if state["n"] == 2:
                    return httpx.Response(500, json={"errors": "boom"})
            return real(request)

        fake.handle_request = flaky  # type: ignore[method-assign]
    return fake, root


def run(fake: FakeCanvas, root: Path, prog: Progress) -> tuple[list[tuple[str, str]], str, object]:
    buf = io.StringIO()
    mark = len(fake.log)
    outcome: object
    try:
        res = run_push(
            root,
            fake.client("write"),
            load_config(root),
            yes=True,
            console=Console(file=buf, width=200),
            progress=prog,
        )
        outcome = (res.ops_done, res.ops_total, res.ok)
    except GuardBlocked as e:
        outcome = str(e)
    return [(r.method, r.path) for r in fake.log[mark:]], buf.getvalue(), outcome


@pytest.mark.parametrize("scenario", ["ok", "guard", "fail"])
def test_identical_requests_and_output(tmp_path: Path, scenario: str) -> None:
    a = run(*build(tmp_path, "a", scenario), NullProgress())
    rec = Recording()
    b = run(*build(tmp_path, "b", scenario), rec)
    assert a[0] == b[0] and a[0]
    assert re.sub(r"[0-9a-f]{7}", "H", a[1]) == re.sub(r"[0-9a-f]{7}", "H", b[1])
    assert a[2] == b[2]
    if scenario == "guard":
        assert rec.events == []
        assert isinstance(b[2], str) and "--confirm-grading-change" in b[2]
    if scenario == "fail":
        assert isinstance(b[2], tuple) and not b[2][2]


def test_recording_sees_ops_in_plan_order(tmp_path: Path) -> None:
    rec = Recording()
    out = run(*build(tmp_path, "c", "ok"), rec)
    assert out[2] == (3, 3, True)
    ev = rec.events
    first = ev.index(("phase", "pushing", 3))
    labels = [str(e[2]) for e in ev[first:] if e[0] == "advance" and e[1] == 0]
    assert [x.split("] ", 1)[0] for x in labels] == ["[1/3", "[2/3", "[3/3"]
    paths = [x.split(" ", 2)[2] for x in labels]
    assert sorted(paths) == paths or "files/x.txt" in paths
    assert sorted(paths) == sorted(["files/x.txt", "pages/a-first.md", "pages/b-second.md"])
    end = next(i for i, e in enumerate(ev[first + 1 :], first + 1) if e[0] == "phase")
    assert sum(int(str(e[1])) for e in ev[first:end] if e[0] == "advance") == 3
    assert ("bytes", 5, 5) in ev
    # the post-push fetch reuses the same progress object
    assert any(e[0] == "phase" and e[1] == "roster" for e in ev[first:])


def test_failure_stops_display_at_failing_op(tmp_path: Path) -> None:
    rec = Recording()
    out = run(*build(tmp_path, "d", "fail"), rec)
    assert isinstance(out[2], tuple) and out[2][2] is False
    done = sum(int(str(e[1])) for e in rec.events if e[0] == "advance")
    assert done == out[2][0]
    assert "push stopped at" in out[1]


class Exploding:
    """A progress whose every call raises, like a closed stderr."""

    def phase(self, name: str, total: int | None = None) -> None:
        raise BrokenPipeError

    def advance(self, n: int = 1, label: str | None = None) -> None:
        raise BrokenPipeError

    def bytes(self, done: int, total: int) -> None:
        raise BrokenPipeError


@pytest.mark.parametrize("scenario", ["ok", "fail"])
def test_raising_progress_changes_nothing(tmp_path: Path, scenario: str) -> None:
    a = run(*build(tmp_path, "a", scenario), NullProgress())
    b = run(*build(tmp_path, "b", scenario), Exploding())
    assert a[0] == b[0] and a[2] == b[2]


def test_prompt_is_intact_before_the_bar_starts() -> None:
    from canvas_sync.progress import RichProgress

    buf = io.StringIO()
    with RichProgress(Console(file=buf, force_terminal=True)) as p:
        assert buf.getvalue() == ""  # entering draws nothing, so a prompt is not erased
        p.phase("pushing", 1)
        assert buf.getvalue() != ""  # the bar starts at the first phase
