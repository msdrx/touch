# Findings disposition register

**Item:** R-06 (normative plan), per GD-17. Generated 2026-07-26 and kept
current by `tests/test_register.py`, which fails if any finding under
`.claude/local-orchestrators/*/findings/research-*.md` is missing here,
listed twice, or listed but nonexistent.

**344 findings** across 20 research reports and four corpora.

Scope: the four **research corpora** (`findings/research-*.md`). The gate
reports beside them (`sp-*-test-attempt-*.md`, `sp-*-critique-attempt-*.md`)
are implementation-loop artifacts, not findings — checked: they contain zero
id-shaped headings, and `test_register.py` scans the research reports for
that reason.

## How to read a disposition

- `→ R-nn`, `→ GD-n`, `→ Tn`, `→ Dn` — the plan sections that cite this
  finding by id. These are **derived**: the register is built by scanning the
  three plans for each id, so a disposition cannot drift from the plan text
  that justifies it. Several sections per finding is normal (a global
  decision plus the items that carry it).
- `merged (alias kept)` — the same defect was found from several
  perspectives; the plan keeps every id as an alias so no report's numbering
  goes dead. See the alias list below.
- `recorded discard` — considered and rejected, with the reason recorded in
  the plan's discard register so it is not re-proposed.
- Free prose — one of the **56** hand-written dispositions: the
  findings no plan cites by id, plus the whole `touch-repo-recon:SKILLS-n`
  set, whose ids collide with a different corpus and therefore cannot be
  dispositioned by an id scan at all. Each was read and decided by hand;
  "superseded" always names what superseded it, and "fixed this pass"
  names the item that fixed it.

Nothing here is silently dropped. A finding with no owning item says so.

## Namespace collision (read before citing a bare id)

`SKILLS-n` exists in **two** corpora: `touch-repo-recon/research-skills-`
(17 findings) and `touch-full-recon/research-skills-` (16). They are
different findings with the same ids. Always qualify:
`touch-full-recon:SKILLS-1`. The plans' prose says "prior SKILLS-…" when it
means the repo-recon set; this register keeps them in separate tables and
`test_register.py` matches on the `(task, id)` pair, never the bare id.

## Aliases (one defect, several ids — all kept live)

The load-bearing ones, restated from the plans so a reader of this file
alone can follow a citation:

- **`touch-full-recon:SKILLS-1` ≡ RUNSTATE-4 ≡ PRODUCT-7** — a successful
  research plan recorded as `failed`. Forward fix R-08/R-09/R-13, legacy
  re-label R-27/R-51, scheduled and proven by **R-58**. `touch-repo-recon`'s
  INTENT-5 is the same defect seen a run earlier.
- PRODUCT-2 ≡ AUDIT-14 ≡ RUNSTATE-10 (gitignore/`.touch/`) → R-01
- PRODUCT-3 ≡ RUNSTATE-11 (zero commits) → R-02
- PRODUCT-5 ≡ RUNSTATE-1 (the `omnigent` falsehood) → R-05
- PLANS-9 ≡ AUDIT-8 (stash checkpoint) → R-35
- PLANS-10 ≡ AUDIT-9 (marker anchoring) → GD-9, R-13, R-18
- PLANS-8 ≡ AUDIT-3 (hook hot-reload unprobed) → R-04, GD-19, R-36 —
  **now probed**: `touch-full-recon/report/probes.md`
- MONITORING-3 ≡ SKILLS-13 ≡ SKILLS-14 (event source vs stoppability) → GD-8, R-20
- MONGOSCHEMA-6 ≡ CUSTOMSTATE-4 ≡ LIVEFLOW-2 (sub-document `_id`) → GD-24, R-43
- MONGOSCHEMA-2 ≡ SESSIONJSONL-9 ≡ LIVEFLOW-4 (token idempotency) → GD-25, R-50
- MONGOSCHEMA-10 ≡ LIVEFLOW-19 ≡ CUSTOMSTATE-13 (unauthenticated mongod) → GD-27, R-42
- MONGOSCHEMA-3 ≡ LIVEFLOW-1 (change streams) → GD-22, recorded discard

## touch-repo-recon

First recon (2026-07-24). Its own plan is history; substance re-verified by the later corpora.

### `research-intent-attempt-1.md` — 16 findings

| id | finding | disposition |
|---|---|---|
| `INTENT-1` | README promises "pause", which the research proved cannot exist; CLAUDE.md repeats it verbatim | **fixed this pass** (R-05/R-33) — README's verb table now carries `pause`'s real status and CLAUDE.md no longer repeats the promise. Alias of PRODUCT-4 → GD-4. |
| `INTENT-2` | CLAUDE.md points a fresh session at the skills and never at the plan, inception, or the two new skills | **fixed this pass** (R-05) — CLAUDE.md now opens with the GD-3 authority ladder (amendment → normative plan → design law → inception → README) and names `touch-orchestrate`. |
| `INTENT-3` | CLAUDE.md's inventory of the repo is factually wrong | **fixed this pass** (R-05) — CLAUDE.md carries a true inventory (`aggregator/`, `touch-visual/`, `tests/`, `docs/`) instead of "no application source yet". Alias of PRODUCT-1. |
| `INTENT-4` | Both CLAUDE.md and inception.md claim the task folders are carried-over `omnigent` history; they are this repo's ow… | **fixed this pass** (R-05) — the carried-over-history claim was false and is retracted in both files; every `orch-config.json` names a `wf_dir` under this repo's own project slug. Alias of PRODUCT-5 ≡ RUNSTATE-1. |
| `INTENT-5` | inception says the research run is "complete"; the only machine record says it FAILED, and the cause is systemic | → R-58 (with R-08/R-09/R-13) — the machine record said FAILED because the watcher fabricated `plan failed` on every verdict-less fan-out; forward fix plus the read-time re-label, and `inception.md` §7 now states run status honestly. Alias of PRODUCT-7 ≡ SKILLS-1 ≡ RUNSTATE-4. |
| `INTENT-6` | `.gitignore` has no `.touch/` entry, in a repo with zero commits | → R-01 — **fixed** (sp-01 this pass): `.gitignore` carries `.touch/`, `.touch*/`, `*.pid`, `.claude/settings.local.json`, the watcher checkpoints and the Mongo entries, with a negative guard that the run streams stay tracked. Alias of PRODUCT-2 ≡ AUDIT-14 ≡ RUNSTATE-10. |
| `INTENT-7` | CLAUDE.md tells the reader to bind Touch to 0.0.0.0 on port 8931, with no token and no mention of 8932 | **fixed this pass** (R-05/R-33) — the two serve blocks are labelled and separated (legacy monitor 8931, Touch 8932), the bind default is 127.0.0.1 with an explicit `--open` opt-in, and the per-boot token is named. Alias of PRODUCT-8 → GD-13. |
| `INTENT-8` | touch-orchestrate's stop mechanism cannot stop the loops README exists to control | → GD-8 (as amended) + `docs/control-semantics.md` — run-level stop exists for Workflow runs via the launch `toolUseResult.taskId`; per-agent stop exists only in the Agent-tool profile. The two granularities are rendered distinctly, so the gap is stated, not papered over. |
| `INTENT-9` | "restart" is defined two incompatible ways | → GD-4 — restart has exactly ONE meaning (re-invoke the script with `subplans_file` + `only:[ids]`); `Workflow({resumeFromRunId})` is rejected as a meaning. README, `inception.md` and `docs/control-semantics.md` all say the same thing now. Alias of SKILLS-6. |
| `INTENT-10` | The session registry lists only LIVE sessions, so the sidebar's "list of sessions" cannot come from it alone | → GD-6 + R-25/R-46 — the sidebar's session list is the union of the live registry and the historical transcript/`history.jsonl` arm, which is exactly why the historical arm exists; a registry that lists only live sessions is expected, not a defect. |
| `INTENT-11` | `~/.claude` root and the repo live under different users; the sessions dir contains a non-JSON entry | → R-25/R-46 — discovery tolerates a `lost+found` entry and zero-byte registry files, and is scoped to the cwd slug plus `.session-aliases`; the differing user/home is why `~/.claude` is a configured root, never a hard-coded path. |
| `INTENT-12` | The per-task layout in CLAUDE.md is described as fixed; one task folder does not match it and the monitor reports i… | → GD-14 (RUNSTATE-13 clause) — a task folder with only `plan/` is the recognized "plan only / never run" kind (`touch-monitor-spawn` is the specimen); empty `plan/`/`report/` dirs are normal and carry `.gitkeep` (R-02). |
| `INTENT-13` | The "deterministic" marker loses parallel-sibling identity: six researchers rendered as one agent | **superseded by GD-7** — "marker mandatory" is exactly the direction that lost parallel siblings; harness facts create nodes (full 17-hex agentId / `(runId,key,ordinal)`), markers only label them, and a missing marker degrades the label, never the node. |
| `INTENT-14` | inception's token figure for the research run understates it by ~27x | → GD-11 + R-05 — **fixed this pass**: `inception.md` §7 now carries the deduped ≈29.5 M in / 316 k out rollup and names AUDIT-13 as its source; `<runId>.json.totalTokens` is display-only forever. |
| `INTENT-15` | A watcher daemon for a finished run has been live for 10+ hours; nothing in the docs says to stop one | **fixed this pass** (R-40/R-05) — CLAUDE.md now carries "when a run ends, stop its watcher; leave state in place", the watcher self-exits on quiet + terminal complete, and GD-1's commit gate is scoped to watchers writing inside the commit path set. Alias of PRODUCT-12 ≡ AUDIT-15. |
| `INTENT-16` | CLAUDE.md omits the two `.claude/` files that shape the session itself | **fixed this pass** (R-05) — CLAUDE.md now names `.claude/settings.json` and `.claude/statusline.sh`, including the status-line-only `jq` exception. |

