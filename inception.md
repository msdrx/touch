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

**Path note, 2026-07-30 — the only edit this file gets for the tasks-root move.**
Every `.claude/local-orchestrators/…` path below is the tree as it stood at this
snapshot; run history now lives at `.touch/local-orchestrators/`, and this
project's Claude Code auto memory at `.touch/memory/`. Nothing else here is
rewritten: a dated record whose paths are quietly updated stops being a record.

## 1. What Touch is

A web app for visualizing and managing subagents in a Claude Code session.
Two components: **aggregator** and **touch-visual**.

- **Main page**: a terminal-styled web view over a Claude Code session — the
  primary UI; the user drives Claude Code from it.
- **Left sidebar**: a list of such terminal sessions; clicking one opens it.
- **Per-terminal page**: n8n-like UML diagrams/graphs of the run, with controls
  to **pause, restart, start and terminate agent loops**.
- The "loops" are exactly those defined by the `research` and
  `implement` skills in `.claude/skills/` — their entities (task, plan,
  sub-plan, agent, attempt, gate) are what the UI renders and drives.

## 2. Repo state

*(Updated 2026-07-26, docs truth pass R-05/R-57.)* The repo now holds real
application source: `aggregator/` (the Python package — tailer, store, ws,
sessions, ingest, legacy, agents, custom_state, refs, mongo_store, mirror,
server), `touch-visual/` (`index.html`, `app.js`, `style.css`), `tests/` (one
stdlib-only executable file per module plus `run_all.sh` and `fixtures/`), and
`docs/` (`mongo.md`, `control-semantics.md`), alongside `README.md` (product
intent), `CLAUDE.md`, `inception.md`, `.gitignore`, and `.claude/`. `CLAUDE.md`
carries the current inventory; this file is the design summary and is not the
place to track file counts.

`.claude/` holds the machinery this project was built with:

