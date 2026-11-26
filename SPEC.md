# canvas-sync: a Canvas course as plain text, with git-style pull and push

Status: spec, rev 3, 2026-11-26. Interfaces between components are fixed in
CONTRACTS.md.

## Goal

Edit a Canvas course locally as a folder of plain-text files, and keep it in
sync with Canvas the way git keeps a clone in sync with a remote:

- `pull` brings in changes made on Canvas (in the web UI, by a TA, by Canvas
  itself) and merges them with local changes.
- `push` sends local changes to Canvas. It is refused if Canvas has changed
  since the last pull, or if there are unresolved conflicts.
- Conflicts show up as ordinary git conflicts and are resolved with ordinary
  git tools.

In scope (v1):

- Pages
- Assignments (no submissions or grading), including per-section,
  per-student and per-group dates ("overrides")
- Announcements, including scheduled ones
- Files, with hidden/locked status and release dates
- Modules: order, items, headers, indentation, release dates, published state,
  per-section/student availability (if the API supports it; see Overrides)

Hard guarantee: **student submissions are never deleted or overwritten**
(see "Student submissions are never destroyed").

Out of scope (v1): quizzes, rubrics, discussions (other than announcements),
grades, course settings and navigation. These are left alone. Module items
can still refer to them by Canvas ID.

## Core idea: Canvas is a git remote

The course folder is a git repository. The tool keeps a remote-tracking ref,
`refs/remotes/canvas/main`, whose tree is a **snapshot of the course as it
exists on Canvas**, written out in exactly the same text format you edit.

```
         canvas fetch                 git merge               canvas push
Canvas ───────────────► canvas/main ─────────────► main ─────────────────► Canvas
        (serialize the              (normal git         (apply diff
         remote state and            three-way merge,    canvas/main..main,
         commit it)                  normal conflicts)   then fetch again)
```

The tool doesn't implement merging. Git does. This gives us for free:

- three-way merges against the last common snapshot
- conflict markers, `git mergetool`, `git status`, `git log canvas/main`
- `git diff canvas/main` = "what would push change?"
- `git log -p canvas/main` = the history of changes made on Canvas, including
  edits made in the UI

### Commands

| command | what it does | git analogue |
|---|---|---|
| `canvas clone <url> <course_id> [dir]` | create the repo, fetch, check out `main` | `git clone` |
| `canvas fetch` | download the Canvas state, serialize it, commit it to `canvas/main` (parent = previous `canvas/main`). No commit if nothing changed. | `git fetch` |
| `canvas pull` | `fetch` + `git merge canvas/main` (or `--rebase`) | `git pull` |
| `canvas status` | local changes not yet pushed + whether Canvas has moved since the last fetch | `git status` |
| `canvas diff` | the API-level plan: what `push` would do, as create/update/delete operations | `git diff canvas/main` |
| `canvas push [--dry-run]` | see Push | `git push` |
| `canvas validate` | offline checks: frontmatter, references, dates, roster names | — |
| `canvas open <path>` | open the object on Canvas | — |

Optional, later: a real git remote helper (`git-remote-canvas`), so that
`git fetch canvas` and `git push canvas` work natively. The commands above are
v1 because they're simpler to build and debug.

### Push rules

`canvas push` is refused unless all of these hold:

1. **No unresolved conflicts.** No unmerged paths in the index, and no
   conflict markers (`<<<<<<<`, `>>>>>>>`) in any tracked text file.
2. **Clean working tree** (or `--allow-dirty`). Push sends commits, not
   uncommitted edits. This keeps the record of what was pushed honest.
3. **Fast-forward.** `canvas/main` is an ancestor of `HEAD`. In other words,
   you have merged the latest fetched snapshot.
4. **Remote hasn't moved.** Right before applying, the tool does a fast
   *staleness check*: it re-reads the `updated_at` timestamps and content
   hashes of every object it is about to change, and compares them with
   `canvas/main`. If any differ: `! Canvas has changed since your last fetch;
   run canvas pull`. (This is the equivalent of git's non-fast-forward
   rejection. Without a server-side compare-and-swap it can't be perfect, but
   the window is seconds.)
5. **`validate` passes.**

