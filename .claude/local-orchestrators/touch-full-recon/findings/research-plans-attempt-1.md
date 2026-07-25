# research-plans — the plan corpus under `.claude/local-orchestrators/`

Perspective: the two plan files plus `inception.md` §6–7 as their summary.
Conflicts and supersession, staleness against the current repo and the
newer `touch-orchestrate` skill, sequencing for `implement-plan`, and
per-item readiness.

Short names used below:

- **T-plan** = `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md`
  (normative design; D1–D14, T1–T23)
- **P-plan** = `.claude/local-orchestrators/touch-monitor-spawn/plan/touch-monitor-spawn-plan.md`
  (scoped v0; G1–G9, P1–P12)
- **skill** = `.claude/skills/touch-orchestrate/SKILL.md`

Verification note: every line anchor the T-plan and P-plan cite into
`.claude/shared/monitoring/` was re-checked and **all resolve correctly**
(`test_shell.py:155` → `test_gitignore`; `status.sh:34-40` → the event dict;
`monitor.html:287` → the flow-key line; `decision_watcher.py:138-152` → `emit`,
`:636-638`/`:682-688` → the two agent-label emits; `monitor_server.py:199-212`
→ `safe_artifact_path`, `:225-241` → `resolve_port`, `:279-310` →
`parse_client_frames`, which indeed deletes client payloads unread). Anchor rot
is **not** a finding; the findings below are substantive.

---

## PLANS-1 — Two rival plans both claim "consumable by implement-plan as-is", with colliding file ownership and no supersession statement

**file:line** `touch-monitor-spawn-plan.md:5`, `:7-14`;
`touch-aggregator-plan.md:331-336`; `inception.md:246`, `:248-254`
**severity** blocker

**Scenario.** `implement-plan` is invoked with `args = { plan_file, parallel }`
— exactly **one** plan file (`.claude/skills/implement-plan/SKILL.md:31`). The
P-plan's header says "Consumable by `implement-plan` as-is"
(`touch-monitor-spawn-plan.md:5`); `inception.md:246` says of the T-plan "Next
step: hand the plan to `implement-plan`", and `inception.md:254` says of the
P-plan "Ready for `implement-plan`". Nothing anywhere states which one is
authoritative, and nothing says the P-plan supersedes, defers, or replaces the
T-plan's item list.

This is not a cosmetic ambiguity, because the divider partitions **by file
ownership** ("one file, exactly one owner" —
`.claude/skills/implement-plan/SKILL.md:42-48`). The two plans claim the same
files under different item ids and, worse, split the same responsibilities
across differently-named modules:

| Responsibility | T-plan | P-plan |
|---|---|---|
| tailing primitives | `aggregator/util.py` (T1, `:341`) | `aggregator/tailer.py` (P4, `:105`) |
| transcript ingestion | `aggregator/transcript.py` (T7, `:452`) | `aggregator/ingest.py` (P5, `:115`) |
| server core | `aggregator/server.py` + `aggregator/routes.py` (T4, `:392`) | `aggregator/server.py` only (P8, `:155`) |
| hook script | `aggregator/hookpack/touch-hook.sh` (T10, `:548`) | `aggregator/hooks/touch-hook.sh` (P11, `:196`) |
| WS codec | separate `aggregator/ws.py` (T3, `:378`) | folded into `server.py` (P8, `:156-158`) |
| server test | `tests/test_server_integration.py` (T4, `:407`) | `tests/test_server_core.py` + `tests/test_api.py` (P8 `:154`, P9 `:167`) |
| run-all script | `tests/run_all.sh` (T1, `:342`) | `tests/run_all.sh` (P1, `:71`) |
| docs | README.md expansion + `docs/control-semantics.md` (T23, `:805`) | `README-touch.md` (P12, `:210`) |

`aggregator/store.py`, `aggregator/api.py`, `aggregator/control.py`,
`tests/test_store.py`, `tests/test_control.py`, `tests/test_hooks.py`,
`tests/test_sessions.py`, `tests/test_touch_frontend.py`,
`touch-visual/index.html|app.js|style.css` and `.gitignore` are claimed by
**both** plans with different scopes.

**Recommendation.** The reconciled plan must be **one file** and must open with
an explicit supersession clause: "this plan supersedes T1–T23 and P1–P12; those
two files are historical." Pick one module layout and state it once — recommend
the P-plan's finer-grained names (`tailer.py`, `ingest.py`, `agents.py`)
because they map 1:1 to sub-plans the divider can isolate, while keeping the
T-plan's separate `ws.py` (a pure-function codec is the single most
unit-testable module in the design and folding it into `server.py` costs that).
Carry every T-item that the reconciled scope defers into an explicit "Phase 2 —
not in this plan" appendix with its original id, so the deferral is recorded
rather than lost.

