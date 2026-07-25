# Touch v0 — monitoring + agent spawning (plan)

Scope: the slice of Touch settled in conversation on 2026-07-25 — spawn agents
to the `touch-orchestrate` standard, discover and monitor them live on a web
page, and stop them from that page. Consumable by `implement-plan` as-is.

Binding context (do not re-derive, do not contradict):

- `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md`
  Parts A–B (decisions D1–D14) — this plan is a scoped subset of it.
- `.claude/skills/touch-orchestrate/SKILL.md` — the spawn standard (naming,
  marker, ledger, control loop) is NORMATIVE; this plan builds the machinery
  around it, it does not redefine it.
- `inception.md` — verified substrate facts (CLI 2.1.220).

## Global decisions (this slice)

- **G1 — In scope**: session discovery, subagent discovery + name join,
  transcript ingestion, live monitoring page (session sidebar + per-session
  agent tree), stop control via `.touch/control.jsonl`. **Out of scope
  (deferred to the full plan)**: PTY terminal + xterm.js (T2/T9/T13/T17),
  owned-session spawner, pause hook gate (T15), SVG n8n graph (T19 — v0 uses
  an HTML tree), archived-run import (T20), monitoring-module changes (T21).
- **G2 — Storage**: `.touch/` JSONL per D5, stdlib only per D8. MongoDB was
  discussed and is NOT adopted here; adopting it later is an explicit D5/D8
  amendment, not an implementation choice. The document shapes from the
  discussion (records by `uuid`, agents by `agentId`, usage by `message.id`)
  become the in-memory model reduced from the events log.
- **G3 — Identity**: exactly the D3 table. Sessions `(pid, procStart)`;
  records `uuid` (upsert); tokens `message.id`; agents 17-hex `agentId`;
  Touch names are logical slots bound per `(name, attempt)` to one agentId.
  File line numbers / byte offsets are tailing cursors only, never identity;
  spawn location is stored as `{recordUuid, toolUseId}` + a perishable
  `fileHint {path, line, ino, size}` validated before use.
- **G4 — Event model**: touch-events-v2 exactly as D4 (`v, seq, ts, source,
  kind, ref, data`; ref union `{uuid}|{toolUseId}|{agentId}|{pid,procStart}`).
  Single-writer `seq` in the aggregator process. State = reduction over the
  log; the server keeps the reduction in memory and replays the log on boot.
- **G5 — Server**: one Python 3.11+ stdlib asyncio process, port 8932
  (argv > `$TOUCH_PORT` > `.touch/server.json` > 8932), bind `0.0.0.0`.
  Per-boot 256-bit token on every route except `/health`
  (`hmac.compare_digest`); `Origin`/`Host` allowlist at WS upgrade;
  method-dispatching router, default 404; no path parameters — ids validated
  by regex (D9).
- **G6 — Control**: stop only, v0. Flow: UI button → `POST /api/control`
  → aggregator appends `{action:"stop", name, ts}` to `.touch/control.jsonl`
  (state `requested`) → the orchestrating session polls the file per the
  skill and calls `TaskStop`, appending an ack line → aggregator observes the
  ack and the agent going quiet/finished → `confirmed`. No ack within 120 s →
  `expired` (shown, never silently dropped). The control log is the only
  record of a stop and wins over inference (D7).
- **G7 — Ingestion cadence**: 250 ms stat-first polling; tail checkpoints
  `(st_dev, st_ino, size, offset)`; inode change or shrink ⇒ idempotent full
  re-ingest (uuid upserts make it a no-op); torn tails cut at last `\n`;
  `/clear` rotation handled by re-resolving sessionId every tick and gluing
  on `(pid, procStart)`.
- **G8 — Honesty in UI** (D13): agent liveness is three-state (`running` /
  `finished` / `unknown ≥180 s idle` with the idle duration shown); control
  intents render `requested / sent / confirmed / expired` distinctly;
  name-derived hierarchy renders as convention (dashed), harness joins
  (toolUseId) as fact (solid).
- **G9 — Tests**: stdlib-only, each `tests/test_*.py` executable, exits
  non-zero on failure; `tests/run_all.sh` runs them plus the four existing
  monitoring tests. Network layer gets socket-level integration tests on
  port 0. Frontend gets static source guards (genre of `test_frontend.py`).

## Ordered items

### P1. Scaffolding

