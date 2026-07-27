#!/usr/bin/env python3
"""Stdlib-only tests for aggregator/store.py (R-24, touch-events-v2). Run as
`python3 test_store.py`; exits non-zero on failure. No pytest, no runner.

R-24's own test list is the spine — ref validation both arms, two streams
legally holding the same seq, `(stream, seq)` cursor round-trip, torn-tail write
recovery — plus the invariants the plans state elsewhere and that only a test
can hold in place:

* `provenance` is mandatory and closed (GD-28), and the `custom-state` WAL
  refuses `harness`/`derived` — the file-side half of GD-28's `$jsonSchema` pin;
* `kind`/`source` are open at the tail, which is precisely what lets R-52's
  custom-state arm (sub-plan sp-11) ride this append machinery with
  `store.py` **unchanged**;
* there is no reducer here (GD-23: exactly one reducer, server-side);
* concurrent appends stay whole and seq-unique (GD-20's flock rule,
  PRIORART-14's one-`write()`-per-record invariant).
"""

import ast
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[0]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

#: SD-2's guard owns the "what does this file import" question; re-deriving it
#: here with string matching is how a guard rots (a docstring reword used to be
#: enough to break it). Import the real one instead.
from test_stdlib_only import imports_of                        # noqa: E402

from aggregator import SCHEMA_VERSION                          # noqa: E402
from aggregator import store as store_mod                      # noqa: E402
from aggregator.store import (                                 # noqa: E402
    DURABLE_STREAMS,
    MAX_RECORD_BYTES,
    PROVENANCE,
    RECORD_KEYS,
    RefError,
    SchemaError,
    Store,
    StoreError,
    StreamError,
    classify_ref,
    cursor_key,
    is_wire_ts,
    normalize_tokens,
    normalize_ts,
    now_ts,
    parse_cursor_key,
    state_root,
    validate_ref,
    validate_stream,
)

failures = []
AGENT = "a2fc883c96ff7b837"          # a real 17-hex agentId from the corpus
UUID = "081b28a7-aee9-43dc-935d-1586407f232e"


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception as other:                     # wrong exception type is a failure
        print(f"    (raised {type(other).__name__}: {other})")
        return False
    return False


def new_store(tmp):
    return Store(root=os.path.join(tmp, ".touch"))


# --- record shape ---------------------------------------------------------
def test_record_shape():
    print("test_record_shape")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        rec = s.append("session:622-10028", kind="session", provenance="harness",
                       ref={"pid": 622, "procStart": "10028"}, data={"cwd": "/repo"})
        check(tuple(rec) == RECORD_KEYS,
              f"keys are exactly {RECORD_KEYS} in that order")
        check(rec["v"] == SCHEMA_VERSION == 2, "v is 2 (touch-events-v2)")
        check(rec["seq"] == 1, "the first record of a stream is seq 1")
        check(is_wire_ts(rec["ts"]), f"ts is the single writer format: {rec['ts']}")
        line = open(s.stream_path("session:622-10028"), "rb").read()
        check(line.endswith(b"\n") and line.count(b"\n") == 1,
              "exactly one newline-terminated line was written")
        check(json.loads(line) == rec, "the line parses back to the returned record")
        check(b'"stream"' not in line,
              "no scalar `stream` field on disk — that is mongo_store's addition (R-24 row)")


def test_ts_format():
    print("test_ts_format")
    check(is_wire_ts(now_ts()) and now_ts().endswith("Z"), "now_ts() emits one format, ...Z")
    check(normalize_ts("2026-07-25T03:20:00.000Z") ==
          normalize_ts("2026-07-25T03:20:00.000+00:00"),
          "readers normalize Z -> +00:00 (GD-11)")
    check(normalize_ts("2026-07-25T03:20:00") == normalize_ts("2026-07-25T03:20:00Z"),
          "a naive legacy ts is read as UTC, not rejected (RUNSTATE-6: mixed formats exist)")
    # Tolerance has a floor, and the floor is still a StoreError: the legacy
    # adapter (R-27) reads mixed-format streams and catches those; a bare
    # ValueError out of fromisoformat would be an unhandled crash on history.
    for bad in ("not a ts", "", "2026-13-99T99:99:99Z", None, []):
        check(raises(SchemaError, normalize_ts, bad),
              f"an unparseable ts is a SchemaError, not a bare ValueError: {bad!r}")
    check(issubclass(SchemaError, StoreError),
          "...and SchemaError is a StoreError, so one except clause covers the module")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        check(raises(SchemaError, s.append, "control", kind="control",
                     provenance="touch", ts="2026-07-25 03:20:00"),
              "a ts in any other format is rejected at the writer")


