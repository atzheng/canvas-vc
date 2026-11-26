# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->


## Build & Test

_Add your build and test commands here_

```bash
# Example:
# npm install
# npm test
```

## Architecture Overview

_Add a brief overview of your project architecture_

## Conventions & Patterns

_Add your project-specific conventions here_

## What this is

`canvas-sync` (CLI `canvas`) keeps a Canvas LMS course in sync with a folder
of Markdown/YAML files, with git-style `fetch`/`pull`/`push` and git merge
conflicts. Read `SPEC.md` (what it does) and `CONTRACTS.md` (exact
interfaces, file layout, allowlist). The non-negotiable rule: **no code path
may delete or overwrite student submissions** (SPEC.md "Student submissions
are never destroyed", CONTRACTS.md sections 7 and 10).

## Build & test

```bash
uv sync
uv run pytest                      # unit tests; never touch the network
uv run ruff check && uv run ruff format --check
uv run pyright
uv run pytest -m integration       # opt-in, needs CANVAS_SANDBOX_URL/COURSE + CANVAS_TOKEN
```

These exist once the scaffold bead is merged.

## Conventions

- Python 3.11+, pyright strict on `src/`, ruff format. Dataclasses or frozen
  pydantic models; no mutable global state.
- Pure logic (serialization, diffing, link rewriting, dates) lives in
  functions with unit tests, separate from I/O.
- All HTTP goes through `remote/http.py`'s `CanvasClient`. Never import
  `httpx`/`requests` anywhere else in `src/`. Never widen
  `remote/allowlist.py` without saying why in the PR body; reviewers check it
  every time.
- Tests use `tests/fake_canvas/`. No test reaches the network except those
  under `tests/integration/`.
- Never put tokens, student names, login IDs or SIS IDs in logs, test
  fixtures or commits. Fixtures use invented people.
- Output that a user sees goes through `rich`; keep messages one line, with
  the path of the object involved.

## Git remote and PRs

The remote is `origin` (GitHub). Bead work goes through a pull request on a
branch named after the bead (`canvas-sync-<id>`). Don't push bead work
straight to `main`. Bead-state-only commits (`.beads/`) may go straight to
`main`.

## Multi-agent workflow

One coordinator session runs several implementer and reviewer subagents. The
coordinator holds the overall picture and is the only one that merges and
changes bead state.

**Roles**

- **Coordinator** (main session):
  - picks beads with `bd ready`, claims them, starts implementers;
  - starts a reviewer per PR;
  - decides on findings, then merges and closes.
  - Do not read whole diffs into the coordinator context: use
    `gh pr diff --stat` and the reviewer's findings. Read the full diff only
    for high-risk beads (below) or when a change touches `CONTRACTS.md`.
- **Implementer** (Sonnet subagent, own git worktree, one bead):
  - builds the bead per "Working a bead" below and opens the PR;
  - never runs `bd`: notes, follow-ups and contract changes go in the PR body.
- **Reviewer** (Sonnet subagent, one PR):
  - reviews independently and does not see the implementer's report;
  - never merges or edits the branch.

**Implementer, before opening the PR**

1. Merge `origin/main` into the branch; resolve conflicts (`uv.lock`:
   regenerate with `uv lock`).
2. Run the quality gate: `uv run pytest && uv run ruff check && uv run ruff format --check && uv run pyright`.
3. Push the branch and open the PR with `gh pr create`.
4. Include in the PR body:
   - each acceptance point and how it was verified;
   - contract changes or deviations;
   - any change to `remote/allowlist.py`, with the reason;
   - anything noticed but not done.

**Reviewer brief.** The coordinator gives the reviewer:
- the PR number and bead id;
- the relevant `CONTRACTS.md` sections;
- known risks for the bead;
- what the beads it blocks expect from it (from `bd show` on those beads).

**Reviewer steps**

1. Check out the branch and run the quality gate.
2. Try to break the change. Back a suspected bug with a failing test where
   practical.
3. Post the findings as one PR review comment.
4. Return the same findings to the coordinator. Each finding gives:
   severity; `file:line`; the failure scenario; whether it is confirmed by a
   test or only likely.

**Review depth by risk**

- Low (scaffold, docs, CLI wiring): the coordinator reads the `--stat` and the
  PR body. No reviewer is needed.
- Normal: one reviewer.
- High (anything in `remote/allowlist.py`, `remote/guard.py`,
  `remote/backup.py`, `remote/assignments.py`, `remote/files.py`,
  `plan/assignments.py`, `apply.py`, `push`, `gitops.py`, serialization
  stability): one reviewer, and the coordinator also reads the diff.

**After review** the coordinator chooses one of four actions for each finding:
send it to the *same* implementer via SendMessage, so it keeps its context;
fix a trivial issue directly; file a follow-up bead; or dismiss it. Then the
coordinator merges with `gh pr merge --merge`, closes the bead with a reason,
and starts agents for any newly ready beads.

**Scheduling.** Start a bead only after its dependencies are merged; do not
build stand-ins for unmerged work. Start new beads as earlier ones merge
rather than in fixed waves. Beads labelled `human-assisted` need the owner
(Canvas token, sandbox course, checking the Canvas UI); the coordinator asks
for what they need rather than starting an implementer.

**Worktrees.**
- Keep an implementer's and a reviewer's worktree until their PR merges. A
  subagent whose worktree was removed cannot be resumed with SendMessage.
- After a merge, remove the worktrees and delete the local branches.

**Bead state.** Do not silence `bd` output: a failed `bd close` otherwise goes
unnoticed.

**Keeping the coordinator lean.** Fix environment problems once, before
starting agents, and pass known pitfalls in each brief. Trim command output.
Start a fresh coordinator session every few rounds; beads holds the state.

## Working a bead

Implementer subagents skip the `bd` steps (2 and 7): the coordinator claims
and closes, and blockers or follow-ups go in the PR body or the final report.

1. `bd show <id>` and read all of it: description, design, acceptance. Then
   read the sections of `SPEC.md` and `CONTRACTS.md` it names.
2. `bd update <id> --claim`.
3. Build only what the bead describes. Its design notes list the files it
   owns; avoid editing files owned by other beads. If you must, keep the
   change minimal and mention it in the PR.
4. `CONTRACTS.md` fixes names, shapes and file paths shared between beads.
   Follow it exactly. If it is wrong or incomplete, fix it in the same commit
   and say so — do not silently diverge.
5. Before finishing: the quality gate passes and every acceptance point is
   checked.
6. Commit with the bead id in the message, e.g.
   `canvas-sync-abc: canonical YAML serialization`. Commit only your own files.
7. `bd close <id> --reason "<what was done, anything the next bead should know>"`.
   File a new bead for anything you found but did not do.

If a bead cannot be finished (unclear contract, blocked by something not
listed as a dependency), do not guess: stop and report it.
