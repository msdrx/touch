// Reference research workflow — deterministic research -> ONE complete
// synthesized plan. This file is GENERIC and SPEC-DRIVEN (GD-D9/D-12): every
// per-run value arrives in `args` from a run-spec JSON, so `orch-scripts/`
// copies are a byte-for-byte `cp` of this file and the driver authors only the
// spec. Keep the protocol:
//   * research + synthesis agents are READ-ONLY for source (they only read, run
//     safe inspection, and write findings/plan files = task state)
//   * the fan-out is a pure function of PERSPECTIVES (deterministic agent count)
//   * every spawn goes through `agentR` — an infrastructure death is a strike,
//     never a verdict (D-11a)
//   * a partial board is REFUSED, never synthesized (D-11b)
//   * synthesis merges accepted findings into ONE complete plan (global
//     decisions + ordered items) — it never partitions into sub-plans; that
//     divide-and-conquer belongs to implement's Fable divider
//   * every agent is a brand-new subagent with fresh context; never reuse/resume
// This script STOPS at the plan. implement consumes { plan_file }.
//
// THIS SCRIPT EMITS NO EVENTS, AND THAT IS THE DESIGN (GD-D5, D-10). The
// workflow runtime has no Node API — every `import('node:…')` throws — so the
// `runStatus`/`closeRun`/`publishConfig` helpers this template used to carry
// silently no-opped in every real run; one run failed on nothing else. They are
// deleted rather than kept as decoration. The DETERMINISTIC emitters are:
//   decision_watcher.py  spawn/result/verdict/token events, derived from the
//                        run journal and the `[monitor]` marker below
//   cycle_reporter.py    the loop-terminal `plan done|failed` events and the
//                        per-cycle report pages
//   touch-run            the run envelope: seeds the cards, publishes caps and
//                        wf_dir into orch-config.json, closes the run, stops
//                        the daemons by recorded pid
// `status.sh` (the `touch-status` wrapper) stays the ONE write path into
// events.jsonl and keeps three legitimate callers — a human, a driver, and
// those emitters (GD-D14). What is gone is the MANDATE: no prompt below
// instructs an agent to trace itself (D-09), because an instruction is not a
// mechanism and the watcher already derives the same spawn and the same result
// from recorded bytes.
//
// HISTORY OF THIS DESIGN. The bracketed ids in the comments below (R-nn, GD-nn,
// D-nn, M-n, SHELL-n, n-n) are the finding and decision ids of the plans this
// protocol was derived from, in the project that produced it. They are kept
// because each one marks a rule that a real run broke once, and a reader who
// removes the rule should know a defect is on the other side of it. They are
// NOT files you have, and nothing here reads them — treat them as footnotes.
export const meta = {
  name: 'touch-research',
  description: 'Research a subject from each configured read-only perspective and synthesize ONE complete implementation plan',
  phases: [
    { title: 'Research', detail: 'N read-only researchers, one perspective each', model: 'opus' },
    { title: 'Synthesize', detail: 'dedup + decide + write ONE complete plan', model: 'fable' },
  ],
}

// ---- the run spec (GD-D9/D-12) --------------------------------------------
// `args` is what the launcher hands this script. `touch-run start` builds it by
// merging the tracked per-project constants (`.touch/run.json`) under the
// run-spec file, so a per-project value is configured ONCE and a per-run value
// overrides it. `typeof args === 'undefined'` is checked FIRST: a bare
// identifier reference on a runtime that never injected `args` is a
// ReferenceError, not a falsy value.
//
// Recognized keys, all optional (the fallbacks are what a hand-launched copy
// sees, never something a preflight greps for — `touch-run verify` preflights
// the SPEC, because this file is copied verbatim and its defaults would
// otherwise read as leaked placeholders):
//   project_dir  perspectives   min_reports
//   task         subject        context
//   plugin_root  plan_file      net_retries
const ARGS = typeof args === 'undefined' ? {}
  : (typeof args === 'string' ? JSON.parse(args) : (args || {}))

// TWO roots, never one. The project this run works in and the installed plugin
// that carries the monitoring commands are different directories on every
// machine but the one a template like this was first written on — and the
// plugin root is a version-stamped cache re-copied on each update, so nothing
// durable may live under it (task state is project-anchored, always). Neither
// is ever BAKED into a copy: both arrive in the spec, and the literals below
// are the defaults a reader sees when no spec was supplied (SKILLS-14).
const PROJECT_DIR = ARGS.project_dir || '/ABS/PATH/TO/PROJECT'
const PLUGIN_ROOT = ARGS.plugin_root || '/ABS/PATH/TO/PLUGIN_ROOT'
const TASK_NAME = ARGS.task || 'task'
const TASK = PROJECT_DIR + '/.touch/local-orchestrators/' + TASK_NAME
const FINDINGS = TASK + '/findings'
const PLAN_FILE = ARGS.plan_file || (TASK + '/plan/' + TASK_NAME + '-plan.md')