# --- ref union: both arms (GD-11) ----------------------------------------
def test_ref_union_accepts():
    print("test_ref_union_accepts")
    good = {
        "uuid": {"uuid": UUID},
        "toolUseId": {"toolUseId": "toolu_01ABC"},
        "agentId": {"agentId": AGENT},
        "runNode": {"runId": "wf_829e6f58-b2f", "key": "research", "ordinal": 0},
        "session": {"pid": 622, "procStart": "10028"},
        "orchAgent": {"root": "touch", "name": "impl-1", "attempt": 1},
        "legacyPlan": {"task": "touch-mongo-live", "plan": "sp-04", "stage": "implement"},
    }
    for name, ref in good.items():
        check(validate_ref(ref) == name, f"{name} ref accepted and classified")
    check(validate_ref({"agentId": "legacy:touch-repo-recon:a2fc883c"}) == "agentId",
          "legacy:<task>:<id8> is exempt from the 17-hex rule (GD-14)")
    check(classify_ref({}) == "none" and classify_ref(None) == "none",
          "an absent ref is 'none' (a stream-level event), not an error")


def test_ref_union_rejects_and_retains():
    print("test_ref_union_rejects_and_retains")
    bad = [
        ({"agentId": "a2fc883c"}, "8-hex agentId (not namespaced) is malformed"),
        ({"agentId": AGENT.upper()}, "upper-case agentId is malformed"),
        ({"uuid": "not-a-uuid"}, "non-UUID uuid is malformed"),
        ({"runId": "wf_1", "key": "k", "ordinal": "0"}, "string ordinal breaks the int pin"),
        ({"pid": "622", "procStart": "10028"}, "string pid breaks the int pin"),
        ({"pid": 622, "procStart": 10028}, "int procStart breaks the STRING pin (GD-24)"),
        ({"root": "r", "name": "n", "attempt": "1"}, "string attempt breaks the int pin"),
        ({"task": "t", "plan": ""}, "empty plan is malformed"),
    ]
    for ref, why in bad:
        check(raises(RefError, validate_ref, ref), f"rejected: {why}")

    weird = {"galaxy": "andromeda", "n": 1}
    check(classify_ref(weird) == "unknown" and validate_ref(weird) == "unknown",
          "an unknown ref shape is retained, not rejected (GD-11's open tail)")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        rec = s.append("control", kind="log", provenance="touch", ref=weird)
        check(s.read_all("control")[0]["ref"] == weird,
              "the unknown ref is stored verbatim, key for key")
        check(rec["ref"] is not weird, "the stored ref is a copy — no caller aliasing")


# --- tokens (GD-11 / GD-25) ----------------------------------------------
def test_token_records():
    print("test_token_records")
    filled = normalize_tokens({"in": 5})
    check(filled == {"in": 5, "out": 0, "cached": 0, "cache_write": 0},
          "a token record always carries all four keys, defaulting to 0")
    check(normalize_tokens({"in": 1, "extra": "kept"})["extra"] == "kept",
          "extra token fields are preserved")
    check(raises(SchemaError, normalize_tokens, {"in": "5"}),
          "a stringly-typed token count is rejected, never silently zeroed")
    check(raises(SchemaError, normalize_tokens, {"out": -1}), "a negative count is rejected")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        rec = s.append("run:wf_1", kind="token", provenance="derived",
                       ref={"agentId": AGENT}, data={"out": 12})
        check(sorted(rec["data"]) == ["cache_write", "cached", "in", "out"],
              "the writer fills the four keys on the way to disk")


# --- provenance (GD-28) ---------------------------------------------------
def test_provenance_is_mandatory_and_closed():
    print("test_provenance_is_mandatory_and_closed")
    check(PROVENANCE == ("harness", "derived", "asserted", "touch", "unknown"),
          "the five-value enum is exactly GD-28's")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        try:
            s.append("run:wf_1", kind="run", data={})
            check(False, "provenance is a required keyword")
        except TypeError:
            check(True, "provenance is a required keyword (omitting it is a TypeError)")
        check(raises(SchemaError, s.append, "run:wf_1", kind="run",
                     provenance="harnesss", data={}),
              "an unknown provenance value is rejected")
        for value in PROVENANCE:
            s.append("run:wf_1", kind="run", provenance=value)
        check(len(s.read_all("run:wf_1")) == 5, "all five legal values are accepted")


