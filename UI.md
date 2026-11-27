# canvas ui: a read-only explorer for a course repo

`canvas ui` starts a local web page for **orientation**: what is in this
course repo, how the modules are laid out, what links to what, what is dated
when, and how the working tree differs from `canvas/main`. Use it on a new
repo, a colleague's course, or after a big `canvas pull`. It is not an editor:
edit the files, then `canvas diff` / `canvas push` as usual.

Contracts (stack, routes, module layout, read-only rules) are in
CONTRACTS.md section 13. This file says what each view is for and what it
shows. Each later bead (mbz.2 to mbz.8) implements exactly one section below.

## Principles

- **Read-only and offline.** No `CanvasClient`, no token, no network, no
  writes to the working tree, refs or `.canvas/`. Binds 127.0.0.1 only.
- **Orientation first.** Every page answers one question (the "Answers"
  line below) and links onward. Lists are sorted and filterable with plain
  query strings; no scripting required.
- **Fresh on every request.** The tree is reloaded from disk each time. No
  cache, no file watching: reload the browser tab after you edit.
- **No student data.** Overrides appear as written in the tree (section name,
  group name, alias). `.canvas/roster-cache.json` is never displayed.
- **Broken trees still render.** A parse error on one file becomes a message
  on the page naming the path (the same text `canvas validate` prints), not a
  500.

## Navigation (all pages)

```
+--------------------------------------------------------------------------+
| canvas ui · CS101 Fall 2026   Overview  Modules  Objects  Timeline       |
|                               Files  Changes                             |
+--------------------------------------------------------------------------+
```

One nav bar from the shared layout helper, current view highlighted, course
title (from `course.yaml`) at left.

## 1. Overview (`/`) — bead mbz.2

**Answers:** What is this repo, is it healthy, and is it in sync?

```
CS101 Fall 2026                   https://canvas.example.edu/courses/123
timezone America/New_York         course.yaml

Objects            total  published  unpublished
  Pages               24       20          4
  Assignments         12       10          2
  Announcements        6        6          0
  Files               31       28          3
  Modules              9        8          1

Sync
  Last fetch: 2026-11-25 14:02    canvas/main: 3f9a2c1   HEAD: 77be0d4
  2 commits not pushed, 0 behind canvas/main
  Working tree: 3 changed files
  [!] conflict markers in: pages/syllabus.md

Validate: 0 errors, 2 warnings
  warning  assignments/hw3.md: due is after lock
```

Shows: `course.yaml` fields (Canvas URL as a plain link, course id, timezone);
counts per kind with the published/unpublished split; sync state
(`gitops.head_commit`, `ref_commit`, `ahead_behind`, `last_fetch_time`,
`operation_in_progress`, `conflict_marker_paths`, `changed_paths`);
`commands.validate.validate_tree` issues, each linking to the object page
when the path is an object. **Never fetched** (no `canvas/main`): the sync
block says "never fetched: run `canvas fetch`", and every canvas/main-based
feature (Changes, "differs from canvas/main") shows the same one-line
explanation instead of failing. Not a git repo: a clear error page, since the
whole tool assumes git.

## 2. Modules (`/modules`) — bead mbz.4

**Answers:** How is the course laid out, in the order students see it?

```
Module 3: Optimization                 [published]  unlocks 2026-09-21 09:00
  |- page        Convexity basics                  pages/convexity.md
  |- assignment  Homework 3        due Oct 02      assignments/hw3.md   [unpublished]
  |- file        Lecture 3 slides                  files/lectures/03.pdf
  |- header      Further reading
  |- url         Boyd & Vandenberghe               https://...
  `- unmanaged   ExternalTool: Gradescope
Module 4: ...
```

Outline of `modules.yaml` in list order: module name, published state, unlock
date, items (honouring `indent`) with kind, title, the target's tree path
(link to its object page), its due/publish date and published flag when it
has one, and prerequisites by module name. Items whose target does not exist
in the tree are flagged "missing target". A final "Not in any module" group
lists pages, assignments, announcements and files that no module item names
(from the reference index).

## 3. Objects (`/objects`, `/objects/<kind>`, `/o/<tree path>`) — bead mbz.5

**Answers (list):** What is in the course, by kind, and what state is each in?
**Answers (detail):** What is this object, what does it link to, what links
to it, and does it differ from canvas/main?

List (`/objects` all kinds, `/objects/pages` etc.): one table per kind with
title, tree path, published, key dates, and badges: `orphan`,
`not in a module`, `broken links`, `differs`.

```
Pages (24)
 title                 path                    pub  publish_at   flags
 Convexity basics      pages/convexity.md      yes  -            in: Module 3
 Old notes             pages/old-notes.md      no   -            orphan
```

Detail (`/o/assignments/hw3.md`):

```
Homework 3                 assignment · assignments/hw3.md
Canvas: https://canvas.example.edu/courses/123/assignments/456   (plain link)
State:  published · differs from canvas/main (due changed)

Frontmatter
  points 20 · group Homework · submission_types online_upload
  unlock Sep 28 09:00 · due Oct 02 23:59 · lock Oct 05 23:59