### `research-skills-attempt-1.md` — 17 findings

| id | finding | disposition |
|---|---|---|
| `SKILLS-1` | Workflow-spawned agents cannot satisfy touch-orchestrate's spawn discipline at all | → GD-7 + GD-8 — a Workflow agent cannot satisfy a spawn discipline it never sees: harness facts create nodes, markers only label them, and the two run profiles are kept separate rather than forced into one. Prior-corpus alias, mapped to R-20 by the normative plan. |
| `SKILLS-2` | The control loop cannot run while a workflow is awaited; every stop expires | → GD-4 + R-34 (gated) — a stop issued while the driver is inside a turn renders `pending — orchestrator busy`, never a bare `expired`; the intent state machine exists precisely because the control loop cannot run mid-workflow. Prior-corpus alias → R-20. |
| `SKILLS-3` | `control.jsonl` is declared single-writer by D5 but the skill makes it multi-writer, with a truncation hazard | → D5 (amended) + R-34 — the control file becomes **per-session and aggregator-owned**, which restores the single writer the multi-writer skill design broke. |
| `SKILLS-4` | The `.touch/` → `<task-dir>` control-file fallback is ambiguous and racy | → GD-12 + SD-8 — no silent fallback to another target, and the control-path list is read from configuration (`TOUCH_CONTROL_PATHS`, recording `pathSource`) instead of being restated in two documents that can drift. |
| `SKILLS-5` | The spawn ledger lives where D5 forbids Touch state, and is undiscoverable by the aggregator | → R-53 — the ledger stays an agent-written file (GD-29: agents never hold a client), and the aggregator discovers it through the `slots` binding, whose ledger line now carries `root` + `sessionKey`. |
| `SKILLS-6` | Marker precedence conflict: `[monitor]` is last-wins, `[touch]` is first-line, and plan T8 parses `[monitor]` ancho… | → GD-9 — ONE marker grammar settles the precedence conflict: matched per physical line, within the first 4 lines of the oldest transcript's first `user` record, `key=value` order-independent, `[monitor]` last-wins, a misplaced `[touch]` flags the node instead of renaming it. |
| `SKILLS-7` | No template emits `[touch]`; neither skill references touch-orchestrate, so every conforming run is non-conforming | → GD-6 — a run with no `[touch]` marker is simply **observed**, not broken: cooperating class requires evidence, and observed sessions get no controls. Prior-corpus alias → R-20 (gated). |
| `SKILLS-8` | Two unmapped identity systems: touch names vs `[monitor] plan=`/`stage=` | → R-53 — `slots` is the SINGLE place the name↔agentId hop happens (custom state addresses by name pre-spawn; the mirror addresses by agentId), which is exactly the missing mapping. |
| `SKILLS-9` | A stopped agent is indistinguishable from a crashed one, and silently degrades the run | → GD-10 (as amended) + R-54 — three-state read-time liveness: an agent with no result and >180 s idle renders `unknown — idle N m` and leaves the running set. Stopped and crashed are both honestly `unknown`, never `failed`. Prior-corpus alias → R-20. |
| `SKILLS-10` | Ack vocabulary has no mapping into the intent state machine | → GD-4 + R-34 — the ack vocabulary maps onto the one intent state machine (`requested / pending — orchestrator busy / sent / confirmed / failed(reason)`); an unmapped ack is `failed(<reason>)`, never silence. |
| `SKILLS-11` | The skill flatly denies pause; the README requires it and the plan ships it (T15) | → GD-4 — settled in one direction: pause is deferred and is **not rendered** anywhere until the hook gate ships. README, `CLAUDE.md`, `docs/control-semantics.md` and the skill now agree (this pass, R-05/R-33). |
| `SKILLS-12` | `<task-dir>` is undefined for the ad-hoc spawns the skill claims to cover | → R-34 + SD-8 — the per-session control file is defined by session key, not by a `<task-dir>` that ad-hoc spawns do not have; until R-20 lands the path list is configuration. |
| `SKILLS-13` | The description→name parse rule is unspecified (em dash, free text) | → GD-9 + R-53 — names come from the marker's `key=value` grammar; the Agent-tool `description` is corroborating bind evidence (`boundBy`), never the parser's input, so no em-dash rule is needed. |
| `SKILLS-14` | Attempt-number authority is split between the workflow and the control plane | → GD-7 + R-53 — attempts are a label layer, and `attempt` is part of the slot key (`slot:<sessionKey>\|<root>\|<name>\|<attempt>`); the workflow remains the only authority that increments it. |
| `SKILLS-15` | Adopting background Agent-tool spawns silently disables the deterministic monitoring both skills mandate | → GD-8 — the two profiles are ingested side by side, so a background Agent-tool spawn is monitored by its ledger + transcript rather than by a journal. R-04 probe 5 (2026-07-26) confirms a `run_in_background` spawn writes a full transcript with `message.usage` rows. |
| `SKILLS-16` | Background spawns lose the typed structured output the gated loops branch on | → GD-8 — recorded as the reason the gated loops stay on the Workflow profile: the gates branch on typed structured output, which a background spawn does not return. Not a defect to fix; a constraint to respect. |
| `SKILLS-17` | Watcher comment contradicts the templates about marker placement | → GD-9 — the marker grammar is stated once, normatively, which is what the watcher comment and the templates must both match. Superseded as a standalone finding. |

### `research-v0task-attempt-1.md` — 18 findings

