# sp-aggregator-core — test gate, attempt 2

**Verdict: PASS.** All owned suites green, full-suite regression green, no
ownership drift, no new commits, and every attempt-1 critique finding (B1, M1,
m1–m5, n1–n5) is now closed in code with a named regression test.

Environment: Python 3.13, **pymongo NOT installed** (`ModuleNotFoundError`), no
services running, no mongod — i.e. the bare-checkout / no-mongod condition the
GD-21 / R-56 arm demands. Suite is green under exactly that condition.

## 1. Targeted suites (owned by sp-04) — 100 % green

Run from the repo root, each file standalone, rc=0 each:

| file | result | ok-assertions |
|---|---|---|
| `tests/test_tailer.py` | PASS rc=0 | 90 `check(...)` sites, 16 test fns |
| `tests/test_store.py` | PASS rc=0 | 125 `check(...)` sites, 23 test fns |
| `tests/test_ws.py` | PASS rc=0 | 98 `check(...)` sites, 14 test fns |
| `tests/test_stdlib_only.py` | PASS rc=0 | 24 `check(...)` sites, 5 test fns |
| `tests/run_all.sh` | PASS rc=0 | `10 passed, 0 failed, 10 file(s), 26s` |

Monitoring-module suites re-run from their own dir: `test_server.py`,
`test_watcher.py`, `test_shell.py`, `test_frontend.py` — all PASS.

## 2. Full-suite regression gate — green

```
PASS .claude/shared/monitoring/tests/test_frontend.py
PASS .claude/shared/monitoring/tests/test_server.py
PASS .claude/shared/monitoring/tests/test_shell.py
PASS .claude/shared/monitoring/tests/test_watcher.py
PASS tests/test_bootstrap.py      (sp-01)
PASS tests/test_fixtures.py       (sp-02)
PASS tests/test_stdlib_only.py
PASS tests/test_store.py
PASS tests/test_tailer.py
PASS tests/test_ws.py
SUITE_RC=0
```

No baseline failure and no new failure. Nothing skipped for the wrong reason:
`test_pymongo_absence_is_the_tested_condition` reports "pymongo is NOT installed
here" and the Mongo-dependent arms skip cleanly, which is precisely the GD-21 /
R-56 no-mongod arm.

## 3. Attempt-1 critique findings — verified closed (independently reproduced)

* **B1 (blocker) — `next_seq()`/`cursor()` poisoned the torn-tail repair.**
  `store.py` now caches the flag rather than only the size:
  `self._needs_nl[stream] = not ends_with_newline` (:577) and
  `append_many` starts from `ends_with_newline = not self._needs_nl.get(stream, False)`
  (:703) with the in-lock rescan on size mismatch (:704), `torn_repairs`
  incremented at :719. Regression test present and green:
  `test_torn_tail_repair_after_a_cursor_read`.
* **M1 (major) — oversize line silently stalled the tailer.**
  `tailer.py` names the outcome: `REASON_OVERSIZE_LINE` (:92), `oversize_line`
  field on the result (:188), `escalate_oversize_line` option (:272) defaulted
  **on** for `read_complete_lines` (:406) so that helper keeps its
  "comes back whole" promise, plus a `/health`-shaped `oversize_lines` counter
  (:437) and a `_stalled_at` `(size, mtime_ns)` guard (:438-452) that stops the
  ~32 MB/s re-read burn. Regression test: `test_line_longer_than_the_read_cap`.
* **m1 — stdlib guard blind to eager-beside-lazy import.** Structural walk now;
  `test_the_guard_detects_an_eager_import_beside_a_lazy_one` covers eager,
  try/except-guarded, class-body, `if`-guarded and purely-lazy shapes.
* **m2 — frozen cursor on a non-writing Store.** `next_seq` re-stats:
  `if cached is not None and self._last_size.get(stream) == size_now` (:563).
  Test: `test_cursor_of_a_non_writing_reader_tracks_the_file`.
* **m3 — `Checkpoint.from_dict` crash on a null field.** Per-key
  `try: int(...) except (TypeError, ValueError): continue`. `test_checkpoint_roundtrip`
  now asserts `{"offset": None}`, `{"offset": "x"}`, `{"offset": [1]}` each drop
  one field while the rest survives, and `5.9 -> 5`.
* **m4 — dot-only stream component.** Reproduced live: `run:.`, `session:.`,
  `run:`, `run:..`, `run:a..b` all raise `StreamError`; `run:wf_1` accepted and
  discoverable via `streams()`.
