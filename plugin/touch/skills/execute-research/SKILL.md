---
name: execute-research
description: Research a codebase or any source from multiple read-only perspectives and synthesize ONE complete implementation plan (no sub-plans — implement-plan divides it). Use when asked to research / investigate / analyze something and produce a plan; pass the user's request as the argument.
---

# execute-research — deterministic research orchestrator

Owns the *research → plan* half of a research→implement pair: it emits ONE
complete plan that `implement-plan` consumes via a file hand-off and divides
itself. Never edits source, never implements, never partitions.

**Argument**: `<user_prompt>` — what to research and why (a codebase area, a
bug class, a design question, docs/logs/URLs). Everything after an optional
`Hints:` marker is `<research_hints>` (files to prioritize, a perspective to
emphasize, "then implement it").

## Contract (what this skill produces)

A plan at `<project>/.claude/local-orchestrators/<task-name>/plan/<name>-plan.md`
plus a structured return `{ plan_file, item_count, summary }`. The plan is ONE
complete, self-contained document — never divided into sub-plans (that
divide-and-conquer belongs to `implement-plan`'s Fable divider). It carries:

- **global decisions** — every cross-cutting/protocol question decided ONCE so
  downstream work cannot diverge;
- **ordered implementation items** — per item: id, title, affected files
  (file:line), the finding ids it resolves, the decided approach, and what a
  test should cover.

Findings stay in the research files (the plan references them by id). Keep each
item concrete and self-contained enough for `implement-plan` to partition by
file ownership without re-research.

## Procedure

`${CLAUDE_PLUGIN_ROOT}/skills/execute-research/templates/research.workflow.js`
is the NORMATIVE protocol — prompts, schemas, models, monitor markers,
`touch-status` calls, phase structure. Adapt it into
`<project>/.claude/local-orchestrators/<task-name>/orch-scripts/research.workflow.js`
(all task state lives under the task folder, inside the user's project),
deciding only:

1. The two path constants at the top of the copy. Nothing substitutes
   placeholders inside a template file, so fill them in yourself:
   `PROJECT_DIR` = the project root (the absolute path this session is working
   in), `PLUGIN_ROOT` = `${CLAUDE_PLUGIN_ROOT}` — the value that literal
   expands to right here, in this instruction.
2. SUBJECT — the exact files / dirs / sources to study.
3. PERSPECTIVES — a deterministic list that partitions the subject (per module
   / layer / concern / source); the fan-out and prompts must be a pure function
   of it.
4. Task name, RESEARCH_CONTEXT, and how `<research_hints>` slot in.

Run it, keeping the template's invariants:

- Research fan-out is parallel with a barrier (synthesis needs all reports);
  synthesis is ONE agent reading all findings files from disk.
- All agents are READ-ONLY for source; findings/plan files are task state and
  MUST be written. Empirical checks only in a throwaway directory outside the
  project (this session's scratchpad, or one under `$TMPDIR`); web research may
  use WebSearch / WebFetch.
- Models: research = `opus` (effort by complexity, never above xhigh);
  synthesizer = `fable` — the only Fable role here (the others are
  `implement-plan`'s divider and final-gate reviewer). Brand-new subagent every
  time; never resume / continue / SendMessage a prior one.
- Keep the `[monitor] plan=… stage=… role=research|synth attempt=…` markers and
  the `touch-status` calls exactly as templated.

## Monitoring

Per the `m-orchestrator` skill (the `touch-status` / `touch-monitor` /
`touch-watcher` commands) — if that skill does not exist, STOP and notify the
caller instead of improvising. Seed one card per phase-stream (`research`,
`synthesis`) before launching and start the daemons, writing the `ACTIVE`
run-scope sentinel (m-orchestrator §4) so research subagents stay out of other
tasks' state. When synthesis finishes, the plan card closes via the templated
status calls; the driver closes the badge with
`touch-status orchestrator complete done "<run summary>"`.

## Completion

Present the plan's item summary and the plan-file path. Build an HTML final
report via the artifact flow: load the `artifact-design` skill FIRST (design
guidance), write the page to
`<project>/.claude/local-orchestrators/<task-name>/report/research-report.html`
(named so an auto-chained `implement-plan` run's `final-report.html` cannot
overwrite it), then publish that file with the Artifact tool. The task-folder
file is the required local copy — the dashboard auto-links artifacts inside
the task folder, so it must live there, not in /tmp, and stays even after
publishing. KEEP the task state folder (including `events.jsonl`) — completed
runs are monitor history; never delete or truncate. Clear the run scope by
removing this task's line from `<project>/.claude/local-orchestrators/ACTIVE`
(m-orchestrator §4) — unless auto-chaining, where `implement-plan` keeps the
same task's line armed.

**Auto-chain**: only if `<user_prompt>` / `<research_hints>` asks to implement
/ build / execute it — then invoke the `implement-plan` skill on the SAME task
folder, handing `{ plan_file }` only (its Fable divider derives the sub-plans;
sequential default). Otherwise hand off the plan and stop.
