# Adversarial critique — sp-watcher-templates-firstwave, attempt 4

**Verdict: REJECTED.** 3 major, 4 minor, 4 nits.

Scope reviewed: `git diff` of the nine owned files
(`decision_watcher.py`, `status.sh`, `monitor_server.py`, `monitoring.md`,
`tests/test_watcher.py`, `tests/test_shell.py`, `tests/test_server.py`,
`execute-research/templates/research.workflow.js`,
`implement-plan/templates/implement.workflow.js`) against
`touch-mongo-live-subplans.md §sp-03`, R-07/R-08/R-09/R-10(slice)/R-13/R-01:guard
(base) and R-39/R-40/R-58 (amendment), GD-10/GD-11/GD-15/GD-21…GD-30, SD-3/SD-4.

## What holds up (verified, not taken on trust)

- **R-58 forward fix is real.** `close_state_for()` is GD-10's predicate verbatim
  (`decisive.get(p) if p in decisive else last_result_ok.get(p, False)`) and is
  the *only* close rule at all three sites (sequenced/legacy, settle,
  `run_outcome`). The e2e arm drives the real `main()` over the real frozen run
  (`tests/fixtures/run-wf_829e6f58`) and the retired-rule control genuinely
  fabricates the badges the assertions deny, so those checks are not vacuous.
  I re-ran the real watcher over a research-shaped journal myself: zero `failed`
  plan badges, no `loop exited ->` detail, `research`/`synthesis` close `done`.
- **SD-4 read side.** `monitor_server.replay_plan_states()` folds badges
  last-event-wins in *file* order (not ts), with a genuinely-failed negative
  control over `legacy/touch-repo-recon-events.jsonl`. Fixtures are present, so
  those arms are not silently skipping.
- **R-39** `w` is additive on both writers; five-key core preserved; `title`
  coexists. **R-13** full 17-hex `id` + display-only `shortId`, stage-qualified
  labels, GD-9 window/two-markers-on-one-line/misplaced-`[touch]` rules all
  behave as documented. **R-07** nested state dir, deferred cap warnings,
  shrink+inode rebuild all exercised as live processes.
- **Mongo invariants:** nothing here imports pymongo; `decision_watcher` and
  `monitor_server` import clean on bare stdlib (checked). No `$`-ops, no `_id`,
  no delete verbs, no credentials — GD-21…GD-30 are not touched by this diff.
- **Ownership:** only the nine owned files are modified (plus this task's own
  `events.jsonl` and a `.gitignore` comment hunk — see nit n3). `test_frontend.py`
  untouched, no commit made (SD-6).
- `test_shell.py` rc=0 and `test_server.py` rc=0 re-run here.

---

## MAJOR

### M-1 — R-40's authorized self-exit is dead under default windows: the watcher's own settle events invalidate the driver's close
`decision_watcher.py:762-767` (and `:789`, `:1433-1447`)

`stream_terminal_close()` *assigns* `terminal` on every `orchestrator complete`
line — including one written by the watcher itself — and resets it to `False` on
**any** later `stage == "plan"` event regardless of state. The watcher's own
settle pass emits exactly those two things, and always *after* the driver's
close, because `QUIET_SECS` (60 s) < `EXIT_QUIET_SECS` (120 s). So in the normal
flow the sequence in `events.jsonl` is:

```
w=agent    orchestrator complete done   <- closeRun(), authorizes the exit
w=watcher  research     plan done       <- settle pass at +60s  → terminal=False
w=watcher  orchestrator complete done   <- settle pass at +60s  → terminal=False (w mismatch)
```

and at +120 s `exit_authorized()` returns `False`.

**Reproduced** (real watcher, real journal, driver close appended by hand exactly
as `closeRun` writes it, `ORCH_QUIET_SECS=1 ORCH_EXIT_QUIET_SECS=3`):
the watcher was still alive after 20 s, having emitted the settle `plan done` +
its own `complete done` after the agent close. With
`ORCH_ABANDON_QUIET_SECS=6` it eventually exited — via the **wrong route**, with
the detail `watcher exiting: run abandoned — no driver close, journal quiet …`
while the driver's close was sitting in the stream (also a D13 honesty
violation). With shipped defaults that is a 20-minute lag (`ABANDON_QUIET_SECS =
10 × 120`) instead of the specified 2 minutes, and the amended GD-1 commit gate
stays tripped for that whole window — the exact CONVO-14 symptom R-40 exists to
remove.

