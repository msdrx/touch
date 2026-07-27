# sp-watcher-templates-firstwave — TEST GATE, attempt 3

**Verdict: PASS.** 4/4 monitoring suites green, 2/2 pre-existing repo suites
green, full-suite `rc=0`, ownership clean, and every attempt-2 critique finding
(m4, m5, n1, n2) is closed with falsifiable assertions. Only nit n3 (suite
wall-clock) remains open — non-blocking.

Read-only gate: no source or test file was edited, nothing was committed,
nothing reverted or stashed.

---

## 1. Targeted suites (all owned test files, run from their own dir)

```
cd /home/laniakea/Projects/touch/.claude/shared/monitoring/tests
python3 test_watcher.py    -> rc 0   "ALL WATCHER TESTS PASSED"
python3 test_shell.py      -> rc 0   "all sp-shell tests passed"
python3 test_server.py     -> rc 0   "all 21 tests passed"
python3 test_frontend.py   -> rc 0   (not owned; run as regression)
```

## 2. Full-suite regression gate

```
cd /home/laniakea/Projects/touch
for t in .claude/shared/monitoring/tests/test_*.py; do (cd $(dirname $t) && python3 $(basename $t)); done
for t in tests/test_*.py; do python3 "$t"; done
```

```
PASS .claude/shared/monitoring/tests/test_frontend.py
PASS .claude/shared/monitoring/tests/test_server.py
PASS .claude/shared/monitoring/tests/test_shell.py
PASS .claude/shared/monitoring/tests/test_watcher.py
PASS tests/test_bootstrap.py        (sp-01, baseline)
PASS tests/test_fixtures.py         (sp-02, baseline)
SUITE_RC=0
```

Bare-checkout / no-services posture verified:

- `grep -rn pymongo .claude/shared/monitoring/ .claude/skills/` -> **no hits**.
  Nothing in this sub-plan imports a third-party package, so the GD-21/R-56
  no-mongod arm is vacuously satisfied here; no Mongo test arm exists in the
  owned files to skip.
- No daemon, port, or network dependency: the four subprocess arms in
  `test_watcher.py` spawn `decision_watcher.py` against temp state dirs only.
- Fixture dependence is guarded: every fixture-backed arm
  (`WF_829`, `tests/fixtures/legacy/*.jsonl`, `_fixture()` in
  `test_server.py`) tests `os.path.isfile` and prints `skip - …` instead of
  failing, so the monitoring module stays usable outside this repo.

Syntax/static sanity on all six changed files:
`node --check` OK on both `*.workflow.js`; `ast.parse` OK on
`decision_watcher.py` + `monitor_server.py`; `bash -n status.sh` OK.

## 3. Item verification against the plans

Checked `touch-mongo-live-subplans.md` §sp-03 against the tree. Every owned
item is present and asserted by a behavioral (non-tautological) test:

- **R-07 / R-08 / GD-10** — `close_state_for` / `close_detail` / `run_outcome`
  with the verbatim predicate, `last_plan` heuristic gated on
  `strategy=="serial"`, verdict-less closes labelled "closed, no verdict".
  Now additionally exercised *through the real `main()`* (see m5 below).
- **R-09** — script-side `runStatus` + `publishConfig`; guarded statically in
  `test_shell.py:330-400` for the terminal `plan done`, the variable-state
  `runStatus('orchestrator','complete',state,…)`, `closeRun(state, summary)`
  arity, and the absence of a hardcoded close state.
- **R-10** — `flock`'d single-line append, 1 KB detail cap at both writers,
  `PARSE_FAILURES` surfaced by `/health`
  (`test_server.py::test_health_parse_failure_counter`).
- **R-13** — stage-qualified labels, full 17-hex `id`, display-only `shortId`.
  Asserted end-to-end: `len(chips)==6` distinct researcher chips,
  `all(len(i)==17)`, `shortId == id[:8]` for every emitted agent block.
- **R-39** — `w:"agent"` / `w:"watcher"` from both writers; five-key core shape
  preserved; documented additively in `monitoring.md`.
- **R-40** — `should_exit` (terminal complete AND quiet window), stale-complete
  immunity, stays alive with no terminal complete, clean rc 0; epilogue kills by
  recorded `watcher.pid` **verified against `/proc/${pid}/cmdline` before
  `process.kill`**, no `pkill`, never touches the shared `monitor.pid`.
- **R-58** — forward fix plus all three affected real streams, see below.
- **R-01:guard / SD-3** — all eight `.gitignore` entries asserted verbatim, both
  negative `git check-ignore` assertions, plus the positive checkpoint and
  mongo-dump ones (`test_shell.py::test_gitignore`, 14 ok lines).

Correctly out of scope and untouched: `monitor.html`, `test_frontend.py`, R-11,
R-17's full doc refresh, R-14/R-15/R-18/R-19/R-21, and the CLAUDE.md/plan-file
wording halves of R-40 (sp-15).

### Attempt-2 critique findings — all closed