* **m5 — `encode_close` vs `parse_close` disagreement.** Reproduced live:
  1004 / 1005 / 1006 / 1012 / 1015 / 2999 / 5000 now rejected; 1000 / 3000 /
  4999 accepted — same predicate both directions.
* **n1** `drain_frames(b"...")` → `TypeError: drain_frames consumes buf in
  place; pass a bytearray, not bytes`. **n2** post-CLOSE bytes no longer decode
  as frames (`if self.closed: ...` at :394/:412). **n3** no `assert` statements
  remain in `store.py`. **n4** the surviving `check(True, ...)` sites are
  except-branch confirmations of a raise, not informational filler; the two
  flagged informational ones in `test_stdlib_only.py` are gone. **n5**
  `test_ws.py` removes the monitoring dir from `sys.path` after import
  (:366-377).

## 4. Plan conformance and ownership

Owned files all present and non-trivial: `aggregator/__init__.py`,
`aggregator/tailer.py`, `aggregator/store.py`, `aggregator/ws.py`,
`tests/run_all.sh`, `tests/test_tailer.py`, `tests/test_store.py`,
`tests/test_ws.py`, `tests/test_stdlib_only.py` (the SD-2 guard as its own file).

Item spot-checks (behavioural, not tautological):

* **R-22:aggregator** — `run_all.sh` loops both suites, cwd per file,
  `PYTHONDONTWRITEBYTECODE=1`, `nullglob` so an empty suite is green; no pytest,
  no runner library. No `touch-visual/` created (correctly sp-13's).
* **R-23 / GD-20** — torn tail cut at last `\n`, multibyte split across ticks,
  in-place truncation, rotation, same-size rewrite policy, compaction backoff,
  checkpoint keyed to the source; `test_byte_budget_is_incremental` and
  `test_read_cap_bounds_one_read` assert O(delta) rather than restating it; real
  frozen fixtures compared byte-for-byte with a plain read and offsets asserted
  to tile the file exactly (journal 14 lines, `touch-mongo-live/events.jsonl`
  320 lines, oversize-line fixture).
* **R-24** — `store.py` carries no scalar stream/seq field (Mongo does not
  replace store.py), no reducer/liveness surface
  (`test_no_reducer_lives_here` prints an empty export list), one `…Z` ts
  format, four-key token records, `(stream,seq)` cursors, ref-union validator
  with retention, oversize record stubbed never dropped, 4-process concurrent
  append with `seqs == range(1, 401)`, batch order = file order.
* **R-29** — `test_drain_frames_parity_with_monitor_server` imports the real
  `monitor_server` function and compares behaviour (not a string diff); RFC
  §5.7 vectors both directions, `Sec-WebSocket-Accept` RFC vector, fragment
  reassembly, byte-by-byte streaming, close-code → 1002 mapping, size caps
  pinned to the RFC's 125 control-frame limit.
* **SD-10** — checkpoint identity is exactly `(st_dev, st_ino, size, offset)`
  in that order, `size < offset` its own explicit branch, `gen` monotonic across
  resets, sweep left to `mirror.py` (absent here, correct).
* **SD-2 / GD-21** — guard names exactly two permitted pymongo importers
  (`mongo_store.py`, `mirror.py`, both "not written yet"), and no third by
  analogy; every `aggregator.*` module imports with nothing third-party loaded.
* **SD-11** — `<stream>#<seq:012d>` zero-padded grammar; no `$inc`, `$set`,
  TTL, delete or `_id` construction anywhere in the three modules.

Ownership / tree hygiene:

* `git status` shows only `?? aggregator/`, `?? tests/` as this sub-plan's
  additions. The modified `.claude/**` paths (`decision_watcher.py`,
  `monitor_server.py`, `monitoring.md`, `status.sh`, the three monitoring test
  files, both workflow templates, `m-orchestrator/SKILL.md`, `.gitignore`) are
  sp-01/sp-03's, with mtimes 20:33–23:05 — all strictly older than this
  attempt's 00:17–00:27 edits to the four owned modules and four owned test
  files. No file outside the ownership list was touched this attempt.
* **No commit added** — `git rev-list --count HEAD` = 3, HEAD still
  `579446e` ("orchestration history", sp-01's C2). SD-6 respected.
* No `.touch/` directory created; no stray
  `.claude/shared/monitoring/events.jsonl`. `__pycache__/` is gitignored
  (`.gitignore:38`), so the compiled dirs under `aggregator/` and `tests/` are
  not tree pollution.

## 5. Failures

None. Zero failing assertions across the 10 suite files; nothing attributable
to the change, so no fix suggestions are owed.
