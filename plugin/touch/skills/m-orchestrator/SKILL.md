---
name: m-orchestrator
description: Plug live websocket monitoring into a deterministic orchestrator (Workflow script or any agent-spawning driver). Use when building or running multi-agent orchestrations that need live per-plan progress, orchestrator decision tracing, and token accounting on a browser dashboard. Provides the touch-status, touch-monitor and touch-watcher commands plus the integration protocol.
---

# Orchestrator monitoring

Reusable live monitoring for deterministic orchestrator runs. Three commands do
the work — `touch-status` (the event writer), `touch-monitor` (the dashboard
server) and `touch-watcher` (the journal-derived decision/token daemon). Call
them **by name**: they are on PATH wherever this plugin is installed, and a
name survives an update that moves the files behind it.

Everything is bash + Python 3 stdlib + a browser — no pip installs, works
behind egress proxies. The commands are stateless and task-agnostic; never
copy or modify them per task. Each orchestration keeps its own state folder at
`<project>/.claude/local-orchestrators/<task-name>/` — inside the user's
project, never inside this plugin.

Full reference for the **event schema, dashboard behaviour and daemon
lifecycle**: `${CLAUDE_PLUGIN_ROOT}/shared/monitoring/monitoring.md`. Read it
before changing anything about the stream — but read it for the semantics only:
its shell snippets are the development-repo forms (they invoke the daemon files
by path, from a checkout that has them at a fixed location). On an installed
plugin the commands above are the invocation surface; ignore any run recipe in
that file that disagrees with this one.

## Architecture

- `touch-status` — agents append status events at deterministic points
- `events.jsonl` — append-only event stream in the task state folder
- `touch-monitor` — serves the dashboard at `/` and streams events over a
  websocket at `/ws` (full replay on connect, then live tail;
  `?task=<name>` selects the task); `/tasks` lists all discovered task
  folders; `/health` probe. It binds loopback and mints a per-boot token,
  printing the URL to open
- `touch-watcher` — tails the Workflow run's `journal.jsonl`, emits
  orchestrator decision events (spawn / verdict / retry / advance) and
  per-agent token accounting (live ~1s deltas for running agents, deduped by
  API message id, restart-safe)
- the dashboard page itself — event-driven: one card per plan, per-stage chips,
  newest-first logs, pulsing token counters; never regenerated for updates

## Integration steps for a new orchestration

1. Resolve the **tasks root** once, then create this run's state folder under
   it and (optionally) pin config:
   ```bash
   ORCH="${ORCH_TASKS_ROOT:-${CLAUDE_PROJECT_DIR:-$PWD}/.claude/local-orchestrators}"
   TASK="$ORCH/<task-name>"
   mkdir -p "$TASK"
   # optional orch-config.json: {"wf_dir": "<workflow transcript dir>", "port": 8931}
   ```
   That expression is the resolution order the run-scope guard uses, in the
   same precedence — `$ORCH_TASKS_ROOT` (only when exported into the `claude`
   process and naming an existing directory) > `$CLAUDE_PROJECT_DIR/.claude/local-orchestrators`
   > a marker-ceilinged walk up from the cwd. Anchoring on a bare `$PWD`
   instead is the one mistake that fails **silently**: the daemons and the
   sentinels land somewhere the guard never looks, so the guard reports itself
   inert and the HALT brake never fires for the whole run (step 4).
2. Seed one card per sub-plan before launching:
   ```bash
   export ORCH_STATE_DIR="$TASK"
   ORCH_TITLE="Phase 1 — parser" touch-status phase1 plan queued "short description"
   ```
3. In the orchestrator script, embed the classification marker in every
   agent prompt — this is the DETERMINISTIC event source (script-authored
   text at a fixed control-flow point, recorded in the Workflow journal;
   the watcher raises spawn/result/plan-badge events from it with zero LLM
   cooperation):
     ```
     [monitor] plan=<plan-id> stage=<stage> role=<role> attempt=<n>
     ```
     `stage=` may be omitted if the prompt mandates a `touch-status ... running`
     command naming the stage. The LAST marker occurrence in the prompt wins
     (quoted findings/JSON may embed stray markers from earlier agents).
     Roles `impl`, `test`, `critique`, `gate:run`, `gate:fix`, `e2e:run`,
     `e2e:fix` get semantic decision lines; other roles get generic ones.
     Result state derives from the structured-output shape
     (`findings`/`real`/`fixed_ids`/`files_changed`/`passed`/`approved`).
   Optionally ALSO mandate status calls at fixed points for intra-agent
   progress notes — best-effort color, not the source of truth (note:
   agents need `ORCH_STATE_DIR` in the command; the writer never guesses which
   task it is reporting into):
     ```
     FIRST run: ORCH_STATE_DIR=<task-dir> touch-status <plan> <stage> running "attempt N: ..."
     LAST  run: ORCH_STATE_DIR=<task-dir> touch-status <plan> <stage> done|failed "attempt N: <summary>"
     ```
     Mark plan completion with `... <plan> plan done "..."`.
   When the whole run ends, the driver MUST close the Orchestrator card's
   badge (the watcher cannot see run completion in the journal — without
   this the card shows `running` forever):
     ```
     ORCH_STATE_DIR=<task-dir> touch-status orchestrator complete done "<run summary>"
     ```
