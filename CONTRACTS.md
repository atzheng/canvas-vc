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
    context.py                 FetchContext, ApplyContext                 (§8)
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
  fake_canvas/                 FakeCanvas (§12): used by every test touching remote/
    __init__.py core.py        transport, routing, pagination, request log, course/sections/users/groups
    pages.py discussion_topics.py assignments.py files.py modules.py   one module per resource
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
.canvas/bodies.json            written only into snapshots by fetch; ignored by plan and validate
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
LocalDT = datetime        # always timezone-aware, in the course zone, microseconds = 0

def parse_local(value: str | date | datetime, tz: ZoneInfo, default_time: time) -> LocalDT
    # accepts "YYYY-MM-DD", "YYYY-MM-DD HH:MM", "YYYY-MM-DD HH:MM:SS", date, datetime;
    # any other type raises ValidationError
def format_local(dt: LocalDT) -> str
    # "YYYY-MM-DD HH:MM" when seconds == 0, else "YYYY-MM-DD HH:MM:SS" (deterministic)
def to_canvas(dt: LocalDT | None) -> str | None    # UTC ISO 8601 "...Z"
def from_canvas(s: str | None, tz: ZoneInfo) -> LocalDT | None
def is_ambiguous_local(dt: LocalDT) -> bool   # wall time occurs twice (fall-back hour)
```

Known limitation: during the fall-back hour (e.g. 2025-11-02 01:30 in
Vancouver) the written local time does not say which of the two occurrences
is meant. `parse_local` always picks the first (fold=0), so a fetched
second-occurrence time (09:30Z) re-parses as 08:30Z. The format is unchanged;
`is_ambiguous_local` lets fetch/validate warn. Compare round trips via
`to_canvas` strings, since datetime `==` ignores fold.

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

`load` returns objects keyed by tree path, in on-Canvas order for modules.
A `load` must raise `ValidationError` (one line, naming both paths) when two objects
of its type carry the same `canvas_id`. Folder-per-type loads (pages, announcements)
also raise for any non-hidden entry that is not a top-level `*.md` file, so a moved
file can never look like a deletion.
`dump` returns `{tree path: exact bytes}` for the files the type owns, must not
depend on dict order of its input, and must satisfy
`dump(load(dump(objs))) == dump(objs)`; `tests/helpers/fixed_point.py`
(`assert_spec_fixed_point`, `assert_spec_normalizes`) checks this and every
type bead calls it. `CanvasObject.key(path)` derives the kind from the path
(`kind_for_path`: `modules.yaml#...`, else the first folder matched against the
registered specs).

The FILE spec has one extra, optional duty because `files/_meta.yaml` holds
`defaults` and `rules`, which a `dict[str, CanvasObject]` cannot carry:

```python
class FilesMetaSpec(TypeSpec, Protocol):
    def load_files_meta(self, root: Path, cfg: CourseConfig) -> FilesMeta
    def dump_files_meta(self, meta: FilesMeta, cfg: CourseConfig) -> dict[str, bytes]
```

`assert_files_meta_fixed_point(spec, meta, cfg, tmp_path)` in
`tests/helpers/fixed_point.py` checks `defaults`, `rules` and entries through
`dump_files_meta`/`load_files_meta` (loaded == input, second dump identical);
the files bead must call it.

If the FILE spec has these methods, `load_tree`/`dump_tree` use them for
`Course.files_meta` (keys of `FilesMeta.files` are tree paths such as
`files/lectures/01.pdf`) and mirror its entries into `Course.objects`; `dump`
is then not called for FILE. Without them, `load`/`dump` carry the entries only.
`dump_tree` gives modules the keys `modules.yaml#<name>` (`#<name>#2` for a
repeated name), in `Course.modules` order.

`model/__init__.py` registers **all five** TypeSpecs from the start (F3
creates them as stubs that load and dump nothing). Type beads fill in their
own module and never edit `model/__init__.py` or `course.py`. TypeSpecs whose
storage isn't "one file per object" (files: `files/_meta.yaml` + blobs;
modules: `modules.yaml`) still implement `load`/`dump`, with `load`
returning objects keyed by the paths in §4 (`modules.yaml#<name>`).

Body-bearing objects (`Page`, `Assignment`, `Announcement`) have a
`body: str` field holding **Markdown** (or HTML when `body_format: html`), with
relative links (§6).

Per-type fields (frontmatter key = field name unless noted; all dates `LocalDT | None`):