- **m4 (agent-block schema drift + `monitor.html` re-key consequence)** — closed.
  `monitoring.md:38` now shows the full agent block including `shortId`,
  `identity`, `flags`, `unconventional`; `:45-46` document each key and state
  the normative rule *"Identity is `id`, and `id` is the FULL 17-hex agentId …
  readers key rows on `id` and never on `shortId`"*, plus the carried
  double-row consequence for `monitor.html` and the `legacy:<task>:<id8>`
  bridge. Guarded by two `test_shell.py` doc assertions.
- **m5 (R-58 asserted only against a re-implementation of `main()`; 1 of 3
  streams and the frozen event streams uncovered)** — closed on all three
  counts:
  - `test_watcher.py:1296-1352` adds the subprocess arm through the **real
    `main()`** over `tests/fixtures/run-wf_829e6f58/.../wf_829e6f58-b2f`
    (the journal that produced the historic fabricated badge): zero `failed`
    plan events, no `loop exited ->` detail anywhere, `research` and
    `synthesis` both close `done`, the research close carries
    "closed, no verdict", and the run closes `orchestrator complete done`.
    A regression *inside* `main()` now fails the gate, not just a string guard.
  - Third stream covered: `test_server.py:354-380` folds
    `touch-aggregator-events.jsonl` (whose `research plan failed
    "loop exited -> synthesis"` at line 571 has **no** corrective `done`) and
    pins the handshake with the sp-09 re-labeler — every surviving
    `stage=plan, state=failed` line starts with `loop exited ->`. Verified
    independently: the fixture does contain exactly that line.
  - Frozen event streams are read: `fold_plan_states()` over
    `tests/fixtures/legacy/touch-mongo-live-events.jsonl` and
    `touch-full-recon-events.jsonl` asserts SD-4 last-event-wins in **file**
    order (the corrective line's `ts` is deliberately earlier, so a ts sort
    would resurrect the failure), and each arm also asserts the fabricated line
    is genuinely present — so the test cannot pass on an empty/wrong fixture.
    Negative control: `touch-repo-recon-events.jsonl` still folds to `failed`
    and none of its details match the re-label predicate.
- **n1 (`stdio:'ignore'` discarded status.sh's stderr)** — closed.
  `runStatus` uses `spawnSync` with `encoding:'utf8'`, logs `r.stderr`, and a
  one-shot `log('status emitter ready (node:child_process)')` probe makes an
  import failure visible. Statically guarded (`no stdio:'ignore'`,
  `r.stderr` + `log(` present) in both templates.
- **n2 (`mkdirSync` after `readFileSync`)** — closed; `fs.mkdirSync(TASK,
  {recursive:true})` now precedes the read in `publishConfig`.

### Open, non-blocking

- **n3 — suite wall-clock.** `test_watcher.py` still takes **~29 s**
  (`real 0m29.288s`; `user 0m0.765s` — almost all of it is fixed
  `wait=`/`sleep` windows in the five subprocess arms, now including the 90 s-cap
  R-58 e2e arm). Not a failure and not attributable to a defect, but it is paid
  on every gate for the rest of this pass. Suggested (out of this gate's scope,
  and a test-only change): replace the fixed windows with a poll for the child's
  exit and for the expected event line, e.g. loop on
  `proc.poll() is not None and expected_line_present(state_dir)` with a short
  sleep and the current value only as a hard timeout.
- **n4 (docstring word about `plan queued` seed lines vs
  `stream_terminal_close`)** — cosmetic; not verified as addressed and not
  gate-relevant.

## 4. Ownership check (`git status`)

Modified, exactly the nine owned files:
`decision_watcher.py`, `status.sh`, `monitor_server.py`, `monitoring.md`,
`tests/test_shell.py`, `tests/test_watcher.py`, `tests/test_server.py`,
`execute-research/templates/research.workflow.js`,
`implement-plan/templates/implement.workflow.js`.
`test_frontend.py` untouched, as required.

Also dirty, neither new nor attributable to this sub-plan:

- `.claude/local-orchestrators/touch-mongo-live/events.jsonl` — this task's own
  monitoring stream (expected; the gate itself appends to it).
- `.gitignore` — the same **3-line comment-only** hunk present since attempt 1
  (it explains why `*.bson` is unanchored and references
  `!tests/fixtures/**/*.bson`, i.e. sp-01/sp-02 residue landed after C1). No
  rule was added, removed, or narrowed; `git diff --stat` = `1 file changed,
  3 insertions(+)`, all comment lines. Correctly left alone.
- Untracked: prior findings files under this task's `findings/`, `tests/`
  (sp-01/sp-02 output), and one `.temp-develop/*.png`.

No commits were made by this sub-plan (`HEAD` still `579446e orchestration
history`, sp-01's C2). SD-6 respected.

## 5. Conclusion

**Gate PASSES.** Full suite `rc=0` on a bare-checkout posture with no services
and no third-party packages; all attempt-2 majors and blocking nits closed with
assertions that would fail if the fix were reverted; ownership clean. This
satisfies the GD-23 hard precondition for every mirror write — sp-04 and later
may proceed.
