---
name: touch-orchestrate
description: Spawn and control subagents to Touch's standards — hierarchical names derived from one root name, background spawns for stoppability, state files, and a control-file loop — so the Touch UI can display, join, and stop every agent. Use whenever spawning subagents in a session Touch monitors, or when asked to run agents "with Touch naming".
---

# touch-orchestrate — spawn subagents Touch can see and stop

Makes every subagent this session spawns *nameable before creation*,
*joinable at spawn*, and *stoppable from the Touch UI*. It layers on top of
`execute-research` / `implement-plan` (their invariants still apply) and works
for ad-hoc spawns too. The harness `agentId` is read-only and unknowable
before spawn — these standards exist so a Touch-chosen name is always on disk
at spawn instant, joined to the `agentId` by the aggregator.

## 1. Naming standard (mandatory)

- **Root name.** Choose `ROOT_NAME` once per orchestration, before the first
  spawn — from the user's words or the task name. Slug rules: lowercase
  `[a-z0-9_]`, starts with a letter, no spaces (e.g. `auth_refactor`). The
  root agent — this session acting as orchestrator — IS `ROOT_NAME`.
- **Derivation.** Every child's name is its parent's name plus one suffix:
  `<parent>_<role><N>` — e.g. `auth_refactor_subagent1`,
  `auth_refactor_research2`. Use the agent's role as the suffix word
  (`research`, `implement`, `testgate`, `critic`, …); use `subagent` only
  when no better role exists. Nesting repeats the rule:
  `auth_refactor_subagent1_subagent1`.
- **N is a per-parent counter**: starts at 1, increments per spawn under that
  parent, never reused within the orchestration — even if an earlier agent
  failed.
- **Names are logical, attempts are physical.** A retried slot keeps its name;
  the marker's `attempt=` field distinguishes spawns. Each (name, attempt)
  pair binds to exactly one harness `agentId`.
- Tooling treats names as opaque ids (parent/root are stated explicitly in
  the marker) — the derivation is for humans reading the graph, so never
  encode extra data into the name beyond the rule above.

## 2. Spawn discipline (every spawn, no exceptions)

- **Marker first.** The FIRST line of every agent prompt is:

  ```
  [touch] name=<name> parent=<parent_name> root=<ROOT_NAME> role=<role> attempt=<N>
  ```

- **Description carries the name.** Agent-tool spawns set
  `description: "<name> — <short task>"` with the name verbatim. (Workflow
  `agent()` spawns persist no description — there the marker is the only name
  channel, which is why it is first.)
- **Background spawns.** Spawn via the Agent tool with `run_in_background`
  so each agent is a harness-tracked task that `TaskStop` can kill
  individually. Immediately after each spawn, append one line to the spawn
  ledger `<task-dir>/state/spawn-ledger.jsonl`:

  ```json
  {"name":"…","parent":"…","root":"…","role":"…","attempt":1,"taskId":"…","sessionKey":"<pid>-<procStart>","ts":"…"}
  ```

  `root` is `ROOT_NAME`. `sessionKey` is this session's own identity:
  the pid of the session process and field 22 of `/proc/<pid>/stat` (its
  start clock ticks, as a **string**), joined with `-`. The orchestrating
  session knows both, so this needs no new capability. Both fields are
  mandatory: the ledger line is the only durable record of a spawn, and
  without them two sessions that pick the same `ROOT_NAME` from
  near-identical task names address the *same* slot — one session's custom
  state then binds onto the other session's agents. Ledger lines written
  before this amendment carry neither; Touch derives `sessionKey` from the
  containing path and records `sessionKeySource:"path"` rather than
  presenting a derived value as something the writer stated.

- **Fresh agent every attempt** — never resume / continue / SendMessage a
  prior agent. Handoff between attempts is file paths, never inlined text.
- When the run also uses the existing monitoring stack, keep its
  `[monitor] plan=… stage=… role=… attempt=…` marker and `status.sh` calls as
  the second line / as templated — the two markers coexist.

## 3. State standard

- Agents that need working state write it to
  `<task-dir>/state/<name>.json` — instruct this in their prompt. Final
  results are structured output (the harness persists them).
- NEVER write under `~/.claude/` — no transcripts, no journals, no config.
  Touch-owned state lives in the task folder or `.touch/`.

## 4. Control loop (while background agents run)

- Watch `.touch/control.jsonl` (fall back to `<task-dir>/control.jsonl` if no
  `.touch/` exists) between steps — poll it whenever you check on background
  tasks, at minimum before each new spawn and after each completion
  notification.
- On `{"action":"stop","name":"<name>"}`: resolve the name to its `taskId`
  via the spawn ledger, call `TaskStop`, then append an acknowledgment line
  `{"ack":"stop","name":"…","taskId":"…","result":"stopped|not_found|already_done","ts":"…"}`
  to the same control file. Never fabricate a result — report what TaskStop
  actually returned.
- A stopped slot may be re-run only as a fresh spawn with `attempt` + 1.

## Why these exact standards (verified constraints)

- `agentId` is harness-generated at spawn; the only writable pre-spawn
  identity is the description + prompt text — hence the marker.
- Workflow-spawned agents get a stub `.meta.json` (no description) — hence
  marker-first, always.
- There is no push channel into a running session — stop only works because
  this session polls the control file and acts on it.
- The harness records nothing about stops — hence the ledger and the ack
  lines: Touch's audit is the only record.
- "Pause" does not exist in any CLI channel (the harness's pause is kill) —
  do not promise it; stop + fresh-attempt restart is the honest cycle.
