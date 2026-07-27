# sp-watcher-templates-firstwave — adversarial critique, attempt 1

**Verdict: REJECTED.** 2 blockers, 4 majors, 7 minors/nits.

Scope reviewed: `git diff` of the nine owned files (all nine are tracked and
modified after sp-01's C1/C2, so the diff is the whole change-set). Suites
re-run by me: `test_watcher.py` (ALL PASSED), `test_server.py` (21/21),
`test_shell.py` (all passed), `test_frontend.py` (passed) — the gate's green
claim holds, and that is part of the problem: two of the shipped defects are
*certified* green by guards that cannot fail.

Ownership: clean. Only the nine owned files are modified. The uncommitted
`.gitignore` comment-only hunk (`+# *.bson is unanchored on purpose …`) touches
a **sp-01-owned** file and is not attributable to this diff (sp-01 already
committed the SD-3 entry list); no action for this sub-plan, but the divider's
one-file-one-owner rule was bent by whoever wrote it. No stray
`events.jsonl`/`.watcher-state.json` in the module dir. No pymongo, no Mongo
code, no delete verbs, no network/secret handling — GD-21/22/24/25/26/27/28/29/30
are not in this sub-plan's blast radius and are not violated.

---

## BLOCKERS

### B1 — `research.workflow.js` emits a **green run badge for a failed run** (confirms gate F1)
`.claude/skills/execute-research/templates/research.workflow.js:74` (`closeRun`),
used at `:227`.

```js
const closeRun = async (summary) => {
  await runStatus('orchestrator', 'complete', 'done', summary)   // state hardcoded
```
```js
await closeRun('run failed: synthesis produced no plan')          // :227
```

The state is baked in, so the failure path appends
`orchestrator complete done "run failed: synthesis produced no plan"`. Three
independent contract breaks:

* GD-10 verbatim: templates emit `orchestrator complete done "<summary>"` **on
  the success path** — not on the throw path.
* `monitor_server.task_status()` short-circuits on the orchestrator card
  (`if orch in ("done","failed"): return {"status": orch …}`), so the home-grid
  tile reports the run **done**. A user sees green for a run that threw.
* `decision_watcher.terminal_complete_seen()` accepts `done|failed`, so this
  line also arms the R-40 self-exit with a lie.

This is precisely the "fabricated badge" failure mode R-58 exists to kill,
re-introduced in the same pass, and it is asymmetric with the sibling template
(`implement.workflow.js:88` correctly takes `closeRun(state, summary)` and passes
`'failed'` at `:335`/`:344`).

**Fix:** give the research template the same signature as its sibling —
`const closeRun = async (state, summary) => { await runStatus('orchestrator',
'complete', state, summary) … }` — then `closeRun('failed', 'run failed:
synthesis produced no plan')` at `:227` and `closeRun('done', …)` at `:237`.

### B2 — R-40 self-exit kills **live** watchers: any stale `complete` line in the stream is treated as terminal
`.claude/shared/monitoring/decision_watcher.py:578-599` (`terminal_complete_seen`),
`:601-616` (`journal_quiescent`), wired at `:1103`.

`terminal_complete_seen()` returns True on the **first** event anywhere in
`events.jsonl` with `stage=="complete"` and `state in ("done","failed")` —
ignoring file order, ignoring `plan`, and ignoring which run wrote it. The
docstring of `journal_quiescent()` *names* the hazard ("ONE task folder hosts
several phases … a driver's earlier `orchestrator complete done` sits in the
stream while a brand-new run is live") and then guards it with `state["running"]`
only. Two holes remain:

1. `journal_quiescent()` returns **True when `plans` is empty** ("a journal that
   produced no run at all is quiescent"). A watcher started *before* the driver's
   first spawn — the exact order documented in `CLAUDE.md` and
   `m-orchestrator/SKILL.md:78-79` (start server + watcher, then run the driver)
   — has `plans == {}`, so it exits `EXIT_QUIET_SECS` (default 120 s) after
   startup and the whole run goes unmonitored.
2. `state["run_complete"]` is correctly reset on a re-spawn (`:891-896` emits
   `complete running`), but the events-file fallback still sees the old
   `complete done` line, so once *any* complete event exists the flag is
   effectively latched True for the life of the stream. A premature settle
   (QUIET_SECS=60) followed by 60 s more journal quiet with no agent in flight
   now **terminates** the watcher where the old code self-healed on the next
   spawn.

Reproduced against a folder shaped exactly like the live one
(`.claude/local-orchestrators/touch-mongo-live/events.jsonl` line 297 is
`orchestrator complete done "research done: 5 perspectives …"` from the research
phase, and this implement-plan pass appends to that same file):

```
state/events.jsonl = the single stale `orchestrator complete done` line
wf/journal.jsonl   = empty (driver has not spawned yet)
ORCH_EXIT_QUIET_SECS=3 python3 decision_watcher.py
→ "decision watcher online (tailing workflow journal)"
→ "watcher exiting: run complete, journal quiet 3s+"   (rc=0)
```

The suite's own arm asserts this as *desired*: `test_watcher.py:490-499` seeds a
stale complete event over an **empty journal** and asserts the watcher exits. So
the semantics were chosen, not slipped — they are wrong. R-40 exists to make "is
the loop still running?" answerable from process state; as delivered, process
state lies in the opposite direction, and the amended GD-1 commit gate built on
top of it will clear while a run is live.

**Fix (all four parts):**
* Scan for the **last** `stage=="complete"` event in file order and treat only
  `done|failed` as terminal — a later `complete running` (the reopen event this
  module already emits) or any later `plan` event means not terminal. Restrict to
  the reserved `plan == "orchestrator"` id (`monitoring.md`'s reserved ids).
* Require the terminal event to belong to *this* watcher session: either
  `state["run_complete"]` was set by this process, or the complete line was
  appended after the watcher's first consumed journal byte (record the events
  file's size/offset at startup and only look past it).
* Do not treat "this journal produced no run at all" as quiescent-and-exitable;
  an empty `plans` set means *unknown*, which GD-10 says is never a verdict.
* Add an arm that fails today: stale `complete done` + a journal that grows a
  `started` entry after the exit window ⇒ the watcher must still be alive and
  must emit the spawn event.

---

## MAJORS

### M1 — `runStatus` executes `statusCmd` through `bash -c`, so agent-authored strings become shell code
`.claude/skills/implement-plan/templates/implement.workflow.js:51-57` and
`.claude/skills/execute-research/templates/research.workflow.js:38-44`.

```js
cp.execFileSync('bash', ['-c', statusCmd(plan, stage, state, msg)], { stdio: 'ignore' })
```

`statusCmd` builds one shell string with `"${plan}"` / `"${msg}"` interpolated
inside double quotes. Until this diff `statusCmd` was only ever embedded in
prompt *text*; `runStatus` is the first place it is executed. The arguments are
**divider-agent output**: `runStatus(sp.id, 'plan', 'done', …)` at `:275/:277`,
and `partition not isolated: ${f} has two owners` at `:337` interpolates an
agent-supplied file path. R-14 (validate divider/perspective ids) is explicitly
out of scope for this sub-plan, so nothing sanitises them. Demonstrated:

```
sp.id = 'sp-a" ; echo PWNED-$(id -u) > /tmp/claude-1000/pwned ; echo "'
→ /tmp/claude-1000/pwned contains PWNED-1000
```

Arbitrary command execution in the driver process from a subagent's structured
output (and, more mundanely, any `"` in a sub-plan id or file path silently
mangles or drops the event).

**Fix:** no shell — pass argv and the env directly:
```js
cp.execFileSync('bash', [S, plan, stage, state, msg],
  { env: { ...process.env, ORCH_STATE_DIR: TASK }, stdio: 'ignore' })
```
`statusCmd` stays as-is for prompt text. Add a `test_shell.py` guard that no
template passes `statusCmd(...)` to a `-c` invocation.

### M2 — the R-58/R-09 template guard has a catch-all disjunct and cannot fail (confirms gate F2)
`.claude/shared/monitoring/tests/test_shell.py:263-266`.

```python
check(re.search(r"runStatus\(\s*'orchestrator',\s*'complete',\s*'done'", src)
      or re.search(r"runStatus\(\s*'orchestrator',\s*'complete',\s*state", src)
      or "runStatus('orchestrator', 'complete'" in src,
      f"{name}: emits orchestrator complete on the success path")
```

The third disjunct is satisfied by the literal prefix both templates contain, so
the state argument is never actually checked — which is exactly why B1 shipped
green. R-58 names this static guard as its own acceptance criterion; a guard that
cannot distinguish `done` from `state` from `'failed'` does not discharge it.

**Fix:** drop the catch-all and assert the *contract*, per path: the success path
call carries the run-complete state, the throw/reject paths carry `'failed'`, and
no template contains a `closeRun` whose orchestrator state is a literal
(`re.search(r"runStatus\('orchestrator',\s*'complete',\s*'done'", src)` must be
**absent** once `closeRun(state, …)` is the shape). Also assert both templates'
`closeRun` take the same arity so the two never drift again.

### M3 — the R-58 "anti-tautology control" is a hardcoded constant (confirms gate F3)
`.claude/shared/monitoring/tests/test_watcher.py:373-378`.

```python
old_rule = "done" if {}.get("research") else "failed"
check(f"R-58: {task} — the retired rule would have said failed",
      old_rule == "failed" and len(entries) > 1)
```

`{}.get("research")` is `None` unconditionally; the control proves nothing about
the retired rule, the fixture, or the fix. It is the one assertion whose job is
to show the R-58 replay is not vacuous.

The claim it *should* be making is true — I verified it out-of-tree by replaying
`wf_930e210a-6da/journal.jsonl` twice, once with the retired ungated ternary and
once with `close_state_for`:

```
OLD: [('research','running',…), ('research','failed','loop exited -> synthesis'),
      ('synthesis','running',…), ('synthesis','failed','settle')]   outcome=failed
NEW: [('research','running',…), ('synthesis','running',…),
      ('research','done','settle'), ('synthesis','done','settle')]  outcome=done
```

**Fix:** parameterise `replay_journal(path, strategy=…, rule=…)` with the old
predicate (`"done" if decisive.get(p) else "failed"`, heuristic ungated) as the
control, then assert on the same entries: old ⇒ a `research`/`synthesis` `failed`
badge and `outcome == "failed"`; new ⇒ zero failed badges and `outcome ==
"done"`. That is a control that would fail if the fix regressed.

### M4 — the R-40 driver epilogue is inert, and one of its two targets is a shared daemon
`.claude/skills/execute-research/templates/research.workflow.js:73-86`,
`.claude/skills/implement-plan/templates/implement.workflow.js:88-104`.

```js
for (const pidFile of ['watcher.pid', 'monitor.pid']) { … process.kill(pid, 'SIGTERM') … }
```

* Nothing in the repository ever writes those files:
  `grep -rn "watcher\.pid|monitor\.pid" .claude/` returns **only these two
  templates**. `m-orchestrator/SKILL.md:78-79` starts both daemons with a bare
  `&` and records no pid. So `fs.existsSync(p)` is always false and R-40's
  "driver epilogue stops daemons on `orchestrator complete`" half — the half that
  exists to fix CONVO-14's three orphaned watchers — does nothing. The guard at
  `test_shell.py:270-271` (`".pid" in src`) certifies the dead code as present.
* `monitor.pid` is the **wrong target by design**: `monitoring.md` and
  `m-orchestrator/SKILL.md:88` both state one `monitor_server.py` serves *all*
  tasks. A per-task epilogue SIGTERMing it takes the dashboard down for every
  other live task — the same wrong-target class the comment right above it warns
  about for name-matched kills.

**Fix:** remove `monitor.pid` from the loop (the shared server outlives any one
run; if it must be stopped, gate on "no other task folder has a live watcher").
For the watcher, either make the pid recording real and in-scope (have the driver
snippet the templates document write `$!` to `$TASK/watcher.pid`, which is what
sp-01's `*.pid` gitignore entry anticipates) or drop the pid path entirely, rely
on the (fixed) self-exit, and change the guard to assert whichever mechanism
actually exists instead of the string `.pid`.

---

## MINORS / NITS

### m1 (minor) — `terminal_complete_seen()` re-reads the entire event stream every ~1 s
`decision_watcher.py:585-596`, called at `:1103`. Python evaluates the argument,
so the full-file scan runs on **every poll tick** whenever
`journal_quiescent(state)` is true and `state["run_complete"]` is unset (the
"idle but not terminal" case — e.g. the `stay alive` arm). `events.jsonl` grows
without bound (236 KB for touch-aggregator today), so this is an O(stream) read
per second inside the 1 s liveness loop. **Fix:** cache by `(st_mtime_ns,
st_size)` exactly like `monitor_server._STATUS_CACHE`, or track the last
complete-stage event incrementally; and short-circuit before the call
(`if journal_quiescent(state) and quiet_enough and terminal_complete_seen(...)`).

### m2 (minor) — R-08's "two interleaved parallel sub-plans produce no spurious badge flap" arm is missing
The normative item lists three test arms; the replay covers the research-shaped
run and the finalgate text, but nothing exercises two plans interleaving
spawns/results with `strategy` unset/parallel. That is the exact configuration
the retired heuristic corrupted. **Fix:** add a replay with `sp-a`/`sp-b`
interleaved `started`/`result` entries asserting zero `plan failed` badges and no
`running→done→running` flap.

### m3 (minor) — settle-pass comment now contradicts the code it documents
`decision_watcher.py:1072-1076` still says "green ONLY on a positive decisive
result, else failed" immediately above the `close_state_for(...)` call that
deliberately closes verdict-less plans **done**. A future reader will "restore"
the old rule. **Fix:** reword to GD-10's predicate.

### m4 (minor) — the finalgate *fixer* still gets the generic impl decision text
`decision_watcher.py:641-652`: the new branch is `plan == FINALGATE_PLAN and
"passed" in r`, so `plan=finalgate stage=implement role=impl`
(`implement.workflow.js:396`) falls through to the impl branch and emits
`… -> spawn test`, naming a stage that never runs — the same "phantom next stage"
that R-08 fixed for the gate half. **Fix:** extend the finalgate branch to
`role == "impl"` with `-> re-gate <attempt+1>/<cap>`.

### m5 (minor) — `marker_misplaced` false-positives on any prose mention of `[touch]`
`decision_watcher.py:509-510`: `elif "[touch]" in text` searches the **whole**
prompt, so a prompt that merely quotes the token (a findings file pasted into a
critique prompt, a skill name discussion) flags the node `marker-misplaced`.
GD-9's rule is about a real marker *line* outside the window. **Fix:** apply
`MARKER_LINE` to `text` minus the window and require a `key=value` payload.

### m6 (nit) — same-line `[touch]`/`[monitor]` markers lose the monitor fields
`MARKER_LINE = \[(monitor|touch)\]\s+([^\n]*)` is greedy to end-of-line, so
`[touch] name=a [monitor] plan=…` yields only the touch record and the agent
falls back to `ROLE_PATTERNS`. Adjacent-lines (the tested shape) is fine; a
`re.split` on `\[(monitor|touch)\]` would make it shape-independent.

### m7 (nit) — truncation detection is size-only; a rotated (replaced, larger) journal is missed
`decision_watcher.py:857-877` implements R-07's `size < offset` literally, but
SD-10 pins the checkpoint identity as `(st_dev, st_ino, size, offset)`. A journal
replaced in place by a larger file leaves the stale offset pointing into
unrelated bytes. Cheap to add now (`st_ino`/`st_dev` alongside the existing
`journal` key) rather than diverging from the tailer sp-04 will build.

### m8 (nit, not this sub-plan's file) — mixed id widths inside one stream
Emitting the full 17-hex `agent.id` is correct per R-13/GD-7, but
`monitor.html:273` keys rows on `a.id`, so a stream containing both pre-change
(8-char) and post-change (17-hex) blocks — e.g. `touch-mongo-live/events.jsonl`,
written by both watcher generations this session — renders one agent as two rows.
Flag for the legacy arm (sp-09) / `shortId` join, not for a fix here.

---

## What is right (so the next attempt does not undo it)

* `close_state_for` / `close_detail` are GD-10 verbatim and are used at **every**
  close site (sequenced, settle, `run_outcome`); the sequenced heuristic is gated
  on `STRATEGY == "serial"`; `last_result_ok` is recorded per result; reopen now
  fires from `failed` too. I independently confirmed the behavioural change on
  the real `wf_930e210a` journal (failed→done, run failed→done).
* R-07: `makedirs` at resolution, `emit` swallowing `OSError` with a stderr
  warning, `_int_cfg`/`_int_env` with deferred warnings flushed after the
  heartbeat, truncation rebuild that keeps `tok_emitted` (correctly reasoned).
* R-10/R-39: both writers flock the *same* file descriptor and cap `detail` at
  1 KB identically; `w` is genuinely additive and `monitoring.md`'s schema note
  says "unknown writer, never a default" — the 24-writer × 9 KB concurrency test
  is a real test.
* R-13: full agentId as identity with display-only `shortId`, stage-qualified
  labels (six researchers, six labels), GD-9 window/order-independent parsing
  with a real out-of-window negative, quoting-tolerant `STAGE_HINT`.
* `test_server.py`'s R-58 arms over the frozen real streams are honest tests,
  including the two controls I would have asked for (uncorrected failures all
  match the `loop exited ->` re-label predicate; the user-killed
  `touch-repo-recon` run's genuine failures do **not**).

## Re-review checklist for attempt 2

1. B1 fixed and a guard that would have caught it (M2).
2. B2: last-complete-wins + orchestrator-scoped + session-scoped + empty-`plans`
   is not exitable, with the stale-complete-then-spawn arm added.
3. M1: `bash -c` gone from both templates, argv+env instead, guard added.
4. M3: the control replays the old predicate on the real journal.
5. M4: `monitor.pid` no longer SIGTERMed per task; the watcher pid path is either
   real or removed along with its guard.
6. m1–m4 addressed; m5–m8 at the implementer's discretion with a one-line reason.
7. All five suites green, no skips, and `git status` still shows only the nine
   owned files.
