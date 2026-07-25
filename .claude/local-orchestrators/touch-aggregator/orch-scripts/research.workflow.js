// touch-aggregator research workflow — adapted from
// .claude/skills/execute-research/templates/research.workflow.js
// Researchers run BLIND (no driver context) so their evidence is independent;
// the synthesizer reconciles them against context/driver-context.md.
export const meta = {
  name: 'touch-aggregator-research',
  description: 'Research how to build Touch (aggregator + touch-visual) over Claude Code session data and synthesize one complete implementation plan',
  phases: [
    { title: 'Research', detail: '6 read-only researchers, one perspective each', model: 'opus' },
    { title: 'Synthesize', detail: 'dedup + decide + write ONE complete plan', model: 'fable' },
  ],
}

const REPO = '/home/laniakea/Projects/touch'
const TASK = REPO + '/.claude/local-orchestrators/touch-aggregator'
const FINDINGS = TASK + '/findings'
const PLAN_FILE = TASK + '/plan/touch-aggregator-plan.md'
const DRIVER_CONTEXT = TASK + '/context/driver-context.md'
const S = REPO + '/.claude/shared/monitoring/status.sh'

const statusCmd = (plan, stage, state, msg) =>
  `ORCH_STATE_DIR="${TASK}" bash "${S}" "${plan}" ${stage} ${state} "${msg}"`

// The subject: every source a researcher may read.
const SUBJECT = [
  REPO + '/README.md — the product spec (authoritative on intent)',
  REPO + '/CLAUDE.md — repo orientation',
  REPO + '/.claude/skills/execute-research/ and implement-plan/ (SKILL.md + templates/*.workflow.js) — the agent loops Touch must show and control',
  REPO + '/.claude/skills/m-orchestrator/SKILL.md',
  REPO + '/.claude/shared/monitoring/ (monitoring.md, monitor_server.py, decision_watcher.py, status.sh, monitor.html, tests/) — existing prior art',
  REPO + '/.claude/local-orchestrators/ — carried-over run history from an EARLIER project (omnigent); read as examples only',
  '~/.claude/ — REAL session data: projects/<slug>/<sessionId>.jsonl, projects/<slug>/<sessionId>/subagents/agent-*.jsonl + *.meta.json, .../subagents/workflows/<runId>/journal.jsonl, projects/<slug>/<sessionId>/workflows/<runId>.json, sessions/<pid>.json, history.jsonl, file-history/, todos/, settings.json',
  '/home/agent/.local/share/claude/versions/2.1.220 — the Claude Code CLI binary (~275MB); `grep -a` with a timeout settles questions the files cannot',
].join('\n')

const RESEARCH_CONTEXT = `
TOUCH (the product to be built, per README.md): a web page for visualizing and
managing subagents in a Claude Code session. Two components, "aggregator" and
"touch-visual". The main page is a terminal-styled web view over a Claude Code
session and is the PRIMARY user interface (the user drives Claude Code from the
browser). A left sidebar lists such terminal sessions; clicking one opens that
terminal. A per-terminal page shows n8n-like UML diagrams/graphs of the run,
WITH controls to pause, restart, start and terminate agent loops. The "loops"
are the ones defined by the execute-research and implement-plan skills.

REPO STATE: no application source exists yet — only README.md, CLAUDE.md and
.claude/ (skills + a working zero-dependency monitoring module + carried-over
run history). You are researching how to build it, not reviewing existing code.

ENVIRONMENT: everything runs in a sandbox. Ports are unreachable from the host
until published; services must bind 0.0.0.0, not 127.0.0.1. Outbound network is
firewalled (default-deny, HTTP 403). The existing monitoring module is
deliberately zero-third-party-dependency (bash + Python 3 stdlib + browser) and
its tests are stdlib-only scripts run directly with python3.

YOU ARE RESEARCHING BLIND ON PURPOSE: no prior conclusions are being handed to
you. Verify everything against primary sources yourself and report what the
evidence supports, including where the product as specified cannot work.
`

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

