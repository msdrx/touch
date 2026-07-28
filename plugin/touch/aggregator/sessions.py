"""Session discovery, the live registry, and the tagged-union session mirror
(R-25 as amended, R-46).

Two halves, deliberately separated by the SD-1 line:

* **discovery** (this module's I/O half) walks a *scoped* slice of `~/.claude`
  and answers "which sessions belong to this project, and what evidence do we
  have for each";
* **mapping** (:data:`MIRROR_MAPPERS`) is pure: observations in,
  `(collection, _id, update)` triples out, built only from `refs.ref_key` and
  `mongo_store`'s op vocabulary. No I/O, no clock, and no database driver of
  any kind — `mirror.py` discovers and drives it. (GD-21 names the two files
  that may import one, and this is not one of them; `tests/test_mirror.py`
  greps this file for the package name, so it does not appear here at all.)

Nothing here parses transcript *content*. A `.jsonl` line is `ingest.py`'s
(GD-15: the module that owns a file format owns its parser), which is also why
`firstTs`/`lastTs` on a historical session document are not written here — see
"What this module does not timestamp" below.

Discovery scope (R-25 as amended by SESSIONJSONL-11)
---------------------------------------------------
`~/.claude/projects/` is **not** enumerated. It currently holds four foreign
slug directories created by nested `claude` runs under `/tmp`
(`-tmp-claude-1000-liveio`, `-tmp-claude-1000-models-probe`, and two
`…-castprobe` slugs), and a `projects/*/*.jsonl` enumerator ingests every one
of them as a Touch session. Scope is instead:

    slug(realpath(cwd))  ∪  slug(cwd)  ∪  every slug named in
    <slug>/.session-aliases, closed transitively and bounded

`.session-aliases` (`Esp` in the 2.1.220 binary, written by
`recordSessionAlias` at @259265480) exists because the CLI records the
*original* project slug whenever `realpath(cwd) ≠ cwd`; under a symlinked
checkout one logical project occupies two slug directories, and an enumerator
that ignores the alias file either double-counts or loses half the sessions.
No instance exists on this machine, so :func:`read_alias_slugs` accepts both
plausible spellings (a JSON array, or one slug per line) and treats an
unreadable or unrecognised file as "no aliases" rather than as an error.

Two scope checks are independent of the slug rule, and both are applied:

* a registry entry is in scope only if its own `cwd` resolves to the project
  cwd (or its slug is in the set) — the registry states the session's cwd
  itself, so this needs no path arithmetic at all;
* `history.jsonl` records carry `project` = the cwd verbatim, which is how a
  sessionId with **no transcript on disk** is discovered at all (R-46's
  "transcriptless seventh sessionId", recorded as `sources: []`).

Identity: a tagged union, and `_id` is immutable (R-46/GD-24)
------------------------------------------------------------
    live:<pid>-<procStart>     a session whose process is running
    hist:<sessionId>           every other transcript

`procStart` is `/proc/<pid>/stat` field 22 as a **string** (GD-24 pins the
bsonType), and it is in the key because the registry file is named for the raw
**pid**: pid reuse overwrites `~/.claude/sessions/15934.json`, so `pid` alone
is not an identity. :func:`read_proc_start` is how a stale entry is caught —
a registry entry whose pid now belongs to a different process (or to no
process) is **not live**, and its session falls to the historical arm.

`_id` is never rewritten. The promotion path exists because `--backfill` walks
transcripts only (no registry), so it writes `hist:<sessionId>` for a session
whose process is running right now; the next live scan of the same project
writes `live:<pid>-<procStart>` for it. The answer to that collision is not an
`_id` rewrite: the live document carries the sessionId in `sessionIds`, and the
historical document gains `promotedTo:<liveId>`. Both stay queryable, and
`{sessionIds:1}` finds either from the sessionId.

A promotion is only emitted when the historical document is known to exist —
:class:`Prior` carries that knowledge (the mirror knows its own `_id`s; a
stateless scan does not). Without it a fresh scan of a project directory
produces **exactly one document per session**, which is what R-46's acceptance
test counts.

What is deliberately NOT joined: the `/clear` pair
--------------------------------------------------
`/clear` gives a running process a **new** sessionId and rewrites its registry
entry, so the process's previous sessionId becomes historical while the process
is still alive (`dd469822…` and `e423cd3c…` are one process split by a `/clear`,
and the registry entry quoted by SESSIONJSONL-11 named `292fc08c…` where it now
names `a8d43bb1…`). Discovery does **not** claim that join: the rewritten
registry entry names only the *current* sessionId, no file records the previous
one against the same pid, and CONVO-4 is explicit that nothing on disk proves
two sessions shared a process. So the pre-`/clear` session stays an unlinked
`hist:` document, and the only sessionId ever promoted is the one the registry
names now. :func:`map_session`'s `$addToSet sessionIds` *would* merge two
sessionIds onto one live `_id` if a caller ever obtained that evidence — the
algebra is ready and unit-tested — but :func:`scan` cannot produce such a pair
from any tree, and a guess would be worse than the gap.

Not yet wired (a handoff, stated rather than left to be discovered)
-------------------------------------------------------------------
:class:`Prior` is the sole gate on **both** promotions and GD-26's
`present:false` sources, and nothing in production supplies one today. The seam
`mirror.iter_sources` declares is::

    def source(path=None) -> Iterable[observation]

and both call sites pass nothing else — `mirror.iter_rebuild_observations`
calls `source()`, `mirror.iter_backfill_observations` calls `source(path)` —
while ``prior`` here is keyword-only, so it cannot arrive that way. Until the
component that owns a mirror handle (the poll loop driving `mirror.py`, which
is not this module's file to edit) reads the existing `sessions._id`s and their
`sources[].path`s and passes a :class:`Prior` in, `iter_promotion_observations`
returns `[]` and no `present:false` element is ever written. Both features are
implemented and unit-tested through :data:`MIRROR_MAPPERS`, and both are
**inert on the wired path**; `tests/test_sessions.py` asserts that inertness
explicitly, so it shows up as a stated gap rather than as coverage that is not
there.

The historical arm is a session arm only (SESSIONJSONL-3)
---------------------------------------------------------
`hist:<sessionId>` is an identity for a *session*. It is **never** a grouping
key for agent records: one agent's transcript is split across two session
directories by `/clear`, the second fragment's `sessionId` is rewritten, and
grouping agent records by session silently splits one agent in two. Structural
enforcement here: every operation this module emits targets the `sessions`
collection and nothing else (:func:`_only_sessions`), so there is no code path
by which a session id can become an agent's key.

What this module does not timestamp
-----------------------------------
`firstTs`/`lastTs` are written **only** for the live arm, from the registry's
own `startedAt`/`updatedAt` (epoch milliseconds), through `$min`/`$max` so the
result does not depend on ingest order (GD-25). The historical arm gets no
timestamp from here at all: the honest source for "when was this session
active" is the records themselves, which `ingest.py` reads and accumulates into
the same two accumulable fields. Deriving them from file mtimes instead would
(a) claim a precision this module has not got and (b) make every poll tick
rewrite the document as the transcript grows.

The side effect is worth stating because it is load-bearing for `--backfill`:
a backfill observation carries no `datetime` at all, so `Mirror.backfill`'s
"no timestamp newer than the source file's mtime" guard has nothing to refuse
and a historical walk can never stamp a session with the import's clock.

`class` is an observation, not a verdict (GD-6/GD-23)
----------------------------------------------------
GD-6's three session classes are `owned` (Touch spawned it), `cooperating`
(observed *plus* evidence of touch-orchestrate conformance) and `observed`
(read-only). Touch spawns nothing in this pass and the cooperation evidence is
control-plane, which this pass excludes — so everything discovery finds is
`observed`, and it is written with `$setOnInsert` because the mirror stores
observations only: an evidence-gated re-classification is derived state and
belongs in the `derived` collection (GD-23), not in a `$set` race between two
writers of one field.

Tolerated, never fatal (D13 honesty, counted rather than logged)
---------------------------------------------------------------
`lost+found` in the registry directory, zero-byte and unparseable registry
files, a registry entry naming a pid that no longer exists, a registry
timestamp that is out of range or not finite (`json.load` accepts bare
`Infinity`), **a NUL byte inside a registry `cwd` or a `history.jsonl`
`project`** (`os.path.realpath` raises `ValueError`, not `OSError`, out of
`lstat`), a `.jsonl` whose basename is not a sessionId, an unreadable
`history.jsonl`, a `.session-aliases` in an unknown format — each is skipped
(the timestamp alone, not its entry) and **counted** on
:attr:`Scan.skipped`, so "we ingested nothing" and "we ingested nothing because
six files were unreadable" are different, visible answers. The per-path
backfill seam has no `Scan`; its equivalent counters are :func:`scope_skips`.

The rule this list encodes is one rule, and it has cost three fixes in this
file: **every value read off disk is untrusted, including its range and its
bytes**, and the guard belongs at the call that converts it. A discovery pass
killed by a file it was tolerating is the failure this module exists to not
have — and the blast radius is never one entry, since `scan`'s callers
(`mirror.iter_rebuild_observations` among them) invoke their sources with no
exception handler at all.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass, field

from . import mongo_store as ms
from . import refs

__all__ = [
    "CLASSES",
    "DEFAULT_CLASS",
    "PROVENANCE",
    "ALIAS_FILE",
    "REGISTRY_DIR",
    "PROJECTS_DIR",
    "HISTORY_FILE",
    "MAX_SLUGS",
    "REGISTRY_FIELDS",
    "SOURCE_KINDS",
    "SessionsError",
    "Source",
    "RegistryEntry",
    "SessionObservation",
    "PromotionObservation",
    "Prior",
    "Scan",
    "claude_root",
    "project_cwd",
    "slug_for",
    "read_alias_slugs",
    "project_slugs",
    "scoped_slugs",
    "scoped_dirs",
    "scope_skips",
    "reset_scope_cache",
    "MAX_SCOPE_KEYS",
    "read_proc_start",
    "read_registry",
    "discover_transcripts",
    "read_history_sessions",
    "session_id_for_path",
    "scan",
    "map_session",
    "map_promotion",
    "MIRROR_MAPPERS",
    "MIRROR_SOURCES",
    "iter_session_observations",
    "iter_promotion_observations",
]


class SessionsError(ValueError):
    """A caller-side misuse: an observation this module cannot map.

    Discovery never raises — every unreadable thing on disk is counted and
    skipped (see the module docstring). This exists for the *mapping* half,
    where a malformed observation is Touch's own bug and must surface before a
    wrong `_id` reaches a permanent store. `mirror.Mapper` converts it into a
    `MapperError` naming this module.
    """


# --- constants ------------------------------------------------------------

#: GD-6's three classes. `owned` and `cooperating` are reachable only with
#: evidence this pass has no source for (see the module docstring).
CLASSES = ("owned", "cooperating", "observed")
DEFAULT_CLASS = "observed"

#: GD-28: sessions are mirrored harness facts. The field is mandatory on every
#: `sessions` document (`mongo_store.COLLECTIONS["sessions"].provenance`).
PROVENANCE = "harness"

#: `Esp` in the 2.1.220 binary; written by `recordSessionAlias` (@259265480).
ALIAS_FILE = ".session-aliases"
REGISTRY_DIR = "sessions"
PROJECTS_DIR = "projects"
HISTORY_FILE = "history.jsonl"

#: Ceiling on the transitive alias closure. An alias file is agent-writable
#: text; a cycle is already handled by the visited set, but a long chain would
#: silently widen discovery scope, which is the one thing scoping exists to
#: prevent. Reaching the cap is counted (`skipped["slug_cap"]`), never silent.
MAX_SLUGS = 32

#: Registry fields copied onto the live document, in this fixed order. An
#: allowlist rather than the whole file: `status` is liveness (GD-23 keeps no
#: state field in a mirror document; liveness is computed at read time), and
#: an open copy would mirror whatever a future CLI version adds, unreviewed.
REGISTRY_FIELDS = ("name", "nameSource", "kind", "entrypoint", "version")

#: `sources[].kind`. Closed, because the value is part of a `$addToSet` set
#: element and a second spelling of one kind would store a second element.
SOURCE_KINDS = ("transcript", "registry")

#: Session ids are lowercase v4-shaped uuids; `refs` rejects any other
#: spelling, so a basename that does not match is not a transcript this module
#: can key (it is counted, not guessed at).
_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

#: Everything outside `[A-Za-z0-9]` becomes `-`. Verified against the two
#: slug/cwd pairs on the development machine, written here with the home path
#: generalized — `/home/user/Projects/touch` ⇒ `-home-user-Projects-touch` and
#: `/tmp/claude-1000/liveio` ⇒
#: `-tmp-claude-1000-liveio` — and against the two nested `…-castprobe` slugs,
#: whose doubled `--` is exactly what a `/` followed by a slug's own leading
#: `-` produces. The rule is not the only scope check (see the module
#: docstring); it is the cheap one.
_SLUG_SUB_RE = re.compile(r"[^A-Za-z0-9]")

#: What a slug read out of an alias file is allowed to look like. `slug_for`
#: can only ever emit `[A-Za-z0-9-]`, so this is deliberately a little wider
#: and still refuses everything that makes a *path*: a separator, a `..`, a NUL.
#: The file is agent-writable text and `project_slugs` joins its contents
#: straight into a filesystem path — an entry containing a NUL raised
#: `ValueError: embedded null byte` out of `open()` and took the whole scan
#: with it, which is a discovery pass killed by a file it was tolerating.
_SLUG_OK_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,255}$")


def _skips():
    """The counter set, declared once so every :class:`Scan` has every key.

    A missing key and a zero are the same fact to a reader and different facts
    to `dict.get`; declaring them makes "nothing was skipped" printable.
    """
    return {
        "registry_unreadable": 0,     # zero-byte / unparseable / not a dict
        "registry_not_json": 0,       # `lost+found`, stray non-.json entries
        "registry_out_of_scope": 0,   # another project's session
        "registry_stale_pid": 0,      # pid gone, or reused by another process
        "registry_unusable": 0,       # no pid / no sessionId / bad shape
        "registry_bad_timestamp": 0,  # startedAt/updatedAt out of range or absurd
        "transcript_not_session_id": 0,
        "history_unreadable": 0,
        "history_bad_line": 0,
        "alias_unreadable": 0,
        "alias_rejected": 0,
        "slug_cap": 0,
    }


# --- environment ----------------------------------------------------------


def claude_root(env=None) -> str:
    """Where discovery walks: `$TOUCH_CLAUDE_ROOT`, else `~/.claude`.

    Byte-for-byte `mirror.claude_root`, and deliberately **not** imported from
    it: `mirror.discover_mappers` imports this module, so an import back into
    `mirror` would close a cycle at module scope. Duplicating four lines is the
    cheaper of the two evils; `tests/test_sessions.py` asserts the two agree.
    """
    environ = os.environ if env is None else env
    return environ.get("TOUCH_CLAUDE_ROOT") or os.path.expanduser("~/.claude")


def project_cwd(cwd=None, env=None) -> str:
    """The project directory discovery is scoped to.

    `$TOUCH_PROJECT_CWD` exists so a daemon started from anywhere can be told
    which checkout it serves; `os.getcwd()` is the default because that is what
    the CLI itself keys its slug on.
    """
    if cwd is not None:
        return os.fspath(cwd)
    environ = os.environ if env is None else env
    return environ.get("TOUCH_PROJECT_CWD") or os.getcwd()


def slug_for(path) -> str:
    """The `~/.claude/projects/` directory name for a working directory."""
    return _SLUG_SUB_RE.sub("-", os.fspath(path))


def _realpath(path):
    """`os.path.realpath`, absorbing every failure it can raise.

    `OSError` is not the whole set, and the missing half is reachable from two
    files on disk. `posixpath.realpath` wraps its own `os.lstat` calls in
    `except OSError`, but an embedded NUL raises **`ValueError`** out of `lstat`
    and escapes — and both of this module's untrusted string inputs flow
    straight in here: a `history.jsonl` record's `project`
    (:func:`read_history_sessions`) and a registry entry's `cwd`
    (:func:`read_registry`). One such byte in one line would take the whole
    discovery pass down for every session, because `scan`'s callers —
    `mirror.iter_rebuild_observations` among them — invoke their sources with
    no handler at all. That is the exact opposite of what this module promises
    ("Tolerated, never fatal"), and it is the same class of defect as the
    NUL-bearing alias entry and the out-of-range registry timestamp.

    Returning the string **unresolved** is the right fallback for every caller
    here rather than a sentinel: an unresolvable path equals no target and
    slugifies to something no `claude` run ever wrote, so the record is refused
    on its own merits, at the site that knows which counter to bump.
    """
    try:
        return os.path.realpath(os.fspath(path))
    except (OSError, ValueError):
        return os.fspath(path)


# --- scope ----------------------------------------------------------------


def read_alias_slugs(root, slug, *, skipped=None) -> list:
    """Slugs named by `<root>/projects/<slug>/.session-aliases`.

    The file's format is not documented and none exists on this machine, so
    both plausible spellings are accepted — a JSON array of strings, or one
    entry per line — and anything else reads as "no aliases". An entry that
    looks like a **path** rather than a slug is slugified, since
    `recordSessionAlias` records the original project *slug* but a
    hand-written file naming the directory is the obvious mistake and costs
    nothing to absorb.

    Three outcomes, counted apart because they answer different questions:

    * **understood, no aliases** — an empty file, `[]`, or line-format text
      whose lines are all blanks and comments. Those are the obvious spellings
      of "this project has no aliases" (and of what `recordSessionAlias` leaves
      behind once the last alias is removed); counting a healthy file as
      unreadable corrupts the one distinction the counters exist for, in the
      direction that manufactures alarm.
    * **rejected entries** (`alias_rejected`) — the file was read and
      understood; an entry in it is not a slug we may join into a path. Counted
      per entry, including the non-string members of a JSON array.
    * **unreadable** (`alias_unreadable`) — the file could not be read at all,
      or it announces itself as JSON (a leading `[` or `{`) and then is not a
      JSON array. The line-format fallback is deliberately **not** taken for a
      failed JSON document: splitting `{"aliases": […]}` into "entries" invents
      rejections that say nothing about the file.

    Each count is a statement about *this* file alone — no branch here reads
    the scan-wide totals, so one file's rejection can never silence another
    file's tally.
    """
    try:
        path = os.path.join(os.fspath(root), PROJECTS_DIR, slug, ALIAS_FILE)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        # `ValueError` too: a slug that reached here from a previous alias file
        # can contain bytes `open()` refuses outright.
        if skipped is not None:
            skipped["alias_unreadable"] += 1
        return []
    stripped = text.strip()
    if not stripped:
        return []                       # an empty file states "no aliases"
    if stripped[0] in "[{":
        try:
            loaded = json.loads(stripped)
        except ValueError:
            loaded = None
        if not isinstance(loaded, list):
            if skipped is not None:
                skipped["alias_unreadable"] += 1
            return []
        entries = loaded
    else:
        entries = text.splitlines()
    out = []
    for entry in entries:
        if not isinstance(entry, str):
            if skipped is not None:
                skipped["alias_rejected"] += 1   # a JSON array of numbers
            continue
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue                             # blank / comment: understood
        candidate = slug_for(entry) if "/" in entry else entry
        if not _usable_slug(candidate):
            if skipped is not None:
                skipped["alias_rejected"] += 1
            continue
        out.append(candidate)
    return out


def _usable_slug(candidate) -> bool:
    """Whether a slug read out of an alias file may be joined into a path.

    Rejects a separator, a `..`, a NUL — and anything with no alphanumeric
    character at all. `slug_for` maps every non-alphanumeric byte to `-`, so an
    entry of `/` slugifies to `-` and `//` to `--`: both match the character
    class, neither is a slug any `claude` run ever wrote, and admitting them
    widens discovery scope to a directory named for punctuation.
    """
    return bool(_SLUG_OK_RE.match(candidate)
                and candidate not in (".", "..")
                and any(char.isalnum() and char.isascii() for char in candidate))


def project_slugs(cwd, root, *, skipped=None) -> list:
    """The closed, bounded slug set discovery is allowed to read.

    Seeded with the slug of `cwd` **and** of `realpath(cwd)` — the alias file
    lives under the realpath slug, so a symlinked checkout finds nothing
    starting from the symlink's slug alone — then closed transitively over
    `.session-aliases`, visited-set guarded and capped at :data:`MAX_SLUGS`.
    Order is stable (seeds first, then discovery order) because the set is
    stored on every session document and a set that reordered itself would
    churn `$addToSet` for no reason.
    """
    seeds = [slug_for(cwd)]
    real = slug_for(_realpath(cwd))
    if real != seeds[0]:
        seeds.append(real)
    out = []
    seen = set()
    queue = list(seeds)
    while queue:
        slug = queue.pop(0)
        if not slug or slug in seen:
            continue
        if len(out) >= MAX_SLUGS:
            if skipped is not None:
                skipped["slug_cap"] += 1
            break
        seen.add(slug)
        out.append(slug)
        for alias in read_alias_slugs(root, slug, skipped=skipped):
            if alias not in seen:
                queue.append(alias)
    return out


#: `{(cwd, root): (slugs, dirs, skips)}` for the per-path backfill seam only.
#: See :func:`scoped_slugs`; :func:`scan` never reads it.
_SCOPE_CACHE = {}

#: How many `(cwd, root)` pairs the memo holds. One, in practice — but this is
#: a module-level dict in a long-lived server process, and an unbounded one
#: that only ever grows is a leak waiting for the day something iterates
#: projects. Overflow forgets *everything* rather than evicting cleverly: the
#: entry costs one directory read per slug to rebuild, and "last keys win" is a
#: policy a reader can hold in their head.
MAX_SCOPE_KEYS = 4


def _scope_entry(cwd, root):
    """`(slugs, owned dirs, skip counts)` for one `(cwd, root)`, memoized once.

    The counters are memoized **with** the closure rather than discarded,
    because this is the only path a `--backfill` takes: computing the closure
    with no `skipped` would mean every rejected alias entry, every unreadable
    alias file and every `slug_cap` hit is silent in exactly the mode that
    reads the whole corpus. They are read back through :func:`scope_skips`
    instead of accumulated per call, so asking twice cannot double-count.
    """
    key = (os.fspath(cwd), os.fspath(root))
    entry = _SCOPE_CACHE.get(key)
    if entry is None:
        counts = _skips()
        slugs = tuple(project_slugs(cwd, root, skipped=counts))
        base = os.path.join(os.path.abspath(os.fspath(root)), PROJECTS_DIR)
        dirs = frozenset(os.path.join(base, slug) for slug in slugs)
        if len(_SCOPE_CACHE) >= MAX_SCOPE_KEYS:
            _SCOPE_CACHE.clear()
        entry = (slugs, dirs, counts)
        _SCOPE_CACHE[key] = entry
    return entry


def scoped_slugs(cwd, root) -> tuple:
    """:func:`project_slugs`, memoized — for the per-path ownership decision.

    `mirror.iter_backfill_observations` calls **every** registered source once
    per `.jsonl` in the corpus, and states the contract in its docstring:
    "returning `()` for a path you do not own … must cost one `str` comparison".
    Computing the transitive alias closure per call `open()`s one
    `.session-aliases` per slug for every file in the walk, owned or foreign,
    times one entity module — small today (28 transcripts, ≤32 slugs) and wrong
    by contract.

    Memoized rather than computed once at import because `cwd` and `root` are
    arguments, not globals. The key set is bounded (:data:`MAX_SCOPE_KEYS`) and
    a caller that expects a `.session-aliases` written *during* its own run
    calls :func:`reset_scope_cache` (as :func:`scan` never needs to: the full
    scan always recomputes).
    """
    return _scope_entry(cwd, root)[0]


def scoped_dirs(cwd, root) -> frozenset:
    """The absolute `<root>/projects/<slug>` directories this project owns.

    Memoized beside the slug set because the per-path ownership test is a
    *rooted* one: a bare "is my parent directory's name one of my slugs" claims
    any path anywhere on disk whose parent happens to be named like a slug, and
    the whole point of scoping is that discovery reads only where it should.
    """
    return _scope_entry(cwd, root)[1]


def scope_skips(cwd, root) -> dict:
    """What the memoized closure skipped — the per-path seam's counters.

    :func:`scan` returns its counts on :attr:`Scan.skipped`; a `--backfill`
    never calls it, so this is where the same facts live for that mode. A copy,
    so a reader cannot edit the memo.
    """
    return dict(_scope_entry(cwd, root)[2])


def reset_scope_cache():
    """Forget :func:`scoped_slugs`' memo (a new alias file, or a test)."""
    _SCOPE_CACHE.clear()


