"""`aggregator/tick.py` — the ingest tick (D-01). The thing that was missing.

What this module is for
-----------------------
Every derivation Touch needs already existed and **nothing ran it**. Before
this file, `server.main()` built `ReadModel(state={})`, no loop ever called
`sessions.scan`, `ingest.read_transcript` or `ingest.read_run`, and the
`.touch/` WAL that `store.py` writes and the socket replays had no writer at
all — so a shipped install answered `/api/sessions` with `[]` over three
hundred transcripts and `streamCount: 0`, while `/api/tasks` (reduced from the
LLM-authored `events.jsonl`) was the only populated route on the server
(AGGREGATOR-1/-3, the headline inversion of GD-D2).

`IngestTick` is the composition. It owns nothing new: the discovery is
`sessions`/`ingest`'s, the parsing is each entity module's `MIRROR_SOURCES`
callable, the mapping is `mirror.discover_mappers()`, and the fold into
`ReadModel.state` is `mongo_store.apply_operations` — the identical algebra
`mirror.MemoryBackend` drives, reached directly rather than through the async
`Backend` interface because there is no database on this path and an `await`
that never yields to a driver is decoration.

Two writes, both of them the point
----------------------------------
1. **`ReadModel.state`** — the `{collection: {_id: doc}}` mapping every
   `/api/*` route reads. It is the *same dict object* the model was constructed
   with, so a tick's work is visible to the next request with no copy and no
   notification (`ReadModel`'s own docstring states the generation bound this
   buys, and it is why every reader goes through `bucket()`/`lookup()`).
2. **The `.touch/` streams** — `store.append_many` onto `run:<runId>` and
   `session:<pid>-<procStart>`, which is what makes the WS replay/tail plane
   have anything to replay.

**The third reserved stream, `custom-state`, is deliberately left unwritten,
and this is the honest half of D-01.** Its documents come from the head/slot
driver `custom_state.py` records as an open handoff — `head_write` →
`Backend.guarded_update`, `bind_slot`, `SlotTable.sweep` — and that driver
needs a database handle and a *write* tick, neither of which exists yet. The
tick READS that stream (`custom_state.MIRROR_SOURCES["customState"]` is in the
per-path pass, so a record written by anything else is ingested), and
`/health.writers.customState` says `"none"` rather than implying a producer.
Writing the tick's own bookkeeping there to make the field non-empty would be
data invented to satisfy a docstring, which is the failure mode R-58 is named
after.

Cadence and cost (GD-30)
------------------------
One `Tailer` per discovered file. A tick polls them all; a file whose tailer
reports new bytes is *dirty*, and only dirty files are re-parsed. That is the
compromise this shape buys: the tailer gives incremental, torn-tail-safe
change detection at O(bytes appended), while the `MIRROR_SOURCES` callables
take a **path** and re-read the file — so the per-tick cost is
O(files polled) + O(bytes of the files that changed), not O(corpus). A file
that never changes is never read twice.

The registry pass (`sessions`' `path=None` arm — live pids, `/proc` liveness,
promotions) is a whole-project scan, so it runs on a slower clock
(:data:`FULL_SCAN_SECONDS`) than the tail pass. It is handed a
:class:`sessions.Prior` built from `ReadModel.state`
(:meth:`IngestTick._session_prior`), which is not a nicety: a promotion is
emitted only for an id the prior already names, so a caller that passes none
has a promotion arm that can never fire.

Stdlib only (GD-21). Nothing here imports a driver, lazily or otherwise.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import inspect
import json
import os
import time

from . import ingest as ingest_mod
from . import mirror as mirror_mod
from . import mongo_store as ms
from . import paths
from . import sessions as sessions_mod
from . import store as store_mod
from . import tailer as tailer_mod

__all__ = [
    "TICK_SECONDS",
    "FULL_SCAN_SECONDS",
    "MAX_FILES",
    "MAX_EMITTED",
    "STATE_CREATED",
    "STATE_IDLE",
    "STATE_RUNNING",
    "STATE_STOPPED",
    "STATE_ABSENT",
    "IngestTick",
]

#: The declared cadence. `ReadModel`'s generation bound is stated against it.
TICK_SECONDS = 0.25

#: The registry/liveness scan's own, slower clock. It walks the project rather
#: than tailing it, so it is not something to do four times a second.
FULL_SCAN_SECONDS = 5.0

#: A bound on how many files one tick will hold tailers for. A project with
#: more than this many transcripts is real; polling all of them every 250 ms is
#: not, so the newest are kept and the count is published (`filesSeen` vs
#: `files` on `/health` is how the truncation is visible rather than silent).
MAX_FILES = 2000

#: A bound on the WAL de-duplication memo (:attr:`IngestTick._emitted`). The
#: tailer count is bounded and published; this was neither, on a process that
#: runs for weeks. Overflow drops the OLDEST half — the cost of a dropped entry
#: is one re-appended record if that exact node or usage line is re-read later,
#: which is a duplicate in an append-only history, not a wrong number. The size
#: is published on `/health.ingest.emitted` so the growth is visible rather
#: than a thing a reader has to take on trust.
MAX_EMITTED = 50_000

#: `/health.ingest.state`, and the reason it is an enum rather than a boolean:
#: "the tick is running and this project has no transcripts" and "no tick was
#: ever started" produce the same zeros and are completely different faults.
STATE_ABSENT = "absent"        # no tick object at all — the pre-D-01 server
STATE_CREATED = "created"      # constructed, no tick has completed yet
STATE_IDLE = "idle"            # ticking, zero files discovered (empty corpus)
STATE_RUNNING = "running"      # ticking, at least one file tracked
STATE_STOPPED = "stopped"      # `stop()` called

#: The observation kinds whose *own* module knows how to find them from a path.
#: The tick calls every registered source for every dirty path and lets each
#: one self-select, which is `mirror.iter_backfill_observations`' contract
#: verbatim — "returning `()` for a path you do not own is the whole contract,
#: and it must cost one `str` comparison".
_SOURCE_KWARGS = ("cwd", "root", "env", "prior")


def _accepted(fn) -> tuple:
    """Which of :data:`_SOURCE_KWARGS` ``fn`` will take.

    The five entity modules' sources do not share a keyword set —
    `ingest.iter_record_observations` takes `cwd/root/env`,
    `custom_state.iter_slot_observations` takes `root/env`, and passing an
    unexpected keyword is a `TypeError` that would take the whole tick down for
    one module's signature. Asked once, at construction.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):                              # pragma: no cover
        return ()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return _SOURCE_KWARGS
    return tuple(name for name in _SOURCE_KWARGS if name in params)


