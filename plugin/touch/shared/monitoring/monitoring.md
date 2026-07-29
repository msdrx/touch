# Orchestrator monitoring module

Reusable, task-agnostic live monitoring for deterministic orchestrator runs
(Workflow-tool scripts, or any driver that spawns agents). This module ships
inside the Touch plugin at `${CLAUDE_PLUGIN_ROOT}/shared/monitoring/` and is
stateless; nothing about it is per-task and nothing about it lives in your
project. The only project-side path is
`.claude/local-orchestrators/<task-name>/`, where each task keeps its
orchestration scripts and monitoring state (events, config, checkpoints),
selected via the `ORCH_STATE_DIR` env var. See the `m-orchestrator` skill for
the integration checklist.

Zero third-party dependencies: bash + Python 3 stdlib + a browser.

## Files

| File | Role |
|------|------|
| `status.sh` | Deterministic trace point — agents/orchestrators append status events |
| `events.jsonl` | Append-only event stream (single source of truth; created on first event) |
| `monitor_server.py` | HTTP + WebSocket server: serves `monitor.html` at `/`, streams `events.jsonl` at `/ws` (full replay on connect, then live tail; `?task=<name>` selects a task; `&v=2` opts into the snapshot-prelude protocol — see "Wire framing" below), `/tasks` lists every discovered task folder, `/artifacts?task=<name>` lists the task's `.html`/`.md` artifacts, `/file?task=<name>&path=<rel>` serves one (extension-whitelisted, realpath-contained), `/health` probe |
| `monitor.html` | Live dashboard — one card per plan, event-driven, no rebuild for updates; header dropdown switches between running tasks live; a header zoom selector (100% default, up to 200%, persisted like the refresh rate) scales the whole task/stats view uniformly via CSS `zoom` (real layout reflow — cards, dials and log text all grow in the same proportion; home never zooms); task page shows a session timeplan strip with time dials (wall-clock bar of agents-working / idle / stall segments derived from event-stream gaps, a 24-hour clock ruler under it, horizontal sliding for multi-day sessions; hovering it draws a time-point cursor line and previews the agents mid-run at that instant) and an artifacts card (pins the final HTML report + the plan document with size/age; every other file sits behind an "all files" popup grouped plan → reports → notes, `.md` opens an in-page preview; each loop card's header carries a "files" pill opening the same popup filtered to that loop's output); the Orchestrator card's fold arrow reveals, above the decision log, a sub-plans bullet list (state dot + name + state, click jumps to that loop's card); a statistics page (`?task=<name>&view=stats`, linked from the timeplan card) shows large-figure run stats — an entire-flow status tile (running/done/failed with a matching state dot, same fold as the home-grid tile: any running plan or agent wins — a failed loop that was resumed never overrides running loops — while idle a failed plan is the verdict, e.g. a loop awaiting the user after its last attempt failed, and all-green folds to done; while running the sub-line reads as progress — settled/all plans, where settled counts green and red both and "all" is `max(cards seen, declared plans_total)` so unstarted plans count — and the green/red breakdown appears only once the run settled), tokens/cache-hit/burn-rate, plans green/red, agents, attempts and retries, agent durations, dead air and stalls |
| `decision_watcher.py` | Tails a Workflow run's `journal.jsonl`; emits orchestrator decision events + per-agent token accounting into `events.jsonl` |
| `orch-config.json` | Optional config: `wf_dir` (workflow transcript dir), `port`, attempt caps `max_plan_attempts` (default 4) / `max_gate_attempts` (default 3) / `max_e2e_attempts` (default 3) / `max_finalgate_attempts` (default 2) that the decision watcher quotes in its retry/exhausted narration, the live token-tick cadence cap `token_tick_secs` (seconds; **default 15**, `0` = no ceiling, i.e. the pre-cadence behavior: a delta line on every poll tick that has a non-zero delta — the env var `ORCH_TOKEN_TICK_SECS` pins the value and WINS over this key, so an operator debugging a live run is never overridden by a config the orchestrator script republishes; a negative value reads as `0`), and `strategy` (`parallel` \| `sequential` \| legacy `serial`, which is the only value that re-enables the retired sequenced plan-close heuristic — **no reference template publishes it**, so that branch is reachable only from a hand-written legacy config; do not "fix" a template to emit `serial`, it resurrects the fabricated `plan failed` badge). The watcher **re-reads this file whenever its mtime moves**, so an orchestrator script that publishes its own caps after the daemons started is still honored (it logs `config reloaded: …`). The **first `orch-config.json` that exists** wins outright — task dir, then the module dir — and if it does not parse the watcher keeps the documented defaults, warns on stderr, and re-reads that same file when it is repaired |
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
- `roster` — optional on an `orchestrator`-card event (array of `"<id> — <title>"` strings; **latest wins**, driver-emitted, typically once after a partition lands): declares the run's full planned sub-plan list by name. The dashboard's Orchestrator accordion shows roster entries whose loop has **no card yet** as neutral `planned` bullets — display only, never materialized as cards, so replay/live counts are untouched. Untrusted like every event key: readers bound the count and entry length and treat entries as text.
- `tokens` — **delta** (not absolute) added to the card's counter; grand total is derived by the page. `in` is TOTAL input (fresh + cache writes + cache reads); optional `cached` is the cache-read (`r:`) portion and optional `cache_write` is the cache-write (`w:`) portion, letting the page display the fresh input plus an `r:`/`w:` cache breakdown (an agent loop re-reads its whole prefix every turn, so cache reads dominate `in`). Events without `cached`/`cache_write` render as plain `in`. The per-agent `agent.tokens` block is the **opposite** kind of number — see the `agent` bullet.
- `agent` — optional per-subagent sub-object (watcher-emitted; see the block above) with the agent's `id`/`label`, its own `state` (`running|done|failed|stale`), a nested `tokens` breakdown, and `started`/`runtime`.
  **`agent.tokens` is that agent's ABSOLUTE running total, last-event-wins** — unlike the top-level `tokens`, which is a delta. The asymmetry is deliberate and load-bearing, so readers must not treat the two alike: summing `agent.tokens` inflates a per-agent figure by the number of ticks it was reported on, while last-winsing the top-level `tokens` collapses a plan's total to its final delta. It is also what makes a lossy replay recoverable — a fold that drops quiet ticks (any snapshot/prelude design) rebuilds totals from `agent.tokens` last-wins per `(plan, agent.id)` on ANY event (terminal events carry a higher cumulative than the last tick did), excluding agent-less deltas, and NEVER by summing the surviving deltas. Deltas are wire-only: sum them over a gappy stream and the answer is measurably wrong. Four further keys are optional: `shortId` (the first 8 hex of `id`, **display only**), `identity` (the `[touch]` marker's `name`/`parent`/`root`/`ledger`, labels only), `flags` (e.g. `["marker-misplaced"]` when a real `[touch]` marker sits below the marker window), and `unconventional: true` for an agent whose prompt carried no usable marker — the node still exists, it just has no plan/stage label.
  **Identity is `id`, and `id` is the FULL 17-hex agentId.** Readers key rows on `id` and never on `shortId`: an 8-hex prefix is not unique. Lines written before this widening carry an 8-hex `id`; they are legacy (`legacy:<task>:<id8>` in the aggregator's namespace) — a stream containing BOTH widths for one agent (a task whose watcher was restarted onto the new code mid-run) therefore renders two rows in `monitor.html`, which keys on `id` alone. That is a known carried consequence for the frontend/legacy work (join the 8-hex form via `shortId`), not something the watcher hides by emitting short ids.
- `quiet` — update counters only, no log line (live per-agent token ticks). The watcher *polls* every second but only *emits* when the agent's transcript actually grew, so the real cadence is **one line per transcript flush per agent** (measured p50 ~5 s, p90 ~22 s — model turn boundaries, not the clock), capped further to **at most one tick per agent per `ORCH_TOKEN_TICK_SECS` / `token_tick_secs` seconds (default 15)**. The cap is a **ceiling, never a floor**: no heartbeat is ever written, because a fixed-interval line would erase exactly the stream gaps the timeplan reads as stalls — liveness belongs to the WebSocket keepalive, never to `events.jsonl`. Coalescing is lossless: the delta baseline advances only when a line is really written, so a suppressed tick's tokens ride along on the next one, the first tick for a new agent is always *due* (no ceiling wait — though, like every tick, it writes nothing until the delta is non-zero), and every terminal path (result rollup, stale/abandoned close, shutdown drain, both self-exits) force-flushes **unthrottled** with the agent's cumulative total attached.
- `w` — **writer attribution**: `"agent"` for a line appended by `status.sh` (an agent or a driver script), `"watcher"` for a line appended by `decision_watcher.py`. `events.jsonl` is a multi-writer file and attribution used to be guessable only from an event's shape, so a reader could not tell an asserted line from a derived one. The key is purely **additive**: the five-key core shape is unchanged, every reader must ignore keys it does not know, and streams written before this key exist stay readable (their lines simply have no `w` — treat that as "unknown writer", never as a default).
- `detail` — capped at **1 KB at the writer** (both writers truncate; the cut ends with `...`). The cap exists because these strings get embedded in shell commands and JS template literals downstream, not because of JSON. Keep details single-line and free of double quotes.

## Wire framing (`/ws` protocol v1 and v2)

The **file** schema above is unchanged and normative: `events.jsonl` stays one
JSON object per line, append-only, and every reader ignores keys it does not
know. Framing is a property of the `/ws` transport only — nothing here changes
a byte on disk, and additive event keys remain legal.

**Reserved control key `m`.** An object carrying a top-level `m` is a *control
frame*, never an event. Events never carry `m` — corpus-verified twice over:
against the 12,334-event `touch-mongo-live` stream measured while this protocol
was designed, and against the frozen legacy corpora held in the development
repository — and no writer may ever add it. Neither corpus ships: the payload
carries no tests and no fixtures, so those are maintainer evidence, not
something to look for in an installed copy (in the development repository they
are `tests/fixtures/legacy/*.jsonl` and, for this module's fold gold files,
`tests/monitoring/fixtures/` — development-repository-only paths, both).
That is why the control envelope needed a reserved NAME rather than an
inference like "objects with a `type` field": additive event keys must stay
legal, so exactly one name is spent instead.

**v1 — anything but `v=2` in the query (`/ws?task=<name>`)** — the compatibility
floor, byte-identical to what the server has always sent: one text frame per
event line, file order, full replay from byte 0, no control frames, ~20 s
keepalive ping; truncation or rotation closes the socket (there is no sentinel
*frame* on the wire — the `-1` sentinel is server-internal, and the client
simply reconnects and rebuilds). An old page against a new server gets exactly
this, and so does a client that pins `v=1` — only the exact string `v=2`
selects v2. The one timing that is NOT frozen is the tail poll:
both v1 and v2 sockets poll at 0.5 s while the stream is moving and back off to
2 s after ~60 s of quiet (the first append resets it). Backoff is invisible to
the wire format — it only changes when bytes are noticed, never what is sent.

**v2 — `/ws?task=<name>&v=2[&snap=0|1|verify][&from=<byte-offset>&sig=<16hex>]`.**
The version is **server-declared, never sniffed**: the client asks, and only a
`hello` as the VERY FIRST frame licenses v2 handling; anything else means legacy
handling for the life of that socket (one decision per connection, no per-frame
guessing). `snap` defaults to `1`; an unrecognised `snap` value falls back to
`1` and is listed in the hello's `ignored`. Frame sequence:

1. `{"m":"hello","v":2,"task":…,"sig":…,"foldGen":N,"fromApplied":<bool>,"reason":…,"snap":…,"ignored":[…]}`
   — after the 101 there is no status code left to refuse with, so every
   parameter the server could not honour is NAMED here. Under v2 an unknown
   `?task=` is refused outright — a hello carrying `"error":"unknown-task"`
   (the full frame is
   `{"m":"hello","v":2,"error":"unknown-task","task":…,"foldGen":N}`) followed
   by a close — never answered with the default task's stream the way v1 falls
   back.
2. One `{"m":"snapshot","kind":"monitor-snapshot",…}` frame: the server's fold
   of everything up to the cursor (per-plan title/state/timestamps, absolute
   tokens, stages, agent rows, roles, a budgeted log with its `logTotal`, and
   the derived timeplan), **every ordered map an array of pairs** so a
   numeric-looking plan id cannot reorder the cards. With `snap=0` the server
   sends raw history instead — **array frames** of event objects capped at 500
   events and 256 KB, whichever binds first; `snap=verify` sends both so the
   page can shadow-replay and compare. An accepted resume sends no snapshot; if
   the client's cursor is behind the server's offset, the gap travels first as
   ordinary array frames under the same batch caps — usually empty, which is
   the whole point of a cursor.
3. ONE `{"m":"tail","cursor":{"sig":…,"offset":N},"n":…,"truncated":<bool>}`
   boundary frame — the explicit replay→tail edge (it replaces the page's old
   600 ms timer guess); `truncated` discloses that the log budget cut lines.
4. The live tail: **array frames always** (a one-event tick is a one-element
   array — one client code path), at most 5,000 events per tick (the 0.5 s live
   tick; see the backoff above) with the remainder carried into the next tick,
   plus a `{"m":"cursor",…}` frame after every tick that **consumed** events —
   a tick whose only lines were unparseable still advances the cursor, with
   `n: 0` — so a long-lived tab can reconnect from where it actually is instead
   of the boundary's stale offset.

Client dispatch rule: an **array** frame is events; an **object carrying `m`**
is control and never reaches the event path; a bare object is a legacy (v1)
event. The control catalogue is exactly four shapes — `hello`, `snapshot`,
`tail`, `cursor` — as of this generation; a reader must **ignore** any other
`m` value rather than treat it as an event, which is what keeps the `m` space
forward-compatible.

**Cursor and resume.** The cursor is `(sig, offset)`: `sig` is the first 16 hex
characters of the sha256 of the stream's first 4 KB (a stream shorter than 4 KB
hashes whole, and the server tracks that as `sig_short` — such a digest is not
yet an identity, because the next append changes it), and `offset` is a
**byte** offset — never a line number. A `&from=&sig=` resume is accepted only
when the sig matches, the offset is not ahead of the server's, and the offset
sits on a line boundary; any failure is reported as
`"fromApplied": false` with a `"reason"` (`sig-mismatch`, `mid-line`,
`offset-ahead`, `bad-offset`, `no-sig`, `no-stream`) and answered with the full
prelude the mode calls for — a snapshot by default, raw history under `snap=0`,
both under `snap=verify` — never with a silent replay layered on top of
already-hydrated state.

**`foldGen`.** The server's fold and the page's fold are ONE specification
living in two files, bound by a `FOLD_GEN` integer literal present verbatim in
both and stamped on every `hello`/`snapshot`. Bump it in both files whenever a
fold rule changes: a page that sees a generation it does not recognise discards
the snapshot and reconnects with `?snap=0` instead of rendering state built by
other rules.

The agent-facing trace protocol below (`status.sh` calls embedded in prompts)
is **unchanged** by any of this — framing is a reader-side concern, and both
writers append lines exactly as they always did.

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
   checkpoint); unset, both daemons fall back to the newest task folder under
   the resolved tasks root and exit 1 if there is none — nothing is ever
   written next to the scripts (see "State-dir authority" below).
   `monitor_server.py` binds **127.0.0.1** and mints a **per-boot token**,
   written 0600 to `<task-dir>/monitor.json`. `/tasks`, `/artifacts`, `/file`
   and the `/ws` upgrade require it (`?token=`, `X-Orch-Token:` or
   `Authorization: Bearer`); `/health` and the page itself stay open, and
   `/health` hashes every filesystem path it reports (`sha256[:12]`) because it
   is the one route with no token in front of it. The startup line prints the
   full `…/?token=…` URL **only on a TTY** (or when the token file could not be
   written): drivers redirect that stdout into a 0644 `daemon.log`, which would
   undo the 0600 — into a log it prints the bare URL plus a token fingerprint.
   `--open` (or `ORCH_BIND=<addr>`) is the explicit opt-in to a non-loopback
   bind — no skill and no wrapper passes it for you. `--allow-origin <O>` /
   `ORCH_ALLOW_ORIGIN` extends the WS upgrade's **Origin** allowlist and
   `--allow-host <N>` / `ORCH_ALLOW_HOST` its **Host** allowlist; both accept
   the `--flag V` and `--flag=V` spellings and a comma-separated env var, and an
   explicitly allow-listed Origin also satisfies the Host gate (the two travel
   together, so anything else would make the flag unreachable). Report HTML
   served from `/file` is sent `Content-Security-Policy: sandbox` **without**
   `allow-scripts` plus `Referrer-Policy: no-referrer`, so a script in an
   agent-generated report cannot read the token out of its own URL.
   The watcher auto-discovers the most recently active
   `~/.claude/projects/*/*/subagents/workflows/wf_*/journal.jsonl` when not
   configured — set `wf_dir` in `orch-config.json` to pin a specific run.
