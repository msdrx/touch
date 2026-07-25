# research-skills — the four skills as ONE protocol

Perspective: `execute-research`, `implement-plan`, `m-orchestrator`, and the new
`touch-orchestrate` read as a single spawn/monitor/control protocol. Focus:
marker coexistence, naming, spawn discipline, state/handoff, control loop —
contradictions, gaps, and what Touch must build for the skills to be honest.

Files read in full: the four `SKILL.md`s, both `templates/*.workflow.js`, this
run's adapted `touch-repo-recon/orch-scripts/research.workflow.js`,
`decision_watcher.py` (marker/classify path), `monitoring.md`, `inception.md`,
`touch-aggregator/plan/touch-aggregator-plan.md` (D1–D14, T8/T10/T14/T15),
`touch-monitor-spawn/plan/touch-monitor-spawn-plan.md` (G1–G9, P1–P12).

Empirical check performed (read-only, no writes outside the mandated
status.sh calls): while **five Workflow-spawned research agents were running**
(this very run), `TaskList` returned **"No tasks found"** — i.e. agents spawned
by the Workflow `agent()` primitive are not harness tasks and carry no
`taskId`. That single fact is the hinge of SKILLS-1/-2/-17.

---

## SKILLS-1 — Workflow-spawned agents cannot satisfy touch-orchestrate's spawn discipline at all

**File:** `.claude/skills/touch-orchestrate/SKILL.md:49-57` vs
`.claude/skills/execute-research/templates/research.workflow.js:139-144` and
`.claude/skills/implement-plan/templates/implement.workflow.js:172-196`
**Severity: blocker**

**Scenario.** `touch-orchestrate` §2 mandates, for *every* spawn with no
exceptions: spawn via the Agent tool with `run_in_background`, then append
`{"name","parent","role","attempt","taskId","ts"}` to
`<task-dir>/state/spawn-ledger.jsonl`, because "`TaskStop` can kill each agent
individually". But the two skills it claims to "layer on top of" spawn
exclusively through the Workflow `agent()` primitive inside a `.workflow.js`
script, which is their NORMATIVE protocol (`execute-research/SKILL.md:36-38`,
`implement-plan/SKILL.md:27-30`). Workflow spawns:

- are **not harness tasks** — verified above: `TaskList` was empty while five
  workflow agents ran, so there is no `taskId` to write into the ledger and
  nothing for `TaskStop` to address;
- persist a **63-byte stub `.meta.json`** with no `description`
  (`inception.md:90-96`), so the skill's second name channel is unavailable too;
- have no `run_in_background` concept — `parallel()` already backgrounds them
  inside the one CLI process.

