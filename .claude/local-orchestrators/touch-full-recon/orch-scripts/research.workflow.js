// touch-full-recon research workflow — deterministic research -> ONE complete
// synthesized plan. Adapted from
// .claude/skills/execute-research/templates/research.workflow.js; protocol:
//   * research + synthesis agents are READ-ONLY for source (they only read, run
//     safe inspection, and write findings/plan files = task state)
//   * the fan-out is a pure function of PERSPECTIVES (deterministic agent count)
//   * synthesis merges accepted findings into ONE complete plan (global
//     decisions + ordered items) — it never partitions into sub-plans; that
//     divide-and-conquer belongs to implement-plan's Fable divider
//   * every agent is a brand-new subagent with fresh context; never reuse/resume
// This script STOPS at the plan. implement-plan consumes { plan_file }.
export const meta = {
  name: 'touch-full-recon-research',
  description: 'Full recon of the Touch repo and its local-orchestrators history — research from 6 perspectives and synthesize one complete implementation plan',
  phases: [
    { title: 'Research', detail: '6 read-only researchers, one perspective each', model: 'opus' },
    { title: 'Synthesize', detail: 'dedup + decide + write ONE complete plan', model: 'fable' },
  ],
}

const REPO = '/home/laniakea/Projects/touch'
const TASK = REPO + '/.claude/local-orchestrators/touch-full-recon'
const FINDINGS = TASK + '/findings'
const PLAN_FILE = TASK + '/plan/touch-full-recon-plan.md'
const S = REPO + '/.claude/shared/monitoring/status.sh'

// Quote every path interpolation so a REPO/TASK/S path with a space cannot split
// the env assignment / arg list. Keep agent-filled <summary> text single-line,
// no double quotes (see m-orchestrator SKILL.md).
const statusCmd = (plan, stage, state, msg) =>
  `ORCH_STATE_DIR="${TASK}" bash "${S}" "${plan}" ${stage} ${state} "${msg}"`

// The subject: the exact files / dirs / sources every researcher may read.
const SUBJECT = [
  REPO + '/README.md',
  REPO + '/inception.md',
  REPO + '/CLAUDE.md',
  REPO + '/.gitignore',
  REPO + '/.claude/settings.json',
  REPO + '/.claude/skills/  (execute-research, implement-plan, m-orchestrator, touch-orchestrate — SKILL.md + templates/*.workflow.js)',
  REPO + '/.claude/shared/monitoring/  (status.sh, monitor_server.py, decision_watcher.py, monitor.html, monitoring.md, tests/)',
  REPO + '/.claude/local-orchestrators/  (touch-repo-recon/, touch-aggregator/, touch-monitor-spawn/ — plans, findings, events.jsonl, orch-config.json, logs, driver context)',
].join('\n')

const RESEARCH_CONTEXT = `Touch is a planned web app for visualizing and managing
subagents in Claude Code sessions (README.md). The repo has NO application source
yet: it holds product docs (README.md, inception.md, CLAUDE.md), a working
zero-dependency live-monitoring stack (.claude/shared/monitoring/), the
orchestration skill pair plus helpers (.claude/skills/), and accumulated
orchestration history under .claude/local-orchestrators/ (three task folders with
plans, findings, event streams, configs). A prior six-perspective research run
produced the normative design plan (touch-aggregator/plan/touch-aggregator-plan.md:
decisions D1-D14, items T1-T23) and a scoped v0 plan
(touch-monitor-spawn/plan/touch-monitor-spawn-plan.md: P1-P12); the
touch-orchestrate skill was drafted AFTER both plans and is not yet reflected in
them. Nothing is committed to git yet (fresh repo, master, all untracked).
Goal of THIS research: a fresh full recon of the codebase AND of the
local-orchestrators history, so the synthesizer can write ONE reconciled,
up-to-date implementation plan — covering doc/plan drift, repo hygiene, module
defects, skill inconsistencies, and what remains unresolved — ready to hand to
implement-plan without re-research.`

