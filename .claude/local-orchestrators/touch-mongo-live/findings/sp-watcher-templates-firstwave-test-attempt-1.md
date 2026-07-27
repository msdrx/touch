# sp-watcher-templates-firstwave — TEST GATE, attempt 1

Verdict: **FAILED** (suites are 100% green; the gate fails on item verification —
one owned file ships a fabricated-green terminal event, and the static guard that
was supposed to cover exactly that call is written so it cannot catch it).

## 1. Suite evidence (all green)

Targeted (each run from its own dir, standalone, non-zero exit on failure):

| test file | result |
|---|---|
| `.claude/shared/monitoring/tests/test_watcher.py` | PASS — "ALL WATCHER TESTS PASSED", 0 skips (both R-58 journals present) |
| `.claude/shared/monitoring/tests/test_server.py` | PASS — `all 21 tests passed` (5 new: health counter, last-event-wins, 3 R-58 fixture arms) |
| `.claude/shared/monitoring/tests/test_shell.py` | PASS — `all sp-shell tests passed` (new: writer attribution, detail cap, bad-state warn, 24-writer flock concurrency, template terminal-event guard, SD-3 gitignore list + negatives) |
| `.claude/shared/monitoring/tests/test_frontend.py` | PASS (untouched, as owned-file list requires) |

Full-suite regression gate (repo root, no services running, no third-party
packages needed): `PASS` for all four monitoring tests **plus** `tests/test_bootstrap.py`
and `tests/test_fixtures.py` (earlier sub-plans) — `SUITE rc=0`. No new failures,
no baseline regressions, nothing requires pymongo/mongod (no Mongo arm in this
sub-plan).

