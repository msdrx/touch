---
name: implement
description: Take the complete plan produced by research (or an equivalent plan), divide it into isolated feature-sub-plans via a Fable divider agent, then implement them through generated, monitored implement→test→critique loops (serial by default). Use when asked to execute / implement a plan; pass the user's request as the argument.
---

# implement — deterministic implementation orchestrator

Owns the *plan → implementation* half of a research→implement pair: it
consumes ONE complete plan (from `research`, or an equivalent shaped
per that skill's Contract section), divides it, and implements it. Never does
the upstream research — if no usable plan exists, stop and point the caller at
`research`.

**Argument**: `<user_prompt>` — which plan (a
`<project>/.touch/local-orchestrators/<task-name>/` folder, a `plan/*.md`
path, or the `{ plan_file }` from an research auto-chain) and how (strategy
override). Everything after an optional `Test hints:` marker is `<test_hints>`
and extends the test gate (e.g. installer / e2e requirements).

**Strategy: SERIAL by default** — one sub-plan runs its full loop to green
before the next starts. PARALLEL only when `<user_prompt>` asks explicitly
("parallel", "concurrently"), and even then only for sub-plans with disjoint
file ownership.

## Procedure

`${CLAUDE_PLUGIN_ROOT}/skills/implement/templates/implement.workflow.js`
is the NORMATIVE protocol — prompts, schemas, models, the Divide phase, monitor
markers, findings handoff, isolation guard. **You do not adapt it.** It is
generic and spec-driven (GD-D9): the `orch-scripts/` copy is a byte-for-byte
`cp` that `touch-run start` makes and `touch-run verify` pins, and every
per-run value arrives in `args` from a run spec you write. The only thing you
author is that JSON:

```json
{
  "kind": "implement",
  "task": "<task-name>",
  "project_dir": "<absolute project root>",
  "plan_file": "<absolute path to the ONE complete plan>",
  "context": "<TASK_SPECIFIC_CONTEXT: goal, constraints, out-of-scope areas>",
  "test_hints": "<from the caller's `Test hints:` marker>",
  "parallel": false,
  "title": "<dashboard card title>",
  "roster": [{"id": "divide",    "title": "Divide — partition by file ownership"},
             {"id": "finalgate", "title": "Final gate — aggregate acceptance"}]
}
```

- **`roster` seeds the cards `touch-run start` can know up front** — `divide`
  and `finalgate`, the protocol's two single-agent plans. Both ids are the
  template's own, so the seeded card and the watcher-derived one are the same
  card. The per-sub-plan cards cannot be seeded from a spec: the partition does
  not exist until Divide returns, which is why `touch-cycle-reporter`
  re-declares `plans_total` as N+2 at the divide close and why the denominator
  folds monotonically — `max(cards seen, declared)`, never shrinking (GD-D11).
  Omit the roster and the run starts with one card, its own, and no denominator
  until Divide closes.
- **Per-project constants are configured ONCE**, in `.touch/run.json` — the one
  tracked file under `.touch/` besides the memory subtree:
  `targeted_test_command`, `full_suite_command`, `baseline_notes`,
  `review_checklist`. `touch-run start` merges them UNDER the spec, so a per-run
  override still wins and nothing is retyped per run. Create it once per
  project; while it is absent the spec supplies every value. For
  the critique's checklist the `/touch:code-quality-review` skill is a
  ready-made one to name (its severities are already the
  blocker/major/minor/nit vocabulary the critique schema gates on); load it
  only when the run wants it, since it is paid per attempt.
- **Caps and strategy are published by `touch-run`, never by the script**: the
  spec's `max_attempts` / `finalgate_attempts` / `parallel` become
  `orch-config.json`'s `max_plan_attempts` / `max_finalgate_attempts` /
  `strategy`, which the watcher and the cycle reporter re-read live.
  `extra_attempts` is the one honest gap — `touch-run start` does not publish it
  yet, so when you grant one, write that map into `orch-config.json` yourself
  as well (a recorded `touch-run` follow-up, and not a licence to hand-edit
  anything else there).
- **What the run reports, and where, is configured per surface** — `reports`,
  in the spec or once per project in `.touch/run.json` (merged under the spec
  surface by surface AND key by key, so naming one surface never silently
  resets the others). Each surface takes `{"enabled": bool, "publish": <dest>}`,
  or the same `<dest>` as a bare string standing for the whole surface, or
  `"off"`. A destination NAMES where the page goes, `|`-joined — `local`,
  `public`, `local|public` — so the value says which ones it means:

  | surface | pages | default |
  |---|---|---|
  | `cycle` | `report/cycles/<sp-id>-cycle-<N>.html` + their index | on, `local` |
  | `final` | `report/final-report.html`, this protocol's end-of-run page | on, `local\|public` |
  | `research` | `report/research-report.html` — the `research` skill's page, never rendered by this run; here because one spec may configure both halves of a chain | on, `local\|public` |

  Omit the key and those defaults apply. `touch-run start` publishes the
  effective map into `orch-config.json` (the reporter re-reads it live, so a
  mid-run edit needs no restart) and prints it; `touch-run status` reads it
  back. Two things the switch does NOT do: **`enabled: false` stops PAGES and
  nothing else** — every loop close, protocol close and roster event still
  fires, so no card is left "running" because reporting was turned off — and
  **it never suppresses the task-folder copy**, which is written for every
  destination. `publish` chooses whether the Artifact step happens (a value
  that does not name `public`: it does not), never whether the durable copy
  exists. A malformed `reports`
  is refused by the preflight, before anything is created.
- **`PLUGIN_ROOT` is left as shipped** — never baked into a copy, not a
  fill-in.
- Serial vs parallel, per the strategy above (`"parallel": true` only for
  disjoint file ownership, only when asked).

Then `touch-run start <task> --spec <file>` and launch the `Workflow({…})` line
it prints. The protocol's invariants, which the template already enforces:

- **Divide first, by Fable.** The partition into isolated sub-plans
  `{ id: "sp-<slug>", title, files, finding_ids, slice_file }` is derived at runtime by
  the READ-ONLY `{model: 'fable'}` divider — file ownership is the isolation
  rule (one file, exactly one owner; cross-file items split into per-file
  halves restating the shared decision). It writes `plan/*-subplans.md` **and
  one plan SLICE per sub-plan** (`plan/<task>-subplan-<id>.md`: the shared
  global-decisions header plus only that sub-plan's items); every downstream
  prompt names the slice, not the full plan (D-23 — measured: 75.8 K tokens of
  plan read 11 times by 7 agents in one run, and an implementer handed only its
  own slice cannot drive-by-fix another sub-plan's file). The loops are a pure
  function of the divider's output, and the script enforces unique ownership.
- **Gated loop per sub-plan**: BRAND-NEW implementer every attempt (never
  resume / continue / SendMessage a prior agent; stop stale ones in parallel
  shapes) → READ-ONLY test gate (targeted suites green + full suite as
  regression gate + `<test_hints>`) → READ-ONLY adversarial critique that
  tries hard to reject; loop until the gate passes AND critique approves, or
  MAX_ATTEMPTS.
- **Findings files are the handoff**: every gate writes
  `findings/<plan>-<gate>-attempt-<N>.md` before returning; attempt N>1
  implementers get FILE PATHS, not inlined findings. The structured return
  carries only verdict + short summary. The one inline exception: an
  implementer that returns `done:false` (a substantive REFUSAL — it spends its
  attempt) leaves no findings file, so its reason is forwarded to the next
  attempt as prompt text instead of dying with the agent.
- **Infrastructure never spends attempts**: every spawn goes through the
  template's `agentR` guard — an `agent()` that returns `null` died on the
  API, not on the work; it is retried on the SAME attempt up to 3×, then the
  run THROWS and stops where it stands with its attempts unspent
  (monitor's `network-recovery.md`, layer 2 — in the protocol now, not
  optional prophylaxis). The script paints no badge for that stop; the run's
  cards settle through the derived close, which reports the infrastructure
  death rather than a verdict. A dead gate is never fabricated into a red
  verdict; a stopped run is resumed, not re-judged.
- **Strictly-last sub-plans**: the divider may mark at most one sub-plan
  `last: true` (the endgame kind — the commit, the aggregate acceptance over
  the merged change-set). It runs ONLY when every other loop closed green;
  otherwise it is recorded `blocked` and never spawned — a strictly-last loop
  must never absorb a dead sibling's work.
- **Final aggregate gate** over the MERGED change-set once all loops close
  green; on failure a fresh implementer scoped to the whole change-set, then
  re-gate.
- **Models**: everything `opus` (effort by complexity, never above xhigh)
  EXCEPT the two `fable` agents — the DIVIDER and the FINAL-GATE REVIEWER (the
  final-gate fixer is an implementer: opus).
- The `[monitor]` marker is line 1 of every prompt and is FENCED (GD-D1a) — the
  watcher and the aggregator derive plan/stage/role/attempt from it with zero
  LLM cooperation. No prompt mandates a `touch-status` call: spawn and result
  are derived from the journal and the marker, and a second assertion of them
  is a duplicate that can disagree with the record (D-09).

## Monitoring

Per the `monitor` skill (the `touch-run` envelope over the
`touch-status` / `touch-monitor` / `touch-watcher` commands) — if that skill
does not exist, STOP and notify the caller instead of improvising. Three verbs,
in order:

```bash
touch-run start <task> --spec <run-spec.json>   # roster cards (divide,
                                                # finalgate), ACTIVE, monitor,
                                                # and the Workflow line to run
# launch the Workflow({…}) line it printed
touch-run bind  <task>                          # wf_dir + RESUME.md + the two
                                                # per-run daemons (watcher,
                                                # cycle reporter)
touch-run close <task> --state done --summary "<one line>"
```

`start` seeds the `divide` and `finalgate` cards from the spec's roster and
arms the `ACTIVE` run-scope sentinel so loop subagents stay out of other tasks'
state, then prints the tokened dashboard URL. Per-sub-plan cards appear as the
watcher sees each loop's first spawn — the partition exists only after Divide
returns, which is also where `touch-cycle-reporter` re-declares `plans_total`
(N sub-plans + 2) and emits the roster (GD-D11). Roles: `synth` (the divider),
`impl`, `test`, `critique` (`gate:run`/`gate:fix` are reserved for standalone
gate→fixer loops, not used here). Attempt caps are read live from
`orch-config.json`, and this protocol uses two of them — `max_plan_attempts`
(4) and `max_finalgate_attempts` (2). Put those in the run spec as
`max_attempts` / `finalgate_attempts` and `touch-run start` publishes them;
never hand-edit a script. (`max_gate_attempts` / `max_e2e_attempts`, both 3,
belong to standalone gate→fixer loops and have no spec key — a run that raises
either one is asking for a cap this protocol never reads.)

## Cycle reports and the user-decision protocol

Every implement→test→critique cycle ends with a visual report:
`report/cycles/<sp-id>-cycle-<N>.html` plus a run overview at
`report/cycles/index.html` — the monitor's artifacts strip lists `report/`
automatically. Each page renders the cycle as a simple UML-style flow
(implementer → test gate → critique → verdict; color always paired with glyph +
word) and MUST answer WHY — on failure *and* on success: the gate/critique
verdict summaries plus the full findings files embedded as the evidence.

**Every page leads with the requirement → implemented → Δ diagram** — a
coverage bar, counting chips, and one row per plan item: what was required,
what was built, and where the two differ. It is what a reader opens the page
for, so it comes before the prose and the prose stays short; the embedded
findings are still there, below, as the evidence for what the diagram claims.
Three recorded inputs feed it, and NOTHING is inferred:

| column | source | schema field |
|---|---|---|
| requirement | the divider's partition | `subplans[].finding_ids` |
| implemented | the implementer's return | `items[]` — `id`, `status` done\|partial\|skipped, `note` ≤120 chars |
| Δ vs requirement | both read-only verdicts | `deviations[]` — `id`, `kind` missing\|differs\|extra, `what` ≤120 chars |

All three are REQUIRED by their schemas, because a diagram that empties when an
agent is terse is worse than no diagram. The rules the renderer will not bend:
an item the implementer never mentioned renders `? unreported`, never as
covered; an id outside the sub-plan's `finding_ids` renders `+ extra`
(implementation beyond the requirement) rather than being dropped; a critique
that APPROVES still reports a real `differs`; and a journal carrying none of
these fields renders a stated absence, never a green zero. The overview and the
final report fold the same numbers, so the three surfaces cannot disagree.

The reports are rendered DETERMINISTICALLY (never by an LLM scribe) by
`touch-cycle-reporter`, which `touch-run bind` starts for you as the third
daemon, with its pid and log beside the watcher's. It tails the run's
`journal.jsonl`, correlates each structured result to (plan, stage, attempt)
via the `[monitor]` markers in the agent transcripts (zero LLM cooperation,
same technique as the decision watcher), reads the caps and `extra_attempts`
from `orch-config.json`, renders the pages, and emits the loop-terminal
`plan done|failed` status event when a loop closes on a REAL verdict at the
published cap — plus the terminal closes of the protocol's two single-agent
plans, `divide` and `finalgate` (without this pass those cards sat on
"running" until the watcher's run-end settle). The workflow script cannot do
any of this itself: the runtime has no filesystem or Node API (`import()`
throws), which is why the dead `runStatus`/`closeRun`/`publishConfig` helpers
were deleted from the template rather than kept as decoration (D-10, GD-D5).

Two by-hand modes exist for streams a live daemon did not cover:

```bash
# backfill a finished or foreign run's pages, emitting no status events
ORCH_STATE_DIR="<task-dir>" touch-cycle-reporter "<wf_dir>" --once --no-status
# settle a gappy stream: emit ONLY the loop closes the journal implies and the
# stream is missing; idempotent, so a second run writes nothing
ORCH_STATE_DIR="<task-dir>" touch-cycle-reporter "<wf_dir>" --settle
```

`touch-run close` runs `--settle` itself as its first step, so the usual answer
to "a card is stuck on running" is to close the run, not to type a verdict.

`"reports": {"cycle": "off"}` switches these pages off and changes nothing
else — the daemon still tails the journal, still emits every loop close, still
settles the run; there is simply no HTML and no `report/cycles/` directory. When
the destination names `public`, publish a cycle page when you SURFACE that
cycle to the user (a red loop, the `awaiting-user` stop) rather than publishing all of them:
each page is its own artifact, and the index links its siblings by relative
path, which resolves on disk and not in the artifact store.

Loop-failure policy (enforced by the template; acted on by you, the driver):

- A loop that exhausts its attempt cap closes `failed` and the NEXT loop starts
  — a red loop never silently blocks the rest of the run.
- The FINAL attempt's critique classifies the failure (CRIT_SCHEMA):
  - `depth: 'needs-own-flow'` — the remaining work is too deep for one more
    attempt (architectural rework, cross-sub-plan redesign, missing research).
    Do NOT stop the run and do NOT grant extra attempts: after the run, route
    that sub-plan to its own research → implement pass.
  - `critical_defect: true` — a defect fundamental enough that its `next_steps`
    need a user decision before the remaining loops are worth running. A serial
    run stops right there (`status: 'stopped-critical'`); send a
    PushNotification naming the decision, surface `decision_needed` and the
    critique findings, and WAIT for the user.
- After the LAST loop's last cycle report, a run with red loops skips the final
  gate and returns `status: 'awaiting-user'`: STOP and ask the user whether
  each red `retryable` loop gets another attempt — granted by adding
  `"extra_attempts": {"sp-<slug>": N}` to the RUN SPEC and resuming (it reaches
  the script as `args` and raises only that loop's cap; per the gap noted above,
  `touch-run start` does not publish this key, so put the same map in
  `orch-config.json` yourself or the reporter's cap arithmetic disagrees with
  the script's) — or whether the red close is accepted.
- A loop recorded `blocked` (a strictly-last sub-plan whose prerequisites are
  not green) is not red work and gets no `extra_attempts`: close its
  prerequisites green, then resume — it runs with its caps untouched.
- A run that THREW through the `agentR` guard is not `awaiting-user` either:
  nothing was rejected and every unspent attempt is preserved. When the outage
  clears, resume per `network-recovery.md` (same-session `resumeFromRunId`
  replays the green prefix from the journal; cross-session, seed a
  continuation script from the journal) — never grant `extra_attempts` to pay
  for infrastructure.
- `status: 'complete'` (all loops green, aggregate sweep green): nothing to ask;
  do the Completion section.

## Completion

Close the run, then render and publish the report.

```bash
touch-run close <task> --state done --summary "<one line>"
```

That one command settles still-open cards from the journal (`--settle`), writes
the `orchestrator complete` event **only if no derived rung already did**,
removes ONLY this task's line from the `ACTIVE` sentinel, and stops this task's
watcher and reporter by recorded pid (the shared monitor keeps running). Never
type `--state failed` for a run whose agents simply returned without a decisive
verdict — that settles `done` with the honest "closed — no verdict" wording
(R-58).

The final report is DETERMINISTIC and diagram-first: run header, the run-shape
flow (divide → gated loops → final gate → run, in the same node vocabulary as
the cycle pages, with every badge read from the STREAM), then the per-sub-plan
requirement → implemented → Δ table (the same fold the cycle pages draw, at run
scale), then timeline, then — behind a fold — the per-plan cards with
verdicts/attempts/tokens/durations, and links to the plan and every findings
file. All read from the journal, the stream and the run snapshot. The only part
you write is one narrative section (D-15):

```bash
# 1. load the `artifact-design` skill (the Artifact tool's own precondition for
#    publishing a page, and the standard this one authored fragment is held to;
#    the rest of the page is the renderer's), then write the narrative fragment
#    (300–600 tokens: what was decided, and why) to
#    <task-dir>/report/narrative.html — an HTML fragment, no <script>, no handlers
# 2. render:
ORCH_STATE_DIR="<task-dir>" touch-cycle-reporter "<wf_dir>" --final \
  --narrative "<task-dir>/report/narrative.html"
# stdout: the path it wrote — <task-dir>/report/final-report.html
# stderr: `publish: <destination>` — this run's configured `local`, `public` or
#         `local|public`, spelled out in words
# 3. publish THAT file with the Artifact tool — UNLESS the line said `local`
#    alone, which means the task-folder copy is the whole deliverable
```

With `reports.final` off, step 2 prints nothing on stdout, writes no page and
exits 0: skip the narrative and the publish with it, and report the run from
the run itself. (`--force` renders it anyway, for a human overriding their own
switch.)

It lands in `report/` by construction and re-renders byte-identically, so the
storage rule is satisfied structurally rather than by remembering to copy a
file. Publishing to claude.ai is a share mirror; the task-folder file is the
durable copy the dashboard auto-links. KEEP the task state folder (including
`events.jsonl`) — completed runs are monitor history; never delete or truncate.
