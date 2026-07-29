#!/usr/bin/env python3
"""End-to-end acceptance for Touch (R-56 amendment + R-37's non-control arms).
Run as `python3 test_e2e_sim.py`; exits non-zero on failure. No pytest, no runner.

Every other test file in this suite proves one module's contract against inputs
that file constructs. This one proves the *composition*: real frozen bytes go in
at the file end and the answers come out of the read API, through
`files -> ingest -> mirror -> reducer -> /api/*`, with nothing hand-built in
between. That is the only place several plan claims are decidable at all — "the
live view is fully functional with no database" is not a statement about
`mirror.py`, it is a statement about what `/api/sessions` returns while
`/health` says `mirror: absent`.

The arms, and the clause each one exists for:

* **No-mongod (R-56)** — with pymongo *blocked in a child interpreter* and with
  a mongod that does not answer: sessions, agent rows, loop cards and token
  counters all update, `/health` reports `mirror: absent|down`, every module
  imports, and the reduction over the corpus is **byte-identical** to the one a
  pymongo-bearing interpreter computes. That last equality is what "the suite is
  green on a bare checkout" means operationally (GD-21/GD-22).
* **Mirror (R-56)** — GD-25's double-ingest fingerprint and R-45's
  wipe + `--rebuild` equivalence, both over the *whole real corpus* rather than
  a synthetic op list (`test_mirror.py` owns the synthetic form); the
  `wf_455b348c-e17` retry topology and the `a2fc883c96ff7b837` cross-session
  union rendered through the full path. Live-mongod repeats the fingerprint
  arms against a real server and **skips cleanly** without one (R-42's
  loopback+auth recipe, `docs/mongo.md` §1).
* **Budget (R-56)** — LIVEFLOW-15's O(delta) claim measured: 1 KB appended to a
  20 MB stream costs a tick under 64 KB of reads. MONGOSCHEMA-4's 30.1 s stall
  measured against a dead mongod while ingest keeps serving.
* **Phase 1 (R-37)** — the real `decision_watcher.py` process over the frozen
  `wf_829e6f58-b2f` run emits zero `failed` plan badges, and **its own output
  stream** then reads back through `legacy.py` and `/api/tasks` as `done` — the
  two halves of the R-58 fix, joined. The frozen `touch-aggregator` stream (the
  historic output of the *broken* watcher on that same run) re-labels to
  "closed — no verdict", never `failed`.
* **Phase 3 (R-37)** — `wf_829e6f58-b2f` renders six distinctly-labelled
  researcher nodes, deduped token rollups that agree between the memory twin and
  the mirrored documents, and all three liveness states; the legacy path renders
  `touch-repo-recon` with stale-closed agents.

Rules this file keeps:

* `tests/fixtures/` is sp-02's and **read-only**. The corpus root is a temp
  `~/.claude`-shaped tree of *directories* (so `os.walk` descends) whose leaves
  are **symlinks** to the frozen files: 8 MB of bytes stay shared and unmodified.
* R-37's phase-4 control arm is excluded with the rest of phase 4, and the live
  smoke check against the real `~/.claude` is manual, never acceptance
  (sp-14's shared decisions).
* Nothing here needs a *live Claude process*. R-46's `live:` arm is the one
  claim a frozen corpus cannot make on its own — liveness is a `(pid,
  procStart)` fact about the machine running the suite — so those arms SKIP
  where no live session exists and the historical arm is asserted instead
  (:data:`LIVE_REGISTRY`, GD-C7). That is what keeps this file green in a
  clean checkout, which is `release.sh` step 2's gate.
"""

import asyncio
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The canonical trees are named through `tests/_roots.py`, never by a
# literal under REPO: GD-U1 moves them and this is the single flip point.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import MON, SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))

from aggregator import agents as agents_mod                       # noqa: E402
from aggregator import ingest as ingest_mod                       # noqa: E402
from aggregator import legacy as legacy_mod                       # noqa: E402
from aggregator import mirror as mr                               # noqa: E402
from aggregator import mongo_store as ms                          # noqa: E402
from aggregator import refs                                       # noqa: E402
from aggregator import sessions as sess                           # noqa: E402
from aggregator import server as server_mod                       # noqa: E402
from aggregator import tailer as tailer_mod                       # noqa: E402

FIX = REPO / "tests" / "fixtures"
RUN_FIX = FIX / "run-wf_829e6f58"
DISCOVERY = FIX / "mirror" / "discovery"
LEGACY_FIX = FIX / "legacy"
WATCHER = MON / "decision_watcher.py"

#: The run ids and session ids the plan names. Spelled out because every
#: assertion below is about *these* specimens, not about "some run".
RUN_829 = "wf_829e6f58-b2f"          # the completed research run (6 + 1)
RUN_455 = "wf_455b348c-e17"          # the killed run: 3 keys x 2 ordinals
RUN_B29 = "wf_b297177a-d11"          # the run with NO terminal snapshot
DD = "dd469822-2546-47d9-aaa3-31db4cb705e8"
E4 = "e423cd3c-f859-45af-9afd-0d6bdec9b4ac"
A8 = "a8d43bb1-0313-45d4-8784-4827af443ead"
CROSS_AGENT = "a2fc883c96ff7b837"    # the disjoint-continuation pair (R-03)

#: The cwd the corpus claims to belong to. Discovery is scoped to its slug
#: (R-25 as amended), which is what makes the foreign `/tmp` slugs a negative
#: control rather than four extra sessions.
OWNED_CWD = "/home/laniakea/Projects/touch"
OWNED_SLUG = sess.slug_for(OWNED_CWD)

#: The frozen slug whose transcripts are re-homed under the owned slug — the
#: corpus's only **top-level** session transcripts, and therefore the only
#: source of `sessions` rows (`run-wf_829e6f58/` holds session *directories*,
#: never a `<sessionId>.jsonl`).
LIVEIO_SLUG = "-tmp-claude-1000-liveio"

#: The rest of `mirror/discovery/projects/` — read from disk rather than
#: spelled out, because two of the four are 100-character nested-run slugs and a
#: typo in a negative control is a control that silently stops controlling.
FOREIGN_SLUGS = tuple(sorted(
    entry.name for entry in (DISCOVERY / "projects").iterdir()
    if entry.is_dir() and entry.name != LIVEIO_SLUG
)) if (DISCOVERY / "projects").is_dir() else ()


