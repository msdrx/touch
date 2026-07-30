// Reference implement-plan workflow — divide ONE complete plan into isolated
// feature-sub-plans (Fable divider), then implement them through gated
// impl->test->critique loops, SERIALLY by default (one sub-plan at a time),
// PARALLEL only when instructed, then a final aggregate gate over the merged
// change-set. Adapt the ALL-CAPS constants and prompt bodies per task; keep the
// protocol:
//   * the sub-plan partition comes from the Divide phase (fable) — the gated
//     loops are a pure function of its structured output
//   * gates are READ-ONLY for source, but MUST write findings files to the task folder
//   * implementer attempts are brand-new agents that READ findings files from disk
//   * once new findings land, prior-attempt agents are stale — never reuse or resume them
//     (serial: they have already exited; parallel: drop/stop them before the successor)
// args = { plan_file?, parallel? } — sub-plans are DERIVED here, never handed in
//
// HISTORY OF THIS DESIGN. The bracketed ids in the comments below (R-nn, GD-nn,
// D-n, M-n, SHELL-n, n-n) are the finding and decision ids of the plans this
// protocol was derived from, in the project that produced it. They are kept
// because each one marks a rule that a real run broke once, and a reader who
// removes the rule should know a defect is on the other side of it. They are
// NOT files you have, and nothing here reads them — treat them as footnotes.
export const meta = {
  name: 'TASK_NAME-implement',
  description: 'ONE_LINE_DESCRIPTION — implement feature-sub-plans via gated loops',
  phases: [
    { title: 'Divide', detail: 'fable divider: one plan -> isolated feature-sub-plans', model: 'fable' },
    { title: 'Implement', model: 'opus' },
    { title: 'Test', model: 'opus' },
    { title: 'Critique', model: 'opus' },
    { title: 'FinalGate', model: 'fable' },
  ],
}

// TWO roots, never one. The project this run works in and the installed plugin
// that carries the monitoring commands are different directories on every
// machine but the one a template like this was first written on — and the
// plugin root is a version-stamped cache re-copied on each update, so nothing
// durable may live under it (task state is project-anchored, always).
//
// Nothing substitutes a placeholder inside a SUPPORTING file: this template is
// handed to you verbatim. The SKILL.md that sent you here IS substituted, and
// it carries the value to paste into PLUGIN_ROOT.
const PROJECT_DIR = '/ABS/PATH/TO/PROJECT'      // FILL IN: this run's project root
const PLUGIN_ROOT = '/ABS/PATH/TO/PLUGIN_ROOT'  // FILL IN: the installed plugin root
const TASK = PROJECT_DIR + '/.claude/local-orchestrators/TASK_NAME'
const FINDINGS = TASK + '/findings'

// The event writer is a COMMAND NAME, not a path: the plugin puts its bin/ on
// PATH, and a name survives an update that moves the file behind it while a
// baked cache path does not. PLUGIN_ROOT exists for exactly one reason — the
// fallback below, for a runtime whose PATH does not carry that bin/. Be honest
// about what that is worth today: the CURRENT workflow runtime has no Node API
// (the `import('node:child_process')` inside runStatus throws — see the
// cycle-report note further down), so neither the command nor the fallback is
// ever reached from here, and the driver emits the terminal events instead.
// The constant is here for a runtime that gains one. If yours has not, leave
// PLUGIN_ROOT exactly as it is — an unfilled placeholder costs nothing, while
// a real path baked into a version-stamped plugin cache is swept out from
// under this file by the next update.
const STATUS = 'touch-status'
const STATUS_FALLBACK = PLUGIN_ROOT + '/bin/touch-status'
const MAX_ATTEMPTS = 4
// Final aggregate gate: gate -> fixer -> re-gate. Published to orch-config.json
// so the watcher's decision text quotes this cap, not its own default (R-09).
const FINALGATE_ATTEMPTS = 2

// Hand-off is { plan_file?, parallel? } — ONE complete plan, never sub-plans;
// the Divide phase below derives them. Default strategy is SEQUENTIAL.
const ARGS = typeof args === 'string' ? JSON.parse(args) : (args || {})
const PLAN_FILE = ARGS.plan_file || TASK + '/plan/TASK_NAME-plan.md'
const SUBPLANS_FILE = TASK + '/plan/TASK_NAME-subplans.md'
const PARALLEL_MODE = ARGS.parallel === true   // never shadow the parallel() global
// User-granted attempt extensions after an 'awaiting-user' close: relaunch /
// resume with args.extra_attempts = { 'sp-<slug>': N } to raise ONLY that
// loop's cap to MAX_ATTEMPTS + N. This is how "add another attempt to that
// loop" is granted — never by editing MAX_ATTEMPTS for everyone.
const EXTRA_ATTEMPTS = ARGS.extra_attempts || {}

