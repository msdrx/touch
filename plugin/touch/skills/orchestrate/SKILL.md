---
name: orchestrate
description: Spawn and control subagents to Touch's standards — hierarchical names derived from one root name, background spawns for stoppability, state files, and a control-file loop — so the Touch UI can display, join, and stop every agent. Use whenever spawning subagents in a session Touch monitors, or when asked to run agents "with Touch naming".
---

# orchestrate — spawn subagents Touch can see and stop

Makes every subagent this session spawns *nameable before creation* and
*joinable at spawn*, so the Touch UI can display and address it — and, when a
control verb ships, stop it (§4 is dormant until then: `CONTROL_ROUTES` is
`{}`). It layers on top of `research` / `implement` (their
invariants still apply) and works for ad-hoc spawns too. The harness `agentId`
is read-only and unknowable before spawn — these standards exist so a
Touch-chosen name is always on disk at spawn instant, joined to the `agentId`
by the aggregator.

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
- **N distinguishes siblings; it is not a count anyone maintains.** Start at 1
  and go up under each parent, never reusing a number within the orchestration
  — but nothing downstream reads N as a total, so a gap left by a failed spawn
  is fine and no running tally has to be kept in your head (D-19). The spawn
  ORDER is recorded by the harness, and the binding is done from the marker;
  the number's only job is to keep two siblings apart.
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
  individually.
- **The spawn ledger is written for you — do not hand-write it.** This skill
  mandated a `<task-dir>/state/spawn-ledger.jsonl` append after every spawn for
  months and **zero ledger lines were ever written** by any agent; an
  instruction is not a mechanism (D-19). The line now comes from the
  `SubagentStart` arm of `hooks/agent_lifecycle.py` (D-18(c), gated on the
  D-17 probe, CLI 2.1.220), built from the harness's own payload. It is
  additive, never the floor: with the hook off, the bind still happens from the
  marker (`agents.find_spawns`), which is why `marker` outranks `ledger` and why
  nothing treats a missing ledger as an error. Your part is to make the marker
  correct.

  Written for you *inside an active run*: the hook is inert with no `ACTIVE`
  sentinel in the tasks root, exactly like the run-scope guard, so an ad-hoc
  spawn outside a run gets no ledger line at all and the `[touch]` marker is
  the whole record.

  Recorded here so the hook has a spec — not so you write it — the line it
  appends is:

  ```json
  {"w":"hook","agentId":"…","name":"…","parent":"…","root":"…","role":"…","attempt":1,"taskId":"…","sessionKey":"<pid>-<procStart>","ts":"…"}
  ```

  `name`, `parent`, `root`, `role` and `attempt` are read out of the `[touch]`
  marker above; `root` is `ROOT_NAME`. `sessionKey` is the orchestrating
  session's own identity — the pid of the session process and field 22 of
  `/proc/<pid>/stat` (its start clock ticks, as a **string**), joined with `-`
  — so it is a *stated* identity rather than one derived from a path. `name`,
  `root`, `attempt` and `sessionKey` are mandatory: without them two sessions
  that pick the same `ROOT_NAME` from near-identical task names address the
  *same* slot, and a line missing any of them is skipped rather than guessed.
  `parent` and `role` are omitted when the marker carries neither. `taskId` is
  recorded only for Agent-tool spawns, where the task id IS the `agentId`; a
  Workflow subagent has no per-agent stop handle, so the field is absent by
  design rather than empty. Lines written before this amendment carry neither
  `root` nor `sessionKey`; Touch derives the session from the containing path
  and records `sessionKeySource:"path"` rather than presenting a derived value
  as something the writer stated.

- **Fresh agent every attempt** — never resume / continue / SendMessage a
  prior agent. Handoff between attempts is file paths, never inlined text.
- When the run also uses the monitoring stack (the `monitor` skill),
  keep its `[monitor] plan=… stage=… role=… attempt=…` marker as the second
  line — the two markers coexist, and each is read by a different reader. That
  skill mandates no `touch-status` call in a prompt any more (D-09); do not
  add one back here.

## 3. State standard

- Agents that need working state write it to
  `<task-dir>/state/<name>.json` — instruct this in their prompt. Final
  results are structured output (the harness persists them).
- NEVER write under `~/.claude/` — no transcripts, no journals, no config.
  Touch-owned state lives in the task folder or `.touch/`.

## 4. Control loop — DORMANT until a control verb ships

**Do not poll for control intents. Nothing produces them.** Touch ships no
session-control verb: `aggregator/server.py`'s `CONTROL_ROUTES` is `{}` by
design (GD-4), so `.touch/control.jsonl` has no writer and the polling this
section used to mandate spent a read on every step to find a file that never
changes (D-19). The reader side stays in the aggregator, and the protocol is
recorded here so it can be switched on in one edit rather than re-invented —
but until a verb ships, this section is documentation, not instruction.

The protocol, for when that day comes: watch `.touch/control.jsonl` (falling
back to `<task-dir>/control.jsonl`) between steps; on
`{"action":"stop","name":"<name>"}` resolve the name to its `taskId` through
the spawn ledger, call `TaskStop`, and append
`{"ack":"stop","name":"…","taskId":"…","result":"stopped|not_found|already_done","ts":"…"}`
to the same file — never fabricating a result, always reporting what `TaskStop`
actually returned. A stopped slot may be re-run only as a fresh spawn with
`attempt` + 1.

What is NOT dormant: the `[touch]` marker (§2), which is the live label
channel, and `TaskStop` itself, which the user or this session can always call
directly on a background task.

## Why these exact standards (verified constraints)

- `agentId` is harness-generated at spawn; the only writable pre-spawn
  identity is the description + prompt text — hence the marker.
- Workflow-spawned agents get a stub `.meta.json` (no description) — hence
  marker-first, always.
- There is no push channel into a running session — which is why a stop, when
  one ships, has to be a file this session polls and acts on, and why §4 is
  dormant rather than deleted.
- The harness records nothing about stops — hence the ledger and the ack lines:
  Touch's audit would be the only record. The ledger half of that is live
  today, written deterministically by the lifecycle hook; the ack half waits on
  the verb.
- "Pause" does not exist in any CLI channel (the harness's pause is kill) —
  do not promise it; stop + fresh-attempt restart is the honest cycle.