---

## PLANS-2 — The v0 plan ships stop controls for sessions that D1 defines as read-only; the `touch-orchestrate` skill introduces a session class neither plan's decisions admit

**file:line** `touch-aggregator-plan.md:68-83` (D1);
`touch-monitor-spawn-plan.md:18-23` (G1), `:45-51` (G6), `:188-190` (P10);
`touch-orchestrate/SKILL.md:72-83` (§4)
**severity** blocker

**Scenario.** D1 is unambiguous: sessions are either **owned** (spawned by
Touch under its PTY — full control surface) or **observed** (discovered in the
registry — "**no** terminal pane, **no** control affordances in v1 (not even
kill)"), and "an affordance that cannot be honest for a class is not rendered
for it" (`touch-aggregator-plan.md:76-83`). T14 hardens this server-side:
"Foreign sessions: every control 403s server-side (not just hidden in UI)"
(`:651-652`).

The P-plan declares D1–D14 binding and "do not contradict"
(`touch-monitor-spawn-plan.md:7-11`), then:

1. **defers the owned-session spawner entirely** (G1, `:20-22` — "Out of scope
   … owned-session spawner"), so in v0 **every** session on the machine is, by
   D1's taxonomy, *observed*; and
2. ships a **Stop button per running agent** in the main pane (P10, `:188-190`)
   and a full stop-intent state machine (G6 `:45-51`, P7 `:142-150`).

Under D1 as written, v0's only feature is an affordance that must 403.

The actual resolution exists but is nowhere written down as a decision: the
`touch-orchestrate` skill (`SKILL.md:72-83`) makes the *orchestrating model*
poll `.touch/control.jsonl` and call `TaskStop` itself. That is a **third
session class** — observed, not owned, but *cooperating* — which D1 never
contemplated because the skill was drafted after the T-plan
(`inception.md:256-257` acknowledges the skill "not yet in it"). Control on a
cooperating session is honest (it is model-mediated, exactly like D7's
stop-loop row) but it is reached through a file the session polls, not through
a PTY Touch owns.

**Recommendation.** Amend D1 explicitly in the reconciled plan: three classes —
`owned` (Touch spawned it; PTY + deterministic verbs), `cooperating`
(observed + declares touch-orchestrate conformance, evidenced by a
`.touch/control.jsonl` ack line or a `[touch]` marker in a live agent prompt;
model-mediated verbs only, every one rendered `requested/sent/confirmed/expired`
per D13), `observed` (everything else; read-only, controls 403 server-side).
State the *evidence rule* for promoting a session to `cooperating` — never a
user checkbox, always an observed artifact — otherwise the UI offers a Stop
button that can never be acked and D13's honesty rules are violated by
construction. Then re-word T14's blanket "foreign sessions 403" to
"non-cooperating sessions 403".

---

## PLANS-3 — G4 says "exactly as D4" but silently drops one member of the ref union; a v0 store that validates strictly will reject every graph event added later

**file:line** `touch-monitor-spawn-plan.md:37-39` (G4);
`touch-aggregator-plan.md:114-123` (D4), `:101-109` (D3 node row);
`inception.md:217-222`
**severity** major

**Scenario.** D4's record shape allows
`ref = {uuid} | {toolUseId} | {agentId} | {runId,key,ordinal} | {pid,procStart}`
(`touch-aggregator-plan.md:121`), and D3 gives the graph node its identity as
`(runId, key, ordinal)` (`:109`). `inception.md:219-221` repeats the five-member
union faithfully.

G4 states "touch-events-v2 **exactly as D4**" and then enumerates
"ref union `{uuid}|{toolUseId}|{agentId}|{pid,procStart}`"
(`touch-monitor-spawn-plan.md:38-39`) — four members. `{runId,key,ordinal}` is
gone. P2 owns the writer and P7/P9 the readers; nothing in the P-plan mentions
runs, journals, or `runId` at all (correctly — the Workflow graph is deferred
with T8/T19).

The failure is concrete and deferred-in-time: P2's test list includes no ref
validation, but T5's does — "`tests/test_store.py` … ref validation rejects
malformed ids" (`touch-aggregator-plan.md:426`). Whoever implements the store
from the reconciled plan will write a union validator from the enumerated list.
If they take G4's list, the first `kind:"node"` event emitted when the graph
lands (T8) is rejected at the writer, in a module already green and no longer
under active edit — a silent data-loss bug discovered by a missing graph, not
by a test.

**Recommendation.** State the ref union **once**, in one place, with all five
members, and require the validator to be *open at the tail*: unknown ref shapes
must be **retained and passed through** (matching D4's "readers treat missing
`v` as v1" and D6's "unknown types: retain raw, never render, never crash" —
`:176-177`), never rejected. Reserve hard rejection for *malformed instances of
known shapes* (a non-17-hex `agentId`, a non-UUID `uuid`). Add that distinction
to the store's test list, because "rejects malformed ids" as currently written
invites the closed-world validator.

---

## PLANS-4 — T11's endpoints are path-parameterised, contradicting D9.3's "no path parameters" and the P-plan's query-string API

**file:line** `touch-aggregator-plan.md:258-260` (D9.3), `:584-590` (T11),
`:397-398` (T4 router); `touch-monitor-spawn-plan.md:41-44` (G5), `:169-172` (P9)
**severity** major

**Scenario.** D9.3 is a security non-negotiable: "Typed, projected endpoints
only over `~/.claude` — ids validated by regex …; **no path parameters**
(STACK-11)" (`touch-aggregator-plan.md:258-260`). G5 restates it verbatim
(`touch-monitor-spawn-plan.md:43-44`).

T11 then specifies: `/api/session/<uuid>/timeline?since=<seq>`,
`/api/session/<uuid>/queue`, `/api/runs/<runId>/graph`,
`/api/runs/<runId>/node/<agentId>`, `/api/toolresult/<tool_use_id>`
(`touch-aggregator-plan.md:584-590`). Every one of those is a path parameter.
T11 defends itself with "No endpoint accepts a filesystem path" (`:591`) —
which is a different and weaker claim than D9.3's.

Two knock-on problems:

1. T4 specifies the router as a "`(method, route) → handler` **table** with
   default 404" (`:397-398`). A literal dict cannot dispatch
   `/api/runs/<runId>/node/<agentId>`; the implementer must invent a pattern
   matcher, which is precisely the component D9.3 was written to avoid, and
   T4's own test list has no case for it.
2. P9 solves the same problem the other way — `/api/session?pid=&procStart=`,
   `/api/events?after=<seq>` (`touch-monitor-spawn-plan.md:169-172`). So the
   two plans hand the implementer two mutually exclusive URL schemes for
   overlapping data, and the frontend items (T16 `:687-689`, P10) hard-code
   whichever they inherit.

**Recommendation.** Settle on **query-string ids with a static route table** —
it honours D9.3 literally, keeps T4's dict router real, makes the id-regex
gate one shared helper, and matches P9. Rewrite T11's seven endpoints into that
form in the reconciled plan (e.g. `/api/session/timeline?session=<uuid>&since=`,
`/api/run/graph?run=<runId>`, `/api/run/node?run=&agent=`,
`/api/toolresult?id=<tool_use_id>`) and add one explicit test: "a request whose
path contains a segment after the registered route 404s rather than matching a
prefix" (the T4 `:410` "unknown session id → 404 not fallback" case
generalised).