// Infrastructure guard — network-recovery.md layer 2, IN the protocol, not
// optional prophylaxis. An `agent()` that returns null died on infrastructure
// (an API outage outlasting the harness's own retries, or a user skip) — that
// is a STRIKE, never a verdict: it must not spend a gated attempt and must not
// be laundered into a fabricated "gate died" red. Retry the same work on the
// SAME attempt up to NET_RETRIES times, then THROW so the run stops cleanly
// where it stands — attempts unspent, the journal marking the exact spawn, the
// RESUME.md procedures continuing from here. The appended retry tag makes the
// prompt distinct so a later resumeFromRunId re-executes the retried call live
// instead of replaying a cached null; the [monitor] marker is unchanged (same
// attempt — honest display, one extra agent row). This wrapper used to live
// only in network-recovery.md as launch-time advice; the run that promoted it
// here had two loops burn all four attempts in a 2h outage (~3 minutes per
// death, zero substantive verdicts) and close `failed (retryable)` — a
// verdict class the gates never issued.
const NET_RETRIES = 3
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

// Quote every path interpolation so a PROJECT_DIR/TASK path with a space cannot
// split the env assignment / arg list. Keep agent-filled <summary> text
// single-line, no double quotes (see m-orchestrator SKILL.md).
const statusCmd = (plan, stage, state, msg) =>
  `ORCH_STATE_DIR="${TASK}" ${STATUS} "${plan}" ${stage} ${state} "${msg}"`

// SCRIPT-emitted status event (R-09). Terminal plan/run events must not depend on
// an agent remembering its LAST-run line: the script emits them at a fixed
// control-flow point, so they are as deterministic as the journal itself.
// Best-effort by contract — a monitoring write never fails the workflow.
// NEVER route this through a shell: `plan` is the DIVIDER AGENT's `sp.id` and
// `msg` can carry an agent-authored file path, so a shell string would let a
// stray `"` mangle the event — or execute arbitrary commands in the driver
// process. argv + env go straight to the writer; `statusCmd` stays for PROMPT
// TEXT only (where the agent runs it itself, in its own shell).
// The writer is best-effort: it reports problems on STDERR (an out-of-enum
// state, an unwritable state dir) and still exits 0, so stderr is captured and
// logged — a discarded warning is the one way this call can fail silently.
// spawnSync (not execFileSync) because only spawnSync hands back the child's
// stderr on SUCCESS.
let statusProbed = false
// `extraEnv` (optional) carries additive writer env keys — e.g.
// ORCH_PLANS_TOTAL, the declared plan-card total — as env, never argv, so the
// argv contract above stays four fixed strings after the command name.
const runStatus = async (plan, stage, state, msg, extraEnv) => {
  try {
    const cp = await import('node:child_process')
    if (!statusProbed) { statusProbed = true; log('status emitter ready (node:child_process)') }
    const argv = [String(plan), String(stage), String(state), String(msg)]
    const opts = { env: { ...process.env, ORCH_STATE_DIR: TASK, ...(extraEnv || {}) }, encoding: 'utf8' }
    let r = cp.spawnSync(STATUS, argv, opts)
    // ENOENT means this runtime's PATH does not carry the plugin's bin/ — take
    // the absolute wrapper instead, ONCE, and through `bash` so a copy that lost
    // its exec bit in a zip round trip still writes its event.
    if (r.error && r.error.code === 'ENOENT') {
      r = cp.spawnSync('bash', [STATUS_FALLBACK, ...argv], opts)
    }
    if (r.error) throw r.error
    const warn = (r.stderr || '').trim()
    if (warn) log(`${STATUS} warned on ${plan}/${stage}/${state}: ${warn.split('\n')[0]}`)
  } catch (e) {
    log(`status event ${plan}/${stage}/${state} not written: ${e}`)
  }
}

// Publish this script's caps into orch-config.json (R-09) so the watcher narrates
// the SAME numbers the loops actually enforce instead of its built-in defaults.
// The watcher re-reads this file while running (it starts BEFORE this script
// does), so writing it here is not too late.
// `strategy` is descriptive only. It must NEVER be published as the literal
// `serial`: that exact value is the legacy opt-in that re-enables the watcher's
// RETIRED sequenced plan-close heuristic (GD-10), which is what fabricated
// `plan failed "loop exited -> ..."` badges — a new run must not resurrect it, so
// the sequential case publishes `sequential`. What actually prevents the
// fabricated badge is the watcher's `close_state_for()` predicate plus the
// script-emitted terminal `plan done` events below (R-58), not this key.
// Merge, never overwrite: the driver may have written wf_dir/port here already.
const publishConfig = async () => {
  try {
    const fs = await import('node:fs')
    const path = TASK + '/orch-config.json'
    fs.mkdirSync(TASK, { recursive: true })
    let cfg = {}
    try { cfg = JSON.parse(fs.readFileSync(path, 'utf8')) } catch (e) { cfg = {} }
    fs.writeFileSync(path, JSON.stringify({
      ...cfg,
      max_plan_attempts: MAX_ATTEMPTS,
      max_finalgate_attempts: FINALGATE_ATTEMPTS,
      strategy: PARALLEL_MODE ? 'parallel' : 'sequential',
    }, null, 2) + '\n')
  } catch (e) {
    log(`could not publish orch-config.json: ${e}`)
  }
}

