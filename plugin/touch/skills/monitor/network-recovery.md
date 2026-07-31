# Network failure → manual loop restart strategy

How to detect, ride out, and manually restart orchestrator loops when the
network drops (mobile uplink, proxy flap, API outage). Everything monitored
(daemons, dashboard, `events.jsonl`, the Workflow `journal.jsonl`) is LOCAL
and survives any uplink loss — only in-flight agent API calls are at risk.
Proven in the field on a long implement run in 2026 (two real incidents, zero
lost work) — the procedures below are what that run actually did, not a design
sketch.

## The three failure layers

1. **Transient drop (seconds–minutes)** — the harness retries the agent's API
   call with backoff. No action needed. Signature in `events.jsonl` after the
   fact: repeated `"<x> attempt N spawned"` for the same attempt, a token tick
   restarting from ~zero (`in 25k, all cache_write`), or a tick whose
   cache-read counter is unmoved while cache-write jumps (a dropped stream
   re-sent in full).
2. **Agent death (outage outlasts harness retries)** — `agent()` returns
   `null`. An unguarded loop burns a gated attempt or fabricates a
   "gate died" verdict. Guard every spawn with the `agentR` wrapper (below):
   retry the same work up to 3× on the SAME attempt, then THROW so the run
   stops cleanly instead of grinding through attempts.
   **BOTH shipped templates now carry this wrapper built in at every spawn
   site** — implement since a real fail pass (2026-07-29, a ~2 h outage):
   an adapted script without it burned all four attempts on two loops
   back-to-back (~3 minutes per death, zero substantive verdicts), settled them
   `failed (retryable)`, and the run marched on into its strictly-last endgame,
   whose fresh implementer then absorbed the dead loops' work and committed
   half-reviewed prose; and research since D-11, where its absence let
   a dead researcher leave the board short and the synthesizer plan blind.
   There is nothing to add by hand any more: `orch-scripts/` copies are a
   byte-for-byte `cp` of the payload template and `touch-run verify` refuses a
   launch whose script lost the wrapper.
3. **Driver/session death** — the workflow stops. All completed work persists
   in the journal + the working tree. Restart manually (below).

## Launch-time prophylaxis (`touch-run verify`, at every launch)

- Every `agent()` call site in the workflow script goes through the wrapper.
  Both shipped templates define it; the launch preflight asserts `agentR` is
  present and that exactly two raw `await agent(` calls survive (the wrapper's
  own), and refuses the launch otherwise:

  ```js
  // Verbatim from both templates (they are byte-identical here); `??`, never
  // `||`, so a spec saying net_retries: 0 means zero.
  const NET_RETRIES = ARGS.net_retries ?? 3
  const agentR = async (prompt, opts) => {
    let r = await agent(prompt, opts)
    for (let n = 1; r === null && n <= NET_RETRIES; n++) {
      log(`${opts.label}: agent returned null (infrastructure death) — same-attempt retry ${n}/${NET_RETRIES}`)
      r = await agent(
        prompt + `\n(infrastructure retry ${n}: the previous try of this exact task died without returning — outage, not a task failure. Do the task from scratch.)`,
        { ...opts, label: `${opts.label}~r${n}` })
    }
    if (r === null) {
      throw new Error(`${opts.label}: agent died ${NET_RETRIES + 1}x — infrastructure down; ` +
        `attempts preserved, resume per plan/RESUME.md (network-recovery.md, manual restart)`)
    }
    return r
  }
  ```

  The appended retry tag makes the prompt distinct, so a later
  `resumeFromRunId` re-executes the retried call live instead of replaying a
  cached `null`. The `[monitor]` marker is unchanged: same attempt, extra
  agent row — honest display. Keep the wrapper's own `agent()` calls raw.
- Resume pointers are recorded by `touch-run bind <task>` immediately after the
  launch — not by hand: it writes `wf_dir` + `"resume_from_run_id": "<wf_…>"`
  + `port` into `orch-config.json` and renders the `plan/RESUME.md` runbook
  (health check, the exact resume invocation, close-out command, procedures
  LINKED not restated) so recovery needs zero archaeology. When the run ends,
  the watcher splices the harness's own verbatim `<recovery>` call into that
  file between `<!-- touch:recovery -->` and `<!-- touch:recovery:end -->`
  (D-08) — bounded, and never touching a byte a human wrote outside those two
  lines — so the resume command in RESUME.md is the harness's, not a
  reconstruction. The harness does not always send one; an absent block is
  normal and leaves the runbook's own instructions standing.

## Detect: was there an outage / is the run alive?

```bash
touch-run status <task-name>        # daemons + recorded pids + stream tail +
                                    # whether the run has already closed
```

It resolves the tasks root itself (the one ladder, printed), so nothing here
has to re-derive it. For the raw view:

```bash
# `status` prints `task folder: <path>` — plus a trailing "(does not exist)"
# and exit 1 for a task it cannot find. The sed strips that suffix; the pipe
# swallows the exit status, so test the folder rather than trust $TASK.
TASK="$(touch-run status <task-name> | sed -n 's/^task folder: \([^ ]*\).*/\1/p')"
[ -d "$TASK" ] || echo "no such task folder: $TASK"
tail -3 "$TASK/events.jsonl"        # token ticks seconds old => alive
tail -2 "<wf_dir>/journal.jsonl"    # a started with no result + long silence => stalled
```

