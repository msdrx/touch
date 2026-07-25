# research-skills — the four skills as ONE spawn/monitor/control protocol

Perspective: `execute-research`, `implement-plan`, `m-orchestrator`,
`touch-orchestrate` + both `templates/*.workflow.js` read as the normative
protocol Touch must **render and drive** (task → plan → sub-plan → agent →
attempt → gate). Focus: what the protocol actually emits at runtime versus what
the prose promises, and which gaps break a UI or a controller.

Files read in full: the four `SKILL.md`s, both templates, this run's adapted
`touch-full-recon/orch-scripts/research.workflow.js`, `decision_watcher.py`,
`status.sh`, `monitoring.md` (skimmed for the event contract), `inception.md`,
and the completed `touch-aggregator` / `touch-repo-recon` event streams.

**Empirical work (throwaway dirs only; no writes outside this findings file and
the two mandated `status.sh` calls):**

1. Replayed real history: `touch-aggregator/events.jsonl` (the successful
   6-researcher + synthesis run).
2. Built two synthetic Workflow journals + transcripts under
   `/tmp/claude-1000/.../scratchpad/wfsim{,2}` and ran the **shipped**
   `decision_watcher.py` against them (`ORCH_STATE_DIR`, `ORCH_WF_GLOB_ROOT`
   and `ORCH_QUIET_SECS` all pointed into /tmp) — one simulating a *fully green*
   serial `implement-plan` run (divide → sp-a impl/test/critique → finalgate),
   one simulating the PARALLEL sub-plan shape.
3. Called `TaskList` while six Workflow-spawned researchers (this run) were
   live: **"No tasks found"** — re-confirming that Workflow agents are not
   harness tasks.

Prior-run ids from `touch-repo-recon/findings/research-skills-attempt-1.md` are
cited as `(prior SKILLS-n)` where an item is the same defect; the skills have
not changed since (mtimes: skill pair 02:03, touch-orchestrate 13:24), so every
prior finding still applies verbatim — this report adds runtime-verified
defects the prior run did not reach.

---

## SKILLS-1 — A fully green `implement-plan` run is reported as FAILED; every plan whose agents return no `passed`/`approved` closes red

**File:** `.claude/shared/monitoring/decision_watcher.py:639-652` and `:450-467`;
`.claude/skills/implement-plan/templates/implement.workflow.js:232-253`
(divider prompt, no `plan done` status call);
`.claude/skills/execute-research/templates/research.workflow.js:66-92`
(researcher prompt, same omission)
**Severity: blocker**

**Scenario (verified twice — real history and simulation).** The watcher closes
a plan card when the *next* plan's first agent spawns:

```python
st = "done" if state["decisive"].get(prev) else "failed"   # :646
```

`decisive` is only ever set from a result containing `passed` or `approved`
(`:689-691`). Researchers return `{findings, findings_file, summary}`; the
divider returns `{subplans, subplans_file, summary}`. Neither key exists, so
`decisive[plan]` stays `None` and the card closes **failed** on a perfect run.

Real history, `touch-aggregator/events.jsonl` (the run that produced the
normative plan, all six researchers succeeded):

```
03:16:40 research  plan failed  loop exited -> synthesis
03:26:31 synthesis plan done    plan written        # only because synthPrompt calls status.sh
```

Simulated green serial implement run (`wfsim2`, shipped watcher, all agents
returning success):

```
divide       plan     failed  loop exited -> sp-a
sp-a         plan     done    critique attempt 1 green
finalgate    plan     done    test attempt 1 green
orchestrator complete failed  run failed: 3 plan(s) closed with failures
```

So the **whole run** is declared failed (`run_outcome`'s
`all(v == "done" for v in state["plans"].values())` at `:463-466` sees the red
`divide` card). Touch renders exactly these entities: its flagship view would
mark the Research phase and the Divide phase of every successful run as failures
and the run as failed. `synthesis` escapes only because `synthPrompt` explicitly
emits `status.sh synthesis plan done` (`research.workflow.js:132`) — no other
prompt in either template does.

