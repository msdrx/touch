// touch-repo-recon — research the whole current repo (docs, skills, monitoring
// module, and both local-orchestrators task folders) and synthesize ONE
// complete, current implementation plan for Touch. Adapted from
// .claude/skills/execute-research/templates/research.workflow.js — protocol
// invariants kept verbatim.
export const meta = {
  name: 'touch-repo-recon-research',
  description: 'Research the Touch repo from 5 perspectives (docs, skills, monitoring, both task folders) and synthesize one complete implementation plan',
  phases: [
    { title: 'Research', detail: '5 read-only researchers, one perspective each', model: 'opus' },
    { title: 'Synthesize', detail: 'dedup + decide + write ONE complete plan', model: 'fable' },
  ],
}

const REPO = '/home/laniakea/Projects/touch'
const TASK = REPO + '/.claude/local-orchestrators/touch-repo-recon'
const FINDINGS = TASK + '/findings'
const PLAN_FILE = TASK + '/plan/touch-repo-recon-plan.md'
const S = REPO + '/.claude/shared/monitoring/status.sh'

// Quote every path interpolation so a REPO/TASK/S path with a space cannot split
// the env assignment / arg list. Keep agent-filled <summary> text single-line,
// no double quotes (see m-orchestrator SKILL.md).
const statusCmd = (plan, stage, state, msg) =>
  `ORCH_STATE_DIR="${TASK}" bash "${S}" "${plan}" ${stage} ${state} "${msg}"`

// The subject: the exact files / dirs / sources every researcher may read.
const SUBJECT = [
  REPO + '/README.md',
  REPO + '/CLAUDE.md',
  REPO + '/inception.md',
  REPO + '/.gitignore',
  REPO + '/.claude/skills/execute-research/  (SKILL.md + templates/)',
  REPO + '/.claude/skills/implement-plan/  (SKILL.md + templates/)',
  REPO + '/.claude/skills/m-orchestrator/SKILL.md',
  REPO + '/.claude/skills/touch-orchestrate/SKILL.md',
  REPO + '/.claude/shared/monitoring/  (all sources, monitoring.md, tests/)',
  REPO + '/.claude/local-orchestrators/touch-aggregator/  (context/, findings/, plan/, orch-scripts/, orch-config.json)',
  REPO + '/.claude/local-orchestrators/touch-monitor-spawn/plan/touch-monitor-spawn-plan.md',
].join('\n')