| id | finding | disposition |
|---|---|---|
| `V0TASK-1` | The stop path is inoperable for the exact loops Touch exists to control | → GD-8 (as amended) — the stop gap is real and is now stated as two granularities rather than one broken verb; the P-plan it critiques is superseded by the normative plan. |
| `V0TASK-2` | P6's discovery glob misses every workflow-spawned agent (verified on disk) | → GD-7 + R-26/R-49 — workflow agents are discovered from `journal.jsonl` and the `subagents/workflows/<runId>/` tree, not from a name glob. P6 is superseded. |
| `V0TASK-3` | P10 renders controls on sessions D1 forbids controlling | → GD-6 — controls exist only for owned/cooperating classes and every control route 403s for observed sessions; v0 renders no control at all (R-32). |
| `V0TASK-4` | G6/P7's state machine has no terminal state for two of the skill's three ack results | → R-34/R-35 (gated) + GD-4 — the intent state machine is restated with terminal states for every ack, including `failed(<reason>)`; prior-corpus alias mapped to R-20/R-34 by the normative plan. |
| `V0TASK-5` | Control intents carry no scope and no identity | → R-34 (gated) + R-53 — control intents carry scope and identity through the `slots` name↔agentId binding and the per-session control file; the item stays blocked on the R-04 probe branch, now recorded in `report/probes.md`. |
| `V0TASK-6` | `TOUCH_STATE_DIR` silently splits the aggregator and the orchestrator | → D5 + GD-15 — `.touch/` has exactly one owner (`store.py`) and `TOUCH_STATE_DIR` selects one root for the whole process; the split it warns about cannot happen with a single owner. |
| `V0TASK-7` | Hook-spool ingestion has no owning source file | → R-36 (gated) — the hook pack owns its own spool file and its delivery mode is decided by the R-04 probe, which now reports hot-reload working; nothing spooled without an owning module. |
| `V0TASK-8` | No module interface contract across P2–P9, and no owner for the in-memory model | → GD-15 — one file, exactly one owner, with the module list stated in the plan and enforced by the sub-plan partition; the in-memory model is `server.py`'s `ReadModel` fed by the single reducer (GD-23). |
| `V0TASK-9` | The auth token has no specified transport, across a sub-plan boundary | → GD-13 + R-30 — the per-boot token is written to `.touch/server.json` (0600) and printed once in the URL; every route but `/health` compares it with `hmac.compare_digest`. |
| `V0TASK-10` | P5 cites the wrong lines for "monotonic clamps"; following the citation yields no clamping | → GD-20 (copy-verbatim list) — monotonic token deltas clamped ≥0 with message-id dedup is copied as a named invariant instead of by line citation, so a stale line number cannot lose it. |
| `V0TASK-11` | P10's "escape-first" citation points at a no-escaping-at-all pattern | → GD-20 + R-32 — escape-first rendering is a named invariant with a source guard in `tests/test_touch_frontend.py`, not a citation into another file. |
| `V0TASK-12` | `seq` is per-session in P2, global in G4, and ambiguous in P9's API | → GD-11 — `seq` is per event-log file (per stream) and a cursor is `(stream, seq)`; a bare seq is never a valid cursor. The ambiguity is closed by decision, and `{stream:1,seq:1}` is unique in Mongo (GD-24). |
| `V0TASK-13` | P1 omits `aggregator/__init__.py` and any import convention | → GD-15 + R-22 — `aggregator/__init__.py` exists and the package is imported as `aggregator.<module>`; `tests/run_all.sh` runs every file with the repo root on the path. |
| `V0TASK-14` | `.touch/server.json` has no defined shape and a circular writer | → GD-13 + R-30 — `.touch/server.json` has a defined shape (`token`, `url`, `host`, `port`, `pid`), mode 0600, written by the server after it binds, so there is no circular writer. |
| `V0TASK-15` | The description→name parse rule is unspecified and the delimiter is an em dash | → GD-7/GD-9 — names come from the `[touch]`/`[monitor]` markers' `key=value` grammar, not from parsing a description's punctuation; the Agent-tool `description` is corroborating evidence (`boundBy`), never the parser's input (R-53). |
| `V0TASK-16` | G3's `fileHint` and G2's document shapes are decisions no item implements | → R-48 (spawn `fileHint`, validated against `(st_dev,st_ino,size)` and stale-marked on mismatch) + GD-24 (the one normative collection table). Both decisions now have owning items. |
| `V0TASK-17` | P3's transcript-path resolution cites a session-dir union but describes slug derivation | → R-25/R-46 — transcript paths resolve through the cwd slug plus every slug in `.session-aliases`, and the session-dir union is the rotation-glob rule; the two are stated separately so neither implies the other. |
| `V0TASK-18` | P12 adds a second root README; and the CLAUDE.md edit needs a stated boundary | → GD-3 — ONE README; P12's `README-touch.md` is dropped, and the CLAUDE.md boundary is stated (session guide, bottom of the authority ladder). |

## touch-aggregator

Six-perspective research → the design law `touch-aggregator-plan.md` (D1–D14, T1–T23).

### `research-agentgraph-attempt-1.md` — 17 findings

| id | finding | disposition |
|---|---|---|
| `AGENTGRAPH-1` | Journal entries have no timestamps, and journal ORDER is not spawn order | → T8, D8.1 |
| `AGENTGRAPH-2` | `label` and `phase` given to `agent()` are NEVER written to disk; workflow node names are a convention, not harness… | → T19 |
| `AGENTGRAPH-3` | One logical node can emit MANY `started` entries with different agentIds (stall / throttle / user-retry), and the e… | → D3 |
| `AGENTGRAPH-4` | The journal `key` is a content hash; identical prompts collide, and a resumed run replays cached agents with NO jou… | → D7 |
| `AGENTGRAPH-5` | Queued-but-not-yet-started agents have ZERO on-disk footprint, and the concurrency cap is CPU-derived | → D13 |
| `AGENTGRAPH-6` | The rich node record exists only in memory and is flushed exactly ONCE, at run completion — a live graph cannot use it | → D8.1 |
| `AGENTGRAPH-7` | Transcript and snapshot paths are keyed to the CURRENT session id, so `/clear` or `/compact` mid-run splits one run… | → T8 |
| `AGENTGRAPH-8` | `toolUseResult.totalTokens` is the LAST API call's usage, not a per-agent rollup — using it under-reports by >10x | → D8.1 |
| `AGENTGRAPH-9` | There is no completion marker inside a subagent transcript; liveness is a three-state problem, not a boolean | → D6 |
| `AGENTGRAPH-10` | `spawnDepth` does not encode workflow nesting; the parent edge for a workflow agent exists only as a directory name | → GD-7 — `spawnDepth` is 1 for both direct and workflow subagents and never encodes nesting; node identity is `(runId,key,ordinal)` / full agentId, and the parent edge comes from `toolUseId`. Recorded in `inception.md` §3. |
| `AGENTGRAPH-11` | The session→workflow edge exists in exactly one place: a single `toolUseResult` in the parent transcript | → D3 |
| `AGENTGRAPH-12` | The prior art's per-agent labels collide across parallel siblings | → T21, D13 |
| `AGENTGRAPH-13` | Agent ids are truncated to 8 chars in the event stream, breaking the join back to the transcript | → T11, T21, D3 |
| `AGENTGRAPH-14` | Every carried-over run's `wf_dir` points at deleted directories, and the watcher's auto-discovery will silently lat… | → T8, T20, D6, D13 |
| `AGENTGRAPH-15` | Two data sources named in the brief are empty on this machine; the graph must degrade, not crash | → T6 |
| `AGENTGRAPH-16` | The only on-disk evidence that a user terminated an agent is `stoppedByUser` in the sidecar — and workflow agents n… | → T14, D7 |
| `AGENTGRAPH-17` | What an n8n-style graph can HONESTLY show at each moment (the summary this perspective owes) | → T19, D8.1 |

### `research-control-attempt-1.md` — 17 findings