5. **Open the dashboard**: the tokened URL
   (`http://127.0.0.1:8931/?token=…`) — printed at startup on a TTY, and always
   available in `<task-dir>/monitor.json`. A tokenless URL loads the page and
   then fails *loudly*: `/tasks` 401s, the `/ws` upgrade is refused, and the
   page shows a banner naming the token instead of retrying forever.
   To reach it from OUTSIDE the sandbox you must do both halves — start it with
   `--open` (the loopback default does not listen on `eth0`, so publishing the
   port alone gets you a connection refused) **and** publish the port. `--open`
   is never passed for you (GD-T8). Alternatively, run `monitor_server.py` on
   the host — if this folder is on a shared mount, it tails the same
   `events.jsonl` the sandboxed agents write.

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
but stay one click away. Each plan/loop card's header additionally carries a
**"files" pill** (same UI, same popup) filtered to the files that loop
produced — attribution is by basename prefix (`<plan>-…`/`<plan>.md`, the
findings handoff naming convention; the longest matching plan id wins), and
the pill stays hidden while no file matches:

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

- **Full state on connect**: the page resets and rebuilds on every (re)connect —
  safe against double counting; token-counter pulse animations are suppressed
  during the replay burst and only fire on live changes. Under v2 the rebuild is
  a *hydration* from the server's folded snapshot rather than a replay of every
  line (same end state, one frame instead of tens of thousands), and a reconnect
  that carries a valid cursor skips the **snapshot** — it still gets the
  `hello`, at most the handful of events appended since its cursor, and the
  boundary frame, then resumes the tail. `?snap=0` forces the old full-replay
  path whenever an operator wants the raw stream.