def _registry_entry_is_live():
    """True when the frozen registry entry's pid is running with its recorded
    start time.

    Deliberately the `(pid, procStart)` half of `sessions.read_registry`'s
    liveness rule and nothing else — not the cwd scoping, `_SESSION_ID_RE`,
    `procStart.isdigit()` or the pid type/range gates it also applies
    (`plugin/touch/aggregator/sessions.py:706-731`). The frozen corpus
    satisfies every one of those, so on THIS input the two agree; the pid half
    is the only one that varies by machine, which is the whole reason this
    predicate exists. If the corpus's `cwd` ever diverges from `OWNED_CWD`,
    do not widen the copy — derive liveness by calling `sess.read_registry`
    over the corpus's `sessions/` dir instead.
    """
    try:
        entry = json.loads(
            (DISCOVERY / "sessions" / "15934.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):                               # pragma: no cover
        return False
    return (sess.read_proc_start(entry.get("pid")) == entry.get("procStart")
            and entry.get("procStart") is not None)


#: R-46's `live:` arm needs a **running process**, and no fixture can freeze
#: one. `sessions.read_registry` calls the frozen `sessions/15934.json` entry
#: live only when THIS machine's `/proc/<pid>/stat` field 22 equals the recorded
#: `procStart` — the `(pid, procStart)` identity that pid reuse forces. On any
#: machine where that pid is gone (every machine but the one the corpus was
#: frozen on, and every clean checkout), the entry is `registry_stale_pid` and
#: its session — `a8d43bb1…`, which the corpus deliberately gives NO top-level
#: transcript — produces no row at all.
#:
#: So liveness is measured here, once, and the arms that depend on it skip
#: rather than fail: `run_all.sh`'s header promises "files that read the absent
#: things SKIP there; nothing crashes", and a live Claude process is an absent
#: thing (GD-C7). Everything the corpus *can* attest to still runs.
#:
#: The `kind:"live"` mapping is NOT lost by skipping here, so do not "restore"
#: the unconditional assertion: `test_api.py`'s
#: `test_sessions_lists_both_classes` pins `live:<pid>-<procStart>` →
#: `kind:"live"` on a synthetic registry, and
#: `test_sessions.py`'s `test_a_reused_pid_is_not_a_live_session`
#: / `test_the_project_dir_yields_six_documents_exactly_one_live` cover the
#: registry identity and the `registry_stale_pid` path against a synthetic
#: `proc_root`. What is unavailable off the freezing machine is only the
#: *composition-level* claim, which is exactly the machine-dependent part.
LIVE_REGISTRY = _registry_entry_is_live()

#: The `/api/sessions` floor: the four `liveio` top-level transcripts, plus the
#: `live:` row only while the frozen registry entry's process is running.
SESSION_ROWS = 5 if LIVE_REGISTRY else 4

#: MONGOSCHEMA-4's measurement: pymongo's default server selection stalls a
#: poll loop for 30.1 s against a dead port. Nothing here may approach it.
STALL_SECONDS = 30.0

#: LIVEFLOW-15's budget, verbatim from R-56: 1 KB appended to a 20 MB stream
#: must cost one tick less than 64 KB of reads.
BIG_STREAM_BYTES = 20 * 1024 * 1024
APPEND_BYTES = 1024
TICK_READ_BUDGET = 64 * 1024

failures = []
skips = []
TMPDIRS = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  SKIP: {msg}")
    skips.append(msg)


def note(msg):
    """A recorded boundary that is NOT a skip and NOT a failure.

    Used exactly once, for a cross-sub-plan interaction this file *measures*
    and does not own; see :func:`test_phase3_three_state_liveness`. Distinct
    from `skip()` because every assertion around it ran.
    """
    print(f"  note: {msg}")


def tmpdir(name):
    path = tempfile.mkdtemp(prefix=f"touch-e2e-{name}-")
    TMPDIRS.append(path)
    return path


def run(coro):
    return asyncio.run(coro)


# --- the corpus: a `~/.claude`-shaped tree of symlinks to frozen bytes -----


def _link_tree(src, dst):
    """Recreate `src`'s directory tree at `dst` with the FILES symlinked.

    Directories are real because `mirror.iter_backfill_sources` and
    `ingest.iter_transcript_paths` both `os.walk`, and `os.walk` does not
    descend a symlinked directory. The 8 MB of frozen bytes are shared, never
    copied and never opened for writing.
    """
    os.makedirs(dst, exist_ok=True)
    for name in sorted(os.listdir(src)):
        source, target = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(source):
            _link_tree(source, target)
        elif not os.path.exists(target):
            os.symlink(source, target)


def build_corpus(tmp):
    """The `~/.claude` root every arm ingests. Returns its path.

    Layout, and why each piece is where it is:

    * `projects/<owned>/` — the two `wf_829e6f58-b2f` session directories (the
      `/clear` split), the `a8d43bb1…` live-run-shape session (no snapshot), and
      the five `liveio` transcripts, which are the corpus's only **top-level**
      session transcripts and therefore the only source of `sessions` rows.
    * `projects/<owned>/<E4>/…/wf_455b348c-e17/journal.jsonl` and
      `projects/<owned>/<E4>/workflows/wf_455b348c-e17.json` — the killed run,
      restored to the two places the harness writes them (PROVENANCE.md records
      it came from that session; the fixture stores the pair flat).
    * `projects/<foreign>/` — three foreign `/tmp` slugs, the R-25 negative
      control: a `projects/*` enumerator ingests them, a scoped one must not.
    * `sessions/15934.json` — the live-session registry entry (R-46's `live:`
      arm; its filename is the raw pid, which is why identity is
      `(pid, procStart)`). It only *reads* live where that pid is running with
      the recorded start time; elsewhere the arms that need it skip — see
      :data:`LIVE_REGISTRY`.
    """
    root = os.path.join(tmp, "claude")
    owned = os.path.join(root, "projects", OWNED_SLUG)
    os.makedirs(owned, exist_ok=True)
    _link_tree(os.fspath(RUN_FIX), owned)
    _link_tree(os.fspath(FIX / "mirror" / "live-run-shape" / A8),
               os.path.join(owned, A8))
    _link_tree(os.fspath(DISCOVERY / "projects" / LIVEIO_SLUG), owned)
    for slug in FOREIGN_SLUGS:
        source = DISCOVERY / "projects" / slug
        if source.is_dir():
            _link_tree(os.fspath(source), os.path.join(root, "projects", slug))
    _link_tree(os.fspath(DISCOVERY / "sessions"), os.path.join(root, "sessions"))

    killed = FIX / "mirror" / RUN_455
    journal_dir = os.path.join(owned, E4, "subagents", "workflows", RUN_455)
    os.makedirs(journal_dir, exist_ok=True)
    os.symlink(os.fspath(killed / "journal.jsonl"),
               os.path.join(journal_dir, "journal.jsonl"))
    snapshot_dir = os.path.join(owned, E4, "workflows")
    os.makedirs(snapshot_dir, exist_ok=True)
    os.symlink(os.fspath(killed / f"{RUN_455}.json"),
               os.path.join(snapshot_dir, f"{RUN_455}.json"))
    sess.reset_scope_cache()
    return root


def build_legacy_root(tmp):
    """The orchestrator root: one task folder per frozen `events.jsonl`."""
    base = os.path.join(tmp, "orchestrators")
    for name in sorted(os.listdir(LEGACY_FIX)):
        if not name.endswith("-events.jsonl"):
            continue
        folder = os.path.join(base, name[: -len("-events.jsonl")])
        os.makedirs(folder, exist_ok=True)
        os.symlink(os.fspath(LEGACY_FIX / name),
                   os.path.join(folder, "events.jsonl"))
    return base


def corpus_env(root, legacy_root, state_dir):
    """The four variables that point every module at the temp corpus."""
    return {"TOUCH_CLAUDE_ROOT": root, "TOUCH_PROJECT_CWD": OWNED_CWD,
            "TOUCH_STATE_DIR": state_dir, "TOUCH_LEGACY_ROOT": legacy_root}


class Corpus:
    """Corpus root + orchestrator root + the process env they need.

    The entity modules read `os.environ` (their `MIRROR_SOURCES` callables take
    no root — `mirror.iter_rebuild_observations` calls `source()`), so the
    variables are set for the duration of a `with` block and restored after.
    """

    def __init__(self, name):
        self.tmp = tmpdir(name)
        self.root = build_corpus(self.tmp)
        self.legacy_root = build_legacy_root(self.tmp)
        self.state_dir = os.path.join(self.tmp, "touchstate")
        self.env = corpus_env(self.root, self.legacy_root, self.state_dir)
        self._saved = {}

    def __enter__(self):
        for name, value in self.env.items():
            self._saved[name] = os.environ.get(name)
            os.environ[name] = value
        sess.reset_scope_cache()
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        sess.reset_scope_cache()
        return False

    @property
    def owned(self):
        return os.path.join(self.root, "projects", OWNED_SLUG)


def observations():
    """`(kind, obs)` for the whole corpus — `--rebuild`'s own seam, unmodified."""
    return list(mr.iter_rebuild_observations())


def new_mirror(backend=None):
    mirror = mr.Mirror(mr.MongoConfig("uri-placeholder", "touch_test"),
                       backend=backend if backend is not None else mr.MemoryBackend({}),
                       registry=mr.discover_mappers())
    mirror.state = mr.STATE_LIVE
    return mirror, mirror.backend


def ingest_corpus(mirror=None, obs=None):
    """The full path, once: files -> sources -> mappers -> mirror -> state.

    Returns `(mirror, backend, report, observations)`; the observation list
    comes back because reading the corpus is the expensive half and several
    arms need to compute an in-memory twin from the very same inputs the
    mirror was handed.
    """
    if mirror is None:
        mirror, _ = new_mirror()
    obs = observations() if obs is None else obs
    report = run(mirror.rebuild(obs))
    return mirror, mirror.backend, report, obs


def counts_of(backend):
    """`backend.counts()` minus `writers` — the lease is runtime, not history."""
    return {name: value for name, value in run(backend.counts()).items()
            if name != "writers"}


def observation_state(state):
    """State minus `writers` — a lease carries a pid and an expiry.

    Same exclusion `test_mirror.py` makes, and for the same reason: a
    fingerprint that includes the lease cannot be compared across a wipe, a
    process, or a backend, which is every comparison this file makes.
    """
    return {name: bucket for name, bucket in state.items() if name != "writers"}


def api_for(backend, corpus, *, mirror=None):
    """`(model, api, get)` over a mirrored state. `get` returns `(status, body)`."""
    model = server_mod.ReadModel(state=backend.state, mirror=mirror,
                                 tasks_root=corpus.legacy_root,
                                 claude_root=corpus.root)
    auth = server_mod.Auth()
    api = server_mod.Api(model, auth=auth)
    headers = {"x-touch-token": auth.token}

    def get(route, **query):
        response = api.handle("GET", route,
                              {name: [str(value)] for name, value in query.items()},
                              headers)
        return response.status, json.loads(response.body.decode("utf-8"))

    return model, api, get


def isoformat(moment):
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# =========================================================================
# NO-MONGOD ARM (R-56): the live view is fully functional without a database
# =========================================================================


def test_no_mongod_the_whole_read_api_answers():
    print("test_no_mongod_the_whole_read_api_answers")
    with Corpus("nomongo") as corpus:
        _mirror, backend, report, obs = ingest_corpus()
        check(report["rejected"] == 0 and report["unmapped"] == 0,
              f"the whole corpus maps with nothing rejected: {report['unmappedKinds']}")
        model, _api, get = api_for(backend, corpus)

        status, health = get("/health")
        check(status == 200, f"/health answers with no mirror configured (got {status})")
        check(health["mirror"]["state"] == "absent",
              f"…reporting mirror:'absent', got {health['mirror']['state']!r}")
        check(health["ok"] is True and health["mirror"]["lastError"] is None,
              "…and 'absent' is a state, not an error (GD-22)")

        status, sessions = get("/api/sessions")
        check(status == 200 and sessions["count"] >= SESSION_ROWS,
              f"the sidebar has rows: {sessions['count']} sessions")
        kinds = {row["kind"] for row in sessions["sessions"]}
        if LIVE_REGISTRY:
            check(kinds == {"live", "historical"},
                  f"…both arms of R-46's tagged union are listed, got {sorted(kinds)}")
        else:
            check(kinds == {"historical"},
                  f"…R-46's historical arm is listed, got {sorted(kinds)}")
            skip("R-46's `live:` arm: the frozen registry entry's process is not "
                 "running on this machine, so no live session exists to list")

        status, graph = get("/api/run/graph", run=RUN_829)
        check(status == 200 and len(graph["nodes"]) == 7,
              f"the loop card for {RUN_829} has its 7 nodes (got "
              f"{len(graph.get('nodes', []))})")
        check(all(node.get("derived") for node in graph["nodes"]),
              "…every node carries the reducer's verdict, so the page derives nothing")

        agent_rows = [node for node in graph["nodes"] if node["observed"].get("agentId")]
        check(len(agent_rows) == 7, f"…and every node names its agent ({len(agent_rows)})")

        # Token counters: the mirrored `usage` documents and the in-memory twin
        # must be the same number computed the same way, database or not.
        usage = [item for kind, item in obs if kind == "usage"]
        rollup = ingest_mod.rollup(usage, by="agentId")
        mirrored = {}
        for doc in backend.state["usage"].values():
            bucket = mirrored.setdefault(doc.get("agentId"), dict.fromkeys(
                ingest_mod.USAGE_FIELDS, 0))
            for field in ingest_mod.USAGE_FIELDS:
                bucket[field] += doc.get(field, 0)
        check(rollup and rollup == mirrored,
              "token counters agree between the memory twin and the mirrored "
              "documents with no mongod in the picture (GD-22)")
        check(len(model.sizes()) >= 6,
              f"…over a state with every collection populated: {model.sizes()}")


def test_no_mongod_rows_still_update_on_an_incremental_tick():
    print("test_no_mongod_rows_still_update_on_an_incremental_tick")
    with Corpus("nomongo-tick") as corpus:
        mirror, backend, _, _obs = ingest_corpus()
        before = {name: len(bucket) for name, bucket in backend.state.items()}

        moment = datetime.datetime(2026, 7, 26, 12, 0, 0, tzinfo=datetime.timezone.utc)
        added = append_live_agent(corpus, moment)
        enqueue_paths(mirror, added)

        after = {name: len(bucket) for name, bucket in backend.state.items()}
        for collection in ("sessions", "runs", "run_nodes", "agents", "usage", "records"):
            check(after.get(collection, 0) > before.get(collection, 0),
                  f"`{collection}` grew on the incremental tick "
                  f"({before.get(collection, 0)} -> {after.get(collection, 0)})")

        _model, _api, get = api_for(backend, corpus)
        status, graph = get("/api/run/graph", run=added["run_id"])
        check(status == 200 and graph["derived"]["nodeCount"] == 1,
              "…and the new loop card is servable immediately, with no database")


def append_live_agent(corpus, moment):
    """Write one live spawn into the corpus: transcript + journal + agent file.

    The agent's transcript goes under `<session>/subagents/`, NOT under the run
    directory — the Task-tool spawn shape the frozen corpus carries two `.meta`
    files for. That placement is what leaves the run without an `endedAt`
    (`ingest._run_observation` takes the run's end from its nodes' last
    transcript activity), i.e. it is what makes this a *live* run rather than a
    closed one. See :func:`test_phase3_three_state_liveness`.
    """
    session_id = "0b6c1c2a-0000-4000-8000-00000000aaaa"
    run_id = "wf_5ea70b12-c1e"
    agent_id = "a" + "9" * 16
    stamp = isoformat(moment)
    owned = corpus.owned

    transcript = os.path.join(owned, f"{session_id}.jsonl")
    with open(transcript, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "assistant", "uuid": "11111111-0000-4000-8000-000000000001",
            "sessionId": session_id, "timestamp": stamp,
            "message": {"id": "msg_live01", "role": "assistant",
                        "usage": {"input_tokens": 10, "output_tokens": 20,
                                  "cache_read_input_tokens": 5,
                                  "cache_creation_input_tokens": 1}}}) + "\n")

    journal_dir = os.path.join(owned, session_id, "subagents", "workflows", run_id)
    os.makedirs(journal_dir, exist_ok=True)
    journal = os.path.join(journal_dir, "journal.jsonl")
    with open(journal, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "started", "agentId": agent_id,
                                 "key": "implement"}) + "\n")

    subagents = os.path.join(owned, session_id, "subagents")
    os.makedirs(subagents, exist_ok=True)
    agent_file = os.path.join(subagents, f"agent-{agent_id}.jsonl")
    with open(agent_file, "w", encoding="utf-8") as handle:
        handle.write("\n".join([
            json.dumps({
                "type": "user", "uuid": "22222222-0000-4000-8000-000000000001",
                "sessionId": session_id, "agentId": agent_id, "timestamp": stamp,
                "message": {"role": "user", "content": [{
                    "type": "text",
                    "text": "[monitor] plan=sp-live stage=implement role=impl "
                            "attempt=1\nwork"}]}}),
            json.dumps({
                "type": "assistant", "uuid": "22222222-0000-4000-8000-000000000002",
                "sessionId": session_id, "agentId": agent_id, "timestamp": stamp,
                "parentUuid": "22222222-0000-4000-8000-000000000001",
                "message": {"id": "msg_live02", "role": "assistant",
                            "usage": {"input_tokens": 1, "output_tokens": 2,
                                      "cache_read_input_tokens": 0,
                                      "cache_creation_input_tokens": 0}}}),
        ]) + "\n")
    sess.reset_scope_cache()
    return {"session_id": session_id, "run_id": run_id, "agent_id": agent_id,
            "paths": (transcript, journal, agent_file), "ts": moment}


