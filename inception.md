# Touch — Inception

Synthesis of everything established about this project as of 2026-07-25.
Sources: `README.md`, `CLAUDE.md`, the `.claude/` skills and monitoring module,
the driver-verified digest at
`.claude/local-orchestrators/touch-aggregator/context/driver-context.md`, and —
now complete — the six-perspective research run (110 findings) and its
synthesized plan at
`.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md`
(**the normative design document**; this file is the summary). All facts
verified against Claude Code CLI 2.1.220.

## 1. What Touch is

A web app for visualizing and managing subagents in a Claude Code session.
Two components: **aggregator** and **touch-visual**.

- **Main page**: a terminal-styled web view over a Claude Code session — the
  primary UI; the user drives Claude Code from it.
- **Left sidebar**: a list of such terminal sessions; clicking one opens it.
- **Per-terminal page**: n8n-like UML diagrams/graphs of the run, with controls
  to **pause, restart, start and terminate agent loops**.
- The "loops" are exactly those defined by the `execute-research` and
  `implement-plan` skills in `.claude/skills/` — their entities (task, plan,
  sub-plan, agent, attempt, gate) are what the UI renders and drives.

## 2. Repo state

No application source yet. The repo holds `README.md` (product intent),
`CLAUDE.md`, `.gitignore`, and `.claude/`:

- `.claude/skills/execute-research/` + `implement-plan/` — the orchestration
  skill pair. Research fans out read-only perspective agents (opus) behind a
  barrier, then ONE fable synthesizer writes `plan/<name>-plan.md`.
  Implementation runs a fable divider (file-ownership partitioning, one file =
  one owner) then per sub-plan a gated loop: fresh implementer per attempt →
  read-only test gate → adversarial critique → until green or MAX_ATTEMPTS,
  then an aggregate gate. Serial by default. `templates/*.workflow.js` are the
  normative protocol. Handoff between attempts is via
  `findings/<plan>-<gate>-attempt-<N>.md` file paths, never inlined text.
  Never resume a prior agent — always a fresh subagent.
- `.claude/shared/monitoring/` — a working, zero-dependency (bash + Python 3
  stdlib + browser) live-monitoring stack: `status.sh` appends events to
  `<task-dir>/events.jsonl`; `decision_watcher.py` tails a Workflow run's
  `journal.jsonl` and derives events plus token accounting from the
  `[monitor] plan=… stage=… role=… attempt=…` prompt marker (the deterministic
  naming source); `monitor_server.py` serves `monitor.html` over
  HTTP + WebSocket. `monitoring.md` is normative for its event schema. The
  module stays untouched and running (plan D11): Touch **copies** its
  battle-tested semantics (torn-tail tailing, checkpoint identity + atomic
  replace, message-id token dedup, session-rotation glob union, realpath
  containment, escape-first rendering) but not its one-process-per-run,
  one-way-transport architecture.
- `.claude/local-orchestrators/` — per-task run history (mostly carried-over
  `omnigent` examples). `touch-aggregator/` is this project's research run:
  findings, driver context, and the completed plan.

## 3. Verified facts about the substrate (CLI 2.1.220, primary-source)

**Session registry** — `~/.claude/sessions/<pid>.json`, one file per CLI
process: `pid`, `procStart`, `sessionId`, `cwd`, `status`, `kind`, `name`.
**Not a heartbeat**: observed 863 s stale (`status:"busy"`) while the session
was actively running six subagents, and it is written non-atomically. Liveness
must be `/proc/<pid>` existence + `procStart` match (= `/proc/<pid>/stat`
field 22). `sessionId` is mutable — `/clear` starts a NEW sessionId and new
transcript file under the same pid — so sessions are keyed on
`(pid, procStart)` and sessionId is re-resolved every poll.