// R-40 run-close protocol: when the run ends, close the Orchestrator badge with
// the run's REAL state — `done` only when everything is green, `failed` on every
// throw path — because monitor_server's home grid treats the reserved
// `orchestrator` card as authoritative and a hardcoded `done` would paint a
// thrown run green. This event is ALSO what authorizes the watcher to stop:
// decision_watcher.py exits only on a `w:"agent"` `orchestrator complete
// done|failed` line appended after it started (its own inferred close never stops
// it, because a harness stall is indistinguishable from a finished run). So this
// call is the mechanism; the pid signal below is only a fast path.
//
// Daemon shutdown: kill by RECORDED PID only, and only after VERIFYING that the
// pid really is a decision_watcher — a stale pid file is the same wrong-target
// hazard as a name-matched kill (pids get reused, and other tasks' watchers are
// live processes; GD-12's invariant, GD-1's gate). The launch side must record
// the pid — `touch-watcher & echo $! > "$TASK/watcher.pid"`, the form
// monitoring.md's run block documents; without that line this block is a
// no-op and the watcher's own self-exit does the work. `decision_watcher` is
// what the /proc check below looks for because that is the PROGRAM the
// touch-watcher wrapper execs — the wrapper name never reaches the argv it
// verifies. The dashboard server is deliberately NOT touched: ONE server
// serves ALL tasks, so a per-task epilogue SIGTERMing it would take the
// dashboard down for every other live run.
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

// The one legitimate response to agentR giving up: close the run's badge as
// failed WITH the infrastructure cause, then rethrow so the workflow visibly
// stops. Never convert this into a loop verdict — nothing was rejected, and
// every unspent attempt is still there for the resume.
const infraStop = async (e) => {
  await closeRun('failed',
    `infrastructure stop: ${e && e.message ? e.message : e}`)
  throw e
}

const IMPL_SCHEMA = {
  type: 'object', required: ['done', 'files_changed', 'summary'],
  properties: { done: { type: 'boolean' },
                files_changed: { type: 'array', items: { type: 'string' } },
                summary: { type: 'string' } },
}
// Gates return the verdict + a SHORT summary only; full findings go to the file.
const GATE_SCHEMA = {
  type: 'object', required: ['passed', 'summary', 'findings_file'],
  properties: { passed: { type: 'boolean' }, summary: { type: 'string' },
                findings_file: { type: 'string' } },
}
// The critique also CLASSIFIES a rejection, because the loop-failure policy
// branches on it at the final attempt:
//   depth 'in-scope'       -> the red loop stays failed, the next loop starts,
//                             and the user is asked at run end about one more
//                             attempt (args.extra_attempts).
//   depth 'needs-own-flow' -> too deep for another attempt: the run does NOT
//                             stop; the close-out routes this sub-plan to its
//                             own execute-research -> implement-plan pass.
//   critical_defect true   -> the defect defines the next steps and needs the
//                             user: a serial run stops HERE (before the next
//                             loop) and the driver notifies the user.
const CRIT_SCHEMA = {
  type: 'object', required: ['approved', 'summary', 'findings_file', 'depth', 'critical_defect'],
  properties: { approved: { type: 'boolean' }, summary: { type: 'string' },
                findings_file: { type: 'string' },
                depth: { type: 'string', enum: ['in-scope', 'needs-own-flow'] },
                critical_defect: { type: 'boolean' },
                next_steps: { type: 'string' } },
}

const CONTEXT = `
Project: ${PROJECT_DIR}. TASK_SPECIFIC_CONTEXT (goal, constraints, out-of-scope areas).
Working tree may hold unrelated in-flight changes — never revert/commit/stash;
touch only files owned by THIS sub-plan.
`

