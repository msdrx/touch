# Driver context digest — for the SYNTHESIZER only

This file carries what the driver session already established BEFORE this
research run. The research agents were deliberately run **blind** (fresh
context, no access to this file) so their findings are independent evidence.
Your job as synthesizer is to reconcile the two.

**Conflict rule**: where a research finding contradicts something below, do NOT
default to either side — open the primary source (files under `~/.claude/`, the
repo, or the CLI binary at `/home/agent/.local/share/claude/versions/2.1.220`)
and decide. State the decision and which side it overrode.

---

## 1. Product spec (from README.md — authoritative on intent)

Touch is a web page for visualizing and managing subagents in a Claude Code
session. Two components: **aggregator** and **touch-visual**.

- Main page: a terminal-styled web view over a Claude Code session — this is
  the **primary user interface**, i.e. the user drives Claude Code from it.
- Left sidebar: list of such terminal sessions; clicking one opens that terminal.
- A per-terminal page with n8n-like UML diagrams/graphs of the run, **plus
  controls to pause, restart, start and terminate agent loops**.
- "Loops" are the ones defined by the `execute-research` and `implement-plan`
  skills in `.claude/skills/`.

Repo state at the time of this run: no application source exists yet. Only
`README.md`, `CLAUDE.md`, `.gitignore`, and `.claude/` (skills + the monitoring
module + carried-over `local-orchestrators/` history from an earlier `omnigent`
project). Git was initialised on `master` with no commits.

## 2. Facts the driver VERIFIED directly (primary-source, high confidence)

### Session registry — `~/.claude/sessions/<pid>.json`
One file per running CLI process. Observed live:
```json
{"pid":622,"sessionId":"dd469822-…","cwd":"/home/laniakea/Projects/touch",
 "startedAt":1784946693282,"procStart":"10028","version":"2.1.220","peerProtocol":1,
 "kind":"interactive","entrypoint":"cli","name":"touch-2b","nameSource":"derived",
 "status":"busy","updatedAt":…,"statusUpdatedAt":…}
```
- `status` ∈ `idle | busy | waiting | shell`; `waitingFor` gives the reason
  (`"input needed"`, `"sandbox request"`, `"dialog open"`, `"worker request"`).
- Binary's parser accepts a superset: `messagingSocketPath`, `logPath`, `jobId`,
  `parkedJobId`, `bridgeSessionId`, `agent`, `state`, `detail`, `tempo`, `needs`,
  `tmux`. `kind` ∈ `interactive | bg | daemon | daemon-worker`.
- Liveness must be `pid` + `procStart` (the CLI itself reaps this way). There is
  **no heartbeat** — a killed session leaves `status:"busy"` forever.
- `sessionId` is a MUTABLE FIELD patched in place; `/clear` rotates it under a
  stable pid. Key on `pid+procStart`, never on sessionId.

### Session transcript — `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl`
Append-only JSONL; slug = cwd with `/`→`-`. Record types observed:
`assistant`, `user`, `mode`, `permission-mode`, `attachment`, `last-prompt`,
`ai-title`, `file-history-snapshot`, `file-history-delta`, `system`.
- Shared envelope: `uuid`, `parentUuid`, `sessionId`, `timestamp`, `cwd`,
  `gitBranch`, `version`, `isSidechain`, `isMeta`.
- `assistant.message`: `{id, model, role, content[], usage, stop_reason,
  stop_details, diagnostics}`; plus `requestId`, `effort`, `attributionSkill`.
- Content blocks: `text`, `thinking`, `tool_use`; user side `tool_result`.
- `toolUseResult` is structured per tool — Bash `{stdout, stderr, interrupted,
  isImage, noOutputExpected}`; Read `{type, file}`; Write/Edit include
  `structuredPatch`, `originalFile`, `userModified` (real diffs available).
- `usage`: `input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`, `cache_creation{ephemeral_5m,1h}`,
  `server_tool_use`, `service_tier`, `speed`, `iterations[]`.
- `system/subtype:"turn_duration"` → `durationMs`, `messageCount`.
- Metadata records are RE-APPENDED periodically (`reAppendSessionMetadata`), so
  duplicates are normal: treat `mode`/`permission-mode`/`ai-title`/`last-prompt`
  as last-writer-wins state, never as an event log.

