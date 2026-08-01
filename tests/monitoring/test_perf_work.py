#!/usr/bin/env python3
"""Work-based perf regression guards for monitor_server.py (M13, GD-G).

Run: ``python3 test_perf_work.py`` (stdlib only, no pytest, non-zero on failure).

**Nothing here asserts on elapsed time.** A wall-clock threshold on a shared
machine is a test that either flakes or gets deleted; what actually regressed
when the monitor became slow is *work* — bytes re-read, events re-folded,
frames built, blobs re-framed, snapshots rebuilt — and every one of those is a
counter the server already keeps for ``/health``. So the assertions are exact
integers: read ONE line's bytes after appending one line, build ONE blob for
two clients, ship a bounded snapshot for an unbounded stream.

The socket harness (ephemeral in-process server + hand-rolled RFC 6455 probe)
is reused from ``test_ws_e2e.py`` rather than copied — one client, one place to
fix. Importing it does not run its cases (they are behind ``__main__``), and it
brings its own throwaway ``$ORCH_STATE_DIR``, so nothing here touches a real
task folder or the live monitor on 8931.

Sizes: 50k events is the "large live stream" case (4x the 12,334-event corpus
that motivated this work) and 100k is the stated headroom target. Both are
generated, not committed — ``gen_stream.py`` reproduces either from two
integers.
"""
import asyncio
import inspect
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gen_stream                    # noqa: E402
import test_ws_e2e as e2e            # noqa: E402  (socket harness, not its cases)

ms = e2e.ms
MODULE_PATH = e2e.MODULE_PATH
BIG, HUGE = 50_000, 100_000
_CACHE: dict = {}


def _stream_file(name: str, n: int, seed: int = 20260727) -> str:
    """A generated stream on disk, built once per process (they are big)."""
    key = (name, n, seed)
    if key in _CACHE:
        return _CACHE[key]
    path = os.path.join(e2e._STATE_DIR, name)
    gen_stream.write_stream(path, n, seed=seed)
    _CACHE[key] = path
    return path


def _fresh(path: str):
    ms.Stream._REGISTRY.pop(os.path.abspath(path), None)
    return ms.Stream.get(path)


def test_appending_one_line_reads_one_line():
    """The headline: a live poll costs the appended bytes, not the file.

    ``/tasks`` used to re-read and re-parse the WHOLE stream on every append
    (the cache key is ``(mtime_ns, size)``, and both move on every append):
    48 ms per poll for a 5.6 MB stream, per stream, forever, growing linearly.
    Here the same append costs exactly its own bytes and exactly one folded
    event — the property that has to hold at 50k as well as at 50.
    """
    path = _stream_file("perf-50k.jsonl", BIG)

    async def go():
        stream = _fresh(path)
        await stream.refresh()
        assert stream.fold.ev_count == BIG, stream.fold.ev_count
        assert stream.bytes_read == os.path.getsize(path), stream.bytes_read
        cold_reads, cold_folds = stream.bytes_read, stream.events_folded

        line = json.dumps({"ts": "2026-07-29T09:00:00.000Z", "plan": "research",
                           "stage": "tokens", "state": "info", "quiet": True,
                           "tokens": {"in": 7, "out": 1},
                           "detail": "one more tick"})
        with open(path, "ab") as f:
            f.write((line + "\n").encode())
        await stream.refresh()
        assert stream.bytes_read - cold_reads == len(line) + 1, \
            stream.bytes_read - cold_reads
        assert stream.events_folded - cold_folds == 1, \
            stream.events_folded - cold_folds
        assert stream.fold.ev_count == BIG + 1

        # ...and a poll of an UNCHANGED stream reads nothing at all: that is
        # the `size == offset` no-op, the thing that makes an idle task free.
        for _ in range(5):
            await stream.refresh()
        assert stream.bytes_read - cold_reads == len(line) + 1
        assert stream.events_folded - cold_folds == 1
        assert stream.refreshes >= 7, stream.refreshes

    asyncio.run(go())


def test_incremental_fold_is_exact_at_scale():
    """50k events folded byte-by-byte still equal the reference full scan.

    Cheap is only useful if it is also right: the same equality
    ``test_server.py`` asserts at 4k, at the size where an off-by-one in the
    offset arithmetic would have somewhere to hide.
    """
    path = _stream_file("perf-50k.jsonl", BIG)

    async def go():
        stream = _fresh(path)
        await stream.refresh()
        states, last, tok, failures = ms.replay_plan_states(path)
        assert stream.fold.plan_states == states
        assert stream.fold.last == last
        assert stream.fold.tok == tok, (stream.fold.tok, tok)
        assert stream.fold.parse_failures == failures == 0
        assert stream.status()["status"] == ms.task_status(path)["status"]

    asyncio.run(go())