def test_custom_state_provenance_pin():
    print("test_custom_state_provenance_pin")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        for value in ("asserted", "touch"):
            s.append("custom-state", kind="annotation", provenance=value,
                     ref={"agentId": AGENT}, data={"stateKey": "note", "text": "hi"})
        for value in ("harness", "derived", "unknown"):
            check(raises(SchemaError, s.append, "custom-state", kind="annotation",
                         provenance=value),
                  f"the custom-state WAL refuses provenance={value} (GD-28)")
        check(len(s.read_all("custom-state")) == 2, "only the two legal writes landed")
        check("custom-state" in DURABLE_STREAMS,
              "the WAL fsyncs: it is the one dataset not rebuildable from ~/.claude (R-52)")


def test_kind_and_source_are_open_at_the_tail():
    print("test_kind_and_source_are_open_at_the_tail")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        # R-52's kinds must work with store.py UNCHANGED (sp-11's constraint).
        for kind in ("control_intent", "control_ack", "topology", "agent_state",
                     "annotation", "tag", "artifact", "ledger"):
            s.append("custom-state", kind=kind, provenance="asserted",
                     ref={"root": "touch", "name": "impl-1", "attempt": 1},
                     data={"stateKey": kind})
        kinds = [r["kind"] for r in s.read_all("custom-state")]
        check(len(kinds) == 8 and "control_intent" in kinds,
              "R-52's custom-state kinds ride the existing append machinery")
        check(raises(SchemaError, s.append, "custom-state", kind="Not A Slug",
                     provenance="touch"),
              "an ill-formed kind is still rejected (open tail, not open season)")
        check(raises(SchemaError, s.append, "custom-state", kind="annotation",
                     provenance="touch", source="Weird Source"),
              "an ill-formed source is rejected")


# --- seq, cursors, streams ------------------------------------------------
def test_two_streams_share_seq_space():
    print("test_two_streams_share_seq_space")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        for _ in range(3):
            s.append("session:622-10028", kind="log", provenance="touch")
            s.append("run:wf_829e6f58-b2f", kind="log", provenance="touch")
        a = [r["seq"] for r in s.read_all("session:622-10028")]
        b = [r["seq"] for r in s.read_all("run:wf_829e6f58-b2f")]
        check(a == b == [1, 2, 3],
              "seq is per event-log file: two streams legally hold the same seq (GD-11)")


def test_seq_resumes_from_the_file():
    print("test_seq_resumes_from_the_file")
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, ".touch")
        first = Store(root=root)
        for _ in range(4):
            first.append("run:wf_1", kind="node", provenance="harness")
        second = Store(root=root)                     # a fresh process/boot
        check(second.next_seq("run:wf_1") == 5, "next_seq resumes from the file at boot")
        rec = second.append("run:wf_1", kind="node", provenance="harness")
        check(rec["seq"] == 5, "the reopened store continues the sequence")
        seqs = [r["seq"] for r in second.read_all("run:wf_1")]
        check(seqs == [1, 2, 3, 4, 5] and len(set(seqs)) == 5, "no duplicates, no gaps")
        check(Store(root=root).next_seq("run:nope") == 1,
              "an unwritten stream starts at seq 1 without creating a file")
        check(not os.path.exists(os.path.join(root, "runs", "nope")),
              "reading next_seq of an unwritten stream creates nothing")


def test_cursor_roundtrip():
    print("test_cursor_roundtrip")
    key = cursor_key("run:wf_829e6f58-b2f", 184)
    check(key == "run:wf_829e6f58-b2f#000000000184",
          f"cursor grammar is <stream>#<seq:012d>: {key}")
    check(parse_cursor_key(key) == ("run:wf_829e6f58-b2f", 184), "cursor round-trips")
    check(raises(StoreError, parse_cursor_key, "184"),
          "a bare seq is never a valid cursor (GD-11)")
    check(raises(StoreError, parse_cursor_key, "run:wf_1#184"),
          "an unpadded seq is not a cursor either (padding makes lexical order = numeric)")
    check(cursor_key("a", 2) < cursor_key("a", 10),
          "zero padding makes lexicographic order equal numeric order (LIVEFLOW-3)")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        for i in range(5):
            s.append("run:wf_1", kind="log", provenance="touch", data={"i": i})
        resumed = s.read_from_cursor(cursor_key("run:wf_1", 2))
        check([r["data"]["i"] for r in resumed] == [2, 3, 4],
              "read_from_cursor resumes strictly after the cursor's seq")
        check(s.cursor("run:wf_1") == cursor_key("run:wf_1", 5),
              "cursor() names the last record in the stream")


