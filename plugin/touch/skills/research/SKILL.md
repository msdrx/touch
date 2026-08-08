---
name: research
description: Research a codebase or any source from multiple read-only perspectives and synthesize ONE complete implementation plan (no sub-plans — implement divides it). Use when asked to research / investigate / analyze something and produce a plan; pass the user's request as the argument.
---

# research — deterministic research orchestrator

Owns the *research → plan* half of a research→implement pair: it emits ONE
complete plan that `implement` consumes via a file hand-off and divides
itself. Never edits source, never implements, never partitions.

**Argument**: `<user_prompt>` — what to research and why (a codebase area, a
bug class, a design question, docs/logs/URLs). Everything after an optional
`Hints:` marker is `<research_hints>` (files to prioritize, a perspective to
emphasize, "then implement it").

## Contract (what this skill produces)

A plan at `<project>/.touch/local-orchestrators/<task-name>/plan/<name>-plan.md`
plus a structured return `{ plan_file, item_count, summary }`. The plan is ONE
complete, self-contained document — never divided into sub-plans (that
divide-and-conquer belongs to `implement`'s Fable divider). It carries:

- **global decisions** — every cross-cutting/protocol question decided ONCE so
  downstream work cannot diverge;
- **ordered implementation items** — per item: id, title, affected files
  (file:line), the finding ids it resolves, the decided approach, and what a
  test should cover.

Findings stay in the research files (the plan references them by id). Keep each
item concrete and self-contained enough for `implement` to partition by
file ownership without re-research.

## Procedure

`${CLAUDE_PLUGIN_ROOT}/skills/research/templates/research.workflow.js`
is the NORMATIVE protocol — prompts, schemas, models, monitor markers, phase
structure. **You do not adapt it.** It is generic and spec-driven (GD-D9): the
`orch-scripts/` copy is a byte-for-byte `cp` that `touch-run start` makes and
`touch-run verify` pins, and every per-run value arrives in `args` from a run
spec you write. The only thing you author is that JSON:

```json
{
  "kind": "research",
  "task": "<task-name>",
  "project_dir": "<absolute project root>",
  "subject": ["<file or dir to study>", "..."],
  "perspectives": [{"key": "<slug>", "focus": "<what this one owns>",
                    "subject": ["<optional narrower source list>"]}],
  "context": "<RESEARCH_CONTEXT: what this system is, invariants, the goal>",
  "title": "<dashboard card title>",
  "roster": [{"id": "research", "title": "<N read-only researchers>"},
             {"id": "synthesis", "title": "<one plan>"}]
}
```

The roster is the run's two plan cards — a research run always has exactly two,
`research` and `synthesis`, which is where its `plans_total` comes from
(GD-D11). The plan ids are fixed by the template's own markers; do not invent
others.

- **SUBJECT** — the exact files / dirs / sources researchers may read. A
  perspective may carry its own narrower `subject`, which scopes that prompt
  instead of pasting the union into all N (D-24).
- **PERSPECTIVES** — a deterministic list that partitions the subject (per
  module / layer / concern / source); the fan-out and the prompts are a pure
  function of it.
- **Task name, context, and how `<research_hints>` slot in** — the rest of the
  spec.
- **`PLUGIN_ROOT` is left as shipped** — never baked into a copy and not
  something you fill in. Per-project constants (test commands, review
  checklist) live once in `.touch/run.json` — the one tracked file under
  `.touch/` besides the memory subtree — and are merged UNDER the spec, so a
  per-run value always wins. Create it once per project; while it is absent the
  spec supplies every value, and `touch-run start` names the constants file in
  its output only when it actually found one.
- **What the run reports, and where, is configured per surface** — `reports`,
  in the spec or in that same `.touch/run.json` (merged surface by surface and
  key by key). This protocol renders ONE page, so it reads one surface:

  | surface | page | default |
  |---|---|---|
  | `research` | `report/research-report.html`, this run's end-of-run page | on, `local\|public` |

  Spelled `{"enabled": bool, "publish": <dest>}`, or as the bare-string
  shorthand `"off"` / `"local"` / `"public"` / `"local|public"`; omit it and
  the default applies. A destination NAMES where the page goes, `|`-joined, so
  `local|public` is the task-folder copy AND the artifact. `"reports": {"research": "off"}` renders no page and
  changes nothing else — card closes are the monitoring protocol, not a
  report — and `"local"` keeps the task-folder copy without publishing it. The
  copy under `report/` is written for every destination; `publish` chooses only
  whether the Artifact step happens. `touch-run start` publishes the effective
  map into `orch-config.json`, prints it, and refuses a malformed one before
  anything is created; the other two surfaces (`cycle`, `final`) belong to
  `implement` and are documented there — a spec may carry all three when a run
  auto-chains.

Then `touch-run start <task> --spec <file>` and launch the `Workflow({…})` line
it prints. The protocol's invariants, which the template already enforces:

- Research fan-out is parallel with a barrier (synthesis needs all reports);
  synthesis is ONE agent reading all findings files from disk.
- **A partial board is refused, never synthesized** — fewer surviving reports
  than `min_reports` (default: one per perspective) logs the count and THROWS,
  rather than handing the synthesizer a silently blind board. The script does
  not paint a badge for that stop: the run's cards are settled by the derived
  close, and a script announcing a verdict it cannot cause is R-58 with the
  sign flipped.
- All agents are READ-ONLY for source; findings/plan files are task state and
  MUST be written. Empirical checks only in a throwaway directory outside the
  project (this session's scratchpad, or one under `$TMPDIR`); web research may
  use WebSearch / WebFetch.
- Models: research = `opus` (effort by complexity, never above xhigh);
  synthesizer = `fable` — the only Fable role here (the others are
  `implement`'s divider and final-gate reviewer). Brand-new subagent every
  time; never resume / continue / SendMessage a prior one.
- The `[monitor] plan=… stage=… role=research|synth attempt=…` marker is line 1
  of every prompt and is FENCED (GD-D1a) — the watcher and the aggregator
  derive plan/stage/role/attempt from it with zero LLM cooperation. No prompt
  mandates a `touch-status` call: spawn and result are derived, and asserting
  them a second time is a duplicate that can disagree with the record (D-09).

**Method — the standard this fan-out is written to.** It lives here rather than
in N copies of the prompt, because it never varied per perspective and a
constant pasted into N prompts is N copies of the same tokens (D-24). The
prompts keep the parts that must reach the agent to be actionable (read-only
discipline, the findings-file shape, the read-discipline line); this is the
whole of it, stated once, for whoever writes the spec, judges a report, or
considers changing the template:

> Study the subject with adversarial/analytical intent. Where cheap, verify a
> suspicion empirically — ONLY in a throwaway directory outside the project,
> never against the live task folder. Report only real, actionable items
> (defects, risks, gaps, decisions to make), each with a concrete rationale and
> a severity of `blocker | major | minor | nit`. Read files with the Read tool
> (offset/limit on long ones) rather than `cat`/`sed`/`head` through Bash —
> measured over this project's own sessions, shelling out to `cat`/`sed`/`head`
> was 50.6% of all *Bash* result volume, and Bash carried 16,786 calls against
> Read's 3,005 (5.6:1).

## Monitoring

Per the `monitor` skill (the `touch-run` envelope over the
`touch-status` / `touch-monitor` / `touch-watcher` commands) — if that skill
does not exist, STOP and notify the caller instead of improvising. Three verbs,
in order:

```bash
touch-run start <task> --spec <run-spec.json>   # cards (research, synthesis),
                                                # ACTIVE, monitor, Workflow line
# launch the Workflow({…}) line it printed
touch-run bind  <task>                          # wf_dir + RESUME.md + daemons
touch-run close <task> --state done --summary "<one line>"
```

`start` seeds the cards from the spec's roster and `plans_total` (research is
always 2 — `research` and `synthesis`) and arms the `ACTIVE` run-scope sentinel
so research subagents stay out of other tasks' state. Card and run closes are
DERIVED — the watcher's layered close (monitor, "What is derived") and
the cycle reporter's terminal events — so nothing here mandates a status call.
`close` writes the run's `orchestrator complete` event only if no earlier rung
already did, and removes only this task's `ACTIVE` line. Skip `close` when
auto-chaining: `implement` runs on the same task folder and keeps that
line armed.

## Completion

Present the plan's item summary and the plan-file path, then render and publish
the report. The page is DETERMINISTIC — the numbers come from the journal, the
stream and the run snapshot, and the only part you write is one narrative
section (D-15).

**It leads with three concise diagrams**, the same rule the implement reports
follow — what was asked for, what was delivered, where they differ — with the
research protocol's nouns:

| diagram | asked | delivered | Δ |
|---|---|---|---|
| the run, end to end | — | board → findings → plan → unaccounted, four nodes | — |
| perspective → reported | every perspective spawned | the ones that returned findings | spawned and never returned; returned empty; no findings file |
| finding → in the plan | every `findings[].id` on the board | the synthesizer's `coverage[]` — `accepted` \| `merged` \| `dropped` + a ≤120-char note | a `dropped` justification, a `merged` target, and `? unaccounted` for a finding the synthesizer never placed |

The board needs no partition to be the requirement: the researchers' own finding
ids **are** it. `coverage` is REQUIRED by `SYNTH_SCHEMA` — a finding left out of
it renders `? unaccounted`, which is the one real gap of the three, because a
stated `dropped` is a decision the plan is entitled to make and silence is a
hole in it. The attempt-by-attempt long form sits behind a fold below the
diagrams; nothing is deleted, it is demoted.

```bash
# 1. load the `artifact-design` skill (the Artifact tool's own precondition for
#    publishing a page, and the standard this one authored fragment is held to;
#    the rest of the page is the renderer's), then write the narrative fragment
#    (300–600 tokens: what was decided, and why) to
#    <task-dir>/report/narrative.html — an HTML fragment, no <script>, no handlers
# 2. render:
ORCH_STATE_DIR="<task-dir>" touch-cycle-reporter "<wf_dir>" --final \
  --narrative "<task-dir>/report/narrative.html"
# stdout: the path it wrote — <task-dir>/report/research-report.html
# stderr: `publish: <destination>` — this run's configured `local`, `public` or
#         `local|public`, spelled out in words
# 3. publish THAT file with the Artifact tool — UNLESS the line said `local`
#    alone, which means the task-folder copy is the whole deliverable
```

With `reports.research` off, step 2 prints nothing on stdout, writes no page
and exits 0: skip the narrative and the publish with it, and hand over the plan
itself. (`--force` renders it anyway, for a human overriding their own switch.)

The renderer picks `research-report.html` for a research run (so an
auto-chained `implement` run's `final-report.html` cannot overwrite it),
lands it in `report/` by construction, and re-renders byte-identically — a
second run over the same inputs produces the same page. Publishing to claude.ai
is a share mirror; the task-folder file is the durable copy the dashboard
auto-links, so it must live there, not in /tmp, and it stays after publishing.
KEEP the task state folder (including `events.jsonl`) — completed runs are
monitor history; never delete or truncate.

**Auto-chain**: only if `<user_prompt>` / `<research_hints>` asks to implement
/ build / execute it — then invoke the `implement` skill on the SAME task
folder, handing `{ plan_file }` only (its Fable divider derives the sub-plans;
sequential default). Otherwise hand off the plan and stop.