def _stream_for_run(run_id):
    return f"run:{run_id}" if isinstance(run_id, str) and run_id.strip() else None


def _agent_ref(agent_id):
    """A `{agentId}` ref, or None — `store.validate_ref` pins 17 hex chars."""
    if isinstance(agent_id, str) and len(agent_id) == 17:
        try:
            store_mod.validate_ref({"agentId": agent_id})
        except store_mod.RefError:
            return None
        return {"agentId": agent_id}
    return None


def _wal_digest(data) -> str:
    """"Has this record's payload changed?" as one comparable string.

    Canonical (`sort_keys`) rather than `repr`, because the two things compared
    are a dict this process just built and a dict `json.load` returned from the
    stream — same content, and key order is not something either side promises.
    A payload that will not encode falls back to `repr`, which is still a
    stable answer for the in-process half of the comparison.
    """
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):                              # pragma: no cover
        return repr(data)


def _record_identity(stream, record):
    """The :meth:`IngestTick._wal_spec` identity of a record ALREADY on disk.

    The inverse of `_wal_spec`'s first return value, and the reason a restart
    does not re-append the corpus: what is in the stream has to be recognizable
    as the same thing an observation would produce. One arm per written kind;
    anything else (a foreign writer's record, a kind this tick does not emit)
    returns None and simply is not memoized.
    """
    kind = record.get("kind")
    data = record.get("data")
    data = data if isinstance(data, dict) else {}
    ref = record.get("ref")
    ref = ref if isinstance(ref, dict) else {}
    if kind == "node":
        key = ref.get("key", data.get("key"))
        ordinal = ref.get("ordinal", data.get("ordinal"))
        if not isinstance(key, str) or not isinstance(ordinal, int):
            return None
        return ("run_nodes", stream, key, ordinal)
    if kind == "run":
        return ("runs", stream)
    if kind == "token":
        message_id = data.get("messageId")
        return ("usage", message_id) if isinstance(message_id, str) else None
    if kind == "session":
        return ("sessions", stream)
    return None


