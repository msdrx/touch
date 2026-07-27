# sp-aggregator-core — adversarial critique, attempt 1

**Verdict: REJECTED** — 1 blocker, 1 major, 5 minor, 5 nits.

Reviewed (full content; all four files are new in an untracked tree):
`aggregator/__init__.py`, `aggregator/tailer.py`, `aggregator/store.py`,
`aggregator/ws.py`, `tests/run_all.sh`, `tests/test_tailer.py`,
`tests/test_store.py`, `tests/test_ws.py`, `tests/test_stdlib_only.py`.

Against: `touch-mongo-live-subplans.md` §sp-04, amendment items R-22:aggregator /
R-23 / R-24 / R-29 and their §2 dispositions, base plan R-22…R-29 + GD-11 /
GD-15 / GD-20, and amendment GD-21…GD-30 + SD-2 / SD-10 / SD-11.

## What holds up (verified independently, not taken from the test gate)

* All four owned suites re-run green here (63 / 109 / 114 / 12 `ok`, rc=0 each),
  no `.touch/` created, no commit added (HEAD is still sp-01's C2 `579446e`),
  nothing written outside the ownership list (the modified `.claude/**` paths
  are 21:51–23:01, all four owned modules 23:28–23:42).
* GD-21 / GD-22 / GD-24 / GD-25 / GD-26 / GD-29: zero pymongo, zero DB I/O,
  zero delete/`$unset`/TTL/`$inc` surface anywhere in the three modules; no
  `_id` is built here at all; the `<stream>#<seq:012d>` grammar is the GD-24
  one, zero-padded, and the store carries no scalar `stream`/`seq` field on
  disk (R-24 row: "Mongo does not replace store.py" — respected literally,
  asserted by `test_record_shape`).
* SD-10 is implemented exactly as written: `(st_dev, st_ino, size, offset)`
  identity, `size < offset` as its own explicit branch, `gen` bumped on every
  reset, sweep left to `mirror.py`.
* GD-28 pin is right per plan (`custom_state*` ⇒ `{asserted,touch}`;
  `control_ack` is a custom-state `kind`, so pinning the `control` stream the
  same way is faithful, not over-restriction), and `kind`/`source` really do
  stay open at the tail, so R-52 can ride this file unchanged.
* R-29's prior-art parity is *checked*, not asserted: `drain_frames` is
  byte-identical to `monitor_server.parse_client_frames` and the test executes
  the real imported function. `ws.py` imports are exactly five stdlib names.
* Tests are behavioural, not tautological (real fixtures byte-for-byte, offset
  tiling, 4-process concurrent append with a `seqs == range(1,401)` assertion,
  RFC §5.7 vectors in both directions).

---

## BLOCKER

### B1 — `append()` after `next_seq()`/`cursor()` on a torn-tail stream silently destroys the record it returns
`aggregator/store.py:532` (the discarded flag) and
`aggregator/store.py:657-666` (`ends_with_newline = True` default).

`next_seq()` scans the file and throws away `_scan_seq`'s
`ends_with_newline` (`seq, _, size = self._scan_seq(fh)`) while caching both
`_next_seq[stream]` **and** `_last_size[stream]`. The next `append_many()` then
sees `seq is not None` and `_last_size == size`, skips the in-lock rescan, and
keeps its optimistic default `ends_with_newline = True` — so no repair newline
is written and the new record is concatenated onto the killed writer's partial
line.

Reproduced (any process that asks for a cursor before it writes — exactly what
a server does at connect, `cursor()` → `next_seq()`):

```python
s = Store(root=root); s.append("run:wf_1", ..., data={"i": 1})
open(path, "ab").write(b'{"v":2,"seq":2,"ts":"2026-')   # killed writer
fresh = Store(root=root)
fresh.cursor("run:wf_1")                                 # <-- poisons the cache
rec = fresh.append("run:wf_1", ..., data={"i": 3})       # returns seq=3
```

Result on disk:

```
{"v":2,"seq":1,...,"data":{"i":1}}
{"v":2,"seq":2,"ts":"2026-{"v":2,"seq":3,...,"data":{"i":3}}
```

`fresh.stats["torn_repairs"] == 0`, `read_all()` returns **only** record 1, and
the record `append()` returned to the caller as durably stored is unrecoverable.
This is silent data loss in the file the module's own docstring calls "the
**system of record** and the crash-durable WAL", and it breaks R-24's named
invariant ("torn-tail write recovery") and GD-20's flock'd-append rule. Worse on
`DURABLE_STREAMS`: `custom-state` is "the ONE dataset not rebuildable from
`~/.claude`" (R-52), and this is the one stream whose fsync implies the record
survived.

`test_torn_tail_write_recovery` misses it because it appends from a fresh
`Store` without touching `next_seq()`/`cursor()` first.

**Fix (one line, plus a test):** do not seed `_last_size` in `next_seq()` —
drop `self._last_size[stream] = size` so the first append re-derives inside the
lock and sees the torn tail. If the rescan-avoidance is worth keeping, cache the
flag instead: `self._needs_nl[stream] = not ends_with_newline` in `next_seq()`
and start `append_many` with
`ends_with_newline = not self._needs_nl.get(stream, False)`. Add the regression
case `cursor()` → `append()` → assert `torn_repairs == 1` and
`len(read_all()) == 2`.

---

## MAJOR

### M1 — a line longer than `read_cap` makes the stream vanish silently and burns `read_cap` bytes every tick forever
`aggregator/tailer.py:318-344` (`more = new_offset < size and consumed > 0`),
`aggregator/tailer.py:347-361` (`read_complete_lines`),
same loop shape in `aggregator/store.py:727-735`.

`tail_once` correctly refuses to advance past an unterminated line, but when
`consumed == 0` it also clears `more`, so every looping caller concludes "caught
up". Observed with `read_cap=1000` on a file whose first line is 3000 bytes
followed by five short lines:

```
read_complete_lines(...)  ->  0 lines   (the file has 6)
tick0: reason=append lines=0 bytes=1000 deferred=1000 more=False offset=0
tick1: reason=append lines=0 bytes=1000 deferred=1000 more=False offset=0
tick2: ... unchanged, forever
```

Two distinct harms:

1. `read_complete_lines` returns a **wrong answer, silently** — its docstring
   promises "Every complete line of `path` right now … a file larger than
   `read_cap` still comes back whole", and it instead returns zero lines and no
   error. Later sub-plans use it for boot-time scans and fixtures.
2. the stall is *not* the "bounded and visible" state the code comment claims:
   `reason` stays `append`, no counter is bumped, and every tick re-reads
   `read_cap` bytes (8 MiB by default) of the same prefix — with a 250 ms poll
   that is ~32 MB/s of pure waste on one stalled stream, i.e. the direct
   opposite of GD-30's "per-tick work is O(bytes appended)" and of this module's
   own headline claim.

Trigger needs a single line > `read_cap`; the frozen corpus tops out at 872 KB,
but R-44 legislates for >8 MB payloads, so the case is contemplated by the plan,
not hypothetical.

**Fix:** distinguish "nothing to do" from "cannot make progress". When
`consumed == 0 and len(data) == capped and st.st_size - start > capped`, return
a distinct `reason` (e.g. `REASON_OVERSIZE_LINE`) with a flag/counter the poll
loop and `/health` can surface, and either (a) escalate that one read to
`st.st_size - start` for this file, or (b) keep the cap but let
`read_complete_lines` / `Store._read_lines` raise or log instead of returning a
short list. Add a test with a line > `read_cap` asserting both the surfaced
reason and that the loop-once helpers do not report success.

---

## MINOR

### m1 — the SD-2 stdlib guard cannot see an eager pymongo import in the file shape it will actually meet
`tests/test_stdlib_only.py:66-85` (`return top - lazy, lazy`),
`tests/test_stdlib_only.py:99-103`.

`imports_of` subtracts every function-level import name from the module-level
set, so a file that imports pymongo **both** at module level and lazily reports
`top == set()`. Verified against the guard's own function:

```python
src = "import pymongo\ndef client():\n    import pymongo\n"
imports_of(ast.parse(src))  ->  top: set()   lazy: {'pymongo'}
eager detected? []
```

That is precisely the shape `mongo_store.py` will have if anyone writes the
usual `try: import pymongo / except ImportError: pymongo = None` at the top plus
a lazy import in the client factory — and in a bare env (GD-21's target) the
subprocess arm passes too, because the guarded import raises nothing and loads
nothing. SD-2 makes this guard the normative enforcement of GD-21, so it should
not be defeated by the most likely real code.

**Fix:** compute module-level imports structurally instead of by subtraction —
walk only nodes that are not inside a `FunctionDef`/`AsyncFunctionDef`/
`Lambda`/`ClassDef` body (e.g. recurse over `tree.body`, descending into
`If`/`Try`/`With` but stopping at function boundaries) and report those as
`top`. Add a self-test on that helper with the two-import fixture above.

### m2 — `next_seq()`/`cursor()` cache is never invalidated, so a non-writing `Store` hands out a frozen cursor
`aggregator/store.py:523-541`, `aggregator/store.py:767-769`.

`next_seq` returns `self._next_seq[stream]` unconditionally once cached, with no
stat re-check. A reader instance (the server serving "resume from here") is
therefore stuck at its first observation:

```
reader cursor after 1 append: run:wf_1#000000000001
writer cursor after 5 appends: run:wf_1#000000000005
reader cursor after 5 appends: run:wf_1#000000000001   <-- stale
```

Direction of error is duplicate replay rather than loss, so it is not a blocker,
but `cursor()` is public and its docstring says it "names the last record
currently in `stream`", which is false for any consumer that does not write.

**Fix:** in `next_seq`, compare `os.stat(path).st_size` with
`self._last_size.get(stream)` and rescan on mismatch (the same signal
`append_many` already uses); or give `cursor()` a `refresh=True` path.

### m3 — `Checkpoint.from_dict` crashes on a null field despite promising it cannot
`aggregator/tailer.py:136-144`.

The docstring: "a checkpoint file written by an older Touch must never crash a
restart, it may only lose precision". `int(v)` over the known keys raises
`TypeError: int() argument must be … not 'NoneType'` for `{"offset": null}` — a
perfectly reachable JSON shape from a half-written or older state file, and the
restart path is the one place tolerance was the point.

**Fix:** per-key `try: int(v) except (TypeError, ValueError): continue` (drop
the field, keep the default), and extend `test_checkpoint_roundtrip` with
`{"offset": None}` / `{"offset": "x"}`.

### m4 — a dot-only stream component is accepted and writes a stream `streams()` can never find
`aggregator/store.py:127` (`_STREAM_RE`), `:334-336`, `:438-467`.

`..` is rejected but a single `.` is not, and `.` survives
`_escape_component` (it is in `_SAFE_PATH_CHARS`' allow set):

```
run:.      -> runs/events.jsonl
session:.  -> sessions/events.jsonl
run:a..b   -> rejected (good)
streams() after appending to "run:." -> []
```

So `run:.` and `session:.` write *above* their per-id directory, two different
ids collide with a directory root, and the resulting stream is invisible to
`streams()` — i.e. invisible to any GD-26 rebuild/replay that enumerates
streams. Silent, not loud, which is the opposite of this validator's stated job.

**Fix:** reject a stream whose post-`partition(":")` remainder escapes to `.`,
`..` or empty — e.g. require the remainder to match
`^[A-Za-z0-9][A-Za-z0-9._:+@=,%-]*$` **and** assert
`_escape_component(rest) not in (".", "..", "")`; add the ids above to
`test_stream_ids_and_paths`' rejection list.

### m5 — `encode_close` will send close codes the module's own `parse_close` rejects
`aggregator/ws.py:190-197` vs `aggregator/ws.py:306-323`.

`encode_close` only bars 1005/1006 and anything outside 1000-4999, so
1004 (reserved by §7.4.1) and the whole undefined 1012-2999 band are sendable,
while `parse_close` treats each of them as a 1002-worthy protocol violation:

```
encode_close(1004) -> OK; our own decoder REJECTS it (reserved/invalid close code 1004)
encode_close(2999) -> OK; our own decoder REJECTS it (reserved/invalid close code 2999)
```

Two Touch endpoints speaking to each other would fail the connection on a close
frame Touch itself generated, and the docstring's "may not be sent on the wire
(RFC 6455 §7.4.1)" is narrower than §7.4.1 actually is.

**Fix:** validate `encode_close` against the same predicate `parse_close` uses
(`code in _LEGAL_CLOSE_CODES or 3000 <= code <= 4999`); the existing
`test_encode_rejects_illegal_requests` table just gains 1004 and 1015.

---

## NITS

* **n1** `aggregator/ws.py:437` — `drain_frames` is documented for a
  `bytearray` and raises `TypeError: 'bytes' object does not support item
  deletion` on `bytes`. One `if not isinstance(buf, bytearray): raise
  TypeError("drain_frames mutates in place; pass a bytearray")` makes the
  contract loud instead of incidental.
* **n2** `aggregator/ws.py:383-390` — after a CLOSE frame, `FrameDecoder` keeps
  decoding and returning subsequent data frames (`[8, 1]`, `closed=True`).
  §5.5.1 says nothing further is processed after a close; ignoring (or
  erroring on) post-close frames would keep `closed` meaningful for the caller.
* **n3** `aggregator/store.py:593` — `assert tuple(record) == RECORD_KEYS`
  guards "serialization order is part of the contract" but disappears under
  `python -O`. Make it an `if … raise SchemaError`, or move the assertion into
  `test_record_shape` only (it is already there).
* **n4** `tests/test_stdlib_only.py:114`, `:152` and
  `tests/test_ws.py:314` — informational `check(True, …)` lines count as passes
  and, in the `test_ws` case, turn a skipped prior-art parity arm into a green
  tick. Prefer a plain `print()` for facts, and reserve `check` for assertions.
* **n5** `tests/test_ws.py:310` — permanently inserts
  `.claude/shared/monitoring` into `sys.path` for the rest of the process; harmless
  today, a shadowing trap once Touch grows a module named like a monitoring one.
  Insert, import, then remove the entry.

## Non-findings (checked, deliberately not raised)

* `control` stream pinned to `{asserted,touch}` — correct per GD-28 class 4
  (control intents *and acks* are Touch application state), not over-restriction.
* Closed `STREAM_KINDS`/`SINGLETON_STREAMS` vocabulary vs the D5 amendment's
  per-session control file — later sub-plans can register entries on the class
  dicts without editing `store.py`, so GD-15 is not violated.
* `_scan_seq`'s O(file) rescan on every append under multi-writer contention —
  out of contract (single writer per stream) and correctness is preserved.
* `compaction_in_progress` globbing the whole directory per changed tick — the
  coarse-grained deferral is documented and gated behind the stat-first short
  circuit.
