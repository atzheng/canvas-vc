"""Fetch stability over FakeCanvas with every object type (bead 6fk.6).

The point: fetch twice with no remote change makes no commit, a remote edit changes only
the file that was edited, and nothing a fetch writes can make a later merge conflict.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from canvas_sync import gitops
from canvas_sync.commands.clone import clone_course
from canvas_sync.commands.fetch import run_fetch
from canvas_sync.config import CourseConfig, load_config
from canvas_sync.model.course import dump_tree, load_tree
from tests.fake_canvas.assignments import add_assignment, add_assignment_group, add_override
from tests.fake_canvas.core import FakeCanvas
from tests.fake_canvas.discussion_topics import add_announcement
from tests.fake_canvas.modules import add_item, add_module
from tests.fake_canvas.pages import add_page, rename_page

URL = "https://canvas.test"
TZ = "America/Vancouver"
STUDENT_NAMES = ["Zelda Fakename", "Quentin Madeup"]
STUDENT_LOGINS = ["zfake01", "qmade02"]
STUDENT_SIS = ["99000001", "99000002"]

_IDENT = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_IDENT},
    )
    return proc.stdout


def changed_in(root: Path, commit: str) -> list[str]:
    out = git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", f"{commit}^", commit)
    return sorted(out.splitlines())


class Seed:
    """The seeded Canvas objects."""

    page: dict[str, Any]
    hw1: dict[str, Any]
    file: dict[str, Any]
    ann: dict[str, Any]
    tool: dict[str, Any]
    m1: dict[str, Any]
    m2: dict[str, Any]


def seed(fake: FakeCanvas) -> Seed:
    s = Seed()
    root = fake.root_folder_id
    lectures = fake.add_folder("lectures", parent_folder_id=root)
    s.file = fake.add_file(lectures["id"], "01 intro.pdf", b"%PDF-fake-one")
    fake.add_file(root, "data.csv", b"a,b\n1,2\n")

    sec = fake.add_section("BA2")
    cat = fake.add_group_category("Project Teams")
    grp = fake.add_group(cat["id"], "Team A")
    for name, login, sis in zip(STUDENT_NAMES, STUDENT_LOGINS, STUDENT_SIS, strict=True):
        fake.add_user(name, section_id=sec["id"], login_id=login, sis_user_id=sis)

    ag = add_assignment_group(fake, "Homework")
    s.hw1 = add_assignment(
        fake,
        "Homework 1",
        description=f'<p>Read <a href="/courses/101/pages/syllabus">the syllabus</a> and '
        f'<a href="/courses/101/files/{s.file["id"]}?wrap=1">slides</a>.</p>',
        points_possible=10,
        assignment_group_id=ag["id"],
        submission_types=["online_upload"],
        due_at="2026-09-22T06:59:00Z",
    )
    add_override(fake, s.hw1["id"], course_section_id=sec["id"], due_at="2026-09-23T06:59:00Z")
    add_override(fake, s.hw1["id"], group_id=grp["id"], due_at="2026-09-24T06:59:00Z")
    last_student = list(fake.users.values())[-1]["id"]
    add_override(fake, s.hw1["id"], student_ids=[last_student], due_at="2026-09-25T06:59:00Z")

    s.page = add_page(
        fake,
        "Syllabus",
        f'<h2>Welcome</h2><p>First <a href="/courses/101/assignments/{s.hw1["id"]}">homework</a>'
        f' and <a href="/courses/101/files/{s.file["id"]}?wrap=1">slides</a>.</p>',
        front_page=True,
    )
    add_page(fake, "Notes", "<p>Plain notes.</p>")
    s.ann = add_announcement(
        fake, "Welcome", '<p>See the <a href="/courses/101/pages/syllabus">syllabus</a>.</p>'
    )

    s.m1 = add_module(fake, "Week 1")
    add_item(fake, s.m1["id"], "Page", title="Syllabus", page_url="syllabus")
    add_item(fake, s.m1["id"], "SubHeader", title="Lecture")
    add_item(fake, s.m1["id"], "File", title="01 intro.pdf", content_id=s.file["id"])
    add_item(fake, s.m1["id"], "Assignment", title="Homework 1", content_id=s.hw1["id"])
    add_item(fake, s.m1["id"], "ExternalUrl", title="Docs", external_url="https://example.org/")
    s.tool = add_item(
        fake,
        s.m1["id"],
        "ExternalTool",
        title="Clicker",
        content_id=9,
        external_url="https://tool.example/launch",
    )
    s.m2 = add_module(
        fake,
        "Week 2",
        published=False,
        prerequisite_module_ids=[s.m1["id"]],
        require_sequential_progress=True,
    )
    quiz = add_item(fake, s.m2["id"], "Quiz", title="Quiz 1", content_id=4321)
    s.m2["completion_requirements"] = [
        {"id": quiz["id"], "type": "min_score", "min_score": 80, "completed": False},
        {"id": s.tool["id"], "type": "must_view", "completed": False},
    ]
    return s


@pytest.fixture
def fake() -> FakeCanvas:
    return FakeCanvas()


@pytest.fixture
def s(fake: FakeCanvas) -> Seed:
    return seed(fake)


@pytest.fixture
def repo(fake: FakeCanvas, s: Seed, tmp_path: Path) -> Path:
    root = tmp_path / "course"
    clone_course(fake.client("read"), URL, 101, root, TZ)
    return root


def fetch(fake: FakeCanvas, root: Path) -> str | None:
    return run_fetch(root, fake.client("read"), load_config(root)).commit


def tree(root: Path) -> dict[str, bytes]:
    return gitops.read_tree_files(root, gitops.REMOTE_REF)


# ---- clone ---------------------------------------------------------------------------


def test_clone_creates_repo_ref_and_checkout(repo: Path) -> None:
    assert gitops.ref_commit(repo) is not None
    assert git(repo, "branch", "--show-current").strip() == "main"
    assert git(repo, "status", "--porcelain").strip() == ""
    canvas = tree(repo)
    for path in (
        "course.yaml",
        "roster.yaml",
        "modules.yaml",
        ".canvas/bodies.json",
        "pages/syllabus.md",
        "assignments/homework-1.md",
        "announcements/welcome.md",
        "files/_meta.yaml",
        "files/lectures/01 intro.pdf",
        "files/data.csv",
    ):
        assert path in canvas, path
    for path, data in canvas.items():  # the checked-out main has all of it
        assert (repo / path).read_bytes() == data
    assert (repo / ".gitignore").read_text() == ".canvas/roster-cache.json\nroster-aliases.yaml\n"
    ignored = git(repo, "check-ignore", ".canvas/roster-cache.json", "roster-aliases.yaml")
    assert ignored.split() == [".canvas/roster-cache.json", "roster-aliases.yaml"]
    roster = (repo / "roster.yaml").read_text()
    assert "BA2" in roster and "Project Teams" in roster
    cfg = load_config(repo)
    assert isinstance(cfg, CourseConfig)
    assert cfg.course_id == 101 and cfg.timezone == TZ


def test_clone_uses_only_get(fake: FakeCanvas, s: Seed, tmp_path: Path) -> None:
    clone_course(fake.client("read"), URL, 101, tmp_path / "c", None)
    assert fake.writes() == []
    # no --timezone: the course's own zone
    assert "America/New_York" in (tmp_path / "c" / "course.yaml").read_text()


def test_clone_refuses_nonempty_directory(fake: FakeCanvas, tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "f").write_text("hi")
    with pytest.raises(Exception, match="not empty"):
        clone_course(fake.client("read"), URL, 101, tmp_path / "x", "UTC")


def test_cloned_tree_has_no_student_data(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    # Student-set overrides use a login ref (student_ref says so); names, SIS ids and
    # Canvas user ids must not be in any tracked file.
    secrets = [*STUDENT_NAMES, *STUDENT_SIS, *(str(u["id"]) for u in fake.users.values())]
    secrets += [n.split()[0] for n in STUDENT_NAMES] + [STUDENT_LOGINS[0]]
    for path, data in tree(repo).items():
        if path.startswith("files/") and not path.endswith("_meta.yaml"):
            continue
        text = data.decode()
        for needle in secrets:
            assert needle not in text, f"{path} contains student data"
    cache = (repo / ".canvas" / "roster-cache.json").read_text()
    assert STUDENT_LOGINS[1] in cache  # the cache is where it lives, untracked
    assert ".canvas/roster-cache.json" not in git(repo, "ls-files")


def test_alias_mode_tracks_only_aliases(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    (repo / "course.yaml").write_text((repo / "course.yaml").read_text() + "student_ref: alias\n")
    (repo / "roster-aliases.yaml").write_text(f"student-01: {STUDENT_LOGINS[1]}\n")
    git(repo, "commit", "-qam", "alias mode")
    assert fetch(fake, repo) is not None
    files = tree(repo)
    assert "student-01" in files["assignments/homework-1.md"].decode()
    tracked = "".join(d.decode() for p, d in files.items() if p.endswith((".md", ".yaml")))
    assert STUDENT_LOGINS[1] not in tracked


# ---- stability -----------------------------------------------------------------------


def test_fetch_twice_no_commit(fake: FakeCanvas, repo: Path) -> None:
    before = gitops.ref_commit(repo)
    assert fetch(fake, repo) is None
    assert fetch(fake, repo) is None
    assert gitops.ref_commit(repo) == before


def test_fetch_is_get_only_and_leaves_work_tree_alone(
    fake: FakeCanvas, s: Seed, repo: Path
) -> None:
    (repo / "pages" / "syllabus.md").write_text("local edit, uncommitted\n")
    (repo / "scratch.txt").write_text("untracked\n")
    s.page["body"] = fake.sanitize("<p>changed remotely</p>")
    status = git(repo, "status", "--porcelain")
    fake.log.clear()
    assert fetch(fake, repo) is not None
    assert fake.writes() == []
    assert git(repo, "status", "--porcelain") == status
    assert (repo / "pages" / "syllabus.md").read_text() == "local edit, uncommitted\n"


def test_remote_edit_changes_exactly_that_file(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    s.hw1["points_possible"] = 12
    commit = fetch(fake, repo)
    assert commit is not None
    assert changed_in(repo, commit) == ["assignments/homework-1.md"]
    assert fetch(fake, repo) is None


def _edit_page(f: FakeCanvas, s: Seed) -> None:
    s.page["body"] = f.sanitize("<p>new</p>")


def _edit_announcement(f: FakeCanvas, s: Seed) -> None:
    s.ann["title"] = "Welcome!"


def _edit_module(f: FakeCanvas, s: Seed) -> None:
    s.m1["name"] = "Week One"


@pytest.mark.parametrize(
    ("edit", "path"),
    [
        (_edit_page, "pages/syllabus.md"),
        (_edit_announcement, "announcements/welcome.md"),
        (_edit_module, "modules.yaml"),
    ],
)
def test_each_type_edit_is_one_file(
    fake: FakeCanvas, s: Seed, repo: Path, edit: Callable[[FakeCanvas, Seed], None], path: str
) -> None:
    edit(fake, s)
    commit = fetch(fake, repo)
    assert commit is not None
    assert changed_in(repo, commit) == [path]
    assert fetch(fake, repo) is None


def test_remote_file_replacement_changes_only_meta_and_file(
    fake: FakeCanvas, s: Seed, repo: Path
) -> None:
    fake.file_content[s.file["id"]] = b"%PDF-fake-two"
    s.file["size"] = len(b"%PDF-fake-two")
    s.file["updated_at"] = "2026-02-02T00:00:00Z"
    commit = fetch(fake, repo)
    assert commit is not None
    assert changed_in(repo, commit) == ["files/_meta.yaml", "files/lectures/01 intro.pdf"]
    assert tree(repo)["files/lectures/01 intro.pdf"] == b"%PDF-fake-two"
    assert fetch(fake, repo) is None


def test_fetched_tree_is_canonical(repo: Path) -> None:
    course = gitops.load_course_at(repo, gitops.ref_commit(repo) or "")
    canvas = tree(repo)
    for path, data in dump_tree(course).items():
        if path != "course.yaml":
            assert canvas[path] == data, path
    load_tree(repo)  # the work tree loads too


def test_link_to_new_assignment_is_relative_on_first_fetch(
    fake: FakeCanvas, s: Seed, tmp_path: Path
) -> None:
    page = next(p for p in fake.state("pages")["pages"].values() if p["title"] == "Notes")
    new = add_assignment(fake, "Late Addition")
    page["body"] = fake.sanitize(f'<p><a href="/courses/101/assignments/{new["id"]}">x</a></p>')
    root = tmp_path / "c"
    clone_course(fake.client("read"), URL, 101, root, TZ)
    notes = tree(root)["pages/notes.md"].decode()
    assert "../assignments/late-addition.md" in notes
    assert "/courses/" not in notes
    assert fetch(fake, root) is None


def test_external_tool_and_prerequisites_survive_two_fetches(fake: FakeCanvas, repo: Path) -> None:
    first = tree(repo)["modules.yaml"]
    text = first.decode()
    assert "ExternalTool" in text and "prerequisites" in text and "completion_requirements" in text
    assert fetch(fake, repo) is None
    assert tree(repo)["modules.yaml"] == first
    assert (repo / "modules.yaml").read_bytes() == first


def test_renamed_page_keeps_its_path(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    rename_page(fake, s.page["page_id"], "Course Syllabus")
    for item in fake.state("modules")["items"][s.m1["id"]]:
        if item["type"] == "Page":
            item["page_url"] = "course-syllabus"
    assert fetch(fake, repo) is not None
    files = tree(repo)
    assert "pages/syllabus.md" in files and "pages/course-syllabus.md" not in files
    assert b"title: Course Syllabus" in files["pages/syllabus.md"]
    assert b"page: syllabus" in files["modules.yaml"]
    assert fetch(fake, repo) is None


def test_ignored_objects_are_left_out_and_stay_out(fake: FakeCanvas, repo: Path) -> None:
    (repo / ".canvasignore").write_text("pages/notes\n")
    git(repo, "add", ".canvasignore")
    git(repo, "commit", "-qm", "ignore")
    assert fetch(fake, repo) is not None
    assert "pages/notes.md" not in tree(repo)
    assert fetch(fake, repo) is None


def test_merge_of_unrelated_edits_is_clean(fake: FakeCanvas, s: Seed, repo: Path) -> None:
    notes = repo / "pages" / "notes.md"
    notes.write_text(notes.read_text() + "\nLocal line.\n")
    git(repo, "commit", "-qam", "local notes")
    s.hw1["points_possible"] = 20
    s.ann["title"] = "Hello"
    assert fetch(fake, repo) is not None
    assert gitops.merge(repo) == 0
    assert "points: 20" in (repo / "assignments" / "homework-1.md").read_text()
    assert "Local line." in notes.read_text()
    assert fetch(fake, repo) is None


def test_local_comment_in_course_yaml_does_not_make_a_commit(fake: FakeCanvas, repo: Path) -> None:
    p = repo / "course.yaml"
    p.write_text("# my notes\n" + p.read_text())
    git(repo, "commit", "-qam", "comment")
    assert fetch(fake, repo) is None


def test_uncommitted_config_and_ignore_edits_do_not_reach_canvas_main(
    fake: FakeCanvas, s: Seed, repo: Path
) -> None:
    before = gitops.ref_commit(repo)
    (repo / ".canvasignore").write_text("pages/notes\n")  # uncommitted
    cfg = (repo / "course.yaml").read_text().replace(TZ, "America/Toronto")
    (repo / "course.yaml").write_text(cfg)  # uncommitted
    s.hw1["points_possible"] = 11
    commit = fetch(fake, repo)
    assert commit is not None
    assert changed_in(repo, commit) == ["assignments/homework-1.md"]
    files = tree(repo)
    assert "pages/notes.md" in files and TZ.encode() in files["course.yaml"]
    assert before is not None


def test_clone_failure_removes_what_it_created(fake: FakeCanvas, s: Seed, tmp_path: Path) -> None:
    fake.fail(500, method="GET", path=r"/courses/101/pages", times=None)
    new = tmp_path / "a" / "b"
    with pytest.raises(Exception):  # noqa: B017
        clone_course(fake.client("read"), URL, 101, new, TZ)
    assert not (tmp_path / "a").exists()

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(Exception):  # noqa: B017
        clone_course(fake.client("read"), URL, 101, empty, TZ)
    assert empty.is_dir() and list(empty.iterdir()) == []