// Infrastructure guard — network-recovery.md layer 2, IN the protocol, not
// optional prophylaxis, and identical in both templates (D-11a: the research
// template shipped WITHOUT it, so a researcher that died on an outage silently
// left the board short and the synthesizer planned blind). An `agent()` that
// returns null died on infrastructure (an API outage outlasting the harness's
// own retries, or a user skip) — that is a STRIKE, never a verdict. Retry the
// same work up to NET_RETRIES times, then THROW so the run stops cleanly where
// it stands, the journal marking the exact spawn. The appended retry tag makes
// the prompt distinct so a later resumeFromRunId re-executes the retried call
// live instead of replaying a cached null; the [monitor] marker is unchanged
// (same attempt — honest display, one extra agent row). `??`, never `||`: a
// spec that says `net_retries: 0` means zero, and a `||` would silently make it
// three.
const NET_RETRIES = ARGS.net_retries ?? 3
const agentR = async (prompt, opts) => {
  let r = await agent(prompt, opts)
  for (let n = 1; r === null && n <= NET_RETRIES; n++) {
    log(`${opts.label}: agent returned null (infrastructure death) — same-attempt retry ${n}/${NET_RETRIES}`)
    r = await agent(
      prompt + `\n(infrastructure retry ${n}: the previous try of this exact task died without returning — outage, not a task failure. Do the task from scratch.)`,
      { ...opts, label: `${opts.label}~r${n}` })
  }
  if (r === null) {
    throw new Error(`${opts.label}: agent died ${NET_RETRIES + 1}x — infrastructure down; ` +
      `attempts preserved, resume per plan/RESUME.md (network-recovery.md, manual restart)`)
  }
  return r
}

// The subject: the exact files / dirs / sources every researcher may read.
// An array in the spec, joined here — a perspective may narrow it (see
// `subjectFor`), which is the whole point of D-24's per-perspective subject:
// pasting the union of every source into all N prompts is the single largest
// avoidable constant in this workflow.
const asLines = (v) => (Array.isArray(v) ? v.join('\n') : String(v || ''))
const SUBJECT = asLines(ARGS.subject) ||
  [PROJECT_DIR + '/path/to/subject-file-a',
   PROJECT_DIR + '/path/to/subject-file-b'].join('\n')

// DETERMINISTIC perspective list — one read-only agent per entry. The agent
// count and prompts are a pure function of this array. `subject` is optional
// per entry and scopes the SUBJECT block to that researcher (D-24).
const PERSPECTIVES = (Array.isArray(ARGS.perspectives) && ARGS.perspectives.length)
  ? ARGS.perspectives
  : [
    { key: 'AREA_A', focus: 'What AREA_A researcher attacks/analyzes — concrete scope.' },
    { key: 'AREA_B', focus: 'What AREA_B researcher attacks/analyzes — concrete scope.' },
  ]

// The board must be COMPLETE before anything is synthesized (D-11b, SKILLS-2).
// A plan merged from a partial board is silently blind — it reads like a
// finished plan while a whole perspective's findings are simply absent — so a
// short board stops the run instead. Mirror of the implement template's
// never-vanish rule: a loop that dies without a verdict is never dropped.
const MIN_REPORTS = ARGS.min_reports ?? PERSPECTIVES.length

// What this run is about, in the researchers' own words. Whatever the driver
// could not express as a subject path belongs here.
const CONTEXT = ARGS.context ||
  'TASK_SPECIFIC_CONTEXT (what this system is, invariants, goal of the research).'

// D-24 / ECONOMICS-6: one line, not a policy. Measured over this project's own
// recorded sessions: Bash carried 50.6% of all tool-result VOLUME, on 16,786
// Bash calls against 3,005 Read calls (5.6:1 by count). Most of that volume is
// source read through `cat`/`sed`/`head`, which bypasses Read's offset/limit
// windowing and the harness's own truncation accounting. One sentence in the
// shared preamble is the whole intervention — anything more polices the
// irreducible core (GD-D7).
const READ_DISCIPLINE =
  'Read files with the Read tool (offset/limit on long files) rather than cat/sed/head through Bash.'

const subjectFor = (p) => asLines(p.subject) || SUBJECT

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

