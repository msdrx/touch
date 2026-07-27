---
name: m-orchestrator
description: Plug live websocket monitoring into a deterministic orchestrator (Workflow script or any agent-spawning driver). Use when building or running multi-agent orchestrations that need live per-plan progress, orchestrator decision tracing, and token accounting on a browser dashboard. Provides generic scripts in .claude/shared/monitoring/ plus the integration protocol.
---

# Orchestrator monitoring

Reusable live monitoring for deterministic orchestrator runs. The generic
scripts live in `.claude/shared/monitoring/` (stateless, task-agnostic —
do not copy or modify them per task). Each orchestration keeps its own state
folder under `.claude/local-orchestrators/<task-name>/`.

Full reference: `.claude/shared/monitoring/monitoring.md`. Everything is
bash + Python 3 stdlib + a browser — no pip installs, works behind egress
proxies.

## Architecture

- `status.sh` — agents append status events at deterministic points
- `events.jsonl` — append-only event stream in the task state folder
- `monitor_server.py` — serves the dashboard at `/` and streams events over a
  websocket at `/ws` (full replay on connect, then live tail;
  `?task=<name>` selects the task); `/tasks` lists all discovered task
  folders; `/health` probe
- `decision_watcher.py` — tails the Workflow run's `journal.jsonl`, emits
  orchestrator decision events (spawn / verdict / retry / advance) and
  per-agent token accounting (live ~1s deltas for running agents, deduped by
  API message id, restart-safe)
- `monitor.html` — event-driven dashboard: one card per plan, per-stage chips,
  newest-first logs, pulsing token counters; never regenerated for updates

## Integration steps for a new orchestration

1. Create the task state folder and (optionally) pin config:
   ```bash
   TASK=/path/to/repo/.claude/local-orchestrators/<task-name>
   mkdir -p "$TASK"
   # optional orch-config.json: {"wf_dir": "<workflow transcript dir>", "port": 8931}
   ```
2. Seed one card per sub-plan before launching:
   ```bash
   export ORCH_STATE_DIR="$TASK"
   S=/path/to/repo/.claude/shared/monitoring/status.sh
   ORCH_TITLE="Phase 1 — parser" "$S" phase1 plan queued "short description"
   ```
3. In the orchestrator script, embed the classification marker in every
   agent prompt — this is the DETERMINISTIC event source (script-authored
   text at a fixed control-flow point, recorded in the Workflow journal;
   the watcher raises spawn/result/plan-badge events from it with zero LLM
   cooperation):
     ```
     [monitor] plan=<plan-id> stage=<stage> role=<role> attempt=<n>
     ```
     `stage=` may be omitted if the prompt mandates a `status.sh ... running`
     command naming the stage. The LAST marker occurrence in the prompt wins
     (quoted findings/JSON may embed stray markers from earlier agents).
     Roles `impl`, `test`, `critique`, `gate:run`, `gate:fix`, `e2e:run`,
     `e2e:fix` get semantic decision lines; other roles get generic ones.
     Result state derives from the structured-output shape
     (`findings`/`real`/`fixed_ids`/`files_changed`/`passed`/`approved`).
   Optionally ALSO mandate status calls at fixed points for intra-agent
   progress notes — best-effort color, not the source of truth (note:
   agents need `ORCH_STATE_DIR` in the command, or a wrapper/symlink in
   the state folder):
     ```
     FIRST run: ORCH_STATE_DIR=<task-dir> bash <shared>/status.sh <plan> <stage> running "attempt N: ..."
     LAST  run: ORCH_STATE_DIR=<task-dir> bash <shared>/status.sh <plan> <stage> done|failed "attempt N: <summary>"
     ```
     Mark plan completion with `... <plan> plan done "..."`.
   When the whole run ends, the driver MUST close the Orchestrator card's
   badge (the watcher cannot see run completion in the journal — without
   this the card shows `running` forever):
     ```
     ORCH_STATE_DIR=<task-dir> bash <shared>/status.sh orchestrator complete done "<run summary>"
     ```
4. Start the daemons (background; restart-safe) and declare the run scope by
   appending this task's line to the `ACTIVE` sentinel (one task name per
   line — concurrent runs coexist; never truncate with `>`):
   ```bash
   ORCH_STATE_DIR="$TASK" python3 <shared>/monitor_server.py &   # port: argv > $ORCH_PORT > config > 8931
   ORCH_STATE_DIR="$TASK" python3 <shared>/decision_watcher.py & # wf_dir: argv > $ORCH_WF_DIR > config > newest wf_* journal
   f=.claude/local-orchestrators/ACTIVE
   grep -qxF "<task-name>" "$f" 2>/dev/null || printf '%s\n' "<task-name>" >> "$f"
   ```
   The sentinel arms the run-scope guard (`.claude/hooks/orch_scope_guard.py`,
   a PreToolUse hook registered in `.claude/settings.json`): while it lists
   tasks, loop SUBAGENTS can touch only the listed tasks' folders under
   `local-orchestrators/` — other tasks keep their `plan/` readable (authority
   ladder) and have everything else denied. The main terminal agent is never
   restricted. At run end, next to the `orchestrator complete` close-out,
   remove ONLY this task's line (another run may still be active):
   ```bash
   f=.claude/local-orchestrators/ACTIVE
   grep -vxF "<task-name>" "$f" > "$f.tmp" 2>/dev/null
   if [ -s "$f.tmp" ]; then mv "$f.tmp" "$f"; else rm -f "$f" "$f.tmp"; fi
   ```
   A crashed run leaves its line behind, which only over-restricts subagents
   (every deny reason lists the active tasks) — delete the stale line (or the
   whole file if nothing is running) and move on.