Then it:

1. computes the operation plan from `git diff canvas/main HEAD`
2. prints it and asks for confirmation (unless `--yes`)
3. applies the operations in dependency order (files → pages/assignments →
   overrides → announcements → modules → module items)
4. runs `fetch` again and records the result as a snapshot commit whose
   parents are **both** the previous `canvas/main` and `HEAD`. Its tree is
   what Canvas actually stored: your content plus the newly assigned IDs.
   Because `HEAD` is a parent, `canvas/main` is now a descendant of your
   branch, just like a remote branch after `git push`.
5. fast-forwards your branch to `canvas/main` (`git merge --ff-only`), which
   brings in the new IDs. If the snapshot differs from `HEAD` in anything other
   than IDs and `.canvas/` metadata, push reports it as a bug and leaves the
   fast-forward to you. A second `push` is a no-op.

If the apply fails partway, nothing is lost. The next `fetch` records the
partial state, and `pull` + `push` finishes the job.

### Example session

```console
$ canvas pull
Fetching from canvas.ubc.ca course 123456... 3 objects changed on Canvas
Auto-merging assignments/hw2.md
CONFLICT (content): Merge conflict in assignments/hw2.md
Automatic merge failed; fix conflicts and then commit the result.

$ git diff assignments/hw2.md
 ---
 title: Homework 2
<<<<<<< HEAD
 due: 2026-09-22 23:59
=======
 due: 2026-09-23 23:59
>>>>>>> canvas/main
 points: 10

$ $EDITOR assignments/hw2.md && git commit -am "Merge Canvas changes"
$ canvas push
~ assignment hw2            due 2026-09-23 23:59
+ override  hw2 / Section BA2  due 2026-09-24 23:59
Push 2 changes to Canvas? [y/N] y
```

## Serialization: the part that has to be right

Git-style merging only works if fetching produces **stable, canonical
text**. If nothing changed on Canvas, a fetch must produce a byte-identical
tree. Otherwise every fetch creates false conflicts. Rules:

- **Deterministic YAML.** Fixed key order per object type, block style,
  quoting only when needed, no default values written out (a key equal to the
  course default is left out), one list item per line so that merges are
  line-wise.
- **Dates** in course-local time, `YYYY-MM-DD HH:MM`, always with the time.
  (When reading, date-only input is accepted. `fetch` always writes the full
  form. `validate --fix` normalizes local files to it.)
- **Body round-tripping.** Canvas stores HTML. We edit Markdown.
  - On push, the tool records `sha256(HTML as Canvas returned it)` next to the
    Markdown that produced it, in `.canvas/bodies.json`. That file is written
    **only into snapshots** (fetch), never by hand, and reaches your branch
    through merges. Plans and `validate` ignore `.canvas/`.
  - On fetch, if the remote HTML hash matches the recorded one, the snapshot
    contains the **original Markdown**, unchanged. This is the common case and
    gives clean merges.
  - If the HTML changed on Canvas (someone edited it in the UI), the snapshot
    contains `html→markdown(remote HTML)` using pandoc with a fixed
    configuration, keeping anything it can't express as raw HTML blocks. The
    diff against your Markdown shows up as a normal change or conflict.
  - Requirement, enforced by tests: `md→html→md` is idempotent after one round
    trip, so the same content doesn't keep flipping between two forms.
  - Escape hatch: `body_format: html` in the frontmatter keeps the body as
    HTML verbatim, for pages with complex layouts built in the Rich Content
    Editor.
- **Internal links** are stored as relative paths (`../assignments/hw1.md`,
  `../files/lectures/01.pdf`) in both local files and snapshots. Fetch
  rewrites Canvas URLs back to relative paths, and push rewrites relative paths
  to Canvas URLs.

## Identity: IDs live in the files

Each text object records its Canvas ID in its own frontmatter:

```yaml
---
canvas_id: 998877
title: Homework 1
...
```

- New local objects have no `canvas_id`. Push creates them, and the IDs
  arrive on your branch through the post-push fast-forward (Push step 5).
- **Renames are free**: `git mv assignments/hw1.md assignments/homework-1.md`
  keeps `canvas_id`, so the Canvas object (and its URL) is kept.
