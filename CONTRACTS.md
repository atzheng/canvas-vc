# canvas-sync contracts

The interfaces that beads share. SPEC.md says what the tool does. This file
fixes names, shapes and file locations, so that beads built separately fit
together. A bead that needs to change something here changes this file in the
same commit and says so in its PR body.

## 1. Layout

```
pyproject.toml                 uv project; console script `canvas = canvas_sync.cli:app`
src/canvas_sync/
  __init__.py
  cli.py                       typer app; imports and registers commands/*
  commands/
    clone.py fetch.py pull.py push.py status.py diff.py validate.py open.py
  config.py                    CourseConfig, load_config()                (§3)
  dates.py                     local <-> UTC                              (§3)
  errors.py                    exception hierarchy                        (§9)
  model/
    base.py                    Kind, ObjectKey, CanvasObject, registry    (§4)
    serialize.py               canonical YAML + frontmatter               (§5)
    course.py                  Course, load_tree, dump_tree               (§4)
    page.py announcement.py assignment.py overrides.py files.py modules.py roster.py
  render/
    md2html.py                 Markdown -> Canvas HTML                    (§6)
    html2md.py                 Canvas HTML -> Markdown (pandoc)           (§6)
    links.py                   relative path <-> Canvas URL               (§6)
    bodies.py                  .canvas/bodies.json                        (§6)
  remote/
    http.py                    CanvasClient                               (§7)
    allowlist.py               ALLOWED endpoints                          (§7)
    fetch.py                   fetch_course(): calls each type's reader   (§8)
    pages.py announcements.py assignments.py files.py modules.py roster.py
                               reader + writer per type                   (§8)
    guard.py                   submission guard                           (§10)
    backup.py                  pre-push backup                            (§10)
  plan/
    core.py                    Operation, Plan, Risk, ordering            (§9)
    pages.py announcements.py assignments.py files.py modules.py
                               differ per type                            (§9)
  apply.py                     run a Plan against Canvas                  (§9)
  gitops.py                    git plumbing                               (§11)
  snapshot.py                  Course -> commit on canvas/main            (§11)
  ignore.py                    .canvasignore matching                     (§11)
tests/
  fake_canvas.py               FakeCanvas (§12): used by every test touching remote/
  fixtures/                    sample course trees, Canvas JSON payloads
  test_*.py
  integration/                 opt-in, real sandbox course (§12)
```

The scaffold bead creates every module above as a stub, with the signatures
from this file raising `NotImplementedError`. Later beads fill in a file
instead of creating shared ones.

Libraries, fixed: Python 3.11+, uv, typer, rich, pydantic v2, ruamel.yaml,
python-frontmatter (parsing only; dumping goes through `serialize.py`),
markdown-it-py + mdit-py-plugins, pygments, pypandoc (needs `pandoc` on
PATH), httpx, pytest, hypothesis, ruff, pyright (strict on `src/`). Don't add
runtime dependencies without giving a reason in the PR body.

Quality gate (every PR): `uv run pytest && uv run ruff check && uv run ruff
format --check && uv run pyright`.

## 2. Repository a user edits (the "course tree")

```
course.yaml  modules.yaml  roster.yaml  .canvasignore
pages/<slug>.md  assignments/<slug>.md  announcements/<slug>.md
files/_meta.yaml  files/**
.canvas/bodies.json            tracked
.canvas/roster-cache.json      gitignored
```

**Path keys.** Every object is identified within a tree by its POSIX path
relative to the tree root, for example `assignments/hw1.md` or
`files/lectures/01.pdf`. Module items refer to objects by *ref*: the path
without the type folder and without `.md` (`hw1`, `syllabus`,
`lectures/01.pdf`).

## 3. Config and dates

