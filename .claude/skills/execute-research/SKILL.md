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

A plan at `.claude/local-orchestrators/<task-name>/plan/<name>-plan.md` plus a
structured return `{ plan_file, item_count, summary }`. The plan is ONE
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

`templates/research.workflow.js` (next to this file) is the NORMATIVE
protocol — prompts, schemas, models, monitor markers, status.sh calls, phase
structure. Adapt it into
`.claude/local-orchestrators/<task-name>/orch-scripts/research.workflow.js`
(all task state lives under the task folder), deciding only:

1. SUBJECT — the exact files / dirs / sources to study.
2. PERSPECTIVES — a deterministic list that partitions the subject (per module
   / layer / concern / source); the fan-out and prompts must be a pure function
   of it.
3. Task name, RESEARCH_CONTEXT, and how `<research_hints>` slot in.

Run it, keeping the template's invariants:

- Research fan-out is parallel with a barrier (synthesis needs all reports);
  synthesis is ONE agent reading all findings files from disk.
- All agents are READ-ONLY for source; findings/plan files are task state and
  MUST be written. Empirical checks only in a throwaway dir under
  `/tmp/claude-1000`; web research may use WebSearch / WebFetch.
- Models: research = `opus` (effort by complexity, never above xhigh);
  synthesizer = `fable` — the only Fable role here (the others are
  `implement-plan`'s divider and final-gate reviewer). Brand-new subagent every
  time; never resume / continue / SendMessage a prior one.
- Keep the `[monitor] plan=… stage=… role=research|synth attempt=…` markers and
  the status.sh calls exactly as templated.

## Monitoring

Per the `m-orchestrator` skill (scripts in `.claude/shared/monitoring/`) — if
that skill does not exist, STOP and notify the caller instead of improvising.
Seed one card per phase-stream (`research`, `synthesis`) before launching and
start the daemons. When synthesis finishes, the plan card closes via the
templated status calls; the driver closes the badge with
`status.sh orchestrator complete done "<run summary>"`.

## Completion

Present the plan's item summary and the plan-file path. Build an HTML final
report via the artifact flow: load the `artifact-design` skill FIRST (design
guidance), write the page to
`.claude/local-orchestrators/<task-name>/report/research-report.html`
(named so an auto-chained `implement-plan` run's `final-report.html` cannot
overwrite it), then publish that file with the Artifact tool. The task-folder
file is the required local copy — the dashboard auto-links artifacts inside
the task folder, so it must live there, not in /tmp, and stays even after
publishing. KEEP the task state folder (including `events.jsonl`) — completed
runs are monitor history; never delete or truncate.

**Auto-chain**: only if `<user_prompt>` / `<research_hints>` asks to implement
/ build / execute it — then invoke the `implement-plan` skill on the SAME task
folder, handing `{ plan_file }` only (its Fable divider derives the sub-plans;
sequential default). Otherwise hand off the plan and stop.