- Deleting a file means deleting that Canvas object (subject to the deletion
  rules below).
- Files and modules can't carry frontmatter. Their IDs live in
  `files/_meta.yaml` (per path) and `modules.yaml` (per module and per item).
- Nothing is in a hidden state file that could get out of sync with git
  except `.canvas/bodies.json` (written only by fetch, regenerable) and the
  roster cache (untracked).

## Repository layout

```
course/
  course.yaml
  modules.yaml
  roster.yaml               # section names; students referred to by alias (see Overrides)
  pages/syllabus.md
  assignments/hw1.md
  announcements/2026-09-08-welcome.md
  files/
    _meta.yaml
    lectures/01-linear-models.pdf
  .canvas/
    bodies.json             # written by fetch only: Markdown↔HTML hashes for round-tripping
    roster-cache.json       # UNTRACKED (.gitignore): Canvas user IDs ↔ names
  .canvasignore             # paths/objects on Canvas to leave out of the snapshot
  .gitattributes            # e.g. LFS for files/**
```

`.canvasignore` lets you exclude things on Canvas from the snapshot entirely
(for example `pages/instructor-scratch`, `files/old-term/**`). Ignored
objects are never fetched, merged or pushed.

## course.yaml

```yaml
canvas_url: https://canvas.ubc.ca
course_id: 123456
timezone: America/Vancouver
token_env: CANVAS_TOKEN

defaults:
  published: false
  assignment:
    submission_types: [online_upload]
    due_time: "23:59"
  unlock_time: "09:00"
```

## Dates and visibility

Dates are written in local time and converted to UTC when pushed (DST handled
through `zoneinfo`).

| key          | meaning                                 | pages | assignments | announcements | files | modules |
|--------------|-----------------------------------------|:-----:|:-----------:|:-------------:|:-----:|:-------:|
| `published`  | visible to students at all              |   ✓   |      ✓      |       ✓       |  ✓¹   |    ✓    |
| `publish_at` | becomes published at this time          |   ✓   |             |               |       |         |
| `unlock`     | can't be opened before this time        |       |      ✓      |               |   ✓   |    ✓    |
| `lock`       | can't be opened after this time         |       |      ✓      |               |   ✓   |         |
| `due`        | due date                                |       |      ✓      |               |       |         |
| `post_at`    | delayed posting                         |       |             |       ✓       |       |         |
| `hidden`     | hidden from the Files page, link-only   |       |             |               |   ✓   |         |
| `overrides`  | different dates/visibility per audience |   ²   |      ✓      |               |       |   ✓²    |

¹ For files, `published: false` maps to Canvas `locked: true`.
² Pages and modules: only if the instance has Canvas's newer "Assign To"
(differentiated modules) API. To be checked on UBC's instance. Not v1 for
pages.

## Overrides: different dates for sections, groups and individuals

Canvas models these as **assignment overrides**. Each override targets exactly
one of: a section (`course_section_id`), a group (`group_id`), or an ad hoc
set of students (`student_ids`), and can set its own `due_at`, `unlock_at` and
`lock_at`. A section can appear in at most one override per assignment. Setting
`only_visible_to_overrides: true` makes the assignment visible *only* to the
audiences listed, which is how "assign to" works.

### Format

```markdown
---
canvas_id: 998877
title: Homework 2
points: 10
due: 2026-09-22 23:59          # "Everyone else"
unlock: 2026-09-15 09:00
overrides:
  - section: BA2
    due: 2026-09-23 23:59
  - students: [jdoe, asmith]
    title: Accommodations       # optional; Canvas's override title for ad hoc sets
    due: 2026-09-26 23:59
    lock: 2026-09-28 23:59
  - group: Project Team 4
    due: 2026-09-24 23:59
---
```

- Leave out a date in an override to inherit the base date.
- `assign_to_overrides_only: true` (maps to `only_visible_to_overrides`)
  removes "Everyone else". Then the base `due`/`unlock`/`lock` must be absent.
- Order is not significant. The serializer sorts overrides as sections (by
  name), then groups (by name), then student sets (by first alias), so that
  fetches are stable and merges are clean.