```python
# config.py
class AssignmentDefaults(BaseModel):
    submission_types: list[str] = ["online_upload"]
    due_time: time = time(23, 59)

class Defaults(BaseModel):
    published: bool = False
    unlock_time: time = time(9, 0)
    assignment: AssignmentDefaults = AssignmentDefaults()

class CourseConfig(BaseModel):
    canvas_url: HttpUrl
    course_id: int
    timezone: str                       # IANA name; validated with zoneinfo
    token_env: str = "CANVAS_TOKEN"
    student_ref: Literal["login_id", "sis", "alias"] = "login_id"
    files_root: str = "/"               # Canvas folder that files/ maps to
    defaults: Defaults = Defaults()

def load_config(root: Path) -> CourseConfig
def get_token(cfg: CourseConfig) -> str   # raises ConfigError if the env var is unset
```

```python
# dates.py
LocalDT = datetime        # always timezone-aware, in the course zone, seconds = 0

def parse_local(value: str | date | datetime, tz: ZoneInfo, default_time: time) -> LocalDT
    # accepts "YYYY-MM-DD", "YYYY-MM-DD HH:MM", date, datetime
def format_local(dt: LocalDT) -> str          # always "YYYY-MM-DD HH:MM"
def to_canvas(dt: LocalDT | None) -> str | None    # UTC ISO 8601 "...Z"
def from_canvas(s: str | None, tz: ZoneInfo) -> LocalDT | None
```

Which default time to use: `due` uses `defaults.assignment.due_time`.
`unlock`, `publish_at` and `post_at` use `defaults.unlock_time`. `lock` uses
`due_time`.

## 4. Model

```python
# model/base.py
class Kind(StrEnum):
    PAGE = "page"; ASSIGNMENT = "assignment"; ANNOUNCEMENT = "announcement"
    FILE = "file"; MODULE = "module"

@dataclass(frozen=True, order=True)
class ObjectKey:
    kind: Kind
    path: str            # tree path, e.g. "assignments/hw1.md"; modules: "modules.yaml#<name>"

class CanvasObject(BaseModel):          # frozen pydantic model
    model_config = ConfigDict(frozen=True, extra="forbid")
    canvas_id: int | str | None = None  # pages use their URL slug (str)
    def key(self, path: str) -> ObjectKey: ...

class TypeSpec(Protocol):               # one per type, registered in model/__init__.py
    kind: Kind
    folder: str                         # "pages", "assignments", ...
    def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]
    def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]

REGISTRY: dict[Kind, TypeSpec]
```

Body-bearing objects (`Page`, `Assignment`, `Announcement`) have a
`body: str` field holding **Markdown** (or HTML when `body_format: html`), with
relative links (§6).

Per-type fields (frontmatter key = field name unless noted; all dates `LocalDT | None`):

- `Page`: `title, published, publish_at, front_page: bool = False, body_format: Literal["md","html"] = "md", body`
- `Announcement`: `title, post_at, published: bool = True, body_format, body`
- `Assignment`: `title, points: float | None, group: str | None, submission_types, allowed_extensions: list[str] = [], grading_type: str = "points", omit_from_final_grade = False, published, unlock, due, lock, assign_to_overrides_only = False, overrides: list[Override] = [], body_format, body`
- `Override` (`model/overrides.py`): exactly one of `section: str`, `group: str`, `students: list[str]` (sorted), plus `title: str | None, unlock, due, lock`. Canonical order: sections by name, then groups by name, then student sets by first student.
- `FileEntry` (`model/files.py`): `path, canvas_id, sha256, published, hidden, unlock, lock, stable_link = False`. `FilesMeta` holds `defaults`, `rules: list[FileRule]` and `files: dict[path, FileEntry]` (only ids, hashes and settings that differ from what the rules give). `effective(path) -> FileSettings` applies defaults, then rules, then the per-file entry.
- `Module` (`model/modules.py`): `name, canvas_id, published, unlock, overrides: list[Override] (unlock only), items: list[ModuleItem]`. `ModuleItem`: exactly one of `page | assignment | file | header | url | quiz | discussion` (the last two are `{canvas_id: int}`, for unmanaged objects), plus `title, indent: int = 0, new_tab = False, published: bool | None, canvas_id`.
- `Roster` (`model/roster.py`): `sections: list[str], groups_category: str | None`. `RosterCache` (untracked): `students: dict[ref, canvas_user_id]`, `sections: dict[name, id]`, `groups: dict[name, id]`.