// DETERMINISTIC perspective list — one read-only agent per entry. The agent
// count and prompts are a pure function of this array.
const PERSPECTIVES = [
  { key: 'intent', focus: 'Product intent and documentation truth. Read README.md, CLAUDE.md, inception.md, .gitignore, and `git status`/repo layout. Verify every claim these docs make against what actually exists on disk NOW (e.g. CLAUDE.md describes local-orchestrators content that has since changed; inception.md summarizes plans and skills — check the pointers). Flag drift, stale references, contradictions between the three docs, and any binding constraint (sandbox, ports, state layout, never-delete rules) an implementation must honor. Output: what is authoritative, what is stale, what must be corrected.' },
  { key: 'skills', focus: 'The four skills as one protocol. Read all SKILL.md files and both templates/*.workflow.js. Map how execute-research, implement-plan, m-orchestrator, and the NEW touch-orchestrate compose: markers ([monitor] vs [touch]), naming standards, spawn discipline, state/handoff conventions, control loop. Hunt for contradictions (e.g. touch-orchestrate mandates background Agent-tool spawns + spawn ledger — do the workflow templates violate or ignore that? do the two markers coexist cleanly on line 1 vs 2?), gaps (what touch-orchestrate references that nothing implements yet: .touch/control.jsonl readers, state/ dirs), and what the implementation must build for the skills to be honest.' },
  { key: 'monitoring', focus: 'The shared monitoring module as prior art AND as a live coexistence constraint. Read monitor_server.py, decision_watcher.py, status.sh, monitor.html, monitoring.md, and all four tests (run them in place — they are read-only checks — or verify statically). Identify: exactly which semantics Touch v0 must copy (torn-tail tailing, checkpoint identity, message-id token dedup, session-rotation glob, realpath containment, escape-first rendering — cite file:line), which architectural limits make it non-extendable (one-way transport, single-run watcher globals, marker-gated visibility), and every coexistence risk when Touch runs beside it (port 8931 vs 8932, shared task folders, double-ingesting events.jsonl, pkill patterns, .gitignore guards in test_shell.py).' },
  { key: 'aggtask', focus: 'The touch-aggregator task folder — the deep research record. Read context/driver-context.md, all six findings/research-*-attempt-1.md files, plan/touch-aggregator-plan.md (Parts A-F), orch-config.json, and orch-scripts/. Extract the binding verified facts and the 14 global decisions D1-D14; check the 23-item plan for internal consistency and for anything OVERTAKEN by later repo work (the touch-orchestrate skill now exists; a v0 plan now exists — neither is reflected in the 23 items). Flag which plan items the v0 slice subsumes, which decisions the new skill already implements differently (naming hierarchy vs the plan silence on names), and any UNVERIFIED item from Part E that the v0 slice depends on.' },
  { key: 'v0task', focus: 'The touch-monitor-spawn v0 plan — implementability review. Read plan/touch-monitor-spawn-plan.md item by item (P1-P12 and decisions G1-G9). Verify every cross-reference it makes: cited monitoring-module patterns (decision_watcher.py:86-100, :154-197, :470-491, monitor_server.py:199-212 — do those lines actually contain what is claimed?), the touch-orchestrate SKILL.md contract (marker fields, spawn-ledger shape, control-file protocol — do plan and skill agree exactly?), and D1-D14 inheritance (any contradiction with the big plan?). Assess each item for file-ownership partitionability by implement-plan (one file one owner), missing items (bootstrap/run scripts? .touch/ creation? server.json shape?), and test realism (stdlib-only, no pytest).' },
  { key: 'models', focus: 'Pinned subagent model + effort usage, repo-wide. Inventory EVERY place a subagent model or effort is pinned or prescribed: both skills SKILL.md files (execute-research says research=opus effort by complexity never above xhigh; implement-plan prescribes models for divider/implementer/test/critique/final gate), both templates/*.workflow.js (model:/effort: options per agent() call), the orch-scripts copies under both task folders, orch-config.json files, and any model references in the plans/docs (inception.md, touch-aggregator-plan.md, touch-monitor-spawn-plan.md). For each: file:line, role, pinned model string (alias like opus vs explicit id like claude-opus-4-8 / claude-opus-5 / [1m] variants), effort. Determine empirically (safe inspection only, e.g. claude --help or version strings — throwaway dir /tmp/claude-1000) what the bare aliases opus/fable resolve to in this environment. THE MANDATE (user-directed, normative): (a) for RESEARCH and IMPLEMENTATION roles (execute-research researchers; implement-plan implementer, test-gate, critique agents), any pin to Opus 4.8 at xhigh (or an alias resolving there) must be replaced with Opus 5 at xhigh — report each occurrence as a finding with the exact edit; (b) Fable is reserved for exactly: the plan SYNTHESIZER, the MAIN USER TERMINAL agent (the driver session itself — check where docs/skills state the driver model and report it as an item if unstated), and the FINAL REVIEW agent (implement-plan aggregate/final gate); (c) NOTE an open decision: implement-plan currently also pins the DIVIDER to fable, which the user enumeration omits — flag every divider pin (file:line) and mark divider-model as a decision the synthesizer must make explicitly, never change it silently; (d) effort caps (never above xhigh) stay as skill-mandated. Flag inconsistencies between what skills prescribe and what templates/scripts actually pin, and recommend the canonical role-to-model table the synthesized plan must mandate.' },
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

RESEARCH_CONTEXT: Touch is a web app for visualizing and managing subagents in
Claude Code sessions (README.md). The repo has NO application source yet; it
holds docs, four orchestration skills, a working zero-dependency monitoring
module, and two prior planning efforts: touch-aggregator (six research reports,
110 findings, and a 23-item full plan with binding decisions D1-D14, all
verified against Claude Code CLI 2.1.220) and touch-monitor-spawn (a 12-item
v0 plan scoped to monitoring + agent spawning, written from a design
conversation). A new skill, touch-orchestrate, defines the spawn standard
(hierarchical naming from a root name, [touch] marker, background spawns,
spawn ledger, control-file stop loop). GOAL of this research: establish what
is true, binding, stale, contradictory, or missing across ALL of it, so a
synthesizer can write ONE current, complete implementation plan for Touch
(monitoring + spawning first) that reconciles both prior plans and the skill.

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

Additional binding inputs you must reconcile (read fully):
- ${REPO}/.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md (decisions D1-D14 are binding unless a finding proves one wrong against a primary source — if you override one, say so explicitly)
- ${REPO}/.claude/local-orchestrators/touch-monitor-spawn/plan/touch-monitor-spawn-plan.md (the v0 slice this plan supersedes or absorbs — decide which, explicitly)
- ${REPO}/.claude/skills/touch-orchestrate/SKILL.md (the spawn standard is NORMATIVE; the plan builds machinery around it, never redefines it)

Tasks:
1. Merge + dedup the findings (same item from two perspectives = one, keep both
   ids as aliases). Discard non-items with a one-line justification each. Where
   two reports contradict, open the source and decide.
2. Decide every global/protocol question ONCE (canonical shapes, who tolerates
   what) so downstream work cannot diverge. That includes: the relation of this
   plan to the two prior plans (supersede/absorb, stated per plan), scope of
   the first implementation (monitoring + spawning first; what is explicitly
   deferred), and any doc corrections (CLAUDE.md/inception.md drift) as plan
   items.
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
// (Driver also emits this from the shell after the workflow returns.)

return {
  raw_findings: reports.map(r => ({ file: r.findings_file, count: r.findings.length, summary: r.summary })),
  plan_file: synth.plan_file,
  item_count: synth.item_count,
  summary: synth.summary,
}