// The `[monitor]` marker is line 1 of every prompt and is FENCED (GD-D1a):
// decision_watcher.py and aggregator/agents.py derive plan/stage/role/attempt
// from it with zero LLM cooperation, so trimming, renaming or moving it turns
// every derived event for that agent into an unnamed bucket. It is the one line
// in this file a token-reduction pass may not touch.
//
// The invariant `Method:` paragraph these prompts used to repeat N times lives
// in the skill's SKILL.md now (D-24) — it never varied per perspective, and a
// constant pasted into N prompts is N copies of the same tokens.
const researchPrompt = (p) => `
[monitor] plan=research stage=${p.key} role=research attempt=1
You are a READ-ONLY researcher on the subject below. Never edit any source file —
you only read, run safe inspection commands, and write ONE findings file in the
task state folder.
${READ_DISCIPLINE}

Subject (read all that your focus touches; line numbers matter):
${subjectFor(p)}

RESEARCH_CONTEXT: ${CONTEXT}

YOUR PERSPECTIVE: ${p.focus}

Write your FULL findings to ${FINDINGS}/research-${p.key}-attempt-1.md — one
section per finding: id ${p.key.toUpperCase()}-<n>, file:line, severity
(blocker | major | minor | nit), the concrete scenario, and a concrete
recommendation. Report only real, actionable items. This file is task state —
writing it is required.
`

// The research protocol's half of the report rule the implement template
// carries: a report shows what was ASKED FOR, what was DELIVERED, and where the
// two differ. Here the requirement is the BOARD — every finding the researchers
// returned, ids and all — and the delivery is the plan. `plan_file` and
// `item_count` say how big the plan is; neither says WHICH findings reached it,
// which is the question a reader of a merged-and-deduped plan actually has, and
// the one a discard justification buried in prose cannot answer at a glance.
//   accepted -> became a plan item (note: which one)
//   merged   -> folded into another finding's item (note: which id)
//   dropped  -> deliberately not carried (note: the justification)
// A finding with no entry renders UNACCOUNTED, which is the only real gap of
// the three: a stated drop is a decision, silence is a hole in the plan.
const SYNTH_COVERAGE = {
  type: 'object', required: ['id', 'status', 'note'],
  properties: { id: { type: 'string' },
                status: { type: 'string', enum: ['accepted', 'merged', 'dropped'] },
                note: { type: 'string' } },
}
const SYNTH_SCHEMA = {
  type: 'object', required: ['plan_file', 'item_count', 'summary', 'coverage'],
  properties: {
    plan_file: { type: 'string' },
    item_count: { type: 'integer' },
    summary: { type: 'string' },
    coverage: { type: 'array', items: SYNTH_COVERAGE },
  },
}

const synthPrompt = (reports) => `
[monitor] plan=synthesis stage=synthesize role=synth attempt=1
You are the PLAN SYNTHESIZER. READ-ONLY for source; you write exactly one plan
file in task state.
${READ_DISCIPLINE}

Research reports (read ALL of them fully from disk first):
${reports.map(r => '- ' + r.findings_file + ' — ' + r.summary).join('\n')}

Subject:
${SUBJECT}

RESEARCH_CONTEXT: ${CONTEXT}

Tasks:
1. Merge + dedup the findings (same item from two perspectives = one, keep both
   ids as aliases). Discard non-items with a one-line justification each. Where
   two reports contradict, open the source and decide.
2. Decide every global/protocol question ONCE (canonical shapes, who tolerates
   what) so downstream work cannot diverge.
3. Order the accepted items into ONE complete implementation plan. Per item:
   id, title, affected files (repo-relative path:line), the finding ids it
   resolves, the decided approach, and what a test should cover. Do NOT
   partition the plan into sub-plans — implement's divider owns
   divide-and-conquer; just keep each item concrete and self-contained enough
   to be partitioned later without re-research.
4. Write the full plan to ${PLAN_FILE} (mkdir -p its dir first): the global
   decisions section, then the ordered item list. Findings stay in the research
   files — reference them by id + path.
5. Return \`coverage\`: ONE entry per finding id on the board — \`id\` exactly as
   the report wrote it, \`status\` accepted|merged|dropped, \`note\` in ≤120
   chars (accepted: the plan item it became; merged: the id it folded into;
   dropped: the justification from task 1). This is the report's board→plan
   row, not the report: the reasoning stays in the plan. A finding you leave out
   renders UNACCOUNTED, which reads worse than an honest \`dropped\`.
`

