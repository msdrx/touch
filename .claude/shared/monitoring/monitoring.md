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
| `monitor.html` | Live dashboard — one card per plan, event-driven, no rebuild for updates; header dropdown switches between running tasks live; a header zoom selector (100% default, up to 200%, persisted like the refresh rate) scales the whole task/stats view uniformly via CSS `zoom` (real layout reflow — cards, dials and log text all grow in the same proportion; home never zooms); task page shows a session timeplan strip with time dials (wall-clock bar of agents-working / idle / stall segments derived from event-stream gaps, a 24-hour clock ruler under it, horizontal sliding for multi-day sessions; hovering it draws a time-point cursor line and previews the agents mid-run at that instant) and an artifacts card (pins the final HTML report + the plan document with size/age; every other file sits behind an "all files" popup grouped plan → reports → notes, `.md` opens an in-page preview); a statistics page (`?task=<name>&view=stats`, linked from the timeplan card) shows large-figure run stats — an entire-flow status tile (running/done/failed with a matching state dot, same fold as the home-grid tile: any running plan or agent wins — a failed loop that was resumed never overrides running loops — while idle a failed plan is the verdict, e.g. a loop awaiting the user after its last attempt failed, and all-green folds to done; while running the sub-line reads as progress — settled/all plans, where settled counts green and red both and "all" is `max(cards seen, declared plans_total)` so unstarted plans count — and the green/red breakdown appears only once the run settled), tokens/cache-hit/burn-rate, plans green/red, agents, attempts and retries, agent durations, dead air and stalls |
| `decision_watcher.py` | Tails a Workflow run's `journal.jsonl`; emits orchestrator decision events + per-agent token accounting into `events.jsonl` |
| `orch-config.json` | Optional config: `wf_dir` (workflow transcript dir), `port`, attempt caps `max_plan_attempts` (default 4) / `max_gate_attempts` (default 3) / `max_e2e_attempts` (default 3) / `max_finalgate_attempts` (default 2) that the decision watcher quotes in its retry/exhausted narration, and `strategy` (`parallel` \| `sequential` \| legacy `serial`, which is the only value that re-enables the retired sequenced plan-close heuristic — **no reference template publishes it**, so that branch is reachable only from a hand-written legacy config; do not "fix" a template to emit `serial`, it resurrects the fabricated `plan failed` badge). The watcher **re-reads this file whenever its mtime moves**, so an orchestrator script that publishes its own caps after the daemons started is still honored (it logs `config reloaded: …`). The **first `orch-config.json` that exists** wins outright — task dir, then the module dir — and if it does not parse the watcher keeps the documented defaults, warns on stderr, and re-reads that same file when it is repaired |
| `.watcher-state.json` | Watcher checkpoint (journal offset, agent classifications, token baselines) — restart-safe, never duplicates events |

## Event schema

One JSON object per line in `events.jsonl`:

```json
{"ts": "<ISO-8601>", "plan": "<plan-id>", "stage": "<stage>", "state": "queued|running|done|failed|info|stale", "detail": "<short text>", "w": "agent|watcher", "title": "<optional card title>", "plans_total": 0, "tokens": {"in": 0, "out": 0, "cached": 0, "cache_write": 0}, "quiet": true}
```

The decision watcher additionally attaches a per-subagent `agent` sub-object to
its events; it drives the live per-agent rows and the impl→test→critique flow
strip on the plan card:

```json
{"agent": {"id": "<full 17-hex agentId>", "shortId": "<first 8 hex>", "label": "<short label>", "state": "running|done|failed|stale", "tokens": {"in": 0, "out": 0, "cached": 0, "cache_write": 0}, "started": "<ISO-8601>", "runtime": "<seconds or human string>", "identity": {"name": "", "parent": "", "root": "", "ledger": ""}, "flags": ["marker-misplaced"], "unconventional": true}}
```

