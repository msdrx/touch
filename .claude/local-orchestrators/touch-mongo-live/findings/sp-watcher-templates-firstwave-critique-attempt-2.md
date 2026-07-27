# sp-watcher-templates-firstwave — adversarial critique, attempt 2

**Verdict: REJECTED** — 2 blocker/major findings (0 blocker, 2 major), 5 minor, 4 nits.

Reviewed diff: `decision_watcher.py`, `tests/test_watcher.py`, `tests/test_shell.py`,
`execute-research/templates/research.workflow.js`,
`implement-plan/templates/implement.workflow.js`
(the other four owned files — `status.sh`, `monitor_server.py`, `monitoring.md`,
`tests/test_server.py` — were read for context only, as instructed).

## What I verified as genuinely correct (no credit-free rejection here)

These are not assumptions; I ran them.

1. **R-58 is really fixed at the `main()` level, not just in the predicates.** I ran the
   *real* watcher over the *real* research fixture
   (`tests/fixtures/run-wf_829e6f58/dd469822…/subagents/workflows/wf_829e6f58-b2f/journal.jsonl`
   with `ORCH_WF_GLOB_ROOT=tests/fixtures`, 6 researchers + synthesizer, real transcripts):

   * new code: `research plan running` → six distinct stage chips → `research plan done
     "run done: settling open plan (closed, no verdict)"`, `synthesis plan done`,
     `orchestrator complete done`, then a clean self-exit (rc 0).
   * control, `git show HEAD:.claude/shared/monitoring/decision_watcher.py` over the same
     fixture: `research plan failed | loop exited -> synthesis` — the exact fabricated
     badge — and rc 124 (never exits).

   That is the falsifiable proof the sub-plan's headline item works end to end.
2. **Attempt-1's three failures are fixed and the guards are mutation-resistant.** I
   mutated the template text in memory and re-ran the new `test_shell.py` regexes:
   reverting `closeRun(state, summary)` to attempt-1's `closeRun(summary)` /
   hardcoded `'done'` flips `state-var`, `no-literal` and `closeRun-arity` to False.
3. **GD-9's window rule survives the real corpus.** I parsed all 18 frozen agent
   transcripts through `parse_markers`: 16/16 real spawn prompts classify correctly
   (leading `\n` + marker on line 1); the two that do not are the foreign negative
   fixture and the 2-line cross-session continuation (list-shaped content, no prompt) —
   both correct outcomes.
4. Suites green as claimed: `test_watcher.py` 176 ok / rc 0, `test_shell.py` rc 0,
   `test_server.py` 21 ok, `test_frontend.py` ok. `flock`, the 1 KB cap, the 24-writer
   concurrency arm and the node argv-injection arm are real behavioural tests, not
   tautologies.
5. Ownership is clean: exactly the nine owned files are modified; the `.gitignore` hunk is
   sp-01/sp-02 residue (a `*.bson` comment), untouched here; no commits added
   (`git log` still ends at `579446e orchestration history`).

---

## MAJOR

### M1 — The self-exit fires on the watcher's OWN inference and silently kills monitoring of a live run
`decision_watcher.py:1239-1246` (exit site), `:429-442` (`terminal_complete_seen`),
`:445-458` (`journal_quiescent`)