---

## PLANS-5 — T20's premise is empirically false: the archived runs' source transcripts and journals are still on disk, so "archived — source transcripts unavailable" would be a lie

**file:line** `touch-aggregator-plan.md:750-766` (T20), `:310-311` (D13),
`:190-192` (D6 no-auto-discovery); `CLAUDE.md` (final bullet, the omnigent claim)
**severity** major

**Scenario.** T20 renders archived task folders "from its `events.jsonl` only …
labelled 'archived — source transcripts unavailable'" (`:759-762`), and D13
repeats the label unconditionally (`:310-311`). The premise is
AGENTGRAPH-14 / the CLAUDE.md note that the `orch-config.json` files "point at
`wf_dir` paths from a **different, earlier project** (`omnigent`)".

That is no longer true of any task folder in this repo. Verified on disk:

```
touch-aggregator/orch-config.json  wf_dir=…/-home-laniakea-Projects-touch/dd469822-…/subagents/workflows/wf_829e6f58-b2f   → EXISTS
touch-repo-recon/orch-config.json  wf_dir=…/-home-laniakea-Projects-touch/e423cd3c-…/subagents/workflows/wf_455b348c-e17   → EXISTS
touch-full-recon/orch-config.json  wf_dir=…/-home-laniakea-Projects-touch/292fc08c-…/subagents/workflows/wf_930e210a-6da   → live (this run)
```

Both completed runs' directories contain the full `journal.jsonl` plus
`agent-<17hex>.jsonl` + `.meta.json` per agent. So the touch-aggregator run is
**not** a degraded legacy artifact — it is fully renderable through the normal
T8 path (harness-derived, solid provenance, full agentIds, real timestamps).

Rendering it through T20's legacy-only path would (a) discard available
harness facts, (b) display a label that is factually wrong, and (c) violate
D13's own honesty premise while claiming to serve it.

