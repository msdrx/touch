#!/usr/bin/env python3
"""Stdlib-only tests for `aggregator/tick.py` — the ingest tick (D-01). Run as
`python3 test_tick.py`; exits non-zero on failure. No pytest, no runner.

D-01's own test list, honoured one function each:

* boot the model over a frozen fixture corpus ⇒ `/api/sessions` non-empty,
  `/api/run/graph?run=<fixture run>` returns the JOURNAL-derived nodes,
  `/health.ingest.files > 0`, `store.streamCount > 0`;
* over an EMPTY corpus, `/health` reports the tick **present but idle** —
  distinguishable from "not running", which before D-01 was the only state a
  shipped install was ever in.

Plus the clauses that list implies and would not otherwise pin: a second tick
over unchanged files re-appends nothing to the WAL (the sources re-read whole
files, so without the digest guard the streams would grow every tick); a token
record lands on exactly ONE stream (GD-M5); nothing is ever written under
`~/.claude`.

Everything reads the FROZEN corpus (`tests/fixtures/`) through a temporary
`~/.claude`-shaped root whose `projects/` entries are symlinks, the same way
`test_ingest.py` does — no test here writes into the corpus, and
`tests/test_fixtures.py` would fail if one did.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The canonical trees are named through `tests/_roots.py`, never by a
# literal under REPO: GD-U1 moves them and this is the single flip point.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))

from aggregator import ingest as ingest_mod                        # noqa: E402
from aggregator import server as server_mod                        # noqa: E402
from aggregator import sessions as sessions_mod                    # noqa: E402
from aggregator import store as store_mod                          # noqa: E402
from aggregator import tick as tick_mod                            # noqa: E402

FIX = REPO / "tests" / "fixtures"
RUN = FIX / "run-wf_829e6f58"
RUN_ID = "wf_829e6f58-b2f"
DISCOVERY = FIX / "mirror" / "discovery" / "projects" / "-tmp-claude-1000-liveio"

#: The cwd every rooted test claims to run in, and the slug the CLI gives it.
#: A fixture tree mounted under that slug is what makes `sessions.scoped_dirs`
#: own it — the same device `test_ingest.py` uses.
OWNED_CWD = "/home/laniakea/Projects/touch"
OWNED_SLUG = sessions_mod.slug_for(OWNED_CWD)

#: The two stream ids `store.py`'s table declares and `server.py`'s routes can
#: resolve. Written here as a pattern because "the prefix is `session`" is what
#: `validate_stream` checks, and a `session:<uuid>` id passes that while naming
#: a file no reader will ever open.
STREAM_GRAMMAR = re.compile(r"^(run:.+|session:\d+-.+)$")

failures = []
skips = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  skip: {msg}")
    skips.append(msg)


def body(response):
    return json.loads(response.body.decode("utf-8"))


def linked_root(tmp, *pairs):
    """A `~/.claude`-shaped root whose project slugs symlink into the corpus."""
    root = os.path.join(tmp, "claude")
    os.makedirs(os.path.join(root, "projects"), exist_ok=True)
    for slug, target in pairs:
        os.symlink(os.fspath(target), os.path.join(root, "projects", slug))
    sessions_mod.reset_scope_cache()
    ingest_mod.reset_read_cache()
    return root


def mirror_tree(src, dst):
    """Recreate `src`'s tree at `dst` with the FILES symlinked (test_ingest.py's).

    Real directories, because `os.walk` does not follow a *nested* directory
    symlink and the tick's discovery walks. The 8 MB of file bytes stay shared,
    and the frozen corpus is never written to.
    """
    os.makedirs(dst, exist_ok=True)
    for name in sorted(os.listdir(src)):
        source = os.path.join(os.fspath(src), name)
        target = os.path.join(dst, name)
        if os.path.isdir(source):
            mirror_tree(source, target)
        else:
            os.symlink(source, target)


def booted(tmp, *pairs, poll=True):
    """A `ReadModel` + `IngestTick` over the named fixture trees, ticked once."""
    root = linked_root(tmp, *pairs)
    store = store_mod.Store(os.path.join(tmp, "touch"))
    model = server_mod.ReadModel(state={}, store=store, claude_root=root,
                                 tasks_root=os.path.join(tmp, "tasks"), reduce_ttl=0)
    tick = tick_mod.IngestTick(model, claude_root=root, cwd=OWNED_CWD)
    model.ingest = tick
    if poll:
        tick.poll()
    return model, tick, store, root


def api_of(model):
    api = server_mod.Api(model, auth=server_mod.Auth("t0ken"))
    return lambda path: api.get(path, {"authorization": "Bearer t0ken"})


def corpus_present() -> bool:
    return RUN.is_dir() and DISCOVERY.is_dir()


# --- D-01's list ----------------------------------------------------------


def test_a_boot_over_the_frozen_corpus_populates_every_empty_route():
    print("test_a_boot_over_the_frozen_corpus_populates_every_empty_route")
    if not corpus_present():
        skip("the frozen corpus is absent (a clean checkout or a packaged copy)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        # Both fixture trees under ONE owned slug: the run supplies the journal
        # and snapshot, the discovery tree the top-level session transcripts.
        # That is the shape a real project has and the shape a single-tree
        # fixture cannot express.
        merged = os.path.join(tmp, "merged")
        for source in (RUN, DISCOVERY):
            mirror_tree(source, merged)
        model, tick, store, root = booted(tmp, (OWNED_SLUG, merged))
        get = api_of(model)

        sessions = body(get("/api/sessions"))
        check(sessions["count"] > 0,
              f"/api/sessions is NON-EMPTY after one tick ({sessions['count']} rows) — "
              f"the pre-D-01 server answered [] over the same bytes")

        graph = body(get(f"/api/run/graph?run={RUN_ID}"))
        check(graph["counts"]["nodes"] == 7,
              f"the run graph returns the JOURNAL-derived nodes ({graph['counts']['nodes']} "
              f"of 7), not a reduction of any events.jsonl")
        check(graph["counts"]["agents"] == 7,
              "…and each node's agent document is joined to it")

        health = body(get("/health"))
        check(health["ingest"]["files"] > 0,
              f"/health.ingest.files > 0 ({health['ingest']['files']})")
        check(health["ingest"]["state"] == tick_mod.STATE_RUNNING,
              "…and the tick reports `running`")
        check(health["store"]["streamCount"] > 0,
              f"store.streamCount > 0 ({health['store']['streamCount']}) — the WAL has a "
              f"writer for the first time")
        check(f"run:{RUN_ID}" in store.streams(),
              "…and the run's own stream is one of them, so the socket has something "
              "to replay")


def test_an_empty_corpus_is_idle_and_idle_is_not_absent():
    print("test_an_empty_corpus_is_idle_and_idle_is_not_absent")
    with tempfile.TemporaryDirectory() as tmp:
        model, tick, _store, _root = booted(tmp)
        get = api_of(model)
        health = body(get("/health"))
        check(health["ingest"]["state"] == tick_mod.STATE_IDLE,
              "a tick over an empty corpus reports `idle` — present, ticking, nothing "
              "to read")
        check(health["ingest"]["ticks"] >= 1 and health["ingest"]["files"] == 0,
              "…with the tick count that proves it ran and the zero files that "
              "explain the silence")

        bare = server_mod.ReadModel(state={}, store=None)
        check(bare.ingest_health()["state"] == tick_mod.STATE_ABSENT,
              "a model with NO tick reports `absent` — the pre-D-01 condition, and a "
              "different word from `idle` (the whole point of the enum)")
        check(tick_mod.STATE_IDLE != tick_mod.STATE_ABSENT
              and tick_mod.STATE_CREATED != tick_mod.STATE_IDLE,
              "…and the three are distinct strings, so no reader can conflate them")


def test_a_second_tick_over_unchanged_files_writes_nothing_new():
    print("test_a_second_tick_over_unchanged_files_writes_nothing_new")
    if not corpus_present():
        skip("the frozen corpus is absent")
        return
    with tempfile.TemporaryDirectory() as tmp:
        model, tick, store, _root = booted(tmp, (OWNED_SLUG, RUN))
        first = store.stats["appended"]
        before = dict(model.sizes())
        check(first > 0, f"the first tick appended {first} records")
        tick.poll()
        tick.poll()
        check(store.stats["appended"] == first,
              "two further ticks append NOTHING: an unchanged file is not dirty, and "
              "even a re-read would be de-duplicated by the emitted digest")
        check(dict(model.sizes()) == before,
              "…and the collection sizes are identical, which is R-47's "
              "ingest-twice-same-counts property applied to the live path")


def test_a_token_record_lands_on_exactly_one_stream():
    print("test_a_token_record_lands_on_exactly_one_stream")
    if not corpus_present():
        skip("the frozen corpus is absent")
        return
    with tempfile.TemporaryDirectory() as tmp:
        model, _tick, store, _root = booted(tmp, (OWNED_SLUG, RUN))
        seen = {}
        for stream in store.streams():
            for record in store.read_all(stream):
                if record.get("kind") != "token":
                    continue
                message = (record.get("data") or {}).get("messageId")
                seen.setdefault(message, []).append(stream)
        check(seen, f"the WAL carries token records ({len(seen)} message ids)")
        doubled = {m: s for m, s in seen.items() if len(set(s)) > 1}
        check(not doubled,
              "GD-M5: every token record is on exactly ONE stream — a duplicated copy "
              "would be counted twice by every global sum that does not know to skip it")
        check(all(s[0].startswith("run:") for s in seen.values()),
              "…and with a runId known, that stream is the run's, not the session's")
        check(all(STREAM_GRAMMAR.match(stream) for stream in store.streams()),
              f"every stream the tick created is one `store.py`'s table declares — "
              f"`run:<runId>` or `session:<pid>-<procStart>` "
              f"({sorted(store.streams())})")


def test_every_token_record_carries_the_four_counts_where_a_reader_looks():
    print("test_every_token_record_carries_the_four_counts_where_a_reader_looks")
    if not corpus_present():
        skip("the frozen corpus is absent")
        return
    # `app.js`'s `noteTokens` and `renderRecord` read `record.data.in` and its
    # three siblings, `store._build_record` re-normalizes them at the top level
    # of `data`, and GD-11 says a token record always carries all four. A
    # writer that nests them one level down still passes every SHAPE test —
    # and renders `in 0 · out 0 · cached 0 · cache_write 0` for a run whose
    # journal holds millions of tokens. So this asserts the NUMBERS.
    with tempfile.TemporaryDirectory() as tmp:
        model, _tick, store, _root = booted(tmp, (OWNED_SLUG, RUN))
        keys = store_mod.TOKEN_KEYS
        per_message = {}
        records = 0
        for stream in store.streams():
            for record in store.read_all(stream):
                if record.get("kind") != "token":
                    continue
                records += 1
                data = record.get("data") or {}
                counts = per_message.setdefault(data.get("messageId"),
                                                dict.fromkeys(keys, 0))
                for key in keys:
                    counts[key] = max(counts[key], data.get(key) or 0)
        check(records > 0 and all(isinstance((r.get("data") or {}).get(k), int)
                                  for stream in store.streams()
                                  for r in store.read_all(stream)
                                  if r.get("kind") == "token"
                                  for k in keys),
              f"all four token keys are ints at the TOP level of `data` on every one "
              f"of the {records} token records")
        nonzero = sum(1 for counts in per_message.values() if any(counts.values()))
        check(nonzero == len(per_message),
              f"…and every message id's counts are non-zero ({nonzero} of "
              f"{len(per_message)}) — zeros here are the silent under-report "
              f"`normalize_tokens` exists to prevent")

        # The cross-check that a shape test cannot do: the WAL and the `usage`
        # collection are two writes of ONE derivation, so per message id the
        # stream's latest counts must be the document's ($max, R-50: the
        # counts are absolute and restated as a message streams).
        docs = {doc.get("_id"): doc for doc in (model.state.get("usage") or {}).values()}
        check(len(docs) == len(per_message),
              f"the WAL names exactly the message ids the `usage` collection does "
              f"({len(per_message)} vs {len(docs)})")
        disagreed = [message for message, counts in per_message.items()
                     if any((docs.get(message) or {}).get(key, 0) != counts[key]
                            for key in keys)]
        check(not disagreed,
              f"…and agrees with it field for field on every one of them "
              f"({len(disagreed)} disagreements)")
        wal_total = sum(counts["in"] + counts["out"] for counts in per_message.values())
        doc_total = sum(doc.get("in", 0) + doc.get("out", 0) for doc in docs.values())
        check(wal_total == doc_total and wal_total > 0,
              f"…so the two totals are the same number ({wal_total}), which is the "
              f"assertion a nested `tokens` sub-document fails and a shape test does not")


def test_a_restart_over_an_unchanged_corpus_appends_nothing():
    print("test_a_restart_over_an_unchanged_corpus_appends_nothing")
    if not corpus_present():
        skip("the frozen corpus is absent")
        return
    # The de-duplication memo guards an append-only file that outlives the
    # process. `poll()` marks a file dirty on first sight — which is every file
    # on the first tick of every boot — so a memo that starts empty makes each
    # restart re-append the whole corpus: 583 / 1166 / 1749 records on one run
    # stream over three boots, unrecoverable (`.touch/` has no compaction) and
    # served twice by `/api/events` and by the socket's replay.
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, (OWNED_SLUG, RUN))
        touch = os.path.join(tmp, "touch")
        boots = []
        for _ in range(3):
            store = store_mod.Store(touch)          # a NEW process's store
            model = server_mod.ReadModel(state={}, store=store, claude_root=root,
                                         tasks_root=os.path.join(tmp, "tasks"))
            tick = tick_mod.IngestTick(model, claude_root=root, cwd=OWNED_CWD)
            model.ingest = tick
            tick.poll()
            boots.append({stream: len(store.read_all(stream))
                          for stream in store.streams()})
        check(boots[0] and sum(boots[0].values()) > 0,
              f"the first boot wrote the corpus ({sum(boots[0].values())} records)")
        check(boots[1] == boots[0] and boots[2] == boots[0],
              f"two restarts over the same unchanged corpus append NOTHING: "
              f"{sum(boots[0].values())} / {sum(boots[1].values())} / "
              f"{sum(boots[2].values())} records")
        check(tick.health()["seededStreams"] == len(boots[0]),
              "…because the memo was seeded from the streams themselves, once each, "
              "which is what makes it outlive the process that filled it")


def test_the_tick_never_invents_a_session_stream_no_route_can_name():
    print("test_the_tick_never_invents_a_session_stream_no_route_can_name")
    # `store.py` declares ONE session grammar — `session:<pid>-<procStart>` —
    # and `ReadModel.session_stream` can resolve no other. A uuid-keyed stream
    # passes `validate_stream` (the prefix is right) and then splits a live
    # session in two: the session record on one stream, its tokens on another
    # that no route will ever open.
    session_id = "11111111-2222-4333-8444-555555555555"

    def usage_source(path=None, **kwargs):
        # Per-path, like every real usage source: the registry arm (`path=None`)
        # is deliberately restricted to the four kinds that answer it.
        if path is None:
            return ()
        return (ingest_mod.UsageObservation(
            message_id="msg_01AAAAAAAAAAAAAAAAAAAAAA", session_id=session_id,
            tokens={"in": 5, "out": 7, "cached": 0, "cache_write": 0},
            agent_id=None, run_id=None),)

    with tempfile.TemporaryDirectory() as tmp:
        # One transcript-shaped file so discovery has a path to hand the source.
        corpus = os.path.join(tmp, "corpus")
        os.makedirs(corpus)
        with open(os.path.join(corpus, session_id + ".jsonl"), "w",
                  encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "summary", "summary": "one line"}) + "\n")
        root = linked_root(tmp, (OWNED_SLUG, corpus))
        store = store_mod.Store(os.path.join(tmp, "touch"))
        model = server_mod.ReadModel(state={}, store=store, claude_root=root)
        tick = tick_mod.IngestTick(model, claude_root=root, cwd=OWNED_CWD,
                                   sources=[("usage", usage_source)])
        model.ingest = tick
        tick.poll()
        check(store.streams() == [],
              f"a usage observation whose session has no LIVE document writes no WAL "
              f"record at all ({store.streams()}) — a historical session names no "
              f"process, so it has no stream, and inventing one is the wrong-target "
              f"hazard `store.py`'s table exists to close")
        check((model.state.get("usage") or {}),
              "…while the observation is still in `ReadModel.state`, so nothing is "
              "lost: the route that reads it is `/api/query`, not a stream")

        # Now the session is live. `_live_session_stream` resolves the SAME key
        # `ReadModel.session_stream` serves reads through.
        model.state["sessions"] = {"live:622-10028": {
            "_id": "live:622-10028", "pid": 622, "procStart": "10028",
            "sessionIds": [session_id]}}
        # Forget the file so it is dirty again — the cheapest way to say "this
        # transcript grew" without depending on a clock.
        tick.tailers.clear()
        tick.poll()
        check(store.streams() == ["session:622-10028"],
              f"with a live document naming it, the token lands on "
              f"`session:<pid>-<procStart>` ({store.streams()})")
        check(model.session_stream(session_id) == "session:622-10028",
              "…which is exactly the stream `ReadModel.session_stream` resolves, so "
              "`/api/events?session=` and the socket open the file the tick wrote")
        check(all(STREAM_GRAMMAR.match(stream) for stream in store.streams()),
              "…and it matches the one declared grammar")


def test_nothing_is_ever_written_under_the_claude_root():
    print("test_nothing_is_ever_written_under_the_claude_root")
    if not corpus_present():
        skip("the frozen corpus is absent")
        return
    with tempfile.TemporaryDirectory() as tmp:
        model, tick, _store, root = booted(tmp, (OWNED_SLUG, RUN))
        before = sorted(os.listdir(os.path.join(root, "projects")))
        tick.poll()
        check(sorted(os.listdir(os.path.join(root, "projects"))) == before,
              "the tick creates nothing under `~/.claude` — it is a read-only tap, and "
              "the corpus is a symlink into frozen fixtures besides")
        check(os.path.isdir(os.path.join(tmp, "touch")),
              "…every byte it writes goes into the `.touch/` store it was given")


def test_the_usage_conflict_counters_are_cumulative_not_incremental():
    print("test_the_usage_conflict_counters_are_cumulative_not_incremental")
    if not corpus_present():
        skip("the frozen corpus is absent")
        return
    with tempfile.TemporaryDirectory() as tmp:
        model, tick, _store, _root = booted(tmp, (OWNED_SLUG, RUN))
        first = dict(model.counters)
        for _ in range(3):
            tick.poll()
        check(dict(model.counters) == first,
              "the sp-12 handoff's counters do not move on a re-read: they are "
              "DISTINCT conflicting message ids, assigned, not a `+=` that would turn "
              "a topology fact into a clock")


def test_the_writers_block_names_the_stream_with_no_producer():
    print("test_the_writers_block_names_the_stream_with_no_producer")
    with tempfile.TemporaryDirectory() as tmp:
        model, _tick, _store, _root = booted(tmp)
        writers = body(api_of(model)("/health"))["writers"]
        check(writers["run"] == "tick" and writers["session"] == "tick",
              "/health names the tick as the writer of the two streams it appends")
        check(writers["customState"] == "none",
              "…and says `none` for `custom-state`, whose head/slot driver needs a "
              "database handle this path does not have — an empty collection with no "
              "writer is correct, not broken")
        query = body(api_of(model)("/api/query?collection=slots"))
        check(query["writers"]["customState"] == "none" and query["count"] == 0,
              "the same note travels on /api/query, which is the route where 'empty' "
              "is most easily misread as 'lost'")


def test_the_full_scan_is_handed_the_prior_it_needs_to_be_useful():
    print("test_the_full_scan_is_handed_the_prior_it_needs_to_be_useful")
    # `sessions.scan` emits a promotion ONLY for a `hist:` id already in
    # `prior.ids`, and a `present:false` source ONLY for a path in
    # `prior.sources` that has since gone. A tick that passes no prior gets an
    # empty `Prior()` on every call and neither arm can ever fire — dead code
    # under a docstring that says it runs.
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp)
        model = server_mod.ReadModel(state={}, store=None, claude_root=root)
        seen = []

        def spy(path=None, *, cwd=None, root=None, prior=None, env=None):
            seen.append((path, prior))
            return ()

        tick = tick_mod.IngestTick(model, claude_root=root, cwd=OWNED_CWD,
                                   sources=[("session", spy)])
        tick.poll()
        check(seen and seen[0][0] is None,
              "the full-scan arm ran (path=None)")
        check(seen[0][1] is None,
              "…with no prior at all while the mirror is empty, which is exactly what "
              "an empty Prior would have meant anyway")

        # One historical session already in the model, with one source recorded.
        model.state["sessions"] = {
            "hist:11111111-2222-4333-8444-555555555555": {
                "_id": "hist:11111111-2222-4333-8444-555555555555",
                "sources": [{"path": "projects/-repo/a.jsonl", "kind": "transcript",
                             "present": True},
                            {"path": "projects/-repo/gone.jsonl", "kind": "transcript",
                             "present": False}]}}
        seen.clear()
        # The registry arm runs on its own slower clock; this is "five seconds
        # later" without sleeping for them.
        tick.last_full_scan = None
        tick.poll()
        prior = seen[0][1]
        check(prior is not None and "hist:11111111-2222-4333-8444-555555555555" in prior.ids,
              "with a session in the state, the prior names its id — which is the ONLY "
              "way a live session's promotion (R-46) is ever emitted")
        check(prior.known_sources("hist:11111111-2222-4333-8444-555555555555")
              == ("projects/-repo/a.jsonl",),
              "…and its still-present sources, so a file that disappears becomes "
              "`present:false` instead of silently vanishing")
        check("projects/-repo/gone.jsonl" not in prior.known_sources(
            "hist:11111111-2222-4333-8444-555555555555"),
              "…while one already recorded absent is not re-offered as a fresh "
              "disappearance on every tick")
        check("prior" in tick_mod._SOURCE_KWARGS,
              "the keyword is in the source-kwarg set, so a source that does not take "
              "one is still never handed it (the per-signature guard)")


def test_one_keyword_named_root_means_three_different_trees():
    print("test_one_keyword_named_root_means_three_different_trees")
    # `root` is not one thing across the five entity modules: `custom_state`'s
    # is the `.touch/` store, `legacy`'s is the TASKS root its sourcePaths are
    # relative to, and everything under `projects/**` is rooted at ~/.claude.
    # Passing the right value under the wrong name is a misread waiting for the
    # day a source stops refusing the path before it looks at its root.
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp)
        store = store_mod.Store(os.path.join(tmp, "touch"))
        tasks = os.path.join(tmp, "tasks")
        model = server_mod.ReadModel(state={}, store=store, claude_root=root,
                                     tasks_root=tasks)
        tick = tick_mod.IngestTick(model, claude_root=root, cwd=OWNED_CWD)
        check(tick._root_for("record") == root and tick._root_for("session") == root,
              "the transcript/journal sources are rooted at ~/.claude")
        check(tick._root_for("customState") == store.root
              and tick._root_for("slot") == store.root,
              "…custom_state's two sources at the `.touch/` store")
        check(tick._root_for("legacyEvent") == tasks
              and tick._root_for("legacyArtifact") == tasks,
              "…and legacy's two at the tasks root, which is neither of the others")
        check(len({tick._root_for(k) for k in
                   ("record", "customState", "legacyEvent")}) == 3,
              "the three really are three different trees on this model")


def test_the_dedup_memo_is_bounded_and_publishes_its_size():
    print("test_the_dedup_memo_is_bounded_and_publishes_its_size")
    if not corpus_present():
        skip("the frozen corpus is absent")
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, (OWNED_SLUG, RUN))
        store = store_mod.Store(os.path.join(tmp, "touch"))
        model = server_mod.ReadModel(state={}, store=store, claude_root=root)
        tick = tick_mod.IngestTick(model, claude_root=root, cwd=OWNED_CWD, max_emitted=10)
        model.ingest = tick
        tick.poll()
        health = tick.health()
        check(health["emitted"] <= 10 and health["maxEmitted"] == 10,
              f"the WAL de-duplication memo is capped, not unbounded "
              f"({health['emitted']} entries)")
        check(health["emitted"] > 0,
              "…and it is still doing its job below the cap")
        check(len(tick._emitted) == health["emitted"],
              "the published number is the real one, so growth is visible on /health "
              "rather than a thing a reader takes on trust")


def test_the_tick_survives_a_source_that_raises():
    print("test_the_tick_survives_a_source_that_raises")
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp)
        model = server_mod.ReadModel(state={}, store=store_mod.Store(os.path.join(tmp, "touch")),
                                     claude_root=root)

        def angry(path=None, **kwargs):
            raise RuntimeError("this module's file is broken")

        tick = tick_mod.IngestTick(model, claude_root=root, cwd=OWNED_CWD,
                                   sources=[("session", angry)])
        stats = tick.poll()
        check(stats["errors"] >= 1 and tick.errors >= 1,
              "a source that raises is counted, not propagated — one module's bad file "
              "must not take the live view down")
        check("RuntimeError" in (tick.health()["lastError"] or ""),
              "…and the reason reaches /health, which is the difference between "
              "degraded and silent")

        # The same promise one layer in: a malformed observation reaches the WAL
        # writer, where `store.normalize_tokens` refuses a stringly-typed count
        # (a coerced 0 is how a silent under-report starts). That refusal costs
        # one record, not the tick.
        def stringly(path=None, **kwargs):
            if path is None:
                return ()
            return (ingest_mod.UsageObservation(
                message_id="msg_01BBBBBBBBBBBBBBBBBBBBBB",
                session_id="22222222-3333-4444-8555-666677778888",
                tokens={"in": "5", "out": 7, "cached": 0, "cache_write": 0},
                agent_id=None, run_id="wf_00000000-000"),)

        corpus = os.path.join(tmp, "corpus")
        os.makedirs(corpus)
        with open(os.path.join(corpus, "33333333-4444-4555-8666-777788889999.jsonl"),
                  "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "summary", "summary": "one line"}) + "\n")
        root2 = linked_root(tmp, (OWNED_SLUG, corpus))
        store = store_mod.Store(os.path.join(tmp, "touch2"))
        model2 = server_mod.ReadModel(state={}, store=store, claude_root=root2)
        tick2 = tick_mod.IngestTick(model2, claude_root=root2, cwd=OWNED_CWD,
                                    sources=[("usage", stringly)])
        stats = tick2.poll()
        check(stats["errors"] >= 1 and "SchemaError" in (tick2.health()["lastError"] or ""),
              "a token count that is not an int is REFUSED and counted, never coerced "
              "to 0 — and the refusal does not escape `poll()`")
        check(store.streams() == [],
              "…and no half-written record reaches the stream")


def main():
    for test in (
        test_a_boot_over_the_frozen_corpus_populates_every_empty_route,
        test_an_empty_corpus_is_idle_and_idle_is_not_absent,
        test_a_second_tick_over_unchanged_files_writes_nothing_new,
        test_a_token_record_lands_on_exactly_one_stream,
        test_every_token_record_carries_the_four_counts_where_a_reader_looks,
        test_a_restart_over_an_unchanged_corpus_appends_nothing,
        test_the_tick_never_invents_a_session_stream_no_route_can_name,
        test_nothing_is_ever_written_under_the_claude_root,
        test_the_usage_conflict_counters_are_cumulative_not_incremental,
        test_the_writers_block_names_the_stream_with_no_producer,
        test_the_full_scan_is_handed_the_prior_it_needs_to_be_useful,
        test_one_keyword_named_root_means_three_different_trees,
        test_the_dedup_memo_is_bounded_and_publishes_its_size,
        test_the_tick_survives_a_source_that_raises,
    ):
        test()
    print()
    if skips:
        for reason in skips:
            print(f"skipped: {reason}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("all ingest tick (D-01) tests passed")


if __name__ == "__main__":
    main()
