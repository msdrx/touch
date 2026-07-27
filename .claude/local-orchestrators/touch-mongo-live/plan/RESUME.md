# touch-mongo-live implement run — recovery / resume runbook (continuation pass)

Supersedes the wf_b297177a-d11 runbook (that run was STOPPED deliberately on
2026-07-26 to adopt the cycle-report loop policy; its 5 finished loops are
frozen, not lost). Current run:

- run id **`wf_93250ff2-ddb`**, task id `w6f1fssrj`, session
  `06e081e6-5531-460c-a2b1-c22e53a0f198` (fourth launch of this pass:
  wf_b297177a-d11 stopped for the policy switch; wf_1a3ffcdd-c60 died in the
  2026-07-26 API outage; wf_cd4b2db3-423 finished ALL 15 loops on 2026-07-27
  ~00:30Z and returned awaiting-user with 7 red retryable loops — user granted
  +1 attempt to every red loop, launched fresh per the cross-session procedure)
- script `orch-scripts/implement-continue.workflow.js` (self-contained: ALL 15
  closed loops are embedded as the FROZEN literal; durable copy in
  `orch-scripts/completed-state.json`)
- this run only re-opens the 7 red loops at attempt 5/5
  (`args.extra_attempts = {each red sp: 1}`, mirrored in orch-config.json):
  sp-watcher-templates-firstwave, sp-refs-mongostore, sp-mirror-deploy,
  sp-agents-reducer, sp-custom-state, sp-server-api, sp-frontend; if all go
  green the final aggregate gate runs (fable, 2 attempts)
- closed green (frozen, adopted as-is): sp-repo-bootstrap ✓2,
  sp-fixtures-freeze ✓1, sp-aggregator-core ✓4, sp-sessions-arm ✓3,
  sp-ingest-pipelines ✓1, sp-legacy-arm ✓1, sp-e2e-acceptance ✓1,
  sp-docs-register ✓1

## LESSON (2026-07-26): resumeFromRunId does NOT work across sessions

A task notification's "To resume …" recovery line is only valid in the session
that launched the run. In a new session the harness accepts the call, keeps the
run id, but starts an EMPTY journal — zero cache hits — so the script re-runs
from the first non-FROZEN loop (it redid sp-mirror-deploy, which had already
closed red 4/4). Cross-session recovery is ALWAYS the "NEW session" procedure
below: extend FROZEN first, then launch fresh WITHOUT resumeFromRunId.

## The loop policy this run enforces

- One deterministic visual report per implement→test→critique cycle:
  `report/cycles/<sp>-cycle-<N>.html` + `index.html`, rendered by the
  `cycle_reporter.py` daemon (never by an LLM), including WHY each cycle
  passed/failed and the findings files as evidence.
- A loop that exhausts its cap (4 + extra_attempts) closes `failed`; the next
  loop starts.
- Final-attempt critique classifies: `needs-own-flow` → never stops the run
  (route that sub-plan to its own execute-research → implement-plan pass
  afterwards); `critical_defect` → the serial run stops immediately, returns
  `status: 'stopped-critical'` — PushNotification the user with
  `decision_needed` and WAIT.
- At the end with red loops: final gate skipped, `status: 'awaiting-user'` —
  ask the user per red retryable loop: another attempt
  (`args.extra_attempts = {"sp-x": 1}` on resume/relaunch — also re-opens a
  frozen red loop) or accept the red close.

## Driver close-out duties (the SCRIPT CANNOT do these — no fs/shell in the
workflow runtime; import() throws)

When the task notification arrives (any status), from the repo root:

```bash
TASK=$PWD/.claude/local-orchestrators/touch-mongo-live
# 1. close the orchestrator badge with the run's REAL state (done only if all_green)
ORCH_STATE_DIR="$TASK" bash .claude/shared/monitoring/status.sh \
  orchestrator complete <done|failed> "<run_close.summary from the return value>"
# 2. stop this task's daemons by recorded pid, verified against /proc
# NB: `grep` here is ugrep, which returns "no match" on NUL-separated
# /proc/*/cmdline unless the bytes are laundered first — pipe through tr.
for f in watcher.pid cycle-reporter.pid; do
  p="$TASK/$f"; [ -f "$p" ] || continue; pid=$(cat "$p")
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null \
    | grep -qE "decision_watcher|cycle_reporter" \
    && kill "$pid" && rm -f "$p"
done
```