def test_stream_ids_and_paths():
    print("test_stream_ids_and_paths")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        rel = os.path.relpath(s.stream_path("session:622-10028"), s.root)
        check(rel == os.path.join("sessions", "622-10028", "events.jsonl"),
              f"session stream maps to D5's layout: {rel}")
        check(os.path.relpath(s.stream_path("run:wf_1"), s.root) ==
              os.path.join("runs", "wf_1", "events.jsonl"), "run stream maps to D5's layout")
        check(os.path.relpath(s.stream_path("custom-state"), s.root) == "custom-state.jsonl",
              "the custom-state WAL is a single file at the root (D5 amendment)")
        for bad in ("", "run:../../etc/passwd", "run:a#b", "run:a|b", "run:a/b",
                    "session", "nope:1", ".hidden", "x" * 300,
                    # A dot-only component survives path escaping and names the
                    # per-kind directory *root*: two ids would collide there and
                    # streams() (hence any GD-26 rebuild) would never see it.
                    "run:.", "session:.", "run:a:.", "run:", "run:a::b"):
            check(raises(StreamError, s.stream_path, bad), f"rejected stream id: {bad!r}")
        for bad in ("run:.", "session:."):
            check(raises(StreamError, validate_stream, bad),
                  f"validate_stream rejects {bad!r} too, before any path is built")
        s.append("run:ok", kind="log", provenance="touch")
        check(s.streams() == ["run:ok"],
              "every stream that can be written is a stream streams() can find")
        check(validate_stream("run:wf_829e6f58-b2f") == "run:wf_829e6f58-b2f",
              "a legal stream id is returned unchanged")


def test_stream_discovery_roundtrip():
    print("test_stream_discovery_roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        ids = ["session:622-10028", "run:wf_829e6f58-b2f", "run:legacy:touch-repo-recon",
               "custom-state", "control"]
        for stream in ids:
            s.append(stream, kind="log",
                     provenance="touch" if stream in ("custom-state", "control") else "harness")
        check(s.streams() == sorted(ids),
              "every written stream is discovered, and the id survives path escaping")
        escaped = os.path.basename(os.path.dirname(s.stream_path("run:legacy:touch-repo-recon")))
        check(":" not in escaped and escaped == "legacy%3Atouch-repo-recon",
              f"path components are percent-escaped on disk: {escaped}")


def test_state_root_resolution():
    print("test_state_root_resolution")
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("TOUCH_STATE_DIR")
        try:
            os.environ["TOUCH_STATE_DIR"] = os.path.join(tmp, "touch-dev")
            check(state_root() == os.path.join(tmp, "touch-dev"),
                  "$TOUCH_STATE_DIR wins over the default")
            check(state_root(os.path.join(tmp, "x")) == os.path.join(tmp, "x"),
                  "an explicit root wins over the env var")
        finally:
            if old is None:
                os.environ.pop("TOUCH_STATE_DIR", None)
            else:
                os.environ["TOUCH_STATE_DIR"] = old
        check(state_root() == str(REPO / ".touch"),
              "the default is <repo>/.touch, never under .claude/local-orchestrators/")


# --- durability: torn tails, oversize, concurrency ------------------------
def test_torn_tail_write_recovery():
    print("test_torn_tail_write_recovery")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        s.append("run:wf_1", kind="log", provenance="touch", data={"i": 1})
        path = s.stream_path("run:wf_1")
        with open(path, "ab") as fh:                     # a killed writer's partial line
            fh.write(b'{"v":2,"seq":2,"ts":"2026-')
        fresh = Store(root=s.root)                       # new process, no cached seq
        rec = fresh.append("run:wf_1", kind="log", provenance="touch", data={"i": 3})
        raw = open(path, "rb").read()
        check(b'{"v":2,"seq":2,"ts":"2026-\n' in raw,
              "the torn line is terminated, not concatenated onto — and never deleted")
        check(fresh.stats["torn_repairs"] == 1, "the repair is counted, not silent")
        check(rec["seq"] >= 3, f"the new record's seq skips the torn line (seq={rec['seq']})")
        records = fresh.read_all("run:wf_1")
        check([r["data"]["i"] for r in records] == [1, 3],
              "readers skip the garbage line and keep both good records")
        check(fresh.stats["bad_lines"] == 1, "the unparseable line is counted")
        seqs = [r["seq"] for r in records]
        check(len(set(seqs)) == len(seqs), "seqs stay unique across the repair")