- **Session timeplan** (task page): one wall-clock strip from the task's first
  event to its last (to "now" while any plan is open). Gaps in the stream are
  classified by run state at the moment they begin: no plan open and >2 min →
  idle (between runs, hatched); a plan open and >4 min → stall (red); anything
  shorter renders as solid working time. Token ticks land at most once per
  agent per `token_tick_secs` (default 15 s) while an agent works — see
  **Token math** below for why the useful range of that knob stays well inside
  these thresholds — so gaps of a couple of minutes are one long model
  generation plus the cadence ceiling, not an outage. Plans open on a `plan`
  stage event with state `running`/`queued`,
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
  last line. Every other live event carries the moment the watcher OBSERVED
  it — journal-derived lines within one 1 s journal poll, and a token tick up
  to `token_tick_secs` (default 15 s) after the transcript growth it reports,
  because the cadence ceiling gates the transcript READ and not merely the
  emit (see **Token math** below). Delivery to the page adds the socket poll
  on top (0.5 s while the stream is moving, 2 s once it has backed off) —
  that is a different process from the watcher and cannot move a stamp already
  written to disk. Rendered in viewer-local time.
- **Token math**: per-agent usage is summed from transcript `message.usage`,
  deduplicated by API message id; input includes cache creation + cache reads.
  All emitted `tokens` values are deltas against the watcher's checkpoint, so
  restarts and live ticks never double-count; the `agent.tokens` block beside
  them states the same agent's absolute total (see the `agent` bullet above).
  Live ticks are throttled per agent (`token_tick_secs`, default 15 s). The
  useful knob range starts around 10–15 s because the emit trigger is the
  transcript flush (p50 ~5 s), not the clock — a 5 s cap barely helps — and it
  ends at 30 s: the cap must stay well inside the timeplan's 2 min idle
  threshold, or thinning the ticks starts inventing gaps in the strip. `0`
  removes the ceiling entirely — the pre-cadence behavior, a line on every poll
  tick that has a non-zero delta (a tick with no transcript growth still writes
  nothing).