5. Dashboard: `http://<host>:8931/`. If the sandbox's ports aren't reachable
   from the host, run `monitor_server.py` on the host — if the state folder is
   on a shared mount it tails the same `events.jsonl`. `ORCH_STATE_DIR` is
   optional for the daemons: unset, they auto-discover the most recently
   active `.claude/local-orchestrators/*/events.jsonl` (both print the
   resolved state dir on startup). It stays required in agent `status.sh`
   calls, which must never guess where to write. When several orchestrations
   run at once, one `monitor_server.py` serves them all — the dashboard's
   header dropdown switches tasks live (`/tasks` rescans the folder per
   request, so later-started tasks appear without a restart); run one
   `decision_watcher.py` per task.

## Event schema (one JSON line per event)

```json
{"ts": "<ISO>", "plan": "<id>", "stage": "<stage>", "state": "queued|running|done|failed|info|stale", "detail": "...", "title": "<optional>", "plans_total": 0, "tokens": {"in": 0, "out": 0, "cached": 0, "cache_write": 0}, "quiet": true}
```

Reserved: plan id `orchestrator` (wide card, pinned last — the watcher writes
there); stage `plan` (sets the card badge); stage `complete` (badge alias of
`plan` — for the final run-summary event on the `orchestrator` card); stage
`tokens` (delta added to the card's token counter; `quiet` events skip the
log). Any event's `title` renames its card. `state: "stale"` marks an abandoned
agent/row (watcher-emitted). `tokens` deltas carry optional `cached` (cache-read,
shown as `r:`) and `cache_write` (cache-write, shown as `w:`) breakdowns on top
of total `in`. Optional `plans_total` (integer; `ORCH_PLANS_TOTAL` env with
`status.sh`) declares the run's expected total plan-card count — dashboards use
`max(cards seen, declared total)` as the progress denominator, folded
monotonically, so plans not yet started still count; the reference templates
declare it at their partition/barrier close.

The decision watcher also attaches a per-subagent `agent` sub-object that drives
the live per-agent rows and the impl→test→critique flow strip:

```json
{"agent": {"id": "<agent-id>", "label": "<short label>", "state": "running|done|failed|stale", "tokens": {"in": 0, "out": 0, "cached": 0, "cache_write": 0}, "started": "<ISO>", "runtime": "<seconds/human>"}}
```

## Operational rules

- Completed tasks are history — NEVER delete a finished task's state folder
  or its `events.jsonl`. The dashboard's task dropdown lists every surviving
  folder and full history replays on connect, so a monitor started later
  still shows old runs' complete progress. There is no completion cleanup
  step.
- Resetting a run (wipe-and-rerun of one task still being iterated on — not
  cleanup after completion): stop daemons, delete that task's `events.jsonl`
  + `.watcher-state.json` from the state folder, re-seed, restart — the
  watcher re-backfills the whole journal with true timestamps (spawn = first
  transcript line, result = last).
- Never `pkill -f` these scripts from a command that spells the script name —
  bracket the first letter: `pkill -f "[m]onitor_server"`.
- Keep detail strings short, single-line, without double quotes.
- **Time policy — implement in UTC, display in the user's local zone.** Every
  timestamp WRITTEN by scripts, daemons, agents, and event streams
  (`events.jsonl` `ts`, watcher emits, findings, configs) is UTC, ISO-8601
  with explicit offset (`+00:00`/`Z`); run daemons and any shell that calls
  `status.sh` with `TZ=UTC` so this holds regardless of container settings.
  Conversion to local time happens ONLY at the presentation layer: the
  dashboard renders `ts` in the viewer's browser timezone, and assistant
  responses/reports show the OS-configured local zone (read it from
  `/etc/localtime`; here `Asia/Tbilisi`, `+04`). Never store local-zone
  timestamps; never compare or sort mixed-source times except as parsed
  instants. Historic events already written with a non-UTC offset are valid
  instants — leave them; the stream is append-only.

## Network failure and manual loop restart

Everything monitored is local and survives uplink loss; only in-flight agent
API calls are at risk. Full strategy — detection signatures, the `agentR`
retry wrapper every workflow script should use, launch-time resume pointers
(`orch-config.json` + `plan/RESUME.md`), and the manual restart procedures
(same-session `resumeFromRunId`, new-session skip-green relaunch, single-loop
remediation, daemon restarts) — lives in `network-recovery.md` next to this
file. Read it BEFORE launching long runs on flaky networks, and again before
any manual restart.