```python
# model/course.py
@dataclass(frozen=True)
class Course:
    config: CourseConfig
    objects: dict[ObjectKey, CanvasObject]   # pages, assignments, announcements, files (FileEntry)
    files_meta: FilesMeta
    modules: list[Module]                    # order matters
    roster: Roster

def load_tree(root: Path) -> Course
def dump_tree(course: Course) -> dict[str, bytes]    # path -> exact file contents
```

**Invariants (tested):**
- `dump_tree(load_tree(dump_tree(c))) == dump_tree(c)` (canonical form is a
  fixed point).
- `load_tree` is tolerant (date-only values, any key order). `dump_tree` is
  strict and canonical.
- Binary file contents are not held in `Course`. `dump_tree` emits only text
  files. Snapshot and apply handle blobs by `sha256` (§11).

## 5. Canonical serialization (`model/serialize.py`)

```python
def dump_yaml(data: Mapping | Sequence, key_order: Sequence[str]) -> str
def dump_frontmatter(meta: Mapping, body: str, key_order: Sequence[str]) -> str
def load_frontmatter(text: str) -> tuple[dict, str]
```

Rules: block style; 2-space indent; keys in `key_order`, then any other keys
sorted; values equal to the course default are left out (the caller drops
them before dumping); strings quoted only when needed; dates via
`format_local`; lists of scalars in flow style only if `len <= 4` and they fit
in 80 columns, otherwise one item per line; the file ends with exactly one
`\n`; the body is separated from the frontmatter by one blank line; trailing
whitespace is stripped from the body.

Key order for each type is a module-level `KEY_ORDER` constant in that type's
model file, starting with `canvas_id`, then `title`/`name`.

## 6. Rendering and links

```python
# render/md2html.py
def md_to_html(md: str, link_ctx: LinkContext) -> str
    # markdown-it (commonmark + tables, footnotes, strikethrough), pygments
    # inline styles, $..$/$$..$$ -> Canvas equation images, relative links -> Canvas URLs

# render/html2md.py
def html_to_md(html: str, link_ctx: LinkContext) -> str
    # pypandoc, fixed options (gfm, --wrap=none), raw HTML kept for what it can't express,
    # equation images -> $..$, Canvas URLs -> relative links

# render/links.py
@dataclass
class LinkContext:
    course_id: int
    from_path: str                                 # tree path of the document being rendered
    ids: Callable[[ObjectKey], int | str | None]   # tree path -> canvas id
    paths: Callable[[Kind, int | str], str | None] # canvas id -> tree path
def to_canvas_url(rel: str, ctx: LinkContext) -> str | None
def from_canvas_url(url: str, ctx: LinkContext) -> str | None
```

URL forms: page `/courses/{c}/pages/{slug}`, assignment
`/courses/{c}/assignments/{id}`, announcement
`/courses/{c}/discussion_topics/{id}`, file link
`/courses/{c}/files/{id}?wrap=1`, image `/courses/{c}/files/{id}/preview`.

```python
# render/bodies.py   — .canvas/bodies.json: {tree_path: {"html_sha256": str, "md_sha256": str}}
class Bodies:
    @classmethod
    def load(cls, root: Path) -> "Bodies"
    def record(self, path: str, md: str, canvas_html: str) -> None
    def matches(self, path: str, canvas_html: str) -> bool   # remote HTML unchanged since our push?
    def dump(self) -> bytes                                  # canonical JSON, sorted keys
```

Fetch rule (§8): if `bodies.matches(path, remote_html)`, the snapshot body is
the Markdown from the previous `canvas/main` tree (unchanged). Otherwise it is
`html_to_md(remote_html)`. `md_sha256` lets fetch confirm it is reusing the
right Markdown.