- `plan` — one card per unique id, first-seen order. **Reserved id `orchestrator`**: rendered as a wide card pinned last; the decision watcher writes there.
- `stage` — free-form; each unique stage gets a colored chip on the card. **Reserved stages**: `plan` (sets the card's badge to `state` — use for plan lifecycle: queued/running/done/failed), `complete` (alias of `plan` for badge purposes — use for the final run-summary event on the `orchestrator` card), `tokens` (updates the card's token counter; no chip).
- `title` — optional on any event; renames the card (or set `ORCH_TITLE` env with `status.sh`).
- `plans_total` — optional on any event (integer; set `ORCH_PLANS_TOTAL` env with `status.sh`); declares the run's expected TOTAL number of plan cards so progress renders over ALL plans, not only the cards already seen in the stream. Readers fold it as a **monotonic max, floored by cards actually seen** — a resume re-declaring the same number is idempotent, a stray smaller value never shrinks the denominator, and one phase's declaration never hides cards another phase appended to the same stream. The reference templates declare it at their partition/barrier close: implement-plan emits `N sub-plans + 2` (divide + finalgate cards), execute-research emits `2` (research + synthesis cards). A stream with several phases therefore converges upward as later cards appear.
- `tokens` — **delta** (not absolute) added to the card's counter; grand total is derived by the page. `in` is TOTAL input (fresh + cache writes + cache reads); optional `cached` is the cache-read (`r:`) portion and optional `cache_write` is the cache-write (`w:`) portion, letting the page display the fresh input plus an `r:`/`w:` cache breakdown (an agent loop re-reads its whole prefix every turn, so cache reads dominate `in`). Events without `cached`/`cache_write` render as plain `in`.
- `agent` — optional per-subagent sub-object (watcher-emitted; see the block above) with the agent's `id`/`label`, its own `state` (`running|done|failed|stale`), a nested `tokens` breakdown, and `started`/`runtime`. Four further keys are optional: `shortId` (the first 8 hex of `id`, **display only**), `identity` (the `[touch]` marker's `name`/`parent`/`root`/`ledger`, labels only), `flags` (e.g. `["marker-misplaced"]` when a real `[touch]` marker sits below the marker window), and `unconventional: true` for an agent whose prompt carried no usable marker — the node still exists, it just has no plan/stage label.
  **Identity is `id`, and `id` is the FULL 17-hex agentId.** Readers key rows on `id` and never on `shortId`: an 8-hex prefix is not unique. Lines written before this widening carry an 8-hex `id`; they are legacy (`legacy:<task>:<id8>` in the aggregator's namespace) — a stream containing BOTH widths for one agent (a task whose watcher was restarted onto the new code mid-run) therefore renders two rows in `monitor.html`, which keys on `id` alone. That is a known carried consequence for the frontend/legacy work (join the 8-hex form via `shortId`), not something the watcher hides by emitting short ids.
- `quiet` — update counters only, no log line (used for ~1s live token ticks).
- `w` — **writer attribution**: `"agent"` for a line appended by `status.sh` (an agent or a driver script), `"watcher"` for a line appended by `decision_watcher.py`. `events.jsonl` is a multi-writer file and attribution used to be guessable only from an event's shape, so a reader could not tell an asserted line from a derived one. The key is purely **additive**: the five-key core shape is unchanged, every reader must ignore keys it does not know, and streams written before this key exist stay readable (their lines simply have no `w` — treat that as "unknown writer", never as a default).
- `detail` — capped at **1 KB at the writer** (both writers truncate; the cut ends with `...`). The cap exists because these strings get embedded in shell commands and JS template literals downstream, not because of JSON. Keep details single-line and free of double quotes.

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
   decisive green result → `done`, and — for legacy `strategy:"serial"` runs
   only — a new plan starting closes the prior one with a
   `serial advance -> <next>` detail). That sequenced close is **retired for
   new runs**: applied to a parallel fan-out it invented a `plan failed` badge
   while every agent had succeeded, so plans now close on the script's terminal
   `plan done` plus the settle pass, and a plan whose agents all resulted with
   no decisive verdict closes `done "… (closed, no verdict)"`, never `failed`.
   The settle pass ADOPTS the closes already in the stream (same
   last-event-wins fold, scoped to this watcher session) instead of writing a
   second one, so a card the script closed with a verified verdict is never
   followed by a contradictory `(closed, no verdict)` line for the same plan.
   A decisive close is provisional: if a later agent spawns
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
   `complete running`. The same heal covers phase continuations: one task
   folder hosts several phases (research, then implement-plan) appending to
   one `events.jsonl`, so at startup a watcher whose stream already ENDS on
   an earlier phase's `complete done|failed` badge arms the reopen — the
   first spawn it sees emits `complete running` so no replaying dashboard
   shows a closed run while this phase's loops are running. The dashboard is
   also defensive on its own (FRONTEND-6): a `running`-state event on the
   reserved `orchestrator` card reopens a done/failed badge during replay of
   histories written before this heal existed.

   **When the run ends, stop its watcher** — and the driver's terminal event
   above is what does it. The watcher stops itself when (a) an
   `orchestrator complete done|failed` line written by a script/agent
   (`"w":"agent"`) lands *after* the watcher started, (b) the journal has
   then been quiet for `ORCH_EXIT_QUIET_SECS` (default 120s), and (c) nothing
   is left that could still resolve. Only evidence that the run is LIVE AGAIN
   cancels (a) — a later `complete running`, or a plan card MOVING (`queued`
   /`running`). A plan card *closing* does not: the watcher's own settle pass
   runs at `ORCH_QUIET_SECS` (60s) — i.e. always *before* the exit window —
   and emits exactly those `plan done` closes plus its own `complete done`,
   so counting them as liveness cancelled the driver's close in the normal
   flow and left every run to time out on the abandoned window instead. Its
   OWN inferred close never stops it:
   a harness stall between agents looks exactly like a finished run, and a
   wrong badge self-heals on the next spawn while a wrong exit ends
   monitoring for good. The one exception is an **abandoned** run (the
   session was killed, so no driver close will ever come): after
   `ORCH_ABANDON_QUIET_SECS` of journal silence (default 10 × the exit
   window) the watcher closes any never-resulted agents as `stale` and then
   stops, so a finished-but-unclosed run cannot orphan its watcher. The two
   windows are independent: whichever you set shorter is the first the
   watcher can act on. `ORCH_NO_SELF_EXIT=1` disables both routes.

   **Signalling the watcher does not lose events.** `SIGTERM`/`SIGINT` arm a
   DRAIN, they do not stop the process where it stands: the watcher makes at
   least one more tail+emit pass, keeps polling for `ORCH_DRAIN_SECS`
   (default 3s), checkpoints, and only then exits (a second signal exits at
   once). This is what makes the templates' `closeRun` epilogue safe — it
   signals ~0.2s after the harness appended the final agent's `result`, and
   the watcher polls once a second, so an unhandled signal used to drop that
   agent's `done` chip, its decision line and its whole token usage
   (deltas are wire-only) from the permanent record.