The test suite cannot see this: the passing arm
(`test_watcher.py:1208-1227`) sets `ORCH_QUIET_SECS=999` and says so in its own
comment ("this arm tests the DRIVER-authorized path only, not the watcher's own
settle pass"). Every unit arm feeds `stream_terminal_close` a stream with a
*single* close; the agent-then-watcher ordering is untested.

**Fix** (keeps all existing arms green, incl. `:593-600` and `:601-607`):
in `stream_terminal_close`, only treat evidence that the run is *live again* as a
reset —

```python
if stage == "complete" and ev.get("plan", "orchestrator") == "orchestrator":
    if ev.get("state") in ("done", "failed"):
        if writer is None or ev.get("w") == writer:
            terminal = True          # matching close; a foreign-writer close is neutral
    else:
        terminal = False             # `complete running` = reopened
elif stage == "plan" and ev.get("state") not in ("done", "failed"):
    terminal = False                 # a plan card MOVING (queued/running) = live again
```

A settle `plan done` is a close, not a sign of life, so it must not reset.
Add the missing arm: driver `complete done` (w=agent) followed by a watcher
`plan done` + watcher `complete done` ⇒ `exit_authorized(...) is True`; and drop
`ORCH_QUIET_SECS=999` from the live arm at `:1211` so the default interaction is
what is actually exercised.

### M-2 — `closeRun`'s SIGTERM races the 1 s poll: the final agent's result, verdict line and token totals are permanently lost from the stream
`research.workflow.js:124`, `implement.workflow.js:145` (with
`decision_watcher.py:1161` — no SIGTERM handler)

`closeRun` emits the terminal event and then `process.kill(pid,'SIGTERM')`
immediately (~0.1-0.3 s after the harness appended the last agent's journal
`result`, i.e. two `spawnSync` calls later). The watcher polls every 1 s and
installs no signal handler, so it dies mid-sleep without draining the journal.

**Reproduced**: `started` consumed, then `result` appended and SIGTERM 200 ms
later ⇒ `events.jsonl` contains the spawn events and **nothing else**:
no `synthesize done`, no `decision: … -> plan complete`, no `tokens` event.
`events.jsonl` is the append-only record that replays on connect (CLAUDE.md's
"completed runs are monitor history"), so this is not a transient UI glitch:

- the last agent's row is stuck `running` forever on replay — the same
  "card hangs open" class of defect this sub-plan exists to kill, reintroduced at
  the other end of the run;
- token deltas are wire-only, so the final agent's *entire* usage never enters
  the totals — and that agent is the synthesizer / final-gate reviewer, usually
  the single largest consumer in the run.

Probability is not marginal: the result line lands at a uniformly random point in
the poll cycle, so ~70-80 % of runs lose it.

**Fix** — any one of:
(a) install a SIGTERM handler in `decision_watcher.main()` that sets a flag, runs
one final tail+emit pass, `save_state()`s and returns (best: also makes the kill
deterministic);
(b) in `closeRun`, wait ≥ 2 poll intervals before signalling
(`await new Promise(r => setTimeout(r, 2500))`);
(c) drop the signal entirely and let the authorized self-exit do it — viable only
once M-1 is fixed.
Add a test: consume a `started`, append the `result`, SIGTERM, assert the
`<stage> done` and `tokens` events are present.

### M-3 — the R-10 write-integrity test cannot fail if the lock is deleted
`tests/test_shell.py:239-273`

`test_status_concurrent_appends_are_atomic` is the only guard on either
`flock` site (`grep -rn flock tests/` returns just this comment; no source-level
assertion exists in `test_shell.py` or `test_watcher.py`). It passes unchanged
with the lock removed: I copied `status.sh`, stripped
`fcntl.flock(..., LOCK_EX)` / `LOCK_UN`, ran the identical 24-writer × 9000-char
scenario ⇒ `lines 24/24, torn 0, distinct 24, trailing newline present`.

The reason is structural: `cap()` truncates to 1 KB **before** the write, so the
"> 8 KiB of detail per writer" the comment at `:249-250` relies on never reaches
`write()` — every line is ~1.1 KB, comfortably inside a single atomic
append. So R-10's stated acceptance test ("concurrent-append test with >8 KiB
details asserting zero lost/torn lines") is unsatisfiable as written once GD-11's
writer-side cap exists, and the test that stands in for it proves nothing about
the mechanism it is named after.

**Fix**: make the guard real *and* correct the misleading comment. Cheapest
honest combination: (a) source assertions that both append sites take
`LOCK_EX` (`"fcntl.flock" in status_sh and "LOCK_EX" in status_sh`, same for
`decision_watcher.emit`), plus (b) a behavioral contention arm — hold `LOCK_EX`
on the events file in the test process, launch one `status.sh`, assert it has not
appended while the lock is held and does append after release. Keep the
24-writer arm as a smoke test but stop claiming it exercises >8 KiB tearing.

---

## MINOR

### m-1 — `config_path()` and `read_config()` can resolve to different files
`decision_watcher.py:59-80`

`config_path()` returns the first **existing** `orch-config.json`;
`read_config()` returns the first **parseable** one. With a corrupt
`STATE_DIR/orch-config.json` and a valid `ROOT/orch-config.json`, `refresh_caps`
watches the mtime of the corrupt file while the values come from ROOT — and
fixing the corrupt file's *content* without changing ROOT's mtime still reloads
the wrong values. **Fix**: one resolver returning `(path, dict)`; have both
callers use it.

### m-2 — a deleted/rotated stream leaves a stale `/health` counter forever
`monitor_server.py:141-155`

`task_status()` returns early on `os.stat` failure (line 141) without
`PARSE_FAILURES.pop(events_path, None)`, so a stream that is removed or rotated
after a poisoned scan keeps contributing to `parse_failures_total` for the life
of the server. **Fix**: pop on the stat-failure path too (and on the `OSError`
path at `:149`).

### m-3 — the settle pass duplicates (and mislabels) closes the templates already wrote
`decision_watcher.py:1433-1440`

The watcher never folds the script-emitted terminal `plan done` into
`state["plans"]`, so after `runStatus('research','plan','done','6/6 researchers
returned')` it emits a second close for the same plan with
`(closed, no verdict)`. Confirmed in my probe run. The badge survives (both say
`done`), but the record carries two contradictory-sounding closes per plan and
the *script-verified* close is the one labelled "no verdict" — and this duplicate
`plan` event is the mechanism behind M-1. **Fix**: before emitting a settle close,
fold the stream's last terminal `plan` event for that plan (the same
last-event-wins fold `monitor_server.replay_plan_states` already implements) and
skip plans already closed there.

