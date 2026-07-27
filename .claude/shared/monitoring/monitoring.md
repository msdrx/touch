# Orchestrator monitoring module

Reusable, task-agnostic live monitoring for deterministic orchestrator runs
(Workflow-tool scripts, or any driver that spawns agents). This module lives
in `.claude/shared/monitoring/` and is stateless; each task keeps its
orchestration scripts and monitoring state (events, config, checkpoints) in
`.claude/local-orchestrators/<task-name>/`, selected via the
`ORCH_STATE_DIR` env var. See the `m-orchestrator` skill for the
integration checklist.

Zero third-party dependencies: bash + Python 3 stdlib + a browser.

## Files

| File | Role |
|------|------|
| `status.sh` | Deterministic trace point — agents/orchestrators append status events |
| `events.jsonl` | Append-only event stream (single source of truth; created on first event) |
| `monitor_server.py` | HTTP + WebSocket server: serves `monitor.html` at `/`, streams `events.jsonl` at `/ws` (full replay on connect, then live tail; `?task=<name>` selects a task), `/tasks` lists every discovered task folder, `/artifacts?task=<name>` lists the task's `.html`/`.md` artifacts, `/file?task=<name>&path=<rel>` serves one (extension-whitelisted, realpath-contained), `/health` probe |
| `monitor.html` | Live dashboard — one card per plan, event-driven, no rebuild for updates; header dropdown switches between running tasks live; task page shows an artifacts strip (final HTML report opens in a new tab, `.md` notes open an in-page preview) |
| `decision_watcher.py` | Tails a Workflow run's `journal.jsonl`; emits orchestrator decision events + per-agent token accounting into `events.jsonl` |
| `orch-config.json` | Optional config: `wf_dir` (workflow transcript dir), `port`, and attempt caps `max_plan_attempts` (default 4) / `max_gate_attempts` (default 3) / `max_e2e_attempts` (default 3) that the decision watcher reads for its retry/exhausted decision narration |
| `.watcher-state.json` | Watcher checkpoint (journal offset, agent classifications, token baselines) — restart-safe, never duplicates events |

## Event schema

One JSON object per line in `events.jsonl`:

```json
{"ts": "<ISO-8601>", "plan": "<plan-id>", "stage": "<stage>", "state": "queued|running|done|failed|info|stale", "detail": "<short text>", "title": "<optional card title>", "tokens": {"in": 0, "out": 0, "cached": 0, "cache_write": 0}, "quiet": true}
```

The decision watcher additionally attaches a per-subagent `agent` sub-object to
its events; it drives the live per-agent rows and the impl→test→critique flow
strip on the plan card:

```json
{"agent": {"id": "<agent-id>", "label": "<short label>", "state": "running|done|failed|stale", "tokens": {"in": 0, "out": 0, "cached": 0, "cache_write": 0}, "started": "<ISO-8601>", "runtime": "<seconds or human string>"}}
```

- `plan` — one card per unique id, first-seen order. **Reserved id `orchestrator`**: rendered as a wide card pinned last; the decision watcher writes there.
- `stage` — free-form; each unique stage gets a colored chip on the card. **Reserved stages**: `plan` (sets the card's badge to `state` — use for plan lifecycle: queued/running/done/failed), `complete` (alias of `plan` for badge purposes — use for the final run-summary event on the `orchestrator` card), `tokens` (updates the card's token counter; no chip).
- `title` — optional on any event; renames the card (or set `ORCH_TITLE` env with `status.sh`).
- `tokens` — **delta** (not absolute) added to the card's counter; grand total is derived by the page. `in` is TOTAL input (fresh + cache writes + cache reads); optional `cached` is the cache-read (`r:`) portion and optional `cache_write` is the cache-write (`w:`) portion, letting the page display the fresh input plus an `r:`/`w:` cache breakdown (an agent loop re-reads its whole prefix every turn, so cache reads dominate `in`). Events without `cached`/`cache_write` render as plain `in`.
- `agent` — optional per-subagent sub-object (watcher-emitted; see the block above) with the agent's `id`/`label`, its own `state` (`running|done|failed|stale`), a nested `tokens` breakdown, and `started`/`runtime`.
- `quiet` — update counters only, no log line (used for ~1s live token ticks).

## Plugging into a new orchestration

1. **Seed cards** (optional but recommended) before launching:
   ```bash
   ORCH_TITLE="Phase 1 — parser" ./status.sh phase1 plan queued "short description"
   ```