- **Completed tasks are history — never delete them.** When a run finishes
  (all plans done), leave the task folder and its `events.jsonl` in place.
  The monitor lists every surviving folder in `/tasks` and hydrates (or, on a
  v1 socket or under `?snap=0`, replays) the full event history on connect, so
  a freshly started `monitor_server.py` shows old runs' complete progress —
  that only works if the events survive.
- **Resetting a run** (wipe-and-rerun of ONE task you are still iterating on
  — never post-completion cleanup): stop the daemons, delete that task's
  `events.jsonl` and `.watcher-state.json`, re-seed, restart. The watcher
  will re-backfill the whole journal with true timestamps.
  A wipe changes the stream's identity, and that is deliberate: the resume
  `sig` is a hash of the file's first 4 KB, so a dashboard still holding a
  cursor from the old stream is **refused** (`"fromApplied": false`,
  `"reason": "sig-mismatch"`) and rebuilt from a fresh prelude instead of
  tailing the new file at an offset that meant something else. Content
  identity — not the inode, not the offset — is what makes this safe across a
  server restart or a reused inode. (The server's own incremental fold adds a
  third check on top, cursor continuity, for the pathological case of a rerun
  whose first 4 KB is byte-identical; a `&from=` resume rests on the sig for
  identity, which in practice differs because the first line carries a fresh
  `ts`.)