### m-4 — `status.sh` imports `fcntl` unconditionally while `emit()` degrades
`status.sh:41` vs `decision_watcher.py:19-22,357-364`

`emit()` explicitly tolerates a missing `fcntl` ("append locking degrades to
unlocked writes without it"); the `status.sh` heredoc hard-imports it, so on a
host without `fcntl` **every** status call would fail and print
"failed to append event", dropping the event — the opposite of the file's own
"best-effort writer must never break an agent" contract. **Fix**: mirror the
watcher's `try/except ImportError` and skip the lock when absent, or state
POSIX-only in the header so the asymmetry is deliberate.

---

## NITS

- **n-1** `monitor_server.py:126-127` — `health_payload()`'s docstring ends in a
  garbled clause: "so in the case the counter exists for they are always
  current". Rewrite.
- **n-2** `implement.workflow.js:82-86` publishes `strategy: 'sequential'` for
  the serial default, so GD-10's `STRATEGY == "serial"` branch
  (`decision_watcher.py:1258`) now has **no** live producer — only a hand-written
  legacy config can reach it. The choice is defensible and documented, but
  `monitoring.md` should say the branch is legacy-config-only so a future reader
  does not "fix" the templates to publish `serial` and resurrect R-58.
- **n-3** `.gitignore` carries an uncommitted comment-only hunk about `*.bson`.
  `.gitignore` is **sp-01's** file (SD-3/GD-15) and sp-01 already committed C1;
  the hunk is not attributable to this diff, but flagging it so the ownership
  ledger stays honest.
- **n-4** `research.workflow.js:250-256`: on `reports.length === 0` the script
  emits `research plan failed` and then still spawns synthesis with zero reports.
  Harmless (synthesis then fails and `closeRun('failed')` runs) but the log reads
  as if the run continued normally; consider closing the run there.

---

## Checklist disposition

| Item | Verdict |
| --- | --- |
| GD-21 / GD-22 / GD-24…GD-30 | N/A here and not violated — no Mongo code, no pymongo import, clean stdlib import verified |
| GD-15 one file one owner | Held for the nine files; see n-3 for the `.gitignore` hunk |
| GD-10 close predicate | Held verbatim at all three sites |
| GD-11 1 KB detail cap | Held at both writers; but see M-3 for the collateral on R-10's test |
| R-07 / R-13 / R-39 | Met, with real behavioral coverage |
| R-08 / R-58 | Forward fix met and proven; SD-4 read side met |
| R-09 | Met (script-emitted terminals, caps published, argv-not-shell) |
| R-10 | Implementation present; acceptance coverage vacuous → **M-3** |
| R-40 | **Not met** in the shipped configuration → **M-1**, and its fast path is lossy → **M-2** |
| Tests assert real behavior | Mostly yes (good anti-tautology controls) — one false guard (**M-3**), one untested ordering (**M-1**), one untested race (**M-2**) |
| Docs match behavior | `monitoring.md`'s exit contract describes M-1's intended, not actual, behavior |