def test_torn_tail_repair_after_a_cursor_read():
    print("test_torn_tail_repair_after_a_cursor_read")
    # The shape a server actually has: it asks for a cursor at connect and only
    # then writes. Caching the scanned size without the newline state made that
    # sequence skip the in-lock rescan and concatenate the new record onto the
    # killed writer's partial line — the record `append()` reported as durably
    # stored was unrecoverable, and `torn_repairs` stayed 0.
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        s.append("run:wf_1", kind="log", provenance="touch", data={"i": 1})
        path = s.stream_path("run:wf_1")
        with open(path, "ab") as fh:
            fh.write(b'{"v":2,"seq":2,"ts":"2026-')
        fresh = Store(root=s.root)
        pre = fresh.cursor("run:wf_1")                   # the poisoning read
        check(pre == cursor_key("run:wf_1", 2),
              f"a torn line still consumes its number: cursor={pre}")
        rec = fresh.append("run:wf_1", kind="log", provenance="touch", data={"i": 3})
        check(fresh.stats["torn_repairs"] == 1,
              "the repair newline is written even though the size was already scanned")
        records = fresh.read_all("run:wf_1")
        check([r["data"]["i"] for r in records] == [1, 3],
              "the record append() returned is on disk and readable, not glued to the torn line")
        check(any(r["seq"] == rec["seq"] for r in records),
              f"the returned seq ({rec['seq']}) names a record that survived")
        raw = open(path, "rb").read()
        check(b'{"v":2,"seq":2,"ts":"2026-\n' in raw,
              "the torn line is terminated and kept (history is never deleted)")
        for durable_stream in ("custom-state", "control"):
            d = Store(root=s.root)
            d.append(durable_stream, kind="control", provenance="asserted", data={"i": 1})
            dpath = d.stream_path(durable_stream)
            with open(dpath, "ab") as fh:
                fh.write(b'{"v":2,"seq":2')
            after = Store(root=s.root)
            after.cursor(durable_stream)
            after.append(durable_stream, kind="control", provenance="asserted", data={"i": 3})
            check(after.stats["torn_repairs"] == 1
                  and [r["data"]["i"] for r in after.read_all(durable_stream)] == [1, 3],
                  f"{durable_stream}: the fsync'd stream cannot lose the record it fsync'd")


def test_cursor_of_a_non_writing_reader_tracks_the_file():
    print("test_cursor_of_a_non_writing_reader_tracks_the_file")
    # `cursor()` is public and says it names the last record *currently* in the
    # stream. A reader instance (the server serving "resume from here") must
    # therefore re-derive it, or it hands out a frozen cursor and replays.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, ".touch")
        writer = Store(root=root)
        reader = Store(root=root)
        writer.append("run:wf_1", kind="log", provenance="touch")
        check(reader.cursor("run:wf_1") == cursor_key("run:wf_1", 1),
              "the reader sees the first record")
        for _ in range(4):
            writer.append("run:wf_1", kind="log", provenance="touch")
        check(reader.next_seq("run:wf_1") == 6 and
              reader.cursor("run:wf_1") == cursor_key("run:wf_1", 5),
              "the reader's cursor follows the file instead of freezing at its first read")
        check(reader.cursor("run:wf_2") == cursor_key("run:wf_2", 0),
              "an unwritten stream's cursor is seq 0, and asking creates no file")


def test_oversize_record_is_stubbed_never_dropped():
    print("test_oversize_record_is_stubbed_never_dropped")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        big = {"blob": "x" * (MAX_RECORD_BYTES + 10), "keep": 1}
        rec = s.append("session:1-2", kind="tool", provenance="harness",
                       ref={"toolUseId": "toolu_big"}, data=big)
        check(rec["data"].get("oversize") is True, "an oversize record is stubbed, not raised")
        check(rec["data"]["bytes"] > MAX_RECORD_BYTES, "the stub records the real byte size")
        check(rec["data"]["keys"] == ["blob", "keep"], "the stub names the dropped keys")
        check(rec["ref"] == {"toolUseId": "toolu_big"} and rec["kind"] == "tool",
              "identity and kind survive the stub (the record is still in the stream)")
        check(s.stats["oversize"] == 1, "the stub is counted")
        line = open(s.stream_path("session:1-2"), "rb").read()
        check(len(line) < MAX_RECORD_BYTES, f"the written line stays bounded ({len(line)} bytes)")
        ok = s.append("session:1-2", kind="tool", provenance="harness",
                      data={"blob": "y" * 900_000})
        check(ok["data"]["blob"].startswith("y") and "oversize" not in ok["data"],
              "an 872 KB-class real line still fits under the cap")