Spot-checks that the new assertions are not tautologies: R-58 replay drives the
real frozen journals
(`tests/fixtures/mirror/r58-replay/…/wf_930e210a-6da/journal.jsonl`,
`…/wf_cca84d59-933/journal.jsonl`) through `close_state_for`/`run_outcome` for
both `strategy=None` and `strategy="serial"` and asserts zero `failed` plan
badges; `test_r58_real_streams_render_corrected` reads verbatim
`tests/fixtures/legacy/*-events.jsonl`; `test_r58_genuine_failure_is_not_a_fabrication`
is a real negative control (`touch-repo-recon` stays `failed`);
`test_status_concurrent_appends_are_atomic` spawns 24 real `status.sh` writers
with 9 KB details and counts lines. Ownership check: `git status` shows exactly
the nine owned files modified (plus this task's own `events.jsonl`, expected).
The uncommitted `.gitignore` comment hunk is sp-01/sp-02 residue (it references
`tests/fixtures/**/*.bson`), not this sub-plan's edit — no action here, but it is
uncommitted work in a file this sub-plan does not own, so do not touch it.

## 2. Failures

### F1 — `research.workflow.js` emits `orchestrator complete DONE` on its failure path (fabricated green run badge)

- File: `.claude/skills/execute-research/templates/research.workflow.js:74` (definition), `:227` (failure-path call).
- Code:
  ```js
  const closeRun = async (summary) => {
    await runStatus('orchestrator', 'complete', 'done', summary)   // state hardcoded
    …
  }
  …
  if (!synth || !synth.plan_file || !synth.item_count) {
    await runStatus('synthesis', 'plan', 'failed', 'synthesis produced no plan items')
    await closeRun('run failed: synthesis produced no plan')       // ← emits state=done
    throw new Error(…)
  }
  ```
- Failure scenario: a synthesizer returns no `plan_file`. The stream then holds
  `synthesis plan failed` followed by `orchestrator complete **done**` with
  detail `run failed: synthesis produced no plan`. `task_status()` treats the
  reserved `orchestrator` card as authoritative and short-circuits on
  `orch in ("done","failed")`, so the home-grid tile reports the run **done**
  for a run that produced no plan — the exact mirror of the fabricated-FAILED
  badge this sub-plan exists to kill, and a direct violation of GD-10's "never a
  silent green". `monitoring.md` and the base plan both spell the terminal event
  as `orchestrator complete done|failed`; the sibling template got this right
  (`implement.workflow.js` `closeRun(state, summary)`, called with
  `'failed'` on both divider failure paths).
- Attribution: new code, added by this sub-plan under R-09/R-40 in an owned file.
- Fix: parameterize it exactly like the implement template —
  `const closeRun = async (state, summary) => { await runStatus('orchestrator', 'complete', state, summary); … }`,
  then `closeRun('failed', 'run failed: synthesis produced no plan')` at :227 and
  `closeRun('done', \`research complete: …\`)` at :237.

### F2 — the R-58 static guard for that call cannot fail (catch-all disjunct)

- File: `.claude/shared/monitoring/tests/test_shell.py:263-266`.
- Code: the check is
  `re.search(r"runStatus\(\s*'orchestrator',\s*'complete',\s*'done'", src) or re.search(…,\s*state", src) or "runStatus('orchestrator', 'complete'" in src`.
- Failure scenario: the third disjunct matches any `runStatus('orchestrator',
  'complete'` occurrence whatsoever, so the guard passes for a template whose
  terminal state is hardcoded, wrong, or a literal `'info'`. That is why F1 ships
  green. R-58's clause asks for a guard that the templates contain the terminal
  `plan done` **and** `orchestrator complete done` calls; as written it asserts
  only that the substring exists.
- Fix: drop the catch-all disjunct and assert both arms of the contract per
  template: a success-path `'done'` (or a `state` variable) **and**, where the
  template has a failure path, that the same emitter is reachable with `'failed'`
  — e.g. require `runStatus('orchestrator', 'complete', state` plus
  `closeRun('failed'` in both templates once F1 is fixed.

### F3 — `test_watcher.py:638` "anti-tautology" control is itself constant

- File: `.claude/shared/monitoring/tests/test_watcher.py:638-640`.
- Code: `old_rule = "done" if {}.get("research") else "failed"` — a literal empty
  dict, so `old_rule` is `"failed"` for any input; the check
  `old_rule == "failed" and len(entries) > 1` reduces to "the fixture has >1
  line". It proves nothing about the retired rule, which is the one thing it
  claims to prove (the surrounding R-58 assertions are fine and do real work).
- Fix: replay the same journal through the retired predicate for real — e.g.
  compute `decisive` from the replay and assert
  `("done" if decisive.get("research") else "failed") == "failed"` **and**
  `dw.close_state_for("research", decisive, last_result_ok) == "done"` from the
  same `replay_journal` state, so the control is derived from the fixture rather
  than from a literal.

## 3. Item verification (per sub-plans §sp-03)

Present and substantiated:

- **R-07** — `_int_cfg`/`_int_env` deferred warnings, nested state-dir creation
  before the first heartbeat, shrink/truncation rebuild (offset rewound to the
  journal's real size, derived plans cleared). Live-subprocess arms.
- **R-08 / GD-10** — `close_state_for` / `close_detail` / `run_outcome`; the
  verbatim predicate is in the source, the sequenced heuristic is gated on
  `STRATEGY == "serial"`, verdict-less closes are labelled "closed, no verdict",
  `orchestrator` excluded from the fold, finalgate decision text keyed on
  (plan, role) with no phantom critique.
- **R-09** — script-side `runStatus`, `publishConfig` merging
  `max_plan_attempts`/`max_finalgate_attempts`/`strategy`, terminal `plan done`
  at the research barrier, after synthesis, per sub-plan loop exit, divide, and
  finalgate; `FINALGATE_ATTEMPTS` replaces the literal `2` bound; the
  agent-emitted `synthesis plan done` prompt line removed. **Defective on the
  research failure path — F1.**
- **R-10** — `flock`'d single-line append in `status.sh` (python does the write,
  no shell redirect), 1 KB detail cap at both writers, out-of-enum state warns
  and still appends, `PARSE_FAILURES` counter surfaced by `health_payload()` and
  wired into `/health`.
- **R-13** — stage-qualified labels (six parallel researchers → six labels),
  full 17-hex `id` with display-only `shortId`, `[touch]`+`[monitor]` adjacent
  parse, 4-line marker window (prose marker below it ignored), unknown marker
  keys tolerated, `STAGE_HINT` tolerant of the templates' quoting.
- **R-39** — `w: "agent"` / `w: "watcher"` on every line from both writers,
  five-key core shape preserved, documented in `monitoring.md` as additive with
  "absent ⇒ unknown writer".
- **R-40** — `should_exit` (terminal complete **and** quiet window),
  `terminal_complete_seen` (driver event or own settle), `journal_quiescent`
  (in-flight agent blocks a stale complete event), exit announced, clean rc 0,
  stays alive without a terminal complete; template epilogue `closeRun` kills by
  recorded pid only, no `pkill`, guarded by the static check.
- **R-58** — forward fix + three real streams replayed (two journals via the
  watcher predicates, four legacy `events.jsonl` streams via
  `replay_plan_states`), failed-then-done renders `done` (last-event-wins in
  file order, ts deliberately inverted in the fixture), same-state duplicates a
  no-op, genuine failure preserved. Template terminal-call guard exists but is
  toothless — F2.
- **R-01:guard / SD-3** — all eight entries asserted verbatim plus both negative
  `git check-ignore` assertions and the positive checkpoint/mongo ones.

Out of scope and correctly untouched: `monitor.html` / `test_frontend.py`, R-11,
R-17's full doc refresh, R-14/R-15/R-18/R-19/R-21, the CLAUDE.md/plan-file
wording halves of R-40.

## 4. What a green attempt 2 needs

F1 (one-line signature change + two call sites) and F2 (tighten the guard so F1
could not have passed), plus F3 (make the control derive from the fixture).
No other change; do not touch `.gitignore` or any non-owned file, and do not
commit.
