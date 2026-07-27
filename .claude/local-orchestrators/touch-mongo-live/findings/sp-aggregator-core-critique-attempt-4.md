# sp-aggregator-core — adversarial critique, attempt 4

**Verdict: APPROVED** — 0 blockers, 0 majors, 6 minors, 6 nits.

Reviewed in full (all files are new in an untracked tree, so "the diff" is the
file): `aggregator/store.py` (1017 lines), `aggregator/ws.py` (525),
`tests/test_store.py` (746), `tests/test_tailer.py` (497).
`aggregator/tailer.py`, `aggregator/__init__.py`, `tests/test_ws.py`,
`tests/test_stdlib_only.py` and `tests/run_all.sh` were read as context because
half of the attempt-2 fixes land there and the reviewed tests assert against
them.

Against: `touch-mongo-live-subplans.md` §sp-04 (R-22:aggregator, R-23, R-24,
R-29; SD-2/SD-10/SD-11), the amendment's GD-21…GD-30 and its §2 rows for
R-22/R-23/R-24, base plan R-22…R-29 + GD-11/GD-15/GD-20/GD-24, and the cited
research (RUNSTATE-15, MONITORING-9, PRIORART-14, LIVEFLOW-3, CUSTOMSTATE-4/7,
R-44's >8 MB rule).

Everything below was reproduced against the current source, not taken from the
test gate's word. Suite re-run here: `tests/run_all.sh` → **10 passed, 0
failed, rc=0, 27 s**, with pymongo absent.

## Attempt-2 findings: all thirteen independently re-verified as closed

| # | closure evidence (reproduced in this review) |
|---|---|
| **M1** (`_encode` did not bound the written line) | `append(ref={"galaxy": "x"*10 MiB}, data={"k":1})` now writes a **174-byte** line; the *ref* is the stub (`{"oversize":true,"bytes":10485773,"keys":["galaxy"]}`) and `data == {"k":1}` survives — "biggest first" really does spare the field that was not the problem. Both-huge (2 MiB each) → both stubbed, 207 bytes. A fresh `Tailer(path)` at the **default** 8 MiB cap returns the record and is `stalled == False`: the live-view blackout is gone. The 64×1 MB-key case is bounded by `STUB_MAX_KEYS`/`STUB_KEY_CHARS`, and `follow()`'s docstring now states the property `_encode` actually enforces. |
| **M2** (`read_complete_lines` blanked by any `.compact.tmp.*`) | A 4-line file with a fresh `.compact.tmp.z` beside it reads as `['1','2','3','4']`. `skip_while_compacting=True` raises `CompactionInProgress` naming the path — never a bare `[]`. `Tailer.drain()` still defers (poll-loop policy) but `last_reason == "compacting"` and `last_result.compacting` explain the empty list. |
| **m1** (`TailLine.__len__` made a blank line falsy) | `__len__` is gone and replaced by a comment explaining *why* it may not come back; `[bool(l) for l in split_lines(b"a\n\nb\n",0,0)[0]] == [True, True, True]`. `test_split_lines_positions:115` asserts `all(lines)`. |
| **m2** (`cursor_key` re-implemented the GD-24 grammar unescaped) | `cursor_key` now escapes `%`/`:` in both components except the one structural separator, carries the `# SD-11: refs.ref_key must round-trip this` hook, and `parse_cursor_key` is its exact inverse. Probed `run:legacy:touch-repo-recon`, `run:a%3Ab`, `run:%25`, `run:a%3`, `custom-state` — all round-trip, and the escape is prefix-free hence injective (`run:a:b` ≠ `run:a%3Ab`). The `#`/`|` omission is justified in-file by `validate_stream` rejecting both. |
| **m3** (durable streams never fsync'd the directory entry) | `_fsync_dir` walks the new file's directory up to `self.root`, only on first creation, deliberately un-memoized. The test is a real `os.fsync` spy asserting `S_ISDIR` on the synced fd: first durable append `[file, dir]`, second `[file]`, non-durable `[]`, and the *sibling* `control.jsonl` in the same directory syncs the entry again. Best-effort `try/except` so an exotic FS cannot fail an append. |
| **m4** (counters incremented before the write) | Deltas accumulate in a local dict and fold in only after `fh.write()`. Reproduced the old failure shape: a batch `[good, {"provenance":"BOGUS"}]` writes nothing, leaves `torn_repairs == 0` and `oversize == 0`, and the next real append counts the repair exactly **once**. |
| **m5** (nothing escalated an over-cap line on the live path) | `Tailer` gained a bounded policy: stall → `escalate_after` consecutive ticks on the *same* `(size, mtime_ns)` → **one** read up to `max_line_bytes` → stay honestly stalled past it. `test_bounded_escalation_recovers_the_live_path` proves the recovery arm, the >`max_line_bytes` arm and — with a byte counter, not a clock — that the escalation is not retried while the file is unchanged. |
| **m6** (`normalize_ts` leaked `ValueError`) | `"not a ts"`, `""`, `"2026-13-99T99:99:99Z"`, `None`, `[]` all raise `SchemaError`; the test also pins `issubclass(SchemaError, StoreError)` so the R-27 adapter's one `except` really covers it. |
| **n1** | The `skip_while_compacting=False` opt-out is now asserted *before* `os.unlink(tmpname)` (`test_compaction_backoff:343`), so the arm proves something. |
| **n2** | The string-`.replace()` pymongo guard is gone; `test_store.py` imports `imports_of` from the SD-2 guard and runs it over the real AST. |
| **n3** | `drain_frames`' docstring now states that the caller owns the read budget and points at `FrameDecoder(max_message_bytes=…)` for the enforcing path. |
| **n4** | `Checkpoint.from_dict` excludes bools explicitly; `{"offset": True, "st_ino": False} == Checkpoint()` is asserted. |
| **n5** | The unbounded caches are documented in `Store.__init__` with the honest bound (O(streams touched), every entry re-derivable). |

## Other things that hold up (checked, not assumed)

* **GD-21**: zero `pymongo`/`bson` in either reviewed module, eagerly or lazily;
  `test_no_reducer_lives_here` proves it through the AST guard rather than text.
  Whole suite green with pymongo absent.
* **GD-22 / GD-29**: no DB, no network, no client anywhere; the store is file
  appends under `flock` only.
* **GD-24**: `_id`/cursor tokens are strings in `<stream>#<seq:012d>`, zero
  padded so lexical order equals numeric order; no subdocument key anywhere.
* **GD-25 / GD-26**: no `$inc`/`$set`/`$unset`/delete/TTL surface, no deltas on
  disk, nothing that removes a record — the torn line is *kept* and counted.
* **GD-28**: `custom-state`/`control` really refuse `harness`/`derived`/
  `unknown`; `kind`/`source` stay open at the tail (8 R-52 kinds appended with
  `store.py` untouched).
* **R-24's own test list**: both ref arms, two streams holding the same seq,
  `(stream, seq)` round-trip, torn-tail write recovery — all present and
  behavioural (4-process concurrent append still yields `seqs == range(1,401)`).
* **R-29**: the codec is unchanged apart from the n3 docstring; minimal-length
  encoding, control-frame rules, close-code legality shared by encoder and
  parser, fragmentation + UTF-8 split, per-frame cap refused from the header.
* **Ownership**: only `aggregator/` and `tests/` are new; no `.touch/`
  anywhere in the repo; `HEAD` is still `579446e` (no commit). The only foreign
  files touched in the sp-04 window are
  `.claude/shared/monitoring/{monitor.html,monitoring.md,tests/test_frontend.py}`
  — the concurrent sp-watcher-templates-firstwave work, zero aggregator
  references. `.temp-develop/` (two PNGs, 14:45 and 16:52) predates this
  sub-plan's window entirely and is user state, not sp-04's.

---

## MINOR

### m1 — `Store.follow()` re-reads the whole tail **every tick, unbounded**, on a foreign over-cap unterminated line
`aggregator/store.py:1006-1007`, against `aggregator/tailer.py:418-430`.

`follow()` is the documented live-serve path ("per tick the server reads only
what was appended (GD-30)") and it passes `escalate_oversize_line=True` with no
`max_escalated_bytes`. Measured, with a spy on `tailer._read_at`, on a
`.touch/` stream carrying one good record plus a foreign 20 MB **unterminated**
tail:

```
tick1 reads: [8388608]              -> 1 record
tick2 reads: [8388608, 20971520]    -> 0 records
tick3 reads: [8388608, 20971520]    -> 0 records   (and so on, forever)
```

29 MB per tick at 250 ms ≈ 116 MB/s for one stream — the exact GD-30 shape the
`Tailer`'s bounded escalation (attempt-2 m5) was added to prevent, left open on
the store's own live path. `Tailer` remembers a stall and stops re-reading;
`follow()` is stateless and cannot. Note the escalation makes it *worse*: with
`escalate_oversize_line=False` the same file costs 8 MB/tick.

Not a major because the store can no longer produce such a line itself (M1's
cap holds, and a killed writer's partial line is < `MAX_RECORD_BYTES` since
every earlier record in the batch is newline-terminated), so it takes a foreign
writer in `.touch/`. But the whole torn-tail/`bad_lines` machinery exists
precisely to survive foreign and killed writers.

**Fix:** pass `max_escalated_bytes=tailer.DEFAULT_MAX_LINE_BYTES` (or
`8 * MAX_RECORD_BYTES`) in `follow()`, so the promotion is bounded like
`Tailer`'s; better, give `follow()` the same "do not re-read an unchanged
stalled file" memo, or document that the live server must drive `.touch/`
streams through `Tailer` and keep `follow()` for replay. Add a test that a
second `follow()` on an unchanged stalled stream reads no bytes.

### m2 — `next_seq()` / `cursor()` are O(file): a 1 KB append costs a full 20 MB re-read
`aggregator/store.py:616-647` with `_scan_seq` at `:572-614`.

The attempt-2 m2 fix (re-stat and rescan when the size moved) is correct, but
it rescans **from byte 0** even though the instance already holds the previous
`(size, next_seq)` observation. Measured on a 20.6 MB stream:

```
cached next_seq            0.000050 s
after a 1 KB append        0.0165 s   -> the whole 20 MB is read again
```

GD-30's own acceptance number for this repo is "append 1 KB to a 20 MB fixture
⇒ tick reads < 64 KB", and `test_byte_budget_is_incremental` enforces it for
the tailer. `cursor()` is the public "where is this stream now" API and its
docstring advertises it for "a server answering 'resume from here'", so sp-12
(R-55) is one plausible loop away from a per-tick full re-read of every stream.

**Fix:** make the rescan incremental — keep `(st_dev, st_ino, size, newlines,
highest_seq)` per stream, and when dev/ino match and `size > cached_size`, read
only `[cached_size, size)`, add its newline count to the cached count and parse
that window for the highest seq (the whole-file count is exactly the sum, so
the result is identical). Fall back to the full scan on rotation/shrink. At
minimum, put an explicit "O(file); do not call per tick" warning in
`cursor()`/`next_seq()` and hand it to sp-12.

### m3 — a rejected batch leaves an empty stream file behind, and `streams()` reports it as a stream
`aggregator/store.py:821-828`.

`_ensure_dir` + `open(path, "a+b")` run **before** any spec is validated, so a
rejected first append materializes the directory and a zero-byte
`events.jsonl`. Reproduced:

```python
s.append("run:wf_bad", kind="log", provenance="BOGUS")   # SchemaError
os.path.exists(p) -> True, size 0
s.streams()       -> ['run:wf_bad']        # a stream that never legally existed
s.read_all(...)   -> []
```

`streams()` is documented as the inverse mapping and as what "any GD-26 rebuild
that enumerates streams" uses, so a phantom stream propagates into the rebuild
(and into `writers`/`cursors` docs keyed on it). It also contradicts the
sibling invariant this suite already pins —
`test_seq_resumes_from_the_file:283-286` asserts that *reading* an unwritten
stream creates nothing.

**Fix:** validate the whole batch before touching the filesystem — either a
`_validate_spec()` pass over `specs` ahead of `_ensure_dir`, or build the
records with a placeholder seq and stamp the real seq inside the lock. That
also makes the "all-or-nothing" promise in `append_many`'s docstring structural
instead of incidental. Add the phantom-stream assertion to
`test_counters_describe_only_what_was_written`.

### m4 — `append()` still leaks a bare `TypeError` out of the JSON layer, and only for *large* records in one case
`aggregator/store.py:722-737` (`_dumps`, `_stub_for`), reached from `_encode`.

The m6 fix established the rule for this module: "a rejection this module makes
is a `StoreError`, never a bare exception leaking out of the stdlib". Two paths
still break it:

```python
s.append("run:wf_1", kind="log", provenance="touch", data={"x": {1, 2}})
# TypeError: Object of type set is not JSON serializable

s.append("run:wf_2", kind="log", provenance="touch",
         data={1: "x" * (MAX_RECORD_BYTES + 10), "b": 2})
# TypeError: '<' not supported between instances of 'str' and 'int'   (sorted() in _stub_for)
```

The second is the nastier shape: that payload is perfectly serializable and is
written fine when it is small — it crashes *only* once it crosses
`MAX_RECORD_BYTES`, i.e. a size-dependent failure in the one path that exists
to make oversize records safe. Any caller doing `except StoreError:` around an
ingest append (R-27 does exactly that) takes an unhandled crash instead.

**Fix:** wrap `_dumps` in `try/except (TypeError, ValueError) → SchemaError`
("data/ref must be JSON-serializable"), and use `sorted(value, key=str)` in
`_stub_for`. Add both cases to `test_data_must_be_a_dict`.

### m5 — the `reseeks` counter, GD-29's second-writer signal, is silently zeroed by any interleaved read and is asserted by no test
`aggregator/store.py:837-844`; `next_seq` at `:616-647`.

`append_many` only counts a reseek when *it* discovers the size moved. A
`cursor()`/`next_seq()` call in between absorbs the foreign growth (it rescans
and updates `_last_size`) without counting anything, so the append that follows
sees a matching size and stays silent. Reproduced with two `Store` instances on
one stream:

```
no interleaved read      -> reseeks = 1
with an interleaved cursor() -> reseeks = 0     # same two-writer race
```

A server that answers "resume from here" per connect is precisely the
interleaving. `grep` finds **no** test asserting `reseeks` at all, so nothing
holds this number in place — while the module docstring leans on it ("the
duplicate-key counter GD-29 exposes is how you notice one").

**Fix:** count the reseek in `next_seq` too (it is the same observation: the
file grew behind this writer), and assert both arms in a test —
`reseeks == 1` for the plain race and for the read-interleaved race.

### m6 — non-string `data`/`ref` keys make `append()`'s return value disagree with the disk, and can silently drop a key
`aggregator/store.py:689-706`.

Nothing validates key types, so JSON stringification happens invisibly:

```python
rec = s.append(..., data={1: "x"})            # returns {1: 'x'};  on disk {'1': 'x'}
rec = s.append(..., data={1: "a", "1": "b"})  # returns both keys; on disk {'1': 'b'}
```

`test_record_shape:106` asserts `json.loads(line) == rec`, which is exactly the
property that quietly fails here, and the returned record is what a caller (and
SD-1's mirror mappers, which map records) will hand onward. Unreachable from
`json.loads`-derived harness data — every key there is already a string — which
is why this is minor rather than major; reachable from Touch-authored custom
state.

**Fix:** reject non-string keys in `_build_record` with a `SchemaError` (one
`all(isinstance(k, str) …)` per dict), or normalize them and say so; add the
collision case to `test_data_must_be_a_dict`.

---

## NITS

* **n1** `aggregator/ws.py:163-181` — `encode_frame(..., mask=b"")` silently
  emits an **unmasked** frame: `mask_bit` is computed from truthiness and the
  4-byte length check lives inside `if mask:`. A client that passes an empty
  key by accident sends an illegal unmasked frame instead of getting the
  `ValueError` the docstring promises. Validate the key before the truthiness
  test.
* **n2** `aggregator/store.py:880-883` — `self.stats[...] += …` is folded under
  the **per-stream** lock, so two threads appending to *different* streams race
  the shared dict (`+=` on a dict entry is not atomic). One `threading.Lock`
  around the fold, or per-stream counters summed on read, matches the care
  taken elsewhere in the file.
* **n3** `aggregator/store.py:718` — `stream_provenance` re-hardcodes
  `("custom-state", "control")`, which is already `SINGLETON_STREAMS` (`:488`)
  and `DURABLE_STREAMS` (`:134`). Three lists that must agree; derive the pin
  from one of them.
* **n4** `aggregator/store.py:793-825` — `append(..., durable=False)` lets a
  caller disable the WAL fsync for `custom-state`/`control`, the one dataset
  R-52 calls unrebuildable. Nothing warns and no test pins it; either ignore
  the override for `DURABLE_STREAMS` or document that it exists for tests only.
* **n5** `tests/test_store.py:704-714` — `append_many`'s docstring claims "one
  lock, one syscall" (PRIORART-14's invariant), but the suite only asserts the
  outcome (no torn lines under concurrency). A spy on the file object counting
  `write()` calls for a 3-record batch would pin the invariant itself.
* **n6** `aggregator/store.py:414-419` — `cursor_key` accepts a `seq >= 10**12`
  and emits a 13-digit token that `parse_cursor_key` then refuses
  (`_CURSOR_RE` pins 12 digits). Unreachable, but a `raise` at build time is
  cheaper than the asymmetry.

## Non-findings (checked, deliberately not raised)

* `_encode`'s "biggest first" order stubbing a `ref`: a *known*-shape ref is
  always tiny (17-hex ids, a UUID, a pid), so a megabyte ref is by construction
  an unknown shape carrying no GD-24 join key — identity is never the thing
  sacrificed. Verified across the tie case (both 2 MiB → both stubbed).
* `MAX_RECORD_BYTES = 1 MiB` vs the corpus: the largest line in
  `tests/fixtures/` is 877,395 bytes (`mirror/records/oversize-line.jsonl`), so
  the docstring's "872 KB / real headroom" claim is accurate and no real corpus
  line is stubbed.
* `next_seq` scanning without `flock` while another thread appends: the cached
  `(size, needs_nl)` pair always comes from one read, and `append_many`
  re-derives whenever the size differs — so a mid-write observation can only
  cause an extra in-lock rescan, never a wrong seq. Probed the interleavings.
* `existed = os.path.exists(path)` being read outside the lock: the race can
  only cause an *extra* directory fsync, never a missing one.
* `_parse` counting `bad_lines` once per read (so two `read_all()` calls count
  a torn line twice) — the counter describes reads, which is what `/health`
  wants.
* `drain_frames`' uncapped 64-bit length — prior-art parity is the stated
  requirement and the docstring now assigns the budget to the caller (n3 of
  attempt 2, closed).
* `.temp-develop/` and the `.claude/shared/monitoring/` edits in the window —
  not this sub-plan's, not held against it.