- `Page`: `title, published, publish_at, front_page: bool = False, body_format: Literal["md","html"] = "md", body`
- `Announcement`: `title, post_at, published: bool = True, body_format, body`
- `Assignment`: `title, points: float | None, group: str | None, submission_types, allowed_extensions: list[str] = [], grading_type: str = "points", omit_from_final_grade = False, published, unlock, due, lock, assign_to_overrides_only = False, overrides: list[Override] = [], body_format, body`
- `Override` (`model/overrides.py`): exactly one of `section: str`, `group: str`, `students: list[str]` (sorted), plus `title: str | None, unlock, due, lock`. Canonical order: sections by name, then groups by name, then student sets by first student. Overrides have no `canvas_id` in files, so readers and differs match existing Canvas overrides by target (section, group or student set). A section or group appears at most once, and a student in at most one student-set override.
- `FileEntry` (`model/files.py`): `path, canvas_id, sha256, published, hidden, unlock, lock, stable_link = False`. `FilesMeta` holds `defaults`, `rules: list[FileRule]` and `files: dict[path, FileEntry]` (only ids, hashes and settings that differ from what the rules give). `effective(path) -> FileSettings` applies defaults, then rules, then the per-file entry (`path` is a tree path, `files/a/b.pdf`, else ValueError; paths, keys and globs are NFC-normalised so macOS NFD names match; a later layer's non-None value wins, None inherits). `unlock`/`lock` are tri-state: a datetime, absent/null = inherit, `false` = explicitly no date (clears an earlier layer; `effective` returns None for it). Dump writes `false` only when an entry overrides a rule/default date, and an entry setting equal to what defaults+rules give is **not written**, so a later rule edit also changes that file (to pin a value, edit the rule or give the file a distinct value). `FileEntry.path` is relative to `files/` and equals the `_meta.yaml` key; `FilesMeta.files` keys are tree paths. In `_meta.yaml` a rule is flat (`- match: "*.pdf"` plus `published/hidden/unlock/lock`); date-only `unlock` uses `defaults.unlock_time`, date-only `lock` uses `defaults.assignment.due_time`. Dump writes an entry's `canvas_id`, `sha256`, `stable_link: true` and only those settings that differ from defaults+rules, entries sorted by path, and no `_meta.yaml` at all when everything is empty. Load rejects: unknown keys, a `canvas_id` shared by two entries (`files/_meta.yaml: files.<rel>: duplicate canvas_id N (also in <other>)`) or negative, `sha256` not `^[0-9a-f]{64}$`, keys that collide case-insensitively, contain a backslash, `..`, or a `_meta.yaml` component, and rule globs that don't compile (`rules[i]: match: invalid glob '<g>'`). `[]a]` makes `]` a class member; ranges are validated (`[z-a]` is invalid).
  Rule `match` globs (`model.files.glob_match`, relative to `files/`, gitignore-like, case-sensitive): `*`/`?`/`[..]` never cross `/`; `**/` = zero or more dirs, trailing `/**` = everything inside; a pattern without `/` matches the name of the file or any parent dir at any depth (`*.pdf`), one containing `/` (or a leading `/`) is anchored at `files/`; matching a directory selects everything inside it; a trailing `/` requires a directory; `\x` escapes. Intended to be unified with `ignore.py` later.
- `Module` (`model/modules.py`): `name, canvas_id, published, unlock, overrides: list[Override] (unlock only), items: list[ModuleItem]`. `ModuleItem`: exactly one of `page | assignment | file | header | url | quiz | discussion` (the last two are `{canvas_id: int}`, for unmanaged objects), plus `title, indent: int = 0, new_tab = False, published: bool | None, canvas_id`.
  Details: `page`/`assignment` are local paths without type folder or extension, `file` is relative to `files/` (no leading `/`, no `..`); `url` must be http(s). `canvas_id` of modules and items is `int | None`; **always write it once known**, because `dump_tree` keys same-named modules positionally (`modules.yaml#<name>#2`), so renaming or reordering one of two same-named modules re-keys the other and plan/differ must match by `canvas_id` first. Module overrides use a local `ModuleOverride` (`section | group | students`, `title`, `unlock`; canonical order as for `Override`), which may be unified with `Override` later. `modules.yaml` is written only when there are modules; module and item order is exactly list order. Models raise pydantic `ValidationError` on construction (item with zero or several types, bad ref shape); `load` converts it to `errors.ValidationError` with `modules.yaml: [i].items[j]...` in the message. Duplicate `canvas_id`s within `modules.yaml` are rejected.
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
- `load_tree` is tolerant (date-only values, any key order, CRLF line
  endings, a missing trailing newline). `dump_tree` is strict and canonical
  (LF, UTF-8, no BOM).
- Binary file contents are not held in `Course`. `dump_tree` emits only text
  files. Snapshot and apply handle blobs by `sha256` (§11).

## 5. Canonical serialization (`model/serialize.py`)

```python
def dump_yaml(data: Mapping | Sequence, key_order: Sequence[str], *,
              nested_orders: Mapping[str, Sequence[str]] | None = None) -> str
def dump_frontmatter(meta: Mapping, body: str, key_order: Sequence[str], *,
                     nested_orders: Mapping[str, Sequence[str]] | None = None) -> str
def load_frontmatter(text: str, source: str = ...) -> tuple[dict, str]
def load_yaml(text: str, source: str = ...) -> object      # tolerant; raises ValidationError
def normalize_body(body: str) -> str
```

`key_order` orders the keys of every mapping in the document. `nested_orders`
gives a different order for the mappings found under a key name (for example
`{"items": ITEM_KEY_ORDER, "overrides": overrides.KEY_ORDER}` for
`modules.yaml`), so nested objects keep their own order. `nested_orders` matches by key name
at any depth, and nested keys it does not list inherit the parent's `key_order`.
**None vs absent**: in the file format an absent key and `null` both mean "no
value"; dump writes neither. Types must model "no value" as `None` and never
give an absent field a non-None default taken from `course.yaml`; course
defaults only supply the time part when a date-only value is parsed (and are
resolved by the code that consumes the model, not at load). `None` values in a
mapping are omitted. Datetimes must be zone-aware and are written with
`format_local`. Strings are quoted whenever a plain scalar could be read back
as anything else (YAML 1.1 and 1.2 booleans, nulls, numbers, dates, sexagesimal
`12:30`), contain `: `, `#`, control or non-printable characters, or have
leading/trailing spaces. Floats that are whole numbers are written as ints.
`dump_yaml` / `dump_frontmatter` are the only YAML writers; python-frontmatter
is not used (the front-matter splitter is `load_frontmatter`).

`load_frontmatter`: strips a BOM, accepts CRLF and a missing final newline,
ends the frontmatter at the first `---` line (the body may contain `---`),
treats a file with no opening `---` as all body, and raises `ValidationError`
for unterminated or non-mapping frontmatter and duplicate keys. The body has
CRLF normalised, leading blank lines and trailing whitespace removed
(`normalize_body`); `dump_frontmatter` applies the same normalisation so the
format is a fixed point. Values come back raw (a date-only value is a `date`
or `str`); types convert with `dates.parse_local`.

`dump_tree` also writes `course.yaml` (fields that differ from the defaults,
`HH:MM` times quoted, no trailing `/` on a bare host) and `roster.yaml` (only
when not empty), with the key orders in `model/course.py`.

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
    exists: Callable[[str], bool] | None = None    # tree path -> local object exists (id or not)
def to_canvas_url(rel: str, ctx: LinkContext, *, image: bool = False) -> str | None
def from_canvas_url(url: str, ctx: LinkContext) -> str | None
```

URL forms: page `/courses/{c}/pages/{slug}`, assignment
`/courses/{c}/assignments/{id}`, announcement
`/courses/{c}/discussion_topics/{id}`, file link
`/courses/{c}/files/{id}?wrap=1`, image `/courses/{c}/files/{id}/preview`.
`?query` and `#fragment` on a relative link are carried through.

`render/md2html.py` also has `md_to_html_report(md, ctx) -> RenderResult(html,
missing_links, unresolved_links)`. `missing_links`: no such local object (an
error for `validate`/push). `unresolved_links`: the object exists locally but
has no canvas_id yet (needs `ctx.exists`). Push renders again after creating
new objects, so unresolved links get filled in. Links are also rewritten in
raw HTML `<a href>` / `<img src>`.

Pinned `pygments>=2.21,<2.22`: code blocks use inline styles, which feed the
`.canvas/bodies.json` hashes, so changing the pygments version requires a body
re-baseline (re-render and re-record all bodies).

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
    def __init__(self, base_url: str, token: str, *, course_id: int, mode: Literal["read", "write"],
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep): ...   # sleep injectable for tests
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
  50. Retries (exponential backoff, max 5): throttling (403 "Rate Limit
  Exceeded", and 429 honouring a capped `Retry-After`) for every method;
  5xx and network errors **only for GET and DELETE**. A POST or PUT that gets
  a 5xx is sent exactly once and raises `CanvasAPIError`, so a retry can never
  duplicate a create.
- Paths are normalised before matching (decoded once; query, fragment, `..`,
  `.`, empty segments, encoded `/` and double encoding are refused; one
  trailing slash dropped); the normalised path is the one sent. Absolute URLs
  (pagination `next` links, upload confirm) must be on the client's own
  origin under `/api/v1` and are re-checked like any other call.
- Any `_method` key (any case) in `data`, `json`, `params` or a URL query is
  refused (Rails method override).
- Denied names (`submissions`, `quizzes`, `enrollments`, ...) are matched
  only in resource positions (top level, or right after `/<parent>/<id>/`),
  so a page slug like `/pages/quizzes` is fine.
- **Course lock for global ids.** Before any `PUT`/`DELETE /files/N` the
  client does `GET /files/N` then `GET /folders/<folder_id>`; before any
  non-GET under `/folders/N` it does `GET /folders/N`. It requires
  `context_type == "Course"` and `context_id == course_id`, else
  `ForbiddenEndpoint` before the write (so a student's submission attachment,
  which lives in a user folder, is refused).
- Upload: step 1 is an allowlisted `POST`; step 2 posts multipart to the
  server-given `upload_url` with no credentials (https; a Canvas-origin `upload_url` is refused)
  and no redirect following; step 3 is `GET /files/\d+/create_success` on the
  Canvas origin (allowlisted). `download(url)`: on the Canvas host only
  `/files/\d+/download` or `/courses/{c}/files/\d+/download` (with token);
  other hosts must be https and never receive the token; redirects are
  followed manually (max 5) and re-checked.
- Tests inject `transport=FakeCanvas(...)` (§12). No test reaches the network.

```python
# remote/allowlist.py
ALLOWED: list[tuple[str, str]]   # (METHOD, path regex anchored at both ends)
```

The initial list (paths relative to `/api/v1`, `{c}` = course id):

```
GET    /courses/{c}  /courses/{c}/(pages|assignments|assignment_groups|discussion_topics|
       files|folders|modules|sections|groups|group_categories|users|enrollments)(/[^/]+)*
       /courses/{c}/late_policy
GET    /folders/\d+(/[^/]+)*  /files/\d+  /files/\d+/create_success  /users/self
POST   /courses/{c}/pages  /courses/{c}/assignments  /courses/{c}/assignment_groups
       /courses/{c}/discussion_topics  /courses/{c}/modules  /courses/{c}/modules/\d+/items
       /courses/{c}/assignments/\d+/overrides  /courses/{c}/files  /folders/\d+/files
       /courses/{c}/folders
PUT    /courses/{c}/pages/[^/]+  /courses/{c}/assignments/\d+  /courses/{c}/assignments/\d+/overrides/\d+
       /courses/{c}/discussion_topics/\d+  /courses/{c}/modules/\d+  /courses/{c}/modules/\d+/items/\d+
       /files/\d+  /courses/{c}/modules/\d+/assignment_overrides (if module overrides are supported)
DELETE /courses/{c}/pages/[^/]+  /courses/{c}/assignments/\d+  /courses/{c}/assignments/\d+/overrides/\d+
       /courses/{c}/modules/\d+  /courses/{c}/modules/\d+/items/\d+  /files/\d+
```

Assignment groups are created, never deleted, so no group `DELETE` is allowed.

**Course lock.** `CanvasClient` is constructed for one `course_id`, and
`{c}` in the allowlist matches only that id, so a client can't touch another
course. Integration tests and experiments also refuse to construct a write
client unless `course_id == CANVAS_SANDBOX_COURSE`.

Never allowed in any mode: any write under `.../submissions`, any
`DELETE /courses/{c}` (deleting or concluding the course),
`.../discussion_topics/\d+` DELETE (announcements are never deleted),
`.../assignment_groups/\d+` DELETE,
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
    NOTIFIES_STUDENTS="notifies_students"   # announcement posted now: needs --confirm-notify

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

def apply(plan: Plan, client: CanvasClient, ctx: ApplyContext, *, confirmed: set[str],
          writers: Mapping[Kind, Writer] | None = None) -> ApplyResult
```

`apply` stops at the first failure and returns what it did so far (`done`,
`failed`, `error`, and `new_ids` of objects already created; `ctx.ids` is
filled as creates happen). It never retries a write. Before the first write it
raises `GuardBlocked` if any `Risk.needs_confirm` flag is not in `confirmed`
(the bare flag, or `"<flag> <tree path>"` for a single object; see
`plan.core.unconfirmed_risks`). Assignment operations also go through
`ctx.guard.assert_allowed` **twice**: a pre-flight pass over every guarded
operation before the first write (a refusal raises `GuardBlocked` with nothing
written), then again immediately before each guarded write, for live state.
`assert_allowed` must therefore be idempotent. An operation is guarded
(`apply.needs_guard`) if its key kind is ASSIGNMENT, or if `Operation.touches_assignment`
is set; module differs set it on ops that change `published` on, or add or
remove, an item whose content is an assignment. A `GuardBlocked` in the
just-in-time pass is returned in `ApplyResult.error` (with `done`/`failed`), not
raised; the push CLI must report it as a stop, not a crash.

**Plug-in interface.** Type beads edit only their own files:
- differs: `plan/<type>.py` `diff_type(base, target)`, listed in
  `plan.core.DIFFER_MODULES` (apply order); `core.diff` moves every DELETE to
  the end in reverse order and drops operations under `.canvas/`;
  `plan.core.changes_between(before, after)` helps build `Operation.changes`.
- writers: `remote/<type>.py` (`create`/`update`/`delete`, section 8), found by
  `op.key.kind` via `apply.WRITER_MODULES`. CREATE and UPLOAD-without-`before`
  call `create(after)`; UPDATE, UNPUBLISH, REORDER and UPLOAD-with-`before` call
  `update(before, after)`; DELETE calls `delete(before)`.
- Differs must emit a same-key replacement (a page rename that keeps its
  slug, a file replaced at the same path) as UPDATE or UPLOAD-with-`before`,
  never as DELETE + CREATE, because deletes run last.
- A cleared value is `(old, None)` in `Operation.changes`; the writer sends an
  explicit null for it.
- Modules are matched by `canvas_id` first, then by `modules.yaml#<name>` key
  (keys of repeated names are positional); that is the modules differ's job,
  core does not care how an operation's key was chosen.

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
- Any assignment group deletion → `GuardBlocked` (also not in the allowlist).

Rules that need confirmation: `GRADING_CHANGE` (points, grading_type),
`LATE_POLICY_REGRADE`, `ACCESS_REMOVED`, `NOTIFIES_STUDENTS`.

```python
# remote/backup.py
def backup(client: CanvasClient, config: CourseConfig, assignment_ids: Iterable[int]) -> Path
    # ~/.canvas-sync/backups/<course_id>/<UTC timestamp>.json : assignment JSON, overrides,
    # submissions (metadata, score, grade, comments), via GET only. Returns the file path.
```

`push` calls `backup` for every assignment touched by the plan, before the
first write. It aborts if the backup fails: `backup` raises
`remote.backup.BackupError` (a `CanvasSyncError`) on any failure, including a
backup location inside a course repo (a parent holding `course.yaml` or `.canvas/`).
The file is mode 0600 in a 0700 directory and is never logged.

Guard details: `SubmissionGuard.__init__` also takes an optional keyword-only
`now` (clock, for tests). `check` returns live-derived risks only; `assert_allowed`
enforces `op.risks` plus the live ones, using the same flag matching as
`plan.core.unconfirmed_risks`, and fails closed (`GuardBlocked`) if live state cannot
be read. Flags: `--confirm-grading-change` (GRADING_CHANGE, LATE_POLICY_REGRADE),
`--confirm-access-removal` (ACCESS_REMOVED), `--confirm-notify` (NOTIFIES_STUDENTS).
Hard blocks also cover: unpublishing an assignment that has submissions (Canvas
refuses). Assignment groups are recognised by an `Operation` whose before/after
object's class is named `AssignmentGroup`. Group-category and submission-type changes
are read from `Operation.changes` keys (`group_category`, `submission_types`).

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
def fast_forward(root, ref=REMOTE_REF) -> bool             # git merge --ff-only; False if not possible
```

```python
# snapshot.py
def snapshot(root: Path, course: Course, bodies: Bodies, blobs: BlobStore, message: str,
             extra_parent: str | None = None) -> str | None
    # dump_tree + bodies.dump() at .canvas/bodies.json + blobs -> tree;
    # if tree == tree of current canvas/main and extra_parent is None, return None;
    # else commit with parents [current canvas/main, extra_parent?] and update_ref (compare-and-swap).
    # push passes extra_parent=HEAD so canvas/main descends from the pushed commit.
```

Implementation notes (fixed in 2zk.6):

- Trees are built in a temporary `GIT_INDEX_FILE`; the user's index and work
  tree are never touched. Only `merge` and `fast_forward` change the work tree.
- `update_ref` raises `gitops.RefMoved` (a `GitError`, a `CanvasSyncError`) when
  the ref is not at `old` (`None` = must not exist); other failures raise `GitError`. `gitops.BlobRef` lives in
  `gitops.py`; it is resolved from `.git/canvas-blobs/` (the common git dir).
- `snapshot` writes a `BlobRef` for every `files_meta.files` entry with a
  `sha256`. If the blob is missing from the store it is recovered from the
  previous canvas/main tree when the hash matches, else snapshot raises and
  nothing is written.
- **course.yaml.** It stays in the snapshot tree (so `load_course_at` works),
  but `dump_tree` output is only used when needed. If the previous canvas/main
  (on the first snapshot: the user's HEAD) has a `course.yaml` that parses to a
  config equal to `course.config`, its exact bytes are reused, so a comment-only
  user edit never makes an unchanged Course produce a commit. A merge of canvas/main therefore
  never shows a spurious course.yaml diff or drops the user's comments. If the
  parsed config differs, the canonical dump is used.
- `merge` passes `--allow-unrelated-histories` (the first fetch makes a root
  commit). `is_clean` ignores untracked files.
- `IgnoreRules` also matches a Markdown path with `.md` dropped, so
  `pages/scratch` ignores `pages/scratch.md`.

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
`canvas push: <n> operations`. After a push the user's branch is fast-forwarded
to `canvas/main`. There's no separate write-back commit.

`plan.diff` and `validate` ignore every path under `.canvas/`.

## 12. Testing

- `tests/fake_canvas/`: `FakeCanvas(httpx.BaseTransport)` keeps courses,
  pages, assignments (with `submissions` you can seed), overrides,
  discussion topics, files and folders (with the replacement-chain behaviour
  from SPEC.md, configurable), modules and items, sections, groups and users
  in memory. It implements the endpoints in §7 with Canvas-shaped JSON
  (fixtures in `tests/fixtures/canvas/`), pagination via `Link` headers, and
  HTML sanitizing that is stable but not the identity (so the round-trip code
  is exercised). It records every request in `fake.log`.
- Each resource lives in its own module (`tests/fake_canvas/<resource>.py`),
  registered by `core.py` from the start. Reader beads add GET behaviour,
  and writer beads add POST/PUT/DELETE behaviour in that module only. A
  module's `register(fake)` calls `fake.route(method, regex, handler)`
  (`{c}` in the regex is the course id; the regex is full-matched against the
  decoded path relative to `/api/v1`; `api=False` routes match raw non-API paths on
  any host, for downloads; a non-GET outside `/api/v1/` is refused unless the
  route has `upload_target=True`, as for upload step 2). A handler takes a `Req`
  (`.params`, `.body`, `.match`) and returns a list (paginated by the fake), a
  JSON value or an `httpx.Response`, or raises `CanvasError(status, msg)`.
  Module routes override core defaults. Modules keep data in
  `fake.state("<name>")`, take ids from `fake.next_id()` and sanitize HTML
  with `fake.sanitize()`.
- Core helpers: `fake.client(mode)` (a `CanvasClient` on the fake; sleeps go to
  `fake.sleeps`), `add_section/add_user/add_group_category/add_group/
  add_folder/add_file`, `seed_submissions(assignment_id, subs)`,
  `snapshot_submissions()` / `assert_submissions_intact(snap)` (submissions
  live in `fake.submissions` and only seeding writes them), fault injection
  `fail(status, method=, path=, times=, exc=)` and `throttle(times, status=,
  retry_after=)`, `max_per_page` (set small to force pagination),
  `rate_limit_remaining`. `fake.log` holds `LoggedRequest(method, path,
  params, body, request)` with `path` relative to `/api/v1`.
- A non-GET the fake has no route for, or one the allowlist would refuse,
  raises `FakeCanvasError` (an `AssertionError`); an unknown GET answers 404.
- `tests/integration/` runs only with `CANVAS_SANDBOX_URL`,
  `CANVAS_SANDBOX_COURSE`, `CANVAS_TOKEN` set (pytest marker `integration`,
  skipped by default). The safety suite seeds a test student's submissions
  through the sandbox's Student View.
