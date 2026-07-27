# sp-watcher-templates-firstwave — TEST GATE, attempt 2

Verdict: **PASSED**. All targeted suites and the full-suite regression gate are
green, the three attempt-1 failures (F1 fabricated-green run badge, F2 toothless
static guard, F3 constant "anti-tautology" control) are genuinely fixed and now
falsifiable, and no edits landed outside the sub-plan's owned files.

## 1. Suite evidence

Targeted suites (each run from its own directory, standalone, non-zero exit on
failure), Python 3.13, no services running, no third-party packages installed
(`import pymongo` -> ModuleNotFoundError, confirmed):

| test file | rc | tail |
|---|---|---|
| `.claude/shared/monitoring/tests/test_watcher.py` | 0 | `ALL WATCHER TESTS PASSED` (176 ok lines, 0 skips) |
| `.claude/shared/monitoring/tests/test_server.py` | 0 | `all 21 tests passed` |
| `.claude/shared/monitoring/tests/test_shell.py` | 0 | `all sp-shell tests passed` (104 ok lines) |
| `.claude/shared/monitoring/tests/test_frontend.py` | 0 | `test_frontend.py: all assertions passed` (untouched file) |

Full-suite regression gate, verbatim from the repo root:

```
cd /home/laniakea/Projects/touch && rc=0; for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done; for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc
=> SUITE rc=0
```

That covers the four monitoring suites plus the earlier sub-plans'
`tests/test_bootstrap.py` (`all sp-repo-bootstrap tests passed`) and
`tests/test_fixtures.py` (`all sp-fixtures-freeze tests passed`). Zero skips
anywhere; nothing in this sub-plan needs pymongo or a live mongod, so the
GD-21/R-56 no-mongod arm is not exercised here and the bare-checkout
requirement holds (the suite passed with pymongo absent).

## 2. Attempt-1 failures — re-verified as fixed

### F1 — research template's failure path (fabricated green run badge) — FIXED
`.claude/skills/execute-research/templates/research.workflow.js`:
`const closeRun = async (state, summary)` (:85), called
`closeRun('failed', 'run failed: synthesis produced no plan')` (:239) and
`closeRun('done', ...)` (:249). Identical shape to the sibling
`implement.workflow.js` (:105 definition; `closeRun('failed', ...)` on both
divider throw paths :350/:358; `closeRun(allGreen ? 'done' : 'failed', ...)`
:463). A thrown research run can no longer paint `orchestrator complete done`.

### F2 — static guard is now falsifiable — FIXED
`tests/test_shell.py::test_templates_emit_terminal_events` dropped the catch-all
substring disjunct and now asserts, per template: a `state`-variable run-close
(`runStatus('orchestrator', 'complete', state`), the **absence** of any
hardcoded literal state (`not re.search(r"runStatus\(\s*'orchestrator',\s*'complete',\s*'")`),
`closeRun(state, summary)` arity, a `closeRun('failed'` throw path, a
`closeRun('done'`/ternary success path, and a terminal `plan done` emit.
Mutation check I ran (in a scratch copy of the source string, no file edited):
re-introducing the attempt-1 bug (`'orchestrator', 'complete', 'done'`) flips
the first two assertions from True/True to False/False — the guard would have
caught F1. It is not a tautology.

### F3 — retired-rule control now derives from the fixture — FIXED
`tests/test_watcher.py` defines `retired_rule(plan, decisive, last_result_ok)`
(:698) and replays the SAME real frozen journal through
`replay_journal(journal, rule=retired_rule, sequenced=True)` (:810), asserting
the retired predicate really fabricates `failed` badges
(`{research, synthesis} ⊆ failed plans`), that they carry the
`loop exited ->` detail, and that the whole run goes `failed`; then replays
under the current rule and asserts zero failed badges plus `outcome == "done"`.
Both directions are computed from the fixture — no literal empty dict anywhere.

## 3. Item verification (sub-plans §sp-03 + amendment/base plan items)

All owned items present in the tree and asserted non-tautologically:

