# sp-aggregator-core — adversarial critique, attempt 2

**Verdict: REJECTED** — 0 blockers, 2 majors, 6 minors, 5 nits.

Reviewed (full content; all files are new in an untracked tree):
`aggregator/tailer.py`, `aggregator/store.py`, `aggregator/ws.py`,
`tests/test_tailer.py`, `tests/test_store.py`, `tests/test_ws.py`,
`tests/test_stdlib_only.py` (with `aggregator/__init__.py` and
`tests/run_all.sh` read as unchanged context — mtimes 23:28 / 23:41, i.e.
attempt-1 artifacts).

Against: `touch-mongo-live-subplans.md` §sp-04, amendment items
R-22:aggregator / R-23 / R-24 / R-29 + GD-21…GD-30 + SD-2 / SD-10 / SD-11,
base plan R-22…R-29 + GD-11 / GD-15 / GD-20 / GD-24.

## Attempt-1 findings: all twelve independently re-verified as closed

Every one was re-run against the *current* source, not taken from the test
gate's word:

| # | closure evidence (reproduced here) |
|---|---|
| **B1** | `cursor()` → `append()` on a torn tail now writes the repair newline: `torn_repairs == 1`, `read_all()` returns both records, the torn line survives terminated. The `_needs_nl` cache is the right fix (it keeps the rescan-avoidance and the repair together as one observation). Also verified the harder shape the test does not cover — `next_seq()` *then* an external torn write — which correctly falls through to `reseeks == 1` + repair. |
| **M1** | `read_complete_lines(p, read_cap=1000)` on a 3000-byte first line now returns all 4/6 lines; bare `tail_once` reports `reason=oversize_line`; `Tailer` bumps `oversize_lines`, sets `stalled`, and — verified over three ticks — reads **0 extra bytes** while the file is unchanged. The `_stalled_at` backoff genuinely closes the ~32 MB/s GD-30 hole. |
| **m1** | `_eager_nodes` is a structural walk; the self-test `test_the_guard_detects_an_eager_import_beside_a_lazy_one` reproduces the exact defeating shape and now catches it, plus `try/except`, class-body and `if`-guarded imports. |
| **m2** | `next_seq` re-stats and rescans on a size change; `test_cursor_of_a_non_writing_reader_tracks_the_file` asserts the reader follows the writer to seq 5. |
| **m3** | `from_dict` is per-key tolerant; `{"offset": None/"x"/[1]}` all drop to the default with `st_ino` surviving. |
| **m4** | `run:.`, `session:.`, `run:a:.`, `run:`, `run:a::b` all raise `StreamError` from `validate_stream` *and* from `stream_path`. |
| **m5** | `encode_close` and `parse_close` share `is_legal_close_code`; probed 1000/1004/1005/1006/1012/2999/3000/4999/5000 — encode and parse agree on every one. |
| **n1–n5** | `drain_frames` raises `TypeError` on `bytes`; post-close bytes are dropped and counted (`post_close_bytes == 7` on a close+text feed); the key-order check is a `raise SchemaError`; `sys.path` is restored in `test_ws.py`; the surviving `check(True, …)` calls are all inside `except` blocks, i.e. real assertions. |

## Other things that hold up (verified, not assumed)

* **Suite**: `tests/run_all.sh` → `10 passed, 0 failed`, rc=0, with pymongo
  absent (the GD-21/R-56 bare-checkout arm).
