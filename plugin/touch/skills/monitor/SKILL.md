---
name: monitor
description: Plug live websocket monitoring into a deterministic orchestrator (Workflow script or any agent-spawning driver). Use when building or running multi-agent orchestrations that need live per-plan progress, orchestrator decision tracing, and token accounting on a browser dashboard. Provides the touch-run envelope over the touch-status, touch-monitor, touch-watcher and touch-cycle-reporter commands plus the integration protocol.
---

# Orchestrator monitoring

Reusable live monitoring for deterministic orchestrator runs. One command owns
the envelope — `touch-run` (start | bind | close | verify | status, D-13) — and
it drives the three that do the work: `touch-status` (the event writer),
`touch-monitor` (the dashboard server) and `touch-watcher` (the
journal-derived decision/token daemon), plus `touch-cycle-reporter` (the
per-cycle and final-report renderer). Call them **by name**: they are on PATH
wherever this plugin is installed, and a name survives an update that moves the
files behind it.

Everything is bash + Python 3 stdlib + a browser — no pip installs, works
behind egress proxies. The commands are stateless and task-agnostic; never
copy or modify them per task. Each orchestration keeps its own state folder at
`<project>/.touch/local-orchestrators/<task-name>/` — inside the user's
project, never inside this plugin.

Full reference for the **event schema, dashboard behaviour and daemon
lifecycle**: `${CLAUDE_PLUGIN_ROOT}/shared/monitoring/monitoring.md` — read it
before changing anything about the stream, but for the SEMANTICS only. Its
shell snippets are development-repo forms that invoke daemon files by path; on
an installed plugin the commands above are the invocation surface, so ignore
any run recipe there that disagrees with this one.

## Architecture

- `touch-run` — the driver envelope: creates the task folder, copies the
  template verbatim, preflights the run spec, seeds the cards, arms the run
  scope, starts and stops the daemons by RECORDED pid, closes the run
- `events.jsonl` — append-only event stream in the task state folder
- `touch-status` — the ONE write path into it (it owns the flock, the 1 KB cap
  and the `w` attribution). Its callers are a human, a driver, and the
  deterministic emitters below — never a mandated line in an agent prompt
- `touch-monitor` — serves the dashboard at `/`, streams events over a
  websocket at `/ws` (full replay on connect, then live tail; `?task=<name>`
  selects the task), and answers `/tasks` (every discovered task folder) and
  `/health`. Loopback bind, per-boot token, prints the URL to open
- `touch-watcher` — tails the Workflow run's `journal.jsonl`, emits
  orchestrator decision events (spawn / verdict / retry / advance) and
  per-agent token accounting (live ~1s deltas for running agents, deduped by
  API message id, restart-safe), and DERIVES the run close (below)
- `touch-cycle-reporter` — renders `report/cycles/*.html` and the run's final
  report from the same journal, and emits the loop-terminal `plan done|failed`
  events; `touch-run bind` starts it
- the dashboard page itself — event-driven: one card per plan, per-stage chips,
  newest-first logs, pulsing token counters; never regenerated for updates

**The deterministic emitters of the run's own flow are three — the two daemons
above plus the envelope: `touch-watcher`, `touch-cycle-reporter` and
`touch-run`** (GD-D5, D-10; call them by command name, never by the file behind
them, which lives in a version-stamped cache). A fourth,
`hooks/agent_lifecycle.py`, appends subagent start/stop lines through
`touch-status` when a run is active, disclosing itself as `(hook <id8>)` in the
`detail` — it is additive, never the floor (GD-D5): inert without an `ACTIVE`
sentinel, and no card, badge or verdict depends on it. The workflow SCRIPT
emits nothing: the runtime has no Node API, so every
`runStatus`/`closeRun`/`publishConfig` helper the templates once carried
silently no-opped in every real run — one run failed on nothing else — and they
are deleted rather than kept as decoration. Anything you read that says a
script emits an event is describing a contract a daemon fulfils.

## Running an orchestration — the `touch-run` envelope

Three verbs bracket a run and two more ask questions of it. They replace the
~105 lines of hand-typed recipe this section used to carry (mkdir, cp, seed,
sentinel, daemons, config, close-out): every step had exactly one correct
spelling and no judgment in it, which is the definition of something a script
owns (D-13, GD-D8). `touch-run` is **not** a control verb — it starts and stops
TOUCH's own daemons and touches no Claude Code session, so `CONTROL_ROUTES`
stays `{}`.