- **R-07** — `_int_cfg`/`_int_env` deferred warnings, nested state-dir creation
  before first heartbeat, shrink/truncate rebuild (offset rewound to real size,
  derived plans cleared); live-subprocess arms.
- **R-08 / GD-10** — `close_state_for` / `close_detail` / `run_outcome` with the
  verbatim predicate; sequenced `last_plan` heuristic gated on
  `STRATEGY == "serial"`; verdict-less closes labelled "closed, no verdict";
  `orchestrator` excluded from the fold; interleaved-parallel arm asserts zero
  spurious badges and no running->done->running flap.
- **R-09** — script-side `runStatus`, `publishConfig` merging
  `max_plan_attempts`/`max_finalgate_attempts`/`strategy`, terminal `plan done`
  at the research barrier, after synthesis, per sub-plan loop exit, divide and
  finalgate; `FINALGATE_ATTEMPTS` replaces the literal bound; agent-emitted
  `synthesis plan done` prompt line removed. Failure path now correct (F1).
- **R-10** — `flock`'d single-line append in `status.sh` (python does the write),
  1 KB detail cap at both writers, out-of-enum state warns and still appends,
  `PARSE_FAILURES` in `health_payload()` and `/health`; 24-writer concurrency
  arm counts lines and finds zero torn records.
- **R-13** — stage-qualified labels, full 17-hex `id` with display-only
  `shortId`, adjacent `[touch]`+`[monitor]` parse, 4-line marker window,
  unknown marker keys tolerated, quoting-tolerant `STAGE_HINT`.
- **R-39** — `w: "agent"` / `w: "watcher"` from both writers, five-key core
  shape preserved, documented in `monitoring.md` as additive ("absent ⇒
  unknown writer").
- **R-40** — `should_exit` (terminal complete AND quiet window),
  `terminal_complete_seen`, `journal_quiescent` (in-flight agent blocks a stale
  complete), announced exit, clean rc 0, stays alive without a terminal
  complete; template epilogue kills by recorded `watcher.pid` only, no `pkill`,
  never stops the shared `monitor_server` — all guarded statically.
- **R-58** — forward fix plus the three real streams: two frozen journals
  (`tests/fixtures/mirror/r58-replay/.../wf_930e210a-6da`,
  `.../wf_cca84d59-933`) replayed for `strategy=None` and `"serial"` with zero
  `failed` badges, four legacy `tests/fixtures/legacy/*-events.jsonl` streams
  through `replay_plan_states`, failed-then-done renders `done`
  (last-event-wins, ts deliberately inverted), same-state duplicate a no-op,
  genuine failure (`touch-repo-recon`) still `failed`, plus the F3 control and
  the now-toothy template guard (F2).
- **R-01:guard / SD-3** — all eight `.gitignore` entries asserted verbatim plus
  both negative `git check-ignore` assertions and the positive
  checkpoint/mongo-dump ones.

Correctly out of scope and untouched: `monitor.html` / `test_frontend.py`,
R-11, R-17's full doc refresh, R-14/R-15/R-18/R-19/R-21, and the
CLAUDE.md/plan-file wording halves of R-40 (sp-15).

## 4. Ownership check (`git status`)

Modified: `decision_watcher.py`, `status.sh`, `monitor_server.py`,
`monitoring.md`, `tests/test_shell.py`, `tests/test_watcher.py`,
`tests/test_server.py`, `execute-research/templates/research.workflow.js`,
`implement-plan/templates/implement.workflow.js` — exactly the nine owned
files. Plus this task's own `events.jsonl` (expected monitoring state) and the
same pre-existing 3-line comment hunk in `.gitignore` (it references
`tests/fixtures/**/*.bson` — sp-01/sp-02 residue, unchanged since attempt 1,
not attributable to this sub-plan and correctly left alone). No new commits were
made by this sub-plan. `test_frontend.py` untouched, as required.

## 5. Conclusion

Gate PASSES: 4/4 monitoring suites + 2/2 earlier repo suites green
(`SUITE rc=0`), all three attempt-1 failures fixed with falsifiable assertions,
ownership clean. This satisfies the GD-23 hard precondition for mirror writes —
sp-04 and later may proceed.