Gap scan (ticks flow every few seconds while an agent works; >90 s gaps are
either one long model generation, a loop-boundary handoff, or an outage —
distinguish by the signatures in layer 1):

```bash
python3 - <<'EOF'
import json; from datetime import datetime
prev=None
for line in open("<project>/.touch/local-orchestrators/<task-name>/events.jsonl"):
    r=json.loads(line); ts=datetime.fromisoformat(r["ts"])
    if prev and (ts-prev).total_seconds()>90:
        print(prev.strftime("%H:%M:%S"),"->",ts.strftime("%H:%M:%S"),
              f"({int((ts-prev).total_seconds())}s)", r.get("plan"), r.get("stage"))
    prev=ts
EOF
```

## Manual restart procedures

**A. Run stopped, SAME session still alive** (thrown by `agentR`, killed, or
script edited): stop the old task if still listed (`TaskStop <taskId>`), then
relaunch with the journal cache:

```
Workflow({ scriptPath: "<task>/orch-scripts/<x>.workflow.js",
           resumeFromRunId: "<wf_…>", args: <same args> })
```

Every completed `agent()` call with unchanged (prompt, opts) replays
instantly from the journal; execution resumes live at the first missing or
changed call — i.e. exactly at the loop the outage interrupted. Same script +
same args in the unchanged prefix is the invariant: never reword earlier
prompts before a resume.

**B. Session died — NEW session** (journal cache does not apply): do NOT
relaunch blindly; the tree already contains completed loops' work.

1. Read the old run's `journal.jsonl` (and `events.jsonl` `plan done/failed`
   cards) to list loops that closed green.
2. Relaunch from the SAME run spec — do not edit the script. The
   `orch-scripts/` copy is byte-for-byte and pinned by the preflight (GD-D9),
   and the sub-plan list is derived by the divider at runtime, so there is no
   array in the file to filter. What a fresh run needs is a spec, and the one
   `touch-run start` wrote is still sitting in the task folder as
   `run-spec.json`.
3. Seed interrupted loops honestly: attempts the outage consumed are NOT
   attempts — only substantive verdicts count (the template's own accounting
   since `agentR` became built-in). Where a loop legitimately needs more room
   than its cap, that is `extra_attempts` in the spec, per sub-plan id — never
   a hand-edited script.
4. Launch fresh (`touch-run start` on the existing folder re-copies the
   template and leaves cards already on the stream at the state they had, then
   `touch-run bind` on the new run). Implementers verify items against the tree
   and gates are idempotent, so a loop that half-finished before the outage
   converges on attempt 1 of the new run; the findings files the old run wrote
   are still the handoff.

**C. Restart one individual loop only** (e.g. a loop the outage caused to
close red): don't re-run the whole workflow — run a focused remediation loop
scoped to that sub-plan: fresh implementer briefed with the loop's open
findings files + the same read-only test gate + critique, same attempt caps.
The findings files under `findings/` are the complete handoff by design.

**D. Daemons after any restart:** `touch-run bind <task> --wf-dir <NEW wf_dir>`
— it records the new directory in `orch-config.json`, stops any watcher it
finds still running on the old one, and relaunches the watcher and the cycle
reporter on the journal it just bound. The monitor server needs no restart (one
instance serves all tasks) and `bind` does not touch it. If you are stopping a
daemon by hand, do it by recorded PID or with the bracket rule
(`pkill -f "[d]ecision_watcher"`, never a bare script name). NEVER delete
`events.jsonl` or `.watcher-state.json` while recovering — history replays on
reconnect and the watcher checkpoint prevents double-counting.

**E. Run completed while offline:** the session died, but nothing was lost —
the run's own records outlive it. The watcher's layered close reads them: if
the snapshot `<session>/workflows/<runId>.json` landed, rung 1 closes the run
from the harness's own status word the next time a watcher runs against that
journal. To settle a stream by hand instead, run the close-out:

```bash
touch-run close <task> --state done --summary "<one line>"
```

It runs `touch-cycle-reporter --settle` first — a one-shot re-read of the
journal that emits ONLY the loop closes the stream implies but is missing, and
nothing at all if they are all present (idempotent, safe to re-run) — then
writes the `orchestrator complete` event **only if no derived rung already
did**, disarms the run scope and stops the daemons. Then build/publish the
final report. Do not hand-type a verdict a record already carries, and never
type `--state failed` for a run whose agents simply returned without a decisive
verdict: that settles `done` with the honest "closed — no verdict" wording
(R-58).

## After restarting

- Verify: `touch-run status <task>` (daemons, recorded pids, stream tail, run
  close), `/health` on the monitor port, fresh ticks in `events.jsonl`, the
  resumed loop's card back to `running`.
- For cards a dead agent left stuck in `running`, run
  `ORCH_STATE_DIR="$TASK" touch-cycle-reporter "<wf_dir>" --settle` rather than
  hand-typing corrective `touch-status` lines (D-14): it derives each missing
  close from the journal's own results, so it cannot assert a verdict the run
  never reached. The stream is append-only either way; never rewrite it.