**Recommendation.** Make the archive label **derived, not constant**: for each
task folder, check `wf_dir` existence (a plain `os.path.isdir`, which is *not*
"auto-discovery" — D6 `:190-192` forbids *globbing for* a journal, not
*stat-ing the one the config names*). Three states: `live source present`
(render via the full graph path, solid), `source pruned` (legacy events only,
"archived — source transcripts unavailable"), `foreign wf_dir` (config points
outside this machine's projects — display the path, never glob). Add a test
fixture per state. Also correct the CLAUDE.md omnigent sentence — it now
mis-describes all three folders and will mislead the next implementer.

---

## PLANS-6 — Part F's acceptance criterion is unreachable through the path the plan assigns to it: the legacy stream has six identical labels and 8-hex truncated agent ids

**file:line** `touch-aggregator-plan.md:893-903` (Part F), `:750-766` (T20),
`:104` + `:106` (D3 agent row), `:768-789` (T21);
`touch-monitor-spawn-plan.md:20-23` (G1 defers T20 and T21)
**severity** major

**Scenario.** Part F says v1 is done when "the touch-aggregator research run of
2026-07-25 renders as a graph with six *distinctly labelled* researcher nodes,
correct token rollups (deduped), three-state liveness, and solid/dashed
provenance" (`:898-901`).

Reduced the actual archived stream
(`touch-aggregator/events.jsonl`, 590 lines). Every researcher agent object is:

```
('research #1', 'a2fc883c') x118   ('research #1', 'a74f0c93') x115
('research #1', 'a9eabf26') x92    ('research #1', 'a82d2e25') x78
('research #1', 'a79fa2f4') x85    ('research #1', 'a2ec1069') x45
```

— six agents, one label, ids truncated to 8 hex by
`decision_watcher.py:637` (`agent_id[:8]`). The *distinguishing* information
exists only in the event's **top-level `stage`** field (`control`,
`agentgraph`, `liveio`, `sessiondata`, `priorart`, `stack` — confirmed present,
4 events each), not in `agent.label`.

Consequences the plan does not address:

- D3 requires "Agent | full 17-hex `agentId` | never truncated" (`:104`). No
  legacy agent event can satisfy it. If the store validates `agentId` shape
  (see PLANS-3), **every** legacy agent event is rejected and the archive
  renders empty.
- T21 fixes the watcher's labels going forward (`:781-784`) but cannot
  retroactively rewrite an already-written `events.jsonl` — and the P-plan
  defers T21 anyway (`touch-monitor-spawn-plan.md:23`).
- "correct token rollups (deduped)" is not verifiable for archived data: the
  legacy events carry rollups the watcher already computed; Touch inherits a
  number it cannot re-derive without the transcripts.

**Recommendation.** Two changes. (1) Because PLANS-5 shows the source data
survives, retarget the Part F criterion at the **full** path — six distinct
labels derived from `agent-<17hex>.meta.json` + the `[monitor]` marker in each
agent transcript — and keep the legacy path's acceptance separate and weaker.
(2) For the legacy path, specify a distinct identity namespace explicitly:
legacy agent ref is `{agentId: "<8hex>", partial: true}` or a synthesised
`legacy:<task>:<8hex>`, label derived from the event's **top-level `stage`**
(not `agent.label`), rendered dashed/declared per D13, and *exempt from the
17-hex validator*. Write both as test cases; today neither plan has one.

---

## PLANS-7 — The control channel has no defined address: `<task-dir>` is undiscoverable from a session, and `.touch/` is repo-root-relative while the sidebar spans every cwd

**file:line** `touch-monitor-spawn-plan.md:45-51` (G6), `:133-135` (P6 ledger),
`:142-150` (P7); `touch-orchestrate/SKILL.md:52-56`, `:74-76`;
`touch-aggregator-plan.md:140-161` (D5)
**severity** major

**Scenario.** Three specifications of the same file disagree about where it is.

- D5 puts Touch state at "`.touch/` at repo root (gitignored; override
  `TOUCH_STATE_DIR`)" (`touch-aggregator-plan.md:143-144`) — *the aggregator's*
  repo root.
- The skill tells the orchestrating session to "Watch `.touch/control.jsonl`
  (fall back to `<task-dir>/control.jsonl` if no `.touch/` exists)"
  (`SKILL.md:74-76`) — resolved relative to *that session's* cwd.
- P7 reduces intents over `.touch/control.jsonl` (`:142-145`) with no mention
  of which one.