Then act on `status`: complete → final report per the skill's Completion
section; awaiting-user → AskUserQuestion about extra attempts; stopped-critical
→ PushNotification + wait. Never touch the shared monitor_server.

## Health check

```bash
TASK=/home/laniakea/Projects/touch/.claude/local-orchestrators/touch-mongo-live
tail -3 "$TASK/events.jsonl"          # fresh token ticks => alive
WF=/home/agent/.claude/projects/-home-laniakea-Projects-touch/06e081e6-5531-460c-a2b1-c22e53a0f198/subagents/workflows/wf_93250ff2-ddb
tail -2 "$WF/journal.jsonl"
ps -p "$(cat "$TASK/watcher.pid")" "$(cat "$TASK/cycle-reporter.pid")"
```

## Resume

- SAME session: `TaskStop w6f1fssrj` if stuck, then
  `Workflow({scriptPath: "<TASK>/orch-scripts/implement-continue.workflow.js",
  resumeFromRunId: "wf_93250ff2-ddb", args: {…same extra_attempts…}})` —
  completed agents replay from cache.
- NEW session (cache does not apply — see LESSON above): FIRST extend the
  frozen state — read the current run's `journal.jsonl` (wf dirs may span
  several session folders; glob
  `~/.claude/projects/-home-laniakea-Projects-touch/*/subagents/workflows/wf_93250ff2-ddb`),
  append every loop that closed to `completed-state.json` AND regenerate the
  FROZEN literal in the script (the injection recipe is in the script header
  comment), THEN launch the script fresh. Never relaunch blindly: FROZEN only
  covers loops closed BEFORE this run started.
- After any relaunch: update `orch-config.json` (`wf_dir`,
  `resume_from_run_id`, `task_id`, and `extra_attempts` if granting attempts —
  the cycle_reporter closes loops against the caps in that file), and restart
  the daemons:

```bash
cd /home/laniakea/Projects/touch
TASK=$PWD/.claude/local-orchestrators/touch-mongo-live
ORCH_STATE_DIR="$TASK" nohup python3 .claude/shared/monitoring/decision_watcher.py "<wf_dir>" \
  >> "$TASK/decision_watcher.log" 2>&1 & echo $! > "$TASK/watcher.pid"
ORCH_STATE_DIR="$TASK" nohup python3 "$TASK/orch-scripts/cycle_reporter.py" "<wf_dir>" \
  $(ls -d ~/.claude/projects/-home-laniakea-Projects-touch/*/subagents/workflows/wf_b297177a-d11) \
  $(ls -d ~/.claude/projects/-home-laniakea-Projects-touch/*/subagents/workflows/wf_1a3ffcdd-c60) \
  $(ls -d ~/.claude/projects/-home-laniakea-Projects-touch/*/subagents/workflows/wf_cd4b2db3-423) \
  $(ls -d ~/.claude/projects/-home-laniakea-Projects-touch/*/subagents/workflows/wf_93250ff2-ddb) \
  >> "$TASK/cycle_reporter.log" 2>&1 & echo $! > "$TASK/cycle-reporter.pid"
```

(cycle_reporter always gets ALL wf dirs of ALL runs of this pass — dedupe the
glob results; it re-reads journals from zero on start; only its emitted-events
checkpoint persists.)

## Never do

- Never delete `events.jsonl`, `.watcher-state.json`,
  `.cycle-reporter-state.json`, or the task folder — completed runs are monitor
  history (repo law).
- Never re-run a sub-plan loop whose card says green — the tree already
  contains its work; re-opening a RED loop goes through `extra_attempts` only.
- Never `pkill -f` daemons — recorded pid + /proc verification only.