// DETERMINISTIC perspective list — one read-only agent per entry.
const PERSPECTIVES = [
  {
    key: 'sessiondata',
    focus: `The PERSISTED SESSION DATA MODEL. Enumerate, from real files under ~/.claude, every record
type and field an aggregator could read from a session transcript and its siblings: the JSONL envelope,
assistant/user messages, content block kinds, tool call + result shapes, usage/token accounting, mode and
permission records, titles, file-history records, system records. Establish what is written WHEN (flush
timing, batching, intra-turn visibility), what is redacted or spilled out-of-band, and what happens to
the files on /clear, /compact, resume and session end. Decide what a faithful "terminal view" can and
cannot be reconstructed from. Quantify latency empirically where you can.`,
  },
  {
    key: 'agentgraph',
    focus: `SUBAGENT + WORKFLOW GRAPH RECONSTRUCTION. Determine exactly how a run's node/edge graph can be
rebuilt from disk: subagent transcripts and their metadata sidecars, parent/child edges, spawn depth,
workflow journals, workflow run metadata, and any per-agent progress records. Distinguish sharply between
data the harness writes unconditionally and data that exists only because an orchestrator embedded a
convention in its prompts. Cover agent identity/naming, liveness (running vs finished vs abandoned),
ordering and timing when records lack timestamps, per-agent token/tool rollups, and how nested or
concurrent agents are represented. Say what an n8n-style graph of a run can honestly show at each moment
of its lifecycle.`,
  },
  {
    key: 'liveio',
    focus: `THE LIVE PATH: how a browser could show what is happening RIGHT NOW. Investigate every push or
low-latency channel a Claude Code installation offers — the hook system (enumerate the events and their
payloads, note which are synchronous/blocking), the running-session registry, notification and status
mechanisms, and any streaming output modes of the CLI. Determine whether raw terminal/PTY output exists
anywhere, and what it would take to obtain a real terminal stream. Judge freshness, ordering and failure
modes of each channel, and the cost each imposes on the user's actual session.`,
  },
  {
    key: 'control',
    focus: `THE CONTROL PLANE: can an external web page start, pause, restart or terminate agent loops?
Establish first what a "loop" IS at runtime by reading the skill templates (who spawns whom, where state
lives, what a resumable boundary is). Then find every mechanism by which anything outside a running CLI
process can affect it: process-level signals, sockets or IPC, CLI flags and headless/streaming modes,
job/background mechanisms, file-based channels, and any in-process control functions that exist but lack
a transport. For each of start/pause/restart/terminate, state what is actually achievable, what it does
to in-flight work and on-disk state, and what is semantically impossible.`,
  },
  {
    key: 'priorart',
    focus: `THE EXISTING MONITORING MODULE as prior art: .claude/shared/monitoring/ plus the three skills.
Read the implementation, not just the docs. Establish precisely which problems it has already solved
correctly (tailing semantics, checkpointing, replay, id rotation, token dedup, path containment, the
event schema and its reserved names, test approach), which of its assumptions break for Touch's broader
scope, and where it is structurally unable to grow (transport direction, coupling to the marker
convention, single-run assumptions, security posture). Recommend concretely what to reuse verbatim, what
to generalize, and what to replace — with the migration consequence of each.`,
  },
  {
    key: 'stack',
    focus: `IMPLEMENTATION STACK AND DELIVERY for aggregator + touch-visual. Decide the runtime, transport,
terminal-emulation and graph-rendering approach, weighing the repo's zero-third-party-dependency precedent
and stdlib-only tests against the sandbox's firewall and port-publishing constraints (verify what package
installs actually succeed here rather than assuming). Cover: process model and how the web tier reaches
session data; live update transport; how a terminal is rendered in a browser; how an n8n-style graph is
laid out and updated incrementally; state/reconnect semantics; auth and exposure of a local daemon that
can read credentials-adjacent files; testability; and repo layout consistent with CLAUDE.md.`,
  },
]

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
Reading ~/.claude and grepping the CLI binary (use timeouts; it is ~275MB) is in
scope and encouraged; treat what you observe as the ground truth over any
documentation. Report only real, actionable items (defects, risks, gaps,
decisions to make) with a concrete rationale each.
Severity: blocker | major | minor | nit.

