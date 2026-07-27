# sp-watcher-templates-firstwave — TEST GATE, attempt 5 — PASS

Read-only gate. No source or test file was edited; only executed and inspected.

## 1. Targeted suites (owned by this sub-plan) — 100% green

Run from `.claude/shared/monitoring/tests/` (stdlib, standalone, rc=0 each):

| file | result |
|---|---|
| `test_watcher.py` | `ALL WATCHER TESTS PASSED` (rc 0) |
| `test_shell.py` | `all sp-shell tests passed` (rc 0) |
| `test_server.py` | `all 22 tests passed` (rc 0) |
| `test_frontend.py` | `all assertions passed` (rc 0) — not owned, run as neighbour |

No third-party packages installed for the run, no daemons started, no network.
`decision_watcher.py` / `monitor_server.py` / `status.sh` import and execute on
bare stdlib; nothing in the owned set references `pymongo`.

## 2. Full-suite regression gate — rc=1, but **no new failure**

```
cd /home/laniakea/Projects/touch
rc=0
for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done
for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done
exit $rc                                                    # -> GATE_RC=1
```

All four monitoring tests green. Two repo-level files fail:

- **`tests/test_mirror.py`** — 3 failures:
  `…proven by the call count: the held ticks made no attempt`,
  `the first generation lands` (`counts() == {"records": 3, "stream_meta": 3}`),
  `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  The live-mongod arm skipped cleanly (`TOUCH_MONGO_URI` unset), as GD-21/R-56
  requires.
- **`tests/test_sessions.py`** — 1 failure:
  `wipe + --rebuild reproduces a byte-identical fingerprint`. Live-Mongo arm
  skipped cleanly.

**Not attributable to this sub-plan.** Both files exercise
`aggregator/{mirror,mongo_store,refs,tailer,sessions}.py` only — their imports
are `from aggregator import mirror | mongo_store | refs | tailer` plus stdlib.
`grep -n "decision_watcher\|status\.sh\|monitor_server\|workflow\.js"` over both
files returns **zero** hits, so no code path of theirs reaches any of the nine
files this attempt touched. The failing assertions are drainer hold/generation
and rebuild-fingerprint semantics — sp-06 mirror-deploy / sp-07 sessions-arm
territory, and sp-mirror-deploy is one of this pass's still-red loops with an
interrupted implementer (its partial edits are explicitly out of bounds here).
These are pre-existing red state from other owners, not a regression introduced
by attempt 5 — the gate treats them as baseline. Every other repo test file
(`test_bootstrap`, `test_fixtures`, `test_store`, `test_tailer`, `test_ws`,
`test_refs`, `test_mongo_store`, `test_ingest`, `test_legacy`, `test_agents`,
`test_reducer`, `test_custom_state`, `test_slots`, `test_api`,
`test_server_core`, `test_touch_frontend`, `test_usage`, `test_docs`,
`test_register`, `test_e2e_sim`, `test_stdlib_only`, `test_mongo_deploy`)
completed without failing.

## 3. Attempt-4 critique defects — all closed with non-tautological arms

Attempt 4 was REJECTED with 3 major + 4 minor. Each now has a real guard:

- **M-1** (watcher's own settle pass invalidated the driver's close, killing
  R-40's authorized exit). Fixed and covered twice: unit arms at
  `test_watcher.py:615-647` assert `exit_authorized(ev_settled) is True` under
  the *shipped* ordering (agent close → watcher settle `plan done` → watcher
  `complete done`), that a settle `plan done` is a close and not a sign of life,
  that a card genuinely *moving* (`queued`) after the close still cancels it,
  and that a foreign-writer close is neutral. The e2e arm at `:1377-1404`
  drives the real watcher and asserts the settle events landed *after* the
  driver close, that the exit still fires, and that it takes the **driver**
  route, not the abandoned one. `:1534+` is the negative control (watcher-only
  close ⇒ no authorized exit).
- **M-2** (SIGTERM raced the 1 s poll, losing the last result/verdict/tokens).
  `test_watcher.py:1445-1530`: the SIGTERM'd watcher exits rc 0 after draining,
  the final agent result and orchestrator decision line survive, its usage
  enters the totals, the drain announces why it stopped — plus a control with
  an unhandleable signal proving the drained content is genuinely absent
  without the fix (so the arm is not vacuous).
- **M-3** (the R-10 write-integrity test passed with the lock deleted).
  `test_shell.py:329-372` is now behavioral: the test process holds `LOCK_EX`
  on `events.jsonl`, launches one `status.sh`, asserts it **blocks** and appends
  nothing while held, then completes rc 0 and lands exactly one line after
  release. This arm fails if the flock is removed. `:374-390` adds source
  guards that both append sites take `LOCK_EX`/`LOCK_UN`. The misleading
  ">8 KiB" claim is corrected in-place (`:291` note).
- **m-1** — one `resolve_config() -> (path, dict)` resolver
  (`decision_watcher.py:60`) used by both `config_path()` and `read_config()`;
  arms at `test_watcher.py:668-690` prove the watched path and the applied
  values agree even with a corrupt state-dir config.
- **m-2** — `PARSE_FAILURES.pop(events_path, None)` now on the stat-failure and
  `OSError` paths (`monitor_server.py:169,178,183`); `test_health_parse_failure_counter`
  covers it.
- **m-3** — the settle pass folds the stream's last terminal `plan` event before
  emitting (`test_watcher.py:649-666` for the fold, `:1409-1436` for the
  adopt-not-duplicate behaviour).
- **m-4** — `status.sh` imports `fcntl` defensively (`status.sh:56 except
  ImportError`) and skips the lock when absent; asserted for **both** writers at
  `test_shell.py:383-389` via the `try: import fcntl … except ImportError` +
  `if fcntl is not None: fcntl.flock` source patterns.

## 4. Plan conformance (subplans.md §"sp-03 — watcher-templates-firstwave")

Owned items present and asserted, not merely mentioned:

- **R-07** nested state dir, deferred cap warnings, shrink/inode rebuild — live
  process arms.
- **R-08 / R-09 / R-13** (= R-58's execution scope): the e2e replay over the
  frozen real run emits **zero** `failed` plan badges, carries no fabricated
  `loop exited ->` detail, closes the research fan-out and synthesis plans
  `done` with the "closed, no verdict" label, closes the run
  `orchestrator complete done`, gives six researchers six distinct stage chips,
  and puts the full 17-hex `agentId` on every agent row with `shortId`
  display-only alongside.
- **R-10 slice** — flock on both append sites (behavioral, above) + `/health`
  parse-failure counter (`test_server.py::test_health_parse_failure_counter`).
- **R-39** — `w` is additive on both writers, five-key core preserved
  (`test_watcher.py:326-333`, heartbeat line at `:1262`).
- **R-40** — self-exit requires journal quiet **and** an agent-written terminal
  close; stale-complete and badge-only arms prove neither alone suffices; the
  abandoned route is distinguishable from the driver route by its detail text.
- **R-58 static guards** — `test_shell.py::test_templates_emit_terminal_events`
  asserts both templates emit the terminal `plan done` and
  `runStatus('orchestrator','complete',…)` (present at
  `implement.workflow.js:144`, `research.workflow.js:118`).
- **R-01:guard / SD-3** — `test_gitignore` covers the module-dir `events.jsonl`
  and `.watcher-state.json`, `.touch/`, `.touch*/`, `settings.local.json`,
  `*.pid`, `mongo-data/`, `mongo-dump/`, `*.bson`, plus the negative assertions
  (nothing ignores `.claude/local-orchestrators/` itself or `events.jsonl`
  underneath it).
- **SD-4** — read-side last-event-wins fold verified on both real streams plus a
  genuinely-failed control (`test_server.py::test_r58_*`,
  `test_watcher.py` SD-4 arms).

## 5. Ownership — clean

Files modified by attempt 5 (mtimes 2026-07-27 07:11–07:25Z) are exactly the
nine owned files:

```
status.sh                          07:11:56
monitor_server.py                  07:12:17
execute-research/templates/research.workflow.js   07:12:47
implement-plan/templates/implement.workflow.js    07:12:47
monitoring.md                      07:13:41
tests/test_server.py               07:16:37
tests/test_watcher.py              07:19:08
tests/test_shell.py                07:22:53
decision_watcher.py                07:25:20
```

`monitor.html` (07-26 03:05) and `tests/test_frontend.py` (07-26 02:51) are
untouched by this attempt, as §sp-03 requires. The other dirty paths in
`git status` (`CLAUDE.md`, `README.md`, `inception.md`, `.gitignore`, the three
`SKILL.md` files, the task folders) all predate this attempt and belong to
sp-01/sp-15 or to live orchestrator state. No commit was made.

## Verdict

**PASS.** Targeted suites 100% green; the only red files in the full suite are
`tests/test_mirror.py` and `tests/test_sessions.py`, owned by other (still-red)
sub-plans and provably unreachable from anything this sub-plan changed.