Round-trip property (tested with hypothesis over a Markdown corpus):
`html_to_md(md_to_html(html_to_md(md_to_html(x))))` equals
`html_to_md(md_to_html(x))`.

## 7. HTTP client and allowlist

```python
# remote/http.py
class CanvasClient:
    def __init__(self, base_url: str, token: str, *, mode: Literal["read", "write"],
                 transport: httpx.BaseTransport | None = None): ...
    def get(self, path: str, params=None) -> Any              # JSON; follows Link pagination, returns full list
    def get_one(self, path: str, params=None) -> Any
    def post(self, path: str, data=None, json=None) -> Any
    def put(self, path: str, data=None, json=None) -> Any
    def delete(self, path: str, params=None) -> Any
    def upload_file(self, folder_path: str, name: str, content: bytes,
                    on_duplicate: Literal["overwrite"] = "overwrite") -> dict   # 3-step upload
    def download(self, url: str) -> bytes
```

- Every call checks `(method, path)` against `remote/allowlist.py` before any
  network I/O, and raises `ForbiddenEndpoint` if it isn't listed. `mode="read"`
  allows only GET.
- Rate limiting: reads `X-Rate-Limit-Remaining`; sleeps when it drops below
  50; retries 403 "Rate Limit Exceeded" and 5xx with exponential backoff (max 5).
- Tests inject `transport=FakeCanvas(...)` (§12). No test reaches the network.

```python
# remote/allowlist.py
ALLOWED: list[tuple[str, str]]   # (METHOD, path regex anchored at both ends)
```

The initial list (paths relative to `/api/v1`, `{c}` = course id):

```
GET    /courses/{c}  /courses/{c}/(pages|assignments|assignment_groups|discussion_topics|
       files|folders|modules|sections|groups|group_categories|users|enrollments)(/.*)?
GET    /folders/\d+(/.*)?  /files/\d+  /users/self
POST   /courses/{c}/pages  /courses/{c}/assignments  /courses/{c}/assignment_groups
       /courses/{c}/discussion_topics  /courses/{c}/modules  /courses/{c}/modules/\d+/items
       /courses/{c}/assignments/\d+/overrides  /courses/{c}/files  /folders/\d+/files
       /courses/{c}/folders
PUT    /courses/{c}/pages/[^/]+  /courses/{c}/assignments/\d+  /courses/{c}/assignments/\d+/overrides/\d+
       /courses/{c}/discussion_topics/\d+  /courses/{c}/modules/\d+  /courses/{c}/modules/\d+/items/\d+
       /files/\d+  /courses/{c}/modules/\d+/assignment_overrides (if module overrides are supported)
DELETE /courses/{c}/pages/[^/]+  /courses/{c}/assignments/\d+  /courses/{c}/assignments/\d+/overrides/\d+
       /courses/{c}/modules/\d+  /courses/{c}/modules/\d+/items/\d+  /files/\d+
DELETE /courses/{c}/assignment_groups/\d+   only with params containing move_assignments_to
```

Never allowed in any mode: any write under `.../submissions`, any
`DELETE /courses/{c}` (deleting or concluding the course),
`.../discussion_topics/\d+` DELETE (announcements are never deleted),
quizzes, enrollments writes. `tests/test_allowlist.py` contains a list of
forbidden calls that must stay forbidden.

## 8. Remote readers and writers (`remote/<type>.py`)

Each type module exposes:

```python
def read(client: CanvasClient, ctx: FetchContext) -> dict[str, CanvasObject] | list[Module] | ...
def create(client, obj, ctx: ApplyContext) -> int | str          # returns new canvas_id
def update(client, before, after, ctx: ApplyContext) -> None     # only changed fields are sent
def delete(client, obj, ctx: ApplyContext) -> None               # goes through guard (§10)
```

```python
@dataclass
class FetchContext:
    config: CourseConfig
    tz: ZoneInfo
    bodies: Bodies
    previous: Course | None          # last canvas/main tree, for body reuse and file hashes
    roster: RosterCache
    ignore: IgnoreRules
    blob_store: BlobStore            # where downloaded file contents go (§11)

@dataclass
class ApplyContext:
    config: CourseConfig
    tz: ZoneInfo
    roster: RosterCache
    ids: dict[ObjectKey, int | str]  # filled in as creates happen; used to render links
    guard: SubmissionGuard
    blobs: BlobStore
```