Write your FULL findings to ${FINDINGS}/research-${p.key}-attempt-1.md — one
section per finding: id ${p.key.toUpperCase()}-<n>, file:line (or the exact
command/path that proves it), severity, the concrete scenario, and a concrete
recommendation. This file is task state — writing it is required.
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
FIRST run: ${statusCmd('synthesis', 'synthesize', 'running', 'merging ' + reports.length + ' research reports plus driver context')}

Research reports (read ALL of them fully from disk first):
${reports.map(r => '- ' + r.findings_file + ' — ' + r.summary).join('\n')}

DRIVER CONTEXT (read this SECOND, in full — it is REQUIRED input):
${DRIVER_CONTEXT}
The research agents ran BLIND on purpose: they never saw that file, so their
findings are independent evidence. It carries the driver session's own verified
observations, two prior feasibility assessments, the product spec, the
environment constraints, and an explicit list of UNVERIFIED items. Where a
research finding and the driver context disagree, do NOT default to either —
open the primary source (~/.claude, the repo, or the CLI binary at
/home/agent/.local/share/claude/versions/2.1.220) and decide; record the decision
and which side it overrode. Where they agree, treat the item as confirmed by
independent evidence and say so.

Subject:
${SUBJECT}

Tasks:
1. Merge + dedup the findings (same item from two perspectives = one, keep both
   ids as aliases). Discard non-items with a one-line justification each. Where
   two reports contradict, open the source and decide.
2. Decide every global/protocol question ONCE (canonical shapes, who tolerates
   what) so downstream work cannot diverge. At minimum decide: process ownership
   (does Touch host sessions or only observe them) and the consequence for every
   UI affordance; the canonical internal event/state model; the read pipeline
   (what is watched, how indexed, live vs historical); which of
   start/pause/restart/terminate ship first, by what mechanism, and what each
   honestly does to in-flight work; the technology stack justified against the
   sandbox firewall, the port-publishing constraint and the zero-dependency
   precedent; and the security posture of a daemon with read access to
   credentials-adjacent files.
3. Order the accepted items into ONE complete implementation plan. Per item:
   id, title, affected files (file:line), the finding ids it resolves, the
   decided approach, and what a test should cover. Do NOT partition the plan
   into sub-plans — implement-plan's divider owns divide-and-conquer; just keep
   each item concrete and self-contained enough to be partitioned later without
   re-research. Every item must name the files it creates or changes, since the
   divider partitions by file ownership.
4. Write the full plan to ${PLAN_FILE} (mkdir -p its dir first): the global
   decisions section, then the ordered item list. Findings stay in the research
   files — reference them by id + path. Include a short section listing anything
   that remains UNVERIFIED and the cheapest experiment that would settle it.

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
const synth = await agent(synthPrompt(reports), {
  model: 'fable', effort: 'xhigh',
  label: 'synthesize', phase: 'Synthesize', schema: SYNTH_SCHEMA,
})
if (!synth || !synth.plan_file || !synth.item_count) {
  throw new Error('synthesis produced no plan items — cannot hand off a plan')
}
log(`plan ready: ${synth.item_count} items in ${synth.plan_file}`)

return {
  raw_findings: reports.map(r => ({ file: r.findings_file, count: r.findings.length, summary: r.summary })),
  plan_file: synth.plan_file,
  item_count: synth.item_count,
  summary: synth.summary,
}
