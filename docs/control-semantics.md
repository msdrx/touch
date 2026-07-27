# Control semantics

What Touch may do to a Claude Code session, what it may only *ask*, and what it
must never claim. This is the detail behind the verb table in `README.md`; the
two never disagree, because they use one vocabulary.

**Nothing described here is shipped in v0.** The read side is real; the control
plane is planned (R-34…R-37) and gated. This document exists so that when it
ships, it ships with these meanings — and so the UI can be built knowing which
affordances it is not allowed to draw.

One rule governs the whole document (D13): **a control is rendered only where
it can be honest.** A verb that cannot be performed is either absent or shown
disabled *with its reason*. A verb that was requested but not confirmed reads
"requested", never "done".

---

## 1. Session classes — who may be controlled at all

Three classes (GD-6). The class is derived from evidence, never from a
checkbox, and it decides every affordance:

| class | what it means | evidence required | controls |
|---|---|---|---|
| **owned** | Touch spawned this session under its own PTY | Touch's own spawn record | full ladder, deterministic verbs included |
| **cooperating** | Touch did not spawn it, but it demonstrably speaks the protocol | an observed ack line in its control file, **or** a `[touch]` marker in a live agent prompt | model-mediated verbs only, each with its state machine |
| **observed** | discovered in `~/.claude/sessions/`; no protocol evidence | registry entry alone | **none.** Every control route 403s server-side, not just in the UI |

Promotion is one-directional and evidence-gated: an observed session becomes
cooperating the moment conforming evidence appears, and the UI says which
evidence promoted it. A session never becomes cooperating because someone
asserted it.

Identity is `(pid, procStart)` — `procStart` being `/proc/<pid>/stat` field 22.
`sessionId` is not identity: `/clear` gives the same process a new one. The
registry `status` field is not liveness either (observed 863 s stale while the
session was actively running six subagents); liveness is `/proc/<pid>`
existence with a matching `procStart`.

## 2. Run profiles — where the events come from, and what can be stopped

Two profiles, ingested into one store (GD-8, as amended):

| | **Workflow profile** | **Agent-tool profile** |
|---|---|---|
| produced by | `execute-research`, `implement-plan` | `touch-orchestrate` background spawns |
| deterministic event source | `journal.jsonl` (via `decision_watcher.py`) | the spawn ledger + transcripts |
| node identity | `(runId, key, ordinal)` | full 17-hex `agentId` |
| **run-level stop** | **available** — the launch `toolUseResult.taskId` (verified `w4hiywrt6`, `www4dk54h`) addresses the whole run | n/a (a spawn is not a run) |
| **per-agent stop** | **unavailable** — renders disabled with that reason | **available** — the task id *is* the agent's 17-hex `agentId` (R-04 probe 5, 2026-07-26) |

The two granularities are rendered distinctly and are never substituted for
each other. Stopping a run is not stopping an agent; a UI that offers one while
meaning the other is lying about blast radius. If only the run-level handle
exists, the agent rows say so rather than borrowing the run's button.

A `journal.jsonl` record carries **no timestamp**, and journal order is not
spawn order. Node times come from the agent transcript's first/last record;
`now()` is never used to invent one.

## 3. The verb ladder

### start — deterministic
Touch spawns a session it owns: PTY host, `--session-id <uuid>`, and a child
environment built from an allowlist. `CLAUDE_CODE_CHILD_SESSION` must **not**
be inherited — a child that inherits it silently writes no transcript
(re-confirmed 2026-07-26), which would starve Touch's entire read side.

### terminate / kill — deterministic, owned sessions only
The escalation ladder, in order, each step observed before the next: type
`/exit` → `SIGHUP` to the process group → `SIGKILL`. `SIGTERM` is verified
ineffective against the TUI and is not a rung. Never offered for cooperating or
observed sessions — Touch does not kill processes it does not own.

### stop (graceful) — model-mediated
An *intent*, written to the session's control file and acknowledged (or not) by
the session itself. Its rendered state machine, in full:

```
requested → pending — orchestrator busy → sent → confirmed
                                            └──→ failed(<reason>)
```

- `pending — orchestrator busy` is a real, normal state: the driver is inside a
  turn and has not polled. It is **never** rendered as `expired` while the
  driver is provably blocked.
- `confirmed` requires an observed effect — an ack line, or the agent actually
  ending. A quiet timeout is not confirmation; Touch's own control audit wins
  over inference, because the harness records nothing about stops.
- An intent that binds to no agent is **orphaned**, and is shown as such. An
  orphaned stop is a stop that went nowhere; hiding it would be the dangerous
  outcome, not the embarrassing one.

### restart — model-mediated, ONE meaning
**Restart = re-invoke the workflow script with the stored partition
(`subplans_file`) and `only:[ids]`.** Fresh agents, attempt numbering continues
(`from_attempt`), the divide/derivation step is skipped.

`Workflow({resumeFromRunId})` is **rejected** as a meaning of restart: it
replays recorded agents without re-executing them. If it is ever surfaced, it
is labelled "replayed, not re-executed" and is a different verb.

Before a restart Touch records a checkpoint, three-state and never blocking:
`sha` (a `git stash create` object), `none` (clean tree, nothing to stash), or
`unavailable(<reason>)` — the zero-commit repository is the specimen: `git
stash create` exits 1 with no output there. The UI renders "no checkpoint —
<reason>"; the verb still runs.

### pause — deferred, and honest about why
There is no pause channel in the CLI. The harness's own "pause" is a kill with
a different status label: no suspend, no resume, no checkpoint.

The only honest pause is a **hook gate** — a `PreToolUse`/`SubagentStart` hook
that holds its response (verified holding a tool call 20 s, then releasing).
Properties, all load-bearing: it is per-agent; it takes effect only at the next
tool boundary, so an agent mid-thought keeps thinking; and it is strictly
blocking, so a slow hook slows the session.

Delivery is settled (R-04 probes 1–2, 2026-07-26, recorded in
`.claude/local-orchestrators/touch-full-recon/report/probes.md`): a hook added
to a session's settings **after** it started fires on the very next tool call
without a restart, from the project `.claude/settings.json` and from a
`--settings` file, in both `-p` and interactive-PTY modes. So the gate is
installable into sessions Touch did not spawn.

It is still **not shipped**, and until it is, no pause control renders anywhere
— GD-4 forbids a verb that cannot be honest, and "the mechanism was proved in a
probe" is not the same as "the product does it".

---

## 4. Rules that outlive any implementation

1. **Every confirmation is a derived observation.** Requested-but-unconfirmed
   is the normal failure mode of a model-mediated verb, not an error state.
2. **Observed sessions 403 server-side.** Hiding a button is a UI decision;
   refusing the route is the security boundary.
3. **Touch never writes under `~/.claude/`** — not transcripts, not journals,
   not settings. It is a read-only tap. (A hook *installed by the user* into a
   project's settings is that user's action, not Touch reaching in.)
4. **Controls are command execution**, so they live behind the same posture as
   everything else: loopback by default, per-boot token on every route,
   Origin/Host allowlist at the WebSocket upgrade, and a separate route group
   from the read-only endpoints.
5. **A control file's path is never restated in two places.** The ingest reads
   the configured path list; it does not hard-code a second copy that can drift
   from the first.
6. **A settings file is code.** A hook command in a project's settings executes
   even when that workspace is untrusted (observed 2026-07-26, where the same
   file's `permissions.allow` entries were ignored as untrusted). Treat any
   settings file Touch can see as arbitrary code, and never write one on a
   user's behalf without saying so.
