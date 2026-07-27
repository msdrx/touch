# sp-aggregator-core (sp-04) — test gate, attempt 1: **PASS**

Read-only gate. Nothing edited; no services started; pymongo NOT installed in
this environment (so the GD-21/R-56 no-mongod arm is the arm that actually ran).

## 1. Targeted suites (owned files) — all green

Run from the repo root, `PYTHONDONTWRITEBYTECODE=1`, each file standalone:

| test file | rc | `ok:` assertions |
|---|---|---|
| `tests/test_tailer.py` | 0 | 63 |
| `tests/test_store.py` | 0 | 109 |
| `tests/test_ws.py` | 0 | 114 |
| `tests/test_stdlib_only.py` | 0 | 12 |

## 2. Full-suite regression gate — green

`bash tests/run_all.sh --keep-going` → `10 passed, 0 failed, 10 file(s) total, 26s`
(rc 0). Files, in the runner's order: `tests/test_bootstrap.py`,
`tests/test_fixtures.py`, `tests/test_stdlib_only.py`, `tests/test_store.py`,
`tests/test_tailer.py`, `tests/test_ws.py`, then the four monitoring tests
(`test_frontend.py`, `test_server.py`, `test_shell.py`, `test_watcher.py`) each
run with its own dir as cwd.

Independently re-run by the loop the prompt specifies:
- monitoring: `test_frontend rc=0`, `test_server rc=0`, `test_shell rc=0`,
  `test_watcher rc=0` (baseline stays green — no regression introduced);
- repo: `test_bootstrap rc=0` (65 ok), `test_fixtures rc=0` (181 ok) — the two
  earlier sub-plans' suites are unaffected by this change.

No third-party package is installed and no daemon was running during the run, so
the "bare checkout" condition is the tested condition, not an assumption.

## 3. Verification against the plans

Owned items (`sp-04` in `touch-mongo-live-subplans.md`): R-22:aggregator, R-23
(base + amended clause), R-24 (stands unchanged), R-29; SD-2, SD-10, SD-11,
GD-20 copy-verbatim list.

Present in the tree, all four owned modules plus five owned test artifacts:
`aggregator/__init__.py` (54 L), `aggregator/tailer.py` (411 L),
`aggregator/store.py` (769 L), `aggregator/ws.py` (474 L),
`tests/run_all.sh` (executable, +x), `tests/test_tailer.py`,
`tests/test_store.py`, `tests/test_ws.py`, `tests/test_stdlib_only.py`.

Item-by-item, each claim checked against source + a real assertion (not a
tautology):

- **R-22 / SD-2** — `aggregator/__init__.py` imports nothing from its own
  package (leaf modules stay independently importable) and states GD-21.
  `tests/test_stdlib_only.py` is the static guard, born naming exactly the two
  permitted pymongo files (`mongo_store.py`, `mirror.py`) *before they exist*,
  and it additionally proves the runtime half in a **subprocess** (`sys.modules`
  contains nothing third-party after importing each module) — that arm keeps
  working for files not yet written, so the suite is never red between
  sub-plans. `run_all.sh` uses `shopt -s nullglob` so an empty suite is green
  by definition, and `--list` reflects real collection.
- **R-23 / SD-10 / D6** — `Checkpoint.identity()` is literally
  `(st_dev, st_ino, size, offset)` in D6's order; `tail_once` has both reset
  triggers as separate, named branches: `(st_dev,st_ino)` change ⇒
  `reason="rotated"`, and `st_size < ck.offset` ⇒ `reason="shrunk"` (the branch
  RUNSTATE-15 found missing in `decision_watcher.py`). Torn tail is verbatim
  prior art: `split_lines` cuts at `data.rfind(b"\n")` and never reports the
  remainder as consumed, so the offset cannot advance past an incomplete line.
  Reads are incremental — stat-first short circuit returns `unchanged`
  *without opening the file*. Signals re-ingest only (`TailResult.reset` +
  monotonic `Checkpoint.gen`); no DB, no JSON parsing here — the GD-26 sweep is
  correctly left to `mirror.py` (sp-06). Tests cover truncation, rotation,
  torn tail, multibyte split across ticks, missing-file-keeps-checkpoint,
  read-cap bounding, compaction backoff, checkpoint round-trip, and — the
  strongest arm — three real frozen fixtures (`journal.jsonl` 14 lines,
  `touch-mongo-live-events.jsonl` 320 lines, `oversize-line.jsonl`) asserted
  byte-for-byte against a plain read *and* offsets asserted to tile the file
  exactly, plus a copy-then-grow live-append case. The byte counter, not a
  clock, is what the incrementality test asserts (GD-30).