Files: `tests/run_all.sh`, `.gitignore` (additive edit).
Create `aggregator/`, `touch-visual/`, `tests/` at repo root. Add `.touch/`
to `.gitignore` WITHOUT touching existing lines (`test_shell.py` guards the
monitoring ignores). `run_all.sh` loops every executable `tests/test_*.py`
and the four monitoring tests; non-zero on first failure.
Test: run_all.sh runs green on a fresh checkout (with only monitoring tests).

### P2. Store — `.touch/` layout + events v2 writer

Files: `aggregator/store.py`, `tests/test_store.py`.
Owns: `.touch/server.json`, `.touch/control.jsonl` (append + iterate),
`.touch/sessions/<pid>-<procStart>/events.jsonl` (v2 records, single-writer
monotonic `seq` per session, fsync batched), boot-time replay iterator.
`TOUCH_STATE_DIR` override. One `write()` per record line (multi-writer
safety invariant is documented and tested for the control file).
Test: seq monotonicity across restart; replay equals written; torn final
line tolerated; control append is a single write.

### P3. Session discovery + liveness

Files: `aggregator/sessions.py`, `tests/test_sessions.py`.
Scan `~/.claude/sessions/*.json` each tick; key `(pid, procStart)`; liveness
= `/proc/<pid>` exists AND `/proc/<pid>/stat` field 22 == procStart — never
`status`/`updatedAt` (registry verified 863 s stale). Tolerate torn/empty
registry JSON (retry once, keep last good). Track the mutable sessionId list
per session; resolve transcript paths via the project-slug glob union
(pattern of `decision_watcher.py:86-100`). Emit v2 `kind:"session"` events on
appear/change/death.
Test: fake registry dir + fake `/proc` root (injectable paths); stale-busy
ghost detected dead; sessionId rotation glues to one session.

### P4. Tailing primitives

Files: `aggregator/tailer.py`, `tests/test_tailer.py`.
Checkpointed tailer: `(st_dev, st_ino, size, offset)`; inode change or
`size < offset` ⇒ signal full re-ingest; cut at last `\n`, defer remainder;
incremental UTF-8 decode; back off 200 ms when `.compact.tmp.*` exists
beside the file. Pure library — no policy.
Test: append, truncate-rewrite, rename-swap, torn multibyte tail, 46 KB
lines.

### P5. Transcript ingestion

Files: `aggregator/ingest.py`, `tests/test_ingest.py`.
Parse records; classify by the CLI's four buckets (transcript /
boundary-cleared / accumulate / last-wins); unknown types retained raw,
never crash. Upsert by `uuid` into the in-memory model; token accounting per
distinct `message.id` (input = input + cache_read + cache_write), monotonic
clamps (semantics of `decision_watcher.py:154-197`); detect `tool_use` /
`tool_result` pairs (spawn/finish edges); handle `tool-results/` pointer
records. Emit v2 `kind:"tool"|"token"|"log"` events.
Test: split-usage dedup (2.09x fixture); re-ingest idempotence; tool pair
matching; pointer record passthrough.

### P6. Agent discovery, name join, rollups

Files: `aggregator/agents.py`, `tests/test_agents.py`.
Watch `<sid>/subagents/` for `agent-*.meta.json` (full or stub). Bind
`touchName ↔ agentId ↔ toolUseId`: name from `description` (Agent-tool
spawns) and/or the `[touch] name=… parent=… root=… role=… attempt=…` first
prompt line of `agent-<id>.jsonl`; cross-check
`<task-dir>/state/spawn-ledger.jsonl` when present. Unnamed agents get
`agentId` as display name, flagged unconventional. Build the hierarchy from
marker `parent=` (fact: the stated parent; convention: name-derivation
match). Per-agent rollups: turns, tool calls, tokens, first/last activity;
three-state liveness (180 s threshold). Emit v2 `kind:"agent"` events
(spawned, named, state change, rollup deltas).
Test: fixture trees for both spawn mechanisms (full meta vs stub+marker);
join correctness; (name, attempt) → agentId uniqueness; stale ⇒ unknown not
finished.

### P7. Control intents

Files: `aggregator/control.py`, `tests/test_control.py`.
Intent state machine over `.touch/control.jsonl`: `requested` (Touch wrote
it) → `sent` (orchestrator ack line observed) → `confirmed` (target agent
observed finished/aborted after ack) | `expired` (120 s without ack).
Idempotent re-reduction on boot. Emits v2 `kind:"control"` events.
Test: full lifecycle on fixture files; expiry; duplicate acks harmless;
restart mid-flight resumes correctly.

