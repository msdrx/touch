// touch-mongo-live research workflow — deterministic research -> ONE complete
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
  name: 'touch-mongo-live-research',
  description: 'MongoDB persistence for Claude Code session jsonl + custom agent state + correct live monitoring of subagents and loops — research and synthesize one complete amendment plan',
  phases: [
    { title: 'Research', detail: '5 read-only researchers, one perspective each', model: 'opus' },
    { title: 'Synthesize', detail: 'dedup + decide + write ONE complete plan', model: 'fable' },
  ],
}

const REPO = '/home/laniakea/Projects/touch'
const TASK = REPO + '/.claude/local-orchestrators/touch-mongo-live'
const FINDINGS = TASK + '/findings'
const PLAN_FILE = TASK + '/plan/touch-mongo-live-plan.md'
const S = REPO + '/.claude/shared/monitoring/status.sh'

// Quote every path interpolation so a REPO/TASK/S path with a space cannot split
// the env assignment / arg list. Keep agent-filled <summary> text single-line,
// no double quotes (see m-orchestrator SKILL.md).
const statusCmd = (plan, stage, state, msg) =>
  `ORCH_STATE_DIR="${TASK}" bash "${S}" "${plan}" ${stage} ${state} "${msg}"`

const SESSION_ID = '292fc08c-923d-4ab4-8ff2-a9572417dbc8'
const PROJ = '/home/agent/.claude/projects/-home-laniakea-Projects-touch'

// The subject: the exact files / dirs / sources every researcher may read.
const SUBJECT = [
  PROJ + '/' + SESSION_ID + '.jsonl  (THE CURRENT CONVERSATION - main-session transcript, live specimen + requirements source)',
  PROJ + '/' + SESSION_ID + '/  (this session tree: subagents/, workflows/ - wf_930e210a journal, agent transcripts)',
  PROJ + '/*.jsonl and sibling session trees  (5 other session specimens incl. the touch-aggregator run)',
  '~/.claude/sessions/  (registry), ~/.claude/history.jsonl  (read-only inspection)',
  REPO + '/.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md  (NORMATIVE plan: GD-1..GD-20, R-01..R-37)',
  REPO + '/.claude/local-orchestrators/touch-monitor-spawn/plan/touch-monitor-spawn-plan.md  (G2 Mongo discard precedent)',
  REPO + '/inception.md  (verified substrate facts, CLI 2.1.220)',
  REPO + '/.claude/shared/monitoring/  (monitor_server.py, decision_watcher.py, monitor.html, monitoring.md, status.sh)',
  REPO + '/.claude/skills/  (execute-research, implement-plan, m-orchestrator, touch-orchestrate)',
].join('\n')

const RESEARCH_CONTEXT = `Touch is a planned web app for visualizing and managing
subagents in Claude Code sessions. The repo has no application source yet; the
NORMATIVE implementation plan is touch-full-recon-plan.md (20 global decisions
GD-1..GD-20, 37 items R-01..R-37, 5 phases; it supersedes the two earlier
plans). Its storage decision: file-based touch-events-v2 JSONL store under
.touch/, stdlib-only runtime (D8), identity table = sessions (pid,procStart),
records uuid, agents full 17-hex agentId, workflow nodes (runId,key,ordinal),
tokens deduped by message.id (GD-7/GD-11). MongoDB was discussed before the v0
plan and explicitly NOT adopted (G2: adopting it later is an explicit D5/D8
amendment; per-session collections discarded as an anti-pattern).
NOW the user asks for exactly that amendment, in this conversation (session
${SESSION_ID}): (1) persist Claude Code session jsonl into MongoDB
deterministically; (2) add CUSTOM agent-state persistence in another collection
referencing the mirrored session/agent records; (3) get the monitoring flow
right - correctly show subagents and deterministic-loop live status.
Environment facts checked this run: no mongod binary, pymongo not installed,
Docker daemon running with zero containers (network egress goes through an
allowlist proxy; npm/pip work). GOAL: ONE complete plan (amendment-style: it
must NOT re-plan what touch-full-recon-plan already covers - it references,
amends, or extends GDs and R-items by id, states every disposition explicitly,
and adds new items) ready for implement-plan without re-research.`