**Recommendation** (one plan item spanning watcher + both templates):
1. Watcher: track a third state. A plan whose agents produced results but no
   decisive verdict must settle **`done`** if its last result was not a failure,
   and only `failed` on an actual failure/`None` result. Concretely, record
   `state["last_result_ok"][plan]` in the `result` branch and use
   `decisive.get(p) if p in decisive else last_result_ok.get(p, False)` in both
   `:646` and the quiet-close settle loop `:748-753`; make `run_outcome` use the
   same predicate.
2. Templates: mandate a terminal `status.sh <plan> plan done "<summary>"` for
   *every* plan — add it to `dividePrompt` (`implement.workflow.js:251`) and,
   for the research fan-out (which has no "last" agent), emit it from the script
   after the barrier (`research.workflow.js:145`) via a shell call, not from an
   agent.
3. Test: extend `tests/test_watcher.py` with a journal fixture whose only
   results are `{findings…}` / `{subplans…}` and assert the plan badge closes
   `done` and `complete` is `done`.

---

## SKILLS-2 — `execute-research` runs never emit a completion event; the orchestrator badge spins "running" forever in replay

**File:** `.claude/shared/monitoring/decision_watcher.py:450-467`;
`.claude/skills/execute-research/templates/research.workflow.js:159-161`
(comment only, no code); `.claude/skills/execute-research/SKILL.md:62-69`
**Severity: major**

**Scenario.** `run_outcome` returns `None` (never terminal) whenever a still-open
plan has `decisive` unset:

```python
if still_open and not all(state["decisive"].get(p) is False for p in still_open):
    return None     # decisive.get(p) is None, not False -> always returns here
```

For a research run the final plan is `synthesis`, whose synth result carries no
`passed`/`approved` — so the watcher's "deterministic" completion never fires.
The template does not emit the closing event either (lines 159-161 are a comment
saying the driver may do it), so the ONLY channel is the model driver
remembering to run `status.sh orchestrator complete done`.

Verified in real history: `touch-aggregator/events.jsonl` (590 events, run
finished 03:26:31Z) contains exactly one `stage:"complete"` event and it is
`state:"running"` ("touch-aggregator research starting"). That completed run
replays today as **still running** — and `m-orchestrator/SKILL.md:76-79` plus
the watcher docstring (`:731-737`) both claim the badge closes deterministically.

**Recommendation.** Make the workflow script itself emit the terminal event
(a `bash` call at the end of both templates, not a comment), keep the driver's
call as an idempotent backstop, and fix `run_outcome` per SKILLS-1 so
watcher-detected completion actually works for research-shaped runs. Touch must
additionally treat "no `complete` event + journal quiet" as *unknown*, never as
running (honesty rule D13), because history predating the fix has no such event.

---

## SKILLS-3 — PARALLEL sub-plan mode makes plan cards flap to "failed"; a card closed red is never reopened

**File:** `.claude/shared/monitoring/decision_watcher.py:639-663`;
`.claude/skills/implement-plan/templates/implement.workflow.js:274-277`
(`PARALLEL_MODE`), `.claude/skills/implement-plan/SKILL.md:20-23`
**Severity: major**

**Scenario (simulated, `wfsim`).** `implement-plan` explicitly supports parallel
sub-plans. The watcher's sequencing rule assumes serial loops: any spawn for a
plan different from `last_plan` closes the previous plan's card. With two live
sub-plans the spawns interleave, so each one repeatedly "closes" the other:

```
sp-a plan running  first agent spawned
sp-a plan failed   loop exited -> sp-b      # sp-a was still running
sp-b plan running  first agent spawned
sp-b plan failed   loop exited -> sp-a      # sp-b was still running
sp-a plan done     test attempt 1 green
sp-b plan done     test attempt 1 green
```

The card recovered here only because a `passed:true` result later arrived. The
reopen branch (`:653-663`) triggers **only** when the stored badge is `"done"` —
a card wrongly closed `"failed"` has no reopen path at all, so a sub-plan that is
mid-attempt (impl running, no verdict yet) when a sibling spawns stays red until
(and unless) a decisive green lands. `DRIVER-1`'s guard at `:610-616` protects
per-agent rows from stale-closing in fan-outs but nothing protects the plan
badges. Touch's per-terminal graph is exactly this data.

