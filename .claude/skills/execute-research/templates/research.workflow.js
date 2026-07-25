// Reference execute-research workflow — deterministic research -> ONE complete
// synthesized plan. Adapt the ALL-CAPS constants and
// the PERSPECTIVES list per task; keep the protocol:
//   * research + synthesis agents are READ-ONLY for source (they only read, run
//     safe inspection, and write findings/plan files = task state)
//   * the fan-out is a pure function of PERSPECTIVES (deterministic agent count)
//   * synthesis merges accepted findings into ONE complete plan (global
//     decisions + ordered items) — it never partitions into sub-plans; that
//     divide-and-conquer belongs to implement-plan's Fable divider
//   * every agent is a brand-new subagent with fresh context; never reuse/resume
// This script STOPS at the plan. implement-plan consumes { plan_file }.
export const meta = {
  name: 'TASK_NAME-research',
  description: 'ONE_LINE_DESCRIPTION — research SUBJECT and synthesize one complete implementation plan',
  phases: [
    { title: 'Research', detail: 'N read-only researchers, one perspective each', model: 'opus' },
    { title: 'Synthesize', detail: 'dedup + decide + write ONE complete plan', model: 'fable' },
  ],
}

const REPO = '/ABS/PATH/TO/REPO'
const TASK = REPO + '/.claude/local-orchestrators/TASK_NAME'
const FINDINGS = TASK + '/findings'
const PLAN_FILE = TASK + '/plan/TASK_NAME-plan.md'
const S = REPO + '/.claude/shared/monitoring/status.sh'

// Quote every path interpolation so a REPO/TASK/S path with a space cannot split
// the env assignment / arg list. Keep agent-filled <summary> text single-line,
// no double quotes (see m-orchestrator SKILL.md).
const statusCmd = (plan, stage, state, msg) =>
  `ORCH_STATE_DIR="${TASK}" bash "${S}" "${plan}" ${stage} ${state} "${msg}"`

// The subject: the exact files / dirs / sources every researcher may read.
const SUBJECT = [
  REPO + '/path/to/subject-file-a',
  REPO + '/path/to/subject-file-b',
].join('\n')

// DETERMINISTIC perspective list — one read-only agent per entry. The agent
// count and prompts are a pure function of this array.
const PERSPECTIVES = [
  { key: 'AREA_A', focus: 'What AREA_A researcher attacks/analyzes — concrete scope.' },
  { key: 'AREA_B', focus: 'What AREA_B researcher attacks/analyzes — concrete scope.' },
  // ...add one entry per module / layer / concern / source.
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

RESEARCH_CONTEXT: TASK_SPECIFIC_CONTEXT (what this system is, invariants, goal of the research).

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

Tasks:
1. Merge + dedup the findings (same item from two perspectives = one, keep both
   ids as aliases). Discard non-items with a one-line justification each. Where
   two reports contradict, open the source and decide.
2. Decide every global/protocol question ONCE (canonical shapes, who tolerates
   what) so downstream work cannot diverge.
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

// OPTIONAL AUTO-CHAIN: only if <user_prompt> asked to implement/build it, the
// DRIVER invokes the implement-plan skill on the SAME task folder after this
// workflow returns, handing it { plan_file } only — implement-plan's Fable
// divider derives the sub-plans. Never partition here.

return {
  raw_findings: reports.map(r => ({ file: r.findings_file, count: r.findings.length, summary: r.summary })),
  plan_file: synth.plan_file,
  item_count: synth.item_count,
  summary: synth.summary,
}