def test_two_clients_share_one_replay_build_and_one_snapshot_build():
    """M7/M9 — the shared blob and the cached snapshot, counted.

    Every connect used to re-read, re-split and re-frame the entire stream for
    that client alone. Now the framed history is built once per byte and the
    snapshot once per ``(sig, offset)``: a second tab is free, and so is a
    reconnect at the same offset.
    """
    async def run():
        server, port = await e2e._serve()
        try:
            lines = gen_stream.make_stream(1_200)
            e2e._write_events(lines)
            stream = _fresh(ms.EVENTS)

            a = await e2e.WsProbe.connect(port)
            await a.pull(len(lines))
            assert len(a.texts()) == len(lines), len(a.texts())
            assert stream.blob_framed == len(lines), stream.blob_framed
            b = await e2e.WsProbe.connect(port)
            await b.pull(len(lines))
            assert len(b.texts()) == len(lines), len(b.texts())
            assert stream.blob_framed == len(lines), \
                "the second client re-frames nothing"
            assert stream.blob_lines == len(lines), stream.blob_lines
            assert stream.blob_offset == os.path.getsize(ms.EVENTS)

            c = await e2e.WsProbe.connect(port, query="?v=2")
            await c.pull(3)
            assert stream.snap_builds == 1, stream.snap_builds
            d = await e2e.WsProbe.connect(port, query="?v=2")
            await d.pull(3)
            assert stream.snap_builds == 1, "one snapshot per (sig, offset)"
            snap_c = [json.loads(p) for p in c.texts()][1]
            snap_d = [json.loads(p) for p in d.texts()][1]
            assert snap_c == snap_d, "and both clients get the same bytes"
        finally:
            await e2e._shutdown(server)

    asyncio.run(run())


def test_snapshot_stays_bounded_at_the_100k_headroom_target():
    """M13/GD-F — the snapshot is bounded by the budget, not by the stream.

    82 % of an unbudgeted snapshot is log lines, and a PER-PLAN cap does not
    bound them: the plan count is what grows. At 100k events the payload must
    still be a small multiple of the 12k one, and the truncation must be
    disclosed rather than silent.
    """
    small = _stream_file("perf-12k.jsonl", 12_000)
    huge = _stream_file("perf-100k.jsonl", HUGE)

    async def go():
        s_small = _fresh(small)
        await s_small.refresh()
        b_small = s_small.snapshot_bytes()
        s_huge = _fresh(huge)
        await s_huge.refresh()
        b_huge = s_huge.snapshot_bytes()

        assert s_huge.fold.ev_count == HUGE, s_huge.fold.ev_count
        stream_bytes = os.path.getsize(huge)
        assert len(b_huge) < stream_bytes / 20, (len(b_huge), stream_bytes)
        # The multiple, and why it is 3.15 rather than 3: the LOG half of the
        # payload is budgeted and does not grow with the stream, but the AGENT
        # ROWS are not budgeted and never were — 162 rows at 12k events against
        # 1,351 at 100k, because the plan count is what grows. GD-LC-9 added one
        # additive key (`ctx`, null when the fold holds no reading) to each of
        # those rows, ~13 B on a ~140 B row: measured 617,122 -> 634,840 B here
        # against a 206,568 -> 208,685 B small snapshot, i.e. 2.99x -> 3.04x.
        # The bound is a ceiling on stream-driven growth, not a byte pin — but
        # it is deliberately tight, so re-measure rather than relax it again:
        # 3.15 is the measured 3.042 plus ~3.5 % slack, NOT a round number
        # chosen for comfort. A 3.5 would leave 15 % of unbudgeted headroom in
        # the one assertion that watches the unbudgeted half of the snapshot,
        # and the next additive row key would slip inside it unnoticed —
        # exactly the regression this bound exists to catch.
        assert len(b_huge) < 3.15 * len(b_small), (len(b_huge), len(b_small))
        snap = json.loads(b_huge)
        shipped = sum(len(p["log"]) for _i, p in snap["plans"])
        assert shipped <= ms.LOG_BUDGET_LINES, shipped
        assert snap["logTruncated"] is True, "a cut log is always disclosed"
        assert sum(p["logTotal"] for _i, p in snap["plans"]) > shipped
        # the timeplan is carried as DERIVED segments, never as raw ticks
        tp = snap["timeplan"]
        assert len(tp["segs"]) < 500, len(tp["segs"])
        # the reorder window is bounded by COUNT as well as by time: at 100k
        # events a dense burst would otherwise ship thousands of raw ticks
        assert len(tp["tailTicks"]) <= ms.TP_TAIL_MAX, len(tp["tailTicks"])
        assert isinstance(tp["open"], list), tp["open"]   # array of pairs
        assert snap["evCount"] == HUGE

    asyncio.run(go())


