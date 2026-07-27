# sp-aggregator-core — test gate, attempt 4

**Verdict: PASS** — 0 failures. Targeted suites green, full-suite regression
green, ownership clean.

Changed by the implementer this attempt (declared):
`aggregator/store.py`, `aggregator/ws.py`, `tests/test_store.py`,
`tests/test_tailer.py` (mtimes 21:07–21:22 also cover `aggregator/tailer.py`,
which is owned by this sub-plan).

## 1. Targeted suites (owned by sp-04) — 4/4 green

| file | rc | evidence |
|---|---|---|
| `tests/test_store.py` | 0 | `all store tests passed` — 121 `check()` assertions, 33 test fns |
| `tests/test_tailer.py` | 0 | `all tailer tests passed` — 86 `check()` assertions, 18 test fns |
| `tests/test_ws.py` | 0 | `all ws codec tests passed` — 81 `check()` assertions |
| `tests/test_stdlib_only.py` | 0 | `all stdlib-only guard checks passed` (SD-2 two-file pymongo exception) |

`tests/run_all.sh` → **`10 passed, 0 failed, 10 file(s) total, 26s`**, rc=0.

Environment for all of the above: `python3 -c "import pymongo"` →
`ModuleNotFoundError` — i.e. this is the GD-21/R-56 **bare-checkout, no-mongod
arm**, and `test_pymongo_absence_is_the_tested_condition` reports
`pymongo is NOT installed here … either way the suite passes and Mongo tests
skip cleanly`. Nothing skipped silently, nothing errored.

## 2. Full-suite regression gate — 10/10 green

```
.claude/shared/monitoring/tests/test_frontend.py  PASS
.claude/shared/monitoring/tests/test_server.py    PASS
.claude/shared/monitoring/tests/test_watcher.py   PASS
.claude/shared/monitoring/tests/test_shell.py     PASS
tests/test_bootstrap.py    PASS
tests/test_fixtures.py     PASS
tests/test_stdlib_only.py  PASS
tests/test_store.py        PASS
tests/test_tailer.py       PASS
tests/test_ws.py           PASS
overall rc=0
```

No baseline failures, no new failures. No services were running for the run
and no third-party package was importable.

## 3. Sub-plan verification (`touch-mongo-live-subplans.md` §sp-04)

**Owned files all present:** `aggregator/__init__.py`, `aggregator/tailer.py`,
`aggregator/store.py`, `aggregator/ws.py`, `tests/run_all.sh`,
`tests/test_tailer.py`, `tests/test_store.py`, `tests/test_ws.py`, plus the
stdlib-only static guard as its own file `tests/test_stdlib_only.py`
(the sub-plan explicitly allows either placement).

**Tests assert behaviour, not tautologies.** Independently re-executed the
four attempt-2 critique findings that the changed files were supposed to
close, outside the suite:

* **M1 (cap bounds the written line, not one field)** — reproduced the
  critique's exact input: `append(ref={"galaxy": "x"*10MiB})` now writes a
  **174-byte** line (`MAX_RECORD_BYTES` = 1 048 576), `ref` stubbed to
  `{"oversize":true,"bytes":10485773,"keys":["galaxy"]}`, `data` preserved,
  `stats["oversize"] == 1`. A fresh `Tailer` at the **default** read cap
  returns the record with `stalled=False` — the live-view blackout is gone.
  Covered by `test_the_cap_bounds_the_written_line_not_one_field`, which also
  pins the 64×megabyte-key case (stub bounded in both dimensions).
* **M2 (compaction blanking a read)** — reproduced: a 4-line file with a
  `.compact.tmp.*` sibling now yields 4 lines from `read_complete_lines`;
  the deferral is only reachable via the explicit
  `skip_while_compacting=True` opt-in, which **raises `CompactionInProgress`**
  rather than returning `[]`. `Tailer.drain()` still backs off (correct for
  the poll loop) but now exposes `last_reason == "compacting"` and
  `last_result.compacting` — verified live. Covered by
  `test_a_compaction_never_makes_a_file_read_as_empty`, and
  `test_compaction_backoff`'s opt-out arm is now checked *before* the
  `os.unlink` (the attempt-2 n1).
* **m1 (blank line falsy)** — `split_lines(b"a\n\nb\n")` → `[True, True, True]`;
  the line-number shift is closed.
* **m3/m4** — regression tests exist and pass and are real:
  `test_durable_streams_fsync_the_directory_entry` spies `os.fsync` and asserts
  a **directory** fd is synced on first append, none on later appends, none for
  a rebuildable stream, and one again for a sibling new file in the same dir
  (defeating a naive per-directory memo);
  `test_counters_describe_only_what_was_written` asserts a rejected batch
  writes zero bytes and bumps neither `torn_repairs` nor `oversize`, then that
  one real repair counts exactly once.
* **m2/m5** — `test_cursor_keys_use_the_gd24_escaping` and
  `test_bounded_escalation_recovers_the_live_path` (escalate-once-per
  observation, `bytes_read` unchanged across idle ticks — the GD-30 budget
  holds).

**Ownership / git.** `git status --porcelain` for this sub-plan's area shows
only `?? aggregator/` and `?? tests/`. No `.touch/` directory anywhere. HEAD is
still `579446e` — **no commit was made** (R-02 belongs to sp-01). The other
modified paths (`.claude/shared/monitoring/*`, `.claude/skills/*`,
`.claude/local-orchestrators/*`) are pre-existing in-flight orchestrator state;
the one with an mtime inside this implementer's window,
`.claude/shared/monitoring/tests/test_frontend.py` (+23 lines), contains zero
references to `aggregator`/`store`/`tailer` — it is the concurrent
sp-watcher-templates-firstwave work, **not attributable to sp-04**, and it
passes.

## Failures

None.