2. **Embed the classification marker** in every agent prompt so the decision
   watcher needs no task-specific patterns:
   ```
   [monitor] plan=<plan-id> stage=<stage> role=<role> attempt=<n>
   ```
   (`stage=` is optional; without it the watcher derives the stage from the
   mandated `status.sh ... running` command text in the prompt. If the
   prompt embeds quoted text containing stray markers — e.g. findings JSON
   from an earlier agent — the LAST occurrence wins, since the orchestrator
   script appends its marker at the end.)

   **This marker is the deterministic event source.** The prompt text is
   authored by the orchestrator script at a fixed control-flow point and is
   recorded in the Workflow journal; the watcher raises `running` (on spawn)
   and `done`/`failed`/`info` (on result, derived from the structured-output
   shape: `findings`/`real`/`fixed_ids`/`files_changed`/`passed`/`approved`)
   events onto the
   plan's card, plus plan-badge transitions (first spawn → `running`,
   decisive green result → `done`, a new sequenced plan starting → closes
   the prior one). A decisive close is provisional: if a later agent spawns
   for the same plan (multi-gate loops — e.g. test green while e2e/critique
   still follow), the watcher reopens the card with a
   `plan running "loop continues: ..."` event; the badge that survives with
   no further spawns is the true close. None of this depends on the agent
   LLM doing anything.

   Two robustness rules cover harness gaps. (1) `stale` rows: an agent whose
   journal `started` never gets a `result` (driver killed/restarted) is
   closed as `stale` when the same plan+role respawns — its runtime is its
   last transcript activity; the dashboard also freezes any still-"running"
   rows as `stale` when their plan card closes. (2) Completion time: journal
   entries carry no timestamps and a transcript can stop flushing mid-run
   (a long final Bash call), so when tailing live the watcher stamps results
   with the read moment unless the transcript's last line is fresh (≤30s).

   Roles `impl`, `test`, `critique`, `gate:run`, `gate:fix`, `e2e:run`,
   `e2e:fix` get semantic decision lines (approve/reject/retry/advance);
   any other role gets a generic "finished" line. Token accounting works for
   every role.
3. **Optionally mandate trace calls in agent prompts** for intra-agent
   detail (progress notes between spawn and result — the only thing the
   journal cannot see):
   ```
   FIRST run: bash <abs-path>/status.sh <plan> <stage> running "attempt N: ..."
   LAST  run: bash <abs-path>/status.sh <plan> <stage> done|failed "attempt N: <summary>"
   ```
   These are best-effort color on top of the deterministic stream — an agent
   that skips them costs nothing but its own extra detail lines.

   **When the whole run ends, the driver SHOULD emit a final badge event for
   the orchestrator card** with the run summary and token totals only the
   driver knows:
   ```bash
   ./status.sh orchestrator complete done "wf_<id> complete: <summary>"
   ```
   The watcher also closes the badge on its own (safety net): once every
   tracked agent has a result, every plan's loop is closed, and the journal
   stays quiet for `ORCH_QUIET_SECS` (default 60s), it emits
   `orchestrator complete done|failed` itself — so a driver that loses its
   context mid-run (`/clear`, compaction, killed session) no longer leaves
   the card `running` forever. A premature watcher close (unusually long
   pause between loops) self-heals: the next spawn reopens the badge with
   `complete running`.
4. **Start the daemons** (any order; both resolve paths relative to their own
   location, so run them from anywhere):
   ```bash
   ORCH_STATE_DIR=<task-dir> python3 monitor_server.py &    # port: argv > $ORCH_PORT > config > 8931
   ORCH_STATE_DIR=<task-dir> python3 decision_watcher.py &  # wf_dir: argv > $ORCH_WF_DIR > config > newest wf_* journal
   ```
   The watcher survives `/clear` / `/compact` in the driver session: those
   rotate the session id, which relocates where the harness writes subagent
   transcripts mid-run (`.../<new-session>/subagents/workflows/<wf>/`). The
   journal stays at its launch-time path, and the watcher searches every
   session dir holding the same `<wf>` name for per-agent transcripts,
   deduping token counts by API message id — so classification, live tokens,
   and runtimes keep working across rotations.
   `ORCH_STATE_DIR` selects the task state folder (events.jsonl, config,
   checkpoint); unset, state lands next to the scripts.
   The watcher auto-discovers the most recently active
   `~/.claude/projects/*/*/subagents/workflows/wf_*/journal.jsonl` when not
   configured — set `wf_dir` in `orch-config.json` to pin a specific run.