def test_the_cap_bounds_the_written_line_not_one_field():
    print("test_the_cap_bounds_the_written_line_not_one_field")
    # MAX_RECORD_BYTES is a memory bound on READERS (GD-20). Measuring `data`
    # alone left `ref` unbounded — and GD-11's open tail makes an unknown ref
    # subtree a *designed* carrier — so a `.touch/` line could exceed the
    # tailer's read cap and take that stream dark for the live view (GD-22/30)
    # while replay still worked. The bound is on the encoded blob now.
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        rec = s.append("run:wf_1", kind="log", provenance="touch",
                       ref={"galaxy": "x" * (10 * 1024 * 1024)}, data={"k": 1})
        path = s.stream_path("run:wf_1")
        size = os.path.getsize(path)
        check(size < MAX_RECORD_BYTES,
              f"a 10 MB ref is stubbed and the written line stays bounded ({size} bytes)")
        check(rec["ref"]["oversize"] is True and rec["ref"]["keys"] == ["galaxy"],
              "the oversize ref is stubbed, naming what it was — never dropped")
        check(rec["data"] == {"k": 1},
              "the field that was NOT the problem survives (biggest first)")
        check(s.stats["oversize"] == 1, "the reduction is counted once")

        # 64 megabyte-long keys: the stub itself must be bounded in both
        # dimensions, or "the stub" is just a smaller unbounded thing.
        s.append("run:wf_2", kind="log", provenance="touch",
                 data={("k" * 1_000_000) + str(i): 1 for i in range(64)})
        size2 = os.path.getsize(s.stream_path("run:wf_2"))
        check(size2 < MAX_RECORD_BYTES,
              f"64 megabyte-long data keys still fit ({size2} bytes): the stub is capped too")

        # The point of the bound: the LIVE path (default read cap) can read it.
        from aggregator.tailer import Tailer                    # noqa: PLC0415
        t = Tailer(path)
        lines = t.drain()
        check(len(lines) == 1 and not t.stalled and json.loads(lines[0].text)["seq"] == 1,
              "a fresh Tailer at the DEFAULT read cap returns the record: no live-view blackout")


def test_counters_describe_only_what_was_written():
    print("test_counters_describe_only_what_was_written")
    # GD-29 leans on these numbers ("the duplicate-key counter is how you notice
    # a second writer"). `_build_record` can reject a spec half way through a
    # batch, and nothing is written then — so nothing may be counted then.
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        s.append("run:wf_1", kind="log", provenance="touch", data={"i": 1})
        path = s.stream_path("run:wf_1")
        with open(path, "ab") as fh:
            fh.write(b'{"v":2,"seq":2,"ts":"2026-')          # a killed writer
        fresh = Store(root=s.root)
        before = os.path.getsize(path)
        check(raises(SchemaError, fresh.append_many, "run:wf_1",
                     [{"kind": "log", "provenance": "touch", "data": {"i": 2}},
                      {"kind": "log", "provenance": "BOGUS"}]),
              "a malformed spec rejects the whole batch")
        check(os.path.getsize(path) == before, "the rejected batch wrote nothing at all")
        check(fresh.stats["torn_repairs"] == 0 and fresh.stats["appended"] == 0,
              "a repair that was never written is not counted")
        fresh.append("run:wf_1", kind="log", provenance="touch", data={"i": 3})
        check(fresh.stats["torn_repairs"] == 1,
              "the one real repair is counted exactly once, not twice")
        check(raises(SchemaError, fresh.append_many, "run:wf_1",
                     [{"kind": "log", "provenance": "touch",
                       "data": {"b": "x" * (MAX_RECORD_BYTES + 10)}},
                      {"kind": "log", "provenance": "BOGUS"}]),
              "a batch with an oversize record AND a bad one still rejects")
        check(fresh.stats["oversize"] == 0,
              "the stub of a record that never reached the disk is not counted either")