| id | finding | disposition |
|---|---|---|
| `CONTROL-1` | A loop cannot be signalled, killed or niced individually; the process is the whole session | → T14, D7 |
| `CONTROL-2` | An already-running interactive session has **no** external control transport at all | → D1, D7 |
| `CONTROL-3` | The one real, documented control channel is `--input-format stream-json`, and it supports exactly four verbs | → T14, D2 |
| `CONTROL-4` | Claude Code's own "pause" is abort-plus-checkpoint, not suspend; in-flight agent work is destroyed | → D7 |
| `CONTROL-5` | Restart/resume exists but is *same-session only* and is a **tool call**, not an API | → T14, D7 |
| `CONTROL-6` | `journal.jsonl` is the restart state, and it is shared with the monitoring watcher; editing it breaks the watcher | → T14, D4, D14 |
| `CONTROL-7` | Nothing in the harness rolls back the working tree; every restart resumes onto a dirty tree | → T14, D7 |
| `CONTROL-8` | The real external control plane is **hooks**, and an HTTP hook works (verified end to end) | → T15, D7 |
| `CONTROL-9` | Hook-based pause has hard limits: tool boundaries, a timeout ceiling, and `deny` is visible to the model | → T15 |
| `CONTROL-10` | Hooks are session-scoped configuration: Touch must install them before the session starts | → T10 |
| `CONTROL-11` | `SIGSTOP` is a real whole-session freeze, and it is a trap | → D7, D14 |
| `CONTROL-12` | "Start" is fully achievable; three routes, and the choice is architectural | → T9, D10 |
| `CONTROL-13` | "Terminate" has two very different meanings; only one of them is safe | → T14, D7 |
| `CONTROL-14` | The private background-session IPC exists but must not be built on | → D14 |
| `CONTROL-15` | Per-agent skip/retry already exist — with no transport out of the TUI | → D7 |
| `CONTROL-16` | `TaskStop` and `Workflow` are model-facing tools; any Touch verb built on them is advisory, not deterministic | → T14, D7, D13 |
| `CONTROL-17` | What is semantically impossible (state this in the plan, don't discover it in sprint 3) | → T14, T15, T23, D7 |

### `research-liveio-attempt-1.md` — 20 findings

| id | finding | disposition |
|---|---|---|
| `LIVEIO-1` | There is no live channel into an *already running* interactive session; Touch must own the process | → T9, D1 |
| `LIVEIO-2` | A real PTY multiplexer exists and is directly invocable: this is the terminal page | → D14 (recorded rejection) — `--bg-pty-host` IS invocable standalone, and is rejected anyway: it is a private, version-coupled interface. The PTY tier is stdlib `pty.openpty()` + `Popen(start_new_session=True)` (T9/T13, deferred). Recorded so it is not re-hunted. |
| `LIVEIO-3` | The PTY socket streams output to *anyone* who connects; only input is token-gated | → T3, T13, D9 |
| `LIVEIO-4` | The PTY channel is a private, version-coupled interface | → D14 |
| `LIVEIO-5` | The supported live channel is SDK streaming; it also carries a real control protocol | → D2 |
| `LIVEIO-6` | 31 hook events exist; they are the only push channel that works for sessions Touch did not spawn | → T10, D6 |
| `LIVEIO-7` | Hooks are strictly blocking; a slow hook stalls the user's session (default timeout 600 s) | → T10, D6 |
| `LIVEIO-8` | `MessageDisplay` is a real streaming-text hook but runs `forceSyncExecution` on the render path | → D14 |
| `LIVEIO-9` | "Pause" does not exist in any channel; the README control set must be redefined before it is built | → T14, T15, T19, D7 |
| `LIVEIO-10` | Live per-subagent attribution is fully solved by hooks (and only by hooks) | → T10, D2 |
| `LIVEIO-11` | Transcript tailing is message-granular and 1–5 s late; it cannot back a terminal view | → T18 |
| `LIVEIO-12` | The session registry has no heartbeat: `status` is stale by design | → T6, D6 |
| `LIVEIO-13` | Subagent content is not in the parent transcript, and workflow agents carry no role in any on-disk record | → T8, T10 |
| `LIVEIO-14` | Remote Control is a cloud relay, not a local channel; do not build on it | → D14 |
| `LIVEIO-15` | Background agents already expose a structured loop-state plane Touch can read for free | → GD-8 — that plane is exactly the Agent-tool profile's deterministic source (spawn ledger + the launch `toolUseResult` + transcripts). Re-measured 2026-07-26 by R-04 probe 5: a background spawn writes a full transcript with `message.usage` rows, and its task id is the 17-hex agentId. |
| `LIVEIO-16` | The live channels are unix sockets and localhost files; the browser is on the other side of a sandbox boundary | → T4, T23, D8.1 |
| `LIVEIO-17` | Hook observability differs by mode, which will make Touch's two pages disagree | → T10, D6 |
| `LIVEIO-18` | The same event arrives on up to three channels at different times; define the dedup key now | → D3 |
| `LIVEIO-19` | A killed run leaves `started` with no `result` forever; liveness must be inferred | → T8, D6 |
| `LIVEIO-20` | `CLAUDE_PTY_RECORD` writes a private binary format; tee the frames yourself instead | → T9, D5, D14 |

### `research-priorart-attempt-1.md` — 18 findings

| id | finding | disposition |
|---|---|---|
| `PRIORART-1` | The transport is one-directional by construction; there is no place to put a control command | → T3 |
| `PRIORART-2` | Zero authentication, no Origin check, no Host check, binds 0.0.0.0 — safe for a read-only dashboard, fatal the mome… | → T4, D9 |
| `PRIORART-3` | The unit of aggregation is a repo-local task folder, not a Claude Code session | → T6, T11, T16 |
| `PRIORART-4` | Only Workflow-tool agents carrying the `[monitor]` marker are visible; ordinary session subagents are invisible | → T8 |
| `PRIORART-5` | Parallel fan-out is unrepresentable: six live agents render as six identical rows and collapse into ONE graph node | → T8, T19, T21, D13 |
| `PRIORART-6` | The event schema is a flat state overlay; it cannot express a graph, and it has no version field | → T5, T12, T19, T21 |
| `PRIORART-7` | The watcher fully re-parses every running agent's entire transcript once per second (O(size) per tick, and size gro… | → T7, D11 |
| `PRIORART-8` | Full replay from byte 0 on every connect, one WS frame per event, no log trimming — and the UI offers a 500 ms full… | → T12, T16 |
| `PRIORART-9` | The watcher is structurally blind to control actions, so a paused/stopped run will be reported as a completed one | → T14, D7, D13 |
| `PRIORART-10` | "Restart" is `resumeFromRunId` cache replay into a NEW journal, and the checkpoint model turns that into duplicated… | → T8, D3 |
| `PRIORART-11` | One watcher process per run, with all run identity in module-level globals — the module cannot grow to N sessions | → D11 |
| `PRIORART-12` | An unknown `?task=` silently streams a different task's data | → T4, T12, T20, D9 |
| `PRIORART-13` | Catch-all route returns the dashboard with 200 for every unknown path; HTTP method is never parsed | → T4 |
| `PRIORART-14` | `events.jsonl` is multi-writer and its safety rests on an undocumented "one `write()` per record" invariant | → T1, T5, T21, D4 |
| `PRIORART-15` | Reserved magic names live in the same flat namespace as user data | → T5, T20, D4 |
| `PRIORART-16` | `classify()` sleeps inside the single poll thread; a fan-out can stall the whole watcher for seconds | → D11 |
| `PRIORART-17` | The network layer has no tests at all, and the frontend is tested by grepping its own source | → T22, D12 |
| `PRIORART-18` | What to reuse verbatim, and what to generalize (consolidated recommendation) | → T16, D11 |

### `research-sessiondata-attempt-1.md` — 20 findings

| id | finding | disposition |
|---|---|---|
| `SESSIONDATA-1` | `usage` is duplicated on every split assistant record; naive summing over-counts tokens 2.09x | → T7, D3 |
| `SESSIONDATA-2` | thinking text is never persisted: every `thinking` block on disk has `thinking: ""` | → T18, D2 |
| `SESSIONDATA-3` | the transcript is **not** append-only: the CLI truncates and whole-file-rewrites it. Byte-offset tailing will break | → T7, D3, D6 |
| `SESSIONDATA-4` | `mode`, `permission-mode`, `ai-title`, `last-prompt` etc. are re-appended state, not events | → T7, D6 |
| `SESSIONDATA-5` | writes are batched (100 ms) into a single large append; torn tails are guaranteed, and latency is ~100 ms | → T7, D6 |
| `SESSIONDATA-6` | `/clear` starts a **new sessionId and a new file** under the same pid; and the transcript file is created lazily | → T6, D3, D10 |
| `SESSIONDATA-7` | `~/.claude/sessions/<pid>.json` is not a heartbeat and is written non-atomically | → T6, D6 |
| `SESSIONDATA-8` | there is no session-end record in the transcript | → T6, T10 |
| `SESSIONDATA-9` | an async `Workflow` run has **no** completion record in the parent transcript | → T8 |
| `SESSIONDATA-10` | the workflow journal has no timestamps, and its `result` payload is not JSON | → T8, D8.1 |
| `SESSIONDATA-11` | workflow subagent `.meta.json` omits `description`/`toolUseId`; the only label is the `[monitor]` prompt marker | → T8 |
| `SESSIONDATA-12` | a faithful terminal replay is not reconstructible; the built-in asciicast recorder is present but not reachable | → T17, D2, D14 |
| `SESSIONDATA-13` | retention deletes both the transcript and the whole subagent tree | → T5, D5 |
| `SESSIONDATA-14` | one content block per record: UI grouping must be by `(requestId, message.id)` | → T7, T18 |
| `SESSIONDATA-15` | transcripts contain unredacted file contents, command output and prompts; serving them is an exfiltration surface | → T4, T11, T23, D9 |
| `SESSIONDATA-16` | `progress` records are in the schema but never appeared on disk; do not build a live tool-progress pane on them | → D14 |
| `SESSIONDATA-17` | hooks, not polling, are the deterministic push channel; the plan should budget for them | → T10, D6 |
| `SESSIONDATA-18` | `~/.claude.json` is the wrong place to read cost or the current session, and a dangerous place to write | → D4, D14 |
| `SESSIONDATA-19` | file-history gives real pre-edit contents, but is keyed by sessionId and dies with it | → T7, T18 |
| `SESSIONDATA-20` | the prompt queue is visible (`queue-operation` + `attachment/queued_command`), but is `last-wins` | → T7, T18 |

### `research-stack-attempt-1.md` — 18 findings

| id | finding | disposition |
|---|---|---|
| `STACK-1` | No local control channel exists on a running CLI process; Touch must OWN the PTY it wants to drive | → D1 |
| `STACK-2` | `CLAUDE_CODE_CHILD_SESSION` is inherited and silently disables transcript persistence in every session Touch spawns | → T9, D10 |
| `STACK-3` | The existing server has no auth and no `Origin` check; bolting a PTY onto it is remote code execution | → T4, D8.1, D9 |
| `STACK-4` | The current WebSocket implementation is unidirectional by construction | → T3 |
| `STACK-5` | The 0.5 s poll loop is a hard latency floor that a terminal cannot live with | → T9, T13, D8.1 |
| `STACK-6` | `node-pty` cannot build here; the PTY tier must be Python | → T9, D14 |
| `STACK-7` | "terminate" and "pause" have no correct signal-level implementation; SIGTERM does not kill an interactive TUI | → T9, T14, D7 |
| `STACK-8` | npm and PyPI both work here; the real constraint is *runtime* offline-safety, not install-time | → T2, D8.1 |
| `STACK-9` | The real Claude Code TUI needs a real terminal emulator; a `<pre>`-based renderer will not work | → T2, T17, D2 |
| `STACK-10` | Do not vendor a graph-layout engine; the loop DAG has a known fixed rank structure and a prototype already exists i… | → T19, D8.1, D14 |
| `STACK-11` | The two halves read from two different roots, and only one of them has any containment logic today | → T11, D9 |
| `STACK-12` | Session liveness: the registry contains ghosts, and `claude agents --json` is the supported enumeration | → T6, D3, D6 |
| `STACK-13` | Reconnect/replay semantics differ per channel and must be designed, not inherited | → T9, T12, T13, D9 |
| `STACK-14` | No inotify in the stdlib; poll, and size the poll from measurement | → D6 |
| `STACK-15` | One process, one port, no build step | → T4 |
| `STACK-16` | Test story: the existing convention does not cover a PTY, a socket, or browser JS | → T1, T22, D12 |
| `STACK-17` | Repo layout: app source must not live under `.claude/` | → T1 |
| `STACK-18` | "Pause / restart a loop" has no external API; the control plane is *typed input into an owned PTY*, and that is racy | → T14, D7 |

## touch-full-recon

Six-perspective re-recon → **the normative plan** (GD-1…GD-20, R-01…R-37).

### `research-audit-attempt-1.md` — 17 findings

| id | finding | disposition |
|---|---|---|
| `AUDIT-1` | 51 findings from `touch-repo-recon` have no disposition in any plan; both plans predate them | → R-06, GD-17 |
| `AUDIT-2` | D8's normative rule "journal `result` is an opaque string, never parsed as JSON" is false on every journal on this… | → R-26, GD-11 |
| `AUDIT-3` | the one unverified item the entire v0 control story rests on (hook hot-reload) has still never been probed | → R-04, GD-19 — merged (alias kept) |
| `AUDIT-4` | the `tool-results` spill is settled, and its schema is NOT the one T7 was told to implement | → R-26 |
| `AUDIT-5` | `<runId>.json` lands under the session current at run END, not the launching session; T8's path yields ENOENT | → R-03, R-26 |
| `AUDIT-6` | `workflowProgress` mixes `workflow_phase` rows whose every field is null; back-filling from it null-wipes good labels | → R-03, R-26 |
| `AUDIT-7` | the plan's acceptance criterion depends on `~/.claude` data that the CLI is scheduled to delete, and that is alread… | → R-03, R-37, GD-18 — merged (alias kept) |
| `AUDIT-8` | D7/T14's `git stash create` checkpoint cannot run in this repo: it fails on a zero-commit repository | → R-35 — merged (alias kept) |
| `AUDIT-9` | marker parsing is specified three incompatible ways, and quoted markers in findings text make every "first occurren… | → R-13, R-18, GD-9 — merged (alias kept) |
| `AUDIT-10` | legacy ingest will faithfully reproduce the watcher's fabricated verdicts, and the plan forbids fixing them | → R-08, R-27, GD-14 — merged (alias kept) |
| `AUDIT-11` | two opposite directions for node identity were adopted a week apart and never reconciled | → R-28, GD-7 |
| `AUDIT-12` | the settling-experiment list omits the probes the control and delivery items actually rest on | → R-04, R-36 — recorded discard |
| `AUDIT-13` | `inception.md`'s token figure is the exact value of the one field D8 bans, and the docs still carry it | → R-05, R-26, GD-11 — recorded discard |
| `AUDIT-14` | the `.gitignore`/first-commit window INTENT-6 opened is still open | → R-01, GD-1 — merged (alias kept) |
| `AUDIT-15` | daemon lifecycle: the monitor the docs point at is dead, two watchers keep running, and nothing owns shutdown | → R-05, R-30 — merged (alias kept) |
| `AUDIT-16` | verification ledger: what I settled or re-confirmed today, so nobody re-runs it | → R-04 |
| `AUDIT-17` | D3's session key has no arm for historical sessions, which is most of them | → R-25 |

### `research-monitoring-attempt-1.md` — 14 findings

| id | finding | disposition |
|---|---|---|
| `MONITORING-1` | watcher dies with an unhandled traceback when `ORCH_STATE_DIR` does not exist yet | → R-07 |
| `MONITORING-2` | no authentication, no `Origin` check, binds `0.0.0.0`; Touch must not inherit this posture once it adds controls | → R-11, R-30, GD-13 — recorded discard |
| `MONITORING-3` | the watcher's only event source is the Workflow journal, which `touch-orchestrate` runs do not produce | → R-04, R-20, GD-8 — merged (alias kept) |
| `MONITORING-4` | the event model is flat; it carries no parent/child edges, so Touch's graph view is not derivable from it | → R-13, R-19, R-28, GD-7 — merged (alias kept) |
| `MONITORING-5` | an unknown `?task=` silently streams a different task's data instead of erroring | → R-11, R-30, GD-12 |
| `MONITORING-6` | concurrent appends to `events.jsonl` are unlocked and unbounded; corrupted lines are then silently dropped | → R-10, GD-11 |
| `MONITORING-7` | the dashboard re-renders every card on every event and never bounds the log, so replay cost is quadratic | → R-12, R-32 |
| `MONITORING-8` | the watcher has no journal-truncation detection, so a shrunk journal stalls it forever and silently | → R-07 — merged (alias kept) |
| `MONITORING-9` | token accounting re-parses every running agent's full transcript once per second | → R-23, R-26 |
| `MONITORING-10` | several watcher "tests" are tautologies that re-implement the logic instead of calling it | → R-16 |
| `MONITORING-11` | malformed config/env values kill the watcher at import; the server handles the same class of error cleanly | → R-07 |
| `MONITORING-12` | `/file` buffers whole artifacts in memory with no size cap | → R-11 |
| `MONITORING-13` | the markdown link whitelist admits protocol-relative `//host` URLs | → R-12 |
| `MONITORING-14` | `monitoring.md` is silent on the security posture, the `detail` length constraint, and the `watcher` stage | → R-17 |

### `research-plans-attempt-1.md` — 15 findings

| id | finding | disposition |
|---|---|---|
| `PLANS-1` | Two rival plans both claim "consumable by implement-plan as-is", with colliding file ownership and no supersession… | → GD-15 |
| `PLANS-2` | The v0 plan ships stop controls for sessions that D1 defines as read-only; the `touch-orchestrate` skill introduces… | → R-34, GD-6 |
| `PLANS-3` | G4 says "exactly as D4" but silently drops one member of the ref union; a v0 store that validates strictly will rej… | → R-24, GD-11 |
| `PLANS-4` | T11's endpoints are path-parameterised, contradicting D9.3's "no path parameters" and the P-plan's query-string API | → R-31, GD-12 |
| `PLANS-5` | T20's premise is empirically false: the archived runs' source transcripts and journals are still on disk, so "archi… | → R-27, GD-14 — merged (alias kept) |
| `PLANS-6` | Part F's acceptance criterion is unreachable through the path the plan assigns to it: the legacy stream has six ide… | → R-03, R-13, R-27, R-37, GD-11, GD-18 — merged (alias kept) |
| `PLANS-7` | The control channel has no defined address: `<task-dir>` is undiscoverable from a session, and `.touch/` is repo-ro… | → R-20, R-34 |
| `PLANS-8` | P11's hook pack has no delivery mechanism in v0, and its viability rests on an unsettled Part E experiment | → R-04, R-36, GD-19 — merged (alias kept) |
| `PLANS-9` | T14's pre-restart checkpoint silently records nothing: `git stash create` no-ops on a repo with no initial commit,… | → R-35 — merged (alias kept); recorded discard |
| `PLANS-10` | T8's anchored marker regex fails against real transcripts, and the skill's "FIRST line" rule is already violated by… | → R-18, GD-9 — merged (alias kept) |
| `PLANS-11` | `seq` scope is never defined; P2 writes it per-session while P9 offers a global cursor | → R-24, R-31, GD-11 |
| `PLANS-12` | T15's gate hook must authenticate against a per-boot token, but T10 requires the settings template to be static | → R-36 |
| `PLANS-13` | Small staleness against the current repo: files listed as "new" that exist, an ignore line already present, and two… | → R-22, R-33, GD-3 |
| `PLANS-14` | Nothing in either plan owns the `touch-orchestrate` skill, so the cooperative standard it defines is never verified… | → R-20 |
| `PLANS-15` | Sequencing: what `implement-plan` should actually receive first | → GD-18 |

### `research-product-attempt-1.md` — 13 findings

| id | finding | disposition |
|---|---|---|
| `PRODUCT-1` | CLAUDE.md, the file every fresh session reads first, points at neither `inception.md` nor either plan, and its repo… | → R-05, GD-3 |
| `PRODUCT-2` | `.touch/` is not gitignored in a zero-commit repo, and the store it names will hold unredacted transcript content | → R-01, GD-1 — merged (alias kept) |
| `PRODUCT-3` | There is no defined initial commit, and `git commit` currently cannot run at all | → R-02, GD-2 — merged (alias kept) |
| `PRODUCT-4` | README promises "pause", `inception.md` proves it cannot exist, and CLAUDE.md repeats the promise | → R-05, R-19, GD-4 — merged (alias kept) |
| `PRODUCT-5` | CLAUDE.md and inception.md both claim the task folders are foreign `omnigent` history; all four are this repo's own… | → R-05 — merged (alias kept) |
| `PRODUCT-6` | A user-directed, normative model policy exists only inside an aborted run's workflow script and is dropped from thi… | → R-21, GD-5 |
| `PRODUCT-7` | The run that produced the normative plan is recorded in its own event stream as never-completed, and a partial user… | → R-05, R-08, R-58, GD-10 — merged (alias kept); recorded discard |
| `PRODUCT-8` | CLAUDE.md's run/serve instructions contradict the security decisions in inception §5 | → R-05, GD-13 |
| `PRODUCT-9` | `.gitignore` gaps and an undecided policy on mutating per-task checkpoints | → R-01, GD-16 — recorded discard |
| `PRODUCT-10` | README.md is nominated as the product source of truth but is a 7-line stub, and the plan schedules a *second* README | → R-05, GD-3 |
| `PRODUCT-11` | `.claude/settings.json` + `statusline.sh` are undocumented committed harness config, an external `jq` dependency, a… | → R-05 |
| `PRODUCT-12` | Operational leftovers nothing in the docs tells anyone to clean up | → R-05 — merged (alias kept) |
| `PRODUCT-13` | Machine-specific absolute paths are baked into everything about to be committed | → R-05 — recorded discard |

### `research-runstate-attempt-1.md` — 18 findings

| id | finding | disposition |
|---|---|---|
| `RUNSTATE-1` | The "omnigent" claim in CLAUDE.md / inception.md / the aggregator workflow script is false | → R-05 — merged (alias kept) |
| `RUNSTATE-2` | Legacy events carry no run id and no task id; one folder's stream spans multiple script invocations | → R-27, GD-14 |
| `RUNSTATE-3` | Agent ids are truncated to 8 chars in events but stored full-length in the checkpoint | → R-13, R-27, GD-14 — merged (alias kept) |
| `RUNSTATE-4` | A fully successful run is recorded as `plan/failed` | → R-08, R-27, R-58, GD-10, GD-14 — merged (alias kept) |
| `RUNSTATE-5` | `.watcher-state.json` contradicts `events.jsonl` and is never closed on kill | → R-27, GD-14 |
| `RUNSTATE-6` | Streams are append-ordered but not timestamp-ordered, and mix two ISO formats | → R-27, GD-11 |
| `RUNSTATE-7` | Every stage completion is written twice, by two independent writers, with different details | → R-27, R-58, GD-14 |
| `RUNSTATE-8` | Agent labels are not unique, `(plan,stage)` is not unique, and orchestrator rows carry no agent at all | → R-13, R-27, GD-7 |
| `RUNSTATE-9` | Abandoned agents are never closed; the stale-close guard cannot fire on same-attempt re-spawns | → R-27, GD-14 |
| `RUNSTATE-10` | `.gitignore` has no `.touch/` entry, and `.touch/` will contain raw PTY capture | → R-01, GD-1 — merged (alias kept) |
| `RUNSTATE-11` | Nothing is committed; the "never delete this history" rule protects files that exist only in the working tree | → R-02, GD-1, GD-2 — merged (alias kept) |
| `RUNSTATE-12` | 91% of the legacy stream is per-delta token noise | → R-27, GD-14 |
| `RUNSTATE-13` | Task-folder layout is not uniform: a plan-only folder is listed as a task | → R-27, GD-14 |
| `RUNSTATE-14` | `/tasks` returns two different token shapes | → R-11, GD-11 |
| `RUNSTATE-15` | The watcher stalls silently if the journal is truncated in place | → R-07, R-23 — merged (alias kept) |
| `RUNSTATE-16` | `status.sh` validates nothing; the "no double quotes" rule is documented for the wrong reason | → R-10, R-17, GD-11, GD-14 — recorded discard |
| `RUNSTATE-17` | `stale` is documented and styled but never appears in 821 real event lines | → R-03, GD-18 |
| `RUNSTATE-18` | Empty state directories will not survive a clone | → R-02 |

### `research-skills-attempt-1.md` — 16 findings

| id | finding | disposition |
|---|---|---|
| `SKILLS-1` | A fully green `implement-plan` run is reported as FAILED; every plan whose agents return no `passed`/`approved` clo… | → R-08, R-09, R-20, R-58, GD-10 — merged (alias kept) |
| `SKILLS-2` | `execute-research` runs never emit a completion event; the orchestrator badge spins "running" forever in replay | → R-08, R-09, GD-10 — merged (alias kept) |
| `SKILLS-3` | PARALLEL sub-plan mode makes plan cards flap to "failed"; a card closed red is never reopened | → R-08, R-20, GD-10 |
| `SKILLS-4` | Divider-returned `sp.id` is unvalidated LLM text used as a filesystem path, a shell argument, and a monitoring plan id | → R-14 |
| `SKILLS-5` | No machine-readable topology exists, so Touch can only draw what has already spawned | → R-19 — merged (alias kept) |
| `SKILLS-6` | "Restart a loop" is unimplementable: sub-plan identity is re-derived by an LLM on every run and there is no re-entr… | → R-19, GD-4 — merged (alias kept); recorded discard |
| `SKILLS-7` | Critique and the final scope audit see only the LAST attempt's changed files; earlier-attempt edits escape review | → R-15 |
| `SKILLS-8` | A failed or killed implementer leaves no findings file, so the next attempt re-runs the identical prompt | → R-15 |
| `SKILLS-9` | Final-gate decision lines describe a loop shape the template does not have | → R-08 |
| `SKILLS-10` | `MAX_ATTEMPTS` is script-private; the watcher's caps come from a file nothing writes, and exhaustion emits no event | → R-09 |
| `SKILLS-11` | The documented `stage=`-omission fallback is dead against the templates' own quoting | → R-13, R-18, GD-9 |
| `SKILLS-12` | Perspective keys and sub-plan ids can silently collide with reserved stage/plan names | → R-14 |
| `SKILLS-13` | `touch-orchestrate`'s mandatory spawn discipline is unsatisfiable for the very loops it targets (re-verified live) | → R-04, R-18, R-20, GD-8 — merged (alias kept) |
| `SKILLS-14` | Stop intents cannot be polled while the driver is blocked in the Workflow call, and a stopped agent is indistinguis… | → R-20, GD-4, GD-8 — merged (alias kept) |
| `SKILLS-15` | The event stream carries no model, agent-type or phase, so Touch cannot render the very pinning the skills treat as… | → R-13 |
| `SKILLS-16` | The file-ownership isolation guard is raw string equality | → R-14 |

## touch-mongo-live

Five-perspective Mongo / live-flow research → **the amendment** (GD-21…GD-30, R-38…R-58).

### `research-convo-attempt-1.md` — 16 findings

| id | finding | disposition |
|---|---|---|
| `CONVO-1` | The binding Mongo requirements are in a DIFFERENT session; this one only re-asks them | → amendment §0.1 — the six decisive record uuids and their verbatim asks are transcribed into the plan itself, so no implementer ever has to re-read a transcript to know what was requested. |
| `CONVO-2` | Two mutually inconsistent Mongo collection sets have already been promised to the user | → GD-24 |
| `CONVO-3` | "Custom state": separate collection (user's words) vs subdocument (the answer given) is an open contradiction | → R-52, GD-28 |
| `CONVO-4` | `sessions._id = "<pid>-<procStart>"` cannot be built for 5 of the 6 sessions on this machine | → R-46, GD-24 |
| `CONVO-5` | `records._id = uuid` covers only 72% of records; 39 records in this session are byte-identical duplicates | → R-47, GD-24 — merged (alias kept) |
| `CONVO-6` | The amendment's own anchor is broken: `D8` is labelled two different decisions | → R-38, GD-21 — merged (alias kept) |
| `CONVO-7` | The Mongo discard and the stdlib pin survive only inside a *superseded* document | → R-38, GD-21 — merged (alias kept) |
| `CONVO-8` | "Separate collections for separate session datas" was asked twice and must be dispositioned in the user's own words | → R-57 — recorded discard |
| `CONVO-9` | The line-number→uuid mapping decision was made with the user and is absent from the normative plan | → R-48 |
| `CONVO-10` | Live specimen: the label-collision defect is reproducing in the very run researching it | → R-58, GD-23 — merged (alias kept) |
| `CONVO-11` | Real session records contain field names with dots and strings with NUL bytes — a verbatim BSON mirror is unsafe | → R-41, R-44 — merged (alias kept); recorded discard |
| `CONVO-12` | A Workflow run *does* have a `taskId`; the plan's "stop unavailable" for the Workflow profile is too strong | → R-49, GD-8, GD-24 |
| `CONVO-13` | Change streams were promised as "a one-flag setup"; nothing on this machine can run them yet | → R-42, R-57, GD-22 — merged (alias kept) |
| `CONVO-14` | Three orphaned `decision_watcher` processes are running; nothing ever reaps them, and GD-1 blocks committing while… | → R-40, GD-1 |
| `CONVO-15` | `queue-operation` records duplicate content already stored as `user` records | → R-47 |
| `CONVO-16` | Corpus volume and the `raw` duplication cost are undecided | → R-57 — merged (alias kept); recorded discard |

### `research-customstate-attempt-1.md` — 19 findings

| id | finding | disposition |
|---|---|---|
| `CUSTOMSTATE-1` | "custom state" is undefined in the entire normative corpus | → GD-28 |
| `CUSTOMSTATE-2` | `source` is a channel, not a trust class; nothing in touch-events-v2 separates fact from assertion | → GD-28 |
| `CUSTOMSTATE-3` | the two legacy writers are byte-identically shaped, so provenance for existing state is unrecoverable | → R-39, R-41, R-51, GD-28 |
| `CUSTOMSTATE-4` | Mongo composite refs are field-order sensitive and type-strict; the ref union breaks determinism as written | → R-43, GD-24 — merged (alias kept) |
| `CUSTOMSTATE-5` | the mirror must be upsert-only/no-delete, or the "durable record" claim is false | → R-45, GD-26 — merged (alias kept) |
| `CUSTOMSTATE-6` | writer topology is undecided; agents must never hold a Mongo client | → GD-29 |
| `CUSTOMSTATE-7` | the ref union cannot address what custom state actually attaches to | → R-43, GD-11 |
| `CUSTOMSTATE-8` | the (name, attempt) → agentId binding needs a first-class `slots` collection | → R-53 |
| `CUSTOMSTATE-9` | orphan policy: refs are forward-references by construction, and some never resolve | → R-53 |
| `CUSTOMSTATE-10` | the ledger line carries no session scope and no root; ROOT_NAME collides across sessions | → R-53 |
| `CUSTOMSTATE-11` | the control-file path on disk contradicts the plan; custom-state ingest cannot be sequenced first | → R-53 |
| `CUSTOMSTATE-12` | "migrate the existing state files" is a phantom item; the real migration surface is different | → R-27, R-51 — recorded discard |
| `CUSTOMSTATE-13` | secrets and unredacted content must have a written deny-list before the first mirror write | → R-42, GD-27 — merged (alias kept) |
| `CUSTOMSTATE-14` | custom state must be an append-only event log with a derived reduction, not mutable documents | → R-52 — merged (alias kept) |
| `CUSTOMSTATE-15` | the "never masquerade as fact" rule needs structural enforcement, not convention | → GD-28 |
| `CUSTOMSTATE-16` | user annotations are the first user-authored durable data and need their own rules | → R-52 |
| `CUSTOMSTATE-17` | restate the per-session/per-task collection discard for custom state specifically | → R-52 — recorded discard |
| `CUSTOMSTATE-18` | the `.touch/` store's fate and the exact D5/D8 amendment wording are unstated | → GD-15, GD-21, GD-22 — merged (alias kept) |
| `CUSTOMSTATE-19` | provisioning is verified; record it so the amendment is not blocked on an unproven path | → R-38, GD-21 |

### `research-liveflow-attempt-1.md` — 19 findings

| id | finding | disposition |
|---|---|---|
| `LIVEFLOW-1` | The live path must read from memory; Mongo is a write-behind mirror. Change streams are not available on the mongod… | → GD-22 — merged (alias kept) |
| `LIVEFLOW-2` | A BSON sub-document `_id` silently defeats deterministic persistence; the ref-union shapes in GD-11 are exactly the… | → R-43, GD-24 — merged (alias kept) |
| `LIVEFLOW-3` | The `(stream, seq)` cursor becomes a COLLSCAN if it is stored inside `_id`; the indexed forms are proven | → R-44, GD-24 |
| `LIVEFLOW-4` | Token **deltas** must not be the persisted representation; mirror absolute per-`(agentId, message.id)` usage documents | → R-50, GD-25 — merged (alias kept) |
| `LIVEFLOW-5` | Never persist a derived verdict as authoritative: this run is *currently* fabricating a `research → failed` badge,… | → R-54, R-58, GD-23 |
| `LIVEFLOW-6` | Liveness must be derived at read time from `now()`, never stored | → R-54, GD-23 |
| `LIVEFLOW-7` | A backfill path that re-ingests an old journal with `live=True` writes today's clock into permanent history | → R-45 |
| `LIVEFLOW-8` | "Deterministic persistence" needs *two different* key rules, because transcripts are rewritten and `events.jsonl` i… | → GD-24, GD-26 — merged (alias kept) |
| `LIVEFLOW-9` | Mongo must never be awaited inside the ingest/poll loop, and the JSONL store must stay the primary durable log | → R-45, GD-21, GD-22, GD-30 — merged (alias kept) |
| `LIVEFLOW-10` | Publish the latency budget as acceptance numbers; the poll interval is the budget, not the database | → R-55, R-56, GD-30 |
| `LIVEFLOW-11` | Deterministic `_id` + "tolerate duplicate key" silently hides a real double-writer bug; add a writer lease | → R-45, GD-29 |
| `LIVEFLOW-12` | There must be exactly one reducer, server-side; the UI currently invents state the stream does not contain | → R-54, GD-23 |
| `LIVEFLOW-13` | One never-resulting sibling in a parallel fan-out wedges the run badge forever *and* pins a 1 Hz transcript re-read… | → R-54, GD-10 |
| `LIVEFLOW-14` | Unbounded replay-on-connect does not survive a shared history store | → R-55 |
| `LIVEFLOW-15` | The 1 Hz full-transcript re-parse scales with the run, not with the delta; carry a measured acceptance number | → R-56, GD-30 |
| `LIVEFLOW-16` | Mongo must never stamp time, and `$natural` is not an ordering | → R-44, GD-11 |
| `LIVEFLOW-17` | Backfill frames must be marked so the UI does not animate a finished run as if it were live | → R-55 |
| `LIVEFLOW-18` | "Attempt N of MAX / which stage next" has no source unless R-19's topology is mirrored; without it the UI must say so | → R-54 |
| `LIVEFLOW-19` | The mongod this environment produces has **no authentication and is published on 0.0.0.0**, while the data being mi… | → R-42, GD-21, GD-27 — merged (alias kept) |

### `research-mongoschema-attempt-1.md` — 20 findings

| id | finding | disposition |
|---|---|---|
| `MONGOSCHEMA-1` | 8.8 % of transcript records carry **no `uuid`**; the D3/GD-11 "records keyed by uuid" identity has no arm for them,… | → R-47, GD-24, GD-26 — merged (alias kept); recorded discard |
| `MONGOSCHEMA-2` | `output_tokens` **grows** across the split records of one `message.id`; `inception.md:78` states the opposite, and… | → R-38, R-50, GD-25 — merged (alias kept) |
| `MONGOSCHEMA-3` | Mongo cannot be the live event bus for a standalone deployment: change streams need a replica set (verified failure… | → GD-22 — merged (alias kept) |
| `MONGOSCHEMA-4` | pymongo's default `serverSelectionTimeoutMS` is 30 s; a dead Mongo freezes the 250 ms poll loop for 30 s per tick | → R-45, R-56, GD-21, GD-30 — merged (alias kept) |
| `MONGOSCHEMA-5` | Adopting Mongo does **not** require breaking D8 (stdlib-only runtime); a ~90-line stdlib wire client was proven wor… | → GD-21 — merged (alias kept); recorded discard |
| `MONGOSCHEMA-6` | compound sub-document `_id`s are **field-order sensitive**: `{s,n}` and `{n,s}` insert as two distinct documents | → R-43, GD-24 — merged (alias kept) |
| `MONGOSCHEMA-7` | legacy `events.jsonl` contains byte-identical duplicate lines and dozens of duplicate timestamps; any content- or t… | → R-51, GD-14, GD-24 |
| `MONGOSCHEMA-8` | real transcript records contain **dotted field names** (filenames used as object keys); they store but are not addr… | → R-41, R-44 — merged (alias kept) |
| `MONGOSCHEMA-9` | the "same agentId in two session dirs" case is **not** two copies: they are disjoint continuations; per-file token… | → R-03, R-38, R-48, GD-25 — merged (alias kept) |
| `MONGOSCHEMA-10` | a default `docker run -p 27017:27017 mongo:7` publishes an **unauthenticated** database on `0.0.0.0`, which violate… | → R-42, GD-27 — merged (alias kept) |
| `MONGOSCHEMA-11` | the exact amendment set (this is the deliverable G2 asks for) | → R-55, R-57 |
| `MONGOSCHEMA-12` | dual-sink vs Mongo-only: measured storage cost makes dual-sink nearly free, and it is the only option that keeps "M… | → R-45, R-56, GD-22 — merged (alias kept) |
| `MONGOSCHEMA-13` | the mirror must survive the CLI's retention sweep, which means **no TTL index anywhere** and a `sourcePresent` flag… | → R-45, GD-26 — merged (alias kept) |
| `MONGOSCHEMA-14` | concrete collection + index list (the schema deliverable) | → R-44, GD-24 |
| `MONGOSCHEMA-15` | DBRef works with `$lookup` on Mongo 7 (contrary to folklore) but is still the wrong choice for the ref union | → GD-24 — recorded discard |
| `MONGOSCHEMA-16` | "re-ingest converges byte-identical" is achievable and was verified; make it the schema's acceptance test | → R-44, GD-25 |
| `MONGOSCHEMA-17` | 16 MB document limit is not a risk for records (max observed line 872 KB), but tool-result spills must stay out of… | → R-44 |
| `MONGOSCHEMA-18` | journal `(type,key)` repeats are real, so `ordinal` is required; but an in-memory ordinal counter is not restart-sa… | → R-49, GD-7 — merged (alias kept) |
| `MONGOSCHEMA-19` | the custom agent-state collection (user requirement 2): design it as an append-only journal with a derived head, no… | → R-52 — merged (alias kept) |
| `MONGOSCHEMA-20` | concurrent agents share one mongod and one database namespace; the plan needs a namespace rule before two Touch ins… | → R-42, GD-27 |

### `research-sessionjsonl-attempt-1.md` — 16 findings

| id | finding | disposition |
|---|---|---|
| `SESSIONJSONL-1` | 28 % of main-transcript records have **no `uuid`**; GD-11's `{uuid}` ref gives them no primary key | → R-47, GD-24 — merged (alias kept); recorded discard |
| `SESSIONJSONL-2` | the transcript is **not append-only**: `performRemoveByUuid` truncates in place, so an upsert-only mirror accumulat… | → R-45, GD-26 — merged (alias kept) |
| `SESSIONJSONL-3` | one agent's transcript is **split across two session directories** by `/clear`; the second fragment is stitchable o… | → R-25, R-41, R-48 — merged (alias kept) |
| `SESSIONJSONL-4` | `ordinal` in `(runId, key, ordinal)` is never defined for Workflow nodes; the empirical rule is journal occurrence… | → R-49, GD-7 — merged (alias kept) |
| `SESSIONJSONL-5` | `journal.jsonl` records carry **no timestamp**; run timing cannot come from the journal | → R-26, R-49 |
| `SESSIONJSONL-6` | `<runId>.json` is a **terminal** artifact; a live run has none, and there is one on disk right now to prove it | → R-26, R-49 |
| `SESSIONJSONL-7` | snapshot `agentCount` is the **distinct node count**, not the agent count | → R-26, R-49, GD-11, GD-24 |
| `SESSIONJSONL-8` | agent transcripts have **no terminal record**, and `started`-without-`result` does not mean running | → R-54, GD-10, GD-23 |
| `SESSIONJSONL-9` | message-id token dedup transfers unchanged, but the DB sink must make it an **upsert**, not a sum | → R-50, GD-25 — merged (alias kept) |
| `SESSIONJSONL-10` | `promptId` is per-turn, not per-agent; it is not an agent key | → GD-7 |
| `SESSIONJSONL-11` | discovery scope: the registry lists only **live** sessions, `~/.claude/projects/` contains foreign project slugs, a… | → R-25, R-41, R-46 |
| `SESSIONJSONL-12` | Mongo document sizing: max observed transcript line is 877 KB; one document per record is mandatory, with a 16 MiB… | → R-44 |
| `SESSIONJSONL-13` | the ingestion contract: source list, checkpoint identity, re-ingest triggers, ordering guarantees | → D6 (amendment §2) + R-23 — the ingestion contract it asks for IS the checkpoint identity `(st_dev,st_ino,size,offset)` plus `size < offset` shrink detection; cross-session globbing of a configured project root is not auto-discovery. |
| `SESSIONJSONL-14` | spilled tool results are **unreferenced** from the transcript; R-26's regex approach is re-confirmed and needs a di… | → R-26 |
| `SESSIONJSONL-15` | there is **no supported push/mirror hook**; file tailing is the only contract | → recorded discard |
| `SESSIONJSONL-16` | `session_id` duplicates `sessionId` on every message record | → R-47 — recorded discard |

---

*Generated by scanning the plans for every finding id; regenerate the same
way after a plan edit, then run `python3 tests/test_register.py`.*
