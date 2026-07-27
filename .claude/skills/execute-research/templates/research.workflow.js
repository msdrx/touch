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

// SCRIPT-emitted status event (R-09). Terminal plan/run events must not depend on
// an agent remembering its LAST-run line: the script emits them at a fixed
// control-flow point, so they are as deterministic as the journal itself.
// Best-effort by contract — a monitoring write never fails the workflow.
// NEVER route this through a shell: `plan`/`msg` can carry agent-authored text
// (a perspective key, a file path from structured output), and a shell string
// would make a stray `"` mangle the event — or worse, execute. argv + env are
// passed straight to status.sh; `statusCmd` stays for PROMPT TEXT only.
// status.sh is a best-effort writer that reports problems on STDERR (an
// out-of-enum state, an unwritable state dir) and still exits 0, so stderr is
// captured and logged — a discarded warning is the one way this call can fail
// silently. spawnSync (not execFileSync) because only spawnSync hands back the
// child's stderr on SUCCESS.
let statusProbed = false
// `extraEnv` (optional) carries additive status.sh env keys — e.g.
// ORCH_PLANS_TOTAL, the declared plan-card total — as env, never argv, so the
// argv contract above stays five fixed strings.
const runStatus = async (plan, stage, state, msg, extraEnv) => {
  try {
    const cp = await import('node:child_process')
    if (!statusProbed) { statusProbed = true; log('status emitter ready (node:child_process)') }
    const r = cp.spawnSync('bash', [S, String(plan), String(stage), String(state), String(msg)],
      { env: { ...process.env, ORCH_STATE_DIR: TASK, ...(extraEnv || {}) }, encoding: 'utf8' })
    if (r.error) throw r.error
    const warn = (r.stderr || '').trim()
    if (warn) log(`status.sh warned on ${plan}/${stage}/${state}: ${warn.split('\n')[0]}`)
  } catch (e) {
    log(`status event ${plan}/${stage}/${state} not written: ${e}`)
  }
}

// Publish this script's caps + strategy into orch-config.json (R-09) so the
// watcher narrates the SAME numbers the loops actually enforce instead of its
// built-in defaults. The watcher re-reads this file while running (it starts
// BEFORE this script does), so writing it here is not too late.
// `strategy` only decides whether the watcher's LEGACY sequenced plan-close
// heuristic runs at all (GD-10; a research fan-out is not serial, so it must
// not). It is NOT what fixes the fabricated `plan failed` badge — the watcher's
// close predicate `close_state_for()` plus the script-emitted terminal
// `plan done` below are (R-58). Do not "simplify" either away on the strength of
// this key. Merge, never overwrite: the driver may have written wf_dir/port here.
const publishConfig = async () => {
  try {
    const fs = await import('node:fs')
    const path = TASK + '/orch-config.json'
    fs.mkdirSync(TASK, { recursive: true })
    let cfg = {}
    try { cfg = JSON.parse(fs.readFileSync(path, 'utf8')) } catch (e) { cfg = {} }
    // This workflow has no retry loop, so it publishes no attempt caps — only
    // the strategy. (implement-plan's template publishes its MAX_ATTEMPTS too.)
    fs.writeFileSync(path, JSON.stringify({ ...cfg, strategy: 'parallel' }, null, 2) + '\n')
  } catch (e) {
    log(`could not publish orch-config.json: ${e}`)
  }
}