// Attempt N>1 hands the implementer FILE PATHS, not inlined findings: the fresh
// agent starts with clean context and reads the durable source of truth itself.
// `notes` is the one inline exception — a refusing implementer (done=false)
// leaves no findings file, and this runtime has no filesystem to write one, so
// its reason rides along as prompt text instead of dying with the agent.
const implPrompt = (sp, attempt, findingsFiles, notes) => `
[monitor] plan=${sp.id} stage=implement role=impl attempt=${attempt}
You are the IMPLEMENTER for sub-plan ${sp.id} (${sp.title}), a fresh subagent —
everything you need is in this prompt and on disk.
FIRST run: ${statusCmd(sp.id, 'implement', 'running', `attempt ${attempt}: implementing`)}
${CONTEXT}
Sub-plan ownership — you may ONLY modify these files (plus their tests):
${sp.files.map(f => '- ' + f).join('\n')}

READ FIRST, in order:
1. ${SUBPLANS_FILE} — your section "${sp.id}": the plan items / finding ids this
   sub-plan owns and the shared decisions it must honor.
2. ${PLAN_FILE} — the global decisions section AND every item matching your
   finding ids (${sp.finding_ids.join(', ')}). Implement EVERY one exactly per
   the decided approach; global decisions bind you even where your file is only
   one half of a cross-file item.
3. The research findings files referenced by those ids (paths in the plan).
${(findingsFiles.length || (notes && notes.length)) ? `4. A previous attempt failed its gates.${findingsFiles.length ? ` READ these gate/critique findings files and
address EVERY item still applicable to the current tree (verify against the tree
— earlier items may already be fixed):
${findingsFiles.map(f => '- ' + f).join('\n')}` : ''}${notes && notes.length ? `
Prior-attempt notes (inline — a refusing implementer leaves no findings file):
${notes.map(n => '- ' + n).join('\n')}` : ''}` : ''}

Then implement the items matching existing repo style, and write/extend tests.
Sanity-check ONLY your own work (run just the tests you touched; syntax-check the
files you changed).
LAST run: ${statusCmd(sp.id, 'implement', 'done', `attempt ${attempt}: <one-line summary>`)}
Return structured output only: done, files_changed, summary.
`

const gateFindingsFile = (sp, attempt) => `${FINDINGS}/${sp.id}-test-attempt-${attempt}.md`
const critFindingsFile = (sp, attempt) => `${FINDINGS}/${sp.id}-critique-attempt-${attempt}.md`

const gatePrompt = (sp, attempt, impl, file) => `
[monitor] plan=${sp.id} stage=test role=test attempt=${attempt}
You are a READ-ONLY TEST GATE for sub-plan ${sp.id}: never edit source or tests;
only run and inspect. Findings files are task state — writing yours is REQUIRED.
FIRST run: ${statusCmd(sp.id, 'test', 'running', `attempt ${attempt}: running suites`)}
${CONTEXT}
Implementer changed: ${JSON.stringify(impl.files_changed)}.
1. Targeted suites for ${sp.id} (must be 100% green): TARGETED_TEST_COMMAND
2. Full suite regression gate: FULL_SUITE_COMMAND (known baseline: BASELINE_NOTES —
   those do not fail the gate; any OTHER failure is NEW and fails it).
3. Apply <test_hints> if given (e.g. build installer, replace sandbox install, run e2e).
4. Verify against ${SUBPLANS_FILE} section "${sp.id}" and the matching items in
   ${PLAN_FILE}: every owned item present in the tree; tests assert the intended
   behavior (not tautologies); no edits outside the sub-plan's owned files
   (git status against the ownership list).
5. Write FULL findings to ${file} (mkdir -p ${FINDINGS} first): every failure with
   test id, traceback essence, why it is attributable to the change, and a concrete
   fix suggestion. On pass, write the green evidence summary there too.
LAST run: ${statusCmd(sp.id, 'test', 'done', `attempt ${attempt}: <passed/failed + counts>`)} (state failed if gate fails)
Return structured output only: passed, summary (short), findings_file (${file}).
`

const critPrompt = (sp, attempt, impl, gate, file) => `
[monitor] plan=${sp.id} stage=critique role=critique attempt=${attempt}
You are a READ-ONLY ADVERSARIAL REVIEWER for sub-plan ${sp.id}: never edit source.
Try hard to REJECT. Findings files are task state — writing yours is REQUIRED.
FIRST run: ${statusCmd(sp.id, 'critique', 'running', `attempt ${attempt}: adversarial review`)}
${CONTEXT}
Review ONLY the diff of: ${JSON.stringify(impl.files_changed)} (git diff -- <files>),
against ${SUBPLANS_FILE} section "${sp.id}", the matching items in ${PLAN_FILE},
and the research findings they cite.
Test gate said: ${JSON.stringify(gate.summary)} (details: ${gate.findings_file}).
Attack checklist: REVIEW_CHECKLIST (correctness, defaults, security, regressions to
shared decisions, tautological/fragile tests, needless rewrites beyond scope, style).
Write your FULL review to ${file} (mkdir -p ${FINDINGS} first): each finding with
severity (blocker/major/minor/nit), file:line, and a concrete fix suggestion.
approved=true ONLY with zero blocker/major findings.
Classify your verdict too (structured fields):
- depth: 'in-scope' if everything you found is fixable by ONE more gated attempt
  on these files; 'needs-own-flow' if the right fix demands its own
  research->plan->implement pass (architectural rework, redesign crossing
  sub-plan boundaries, missing upstream research). An approved review is 'in-scope'.
- critical_defect: true ONLY for a defect so fundamental that implementing the
  REMAINING sub-plans before a human decides would waste or corrupt work; then
  next_steps MUST name the decision the user has to make (one short sentence).
LAST run: ${statusCmd(sp.id, 'critique', 'done', `attempt ${attempt}: <approved/rejected + finding count>`)} (state failed if rejected)
Return structured output only: approved, summary (short), findings_file (${file}), depth, critical_defect, next_steps.
`