```bash
touch-run start <task> --spec run-spec.json   # folder + verbatim script copy +
      # preflight + seeded cards + ACTIVE + monitor; prints the dashboard URL
      # and the exact Workflow({ scriptPath, args }) line to launch next
touch-run bind  <task>                        # AFTER the Workflow is running:
      # records wf_dir/run id/port in orch-config.json, renders plan/RESUME.md,
      # starts the watcher and the cycle reporter on the journal it just bound
touch-run close <task> --state done --summary "<one line>"
      # settles open cards (reporter --settle), closes the run card if no
      # derived rung already did, removes ONLY this task's ACTIVE line, stops
      # this task's daemons by recorded pid (the shared monitor is left alone)
touch-run verify <task>                       # the launch preflight, standalone
touch-run verify --spec <file>                # ...or before a task folder exists
touch-run status <task>                       # daemons, pids, stream, run close
```

The driver authors the **run spec** and nothing else (GD-D9): a JSON object of
per-run values (`task`, `project_dir`, `subject`/`perspectives` or `plan_file`,
`context`, caps, `title`, `roster`) that `touch-run start` merges over the
tracked per-project constants in `.touch/run.json` and hands to the script as
`args`. The `orch-scripts/` copy is a byte-for-byte `cp` of the shipped
template — never a re-emitted or hand-edited script — and the preflight refuses
a spec still carrying a placeholder or naming a plan file that does not exist.

Two things it gets right that hand-typed recipes reliably got wrong:

- **The tasks root is resolved once, by the shipped ladder, and PRINTED** —
  `$ORCH_TASKS_ROOT` (only when it names an existing directory) >
  `$CLAUDE_PROJECT_DIR/.touch/local-orchestrators` > a walk up from the cwd to a
  `.claude/` project marker, then `.touch/local-orchestrators` beneath it. (The
  marker and the state directory differ on purpose: `.claude/` is what makes a
  folder a *Claude Code* project, while `.touch/` is Touch's own and
  gitignored.) Anchoring on a bare `$PWD` is the one mistake that fails
  **silently** — sentinels land where the run-scope guard never looks, so it
  reports itself inert and the `HALT` brake never fires for a whole run.
- **The `ACTIVE` close-out edits one line.** `start` appends this task's name
  idempotently under a lock; `close` drops exactly that line and writes every
  other back, byte for byte. The shell idiom this file used to document — an
  inverted grep into a temp file beside the sentinel — is **retired** and
  deliberately not reprinted: its temp path was fixed and shared, so two
  concurrent close-outs interleaved into one file, and its fallback branch
  deleted the WHOLE sentinel whenever grep failed for any reason but "nothing
  matched", disarming the run scope for every other run still listed.

**Both sentinels live in that resolved tasks root**, never where the driver
happens to be standing. While `ACTIVE` lists task names (one per line), the
PreToolUse run-scope guard this plugin registers lets loop SUBAGENTS touch only
those tasks' folders; every other task keeps its `plan/` readable (the authority
ladder) and the rest denied. `HALT` is the second — a file of that name denies
every loop subagent outright, so a run stops without anything being killed. Both
names are exempt from the guard's own denials (a brake the governed agents
cannot read is not a brake) and neither is a task folder — never list `HALT` in
`ACTIVE`. The main terminal agent is never restricted; with no `ACTIVE` file the
guard is inert, and a crashed run's leftover line only over-restricts (every
deny reason lists the active tasks) — delete it.

**Dashboard**: the tokened URL `touch-run start` prints
(`http://127.0.0.1:8931/?token=…`). The server binds loopback on purpose —
forward the port over SSH rather than opening one. One `touch-monitor` serves
every task on the project (the header dropdown switches tasks live; `/tasks`
rescans per request), so `start` probes `/health` and only starts one if none
answers, and `close` never stops it. The token also rests in `monitor.json`
(0600) — but only in the state folder the running monitor was STARTED under, so
a second task sharing it has no `monitor.json` of its own and reads the first
one's. One `touch-watcher` and one `touch-cycle-reporter` per task.

Doing it by hand is still possible — the daemons take `ORCH_STATE_DIR` and the
same argv they always did (`touch-monitor` port: argv > `$ORCH_PORT` > config >
8931; `touch-watcher` wf_dir: argv > `$ORCH_WF_DIR` > config > newest `wf_*`) —
but the last rung of that watcher fallback binds ONCE, at import, to whatever
run was newest on the machine, which is why `bind` and not `start` is where the
watcher is launched.

## What is derived, and what you may still write

