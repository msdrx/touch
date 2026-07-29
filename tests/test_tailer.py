#!/usr/bin/env python3
"""Stdlib-only tests for aggregator/tailer.py (R-23). Run as
`python3 test_tailer.py`; exits non-zero on failure. No pytest, no runner.

The item's test list is "P4's fixture list + truncation/rotation cases + torn
tail". Each of those is one function below, plus the two rules the plan calls
out as *not* inherited from the monitoring module: the `size < offset` branch
RUNSTATE-15 found missing, and the O(bytes-appended) budget of GD-30 (asserted
as a byte counter, never as a duration — a timing assertion on a shared sandbox
is a flake, not a test).

Real fixture corpus (tests/fixtures/, frozen by sp-02) is used read-only for the
"does it agree with reality" arm: a journal and a transcript are tailed
line-by-line and compared against a plain read of the same file.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The canonical trees are named through `tests/_roots.py`, never by a
# literal under REPO: GD-U1 moves them and this is the single flip point.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))

from aggregator.tailer import (           # noqa: E402  (path juggling first)
    COMPACT_TMP_PREFIX,
    REASON_APPEND,
    REASON_COMPACTING,
    REASON_MISSING,
    REASON_NEW,
    REASON_OVERSIZE_LINE,
    REASON_RESYNC,
    REASON_ROTATED,
    REASON_SHRUNK,
    REASON_UNCHANGED,
    Checkpoint,
    CompactionInProgress,
    Tailer,
    compaction_in_progress,
    read_complete_lines,
    split_lines,
    tail_once,
)

FIX = REPO / "tests" / "fixtures"
failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def write(path, data, mode="wb"):
    with open(path, mode) as fh:
        fh.write(data if isinstance(data, bytes) else data.encode())


# --- torn tails (GD-20 copy-verbatim) -------------------------------------
def test_torn_tail():
    print("test_torn_tail")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "events.jsonl")
        write(p, b'{"a":1}\n{"a":2}\n{"a":3')          # third line incomplete
        res = tail_once(p)
        check([l.text for l in res.lines] == ['{"a":1}', '{"a":2}'],
              "only complete lines are returned")
        check(res.deferred == len('{"a":3'), f"partial tail deferred ({res.deferred} bytes)")
        check(res.checkpoint.offset == 16, "offset stops at the last newline, not at EOF")

        write(p, b'}\n', "ab")                          # the line completes
        res2 = tail_once(p, res.checkpoint)
        check([l.text for l in res2.lines] == ['{"a":3}'],
              "the completed line is emitted exactly once, whole")
        check(res2.reason == REASON_APPEND and not res2.reset,
              "completing a torn line is an ordinary append, not a reset")
        check(res2.deferred == 0, "nothing left deferred")


def test_multibyte_split_across_ticks():
    print("test_multibyte_split_across_ticks")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "t.jsonl")
        write(p, b'{"s":"caf\xc3')                      # half of "é", no newline
        res = tail_once(p)
        check(res.lines == [] and res.deferred == 10,
              "a truncated multi-byte sequence is deferred, never decoded early")
        write(p, b'\xa9"}\n', "ab")
        res2 = tail_once(p, res.checkpoint)
        check(json.loads(res2.lines[0].text)["s"] == "café",
              "the character survives the tick boundary intact")


def test_split_lines_positions():
    print("test_split_lines_positions")
    data = b'a\n\nbb\nccc'                              # note the blank line
    lines, consumed, line_no = split_lines(data, 100, 0)
    check([l.line_no for l in lines] == [1, 2, 3],
          "blank lines are returned and counted (positional ids must not shift)")
    check([l.text for l in lines] == ["a", "", "bb"], "text excludes the newline")
    check([l.byte_offset for l in lines] == [100, 102, 103],
          "byte offsets are absolute, from the read's start offset")
    check(consumed == 6 and line_no == 3, "consumed covers whole lines only")
    check(split_lines(b"no newline", 0, 0) == ([], 0, 0),
          "a buffer with no newline consumes nothing")
    # `if line:` / `[l for l in lines if l]` is the most natural ingest loop
    # anyone writes. A falsy blank line there would drop the record and shift
    # every line number after it — the exact failure the accounting exists to
    # prevent (GD-24 keys stream_meta by <sessionId>#<line:08d>).
    check(all(lines) and [l.line_no for l in lines if l] == [1, 2, 3],
          "a blank line is still a truthy TailLine, so filtering cannot shift line numbers")


# --- checkpoint identity: the two reset triggers (D6, RUNSTATE-15) --------
def test_in_place_truncation():
    print("test_in_place_truncation")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "journal.jsonl")
        write(p, b"one\ntwo\nthree\n")
        first = tail_once(p)
        check(first.reason == REASON_NEW and first.checkpoint.gen == 1,
              "first sight: reason=new, gen=1")
        with open(p, "r+b") as fh:                      # truncate IN PLACE: same inode
            fh.truncate(0)
            fh.write(b"fresh\n")
        res = tail_once(p, first.checkpoint)
        check(res.reason == REASON_SHRUNK and res.reset,
              "size < offset is detected even though the inode is unchanged")
        check([l.text for l in res.lines] == ["fresh"], "re-ingest starts from byte 0")
        check(res.checkpoint.line_no == 1, "line numbering restarts with the generation")
        check(res.checkpoint.gen == 2, "gen advances so GD-26's sweep can retract gen<2")


def test_rotation():
    print("test_rotation")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "events.jsonl")
        write(p, b"a\nb\n")
        first = tail_once(p)
        os.replace(os.path.join(tmp, "events.jsonl"), os.path.join(tmp, "events.1"))
        write(p, b"c\n")                                # new file, new inode
        res = tail_once(p, first.checkpoint)
        check(res.reason == REASON_ROTATED and res.reset, "inode change => rotated")
        check([l.text for l in res.lines] == ["c"], "the new file is read from 0")
        check(res.checkpoint.gen == 2, "rotation bumps gen too")


def test_unchanged_is_free():
    print("test_unchanged_is_free")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "a.jsonl")
        write(p, b"x\n")
        ck = tail_once(p).checkpoint
        res = tail_once(p, ck)
        check(res.reason == REASON_UNCHANGED and res.bytes_read == 0,
              "an unchanged file is stat'd, never opened (stat-first, D6)")
        check(res.checkpoint.offset == ck.offset and not res.reset,
              "the checkpoint survives an idle tick unchanged")


def test_same_size_rewrite_policy():
    print("test_same_size_rewrite_policy")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "a.jsonl")
        write(p, b"aaaa\n")
        ck = tail_once(p).checkpoint
        with open(p, "r+b") as fh:
            fh.seek(0)
            fh.write(b"bbbb\n")                         # same length, mtime moves
        os.utime(p, ns=(ck.mtime_ns + 10**9, ck.mtime_ns + 10**9))
        default = tail_once(p, ck)
        check(default.reason == REASON_UNCHANGED and not default.reset,
              "default: an equal-size rewrite is not re-ingested (documented, O(delta))")
        opted = tail_once(p, ck, resync_on_mtime_only=True)
        check(opted.reason == REASON_RESYNC and opted.reset
              and [l.text for l in opted.lines] == ["bbbb"],
              "opt-in resync re-ingests the whole file")


def test_missing_file_keeps_checkpoint():
    print("test_missing_file_keeps_checkpoint")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "gone.jsonl")
        write(p, b"1\n2\n")
        ck = tail_once(p).checkpoint
        os.unlink(p)
        res = tail_once(p, ck)
        check(res.missing and res.reason == REASON_MISSING, "a vanished file reports missing")
        check(res.checkpoint == ck and not res.reset,
              "the checkpoint is NOT reset: a deleted transcript must not replay on return")
        check(tail_once(p).lines == [], "a never-seen missing file yields nothing, no crash")


# --- GD-30: per-tick cost is O(bytes appended) ----------------------------
def test_byte_budget_is_incremental():
    print("test_byte_budget_is_incremental")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "big.jsonl")
        line = b'{"pad":"' + b"x" * 900 + b'"}\n'
        with open(p, "wb") as fh:
            for _ in range(20 * 1024 * 1024 // len(line)):
                fh.write(line)
        size = os.path.getsize(p)
        check(size > 19 * 1024 * 1024, f"fixture is {size / 1048576:.1f} MB")

        t = Tailer(p)
        t.drain()                                       # cold ingest, however many ticks
        check(t.checkpoint.offset == size, "cold ingest consumed the whole file")
        before = t.bytes_read
        write(p, line, "ab")                            # append ~1 KB
        res = t.poll()
        delta = t.bytes_read - before
        check(len(res.lines) == 1, "the appended line is the only line returned")
        check(delta < 64 * 1024,
              f"tick after a 1 KB append read {delta} bytes (<64 KB) — O(delta), not O(file)")


def test_read_cap_bounds_one_read():
    print("test_read_cap_bounds_one_read")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "capped.jsonl")
        write(p, b"".join(b"%04d\n" % i for i in range(1000)))     # 5000 bytes
        res = tail_once(p, read_cap=1000)
        check(res.bytes_read <= 1000 and res.more,
              "the read cap bounds one read and sets more=True")
        allofit = read_complete_lines(p, read_cap=1000)
        check(len(allofit) == 1000 and allofit[-1].line_no == 1000,
              "read_complete_lines loops the cap and returns the whole file")
        check(allofit[500].byte_offset == 500 * 5,
              "byte offsets stay absolute across capped reads")


def test_line_longer_than_the_read_cap():
    print("test_line_longer_than_the_read_cap")
    # R-44 legislates for >8 MB payloads, so a line bigger than one read is
    # contemplated by the plan. It must never look like "caught up": the offset
    # cannot advance (torn-tail rule has no exception), so the outcome is named,
    # the stall is visible, and a looping reader still gets every line.
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "huge.jsonl")
        big = b"x" * 3000
        write(p, big + b"\n" + b"".join(b"line%d\n" % i for i in range(5)))

        res = tail_once(p, read_cap=1000)
        check(res.reason == REASON_OVERSIZE_LINE and res.oversize_line,
              "a line over read_cap gets its own reason, not a silent 'append'")
        check(res.lines == [] and not res.more and res.checkpoint.offset == 0,
              "no line is emitted and the offset does not move")
        check(res.deferred == res.bytes_read == 1000,
              "the whole capped read is deferred (the stall is measurable)")

        whole = read_complete_lines(p, read_cap=1000)
        check(len(whole) == 6 and whole[0].text == big.decode(),
              "read_complete_lines keeps its promise: 6 lines, the long one whole")
        check(whole[-1].text == "line4" and whole[1].byte_offset == 3001,
              "the lines after the long one are read normally, offsets absolute")
        check(tail_once(p, read_cap=1000, escalate_oversize_line=True).lines[0].text
              == big.decode(),
              "escalation is available to any caller that needs the line whole")

        # escalate_after=0 disables the recovery policy: the pure "stall and
        # name it" behaviour, which is what keeps the tick budget bounded.
        t = Tailer(p, read_cap=1000, escalate_after=0)
        first = t.poll()
        check(first.oversize_line and t.oversize_lines == 1 and t.stalled,
              "the Tailer counts the stall and knows it is stalled, not idle")
        before = t.bytes_read
        second = t.poll()
        check(second.oversize_line and t.bytes_read == before,
              "a stalled, unchanged file is not re-read every tick (GD-30)")
        check(t.drain() == [], "drain does not spin on a file it cannot advance")
        check(t.last_reason == REASON_OVERSIZE_LINE and t.last_result.oversize_line,
              "drain's empty list is explained by last_reason, not left ambiguous")
        write(p, b"tail\n", "ab")                     # the file moves again
        after = t.poll()
        check(after.oversize_line and t.bytes_read > before,
              "once the file changes the read is retried (still capped => still stalled)")
        check(t.escalations == 0, "escalate_after=0 really does opt out of escalation")
        raised = Tailer(p, read_cap=8192)
        lines = raised.drain()
        check([l.text for l in lines][0] == big.decode() and len(lines) == 7,
              "raising the cap unblocks the stream and every line arrives once")


def test_bounded_escalation_recovers_the_live_path():
    print("test_bounded_escalation_recovers_the_live_path")
    # DEFAULT_READ_CAP is exactly 8 MiB and R-44 legislates for payloads >8 MB,
    # so the first document R-44 was written for would otherwise be the first
    # line to wedge the live tail forever. The Tailer therefore escalates ONCE
    # per observed (size, mtime_ns), bounded by max_line_bytes.
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "huge.jsonl")
        big = b"x" * 3000
        write(p, big + b"\n" + b"".join(b"line%d\n" % i for i in range(5)))

        t = Tailer(p, read_cap=1000, escalate_after=2, max_line_bytes=1 << 20)
        check(t.poll().oversize_line and t.stalled,
              "tick 1 meets the long line and stalls with the named reason")
        res = t.poll()
        check([l.text for l in res.lines] == [big.decode()] + [f"line{i}" for i in range(5)],
              "tick 2 escalates once and the whole file comes through")
        check(t.escalations == 1 and not t.stalled,
              "exactly one escalation was spent and the stall cleared")
        write(p, b"after\n", "ab")
        check([l.text for l in t.poll().lines] == ["after"],
              "the stream is live again: the next append arrives normally")

        # A line past max_line_bytes stays honestly stalled — and is read at
        # most once per observation, never once per tick.
        q = os.path.join(tmp, "unreadable.jsonl")
        write(q, b"y" * 40000 + b"\n")
        u = Tailer(q, read_cap=1000, escalate_after=1, max_line_bytes=8000)
        u.poll()
        u.poll()                                   # spends the one escalation
        check(u.escalations == 1 and u.stalled and u.last_reason == REASON_OVERSIZE_LINE,
              "a line past max_line_bytes stays stalled with a named reason")
        spent = u.bytes_read
        u.poll()
        u.poll()
        check(u.bytes_read == spent,
              "the escalation is not retried while the file is unchanged (GD-30)")


# --- D6: back off while the CLI rewrites a transcript ---------------------
def test_compaction_backoff():
    print("test_compaction_backoff")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "chat.jsonl")
        write(p, b"a\n")
        tmpname = os.path.join(tmp, COMPACT_TMP_PREFIX + "9f2")
        write(tmpname, b"partial")
        check(compaction_in_progress(p), "a fresh .compact.tmp.* is detected")
        res = tail_once(p)
        check(res.compacting and res.reason == REASON_COMPACTING and res.lines == [],
              "reads defer while a rewrite is in progress")
        # The opt-out is checked WHILE the compaction is in progress — after the
        # unlink it would prove nothing at all.
        check(len(tail_once(p, skip_while_compacting=False).lines) == 1,
              "the backoff is opt-out for callers that know better")
        old = 1_000_000_000
        os.utime(tmpname, (old, old))
        check(not compaction_in_progress(p),
              "an abandoned temp file goes stale instead of wedging the stream forever")
        check(len(tail_once(p).lines) == 1, "the tail flows again once the tmp file is stale")
        os.unlink(tmpname)


def test_a_compaction_never_makes_a_file_read_as_empty():
    print("test_a_compaction_never_makes_a_file_read_as_empty")
    # `compaction_in_progress` is directory-scoped by design, and
    # ~/.claude/projects/<slug>/ holds every transcript of a project, so one
    # `.compact.tmp.*` used to blank EVERY transcript there for up to
    # COMPACT_STALE_S — and report it as an empty file. A "give me every line"
    # helper may not answer a deferral with an empty list.
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "chat.jsonl")
        write(p, b"1\n2\n3\n4\n")
        write(os.path.join(tmp, COMPACT_TMP_PREFIX + "abc"), b"partial")
        lines = read_complete_lines(p)
        check([l.text for l in lines] == ["1", "2", "3", "4"],
              "a 4-line file mid-compaction reads as 4 lines, not as empty-and-successful")
        try:
            read_complete_lines(p, skip_while_compacting=True)
            check(False, "the deferral must be visible when a caller asks for it")
        except CompactionInProgress as exc:
            check(p in str(exc),
                  "a caller that opts into the deferral gets it raised, never a bare []")
        # Tailer.drain() is the poll-loop shape: it DOES defer, and says so.
        t = Tailer(p)
        check(t.drain() == [] and t.last_reason == REASON_COMPACTING
              and t.last_result.compacting,
              "the poll loop still backs off, and last_reason explains the empty list")


# --- checkpoint value semantics ------------------------------------------
def test_checkpoint_roundtrip():
    print("test_checkpoint_roundtrip")
    ck = Checkpoint(st_dev=1, st_ino=2, size=3, offset=3, line_no=1, gen=4, mtime_ns=5)
    again = Checkpoint.from_dict(json.loads(json.dumps(ck.to_dict())))
    check(again == ck, "a checkpoint round-trips through JSON unchanged")
    check(ck.identity() == (1, 2, 3, 3), "identity() is D6's four-tuple in D6's order")
    check(Checkpoint().fresh and not ck.fresh, "fresh() distinguishes a virgin checkpoint")
    check(Checkpoint.from_dict({"offset": 9, "bogus": 1}).offset == 9,
          "unknown keys are ignored so an older checkpoint file cannot crash a restart")
    check(Checkpoint.from_dict(None) == Checkpoint(), "a missing checkpoint reads as fresh")
    # A half-written or older state file is exactly what a restart meets, and the
    # restart path is the one place tolerance was the point: lose the field, not
    # the process.
    for bad, why in (({"offset": None}, "a null field"),
                     ({"offset": "x"}, "a non-numeric field"),
                     ({"offset": [1]}, "a wrong-typed field")):
        try:
            ck2 = Checkpoint.from_dict(dict(bad, st_ino=7))
            check(ck2.offset == 0 and ck2.st_ino == 7,
                  f"{why} drops to its default and the rest of the checkpoint survives")
        except Exception as exc:
            check(False, f"{why} must not crash a restart, raised {exc!r}")
    check(Checkpoint.from_dict({"offset": 5.9}).offset == 5,
          "a float loses precision rather than the position (int(), not a raise)")
    # int(True) == 1, so a `true` in a state file would otherwise become byte
    # offset 1 — a plausible-looking position rather than the default.
    check(Checkpoint.from_dict({"offset": True, "st_ino": False}) == Checkpoint(),
          "a boolean is not a position: it drops to the default like any other bad value")


def test_tailer_rewind_and_counters():
    print("test_tailer_rewind_and_counters")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "s.jsonl")
        write(p, b"1\n2\n3\n")
        t = Tailer(p)
        check(len(t.poll().lines) == 3 and t.lines_read == 3, "poll advances and counts")
        check(t.poll().lines == [], "a second poll on an idle file yields nothing")
        t.rewind()
        res = t.poll()
        check(len(res.lines) == 3 and res.reset, "rewind forces a full re-ingest")
        check(res.checkpoint.gen == 2 and t.resets == 2,
              "the generation counter is monotonic across rewinds")


# --- the frozen corpus, read-only ----------------------------------------
def test_real_fixtures_agree_with_a_plain_read():
    print("test_real_fixtures_agree_with_a_plain_read")
    targets = [
        FIX / "run-wf_829e6f58" / "dd469822-2546-47d9-aaa3-31db4cb705e8" /
        "subagents" / "workflows" / "wf_829e6f58-b2f" / "journal.jsonl",
        FIX / "legacy" / "touch-mongo-live-events.jsonl",
        FIX / "mirror" / "records" / "oversize-line.jsonl",
    ]
    for path in targets:
        if not path.exists():
            check(False, f"fixture missing: {path}")
            continue
        expected = [l for l in path.read_bytes().split(b"\n")]
        if expected and expected[-1] == b"":
            expected.pop()
        lines = read_complete_lines(path)
        check(len(lines) == len(expected), f"{path.name}: {len(lines)} lines, same as a plain read")
        check(all(l.text.encode("utf-8", "replace") == e for l, e in zip(lines, expected)),
              f"{path.name}: every line matches byte-for-byte")
        check([l.line_no for l in lines] == list(range(1, len(lines) + 1)),
              f"{path.name}: line numbers are 1-based and contiguous")
        if lines:
            check(lines[0].byte_offset == 0 and
                  lines[-1].byte_offset + lines[-1].nbytes == path.stat().st_size,
                  f"{path.name}: offsets tile the file exactly")


def test_fixture_copy_then_grow():
    print("test_fixture_copy_then_grow")
    src = (FIX / "legacy" / "touch-full-recon-events.jsonl")
    if not src.exists():
        check(False, f"fixture missing: {src}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "events.jsonl")
        shutil.copy(src, p)
        t = Tailer(p)
        first = len(t.drain())
        extra = json.dumps({"ts": "2026-07-25T00:00:00.000Z", "plan": "p", "stage": "s",
                            "state": "info", "detail": "appended by the test"}) + "\n"
        write(p, extra, "ab")
        res = t.poll()
        check(len(res.lines) == 1 and json.loads(res.lines[0].text)["detail"].startswith("appended"),
              "a live append to a real stream is picked up incrementally")
        check(res.lines[0].line_no == first + 1,
              "line numbering continues across ticks (positional ids stay stable)")


def main():
    for t in (test_torn_tail, test_multibyte_split_across_ticks, test_split_lines_positions,
              test_in_place_truncation, test_rotation, test_unchanged_is_free,
              test_same_size_rewrite_policy, test_missing_file_keeps_checkpoint,
              test_byte_budget_is_incremental, test_read_cap_bounds_one_read,
              test_line_longer_than_the_read_cap,
              test_bounded_escalation_recovers_the_live_path,
              test_compaction_backoff, test_a_compaction_never_makes_a_file_read_as_empty,
              test_checkpoint_roundtrip,
              test_tailer_rewind_and_counters, test_real_fixtures_agree_with_a_plain_read,
              test_fixture_copy_then_grow):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all tailer tests passed")


if __name__ == "__main__":
    main()