- **Do not `pkill -f` these scripts from a command line that also spells the
  script name** — bracket the first letter (`pkill -f "[m]onitor_server"`).
- **State-dir authority — always set `ORCH_STATE_DIR`.** The shared module
  directory (`${CLAUDE_PLUGIN_ROOT}/shared/monitoring/`) is code-only and is NOT
  an authoritative state dir — as of 2026-07-28 that is enforced, not merely
  documented. With `ORCH_STATE_DIR` unset, `status.sh` resolves the project's
  **tasks root** (`$ORCH_TASKS_ROOT` > `$CLAUDE_PROJECT_DIR/.claude/local-orchestrators`
  > cwd walk-up to a `.claude/` marker > the legacy module-relative path *only
  if it already exists*), writes to the newest task folder there with a loud
  stderr warning, and **exits 2** when even that fails; both daemons resolve the
  same four rungs (`resolve_tasks_root()`, duplicated verbatim and pinned by a
  source-text equality test) and **exit 1** rather than falling back to the
  module dir. None of the three will write inside an ancestor holding
  `.claude-plugin/plugin.json` — a packaged copy must never write into the
  version-stamped plugin cache: `status.sh` and `decision_watcher.py` refuse
  outright (exit 2 / exit 1, they exist to write), while `monitor_server.py`
  writes no token file and says so on startup, since serving read-only from a
  plugin cache is fine and it has no other state to write.
  The two `.gitignore` lines that used to sanction
  module-dir droppings are gone, and the development repository's
  `tests/test_bootstrap.py` asserts they stay gone. Set `ORCH_STATE_DIR` on
  every call regardless: it is the only way to say *which* task an event
  belongs to.