def enqueue_paths(mirror, added):
    """Feed just the changed files through the per-path (`--backfill`) seam."""
    count = 0
    for kind, source in mr.iter_sources():
        for path in added["paths"]:
            for observation in source(path) or ():
                count += mirror.map_and_enqueue(kind, observation)
    run(mirror.flush())
    return count


def test_a_bare_checkout_reduces_to_the_same_state():
    print("test_a_bare_checkout_reduces_to_the_same_state")
    with Corpus("bare") as corpus:
        _mirror, backend, _, _obs = ingest_corpus()
        here = ms.fingerprint(observation_state(backend.state))
        counts = counts_of(backend)

        result = subprocess.run(
            [sys.executable, "-c", _BARE_CHECKOUT_CHILD],
            cwd=str(SRC), env={**os.environ, **corpus.env,
                                "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            check(False, f"the pymongo-free child failed: {result.stderr.strip()[-800:]}")
            return
        child = json.loads(result.stdout.strip().splitlines()[-1])

        check(child["pymongo_available"] is False,
              "the child interpreter really has no pymongo (the blocker works)")
        check(child["imported"] == child["modules"],
              f"every aggregator module imports without it: missing "
              f"{sorted(set(child['modules']) - set(child['imported']))}")
        check(not child["third_party"],
              f"…and nothing third-party reached sys.modules: {child['third_party']}")
        check(child["mirror_state"] == "absent",
              f"a mirror with no driver is 'absent', not an exception "
              f"(got {child['mirror_state']!r})")
        check(child["health_mirror"] == "absent",
              f"/health says mirror:'absent' on a bare checkout "
              f"(got {child['health_mirror']!r})")
        check(child["fingerprint"] == here,
              "the corpus reduces to a BYTE-IDENTICAL state with the driver "
              "blocked — which is what 'green on a bare checkout' means (GD-21)")
        check(child["counts"] == counts,
              f"…with identical collection counts: {child['counts']} == {counts}")
        # The child reads the same corpus in the same process tree, so it sees
        # the same registry liveness this parent measured (SESSION_ROWS).
        check(child["sessions"] >= SESSION_ROWS and child["nodes_829"] == 7,
              f"…and the API still answers there: {child['sessions']} sessions, "
              f"{child['nodes_829']} nodes on {RUN_829}")


#: Runs in a child interpreter with `pymongo`/`bson`/`dns` made unimportable at
#: the meta-path, which is stronger than "not installed": it also fails an
#: import that a `sys.modules` entry would otherwise have satisfied.
_BARE_CHECKOUT_CHILD = r'''
import json, sys, asyncio, os

BLOCKED = {"pymongo", "bson", "dns", "dnspython", "gridfs"}


class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError("blocked by the bare-checkout arm", name=name)
        return None


sys.meta_path.insert(0, Blocker())
sys.path.insert(0, os.getcwd())

MODULES = ["tailer", "store", "ws", "refs", "mongo_store", "mirror", "sessions",
           "ingest", "legacy", "agents", "custom_state", "server"]
imported = []
for name in MODULES:
    __import__("aggregator." + name)
    imported.append(name)

from aggregator import mirror as mr
from aggregator import mongo_store as ms
from aggregator import server as server_mod

obs = list(mr.iter_rebuild_observations())
backend = mr.MemoryBackend({})
mirror = mr.Mirror(mr.MongoConfig("uri-placeholder", "touch_test"),
                   backend=backend, registry=mr.discover_mappers())
mirror.state = mr.STATE_LIVE
asyncio.run(mirror.rebuild(obs))

driverless = mr.Mirror(mr.MongoConfig("mongodb" + "://127.0.0.1:27017/x", "touch_test"))
mirror_state = asyncio.run(driverless.start())

model = server_mod.ReadModel(state=backend.state)
auth = server_mod.Auth()
api = server_mod.Api(model, auth=auth)
headers = {"x-touch-token": auth.token}
health = json.loads(api.handle("GET", "/health", {}, headers).body.decode())
sessions = json.loads(api.handle("GET", "/api/sessions", {}, headers).body.decode())
graph = json.loads(api.handle("GET", "/api/run/graph",
                              {"run": ["wf_829e6f58-b2f"]}, headers).body.decode())

state = {n: b for n, b in backend.state.items() if n != "writers"}
counts = {n: len(b) for n, b in state.items()}
third = sorted(n for n in sys.modules if n.split(".")[0] in BLOCKED)
print(json.dumps({
    "pymongo_available": ms.pymongo_available(),
    "modules": MODULES, "imported": imported, "third_party": third,
    "mirror_state": mirror_state,
    "health_mirror": health["mirror"]["state"],
    "fingerprint": ms.fingerprint(state),
    "counts": counts,
    "sessions": sessions["count"],
    "nodes_829": len(graph.get("nodes") or []),
}))
'''


def test_a_dead_mongod_is_reported_down_and_changes_no_answer():
    print("test_a_dead_mongod_is_reported_down_and_changes_no_answer")
    if not ms.pymongo_available():
        skip("the dead-mongod arm needs a driver to time out against (GD-21)")
        return
    with Corpus("deadmongo") as corpus:
        _mirror, backend, _, _obs = ingest_corpus()
        reference = ms.fingerprint(observation_state(backend.state))

        # Port 1 is privileged and closed: refused, no listener, no DNS. One
        # event loop for the whole scenario — `AsyncMongoClient` binds to the
        # loop it was made on.
        dead = mr.Mirror(mr.MongoConfig("mongodb" + "://127.0.0.1:1/x", "touch_test"),
                         registry=mr.discover_mappers())

        async def scenario():
            started = time.monotonic()
            state = await dead.start()
            connect = time.monotonic() - started
            durations = []
            for _ in range(mr.BREAKER_FAILURES + 3):
                dead.enqueue([mr.MirrorOp("records", refs.record_key(
                    "00000000-0000-4000-8000-000000000001"),
                    ms.op_set({"sessionId": DD, "type": "assistant",
                               "provenance": "harness"}))])
                tick = time.monotonic()
                await dead.tick()
                durations.append(time.monotonic() - tick)
            return state, connect, durations

        state, connect, durations = run(scenario())
        check(state == mr.STATE_DOWN, f"a dead mongod leaves the mirror down: {state!r}")
        check(connect < STALL_SECONDS / 3,
              f"connect cost {connect:.2f}s — MONGOSCHEMA-4's 30.1 s stall is gone")
        check(max(durations) < STALL_SECONDS / 3,
              f"no tick came near the stall (worst {max(durations):.2f}s)")
        steady = durations[mr.BREAKER_FAILURES:]
        check(steady and max(steady) < mr.TICK_INTERVAL_S,
              f"once the breaker holds a dead server costs a tick nothing: "
              f"{[f'{d:.3f}' for d in steady]} < {mr.TICK_INTERVAL_S}s")

        _model, _api, get = api_for(backend, corpus, mirror=dead)
        status, health = get("/health")
        check(status == 200 and health["mirror"]["state"] == "down",
              f"/health reports mirror:'down', got {health['mirror']['state']!r}")
        status, sessions = get("/api/sessions")
        check(status == 200 and sessions["count"] >= SESSION_ROWS,
              "…while the sidebar keeps answering (the database is never on the "
              "liveness path — GD-22)")
        status, graph = get("/api/run/graph", run=RUN_829)
        check(status == 200 and len(graph["nodes"]) == 7,
              "…and so does the loop card")
        check(ms.fingerprint(observation_state(backend.state)) == reference,
              "…and a mongod that never answered changed no observation")
        check(dead.stats["dropped"] == 0,
              "nothing was dropped while it was down — the queue is the buffer")


# =========================================================================
# MIRROR ARM (R-56): fingerprints over the real corpus, and the two topologies
# =========================================================================


def test_double_ingest_of_the_whole_corpus_changes_nothing():
    print("test_double_ingest_of_the_whole_corpus_changes_nothing")
    with Corpus("double"):
        mirror, backend, _, obs = ingest_corpus()
        first = ms.fingerprint(observation_state(backend.state))
        counts = counts_of(backend)

        # GD-25's claim is about the WHOLE pipeline, so the second pass re-reads
        # the files rather than replaying the first pass's observation objects: a
        # source that stamped `now()` anywhere would diverge here and nowhere else.
        run(mirror.rebuild(observations()))
        check(ms.fingerprint(observation_state(backend.state)) == first,
              "re-ingesting the whole corpus is byte-identical (GD-25)")
        check(counts_of(backend) == counts,
              f"…and creates no documents: {counts_of(backend)} == {counts}")

        # Order independence: the requeue-on-outage path reorders writes, so a
        # reversed replay landing on a different fingerprint would make every
        # recovery a silent corruption.
        other_mirror, other_backend = new_mirror()
        run(other_mirror.rebuild(list(reversed(obs))))
        check(ms.fingerprint(observation_state(other_backend.state)) == first,
              "…and a reversed replay of the same corpus agrees, op for op")


def test_wipe_and_rebuild_reproduce_the_corpus():
    print("test_wipe_and_rebuild_reproduce_the_corpus")
    with Corpus("rebuild"):
        mirror, backend, _, obs = ingest_corpus()
        before = ms.fingerprint(observation_state(backend.state))
        counts = counts_of(backend)

        # A stale reduction, left behind by a previous `reducerVersion`. It has
        # to survive the wipe so the rebuild has something to drop — GD-23 says
        # `derived` is dropped and replayed, never migrated, and a rebuild onto
        # an empty store cannot demonstrate that.
        stale = {"_id": "d1", "reducerVersion": "0", "derivedFromSeq": 5,
                 "provenance": "derived"}

        # The wipe: every observation Mongo holds, gone, from THE SAME store —
        # R-45's clause. Two backends would only re-test mapper determinism.
        for collection in [name for name in backend.state if name != "writers"]:
            backend.state.pop(collection)
        backend.state["derived"] = {"d1": stale}
        check(counts_of(backend) == {"derived": 1},
              f"…leaving only the stale reduction behind: {counts_of(backend)}")

        report = run(mirror.rebuild(observations()))
        check(ms.fingerprint(observation_state(backend.state)) == before,
              "wipe + --rebuild reproduces the corpus byte for byte (GD-22)")
        check(counts_of(backend) == counts,
              f"…with identical counts: {counts_of(backend)} == {counts}")
        check(report["droppedDerived"] is True and report["rejected"] == 0,
              "…having dropped `derived` rather than migrating it (GD-23)")
        check(not backend.state.get("derived"),
              f"…so the stale reduction is gone, not carried under a new version: "
              f"{backend.state.get('derived')}")
        check(report["replayed"] > 2000,
              f"…and it really replayed the corpus ({report['replayed']} ops)")


def test_the_killed_run_keeps_its_retry_topology_through_the_api():
    print("test_the_killed_run_keeps_its_retry_topology_through_the_api")
    with Corpus("retry") as corpus:
        _mirror, backend, _, _obs = ingest_corpus()
        _model, _api, get = api_for(backend, corpus)
        status, graph = get("/api/run/graph", run=RUN_455)
        check(status == 200, f"the killed run is servable (got {status})")

        nodes = [node["observed"] for node in graph["nodes"]]
        check(len(nodes) == 9, f"9 `started` records become 9 nodes, got {len(nodes)}")
        by_key = {}
        for node in nodes:
            by_key.setdefault(node["key"], []).append(node["ordinal"])
        retried = {key: sorted(ordinals) for key, ordinals in by_key.items()
                   if len(ordinals) > 1}
        check(len(retried) == 3,
              f"…3 keys occur twice — the retry (SESSIONJSONL-4), got {sorted(retried)}")
        check(all(ordinals == [0, 1] for ordinals in retried.values()),
              f"…distinguished by an `ordinal` of 0 then 1, got {retried}")
        seqs = [node["journalSeq"] for node in nodes]
        check(seqs == sorted(seqs) and len(set(seqs)) == 9,
              "…derived from journal line order, not from an in-memory counter")

        resulted = [node for node in nodes if node["resultSeen"]]
        check(len(resulted) == 2,
              f"only 2 of the 9 ever resulted — the user killed it ({len(resulted)})")
        check(graph["observed"]["status"] == "killed",
              f"…and the snapshot says so: {graph['observed'].get('status')!r}")
        check(graph["observed"]["harnessTotals"]["nodeCount"] == 6,
              "…while `agentCount` is republished as `nodeCount`, because 6 over "
              "9 rows is a node count and never an agent count (SESSIONJSONL-7)")
        check(graph["derived"]["nodes"]["unknown"] == 7
              and graph["derived"]["nodes"]["done"] == 2,
              f"…so the reducer leaves 7 rows `unknown`, never `failed`: "
              f"{graph['derived']['nodes']}")
        check(graph["derived"]["state"] == "done"
              and graph["derived"]["label"] == agents_mod.CLOSED_NO_VERDICT,
              f"…and the run itself closes '{agents_mod.CLOSED_NO_VERDICT}' "
              f"(GD-10), got {graph['derived']['label']!r}")


def test_the_cross_session_agent_unions_through_the_api():
    print("test_the_cross_session_agent_unions_through_the_api")
    with Corpus("union") as corpus:
        _mirror, backend, _, obs = ingest_corpus()
        _model, _api, get = api_for(backend, corpus)

        status, payload = get("/api/run/node", agent=CROSS_AGENT)
        if status != 200:
            status, payload = get("/api/run/node", run=RUN_829, agent=CROSS_AGENT)
        check(status == 200, f"the cross-session agent is servable (got {status}): "
                             f"{str(payload)[:200]}")
        agent = payload.get("agent") or {}
        observed = agent.get("observed") or {}
        check(sorted(observed.get("sessions") or []) == sorted((DD, E4)),
              f"`sessions[]` unions BOTH session ids (R-46/$addToSet), got "
              f"{observed.get('sessions')}")
        fragments = agent.get("fragments") or []
        check(len(fragments) == 2,
              f"…and both disjoint continuations are kept as fragments, got "
              f"{len(fragments)}")
        check(len({fragment.get("path") for fragment in fragments}) == 2,
              "…from two different files, which is what 'disjoint continuations' means")

        # The under-report R-03's amended wording exists to prevent: a rollup
        # over ONE file's records is smaller than the union's.
        usage = [item for kind, item in obs
                 if kind == "usage" and item.agent_id == CROSS_AGENT]
        by_session = {}
        for obs in usage:
            by_session.setdefault(obs.session_id, []).append(obs)
        check(len(by_session) == 2,
              f"the agent's token records really span two sessions: "
              f"{sorted(by_session)}")
        union = ingest_mod.rollup(usage, by="agentId")[CROSS_AGENT]
        biggest = max(sum(ingest_mod.rollup(part, by="agentId")[CROSS_AGENT][field]
                          for field in ingest_mod.USAGE_FIELDS)
                      for part in by_session.values())
        total = sum(union[field] for field in ingest_mod.USAGE_FIELDS)
        check(total > biggest,
              f"…so a per-file rollup under-reports ({biggest} < {total}) and only "
              f"the union is right (MONGOSCHEMA-9)")

        naive = sum(obs.tokens.get(field, 0) for obs in usage
                    for field in ingest_mod.USAGE_FIELDS)
        check(naive > total,
              f"…while summing split records over-counts ({naive} > {total}); the "
              f"`$max`-per-message dedup is the difference (R-50)")


def test_live_mongod_arm():
    print("test_live_mongod_arm")
    uri, name = live_database()
    if uri is None:
        skip(f"live mirror arm: {name}")
        return
    check(name.startswith("touch_test_"),
          f"the live arm uses a name it constructed: {name}")
    with Corpus("live"):
        obs = observations()
        memory_mirror, memory_backend = new_mirror()
        run(memory_mirror.rebuild(obs))
        expected = ms.fingerprint(replayable(memory_backend.state))
        try:
            run(_live_checks(uri, name, obs, expected))
        finally:
            client = ms.open_client(uri)
            check(name.startswith("touch_test_"),
                  f"dropping only the database this test constructed: {name}")
            client.drop_database(name)
            client.close()


def live_database():
    """(uri, name) against `TOUCH_MONGO_URI`, or (None, reason).

    Byte-for-byte the convention `test_mirror.py` and `test_mongo_store.py`
    established: R-42's loopback+auth recipe supplies the URI (`docs/mongo.md`
    §1), the database is `touch_test_<pid>`, and only that name is ever dropped.
    """
    uri = os.environ.get("TOUCH_MONGO_URI")
    if not uri:
        return None, "TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)"
    if not ms.pymongo_available():
        return None, "pymongo is not installed (GD-21: absence is legal)"
    try:
        client = ms.open_client(uri)
    except ms.MongoUnavailable as exc:
        return None, str(exc)
    reachable = ms.ping(client)
    client.close()
    if not reachable:
        return None, "no mongod answered within the GD-21 timeouts"
    return uri, f"touch_test_{os.getpid()}"


def replayable(state):
    """Observation collections only: `derived` is dropped by definition."""
    return {name: bucket for name, bucket in observation_state(state).items()
            if name != "derived"}


async def _live_checks(uri, name, obs, expected):
    from aggregator.mirror import AsyncBackend

    backend = await AsyncBackend.connect(uri, name)
    mirror = mr.Mirror(mr.MongoConfig(uri, name), backend=backend,
                       registry=mr.discover_mappers())
    state = await mirror.start()
    check(state == mr.STATE_LIVE,
          f"the mirror reaches 'live' against a real mongod, got {state!r}")

    report = await mirror.rebuild(obs)
    check(report["rejected"] == 0,
          f"the whole real corpus lands on a real server: {report['unmappedKinds']}")
    stored = ms.fingerprint(replayable(await backend._read_state()))
    check(stored == expected,
          "…producing the SAME fingerprint as MemoryBackend, which is what makes "
          "the bare-checkout arms meaningful (GD-25)")
    counts = await backend.counts()

    # GD-25 against a REAL server: replaying the corpus onto its own output.
    await mirror.rebuild(obs)
    check(ms.fingerprint(replayable(await backend._read_state())) == expected,
          "double-ingest against a real mongod changes nothing (GD-25)")
    after = await backend.counts()
    check({k: v for k, v in after.items() if k != "derived"}
          == {k: v for k, v in counts.items() if k != "derived"},
          f"…and creates no documents: {after} == {counts}")

    # R-45's wipe + `--rebuild` clause, server-side. The wipe goes through the
    # *client*, not the backend: `Backend.drop_collection` refuses every name but
    # `derived` (GD-23/GD-26), which is the invariant `test_mirror.py` asserts —
    # so an operator's "wipe it and rebuild" is a `dropDatabase`, and rebuilding
    # after one means a fresh connection, a fresh schema bootstrap and a fresh
    # writer lease, exactly as it would after `docker rm`.
    await backend.client.drop_database(name)
    check(not await backend.counts(), "the real database really is empty")
    fresh_backend = await AsyncBackend.connect(uri, name)
    fresh = mr.Mirror(mr.MongoConfig(uri, name), backend=fresh_backend,
                      registry=mr.discover_mappers())
    check(await fresh.start() == mr.STATE_LIVE,
          "…a new writer takes the lease the wipe removed (GD-29)")
    report = await fresh.rebuild(obs)
    check(report["rejected"] == 0, f"…and replays: {report['unmappedKinds']}")
    check(ms.fingerprint(replayable(await fresh_backend._read_state())) == expected,
          "wipe + --rebuild reproduces the corpus against a real mongod (GD-22)")
    await fresh_backend.close()
    await backend.close()


# =========================================================================
# BUDGET ARM (R-56): O(delta) reads, and a dead database that costs nothing
# =========================================================================


def test_a_tick_reads_the_delta_not_the_stream():
    print("test_a_tick_reads_the_delta_not_the_stream")
    directory = tmpdir("budget")
    path = os.path.join(directory, "big.jsonl")

    line = json.dumps({"type": "assistant", "text": "x" * 900}) + "\n"
    written = 0
    with open(path, "w", encoding="utf-8") as handle:
        while written < BIG_STREAM_BYTES:
            handle.write(line)
            written += len(line)
    size = os.path.getsize(path)
    check(size >= BIG_STREAM_BYTES,
          f"the stream is the 20 MB LIVEFLOW-15 measures against ({size} B)")

    tailer = tailer_mod.Tailer(path)
    lines = tailer.drain(max_ticks=1000)
    check(len(lines) * len(line) == size,
          f"the first pass read every line ({len(lines)} lines)")
    check(tailer.bytes_read >= size,
          f"…and paid for the whole file once: {tailer.bytes_read} B")

    baseline = tailer.bytes_read
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("y" * (APPEND_BYTES - 1) + "\n")
    result = tailer.poll()
    delta = tailer.bytes_read - baseline
    check(result.reason == tailer_mod.REASON_APPEND and len(result.lines) == 1,
          f"the appended line is seen as an append ({result.reason})")
    check(result.bytes_read < TICK_READ_BUDGET,
          f"…and the tick read {result.bytes_read} B < {TICK_READ_BUDGET} B for a "
          f"{APPEND_BYTES} B append on a {size} B stream (LIVEFLOW-15: O(delta))")
    check(delta == result.bytes_read,
          f"…which is the number `/health` publishes, not a second one "
          f"({delta} vs {result.bytes_read})")

    idle = tailer.poll()
    check(idle.reason == tailer_mod.REASON_UNCHANGED and idle.bytes_read == 0,
          f"…and an idle tick opens nothing at all ({idle.reason}, "
          f"{idle.bytes_read} B) — the stat-first short circuit")


def test_a_dead_database_never_slows_the_ingest_loop():
    print("test_a_dead_database_never_slows_the_ingest_loop")
    if not ms.pymongo_available():
        skip("the dead-database budget arm needs a driver to time out (GD-21)")
        return
    with Corpus("budget-mongo"):
        mirror = mr.Mirror(mr.MongoConfig("mongodb" + "://127.0.0.1:1/x", "touch_test"),
                           registry=mr.discover_mappers())
        run(mirror.start())
        check(mirror.state == mr.STATE_DOWN, f"the mirror is down: {mirror.state!r}")

        # `enqueue` is the ONLY thing the 250 ms poll loop calls (GD-30), so the
        # measurement that matters is the ingest pass' own wall clock with the
        # mirror attached to a database that will never answer.
        obs = observations()
        started = time.monotonic()
        accepted = 0
        for kind, observation in obs:
            accepted += mirror.map_and_enqueue(kind, observation)
        elapsed = time.monotonic() - started
        check(accepted > 2000, f"the whole corpus was mapped and offered ({accepted} ops)")
        check(elapsed < STALL_SECONDS / 3,
              f"…in {elapsed:.2f}s against a dead mongod — nowhere near "
              f"MONGOSCHEMA-4's 30.1 s (GD-30: 0 ms on the critical path)")
        per_op = elapsed / max(1, accepted)
        check(per_op < 0.001,
              f"…{per_op * 1e6:.1f} us per operation, mapping included, so a 250 ms "
              f"tick is never the database's to spend")

        health = mirror.health()
        check(health["state"] in (mr.STATE_DOWN, mr.STATE_DEGRADED),
              f"/health names the database as the fault: {health['state']!r}")
        check(health["queued"] == accepted,
              f"…every accepted operation is buffered for recovery: "
              f"{health['queued']} == {accepted}")
        # The corpus is larger than the queue, so this also exercises GD-30's
        # other half: the overflow is dropped and COUNTED, never awaited. A
        # mirror that back-pressured the poll loop to keep its history would
        # have stalled the live view over an optional database.
        check(health["dropped"] > 0 and health["queued"] == mirror.queue.maxsize,
              f"…and the overflow of a full queue is dropped, not awaited: "
              f"{health['dropped']} dropped, queue {health['queued']}/"
              f"{mirror.queue.maxsize}")


# =========================================================================
# PHASE 1 (R-37): the watcher, end to end, and its output read back
# =========================================================================


def test_phase1_the_real_watcher_emits_no_failed_verdict():
    print("test_phase1_the_real_watcher_emits_no_failed_verdict")
    wf_dir = RUN_FIX / DD / "subagents" / "workflows" / RUN_829
    if not (wf_dir / "journal.jsonl").is_file():
        skip(f"phase-1 arm: fixture missing ({wf_dir})")
        return
    if not WATCHER.is_file():
        skip(f"phase-1 arm: watcher missing ({WATCHER})")
        return

    directory = tmpdir("watcher")
    state_dir = os.path.join(directory, "state")
    os.makedirs(state_dir, exist_ok=True)
    env = dict(os.environ)
    env.update({"ORCH_STATE_DIR": state_dir, "ORCH_WF_DIR": os.fspath(wf_dir),
                "ORCH_WF_GLOB_ROOT": os.fspath(FIX), "ORCH_QUIET_SECS": "1",
                "ORCH_EXIT_QUIET_SECS": "1", "ORCH_ABANDON_QUIET_SECS": "3"})
    try:
        proc = subprocess.run([sys.executable, os.fspath(WATCHER)], env=env,
                              capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        check(False, "the watcher did not exit inside 180 s (R-40 self-exit)")
        return
    check(proc.returncode == 0,
          f"the watcher exits cleanly on an abandoned run (rc={proc.returncode}): "
          f"{proc.stderr.strip()[-400:]}")

    events_path = os.path.join(state_dir, "events.jsonl")
    if not os.path.isfile(events_path):
        check(False, "the watcher wrote no events.jsonl at all")
        return
    events = [json.loads(line) for line in open(events_path, encoding="utf-8")
              if line.strip()]
    check(len(events) > 20, f"…having emitted a real stream ({len(events)} events)")

    badges = [event for event in events if event.get("stage") == "plan"]
    check([event for event in badges if event.get("state") == "failed"] == [],
          "ZERO failed plan badges on the real research run (R-08/R-58)")
    check(not [event for event in events
               if "loop exited ->" in (event.get("detail") or "")],
          "…and nothing carries the fabricated 'loop exited ->' detail")
    research = [event for event in badges
                if event.get("plan") == "research" and event.get("state") == "done"]
    check(bool(research), "the research fan-out closes `done`")
    check(any("closed, no verdict" in (event.get("detail") or "")
              for event in research),
          "…labelled 'closed, no verdict' rather than decided (GD-10)")
    check(any(event.get("stage") == "complete" and event.get("state") == "done"
              for event in events),
          "…and the run emits the terminal `orchestrator complete done` (R-09)")

    # The join nothing else makes: the watcher's OWN stream, read back through
    # Touch's legacy adapter. The forward fix (sp-03) and the read-time rules
    # (sp-09) must agree about the same bytes, or the page disagrees with the
    # daemon that produced them.
    _read_watcher_stream_back(directory, events_path)


def _read_watcher_stream_back(directory, events_path):
    legacy_root = os.path.join(directory, "orchestrators")
    folder = os.path.join(legacy_root, "wf-829-replay")
    os.makedirs(folder, exist_ok=True)
    shutil.copyfile(events_path, os.path.join(folder, "events.jsonl"))

    tasks = legacy_mod.scan(legacy_root)
    check(len(tasks) == 1, f"the replayed stream is one task folder ({len(tasks)})")
    if not tasks:
        return
    reduction = tasks[0]
    states = {name: plan.badge for name, plan in reduction.plans.items()}
    check("failed" not in set(states.values()),
          f"…and Touch's own reader finds no failed plan either: {states}")
    check(states.get("research") == "done",
          f"…with `research` rendered done, got {states.get('research')!r}")

    model = server_mod.ReadModel(state={}, tasks_root=legacy_root)
    auth = server_mod.Auth()
    api = server_mod.Api(model, auth=auth)
    response = api.handle("GET", "/api/tasks", {}, {"x-touch-token": auth.token})
    payload = json.loads(response.body.decode("utf-8"))
    check(response.status == 200 and payload["tasks"],
          "…and `/api/tasks` serves it")
    plans = payload["tasks"][0]["plans"]
    check(plans.get("research", {}).get("badge") == "done",
          f"…as `done` on the wire too: {plans.get('research', {}).get('badge')!r}")
    check(all(row["state"] != "failed" for row in payload["tasks"][0]["nodes"]),
          "…with no agent row failed anywhere in the rendered task")


def test_phase1_the_broken_watchers_own_stream_relabels():
    print("test_phase1_the_broken_watchers_own_stream_relabels")
    with Corpus("relabel") as corpus:
        tasks = {reduction.task: reduction for reduction in
                 legacy_mod.scan(corpus.legacy_root)}
        check("touch-aggregator" in tasks,
              f"the frozen streams are discovered: {sorted(tasks)}")
        if "touch-aggregator" not in tasks:
            return

        # `touch-aggregator-events.jsonl` is the historic output of the BROKEN
        # watcher on the very run the arm above replays through the fixed one.
        # Line 571 is `research plan failed "loop exited -> synthesis"`.
        raw = (LEGACY_FIX / "touch-aggregator-events.jsonl").read_text(encoding="utf-8")
        check('"state": "failed"' in raw and "loop exited ->" in raw,
              "the fabricated failure really is in the frozen bytes (control)")

        research = tasks["touch-aggregator"].plans.get("research")
        check(research is not None and research.badge == legacy_mod.CLOSED_STATE,
              f"…and the read-time rule renders it `closed`, not failed: "
              f"{getattr(research, 'badge', None)!r}")
        check(research is not None and research.relabel == legacy_mod.CLOSED_NO_VERDICT,
              f"…re-labelled '{legacy_mod.CLOSED_NO_VERDICT}' (SD-4), got "
              f"{getattr(research, 'relabel', None)!r}")
        check(research is not None and research.derived_from_legacy is True,
              "…and the derivation travels with the row (`derivedFromLegacy`), so "
              "the page renders WHY (D13)")

        # SD-4's other two arms, on the streams that carry a correction.
        for task, plan in (("touch-full-recon", "research"),
                           ("touch-mongo-live", "research")):
            row = tasks.get(task).plans.get(plan) if task in tasks else None
            check(row is not None and row.badge == "done",
                  f"{task}: the corrective `done` beats the earlier fabricated "
                  f"`failed` in FILE order, got {getattr(row, 'badge', None)!r}")

        # The negative control that keeps every assertion above non-vacuous: the
        # run the user actually killed stays failed (PROVENANCE.md; amendment §2).
        killed = tasks.get("touch-repo-recon")
        check(killed is not None
              and killed.plans.get("research").badge == "failed",
              "…while the genuinely killed run STAYS failed — the re-label is a "
              "rule, not a blanket amnesty")


# =========================================================================
# PHASE 3 (R-37): the research run and the legacy task, rendered
# =========================================================================


def test_phase3_six_distinctly_labelled_researchers():
    print("test_phase3_six_distinctly_labelled_researchers")
    with Corpus("labels") as corpus:
        _mirror, backend, _, _obs = ingest_corpus()
        _model, _api, get = api_for(backend, corpus)
        status, graph = get("/api/run/graph", run=RUN_829)
        check(status == 200, f"the research run is servable (got {status})")

        labels = [node["derived"]["display"] for node in graph["nodes"]]
        researchers = [label for label in labels if label.startswith("research:")]
        check(len(researchers) == 6,
              f"six researcher nodes, got {len(researchers)}: {researchers}")
        check(len(set(researchers)) == 6,
              f"…and six DISTINCT labels, which is CONVO-10's whole complaint: "
              f"{sorted(researchers)}")
        check(sorted(researchers) == sorted([
            "research:agentgraph", "research:control", "research:liveio",
            "research:priorart", "research:sessiondata", "research:stack"]),
              f"…the six perspectives the run actually had: {sorted(researchers)}")
        check("synthesize" in labels,
              f"…plus the synthesizer, labelled by its own stage: {labels}")

        # The labels come from the marker layer over harness facts, never from
        # the id: five agents under one 8-hex label was the live specimen.
        ids = [node["observed"]["agentId"] for node in graph["nodes"]]
        check(len(set(ids)) == 7 and all(len(value) == 17 for value in ids),
              f"…over seven full 17-hex agentIds (GD-7/R-13): {sorted(set(ids))}")
        agents = backend.state["agents"]
        marked = [agents[value].get("labels", {}) for value in ids if value in agents]
        check(all(row.get("plan") for row in marked),
              "…and every one carries its `[monitor]` marker labels")
        check(sum(1 for row in marked if row.get("plan") == "research") == 6,
              "…six of them on plan `research`, one on `synthesis`")


def test_phase3_rollups_are_deduped_and_agree_everywhere():
    print("test_phase3_rollups_are_deduped_and_agree_everywhere")
    with Corpus("rollups"):
        _mirror, backend, _, obs = ingest_corpus()
        usage = [item for kind, item in obs if kind == "usage"]
        check(len(usage) > 1000, f"the corpus carries real token records ({len(usage)})")

        deduped = ingest_mod.dedup_usage(usage)
        check(len(deduped) == len(backend.state["usage"]),
              f"one mirrored document per `message.id`: {len(deduped)} == "
              f"{len(backend.state['usage'])}")
        check(len(deduped) < len(usage),
              f"…which is fewer than the records ({len(deduped)} < {len(usage)}), "
              f"because a message's records are split (R-50)")

        for key in ("agentId", "sessionId", "runId"):
            rollup = ingest_mod.rollup(usage, by=key)
            field = {"agentId": "agentId", "sessionId": "sessionId",
                     "runId": "runId"}[key]
            mirrored = {}
            for doc in backend.state["usage"].values():
                bucket = mirrored.setdefault(doc.get(field),
                                             dict.fromkeys(ingest_mod.USAGE_FIELDS, 0))
                for name in ingest_mod.USAGE_FIELDS:
                    bucket[name] += doc.get(name, 0)
            check(rollup == mirrored,
                  f"the `{key}` rollup agrees with the mirrored documents — the "
                  f"live view and the database compute the same number (GD-22)")

        naive = {}
        for obs in usage:
            bucket = naive.setdefault(obs.agent_id, 0)
            naive[obs.agent_id] = bucket + obs.tokens.get("out", 0)
        deduped_out = {agent: totals["out"]
                       for agent, totals in ingest_mod.rollup(usage).items()}
        over = [agent for agent, value in naive.items()
                if value > deduped_out.get(agent, 0)]
        check(over,
              f"…and summing split records really would over-count "
              f"({len(over)} agents), which is why nothing here uses `$inc`")


def test_phase3_three_state_liveness():
    print("test_phase3_three_state_liveness")
    with Corpus("liveness") as corpus:
        mirror, backend, _, _obs = ingest_corpus()
        moment = datetime.datetime(2026, 7, 26, 12, 0, 0, tzinfo=datetime.timezone.utc)
        added = append_live_agent(corpus, moment)
        enqueue_paths(mirror, added)

        fresh = agents_mod.reduce(backend.state,
                                  now=moment + datetime.timedelta(seconds=10))
        stale = agents_mod.reduce(backend.state,
                                  now=moment + datetime.timedelta(minutes=30))

        live_row = fresh.agents.get(added["agent_id"])
        check(live_row is not None and live_row["state"] == agents_mod.RUNNING,
              f"an agent active 10 s ago is `running`: "
              f"{None if live_row is None else live_row['state']!r}")
        idle_row = stale.agents.get(added["agent_id"])
        check(idle_row is not None and idle_row["state"] == agents_mod.UNKNOWN,
              f"…the SAME agent 30 minutes later is `unknown`, never `failed` and "
              f"never still `running` (R-54): "
              f"{None if idle_row is None else idle_row['state']!r}")
        check(idle_row is not None and str(agents_mod.IDLE_LIMIT_SECONDS)
              in (idle_row.get("reason") or ""),
              f"…with the 180 s rule named in the reason: "
              f"{None if idle_row is None else idle_row.get('reason')!r}")
        check(fresh.agents.get(CROSS_AGENT, {}).get("state") == agents_mod.DONE,
              "…while a resulted agent of the closed research run is `done`")

        states = {row["state"] for row in fresh.agents.values()}
        check(states == {agents_mod.RUNNING, agents_mod.DONE, agents_mod.UNKNOWN},
              f"all three states occur at ONE `now`, which is what makes it a "
              f"three-state model rather than a flag: {sorted(states)}")
        check(fresh.counters["agent_" + agents_mod.RUNNING] == 1,
              f"…exactly one of them running: "
              f"{fresh.counters['agent_' + agents_mod.RUNNING]}")

        # No `state` field is ever stored (GD-23): liveness is read-time only.
        stored = [key for key, doc in backend.state["agents"].items()
                  if "state" in doc]
        check(not stored, f"no agent document stores a state: {stored[:3]}")

        # The boundary this arm measures and does not own: the freeze fires on
        # `runs.endedAt`, and `ingest._run_observation` derives that from the
        # nodes' last *transcript* activity. A run whose agents write inside the
        # run directory therefore reads terminal even with no `<runId>.json` —
        # the shape `mirror/live-run-shape/` was frozen for (SESSIONJSONL-6).
        # That is why the live spawn above is placed under `subagents/`.
        b29 = backend.state["runs"].get(refs.run_key(RUN_B29))
        check(b29 is not None, f"the no-snapshot run is mirrored ({RUN_B29})")
        if b29 is not None:
            check(b29.get("status") is None,
                  "…with no harness `status`, because it has no terminal snapshot")
            if b29.get("endedAt") is not None:
                note(f"{RUN_B29} has no snapshot yet still carries an `endedAt` "
                     f"derived from transcript activity, so the reducer freezes "
                     f"its rows — owned by ingest._run_observation (R-49), not "
                     f"by this file")


def test_phase3_the_legacy_task_renders_stale_closed_agents():
    print("test_phase3_the_legacy_task_renders_stale_closed_agents")
    with Corpus("legacy-render") as corpus:
        _model, _api, get = api_for(mr.MemoryBackend({}), corpus)
        status, payload = get("/api/tasks")
        check(status == 200, f"/api/tasks answers (got {status})")
        tasks = {row["task"]: row for row in payload["tasks"]}
        check("touch-repo-recon" in tasks,
              f"the two-wave respawn task is listed: {sorted(tasks)}")
        if "touch-repo-recon" not in tasks:
            return

        task = tasks["touch-repo-recon"]
        states = {}
        for node in task["nodes"]:
            states[node["state"]] = states.get(node["state"], 0) + 1
        check(states.get("stale", 0) >= 1,
              f"…with agents closed `stale` — the run was killed and they never "
              f"resulted: {states}")
        check(states.get("superseded", 0) == 3,
              f"…and the second wave supersedes the first three (RUNSTATE-2, the "
              f"only two-wave sample in existence): {states}")
        check(not any(node["state"] == "running" for node in task["nodes"]),
              "…so nothing ticks as running hours later (GD-10's freeze)")
        check(set(states) <= set(legacy_mod.STATES) | set(legacy_mod.DERIVED_STATES),
              f"…using only the declared state vocabulary: {sorted(states)}")

        # "closed — no verdict" is a *rule* about verdict-less closes, and the
        # legacy renderer applies the same string the reducer does. It belongs to
        # the streams SD-4 names — never to this one, whose failures are real.
        check(task["plans"]["research"]["badge"] == "failed",
              "the killed run keeps its genuine failure (amendment §2 over R-37's "
              "base wording)")
        with_label = [name for row in payload["tasks"] for name, plan
                      in row["plans"].items()
                      if plan.get("relabel") == legacy_mod.CLOSED_NO_VERDICT]
        check(with_label,
              f"…while the legacy path does render '{legacy_mod.CLOSED_NO_VERDICT}' "
              f"where the rule applies: {sorted(set(with_label))}")
        check(legacy_mod.CLOSED_NO_VERDICT == agents_mod.CLOSED_NO_VERDICT,
              "…and it is ONE string shared by both reducers, not two spellings")


def test_the_foreign_slugs_are_never_ingested():
    print("test_the_foreign_slugs_are_never_ingested")
    with Corpus("foreign") as corpus:
        _mirror, backend, _, _obs = ingest_corpus()
        foreign_ids = set()
        for slug in FOREIGN_SLUGS:
            directory = os.path.join(corpus.root, "projects", slug)
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                if name.endswith(".jsonl"):
                    foreign_ids.add(name[: -len(".jsonl")])
        check(len(FOREIGN_SLUGS) == 3 and len(foreign_ids) == 4,
              f"the negative fixtures are present: {len(FOREIGN_SLUGS)} foreign "
              f"slugs, {len(foreign_ids)} transcripts (SESSIONJSONL-11)")

        seen = set()
        for doc in backend.state.get("sessions", {}).values():
            seen.update(doc.get("sessionIds") or [])
        check(not (seen & foreign_ids),
              f"no foreign-slug session is ingested (R-25 as amended): "
              f"{sorted(seen & foreign_ids)}")
        records = {doc.get("sessionId") for doc in backend.state["records"].values()}
        check(not (records & foreign_ids),
              f"…and none of their records either: {sorted(records & foreign_ids)}")


def test_this_file_is_part_of_the_suite():
    print("test_this_file_is_part_of_the_suite")
    runner = REPO / "tests" / "run_all.sh"
    check(runner.is_file(), "tests/run_all.sh exists")
    if not runner.is_file():
        return
    result = subprocess.run(["bash", os.fspath(runner), "--list"],
                            capture_output=True, text=True, timeout=60)
    listed = result.stdout.split()
    check("tests/test_e2e_sim.py" in listed,
          f"…and the acceptance file runs with the rest ({len(listed)} files)")
    check(os.access(__file__, os.X_OK),
          "…and this file is executable, like its siblings (D12/STACK-16)")


TESTS = [
    # no-mongod
    test_no_mongod_the_whole_read_api_answers,
    test_no_mongod_rows_still_update_on_an_incremental_tick,
    test_a_bare_checkout_reduces_to_the_same_state,
    test_a_dead_mongod_is_reported_down_and_changes_no_answer,
    # mirror
    test_double_ingest_of_the_whole_corpus_changes_nothing,
    test_wipe_and_rebuild_reproduce_the_corpus,
    test_the_killed_run_keeps_its_retry_topology_through_the_api,
    test_the_cross_session_agent_unions_through_the_api,
    test_live_mongod_arm,
    # budget
    test_a_tick_reads_the_delta_not_the_stream,
    test_a_dead_database_never_slows_the_ingest_loop,
    # phase 1
    test_phase1_the_real_watcher_emits_no_failed_verdict,
    test_phase1_the_broken_watchers_own_stream_relabels,
    # phase 3
    test_phase3_six_distinctly_labelled_researchers,
    test_phase3_rollups_are_deduped_and_agree_everywhere,
    test_phase3_three_state_liveness,
    test_phase3_the_legacy_task_renders_stale_closed_agents,
    test_the_foreign_slugs_are_never_ingested,
    test_this_file_is_part_of_the_suite,
]


def main():
    for test in TESTS:
        test()
    for path in TMPDIRS:
        shutil.rmtree(path, ignore_errors=True)
    print()
    if skips:
        print(f"{len(skips)} skipped:")
        for message in skips:
            print(f"  - {message}")
    if failures:
        print(f"{len(failures)} FAILED:")
        for message in failures:
            print(f"  - {message}")
        return 1
    print("ALL E2E ACCEPTANCE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