So the ledger line is unfillable, stop is impossible, and *the only agent loops
the README says must be stoppable* (`README.md:5-7`: "pause, restart, start and
terminate agents loops … about loops you can find in /execute-research and
/implement-plan") are precisely the ones the standard cannot reach. This repo's
own live orchestration is the proof: `touch-repo-recon/orch-scripts/
research.workflow.js:71-76,120-124` conforms to the templates and contains no
`[touch]` marker, no name, no ledger.

**Recommendation.** The plan must decide this once, as a global decision, not
leave it to an implementer:

1. Give `touch-orchestrate` an explicit **two-profile** structure —
   *Agent-tool profile* (background + `taskId` + `TaskStop`, today's text) and
   *Workflow profile* (marker-only identity; ledger line with
   `"taskId": null` plus `{"wfRunId","wfKey","plan","stage"}`; **stop is
   declared unavailable** and the UI must render the Stop button disabled with
   the reason). Anything else makes the UI lie.
2. Or migrate the skill pair off Workflow onto background Agent-tool spawns —
   but that costs the deterministic journal (SKILLS-15) and typed structured
   output (SKILLS-17), so it is a large, separately-planned change.

Either way this becomes an ordered plan item that touches
`.claude/skills/touch-orchestrate/SKILL.md` and both `templates/*.workflow.js`.

---

## SKILLS-2 — The control loop cannot run while a workflow is awaited; every stop expires

**File:** `.claude/skills/touch-orchestrate/SKILL.md:72-83` vs
`.claude/local-orchestrators/touch-monitor-spawn/plan/touch-monitor-spawn-plan.md:45-51`
**Severity: blocker**

**Scenario.** §4 says the session must poll `.touch/control.jsonl` "between
steps — at minimum before each new spawn and after each completion
notification". During a skill run the orchestrating session is blocked inside a
single `Workflow` tool call for the entire run (minutes to hours); it executes
no turns, so it polls nothing. Even in the Agent-tool profile, "between steps"
in a *serial* `implement-plan` loop means once per agent — and one implementer
attempt routinely runs 5–20 minutes. Meanwhile G6 expires an unacknowledged
intent after **120 s** and shows `expired`. Result: a user presses Stop, Touch
marks it expired ~2 minutes later, and the agent then dies (or does not) with
no correlation. The UI's honest-states table (G8/D13) is defeated by its own
timing constants.

**Recommendation.**
- Replace `expired` for this case with `pending — orchestrator busy (last poll
  N s ago)`; only expire after an *observed* poll that ignored the intent.
- State the honest latency bound in both the skill and the UI: "stop takes
  effect at the next orchestrator step; for skill runs that is the end of the
  currently running agent".
- Make the deterministic backstop a v0 item, not v1.5: the `PreToolUse` /
  `SubagentStart` hook (plan T10/T15,
  `touch-aggregator-plan.md:546-578`, `:660-680`) is the only channel that acts
  inside a blocked session. A hook that consults a per-agent stop table gives a
  real stop for both profiles.

---

## SKILLS-3 — `control.jsonl` is declared single-writer by D5 but the skill makes it multi-writer, with a truncation hazard

**File:** `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md:149`
("control.jsonl # control audit, single writer (D7)") vs
`.claude/skills/touch-orchestrate/SKILL.md:78-82` and
`touch-monitor-spawn-plan.md:85-87`
**Severity: major**

**Scenario.** D5 fixes `.touch/control.jsonl` as a single-writer audit log —
and D4's `seq` is explicitly "single-writer `seq`" (`inception.md:216-222`).
`touch-orchestrate` §4 then requires the *orchestrating session* to append ack
lines "to the same control file", making it two independent writers (server +
model-driven shell). Worse, an agent instructed to "append a line" frequently
reaches for the Write tool (whole-file rewrite) or Edit, which will silently
drop intents the server appended microseconds earlier. There is also no `seq`
field defined for ack lines, so the aggregator's reduction has no ordering key
for them.

**Recommendation.**
- Split the channel: `.touch/control.jsonl` (Touch-written intents, single
  writer, carries `seq`) + `.touch/control-ack.jsonl` (agent-appended, no
  `seq`, reduced by `(name, ts)`); or keep one file but declare it explicitly
  multi-writer and drop the D5 "single writer" wording.
- The skill must mandate the exact append command
  (`printf '%s\n' "$LINE" >> "$CTRL"`), and forbid Write/Edit on the control
  file in one sentence.
- The aggregator's tailer must treat a size shrink on the control file as
  corruption → full re-ingest + a visible `control-log-truncated` health event
  (P4 already has the shrink detector; P7 must use it).

---

## SKILLS-4 — The `.touch/` → `<task-dir>` control-file fallback is ambiguous and racy

**File:** `.claude/skills/touch-orchestrate/SKILL.md:74-75`
**Severity: major**

**Scenario.** "Watch `.touch/control.jsonl` (fall back to
`<task-dir>/control.jsonl` if no `.touch/` exists)". Three undefined things:
(a) `.touch/` relative to what — the session `cwd`, the repo root, or
`TOUCH_STATE_DIR` (D5 defines the override; the skill never mentions it);
(b) *when* the fallback is evaluated — if the orchestrator starts before the
aggregator, `.touch/` does not exist yet, the model latches the task-dir path,
and every intent the UI writes to `.touch/control.jsonl` is invisible forever;
(c) the aggregator has no way to learn the task-dir path, so it can neither
read acks nor mirror intents there. The v0 plan only ever names
`.touch/control.jsonl` (G6, P7), so the fallback branch is dead machinery that
can only cause a silent split-brain.

**Recommendation.** Delete the fallback. Canonical path:
`${TOUCH_STATE_DIR:-<repo-root>/.touch}/control.jsonl`, resolved **on every
poll** (never cached), with the skill instructing `mkdir -p` of the directory
before the first spawn so the file exists even when no aggregator is running.
Repo root = the git toplevel of the session `cwd`; state that explicitly.

---

## SKILLS-5 — The spawn ledger lives where D5 forbids Touch state, and is undiscoverable by the aggregator

**File:** `.claude/skills/touch-orchestrate/SKILL.md:51-57` vs
`touch-aggregator-plan.md:160-161` ("Never under
`.claude/local-orchestrators/`") and `touch-monitor-spawn-plan.md:129-135`
**Severity: major**

**Scenario.** The ledger is a *Touch-consumed* artifact (P6: "cross-check
`<task-dir>/state/spawn-ledger.jsonl` when present") yet the skill puts it
under the orchestrator task folder, which for skill runs is
`.claude/local-orchestrators/<task>/state/` — the exact tree D5 declares off
limits for Touch state and CLAUDE.md protects as monitoring history. Beyond the
policy clash it is operationally broken: nothing tells the aggregator which
task folders exist or which belongs to the session it is rendering, so the
"cross-check" in P6 can never actually run. The ledger line also lacks any key
that would let a repo-wide reader disambiguate two orchestrations
(no `root`, no `cwd`, no `sessionId`).

**Recommendation.** Canonical, append-only `.touch/spawn-ledger.jsonl` (one
file per repo, all orchestrations), line shape:
`{"ts","root","name","parent","role","attempt","taskId"|null,"cwd","sessionId","plan","stage","wfRunId"?,"wfKey"?}`.
An optional mirror under `<task-dir>/state/` is fine for the orchestrator's own
convenience, but Touch reads only `.touch/`. Update the skill, P6 and G3
together so the three agree on one path.

---

## SKILLS-6 — Marker precedence conflict: `[monitor]` is last-wins, `[touch]` is first-line, and plan T8 parses `[monitor]` anchored at string start

**File:** `.claude/shared/monitoring/decision_watcher.py:340-345` vs
`.claude/skills/touch-orchestrate/SKILL.md:39-48,60-62` vs
`touch-aggregator-plan.md:499-501` (T8: "parse `^\[monitor\] plan=… stage=…
role=… attempt=…` from the oldest transcript's first user record")
**Severity: major**

**Scenario.** The watcher deliberately takes the **last** `[monitor]` match in
the prompt ("earlier text may embed quoted findings that leaked a previous
agent's marker"), using an unanchored `finditer` — so putting `[touch]` on
line 1 and `[monitor]` on line 2 is safe *for the watcher*. But plan item T8
specifies Touch's own label parser as `^\[monitor\] …`; in Python `^` without
`re.MULTILINE` matches only at string start, so as soon as the skill's
mandatory `[touch]` line is prepended, T8's parse never matches and every
workflow node falls back to `agentType` + first-60-chars labelling — silently,
with no test catching it (there is no test yet). Symmetrically, `[touch]`
precedence is undefined: the skill says "FIRST line", but the aggregator will
be handed prompts that quote other prompts (findings text, JSON file lists),
so "first occurrence" and "first line" are not the same rule.

**Recommendation.** Write one normative marker-parsing paragraph into the plan
and mirror it in the skill:
- both markers matched with unanchored `re.search`/`finditer`, `re.MULTILINE`
  never needed;
- `[touch]`: take the **first** occurrence, and additionally assert it is on
  physical line 1 — if it is not, keep the name but flag the node
  `marker-misplaced` (honesty rule D13);
- `[monitor]`: keep **last-wins** (matches the shipped watcher);
- when the two disagree about role/attempt, the harness-derived join
  (`meta.toolUseId` → parent `tool_use`) wins and the node is flagged
  `marker-conflict`; convention-derived fields render dashed per G8.
- Add a test fixture with `[touch]` line 1 + `[monitor]` line 2 + a quoted
  stray `[monitor]` in the body.

---

## SKILLS-7 — No template emits `[touch]`; neither skill references touch-orchestrate, so every conforming run is non-conforming

**File:** `.claude/skills/execute-research/templates/research.workflow.js:66-67,103-104`;
`.claude/skills/implement-plan/templates/implement.workflow.js:88-89,120-121,142-143,232-233,293-294,312-313`;
`execute-research/SKILL.md:48-61`; `implement-plan/SKILL.md:40-66`
**Severity: major**

**Scenario.** `touch-orchestrate` claims to "layer on top of
`execute-research` / `implement-plan` (their invariants still apply)", but the
layering is one-directional prose: neither skill mentions it, and both instruct
the adapter to "keep the `[monitor]` markers and status.sh calls **exactly as
templated**" — which yields prompts whose first line is `[monitor]`, with no
name, no parent, no root, no ledger. Ten prompt sites across the two templates
would each need the extra line. The live proof is in this repo: the adapted
`touch-repo-recon/orch-scripts/research.workflow.js` (written *after* the skill
existed) has zero `[touch]` content. Consequence for Touch: its own
orchestrations render as an unnamed flat list of `agent-<17hex>` nodes, i.e.
the flagship demo of the product fails on the product's own repo.

**Recommendation.** One ordered plan item, "make the skill pair Touch-native":
- add to both templates a pure `touchName(plan, stage, attempt)` helper and a
  `ROOT_NAME` constant, emit line 1 `[touch] name=… parent=… root=… role=…
  attempt=…` and line 2 `[monitor] …` in every prompt builder;
- append a ledger line (SKILLS-5 shape) immediately after each `agent()` call;
- add one binding sentence to both `SKILL.md` Procedure sections: "If the
  `touch-orchestrate` skill exists, its naming / marker / ledger standards
  apply to every spawn in this workflow" — mirroring the existing
  "if `m-orchestrator` does not exist, STOP" convention;
- a static guard test (genre of `test_shell.py`) asserting every prompt
  template in `.claude/skills/**/templates/*.workflow.js` contains both marker
  lines, `[touch]` before `[monitor]`.

---

## SKILLS-8 — Two unmapped identity systems: touch names vs `[monitor] plan=`/`stage=`

**File:** `.claude/skills/touch-orchestrate/SKILL.md:15-35` vs
`research.workflow.js:41-45,67` (`plan=research stage=<key>`) and
`implement.workflow.js:89,233,294` (`plan=sp-<slug>|divide|finalgate`)
**Severity: major**

**Scenario.** The skill's graph is `root → root_research1 → …`; the monitoring
protocol's grouping is `plan` (`research`, `synthesis`, `divide`, `sp-*`,
`finalgate`) × `stage`. The v0 UI builds its tree from touch names (P10,
`touch-monitor-spawn-plan.md:178-192`) while the existing dashboard and every
legacy `events.jsonl` group by plan id — so one run produces two disjoint
graphs with no join, and the legacy-ingest path (D4 `source:"legacy"`) cannot
attach its cards to any named node. Additionally the skill's "N is a per-parent
counter, incremented per spawn" is *non-deterministic under `parallel()`*:
completion order is arbitrary, so a re-run of the same plan yields different
names — breaking "names are logical slots" across restarts.

**Recommendation.** Decide the mapping once in the plan:
`name = <root>_<stage-or-role><N>` where **N is the deterministic index of the
slot in the fan-out list / sub-plan partition, assigned before spawning**, not
a completion-order counter; carry `plan` and `stage` as extra fields on both
the `[touch]` marker (optional) and the ledger line; the aggregator joins
legacy `events.jsonl` cards to named nodes on `(plan, stage, attempt)`.

---

## SKILLS-9 — A stopped agent is indistinguishable from a crashed one, and silently degrades the run

**File:** `research.workflow.js:138-145` (`parallel(...).filter(Boolean)` under
the comment "Barrier is required: synthesis needs ALL reports") and
`implement.workflow.js:183-196` (null result ⇒ "gate agent died" + placeholder
findings)
**Severity: major**

**Scenario.** Once Touch can stop agents, stopping one researcher makes
`agent()` return null; `.filter(Boolean)` drops it and synthesis proceeds with
N-1 reports while its prompt still claims it has all of them — a plan silently
missing a perspective, with the run reported as success. On the implement side,
stopping a test gate writes a placeholder findings file saying "the gate agent
returned no result (crashed / killed)" and the loop burns an attempt. The UI
will show a crash where the user pressed Stop; D7's rule that "Touch's audit is
the only record" is exactly what must arbitrate here, and no code does.

**Recommendation.** Templates must resolve a null `agent()` result against the
control record before labelling it: read the control/ack log (or a
`stopped-names` set the driver maintains) and, if the slot was stopped,
(a) emit `status.sh <plan> <stage> failed "stopped by touch"`, (b) for research
**abort the barrier** (a partial fan-out must not synthesize silently) or mark
the returned plan `partial: true` with the missing perspectives listed,
(c) for implement, do not consume an attempt. Add the corresponding
`kind:"control"` reconciliation to P7 so `stopped` beats `crashed` in the UI.

---

## SKILLS-10 — Ack vocabulary has no mapping into the intent state machine

**File:** `.claude/skills/touch-orchestrate/SKILL.md:78-82`
(`result: stopped|not_found|already_done`) vs
`touch-monitor-spawn-plan.md:144-150` (`requested → sent → confirmed |
expired`)
**Severity: minor**

**Scenario.** P7's machine only knows "an ack line was observed" ⇒ `sent`, then
waits for the agent to go quiet ⇒ `confirmed`, else `expired` at 120 s. An ack
with `result:"not_found"` (bad name, or a workflow agent with no taskId) will
sit in `sent` and then be reported `expired` — indistinguishable from an
orchestrator that never answered. `already_done` will usually confirm by
accident (the agent is already quiet) but for the wrong reason.

**Recommendation.** Publish the mapping in the plan: `stopped` → `sent`
(awaiting observation, then `confirmed`); `already_done` → `confirmed`
immediately with `reason:"already_done"`; `not_found` → terminal `failed`
with the ack's text surfaced; any unknown `result` → terminal `failed`
(never silently `expired`). Test each arm in `tests/test_control.py`.

---

## SKILLS-11 — The skill flatly denies pause; the README requires it and the plan ships it (T15)

**File:** `.claude/skills/touch-orchestrate/SKILL.md:95-96` vs `README.md:5-6`
and `touch-aggregator-plan.md:660-680` (T15 pause gate, v1.5) /
`inception.md:141-148`
**Severity: minor**

**Scenario.** "'Pause' does not exist in any CLI channel (the harness's pause
is kill) — do not promise it". True *for this skill's channel* (Agent tool +
TaskStop + control file), false for the product: the plan's PreToolUse /
SubagentStart hook gate is a verified per-agent pause (held a tool call 20 s).
As written, a future reader of the skill will conclude Touch must not offer
pause and will contradict D7/T15 — or will implement pause and consider the
skill stale. Product requirement, plan and skill must not disagree in prose.

**Recommendation.** Reword to: "No pause exists in the *spawn/control-file*
channel — this skill's stop is a kill, and a stopped slot restarts as a fresh
attempt. The only honest pause is the hook gate (plan T15), which is outside
this skill's scope." Keep the rest of the "Why these exact standards" section
as-is; it is the most valuable part of the file.

---

## SKILLS-12 — `<task-dir>` is undefined for the ad-hoc spawns the skill claims to cover

**File:** `.claude/skills/touch-orchestrate/SKILL.md:10-13,51-52,65-67`
**Severity: minor**

**Scenario.** The skill "works for ad-hoc spawns too", but every state path it
mandates (`<task-dir>/state/spawn-ledger.jsonl`,
`<task-dir>/state/<name>.json`, the control fallback) is anchored on a
`<task-dir>` that only exists when an orchestrator skill created
`.claude/local-orchestrators/<task-name>/`. An ad-hoc `[touch]`-conforming
spawn therefore has nowhere defined to write, and the model will invent a path
(often `~/.claude/…`, which §3 bans, or the repo root).

**Recommendation.** Define the default in the skill: when no orchestrator task
folder exists, `<task-dir>` = `${TOUCH_STATE_DIR:-<repo-root>/.touch}/
orchestrations/<ROOT_NAME>/`. Combined with SKILLS-5 this makes every path in
the skill resolvable without an orchestrator.

---

## SKILLS-13 — The description→name parse rule is unspecified (em dash, free text)

**File:** `.claude/skills/touch-orchestrate/SKILL.md:45-48` vs
`touch-monitor-spawn-plan.md:129-133` ("name from `description`")
**Severity: minor**

**Scenario.** The skill mandates `description: "<name> — <short task>"` with a
U+2014 em dash; P6 says the aggregator takes the name "from `description`" with
no grammar. Implementers will split on `-`, on `—`, or on the first space, and
descriptions that omit the separator (or use `-`) will yield garbage display
names bound to real `agentId`s — poisoning the join table that also feeds
`(name, attempt) → agentId` uniqueness.

**Recommendation.** Normative regex in the plan:
`^(?P<name>[a-z][a-z0-9_]*)\s*[—-]\s+` — anything else is "unnamed
(unconventional)", never a guessed name. The `[touch]` marker in the prompt
always wins over the description when both parse; the description is the
fallback for pre-marker or truncated prompts.

---

## SKILLS-14 — Attempt-number authority is split between the workflow and the control plane

**File:** `.claude/skills/touch-orchestrate/SKILL.md:27-32,83` vs
`implement.workflow.js:168-176` and `research.workflow.js:67` (attempt hardcoded
to 1)
**Severity: minor**

**Scenario.** The skill's identity invariant is "each (name, attempt) pair binds
to exactly one harness `agentId`", and "a stopped slot may be re-run only as a
fresh spawn with attempt + 1". The implement loop happens to satisfy this (a
null result falls through `continue` and `attempt++`), but the research
template hardcodes `attempt=1` in the prompt, so any re-spawn of a stopped or
crashed researcher produces a **second** agentId for `(name, 1)` — the
aggregator's uniqueness assertion (P6 test: "(name, attempt) → agentId
uniqueness") then fails on a legitimate run and will either drop a node or
overwrite the earlier one.

**Recommendation.** Templates must carry a per-slot attempt counter (research
included) and always increment on respawn; the aggregator keys nodes on
`(name, attempt, spawnSeq)` and renders a duplicate `(name, attempt)` as a
visible `identity-collision` flag rather than silently collapsing — the
collision is real data about a non-conforming orchestrator.

---

## SKILLS-15 — Adopting background Agent-tool spawns silently disables the deterministic monitoring both skills mandate

**File:** `.claude/skills/m-orchestrator/SKILL.md:24-28,43-58` vs
`.claude/skills/touch-orchestrate/SKILL.md:49-51,60-62`
**Severity: major**

**Scenario.** `decision_watcher.py` derives every spawn / verdict / retry /
token event from a **Workflow run's `journal.jsonl`**; that is why
`m-orchestrator` calls the `[monitor]` marker "the DETERMINISTIC event source …
with zero LLM cooperation". Background Agent-tool spawns write no journal at
all, so under touch-orchestrate's mandated spawn mechanism the watcher produces
nothing, the dashboard cards stay `queued`, and the only remaining events are
the best-effort `status.sh` calls the skills explicitly label "best-effort
color, not the source of truth". Yet both `execute-research` and
`implement-plan` still say "Monitoring: per the `m-orchestrator` skill … if
that skill does not exist, STOP". The three skills cannot all be honest at once.

**Recommendation.** State in the plan (global decision) that:
- Workflow-profile runs are observed by `decision_watcher.py` (journal) **and**
  by Touch's aggregator (transcripts + `.meta.json`);
- Agent-tool-profile runs are observed by the Touch aggregator only —
  `decision_watcher.py` is out of scope for them, and `status.sh` events remain
  color;
- the per-profile monitoring statement is added to the `Monitoring` section of
  both orchestration skills so nobody expects journal events that cannot exist.

---

## SKILLS-16 — Background spawns lose the typed structured output the gated loops branch on

**File:** `.claude/skills/touch-orchestrate/SKILL.md:49-51` vs
`implement.workflow.js:45-61,176-205` (IMPL/GATE/CRIT schemas;
`success = gate.passed && crit.approved`) and
`research.workflow.js:47-64,139-156`
**Severity: major**

**Scenario.** Every control-flow decision in both templates reads a
**schema-validated** return value from `agent()` (`impl.done`,
`gate.passed`, `crit.approved`, `synth.plan_file`, `synth.item_count`, and the
hard `throw` when synthesis returns no plan). Background Agent-tool spawns
return their result as a task notification into the parent's context — text the
*model* must read and act on, not a validated object a script branches on.
Migrating the skill pair to background spawns (the naive reading of
touch-orchestrate §2) therefore converts a deterministic orchestrator into a
model-mediated one and voids the "the loops are a pure function of the divider's
output" invariant (`implement-plan/SKILL.md:41-49`).

**Recommendation.** Fold this into the SKILLS-1 decision: the Workflow profile
must remain the default for the skill pair *because* of the typed-return
requirement, and the Agent-tool profile is for ad-hoc / user-driven spawns where
stoppability outweighs determinism. If both are wanted at once, the plan needs
an explicit item: a structured-output handoff file (`state/<name>.result.json`
written by the agent, schema-validated by the parent) as the background
profile's replacement for `agent()`'s typed return.

---

## SKILLS-17 — Watcher comment contradicts the templates about marker placement

**File:** `.claude/shared/monitoring/decision_watcher.py:340-342`
("the orchestrator script appends its marker at the end of the prompt")
vs `research.workflow.js:67` / `implement.workflow.js:89` (marker is line 1)
**Severity: nit**

**Scenario.** Harmless today (last-wins protects against quoted markers either
way), but it is the one comment a future implementer of Touch's own parser will
read to decide precedence, and it describes a layout that no template uses.
With `[touch]` about to become line 1 (SKILLS-6/-7), a wrong mental model here
is cheap to fix and expensive to inherit.

**Recommendation.** Correct the comment to "the marker is the first line of
script-authored prompts; last-occurrence-wins guards against quoted findings
that embed an earlier agent's marker" when the templates are touched for
SKILLS-7, and keep `finditer`-last behaviour unchanged (it is load-bearing).

---

## Cross-cutting summary — what the implementation must build for the skills to be honest

1. One canonical, resolvable path set: `.touch/control.jsonl`,
   `.touch/control-ack.jsonl`, `.touch/spawn-ledger.jsonl`, with
   `TOUCH_STATE_DIR` and a defined repo root (SKILLS-3/-4/-5/-12).
2. One marker specification — placement, precedence, conflict handling, plus a
   fixture test (SKILLS-6).
3. Touch-native templates: `[touch]` line 1, `[monitor]` line 2, deterministic
   names, ledger appends, static guard test (SKILLS-7/-8/-14).
4. A two-profile spawn standard with stop declared *unavailable* where it truly
   is, and a hook-based deterministic stop for the blocked-session case
   (SKILLS-1/-2/-15/-16).
5. Stop/crash disambiguation wired end-to-end: control record beats inference,
   ack vocabulary mapped, barrier failures surfaced (SKILLS-9/-10).
6. Prose reconciliation so README (pause), plan (T15) and skill do not
   contradict each other (SKILLS-11).