* **Ownership**: `git status` shows only `aggregator/` and `tests/` added; no
  `.touch/` anywhere; HEAD is still `579446e` (3 commits) — no commit added.
  `.claude/skills/m-orchestrator/SKILL.md` + `network-recovery.md` were touched
  at 00:40 (after the implementer's 00:17–00:27 writes), but their content is
  a time-policy/network-recovery note — orchestrator/user in-flight state, not
  this sub-plan. Not held against sp-04.
* **GD-21/22/24/25/26/29**: zero pymongo, zero DB I/O, zero
  `$inc`/`$set`/`$unset`/delete/TTL surface, no BSON subdocument `_id`, no
  scalar `stream` field on disk (asserted by `test_record_shape`).
* **GD-28**: `custom-state`/`control` really do refuse `harness`/`derived`/
  `unknown`, and `kind`/`source` stay open at the tail so R-52 rides this file
  unchanged (8 custom-state kinds appended without touching `store.py`).
* **R-29**: probed independently — RFC accept-key vector, minimal-length
  enforcement in both the 16- and 64-bit forms, ping-during-fragmentation,
  UTF-8 split across a fragment boundary reassembling to `é`, `mask_bytes`
  correct and involutive over 1000 random bytes including a leading-zero
  payload, per-frame cap refused from the header. `drain_frames` is executed
  against the real imported `monitor_server.parse_client_frames`.
* Tests remain behavioural: real frozen fixtures, 20 MB byte-budget fixture,
  4-process concurrent append asserting `seqs == range(1, 401)`.

---

## MAJOR

### M1 — `Store._encode` never measures what it actually writes, so `MAX_RECORD_BYTES` is not a bound; an over-cap line permanently blinds the live `Tailer` for that stream
`aggregator/store.py:653-668` (`_encode`), consequence at
`aggregator/store.py:806-822` (`follow`) and `aggregator/tailer.py:74`
(`DEFAULT_READ_CAP`).

`_encode` measures `json.dumps(record)`, and when it is over the cap it stubs
**`data` only** — then re-serializes and returns without re-measuring. `ref`,
`kind`, `source`, `ts` and the stub's own `keys` list are all outside the cap.
Reproduced:

```python
s.append("run:wf_1", kind="log", provenance="touch",
         ref={"galaxy": "x" * (10 * 1024 * 1024)}, data={"k": 1})
# line bytes = 10,485,935   (MAX_RECORD_BYTES = 1,048,576, stats["oversize"] == 1)
```

A ref is not a hypothetical carrier: GD-11's open tail *requires* unknown ref
shapes to be "retained verbatim" (`test_ref_union_rejects_and_retains` asserts
exactly that), so an ingest sub-plan passing a harness subtree straight through
is the designed path. The stub is unbounded for the same reason —
`sorted(record["data"])[:64]` is 64 key *strings* of arbitrary length.

Two harms, both against invariants this sub-plan owns:

1. **The documented guarantee is false.** `follow()`'s docstring justifies its
   escalation with "Store-written lines are capped at `MAX_RECORD_BYTES`
   (1 MiB, well under the tailer's read cap), so the escalation can only ever
   fire on a foreign writer's line." Verified false. The class docstring's
   "the cap is a memory bound on readers (GD-20 'no unlocked appends without a
   length cap')" is likewise not enforced.
2. **It re-opens the M1 stall on Touch's own data.** Above
   `tailer.DEFAULT_READ_CAP` (8 MiB) the line is unreadable by the live poll
   path. Verified on the 10 MB line above:

   ```
   Tailer.poll -> reason=oversize_line  lines=0  stalled=True     # forever
   ```

   Every subsequent record appended to that `.touch/` stream is behind the
   wedge, so the stream goes dark for the live view while `read_all()` (which
   escalates) still returns it — an aggregator that can replay history but
   cannot serve it live is precisely the GD-22/GD-30 failure the amendment is
   about, and `store.py` is the one writer that must never produce it.

`test_oversize_record_is_stubbed_never_dropped:458-459` asserts
`len(line) < MAX_RECORD_BYTES`, but only for the oversize-**data** case, so it
passes while the invariant is broken.

**Fix:** make `_encode` a loop over the *encoded* blob, not the payload —
after stubbing `data`, re-serialize and, if still over the cap, stub `ref` the
same way (`{"oversize": True, "keys": [...]}` with each key truncated) and
finally hard-truncate the `keys`/`bytes` metadata; assert
`len(blob) + 1 <= MAX_RECORD_BYTES` before returning. Then either correct
`follow()`'s docstring or keep it and let the assertion be what makes it true.
Add a regression test with an oversize `ref` and with 64 megabyte-long data
keys, asserting the written line is under the cap and that a fresh
`Tailer(path)` (default cap) returns the record.

### M2 — `read_complete_lines` returns an empty list, silently, whenever any `.compact.tmp.*` is in the directory
`aggregator/tailer.py:395-414`, reached via `aggregator/tailer.py:337-342`.

`read_complete_lines` does not pass `skip_while_compacting=False`, so a
compaction anywhere in the directory yields `reason=REASON_COMPACTING`,
`more=False`, and the loop returns `out == []`. Reproduced on a 4-line file:

```
tail_once   -> reason=compacting  compacting=True
read_complete_lines -> 0 lines   (the file really has 4)
Tailer.drain        -> 0 lines
```

This is the *same defect class* the attempt-1 M1 fix explicitly outlawed for
this exact function — its own docstring now reads "returning a short list
silently is the one outcome a 'give me every line' helper may not have" — just
through a different door. It matters more here than the oversize door did:

* the helper is documented for "fixtures, boot-time scans", and the only place
  `.compact.tmp.*` exists is `~/.claude/projects/<slug>/`, i.e. exactly where
  boot-time transcript scans run;
* `compaction_in_progress` is directory-scoped by design ("we do not try to
  match it to a specific transcript"), so one compaction blanks **every**
  transcript in that project directory;
* `COMPACT_STALE_S` is 60 s, so the blank window is up to a minute;
* the return value carries no signal at all — a caller cannot distinguish
  "empty session" from "deferred", and `Tailer.drain()` has the same shape.

**Fix:** give the one-shot helper the outcome its contract demands. Either
default it to `skip_while_compacting=False` (a one-shot read of a file being
rewritten is the caller's decision, and `read_at`/`split_lines` already
tolerate garbage), or keep the deferral and make it visible — raise a
`CompactionInProgress` (or return `(lines, reason)`) instead of a bare `[]`.
Same treatment for `Tailer.drain()`, which should surface `last_reason`
rather than an indistinguishable empty list. Add a test that a compaction
tmp file does not make a 4-line file read as empty-and-successful, and fix
`test_compaction_backoff`'s opt-out arm (see n1) so it actually covers this.

---

## MINOR

### m1 — `TailLine.__len__` makes a blank line falsy, re-introducing the exact line-number shift the module exists to prevent
`aggregator/tailer.py:171-172`.

The module docstring is emphatic: "Blank lines are returned too (with
`text == ""`) — skipping them here would silently shift the line numbers of
everything after them." Then `__len__` returns `len(self.text)`, so:

```python
lines, _, _ = split_lines(b"a\n\nb\n", 0, 0)
[bool(l) for l in lines]        -> [True, False, True]
[l.line_no for l in lines if l] -> [1, 3]        # line 2 filtered out
```

`if line:` is the single most natural thing a downstream ingest loop writes,
and it silently drops precisely the records GD-24's `<sessionId>#<line:08d>`
keys depend on. `TailResult` deliberately defines `__bool__`; `TailLine`
defines `__len__` and inherits the implicit `__bool__` from it.

**Fix:** delete `__len__` (nothing uses it — `grep` finds no caller), or add
`def __bool__(self): return True`. Add a check to
`test_split_lines_positions` asserting `all(lines)` for a buffer containing a
blank line.

### m2 — `cursor_key` re-implements the GD-24 `_id` grammar outside `refs.ref_key`, unescaped
`aggregator/store.py:350-370`.

SD-11 is unconditional: "All `_id`s are strings from `refs.ref_key`". GD-24
gives `events` and `custom_state_events` the `_id` `<stream>#<seq:012d>` and
requires `%`-escaping of `% # | :` in **user-chosen** components. `cursor_key`
builds that string here, and `validate_stream` deliberately *permits* raw `:`
and `%` in a stream id. `store.py`'s own class docstring says the `_id`
grammar "lives in `mongo_store.py`, not here", while `cursor_key`'s docstring
says it *is* "the GD-24 event `_id`" — the file contradicts itself.

Concretely: `legacy:<task>` uses a user-chosen folder name (the tests already
exercise `run:legacy:touch-repo-recon`). A task folder containing `:` or `%`
produces a `cursor_key` that `refs.ref_key` (sp-05) will escape differently,
so the file-side cursor and the Mongo `_id` diverge and the
`{stream:1,seq:1}` unique index stops meaning what the mirror thinks it means.
Nothing in this sub-plan can be broken by it today, which is why it is minor —
but sp-05 will write the second implementation blind.

**Fix:** state in `cursor_key`'s docstring that it is the *cursor token* and
that `refs.ref_key` (sp-05) is the normative `_id` producer that must be
proven equal to it, and leave a named hook (`# SD-11: refs.ref_key must
round-trip this`) plus a note in the sp-05 hand-off. Better: have `cursor_key`
percent-escape `%` and `:` in the stream component now, so the two grammars
cannot diverge at all.

### m3 — `DURABLE_STREAMS` fsync the file but never the parent directory, so the first append to the one unrecoverable stream can vanish
`aggregator/store.py:738-739`, with `_ensure_dir` at `:496-499`.

`os.fsync(fh.fileno())` makes the *contents* durable but does not commit the
new directory entry created moments earlier by `open(..., "a+b")` /
`os.makedirs`. After a power loss the very first `custom-state` /
`control` record — the one R-52 calls "the ONE dataset not rebuildable from
`~/.claude`", and D7 calls "a legal record of intents" — can be gone with the
file itself. The docstring's "crash-durable WAL" and "Streams whose loss is
unrecoverable get an fsync per append" both overstate what is implemented.

**Fix:** when `durable` and the file was just created, `os.open(dirname,
O_RDONLY)` → `os.fsync` → close the parent directory (once per stream is
enough; cache a `set` of dirs already synced). Assert it in a test by
monkey-patching `os.fsync` and checking a directory fd is among the sync
targets on first append.

### m4 — `torn_repairs` / `oversize` are incremented before the write, so a rejected batch inflates them permanently
`aggregator/store.py:719` and `:665`.

`_build_record` can raise (bad `kind`, `provenance`, `ts`, `ref`, `data`) part
way through a batch, after `torn_repairs` was already bumped and after
`_encode` may have bumped `oversize` — while `fh.write(payload)` is never
reached, so nothing lands on disk. Reproduced:

```
append_many([good, {"provenance": "BOGUS"}])  -> SchemaError, file size unchanged
stats["torn_repairs"] == 1        # no repair was written
next successful append            -> stats["torn_repairs"] == 2   # for ONE real repair
```

These counters are the observability GD-29 leans on ("the duplicate-key
counter … is how you notice one"); double-counting makes them lie.

**Fix:** accumulate the deltas locally and fold them into `self.stats` only
after `fh.write(payload)` succeeds (they are already inside the lock, so a
local dict + one update is enough).

### m5 — nothing escalates an over-cap line on the *live* path, and `DEFAULT_READ_CAP` sits exactly on R-44's threshold
`aggregator/tailer.py:74`, `:444-470`.

`read_complete_lines` and `Store._read_lines`/`follow` all pass
`escalate_oversize_line=True`; `Tailer` — the class the ingest poll loop will
actually use — does not, and has no way to recover other than a caller
changing `read_cap`. R-44 legislates for payloads **>8 MB**, and
`DEFAULT_READ_CAP` is exactly 8 MiB, so the first document R-44 was written
for is also the first line that wedges the live tail. `test_line_longer_than_
the_read_cap` covers the stalled/counter behaviour but never a caller
recovering at the default cap.

**Fix:** give `Tailer` a bounded escalation policy — e.g. after N consecutive
`REASON_OVERSIZE_LINE` ticks on an unchanged file, retry once with
`escalate_oversize_line=True` up to a hard `max_line_bytes`, and only then
stay stalled with the named reason. Document the resulting worst-case tick
cost against GD-30.

### m6 — `normalize_ts` raises a bare `ValueError` on an unparseable legacy ts
`aggregator/store.py:186-203`.

Everything else in this module funnels rejections through `StoreError`
subclasses (`__all__` exports them for exactly that reason). `normalize_ts`
is the one documented to be *tolerant* of legacy input ("a legacy line is
history and cannot be fixed retroactively"), yet
`normalize_ts("not a ts")` → `ValueError: Invalid isoformat string`. The
legacy adapter (R-27, sp-09) reads RUNSTATE-6's mixed-format streams and will
hit it.

**Fix:** wrap `fromisoformat` and re-raise as `SchemaError` (or return `None`
with a documented contract), and add a case to `test_ts_format`.

---

## NITS

* **n1** `tests/test_tailer.py:295-297` — the `skip_while_compacting=False`
  opt-out is asserted *after* `os.unlink(tmpname)`, so no compaction is in
  progress and the arm proves nothing. Move the `os.unlink` after the check.
* **n2** `tests/test_store.py:526-529` — the "no pymongo in `store.py`" guard
  is a chain of `.replace()` calls on the source text; it breaks the moment
  the docstring rewords. `tests/test_stdlib_only.py` already does this
  properly via AST — delete the duplicate or call `imports_of`.
* **n3** `aggregator/ws.py:471-517` — `drain_frames` honours a 64-bit length
  field with no cap, so a hostile peer sending `0x7F` + a huge length makes
  the caller's buffer grow until the frame "completes". Prior-art parity is
  the requirement, so this is not a defect here, but the docstring should say
  the caller owns the read budget.
* **n4** `aggregator/tailer.py:152-158` — `int(True) == 1`, so
  `Checkpoint.from_dict({"offset": true})` yields offset 1 rather than the
  default. One `isinstance(v, bool)` guard matches the care taken elsewhere in
  the sub-plan (`validate_ref`/`normalize_tokens` both exclude bools).
* **n5** `aggregator/store.py:435,493-494` — `_locks` and `_next_seq` grow one
  entry per stream id forever. Bounded in practice by the number of sessions,
  but a long-lived aggregator over a machine's whole `~/.claude` history is
  the intended deployment; a note or an LRU would be honest.

## Non-findings (checked, deliberately not raised)

* `compaction_in_progress` globbing per changed tick — gated behind the
  stat-first short circuit, and `glob` does match the dot-prefixed pattern
  correctly (verified).
* `_scan_seq`'s 256 KB tail window — line counting is exact and independent of
  the window, so `max(lines, highest) + 1` is correct even when no parseable
  seq is in the tail.
* Percent-escaping in `_escape_component`/`_unescape_component` — verified
  injective; `run:a:b` and `run:a%3Ab` map to distinct directories and both
  round-trip through `streams()`.
* `follow()` not looping on `more` — the checkpoint advances, so the next tick
  continues; that is the intended O(delta) shape, not a loss.
* `decode_frame`'s `consumed` being relative to `offset` — only `feed()` uses a
  non-zero offset path, and it passes 0. Documented adequately.
* `control` stream pinned to `{asserted, touch}` — still correct per GD-28.