// DETERMINISTIC perspective list — one read-only agent per entry. The agent
// count and prompts are a pure function of this array. Three perspectives cover
// the codebase; three are dedicated to .claude/local-orchestrators/ (user ask).
const PERSPECTIVES = [
  { key: 'product', focus: 'Product intent and docs: README.md, inception.md, CLAUDE.md, .gitignore, .claude/settings.json. Contradictions between the three docs and between docs and repo reality (check every checkable claim against the tree — e.g. CLAUDE.md claims about orch-config wf_dir provenance, test commands, project status). Repo hygiene: nothing is committed yet — what should the initial commit(s) contain, what must stay ignored (.gitignore coverage vs actual state files, __pycache__, logs), what the docs demand that nothing yet provides. Gaps and stale statements that would mislead the next implementer.' },
  { key: 'monitoring', focus: 'The monitoring module .claude/shared/monitoring/ (status.sh, monitor_server.py, decision_watcher.py, monitor.html, monitoring.md as normative spec, tests/). Architecture and semantics Touch inherits or copies (tailing, checkpointing, token dedup, path containment, escape-first rendering); real defects, races, security gaps in the current code; divergence between monitoring.md and the code; test coverage gaps; what Touch must copy verbatim vs deliberately not inherit.' },
  { key: 'skills', focus: 'The four skills in .claude/skills/ (execute-research, implement-plan, m-orchestrator, touch-orchestrate) including templates/*.workflow.js as normative protocol. The loop entities Touch must render and drive (task, plan, sub-plan, agent, attempt, gate); inconsistencies between SKILL.md prose and templates, and between the skills; whether touch-orchestrate composes cleanly with the pair and the monitoring module; ambiguities or protocol gaps that would break a UI or a controller driving these loops.' },
  { key: 'plans', focus: 'The plan corpus under .claude/local-orchestrators/: touch-aggregator/plan/touch-aggregator-plan.md (normative design, D1-D14, T1-T23), touch-monitor-spawn/plan/touch-monitor-spawn-plan.md (scoped v0, P1-P12), plus inception.md sections 6-7 as their summary. Conflicts and supersession between the plans; items already stale versus the current repo and skills (touch-orchestrate postdates both); sequencing — what should implement-plan actually receive first; per-item readiness (concrete files, testable) versus items needing re-decision.' },
  { key: 'audit', focus: 'All prior research findings files: touch-aggregator/findings/*.md (six perspectives) and touch-repo-recon/findings/*.md (three), plus touch-aggregator/context/driver-context.md. Mine what remains UNRESOLVED or UNVERIFIED; findings the plans dropped or contradicted without justification; claims inception.md later corrected (write-latency, firewall) and whether other corrected-class claims still lurk uncorrected; the concrete list of experiments/verifications the next implementation phase must still settle.' },
  { key: 'runstate', focus: 'The operational state under .claude/local-orchestrators/ treated as DATA: events.jsonl streams, .watcher-state.json checkpoints, orch-config.json files, *.log files, folder layout. What real event shapes, edge cases and schema variance a Touch ingester must tolerate; config/state drift (verify the CLAUDE.md claim that orch-configs point at an earlier omnigent project — check actual wf_dir values and whether those dirs exist); hygiene issues (logs and pycache in tracked space, gitignore coverage per the test_shell.py guard); what this history implies for the planned .touch/ store and legacy-ingest decision D5/D4.' },
]

const RESEARCH_SCHEMA = {
  type: 'object', required: ['findings', 'findings_file', 'summary'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object', required: ['id', 'file', 'severity', 'title'],
        properties: {
          id: { type: 'string' }, file: { type: 'string' }, line: { type: 'integer' },
          severity: { enum: ['blocker', 'major', 'minor', 'nit'] },
          title: { type: 'string' },
        },
      },
    },
    findings_file: { type: 'string' },
    summary: { type: 'string' },
  },
}

const researchPrompt = (p) => `
[monitor] plan=research stage=${p.key} role=research attempt=1
You are a READ-ONLY researcher on the subject below. Never edit any source file —
you only read, run safe inspection commands, and write ONE findings file in the
task state folder.
FIRST run: ${statusCmd('research', p.key, 'running', 'scanning ' + p.key + ' perspective')}

Subject (read all that your focus touches; line numbers matter):
${SUBJECT}

RESEARCH_CONTEXT: ${RESEARCH_CONTEXT}

YOUR PERSPECTIVE: ${p.focus}

Method: study the subject with adversarial/analytical intent; where cheap, verify
a suspicion empirically ONLY in a throwaway dir under /tmp/claude-1000 — never
against the live task folder ${TASK} except the two mandated status.sh calls.
Report only real, actionable items (defects, risks, gaps, decisions to make) with
a concrete rationale each. Severity: blocker | major | minor | nit.

Write your FULL findings to ${FINDINGS}/research-${p.key}-attempt-1.md — one
section per finding: id ${p.key.toUpperCase()}-<n>, file:line, severity, the
concrete scenario, and a concrete recommendation. This file is task state —
writing it is required.
LAST run: ${statusCmd('research', p.key, 'done', 'found N findings')} (replace N)
Return structured output only: findings (id/file/line/severity/title each), findings_file, summary.
`