// ---- Per-cycle visual report: ONE artifact after EVERY impl->test->critique cycle ----
// Rendered DETERMINISTICALLY by the cycle reporter daemon (`touch-cycle-reporter`,
// launched by the driver alongside the watcher — see SKILL.md; it carries no
// placeholders, so it is run by name and never copied). The
// workflow runtime has NO filesystem or Node API access (import() throws in
// scripts; the try/catch'd runStatus/closeRun helpers above are the documented
// contract but silently no-op at runtime), so the script CANNOT write pages
// itself, and an LLM scribe would be non-deterministic. The daemon tails the
// run journal, correlates every structured result to (plan, stage, attempt)
// via the [monitor] markers — zero LLM cooperation — renders
// report/cycles/<sp>-cycle-<N>.html + index.html with the WHY (verdict
// summaries + findings files embedded as evidence, on failure AND success),
// and emits the loop-terminal `plan done|failed` status event when it sees a
// loop close (a REAL verdict at the published cap — not the retired GD-10
// phase-advance inference). The script carries only the CLASSIFICATION
// contract, which the daemon and the driver both read:
//   retryable      -> stays failed; next loop starts; user asked at run end.
//   needs-own-flow -> never stops the run; gets its own research pass later.
//   critical-stop  -> a serial run stops before the next loop starts.
const classify = (success, crit) => success ? 'green'
  : (crit && crit.critical_defect) ? 'critical-stop'
    : (crit && crit.depth === 'needs-own-flow') ? 'needs-own-flow' : 'retryable'

// One full impl->test->critique loop for a single sub-plan. Fresh implementer
// every attempt; the handoff is ONLY through findings files + the current tree.
// ATTEMPTS ARE VERDICTS: `attempt` advances only when a spawned agent RETURNED
// — agentR has already absorbed infrastructure deaths (or thrown), so a null
// can never reach this loop, spend a cap slot, or be fabricated into a "gate
// died" red. A `done:false` REFUSAL does spend its attempt — the agent judged
// the task and that judgment is a result — but the judgment must survive it:
// the reason rides to the next attempt via openNotes (the refusal that went
// unread once cost a run its endgame — the next fresh implementer re-derived
// the blockage from scratch and resolved it the wrong way).
const runLoop = async (sp) => {
  const cap = MAX_ATTEMPTS + (EXTRA_ATTEMPTS[sp.id] || 0)
  let attempt = 0
  let openFindings = []   // findings-file paths from every failed gate so far
  let openNotes = []      // inline refusal reasons (no file to point at)
  let impl = null, gate = null, crit = null
  let success = false
  while (!success && attempt < cap) {
    attempt++
    log(`${sp.id} attempt ${attempt}/${cap}${openFindings.length ? ` (open findings: ${openFindings.length})` : ''}`)

    impl = await agentR(implPrompt(sp, attempt, openFindings, openNotes), {
      model: 'opus', effort: attempt >= 3 ? 'xhigh' : 'high',
      label: `${sp.id}:impl:${attempt}`, phase: 'Implement', schema: IMPL_SCHEMA,
    })
    if (!impl.done) {
      openNotes.push(`attempt ${attempt} implementer returned done=false: ` +
        String(impl.summary || '(no reason given)').slice(0, 600))
      continue
    }

    const gateFile = gateFindingsFile(sp, attempt)
    gate = await agentR(gatePrompt(sp, attempt, impl, gateFile), {
      model: 'opus', effort: 'medium',
      label: `${sp.id}:gate:${attempt}`, phase: 'Test', schema: GATE_SCHEMA,
    })

    const critFile = critFindingsFile(sp, attempt)
    crit = await agentR(critPrompt(sp, attempt, impl, gate, critFile), {
      model: 'opus', effort: 'high',
      label: `${sp.id}:critique:${attempt}`, phase: 'Critique', schema: CRIT_SCHEMA,
    })

    success = gate.passed && crit.approved
    if (!success) {
      // New findings landed: prior-attempt agents are now stale. Queue the files;
      // the NEXT fresh implementer reads them.
      if (!gate.passed && gate.findings_file) openFindings.push(gate.findings_file)
      if (!crit.approved && crit.findings_file) openFindings.push(crit.findings_file)
      openFindings = [...new Set(openFindings)]
    }
  }
  const classification = classify(success, crit)
  // Terminal plan event for this sub-plan's loop, script-emitted at the loop
  // exit (R-09) — the one place that deterministically knows whether the loop
  // closed green. `failed` here is a REAL verdict: the gates rejected every
  // attempt, unlike the retired heuristic that inferred failure from a phase
  // advance (R-58).
  if (success) {
    await runStatus(sp.id, 'plan', 'done', `green on attempt ${attempt}/${cap}`)
  } else {
    await runStatus(sp.id, 'plan', 'failed', `attempts exhausted ${attempt}/${cap} (${classification})`)
  }
  return { id: sp.id, success, attempts: attempt, classification,
           next_steps: (!success && crit && crit.next_steps) || null,
           files_changed: (impl && impl.files_changed) || [],
           gate, critique: crit, open_findings: success ? [] : openFindings }
}

