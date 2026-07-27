#!/usr/bin/env python3
"""Stdlib-only tests for aggregator/mirror.py (R-45: the write-behind mirror).
Run as `python3 test_mirror.py`; exits non-zero on failure. No pytest, no runner.

R-45's own test list is the spine, and every line of it is a *reproduced
failure*, not a happy path:

* **dead-port** — tick duration never approaches MONGOSCHEMA-4's measured 30.1 s
  stall, and `/health` reports `mirror:"down"` once the breaker holds;
* **queue-full** drops mirror writes, never live frames, and counts them;
* **two writers on one stream** ⇒ the second refuses (GD-29);
* **replay of own output** ⇒ duplicates tolerated, zero data change (GD-25);
* **backfill** of a 03:00Z-dated fixture ⇒ no stored `ts` within 24 h of `now()`;
* **wipe + `--rebuild`** ⇒ fingerprint equal to pre-wipe (GD-22's whole claim).

Plus the invariants that only a test holds in place:

* GD-21 — `pymongo` is imported lazily, inside functions, and its absence is a
  *state* (`absent`), never an exception;
* GD-26 — the module has no delete verb but the one scoped `stream_meta`
  renumber, asserted statically over the source as well as behaviourally;
* GD-30 — `enqueue` is synchronous and total: it is the only thing the 250 ms
  poll loop calls, so it is timed against a *dead* server, which is the only
  time the claim "0 ms on the critical path" can actually be wrong;
* SD-1 — one observation kind has one owning mapper, and a mapper's output is
  validated at the registry boundary.

The deployment/security half of this sub-plan (R-42's credentials, the derived
database name, GD-27's refusals, and the documented `docker run` recipe) lives
in `test_mongo_deploy.py`; the split follows the two plan items, not the two
modules.

The live arm follows `test_mongo_store.py`'s convention exactly: it runs against
`TOUCH_MONGO_URI` when that points at a mongod (R-42's loopback+auth recipe) and
**skips cleanly** otherwise, using `touch_test_<pid>` and dropping only that.
"""

import ast
import asyncio
import datetime
import os
import re
import sys
import tempfile
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[0]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from aggregator import mirror as mr                            # noqa: E402
from aggregator import mongo_store as ms                       # noqa: E402
from aggregator import refs                                    # noqa: E402
from aggregator import tailer as tailer_mod                    # noqa: E402
from aggregator.mirror import (                                # noqa: E402
    BREAKER_FAILURES,
    MapperError,
    Mapper,
    MemoryBackend,
    Mirror,
    MirrorError,
    MirrorOp,
    MongoConfig,
    STATE_ABSENT,
    STATE_DEGRADED,
    STATE_DOWN,
    STATE_LIVE,
    STATE_REFUSED,
    SweepScopeError,
    discover_mappers,
    map_observation,
    stamp_gen,
    validate_op,
)

failures = []
skips = []

UTC = datetime.timezone.utc
SESSION = "292fc08c-923d-4ab4-8ff2-a9572417dbc8"    # a real session id

#: MONGOSCHEMA-4's measurement, and the number this module exists to make
#: impossible: pymongo's default `serverSelectionTimeoutMS` stalls the poll loop
#: for 30.1 s against a dead port. Any tick anywhere near this is the bug back.
STALL_SECONDS = 30.0

#: D6/R-23's poll interval. The drainer is not ON the critical path, so this is
#: the budget for the *steady state* — once the breaker holds, a dead server
#: must cost a tick nothing at all.
TICK_BUDGET = mr.TICK_INTERVAL_S


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  SKIP: {msg}")
    skips.append(msg)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception as other:                                  # noqa: BLE001
        print(f"    (raised {type(other).__name__}: {other})")
        return False
    return False


def uuid_at(n):
    """A well-formed, stable record uuid for index ``n``."""
    return f"00000000-0000-4000-8000-{n:012d}"


def record_op(n, *, out=1, session=SESSION, ts=None):
    """One `records` upsert in GD-25's algebra: `$max` on the accumulable.

    Whole-second timestamps on purpose — BSON stores milliseconds, so a
    microsecond-precision datetime would make the memory model and a real mongod
    disagree on the fingerprint for a reason that has nothing to do with the
    algebra under test.
    """
    fields = {"sessionId": session, "type": "assistant", "provenance": "harness"}
    if ts is not None:
        fields["ts"] = ts
    return MirrorOp("records", refs.record_key(uuid_at(n)),
                    ms.merge_ops(ms.op_set(fields), ms.op_max({"outputTokens": out}),
                                 collection="records"))


def meta_op(line_no, *, session=SESSION):
    return MirrorOp("stream_meta", refs.stream_meta_key(session, line_no),
                    ms.op_set({"sessionId": session, "lineNo": line_no,
                               "type": "summary", "provenance": "harness"}))


def observations(count, *, out=1):
    """`(kind, obs)` pairs for the rebuild/backfill paths."""
    return [("record", {"n": n, "out": out}) for n in range(count)]


def record_registry():
    """SD-1's registry, hand-built: pure, no I/O, `mongo_store` vocabulary only."""
    return {"record": Mapper("record", "tests",
                             lambda obs: [record_op(obs["n"], out=obs.get("out", 1),
                                                    ts=obs.get("ts"))])}


def observation_state(state):
    """State minus `writers`: a lease is runtime, not mirrored history.

    Fingerprint comparisons across processes and across a wipe must be about the
    observations. The lease document carries a pid and an expiry that legitimately
    differ between two runs, and including it would make GD-22's rebuild claim
    untestable for a reason GD-22 says nothing about.
    """
    return {name: bucket for name, bucket in state.items() if name != "writers"}


def live_mirror(state=None, *, backend=None, **kwargs):
    """A `Mirror` on the memory backend, already in the state a tick expects.

    The writer lease is **taken for real**, not faked, because `lease_required`
    now defaults to True and a tick under GD-29 declines to write without one. A
    fixture that opted out would exercise the drain path in a configuration
    production never reaches — and it is the configuration the fix was about, so
    every test built on it would be proving things about a mirror that is
    refusing to write.

    The cost is one `writers` document in the backend, which is why
    :func:`counts` exists: a lease is runtime, not mirrored history (the same
    reason :func:`observation_state` drops it before a fingerprint).
    """
    backend = backend if backend is not None else MemoryBackend(
        state if state is not None else {})
    mirror = Mirror(MongoConfig("uri-placeholder", "touch_test"), backend=backend,
                    registry=record_registry(), **kwargs)
    mirror.state = STATE_LIVE
    fixture_run(mirror.acquire())
    return mirror, backend