// DETERMINISTIC perspective list — one read-only agent per entry. The agent
// count and prompts are a pure function of this array.
const PERSPECTIVES = [
  { key: 'convo', focus: 'The current conversation as REQUIREMENTS SOURCE and specimen. Read the main-session transcript (' + PROJ + '/' + SESSION_ID + '.jsonl) end to end: extract every user ask, every answer already given, and every decision already made in-conversation about MongoDB persistence, custom state collections, deterministic ingestion, and monitoring correctness (quote the record uuid + text). Flag anything answered in-conversation that contradicts or extends the normative plan. Then inventory the session AS DATA: record types present and counts, the four CLI buckets, largest lines, tool-result spills, how this very conversation manifests as jsonl (including this research run spawning inside it).' },
  { key: 'sessionjsonl', focus: 'The session data layer as ingestion source, re-verified LIVE against this session and the 5 sibling sessions: registry files, main transcript record taxonomy, rewrite/truncation semantics (compaction, removeByUuid), subagents/ layout as it actually is on this machine (note: this session has subagents/workflows/ - map the real tree, do not assume the documented agent-<id>.jsonl layout), workflow journal + <runId>.json snapshot, tool-results spill, lazy creation, /clear splits. Deliver the exact ingestion contract a DB mirror needs: source list, per-source checkpoint identity, re-ingest triggers, ordering guarantees, and which normative-plan items (R-23 tailer, R-25 sessions, R-26 ingest) transfer unchanged vs change when the sink is a database.' },
  { key: 'mongoschema', focus: 'The MongoDB layer itself. Empirically probe feasibility in this sandbox ONLY in throwaway dirs under /tmp/claude-1000: can pip install pymongo; can docker pull/run a mongo image through the proxy (try it; if blocked, record the exact 403/error and what allowlist entry the user must add); mongod in apt? Then design: collections + natural-key _id scheme mapping the GD-7/GD-11 identity table (sessions, records, agents, run_nodes, usage, plus streams/cursors), idempotent upsert semantics (re-ingest converges byte-identical), index list, what of the ref union becomes DBRefs vs embedded, retention/mirror-before-sweep policy, mongo-down failure mode, dual-sink (.touch/ JSONL + mongo) vs mongo-only tradeoff, and the EXACT amendment set adopting Mongo requires (D5, D8 stdlib-only pin, GD-11, GD-15 module layout, R-24 store) honoring the G2 precedent (one collection per entity type, never per session).' },
  { key: 'customstate', focus: 'Custom agent-state persistence. Define what custom state IS from the conversation and the corpus: touch-orchestrate state files under <task-dir>/state/, spawn ledgers, control intents/acks (control.jsonl), user annotations, app-level tags - versus harness-mirrored data which is immutable source-of-record. Design the custom_state collection(s): schema, who writes (Touch server only? agents via status.sh-like helper? both?), when, referential integrity to the mirrored session/agent/node documents (which id shape each ref uses, what happens when the referent is not yet mirrored or never arrives - orphan policy), how it composes with the touch-events-v2 ref union and the .touch/ store, migration of the state files that already exist on disk, and conflict rules (custom state must never masquerade as harness fact - GD-7/D13 honesty).' },
  { key: 'liveflow', focus: 'The live monitoring flow end to end, and how a DB sink must not break it. Trace today’s pipeline (status.sh + decision_watcher -> events.jsonl -> monitor_server -> WS -> monitor.html) and the planned one (R-23 tailer -> R-26 ingest -> R-24 store -> R-30/R-31 API/WS -> R-32 UI): derive the latency budget (100ms transcript flush, ~1s token deltas, 250ms poll), the correctness rules that stop fabricated status (GD-10 close semantics, three-state liveness, unknown-never-running, SKILLS-1 class of bugs - this very run showed a fabricated research-failed badge), topology/R-19 for loop shape (attempt N of MAX, which stage next), and decide-inputs for: does the live path read from memory with Mongo as a write-behind mirror, or from Mongo (change streams? polling?); what happens to live view when Mongo is down; how loop cards, per-agent rows and token counters stay truthful during backfill vs live tail.' },
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
Web research (WebSearch/WebFetch) is allowed for MongoDB/driver facts.
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
   what) so downstream work cannot diverge. This is an AMENDMENT plan on top of
   touch-full-recon-plan.md: for every GD-1..GD-20 and R-01..R-37 you touch,
   state explicitly whether it stands, is amended, or is superseded — never
   silently fork it, and never re-plan what it already covers.
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