// ---- Divide: the Fable divider derives the sub-plans from the plan ----
const DIVIDE_SCHEMA = {
  type: 'object', required: ['subplans', 'subplans_file', 'summary'],
  properties: {
    subplans: {
      type: 'array',
      items: {
        type: 'object', required: ['id', 'title', 'files', 'finding_ids'],
        properties: {
          id: { type: 'string' }, title: { type: 'string' },
          files: { type: 'array', items: { type: 'string' } },
          finding_ids: { type: 'array', items: { type: 'string' } },
          last: { type: 'boolean' },
        },
      },
    },
    subplans_file: { type: 'string' },
    summary: { type: 'string' },
  },
}

const dividePrompt = () => `
[monitor] plan=divide stage=partition role=synth attempt=1
You are the DIVIDER — the divide-and-conquer analyst for the plan below.
READ-ONLY for source; you write exactly one partition file in task state.
FIRST run: ${statusCmd('divide', 'partition', 'running', 'partitioning plan into sub-plans')}
${CONTEXT}
READ ${PLAN_FILE} fully: the global decisions section and every ordered
implementation item.
Partition the items into the OPTIMAL set of clean, concrete, ISOLATED
feature-sub-plans:
- FILE OWNERSHIP is the isolation rule: every touched file belongs to exactly
  one sub-plan, so sub-plans can be implemented independently (and in parallel).
- A cross-file item is split into per-file halves, each half restating the
  shared decision so the halves cannot drift.
- Optimize the cut: cohesive features, minimal cross-sub-plan coupling, no
  sub-plan too broad to implement and review in one gated loop.
- If the plan ends with an endgame item that must run STRICTLY LAST over the
  MERGED change-set (the commit, a release dry-run, an aggregate acceptance),
  make it its own sub-plan marked \`last: true\` (at most one). At run time it
  is gated on every other loop closing green — a strictly-last loop must never
  absorb a dead sibling's work — so keep its \`files\` list empty or minimal.
Write the partition to ${SUBPLANS_FILE} (mkdir -p its dir first): one section
per sub-plan — id (sp-<slug>), title, owned files, the ordered plan items /
finding ids it implements, and the shared decisions it must honor.
LAST run: ${statusCmd('divide', 'partition', 'done', 'N sub-plans')} (replace N)
Return structured output only: subplans (id/title/files/finding_ids each), subplans_file, summary.
`

phase('Divide')
await publishConfig()
let divide = null
try {
  divide = await agentR(dividePrompt(), {
    model: 'fable', effort: 'high',
    label: 'divide', phase: 'Divide', schema: DIVIDE_SCHEMA,
  })
} catch (e) { await infraStop(e) }
if (!divide || !Array.isArray(divide.subplans) || !divide.subplans.length) {
  await runStatus('divide', 'plan', 'failed', 'divider produced no sub-plans')
  await closeRun('failed', 'run failed: no partition to implement')
  throw new Error('divider produced no sub-plans — cannot implement')
}
// Deterministic isolation guard: one file, exactly one owner.
const owner = {}
for (const sp of divide.subplans) for (const f of sp.files) {
  if (owner[f]) {
    await runStatus('divide', 'plan', 'failed', `partition not isolated: ${f} has two owners`)
    await closeRun('failed', 'run failed: partition not isolated')
    throw new Error(`partition not isolated: ${f} owned by ${owner[f]} and ${sp.id}`)
  }
  owner[f] = sp.id
}
const SUBPLANS = divide.subplans
// The Divide phase is a single agent with no gate verdict — its card closes here
// (R-09), not by inference from the first sub-plan spawning (R-58). The close
// also DECLARES this run's full plan-card count — divide + N sub-plans +
// finalgate — so dashboards can show progress over all plans, including the
// ones not started yet, instead of only the cards already in the stream.
// Readers fold plans_total as a monotonic max, so a resume re-declaring the
// same number is idempotent.
await runStatus('divide', 'plan', 'done', `${SUBPLANS.length} sub-plans`,
  { ORCH_PLANS_TOTAL: String(SUBPLANS.length + 2) })