5. **Open the dashboard**: `http://<host>:8931/`. In a sandbox whose ports the
   host can't reach, run `monitor_server.py` on the host instead — if this
   folder is on a shared mount, it tails the same `events.jsonl` the sandboxed
   agents write.

   One `monitor_server.py` serves every task: it rescans
   `.claude/local-orchestrators/*/` on each request (`/tasks` endpoint), and
   the dashboard's header dropdown switches the live stream between them —
   no restart needed when a new orchestration starts. `ORCH_STATE_DIR` only
   picks which task is selected by default. `decision_watcher.py` still
   watches one task per process — start one per concurrent orchestration.

## Task-page artifacts (final report + agent notes)

The task page shows an **artifacts row** inside the cards grid — after the
plan/loop cards and before the wide Orchestrator card — refreshed every 5s
from `/artifacts?task=<name>`:

- **`.html`/`.htm` files** (e.g. the final illustrated report the driver
  writes when the run completes) render as a highlighted link that opens the
  report in a new tab. Reports are served with `Content-Security-Policy:
  sandbox allow-scripts`, so a self-contained report (inline CSS/JS) renders
  fully but its scripts run in an opaque origin, cut off from the monitor.
- **`.md` files** (gate findings, reviews/critiques, research notes, plans —
  anything agents persist for other agents) render as chips that open an
  in-page preview modal (escape-first mini markdown renderer: headings,
  lists, fenced code, tables, blockquotes; "open raw" links the plain text).

Discovery is convention-free: every non-hidden `.html`/`.md` under the task
folder (depth ≤ 3 of subdirectories, e.g. `findings/`, `reviews/`, `plan/`,
`report/`) is listed, reports first. So the only requirement on drivers and
gate prompts is the existing one — **write handoff notes and the final report
into the task state folder** — and they appear on the dashboard
automatically. `/file` never serves other extensions, hidden entries, or any
path resolving outside the task folder (traversal/symlink safe).

## Behavior notes

- **Full replay on connect**: the page resets and rebuilds on every
  (re)connect — safe against double counting; token-counter pulse animations
  are suppressed during the replay burst and only fire on live changes.
- **Timestamps are true occurrence times** where derivable: the watcher stamps
  spawn events from the agent transcript's first line and results from its
  last line; live events lag ≤1s (poll interval). Rendered in viewer-local time.
- **Token math**: per-agent usage is summed from transcript `message.usage`,
  deduplicated by API message id; input includes cache creation + cache reads.
  All emitted `tokens` values are deltas against the watcher's checkpoint, so
  restarts and live ticks never double-count.
- **Completed tasks are history — never delete them.** When a run finishes
  (all plans done), leave the task folder and its `events.jsonl` in place.
  The monitor lists every surviving folder in `/tasks` and replays the full
  event history on connect, so a freshly started `monitor_server.py` shows
  old runs' complete progress — that only works if the events survive.
- **Resetting a run** (wipe-and-rerun of ONE task you are still iterating on
  — never post-completion cleanup): stop the daemons, delete that task's
  `events.jsonl` and `.watcher-state.json`, re-seed, restart. The watcher
  will re-backfill the whole journal with true timestamps.
- **Do not `pkill -f` these scripts from a command line that also spells the
  script name** — bracket the first letter (`pkill -f "[m]onitor_server"`).
- **State-dir authority — always set `ORCH_STATE_DIR`.** The shared module
  directory (`.claude/shared/monitoring/`) is code-only and is NOT an
  authoritative state dir. `status.sh` falls back to it (and warns on stderr)
  when `ORCH_STATE_DIR` is unset, so a forgotten variable dribbles a stray
  `events.jsonl` into the module dir; the daemons no longer short-circuit onto
  that file, and `.claude/shared/monitoring/events.jsonl` /
  `.watcher-state.json` are gitignored — but every `status.sh` call should still
  set `ORCH_STATE_DIR` to the task folder so events land where the daemons watch.

## Layout convention

```
.claude/
├── shared/monitoring/                        # this module — stateless, same for every task
├── skills/m-orchestrator/SKILL.md   # monitoring integration skill
├── skills/execute-research/         # research -> feature-sub-plan plan (read-only)
├── skills/implement-plan/             # plan -> implement->test->critique loops
└── local-orchestrators/<task-name>/   # per task: workflow scripts + monitoring state
```