def fixture_run(coro):
    """Run a fixture coroutine from sync code *or* from inside a running loop.

    `live_mirror` takes a real lease, and it is called from both kinds of test
    body, so `asyncio.run` is unavailable to it in half of them. Every backend a
    fixture drives is a `MemoryBackend`, whose methods never await anything real
    — so one `send(None)` runs the coroutine to completion, and one that *did*
    await real I/O fails loudly here rather than half-running.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise RuntimeError("a fixture coroutine awaited real I/O — use `run()` for that")


def counts(backend):
    """`backend.counts()` minus `writers` — the observations, not the lease.

    Same reason as :func:`observation_state`: the lease is runtime state that a
    fixture takes for real (GD-29 now stops a tick that holds none), and an
    assertion about what was *mirrored* must not have to know that.
    """
    return {name: n for name, n in fixture_run(backend.counts()).items()
            if name != "writers"}


def run(coro):
    return asyncio.run(coro)


# --- GD-21: the dependency is lazy, and its absence is a state ------------
def test_pymongo_is_lazy_and_its_absence_is_a_state():
    print("test_pymongo_is_lazy_and_its_absence_is_a_state")
    tree = ast.parse((REPO / "aggregator" / "mirror.py").read_text(encoding="utf-8"))

    top_level = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            names = [a.name for a in node.names]
            if "pymongo" in module or any("pymongo" in n for n in names):
                top_level.append(node.lineno)
    check(not top_level,
          f"no module-level pymongo import (GD-21: every module outside the two "
          f"stays importable with nothing installed) — found at {top_level}")

    imports = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            if "pymongo" in module or any("pymongo" in a.name for a in node.names):
                imports += 1
    check(imports > 0, f"…and pymongo IS imported, inside functions ({imports} sites)")

    # The absence itself: a configured mirror with no driver is `absent`, and it
    # neither raises nor leaves a half-open client behind.
    real = ms.pymongo_available
    ms.pymongo_available = lambda: False
    try:
        mirror = Mirror(MongoConfig("mongodb" + "://ignored/x", "touch_test"))
        state = run(mirror.start())
    finally:
        ms.pymongo_available = real
    check(state == STATE_ABSENT, f"a configured mirror with no pymongo is 'absent', got {state!r}")
    check(mirror.backend is None, "…and no client was constructed")
    check("pymongo" in (mirror.last_error or ""),
          "…and /health says why, in words an operator can act on")

    # And an *unconfigured* one, which is the default deployment.
    bare = Mirror()
    check(bare.state == STATE_ABSENT and run(bare.start()) == STATE_ABSENT,
          "no URI at all is 'absent' too — the mirror is opt-in (GD-22)")


# --- GD-30: enqueue is the only thing the poll loop calls -----------------
def test_enqueue_never_blocks_never_raises_and_never_awaits():
    print("test_enqueue_never_blocks_never_raises_and_never_awaits")
    source = (REPO / "aggregator" / "mirror.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    enqueue = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "enqueue")
    check(not isinstance(enqueue, ast.AsyncFunctionDef),
          "enqueue is a plain def: the poll loop cannot accidentally await the database")
    check(not [n for n in ast.walk(enqueue) if isinstance(n, (ast.Await, ast.AsyncFor))],
          "…and contains no await at all (GD-30: Mongo is 0 ms on the critical path)")

    # Timed against a DEAD server, which is the only case where the claim can be
    # wrong: a client that connects lazily makes a live server prove nothing.
    dead = Mirror(MongoConfig("mongodb" + "://127.0.0.1:1/x", "touch_test"))
    dead.state = STATE_LIVE                         # as if it had once connected
    ops = [record_op(n) for n in range(200)]
    started = time.monotonic()
    accepted = dead.enqueue(ops)
    elapsed = time.monotonic() - started
    check(accepted == 200, f"all 200 operations accepted, got {accepted}")
    check(elapsed < 0.05,
          f"200 enqueues against an unreachable server took {elapsed*1000:.1f} ms (< 50 ms)")

    # Total: a poison operation must not raise INTO the poll loop either.
    mirror, _ = live_mirror()
    check(mirror.enqueue(["not-a-triple"]) == 1,
          "even a malformed operation is accepted rather than raised at the caller")
    check(run(mirror.tick())["rejected"] == 1,
          "…and rejected by the drainer, on its own side of the line")
    check(mirror.stats["rejected"] == 1, "…and counted")
    check(mirror.queue.empty(),
          "…and never re-queued: it would fail identically forever and wedge the queue")

    # …and the argument itself is coerced honestly. `MirrorOp` is a tuple
    # subclass, so "not a list or tuple ⇒ one operation" queued a GENERATOR
    # object as a single operation and returned `accepted=1` for it — a count
    # that lied, followed by a `rejected` one tick later when nothing could
    # unpack it. A streaming mapper is exactly the caller that hits this.
    streamed, streamed_backend = live_mirror()
    accepted = streamed.enqueue(op for op in [record_op(1), record_op(2)])
    check(accepted == 2, f"a generator of operations queues each of them, got {accepted}")
    run(streamed.flush())
    check(counts(streamed_backend).get("records") == 2,
          "…and all of them land, rather than one unusable generator object")
    check(streamed.enqueue(record_op(3)) == 1,
          "…while a bare MirrorOp — itself a tuple — is still one operation, not three")
    before = streamed.stats["rejected"]
    check(streamed.enqueue(42) == 0 and streamed.stats["rejected"] == before + 1,
          "…and a shape that is neither is refused and counted here, where the "
          "caller can be named, instead of failing later with less context")
    check(streamed.state == STATE_DEGRADED and "int" in (streamed.last_error or ""),
          f"…saying so on /health: {streamed.last_error!r}")
    run(streamed.flush())
    check(streamed.state == STATE_DEGRADED,
          "…and staying there: what that caller meant to write is gone, so a later "
          "clean tick does not make `live` true about it (the `map_total` rule)")


def test_queue_full_drops_mirror_writes_and_degrades():
    print("test_queue_full_drops_mirror_writes_and_degrades")
    mirror, _ = live_mirror(queue_size=4)
    accepted = mirror.enqueue([record_op(n) for n in range(10)])
    check(accepted == 4, f"a bounded queue accepts what fits, got {accepted}")
    check(mirror.stats["dropped"] == 6, f"…and counts the rest: {mirror.stats['dropped']}")
    check(mirror.state == STATE_DEGRADED,
          f"…and says so in /health rather than stalling the ingest: {mirror.state}")
    health = mirror.health()
    check(health["dropped"] == 6 and health["queued"] == 4,
          "the /health block carries both numbers (R-45's shape)")

    # The other half of GD-30's sentence — "drop mirror writes, never live
    # frames" — is structural: live frames are served from the in-memory
    # reduction and are never in this queue. So the queue holds mirror
    # operations only, and that is what is asserted.
    queued = [mirror.queue.get_nowait() for _ in range(mirror.queue.qsize())]
    check(all(isinstance(op, MirrorOp) for op in queued),
          "the queue holds MirrorOps only — live frames are not in it to be dropped")

    # An `absent` mirror promised nothing, so it must not accumulate a loss.
    absent = Mirror()
    absent.enqueue([record_op(1)])
    check(absent.stats["dropped"] == 0 and absent.stats["skipped_absent"] == 1,
          "a deployment with no Mongo counts skips, not drops (GD-21)")

    # A burst that fills the queue while the mirror is still STARTING loses just
    # as much history as one at steady state — GD-30 does not qualify the prior
    # state, and leaving /health on `starting` would hide the loss.
    early = Mirror(MongoConfig("uri-placeholder", "touch_test"),
                   backend=MemoryBackend({}), queue_size=2)
    check(early.state == mr.STATE_STARTING, "a freshly constructed mirror is `starting`")
    early.enqueue([record_op(n) for n in range(5)])
    check(early.state == STATE_DEGRADED,
          f"…and a queue-full burst degrades it from `starting` too, got {early.state!r}")
    check(run(early.start()) == STATE_DEGRADED,
          "…and start() does not then promote it to `live` over the loss")
    check(early.health()["dropped"] == 3 and early.last_error,
          f"…with the count and the reason both on /health: {early.health()['lastError']!r}")


# --- the dead port: MONGOSCHEMA-4's stall, reproduced then fixed ----------
def test_dead_port_never_stalls_and_reports_down():
    print("test_dead_port_never_stalls_and_reports_down")
    if not ms.pymongo_available():
        skip("the dead-port arm needs pymongo to have a driver to time out (GD-21)")
        return
    # Port 1 is privileged and closed: connection refused, no listener, no DNS.
    mirror = Mirror(MongoConfig("mongodb" + "://127.0.0.1:1/x", "touch_test"))

    # ONE event loop for the whole scenario: `AsyncMongoClient` binds to the loop
    # it was created on, so a test that called asyncio.run() per step would be
    # measuring its own bug instead of the server-selection timeout.
    async def scenario():
        started = time.monotonic()
        state = await mirror.start()
        connect = time.monotonic() - started
        durations = []
        for n in range(BREAKER_FAILURES + 3):
            mirror.enqueue([record_op(n)])
            tick_started = time.monotonic()
            await mirror.tick()
            durations.append(time.monotonic() - tick_started)
        return state, connect, durations

    state, connect, durations = run(scenario())
    check(state == STATE_DOWN, f"a dead port leaves the mirror 'down', got {state!r}")
    check(connect < STALL_SECONDS / 3,
          f"connect took {connect:.2f}s — MONGOSCHEMA-4's 30.1 s stall is gone "
          f"(GD-21 caps server selection at 500 ms)")

    check(max(durations) < STALL_SECONDS / 3,
          f"no tick came near the 30.1 s stall (worst {max(durations):.2f}s)")
    check(mirror.breaker_open,
          f"the breaker is holding after {BREAKER_FAILURES} consecutive failures (GD-30)")
    check(mirror.state == STATE_DOWN, f"/health reports mirror:'down', got {mirror.state!r}")

    steady = durations[BREAKER_FAILURES:]
    check(steady and max(steady) < TICK_BUDGET,
          f"once the breaker holds, a dead server costs a tick nothing: "
          f"{[f'{d:.3f}' for d in steady]} < {TICK_BUDGET}s budget")

    # The whole point of the breaker: the cost is paid per HOLD, not per tick.
    check(sum(1 for d in durations if d > 0.1) <= BREAKER_FAILURES,
          "at most BREAKER_FAILURES ticks ever pay the server-selection timeout")

    # Nothing was lost while it was down — the queue is the buffer.
    check(mirror.queue.qsize() == BREAKER_FAILURES + 3,
          f"every operation is still queued for recovery, got {mirror.queue.qsize()}")
    check(mirror.stats["dropped"] == 0, "…and nothing was dropped (the queue had room)")


def test_a_transient_outage_requeues_rather_than_losing_writes():
    print("test_a_transient_outage_requeues_rather_than_losing_writes")
    mirror, backend = live_mirror()
    run(mirror.start(ensure_schema=False))
    backend.fail = ms.MongoUnavailable("the server went away mid-tick")
    mirror.enqueue([record_op(n) for n in range(3)])

    # Counted through the outage, because the retry path is where "once per
    # operation" was false: `_requeue` put already-scrubbed operations back and
    # the next drain walked all of them again — 8.79 ms per 550 KB document, per
    # tick, for the whole length of the outage, on bytes that cannot have
    # changed. Idempotent, so the stored result was right; the cost was not, and
    # it was paid exactly when the mirror was already unhealthy.
    scrubs = []
    real_scrub_op = mr.scrub_op_update
    mr.scrub_op_update = lambda update: (scrubs.append(1), real_scrub_op(update))[1]
    try:
        report = run(mirror.tick())
        check(report["skipped"] == "unavailable", "the tick reports the outage")
        check(mirror.queue.qsize() == 3,
              f"operations taken out of the queue go BACK into it, got {mirror.queue.qsize()} "
              f"— a mirror that drops history silently is the one thing GD-26 forbids")
        check(mirror.stats["dropped"] == 0,
              "…and nothing is counted as dropped, because nothing was")
        first_pass = len(scrubs)
        check(first_pass == 3, f"GD-27's backstop ran once per operation: {first_pass}")

        # One retry, not several: a third consecutive failure opens the breaker
        # (BREAKER_FAILURES), and a held tick proves nothing about the scrub
        # because it never reaches the queue at all.
        run(mirror.tick())                                   # still down: requeued again
        check(len(scrubs) == 3,
              f"…and NOT again on the retry that re-drains the same operations: "
              f"{len(scrubs)} scrub(s) for 3 operations")
        check(all(isinstance(op, mr.ScrubbedOp) for op in list(mirror.queue._queue)),
              "requeued operations carry the marker that says so (the type IS the flag: "
              "a tuple subclass cannot hold a per-instance one)")

        backend.fail = None
        run(mirror.tick())
        check(len(scrubs) == 3,
              f"…nor on the recovery tick that finally writes them: {len(scrubs)}")
    finally:
        mr.scrub_op_update = real_scrub_op

    check(run(backend.counts()).get("records") == 3,
          "…and they land on recovery, in full")
    check(mirror.state == STATE_LIVE, f"a clean tick clears the degrade, got {mirror.state!r}")

    # The marker is not a way to smuggle an unscrubbed payload past the backstop:
    # only `validate_op` mints one, and only after actually scrubbing.
    hostile = mr.validate_op(("custom_state_events",
                              refs.ref_key({"kind": "customStateEvent",
                                            "stream": "s", "seq": 1}),
                              ms.op_set({"kind": "progress", "seq": 1,
                                         "provenance": "asserted",
                                         "data": {"custom": ms.wrap_raw(
                                             {"authToken": "sk-ant-api03-" + "A" * 30})}})))
    check(isinstance(hostile, mr.ScrubbedOp),
          "validate_op returns a ScrubbedOp when it scrubbed…")
    stored = ms.unwrap_raw(hostile.update["$set"]["data"]["custom"])
    check(stored["authToken"] == mr.REDACTED,
          f"…and it is marked because the walk RAN, not instead of it: {stored}")

    # When the queue DOES overflow while the server is away, the loss is counted
    # and visible — that is the documented degrade, and it is a different thing.
    mirror2, backend2 = live_mirror(queue_size=3)
    backend2.fail = ms.MongoUnavailable("still gone")
    mirror2.enqueue([record_op(n) for n in range(3)])
    run(mirror2.tick())
    check(mirror2.queue.qsize() == 3, "the in-flight batch is back in a full queue")
    mirror2.enqueue([record_op(99)])
    check(mirror2.stats["dropped"] == 1,
          "…and the next write overflows it, counted in /health (GD-30)")

    # The narrow window where an operation could vanish uncounted: it has already
    # left the queue and has not yet entered a batch. `_take_batches` caught
    # `MapperError` only, while the scrub it runs there walks a payload of
    # agent-controlled depth — so a pathologically nested document raised
    # `RecursionError`, escaped to `tick`'s blanket guard, and was booked as a
    # *failure* rather than as `rejected` or `dropped`. Nobody will reach it; it
    # is still an operation GD-26 says cannot disappear quietly.
    mirror3, backend3 = live_mirror()
    mirror3.enqueue([record_op(4)])

    def explode(update):
        raise RecursionError("maximum recursion depth exceeded while scrubbing")

    real_scrub = mr.scrub_op_update
    mr.scrub_op_update = explode
    try:
        report3 = run(mirror3.tick())
    finally:
        mr.scrub_op_update = real_scrub
    check(report3["rejected"] == 1 and mirror3.stats["rejected"] == 1,
          f"an operation that explodes mid-scrub is counted as REJECTED: {report3}")
    check(mirror3.queue.empty() and backend3.calls["bulk_upsert"] == 0,
          "…dropped from the queue rather than retried forever, and never written")
    check("RecursionError" in (mirror3.last_error or ""),
          f"…and named on /health: {mirror3.last_error!r}")
    mirror3.enqueue([record_op(5)])
    run(mirror3.flush())
    check(counts(backend3).get("records") == 1,
          "…while the next operation drains normally: one poison document is a fact "
          "about one document")


def test_a_driver_surprise_on_the_lease_path_degrades_instead_of_killing_the_drainer():
    print("test_a_driver_surprise_on_the_lease_path_degrades_instead_of_killing_the_drainer")
    # `tick()` said "never raises" and `start()` said "every failure here is a
    # state, never an exception", and both were false on one branch: the lease.
    # `acquire()` caught `MongoUnavailable` only, while its single call —
    # `guarded_update` — has three other exits: a server-side $jsonSchema refusal
    # arriving as SchemaError, validate_document's MongoStoreError, and anything
    # the driver raises that is not a PyMongoError. The module's own comment
    # names the specimen ("Cannot use AsyncMongoClient in different event loop")
    # and defends against it around `bulk_upsert` — one branch later on the same
    # tick. A renewal is due at least every LEASE_TTL_S * LEASE_RENEW_AT, so it
    # was reachable roughly every 15 s.
    surprises = [
        RuntimeError("Cannot use AsyncMongoClient in different event loop"),
        ms.SchemaError("writers: the server's $jsonSchema refused this guarded update"),
    ]
    for exc in surprises:
        label = type(exc).__name__
        mirror, backend = live_mirror()
        run(mirror.start(ensure_schema=False))
        check(mirror.state == STATE_LIVE, f"{label}: the mirror starts live")
        # Force the renewal branch: the lease expires this instant.
        mirror._lease["expiresAt"] = mirror.clock().isoformat().replace("+00:00", "Z")
        backend.fail = exc
        mirror.enqueue([record_op(0)])
        try:
            report = run(mirror.tick())
            raised = None
        except Exception as escaped:                             # noqa: BLE001
            report, raised = None, escaped
        check(raised is None, f"{label}: tick() returns a report instead of raising it "
                              f"(got {raised!r})")
        check(report and report["skipped"] is not None,
              f"{label}: …and the report says the tick did not write: {report}")
        check(mirror.state in (STATE_DEGRADED, STATE_DOWN),
              f"{label}: /health degrades rather than claiming `live` over a dead "
              f"mirror, got {mirror.state!r}")
        check(mirror.health()["lastError"] and label in mirror.health()["lastError"],
              f"{label}: …with a truthful lastError: {mirror.health()['lastError']!r}")

        # …and it recovers, which is what makes it a state rather than a death.
        backend.fail = None
        run(mirror.tick())
        run(mirror.flush())
        check(run(backend.counts()).get("records") == 1,
              f"{label}: the write lands once the surprise clears")

    # `start()` is total too: a driver that surprises the connect/ping/schema
    # path must not abort the caller's startup over a database GD-22 calls
    # optional.
    starting, starting_backend = live_mirror()
    starting.state = mr.STATE_STARTING
    starting_backend.fail = RuntimeError("driver surprise")
    try:
        state = run(starting.start(ensure_schema=False))
        raised = None
    except Exception as escaped:                                 # noqa: BLE001
        state, raised = None, escaped
    check(raised is None, f"start() returns a state instead of raising (got {raised!r})")
    check(state in (STATE_DEGRADED, STATE_DOWN),
          f"…and it is an honest one: {state!r}")

    # The consequence that made this a major: the long-lived task itself. A
    # `run()` that dies leaves /health frozen on its last state while every
    # enqueue piles into a queue nobody drains.
    async def scenario():
        mirror, backend = live_mirror()
        await mirror.start(ensure_schema=False)
        stop = asyncio.Event()
        task = asyncio.create_task(mirror.run(stop=stop, interval=0.01))
        mirror._lease["expiresAt"] = mirror.clock().isoformat().replace("+00:00", "Z")
        backend.fail = RuntimeError("Cannot use AsyncMongoClient in different event loop")
        mirror.enqueue([record_op(0)])
        for _ in range(50):
            await asyncio.sleep(0.005)
            if mirror.state in (STATE_DEGRADED, STATE_DOWN):
                break
        alive = not task.done()
        mirror._hold_until = 0.0
        backend.fail = None
        for _ in range(200):
            await asyncio.sleep(0.005)
            if len(backend.state.get("records", {})) == 1:
                break
        stop.set()
        mirror._wakeup.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:                             # pragma: no cover
            task.cancel()
        return alive, mirror, backend

    alive, mirror, backend = run(scenario())
    check(alive, "the drainer task is still alive after the surprise — a dead task with "
                 "a /health that still says `live` is the worst failure this module has")
    check(len(backend.state.get("records", {})) == 1,
          "…and it is still writing once the fault clears")
    check(mirror.state == STATE_LIVE,
          f"…back to live, because a failure is a state and states change: {mirror.state!r}")


def test_a_renewal_that_failed_stops_the_tick_even_when_it_was_not_a_refusal():
    print("test_a_renewal_that_failed_stops_the_tick_even_when_it_was_not_a_refusal")
    # `acquire()` returns False for two different reasons, and the tick branched
    # on the STATE instead of on the boolean: a lost race sets `refused` and was
    # handled, while a failure to reach `writers` at all sets `degraded` and fell
    # straight through to `bulk_upsert` — writing a batch under a lease that was
    # not renewed and may already have expired. GD-29 says a process that cannot
    # hold the lease does not mirror; it does not say "unless the reason was
    # interesting".
    #
    # A partial outage is what reaches it: `writers` unreachable while the data
    # collections answer normally. Whole-server outages hide the bug, because
    # then the write fails too.
    class WritersUnreachable(MemoryBackend):
        async def guarded_update(self, collection, key, update, *, require=None, upsert=True):
            self.calls["guarded_update"] += 1
            raise ms.MongoUnavailable(f"{collection} is unreachable")

    backend = WritersUnreachable({})
    mirror = Mirror(MongoConfig("u", "touch_test"), backend=backend,
                    registry=record_registry())
    mirror.state = STATE_LIVE
    # A lease this process holds, expiring now: the renewal branch, not the
    # take-over one.
    mirror._lease.update(held=True,
                         expiresAt=mirror.clock().isoformat().replace("+00:00", "Z"))
    check(mirror._lease_due() is True, "the lease is due for renewal this tick")

    mirror.enqueue([record_op(n) for n in range(3)])
    report = run(mirror.tick())
    check(mirror.state == STATE_DEGRADED,
          f"the failed renewal is a fault, not a refusal: {mirror.state!r}")
    check(report["skipped"] == STATE_DEGRADED,
          f"…and the tick still stops on it, reporting the real state: {report}")
    check(backend.calls["bulk_upsert"] == 0,
          f"…having written NOTHING under an unrenewed lease (GD-29): "
          f"{backend.calls['bulk_upsert']} bulk_upsert call(s)")
    check(mirror.queue.qsize() == 3, "…and the operations are still queued, not lost")


def test_a_mirror_that_never_took_the_lease_writes_nothing():
    print("test_a_mirror_that_never_took_the_lease_writes_nothing")
    # The renewal above was airtight; the branch that guarded it was not. The
    # whole lease block was gated on `_lease["held"]`, so a process that never
    # acquired the lease never even TRIED to, and never declined to write: an
    # `acquire()` that failed for any reason but a lost race leaves `degraded`
    # with `held=False`, and `degraded` is a state `enqueue` accepts and the
    # drainer writes in. Result: two live writers on one stream — the exact thing
    # GD-29's lease exists to prevent — with `/health` publishing `state:"live"`
    # beside `lease:{held:false}`, which nothing downstream can tell from healthy.
    #
    # The backend shape is the one none of the other fakes have: `writers` fails,
    # everything else answers normally. `MemoryBackend.fail` fails EVERYTHING,
    # which hides this the way a whole-server outage hides a renewal bug.
    class FlakyLease(MemoryBackend):
        def __init__(self, exc):
            super().__init__({})
            self.exc = exc

        async def guarded_update(self, collection, key, update, *, require=None, upsert=True):
            if collection == "writers" and self.exc is not None:
                self.calls["guarded_update"] += 1
                raise self.exc
            return await super().guarded_update(collection, key, update,
                                                require=require, upsert=upsert)

    specimens = [
        ms.MongoUnavailable("writers unreachable"),
        RuntimeError("Cannot use AsyncMongoClient in different event loop"),
        ms.SchemaError("writers: the server's $jsonSchema refused this guarded update"),
    ]
    for exc in specimens:
        label = type(exc).__name__
        backend = FlakyLease(exc)
        mirror = Mirror(MongoConfig("u", "touch_test"), backend=backend,
                        registry=record_registry())
        state = run(mirror.start(ensure_schema=False))
        check(state != STATE_LIVE and mirror._lease["held"] is False,
              f"{label}: a start() whose lease attempt failed does not reach `live`: "
              f"{state!r}, held={mirror._lease['held']}")

        # The `writers` outage persists, and the DATA collections are healthy —
        # so nothing but the lease can stop this write. It must stop it.
        attempts = backend.calls["guarded_update"]
        accepted = mirror.enqueue([record_op(0)])
        report = run(mirror.tick())
        check(backend.calls["bulk_upsert"] == 0,
              f"{label}: a tick with no lease writes NOTHING, however healthy the "
              f"data collections are (GD-29): {backend.calls['bulk_upsert']} "
              f"bulk_upsert call(s), accepted={accepted}, report={report}")
        check(backend.calls["guarded_update"] > attempts,
              f"{label}: …having actually TRIED to take the lease first, rather than "
              f"skipping the branch because it held none")
        check(mirror.stats["written"] == 0, f"{label}: …nothing is counted as written")
        check(mirror.queue.qsize() == 1, f"{label}: …and the operation is still queued")
        health = mirror.health()
        check(not (health["state"] == STATE_LIVE and health["lease"]["held"] is False),
              f"{label}: /health never publishes `live` beside an unheld lease: "
              f"{health['state']!r} / {health['lease']['held']}")

        # …and it is not a wedge: once the blip clears, the tick takes the lease it
        # never had, so the outage costs a tick rather than the life of the process.
        backend.exc = None
        run(mirror.tick())
        check(mirror._lease["held"] is True,
              f"{label}: the next tick takes the lease it never had…")
        run(mirror.flush())
        check(counts(backend).get("records") == 1,
              f"{label}: …and the queued write lands, once it is legal")
        check(mirror.state == STATE_LIVE and mirror.health()["lastError"] is None,
              f"{label}: …and only THEN is /health live: {mirror.state!r} / "
              f"{mirror.health()['lastError']!r}")

    # The belt, exercised directly: no path from the queue to `bulk_upsert`
    # re-checks the lease, so a future refactor of the branch above must still
    # find this one in its way.
    belt, belt_backend = live_mirror()
    belt._lease.update(held=False, expiresAt=None)
    belt._lease_due = lambda: False                               # the branch, disabled
    belt.enqueue([record_op(1)])
    report = run(belt.tick())
    check(report["skipped"] == "no-lease" and belt_backend.calls["bulk_upsert"] == 0,
          f"a tick with the lease branch disabled still refuses to write: {report}")

    # …and the opt-out is explicit, for the fixtures and for `start(acquire_lease=False)`:
    opted, opted_backend = live_mirror(lease_required=False)
    opted._lease.update(held=False, expiresAt=None)
    opted.enqueue([record_op(2)])
    run(opted.tick())
    check(opted_backend.calls["bulk_upsert"] == 1,
          "…while a mirror that explicitly requires no lease still drains")
    check(run(mr.Mirror(MongoConfig("u", "touch_test"), backend=MemoryBackend({}))
              .start(ensure_schema=False, acquire_lease=False)) == STATE_LIVE,
          "…which is what start(acquire_lease=False) asks for")


def test_the_breaker_holds_then_lets_the_mirror_recover():
    print("test_the_breaker_holds_then_lets_the_mirror_recover")
    clock = {"t": 1000.0}
    mirror, backend = live_mirror(monotonic=lambda: clock["t"])
    backend.fail = ms.MongoUnavailable("down")
    for _ in range(BREAKER_FAILURES):
        mirror.enqueue([record_op(1)])
        run(mirror.tick())
    check(mirror.breaker_open and mirror.state == STATE_DOWN,
          "N consecutive failures open the breaker and report 'down'")

    mirror.enqueue([record_op(2)])
    report = run(mirror.tick())
    check(report["skipped"] == "breaker" and report["held"],
          "while it holds, the tick does not touch the driver at all")
    check(backend.calls["bulk_upsert"] == BREAKER_FAILURES,
          "…proven by the call count: the held ticks made no attempt")

    clock["t"] += mr.BREAKER_HOLD_S + 1
    check(not mirror.breaker_open, f"the hold expires after {mr.BREAKER_HOLD_S}s")
    backend.fail = None
    run(mirror.tick())
    check(mirror.state == STATE_LIVE, f"…and the mirror recovers to live, got {mirror.state!r}")
    check(counts(backend).get("records") == 2,
          "…writing everything that queued up while it was down")
    # `docs/mongo.md` offers this as a contract an alert rule may read literally:
    # "a `live` mirror never publishes a `lastError`". The re-take path knew that
    # and cleared it; the ordinary degrade→recover path left the fault text
    # behind, so one transient blip made that rule fire forever.
    check(mirror.health()["lastError"] is None,
          f"…with the stale fault cleared off /health, because `live` and a "
          f"`lastError` cannot both be true: {mirror.health()['lastError']!r}")
    check(mirror.stats["write_errors"] == 0 and mirror.health()["counters"]["dropped"] == 0,
          "…while the counters, which are the durable record, are untouched")

    # An IDLE deployment recovers too — the regression that made this arm exist.
    # `_record_success` was reachable only through a tick that had batches, and a
    # work-free tick returned before the state-clearing block, so between
    # transcript writes (the steady state of a quiet session) a recovered mirror
    # kept reporting `down` with a stale `lastError` until traffic happened to
    # arrive. GD-22 asks /health to be truthful, not pessimistic.
    ticks = {"t": 1000.0}
    idle, idle_backend = live_mirror(monotonic=lambda: ticks["t"])
    idle_backend.fail = ms.MongoUnavailable("down")
    for _ in range(BREAKER_FAILURES):
        idle._lease["expiresAt"] = idle.clock().isoformat().replace("+00:00", "Z")
        run(idle.tick())
    check(idle.state == STATE_DOWN and idle.queue.qsize() == 0,
          f"a lease renewal against a dead server opens the breaker with an empty "
          f"queue: {idle.state!r}")
    ticks["t"] += mr.BREAKER_HOLD_S + 1

    # First: the fail-OPEN half of the same rule. A tick that made no round trip
    # is not evidence of anything, so it must not promote a server that is still
    # dead — the lease is not due here, so this tick touches the driver not at all.
    idle._lease["expiresAt"] = (idle.clock() + datetime.timedelta(
        seconds=mr.LEASE_TTL_S)).isoformat().replace("+00:00", "Z")
    calls = dict(idle_backend.calls)
    run(idle.tick())
    check(idle_backend.calls == calls,
          "a work-free tick with a fresh lease makes no driver call at all…")
    check(idle.state == STATE_DOWN,
          f"…and therefore promotes nothing: an empty tick is not evidence, "
          f"got {idle.state!r}")

    # Then the fail-safe half: one real round trip — the renewal, which is the
    # only one an idle process makes — and the state settles.
    idle_backend.fail = None
    idle._lease["expiresAt"] = idle.clock().isoformat().replace("+00:00", "Z")
    for _ in range(3):
        run(idle.tick())
    check(idle.state == STATE_LIVE and idle._failures == 0,
          f"a healthy, WORK-FREE tick clears the breaker's failure count and the "
          f"state: {idle.state!r}, failures={idle._failures}")
    check(idle.health()["lastError"] is None,
          f"…and the stale fault with them: {idle.health()['lastError']!r}")
    check(idle_backend.calls["bulk_upsert"] == 0 and idle.stats["written"] == 0,
          "…having written nothing at all: the recovery came from the lease "
          "renewal, which is what an idle deployment has instead of traffic")


# --- GD-29: one writer per stream ----------------------------------------
def test_two_writers_on_one_stream_and_the_second_refuses():
    print("test_two_writers_on_one_stream_and_the_second_refuses")
    shared = {}
    now = datetime.datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    first = Mirror(MongoConfig("u", "touch_test"), backend=MemoryBackend(shared),
                   clock=lambda: now)
    second = Mirror(MongoConfig("u", "touch_test"), backend=MemoryBackend(shared),
                    clock=lambda: now)
    second._lease["holderPid"] = first._lease["holderPid"] + 1

    check(run(first.acquire()) is True, "the first process takes the lease")
    check(run(second.acquire()) is False, "the second is refused (GD-29)")
    check(second.state == STATE_REFUSED, f"…and says so in /health: {second.state!r}")
    check("lease" in (second.last_error or "").lower(),
          "…with a message naming the reason")

    accepted = second.enqueue([record_op(n) for n in range(3)])
    check(accepted == 0 and second.stats["refused_no_lease"] == 3,
          "a process without the lease mirrors nothing, and counts what it refused")
    check(second.stats["refused_policy"] == 0,
          "…under the counter that names the actual cause, and not the other one")
    check(run(second.tick())["skipped"] == STATE_REFUSED,
          "…and its drainer writes nothing either")
    check(second.health()["lease"]["held"] is False,
          "…while remaining a perfectly good read server (nothing here stops that)")

    # Duplicate-key is the signature of the race, so it is COUNTED, not swallowed.
    check(second.stats["tolerated_dups"] >= 1,
          "the lost race is recorded as a tolerated duplicate (GD-29's whole point: "
          "a nonzero steady state means a second writer or a key bug)")

    # An expired lease is takeable — otherwise a crashed holder wedges the mirror.
    later = now + datetime.timedelta(seconds=mr.LEASE_TTL_S + 1)
    third = Mirror(MongoConfig("u", "touch_test"), backend=MemoryBackend(shared),
                   clock=lambda: later)
    third._lease["holderPid"] = first._lease["holderPid"] + 2
    check(run(third.acquire()) is True, "an EXPIRED lease is taken over (a crash must not wedge it)")

    # Renewal writes only the expiry, behind an equality guard on ourselves.
    check(third._lease_due() is False, "a fresh lease is not due for renewal")
    third.clock = lambda: later + datetime.timedelta(seconds=mr.LEASE_TTL_S * 0.9)
    check(third._lease_due() is True, "…and is due once most of the TTL has elapsed")
    check(run(third.acquire()) is True, "…and renews in place")


def test_a_lost_lease_is_retaken_once_the_previous_holder_expires():
    print("test_a_lost_lease_is_retaken_once_the_previous_holder_expires")
    # GD-29 requires a process that cannot hold the lease to refuse to mirror. It
    # does not require that refusal to be terminal — and it was: `refused` short-
    # circuited every tick, nothing ever called `acquire()` again, so a TRANSIENT
    # takeover (this process stalled past the 30 s TTL, another took over, then
    # exited) left a running aggregator mirroring nothing for the rest of its life
    # with no operator remedy but a restart.
    shared = {}
    now = [datetime.datetime(2026, 7, 25, 12, 0, tzinfo=UTC)]
    mono = [1000.0]

    def mirror_at():
        return Mirror(MongoConfig("u", "touch_test"), backend=MemoryBackend(shared),
                      registry=record_registry(),
                      clock=lambda: now[0], monotonic=lambda: mono[0])

    holder = mirror_at()
    loser = mirror_at()
    loser._lease["holderPid"] = holder._lease["holderPid"] + 1
    check(run(holder.acquire()) is True, "the first process takes the lease")
    check(run(loser.acquire()) is False and loser.state == STATE_REFUSED,
          "the second refuses to mirror (GD-29)")

    report = run(loser.tick())
    check(report["skipped"] == STATE_REFUSED and report["reacquired"] is False,
          "a tick inside the TTL does not re-probe: the lease is still somebody's")
    check(shared_lease_holder(shared) == holder._lease["holderPid"],
          "…and the real holder's document is untouched")

    # …and an OPEN breaker outranks the re-take: "do not touch the driver" has to
    # mean every reason one might want to, or a dead server gets one extra
    # server-selection timeout per TTL for a lease nobody can write anyway.
    loser._hold_until = mono[0] + 60
    attempts = loser.backend.calls["guarded_update"]
    mono[0] += mr.LEASE_TTL_S + 1
    report = run(loser.tick())
    check(report["skipped"] == "breaker" and loser.backend.calls["guarded_update"] == attempts,
          "a held breaker outranks the lease re-take: zero driver calls")
    loser._hold_until = 0.0

    # The holder's lease expires — it stalled, or it exited without releasing.
    now[0] += datetime.timedelta(seconds=mr.LEASE_TTL_S + 1)
    mono[0] += mr.LEASE_TTL_S + 1
    report = run(loser.tick())
    check(report["reacquired"] is True,
          f"…and the NEXT tick after a TTL re-takes the expired lease: {report}")
    check(loser.state == STATE_LIVE and loser._lease["held"] is True,
          f"…clearing the refusal rather than staying refused forever: {loser.state!r}")
    check(loser.health()["lastError"] is None,
          "…and clearing the stale refusal message off /health with it")

    accepted = loser.enqueue([record_op(0)])
    run(loser.flush())
    check(accepted == 1 and run(MemoryBackend(shared).counts()).get("records") == 1,
          "…so writes resume: the whole point of a TTL lease is that it can be re-taken")

    # The other two refusals are DELIBERATE and must never be retried on a timer:
    # an unauthenticated mongod (GD-27) and a schema Touch will not write to.
    principled, principled_backend = live_mirror()
    principled.state = STATE_REFUSED           # as start() leaves a zero-users mongod
    principled.last_error = "the mongod reports zero configured users"
    before = principled_backend.calls["guarded_update"]
    mono[0] += mr.LEASE_TTL_S * 10
    report = run(principled.tick())
    check(report["skipped"] == STATE_REFUSED and report["reacquired"] is False,
          "a refusal that is not about the lease is never retried…")
    check(principled_backend.calls["guarded_update"] == before,
          "…and touches the driver zero times, however long it waits (GD-27)")


def shared_lease_holder(state):
    """The pid recorded in the shared `writers` document, or None."""
    for doc in state.get("writers", {}).values():
        return doc.get("holderPid")
    return None


# --- GD-25 / GD-26: replay, sweep, and the one legal delete ---------------
def test_replay_of_own_output_tolerates_dups_and_changes_nothing():
    print("test_replay_of_own_output_tolerates_dups_and_changes_nothing")
    mirror, backend = live_mirror()
    ops = [record_op(n, out=n + 1) for n in range(6)]
    mirror.enqueue(ops)
    run(mirror.flush())
    first_fingerprint = ms.fingerprint(observation_state(backend.state))
    first_counts = run(backend.counts())

    for _ in range(3):
        mirror.enqueue(ops)
        run(mirror.flush())
    check(ms.fingerprint(observation_state(backend.state)) == first_fingerprint,
          "replaying the mirror's own output changes nothing (GD-25's algebra)")
    check(run(backend.counts()) == first_counts,
          f"…and creates no documents: {run(backend.counts())} == {first_counts}")

    # Order independence is what makes the requeue-on-outage path safe, so it is
    # asserted here rather than assumed: shuffled and reversed must agree.
    for variant in (list(reversed(ops)), ops[3:] + ops[:3]):
        other, other_backend = live_mirror()
        other.enqueue(variant)
        run(other.flush())
        check(ms.fingerprint(observation_state(other_backend.state)) == first_fingerprint,
              "…and a different write order produces a byte-identical fingerprint")


def test_the_generation_sweep_retracts_and_never_deletes():
    print("test_the_generation_sweep_retracts_and_never_deletes")
    mirror, backend = live_mirror()
    records = stamp_gen([record_op(n) for n in range(3)], 1)
    metas = stamp_gen([meta_op(n) for n in range(3)], 1)
    mirror.enqueue(records + metas)
    run(mirror.flush())
    check(counts(backend) == {"records": 3, "stream_meta": 3}, "the first generation lands")

    report = run(mirror.sweep({"sessionId": SESSION}, 2, reinsert=stamp_gen([meta_op(0)], 2)))
    check(report["retracted"] == 3,
          "records of an older generation are RETRACTED (an updateMany, never a delete)")
    check(all(doc.get("retracted") is True and doc.get("retractedGen") == 2
              for doc in backend.state["records"].values()),
          "…carrying `retracted:true, retractedGen:G` so the UI can hide-by-default and "
          "still show them on demand (GD-26 / D13 honesty by rendering)")
    check(run(backend.counts())["records"] == 3,
          "…and every record document still EXISTS: the mirror exists because the CLI "
          "deletes history, so deleting rewound records would re-import that destruction")
    check(report["renumbered"] == 3 and report["reinserted"] == 1,
          "positional stream_meta documents are deleted and re-inserted in one code path")
    check(run(backend.counts())["stream_meta"] == 1,
          "…leaving no aliasing garbage behind (the ONE legal delete)")

    # The scope is mandatory, and it must select by more than a generation.
    check(raises(SweepScopeError, lambda: run(mirror.sweep({}, 2))),
          "an unscoped sweep is refused — it would retract the whole collection (GD-12)")
    check(raises(SweepScopeError, lambda: run(mirror.sweep({"gen": 1}, 2))),
          "…and so is one that selects by generation alone")
    check(raises(MirrorError, lambda: run(mirror.sweep({"sessionId": SESSION}, 0))),
          "a non-positive generation is refused")
    check(raises(MirrorError, lambda: run(mirror.sweep({"sessionId": SESSION}, 2,
                                                       reinsert=[record_op(9)]))),
          "…and the re-insert takes stream_meta operations only")

    # The delete and its re-insert are ONE code path, and that is the whole of
    # what makes Touch's single delete defensible — so the empty case has to be
    # asked for. A caller who simply forgot `reinsert=` got a bare scoped delete
    # that the code presented as the legal one.
    before_deletes = backend.calls["delete_many"]
    check(raises(MirrorError, lambda: run(mirror.sweep({"sessionId": SESSION}, 3))),
          "a sweep with nothing to re-insert is refused by default (GD-26)")
    check(backend.calls["delete_many"] == before_deletes,
          "…and refused BEFORE the delete is issued, not after it")
    report2 = run(mirror.sweep({"sessionId": SESSION}, 3, allow_empty_reinsert=True))
    check(report2["reinserted"] == 0 and backend.calls["delete_many"] == before_deletes + 1,
          "…while a source that really did shrink to nothing says so out loud, in "
          "one keyword, at the one call site that needs it")

    # The delete verb itself refuses every other collection, at the backend.
    check(raises(MirrorError, lambda: run(backend.delete_many("records", {"sessionId": SESSION}))),
          "delete_many on `records` is refused by the backend, not by convention")
    check(raises(MirrorError, lambda: run(backend.drop_collection("records"))),
          "…and only the reducer-owned `derived` collection is droppable (GD-23)")


def test_the_scrub_never_corrupts_a_schema_field_or_a_ref():
    print("test_the_scrub_never_corrupts_a_schema_field_or_a_ref")
    # The attempt-2 regression, and it was silent data corruption rather than a
    # crash: SECRET_KEY_RE matches "Key", GD-24 declares `sessionKey` and
    # `stateKey`, so every mirrored ref to a slot or a custom-state head was
    # stored as `[redacted]` — while the SAME datum survived inside `refId` and
    # inside the top-level `slots.sessionKey`. Three copies of one value, two of
    # them real, and no way downstream to tell which. In an upsert-only mirror
    # `$set` of `[redacted]` wins on every replay, so it is permanent.
    slot = refs.canonical_ref({"kind": "slot", "sessionKey": "622-10028",
                               "root": "r", "name": "n", "attempt": 1})
    slot_id = refs.ref_id(slot)
    head = refs.canonical_ref({"kind": "customState", "refId": slot_id,
                               "stateKey": "progress"})
    mirror, backend = live_mirror()

    event_key = refs.ref_key({"kind": "event", "stream": "custom-state", "seq": 7})
    state_key = refs.ref_key({"kind": "customStateEvent", "stream": "custom-state", "seq": 9})
    mirror.enqueue([
        ("events", event_key,
         ms.op_set({"stream": "custom-state", "seq": 7, "source": "touch",
                    "provenance": "asserted", "kind": "slot.bound",
                    "ref": slot, "refId": slot_id})),
        ("custom_state_events", state_key,
         ms.op_set({"kind": "progress", "seq": 9, "provenance": "asserted",
                    "author": "agent:driver", "sessionKey": "622-10028",
                    "ref": head, "refId": refs.ref_id(head),
                    # …and the payload the backstop actually exists for, in the
                    # one place an agent can write arbitrary keys.
                    "data": {"custom": ms.wrap_raw({
                        "authToken": "sk-ant-api03-" + "A" * 30,
                        "key": "sk-ant-api03-" + "B" * 30,
                        "sessionKey": "622-10028", "note": "fine"})}})),
    ])
    run(mirror.flush())
    event = backend.state["events"][event_key]
    state = backend.state["custom_state_events"][state_key]

    # The property, stated as the critique stated it: the two copies of one datum
    # agree. Not "is not the redaction marker" — that would pass on any string.
    check(event["ref"]["sessionKey"] == refs.parse_ref_key("slot", event["refId"])["sessionKey"],
          f"a slot ref and its refId carry the SAME sessionKey, so GD-24's "
          f"dot-notation join resolves: {event['ref']!r} vs {event['refId']!r}")
    check(event["ref"] == slot,
          "…and the whole canonical ref survives byte-identically (refs.validate_ref "
          "already fixes its shape and value grammar, so nothing can hide in it)")
    check(state["ref"]["stateKey"] == refs.parse_ref_key("customState", state["refId"])["stateKey"],
          f"…same for a custom-state head's stateKey: {state['ref']!r}")
    check(state["sessionKey"] == "622-10028" and state["author"] == "agent:driver",
          f"declared schema fields survive at the top level too — `author` matches "
          f"the pattern on 'auth' and is GD-28's writer field: {state['author']!r}")

    # …and the backstop is still a backstop: the exemption is the DECLARED
    # vocabulary, not "anything with a familiar-looking name".
    payload = ms.unwrap_raw(state["data"]["custom"])
    check(payload["authToken"] == mr.REDACTED and payload["key"] == mr.REDACTED,
          f"a credential in an agent-asserted payload is still redacted: {payload}")
    check(payload["sessionKey"] == "622-10028" and payload["note"] == "fine",
          "…while a schema name inside the same payload is not corrupted")

    # The exemption set is DERIVED, so sp-07…sp-11 cannot re-open the hole by
    # adding a field: it is read out of the two modules that declare the schema.
    for name in ("sessionKey", "stateKey", "author"):
        check(name in mr.SCHEMA_FIELD_NAMES,
              f"`{name}` is exempt because the schema declares it, not because "
              f"somebody remembered to hand-list it")
    check(mr.SCHEMA_FIELD_NAMES >= set(refs.KIND_SPECS["slot"].required),
          "…the vocabulary comes from refs.KIND_SPECS")
    check("holderPid" in mr.SCHEMA_FIELD_NAMES and "tsRaw" in mr.SCHEMA_FIELD_NAMES,
          "…and from mongo_store.COLLECTIONS' declared types")
    # `key` is in BOTH the schema (run_nodes.key) and the value-exempt pair. The
    # value has to keep deciding, or a declared name would buy an unconditional
    # exemption for the likeliest credential holder in the store.
    check("key" in mr.SCHEMA_FIELD_NAMES,
          "`key` IS a declared schema field (run_nodes.key, GD-24)…")
    check(mr.scrub_value({"key": "sk-ant-api03-" + "C" * 30})["key"] == mr.REDACTED,
          "…and is redacted anyway when its value is not a label — the value-exempt "
          "rule is checked BEFORE the schema vocabulary")
    check(mr.scrub_value({"key": "Enter"})["key"] == "Enter",
          "…while a keystroke survives, as it did before")

    # The attempt-3 regression: the ref exemption was unconditional on the FIELD
    # NAME, justified by "refs.validate_ref fixes its shape". Nothing on the
    # write path calls validate_ref, and GD-24 deliberately makes `ref` an open
    # tail — "unknown ref shapes: retained under `ref` with `kind:"unknown"`" —
    # which `refs.canonical_ref` implements by copying every key straight
    # through. So the one subtree the backstop skipped whole was, by design, the
    # one that may carry arbitrary agent-authored keys, in an upsert-only store
    # where that is permanent.
    hostile = refs.canonical_ref({"kind": "unknown",
                                  "authToken": "sk-ant-api03-" + "A" * 30,
                                  "password": "hunter2", "note": "kept"})
    check(refs.validate_ref(hostile) == "unknown",
          "an unclassifiable ref is RETAINED, not refused — GD-11's open tail, "
          "so nothing upstream pins its keys")
    open_tail = mr.scrub_op_update(ms.op_set(
        {"stream": "s", "seq": 2, "source": "touch", "provenance": "asserted",
         "kind": "k", "ref": hostile}))["$set"]["ref"]
    check(open_tail["authToken"] == mr.REDACTED and open_tail["password"] == mr.REDACTED,
          f"…so it goes through the backstop like any other payload: {open_tail}")
    check(open_tail["kind"] == "unknown" and open_tail["note"] == "kept",
          f"…while the tail itself is still retained, keys and all (GD-24): {open_tail}")

    # …and the fix is conditional on the property the exemption CLAIMS, so the
    # declared kinds — the ones with a closed key set and per-field pins — are
    # still byte-identical, which is what keeps the dot-notation join alive.
    kept_ref = mr.scrub_op_update(ms.op_set(
        {"stream": "s", "seq": 3, "source": "touch", "provenance": "asserted",
         "kind": "k", "ref": slot}))["$set"]["ref"]
    check(kept_ref is slot,
          "a ref of a DECLARED kind is skipped whole, not merely reproduced")
    check(mr._scrub_ref({"kind": "not-a-real-kind", "authToken": "sk-ant-x"}) ==
          {"kind": "not-a-real-kind", "authToken": mr.REDACTED},
          "a ref whose declared kind refs.classify refuses is scrubbed, not trusted")
    check(mr._scrub_ref("not-a-dict") == "not-a-dict" and mr._scrub_ref({}) == {},
          "…and the degenerate shapes are total (an empty ref is `none`: nothing to hide)")

    # The attempt-4 regression, and the same door from the other side: the
    # exemption was decided by `refs.classify`, which names a ref's kind "without
    # validating its values" — a bare membership test against KIND_SPECS. So a
    # ref that merely SAYS `kind:"uuid"` was exempt whatever else it carried, and
    # the property the exemption is made of (a closed key set, per-field pins) was
    # checked by nothing on the write path. Built BY HAND here, not through
    # `canonical_ref`: that constructor calls `validate_ref` and strips the
    # extras, so a hostile ref assembled through it can never show the hole. A
    # mapper writing `{"kind": "agentId", "agentId": …, **passthrough}` is the
    # natural thing to write against a contract that says `ref` is a safe subtree.
    forged = {"kind": "uuid", "uuid": uuid_at(7),
              "authToken": "sk-ant-api03-" + "D" * 30, "password": "hunter2"}
    check(refs.classify(forged) == "uuid",
          "a hand-built ref naming a declared kind CLASSIFIES as that kind — "
          "classify reads the `kind` key and nothing else")
    check(raises(refs.RefError, refs.validate_ref, forged),
          "…while validate_ref, which enforces the closed key set, refuses it")
    scrubbed = mr.scrub_op_update(ms.op_set(
        {"stream": "s", "seq": 4, "source": "touch", "provenance": "asserted",
         "kind": "k", "ref": forged}))["$set"]["ref"]
    check(scrubbed["authToken"] == mr.REDACTED and scrubbed["password"] == mr.REDACTED,
          f"…so the backstop scrubs it: a declared kind wearing undeclared keys is "
          f"the open tail under a pinned kind's name: {scrubbed}")
    check(scrubbed["kind"] == "uuid" and scrubbed["uuid"] == uuid_at(7),
          f"…while the fields that kind DOES declare survive: {scrubbed}")
    # …and the same ref reaching `validate_op` — the registry boundary an entity
    # module's output crosses — is scrubbed there too, not only in isolation.
    at_boundary = mr.validate_op(
        ("events", refs.ref_key({"kind": "event", "stream": "s", "seq": 4}),
         ms.op_set({"stream": "s", "seq": 4, "source": "touch",
                    "provenance": "asserted", "kind": "k", "ref": dict(forged)})),
        scrub=True).update["$set"]["ref"]
    check(at_boundary["password"] == mr.REDACTED,
          f"…on the drainer's path as well, which is the copy that reaches the "
          f"store (upsert-only, so it is permanent): {at_boundary}")


def test_the_scrub_runs_once_per_operation_and_off_the_poll_loop():
    print("test_the_scrub_runs_once_per_operation_and_off_the_poll_loop")
    # GD-30 budgets Mongo at 0 ms on the critical path, and the module's own
    # contract says mapping is "pure and cheap, so it happens on the caller's
    # side of the line". The scrub is neither cheap nor needed there: measured at
    # 8.79 ms on a 550 KB document (R-44 records an 872 KB real maximum) against
    # 0.006 ms for the validation, and it ran a second time on the way out of the
    # queue — where it is free, and where it is the copy that reaches the store.
    #
    # The existing AST guard cannot see this: synchronous-but-slow contains no
    # Await node and passes it perfectly. So this asserts the property.
    blob = {"k%03d" % n: ("x" * 512) for n in range(1000)}       # ~0.5 MB of payload

    def big_op(n):
        key = refs.ref_key({"kind": "customStateEvent", "stream": "big", "seq": n})
        return ("custom_state_events", key,
                ms.op_set({"kind": "progress", "seq": n, "provenance": "asserted",
                           "data": {"custom": ms.wrap_raw(dict(blob))}}))

    # Built OUTSIDE the timed region: what is under test is the seam's own cost,
    # not the mapper's, and `wrap_raw` of half a megabyte is the mapper's.
    prepared = [big_op(n) for n in range(8)]
    calls = []
    real_scrub_op = mr.scrub_op_update

    def counting_scrub_op(update):
        calls.append(1)
        return real_scrub_op(update)

    mirror, backend = live_mirror()
    mirror.registry = {"big": Mapper("big", "tests", lambda obs: [prepared[obs["n"]]])}

    mr.scrub_op_update = counting_scrub_op
    try:
        started = time.perf_counter()
        for n in range(8):
            mirror.map_and_enqueue("big", {"n": n})
        loop_side = time.perf_counter() - started
        on_the_loop = len(calls)
        run(mirror.flush())
        total = len(calls)
    finally:
        mr.scrub_op_update = real_scrub_op

    check(on_the_loop == 0,
          f"the poll-loop seam (map_and_enqueue) never runs GD-27's deep walk: "
          f"{on_the_loop} scrub(s) for 8 half-megabyte operations")
    check(total == 8,
          f"…and the drainer runs it exactly ONCE per operation, not the two passes "
          f"the double-scrub made: {total} for 8 operations")
    # Machine-independent version of the same claim: the whole eight-operation
    # loop-side pass must cost less than ONE scrub of one of those operations.
    # With the scrub back on the loop side it costs eight of them.
    started = time.perf_counter()
    real_scrub_op(prepared[0][2])
    one_scrub = time.perf_counter() - started
    check(loop_side < one_scrub,
          f"the loop-side seam for 8 operations ({loop_side * 1000:.2f} ms) costs less "
          f"than a SINGLE scrub of one of them ({one_scrub * 1000:.2f} ms)")
    check(loop_side < TICK_BUDGET / 5,
          f"…and {loop_side * 1000:.1f} ms is a small fraction of the "
          f"{TICK_BUDGET * 1000:.0f} ms tick budget in absolute terms (GD-30)")
    check(run(backend.counts())["custom_state_events"] == 8,
          "…and every operation still reaches the store")
    stored = list(backend.state["custom_state_events"].values())[0]
    check(ms.unwrap_raw(stored["data"]["custom"])["k000"] == "x" * 512,
          "…with its payload intact, because the scrub still happened — once, on the "
          "drainer side, which is the copy that is written")


def test_the_module_has_no_delete_verbs_outside_the_one_exception():
    print("test_the_module_has_no_delete_verbs_outside_the_one_exception")
    source = (REPO / "aggregator" / "mirror.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Over the AST, not the text: the module's own prose explains which verbs it
    # refuses to have ("there is no `delete_one`, no `drop_database`"), and a
    # grep over raw source would fail on the documentation of the very rule it
    # is checking. What matters is whether the verb is ever *called*.
    forbidden = {"delete_one", "deleteOne", "find_one_and_delete", "find_one_and_replace",
                 "drop_database", "drop_indexes", "remove", "replace_one"}
    called = sorted({node.func.attr for node in ast.walk(tree)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and node.func.attr in forbidden})
    check(not called,
          f"no forbidden delete/replace verb is ever CALLED (GD-26: insert/upsert only) "
          f"— found {called}")

    # `delete_many` and `drop_collection` exist, but only as scoped, refusing
    # methods — never as a call against an arbitrary collection handle.
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr in ("delete_many", "drop_collection")]
    check(calls, "delete_many/drop_collection do exist (the exceptions are real)")

    # Every CONCRETE implementation guards them with a collection check in its
    # own body — that is what makes them exceptions rather than holes. (The
    # abstract `Backend` declares them and raises NotImplementedError, so it is
    # not part of this check: it has no collection to refuse.)
    classes = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    for class_name in ("MemoryBackend", "AsyncBackend"):
        body = classes.get(class_name)
        check(body is not None, f"{class_name} exists")
        if body is None:
            continue
        for method_name, allowed in (("delete_many", "stream_meta"), ("drop_collection", "derived")):
            method = next((n for n in body.body
                           if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                           and n.name == method_name), None)
            check(method is not None, f"{class_name}.{method_name} is implemented")
            if method is None:
                continue
            guards = [ast.dump(n) for n in ast.walk(method) if isinstance(n, ast.Compare)]
            check(any(allowed in g for g in guards),
                  f"{class_name}.{method_name} refuses every collection but "
                  f"`{allowed}`, in its own body (GD-26's wall, not a convention)")

    ttl = [n for n in ast.walk(tree) if isinstance(n, ast.Constant)
           and isinstance(n.value, str) and "expireAfterSeconds" in n.value]
    check(not ttl, "no TTL anywhere: the module never names expireAfterSeconds (GD-26)")

    # `$inc` is forbidden for accumulation — re-ingest is mandatory and deltas double.
    incs = [n for n in ast.walk(tree) if isinstance(n, ast.Constant)
            and isinstance(n.value, str) and n.value == "$inc"]
    check(not incs, "no $inc operator (GD-25: re-ingest after a rewrite would double it)")
    check("$unset" not in source,
          "…and $unset appears nowhere at all, prose included (GD-26)")


# --- R-45: backfill and rebuild ------------------------------------------
def test_backfill_is_never_live_and_refuses_a_future_timestamp():
    print("test_backfill_is_never_live_and_refuses_a_future_timestamp")
    source = (REPO / "aggregator" / "mirror.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    backfill = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "backfill")
    params = [a.arg for a in backfill.args.args + backfill.args.kwonlyargs]
    check("live" not in params,
          f"`live` is not a parameter of backfill — it is hard-coded (R-45), got {params}")
    assigns = [n for n in ast.walk(backfill) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "live" for t in n.targets)]
    check(len(assigns) == 1 and assigns[0].value.value is False,
          "…as a literal `live = False` in the body")

    mirror, backend = live_mirror()
    # R-45's own fixture, as a FILE: a transcript dated 03:00Z, and a mapper that
    # reached for `now()` — the failure a backfill actually has (SESSIONJSONL-5).
    # The refusal has to be driven by the file's mtime, not by a literal an hour
    # in the future that no real mapper would ever emit.
    historic = datetime.datetime(2026, 7, 20, 3, 0, 0, tzinfo=UTC)
    with tempfile.TemporaryDirectory() as tmp:
        source_file = Path(tmp) / "projects" / "slug" / "session.jsonl"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("{}\n", encoding="utf-8")
        stamp = historic.timestamp()
        os.utime(source_file, (stamp, stamp))

        report = run(mirror.backfill([
            ("record", {"n": 1, "ts": historic}, str(source_file)),
            # This one is the whole test: a mapper that reached for now().
            ("record", {"n": 2, "ts": datetime.datetime.now(UTC)}, str(source_file)),
        ]))

        check(report["live"] is False, "a backfill burst is never live")
        check(report["refused"] == 1,
              "an operation stamped with the IMPORT's clock is refused against the "
              "source file's 03:00Z mtime — the failure R-45 names, not a synthetic "
              "hour-in-the-future literal")
        check(mirror.stats["refused_future_ts"] == 1, "…and counted")
        check(report["stamped"] == 1 and run(backend.counts())["records"] == 1,
              "…while the historical one lands")

        stored = list(backend.state["records"].values())[0]
        check(stored.get("ingestMode") == "backfill",
              "every backfilled document is stamped ingestMode:'backfill' (a field, "
              "not a log line)")
        now = datetime.datetime.now(UTC)
        stamps = [v for v in stored.values() if isinstance(v, datetime.datetime)]
        check(stamps and all((now - s) > datetime.timedelta(hours=24) for s in stamps),
              f"R-45's acceptance: no stored ts is within 24 h of now() — {stamps}")

        # An explicit mtimes override is the same guard by another route.
        mirror2, backend2 = live_mirror()
        cutoff = datetime.datetime(2020, 1, 1, tzinfo=UTC)
        report2 = run(mirror2.backfill([("record", {"n": 3, "ts": historic}, "src")],
                                       mtimes={"src": cutoff}))
        check(report2["refused"] == 1 and counts(backend2) == {},
              "a record newer than ITS OWN source file's mtime is refused too")

    # FAIL CLOSED: no source ⇒ the guard cannot be evaluated ⇒ refuse. Widening
    # the limit to now() makes the refusal `ts > now()`, which no mapper reading
    # a historical file can ever trip: the guard would be switched off in exactly
    # the case it exists for.
    mirror3, backend3 = live_mirror()
    report3 = run(mirror3.backfill([("record", {"n": 4, "ts": datetime.datetime.now(UTC)})]))
    check(report3["refused_no_source"] == 1 and report3["refused"] == 1,
          "a 2-tuple carrying now() is REFUSED, not admitted against the import's clock")
    check(mirror3.stats["refused_no_source"] == 1, "…and counted under its own name")
    check(counts(backend3) == {}, "…so nothing lands with the import's clock on it")
    check("fails closed" in (mirror3.last_error or ""),
          f"…and /health says why: {mirror3.last_error!r}")

    # …while an operation with NO timestamp has no claim about time to check.
    mirror4, backend4 = live_mirror()
    report4 = run(mirror4.backfill([("record", {"n": 5})]))
    check(report4["stamped"] == 1 and counts(backend4)["records"] == 1,
          "an operation carrying no timestamp at all passes: there is nothing to refuse")

    # The unpack was the one line in this loop that could kill the walk it is
    # designed to survive: `len(item)` needs a SIZED object, so a streaming
    # source's generator raised TypeError, and a 4-tuple raised "too many values
    # to unpack" — both out of a method that reports every other kind of bad
    # item as a counted refusal.
    mirror6, backend6 = live_mirror()
    report6 = run(mirror6.backfill([
        ("record", {"n": 6}, "src", "an extra element"),          # ValueError, once
        42,                                                       # not even iterable
        (part for part in ("record", {"n": 7})),                  # no len(): a generator
        ("record", {"n": 8}),                                     # …and a good one after
    ]))
    check(report6["malformed"] == 2,
          f"a malformed item is a fact about ONE item, counted and named: {report6}")
    check(mirror6.stats["rejected"] == 2, "…and booked as a rejection like any other")
    check(report6["stamped"] == 2 and counts(backend6).get("records") == 2,
          f"…while the walk carries on: the generator item unpacks now, and the "
          f"items after the bad ones still land: {counts(backend6)}")
    check("malformed" in (mirror6.last_error or "") and mirror6.state == STATE_DEGRADED,
          f"…with /health saying so, and STAYING degraded through the clean ticks "
          f"that follow — like an unmapped observation, a skipped item is data no "
          f"later tick can put in the store: {mirror6.state!r} / {mirror6.last_error!r}")

    # The guard is per FILE, not per observation: a real walk yields thousands of
    # observations over hundreds of transcripts, and one stat() each would be a
    # syscall per document for an answer that cannot change during the walk.
    mirror5, _backend5 = live_mirror()
    stats = []
    real_mtime = mr._mtime
    mr._mtime = lambda path, default=None: (stats.append(path) or real_mtime(path, default))
    try:
        run(mirror5.backfill([("record", {"n": n, "ts": historic}, "one-file")
                              for n in range(25)]))
    finally:
        mr._mtime = real_mtime
    check(len(stats) == 1,
          f"25 observations from one source cost ONE stat(), not 25: {len(stats)}")


def test_the_backfill_walk_is_wired_to_the_cli():
    print("test_the_backfill_walk_is_wired_to_the_cli")
    # MAJOR 1's regression: `iter_backfill_sources` existed and was never called
    # from the module, so `--backfill` ran `--rebuild`'s source and every item
    # reached `backfill()` as a 2-tuple with no path.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in ("a.jsonl", "b.jsonl"):
            path = root / "projects" / "slug" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        (root / "projects" / "slug" / ".credentials.json").write_text("{}", encoding="utf-8")

        seen = []

        def source(path=None):
            seen.append(path)
            return [] if path is None else [{"n": 1, "file": path}]

        module = types.ModuleType("fake_entity")
        module.MIRROR_SOURCES = {"record": source}
        sys.modules["aggregator.fake_entity"] = module
        try:
            triples = list(mr.iter_backfill_observations(root, registry_modules=["fake_entity"]))
        finally:
            del sys.modules["aggregator.fake_entity"]

    check(len(triples) == 2,
          f"the walk reaches every transcript and calls the owning source per FILE: {triples}")
    check(all(len(t) == 3 for t in triples),
          "…yielding 3-tuples, so `backfill()` can name the source of every operation")
    check(all(Path(t[2]).name in ("a.jsonl", "b.jsonl") for t in triples),
          f"…and the third element is the file it came from: {[t[2] for t in triples]}")
    check(all(p is not None for p in seen),
          "…so a MIRROR_SOURCES callable is never asked for the whole corpus on the "
          "backfill path")
    check(not any(".credentials.json" in str(p) for p in seen),
          "the deny-list is applied at the SOURCE: a credentials file is never read")

    # The seam is declared, not implied: the signature sp-07…sp-11 implement
    # against is in `iter_sources`'s docstring, and `main()` calls the walk.
    source_text = (REPO / "aggregator" / "mirror.py").read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = {n.func.id for n in ast.walk(main_fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check("iter_backfill_observations" in called,
          f"main() calls the walk rather than --rebuild's source: {sorted(called)}")
    doc = ast.get_docstring(next(n for n in ast.walk(tree)
                                 if isinstance(n, ast.FunctionDef) and n.name == "iter_sources"))
    check("def source(path=None)" in doc,
          "…and the per-source signature is declared for the modules that implement it")

    # Five entity modules × N transcripts: the contract has to say that the
    # ownership decision costs a string comparison, or sp-07…sp-11 implement it by
    # opening every file five times.
    walk_doc = " ".join(ast.get_docstring(next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "iter_backfill_observations")).split())
    check("from the path alone" in walk_doc,
          "…and the walk's docstring pins HOW a source decides it does not own a path")
    check("opened or parsed" in walk_doc,
          "…naming the thing it must not do, since every source is called for every file")


def test_wipe_and_rebuild_produce_the_same_fingerprint():
    print("test_wipe_and_rebuild_produce_the_same_fingerprint")
    obs = observations(8, out=3)
    mirror, backend = live_mirror()
    run(mirror.rebuild(obs))
    before = ms.fingerprint(observation_state(backend.state))
    counts_before = counts(backend)

    # The wipe: everything Mongo holds, gone — from THE SAME store, which is
    # R-45's clause ("Mongo wipe + --rebuild ⇒ fingerprint equal to pre-wipe").
    # Two independent backends would only re-test the mapper's determinism, and
    # would pass even if rebuild left residue or diverged on a non-empty store.
    backend.state.clear()
    check(run(backend.counts()) == {}, "the store really is empty before the rebuild")
    result = run(mirror.rebuild(obs))
    after = ms.fingerprint(observation_state(backend.state))

    check(after == before, "wipe + --rebuild reproduces a byte-identical fingerprint (GD-22)")
    check(counts(backend) == counts_before,
          f"…and identical counts: {counts(backend)} == {counts_before}")
    check(result["replayed"] == 8, f"every observation was replayed, got {result['replayed']}")
    check(result["fingerprint"] == ms.fingerprint(mr.projection_state(backend.state)),
          "the report's fingerprint is the store's PROJECTION")
    # …and that qualifier is the point, not a caveat: `writers` holds GD-29's
    # lease — a pid, a boot digest and an expiry, none of it read out of a file
    # and none of it replayable — so including it would make R-45's acceptance
    # criterion false by construction, for every deployment that actually takes
    # the lease. Two processes that replayed the same files must agree.
    other, other_backend = live_mirror()
    other._lease["holderPid"] = mirror._lease["holderPid"] + 1
    run(other.rebuild(obs))
    check(run(other_backend.fingerprint()) == before,
          "a second process, holding its own lease, fingerprints the same corpus "
          "identically (the lease is runtime state, not mirrored history)")
    check("writers" in other_backend.state and mr.RUNTIME_COLLECTIONS == ("writers",),
          "…and the lease document really is there to have been excluded")

    # …and rebuilding onto a store that is NOT empty converges on the same thing,
    # because every collection but `derived` is upsert-only (GD-25).
    run(mirror.rebuild(obs))
    check(ms.fingerprint(observation_state(backend.state)) == before,
          "a rebuild onto its own output changes nothing (upsert-only, GD-25)")

    # `derived` is DROPPED, never migrated (GD-23's reducer-version rule).
    seeded, seeded_backend = live_mirror()
    seeded_backend.state["derived"] = {
        "d1": {"_id": "d1", "reducerVersion": 1, "derivedFromSeq": 5, "provenance": "derived"}}
    run(seeded.rebuild(obs))
    check(not seeded_backend.state.get("derived"),
          "--rebuild drops `derived` rather than migrating it (GD-23)")
    check(seeded_backend.calls["drop_collection"] == 1, "…exactly once")

    # `--rebuild` is the command an operator runs against a database somebody is
    # in the middle of fiddling with, and three of its driver calls were
    # unguarded — so a transient outage produced a traceback out of `asyncio.run`
    # where the method's own docstring promises a report and `/health` promises a
    # state. "Every failure is a state" has to include the server's.
    class DropFails(MemoryBackend):
        async def drop_collection(self, collection):
            self.calls["drop_collection"] += 1
            raise ms.MongoUnavailable("drop failed, the server went away")

    dropper = DropFails({})
    dropper.state["derived"] = {"d1": {"_id": "d1", "reducerVersion": 1,
                                       "derivedFromSeq": 5, "provenance": "derived"}}
    broken, _ = live_mirror(backend=dropper)
    try:
        result = run(broken.rebuild(obs))
        raised = None
    except Exception as escaped:                                 # noqa: BLE001
        result, raised = None, escaped
    check(raised is None, f"a failed drop is a report, not a traceback (got {raised!r})")
    check(result and result["droppedDerived"] is False and result["replayed"] == 0,
          f"…and the replay does NOT run: dropping is the precondition for a "
          f"faithful rebuild, so a store that is neither the old projection nor "
          f"the new one is worse than one that is untouched: {result}")
    check(dropper.state["derived"], "…`derived` is still the OLD reducer's output")
    check(broken.state in (STATE_DEGRADED, STATE_DOWN) and broken.health()["lastError"],
          f"…with /health saying why: {broken.state!r} / {broken.health()['lastError']!r}")

    class ReadFails(MemoryBackend):
        async def counts(self):
            raise ms.MongoUnavailable("read failed")

        async def fingerprint(self):
            raise ms.MongoUnavailable("read failed")

    reader, _ = live_mirror(backend=ReadFails({}))
    try:
        result = run(reader.rebuild(obs, drop_derived=False))
        raised = None
    except Exception as escaped:                                 # noqa: BLE001
        result, raised = None, escaped
    check(raised is None, f"…nor does the SUMMARY kill a rebuild that already ran "
                          f"(got {raised!r})")
    check(result and result["replayed"] == 8 and result["counts"] is None
          and result["fingerprint"] is None,
          f"…the two report-only reads come back None, beside a lastError: {result}")


def test_rebuild_survives_an_unmapped_kind_and_keeps_derived():
    print("test_rebuild_survives_an_unmapped_kind_and_keeps_derived")
    # MAJOR 2's regression: `map_and_enqueue` was not total, so the FIRST
    # observation of a kind no entity module has registered yet aborted the
    # rebuild — after `derived` had already been dropped, with nothing flushed,
    # an unhandled traceback out of asyncio.run(), and /health still saying
    # `live, lastError: null`. Every one of those is something this module
    # promises cannot happen.
    mirror, backend = live_mirror()
    backend.state["derived"] = {"d1": {"_id": "d1", "reducerVersion": 1,
                                       "derivedFromSeq": 5, "provenance": "derived"}}
    mixed = [("record", {"n": 0}), ("session", {"id": "x"}), ("record", {"n": 1})]

    result = run(mirror.rebuild(mixed))
    check(result["replayed"] == 2,
          f"every MAPPABLE observation is replayed, got {result['replayed']} of 2")
    check(run(backend.counts()).get("records") == 2, "…and reaches the store")
    check(result["unmapped"] == 1 and result["unmappedKinds"] == ["session"],
          f"…while the unmapped kind is REPORTED, not fatal: {result}")

    health = mirror.health()
    check(health["state"] == STATE_DEGRADED,
          f"/health degrades rather than claiming live, got {health['state']!r}")
    check(health["lastError"] and "session" in health["lastError"],
          f"…with a non-null lastError naming the kind: {health['lastError']!r}")
    check(mirror.stats["rejected"] == 1 and mirror.stats["unmapped"] == 1,
          "…and the observation is counted, never dropped quietly (GD-26)")

    # The destructive step is ordered defensively: a rebuild that cannot replay
    # every kind does not also destroy the reducer's collection.
    check(result["droppedDerived"] is False and backend.state.get("derived"),
          "`derived` survives a rebuild that could not replay everything (GD-23)")
    check(backend.calls["drop_collection"] == 0,
          "…because the registry is resolved BEFORE the drop, not after it")

    check(result["rejected"] == 1,
          f"…and the report carries the rejection count, not only the kind names: {result}")

    # One step over, and the case a registry-only check cannot see: a kind that IS
    # registered whose mapper raises. `Mapper.__call__` reports every mapper bug
    # as a MapperError, so resolving the registry before the drop left this shape
    # dropping `derived` and then replaying nothing — with no number in the report
    # that said so.
    boom, boom_backend = live_mirror()
    boom_backend.state["derived"] = {"d1": {"_id": "d1", "reducerVersion": 1,
                                            "derivedFromSeq": 5, "provenance": "derived"}}

    def explode(_obs):
        raise ZeroDivisionError("a mapper bug, not an unregistered kind")

    boom.registry = {"record": Mapper("record", "tests", explode)}
    boom_result = run(boom.rebuild([("record", {"n": 0}), ("record", {"n": 1})]))
    check(boom_result["rejected"] == 2 and boom_result["replayed"] == 0,
          f"a failing mapper is counted as a rejection in the report: {boom_result}")
    check(boom_result["unmapped"] == 0 and boom_result["unmappedKinds"] == [],
          "…and is NOT reported as an unmapped kind — the kind was registered")
    check(boom_result["droppedDerived"] is False and boom_backend.state.get("derived"),
          "`derived` survives a rebuild whose mappers all failed, exactly as it "
          "survives one with an unregistered kind (GD-23)")
    check(boom_backend.calls["drop_collection"] == 0,
          "…because everything is mapped BEFORE the one destructive step, not after it")
    check(boom.health()["state"] == STATE_DEGRADED and boom.health()["lastError"],
          f"…and /health says so: {boom.health()['state']!r}")

    # And `map_and_enqueue` itself is total, which is the property rebuild leans on.
    solo, solo_backend = live_mirror()
    check(solo.map_and_enqueue("session", {"id": "x"}) == 0,
          "map_and_enqueue returns 0 for an unregistered kind rather than raising")
    check(solo.state == STATE_DEGRADED and solo.last_error,
          "…having degraded /health and recorded why")
    # …while the raising form stays available for callers that want it.
    check(raises(MapperError, map_observation, record_registry(), "session", {}),
          "map_observation still raises: the wrapper is the total one, not the primitive")


def test_the_owned_suites_are_executable_like_their_siblings():
    print("test_the_owned_suites_are_executable_like_their_siblings")
    # CLAUDE.md and the sub-plan both state the convention as "each file is
    # executable and exits non-zero on failure". `run_all.sh` invokes the
    # interpreter explicitly, so a 0644 file is green either way — which is
    # exactly why the mode needs a test rather than a habit.
    for name in ("test_mirror.py", "test_mongo_deploy.py"):
        path = HERE / name
        check(path.exists() and os.access(path, os.X_OK),
              f"{name} is executable, like all eight of its siblings "
              f"(mode {oct(path.stat().st_mode & 0o777)})")
        first = path.read_text(encoding="utf-8").splitlines()[0]
        check(first.startswith("#!") and "python3" in first,
              f"…and carries the shebang that makes the mode mean something: {first!r}")


def test_cursors_round_trip_as_tailer_checkpoints():
    print("test_cursors_round_trip_as_tailer_checkpoints")
    mirror, backend = live_mirror()
    checkpoint = tailer_mod.Checkpoint(st_dev=66306, st_ino=1234567, size=8192,
                                       offset=8192, gen=3)
    run(mirror.save_cursor("session:" + SESSION, checkpoint, last_seq=41))
    loaded = run(mirror.load_cursor("session:" + SESSION))
    check(loaded is not None, "a saved cursor loads back")
    check((loaded.st_dev, loaded.st_ino, loaded.size, loaded.offset, loaded.gen)
          == (66306, 1234567, 8192, 8192, 3),
          f"…as SD-10's whole identity tuple, not a subset: {loaded}")

    # A shrink rewinds the offset legitimately — a monotonic watermark would pin
    # the cursor past the end of a truncated file forever.
    rewound = tailer_mod.Checkpoint(st_dev=66306, st_ino=1234567, size=100, offset=100, gen=4)
    run(mirror.save_cursor("session:" + SESSION, rewound))
    reloaded = run(mirror.load_cursor("session:" + SESSION))
    check(reloaded.offset == 100 and reloaded.gen == 4,
          f"a shrink REWINDS the stored cursor ($set, not $max) — got offset {reloaded.offset}")
    check(run(mirror.load_cursor("session:nonexistent")) is None,
          "an unknown stream has no cursor, and that is not an error")


# --- SD-1: the mapper registry -------------------------------------------
def test_one_kind_has_one_owner_and_mapper_output_is_validated():
    print("test_one_kind_has_one_owner_and_mapper_output_is_validated")

    class FakeModule:
        __name__ = "aggregator.fake_a"
        MIRROR_MAPPERS = {"record": lambda obs: [record_op(0)]}

    class OtherModule:
        __name__ = "aggregator.fake_b"
        MIRROR_MAPPERS = {"record": lambda obs: [record_op(1)]}

    registry = discover_mappers([FakeModule()])
    check(set(registry) == {"record"}, "a module's MIRROR_MAPPERS are discovered")
    check(raises(MapperError, discover_mappers, [FakeModule(), OtherModule()]),
          "two modules claiming one kind is refused (GD-15/SD-1: one kind, one owner)")

    check(discover_mappers(["no_such_entity_module"]) == {},
          "a module that does not exist yet is skipped silently — four of five are")
    check(discover_mappers() == {} or isinstance(discover_mappers(), dict),
          "…so today's registry is empty, and that is the normal case")

    # "not written yet" and "written, and its dependency is missing" must never
    # look alike — the second is a mirror silently writing a subset of the
    # schema. Told apart by the FULLY-QUALIFIED name: an entity module called
    # `legacy` that fails because a top-level package also called `legacy` is
    # absent produces ModuleNotFoundError(name='legacy'), whose leaf matches the
    # entity name exactly, and a leaf comparison skipped it without a word.
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "fake_entities"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "present.py").write_text(
            "MIRROR_MAPPERS = {'present': lambda obs: []}\n", encoding="utf-8")
        # An absolute import of a third-party package that shares this module's
        # own leaf name.
        (pkg / "legacy.py").write_text("import legacy\n", encoding="utf-8")
        sys.path.insert(0, tmp)
        try:
            check(set(discover_mappers(["present"], package="fake_entities")) == {"present"},
                  "a package's real entity module is discovered by name")
            check(discover_mappers(["absent"], package="fake_entities") == {},
                  "…a name with no module behind it is still skipped")
            check(raises(ModuleNotFoundError, discover_mappers,
                         ["legacy"], package="fake_entities"),
                  "…and a module whose MISSING DEPENDENCY merely shares its leaf name "
                  "raises, instead of being mistaken for one that was never written")
            check(raises(ModuleNotFoundError, lambda: list(
                      mr.iter_sources(["legacy"], package="fake_entities"))),
                  "…the same rule on the MIRROR_SOURCES seam, which had the same bug")
        finally:
            sys.path.remove(tmp)
            for name in list(sys.modules):
                if name == "fake_entities" or name.startswith("fake_entities."):
                    del sys.modules[name]

    # Output validation happens at the registry boundary, with the mapper named.
    class BadShape:
        __name__ = "aggregator.fake_c"
        MIRROR_MAPPERS = {"bad": lambda obs: [("records", "k")]}

    class BadCollection:
        __name__ = "aggregator.fake_d"
        MIRROR_MAPPERS = {"bad": lambda obs: [("not_a_collection", "k", ms.op_set({"a": 1}))]}

    class Exploding:
        __name__ = "aggregator.fake_e"
        MIRROR_MAPPERS = {"bad": lambda obs: 1 / 0}

    for module, label in ((BadShape(), "a non-triple"), (BadCollection(), "an unknown collection"),
                          (Exploding(), "a mapper that raises")):
        mapper = discover_mappers([module])["bad"]
        ok = raises(MapperError, mapper, {})
        check(ok, f"{label} is a MapperError naming the mapper, not a mystery bulk failure later")

    check(raises(MapperError, map_observation, {}, "unmapped", {}),
          "an observation nobody maps is a refusal — data is never dropped quietly (GD-26)")

    # Forbidden operators never make it out of a mapper.
    check(raises(MapperError, validate_op,
                 ("records", refs.record_key(uuid_at(1)), {"$inc": {"outputTokens": 1}})),
          "$inc is refused at the boundary (GD-25)")
    check(raises(MapperError, validate_op, ("records", "not-a-ref-key-shape", ms.op_set({"a": 1}))),
          "an _id that did not come from refs.ref_key is refused (SD-11)")

    # …and the "already scrubbed" marker cannot be FORGED by a mapper. The type
    # is the flag (a tuple subclass cannot carry a per-instance boolean), and
    # `validate_op` honours it wherever it finds one — which is what makes
    # "scrubbed once per operation" true across `_requeue`'s retries. An entity
    # module already imports `MirrorOp` from this module, so yielding a
    # `ScrubbedOp` instead is one word away, and it would exempt that module's
    # own payloads from the only backstop GD-27 has. Mapper output has by
    # definition never been through the walk, whatever its type says.
    secret = "sk-ant-api03-" + "F" * 30

    def forging_mapper(obs):
        return [mr.ScrubbedOp("custom_state_events",
                              refs.ref_key({"kind": "customStateEvent",
                                            "stream": "s", "seq": 11}),
                              ms.op_set({"kind": "progress", "seq": 11,
                                         "provenance": "asserted",
                                         "data": {"custom": ms.wrap_raw(
                                             {"authToken": secret})}}))]

    class Forger:
        __name__ = "aggregator.fake_f"
        MIRROR_MAPPERS = {"forged": forging_mapper}

    forged_op = discover_mappers([Forger()])["forged"]({})[0]
    check(not isinstance(forged_op, mr.ScrubbedOp),
          f"a mapper cannot hand back the marker: it is downgraded at the one "
          f"boundary that knows better, got {type(forged_op).__name__}")
    forger, forger_backend = live_mirror()
    forger.registry = discover_mappers([Forger()])
    forger.map_and_enqueue("forged", {})
    run(forger.flush())
    stored = ms.unwrap_raw(
        list(forger_backend.state["custom_state_events"].values())[0]["data"]["custom"])
    check(stored["authToken"] == mr.REDACTED,
          f"…so the payload is scrubbed on the way out of the queue like any "
          f"other: {stored}")
    check("ScrubbedOp" not in mr.__all__,
          "…and the marker is not exported at all: it is an internal transport "
          "type that DISABLES the backstop, not part of SD-1's mapper contract")

    # The entity modules, once they exist, must stay pure (SD-1). None do yet,
    # so this asserts the rule against whichever have landed.
    checked = 0
    for name in mr.ENTITY_MODULES:
        path = REPO / "aggregator" / f"{name}.py"
        if not path.exists():
            continue
        checked += 1
        text = path.read_text(encoding="utf-8")
        check("pymongo" not in text,
              f"{name}.py imports no pymongo (GD-21: only mongo_store and mirror may)")
    if not checked:
        skip("no entity module exists yet — SD-1's purity rule has nothing to check")


def test_health_is_r45s_block_and_carries_no_credential():
    print("test_health_is_r45s_block_and_carries_no_credential")
    import json

    secret = "sup3rs3cr3t-passw0rd"
    uri = "mongodb" + f"://touch:{secret}@127.0.0.1:27017/touch_test?authSource=touch_test"
    mirror = Mirror(MongoConfig(uri, "touch_test"), backend=MemoryBackend())
    health = mirror.health()
    for field in ("state", "lastError", "queued", "dropped", "tolerated_dups", "lease"):
        check(field in health, f"/health carries R-45's `{field}`")
    check(health["state"] in mr.STATES, f"…and a state from the closed set: {health['state']!r}")

    # The two pairs this block must never publish together. Both were real: a
    # tick wrote with no lease and reported `live` beside `lease:{held:false}`,
    # and a recovered mirror reported `live` beside the fault it had recovered
    # from. `server.py` serves this dict verbatim, so a self-contradicting block
    # is indistinguishable downstream from a healthy one.
    consistent, consistent_backend = live_mirror()
    consistent.state = mr.STATE_STARTING
    consistent.note_error(ms.MongoUnavailable("a fault, since recovered"))
    consistent.enqueue([record_op(0)])
    run(consistent.flush())
    settled = consistent.health()
    check(settled["state"] == STATE_LIVE and settled["lease"]["held"] is True,
          f"a mirror that reaches `live` holds the lease it wrote under: {settled['lease']}")
    check(settled["lastError"] is None,
          f"…and publishes no lastError, which is what makes the field readable "
          f"literally by an alert rule: {settled['lastError']!r}")
    for state in (STATE_DEGRADED, STATE_DOWN, STATE_REFUSED):
        stuck, _ = live_mirror()
        stuck.state = state
        stuck.note_error(ms.MongoUnavailable("still wrong"))
        block = stuck.health()
        check(block["state"] == state and block["lastError"],
              f"…while a mirror that is {state!r} keeps the fault that says why")
    check(consistent_backend.calls["bulk_upsert"] == 1, "…(and the write really happened)")

    # `server.py` (R-30, sp-12) serves this dict verbatim, so `docs/mongo.md`'s
    # field list IS the published contract — and it silently disagreed with the
    # code (`backend` and `db` were returned and undocumented). Asserted in both
    # directions: a field added here without a doc edit is an undocumented API,
    # and a field documented without being returned is a lie to sp-12.
    page = (REPO / "docs" / "mongo.md").read_text(encoding="utf-8")
    match = re.search(r"`mirror` block is\s*\n?`\{([^}]*)\}`", page)
    check(match is not None, "docs/mongo.md states the /health mirror block's fields")
    if match:
        documented = {name.strip() for name in match.group(1).replace("\n", " ").split(",")}
        check(documented == set(health),
              f"…and it matches health() exactly — documented-only "
              f"{sorted(documented - set(health))}, undocumented "
              f"{sorted(set(health) - documented)}")

    # `counters` is a field of that block, so the check above passed while its
    # CONTENTS drifted freely — and the contents are what an operator reads. The
    # split that made this matter: `refused` means three different things
    # (a lost lease, an unauthenticated mongod, a schema Touch will not write
    # to), all three were booked under `refused_no_lease`, and an operator
    # reading "42 refused_no_lease" against an unauthenticated single-process
    # deployment goes hunting for a second writer that does not exist.
    counters = re.search(r"`counters` carries, in full:\s*([^.]*)\.", page)
    check(counters is not None, "docs/mongo.md enumerates the /health counters")
    if counters:
        listed = {name.strip().strip("`")
                  for name in counters.group(1).replace("\n", " ").split(",")}
        check(listed == set(health["counters"]),
              f"…and the list matches health()['counters'] exactly — documented-only "
              f"{sorted(listed - set(health['counters']))}, undocumented "
              f"{sorted(set(health['counters']) - listed)}")
    for name in ("refused_no_lease", "refused_policy"):
        check(name in health["counters"], f"…including `{name}`")
    check(re.search(r"`refused_policy`.*?GD-27", page, re.S) is not None,
          "…and the page says which refusal each of the two counters is about")

    # The realistic leak: a driver exception embeds the whole URI, and /health is
    # the one unauthenticated route (GD-13).
    mirror.note_error(ms.MongoUnavailable(f"ServerSelectionTimeoutError: {uri}"))
    blob = json.dumps(mirror.health(), default=str)
    check(secret not in blob, "a password in a driver exception never reaches /health (GD-27)")
    check(uri not in blob, "…nor does the URI itself")
    check(mr.REDACTED in mirror.health()["lastError"],
          "…and the redaction is visible, so nobody thinks the field was empty")
    check("127.0.0.1" in mirror.health()["lastError"],
          "…while the host survives, because that is what an operator needs")

    # A HEALTHY mirror never publishes a lastError. `/health` is the route an
    # operator pages on, and a live mirror carrying a stale fault cries wolf on
    # the one channel that decides whether somebody gets woken up.
    class NoUsersInfo(MemoryBackend):
        async def user_count(self):
            raise ms.MongoStoreError("not authorized on admin to execute usersInfo")

    least_privilege = Mirror(MongoConfig(uri, "touch_test"), backend=NoUsersInfo({}))
    check(run(least_privilege.start()) == STATE_LIVE,
          "a least-privilege user that cannot enumerate users still reaches `live`")
    block = least_privilege.health()
    check(block["lastError"] is None,
          f"…and /health carries NO lastError for it: {block['lastError']!r}")
    check(block["notes"] and "usersInfo" in block["notes"],
          f"…the commentary lives in `notes`, which is not a fault: {block['notes']!r}")
    least_privilege.note(f"connection failed for {uri}")
    check(secret not in json.dumps(least_privilege.health(), default=str),
          "…and `notes` is redacted like everything else /health publishes")
    least_privilege.notes = block["notes"]

    # GD-13: /health is unauthenticated, so it must not publish a stable host
    # fingerprint either. `holderBoot` is a truncated digest, not the raw
    # /proc/sys/kernel/random/boot_id.
    raw = mr._boot_identity()
    boot = block["lease"]["holderBoot"]
    check(boot != raw and raw not in json.dumps(block, default=str),
          "the host's boot_id is never published — holderBoot is a hash of it (GD-13)")
    check(re.fullmatch(r"[0-9a-f]{16}", boot), f"…a stable hex digest: {boot!r}")
    check(mr._boot_id() == boot, "…and it is stable within a boot, which is all the lease needs")


def test_the_drainer_loop_wakes_on_work_and_stops_when_told():
    print("test_the_drainer_loop_wakes_on_work_and_stops_when_told")
    mirror, backend = live_mirror()

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(mirror.run(stop=stop, interval=5.0))
        mirror.enqueue([record_op(n) for n in range(4)])
        started = time.monotonic()
        for _ in range(200):
            await asyncio.sleep(0.005)
            if run_counts(backend).get("records") == 4:
                break
        elapsed = time.monotonic() - started
        stop.set()
        mirror._wakeup.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            return elapsed, False
        return elapsed, True

    def run_counts(b):
        return {name: len(bucket) for name, bucket in b.state.items()}

    elapsed, stopped = run(scenario())
    check(run_counts(backend).get("records") == 4, "the drainer wrote what was enqueued")
    check(elapsed < 5.0,
          f"…waking immediately on work rather than sleeping the interval ({elapsed:.3f}s)")
    check(stopped, "…and the stop event ends the loop")


# --- live arm (skips cleanly) ---------------------------------------------
def live_database():
    """(uri, name) against `TOUCH_MONGO_URI`, or (None, reason)."""
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


def test_live_mongod_arm():
    print("test_live_mongod_arm")
    uri, name = live_database()
    if uri is None:
        skip(f"live mirror arm: {name}")
        return
    check(name.startswith("touch_test_"), f"the live arm uses a name it constructed: {name}")
    try:
        run(_live_checks(uri, name))
    finally:
        client = ms.open_client(uri)
        check(name.startswith("touch_test_"),
              f"dropping only the database this test constructed: {name} (GD-27/GD-12)")
        client.drop_database(name)
        client.close()


async def _live_checks(uri, name):
    from aggregator.mirror import AsyncBackend

    backend = await AsyncBackend.connect(uri, name)
    mirror = Mirror(MongoConfig(uri, name), backend=backend, registry=record_registry())
    state = await mirror.start()
    check(state == STATE_LIVE, f"the mirror reaches 'live' against a real mongod, got {state!r}")
    check(mirror.health()["lease"]["held"], "…holding the GD-29 writer lease")

    ops = [record_op(n, out=n + 1) for n in range(6)]
    mirror.enqueue(ops)
    await mirror.flush()
    counts = await backend.counts()
    check(counts.get("records") == 6, f"a real bulk_write lands every document: {counts}")

    fingerprint = await backend.fingerprint()
    for _ in range(2):
        mirror.enqueue(ops)
        await mirror.flush()
    check(await backend.fingerprint() == fingerprint,
          "replaying the mirror's own output against a REAL server changes nothing (GD-25)")
    check((await backend.counts()).get("records") == 6, "…and creates no documents")

    # The model is only worth having if it agrees with the server it models.
    memory = MemoryBackend()
    memory_mirror = Mirror(MongoConfig("u", name), backend=memory, registry=record_registry())
    memory_mirror.state = STATE_LIVE
    memory_mirror.enqueue(ops)
    await memory_mirror.flush()
    live_state = await backend._read_state()
    check(ms.fingerprint(observation_state(live_state))
          == ms.fingerprint(observation_state(memory.state)),
          "MemoryBackend and a real mongod produce the SAME fingerprint — which is what "
          "makes the bare-checkout suite meaningful")

    # GD-26 against the server: the sweep retracts, and the retraction is real.
    # An UNSTAMPED record is untouched first — `{gen:{$lt:2}}` does not match a
    # document with no `gen`, which is Mongo's missing-field semantics and the
    # rule `MemoryBackend._matches` reproduces. An incremental append tick never
    # bumps a generation, so its records must survive a later sweep untouched.
    # `allow_empty_reinsert`: this sweep is about `records`, and the file it
    # stands for did not renumber anything — so there is nothing to put back, and
    # GD-26's "the delete and its re-insert are one code path" has to be said out
    # loud rather than defaulted into.
    await mirror.sweep({"sessionId": SESSION}, 2, allow_empty_reinsert=True)
    untouched = await mirror._find_one("records", refs.record_key(uuid_at(0)))
    check(untouched and "retracted" not in untouched,
          "a record with no generation is not swept (incremental appends never retract)")

    mirror.enqueue(stamp_gen([record_op(n) for n in range(6)], 1))
    await mirror.flush()
    await mirror.sweep({"sessionId": SESSION}, 2, allow_empty_reinsert=True)
    remaining = await backend.counts()
    check(remaining.get("records") == 6, "the sweep retracted rather than deleted, server-side")
    doc = await mirror._find_one("records", refs.record_key(uuid_at(0)))
    check(doc and doc.get("retracted") is True and doc.get("retractedGen") == 2,
          "…and the retraction fields are on the stored document")

    # The lease, against a real unique index: a second holder is refused.
    other = Mirror(MongoConfig(uri, name), backend=backend)
    other._lease["holderPid"] = mirror._lease["holderPid"] + 1
    check(await other.acquire() is False,
          "a second writer is refused by the real conditional write (GD-29)")

    # R-45's wipe+rebuild clause, against the REAL server — the memory arm can
    # only model `drop_collection` and the replay's landing on its own output.
    # This is also the only exercise of the `dropCollection` grant docs/mongo.md
    # §2 hands the least-privilege role.
    def replayable(state):
        # `derived` is reducer-owned and dropped by definition, so it is not part
        # of what a rebuild must reproduce (GD-23); `writers` is runtime.
        return {name: bucket for name, bucket in observation_state(state).items()
                if name != "derived"}

    before = ms.fingerprint(replayable(await backend._read_state()))
    # `derived` ids are reducer-owned, so the seed goes in straight rather than
    # through a mapper (mongo_store exempts the collection from the ref-key rule
    # for exactly that reason).
    await backend.db["derived"].insert_one(
        {"_id": "d1", "reducerVersion": "1", "derivedFromSeq": 5, "provenance": "derived"})
    check((await backend.counts()).get("derived") == 1, "a derived document exists to drop")
    observed = [("record", {"n": n, "out": n + 1}) for n in range(6)]
    result = await mirror.rebuild(observed)
    check(result["droppedDerived"] is True and (await backend.counts()).get("derived", 0) == 0,
          "--rebuild drops the reducer-owned collection at the server (GD-23), which is "
          "the grant docs/mongo.md §2 gives the role")
    check(ms.fingerprint(replayable(await backend._read_state())) == before,
          "…and the replay reproduces a byte-identical fingerprint against a real "
          "mongod (GD-22's whole claim)")
    check(result["unmapped"] == 0, f"…with nothing unmapped: {result['unmappedKinds']}")

    # GD-24's mandated join path, at the SERVER: `{"ref.sessionKey": …}`. This is
    # what the redaction backstop broke — a `ref` stored as `[redacted]` matches
    # nothing under dot notation, and in an upsert-only mirror no later re-ingest
    # repairs it. The memory arm proves the document; only this proves the query.
    slot = refs.canonical_ref({"kind": "slot", "sessionKey": "622-10028",
                               "root": "r", "name": "n", "attempt": 1})
    event_key = refs.ref_key({"kind": "event", "stream": "custom-state", "seq": 7})
    mirror.enqueue([("events", event_key,
                     ms.op_set({"stream": "custom-state", "seq": 7, "source": "touch",
                                "provenance": "asserted", "kind": "slot.bound",
                                "ref": slot, "refId": refs.ref_id(slot)}))])
    await mirror.flush()
    joined = await backend.db["events"].find_one({"ref.sessionKey": "622-10028"})
    check(joined is not None and joined.get("_id") == event_key,
          "a mirrored ref resolves under GD-24's dot-notation join against a real "
          f"mongod: {joined and joined.get('ref')}")

    await backend.close()


def main():
    for test in (
        test_pymongo_is_lazy_and_its_absence_is_a_state,
        test_enqueue_never_blocks_never_raises_and_never_awaits,
        test_queue_full_drops_mirror_writes_and_degrades,
        test_dead_port_never_stalls_and_reports_down,
        test_a_transient_outage_requeues_rather_than_losing_writes,
        test_a_driver_surprise_on_the_lease_path_degrades_instead_of_killing_the_drainer,
        test_a_renewal_that_failed_stops_the_tick_even_when_it_was_not_a_refusal,
        test_a_mirror_that_never_took_the_lease_writes_nothing,
        test_the_breaker_holds_then_lets_the_mirror_recover,
        test_two_writers_on_one_stream_and_the_second_refuses,
        test_a_lost_lease_is_retaken_once_the_previous_holder_expires,
        test_replay_of_own_output_tolerates_dups_and_changes_nothing,
        test_the_generation_sweep_retracts_and_never_deletes,
        test_the_scrub_never_corrupts_a_schema_field_or_a_ref,
        test_the_scrub_runs_once_per_operation_and_off_the_poll_loop,
        test_the_module_has_no_delete_verbs_outside_the_one_exception,
        test_backfill_is_never_live_and_refuses_a_future_timestamp,
        test_the_backfill_walk_is_wired_to_the_cli,
        test_wipe_and_rebuild_produce_the_same_fingerprint,
        test_rebuild_survives_an_unmapped_kind_and_keeps_derived,
        test_the_owned_suites_are_executable_like_their_siblings,
        test_cursors_round_trip_as_tailer_checkpoints,
        test_one_kind_has_one_owner_and_mapper_output_is_validated,
        test_health_is_r45s_block_and_carries_no_credential,
        test_the_drainer_loop_wakes_on_work_and_stops_when_told,
        test_live_mongod_arm,
    ):
        test()
    print()
    for message in skips:
        print(f"skipped: {message}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("all mirror (R-45) tests passed")


if __name__ == "__main__":
    main()