### P8. Server core

Files: `aggregator/server.py`, `tests/test_server_core.py`.
Asyncio HTTP + WebSocket (hand-rolled upgrade per the monitoring module's
codec, but with inbound frame unmasking — the existing server discards
client frames). Per-boot token (printed once with the publish instructions),
required on everything but `/health`; Origin/Host allowlist at upgrade;
method dispatch; 404 default; static serving of `touch-visual/` with
extension whitelist + realpath containment (pattern of
`monitor_server.py:199-212`). Wires the 250 ms poll loop: sessions → tailer
→ ingest → agents → store.
Test: socket-level — auth rejected/accepted, bad Origin rejected at
upgrade, unknown route 404, static containment, inbound WS frame decoded.

### P9. Read API + live WebSocket

Files: `aggregator/api.py`, `tests/test_api.py`.
Typed endpoints (ids regex-validated, no path params): `/api/sessions`,
`/api/session?pid=&procStart=` (agents tree + rollups + intents),
`/api/events?after=<seq>` (page), `/ws?session=<key>` (v2 replay from seq 0
or `?after=`, then live tail). `POST /api/control` `{action:"stop", name}` →
P7. Hard denylist regardless of route: `.credentials.json`,
`history.jsonl`, `~/.claude.json`, `shell-snapshots/`, `settings.json`.
Test: socket-level round-trips; replay-then-live ordering; stop POST lands
in control.jsonl exactly once.

### P10. Frontend — monitoring page

Files: `touch-visual/index.html`, `touch-visual/app.js`,
`touch-visual/style.css`, `tests/test_touch_frontend.py`.
No bundler, no external fetches (static test enforces). Left sidebar: all
sessions (owned/observed class label, liveness, name, cwd). Main pane per
session: agent tree from the name hierarchy (`root_name` → children by
derivation), per node: touchName, role, attempt, state chip
(running/finished/unknown+idle), token count, spawn time; solid vs dashed
edge styling per G8; a **Stop** button per running agent showing intent
state (`requested → sent → confirmed/expired`) after click. Escape-first
rendering discipline (pattern of `monitor.html:299-321`). WS client with
replay + tail and resync on gap.
Test: static guards — no external URLs, escape function present and used,
stop button wired to `/api/control`, state-chip whitelist.

### P11. Hook pack (opt-in, deterministic facts)

Files: `aggregator/hooks/touch-hook.sh`, `aggregator/hooks/README.md`,
`tests/test_hooks.py`.
`SubagentStart`/`SubagentStop` hook script: append one JSON line
(`agent_id`, event, ts) to `.touch/hooks/<session_id>.jsonl` and exit;
explicit `"timeout": 5`. README documents the settings.json wiring and the
optional PreToolUse marker-enforcement variant (deny Agent-tool calls whose
description/prompt lack the `[touch]` marker) — enforcement is opt-in and
off by default. Aggregator ingests the spool as `source:"hook"` (P8 wires
it).
Test: hook script emits a single well-formed line and exits 0 fast; spool
ingestion produces agent events; malformed spool lines skipped.

### P12. End-to-end simulation + docs

Files: `tests/test_e2e_sim.py`, `README-touch.md`, `CLAUDE.md` (additive
edit: run commands + test list).
Simulation: fabricate a session under a fake `~/.claude` root — registry
file, main transcript with two Agent spawns per the touch-orchestrate
standard (one full meta, one stub+marker), growing agent transcripts, spawn
ledger — assert: sidebar model, join table, tree, rollups; then write a stop
intent + ack + agent goes quiet, assert `confirmed`. Docs: how to run
(`python3 aggregator/server.py`), publish port 8932, token flow, how the
skill + page + control file interact, and the honest-semantics table for
stop.
Test: the simulation IS the test.

## Discarded in this slice (recorded)

Mongo storage (needs D5/D8 amendment); per-session Mongo collections
(anti-pattern — cross-session queries, index duplication); line numbers as
identity (rot on /clear, compaction, truncation — cursor/hint only); writing
any file under `~/.claude/`; pause (no honest mechanism without the hook
gate, deferred with T15); push transport into a running session (does not
exist).