# --- the live registry ----------------------------------------------------


def read_proc_start(pid, *, proc_root="/proc"):
    """`/proc/<pid>/stat` field 22 (start time, clock ticks) as a string.

    Returns None when there is no such process or the file cannot be read —
    which is the same answer for "the session exited" and "we are on a system
    without procfs", and both mean the same thing here: not live.

    Field 2 (`comm`) is the executable name in parentheses and may itself
    contain spaces and parentheses, so the split starts after the **last**
    `)`; the token immediately after it is field 3, which puts field 22 at
    index 19. Splitting the whole line on whitespace — the obvious version —
    misreads every process whose name contains a space.
    """
    try:
        with open(os.path.join(os.fspath(proc_root), str(pid), "stat"), "rb") as fh:
            data = fh.read()
    except (OSError, ValueError):
        return None
    close = data.rfind(b")")
    if close < 0:
        return None
    fields = data[close + 1:].split()
    if len(fields) < 20:
        return None
    try:
        return fields[19].decode("ascii")
    except UnicodeDecodeError:                                   # pragma: no cover
        return None


@dataclass(frozen=True)
class RegistryEntry:
    """One `~/.claude/sessions/<pid>.json`, after the liveness check."""

    pid: int
    proc_start: str
    session_id: str
    cwd: str
    path: str
    live: bool
    fields: dict = field(default_factory=dict)
    started_at: object = None
    updated_at: object = None