4. Start the daemons (background; restart-safe) and declare the run scope by
   appending this task's line to the `ACTIVE` sentinel (one task name per
   line — concurrent runs coexist; never truncate with `>`):
   ```bash
   ORCH_STATE_DIR="$TASK" touch-monitor &   # port: argv > $ORCH_PORT > config > 8931
   ORCH_STATE_DIR="$TASK" touch-watcher &   # wf_dir: argv > $ORCH_WF_DIR > config > newest wf_* journal
   echo $! > "$TASK/watcher.pid"            # a workflow epilogue stops it by RECORDED pid
   f="$ORCH/ACTIVE"
   grep -qxF "<task-name>" "$f" 2>/dev/null || printf '%s\n' "<task-name>" >> "$f"
   ```
   The sentinel arms the run-scope guard — the PreToolUse hook this plugin
   registers (on by default; the `run_scope_guard` plugin setting turns it
   off). While `ACTIVE` lists tasks, loop SUBAGENTS can touch only the listed
   tasks' folders under the tasks root; other tasks keep their `plan/` readable
   (authority ladder) and have everything else denied. The main terminal agent
   is never restricted, and with no `ACTIVE` file the guard is inert.

   **`ACTIVE` and `HALT` live in the tasks root the guard resolves** — the
   `$ORCH` of step 1, not wherever the driver happens to be standing. Write
   them anywhere else and the guard finds no sentinel and stays inert for the
   whole run; nothing errors and nothing warns. `HALT` is the second sentinel:
   a file of that name in the tasks root is the emergency brake — while it
   exists every loop subagent is denied outright, so a run can be stopped
   without killing anything. Both names are exempt from the guard's own denials
   (a brake the governed agents cannot read is not a brake), and neither is a
   task folder — never list `HALT` in `ACTIVE`.

   At run end, next to the `orchestrator complete` close-out, remove ONLY this
   task's line (another run may still be active):
   ```bash
   f="$ORCH/ACTIVE"
   grep -vxF "<task-name>" "$f" > "$f.tmp" 2>/dev/null
   if [ -s "$f.tmp" ]; then mv "$f.tmp" "$f"; else rm -f "$f" "$f.tmp"; fi
   ```
   A crashed run leaves its line behind, which only over-restricts subagents
   (every deny reason lists the active tasks) — delete the stale line (or the
   whole file if nothing is running) and move on.
5. Dashboard: the **tokened** URL `touch-monitor` prints at startup
   (`http://127.0.0.1:8931/?token=…`); it is also written into
   `<task-dir>/monitor.json`. The server binds loopback on purpose — to reach
   it from another machine, forward the port over SSH rather than opening one.
   `ORCH_STATE_DIR` is optional for the daemons: unset, they auto-discover the
   most recently active task folder's `events.jsonl` (both print the resolved
   state dir on startup). It stays required in agent `touch-status` calls,
   which must never guess where to write. When several orchestrations run at
   once, one `touch-monitor` serves them all — the dashboard's header dropdown
   switches tasks live (`/tasks` rescans the folder per request, so
   later-started tasks appear without a restart); run one `touch-watcher` per
   task.

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
`touch-status`) declares the run's expected total plan-card count — dashboards
use `max(cards seen, declared total)` as the progress denominator, folded
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
- Stop a run's watcher when the run ends — by RECORDED pid, never by a
  name-matched kill (pids get reused and other tasks' watchers are live
  processes) — and leave its state files in place. If you must match by name
  from a shell, bracket the first letter so the command does not match itself:
  `pkill -f "[m]onitor_server"`.
- Keep detail strings short, single-line, without double quotes.
- **Time policy — implement in UTC, display in the user's local zone.** Every
  timestamp WRITTEN by scripts, daemons, agents, and event streams
  (`events.jsonl` `ts`, watcher emits, findings, configs) is UTC, ISO-8601
  with explicit offset (`+00:00`/`Z`); run daemons and any shell that calls
  `touch-status` with `TZ=UTC` so this holds regardless of container settings.
  Conversion to local time happens ONLY at the presentation layer: the
  dashboard renders `ts` in the viewer's browser timezone, and assistant
  responses/reports show the machine's configured local zone (read it from
  `/etc/localtime`). Never store local-zone timestamps; never compare or sort
  mixed-source times except as parsed instants. Historic events already
  written with a non-UTC offset are valid instants — leave them; the stream is
  append-only.

## Network failure and manual loop restart

Everything monitored is local and survives uplink loss; only in-flight agent
API calls are at risk. Full strategy — detection signatures, the `agentR`
retry wrapper every workflow script should use, launch-time resume pointers
(`orch-config.json` + `plan/RESUME.md`), and the manual restart procedures
(same-session `resumeFromRunId`, new-session skip-green relaunch, single-loop
remediation, daemon restarts) — lives in
`${CLAUDE_PLUGIN_ROOT}/skills/m-orchestrator/network-recovery.md`. Read it
BEFORE launching long runs on flaky networks, and again before any manual
restart.