**Transcripts** — `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl`.
**NOT append-only**: `performRemoveByUuid` truncates and rewrites the tail,
`performCompactTranscript` rewrites the whole file via tmp + rename — so
byte-offset tailing silently freezes; checkpoints must carry
`(st_dev, st_ino, size, offset)` and re-ingest idempotently from 0 on inode
change or shrink. Records classify into the CLI's own four buckets:
`transcript` (user/assistant/system/attachment — carry globally unique `uuid`),
`boundary-cleared`, `accumulate`, and `last-wins` metadata. `usage` is copied
onto every split record of one API response — naive summing over-counts output
tokens 2.09x; dedupe by `message.id`. Large tool results can spill to a
`tool-results/` dir with a pointer record (confirmed in the binary; readers
must handle it). Lazy creation: a session may exist with no `.jsonl` yet.
There is no session-end record.

**Write latency** — *corrects the earlier driver estimate of ~5 s*: the writer
flushes on a **100 ms timer per completed content block** (measured: a
`tool_use` record landed +0.10 s, intra-turn). Transcript-fed panes are honest
at block granularity with sub-second freshness; a void occurs only inside one
long uninterrupted thinking/text block. Cost: torn tails (single `appendFile`
batches, 46 KB lines observed) — cut at last `\n`, defer the remainder.

**Subagents** — `<sid>/subagents/agent-<agentId>.jsonl` + `.meta.json`.
For **Agent-tool spawns**, `.meta.json` carries
`agentType, description, toolUseId, spawnDepth, model` at spawn instant —
`toolUseId` joins to the parent's `tool_use` block, a harness-written
parent→child edge. For **Workflow spawns**, `.meta.json` is a 63-byte stub
with no description or toolUseId — node naming comes only from the `[monitor]`
prompt marker. `agentId` (17-hex) is harness-generated, never choosable.
`spawnDepth` is 1 for both direct and workflow subagents (does not encode
nesting). There is no completion marker inside a transcript: node liveness is
three-state — running (<180 s idle), finished (journal `result` / parent
`tool_result`), unknown/possibly-stalled (≥180 s idle).

**Workflow runs** — `journal.jsonl` entries are bare
`{type: "started"|"result", key, agentId}`: no timestamps, journal order ≠
spawn order (proved), `key` is a content hash (identical prompts collide;
a stall watchdog can emit multiple `started` for one logical node — graph
nodes key on `(runId, key, ordinal)`), and `result` is a Python-repr string,
never parseable as JSON. `agent()` label/phase are **never persisted**
mid-run. The rich record (`<sid>/workflows/<runId>.json`) is written once, on
the completion path, only if the CLI survives — post-hoc reconciliation only.
Async runs return `async_launched` and never get a parent completion record;
the run graph exists solely in the journal. `toolUseResult.totalTokens` is the
LAST API call only (measured 14x under-report) — ignore it; sum deduped
`message.usage` instead.

**Other sources** — `~/.claude/history.jsonl`, `~/.claude/file-history/`,
`~/.claude.json`. `~/.claude/.credentials.json` must never be served. The
CLI's retention sweep unlinks transcripts and `rm -rf`s whole subagent trees —
**Touch must own its history** (`.touch/` store), treating `~/.claude` as a
read-only tap it never writes to.

## 4. The hard truths (independently confirmed by driver + research)

**No attach, ever.** There is no channel into an already-running interactive
session — no socket, no port, TIOCSTI disabled, no signal handler beyond
terminate. A "loop" is a JS closure inside the one CLI process (verified: one
pid while six agents ran) — no OS identity to signal. Per-agent
kill/pause/skip/retry exist in-process but are wired only to the TUI.
**Conclusion (plan D1): Touch hosts sessions; it never attaches.** Owned
sessions (spawned by Touch under its PTY) get terminal + controls; observed
sessions (found in the registry) get a read-only semantic transcript view.

**Thinking is never persisted** — every thinking block on disk is
`thinking: ""` + signature (44/44 main, all subagents). No thinking pane;
render "thought for N s" markers only.

**No PTY/ANSI bytes on disk**; the binary's asciicast recorder is inert in
2.1.220. Terminal fidelity requires owning the PTY; the transcript supports a
*semantic re-render* (prompts, text, tool cards with full untruncated results
— richer than the TTY) but not a terminal.