The sidebar lists every session on the machine with its own `cwd`
(P3 `:91-98`, T6 `:441-444`). A session running in another repo that follows the
skill writes its ack into *its* `.touch/` or *its* task dir — a path the
aggregator never reads. G6's state machine then behaves exactly as designed for
a session that ignored the request: `requested` → 120 s → `expired`
(`:49-51`). The user sees an honest-looking failure caused entirely by
addressing, and it is indistinguishable from real non-cooperation.

The same hole breaks P6's name join: "cross-check
`<task-dir>/state/spawn-ledger.jsonl` when present" (`:133-135`). `<task-dir>`
is a concept internal to an orchestration run; the aggregator discovers
sessions through `~/.claude/sessions/*.json`, which carries `pid, procStart,
sessionId, cwd, status, kind, name` and **no** task-dir. There is no stated
mechanism to get from a discovered session to its ledger, so the cross-check —
which is what binds a Touch name to a `taskId`, and therefore what makes
`TaskStop` addressable at all — is unimplementable as written.

**Recommendation.** Make the control file **per-session and aggregator-owned**:
`<TOUCH_STATE_DIR>/sessions/<pid>-<procStart>/control.jsonl`, with the absolute
path handed to the session at the moment Touch decides it is cooperating —
injected via env for owned sessions, and for cooperating sessions announced by
the session itself. Concretely: require the skill to write one
**registration line** into the ledger *and* to record the ledger's absolute
path in its first `[touch]`-marked agent prompt (the marker is already parsed —
add `ledger=<abspath>`), so the aggregator learns `session → task-dir` from an
artifact it already reads instead of guessing. Then update the skill
(`SKILL.md:74-76`) to drop the ambiguous relative-path fallback: one absolute
path, or the session declares it does not participate. This is a
skill-and-plan co-change and must be one item in the reconciled plan, owning
both `SKILL.md` and `aggregator/control.py`.

---

## PLANS-8 — P11's hook pack has no delivery mechanism in v0, and its viability rests on an unsettled Part E experiment

