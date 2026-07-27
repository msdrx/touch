# Network failure → manual loop restart strategy

How to detect, ride out, and manually restart orchestrator loops when the
network drops (mobile uplink, proxy flap, API outage). Everything monitored
(daemons, dashboard, `events.jsonl`, the Workflow `journal.jsonl`) is LOCAL
and survives any uplink loss — only in-flight agent API calls are at risk.
Proven on the 2026-07-25 touch-mongo-live implement run (two real incidents,
zero lost work).

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
3. **Driver/session death** — the workflow stops. All completed work persists
   in the journal + the working tree. Restart manually (below).

## Launch-time prophylaxis (do this when starting any run)

- Wrap all `agent()` call sites in the workflow script:

  ```js
  const NET_RETRIES = 3
  const agentR = async (prompt, opts) => {
    let r = await agent(prompt, opts)
    for (let n = 1; r === null && n <= NET_RETRIES; n++) {
      log(`${opts.label}: agent returned null (network drop?) — retry ${n}/${NET_RETRIES}`)
      r = await agent(
        prompt + `\n(infrastructure retry ${n}: the previous try of this exact task died without returning — network outage, not a task failure. Do the task from scratch.)`,
        { ...opts, label: `${opts.label}~r${n}` })
    }
    if (r === null) throw new Error(`${opts.label}: died ${NET_RETRIES + 1}x — network down? resume per RESUME.md`)
    return r
  }
  ```

  The appended retry tag makes the prompt distinct, so a later
  `resumeFromRunId` re-executes the retried call live instead of replaying a
  cached `null`. The `[monitor]` marker is unchanged: same attempt, extra
  agent row — honest display. Keep the wrapper's own `agent()` calls raw.
- Record resume pointers immediately after launch in the task's
  `orch-config.json`: `"resume_from_run_id": "<wf_…>"` plus the `wf_dir`,
  and write a `plan/RESUME.md` runbook (health check, resume invocation,
  daemon restart) so recovery needs zero archaeology.

## Detect: was there an outage / is the run alive?

```bash
TASK=.claude/local-orchestrators/<task-name>
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
for line in open(".claude/local-orchestrators/<task-name>/events.jsonl"):
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
2. Edit a copy of the workflow script to skip those loops (filter the
   sub-plans array by id), keep everything else byte-identical.
3. Launch fresh. Implementers verify items against the tree and gates are
   idempotent, so a loop that half-finished before the outage converges on
   attempt 1 of the new run.

**C. Restart one individual loop only** (e.g. a loop the outage caused to
close red): don't re-run the whole workflow — run a focused remediation loop
scoped to that sub-plan: fresh implementer briefed with the loop's open
findings files + the same read-only test gate + critique, same attempt caps.
The findings files under `findings/` are the complete handoff by design.

**D. Daemons after any restart:** one watcher per task, pointed at the NEW
`wf_dir` (argv beats env beats config); update `orch-config.json.wf_dir`.
The monitor server needs no restart (one instance serves all tasks). Kill
stale watchers by PID or with the bracket rule (`pkill -f "[d]ecision_watcher"`
never a bare script name). NEVER delete `events.jsonl` or
`.watcher-state.json` while recovering — history replays on reconnect and the
watcher checkpoint prevents double-counting.

**E. Run completed while offline:** the completion notification died with the
session, but the journal's final record holds the return value. Read it, close
any still-open cards (`status.sh <plan> plan done "…"`), emit
`status.sh orchestrator complete done "<summary>"`, then build/publish the
final report.

## After restarting

- Verify: `/health` on the monitor port, fresh ticks in `events.jsonl`, the
  resumed loop's card back to `running`.
- Append corrective `status.sh` events for any card a dead agent left stuck
  in `running` — the stream is append-only; never rewrite it.