**"Pause" does not exist** in any CLI channel — the harness's own pause is
kill with a different status label; no suspend, no checkpoint. The only honest
pause is a **hook gate**: a PreToolUse/SubagentStart hook that *holds* its
response (verified: held a tool call 20 s, then released; payloads carry
`agent_id`/`agent_type`) — per-agent, effective at the next tool boundary,
deterministic on owned sessions. Hooks are strictly blocking (a slow hook
delays the session), so Touch hooks only append-a-line-and-exit or gate
intentionally.

**Working control channels, honestly classified** (plan D7):
- *Start* (v1, deterministic): Touch spawns the owned session.
- *Terminate session* (v1, deterministic): escalation ladder — type `/exit` →
  SIGHUP the process group → SIGKILL (SIGTERM verified ineffective on the TUI).
- *Stop loop* (v1, model-mediated): type a `TaskStop({taskId})` instruction
  into the owned session, gated on registry idle; confirmed only by observed
  effect. The harness records nothing about stops — Touch's own
  `control.jsonl` audit is the only record, and wins over quiet-timeout
  inference.
- *Restart loop* (v1, model-mediated): typed
  `Workflow({scriptPath, resumeFromRunId})`; same-session-only; replayed
  agents don't re-execute (rendered "replayed, not re-executed"); Touch
  records a `git stash create` checkpoint first because resume onto a dirty
  tree is unguarded.
- *Pause* (v1.5, deterministic on owned sessions): the hook gate above.
- Also real: `--input-format stream-json` accepts
  `initialize/interrupt/set_model/set_permission_mode` (interrupt verified
  live) — deferred, PTY-hosted sessions only in v1. `--bg-pty-host` IS
  invocable standalone (frame protocol, 256KB scrollback replay, multi-
  subscriber — driver overridden on this) but stays **rejected** as private
  and version-coupled, like `--remote-control` (cloud relay).

**Every control confirmation is a derived observation, never an assumption** —
model-mediated verbs surface `requested → sent → confirmed` distinctly in the
UI; requested-but-unconfirmed is the *normal* failure mode.

## 5. Environment constraints (corrected)

- **The firewall does NOT block package installs** (npm/pip through the proxy
  verified — earlier assumption overturned). The real rule (plan D8):
  stdlib-only at runtime, zero network fetches from the page; npm allowed only
  build-time to vendor pinned, committed assets (`touch-visual/vendor/`,
  sha256 manifest). Vendored xterm.js is required — captured PTY bytes include
  alt-screen, mouse protocols, bracketed paste, 24-bit color.
- **No g++ in the image** — node-pty cannot build; the PTY tier is Python
  stdlib `pty.openpty()` + `Popen(start_new_session=True)` + asyncio
  `add_reader` (never `pty.fork()`).
- Sandbox: bind `0.0.0.0`, publish via
  `sbx ports $SANDBOX_VM_ID --publish 8932:8932/tcp` (8931 is the live
  monitor). Because transcripts hold unredacted secrets and controls are
  command execution, the 0.0.0.0 bind is compensated by a **per-boot 256-bit
  token on every route** + Origin/Host allowlist at WS upgrade (the existing
  monitor accepted a cross-origin handshake — that class of bug is a
  non-negotiable fix in Touch). Typed endpoints only; hard denylist on
  credentials/history/settings.
- **Spawn hygiene**: child env built from an allowlist —
  `CLAUDE_CODE_CHILD_SESSION=1`, if inherited, silently disables transcript
  persistence and would starve the whole read side. Always
  `--session-id <uuid>`.
- Never delete finished task folders; tests stay stdlib-only, directly
  executable, plus socket-level integration tests for the network layer.

## 6. Decisions — RESOLVED (plan Part B, D1–D14)

The formerly open questions are decided in the plan; headlines:

1. **Host vs observe (D1/D2)**: Touch hosts; owned vs observed class drives
   every affordance; observed sessions get no controls in v1.
2. **Verbs (D7)**: the honest control table in §4 — start/terminate
   deterministic v1; stop/restart model-mediated v1; pause as hook gate v1.5;
   suspend impossible; rejected channels recorded in D14 so nobody re-hunts.