def test_durable_streams_fsync_the_directory_entry():
    print("test_durable_streams_fsync_the_directory_entry")
    # fsync(file) commits CONTENTS; the directory entry `open()` just created is
    # a separate write. Without the directory fsync the very first custom-state
    # record — the one dataset not rebuildable from ~/.claude (R-52) and the
    # legal record of intents (D7) — can vanish with the file after a power cut.
    real_fsync = os.fsync
    seen = []

    def spy(fd):
        try:
            seen.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        except OSError:                                        # pragma: no cover
            seen.append(None)
        return real_fsync(fd)

    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        os.fsync = spy
        try:
            s.append("custom-state", kind="annotation", provenance="asserted",
                     data={"stateKey": "note"})
            first = list(seen)
            seen.clear()
            s.append("custom-state", kind="annotation", provenance="asserted",
                     data={"stateKey": "note2"})
            second = list(seen)
            seen.clear()
            s.append("run:wf_1", kind="log", provenance="touch", data={"i": 1})
            plain = list(seen)
            seen.clear()
            # control.jsonl lives in the SAME directory as custom-state.jsonl,
            # and it is a second entry: memoizing "this directory is synced"
            # would skip exactly the entry at risk.
            s.append("control", kind="control_intent", provenance="asserted",
                     data={"stateKey": "stop"})
            sibling = list(seen)
        finally:
            os.fsync = real_fsync
    check(first.count(False) == 1 and first.count(True) == 1,
          f"the first durable append fsyncs the file AND its directory: {first}")
    check(second == [False],
          f"later appends fsync only the file — the entry is already committed: {second}")
    check(plain == [],
          f"a rebuildable stream pays no fsync at all (GD-30 latency tax): {plain}")
    check(sibling.count(True) == 1,
          f"a second new file in the same directory syncs the entry too: {sibling}")


def test_cursor_keys_use_the_gd24_escaping():
    print("test_cursor_keys_use_the_gd24_escaping")
    # SD-11: every _id is a string from refs.ref_key (sp-05) in GD-24's grammar,
    # which percent-escapes `% # | :` in user-chosen components. `validate_stream`
    # deliberately PERMITS a raw `:`/`%` in a stream id (a legacy task folder is
    # user-chosen), so the cursor token has to do the escaping or the two
    # grammars diverge and `{stream:1,seq:1}` stops meaning what the mirror thinks.
    plain = cursor_key("run:wf_829e6f58-b2f", 184)
    check(plain == "run:wf_829e6f58-b2f#000000000184",
          f"an id with nothing to escape is untouched: {plain}")
    for stream in ("run:legacy:touch-repo-recon", "run:a%3Ab", "run:%25", "custom-state",
                   "session:622-10028"):
        key = cursor_key(stream, 7)
        head = key.rsplit("#", 1)[0]
        check(head.count(":") <= 1,
              f"only the structural separator survives unescaped: {stream!r} -> {head}")
        check(parse_cursor_key(key) == (stream, 7),
              f"the escaping round-trips exactly: {stream!r} -> {key}")
    check(cursor_key("run:a:b", 1) != cursor_key("run:a%3Ab", 1),
          "escaping is injective: two distinct ids cannot collide on one cursor")


def test_concurrent_appends_stay_whole():
    print("test_concurrent_appends_stay_whole")
    # PRIORART-14 / GD-20: appends are flock'd, one write() per batch. Four
    # processes is out of contract (single writer per stream) but must still be
    # *safe*: no torn lines, no duplicate seq.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, ".touch")
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from aggregator.store import Store\n"
            "s = Store(root=%r)\n"
            "for i in range(100):\n"
            "    s.append('run:wf_race', kind='log', provenance='touch', data={'w': sys.argv[1]})\n"
        ) % (str(REPO), root)
        procs = [subprocess.Popen([sys.executable, "-c", code, str(w)]) for w in range(4)]
        rcs = [p.wait() for p in procs]
        check(all(rc == 0 for rc in rcs), f"all 4 writers exited cleanly: {rcs}")
        path = os.path.join(root, "runs", "wf_race", "events.jsonl")
        raw = open(path, "rb").read()
        lines = [l for l in raw.split(b"\n") if l.strip()]
        parsed = []
        torn = 0
        for line in lines:
            try:
                parsed.append(json.loads(line))
            except ValueError:
                torn += 1
        check(torn == 0, f"0 torn lines out of {len(lines)} concurrent appends")
        check(len(parsed) == 400, f"all 400 records present ({len(parsed)})")
        seqs = sorted(r["seq"] for r in parsed)
        check(seqs == list(range(1, 401)),
              "seq is re-derived inside the lock, so concurrent writers do not collide")