**file:line** `touch-monitor-spawn-plan.md:194-206` (P11), `:20-22` (G1);
`touch-aggregator-plan.md:557-562` (T10 delivery), `:866-869` (Part E #2)
**severity** major

**Scenario.** T10 is explicit about how the hook pack reaches a session: the
settings template "is passed via `--settings` **at spawn** for owned sessions;
installing into a project's `.claude/settings.json` for foreign sessions is a
separate, explicit, reversible user action"
(`touch-aggregator-plan.md:558-562`). Part E #2 records the open question
underneath it — "Hook hot-reload for already-running sessions (CONTROL-10) …
**v1 assumes NO** (hooks only at spawn)" (`:866-869`).

G1 defers the owned-session spawner (`touch-monitor-spawn-plan.md:20-22`). So
in v0 there are no spawns, therefore no `--settings` delivery, therefore the
only path is hand-editing a project's `.claude/settings.json` — and if the
Part E assumption holds, that has **no effect on the sessions already running**,
which are the only sessions v0 can see. P11 would ship a script, a README, and
`tests/test_hooks.py`, all green, that never fires for a real user in the v0
feature set.

Note also that this repo's own `.claude/settings.json` currently contains only
a `statusLine` entry — there is no hooks block, so nothing is in place today.

**Recommendation.** Either (a) settle Part E #2 first — it is a ten-minute
experiment (start a probe session, add a `PreToolUse` hook to project settings,
invoke a tool, observe) and it decides P11's fate outright; or (b) reorder so
that the minimal owned-session spawner (the `--settings` + `--session-id` +
env-allowlist part of T9, *without* the PTY/xterm tier) moves **into** v0,
which is what makes hooks deterministic and also what makes D1's "owned" class
non-empty. Recommend (a) then (b): run the experiment, and if it confirms
"no hot-reload", promote the spawner and demote P11 to depend on it. Until one
of those happens, P11 should not be an implementable item — mark it blocked in
the reconciled plan rather than sequenced.

---

## PLANS-9 — T14's pre-restart checkpoint silently records nothing: `git stash create` no-ops on a repo with no initial commit, and exits 0

**file:line** `touch-aggregator-plan.md:646-650` (T14), `:208` (D7 restart row),
`:657` (test list); `inception.md:161-163`
**severity** major

**Scenario.** D7's restart row and T14 both require a tree checkpoint before
any stop/restart affecting an implement loop: "record a tree checkpoint
(`git stash create` sha + `git status --porcelain` scoped to the sub-plan's
owned files) and expose 'restore checkpoint' as a separate explicit action"
(`:646-650`). T14's test list asserts "checkpoint recorded before restart"
(`:657`).

This repo has **zero commits** (`git log` → "your current branch 'master' does
not have any commits yet"; `git rev-parse HEAD` fails). Reproduced the
behaviour in a throwaway repo under `/tmp/claude-1000`:

```
$ git stash create
You do not have the initial commit yet
$ echo $?
0
```

It prints a message to stderr, emits **no sha on stdout**, and **exits 0**. So
the natural implementation — capture stdout, store it as the checkpoint sha —
records an empty string as a successful checkpoint. The UI then offers "restore
checkpoint" against nothing, which is exactly the class of dishonest
affordance D13 exists to prevent. And T14's test would pass against a fixture
repo that *does* have a commit while failing silently on the real one.

**Recommendation.** Two changes. (1) T14: treat an empty `git stash create`
output as **failure**, not success — never trust the exit code here; on failure
record `checkpoint: none, reason: "no initial commit"` in `control.jsonl`,
render restart as "no checkpoint available" and require an extra confirm.
(2) Add an item, absent from both plans, that makes the **initial commit**:
nothing in the corpus commits anything, yet the T-plan's own control semantics
assume a git history exists, and `implement-plan`'s gated loops run against the
working tree. The reconciled plan should start with "commit the current
untracked tree on `master`" as an explicit prerequisite step, and T14's test
list should gain the no-HEAD case.

---

## PLANS-10 — T8's anchored marker regex fails against real transcripts, and the skill's "FIRST line" rule is already violated by the shipped templates

**file:line** `touch-aggregator-plan.md:499-502` (T8 label parsing);
`touch-orchestrate/SKILL.md:39-44` (§2 marker-first);
`touch-monitor-spawn-plan.md:130-134` (P6 "first prompt line")
**severity** major

**Scenario.** T8 specifies label extraction as: "parse
`^\[monitor\] plan=… stage=… role=… attempt=…` from the oldest transcript's
first user record" (`:499-502`). The skill mandates "**Marker first.** The
FIRST line of every agent prompt is: `[touch] name=… parent=… root=… role=…
attempt=…`" (`SKILL.md:39-44`), and P6 joins on "the `[touch] …` **first
prompt line**" (`:130-134`).

Both templates build the prompt with a JS template literal that opens on the
line *before* the marker
(`execute-research/templates/research.workflow.js:66-67`; identically in this
task's `orch-scripts/research.workflow.js:94-95`), so every prompt on disk
begins with a newline. Confirmed by reading a real agent transcript's first
user record:

```
'\n[monitor] plan=research stage=sessiondata role=research attempt=1\nYou are a READ-ONLY researcher…'
```

The marker is on line 2 of the string, not line 1. An anchored `^\[monitor\]`
without `re.MULTILINE` matches nothing; a literal "first line" check finds an
empty string. The working prior art already knows this and deliberately avoids
the trap — `decision_watcher.py:121` uses an unanchored pattern applied with
`MARKER.finditer(text)` (`:344`), no `^`, no line splitting. T8 would
reintroduce a bug the module solved.

A second, subtler trap in the same area: the watcher's regex is **order-fixed**
(`plan=` then optional `stage=` then `role=` then `attempt=`,
`decision_watcher.py:121`). The skill's `[touch]` marker uses a different field
order (`name parent root role attempt`) and the two markers are expected to
coexist on adjacent lines (`SKILL.md:60-62`). Any parser copied from the
watcher will silently fail on reordered or extended markers — and PLANS-7
recommends adding a `ledger=` field, which such a parser would reject.

**Recommendation.** Specify marker parsing once, normatively, in the reconciled
plan: scan the **whole first user record text** (not the first line) with an
unanchored `\[touch\]\s+(.*)` / `\[monitor\]\s+(.*)` match, then parse the
captured remainder as **order-independent `key=value` pairs** into a dict,
ignoring unknown keys. Add the leading-newline case to the fixture set
explicitly (it is the real-world shape, not an edge case). Separately, either
fix the templates to emit the marker with no leading newline **or** delete the
"FIRST line" wording from `SKILL.md:39` — as written the skill states a rule
its own reference templates break, which will send an implementer hunting for a
non-existent bug.

---

## PLANS-11 — `seq` scope is never defined; P2 writes it per-session while P9 offers a global cursor

**file:line** `touch-monitor-spawn-plan.md:82-85` (P2), `:37-39` (G4),
`:169-172` (P9); `touch-aggregator-plan.md:118-127` (D4), `:419-421` (T5),
`:602-607` (T12)
**severity** minor

**Scenario.** D4 shows `"seq":184` and says only that it "is assigned by the
single-writer aggregator process" (`:126-127`) — no scope. T5 says "Single-writer
append with `seq` (**resumes from line count at boot**)" (`:419-420`), which is
per-file. P2 writes
`.touch/sessions/<pid>-<procStart>/events.jsonl` with "single-writer monotonic
`seq` **per session**" (`:82-84`). But P9 exposes `/api/events?after=<seq>` as a
global page across everything (`:170`), and T12 exposes
`/ws?session=…&after=<seq>` per session (`:602-604`).

With per-session seq, `/api/events?after=42` is meaningless — 42 exists once per
session and the union has no total order. Clients that keep "their cursor across
reconnects" (T12 `:605-607`) will silently resync to the wrong point when they
switch sessions, and the "no duplicates on reconnect" test (`:610`) passes
per-session while the global endpoint is quietly broken.

**Recommendation.** Declare seq **per event-log file** (per session, per run) —
it is the only scope compatible with "resumes from line count at boot" and with
the single-writer-per-file layout of D5. Then either delete P9's
`/api/events?after=` or redefine it to require a stream selector
(`/api/events?session=<key>&after=<seq>`). Add one line to the store's contract:
"a cursor is `(stream, seq)`; a bare seq is never a valid cursor", and a test
that two streams may legally hold the same seq.

---

## PLANS-12 — T15's gate hook must authenticate against a per-boot token, but T10 requires the settings template to be static

**file:line** `touch-aggregator-plan.md:662-674` (T15), `:558-560` (T10 static
template), `:252-255` (D9.1); `touch-monitor-spawn-plan.md:194-206` (P11)
**severity** minor

**Scenario.** T15 registers the pause gate as "an HTTP hook endpoint
(`POST /hook/gate`)" (`:666-667`). D9.1 permits exactly one unauthenticated
route: "required … on the page load, every `/api/*`, `/ws`, and `/pty`. **No
unauthenticated route except `/health`**" (`:252-255`) — so `/hook/gate` needs
the token. The token is per-boot and random (`:252`).

T10 requires the settings template to be "**static** (matcher + command only,
all policy server-side — CONTROL-10)" (`:558-560`). A static file cannot embed
a token that changes every boot. Nothing in T10 or T15 says how the hook
process obtains it.

(P11's v0 hook pack is unaffected — it only appends to a file spool and makes
no network call (`:198-203`). The problem is specific to the gate.)

**Recommendation.** Keep the template static and have the hook **read the token
at invocation time** from `.touch/server.json` (already in D5's layout,
`:150`), whose path is passed as a literal argument in the static command line.
Add the file-mode requirement explicitly (`0600` on `server.json`, since it now
holds a live credential rather than a "token fingerprint" as D5 `:150`
currently describes it — that wording also needs fixing, since a fingerprint is
not usable for authentication). Add one test: the gate rejects a POST with no
token and with a stale token.

---

## PLANS-13 — Small staleness against the current repo: files listed as "new" that exist, an ignore line already present, and two rival documentation targets

**file:line** `touch-aggregator-plan.md:339-345` (T1), `:803-807` (T23);
`touch-monitor-spawn-plan.md:69-72` (P1), `:208-211` (P12); `.gitignore:17`
**severity** minor

**Scenario.** Three small drifts, each of which the file-ownership divider
takes literally:

1. T1 says ".gitignore gains `.touch/` and `__pycache__/`" (`:347-348`).
   `__pycache__/` is already there (`.gitignore:17`, under "# --- Python ---").
   An implementer following the item adds a duplicate line.
2. T23 lists "**Files (new):** `README.md` expansion (root)" (`:805`). README.md
   exists (product intent) — it is a *change*, not a new file. The new/changed
   distinction is what the divider uses to assign owners and to decide whether
   an item may run in parallel.
3. T23 expands the root `README.md` plus a new `docs/control-semantics.md`
   (`:805`); P12 instead creates `README-touch.md` (`:210`). Two plans, two doc
   homes, and both also edit `CLAUDE.md`. Whichever lands first, the other
   leaves an orphan doc.

**Recommendation.** In the reconciled plan, mark every file explicitly `new` or
`changed` against a re-checked working tree (the `.gitignore` line already
present, `README.md` and `CLAUDE.md` existing, `.claude/settings.json` existing
with only a `statusLine` key). Pick one doc target — recommend expanding the
root `README.md` plus `docs/control-semantics.md` (T23's shape), and drop
`README-touch.md`, since a second top-level README competing with the product
README is precisely the confusion the reconciled plan is meant to end.

---

## PLANS-14 — Nothing in either plan owns the `touch-orchestrate` skill, so the cooperative standard it defines is never verified against the code that consumes it

**file:line** `touch-orchestrate/SKILL.md:37-62`, `:72-83`;
`touch-monitor-spawn-plan.md:11-13`, `:126-140` (P6), `:194-206` (P11);
`inception.md:256-270`
**severity** minor

**Scenario.** The P-plan declares the skill NORMATIVE — "the spawn standard
(naming, marker, ledger, control loop) is NORMATIVE; this plan builds the
machinery around it, **it does not redefine it**"
(`touch-monitor-spawn-plan.md:11-13`). Every join in P6 and every ack in P7
depends on the skill's exact on-disk shapes: the `[touch]` marker fields, the
`description: "<name> — <short task>"` convention (`SKILL.md:45-47`), the
spawn-ledger line shape (`SKILL.md:52-56`), and the ack line shape
(`SKILL.md:79-81`).

No item in either plan lists `SKILL.md` in its **Files**, and no test asserts
that the aggregator's parsers and the skill's documented shapes agree. The two
will drift on the first change to either — and PLANS-7 and PLANS-10 already
propose changes to `SKILL.md`, which currently have no owner.

Worth noting too that the skill's `description:` convention is untestable
against existing data: the only real Agent-tool spawn meta on this machine is
`{"agentType":"general-purpose","description":"Assess control and UI
feasibility",…}` — no `[touch]` name, because it predates the skill. So P6's
"Unnamed agents get `agentId` as display name, flagged unconventional"
(`:135-136`) is the *common* case today, not the exception, and deserves a
fixture.

**Recommendation.** Add one item that owns `.claude/skills/touch-orchestrate/SKILL.md`
together with a conformance test (`tests/test_touch_standard.py`) asserting, on
shared fixtures, that: the marker parser accepts the exact marker string the
skill documents; the ledger reader accepts the exact JSON line the skill
documents; the ack reducer accepts each of the three documented `result` values
(`stopped|not_found|already_done`, `SKILL.md:80`). Fold PLANS-7's `ledger=`
addition and PLANS-10's "FIRST line" correction into that item so the skill and
its consumers change together, and add the unnamed-legacy-agent fixture to P6.

---

## PLANS-15 — Sequencing: what `implement-plan` should actually receive first

**file:line** `touch-monitor-spawn-plan.md:16-66` (G1–G9);
`touch-aggregator-plan.md:331-336` (Part C ordering), `:893-903` (Part F)
**severity** minor

**Scenario.** Beyond PLANS-1's "one file" requirement, the corpus gives no
guidance on *order*, and the T-plan's own ordering has a dependency inversion
for the v0 scope: T4 (server core) precedes T5 (store) and T6/T7 (the readers),
so the first sub-plan to go green is a server with nothing to serve, and its
integration tests (`:407-411`) are the only tests exercising anything real for
several items. The P-plan orders better (store → discovery → tailing →
ingestion → agents → control → server → API → UI) but then puts P11's blocked
hook pack (PLANS-8) at position 11, ahead of the e2e simulation that would have
exposed it.

Per-item readiness, assessed against "concrete files + a test a fresh
implementer could actually write today":

- **Ready as written**: P1/T1 (scaffolding), P2/T5 (store — once PLANS-3's ref
  union is fixed), P4/T1-util (tailer; the richest fixture list in the corpus
  and fully verified prior art to copy), P3/T6 (session discovery; injectable
  fake `/proc` + registry is specified), P5/T7 (ingestion; the 2.09x dedup
  fixture is concrete), T3 (WS codec; RFC vectors, pure functions).
- **Ready only after a decision**: P7/T14 (control — blocked on PLANS-2's
  session class and PLANS-7's addressing), P9/T11 (API — blocked on PLANS-4's
  URL scheme), P6 (agent join — blocked on PLANS-7's ledger discovery),
  T20 (archive — blocked on PLANS-5's label derivation).
- **Blocked outright**: P11/T10 (hooks — PLANS-8), T15 (pause gate — depends on
  T10 plus PLANS-12's token path plus Part E #4, unverified).
- **Deferred cleanly, no re-decision needed**: T2, T9, T13, T17 (PTY/xterm
  tier), T19 (SVG graph), T21 (monitoring module — and note it "must not run
  while a live orchestration is mid-run", `:786`, which is a scheduling
  constraint no other item has).

**Recommendation.** Hand `implement-plan` **one** reconciled plan whose first
phase is exactly the six "ready as written" items in the P-plan's order
(scaffold → tailer → store → sessions → ingest → agents), with the read-only
API and page appended once PLANS-4 is settled — i.e. a monitoring-only v0 with
**no** control verbs at all. That produces a demonstrable, honest artifact (the
sidebar and agent tree over this machine's real sessions, including the two
archived runs whose sources survive) without touching a single unsettled
decision. Make the control plane phase 2, gated on PLANS-2 + PLANS-7 + the
Part E #2 experiment, and restate acceptance per phase — Part F's current
single criterion spans three phases and cannot be met by any one of them.