def test_every_growing_collection_is_capped_and_the_cap_is_reachable():
    """PRIOR-ART-TOUCH-12 guard shape — nothing here may grow without a bound.

    Ported from ``tests/test_touch_frontend.py``'s house rule: every collection
    that grows with the stream carries an explicit cap, and each cap is a named
    constant (so it is reviewable and changeable in one place) rather than a
    literal buried in a loop.
    """
    src = open(MODULE_PATH).read()
    caps = {"LOG_KEEP_PER_PLAN": ms.LOG_KEEP_PER_PLAN,
            "LOG_BUDGET_LINES": ms.LOG_BUDGET_LINES,
            "LOG_BUDGET_BYTES": ms.LOG_BUDGET_BYTES,
            "MAX_TICK_EVENTS": ms.MAX_TICK_EVENTS,
            "MAX_PENDING_EVENTS": ms.MAX_PENDING_EVENTS,
            "BATCH_MAX_EVENTS": ms.BATCH_MAX_EVENTS,
            "BATCH_MAX_BYTES": ms.BATCH_MAX_BYTES,
            "WRITE_CHUNK": ms.WRITE_CHUNK,
            "REPLAY_WINDOW": ms.REPLAY_WINDOW,
            "SCAN_WINDOW": ms.SCAN_WINDOW,
            "TP_TAIL_MS": ms.TP_TAIL_MS,
            "TP_TAIL_MAX": ms.TP_TAIL_MAX,
            "BLOB_IDLE_SECS": ms.BLOB_IDLE_SECS}
    for name, value in caps.items():
        assert f"{name} = " in src, f"{name} must be a named constant"
        assert value > 0, (name, value)
    # the per-plan log really is a bounded deque, not a list that "should" be
    assert "maxlen=LOG_KEEP_PER_PLAN" in src, "the log buffer must be a ring"
    fold = ms.Fold()
    for i in range(ms.LOG_KEEP_PER_PLAN + 50):
        fold.apply(json.dumps({"ts": "2026-07-28T08:00:00.000Z", "plan": "p",
                               "stage": "impl", "state": "running",
                               "detail": f"line {i}"}).encode())
    plan = fold.plans["p"]
    assert len(plan["log"]) == ms.LOG_KEEP_PER_PLAN, len(plan["log"])
    assert plan["logTotal"] == ms.LOG_KEEP_PER_PLAN + 50, plan["logTotal"]
    assert plan["log"][0]["detail"].endswith(str(ms.LOG_KEEP_PER_PLAN + 49)), \
        "newest-first: the ring drops the OLDEST line"


def test_the_expensive_read_is_coalesced_and_backs_off_when_quiet():
    """SERVER-READ-3/12 + WS-PROTOCOL-14 — one read per window, per stream.

    Two guards in one: N dashboard tabs on one task must not multiply the same
    scan (the lock + min-refresh stamp IS the single-flight), and a stream that
    stopped moving must stop being polled twice a second forever.
    """
    path = _stream_file("perf-coalesce.jsonl", 4_000)

    async def go():
        stream = _fresh(path)
        await stream.refresh()
        refreshes = stream.refreshes
        # ten concurrent pokes inside one min-refresh window: at most one read
        await asyncio.gather(*[stream.refresh(ms.REFRESH_MIN_SECS)
                               for _ in range(10)])
        assert stream.refreshes - refreshes <= 1, stream.refreshes - refreshes

        assert stream.poll_interval() == ms.POLL_SECS, stream.poll_interval()
        stream.last_change -= ms.IDLE_AFTER_SECS + 1
        assert stream.poll_interval() == ms.IDLE_POLL_SECS, stream.poll_interval()
        # any change resets the cadence to the live contract
        with open(path, "ab") as f:
            f.write((json.dumps({"ts": "2026-07-29T10:00:00.000Z", "plan": "p",
                                 "stage": "impl", "state": "running",
                                 "detail": "moved"}) + "\n").encode())
        await stream.refresh()
        assert stream.poll_interval() == ms.POLL_SECS, stream.poll_interval()

    asyncio.run(go())