**Recommendation.** Delete the `last_plan` heuristic in favour of explicit
lifecycle events: a plan card closes only on (a) an explicit `status.sh <plan>
plan done|failed` from the script, or (b) the quiet-close settle pass. If the
heuristic must stay for legacy streams, gate it on a serial-mode declaration in
`orch-config.json` (`"strategy": "serial"|"parallel"`, written by the adapted
script) and make the reopen branch fire for `failed` as well as `done`. Add a
`test_watcher.py` fixture with two interleaved plans asserting no spurious
badge event.

---

## SKILLS-4 — Divider-returned `sp.id` is unvalidated LLM text used as a filesystem path, a shell argument, and a monitoring plan id

**File:** `.claude/skills/implement-plan/templates/implement.workflow.js:213-230`
(schema), `:263-269` (isolation guard — files only), `:117-118`
(`${FINDINGS}/${sp.id}-test-attempt-${attempt}.md`), `:42-43` (`statusCmd`
interpolation), `:89` (`[monitor] plan=${sp.id}`)
**Severity: major**

**Scenario.** The Fable divider's structured output is trusted verbatim. The
script validates only that files are uniquely owned; `id` is never checked
against the `sp-<slug>` shape the prompt asks for. Three concrete failures:

- `id: "../../../../tmp/pwn"` → gate/critique findings files are written outside
  `FINDINGS` (the prompts instruct `mkdir -p` and a write to that exact path).
- `id: "orchestrator"` → every status event and marker for that sub-plan lands
  on the **reserved** orchestrator card (`monitoring.md`; watcher `emit()`
  default plan), corrupting the run badge and token totals.
- `id` containing a double quote → `statusCmd`'s `"${plan}"` interpolation
  breaks the generated shell command; the agent's mandated first command fails
  and the card never opens.

None of these need malice — a divider that emits `sp-auth/api` (a slug with a
slash) is enough to scatter findings into a nonexistent directory.

**Recommendation.** After the divider returns, enforce
`/^sp-[a-z0-9]+(-[a-z0-9]+)*$/` on every id (throw with the offending id),
reject the reserved ids `orchestrator`, `divide`, `finalgate`, `research`,
`synthesis`, and assert ids are unique. Same guard belongs in the aggregator:
Touch must never join a node on an unvalidated id. Test: divider fixture
returning a traversal id ⇒ workflow throws before any agent spawns.

---

## SKILLS-5 — No machine-readable topology exists, so Touch can only draw what has already spawned

