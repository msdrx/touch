---
name: implement-plan
description: Take the complete plan produced by execute-research (or an equivalent plan), divide it into isolated feature-sub-plans via a Fable divider agent, then implement them through generated, monitored implement→test→critique loops (serial by default). Use when asked to execute / implement a plan; pass the user's request as the argument.
---

# implement-plan — deterministic implementation orchestrator

Owns the *plan → implementation* half of a research→implement pair: it
consumes ONE complete plan (from `execute-research`, or an equivalent shaped
per that skill's Contract section), divides it, and implements it. Never does
the upstream research — if no usable plan exists, stop and point the caller at
`execute-research`.

**Argument**: `<user_prompt>` — which plan (a
`<project>/.claude/local-orchestrators/<task-name>/` folder, a `plan/*.md`
path, or the `{ plan_file }` from an execute-research auto-chain) and how (strategy
override). Everything after an optional `Test hints:` marker is `<test_hints>`
and extends the test gate (e.g. installer / e2e requirements).

**Strategy: SERIAL by default** — one sub-plan runs its full loop to green
before the next starts. PARALLEL only when `<user_prompt>` asks explicitly
("parallel", "concurrently"), and even then only for sub-plans with disjoint
file ownership.

## Procedure

`${CLAUDE_PLUGIN_ROOT}/skills/implement-plan/templates/implement.workflow.js`
is the NORMATIVE protocol — prompts, schemas, models, the Divide phase, monitor
markers, `touch-status` calls, findings handoff, isolation guard. Adapt it into
`<project>/.claude/local-orchestrators/<task-name>/orch-scripts/implement.workflow.js`,
invoked with `args = { plan_file, parallel }` (all task state lives under the
task folder, inside the user's project), deciding only:

1. Task name, TASK_SPECIFIC_CONTEXT, and the two path constants at the top of
   the copy. Nothing substitutes placeholders inside a template file, so fill
   them in yourself: `PROJECT_DIR` = the project root (the absolute path this
   session is working in), `PLUGIN_ROOT` = `${CLAUDE_PLUGIN_ROOT}` — the value
   that literal expands to right here, in this instruction.
2. TARGETED_TEST_COMMAND / FULL_SUITE_COMMAND / BASELINE_NOTES, folding in
   `<test_hints>`.
3. REVIEW_CHECKLIST for the critique — the `/touch:code-quality-review` skill
   is a ready-made checklist to fold in (its severities are already the
   blocker/major/minor/nit vocabulary the critique schema gates on); load it
   only when the run wants it, since it is paid per attempt.
4. Serial vs parallel, per the strategy above.

Run it, keeping the template's invariants:

- **Divide first, by Fable.** The partition into isolated sub-plans
  `{ id: "sp-<slug>", title, files, finding_ids }` is derived at runtime by
  the READ-ONLY `{model: 'fable'}` divider — file ownership is the isolation
  rule (one file, exactly one owner; cross-file items split into per-file
  halves restating the shared decision). It writes `plan/*-subplans.md`; the
  loops are a pure function of its output, and the script enforces unique
  ownership.
- **Gated loop per sub-plan**: BRAND-NEW implementer every attempt (never
  resume / continue / SendMessage a prior agent; stop stale ones in parallel
  shapes) → READ-ONLY test gate (targeted suites green + full suite as
  regression gate + `<test_hints>`) → READ-ONLY adversarial critique that
  tries hard to reject; loop until the gate passes AND critique approves, or
  MAX_ATTEMPTS.
- **Findings files are the handoff**: every gate writes
  `findings/<plan>-<gate>-attempt-<N>.md` before returning (the loop writes a
  placeholder if a gate agent dies); attempt N>1 implementers get FILE PATHS,
  not inlined findings. The structured return carries only verdict + short
  summary.
- **Final aggregate gate** over the MERGED change-set once all loops close
  green; on failure a fresh implementer scoped to the whole change-set, then
  re-gate.
- **Models**: everything `opus` (effort by complexity, never above xhigh)
  EXCEPT the two `fable` agents — the DIVIDER and the FINAL-GATE REVIEWER (the
  final-gate fixer is an implementer: opus).
- Keep the `[monitor]` markers and `touch-status` calls exactly as templated.

## Monitoring

Per the `m-orchestrator` skill (the `touch-status` / `touch-monitor` /
`touch-watcher` commands) — if that skill does not exist, STOP and notify the
caller instead of improvising. Seed the `divide` card before launching;
per-sub-plan cards are created by each loop's first `touch-status` call (the
partition exists only after Divide returns). Roles: `synth` (the divider),
`impl`, `test`, `critique` (`gate:run`/`gate:fix` are reserved for standalone
gate→fixer loops, not used here). The decision watcher reads attempt caps from
`orch-config.json` (`max_plan_attempts` 4 / `max_gate_attempts` 3 /
`max_e2e_attempts` 3) — publish your MAX_ATTEMPTS there if different. Start the
daemons and write the `ACTIVE` run-scope sentinel (m-orchestrator §4) so loop
subagents stay out of other tasks' state; open the tokened dashboard URL
`touch-monitor` prints at startup.

## Cycle reports and the user-decision protocol

Every implement→test→critique cycle ends with a visual report:
`report/cycles/<sp-id>-cycle-<N>.html` plus a run overview at
`report/cycles/index.html` — the monitor's artifacts strip lists `report/`
automatically. Each page renders the cycle as a simple UML-style flow
(implementer → test gate → critique → verdict; color always paired with glyph +
word) and MUST answer WHY — on failure *and* on success: the gate/critique
verdict summaries plus the full findings files embedded as the evidence.

The reports are rendered DETERMINISTICALLY (never by an LLM scribe) by
`touch-cycle-reporter`: launch it as a third daemon next to the watcher — it
carries no placeholders, so run the command, never a copy. It tails the run's
`journal.jsonl`, correlates each structured result to (plan, stage, attempt)
via the `[monitor]` markers in the agent transcripts (zero LLM cooperation,
same technique as the decision watcher), reads the caps and `extra_attempts`
from `orch-config.json`, renders the pages, and emits the loop-terminal
`plan done|failed` status event when a loop closes on a REAL verdict at the
published cap — plus the terminal closes of the protocol's two single-agent
plans, `divide` (with the template's ORCH_PLANS_TOTAL declaration) and
`finalgate`, whose script-side closes no-op with the runtime (without this
pass those cards sat on "running" until the watcher's run-end settle).
The workflow script cannot do any of this itself: the runtime
has no filesystem or Node API (`import()` throws; the template's try/catch'd
`runStatus`/`closeRun` helpers silently no-op — they document the contract the
daemon fulfills).

```bash
# Same tasks root the other daemons and the run-scope guard resolve
# (m-orchestrator SKILL.md step 1) — anchoring on a bare $PWD splits the run's
# state across two directories.
ORCH="${ORCH_TASKS_ROOT:-${CLAUDE_PROJECT_DIR:-$PWD}/.claude/local-orchestrators}"
TASK="$ORCH/<task-name>"
ORCH_STATE_DIR="$TASK" nohup touch-cycle-reporter "<wf_dir>" \
  >> "$TASK/cycle-reporter.log" 2>&1 &
echo $! > "$TASK/cycle-reporter.pid"
```

`touch-cycle-reporter --once <wf_dir> --no-status` renders a finished/foreign
run's pages without emitting status events (backfill mode).

Loop-failure policy (enforced by the template; acted on by you, the driver):

- A loop that exhausts its attempt cap closes `failed` and the NEXT loop starts
  — a red loop never silently blocks the rest of the run.
- The FINAL attempt's critique classifies the failure (CRIT_SCHEMA):
  - `depth: 'needs-own-flow'` — the remaining work is too deep for one more
    attempt (architectural rework, cross-sub-plan redesign, missing research).
    Do NOT stop the run and do NOT grant extra attempts: after the run, route
    that sub-plan to its own execute-research → implement-plan pass.
  - `critical_defect: true` — a defect fundamental enough that its `next_steps`
    need a user decision before the remaining loops are worth running. A serial
    run stops right there (`status: 'stopped-critical'`); send a
    PushNotification naming the decision, surface `decision_needed` and the
    critique findings, and WAIT for the user.
- After the LAST loop's last cycle report, a run with red loops skips the final
  gate and returns `status: 'awaiting-user'`: STOP and ask the user whether
  each red `retryable` loop gets another attempt — granted by
  relaunching/resuming with `args.extra_attempts = { 'sp-<slug>': N }`, which
  raises only that loop's cap — or whether the red close is accepted.
- `status: 'complete'` (all loops green, aggregate sweep green): nothing to ask;
  do the Completion section.

## Completion

Emit `touch-status <plan> plan done "..."` for any still-open card, then
`touch-status orchestrator complete done "<run summary>"`, and clear the run
scope by removing this task's line from
`<project>/.claude/local-orchestrators/ACTIVE`
(m-orchestrator §4 — never `rm` the whole file; another run may be active). Build the HTML final
report via the artifact flow: load the `artifact-design` skill FIRST (design
guidance), write the page to
`<project>/.claude/local-orchestrators/<task-name>/report/final-report.html`, then
publish that file with the Artifact tool. The task-folder file is the required
local copy — the dashboard auto-links artifacts inside the task folder, so it
must live there, not in /tmp, and stays even after publishing. KEEP the task
state folder (including `events.jsonl`) — completed runs are monitor history;
never delete or truncate.