### Subagents — `<project-slug>/<sessionId>/subagents/`
Verified live for two agents spawned in the driver session:
```
agent-<agentId>.jsonl        full transcript, same schema as main
agent-<agentId>.meta.json    {"agentType","description","toolUseId","spawnDepth","model"}
```
- `.meta.json` is written **at spawn** (mtime = spawn instant), the transcript
  keeps growing — so a node gets its label immediately.
- `toolUseId` matches the parent transcript's `tool_use` block → a
  harness-written parent→child edge requiring **no convention**.
- Running vs finished is derivable: a spawn is live while its `tool_use` id has
  no matching `tool_result` in the parent transcript. (Caveat: an agent that
  dies with its session never gets a `tool_result` and looks RUNNING forever —
  needs a staleness rule.)
- Per-agent rollups computed successfully from transcripts: turns, tool-call
  count, first/last timestamp, tokens (dedupe by `message.id`; input must
  include cache reads + cache writes).
- **A plain `Agent`-tool spawn writes NO journal entry.** Only Workflow runs
  produce `journal.jsonl`. The existing `decision_watcher.py` tails only the
  journal and is therefore blind to ordinary subagents.

### Workflow runs
- `<sessionId>/subagents/workflows/<runId>/journal.jsonl` — entries are exactly
  `{type:"started", key, agentId}` / `{type:"result", key, agentId, result}`.
  `key` is a sha256-derived hash, not a label. **Journal entries carry no
  timestamps** (the existing watcher reconstructs times from transcripts).
- `<sessionId>/workflows/<runId>.json` — written ONCE on the completion path:
  `{runId, timestamp, taskId, script, scriptPath, args, result, agentCount,
  logs, durationMs, error, summary, workflowName, title, status, startTime,
  phases, defaultModel, workflowProgress, totalTokens, totalToolCalls}`.