3. **Touch-owned state (D5)**: repo-local `.touch/` (gitignored;
   `TOUCH_STATE_DIR` override) — `control.jsonl` audit, hook spools, per-
   session and per-run stores, PTY spool + index. Never under
   `.claude/local-orchestrators/`.
4. **Never write under `~/.claude/`** (D4, confirms driver §7.2) — not
   transcripts, not journals, not config. Read-only tap.
5. **Event model (D4)**: ONE new format, "touch events v2" —
   `{v, seq, ts, source, kind, ref, data}` with the ref union
   `{uuid} | {toolUseId} | {agentId} | {runId,key,ordinal} |
   {pid,procStart}`; event log with state by reduction; single-writer `seq`;
   legacy `events.jsonl` ingested read-only as `source:"legacy"`, not
   extended, not destructively subsumed.
6. **Identity (D3)**: session `(pid, procStart)`; record `uuid` (upsert);
   tokens by `message.id`; tool joins by `tool_use_id`; agent by full 17-hex
   id; graph node `(runId, key, ordinal)`.
7. **Stack (D8)**: Python 3.11+ stdlib, one asyncio process, one port (8932),
   vendored xterm.js, hand-rolled layered SVG graph (no dagre/elk/cytoscape/
   mermaid), no bundler, 250 ms stat-first polling + opt-in hook push channel.
8. **Honesty rules (D13)**: harness-derived graph facts render solid,
   convention-derived (marker) dashed, declared-not-observed as declarations;
   quiet runs show "no activity", never fabricated verdicts.

## 7. Where things stand

The `execute-research` run for Touch is **complete** (7 agents, ~1.09M tokens,
finished 2026-07-25 ~03:26Z): six read-only researchers (sessiondata,
agentgraph, liveio, control, priorart, stack — 110 findings, files under
`touch-aggregator/findings/`) and the fable synthesizer, which reconciled them
against the driver context (6 conflicts resolved against primary sources) and
wrote **`plan/touch-aggregator-plan.md`**: 14 binding global decisions,
**23 ordered implementation items** (T1 scaffolding → T23 docs, spanning
vendoring, WS codec, server core, `.touch/` store, ingestion, graph model,
PTY host, hook pack, APIs, control plane, pause gate, frontend, tests), a
discarded-findings register, and 10 UNVERIFIED items each with its cheapest
settling experiment. Next step: hand the plan to `implement-plan` for
division and gated implementation.

**Scoped v0 plan** (2026-07-25): a monitoring + spawning slice was planned
from conversation at
`.claude/local-orchestrators/touch-monitor-spawn/plan/touch-monitor-spawn-plan.md`
— 12 items (P1–P12: store, discovery, tailing, ingestion, name join, control
intents, server, API/WS, monitoring page with stop buttons, hook pack, e2e
simulation), inheriting D1–D14, deferring PTY/terminal, pause gate, SVG
graph, and Mongo. Ready for `implement-plan`.

**Drafted since the plan** (not yet in it — companion to plan items T10/T14):
`.claude/skills/touch-orchestrate/SKILL.md`, the orchestrator skill that
standardizes how the main session spawns agents Touch can see and stop. Its
standards: hierarchical naming derived from one root name
(`root_name` → `root_name_research1` → `root_name_research1_subagent1`; names
are logical slots, `attempt=` distinguishes physical spawns), a mandatory
`[touch] name=… parent=… root=… role=… attempt=…` first prompt line plus the
name verbatim in the Agent-tool description (the only writable pre-spawn
identity — harness `agentId` is read-only, joined at spawn), background
spawns recorded in a spawn ledger so `TaskStop` can kill each agent
individually, state files under `<task-dir>/state/`, and a
`.touch/control.jsonl` polling loop with acknowledged stop intents. Skill =
cooperative standard; the T10 hook pack is the deterministic backstop (a
PreToolUse hook can deny non-conforming spawns; SubagentStart/Stop hooks
record lifecycle facts regardless of model cooperation).