### Referring to people without putting student data in git

The repo, and possibly a public GitHub remote, should not contain student
names or numbers. So:

- `roster.yaml` (tracked) defines **aliases**, short handles that only mean
  something together with the untracked cache:
  ```yaml
  sections: [BA1, BA2]          # Canvas section names, as they appear on Canvas
  groups_category: Project Teams
  ```
- Students are referred to by Canvas **login ID** (UBC CWL) or SIS ID
  (`sis:12345678`), whichever the instructor prefers (`student_ref: login_id`
  in `course.yaml`).
- `canvas fetch` downloads the roster into `.canvas/roster-cache.json`
  (gitignored). Snapshots write students by their login/SIS ID, never by
  Canvas internal user ID or name.
- `validate` fails if an override names a student, section or group that
  isn't in the roster.
- If even login IDs are too sensitive for the remote, set
  `student_ref: alias`. A gitignored `roster-aliases.yaml` then maps
  `student-01: <login>`, and only aliases appear in tracked files.

### Pushing overrides

Overrides are compared as a set, keyed by target (section, group, or sorted
student set). Changing one is an update to that override. Adding a student to
an ad hoc set rewrites the whole `student_ids` list, because Canvas has no
add/remove-one endpoint. A student set whose membership changes is matched to
the existing override by its `title` if it has one, or else treated as a
delete + create.

### Module-level availability

Modules get the same `overrides:` key (section/student/group, with an
`unlock`) **if** the instance supports the module "Assign To" API. Otherwise
`validate` reports that module overrides aren't supported on this instance.
Test on a sandbox before building.

## Object formats

### Pages: `pages/<slug>.md`

```markdown
---
canvas_id: syllabus            # page URL slug on Canvas
title: Syllabus
published: true
publish_at: 2026-09-01 09:00
---
Body in Markdown.
```

### Assignments: `assignments/<slug>.md`

Shown above. Other keys: `group` (assignment group, created if missing),
`submission_types`, `allowed_extensions`, `points`, `grading_type`,
`omit_from_final_grade`.

### Announcements: `announcements/<slug>.md`

```markdown
---
canvas_id: 776655
title: Welcome to BAIT 509
post_at: 2026-09-08 08:00
---
Body.
```

Once posted, students may already have been notified. `push` warns before
editing a posted announcement and never deletes one (it unpublishes instead,
with a warning).

### Files: `files/**` and `files/_meta.yaml`

```yaml
defaults:                         # applied to every file unless overridden
  published: true
rules:                            # globs, later rules win
  - match: "lectures/*.pdf"
    hidden: true
  - match: "solutions/hw1-*"
    unlock: 2026-09-19 09:00
files:                            # per-file, written by the tool: IDs + exact overrides
  lectures/01-linear-models.pdf: {canvas_id: 5551212, sha256: "…"}
  solutions/hw1-solutions.ipynb: {canvas_id: 5551299, sha256: "…", lock: 2026-12-20 23:59}
```

`unlock` and `lock` can be `false` (in an entry, rule or defaults) to mean "no
date", overriding a date from an earlier layer; leaving them out inherits.
`fetch` writes `lock: false` on a file only when a rule would otherwise give it
a lock.

`fetch` writes the `files:` section. A file's settings on Canvas that match
what the rules imply are *not* written out per file, so the `rules:` section
stays the place people edit, and changes to it merge cleanly.

Binary files: if a file changed on both sides, git reports a binary conflict.
Resolve with `git checkout --ours/--theirs files/...`. Fetch downloads only
files whose Canvas `updated_at`/size differ from the snapshot. Large files can
go in git LFS (`.gitattributes`).

### Modules: `modules.yaml`

```yaml
- name: Week 1 – Linear models
  canvas_id: 4433
  unlock: 2026-09-08 09:00
  published: true
  overrides:                      # only if supported (see above)
    - section: BA2
      unlock: 2026-09-09 09:00
  items:
    - page: syllabus              # by local path, without type folder or extension
      canvas_id: 1234
    - header: Lecture
      canvas_id: 1235
    - file: lectures/01-linear-models.pdf
      title: Slides
      indent: 1
      canvas_id: 1236
    - assignment: hw1
      canvas_id: 1237
    - url: https://scikit-learn.org/stable/modules/linear_model.html
      title: sklearn docs
      indent: 1
      new_tab: true
      canvas_id: 1238
    - quiz:                       # unmanaged Canvas object, referred to by ID
        canvas_id: 4321
      canvas_id: 1239
```