`remote/fetch.py`:

```python
def fetch_course(client: CanvasClient, ctx: FetchContext) -> Course
```

calls roster → files → pages → assignments → announcements → modules (files
first, so that later readers can turn file URLs into paths). Objects matched
by `.canvasignore` are dropped. Tree paths for new remote objects: `slugify(title)`,
with `-2`, `-3`, ... on collision. Existing objects keep their path from
`previous` (matched by `canvas_id`).

## 9. Plan and apply

```python
# plan/core.py
class Action(StrEnum):
    CREATE="create"; UPDATE="update"; DELETE="delete"; UNPUBLISH="unpublish"
    UPLOAD="upload"; REORDER="reorder"

class RiskKind(StrEnum):
    HAS_SUBMISSIONS="has_submissions"; GRADING_CHANGE="grading_change"
    LATE_POLICY_REGRADE="late_policy_regrade"; ACCESS_REMOVED="access_removed"
    POSTED_ANNOUNCEMENT="posted_announcement"; FILE_ID_CHANGE="file_id_change"

@dataclass(frozen=True)
class Risk:
    kind: RiskKind
    message: str
    needs_confirm: str | None       # flag name required to proceed, e.g. "--confirm-grading-change"

@dataclass(frozen=True)
class Operation:
    key: ObjectKey
    action: Action
    before: CanvasObject | None
    after: CanvasObject | None
    changes: dict[str, tuple[Any, Any]]   # field -> (old, new), for display
    risks: tuple[Risk, ...] = ()

@dataclass
class Plan:
    ops: list[Operation]            # in apply order (below)
    def render(self) -> rich.console.RenderableType
    def is_empty(self) -> bool

def diff(base: Course, target: Course) -> Plan     # base = canvas/main tree, target = HEAD tree
```

Each `plan/<type>.py` exposes `diff_type(base: Course, target: Course) -> list[Operation]`.
`core.diff` concatenates them in apply order: file uploads/updates → pages →
assignment groups → assignments (+ overrides) → announcements → modules →
module items → deletions in reverse order. Differs mark risks from what they
can see statically. The guard (§10) adds live risks at apply time.

```python
# apply.py
@dataclass
class ApplyResult:
    done: list[Operation]
    failed: Operation | None
    error: Exception | None
    new_ids: dict[ObjectKey, int | str]        # to write back into the tree

def apply(plan: Plan, client: CanvasClient, ctx: ApplyContext, *, confirmed: set[str]) -> ApplyResult
```

`apply` stops at the first failure and returns what it did so far. It never
retries a write that isn't idempotent.

```python
# errors.py
class CanvasSyncError(Exception)
class ConfigError, ValidationError, ForbiddenEndpoint, GuardBlocked, StaleRemote,
      UnresolvedConflicts, NotFastForward, DirtyTree, CanvasAPIError(status, body)
```

The CLI maps `CanvasSyncError` subclasses to a one-line message and exit code
1. Anything else is a bug and shows a traceback.

## 10. Submission safety

```python
# remote/guard.py
class SubmissionGuard:
    def __init__(self, client: CanvasClient, config: CourseConfig): ...
    def check(self, op: Operation) -> list[Risk]
        # re-fetches the assignment live: has_submitted_submissions, graded_submissions_exist,
        # late policy; for override removals, which removed students have submitted
    def assert_allowed(self, op: Operation, confirmed: set[str]) -> None   # raises GuardBlocked
```

Hard rules, which `confirmed` can't override:
- `DELETE` on an assignment with any submission → `GuardBlocked`. The differ
  has already turned it into UNPUBLISH + add to `.canvasignore`.
- Changing `group_category` with submissions → `GuardBlocked`.
- Deleting an assignment group without `move_assignments_to` → `GuardBlocked`.