- **R-24** — store.py is unmistakably the system of record, not a Mongo shim:
  no pymongo anywhere (`test_no_reducer_lives_here` asserts the absence by
  name), no scalar `stream`/`_id`-grammar leakage. `seq` is per event-log file,
  re-derived **inside the `flock`** whenever the file grew behind our back, and
  `_scan_seq` returns `max(line count, highest stored seq) + 1` so a torn or
  garbage line consumes a number and cannot cause a duplicate. Appends are one
  `flock`'d `write()` per batch with `fsync` for `DURABLE_STREAMS`. Ref union
  is open at the tail with both arms real: malformed known shapes raise
  `RefError` (non-17-hex agentId, non-UUID uuid, `procStart` pinned to a
  *string*, bool excluded from int pins), unknown shapes classify as
  `"unknown"` and are retained; `legacy:<task>:<id8>` exempted per GD-14. One ts
  format `…Z` enforced on write, `normalize_ts` tolerant on read. Token records
  always four keys, non-int rejected rather than silently zeroed; no deltas on
  disk (GD-25). `provenance` mandatory, closed five-value enum, with
  `custom-state`/`control` refusing `harness`/`derived`. R-24's four named test
  cases are all present and non-trivial: both ref arms; two streams legally
  holding the same seq; `(stream, seq)` cursor round-trip (and "a bare seq is
  never a cursor" as a rejection); torn-tail write recovery — that last one
  asserts the partial line is *terminated and kept*, counted in
  `torn_repairs`/`bad_lines`, seqs stay unique, and both good records survive.
  Extra real coverage: 4 concurrent writer processes × 100 appends ⇒ 0 torn
  lines, 400 records, `reseeks` observed; oversize record stubbed, never
  dropped; `follow()` incremental with reset semantics.
- **R-29** — pure functions, no I/O: `test_module_does_no_io` asserts the import
  set is exactly `{__future__, base64, dataclasses, hashlib, os}` and that
  `open/print/exec/eval` never appear. RFC 6455 §5.7 byte vectors are hard-coded
  and asserted in **both** directions (encode and decode), including the
  126/127 minimal-length-encoding rejections and the high-bit 64-bit length.
  Fragmentation reassembly, control-frame interleaving mid-message, byte-by-byte
  feeding, close-code legality (§7.4.1), UTF-8 validation ⇒ 1007, size caps ⇒
  1009, masking-direction violations ⇒ 1002. The "masked client frames dropped
  unread" clause is honoured by `drain_frames`, and the parity claim is
  *executed*, not asserted: the test imports `monitor_server` and compares
  `drain_frames` against the real `parse_client_frames` on the same bytes
  (observed `ok: drain_frames matches monitor_server.parse_client_frames on
  8 bytes`), skipping cleanly if that import is unavailable.
- **SD-11 / GD-24** — every id built through `cursor_key` as
  `<stream>#<seq:012d>` (zero-padded so lexicographic order equals numeric);
  `validate_stream` rejects `#`, `|`, path separators, `..`, control chars, so
  an unescaped component fails fast rather than writing to a wrong path. No
  `$inc`-style accumulation or reducer surface exists in this layer.

**Ownership**: `git status --porcelain` shows the only new paths are
`aggregator/` and `tests/` (untracked as wholes) — no file outside the sub-plan's
ownership list was touched by this attempt. The modified entries under
`.claude/shared/monitoring/`, `.claude/skills/*/templates/` and `.gitignore` all
carry mtimes of 19:37–23:05, i.e. sp-01/sp-02/sp-03's earlier work, whereas every
sp-04 file has an mtime of 23:28–23:42. No commit was made (SD-6 respected).
No stray `.touch/` directory was created by the suite; `__pycache__/` is already
gitignored (`.gitignore:38`).

## 4. Advisory notes (NOT gate failures, no fix required this attempt)

1. `tests/test_stdlib_only.py:imports_of` computes module-level imports as
   `top - lazy`. A future `mongo_store.py` that imports pymongo **both** at
   module level and inside a function would therefore pass the
   "pymongo is imported lazily" check. The subprocess arm would still catch it
   once pymongo is installed, but on a bare machine it would not. Suggested
   tightening when sp-05/sp-06 land: compute the eager set directly from
   module-level `Import`/`ImportFrom` nodes instead of subtracting the lazy set.
2. Two `check(True, ...)` calls exist (`test_the_exception_is_named_and_narrow`,
   `test_pymongo_absence_is_the_tested_condition`). Both are explicitly
   documented as informational prints of environment facts, and each sits beside
   a real assertion, so they do not inflate the green claim — but they are the
   only tautological lines in the sub-plan's suite.
3. `tailer.tail_once` sets `more=False` when a single line exceeds `read_cap`
   (`consumed == 0`), so `Tailer.drain()` stops rather than spinning. That is the
   documented, bounded behaviour (`deferred == bytes_read` makes it visible), but
   a caller polling only via `drain()` will make no progress on such a file until
   the line is terminated. Worth a `/health` counter when `server.py` (sp-12)
   lands.

## Verdict

PASS — 4/4 owned test files green (298 assertions), full suite 10/10 files
green including the four monitoring baselines, all owned items (R-22:aggregator,
R-23, R-24, R-29 + SD-2/SD-10/SD-11) implemented with behavioural, fixture-backed
tests, and no edits outside the sub-plan's ownership.