def test_replay_is_written_in_drained_chunks():
    """SERVER-READ-5 — backpressure instead of a per-client memory balloon.

    Source-text guard beside the behavioural ones: the two paths that can write
    megabytes (the v1 framed history and the v2 snapshot) must slice and
    ``drain()``, or a single stalled browser makes the server buffer the whole
    stream for it.
    """
    src = open(MODULE_PATH).read()
    v1 = inspect.getsource(ms._stream_v1)
    assert "WRITE_CHUNK" in v1 and "await writer.drain()" in v1, v1
    chunked = inspect.getsource(ms.write_chunked)
    assert "WRITE_CHUNK" in chunked and "await writer.drain()" in chunked
    assert "closed.is_set()" in chunked, "abort cleanly when the client leaves"
    v2 = inspect.getsource(ms._stream_v2)
    assert "write_chunked" in v2, v2
    assert "await asyncio.to_thread" in src, "file reads stay off the event loop"
    # ...and the raw-history path reads in WINDOWS. `read_records(path, 0)`
    # would split a whole 19 MB stream into a list of bytes objects, inside the
    # stream lock, on the one route an operator reaches for when the snapshot
    # already failed them (m-2).
    replay = inspect.getsource(ms._write_replay)
    assert "read_window" in replay, replay
    assert "read_records" not in v2, "snap=0 must not materialise the stream"


def test_the_cold_fold_walks_a_big_stream_in_bounded_steps():
    """n-4 — a reset is the one path that could still read a whole stream.

    Every OTHER long read here was already windowed (``read_window`` for the
    ``snap=0`` replay), but the cold fold and every identity reset went through
    ``f.read()`` on the whole file and handed back one list of every record:
    ~23 MB of file plus the list overhead at this size, ~45 MB at the 100k
    headroom target, live at once on the thread pool. Work-based assertion: no
    single step yields more than one window of bytes, and the walk still ends
    exactly at the file's size.
    """
    path = _stream_file("perf-50k.jsonl", BIG)
    size = os.path.getsize(path)
    assert size > 4 * ms.SCAN_WINDOW, (size, ms.SCAN_WINDOW)

    steps, offset, known, widest = 0, 0, {}, 0
    while True:
        res = ms._scan(path, offset, known)
        base = 0 if res["reset"] else offset
        widest = max(widest, res["new_offset"] - base)
        offset = res["new_offset"]
        known = {"sig": res["sig"], "sig_short": res["sig_short"],
                 "dev_ino": res["dev_ino"], "mtime_ns": res["mtime_ns"],
                 "tail": res["tail"]}
        steps += 1
        if not res["more"]:
            break
        assert steps < 200, "the walk must terminate"
    assert offset == size, (offset, size)
    assert widest <= ms.SCAN_WINDOW, (widest, ms.SCAN_WINDOW)
    assert steps >= size // ms.SCAN_WINDOW, (steps, size // ms.SCAN_WINDOW)


def test_the_history_blob_is_evicted_when_nothing_is_reading_it():
    """M7 — a blob is a cache, not a leak: it goes away with its last reader."""
    path = _stream_file("perf-12k.jsonl", 12_000)

    async def go():
        stream = _fresh(path)
        await stream.refresh()
        async with stream.sync_lock():
            await stream.ensure_blob()
        assert len(stream.blob) > 0 and stream.blob_builds == 1
        stream.blob_idle_at = None
        stream.blob_subs = 1                 # a reader is attached: never evict
        await stream.refresh()
        stream.blob_idle_at = ms.time.monotonic() - ms.BLOB_IDLE_SECS - 1
        await stream.refresh()
        assert len(stream.blob) > 0, "an attached reader keeps its blob"
        stream.blob_subs = 0
        stream.blob_idle_at = ms.time.monotonic() - ms.BLOB_IDLE_SECS - 1
        await stream.refresh()
        assert len(stream.blob) == 0 and stream.blob_offset == 0, "evicted"
        assert stream.blob_lines == 0, stream.blob_lines
        assert stream.fold.ev_count == 12_000, "the FOLD is not a cache"
        assert stream.blob_framed == 12_000, stream.blob_framed
        # ...and a blob rebuilt after an eviction counts ITS OWN lines. That
        # count is what a v1 replay reports into events_sent, so a stale one
        # doubles the number the dashboard's own stats line prints.
        async with stream.sync_lock():
            await stream.ensure_blob()
        assert stream.blob_lines == 12_000, stream.blob_lines
        assert stream.blob_framed == 24_000, stream.blob_framed
        assert stream.blob_offset == os.path.getsize(path)

    asyncio.run(go())


def run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e!r}")
    if failed:
        print(f"\n{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