// REFUSE an incomplete spec before spending anything (D-12). This is defense in
// depth UNDER `touch-run verify`, not a duplicate of it: `verify` preflights the
// spec FILE, this checks what the runtime actually INJECTED — and the two fail
// in different places, because a launcher can hand a verified spec to the wrong
// script or hand this one nothing at all. Narrating the problem and continuing
// would spawn the full opus fan-out against `/ABS/PATH/TO/PROJECT`, whose
// findings writes cannot land, off the placeholder perspective board: a whole
// run's tokens for a result that cannot exist. The literals above stay as
// documentation of the SHAPE a spec must fill (SKILLS-14 leaves `plugin_root`
// as shipped, so it is deliberately not required here).
if (!ARGS.project_dir || !ARGS.task) {
  throw new Error(
    'run-spec incomplete: project_dir and task are required — launch through ' +
    '`touch-run start --spec <file>`; this script is copied byte-for-byte and ' +
    'its /ABS/PATH defaults are placeholders, never a working configuration')
}

phase('Research')
log(`touch research: task=${TASK_NAME} project=${PROJECT_DIR} plugin=${PLUGIN_ROOT}`)
log(`spawning ${PERSPECTIVES.length} read-only research agents: ${PERSPECTIVES.map(p => p.key).join(', ')}`)
// Barrier is required: synthesis needs ALL reports to dedup across perspectives.
const reports = (await parallel(PERSPECTIVES.map(p => () =>
  agentR(researchPrompt(p), {
    model: 'opus', effort: 'high',
    label: `research:${p.key}`, phase: 'Research', schema: RESEARCH_SCHEMA,
  })
))).filter(Boolean)
log(`research done: ${reports.length}/${PERSPECTIVES.length} returned, ${reports.reduce((n, r) => n + r.findings.length, 0)} raw findings`)

// D-11b, the partial-board refusal. `parallel()` turns a thrown callback (an
// agentR that gave up on a dead API) into a silent null, so a short board is
// exactly how a dead perspective LOOKS — never let one vanish. Synthesizing
// anyway buys a plan that reads complete and is not, and the run log reads as
// if nothing happened. The run stops here instead.
//
// This script claims NO badge for that stop, and the distinction matters: it
// cannot write an event at all (GD-D5), and the badge it would like to claim is
// not the one that lands. cycle_reporter.py's zero-return rule (D-14) closes a
// research card `failed` only when the board is EMPTY; a PARTIAL board carries
// results with `findings`, which that same rule reads as `done`. Until D-14
// learns MIN_REPORTS (carried to the reporter sub-plan), a partial-board stop is
// settled by the watcher's layered close (D-07), not by a `failed` badge — so
// the log line below states what happened here, never what some other program
// will emit. Announcing a verdict this script cannot cause is R-58's defect with
// the sign flipped, and it sends a maintainer chasing the wrong badge into a
// file that is behaving exactly as specified.
//
// A spec is free to lower MIN_REPORTS, but never to zero-out this guard: an
// empty board is the blind plan in its purest form, and the synthesizer would
// happily write one from a prompt whose report list is a blank line.
if (!reports.length || reports.length < MIN_REPORTS) {
  log(`refusing to synthesize: ${reports.length}/${PERSPECTIVES.length} researchers returned, ` +
    `${MIN_REPORTS} required — a partial board yields a plan that reads complete and is not`)
  throw new Error(
    `only ${reports.length} of ${MIN_REPORTS} required research reports returned — ` +
    `nothing is synthesized from a partial board (attempts preserved; resume per plan/RESUME.md)`)
}

phase('Synthesize')
// Synthesis is the only stage in THIS workflow allowed to use fable; research
// agents stay on opus. (implement's final gate reviewer also runs fable.)
const synth = await agentR(synthPrompt(reports), {
  model: 'fable', effort: 'xhigh',
  label: 'synthesize', phase: 'Synthesize', schema: SYNTH_SCHEMA,
})
if (!synth.plan_file || !synth.item_count) {
  throw new Error('synthesis produced no plan items — cannot hand off a plan')
}
log(`plan ready: ${synth.item_count} items in ${synth.plan_file}`)

// OPTIONAL AUTO-CHAIN: only if <user_prompt> asked to implement/build it, the
// DRIVER invokes the implement skill on the SAME task folder after this
// workflow returns, handing it { plan_file } only — implement's Fable
// divider derives the sub-plans. Never partition here.
//
// The run close (badge, ACTIVE line, daemon stop) is `touch-run close`'s, not
// this script's (D-13): the driver runs it after this workflow returns, and the
// watcher's own layered close (D-07) settles the badge even if it never does.

return {
  raw_findings: reports.map(r => ({ file: r.findings_file, count: r.findings.length, summary: r.summary })),
  plan_file: synth.plan_file,
  item_count: synth.item_count,
  summary: synth.summary,
}