## Layout convention

Two trees, and the split between them is the whole convention: **code ships in
the plugin, state stays in your project.** Nothing under the plugin root is
ever written to — it is a version-stamped cache that an update replaces — and
nothing under the project tree is copied per task from the module.

The plugin root (`${CLAUDE_PLUGIN_ROOT}` inside a session; `claude plugin list`
prints the install path; in the development checkout it is `plugin/touch`):

```
${CLAUDE_PLUGIN_ROOT}/
├── bin/                             # touch-status, touch-monitor, touch-watcher, …
│                                    #   on PATH while the plugin is enabled
├── shared/monitoring/               # this module — stateless, same for every task
└── skills/
    ├── m-orchestrator/SKILL.md      # monitoring integration skill
    ├── execute-research/            # research -> ONE synthesized plan (read-only)
    └── implement-plan/              # plan -> implement->test->critique loops
```

Your project, the only place a run writes:

```
<your project>/
└── .claude/
    └── local-orchestrators/<task-name>/   # per task: workflow scripts + state
        ├── events.jsonl                   # the append-only stream
        ├── orch-config.json               # the run's config
        ├── .watcher-state.json            # the watcher's checkpoint
        └── orch-scripts/                  # the task's adapted workflow script
```

`ORCH_STATE_DIR` names one `<task-name>` folder in the second tree; it never
points into the first.