// ---- Drive the sub-plans: SERIAL by default, PARALLEL only when instructed ----
log(`implementing ${SUBPLANS.length} feature-sub-plans (${PARALLEL_MODE ? 'PARALLEL' : 'SERIAL'}): ${SUBPLANS.map(s => s.id).join(', ')}`)
// Strictly-last loops (divider-marked `last: true` — e.g. the endgame that
// commits the merged change-set) run ONLY over an all-green board, serially,
// after everything else. A red or missing prerequisite records the loop as
// `blocked` WITHOUT spawning it: the one run that let an endgame start over
// red siblings watched a fresh implementer "resolve" the contradiction by
// taking over the dead loops' files and committing half-reviewed work.
const NORMAL = SUBPLANS.filter(sp => sp.last !== true)
const LAST = SUBPLANS.filter(sp => sp.last === true)
let results = []
let criticalStop = null   // the red loop whose final critique flagged critical_defect
let failed = []
let allFiles = []
let finalGate = { passed: false, summary: 'final gate not run' }
try {
  if (PARALLEL_MODE) {
    // Opt-in only, and only for disjoint file ownership. Barrier: the final gate
    // sweeps the MERGED change-set. (The critical-stop early exit is a serial-mode
    // behavior — concurrent loops cannot be stopped mid-flight cleanly.)
    results = (await parallel(NORMAL.map(sp => () => runLoop(sp)))).filter(Boolean)
    // parallel() converts a thrown runLoop (agentR giving up) into a silent
    // null — never let a loop vanish from the board without a verdict.
    if (results.length < NORMAL.length) {
      const missing = NORMAL.filter(sp => !results.find(r => r.id === sp.id)).map(sp => sp.id)
      throw new Error(`loops ${missing.join(', ')} died without a verdict (infrastructure)`)
    }
    criticalStop = results.find(r => r.classification === 'critical-stop') || null
  } else {
    // Default: one sub-plan at a time. A red loop STAYS failed and the next loop
    // starts — except a critical-stop, which ends the run before the next loop
    // so the user decides while the remaining token budget is still unspent.
    for (const sp of NORMAL) {
      const r = await runLoop(sp)
      results.push(r)
      if (!r.success) log(`${sp.id} did not close green after ${r.attempts} attempts (${r.classification})`)
      if (r.classification === 'critical-stop') { criticalStop = r; break }
    }
  }
  for (const sp of LAST) {
    if (criticalStop) break
    const notGreen = results.filter(r => !r.success).map(r => r.id)
    const notRun = NORMAL.filter(s => !results.find(r => r.id === s.id)).map(s => s.id)
    const holds = [...notGreen, ...notRun]
    if (holds.length) {
      log(`${sp.id} BLOCKED (strictly last): ${holds.join(', ')} not green — not started`)
      results.push({ id: sp.id, success: false, attempts: 0, classification: 'blocked',
                     next_steps: `close ${holds.join(', ')} green, then run ${sp.id}`,
                     files_changed: [], gate: null, critique: null, open_findings: [] })
      continue
    }
    const r = await runLoop(sp)
    results.push(r)
    if (!r.success) log(`${sp.id} did not close green after ${r.attempts} attempts (${r.classification})`)
    if (r.classification === 'critical-stop') { criticalStop = r; break }
  }

  failed = results.filter(r => !r.success)
  allFiles = [...new Set(results.flatMap(r => r.files_changed))]

// ---- Final aggregate gate over the merged change-set (read-only test role) ----
const finalGateFindings = (a) => `${FINDINGS}/finalgate-attempt-${a}.md`
const finalGatePrompt = (attempt, file) => `
[monitor] plan=finalgate stage=sweep role=test attempt=${attempt}
You are the READ-ONLY FINAL AGGREGATE GATE over the whole change-set: never edit
source or tests. Findings files are task state — writing yours is REQUIRED.
FIRST run: ${statusCmd('finalgate', 'sweep', 'running', `attempt ${attempt}: full test sweep`)}
${CONTEXT}
Every sub-plan loop closed green; merged changed files: ${JSON.stringify(allFiles)}.
1. Run the FULL suite (all sub-plans' tests together): FULL_SUITE_COMMAND.
2. Syntax-check every changed file as applicable.
3. Apply <test_hints> if given (installer / e2e sweep).
4. Scope audit: git status — no edits outside the planned files.
5. Write FULL findings to ${file}: each failure with command, output essence, fix
   suggestion, and the OWNING sub-plan id; on pass, the green evidence.
LAST run: ${statusCmd('finalgate', 'sweep', 'done', `attempt ${attempt}: <passed/failed>`)} (state failed if gate fails)
Return structured output only: passed, summary (short), findings_file (${file}).
`
// The final-gate fixer is a fresh IMPLEMENTER scoped to the whole change-set
// (role=impl) — the impl->test loop, not a standalone gate->fixer. It stays on
// opus: fable is reserved for the gate REVIEWER above.
const finalFixPrompt = (attempt, findingsFile) => `
[monitor] plan=finalgate stage=implement role=impl attempt=${attempt}
You are the FINAL-GATE FIXER, a fresh subagent. The aggregate sweep over the
merged change-set failed after all per-sub-plan loops were green — likely a
cross-file integration slip.
FIRST run: ${statusCmd('finalgate', 'implement', 'running', `attempt ${attempt}: fixing sweep failures`)}
${CONTEXT}
READ FIRST: ${findingsFile} (the sweep's findings), then ${PLAN_FILE} global
decisions. Fix every finding, editing only files within the planned change-set
(so far: ${JSON.stringify(allFiles)}). Keep every sub-plan's intended items
intact — reconcile, don't revert. Rerun the failing commands until green.
LAST run: ${statusCmd('finalgate', 'implement', 'done', `attempt ${attempt}: <one-line summary>`)}
Return structured output only: done, files_changed, summary.
`

  if (!failed.length) {
    phase('FinalGate')
    for (let fga = 1; fga <= FINALGATE_ATTEMPTS; fga++) {
      const file = finalGateFindings(fga)
      // The final gate reviewer is the one implement-side agent that runs fable.
      finalGate = await agentR(finalGatePrompt(fga, file), {
        model: 'fable', effort: 'medium',
        label: `finalgate:${fga}`, phase: 'FinalGate', schema: GATE_SCHEMA,
      })
      if (finalGate.passed) break
      if (fga < FINALGATE_ATTEMPTS) {
        const fixer = await agentR(finalFixPrompt(fga, finalGate.findings_file), {
          model: 'opus', effort: 'xhigh',
          label: `finalgate:fix:${fga}`, phase: 'FinalGate', schema: IMPL_SCHEMA,
        })
        if (!fixer.done) break
      }
    }
    // Terminal plan event for the aggregate sweep (R-09).
    if (finalGate.passed) {
      await runStatus('finalgate', 'plan', 'done', 'aggregate sweep green')
    } else {
      await runStatus('finalgate', 'plan', 'failed',
        `sweep not green after ${FINALGATE_ATTEMPTS} attempts`)
    }
  } else {
    log(`skipping final gate: ${failed.map(f => f.id).join(', ')} did not close green`)
  }
} catch (e) { await infraStop(e) }

