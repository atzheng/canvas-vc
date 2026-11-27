# canvas-sync

Keep a Canvas LMS course in sync with a folder of Markdown and YAML files,
with git-style `fetch`, `pull` and `push`. Conflicts are ordinary git merge
conflicts. The command is `canvas`.

Design documents: [SPEC.md](SPEC.md) (what it does) and
[CONTRACTS.md](CONTRACTS.md) (interfaces and file layout).

## Status

| command | state |
|---|---|
| `canvas clone`, `canvas fetch`, `canvas validate`, `canvas open` | implemented |
| `canvas pull`, `canvas push`, `canvas status`, `canvas diff` | **not implemented yet** (being built). Described below as SPEC.md defines them. |

Until `pull` and `push` land, you can clone, edit and validate, but not send
changes back to Canvas. Do not point the tool at a real course; use a sandbox
course (see below).

## Install

Needs Python 3.11+, `git`, [`uv`](https://docs.astral.sh/uv/) and
[`pandoc`](https://pandoc.org/) (pinned to 3.5; a different version warns) on
your `PATH`.

```console
$ git clone <this repository> canvas-sync
$ cd canvas-sync
$ uv sync
$ uv run canvas --help
```

The examples below write `canvas`; use `uv run canvas` (or activate
`.venv`) if it is not on your `PATH`.

## Token setup

Create a personal access token in Canvas under Account, Settings, "New
Access Token". Give it to the tool only through an environment variable:

```console
$ export CANVAS_TOKEN=...        # the name is configurable via token_env in course.yaml
```

The token is never written to `course.yaml`, to git, or to any log. Do not put
it in a file in the course folder.

## Clone a course

Use a sandbox or test course first. Course 12345 on `canvas.example.edu` is
an invented example:

```console
$ canvas clone https://canvas.example.edu 12345 my-course
$ cd my-course
```

`clone` creates a git repository, fetches the course, and checks out `main`.
The course time zone is read from Canvas (`--timezone` overrides it). The
layout:

```
course.yaml  modules.yaml  roster.yaml  .canvasignore
pages/<slug>.md  assignments/<slug>.md  announcements/<slug>.md
files/_meta.yaml  files/**
.canvas/bodies.json          written by fetch only; do not edit
.canvas/roster-cache.json    untracked (gitignored)
```

Every object keeps its Canvas ID in its own frontmatter (`canvas_id:`), so
renaming a file with `git mv` keeps the Canvas object. Files and modules keep
their IDs in `files/_meta.yaml` and `modules.yaml`. Dates are local time
(`YYYY-MM-DD HH:MM`) in the course time zone.

## Daily workflow

```console
$ canvas pull                      # bring in changes made on Canvas, merge with yours
$ $EDITOR assignments/hw1.md       # edit
$ canvas validate                  # offline checks
$ git add -A && git commit -m "Move HW1 due date"
$ canvas diff                      # what push would do
$ canvas push --dry-run            # show the plan only
$ canvas push                      # apply it (asks for confirmation)
```

Commands, as SPEC.md defines them:

| command | what it does |
|---|---|
| `canvas clone <url> <course_id> [dir]` | create the repo, fetch, check out `main` |
| `canvas fetch` | download the Canvas state, serialize it, commit it to `canvas/main` (no commit if nothing changed) |
| `canvas pull [--rebase]` | `fetch` plus `git merge canvas/main` (or rebase). *Not implemented yet.* |
| `canvas status` | local changes not yet pushed, and whether Canvas has moved. *Not implemented yet.* |
| `canvas diff` | the API-level plan: what `push` would do, as create/update/delete operations. *Not implemented yet.* |
| `canvas push [--dry-run] [--yes] [--allow-dirty] [--no-delete]` | send local commits to Canvas. *Not implemented yet.* |
| `canvas validate [--fix]` | offline checks: frontmatter, references, dates, roster names; `--fix` rewrites files in canonical form |
| `canvas open [--print] <path>` | open the object on Canvas |

`push` is refused unless there are no unresolved conflicts, the working tree
is clean (or `--allow-dirty`), `canvas/main` is an ancestor of `HEAD`,
Canvas has not changed since your last fetch, and `validate` passes. It then
prints the plan, asks for confirmation (unless `--yes`), applies the
operations in dependency order, fetches again, and fast-forwards your branch
so the newly assigned Canvas IDs appear in your files. A second `push` is a
no-op. If an apply fails partway, `pull` and `push` again finish the job.
`push --no-delete` turns all deletions into unpublishes.

### Opening an object on Canvas

```console
$ canvas open assignments/hw1.md
$ canvas open --print pages/syllabus.md       # print the URL only
$ canvas open "modules.yaml#Week 1"           # a module, by name
$ canvas open files/lectures/01.pdf
$ canvas open course.yaml                     # the course home page
```

The URL is built from `canvas_url`, `course_id` and the `canvas_id` stored in
the file. Nothing is fetched, so it works offline. An object that has not been
pushed yet has no `canvas_id` and cannot be opened.

## Conflicts

`pull` merges the latest Canvas snapshot (`canvas/main`) into your branch with
git. If you and Canvas changed the same lines, you get a normal git conflict:

```console
$ canvas pull
CONFLICT (content): Merge conflict in assignments/hw2.md

$ git diff assignments/hw2.md
<<<<<<< HEAD
 due: 2026-09-22 23:59
=======
 due: 2026-09-23 23:59
>>>>>>> canvas/main

$ $EDITOR assignments/hw2.md && git commit -am "Merge Canvas changes"
$ canvas push
```

Resolve them with any git tool (`git mergetool`, your editor). For a binary
file changed on both sides, use `git checkout --ours files/...` or
`--theirs`. `push` refuses while any conflict marker remains. Useful git
views: `git log -p canvas/main` is the history of changes made on Canvas, and
`git diff canvas/main` shows what `push` would change.

## Student submissions are never destroyed

No `canvas` command, with any flags, can delete, overwrite or detach a student
submission. This is enforced in code by independent layers:

1. **Endpoint allowlist.** All HTTP goes through one client that allows only a
   fixed list of (method, path) pairs. Nothing under `/submissions` can be
   written, and course-level `DELETE` is not on the list. Anything else
   raises before any network I/O.
2. **Live guard at apply time.** Immediately before a risky operation (delete,
   `group_category`, `grading_type` or `points` change, override removal) the
   tool re-reads the assignment from Canvas and checks whether submissions
   exist. It does not trust the snapshot.
3. **Delete is never forced.** If you delete an assignment file locally and
   the assignment has any submission, `push` refuses the whole push with a
   one-line message naming the file. Nothing is written, not even the other
   changes. There is no `--force`. To resolve it, restore the file
   (`git checkout -- assignments/hw1.md`), or add its path to `.canvasignore`
   and fetch. Deleting such an assignment is something you do in the Canvas
   UI on purpose.
4. **Backup before every push.** Before applying, `push` saves submission
   metadata, grades and comments for every assignment it will touch to
   `~/.canvas-sync/backups/<course>/<timestamp>.json`, outside the repo,
   because it contains student data.

Other deletion rules:

- **Announcements are never deleted.** Deleting the file unpublishes a
  published announcement (with a warning); an already unpublished one is a
  no-op with a warning. Creating or publishing an announcement with no future
  `post_at` emails students at once, so the plan flags it and asks for
  confirmation.
- **Assignment groups are never deleted.** The tool creates missing groups but
  never removes one, because Canvas deletes every assignment in a deleted
  group. Delete empty groups in the Canvas UI.
- Changing `grading_type` or `points` needs `--confirm-grading-change <name>`.
  Changing a due date when a late policy is on, removing a student or section
  from an override, and changing `submission_types` are warned about in the
  plan.

## .canvasignore

`.canvasignore` lists things on Canvas to leave out of the snapshot entirely.
Ignored objects are never fetched, merged or pushed. It uses gitignore-style
globs over tree paths:

```
pages/instructor-scratch
files/old-term/**
```

## Privacy

- The tracked repository contains no student names, numbers or Canvas user
  IDs. Students in overrides are referred to by login ID or SIS ID
  (`student_ref` in `course.yaml`), or by an alias (`student_ref: alias`, with
  a gitignored `roster-aliases.yaml`).
- `roster.yaml` lists section names and a group category. The mapping to
  Canvas user IDs lives in `.canvas/roster-cache.json`, which is gitignored.
- The access token lives only in the `CANVAS_TOKEN` environment variable.
- Backups, which contain student data, are written under
  `~/.canvas-sync/backups/`, never into the repo.

## Development

```console
$ uv sync
$ uv run pytest                  # unit tests, no network
$ uv run ruff check && uv run ruff format --check
$ uv run pyright
$ uv run pytest -m integration   # opt-in; needs CANVAS_SANDBOX_URL, CANVAS_SANDBOX_COURSE, CANVAS_TOKEN
```