- `workflowProgress[]` entries: `{type:"workflow_agent", index, label,
  phaseIndex, phaseTitle, agentId, agentType, isolation, model, state,
  startedAt, lastProgressAt, cached, resultPreview, promptPreview}`.
  So labels + phase grouping exist — but only AFTER the run ends. Mid-run the
  only on-disk name source is the prompt (hence this repo's `[monitor]` marker).

### Naming
- `Agent` tool: `description` is the only free-text name; lands in `.meta.json`
  at spawn. `agentId` is harness-generated and cannot be chosen — yet it is the
  handle every control API keys on.
- Workflow: `opts.label` + `phaseTitle`, persisted only at run end.

### Other readable sources
`~/.claude/history.jsonl` (every prompt: `display, pastedContents, project,
sessionId, timestamp`), `~/.claude/file-history/<sessionId>/<hash>@vN` (pre-edit
file snapshots — verified to hold real prior contents), `~/.claude/todos/`,
`~/.claude/shell-snapshots/`, `~/.claude.json`, `~/.claude/settings.json`.
`~/.claude/.credentials.json` exists and must never be served.

## 3. Two independent feasibility assessments already collected

Both returned **feasible-with-caveats** and converged: reading is solved; the
terminal and control halves are not.

### Assessment A — data layer
- Grepped every file under `~/.claude` <20 MB for `\x1b[`: **zero hits**. No
  PTY/ANSI bytes, no scrollback, anywhere on disk.
- **`thinking` blocks persist EMPTY** (`thinking:""` + signature only) — 32/32
  in the parent transcript, 14/14 in a subagent. Reasoning shown in the TUI is
  never written to disk.
- Write latency measured ≈5 s and **per completed message** — batched via
  `appendToFile`/`scheduleDrain`/`drainWriteQueue`. No intra-turn flush, so a
  90 s turn (observed `durationMs: 90511`) is a 90 s void in the files.
- Hook surface enumerated from the binary: `PreToolUse, PostToolUse,
  PostToolUseFailure, PostToolBatch, Notification, UserPromptSubmit,
  UserPromptExpansion, SessionStart, SessionEnd, Stop, StopFailure,
  SubagentStart, SubagentStop, PreCompact, PostCompact, PermissionRequest,
  PermissionDenied, Setup, TeammateIdle, TaskCreated, TaskCompleted,
  Elicitation, ElicitationResult, ConfigChange, WorktreeCreate, WorktreeRemove,
  InstructionsLoaded, CwdChanged, FileChanged, DirectoryAdded, MessageDisplay`.
- `MessageDisplay` fires per streaming display delta
  (`{hook_event_name, turn_id, message_id, index, final, delta}`) and is the
  only token-granularity channel — but `forceSyncExecution:true`, i.e. it runs
  on the render path; a slow hook degrades the user's real terminal.
- `SessionEnd` reasons: `["clear","resume","logout","prompt_input_exit","other",
  "bypass_permissions_disabled"]`. `/clear` ends the session (new id, new file);
  `/compact` stays in-band via `system/subtype:"compact_boundary"`. Different
  handling required.
- Large tool results spill out-of-band to a `tool-results/` dir (threshold
  constant 50000), so the transcript may hold a pointer rather than content.

### Assessment B — control layer
- `/proc/<pid>/fd` for the live session: only `/dev/pts/0` + outbound sockets.
  `ss -lnpx` and `ss -lntp` return **nothing** — no unix or TCP listener exists.
  `dev.tty.legacy_tiocsti = 0`, so TTY keystroke injection is off. An
  already-running interactive session has **no input channel but its own TTY**.
- The CLI already implements the four verbs — as in-process TUI functions:
  `pauseWorkflowTask`, `killWorkflowTask`, `skipWorkflowAgent`,
  `retryWorkflowAgent`, wired to a `WorkflowDetailDialog`
  (`{onSkipAgent, onRetryAgent, onPause}`). Only the transport is missing.
- **Pause IS kill**: both route through the same `abortController.abort()`,
  differing only in a status label (`"paused"` vs `"killed"`). No suspend, no
  cooperative checkpoint anywhere.
- A "loop" has NO on-disk existence: `runLoop()` in `implement.workflow.js` is a
  JS `while` closure; `attempt`, `openFindings`, counters live in V8 heap. The
  workflow itself runs inside the session process (`taskRegistry` entry
  `{type:"local_workflow", …, abortController, agentControllers:new Map}`).
- `Workflow({scriptPath, resumeFromRunId})` replays the longest unchanged prefix
  of `agent()` calls from the journal — but it is **same-session-only**, and
  cached agents do NOT re-run, so their file edits are not re-applied. Resuming
  onto a dirty/reverted tree can yield a "green" run with missing code.
- Other input paths, all constrained: `--bg-pty-host` attach (daemon workers
  only, auth-gated `CLAUDE_BG_PTY_AUTH`, undocumented framed protocol);
  `--input-format stream-json` (full control plane incl. `interrupt`,
  `set_permission_mode`, `can_use_tool` — but requires `--print`, so no TUI);
  teammate mailbox `<teams-root>/<team>/inboxes/<agent>.json` (team-gated);
  `--remote-control` (works, but routes through Anthropic's cloud).
- The existing `monitor_server.py` is structurally one-way: it never parses the
  HTTP method, has no body parsing/`do_POST`, and its hand-rolled WebSocket
  discards all inbound frames. `monitor.html` never calls `ws.send`.
- Conclusion both agents reached: **Touch cannot be a view *over* sessions; it
  must be the *host* of them** (own the PTY). The sidebar can list foreign
  sessions read-only from the registry, but clicking one cannot yield a typeable
  terminal.

## 4. Explicitly UNVERIFIED (do not treat as settled)

- Whether `journal.agentId` is byte-identical to the `agentControllers` map key
  used by `skipWorkflowAgent`/`retryWorkflowAgent`. No workflow has ever run on
  this machine; `~/.claude/projects/*/*/subagents/workflows/` is empty.
- Whether `messagingSocketPath` / the UDS inject path
  (`{type:"user", message:{…}, priority:"next", from:"uds:…"}`) is ever present
  for `kind:"interactive"` sessions. It is absent from this session's file.
- The `~/.claude/todos/` filename format (dir empty in this sandbox).

## 5. Environment constraints that bind any design

- Runs in a sandbox. Ports are NOT reachable from the host until the user runs
  `sbx ports <sandbox-name> --publish 8931:8931/tcp`; services must bind
  `0.0.0.0`/`::`, not `127.0.0.1`. Outbound network is firewalled (default-deny,
  HTTP 403 with a structured body).
- The existing monitoring module is deliberately **zero third-party
  dependency** (bash + Python 3 stdlib + browser) and works behind the egress
  proxy. Any proposal to adopt an npm/PyPI stack must justify the change against
  that precedent AND against the firewall, not assume installs work.
- `.claude/shared/monitoring/` is stateless and task-agnostic; per-run state
  lives in `.claude/local-orchestrators/<task>/`. Completed task folders are
  history and must never be deleted.
- Tests are stdlib-only, no pytest: each `tests/test_*.py` is run directly with
  `python3` and exits non-zero on failure.

## 6. What the synthesized plan must deliver

A single ordered implementation plan for Touch (aggregator + touch-visual) that:
1. decides the process-ownership question (host vs observe) explicitly, with the
   consequence for every UI affordance;
2. specifies the read pipeline (what is watched, how it is indexed, how live vs
   historical differ) precisely enough to implement without re-research;
3. states honestly which of start/pause/restart/terminate ship in v1, by what
   mechanism, and what each actually does to a running loop;
4. names the technology stack and justifies it against the sandbox/firewall and
   the zero-dependency precedent;
5. is partitionable by file ownership by `implement-plan`'s divider without
   further research.

---

## 7. LATE-BREAKING: decisions the driver requires the plan to make explicitly

Added after the research agents launched (they never saw it). The driver and the
user worked these through during the run; the plan MUST rule on each.

### 7.1 Canonical location of Touch-owned per-agent state

An agent cannot hold its own state: a subagent IS its transcript, and the only
in-memory per-agent objects (`taskRegistry` entry, `abortController`,
`agentControllers` map) are private to the CLI process and die with it. State
that a UI must read or a later attempt must consume therefore has to live
outside the agent. Existing mechanisms, in the order this repo already uses
them: (1) orchestrator-script variables — reconstructed by deterministic replay
on `resumeFromRunId`, never restored, which is why `Date.now()`/`Math.random()`
are banned in workflow scripts; (2) files in the task folder — the mandated
findings handoff, where attempt N>1 receives FILE PATHS, not inlined findings;
(3) structured output as the `agent()` return value.

DECIDE: the canonical location and format of Touch-owned per-agent state.

### 7.2 Do NOT write into the harness's own transcripts

Appending custom records to `~/.claude/.../<sessionId>.jsonl` or
`agent-<id>.jsonl` is mechanically possible — the files are `-rw-------` and
owned by us, the CLI holds no persistent fd on them (verified: `/proc/622/fd`
has no transcript entry, so it opens/appends/closes per flush), and the format
already mixes non-message record types, so readers filter on `type`. It is
still rejected for three reasons: batched writes via
`appendToFile`/`scheduleDrain`/`drainWriteQueue` take no lock and a large batch
can split across multiple `write()` calls, so a foreign append can land inside
one; that file is the resume source, so a record with a KNOWN type would be
replayed into the model's context and a torn line breaks the user's real
session; and the format is private and version-pinned to 2.1.220, established
only by reading a minified binary.

DECIDE: confirm or overturn this, and state where Touch's writes go instead.

### 7.3 Overlay format and join key (the user's proposal)

Proposal: a separate append-only JSONL overlay that maps Touch's own entries
onto records in the harness's transcripts. Driver-verified evidence for the key:

- `uuid` is present on exactly the message-bearing records — measured
  `{assistant:135, user:83, attachment:14, system:8}` = 240 records, all 240
  unique — and had ZERO overlap with the 239 uuids across sibling subagent
  transcripts. It is a global primary key requiring no namespacing.
- Records WITHOUT `uuid`: `mode`(22), `permission-mode`(22), `last-prompt`(21),
  `ai-title`(17), `file-history-snapshot`(9), `file-history-delta`(4),
  `queue-operation`(6) — all last-writer-wins metadata, not annotation targets.
- Line number / byte offset must NOT be identity: `/clear` splits one logical
  run across sibling session dirs, so the same message exists in two files at
  different line numbers (`decision_watcher.py` already globs the copies and
  dedupes by `message.id`). Offset stays a tailing cursor only.
- Liveness gap: `uuid` does not exist until the record is flushed (batched, per
  completed message, ~5s). Keys known AT SPAWN are `agentId` (`.meta.json`) and
  `toolUseId` (parent's `tool_use` block), so the ref must be a union of
  `{uuid} | {toolUseId} | {agentId}`.

DECIDE: the exact overlay record schema; the ref union; whether one entry per
ref is last-writer-wins or an event log; and whether this overlay SUBSUMES the
existing `events.jsonl` (it is that stream plus a `ref` field) or coexists with
it. Shipping two overlapping append-only formats is not acceptable.