class IngestTick:
    """One poll loop over this project's harness files. Composition, not policy.

    ``model`` is the injected `server.ReadModel` (anything with `.state`,
    `.store`, `.tailers` and `.counters`). The tick writes those four and
    derives nothing itself: every verdict on the wire still comes out of the
    one reducer at read time (GD-23/R-54).
    """

    def __init__(self, model, *, claude_root=None, cwd=None, env=None,
                 interval=TICK_SECONDS, full_scan_interval=FULL_SCAN_SECONDS,
                 max_files=MAX_FILES, max_emitted=MAX_EMITTED, registry=None,
                 sources=None, clock=time.monotonic):
        self.model = model
        self.env = env
        self.cwd = sessions_mod.project_cwd(cwd, env)
        self.claude_root = os.fspath(
            claude_root if claude_root is not None else sessions_mod.claude_root(env))
        self.interval = float(interval)
        self.full_scan_interval = float(full_scan_interval)
        self.max_files = int(max_files)
        self.clock = clock

        self.registry = registry if registry is not None else mirror_mod.discover_mappers()
        pairs = list(sources) if sources is not None else list(mirror_mod.iter_sources())
        #: `(kind, fn, accepted-kwargs)`, resolved once (see :func:`_accepted`).
        self.sources = tuple((kind, fn, _accepted(fn)) for kind, fn in pairs)

        self.tailers = {}                  # path -> tailer.Tailer
        self.started = False
        self.stopped = False
        self.ticks = 0
        self.files_seen = 0
        self.lines_read = 0
        self.ops_applied = 0
        self.wal_records = 0
        self.errors = 0
        self.last_error = None
        self.last_tick = None              # wall-clock ISO, for an operator
        self.last_full_scan = None         # monotonic
        self.max_emitted = int(max_emitted)
        #: The records already in the WAL, as an insertion-ordered set of
        #: `((collection, key…), payload-digest)` pairs, so re-reading an
        #: unchanged file cannot append the same record twice. The digest is
        #: half of the KEY and not a value beside it, because one identity
        #: legitimately produces several payloads: `message.usage` counts are
        #: absolute and restated as a message streams (R-50), so one
        #: `message.id` writes an `out: 1` record and later an `out: 333` one.
        #: Keyed by identity alone, a restart re-appended both of them every
        #: boot — the memo could only remember the last. Capped at
        #: :attr:`max_emitted` and published on `/health`; see
        #: :meth:`_seed_stream` for what an eviction costs.
        self._emitted = {}
        #: Streams whose existing records have been folded into `_emitted`
        #: (:meth:`_seed_stream`) — one `read_all` per stream per process, and
        #: the reason a restart appends nothing over an unchanged corpus.
        self._seeded = set()
        #: How many memo entries the cap has dropped. Published, because after
        #: an eviction the de-duplication guarantee is weaker than "exactly
        #: once" and a reader should not have to infer that from the size.
        self.evicted = 0
        #: `message_id -> {field, …}` for every usage identity conflict seen —
        #: see :meth:`_raise_counters` for why this is a set and not a counter.
        self._conflicts = {}
        self._tasks_root_memo = None
        self._task = None

    # --- discovery --------------------------------------------------------

    def discover(self) -> list:
        """Every transcript and journal this project owns, newest last.

        Scope is `sessions.scoped_dirs` — the cwd's slug plus its declared
        aliases, never `projects/*` (R-25 as amended). The cap keeps the newest
        files, because the newest are the ones a live view is about.
        """
        # Never named `paths` — that is the module this class resolves its
        # tasks root through (`_tasks_root`), and a shadow of it inside the
        # class that depends on it is a loaded gun for the next edit.
        try:
            found = list(ingest_mod.iter_transcript_paths(self.claude_root, self.cwd,
                                                          env=self.env))
            found += list(ingest_mod.iter_journal_paths(self.claude_root, self.cwd,
                                                        env=self.env))
        except OSError as exc:                                   # pragma: no cover
            self._note_error(exc)
            return []
        self.files_seen = len(found)
        if len(found) <= self.max_files:
            return found

        def mtime(path):
            try:
                return os.stat(path).st_mtime
            except OSError:
                return 0.0

        return sorted(found, key=mtime)[-self.max_files:]

    def _tailer(self, path):
        got = self.tailers.get(path)
        if got is None:
            got = self.tailers[path] = tailer_mod.Tailer(path)
            # `/health` reports per-file liveness from the model's own mapping,
            # so the tailer is registered where `tailer_health()` will find it.
            # The NAME is the target hash, never the path and never the
            # basename: `/health` is the one unauthenticated route, and for a
            # session transcript the basename IS the session uuid, for an agent
            # transcript it is the agent id, and for a journal its directory is
            # the run id (GD-27, and `ReadModel.target_hash`'s own docstring).
            if isinstance(getattr(self.model, "tailers", None), dict):
                self.model.tailers[self._tailer_name(path)] = got
        return got

    def _tailer_name(self, path) -> str:
        """`tick:<digest>` — an opaque, stable name, carrying no observation.

        The digest is `ReadModel.target_hash`'s, taken from the model when it
        has one so the two names on `/health` (`name` and `target`) are the
        same function of the same path and a reader can pair them; the local
        fallback keeps the tick usable with a model stub that has no such
        method. The `tick:` prefix says which poller owns the row, which is the
        one thing a name here is for.
        """
        text = os.fspath(path)
        hasher = getattr(self.model, "target_hash", None)
        digest = hasher(text) if callable(hasher) else None
        if not isinstance(digest, str) or not digest:
            digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
        return f"tick:{digest}"

    def _retire(self, live_paths):
        """Drop tailers whose file is gone (AUDIT-15: never poll forever)."""
        for path in [p for p in self.tailers if p not in live_paths]:
            self.tailers.pop(path, None)
            if isinstance(getattr(self.model, "tailers", None), dict):
                self.model.tailers.pop(self._tailer_name(path), None)

    # --- one pass ---------------------------------------------------------

    def poll(self) -> dict:
        """One synchronous tick. Returns this tick's own counters.

        Never raises: a tick that dies takes the live view down with it, and
        every failure it can meet (an unreadable file, a mapper refusing a
        malformed observation) is a fact about ONE file. They are counted and
        published on `/health.ingest.errors`, which is the difference between
        degraded and silent.
        """
        stats = {"files": 0, "dirty": 0, "lines": 0, "ops": 0, "wal": 0, "errors": 0}
        paths = self.discover()
        self._retire(set(paths))
        dirty = []
        for path in paths:
            tail = self._tailer(path)
            before = tail.lines_read
            try:
                tail.poll()
            except OSError as exc:                               # pragma: no cover
                self._note_error(exc, stats)
                continue
            gained = tail.lines_read - before
            stats["lines"] += max(0, gained)
            # A file is dirty when it grew OR when this is the first time we
            # have seen it (a corpus that exists before the server starts is
            # the normal case, and it never "grows" at all).
            if gained or before == 0:
                dirty.append(path)
        stats["files"] = len(paths)
        stats["dirty"] = len(dirty)

        # Built ONCE per tick, from the state this tick is about to update. It
        # is what makes `sessions`' promotion arm and its `present:false`
        # source retirement reachable at all (see :meth:`_session_prior`).
        prior = self._session_prior()
        observations = []
        for path in dirty:
            observations.extend(self._observe(path, stats, prior=prior))
        if self._full_scan_due():
            observations.extend(self._observe(None, stats, prior=prior))
            self.last_full_scan = self.clock()

        self._apply(observations, stats)
        self.ticks += 1
        self.started = True
        self.lines_read += stats["lines"]
        self.ops_applied += stats["ops"]
        self.wal_records += stats["wal"]
        self.last_tick = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="milliseconds")
        return stats

    def _full_scan_due(self) -> bool:
        if self.full_scan_interval <= 0:
            return False
        if self.last_full_scan is None:
            return True
        return (self.clock() - self.last_full_scan) >= self.full_scan_interval

    def _observe(self, path, stats, *, prior=None) -> list:
        """`(kind, observation)` pairs for one path, or for the whole project.

        ``path=None`` is the registry arm and is deliberately restricted to the
        sources that answer it usefully — `sessions` (live pids, `/proc`
        liveness, promotions) and `custom_state` (the `.touch/` WAL and the
        control files, neither of which lives under `projects/**`). Handing the
        transcript sources a `None` here would re-read the entire corpus every
        five seconds, which is the O(corpus) tick this design exists to avoid.
        """
        out = []
        for kind, fn, accepted in self.sources:
            if path is None and kind not in ("session", "sessionPromotion",
                                             "customState", "slot"):
                continue
            kwargs = {}
            if "cwd" in accepted:
                kwargs["cwd"] = self.cwd
            if "env" in accepted:
                kwargs["env"] = self.env
            if "root" in accepted:
                kwargs["root"] = self._root_for(kind)
            if "prior" in accepted and prior is not None:
                kwargs["prior"] = prior
            try:
                for observation in fn(path, **kwargs) or ():
                    out.append((kind, observation))
            except Exception as exc:                             # noqa: BLE001
                # One module's bad file must not stop the other four.
                self._note_error(exc, stats)
        return out

    def _root_for(self, kind):
        """The tree ``kind``'s `root` keyword means. One name, three trees.

        `root` is not one thing across the five entity modules and passing the
        wrong tree under the right name is a silent misread waiting for the day
        a source stops refusing the path first: `custom_state`'s root is the
        `.touch/` store, `legacy`'s is the **tasks** root that `sourcePath`s
        are relative to, and everything under `projects/**` is rooted at
        `~/.claude`.
        """
        if kind in ("customState", "slot"):
            return self._touch_root()
        if kind in ("legacyEvent", "legacyArtifact"):
            return self._tasks_root()
        return self.claude_root

    def _touch_root(self):
        store = getattr(self.model, "store", None)
        return getattr(store, "root", None)

    def _tasks_root(self):
        """The tasks root, resolved at most once.

        The model's is the answer whenever it has one (`server.main` always
        sets it). Otherwise the ONE resolver in `paths` decides — never a
        second ladder — and the answer is memoized, because `legacy`'s sources
        resolve their root before refusing a path they do not own and this is
        called once per source per dirty file.
        """
        configured = getattr(self.model, "tasks_root", None)
        if configured:
            return configured
        if self._tasks_root_memo is None:
            self._tasks_root_memo = paths.tasks_root(env=self.env)
        return self._tasks_root_memo

    def _session_prior(self):
        """`sessions.Prior` built from the state this tick owns, or None.

        Without it two of `sessions.scan`'s arms are unreachable code with a
        docstring saying they run. A promotion — the thing that stops a live
        session and its historical twin rendering as two rows (R-46) — is
        emitted **only** for a `hist:` id already in `prior.ids`, and a
        `present:false` source element is produced **only** for a path in
        `prior.sources` that has since gone. A stateless caller passing nothing
        gets neither, forever.

        The tick is not stateless: `ReadModel.state` *is* the mirror's content,
        so the prior is read straight out of it. Sources are the ones currently
        recorded present — an element already marked absent must not be
        re-offered as a disappearance every tick.
        """
        state = getattr(self.model, "state", None)
        docs = state.get("sessions") if isinstance(state, dict) else None
        if not isinstance(docs, dict) or not docs:
            return None
        ids = []
        sources = {}
        for key, doc in docs.items():
            if not isinstance(key, str):
                continue
            ids.append(key)
            elements = doc.get("sources") if isinstance(doc, dict) else None
            present = [element.get("path") for element in (elements or ())
                       if isinstance(element, dict) and element.get("present", True)
                       and isinstance(element.get("path"), str)]
            if present:
                sources[key] = present
        return sessions_mod.Prior(ids=frozenset(ids), sources=sources)

    def _live_session_stream(self, session_id):
        """`session:<pid>-<procStart>` for a LIVE session id, or None.

        The one mapping from a session uuid to a `.touch/` stream, and the same
        one `ReadModel.session_stream` serves reads through: a `live:` document
        keyed `live:<pid>-<procStart>` whose `sessionIds[]` names the uuid
        (a promotion appends to that list, R-46). No live document means no
        process, which means no stream — the caller writes no record rather
        than opening a file no reader can name (GD-12, one layer down).

        Read from `ReadModel.state` rather than from the observation because
        `_apply` folds this tick's operations BEFORE it writes the WAL, so a
        session promoted by the very batch being written is already there.
        """
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        state = getattr(self.model, "state", None)
        docs = state.get("sessions") if isinstance(state, dict) else None
        if not isinstance(docs, dict):
            return None
        for key, doc in docs.items():
            if not isinstance(key, str) or not key.startswith("live:"):
                continue
            if not isinstance(doc, dict):
                continue
            if session_id in (doc.get("sessionIds") or ()):
                return "session:" + key[len("live:"):]
        return None

    def _apply(self, observations, stats):
        """Observations ⇒ operations ⇒ `ReadModel.state` + the `.touch/` WAL."""
        if not observations:
            return
        ops = []
        for kind, observation in observations:
            try:
                ops.extend(mirror_mod.map_observation(self.registry, kind, observation))
            except Exception as exc:                             # noqa: BLE001
                self._note_error(exc, stats)
        if ops:
            try:
                ms.apply_operations(self.model.state, ops)
                stats["ops"] += len(ops)
            except ms.MongoStoreError as exc:
                self._note_error(exc, stats)
        stats["wal"] += self._append_wal(observations, stats)
        self._raise_counters(observations)

    def _raise_counters(self, observations):
        """`ingest.usage_conflicts` into `ReadModel.counters` — the sp-12 handoff.

        `server.py` records this exact handoff as *taken*: the model carries a
        `counters` dict a caller raises into and `/health` publishes it. This
        is that caller.

        The counters are **cumulative distinct message ids**, assigned rather
        than incremented, because a file is re-scanned whole whenever it grows:
        a `+=` would report the same three conflicting ids over and over and
        turn a topology fact into a clock. The scope is one tick's batch, which
        is the scope the conflict is visible in — a whole file's usage arrives
        in one batch, and `usage_session_span` (the expected, benign one) is a
        property of one agent's fragments within a run.
        """
        usage = [obs for kind, obs in observations if kind == "usage"]
        if not usage:
            return
        counters = getattr(self.model, "counters", None)
        if not isinstance(counters, dict):
            return
        try:
            conflicts = ingest_mod.usage_conflicts(usage)
        except Exception as exc:                                 # noqa: BLE001
            self._note_error(exc)
            return
        for message_id, row in (conflicts or {}).items():
            known = self._conflicts.setdefault(message_id, set())
            known.update(row)
        if not self._conflicts:
            return
        counters["usageConflicts"] = len(self._conflicts)
        for name in ingest_mod.USAGE_IDENTITY:
            count = sum(1 for fields in self._conflicts.values() if name in fields)
            if count:
                counters[f"usageConflict_{name}"] = count

    # --- the WAL ----------------------------------------------------------

    def _append_wal(self, observations, stats=None) -> int:
        """`store.append_many` onto the reserved streams. One record, one stream.

        A token record goes to `run:<runId>` when a runId is known and to the
        session's own `session:<pid>-<procStart>` otherwise — never both,
        because a deliberately duplicated copy would be counted twice by every
        global sum that does not know to skip it.

        Batched per stream (one `flock`, one `write()`), and de-duplicated
        against :attr:`_emitted` on `(identity, payload digest)`: the sources
        re-read a whole file whenever it grows, so without the memo a 500-line
        transcript would re-append its every node on every append to it, while
        a memo keyed on identity alone would suppress the *restatements* a
        streaming `message.usage` legitimately produces. The memo is **seeded
        from the stream itself** the first time this process touches it
        (:meth:`_seed_stream`) — without that it only spans one process, and
        every restart re-appends the whole corpus to an append-only file that
        has no compaction step.
        """
        store = getattr(self.model, "store", None)
        if store is None:
            return 0
        batches = {}
        for kind, observation in observations:
            try:
                spec = self._wal_spec(kind, observation)
            except (store_mod.StoreError, AttributeError, TypeError, ValueError) as exc:
                # A malformed observation is one line of one file — `poll()`
                # promises not to raise, and `normalize_tokens` refusing a
                # stringly-typed count is exactly the refusal that should cost
                # one record and a counter, not the live view.
                self._note_error(exc, stats)
                continue
            if spec is None:
                continue
            stream, identity, record = spec
            self._seed_stream(store, stream, stats)
            key = (identity, _wal_digest(record.get("data")))
            if key in self._emitted:
                continue
            self._remember(key)
            batches.setdefault(stream, []).append(record)
        written = 0
        for stream, specs in batches.items():
            try:
                written += len(store.append_many(stream, specs))
            except (store_mod.StoreError, OSError) as exc:
                self._note_error(exc, stats)
        return written

    def _seed_stream(self, store, stream, stats=None):
        """Populate :attr:`_emitted` from what is ALREADY in ``stream``. Once.

        The de-duplication memo guards an append-only file that outlives the
        process holding it. `poll()` marks a file dirty on its first sight of
        it — which is every file on the first tick of every boot — so a memo
        that starts empty makes a restart re-parse the corpus AND re-append
        every record it already wrote: three boots over one unchanged corpus
        measured 583 / 1166 / 1749 records on one run stream. Nothing recovers
        that; `.touch/` has no compaction step, and `/api/events` and the
        socket's replay would serve every node and token twice.

        So the memo is seeded from the store it guards, lazily: one
        `read_all()` per stream per process, on the first append to it, keyed
        by the same identity/digest pair :meth:`_wal_spec` produces. A stream
        that does not exist yet reads as empty — `read_all` reports a missing
        file as no records, which is the right answer.

        **The eviction bound, stated.** Seeding happens once per stream, so an
        identity dropped by :meth:`_remember`'s cap is NOT re-seeded: if that
        exact node or usage line is read again later in the same process it is
        appended a second time. One duplicate record in an append-only history
        is not a wrong number (every reader of `run_nodes`/`usage` is keyed and
        latest-wins), but it is a real cost, so the eviction count is published
        on `/health.ingest.evicted` rather than left to be inferred.
        """
        if stream in self._seeded:
            return
        self._seeded.add(stream)
        try:
            records = store.read_all(stream)
        except (store_mod.StoreError, OSError) as exc:
            self._note_error(exc, stats)
            return
        for record in records:
            if not isinstance(record, dict):
                continue
            identity = _record_identity(stream, record)
            if identity is None:
                continue
            self._remember((identity, _wal_digest(record.get("data"))))

    def _remember(self, key):
        """Record one written `(identity, digest)`, evicting past the cap.

        A dict preserves insertion order, so "oldest" is free and the eviction
        is one slice. Dropping half rather than one entry keeps the amortized
        cost at O(1) per insert; dropping the *oldest* is right because the
        newest files are the ones a live view keeps re-reading. What an
        eviction costs is stated in :meth:`_seed_stream`.
        """
        self._emitted[key] = None
        if self.max_emitted > 0 and len(self._emitted) > self.max_emitted:
            keep = self.max_emitted // 2
            dropped = list(self._emitted)[:len(self._emitted) - keep]
            for stale in dropped:
                self._emitted.pop(stale, None)
            self.evicted += len(dropped)

    def _wal_spec(self, kind, obs):
        """`(stream, identity, spec)` for one observation, or None to skip.

        Only the four kinds a live page actually replays get a record. The
        others (`record`, `streamMeta`) are the *corpus*: mirroring 300 000
        transcript lines into `.touch/` would make the WAL a second copy of
        `~/.claude` rather than the event stream the socket tails.
        """
        if kind == "runNode":
            stream = _stream_for_run(getattr(obs, "run_id", None))
            if stream is None:
                return None
            ref = {"runId": obs.run_id, "key": obs.key, "ordinal": obs.ordinal}
            data = {"key": obs.key, "ordinal": obs.ordinal, "agentId": obs.agent_id,
                    "label": obs.label, "resultSeen": bool(obs.result_seen),
                    "harnessState": obs.harness_state,
                    "lastToolName": obs.last_tool_name,
                    "lastToolSummary": obs.last_tool_summary}
            return stream, ("run_nodes", stream, obs.key, obs.ordinal), {
                "kind": "node", "provenance": "harness", "source": "ingest",
                "ref": ref, "data": {k: v for k, v in data.items() if v is not None},
                "ts": None}
        if kind == "run":
            stream = _stream_for_run(getattr(obs, "run_id", None))
            if stream is None:
                return None
            data = {"runId": obs.run_id, "status": obs.status,
                    "workflowName": obs.workflow_name, "taskId": obs.task_id}
            return stream, ("runs", stream), {
                "kind": "run", "provenance": "harness", "source": "ingest",
                "ref": None, "data": {k: v for k, v in data.items() if v is not None},
                "ts": None}
        if kind == "usage":
            stream = _stream_for_run(getattr(obs, "run_id", None))
            if stream is None:
                # NOT `session:<sessionId>`. `store.py`'s table declares exactly
                # one session grammar — `session:<pid>-<procStart>` — and
                # `ReadModel.session_stream` can resolve no other, so a uuid-keyed
                # stream is a file no route will ever open while the session's
                # own stream shows no tokens at all. The live document is the
                # only thing that maps a sessionId to a process, and a
                # historical session has no process and therefore no stream:
                # that is a fact to report, not a stream to invent (the same
                # reasoning the `session` arm below already applies).
                stream = self._live_session_stream(getattr(obs, "session_id", None))
            if stream is None:
                return None
            # The four token keys go at the TOP level of `data` — where
            # `store.normalize_tokens` puts them (GD-11: "a token record always
            # carries all four"), where `_build_record` re-normalizes them, and
            # where every reader looks (`app.js`'s `noteTokens` and
            # `renderRecord` both read `record.data[k]`). Nesting them under a
            # `tokens` sub-document left `_build_record` filling the real four
            # with 0 beside it, so every token line of a 4.3 M-token run
            # rendered `in 0 · out 0 · cached 0 · cache_write 0`.
            data = dict(store_mod.normalize_tokens(getattr(obs, "tokens", None) or {}))
            data["messageId"] = obs.message_id
            return stream, ("usage", obs.message_id), {
                "kind": "token", "provenance": "harness", "source": "ingest",
                "ref": _agent_ref(getattr(obs, "agent_id", None)),
                "data": data, "ts": None}
        if kind == "session":
            if not getattr(obs, "live", False):
                # A `hist:` document names no process, so there is no
                # `session:<pid>-<procStart>` stream for it to belong to. It is
                # in `ReadModel.state` (the sidebar lists it); it is simply not
                # an event.
                return None
            stream = f"session:{obs.pid}-{obs.proc_start}"
            return stream, ("sessions", stream), {
                "kind": "session", "provenance": "harness", "source": "ingest",
                "ref": {"pid": obs.pid, "procStart": str(obs.proc_start)},
                "data": {"sessionId": obs.session_id, "cwd": obs.cwd}, "ts": None}
        return None

    # --- lifecycle --------------------------------------------------------

    def _note_error(self, exc, stats=None):
        """Count a per-file failure and remember its TYPE — never its message.

        The message is deliberately dropped: `/health` is the one
        unauthenticated route, and an `OSError`'s string carries the filename,
        which is `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl` — the home
        directory, the cwd and a session uuid, i.e. exactly the three
        observations GD-27 keeps behind the token. A type name answers "is
        something failing, and is it I/O or a schema refusal", which is what an
        operator needs from an open route.

        ``stats`` is this tick's own returned counters. Callers that have one
        pass it, so a failure is visible BOTH cumulatively (`/health`) and in
        the tick that met it — `poll()`'s return value is what a caller with no
        HTTP surface (a test, a future supervisor) actually reads.
        """
        self.errors += 1
        self.last_error = type(exc).__name__
        if isinstance(stats, dict):
            stats["errors"] = stats.get("errors", 0) + 1

    async def run(self):
        """Poll until :meth:`stop`. The loop `server.main()` starts as a task.

        The poll itself runs on a worker thread: it is blocking file I/O, and
        `ReadModel`'s readers are already `to_thread`'d for the same reason.
        """
        self.stopped = False
        while not self.stopped:
            try:
                await asyncio.to_thread(self.poll)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                             # noqa: BLE001
                self._note_error(exc)
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise

    def start(self):
        """Schedule :meth:`run` on the running loop; returns the task."""
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self.run())
        return self._task

    def stop(self):
        self.stopped = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        self._task = None

    # --- /health ----------------------------------------------------------

    @property
    def state(self) -> str:
        if self.stopped:
            return STATE_STOPPED
        if not self.started:
            return STATE_CREATED
        return STATE_RUNNING if self.tailers else STATE_IDLE

    def health(self) -> dict:
        """`/health.ingest` — operational facts only, never an observation.

        No path, no session id, no run id: `/health` is unauthenticated, and
        the same rule that made `tailer_health` publish a target *hash* applies
        to every number here (GD-27).
        """
        return {
            "state": self.state,
            "files": len(self.tailers),
            "filesSeen": self.files_seen,
            "linesRead": self.lines_read,
            "lastTick": self.last_tick,
            "ticks": self.ticks,
            "ops": self.ops_applied,
            "walRecords": self.wal_records,
            # The de-duplication memo's size and its cap. Every other number
            # that grows on this block is bounded and published; this one used
            # to be neither, on a process that runs for weeks.
            "emitted": len(self._emitted),
            "maxEmitted": self.max_emitted,
            # Non-zero means the memo has dropped identities, so a later
            # re-read of one of them appends a duplicate (:meth:`_seed_stream`).
            "evicted": self.evicted,
            "seededStreams": len(self._seeded),
            "errors": self.errors,
            "lastError": self.last_error,
            "intervalSeconds": self.interval,
            "fullScanSeconds": self.full_scan_interval,
        }