const SYNTH_SCHEMA = {
  type: 'object', required: ['plan_file', 'item_count', 'summary'],
  properties: {
    plan_file: { type: 'string' },
    item_count: { type: 'integer' },
    summary: { type: 'string' },
  },
}

const synthPrompt = (reports) => `
[monitor] plan=synthesis stage=synthesize role=synth attempt=1
You are the PLAN SYNTHESIZER. READ-ONLY for source; you write exactly one plan
file in task state.
FIRST run: ${statusCmd('synthesis', 'synthesize', 'running', 'merging ' + reports.length + ' research reports')}

Research reports (read ALL of them fully from disk first):
${reports.map(r => '- ' + r.findings_file + ' — ' + r.summary).join('\n')}

Subject:
${SUBJECT}

RESEARCH_CONTEXT: ${RESEARCH_CONTEXT}

Tasks:
1. Merge + dedup the findings (same item from two perspectives = one, keep both
   ids as aliases). Discard non-items with a one-line justification each. Where
   two reports contradict, open the source and decide.
2. Decide every global/protocol question ONCE (canonical shapes, who tolerates
   what) so downstream work cannot diverge. Where a prior decision (D1-D14) or
   plan item (T1-T23, P1-P12) is affected, say explicitly whether it stands,
   is amended, or is superseded — never silently fork the prior plans.
3. Order the accepted items into ONE complete implementation plan. Per item:
   id, title, affected files (file:line), the finding ids it resolves, the
   decided approach, and what a test should cover. Do NOT partition the plan
   into sub-plans — implement-plan's divider owns divide-and-conquer; just keep
   each item concrete and self-contained enough to be partitioned later without
   re-research.
4. Write the full plan to ${PLAN_FILE} (mkdir -p its dir first): the global
   decisions section, then the ordered item list. Findings stay in the research
   files — reference them by id + path.

LAST run: ${statusCmd('synthesis', 'synthesize', 'done', 'plan ready: N items')}
Then:     ${statusCmd('synthesis', 'plan', 'done', 'plan written')}
Return structured output only: plan_file, item_count, summary.
`

phase('Research')
log(`spawning ${PERSPECTIVES.length} read-only research agents: ${PERSPECTIVES.map(p => p.key).join(', ')}`)
// Barrier is required: synthesis needs ALL reports to dedup across perspectives.
const reports = (await parallel(PERSPECTIVES.map(p => () =>
  agent(researchPrompt(p), {
    model: 'opus', effort: 'high',
    label: `research:${p.key}`, phase: 'Research', schema: RESEARCH_SCHEMA,
  })
))).filter(Boolean)
log(`research done: ${reports.length}/${PERSPECTIVES.length} returned, ${reports.reduce((n, r) => n + r.findings.length, 0)} raw findings`)

phase('Synthesize')
// Synthesis is the only stage in THIS workflow allowed to use fable; research
// agents stay on opus. (implement-plan's final gate reviewer also runs fable.)
const synth = await agent(synthPrompt(reports), {
  model: 'fable', effort: 'xhigh',
  label: 'synthesize', phase: 'Synthesize', schema: SYNTH_SCHEMA,
})
if (!synth || !synth.plan_file || !synth.item_count) {
  throw new Error('synthesis produced no plan items — cannot hand off a plan')
}
log(`plan ready: ${synth.item_count} items in ${synth.plan_file}`)

// Close the orchestrator badge (the watcher cannot see run completion in the journal).
// (Driver may also emit this from the shell after the workflow returns.)

// NO AUTO-CHAIN: the user prompt asked for research + plan only; the driver
// hands off { plan_file } and stops.

return {
  raw_findings: reports.map(r => ({ file: r.findings_file, count: r.findings.length, summary: r.summary })),
  plan_file: synth.plan_file,
  item_count: synth.item_count,
  summary: synth.summary,
}