Rules that need confirmation: `GRADING_CHANGE` (points, grading_type),
`LATE_POLICY_REGRADE`, `ACCESS_REMOVED`.

```python
# remote/backup.py
def backup(client: CanvasClient, config: CourseConfig, assignment_ids: Iterable[int]) -> Path
    # ~/.canvas-sync/backups/<course_id>/<UTC timestamp>.json : assignment JSON, overrides,
    # submissions (metadata, score, grade, comments), via GET only. Returns the file path.
```

`push` calls `backup` for every assignment touched by the plan, before the
first write. It aborts if the backup fails.

## 11. Git, snapshots, blobs

```python
# gitops.py   (subprocess `git`; never changes the working tree unless the name says so)
REMOTE_REF = "refs/remotes/canvas/main"
def repo_root(start: Path) -> Path
def head_commit(root) -> str | None
def ref_commit(root, ref=REMOTE_REF) -> str | None
def write_tree(root, files: Mapping[str, bytes | BlobRef]) -> str     # hash-object + mktree, nested
def commit_tree(root, tree: str, parents: list[str], message: str) -> str
def update_ref(root, ref: str, new: str, old: str | None) -> None    # compare-and-swap
def is_ancestor(root, a: str, b: str) -> bool
def unmerged_paths(root) -> list[str]
def conflict_marker_paths(root) -> list[str]     # tracked text files containing <<<<<<< / >>>>>>>
def is_clean(root) -> bool
def read_tree_files(root, commit: str) -> dict[str, bytes]   # for loading canvas/main or HEAD
def load_course_at(root, commit: str) -> Course             # load_tree over a commit's files
def merge(root, ref=REMOTE_REF, rebase=False) -> int        # runs git merge/rebase in the work tree; returns exit code
def commit_paths(root, paths, message) -> str               # used for id write-back
```

```python
# snapshot.py
def snapshot(root: Path, course: Course, blobs: BlobStore, message: str) -> str | None
    # dump_tree + blobs -> tree; if tree == tree of current canvas/main, return None;
    # else commit with parent = current canvas/main and update_ref (compare-and-swap)
```

`BlobStore` (in `snapshot.py`): `put(content) -> sha256`, `get(sha256) ->
bytes`, `path_for(sha256)`. It lives in `.git/canvas-blobs/` and is shared by
fetch (downloads) and apply (uploads). Files in the tree are real git blobs.
`BlobStore` only avoids downloading the same file again.

```python
# ignore.py
class IgnoreRules:
    @classmethod
    def load(cls, root: Path) -> "IgnoreRules"   # .canvasignore, gitignore-style globs over tree paths
    def ignored(self, path: str) -> bool
    def add(self, path: str) -> None             # used when an assignment can't be deleted
```

Commit messages on `canvas/main`: `canvas fetch: <n> changed` or
`canvas fetch after push`. The ID write-back commit on the user's branch:
`canvas: record ids after push`.

## 12. Testing

- `tests/fake_canvas.py`: `FakeCanvas(httpx.BaseTransport)` keeps courses,
  pages, assignments (with `submissions` you can seed), overrides,
  discussion topics, files and folders (with the replacement-chain behaviour
  from SPEC.md, configurable), modules and items, sections, groups and users
  in memory. It implements the endpoints in §7 with Canvas-shaped JSON
  (fixtures in `tests/fixtures/canvas/`), pagination via `Link` headers, and
  HTML sanitizing that is stable but not the identity (so the round-trip code
  is exercised). It records every request in `fake.log`.
- Every reader/writer bead adds the endpoints it needs to `FakeCanvas`, in
  a clearly separated section of the file per type, to keep merge conflicts
  small.
- `tests/integration/` runs only with `CANVAS_SANDBOX_URL`,
  `CANVAS_SANDBOX_COURSE`, `CANVAS_TOKEN` set (pytest marker `integration`,
  skipped by default). The safety suite seeds a test student's submissions
  through the sandbox's Student View.