- **The `[monitor]` marker is the deterministic event source and is FENCED**
  (GD-D1a): script-authored prompt text at a fixed control-flow point, recorded
  in the Workflow journal, from which the watcher raises spawn / result /
  plan-badge events with zero LLM cooperation. Never trim, rename or move it.
  ```
  [monitor] plan=<plan-id> stage=<stage> role=<role> attempt=<n>
  ```
  The LAST occurrence in the prompt wins (quoted findings/JSON may embed stray
  markers from earlier agents). Roles `impl`, `test`, `critique`, `gate:run`,
  `gate:fix`, `e2e:run`, `e2e:fix` get semantic decision lines; other roles get
  generic ones. Result state derives from the structured-output shape
  (`findings`/`real`/`fixed_ids`/`files_changed`/`passed`/`approved`).
- **Do NOT mandate status calls in agent prompts.** The FIRST/LAST
  `touch-status` pair prompts used to require is deleted, and no template emits
  it. The reason is correctness, not tokens — the pair was 0.05% of a run's
  bill (~$0.14), so it must never be re-introduced or defended with a cost
  argument in either direction. An instruction is not a mechanism: a mandated
  line can be forgotten, mistyped, or written into the wrong task folder with a
  missing `ORCH_STATE_DIR`, while the watcher derives the same spawn and result
  from the journal and the marker — measured 96–99% twin coverage, usually
  *earlier* than the agent's own line, against 79–100% compliance across 1,197
  solo model requests project-lifetime. The one thing the pair carried that
  derivation could not — the agent's own `summary` — now rides on the derived
  result line, which made the deletion information-neutral; the stated trade is
  the STAMP, since a transcript that stops flushing mid-run leaves the watcher's
  read moment rather than the true end (honest about being observed, not
  confidently wrong). What stays legitimate is an OPTIONAL progress note for a
  genuinely unobservable middle (one long tool call with nothing between spawn
  and result), never for spawn or result state:
  ```
  Optional, for a long unobservable middle only:
    ORCH_STATE_DIR=<task-dir> touch-status <plan> <stage> info "<what is happening now>"
  ```
  `ORCH_STATE_DIR` stays mandatory in any such call — the writer never guesses
  which task it is reporting into.
- **The run close is DERIVED; a driver-typed close is belt-and-braces, no
  longer a MUST** (GD-D6). The watcher polls two recorded sources and the
  first rung to land wins, with the one it fired on recorded in the `detail`:
  (1) the run snapshot `<session>/workflows/<runId>.json`, carrying the
  harness's own `completed|failed|killed` vocabulary; (2) the driver session's
  close notification, matched on `record.origin.kind == "task-notification"`
  and joined by the launch record's `taskId`; (3) a driver-typed
  `touch-status orchestrator complete done "<summary>"` — still honoured,
  simply no longer required, and `touch-run close` writes it only when no
  earlier rung has; (4) the existing quiet/abandon timeouts, unchanged. Neither
  recorded source is guaranteed (~7% of runs never get a snapshot), so absence
  is normal and never an error — and `killed` never renders `done`.
- **The plane keeps exactly two writers** (GD-D14): `touch-status`-mediated
  `agent` lines and the watcher's own. The deletion above removed instructed
  invocations, not the command — and the lifecycle hook is not a third writer
  either: it shells out to `touch-status` like any other caller, so its lines
  land on the `agent` side with a `(hook <id8>)` disclosure in the `detail`.

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
monotonically, so plans not yet started still count. It has deterministic
writers now (GD-D11): `touch-run start` seeds it from the spec's roster, and
`touch-cycle-reporter` re-declares it at the divide close, where the real
partition is first known. Optional `roster` (array of `"<id> — <title>"`
strings on an `orchestrator`-card event, latest wins) declares the planned
sub-plan list so loops with no card yet render as neutral `planned` bullets;
`ORCH_ROSTER` on `touch-status` is a **file path**, never env-inlined JSON, and
is bounded at the writer.

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
- Stop a run's daemons when the run ends — `touch-run close <task>` does it by
  RECORDED pid, verifying the `/proc` cmdline before it signals, and leaves the
  state files in place. Never a name-matched kill (pids get reused and other
  tasks' watchers are live processes). If you must match by name from a shell,
  bracket the first letter so the command does not match itself:
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
API calls are at risk. Full strategy — detection signatures, the `agentR` retry
wrapper both shipped templates now carry built in, the resume pointers
`touch-run bind` records (`orch-config.json` + `plan/RESUME.md`), and the manual
restart procedures (same-session `resumeFromRunId`, new-session skip-green
relaunch, single-loop remediation, daemon restarts) — lives in
`${CLAUDE_PLUGIN_ROOT}/skills/monitor/network-recovery.md`. Read it
BEFORE launching long runs on flaky networks, and again before any manual
restart.