The order of entries is the order on Canvas. Moving a block of lines in YAML
is a move on Canvas, and git merges reorders reasonably as long as both sides
didn't move the same item.

## Replacing files without breaking links

**Question:** can a file be replaced without its ID/URL changing?

**Answer:** not exactly. A new upload always gets a new file ID. Canvas keeps
a **replacement chain**: overwriting a file with the same folder and name
(`on_duplicate=overwrite`) marks the old file as replaced by the new one, and
links to the old ID inside the course resolve to the new file. The Files API
refers to this chain (`replacement_chain_context_*` on upload, and an
`include[]` option to "follow the replacement chain" on GET). My understanding
from Canvas's source is that module items are moved to the new file. **To be
confirmed in the sandbox.**

What can still break: links outside the course (`/files/:id/download?verifier=…`
pasted into Piazza/email/PDFs), and possibly the file's
`hidden/locked/unlock_at/lock_at`, which may not carry over.

The tool:

1. uploads only when `sha256` changes. Changing only settings updates the
   existing file, so the ID is kept.
2. overwrites in place (same folder and name), so the replacement chain exists.
3. reapplies the file's settings to the new file after uploading.
4. writes the new `canvas_id` to `_meta.yaml`, re-renders every body that
   links to the file, and makes sure module items point at the new ID.
5. offers `stable_link: true` for links that must never change: it creates a
   wrapper page that embeds the file. Pages are updated in place, so their URLs
   never change.

Sandbox test: upload → add to a module, link from a page, set hidden + unlock
→ re-upload with overwrite (API and web UI) → record: the new ID, whether the
old URL resolves, whether the module item follows, whether the page link
works, which settings carried over, and what the student view shows.

## Student submissions are never destroyed

**Guarantee:** no `canvas` command, with any flags, can delete, overwrite or
detach a student submission, or make one unrecoverable. This is enforced in
code by four independent layers, not left to good intentions.

### What could put submissions at risk

| action | effect on Canvas | tool behaviour |
|---|---|---|
| Delete an assignment | the assignment and its submissions are soft-deleted (restorable from `/courses/:id/undelete`, but treat as destructive) | **blocked** if any submission exists |
| Delete an assignment group | **deletes every assignment in it** unless `move_assignments_to` is given | **never done**: the tool creates missing groups but never deletes one. Delete empty groups in the UI |
| Create or publish an announcement with no future `post_at` | posts at once and **emails students** | not a submission risk, but flagged in the plan and needs confirmation, because it can't be taken back |
| Change `group_category` (group vs. individual assignment) | changes who owns submissions | **blocked** once submissions exist (Canvas also refuses) |
| Change `grading_type` or `points` | existing scores are reinterpreted or rescaled | allowed only with `--confirm-grading-change hw1`. The plan shows how many graded submissions are affected |
| Change due dates/overrides when a late policy is on | Canvas recalculates late penalties, which **changes existing grades** | warned in plan with a count of affected graded submissions. Needs `--confirm-grading-change` |
| Remove a student or section from an override, or turn on `assign_to_overrides_only` | the student loses access, and their submission is hidden from them (but kept) | warned in plan, listing affected students who have submitted. Needs confirmation |
| Unpublish an assignment with submissions | Canvas refuses | not attempted; reported |
| Change `submission_types` | existing submissions are kept | allowed, with a warning if submissions exist |

### Enforcement layers

1. **Endpoint allowlist.** All HTTP goes through one client that only allows
   a fixed list of (method, path pattern) pairs. Nothing under
   `/submissions` can be written in content mode, and course-level `DELETE`
   isn't on the list at all. Any other call raises before any network I/O. A
   unit test checks the list, so that loosening it is a reviewed code change.