Overrides
  section "Section B"  due Oct 04 23:59
  students alias:ab12  due Oct 06 23:59
Body (rendered Markdown; relative links point at /o/... pages)
  ...
Links out (3)             Linked from (2)
  pages/convexity.md        Module 3 (module item)
  files/hw3-data.zip        pages/syllabus.md
Broken links (1): ../pages/gone.md
```

The body is rendered with markdown-it, with relative links rewritten to UI
routes (not Canvas URLs). `body_format: html` bodies are shown as escaped
source, never injected. "Differs from canvas/main" compares this object in the
working tree with `load_course_at(canvas/main)` and names the changed fields
only. Paths come from the URL: anything that is not a key of the loaded course
is a 404, so `..`, absolute paths and symlinks never reach the file system.

## 4. Timeline (`/timeline`) — bead mbz.6

**Answers:** What happens when, and does the schedule look wrong?

```
Week of Sep 28 - Oct 4              (course timezone: America/New_York)
  Mon Sep 28 09:00  unlock   Homework 3                assignments/hw3.md
  Wed Sep 30 09:00  publish  Page: Convexity basics    pages/convexity.md
  Fri Oct  2 23:59  due      Homework 3                assignments/hw3.md
  Fri Oct  2 23:59  due      Homework 3 (Section B)    override
  Sun Oct  4 23:59  lock     Homework 3                [!] lock before due
Undated: 5 objects (list)
```

Every date in the tree: assignment unlock/due/lock and override dates, page
`publish_at`, announcement `post_at`, file unlock/lock (effective settings,
including rules), module unlock. Grouped by ISO week (Monday start) in the
course timezone, sorted within the week. Flags reuse the date checks of
`validate` (factored into one function both call): lock before due, due before
unlock, override outside the base window. Dates are displayed, never edited;
filter by kind with `?kind=assignment`.

## 5. Files (`/files`, `/files/raw/<path>`) — bead mbz.7

**Answers:** What course files exist, how are they configured, and are they
used?

```
files/
  lectures/
    01.pdf   1.2 MB  published  linked from: pages/week1.md, Module 1
    02.pdf   0.8 MB  hidden     unreferenced
  data/hw3.zip  4 MB  unlock Sep 28   linked from: assignments/hw3.md
Rules (_meta.yaml): "*.pdf" published; "solutions/**" hidden
```

A tree of `files/**`: size (from disk), effective settings
(`FilesMeta.effective`: published, hidden, unlock, lock, stable link), what
sets them (rule or entry), backlinks (pages, assignments, announcements,
module items) and an `unreferenced` badge, from the reference index.
`/files/raw/<path>` serves the bytes of a regular, non-symlink file under the
working tree's `files/` only (the path must be a key of the loaded tree),
inline only for PNG, JPEG, GIF, WebP and PDF; everything else (SVG and HTML
included) is an attachment of type `application/octet-stream`; always with
`X-Content-Type-Options: nosniff` (rules in CONTRACTS.md section 13).

## 6. Changes (`/changes`) — bead mbz.8

**Answers:** What would `canvas push` do, and what has changed on Canvas?

```
Pending (canvas/main..HEAD): 4 operations
  UPDATE assignment  Homework 3   due: Oct 02 -> Oct 03      assignments/hw3.md
  CREATE page        Week 5       pages/week5.md
  UPDATE module      Module 3     items: +1                  modules.yaml#Module 3
  ! risk: grading change on a published assignment (needs --confirm-grading-change)

Unpushed commits on main (2)         canvas/main history (last 10)
  77be0d4  add week 5                  3f9a2c1  canvas fetch: 3 changed
  1c8e9aa  move hw3 due date           ...
Uncommitted: 3 changed files (not part of the plan)
```

Pending operations come from `commands.diff.build_plan(root)` (the plan
`push` computes), grouped by kind, each with action, label, changed fields
(old -> new, from `Operation.changes`), tree path link, and its risks with
the flag each needs (named, never actionable). Below: unpushed commits (in
`HEAD`, not in `canvas/main`) and recent `canvas/main` commits (`canvas
fetch` / `canvas push` messages with times). With no `canvas/main` the page
shows the never-fetched message. A plan error (`CanvasSyncError`) shows as one line. The page
says it is as of the last fetch. History comes from the read-only `gitops.log_commits`. Uncommitted changes are listed from
`changed_paths` and called out as not in the plan.

## Reference index (no page of its own) — bead mbz.3

A pure function `build_index(course) -> Index` feeding the badges and link
sections of views 2, 3 and 5: outbound relative links per object, backlinks
(modules count as referrers), orphans, unreferenced files, broken links. See
CONTRACTS.md section 13.

## Visual style

One small `static/ui.css` (system fonts, light/dark via
`prefers-color-scheme`, tables, badges). No JavaScript, no external fonts or
CDNs (offline). Everything from the tree (titles, bodies, paths) is
HTML-escaped by the layout helper.

## Out of scope (v1)

Editing, saving, running fetch/push/pull, live reload or file watching,
authentication, multiple courses, full-text search, graph visualisation,
student names or roster data, anything that needs the network.
