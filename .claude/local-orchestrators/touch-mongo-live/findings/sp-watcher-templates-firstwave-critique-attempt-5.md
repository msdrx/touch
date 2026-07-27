# Adversarial critique — sp-watcher-templates-firstwave, attempt 5

**Verdict: REJECTED.** 1 major, 3 minor, 4 nits.
depth: `in-scope` · critical_defect: `false`

Scope reviewed: `git diff` of the nine owned files (`decision_watcher.py`,
`status.sh`, `monitor_server.py`, `monitoring.md`, `tests/test_watcher.py`,
`tests/test_shell.py`, `tests/test_server.py`,
`execute-research/templates/research.workflow.js`,
`implement-plan/templates/implement.workflow.js`) against
`touch-mongo-live-subplans.md §sp-03`, R-07/R-08/R-09/R-10(slice)/R-13/R-01:guard
(base), R-39/R-40/R-58 (amendment), GD-9…GD-15, GD-21…GD-30, SD-3/SD-4/SD-10.

---

## What holds up (re-verified here, not taken on trust)

- **Every attempt-4 defect is genuinely closed, with non-tautological arms.**
  - **M-1** — `stream_terminal_close()` (`decision_watcher.py:851-860`) now
    resets only on *liveness* (`complete running`, or a `plan` event whose state
    is not itself a close). I re-derived the whole shipped ordering against the
    real module (probe: driver `complete done` w=agent → watcher settle
    `plan done` → watcher `complete done`) ⇒ `exit_authorized` stays **True**;
    a watcher-only close never authorizes; a later `plan queued` seed correctly
    cancels. `test_watcher.py:1380-1405` drives the same ordering end-to-end
    with an explicit anti-vacuity assertion that the settle events really landed
    after the driver's close, and asserts the exit came by the *driver* route.
  - **M-2** — `SIGTERM`/`SIGINT` arm a drain (`:307-347`, `:1364-1381`), and the
    arm at `test_watcher.py:1448-1520` has a real control: the same stimulus
    delivered as `SIGKILL` (unhandleable) demonstrably loses the result, the
    decision line and the tokens, so the passing arm is not a lucky poll tick.
  - **M-3** — `test_shell.py:330-373` holds `LOCK_EX` from the test process and
    proves the writer *blocks* and then lands exactly one line after release;
    `:375-390` adds source guards for both append sites plus the m-4
    defensive-import shape. The old 24-writer arm now documents honestly that it
    does **not** prove the lock.
  - **m-1** — one `resolve_config()` returns `(path, values)` from the same file
    (`:60-103`); **m-2** — `PARSE_FAILURES.pop()` on both the stat- and
    OSError-failure paths (`monitor_server.py` `task_status`); **m-3** —
    `stream_plan_closes()` + the adoption at `:1647-1660` (arm at
    `test_watcher.py:1418-1446` asserts *exactly one* close and that it is the
    script's, not a `(closed, no verdict)` duplicate); **m-4** — `status.sh:50-57`
    now mirrors the watcher's `try/except ImportError`.
- **R-58 / GD-10 forward fix.** `close_state_for()` is the predicate verbatim and
  is the only close rule at all three sites. `test_watcher.py` replays the frozen
  real streams and folds the SD-4 last-event-wins rule with a genuinely-failed
  negative control.
- **R-39 / R-13 / R-07.** `w` is additive on both writers, five-key core intact;
  full 17-hex `id` with display-only `shortId`; nested state dir, deferred cap
  warnings and the SD-10 shrink/inode rebuild all exercised as live processes.
- **Mongo invariants.** No `pymongo`/`mongodb://`/`27017`/`MONGO_` token anywhere
  in the nine files (grepped); `decision_watcher` and `monitor_server` import
  clean on bare stdlib (re-checked). No `$`-ops, no `_id`, no delete verbs, no
  TTL, no credentials — GD-21…GD-30 are untouched by this diff.
- **Suites.** `test_watcher.py` (268 ok), `test_server.py` (22/22),
  `test_shell.py`, `test_frontend.py` — all rc=0 here. `test_frontend.py` and
  `monitor.html` carry no mtime from this attempt (2026-07-26 02:5x); the nine
  owned files were all touched 07:11–07:25Z today. No commit (SD-6).

---

## MAJOR

### M-1 — The templates' script-side emitters are inert: R-09's and R-40's template halves ship as dead code, and this diff deletes the one producer that worked

`implement.workflow.js:313-315` vs `:118-150`; `research.workflow.js:86-118`;
`research.workflow.js:242` (deleted prompt line); `test_shell.py:539`;
`monitoring.md` (the new "When the run ends, stop its watcher" block)

The reviewed diff makes **two mutually exclusive normative claims about the same
two helpers**, roughly 180 lines apart in the same file:

```js
// implement.workflow.js:313 (cycle-report block)
// The workflow runtime has NO filesystem or Node API access (import() throws in
// scripts; the try/catch'd runStatus/closeRun helpers above are the documented
// contract but silently no-op at runtime), so the script CANNOT write pages itself
```
```js
// implement.workflow.js:124 / research.workflow.js:95 (closeRun block)
// This event is ALSO what authorizes the watcher to stop: decision_watcher.py
// exits only on a `w:"agent"` `orchestrator complete done|failed` line ...
// So this call is the mechanism; the pid signal below is only a fast path.
```

`.claude/skills/implement-plan/SKILL.md:100-102` repeats the first claim verbatim,
and `monitoring.md` now asserts the second ("This is what makes the templates'
`closeRun` epilogue safe — it signals ~0.2 s after the harness appended the final
agent's `result`"). One of these is false in the **normative protocol file**
(CLAUDE.md: "Both skills' `templates/*.workflow.js` are the normative protocol").
A maintainer adapting the template cannot tell which, and the sub-plan's own
static guards (`test_shell.py:442-533`, ~25 assertions) assert only that the
*text* is present — they cannot distinguish a working emitter from dead code.

**The evidence in this repo says the "silently no-op" claim is the true one:**

| Check | Result |
| --- | --- |
| Who wrote every loop-terminal `plan` event in `touch-mongo-live/events.jsonl`? | `cycle_reporter.py` — `.cycle-reporter-state.json` `emitted` lists all 15 `sp-*` plans |
| The only `plans_total` event | `{"plan":"divide", …,"plans_total":19}` with detail `15 sub-plans; run totals 19 plan cards incl research, synthesis, divide, finalgate` — neither the template's text nor its value (`SUBPLANS.length + 2` = 17). Hand-written by the driver |
| `finalgate` plan events in the whole stream | **0** (the template emits one on both branches) |
| Non-`sp-` plans in the daemon | `cycle_reporter.py:207` — `if slot is None or not plan.startswith("sp-"): continue`; `emit_close` never emits `orchestrator complete` |
| Runtime probe log `status emitter ready (node:child_process)` | never appears in any transcript or log; only as source text |

**Concrete failure scenario (execute-research, run as the template ships):**
1. `runStatus('research','plan','done', …)` and `runStatus('synthesis','plan',
   'done', …)` no-op inside their `catch`.
2. The **agent-side** fallback that used to write the synthesis close was deleted
   by this diff — `research.workflow.js:242` removed
   `Then: ${statusCmd('synthesis','plan','done','plan written')}` from the
   synthesis prompt — and `test_shell.py:539` now **forbids restoring it**
   (`check("statusCmd('synthesis', 'plan', 'done'" not in research)`).
3. `cycle_reporter.py` skips both plans (`sp-` prefix filter).
   ⇒ **nobody** writes `research plan done` / `synthesis plan done`. Both cards
   sit `running` until the watcher's 60 s settle pass closes them
   `(closed, no verdict)` — the "card hangs open because nobody emitted the
   terminal event" class of defect R-09 exists to remove, reintroduced.
4. `closeRun('done', …)` no-ops too ⇒ no `w:"agent" orchestrator complete done`,
   so `exit_authorized()` (the M-1 fix I just verified) is **never satisfied**
   and the watcher can only stop through the 10× ABANDONED window (20 min by
   default) — the exact CONVO-14 orphan symptom R-40 exists to remove, and the
   `run abandoned — no driver close` detail is then honest but useless.
5. `publishConfig()` no-ops ⇒ `orch-config.json` gets no
   `max_plan_attempts`/`max_finalgate_attempts`/`strategy` from the script, so
   the watcher narrates its built-in defaults — defect D4, which R-09's
   config-publishing half exists to close. (In *this* task the file is
   driver-written, which is why nobody noticed.)

Note the finding is robust to which claim is true: if `import()` *does* work,
then `implement.workflow.js:313` and `SKILL.md:100-102` are a false statement
that tells every future maintainer the entire R-09/R-40 template mechanism is
decorative — and the reference daemon (`cycle_reporter.py`) was built on that
false premise. Either way the normative template is wrong about itself.

**Fix (all inside this sub-plan's owned files):**
1. Settle the fact and say it **once**. Keep the probe log that already exists
   (`status emitter ready (node:child_process)`); if it never fires, state at the
   top of both templates that `runStatus`/`closeRun`/`publishConfig` are the
   *contract* and name the component that actually fulfils each event, and delete
   the contradicting "this call is the mechanism" sentences from
   `closeRun`'s comment and from `monitoring.md`'s new lifecycle block.
2. Restore a **working producer for the non-`sp-` plan cards**: put the
   `statusCmd(plan,'plan','done', …)` line back in the synthesis prompt (and the
   equivalent for `research`/`divide`/`finalgate`) as the belt-and-braces path,
   and drop the `test_shell.py:539` assertion that forbids it. R-09's "not
   agent-emitted" preference is only worth honouring while a script *can* emit.
3. Retarget the R-09/R-40 guards at whatever really writes the events, so a
   future regression is caught by behavior rather than by string presence.
4. Record the runtime limitation as a deviation note next to this findings file —
   R-09 as written ("script-emitted … adapted scripts write/merge
   `orch-config.json`") is not implementable in this runtime and the plan should
   say so.

---

## MINOR

### m-1 — `monitor_server.py` and `monitoring.md` both exceed their explicitly recorded scope carve-outs
`monitor_server.py` (`replay_plan_states` continuation-reopen + the new
`elif "running" in plans` fold); `monitoring.md` (~140 changed lines)

`touch-mongo-live-subplans.md §Scope exclusions 1` is unambiguous:
"`monitor_server.py` receives ONLY R-10's slice (sp-03)", R-11/R-12 OUT; and
§sp-03 fences `monitoring.md` to the "R-39 schema note only; R-17's full refresh
is out of scope". This diff adds to `monitor_server.py` a FRONTEND-6 continuation
reopen and a **behavioral change to every task's home-grid verdict** (a running
plan now beats a failed one), and to `monitoring.md` a large normative refresh
including `monitor.html` prose (header zoom, session timeplan, stats page,
artifacts popup) that describes another sub-plan's deliverables. The changes look
correct and are tested — the objection is scope, and it matters because sp-12
(`server.py`) and sp-13 (frontend) reason about these semantics from their own
sections. Some of the `monitoring.md` prose may predate this attempt (another
sub-plan writing into a file it does not own, a GD-15 breach of its own); either
way this sub-plan is the file's owner and should either drop the out-of-scope
hunks or record them as an accepted deviation.

### m-2 — `/health`'s R-10 counter is only as fresh as the last `/tasks` poll, which is not what R-10's acceptance test says
`monitor_server.py` `health_payload()` / `PARSE_FAILURES`

`PARSE_FAILURES` is populated exclusively as a by-product of `task_status()`,
i.e. of a `/tasks` request. R-10's stated test is "`/health` counter increments
on a poisoned stream" — as shipped, a probe that polls only `/health` reports
`0` forever no matter how poisoned the stream is. The docstring is admirably
honest about this ("a probe taken before the first scan honestly reports zero"),
so this is a spec/behavior mismatch rather than a lie. **Fix:** either scan on
demand in `health_payload()` (cheap: the per-file scan is already cached by
`(mtime_ns, size)`) or amend the R-10 wording where it is restated so no future
reader treats `/health` as self-sufficient.

### m-3 — the stop handlers are installed after a potentially long startup backfill
`decision_watcher.py:1308-1340`

The one-time token backfill runs one `agent_paths()` glob plus a full transcript
read per already-tracked agent (dozens of agents, multi-MB transcripts) *before*
`install_stop_handlers()`. A `closeRun`/operator SIGTERM that lands in that window
still kills the process where it stands — precisely M-2's loss scenario, for the
case where a watcher is restarted just as its run is being closed. The comment
justifies the order by checkpoint safety, but `save_state()` is already atomic
(`tmp` + `os.replace`), and the risk of re-emitting a delta is bounded by the
monotonic clamp. **Fix:** install the handlers first and check `stop_requested()`
between agents in the backfill loop, saving before the early return.

---

## NITS

- **n-1** `test_shell.py:330-373` infers "the writer blocked" from a fixed
  `proc.wait(timeout=1.5)`. I measured an uncontended `status.sh` at ~0.06 s, so
  the 25× margin is fine today — but `test_watcher.py` already has the better
  idiom (`AUTH_EXIT_LATENCY` / `negative_window()`), and using it here would make
  the arm robust on a loaded gate machine.
- **n-2** `decision_watcher.py:1353-1356` — during the drain `tick_sleep()` polls
  every 0.1 s and each pass re-globs transcripts for every running agent
  (`agent_tokens`). Harmless at `DRAIN_SECS=3`, wasteful if someone raises it;
  a cheap guard would be to skip the live-token block while `drain_until` is set.
- **n-3** `research.workflow.js` `publishConfig()` writes `strategy: 'parallel'`
  unconditionally (the attempt-2 mkdir-ordering nit is fixed). One task folder
  hosts research *then* implement-plan, so this clobbers a `strategy` the driver
  or the later phase set. `strategy: cfg.strategy ?? 'parallel'` would make the
  merge as conservative as the helper's own comment claims ("Merge, never
  overwrite"). Same shape in `implement.workflow.js`.
- **n-4** `test_shell.py:442-533` is now ~25 string assertions over two template
  files. Given M-1, consider labelling that block for what it is (a *contract*
  guard, not a behavior guard) so a reader does not mistake its greenness for
  evidence that the events are emitted.

---

## Checklist disposition

| Item | Verdict |
| --- | --- |
| GD-21 / GD-22 / GD-24…GD-30 | N/A here and not violated — no pymongo, no Mongo I/O, no `_id`s, no `$`-ops, no deletes/TTL, no credentials |
| GD-15 one file one owner | Held for the nine files this attempt touched; see m-1 for the item-scope breaches inside two of them |
| GD-9 / GD-10 / GD-11 | Held — marker grammar, close predicate verbatim at all three sites, 1 KB cap at both writers |
| R-07 / R-13 / R-39 | Met, with real behavioral coverage |
| R-08 / R-58 | Met and proven against the frozen real streams; SD-4 read side held |
| R-10 | Implementation + a real lock-contention guard (M-3 closed); `/health` freshness caveat → m-2 |
| R-09 | **Not met at runtime** → M-1 (and this diff removed the agent-side fallback) |
| R-40 | Watcher side met and now genuinely reachable (M-1/attempt-4 closed); **template/driver-epilogue side inert** → M-1 |
| Tests assert real behavior | Yes for the watcher/server/lock arms (good controls); the template guards are string-presence only → M-1, n-4 |
| Docs match implemented behavior | `monitoring.md`'s new lifecycle block describes the `closeRun` epilogue as live → M-1 |