4. **Start the daemons** (any order; both resolve paths relative to their own
   location, so run them from anywhere):
   ```bash
   ORCH_STATE_DIR=<task-dir> python3 monitor_server.py &    # port: argv > $ORCH_PORT > config > 8931
   ORCH_STATE_DIR=<task-dir> python3 decision_watcher.py &  # wf_dir: argv > $ORCH_WF_DIR > config > newest wf_* journal
   echo $! > "<task-dir>/watcher.pid"                       # record the pid: the workflow epilogue signals it
   ```
   Recording the watcher pid is what makes the templates' `closeRun` epilogue
   able to stop it promptly (it verifies `/proc/<pid>/cmdline` is really a
   `decision_watcher` before signalling, so a stale pid file can never become
   a wrong-target kill). Without the pid line nothing breaks — the watcher's
   own self-exit still applies. `*.pid` is gitignored. Never record or signal
   a pid for `monitor_server.py`: ONE server serves every task, so stopping
   it per task would blank the dashboard for all the others.
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

The task page shows an **artifacts card** inside the cards grid — after the
wide Orchestrator card and before the plan/loop cards — refreshed every 5s
from `/artifacts?task=<name>`. The card itself stays lean — it pins at most
**two rows** (name, folder, size, age each): **the final HTML report** (an
`.html` whose basename mentions `report`/`final`, else the newest `.html`)
and **the plan document** (the newest `plan/*-plan.md`, else the newest `.md`
under `plan/`). An "all files" button opens a popup over the page listing
every artifact, grouped **plan files** → **html reports** → **notes**, so
secondary reports, `RESUME.md` and per-attempt findings never crowd the card
but stay one click away:

- **`.html`/`.htm` files** (e.g. the final illustrated report the driver
  writes when the run completes) open in a new tab. Reports are served with
  `Content-Security-Policy: sandbox allow-scripts`, so a self-contained
  report (inline CSS/JS) renders fully but its scripts run in an opaque
  origin, cut off from the monitor.
- **`.md` files** (plans, gate findings, reviews/critiques, research notes —
  anything agents persist for other agents) open an in-page preview modal
  (escape-first mini markdown renderer: headings, lists, fenced code, tables,
  blockquotes; "open raw" links the plain text). The preview stacks above the
  all-files popup, so closing it (Esc/✕) drops back to the list.

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
- **Session timeplan** (task page): one wall-clock strip from the task's first
  event to its last (to "now" while any plan is open). Gaps in the stream are
  classified by run state at the moment they begin: no plan open and >2 min →
  idle (between runs, hatched); a plan open and >4 min → stall (red); anything
  shorter renders as solid working time. Token ticks land every few seconds
  while an agent works, so 1.5–3 min gaps are one long model generation, not
  an outage. Plans open on a `plan` stage event with state `running`/`queued`,
  close on `done`/`failed`, and a run-level `complete` closes all — the
  reserved `orchestrator` plan id never opens one. Under the strip runs a
  measuring ruler: graduations sit on round wall-clock instants at a round
  step chosen so at most 12 labeled major lines fit the visible viewport,
  each labeled in 24-hour local time (`13:00, 14:00, …`; seconds when the
  step is sub-minute; a local midnight shows its date and wears a stronger
  day line) and extending as a faint gridline through the bar, with unlabeled
  minor ticks at fifths of the step between them. The visible view range
  defaults to at most 1 day: a longer session widens the track to one
  viewport width per day and slides horizontally (a view scrolled to the
  right edge stays pinned to "now"; edge labels gain date prefixes).
  The range is directly resizable — ctrl/⌘+scroll (trackpad pinch) scales it
  continuously (4h, 1d, 2d, …) anchored at the pointer, double-click resets
  to the default; no dropdown. Hovering the strip draws a
  time-point cursor line with the preview popover centered under it, showing
  that instant: the clock time, the strip
  state with its segment span, and every agent whose run covered that moment
  (final state dot + `plan · role #attempt` + how long it had been running by
  then); a plan known only from open/close events previews as `plan · run`.
  Beside the strip sit three
  time dials: **elapsed** (chronograph hand, one revolution per hour, true
  duration underneath), **silence** (stopwatch — time since the last event
  sweeping toward the 4 min stall limit; green ≤3 min, amber to 4 min, red
  past it, always paired with an ok/slow/stall word; rests "off" when no run
  is open), and **working** (share of session wall-clock spent in working
  segments).
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
