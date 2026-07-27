# sp-watcher-templates-firstwave — TEST GATE, attempt 4 — PASS

Read-only gate. No source or test file was edited. Environment: Python 3.13,
bare-checkout posture — no monitor_server / decision_watcher daemon started by
this gate, no third-party packages (`import pymongo` → `ModuleNotFoundError`,
confirmed), no mongod. Nothing in this sub-plan needs either, so there was no
skip arm to exercise.

## 1. Targeted suites (owned test files) — 100% green

Run from their own directory, standalone, exit status checked:

| file | result |
|---|---|
| `.claude/shared/monitoring/tests/test_watcher.py` | **40 checks, ALL PASSED**, rc 0 |
| `.claude/shared/monitoring/tests/test_server.py` | **21 tests passed**, rc 0 |
| `.claude/shared/monitoring/tests/test_shell.py` | **all sp-shell tests passed**, rc 0 |

`test_frontend.py` is not owned by this sub-plan and is untouched (still green,
see §2).

Evidence that the assertions are behavioral, not tautological:

- **R-58 end-to-end through `main()`** (the attempt-2 critique's m1): the real
  watcher process is run over the real research fixture and the stream is then
  asserted — `ZERO failed plan badges`, `no event carries the fabricated
  'loop exited ->' detail`, research fan-out closes `done` labelled
  `closed, no verdict`, synthesis closes `done`, run closes
  `orchestrator complete done`. Not string-matching on `main` source.
- **SD-4 last-event-wins over the frozen streams**: both
  `touch-mongo-live-events.jsonl` and `touch-full-recon-events.jsonl` fold to
  `done`, each paired with a control assertion that the fabricated `failed`
  line really is present in the fixture (so the test cannot pass by reading an
  empty/wrong file), plus a negative control — "a genuinely failed run still
  folds to failed".
- **R-40 self-exit**: four subprocess arms with real timing — driver `complete`
  exits the watcher (rc 0, announced); the watcher's OWN `complete` does not;
  `ORCH_NO_SELF_EXIT` survives an authorized close (4.0 s window vs the
  unprotected sibling acting in 1.01 s); a STALE `complete` does not kill a live
  watcher; no terminal `complete` ⇒ stays alive.
- **R-13**: six researchers ⇒ six distinct stage chips; every agent row carries
  the full 17-hex agentId with `shortId` display-only.
- **R-07**: truncation rewinds the offset and clears derived plan state.
- **R-10**: `test_health_parse_failure_counter`; `status.sh` out-of-enum warns on
  stderr and still writes; detail cap; concurrent appends atomic.
- **R-01:guard / SD-3**: `test_gitignore` asserts all eight entries verbatim
  **and** both negative assertions (nothing ignores
  `.claude/local-orchestrators/` itself, nothing ignores `events.jsonl` under
  it), plus the positive that watcher checkpoints there ARE ignored.
- **Template guards are executed, not just grepped**: `test_shell.py` actually
  runs node against the templates' verbatim `runStatus` shape (hostile plan id
  exits 0; the child's stderr warning is visible to the caller), alongside the
  static guards for the terminal `plan done` / `orchestrator complete done`
  calls, the published caps, and the ban on stamping new runs with the legacy
  sequenced-close detail.

## 2. Full-suite regression gate — green, `RC=0`

```
.claude/shared/monitoring/tests/test_frontend.py   all assertions passed
.claude/shared/monitoring/tests/test_server.py     all 21 tests passed
.claude/shared/monitoring/tests/test_shell.py      all sp-shell tests passed
.claude/shared/monitoring/tests/test_watcher.py    ALL WATCHER TESTS PASSED
tests/test_bootstrap.py                            all sp-repo-bootstrap tests passed
tests/test_fixtures.py                             all sp-fixtures-freeze tests passed
RC=0
```

No new failures; no baseline failure either (baseline four are all green).
Wall clock: `test_watcher.py` 22.3 s (real; user 0.8 s — subprocess/timing
windows), the two repo tests 0.7 s combined. Down from the ~30 s flagged as nit
n3 in the attempt-2 critique; still the slowest file in the suite, and still
purely a cost, not a failure.

## 3. Plan-conformance and ownership

Verified against `touch-mongo-live-subplans.md` §"sp-03 —
watcher-templates-firstwave" and the cited items in both plan files.

Owned items present and asserted: **R-07, R-08, R-09, R-10 (flock/health
parse-failure slice only), R-13, R-01:guard, R-39 (schema note in
monitoring.md — `cache_write`, `stale` state, `files_changed`, agent sub-object,
`shortId`/`identity`/`flags`/`unconventional`, exit windows + opt-out, retired
sequenced close), R-40 (watcher self-exit + template/driver epilogue halves),
R-58 (watcher + templates)**. Out-of-scope items stayed out: no R-11 server
work, no R-17 doc refresh, no R-14/R-15/R-18/R-19/R-21 template additions;
`test_frontend.py` untouched. Nits n1 (stderr of `status.sh` captured and logged
via `spawnSync`, with a first-use `status emitter ready (node:child_process)`
probe), n2 (`mkdirSync(TASK)` now precedes `readFileSync` in `publishConfig`),
n4 (`stream_terminal_close` docstring now names the `plan queued` seed lines
explicitly, decision_watcher.py:728) are all closed, each n1 half backed by an
assertion that would fail if reverted.

`git status` — modified files are exactly the nine owned ones:
`decision_watcher.py`, `status.sh`, `monitor_server.py`, `monitoring.md`,
`tests/test_shell.py`, `tests/test_watcher.py`, `tests/test_server.py`,
`execute-research/templates/research.workflow.js`,
`implement-plan/templates/implement.workflow.js`
(`+4281/-141` overall, of which the templates are +124/+152).

Also dirty, neither new nor attributable to this sub-plan (unchanged from
attempt 3, correctly left alone):

- `.claude/local-orchestrators/touch-mongo-live/events.jsonl` — this task's own
  monitoring stream; the gate itself appends to it.
- `.gitignore` — the same **comment-only** 3-line hunk (`1 file changed,
  3 insertions(+)`, all `#` lines explaining why `*.bson` is unanchored). No
  rule added, removed, or narrowed; sp-01 residue landed after C1.
- Untracked: prior findings files under this task's `findings/`, `tests/`
  (sp-01/sp-02 output), one `.temp-develop/*.png`.

No commit was made by this sub-plan — SD-6 respected.

## 4. Failures

None.

## 5. Conclusion

**Gate PASSES.** Full suite `rc=0` on a bare-checkout posture with no services
and no third-party packages; every owned item is present in the tree and covered
by assertions that exercise real behavior (including the watcher's `main()` over
real fixtures) rather than restating the implementation; ownership is clean.
This satisfies the GD-23 hard precondition for every mirror write — sp-04 and
later may proceed.