def test_follow_is_incremental():
    print("test_follow_is_incremental")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        s.append("run:wf_1", kind="log", provenance="touch", data={"i": 1})
        recs, ck, reset = s.follow("run:wf_1")
        check(len(recs) == 1 and reset, "the first follow() reads from 0 and reports a reset")
        s.append("run:wf_1", kind="log", provenance="touch", data={"i": 2})
        recs2, ck2, reset2 = s.follow("run:wf_1", ck)
        check([r["data"]["i"] for r in recs2] == [2] and not reset2,
              "the next follow() returns only what was appended")
        check(ck2.offset > ck.offset, "the checkpoint advances")
        recs3, _, _ = s.follow("run:wf_1", ck2)
        check(recs3 == [], "an idle stream yields nothing")


# --- boundaries -----------------------------------------------------------
def test_no_reducer_lives_here():
    print("test_no_reducer_lives_here")
    # GD-23: exactly one reducer and it is server-side (R-54, sub-plan sp-10).
    # A "current state" helper appearing here is the drift this guards against.
    names = [n for n in dir(store_mod) if not n.startswith("_")]
    offenders = [n for n in names
                 if any(w in n.lower() for w in ("reduce", "reducer", "current_state",
                                                 "liveness", "derive"))]
    check(not offenders, f"store.py exports no reduction/liveness surface: {offenders}")
    top, lazy = imports_of(ast.parse((REPO / "aggregator" / "store.py").read_text()))
    check("pymongo" not in (top | lazy) and "bson" not in (top | lazy),
          "store.py imports no pymongo, eagerly or lazily "
          "(GD-21: only mongo_store.py and mirror.py may)")


def test_data_must_be_a_dict():
    print("test_data_must_be_a_dict")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        check(raises(SchemaError, s.append, "run:wf_1", kind="log",
                     provenance="touch", data=[1, 2]),
              "data must be an object: a list would not survive the Mongo mirror's shape")
        check(raises(RefError, s.append, "run:wf_1", kind="log",
                     provenance="touch", ref="a2fc883c96ff7b837"),
              "a bare string ref is rejected — refs are objects (GD-11)")
        rec = s.append("run:wf_1", kind="log", provenance="touch")
        check(rec["data"] == {} and rec["ref"] == {},
              "omitted data/ref default to empty objects, keeping the shape stable")


def test_append_many_is_one_batch():
    print("test_append_many_is_one_batch")
    with tempfile.TemporaryDirectory() as tmp:
        s = new_store(tmp)
        specs = [{"kind": "log", "provenance": "touch", "data": {"i": i}} for i in range(3)]
        out = s.append_many("run:wf_1", specs)
        check([r["seq"] for r in out] == [1, 2, 3], "a batch gets consecutive seqs")
        check(s.append_many("run:wf_1", []) == [], "an empty batch is a no-op")
        check(s.stats["appended"] == 3 and s.stats["bytes_written"] > 0, "counters add up")
        check([r["data"]["i"] for r in s.read_all("run:wf_1")] == [0, 1, 2],
              "batch order is file order (GD-11: order is line order, never a ts sort)")


def main():
    for t in (test_record_shape, test_ts_format, test_ref_union_accepts,
              test_ref_union_rejects_and_retains, test_token_records,
              test_provenance_is_mandatory_and_closed, test_custom_state_provenance_pin,
              test_kind_and_source_are_open_at_the_tail, test_two_streams_share_seq_space,
              test_seq_resumes_from_the_file, test_cursor_roundtrip,
              test_stream_ids_and_paths, test_stream_discovery_roundtrip,
              test_state_root_resolution, test_torn_tail_write_recovery,
              test_torn_tail_repair_after_a_cursor_read,
              test_cursor_of_a_non_writing_reader_tracks_the_file,
              test_oversize_record_is_stubbed_never_dropped,
              test_the_cap_bounds_the_written_line_not_one_field,
              test_counters_describe_only_what_was_written,
              test_durable_streams_fsync_the_directory_entry,
              test_cursor_keys_use_the_gd24_escaping,
              test_concurrent_appends_stay_whole, test_follow_is_incremental,
              test_no_reducer_lives_here, test_data_must_be_a_dict,
              test_append_many_is_one_batch):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all store tests passed")


if __name__ == "__main__":
    main()