- `.claude/skills/research/` + `implement/` — the orchestration
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
- `.claude/local-orchestrators/` — per-task run history: five folders, **all
  five produced by this repo's own runs** (`touch-repo-recon`,
  `touch-aggregator`, `touch-monitor-spawn`, `touch-full-recon`,
  `touch-mongo-live`). The earlier claim that these were carried-over
  `omnigent` examples was **false** and is retracted (PRODUCT-5 ≡ RUNSTATE-1):
  every `orch-config.json` on disk names a `wf_dir` under
  `~/.claude/projects/-home-laniakea-Projects-touch/…/subagents/workflows/`,
  i.e. this project's own harness journals — which is exactly what makes
  `wf_dir` the join key from a task folder to its harness run. When a `wf_dir`
  no longer exists the honest label is "archived — source transcripts
  unavailable" (GD-14), never "wrong repo". `CLAUDE.md` carries the per-folder
  inventory.

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
`boundary-cleared`, `accumulate`, and `last-wins` metadata. `usage` is a
**running counter**, re-emitted on every split record of one API response with
**growing** values — *not* copied unchanged (measured: 571 of 901 corpus
`message.id`s carry differing `output_tokens` across their records —
MONGOSCHEMA-2 ≡ SESSIONJSONL-9 ≡ LIVEFLOW-4; the earlier "copied onto every
split record" wording was false and is corrected here per R-38). Two
consequences, both binding: summing every record over-counts output tokens
~2.09×, and taking the *first* record per `message.id` under-reports 2.8×. The
only correct fold is **`$max` per field, keyed by `message.id`** — never
`$set` (write-order dependent), never `$inc`/running sums (re-ingest after a
`performRemoveByUuid` doubles them). Verified byte-identical across randomly
shuffled ingest passes (GD-25). Large tool results can spill to a
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
parent→child edge — but every field is optional: a **background**
(`run_in_background:true`) spawn's `.meta.json` omits `model` entirely, and its
launch `toolUseResult` is the richer join
(`{status:"async_launched", agentId, description, resolvedModel, outputFile}`),
with the pollable task id equal to the 17-hex `agentId` (R-04 probe 5,
2026-07-26). Background spawns write to the same
`<sid>/subagents/agent-<agentId>.jsonl` path as foreground ones.
For **Workflow spawns**, `.meta.json` is a 63-byte stub
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

**Agent lifecycle hooks — GREEN for the Workflow profile (probe D-17,
2026-07-31, CLI 2.1.220).** Until this probe, "`SubagentStart`/`SubagentStop`
fire for Workflow-profile agents" was a hypothesis read out of the binary's own
strings; only the Agent-tool case was verified. Both fire, once per `agent()`
call, matcher `"*"`, with zero LLM cooperation. Full method and payloads:
`.touch/local-orchestrators/touch-determinism/findings/d17-hook-probe-2026-07-31.md`.
What binds:

- `SubagentStart` carries exactly `{session_id, transcript_path, cwd,
  prompt_id, agent_id, agent_type, hook_event_name}` — the 17-hex `agentId`, and
  `agent_type` as the profile discriminator (`"workflow-subagent"` for Workflow
  agents, the agent type name for Agent-tool ones). **No `runId`, no label, no
  prompt** — the `[monitor]` marker is not in the payload, so anything
  plan/stage-shaped must be read from the agent transcript, which at Start may
  not exist yet (the event fires ~3 ms after launch).
- `SubagentStop` adds `agent_transcript_path` — `…/workflows/<runId>/agent-<id>.jsonl`
  for Workflow agents, `…/subagents/agent-<id>.jsonl` for Agent-tool ones, so
  `wf_dir` and `runId` are a `dirname`/`basename` away — plus
  `last_assistant_message`, `stop_hook_active` and `background_tasks[]`
  (`{id, type, status, description, …}`: the Agent-tool entry's `id` **is** the
  agentId stop handle and its `description` is the Agent-tool description; the
  Workflow entry is the *run*, which has no per-agent stop — the two
  granularities of §4, confirmed from a third source). Neither
  `last_assistant_message` nor `background_tasks[].status` is a verdict: the
  event observes a *stop*, and the verdict stays the journal `result`.
- `PostToolUse` with matcher `Workflow` fires **at launch** (`duration_ms: 4`,
  `status: "async_launched"` even for a foreground call, no second event at run
  end) and its `tool_response` is the launch record verbatim — `taskId`,
  `taskType`, `workflowName`, `runId`, `summary`, `transcriptDir`, `scriptPath`.
  It lands **3 ms before the first `SubagentStart`**, so the run→task bind can
  be published before any agent event exists to need it.
- Operationally: hooks are delivered by `--settings` in a throwaway
  `CLAUDE_CONFIG_DIR` (a second injection point for the R-04 probe-1 result
  below), the `Workflow` tool is permission-gated, and a *denied* launch
  produces a `PreToolUse` with no matching `PostToolUse`. Inside a hook,
  `os.getppid()` is the `claude` process, so `<pid>-<procStart>` is derivable
  there.

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
intentionally. **Delivery is settled (R-04 probe 1, 2026-07-26):** a hook
written into a session's settings *after* it started fires on the very next
tool call, with no restart, from the project `.claude/settings.json` **and**
from the `--settings` file — and it fires under an interactive PTY too
(probe 2). So the gate is installable into sessions Touch did not spawn; see
`.claude/local-orchestrators/touch-full-recon/report/probes.md`. Until it
ships, `pause` is not offered — GD-4 forbids rendering a verb that cannot be
honest.

**Working control channels, honestly classified** (plan D7):
- *Start* (v1, deterministic): Touch spawns the owned session.
- *Terminate session* (v1, deterministic): escalation ladder — type `/exit` →
  SIGHUP the process group → SIGKILL (SIGTERM verified ineffective on the TUI).
- *Stop loop* (v1, model-mediated): type a `TaskStop({taskId})` instruction
  into the owned session, gated on registry idle; confirmed only by observed
  effect. The harness records nothing about stops — Touch's own
  `control.jsonl` audit is the only record, and wins over quiet-timeout
  inference. **Two granularities, never conflated** (GD-8 as amended):
  *run-level* stop exists in the Workflow profile via the launch
  `toolUseResult.taskId` (verified `w4hiywrt6` / `www4dk54h`), and stops the
  whole loop; *agent-level* stop exists only in the Agent-tool profile, where
  the task id **is** the 17-hex `agentId` (R-04 probe 5). A Workflow agent has
  no per-agent stop and renders disabled with that reason.
- *Restart loop* (v1, model-mediated): **`Workflow({resumeFromRunId})` is
  rejected as the meaning of "restart"** — it replays agents without
  re-executing them (SKILLS-6), which is a transcript, not a rerun. GD-4 fixes
  ONE meaning: **re-invoke the workflow script with the stored partition
  (`subplans_file`) and `only:[ids]` — fresh agents, attempt numbering
  continues (`from_attempt`), the Divide/derivation step skipped.** Touch
  records a checkpoint first (`git stash create`, three-state per R-35:
  `sha | none | unavailable(reason)`) because a rerun onto a dirty tree is
  unguarded, and never blocks the verb on the checkpoint.
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
  verified — earlier assumption overturned). The real rule (plan **D8.1**, as
  amended by GD-21): stdlib-only on the ingest and serve critical path, with
  `pymongo==4.17.0` as the single named exception in two named modules; zero
  network fetches from the page; npm allowed only build-time to vendor pinned,
  committed assets (`touch-visual/vendor/`, sha256 manifest). Vendored xterm.js
  is required — captured PTY bytes include alt-screen, mouse protocols,
  bracketed paste, 24-bit color.
- **No g++ in the image** — node-pty cannot build; the PTY tier is Python
  stdlib `pty.openpty()` + `Popen(start_new_session=True)` + asyncio
  `add_reader` (never `pty.fork()`).