2. **Live guard at apply time.** Immediately before any risky operation
   (delete, `group_category`/`grading_type`/`points` change, override
   removal), the tool re-fetches the assignment and checks
   `has_submitted_submissions` / `graded_submissions_exist`. It doesn't trust
   the snapshot, which may be minutes old.
3. **Delete is never forced.** A local delete of an assignment that has
   submissions turns into: keep it on Canvas, unpublish it if Canvas allows,
   and add it to `.canvasignore`, with a message explaining this. There's no
   `--force` that overrides this. Deleting such an assignment is something
   you do in the Canvas UI on purpose.
4. **Backup before every push.** Before applying, `push` saves a full export
   of submissions metadata, grades and comments for every assignment it will
   touch to `~/.canvas-sync/backups/<course>/<timestamp>.json`, outside the
   repo, because it contains student data. Combined with Canvas's own
   gradebook history, every grade change the tool causes is reversible.

Integration test: a sandbox course with a test student who has submitted to
every assignment. The test runs every push scenario, including deleting
everything locally, and checks afterwards that every submission still exists
and is unchanged.

## Deletion rules

- Deleting a local file and pushing deletes the Canvas object, except that:
  - assignments with submissions are never deleted (see above)
  - posted announcements are unpublished instead
- An object deleted **on Canvas** shows up after `pull` as a deletion in the
  merge. If you also edited it locally, git reports a modify/delete conflict.
- `push --no-delete` turns all deletions into unpublishes.

## Implementation

- Python 3.11+, managed with `uv`. A thin REST client of our own on `httpx`
  (not `canvasapi`), so that every request goes through one place where the
  endpoint allowlist is enforced. `pydantic` models, `ruamel.yaml` (round-trip + deterministic output),
  `python-frontmatter`, `markdown-it-py` for md→html, `pandoc` (via
  `pypandoc`) for html→md, `typer`, `rich`. Git through the `git` CLI
  (`subprocess`); there's no need for a library.
- Modules:
  - `model/`: typed objects and their canonical serialization
  - `remote/`: Canvas → model (fetch) and operations → Canvas (apply)
  - `snapshot.py`: model → tree → commit on `refs/remotes/canvas/main`
    (built with `git hash-object` / `git mktree` / `git commit-tree`, without
    touching the working tree)
  - `plan.py`: `git diff canvas/main HEAD` → parsed old/new models → list of
    operations
  - `push.py`: checks, staleness check, apply, refetch, post-push snapshot + fast-forward
- Rate limits: follow `X-Rate-Limit-Remaining`, and back off on 403.
- Tests:
  - **Serialization stability** (the most important test): fetching twice with
    no changes produces the same tree hash; push followed by fetch leaves
    `git diff canvas/main HEAD` empty.
  - Round-trip property tests for md→html→md.
  - Plan tests against a fake remote.
  - Opt-in integration tests against a sandbox course
    (`CANVAS_SANDBOX_COURSE`).

## Milestones

1. `clone`/`fetch` for pages + assignments (no overrides), with canonical
   serialization and the stability tests. This is the foundation; get it
   right first.
2. `pull`, `push` with all push checks, post-push fast-forward, the endpoint
   allowlist, the live submission guard and pre-push backups. (Submission
   safety is in place before the first real push, not added later.)
3. Assignment overrides + roster handling.
4. Run the file-replacement sandbox test → files + `_meta.yaml`.
5. Modules (+ module overrides if supported).
6. Announcements, deletion rules, `.canvasignore`.
7. Later: `git-remote-canvas` helper, relative dates for term rollover,
   `stable_link`, building lecture PDFs from the slides repo before push.

## Open questions

- Does UBC Canvas allow personal access tokens for instructors, or is a
  developer key needed?
- Does UBC's instance have the module/page "Assign To" API (needed for
  module overrides)?
- How should students be referred to in tracked files: CWL login, SIS ID, or
  opaque aliases? (Depends on whether the repo will ever be public.)
- Course front page: manage it as a page with `front_page: true`?
- Cross-listed/combined sections (BA1 + BA2 in one Canvas shell vs. two
  shells): if it's two shells, do we want one repo pushing to two courses?
  (This would fit as two "remotes": `canvas/ba1`, `canvas/ba2`.)