**File:** `.claude/skills/m-orchestrator/SKILL.md:40-45` ("Seed one card per
sub-plan before launching") vs `.claude/skills/implement-plan/SKILL.md:72-75`
("per-sub-plan cards are created by each loop's first status.sh call — the
partition exists only after Divide"); `implement.workflow.js:248-252` (partition
written as **prose markdown**); `research.workflow.js:41-45` (PERSPECTIVES
exists only inside the script)
**Severity: major**

**Scenario.** The product requirement is an n8n-like graph of the run with
controls. But at runtime nothing on disk declares the graph: the perspective
list and `MAX_ATTEMPTS` live in a JS constant; the sub-plan partition is returned
as a workflow value (never persisted mid-run — `inception.md:102-113`: the rich
`<sid>/workflows/<runId>.json` is written only on the completion path) and
otherwise only as a human-readable `plan/*-subplans.md`. `agent()`'s `label` and
`phase`, and `meta.phases` (`Divide/Implement/Test/Critique/FinalGate`), are
never persisted either. Consequences: no queued skeleton, no "3 of 7 sub-plans
done", no owned-file list per node, no expected-attempt budget, no swimlanes —
Touch can only draw nodes that already exist, which is exactly the "quiet run
shows nothing" case D13 forces it to admit. The two skills also **contradict**
each other on the seeding contract (m-orchestrator mandates what implement-plan
declares impossible).

Related, cheap: `statusCmd` never sets `ORCH_TITLE`, though `status.sh:5,41-43`
supports it and the divider returns a human `title` per sub-plan — so every card
shows a raw id (`sp-foo`, `research`) instead of the title that exists.

**Recommendation.**
- Both templates write `<task-dir>/state/topology.json` at the moment the
  deterministic list is known (research: before the fan-out; implement:
  immediately after Divide) — `{task, strategy, max_attempts, phases[],
  plans:[{id, title, kind:"research|subplan|gate", files[], finding_ids[],
  expected_stages[]}]}`. This is the UI's skeleton and the controller's
  addressing table.
- Immediately after writing it, seed `ORCH_TITLE="<title>" status.sh <id> plan
  queued` per plan, which also fixes the titles and satisfies m-orchestrator's
  step 2 honestly. Amend `m-orchestrator/SKILL.md:40-45` to "seed after the
  partition is known".
- Test: run the adapted script's Divide phase against a stub divider and assert
  `topology.json` matches the returned partition.

---

## SKILLS-6 — "Restart a loop" is unimplementable: sub-plan identity is re-derived by an LLM on every run and there is no re-entry point

**File:** `.claude/skills/implement-plan/templates/implement.workflow.js:255-269`
(Divide always runs; no `only`/`subplans_file` arg), `:32-37` (ARGS = plan_file,
parallel only); `research.workflow.js:41-45,139-144` (PERSPECTIVES constant, no
`only`); `README.md:5-7` and `inception.md:19-25` (pause, **restart**, start,
terminate agent loops)
**Severity: major**

**Scenario.** The product's restart verb is defined over "loops … exactly those
defined by execute-research and implement-plan". Re-running
`implement.workflow.js` re-executes the Fable divider from scratch: the
partition is a fresh LLM judgement, so `sp-<slug>` ids, titles and file
ownership can all differ from the run being restarted. There is no argument to
skip Divide, none to run a single sub-plan, and no per-perspective re-run for
research. Therefore "restart sp-auth" cannot be expressed at all, and any UI
button offering it either lies or silently re-runs everything (burning the whole
change-set through fresh implementers). `inception.md:159-163` already narrows
restart to a typed `Workflow({scriptPath, resumeFromRunId})` — but that replays
agents without re-executing them, i.e. it does not restart a loop either.

**Recommendation.** Make the partition durable and addressable, then re-entrant:
1. Divide writes `plan/<name>-subplans.json` (machine-readable, alongside the
   existing `.md`) — the SAME artifact as SKILLS-5's topology, or referenced by it.
2. `args` gains `{ subplans_file?, only?: string[], from_attempt?: number }`:
   when `subplans_file` is given the Divide phase is **skipped** and the stored
   partition is used verbatim (ids stable across runs); `only` restricts the
   loop set; the final aggregate gate still sweeps the merged change-set.
3. `research.workflow.js` gains the mirror: `{ only?: string[] }` over
   PERSPECTIVES, and writes the perspective list into the same topology file.
4. Both `SKILL.md` Procedure sections document restart as "re-invoke with
   `{subplans_file, only:[id]}`", which is what Touch's Restart button issues.
Test: run with a stored partition + `only:["sp-b"]` and assert no divider agent
was spawned and exactly one loop ran.

---

## SKILLS-7 — Critique and the final scope audit see only the LAST attempt's changed files; earlier-attempt edits escape review

**File:** `.claude/skills/implement-plan/templates/implement.workflow.js:148`
(`Review ONLY the diff of: ${JSON.stringify(impl.files_changed)}`), `:126`
(gate), `:207-209` (`files_changed: (impl && impl.files_changed) || []` — last
attempt only), `:289` (`allFiles`), `:299-303` (final gate scope audit)
**Severity: major**

**Scenario.** Attempt 1 edits `a.py` and `b.py`, the gate fails on `b.py`;
attempt 2 (a brand-new agent) fixes `b.py` only and returns
`files_changed: ["b.py"]`. `runLoop` returns just that list, so:

- the attempt-2 critique reviews the diff of `b.py` alone — `a.py`'s change,
  never approved by anyone, ships unreviewed;
- `allFiles` omits `a.py`, so the final gate's "no edits outside the planned
  files" audit (step 4) flags `a.py` as an **unplanned** edit, and the
  final-gate fixer is told it may only edit files "within the planned
  change-set (so far: …)" — i.e. it is forbidden to fix the very file that
  failed the audit.

Both halves are silent: the run closes green with an unreviewed file, or the
final gate fails with a misleading scope violation.

**Recommendation.** Accumulate per-loop: keep `touchedFiles = new Set()` across
attempts, union each `impl.files_changed` into it, hand the union to the gate,
the critique and the loop result. Additionally derive the ground truth from
`git status --porcelain` in the gate rather than trusting the implementer's
self-report, and cross-check the two (a mismatch is itself a finding). Test:
two-attempt fixture asserting the attempt-2 critique prompt names both files.

---

## SKILLS-8 — A failed or killed implementer leaves no findings file, so the next attempt re-runs the identical prompt

**File:** `.claude/skills/implement-plan/templates/implement.workflow.js:172-176`
(`if (!impl || !impl.done) { continue }`) vs `:183-186`, `:193-196`
(gate/critique get `writePlaceholderFindings`)
**Severity: major**

**Scenario.** Gate and critique deaths are handled: a placeholder findings file
is written so the next implementer has a handoff. The implementer path has no
such treatment — `continue` skips straight to `attempt++` with `openFindings`
unchanged, so attempt 2 receives a prompt byte-identical to attempt 1 (no
`findingsFiles` block at `:105-108`), with nothing in task state explaining the
failure. The loop can burn all four attempts, produce no findings file, and
return `success:false` whose `open_findings` is empty — the operator (and Touch)
sees four dead implementers and zero recorded reason. Once Touch can *stop* an
agent (prior SKILLS-9) this becomes the common case: a stopped implementer is
indistinguishable from a crashed one **and** consumes an attempt.

**Recommendation.** Mirror the gate handling: on `!impl || !impl.done` write
`${FINDINGS}/${sp.id}-impl-attempt-${N}.md` recording what was known (returned
`summary` if any, else "implementer died/was stopped"), push it into
`openFindings`, and emit `status.sh <sp.id> implement failed "attempt N: no
result"` so the event stream carries the fact. Decide explicitly whether a
*stopped* (vs crashed) implementer consumes an attempt — Touch's control audit
is the only source that can tell them apart, so the templates must consult it
(one shared decision with prior SKILLS-9).

---

## SKILLS-9 — Final-gate decision lines describe a loop shape the template does not have

**File:** `.claude/shared/monitoring/decision_watcher.py:386-389`
(`role == "test"` branch) vs
`.claude/skills/implement-plan/templates/implement.workflow.js:293-294,330-349`
(`plan=finalgate role=test`, hardcoded 2 rounds, fixer not critique)
**Severity: minor**

**Scenario (observed in `wfsim2`).** The watcher emits, for the aggregate gate:

```
orchestrator finalgate done  finalgate test #1 PASS -> spawn critique
```

No critique is ever spawned after the final gate — it is the terminal step. On
failure the line reads "FAIL -> critique will reject; feedback loops" while the
script actually spawns a **fixer** (`role=impl`) and re-gates once. The
`MAX_GATE_ATTEMPTS`/`MAX_E2E_ATTEMPTS` caps the watcher reads from
`orch-config.json` (`:107-109`) are unreachable for this plan because the
`role == "test"` branch wins before the cap-aware branch at `:403-414`. A UI
replaying decision text shows a phantom step.

**Recommendation.** Key the decision text on `(plan, role)`, not `role` alone:
treat `plan == "finalgate"` (or a `kind` field added to the marker) as the
aggregate-gate shape — "PASS -> run complete" / "FAIL -> spawn fixer, re-gate
N/2". Publish the final-gate round cap in `orch-config.json` alongside the
others and read it. Test: journal fixture with `plan=finalgate role=test`
asserting the emitted detail mentions no critique.

---

## SKILLS-10 — `MAX_ATTEMPTS` is script-private; the watcher's caps come from a file nothing writes, and exhaustion emits no event

**File:** `.claude/skills/implement-plan/templates/implement.workflow.js:30`
(`MAX_ATTEMPTS = 4`), `:168-206`, `:284` (`log(...)` only);
`.claude/shared/monitoring/decision_watcher.py:106-109`;
`.claude/skills/implement-plan/SKILL.md:75-78` ("publish your MAX_ATTEMPTS
there if different")
**Severity: minor**

**Scenario.** The cap that governs the loop lives in the JS constant; the cap
the watcher prints ("retry attempt 3/4", "attempts exhausted") comes from
`orch-config.json`, which no template ever writes — it is a manual step in prose.
Any adapter that raises `MAX_ATTEMPTS` gets decision lines announcing exhaustion
while the loop keeps retrying (or vice versa). Worse, when the loop *does*
exhaust, the only trace is `log()` into the workflow transcript: the last event
on the card is the critique's "rejected", identical to a retry-in-progress, so
neither the dashboard nor Touch can distinguish "gave up" from "still going".

**Recommendation.** The adapted script writes/merges `orch-config.json`
(`max_plan_attempts` = its own `MAX_ATTEMPTS`, plus the final-gate cap and
`strategy` from SKILLS-3/-5) before starting the daemons, and emits
`status.sh <sp.id> plan failed "attempts exhausted N/N"` when `runLoop` exits
unsuccessfully. Static guard test: every `templates/*.workflow.js` that defines
`MAX_ATTEMPTS` also writes it into `orch-config.json`.

---

## SKILLS-11 — The documented `stage=`-omission fallback is dead against the templates' own quoting

**File:** `.claude/shared/monitoring/decision_watcher.py:122-124` (`STAGE_HINT`)
vs `.claude/skills/m-orchestrator/SKILL.md:55-56,66-67` vs
`research.workflow.js:30-31` / `implement.workflow.js:42-43` (`bash "${S}"`,
`"${plan}"`)
**Severity: minor**

**Scenario (verified).** `m-orchestrator` says `stage=` may be omitted from the
marker because the mandated `status.sh … running` line names the stage. Its
regex requires whitespace immediately after `status.sh`:

```python
STAGE_HINT = re.compile(r"status\.sh\s+\S+\s+(\S+)\s+running")
```

Both templates render `bash "/…/status.sh" "research" skills running …` — the
quote after `status.sh` defeats `\s+`. Checked directly: the templated form
yields `[]`, the doc's unquoted form yields `['<stage>']`. So an adapter that
follows the documented shortcut silently gets `stage = role` (`:355`), and every
chip in the run is labelled `research`/`impl` instead of the real stage — with
no error anywhere.

**Recommendation.** Relax the regex to tolerate quoting
(`status\.sh"?\s+"?\S+?"?\s+"?([\w:-]+)"?\s+running`) **and** add a
`tests/test_watcher.py` case built from the literal string the shipped
`statusCmd` produces. Alternatively delete the shortcut from
`m-orchestrator/SKILL.md` and make `stage=` mandatory in the marker — Touch's
own parser then needs one rule, not two.

---

## SKILLS-12 — Perspective keys and sub-plan ids can silently collide with reserved stage/plan names

**File:** `.claude/skills/execute-research/templates/research.workflow.js:41-45,71`
(`statusCmd('research', p.key, …)`); `implement.workflow.js:89,92` ;
`.claude/shared/monitoring/monitoring.md` (reserved: plan `orchestrator`;
stages `plan`, `complete`, `tokens`)
**Severity: minor**

**Scenario.** A perspective keyed `plan` (entirely plausible — this very run has
`plans`) renders `status.sh research plan running …`, which the schema defines
as the **card badge**, not a stage chip: the research card would flip to
`running`/`done` on that one researcher's progress and its chip would never
appear. A key of `tokens` would be parsed as a token delta with no `tokens`
object. Nothing in either template or SKILL.md warns about the reserved
vocabulary, and `status.sh` accepts any string.

**Recommendation.** Reject reserved names at the top of both templates
(`for (const p of PERSPECTIVES) if (['plan','complete','tokens'].includes(p.key)) throw …`,
and the same for sub-plan ids per SKILLS-4), and state the reserved list in
`m-orchestrator/SKILL.md` next to the event schema. Add it to the static guard
test in `tests/test_shell.py`.

---

## SKILLS-13 — `touch-orchestrate`'s mandatory spawn discipline is unsatisfiable for the very loops it targets (re-verified live)

**File:** `.claude/skills/touch-orchestrate/SKILL.md:37-57` vs
`research.workflow.js:139-144` and `implement.workflow.js:172-196`;
`execute-research/SKILL.md:36-38`, `implement-plan/SKILL.md:27-30`
**Severity: major** *(same defect as prior SKILLS-1/-7; re-confirmed this run)*

**Scenario.** §2 mandates, with no exceptions: Agent-tool spawn with
`run_in_background`, then a ledger line carrying `taskId`, so `TaskStop` can kill
each agent. I called `TaskList` while six Workflow-spawned researchers were
running (this run) → **"No tasks found"**. Workflow agents have no `taskId`, no
`TaskStop` handle, and (`inception.md:90-96`) only a stub `.meta.json` with no
description — so both name channels and the whole stop mechanism are
unavailable for the skill pair, which spawns exclusively through Workflow
`agent()`. Neither `execute-research/SKILL.md` nor `implement-plan/SKILL.md`
mentions `touch-orchestrate`; both say to keep the markers "exactly as
templated", so no adapted script emits `[touch]` — verified again in this run's
`touch-full-recon/orch-scripts/research.workflow.js:94-95`, written *after* the
skill existed. Touch's own orchestrations therefore render as unnamed
`agent-<17hex>` nodes with disabled-in-reality Stop buttons.

**Recommendation.** Unchanged from prior SKILLS-1/-7 and still the single most
important protocol decision: give `touch-orchestrate` two explicit profiles
(Agent-tool: background + `taskId` + `TaskStop`; Workflow: marker-only identity,
ledger with `"taskId": null` + `{wfRunId, wfKey, plan, stage}`, **stop declared
unavailable** and rendered disabled with the reason), add `[touch]` line 1 to all
ten prompt sites in the two templates plus a `touchName()` helper and ledger
append, and add the binding "if `touch-orchestrate` exists, its standards apply"
sentence to both Procedure sections (mirroring the existing m-orchestrator STOP
convention). Static guard test over `.claude/skills/**/templates/*.workflow.js`.

---

## SKILLS-14 — Stop intents cannot be polled while the driver is blocked in the Workflow call, and a stopped agent is indistinguishable from a crashed one

**File:** `.claude/skills/touch-orchestrate/SKILL.md:72-83,95-96` vs
`research.workflow.js:139-144` (`.filter(Boolean)` behind "Barrier is required:
synthesis needs ALL reports") and `implement.workflow.js:183-196` ("gate agent
died")
**Severity: major** *(same defect as prior SKILLS-2/-9; restated because it is
load-bearing for the plan)*

**Scenario.** §4's control loop runs "between steps", but during a skill run the
driver session is inside ONE `Workflow` tool call for the entire run — zero
turns, zero polls, so every intent sits unacknowledged (and the v0 plan expires
it at 120 s). Even if a stop lands, the templates cannot tell stop from crash: a
stopped researcher makes `agent()` return null, `.filter(Boolean)` drops it, and
synthesis proceeds on N-1 perspectives while reporting success; a stopped gate
gets a placeholder findings file that says "crashed / killed".

**Recommendation.** As prior SKILLS-2/-9: replace `expired` with "pending —
orchestrator busy", make the hook-based deterministic stop a v0 item (it is the
only channel that acts inside a blocked session), and have the templates
reconcile every null `agent()` result against the control/ack log before
labelling it — aborting the research barrier (or returning `partial:true` with
the missing perspectives) rather than silently synthesizing a partial plan.

---

## SKILLS-15 — The event stream carries no model, agent-type or phase, so Touch cannot render the very pinning the skills treat as normative

**File:** `.claude/shared/monitoring/decision_watcher.py:121` (marker fields:
plan/stage/role/attempt only), `:636-638` (`agent.label = "<role> #<attempt>"`);
`research.workflow.js:141-152` (`model: 'opus'` / `'fable'`, `phase:`, `label:`);
`implement.workflow.js:15-24` (`meta.phases`), `:256-259`, `:333-335`
**Severity: minor**

**Scenario.** Both skills make model assignment normative ("everything opus
EXCEPT the two fable agents"), and `inception.md:105-108` records that
`agent()`'s `label`/`phase` are never persisted mid-run. Nothing carries model or
phase into `events.jsonl`: the fable divider (`role=synth`) is indistinguishable
from a research synthesizer, and the fable final-gate reviewer appears as
`role=test` exactly like an opus sub-plan gate. A UI cannot show model pinning,
cannot colour by phase, and cannot verify the invariant the skills care most
about.

**Recommendation.** Extend the marker to
`[monitor] plan=… stage=… role=… attempt=… model=… phase=…` (backwards
compatible — the watcher's regex keeps the leading fields required and the new
ones optional), emit both from a single `markerLine()` helper in each template,
and pass them through into the `agent` sub-object the watcher attaches. Test:
marker fixture with and without the new fields.

---

## SKILLS-16 — The file-ownership isolation guard is raw string equality

**File:** `.claude/skills/implement-plan/templates/implement.workflow.js:263-268`
**Severity: nit**

**Scenario.** `owner[f]` keys on the divider's literal string, so `src/a.py`,
`./src/a.py` and `/abs/repo/src/a.py` are three different owners of one file —
the "one file, exactly one owner" invariant the whole isolation model rests on
(`implement-plan/SKILL.md:41-49`) is defeated by path-form variance, and two
implementers can be told they own the same file. It also leaves Touch with mixed
path forms to display and to join against `git status`.

**Recommendation.** Normalize before the guard: resolve every entry relative to
`REPO` (`path.resolve(REPO, f)`), store the repo-relative form, and reject any
path escaping `REPO`. Reuse the same normalization in the topology file
(SKILLS-5) so the UI has one canonical path form. Test: divider fixture
returning `a.py` and `./a.py` in two sub-plans ⇒ the guard throws.

---

## Still-valid prior findings (not restated in full)

The skills are byte-identical to when `touch-repo-recon` reviewed them (only
`touch-orchestrate/SKILL.md` has a later mtime, 13:24, still predating that
run), so every finding in
`.claude/local-orchestrators/touch-repo-recon/findings/research-skills-attempt-1.md`
remains open. The synthesizer should carry them forward, in particular:
prior **SKILLS-3/-4/-5/-12** (control/ack/ledger path canonicalization,
`.touch/` vs `<task-dir>` split-brain), prior **SKILLS-6** (marker precedence:
`[monitor]` last-wins vs T8's `^`-anchored parse vs `[touch]` first-line),
prior **SKILLS-8/-14** (touch names vs `plan`/`stage` identity, non-deterministic
`N`, research `attempt=1` hardcoded), prior **SKILLS-10/-11** (ack vocabulary
mapping; pause prose contradiction), prior **SKILLS-15/-16** (background spawns
would void the journal and the typed structured output the loops branch on),
prior **SKILLS-17** (stale watcher comment about marker placement).

## What this perspective says the plan must decide

1. **Verdict semantics for non-gate plans** (SKILLS-1/-2/-3/-9/-10): one rule for
   how a plan card and a run close, honoured by the watcher, both templates and
   Touch's ingester — today a green run reads as failed and a finished research
   run reads as running.
2. **A durable, machine-readable topology + stable sub-plan identity**
   (SKILLS-5/-6/-4/-16): the prerequisite for both the graph and every control
   verb; without it "restart this loop" cannot be expressed.
3. **Attempt bookkeeping that never loses evidence** (SKILLS-7/-8): union of
   touched files, findings file on every failure path, stop-vs-crash arbitrated
   by Touch's control audit.
4. **One marker specification** carrying identity *and* class (SKILLS-13/-15/-11
   + prior SKILLS-6): plan, stage, role, attempt, model, phase, touch name —
   parsed by one documented rule, guarded by one static test.