- Sandbox: the Touch server binds **`127.0.0.1` by default** (GD-13); a wider
  bind is an explicit `--open` opt-in, published from the host with
  `sbx ports $SANDBOX_VM_ID --publish 8932:8932/tcp` (8931 is the live
  monitor). Because transcripts hold unredacted secrets and controls are
  command execution, any non-loopback bind is compensated by a **per-boot
  256-bit token on every route** + Origin/Host allowlist at WS upgrade (the
  existing monitor accepted a cross-origin handshake — that class of bug is a
  non-negotiable fix in Touch). Typed endpoints only; hard denylist on
  credentials/history/settings.
- **Spawn hygiene**: child env built from an allowlist —
  `CLAUDE_CODE_CHILD_SESSION=1`, if inherited, silently disables transcript
  persistence and would starve the whole read side (**re-confirmed 2026-07-26**:
  a PTY-spawned child that inherited it printed
  `⚠ Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION marker`
  and wrote no transcript — R-04 probe 2). Always `--session-id <uuid>`.
- **The Mongo mirror is never published.** The database binds
  `127.0.0.1:27017` inside the sandbox and `sbx ports` must **not** publish
  27017 — it holds the same unredacted transcripts the token posture exists to
  protect. Use `docker exec touch-mongo mongosh …` (`docs/mongo.md`).
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
7. **Stack (D8.1)**: Python 3.11+ stdlib, one asyncio process, one port (8932),
   vendored xterm.js, hand-rolled layered SVG graph (no dagre/elk/cytoscape/
   mermaid), no bundler, 250 ms stat-first polling + opt-in hook push channel.
   **Amended by GD-21**: stdlib-only now means *stdlib-only on the ingest and
   serve critical path*. `pymongo` (pinned `==4.17.0`, with `dnspython`) is the
   ONE permitted runtime dependency and may be imported **only** from
   `aggregator/mongo_store.py` and `aggregator/mirror.py`, lazily. Its absence
   degrades the mirror to `mirror:"absent"` in `/health`; it never fails
   startup, never breaks an agent, never blocks a test. (Cite **D8.1** for this
   decision and **D8.2** for the superseded journal-`result` clause — a bare
   "D8" means neither.)
8. **Honesty rules (D13)**: harness-derived graph facts render solid,
   convention-derived (marker) dashed, declared-not-observed as declarations;
   quiet runs show "no activity", never fabricated verdicts.

## 7. Where things stand

*(Rewritten 2026-07-26 in the R-05 docs truth pass. Five run folders exist;
here is what each one actually is, and which artifact is authoritative.)*

| task folder | what it was | state | authoritative artifact |
|---|---|---|---|
| `touch-repo-recon` | first research pass over the repo/skills | complete | `findings/` (51 findings; its plan is history) |
| `touch-aggregator` | 6-perspective research → the design law | complete | `plan/touch-aggregator-plan.md` — **D1–D14 design law**, superseded as the *implementable* plan |
| `touch-monitor-spawn` | a scoped v0 slice planned from conversation | **plan only, never run** — the folder holds `plan/` and nothing else | `plan/touch-monitor-spawn-plan.md` (P1–P12, G1–G9; historical) |
| `touch-full-recon` | 6-perspective re-recon | complete | **`plan/touch-full-recon-plan.md` — the normative plan** (GD-1…GD-20, R-01…R-37) |
| `touch-mongo-live` | 5-perspective Mongo/live-flow research, then this implementation pass | research complete; implementation in progress | **`plan/touch-mongo-live-plan.md` — the amendment** (GD-21…GD-30, R-38…R-58) |

Authority ladder (GD-3), highest first: **`touch-mongo-live-plan.md`
(amendment)** → **`touch-full-recon-plan.md` (normative)** →
`touch-aggregator-plan.md` (design law D1–D14, as amended) → this file
(summary) → `README.md` (intent) → `CLAUDE.md` (session guide). A plan-only
folder is a legitimate kind, not a broken run (RUNSTATE-13), and empty `plan/`
or `report/` directories are normal.

**The `touch-aggregator` research run's token cost, corrected:** the 7-agent
run (`wf_829e6f58-b2f`, finished 2026-07-25 ~03:26Z) cost ≈ **29.5 M input /
316 k output** — the per-`message.id`-deduped rollup measured by **AUDIT-13**
(`touch-full-recon/findings/research-audit-attempt-1.md`). This file used to
say "~1.09M tokens", which was not merely wrong: `1089990` is the literal value
of `workflows/wf_829e6f58-b2f.json → totalTokens`, i.e. **the one field the
plan forbids reading** (last-API-call only, a 27× under-report here, 14×
per-agent elsewhere). Run-level tokens are always Σ over the run's nodes of the
per-node deduped total; `totalTokens`/`totalToolCalls` stay display-only
"harness reported" values, rendered beside the computed one and never
substituted for it (GD-11, GD-24's `harnessTotals`).

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
