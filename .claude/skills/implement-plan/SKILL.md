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
`.claude/local-orchestrators/<task-name>/` folder, a `plan/*.md` path, or the
`{ plan_file }` from an execute-research auto-chain) and how (strategy
override). Everything after an optional `Test hints:` marker is `<test_hints>`
and extends the test gate (e.g. installer / e2e requirements).

**Strategy: SERIAL by default** — one sub-plan runs its full loop to green
before the next starts. PARALLEL only when `<user_prompt>` asks explicitly
("parallel", "concurrently"), and even then only for sub-plans with disjoint
file ownership.

## Procedure

`templates/implement.workflow.js` (next to this file) is the NORMATIVE
protocol — prompts, schemas, models, the Divide phase, monitor markers,
status.sh calls, findings handoff, isolation guard. Adapt it into
`.claude/local-orchestrators/<task-name>/orch-scripts/implement.workflow.js`,
invoked with `args = { plan_file, parallel }` (all task state lives under the
task folder), deciding only:

1. Task name, REPO/TASK paths, TASK_SPECIFIC_CONTEXT.
2. TARGETED_TEST_COMMAND / FULL_SUITE_COMMAND / BASELINE_NOTES, folding in
   `<test_hints>`.
3. REVIEW_CHECKLIST for the critique.
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
- Keep the `[monitor]` markers and status.sh calls exactly as templated.

## Monitoring

Per the `m-orchestrator` skill (scripts in `.claude/shared/monitoring/`) — if
that skill does not exist, STOP and notify the caller instead of improvising.
Seed the `divide` card before launching; per-sub-plan cards are created by
each loop's first status.sh call (the partition exists only after Divide
returns). Roles: `synth` (the divider), `impl`, `test`, `critique`
(`gate:run`/`gate:fix` are reserved for standalone gate→fixer loops, not used
here). The decision watcher reads attempt caps from `orch-config.json`
(`max_plan_attempts` 4 / `max_gate_attempts` 3 / `max_e2e_attempts` 3) —
publish your MAX_ATTEMPTS there if different. Start the daemons; dashboard at
`http://<host>:8931/`.

## Completion

Emit `status.sh <plan> plan done "..."` for any still-open card, then
`status.sh orchestrator complete done "<run summary>"`. Build the HTML final
report via the artifact flow: load the `artifact-design` skill FIRST (design
guidance), write the page to
`.claude/local-orchestrators/<task-name>/report/final-report.html`, then
publish that file with the Artifact tool. The task-folder file is the required
local copy — the dashboard auto-links artifacts inside the task folder, so it
must live there, not in /tmp, and stays even after publishing. KEEP the task
state folder (including `events.jsonl`) — completed runs are monitor history;
never delete or truncate.