// R-40 run-close protocol: when the run ends, close the Orchestrator badge with
// the run's REAL state — `done` only on the success path, `failed` on every throw
// path, because monitor_server's home grid treats the reserved `orchestrator`
// card as authoritative and a hardcoded `done` would paint a thrown run green.
// This event is ALSO what authorizes the watcher to stop: decision_watcher.py
// exits only on a `w:"agent"` `orchestrator complete done|failed` line appended
// after it started (its own inferred close never stops it, because a harness
// stall is indistinguishable from a finished run). So this call is the mechanism;
// the pid signal below is only a fast path.
//
// Daemon shutdown: kill by RECORDED PID only, and only after VERIFYING that the
// pid really is a decision_watcher — a stale pid file is the same wrong-target
// hazard as a name-matched kill (pids get reused, and other tasks' watchers are
// live processes; GD-12's invariant, GD-1's gate). The launch side must record
// the pid — `python3 decision_watcher.py & echo $! > "$TASK/watcher.pid"`, the
// form monitoring.md's run block documents; without that line this block is a
// no-op and the watcher's own self-exit does the work. monitor_server.py is
// deliberately NOT touched: ONE server serves ALL tasks, so a per-task epilogue
// SIGTERMing it would take the dashboard down for every other live run.
//
// The signal is sent IMMEDIATELY after the terminal event, which is only safe
// because decision_watcher.py handles SIGTERM by DRAINING (one more tail+emit
// pass, then a short quiet window) instead of dying where it stands. Its poll
// interval is ~1 s and the last agent's journal `result` lands ~0.2 s before
// this call, so an unhandled signal lost that agent's stage chip, its decision
// line and — token deltas being wire-only — its ENTIRE usage from the run
// totals, permanently, in most runs (M-2). Do not "simplify" the watcher's
// stop handler away on the grounds that this epilogue already closed the run.
const closeRun = async (state, summary) => {
  await runStatus('orchestrator', 'complete', state, summary)
  try {
    const fs = await import('node:fs')
    const p = TASK + '/watcher.pid'
    if (!fs.existsSync(p)) { return }
    const pid = parseInt(fs.readFileSync(p, 'utf8').trim(), 10)
    if (!pid) { return }
    let cmdline = null
    try { cmdline = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8') } catch (e) { cmdline = null }
    if (cmdline === null) {
      log(`watcher.pid ${pid} not verifiable (no /proc entry) — leaving it to the watcher's self-exit`)
      return
    }
    if (!cmdline.includes('decision_watcher')) {
      log(`stale watcher.pid ${pid} (not a decision_watcher) — not signalling`)
      try { fs.unlinkSync(p) } catch (e) { /* leave it */ }
      return
    }
    try { process.kill(pid, 'SIGTERM'); log(`stopped watcher.pid (${pid})`) } catch (e) { /* already gone */ }
    try { fs.unlinkSync(p) } catch (e) { /* leave it */ }
  } catch (e) {
    log(`daemon shutdown skipped: ${e}`)
  }
}

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
Return structured output only: plan_file, item_count, summary.
`
// NOTE: the agent is deliberately NOT asked to emit `synthesis plan done` — the
// terminal plan event is script-emitted below (R-09), so a plan card can never
// hang open because an agent forgot its last line.

phase('Research')
await publishConfig()
log(`spawning ${PERSPECTIVES.length} read-only research agents: ${PERSPECTIVES.map(p => p.key).join(', ')}`)
// Barrier is required: synthesis needs ALL reports to dedup across perspectives.
const reports = (await parallel(PERSPECTIVES.map(p => () =>
  agent(researchPrompt(p), {
    model: 'opus', effort: 'high',
    label: `research:${p.key}`, phase: 'Research', schema: RESEARCH_SCHEMA,
  })
))).filter(Boolean)
log(`research done: ${reports.length}/${PERSPECTIVES.length} returned, ${reports.reduce((n, r) => n + r.findings.length, 0)} raw findings`)
// Terminal plan event for the research fan-out, emitted at the barrier (R-09).
// A fan-out returns findings, never a gate verdict, so nothing else would ever
// close this card — and the watcher heuristic that used to close it invented a
// `failed` badge while every researcher had succeeded (R-58/GD-10).
// The close also DECLARES this run's plan-card count (research + synthesis)
// via ORCH_PLANS_TOTAL, so dashboards show progress over both cards before
// the synthesis card ever appears. Readers fold plans_total as a monotonic
// max, so a re-declaration is idempotent.
if (reports.length) {
  await runStatus('research', 'plan', 'done',
    `${reports.length}/${PERSPECTIVES.length} researchers returned`,
    { ORCH_PLANS_TOTAL: '2' })
} else {
  await runStatus('research', 'plan', 'failed',
    `no researcher returned (0/${PERSPECTIVES.length})`,
    { ORCH_PLANS_TOTAL: '2' })
  // Nothing to synthesize. Spawning synthesis with zero reports only buys a
  // second failure while the log reads as if the run continued normally, so the
  // run ends here — badge, daemons and thrown error all agree (R-40).
  await closeRun('failed', 'run failed: no researcher returned')
  throw new Error('no researcher returned — nothing to synthesize')
}

phase('Synthesize')
// Synthesis is the only stage in THIS workflow allowed to use fable; research
// agents stay on opus. (implement-plan's final gate reviewer also runs fable.)
const synth = await agent(synthPrompt(reports), {
  model: 'fable', effort: 'xhigh',
  label: 'synthesize', phase: 'Synthesize', schema: SYNTH_SCHEMA,
})
if (!synth || !synth.plan_file || !synth.item_count) {
  await runStatus('synthesis', 'plan', 'failed', 'synthesis produced no plan items')
  await closeRun('failed', 'run failed: synthesis produced no plan')
  throw new Error('synthesis produced no plan items — cannot hand off a plan')
}
log(`plan ready: ${synth.item_count} items in ${synth.plan_file}`)
await runStatus('synthesis', 'plan', 'done', `plan written: ${synth.item_count} items`)

// Close the orchestrator badge and stop this task's daemons (R-09/R-40): the
// watcher cannot see run completion in the journal. The driver may repeat the
// `orchestrator complete done` call after the workflow returns — it is an
// idempotent backstop, and the badge is last-event-wins either way.
await closeRun('done', `research complete: ${synth.item_count} plan items`)

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