// Close the Orchestrator badge and stop this task's daemons (R-09/R-40): the
// watcher cannot see run completion in the journal. The driver may repeat the
// `orchestrator complete done` call after the workflow returns — it is an
// idempotent backstop, and the badge is last-event-wins either way.
const allGreen = !failed.length && !!finalGate.passed
// Run disposition for the DRIVER (the assistant that launched this workflow):
//   complete         -> publish the final report; nothing to ask.
//   stopped-critical -> PushNotification the user with decision_needed and WAIT;
//                       remaining loops were deliberately not started.
//   awaiting-user    -> every loop ran and the last cycle report is written:
//                       STOP and ask the user whether each red 'retryable' loop
//                       gets another attempt (relaunch/resume with
//                       args.extra_attempts = { 'sp-<slug>': N }) or the red
//                       close is accepted; 'needs-own-flow' loops get their own
//                       execute-research -> implement-plan pass, not attempts.
const status = criticalStop ? 'stopped-critical'
  : failed.length ? 'awaiting-user' : 'complete'
await closeRun(allGreen ? 'done' : 'failed',
  criticalStop
    ? `stopped at ${criticalStop.id}: critical defect needs a user decision`
    : allGreen
      ? `all ${results.length} sub-plans green; aggregate sweep green`
      : `${failed.length} of ${results.length} sub-plans not green; sweep ${finalGate.passed ? 'green' : 'not green'}; awaiting user decision`)

return { status, subplans: results, final_gate: finalGate,
         all_green: allGreen,
         decision_needed: criticalStop
           ? (criticalStop.next_steps || 'see the final critique findings file')
           : null,
         not_started: SUBPLANS.filter(s => !results.find(r => r.id === s.id)).map(s => s.id),
         failed_loops: failed.map(f => ({ id: f.id, classification: f.classification,
                                          attempts: f.attempts, next_steps: f.next_steps,
                                          open_findings: f.open_findings })),
         files_changed: allFiles }