def _epoch_ms(value):
    """Epoch **milliseconds** → an aware UTC datetime, or None.

    Truncated to whole milliseconds on purpose: BSON dates are millisecond
    precision, so a value with microseconds would fingerprint differently in
    the in-memory model than after a round trip through mongod, and GD-25's
    equivalence test compares exactly those two.

    The *range* is guarded as carefully as the type, because a registry file is
    just a file: `json.load` accepts bare `Infinity` and `NaN` by default, and
    `1e18` is a plain JSON number. `int(float("inf"))` raises `OverflowError`,
    `int(float("nan"))` raises `ValueError`, and any millisecond count past year
    9999 raises `ValueError`/`OSError` out of `fromtimestamp` — every one of
    which would escape :func:`read_registry` (whose `try` covers `json.load`
    only), then :func:`scan`, then `mirror.iter_rebuild_observations`, which
    calls its sources with no handler at all. One absurd number in one registry
    file would take down the whole discovery pass for every session. This module
    promises the opposite (see "Tolerated, never fatal"), so the conversion
    answers None and the caller counts it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        millis = int(value)                     # OverflowError on ±inf, ValueError on nan
        if millis <= 0:
            return None
        moment = datetime.datetime.fromtimestamp(millis / 1000.0, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.replace(microsecond=(millis % 1000) * 1000)


def read_registry(root, *, cwd=None, slugs=(), proc_root="/proc", skipped=None) -> list:
    """Every in-scope, **live** registry entry under `<root>/sessions/`.

    `lost+found` is on this machine and is a directory, not JSON; a zero-byte
    file is the other tolerated case (both are named by R-25's test list). They
    are counted and skipped.

    Scope is checked against the entry's own `cwd` first, which needs no path
    arithmetic at all, and falls back to the slug set for an entry whose cwd
    has since moved. Liveness is `/proc/<pid>/stat` field 22 equal to the
    recorded `procStart`: unequal means the pid was reused, which is precisely
    the failure the filename (the raw pid) invites.
    """
    counts = _skips() if skipped is None else skipped
    directory = os.path.join(os.fspath(root), REGISTRY_DIR)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    target = _realpath(cwd) if cwd is not None else None
    slug_set = set(slugs)
    out = []
    for name in names:
        path = os.path.join(directory, name)
        if not name.endswith(".json"):
            counts["registry_not_json"] += 1               # lost+found, and friends
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            counts["registry_unreadable"] += 1             # zero-byte, truncated, a dir
            continue
        if not isinstance(data, dict):
            counts["registry_unreadable"] += 1
            continue
        pid = data.get("pid")
        session_id = data.get("sessionId")
        recorded = data.get("procStart")
        if (isinstance(pid, bool) or not isinstance(pid, int) or pid < 1
                or not isinstance(session_id, str)
                or not _SESSION_ID_RE.match(session_id)
                or not isinstance(recorded, str) or not recorded.isdigit()):
            counts["registry_unusable"] += 1
            continue
        entry_cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
        if "\x00" in entry_cwd:
            # No path holds a NUL: `realpath` raises `ValueError` (not
            # `OSError`) out of `lstat`, and `open()` refuses it outright.
            # `_realpath` absorbs the exception so the pass survives; the entry
            # is refused *here* so the count says "unusable" — which is what it
            # is — rather than "another project's".
            counts["registry_unusable"] += 1
            continue
        if target is not None:
            in_scope = (_realpath(entry_cwd) == target if entry_cwd
                        else False) or (slug_for(entry_cwd) in slug_set if entry_cwd else False)
            if not in_scope:
                counts["registry_out_of_scope"] += 1
                continue
        if read_proc_start(pid, proc_root=proc_root) != recorded:
            counts["registry_stale_pid"] += 1              # exited, or the pid was reused
            continue
        # A timestamp that is present but unusable (out of range, infinite, 0)
        # costs the entry its `firstTs`/`lastTs`, never the entry itself and
        # never the pass — and it is counted, so "no timestamps" and "absurd
        # timestamps" stay different answers.
        started_at = _epoch_ms(data.get("startedAt"))
        updated_at = _epoch_ms(data.get("updatedAt"))
        for raw, parsed in ((data.get("startedAt"), started_at),
                            (data.get("updatedAt"), updated_at)):
            if raw is not None and parsed is None:
                counts["registry_bad_timestamp"] += 1
        out.append(RegistryEntry(
            pid=pid,
            proc_start=recorded,
            session_id=session_id,
            cwd=entry_cwd,     # never a fallback: an empty cwd is already refused
                               # above whenever a scope was given, and with no
                               # scope there is nothing truer to substitute.
            path=path,
            live=True,
            fields={key: data[key] for key in REGISTRY_FIELDS
                    if isinstance(data.get(key), (str, int)) and not isinstance(data.get(key), bool)},
            started_at=started_at,
            updated_at=updated_at,
        ))
    return out


# --- transcripts and history ----------------------------------------------


def session_id_for_path(path):
    """The sessionId a top-level transcript filename names, or None."""
    name = os.path.basename(os.fspath(path))
    if not name.endswith(".jsonl"):
        return None
    stem = name[: -len(".jsonl")]
    return stem if _SESSION_ID_RE.match(stem) else None


def discover_transcripts(root, slugs, *, skipped=None) -> dict:
    """`{sessionId: [path, …]}` for `<root>/projects/<slug>/<sessionId>.jsonl`.

    Top-level only. `<sessionId>/subagents/**` holds agent transcripts, which
    are `agents.py`'s and are keyed by agentId — a session directory is not a
    grouping key for them (SESSIONJSONL-3), so this walk deliberately does not
    descend.

    One sessionId can legitimately map to two paths when two slugs alias one
    project; both are recorded as sources.
    """
    counts = _skips() if skipped is None else skipped
    out = {}
    for slug in slugs:
        directory = os.path.join(os.fspath(root), PROJECTS_DIR, slug)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            session_id = session_id_for_path(name)
            if session_id is None:
                counts["transcript_not_session_id"] += 1
                continue
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                out.setdefault(session_id, []).append(path)
    return out


def read_history_sessions(root, cwd, *, skipped=None) -> list:
    """SessionIds `<root>/history.jsonl` attributes to this project.

    The only discovery source for a session with **no transcript on disk**
    (R-46's "transcriptless seventh sessionId"). Records are
    `{display, pastedContents, timestamp, project, sessionId}` and `project` is
    the cwd verbatim, so the scope check is an equality, not a slug guess.

    Only the `sessionId` is read. `display` and `pastedContents` are the user's
    prompt text, and a session registry has no business storing it.
    """
    counts = _skips() if skipped is None else skipped
    path = os.path.join(os.fspath(root), HISTORY_FILE)
    target = _realpath(cwd)
    seen = []
    known = set()
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError:
        counts["history_unreadable"] += 1
        return []
    with handle as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                counts["history_bad_line"] += 1
                continue
            if not isinstance(record, dict):
                counts["history_bad_line"] += 1
                continue
            project = record.get("project")
            session_id = record.get("sessionId")
            if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
                continue
            if not isinstance(project, str):
                continue
            if project != cwd and _realpath(project) != target:
                continue
            if session_id not in known:
                known.add(session_id)
                seen.append(session_id)
    return seen


# --- observations ---------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One file this session was observed through (GD-26's `sources[]`).

    ``path`` is **root-relative**, POSIX-separated: an absolute path would make
    a rebuild's fingerprint depend on the home directory it ran in, and GD-25's
    acceptance test compares fingerprints across passes, not across machines.

    ``present`` is GD-26's "source disappearance is a field, never a removal".
    A stateless scan only ever sees files that exist; a `present:false` element
    is produced when :class:`Prior` names a source that has since gone, which
    is the only way to know the difference between "gone" and "never seen".
    """

    path: str
    kind: str = "transcript"
    present: bool = True

    def as_element(self) -> dict:
        """The `$addToSet` element, in **fixed** field order — and validated.

        `mongo_store.apply_update` dedupes with BSON identity, and a BSON
        sub-document compares field-by-field *in order*: `{path, kind, present}`
        and `{kind, path, present}` are two elements of one set. The order is
        pinned here, in the one place elements are built.

        The path *shape* is checked here for the same reason: this is the
        mapping boundary, and `_rel()` at the four scan-site constructions is
        not one. :func:`map_session` accepts the plain dicts a replay or a
        fixture hands back, so `{"path": "/home/someone/x.jsonl"}` would
        otherwise reach a permanent store and make that document's fingerprint
        depend on the home directory it was built in — which is precisely what
        GD-25's acceptance pass compares across runs. The module's contract for
        the mapping half is that a malformed observation surfaces as
        :class:`SessionsError` *before* a wrong value is stored, and a path
        shape is such a value.
        """
        if self.kind not in SOURCE_KINDS:
            raise SessionsError(f"source kind {self.kind!r} is not one of {SOURCE_KINDS}")
        path = self.path
        if not isinstance(path, str) or not path:
            raise SessionsError(
                f"a source path is a non-empty root-relative string, got {path!r}")
        if (path.startswith("/") or "\\" in path or "\x00" in path
                or ".." in path.split("/")):
            raise SessionsError(
                f"source path {path!r} is not root-relative POSIX — an absolute path "
                f"(or one escaping the root) makes this document's fingerprint depend "
                f"on the machine it was built on")
        return {"path": path, "kind": self.kind, "present": bool(self.present)}


@dataclass(frozen=True)
class SessionObservation:
    """Everything discovery learned about one session. Pure data.

    The tagged union is `pid`/`proc_start` set (live) versus `session_id` alone
    (historical); :meth:`key` is the only place that decides, and it decides by
    calling `refs`.
    """

    session_id: str = ""
    pid: object = None
    proc_start: object = None
    cwd: str = ""
    slugs: tuple = ()
    session_ids: tuple = ()
    sources: tuple = ()
    session_class: str = DEFAULT_CLASS
    registry: dict = field(default_factory=dict)
    first_ts: object = None
    last_ts: object = None

    @property
    def live(self) -> bool:
        return self.pid is not None and self.proc_start is not None

    def key(self) -> str:
        if self.live:
            return refs.session_key(self.pid, self.proc_start)
        return refs.hist_session_key(self.session_id)


@dataclass(frozen=True)
class PromotionObservation:
    """A historical document learning that its session now has a live process.

    Carries the same class the historical document was created with, so the
    `$setOnInsert` payload of the two operations that can target that `_id` is
    identical — the one operator in GD-25's algebra that is order-dependent by
    construction.
    """

    session_id: str
    live_id: str
    session_class: str = DEFAULT_CLASS


@dataclass(frozen=True)
class Prior:
    """What the mirror already holds, when the caller can supply it.

    Two questions a stateless scan cannot answer, and both have honest
    "unknown" defaults rather than guesses:

    * ``ids`` — which `sessions._id`s exist. A promotion is emitted only for a
      `hist:` id in here, so a first pass writes exactly one document per
      session and never conjures a half-populated historical twin.
    * ``sources`` — `{_id: [root-relative path, …]}` previously recorded. A
      path in here that is no longer on disk becomes `present:false`.
    """

    ids: frozenset = frozenset()
    sources: dict = field(default_factory=dict)

    def known_sources(self, key) -> tuple:
        return tuple(self.sources.get(key, ()))


@dataclass
class Scan:
    """The result of one discovery pass."""

    root: str
    cwd: str
    slugs: tuple
    sessions: tuple
    promotions: tuple
    skipped: dict
    transcripts: dict = field(default_factory=dict)
    history_only: tuple = ()

    def observations(self):
        """`(kind, observation)` pairs, the shape `Mirror.rebuild` consumes."""
        for session in self.sessions:
            yield "session", session
        for promotion in self.promotions:
            yield "sessionPromotion", promotion


# --- the scan ------------------------------------------------------------


def _rel(root, path) -> str:
    root = os.fspath(root)
    path = os.fspath(path)
    try:
        rel = os.path.relpath(path, root)
    except ValueError:                                           # pragma: no cover
        rel = path
    return rel.replace(os.sep, "/")


def scan(cwd=None, root=None, *, prior=None, proc_root="/proc", env=None) -> Scan:
    """Discover this project's sessions. The I/O half; returns pure data.

    Order of resolution, and why:

    1. the slug set (:func:`project_slugs`) — everything else is read through
       it, so scope is decided before a single transcript is opened;
    2. the registry, filtered to live, in-scope entries. A session that is live
       *and* has a transcript takes the `live:` id: the transcript is a source
       of the live document, not a second document, which is why a fresh scan
       of six transcripts with one live session yields six documents (R-46);
    3. the remaining transcripts, as `hist:` documents;
    4. `history.jsonl`, for sessionIds with no transcript at all — `sources:[]`.

    ``prior`` (:class:`Prior`) enables the two answers a stateless pass cannot
    give: promotions, and `present:false` sources.

    **Cost, for whoever wires the poll loop.** This is O(corpus), not O(delta):
    every call re-lists each slug directory and re-reads all of
    `history.jsonl` (33 KB and five directories today, so nowhere near GD-30's
    budget, but the shape is the shape). It is deliberately not memoized — a
    scan that cached would stop noticing new sessions, which is its whole job.
    The caller is the right place to decide: run discovery on a slower cadence
    than the 250 ms tailer tick, or checkpoint `history.jsonl` on
    `(st_ino, size)` and re-read only the tail.
    """
    root = claude_root(env) if root is None else os.fspath(root)
    cwd = project_cwd(cwd, env)
    prior = prior or Prior()
    skipped = _skips()
    slugs = tuple(project_slugs(cwd, root, skipped=skipped))

    transcripts = discover_transcripts(root, slugs, skipped=skipped)
    entries = read_registry(root, cwd=cwd, slugs=slugs, proc_root=proc_root, skipped=skipped)

    sessions = []
    promotions = []
    claimed = set()

    for entry in entries:
        key = refs.session_key(entry.pid, entry.proc_start)
        sources = [Source(_rel(root, entry.path), "registry")]
        for path in transcripts.get(entry.session_id, ()):
            sources.append(Source(_rel(root, path), "transcript"))
        claimed.add(entry.session_id)
        sessions.append(SessionObservation(
            session_id=entry.session_id,
            pid=entry.pid,
            proc_start=entry.proc_start,
            cwd=cwd,
            slugs=slugs,
            session_ids=(entry.session_id,),
            sources=tuple(_with_absent(sources, prior.known_sources(key))),
            registry=dict(entry.fields),
            first_ts=entry.started_at,
            last_ts=entry.updated_at,
        ))
        # The historical twin exists only if something wrote it first — a
        # `--backfill` (transcripts, no registry) or a pass taken before this
        # process's `/clear`. `_id` is never rewritten; the twin is annotated.
        hist_key = refs.hist_session_key(entry.session_id)
        if hist_key in prior.ids:
            promotions.append(PromotionObservation(entry.session_id, key))

    for session_id in sorted(transcripts):
        if session_id in claimed:
            continue
        key = refs.hist_session_key(session_id)
        sources = [Source(_rel(root, path), "transcript")
                   for path in transcripts[session_id]]
        sessions.append(SessionObservation(
            session_id=session_id,
            cwd=cwd,
            slugs=slugs,
            session_ids=(session_id,),
            sources=tuple(_with_absent(sources, prior.known_sources(key))),
        ))

    history_only = []
    for session_id in read_history_sessions(root, cwd, skipped=skipped):
        if session_id in claimed or session_id in transcripts:
            continue
        history_only.append(session_id)
        key = refs.hist_session_key(session_id)
        sessions.append(SessionObservation(
            session_id=session_id,
            cwd=cwd,
            slugs=slugs,
            session_ids=(session_id,),
            sources=tuple(_with_absent([], prior.known_sources(key))),
        ))

    return Scan(root=root, cwd=cwd, slugs=slugs, sessions=tuple(sessions),
                promotions=tuple(promotions), skipped=skipped,
                transcripts=transcripts, history_only=tuple(history_only))


def _with_absent(sources, known):
    """Append `present:false` elements for known sources that are gone (GD-26).

    The set is keyed by `(path, kind)` so a source that came back is recorded
    present again and the two elements coexist — a disappearance is history,
    and history is not edited (GD-26's rule for the whole mirror, applied to
    the one array that can express it).
    """
    out = list(sources)
    here = {(source.path, source.kind) for source in out}
    for path in known:
        element = (path, _kind_for(path))
        if element not in here:
            here.add(element)
            out.append(Source(path, element[1], present=False))
    return out


def _kind_for(path) -> str:
    return "registry" if path.startswith(REGISTRY_DIR + "/") else "transcript"


# --- mappers (SD-1: pure — no I/O, no clock, no driver) -------------------


def _only_sessions(ops):
    """SESSIONJSONL-3's rule, enforced structurally rather than by review.

    The historical arm is a *session* identity and must never become a grouping
    key for agent records. Nothing in this module may therefore emit an
    operation for any other collection, and this is the single gate every
    mapper returns through.
    """
    for collection, _key, _update in ops:
        if collection != "sessions":
            raise SessionsError(
                f"sessions.py may only write the `sessions` collection, not {collection!r} — "
                f"a session id is never a grouping key for agent records (SESSIONJSONL-3)"
            )
    return ops


def _as_observation(observation, cls):
    """Accept a dataclass or the plain dict a replay/fixture hands back."""
    if isinstance(observation, cls):
        return observation
    if isinstance(observation, dict):
        try:
            return cls(**observation)
        except TypeError as exc:
            raise SessionsError(f"unusable {cls.__name__}: {exc}") from None
    raise SessionsError(
        f"expected a {cls.__name__} or a dict, got {type(observation).__name__}")


def _identity_on_insert(observation) -> dict:
    """The immutables (SD-11), identical for every operation on one `_id`.

    `class` and `provenance` are here rather than under `$set` because the
    mirror stores observations, not verdicts: GD-6's evidence-gated
    re-classification is derived state (GD-23), and a `$set` on `class` would
    make the stored value depend on which writer arrived last.
    """
    session_class = observation.session_class or DEFAULT_CLASS
    if session_class not in CLASSES:
        raise SessionsError(
            f"session class {session_class!r} is not one of GD-6's {CLASSES}")
    fields = {"class": session_class, "provenance": PROVENANCE}
    if getattr(observation, "live", False):
        fields["pid"] = observation.pid
        fields["procStart"] = observation.proc_start
    else:
        fields["sessionId"] = observation.session_id
    return fields


def map_session(observation):
    """`session` ⇒ one `sessions` upsert. Pure (SD-1).

    The algebra, per SD-11/GD-25: multi-valued fields are `$addToSet`,
    first/last are `$min`/`$max`, immutables are `$setOnInsert`, and the only
    `$set` fields are ones whose value is a function of the session's own facts
    (`cwd`, the registry allowlist) — so the stored document does not depend on
    the order observations arrive in, which is exactly what GD-25's
    shuffled/reversed acceptance pass measures.
    """
    obs = _as_observation(observation, SessionObservation)
    if obs.live:
        if not obs.session_id:
            raise SessionsError("a live session observation still needs its sessionId")
    elif not obs.session_id:
        raise SessionsError("a session observation needs a sessionId")

    key = obs.key()                                  # `refs` validates both arms
    on_insert = _identity_on_insert(obs)

    ids = list(dict.fromkeys([obs.session_id, *obs.session_ids]))
    add = {"sessionIds": {"$each": ids}}
    if obs.slugs:
        add["slugs"] = {"$each": list(dict.fromkeys(obs.slugs))}

    # `$each` even when the list is empty, and that is R-46's transcriptless
    # session: `{$addToSet: {sources: {$each: []}}}` on an upsert *creates* the
    # field as `[]`, so "observed, no source file" is stored as a different fact
    # from "the field is missing". Both halves below are asserted against a real
    # mongod by `tests/test_sessions.py::test_live_mongod_arm` (it skips cleanly
    # without one) rather than left as prose, because the obvious alternative is
    # wrong in a way no memory model would ever show:
    # `$setOnInsert:{sources:[]}` alongside `$addToSet:{sources:…}` is
    # "Updating the path 'sources' would create a conflict at 'sources'" — so
    # `sources` cannot live in the immutables for the empty case and in
    # `$addToSet` for the populated one without the two operations that may
    # target one `_id` (this and :func:`map_promotion`) carrying *different*
    # `$setOnInsert` payloads, which is the one order-dependence GD-25 forbids.
    add["sources"] = {"$each": [_as_observation(source, Source).as_element()
                                for source in obs.sources]}

    setters = {}
    if obs.cwd:
        setters["cwd"] = obs.cwd
    if obs.registry:
        setters["registry"] = {name: obs.registry[name]
                               for name in REGISTRY_FIELDS if name in obs.registry}

    ops = [ms.op_set_on_insert(on_insert), ms.op_add_to_set(add)]
    if setters:
        ops.append(ms.op_set(setters))
    if obs.first_ts is not None:
        ops.append(ms.op_min({"firstTs": obs.first_ts}))
    if obs.last_ts is not None:
        ops.append(ms.op_max({"lastTs": obs.last_ts}))
    return _only_sessions([("sessions", key, ms.merge_ops(*ops, collection="sessions"))])


def map_promotion(observation):
    """`sessionPromotion` ⇒ `promotedTo` on the historical document. Pure.

    R-46's immutability rule in one operation: the `hist:` `_id` is not
    rewritten, not deleted and not merged — it gains a pointer, and both
    documents stay queryable. The `$setOnInsert` payload is built by the same
    helper the session mapper uses — and carries the *same* fields, since
    neither mapper puts `sources` in the immutables — so if this operation is
    the one that happens to create the document (a promotion replayed before its
    session, which the shuffled pass does on purpose), the document is a valid
    `sessions` row and is byte-identical to the one the other order produces.
    The empty `$addToSet` is what makes that true of `sources` too: it creates
    the field as `[]` on an insert and is a no-op on an existing document.
    """
    obs = _as_observation(observation, PromotionObservation)
    if not obs.live_id.startswith("live:"):
        raise SessionsError(
            f"promotedTo must name the live arm of the union, got {obs.live_id!r}")
    ms.check_id("sessions", obs.live_id)
    key = refs.hist_session_key(obs.session_id)
    update = ms.merge_ops(
        ms.op_set_on_insert(_identity_on_insert(obs)),
        ms.op_add_to_set({"sources": {"$each": []}}),
        ms.op_set({"promotedTo": obs.live_id}),
        collection="sessions",
    )
    return _only_sessions([("sessions", key, update)])


#: SD-1's registry. `mirror.discover_mappers` finds it by name.
MIRROR_MAPPERS = {
    "session": map_session,
    "sessionPromotion": map_promotion,
}


# --- sources (the rebuild/backfill seam) ---------------------------------


def iter_session_observations(path=None, *, cwd=None, root=None, prior=None,
                              proc_root="/proc", env=None):
    """`MIRROR_SOURCES["session"]`, both modes (see `mirror.iter_sources`).

    ``path=None`` — the full scan: registry, transcripts, history. This is the
    `--rebuild` call and the one a live poll uses.

    A concrete ``path`` — the `--backfill` call, once per `.jsonl` under
    `projects/**`. It answers only for a **top-level transcript in a slug this
    project owns**; agent transcripts, journals and foreign slugs return
    nothing, which is the contract's "a source handed a path it does not own
    returns nothing".

    The per-path mode deliberately does **not** consult the registry, and the
    document it produces is therefore always the `hist:` arm. Two reasons, and
    they point the same way: a backfill is a walk of history, and the registry
    is live state that no historical file attests to; and the live arm's
    timestamps come from the registry's clock, which `Mirror.backfill` would
    (correctly) refuse against an old transcript's mtime. The live document is
    written by the next scan, and the `hist:` document this produced is what
    the promotion then annotates — which is exactly the sequence R-46's
    immutable-`_id` rule exists for.
    """
    root = claude_root(env) if root is None else os.fspath(root)
    cwd = project_cwd(cwd, env)
    if path is None:
        return list(scan(cwd=cwd, root=root, prior=prior, proc_root=proc_root, env=env).sessions)

    session_id = session_id_for_path(path)
    if session_id is None:
        return []
    # The ownership decision is one set membership test against a MEMOIZED set
    # of directories (`mirror.iter_backfill_observations`' contract) — never a
    # fresh walk of `.session-aliases` per file. The slug set is also stored on
    # the observation, so it is needed for an owned path either way; what the
    # memo removes is the per-file I/O, on owned and foreign paths alike.
    #
    # The test is ROOTED — the parent must BE `<root>/projects/<slug>`, not
    # merely be *named* like a slug. The cheap version ("is my parent
    # directory's basename in the slug set") claims any path anywhere on disk
    # whose parent happens to carry that name, which is a scope rule that stops
    # scoping the moment the caller's walk is rooted somewhere else.
    slugs = scoped_slugs(cwd, root)
    parent = os.path.dirname(os.path.abspath(os.fspath(path)))
    if parent not in scoped_dirs(cwd, root):
        return []
    prior = prior or Prior()
    key = refs.hist_session_key(session_id)
    sources = [Source(_rel(root, path), "transcript")]
    return [SessionObservation(
        session_id=session_id,
        cwd=cwd,
        slugs=tuple(slugs),
        session_ids=(session_id,),
        sources=tuple(_with_absent(sources, prior.known_sources(key))),
    )]


def iter_promotion_observations(path=None, *, cwd=None, root=None, prior=None,
                                proc_root="/proc", env=None):
    """`MIRROR_SOURCES["sessionPromotion"]`. Empty in the per-path mode.

    A promotion is a statement about a *live process*, which no file under
    `projects/**` attests to — so the backfill walk yields none, and the full
    scan yields one per live session whose historical twin ``prior`` says
    already exists.
    """
    if path is not None:
        return []
    root = claude_root(env) if root is None else os.fspath(root)
    cwd = project_cwd(cwd, env)
    return list(scan(cwd=cwd, root=root, prior=prior, proc_root=proc_root, env=env).promotions)


#: The rebuild/backfill seam declared beside the mappers (`mirror.iter_sources`).
MIRROR_SOURCES = {
    "session": iter_session_observations,
    "sessionPromotion": iter_promotion_observations,
}