`terminal_complete_seen()` accepts **two** sources as "this run ended":
`state["run_complete"]` (the watcher's own debounced settle pass) or an externally
appended `orchestrator complete`. The settle pass is an *inference* whose false-positive
mode is documented three lines above it (`:1194` "a premature close (pause between loops)
is reopened by the next spawn"). Reopening self-heals a badge. **Exiting does not
self-heal anything** — nothing restarts the watcher.

Reproduced (this is not theoretical):

```
journal: started a1 [monitor] plan=sp-a stage=test role=test attempt=1
         result  a1 {"passed": true, ...}
run with ORCH_QUIET_SECS=2 ORCH_EXIT_QUIET_SECS=4, then append `started b1` 12 s later
```
emitted stream:
```
orchestrator sp-a done   | sp-a test #1 PASS -> spawn critique      <-- it KNOWS a critique is next
orchestrator complete done| run done: 1 plan(s) all green; loops idle 2s+
orchestrator watcher info | watcher exiting: run complete, journal quiet 4s+
                          <-- sp-b spawn arrives: NOT MONITORED, watcher gone
```
The watcher wrote "PASS -> spawn critique" and then declared the run complete and exited.
With production defaults this needs ~120 s in which nothing is in flight and the journal
does not grow — a harness queue/rate-limit delay before the next agent's first journal
entry, a driver pause, a user-approval stall. The result is an irreversible, silent loss
of exactly the live visibility this repo exists to provide, and it is a **new** failure
mode (the old watcher never exited).

`journal_quiescent()` does not protect against this: it deliberately returns True for an
open plan with nothing running (`test_watcher.py:374-376` asserts that), and after the
settle pass every plan is terminal anyway.

Fix (small, and more faithful to R-40 now that the templates emit the run-close
themselves): make the **exit** require an externally-written terminal complete — a line
appended after `events_baseline` with `w == "agent"` — and never the watcher's own
`run_complete`. Keep `run_complete` for the badge. Concretely, split the predicate:

```python
def terminal_complete_seen(state, events_path=None, since_offset=0):   # badge/back-compat
    ...
def exit_authorized(events_path=None, since_offset=0):                 # NEW: exit only
    # only a script/driver-written `orchestrator complete done|failed` after the baseline
```
and call `exit_authorized(...)` at `:1242`. If you want to keep the inference path at all,
gate it behind a much larger window plus an opt-out (`ORCH_NO_SELF_EXIT`) and require that
the last decision line did not promise a next stage. Note `test_watcher.py:301-302`
("the watcher's own run_complete counts as terminal") currently locks the hazard in — that
assertion must move to the badge-level predicate.

### M2 — `publishConfig()` can never reach the running watcher, and both templates' comments claim the opposite (including crediting it with the R-58 fix)
`research.workflow.js:51-70, 208`, `implement.workflow.js:66-88, 343`,
`decision_watcher.py:153-164`

The watcher resolves caps and strategy **at import**:
`_CAPS_CFG = read_config()` / `MAX_*` / `STRATEGY` are module-level (`:153-164`), read
once, never re-read (`read_config()` has no other call site in the loop). The documented
launch order (`m-orchestrator/SKILL.md` step 1 = create folder + optional config, step 4 =
start daemons, then run the workflow) puts the daemons **before** the workflow script, and
`publishConfig()` runs inside the script at `phase('Divide')` / `phase('Research')`.
Therefore every value it writes — `max_plan_attempts`, `max_finalgate_attempts`,
`strategy` — lands after the watcher has already frozen its own. Evidence: the live
`touch-mongo-live/orch-config.json` carries the caps the *driver* seeded at step 1 and no
`strategy`/`max_finalgate_attempts` at all.

The comments assert the opposite, in a file the skills call the normative protocol and
every task copies:

* `research.workflow.js:51-55` — "so the watcher reports the SAME numbers the loops
  actually enforce instead of its built-in defaults" (it does not), and, worse,
  "`strategy` … declaring so **is what stops** a fabricated `plan failed` when synthesis
  spawns (R-58)". It is not: R-58 is stopped by `close_state_for()` plus `STRATEGY ==
  "serial"` defaulting to off. A maintainer who believes this comment could "simplify"
  the GD-10 predicate and silently resurrect the defect.
* `implement.workflow.js:66-71` — same claim about the caps.

Also inert as a consequence: `MAX_FINALGATE_ATTEMPTS` in the new decision text
(`:730-744`) always reads the built-in `2`, so a task that adapts `FINALGATE_ATTEMPTS`
gets wrong "re-gate N/M" text — which is precisely D4's "caps are not baked into the
shared watcher".

Fix: make the watcher pick config up while running — cheapest form, in the poll loop next
to the existing `os.stat` work:

```python
_CFG_MTIME = None
def refresh_caps() -> None:      # re-read only when orch-config.json's mtime moves
    global MAX_PLAN_ATTEMPTS, MAX_FINALGATE_ATTEMPTS, STRATEGY, _CFG_MTIME
    ...
```
and correct both comment blocks so they state what actually gates R-58
(`close_state_for()`), not the config write.

---

## MINOR

### m1 — The implement template stamps every NEW run `strategy:"serial"`, re-enabling the heuristic GD-10 retired — and reuses the exact `loop exited ->` signature SD-4's re-labeler keys on
`implement.workflow.js:84`, `decision_watcher.py:1071-1085`

GD-10: "the serial-only `last_plan` heuristic is **retired for new runs** (kept behind
`strategy:"serial"` in `orch-config.json` for legacy)". `strategy: PARALLEL_MODE ?
'parallel' : 'serial'` declares every default implement-plan run — all new runs — as
exactly the config value that re-enables it. It is inert today only because of M2; the
moment M2 is fixed (or a watcher is restarted mid-run against the published config, an
explicitly supported operation) new runs get the retired path back.

I checked whether it can fabricate a wrong *state* now, and it cannot (a green plan is
already `done` so the branch is skipped; a verdict-less plan folds through
`close_state_for`) — that is why this is minor, not major. What it does produce is a
**new** run's legitimate `plan failed` carrying `detail = "loop exited -> sp-b"`, which is
byte-for-byte the signature SD-4's read-time rule matches ("`plan failed` + detail
`loop exited ->` + all stage agents resulted ⇒ *closed — no verdict*"). sp-09 would then
re-label a real failure as "no verdict".

Fix: either stop publishing `serial` from the template (publish `sequential`, or omit the
key and let a legacy config opt in — the whole point of "legacy only"), or change the
retained heuristic's detail text so it cannot collide, e.g.
`f"serial advance -> {info['plan']}"`, and restate that in SD-4's sp-09 half.

### m2 — A killed run still orphans its watcher — the exact case CONVO-14 cites
`decision_watcher.py:445-458`, `:807-826`

`journal_quiescent()` requires `run_outcome()`, which requires `not state["running"]`, and
an agent is removed from `running` only by a journal `result`. If the session dies
mid-agent (context cleared / session killed — the scenario the comment at `:1189-1191`
names), the agents die with no result, `running` never empties, the settle pass never
fires, and the self-exit never fires. R-40's goal ("is this loop still running is
answerable from process state") is unmet in the abnormal-termination case, which is how
orphans arise in the first place.

Fix: bound it with the liveness rule the plan already sanctions (GD-10 as amended: idle
> 180 s ⇒ `unknown`, never running): if the journal has not grown for, say,
`10 * EXIT_QUIET_SECS` **and** every id in `running` has a transcript whose mtime is older
than that window, emit `agent … stale` for them, drop them from `running`, and let the
normal settle/exit path proceed.

### m3 — `watcher.pid` is a convention invented here that nothing produces or documents, and a stale pid file makes the SIGTERM a wrong-target kill
`research.workflow.js:85-100`, `implement.workflow.js:105-120`,
`tests/test_shell.py` (`"watcher.pid" in src` guard)

`grep -rn "watcher.pid"` matches only these two templates. No owned file writes it
(`status.sh`, `decision_watcher.py`, `monitoring.md`), and the launch snippets in
`m-orchestrator/SKILL.md:76-80` and `CLAUDE.md` do not either. So R-40's "driver epilogue
stops daemons" half is dead code in every real run, and the new static guard asserts a
mechanism that never executes. Worse, the comment claims "kill by RECORDED PID only,
never by matching a process NAME … a name-matched kill is a wrong-target kill" while a
*stale* pid file is the same hazard by another route — nothing ever unlinks
`watcher.pid`, and the watcher now self-exits at run close, so the file goes stale
by design.

Fix: verify the target before signalling —
`fs.readFileSync('/proc/'+pid+'/cmdline')` must contain `decision_watcher` (guard the
read, non-Linux ⇒ skip) — and either land the pid-recording line in a file this sub-plan
owns (`monitoring.md`'s run block) or say plainly in the comment that the launch-side half
is deferred to sp-15's doc pass, so the guard is not read as evidence the path works.

### m4 — `agent.id` widened to 17-hex while `monitor.html` keys rows on `a.id` and is out of scope ⇒ duplicate agent rows on every in-flight stream
`decision_watcher.py:330-350` (`agent_block`), consumer at `monitor.html:273,281,350`

R-13 mandates the full id, so the change is right. But `monitor.html` (explicitly
untouched this pass) does `p.agents.get(a.id)` / `p.agents.set(a.id, row)`. Any stream
containing both pre-change 8-hex and post-change 17-hex blocks for the same agent — i.e.
any live task whose watcher is restarted onto the new code, including `touch-mongo-live`
right now — renders **two rows per agent**, and replay-on-connect makes it permanent for
history. Nothing records this consequence. Related: `monitoring.md`'s agent-block schema
(owned by this sub-plan) still says "with the agent's `id`/`label`" and does not document
the four keys now emitted (`shortId`, `identity`, `flags`, `unconventional`), so the
normative spec no longer describes the writer.

Fix: add those keys plus a one-line "id widened to the full 17-hex agentId; 8-hex ids in
pre-existing lines are legacy (GD-14 `legacy:<task>:<id8>`); readers key on `id`" note to
`monitoring.md`'s agent-block list, and record the `monitor.html` re-key as a carried
consequence for the frontend item (sp-13 / the deferred R-11/R-12 pass).

### m5 — R-58's acceptance is asserted against a re-implementation of `main()`, and two of its clauses are not covered at all
`tests/test_watcher.py:443-565` (`replay_journal`), sub-plan text
"replay the three real streams … ; failed-then-done fixture renders `done`"

`replay_journal()` re-implements the spawn/result/close bookkeeping of `main()` in the
test file. It exercises the *predicates* over real journal bytes (good) but a regression
**inside `main()`** — the ungated heuristic returning, the reopen branch narrowing back to
`("done",)` — is caught only by the four `in main_src` string guards, which any
refactor of those lines defeats while staying correct-looking. Also: only 2 of the 3
affected streams are replayed (no `touch-aggregator`, whose fixture carries a
`loop exited ->` failed badge too), and the frozen event streams
(`tests/fixtures/legacy/*-events.jsonl`) — including the failed-then-done correction lines
the clause names — are never read by any owned test.

This one is cheap to close, and the fixture already exists: `tests/fixtures/run-wf_829e6f58`
is a *real* research run **with transcripts**, and I confirmed it reproduces the defect
through `main()` (old watcher ⇒ `research plan failed "loop exited -> synthesis"`, new ⇒
none). Add one subprocess arm next to the existing R-40 ones:

```python
exited, rc, err = run_watcher(state, WF_829, {"ORCH_QUIET_SECS": "3", "ORCH_EXIT_QUIET_SECS": "4",
                                              "ORCH_WF_GLOB_ROOT": FIXTURES}, wait=40)
evs = events_of(state)
check(not [e for e in evs if e["stage"] == "plan" and e["state"] == "failed"], ...)
check(any(e["plan"] == "research" and e["stage"] == "plan" and e["state"] == "done" for e in evs), ...)
```
and, for the failed-then-done clause, fold
`tests/fixtures/legacy/touch-mongo-live-events.jsonl` with SD-4's last-event-wins rule and
assert the `(task, plan, stage='plan')` badge is `done`.

---

## NITS

* **n1** `runStatus` uses `stdio: 'ignore'`, so `status.sh`'s R-10 out-of-enum stderr
  warning is discarded on the script path — the one writer that cannot be told it wrote a
  bad state (`research.workflow.js:41-49`, `implement.workflow.js:55-63`). Capture stderr
  and `log()` it. Also `node:child_process` has no precedent in an adapted script (only
  `node:fs` does, `touch-mongo-live/orch-scripts/implement.workflow.js:118`); if the
  import ever throws in the Workflow runtime, every script-emitted terminal event vanishes
  into the `catch` and only the watcher-side half of R-58 remains. A single probe log at
  first use would make that visible.
* **n2** `publishConfig` calls `fs.mkdirSync(TASK)` *after* `fs.readFileSync(path)`
  (`research.workflow.js:62-63`, `implement.workflow.js:75-76`) — harmless, but the mkdir
  belongs first if the intent is "works when TASK does not exist yet".
* **n3** `test_watcher.py` now takes ~30 s (was well under 1 s) because the four
  subprocess arms use fixed `wait=`/`sleep` windows. Poll for the child's exit (and for
  the expected event line) instead of sleeping the full window; the suite is run on every
  gate in this pass.
* **n4** `stream_terminal_close` treats *any* later `stage == "plan"` line as "the run is
  live again", including the `plan queued` seed lines the m-orchestrator recipe writes at
  step 2. Harmless in the current order, but worth one word in the docstring so a future
  seeding change does not silently disable the exit.

---

## Checklist items with nothing to report

GD-21/-24/-25/-26/-27/-28/-29/-30 and GD-22 are not touched by this sub-plan (no pymongo,
no Mongo I/O, no `_id`s, no deletes, no credentials — I grepped: no `pymongo`, `mongo`,
`27017`, `MONGO_` token anywhere in the five files). No secret, path or credential leaks
into events, prompts or details. GD-15 one-file-one-owner respected. No needless rewrites:
every hunk maps to R-07/R-08/R-09/R-10/R-13/R-39/R-40/R-58 or SD-10. Templates stayed
inside the R-09/R-40 scope — no R-14 id validation, no R-15 attempt bookkeeping, no
R-18/-19/-21 additions.
