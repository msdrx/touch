// Reference implement workflow — divide ONE complete plan into isolated
// feature-sub-plans (Fable divider), then implement them through gated
// impl->test->critique loops, SERIALLY by default (one sub-plan at a time),
// PARALLEL only when instructed, then a final aggregate gate over the merged
// change-set. This file is GENERIC and SPEC-DRIVEN (GD-D9/D-12): every per-run
// value arrives in `args` from a run-spec JSON, so `orch-scripts/` copies are a
// byte-for-byte `cp` of this file and the driver authors only the spec. Keep
// the protocol:
//   * the sub-plan partition comes from the Divide phase (fable) — the gated
//     loops are a pure function of its structured output
//   * the divider also writes ONE PLAN SLICE per sub-plan, and the downstream
//     agents read the slice, never the whole plan (D-23)
//   * gates are READ-ONLY for source, but MUST write findings files to the task folder
//   * implementer attempts are brand-new agents that READ findings files from disk
//   * once new findings land, prior-attempt agents are stale — never reuse or resume them
//     (serial: they have already exited; parallel: drop/stop them before the successor)
//
// THIS SCRIPT EMITS NO EVENTS, AND THAT IS THE DESIGN (GD-D5, D-10). The
// workflow runtime has no Node API — every `import('node:…')` throws — so the
// `runStatus`/`closeRun`/`publishConfig` helpers this template used to carry
// silently no-opped in every real run; one run failed on nothing else. They are
// deleted rather than kept as decoration. The DETERMINISTIC emitters are:
//   decision_watcher.py  spawn/result/verdict/token events, derived from the
//                        run journal and the `[monitor]` marker below
//   cycle_reporter.py    the loop-terminal `plan done|failed` events (a REAL
//                        verdict read off the journal at the published cap, not
//                        the retired GD-10 phase-advance inference — R-58) and
//                        the per-cycle report pages
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
  name: 'touch-implement',
  description: 'Implement one complete plan as isolated feature-sub-plans via gated loops',
  phases: [
    { title: 'Divide', detail: 'fable divider: one plan -> isolated feature-sub-plans', model: 'fable' },
    { title: 'Implement', model: 'opus' },
    { title: 'Test', model: 'opus' },
    { title: 'Critique', model: 'opus' },
    { title: 'FinalGate', model: 'fable' },
  ],
}

// ---- the run spec (GD-D9/D-12) --------------------------------------------
// `args` is what the launcher hands this script. `touch-run start` builds it by
// merging the tracked per-project constants (`.touch/run.json` — the test
// commands, the known baseline, the review checklist) under the run-spec file,
// so a per-project value is configured ONCE and a per-run value overrides it.
// `typeof args === 'undefined'` is checked FIRST: a bare identifier reference on
// a runtime that never injected `args` is a ReferenceError, not a falsy value.
//
// Recognized keys, all optional (the fallbacks are what a hand-launched copy
// sees, never something a preflight greps for — `touch-run verify` preflights
// the SPEC, because this file is copied verbatim and its defaults would
// otherwise read as leaked placeholders):
//   project_dir  plan_file       parallel         max_attempts
//   task         context         extra_attempts   finalgate_attempts
//   plugin_root  test_hints      net_retries
//   targeted_test_command  full_suite_command  baseline_notes  review_checklist
// Sub-plans are DERIVED by the Divide phase below and are never handed in.
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
const SUBPLANS_FILE = TASK + '/plan/' + TASK_NAME + '-subplans.md'

// D-23: one PLAN SLICE per sub-plan — the shared global-decisions header plus
// only that sub-plan's items. The divider writes them; every downstream prompt
// names the slice instead of the full plan (measured: 75.8 K tokens of plan
// read 11 times by 7 agents in one run). The comprehension win doubles as an
// isolation win — an implementer handed only its own slice cannot drive-by-fix
// another sub-plan's file because it never read that sub-plan's items.
const sliceFile = (sp) => sp.slice_file || `${TASK}/plan/${TASK_NAME}-subplan-${sp.id}.md`

// `??`, never `||`, on every numeric spec key: `0` is a meaningful value in all
// three (`finalgate_attempts: 0` = skip the aggregate sweep) and `||` would
// silently substitute the default for it. `MIN_REPORTS` in the research
// template is the same idiom.
const MAX_ATTEMPTS = ARGS.max_attempts ?? 4
// Final aggregate gate: gate -> fixer -> re-gate.
const FINALGATE_ATTEMPTS = ARGS.finalgate_attempts ?? 2
const PARALLEL_MODE = ARGS.parallel === true   // never shadow the parallel() global
// User-granted attempt extensions after an 'awaiting-user' close: relaunch /
// resume with args.extra_attempts = { 'sp-<slug>': N } to raise ONLY that
// loop's cap to MAX_ATTEMPTS + N. This is how "add another attempt to that
// loop" is granted — never by editing MAX_ATTEMPTS for everyone.
const EXTRA_ATTEMPTS = ARGS.extra_attempts || {}

// Per-project constants (SKILLS-7): configured once in the tracked
// `.touch/run.json` and merged into the spec by `touch-run start`, so no run
// ever hand-edits a command into this script. The defaults are honest
// statements of "not configured", never ALL-CAPS placeholders.
const TARGETED_TEST_COMMAND = ARGS.targeted_test_command ||
  '(none configured — run exactly the suites this sub-plan slice names)'
const FULL_SUITE_COMMAND = ARGS.full_suite_command ||
  '(none configured — run the project full test suite)'
const BASELINE_NOTES = ARGS.baseline_notes ||
  '(no recorded baseline — treat every failure as new)'
const REVIEW_CHECKLIST = ARGS.review_checklist ||
  'correctness, defaults, security, regressions to shared decisions, ' +
  'tautological/fragile tests, needless rewrites beyond scope, style'
const TEST_HINTS = ARGS.test_hints || ''

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

// Per-requirement coverage: ONE row of the report's requirement -> implemented
// -> delta diagram, and the one thing that diagram cannot derive from anything
// already recorded. `files_changed` says which files moved and `summary` says
// it in prose; neither answers "was plan item R-12 built as decided", which is
// the question a reader of the report actually has. `id` must be one of the
// sub-plan's finding_ids — an id outside that set is not rejected, it renders
// as `extra` (implementation beyond the requirement), which is a difference
// worth seeing rather than an error worth losing.
const COVERAGE_ITEM = {
  type: 'object', required: ['id', 'status', 'note'],
  properties: { id: { type: 'string' },
                status: { type: 'string', enum: ['done', 'partial', 'skipped'] },
                note: { type: 'string' } },
}
// The other half of the same diagram, from the two READ-ONLY verdicts: where
// the TREE differs from the slice. Kept to (id, kind, one line) on purpose —
// the argument and the evidence stay in the findings file; this is the label.
const DEVIATION = {
  type: 'object', required: ['id', 'kind', 'what'],
  properties: { id: { type: 'string' },
                kind: { type: 'string', enum: ['missing', 'differs', 'extra'] },
                what: { type: 'string' } },
}
const IMPL_SCHEMA = {
  type: 'object', required: ['done', 'files_changed', 'summary', 'items'],
  properties: { done: { type: 'boolean' },
                files_changed: { type: 'array', items: { type: 'string' } },
                summary: { type: 'string' },
                items: { type: 'array', items: COVERAGE_ITEM } },
}
// Gates return the verdict + a SHORT summary only; full findings go to the file.
const GATE_SCHEMA = {
  type: 'object', required: ['passed', 'summary', 'findings_file', 'deviations'],
  properties: { passed: { type: 'boolean' }, summary: { type: 'string' },
                findings_file: { type: 'string' },
                deviations: { type: 'array', items: DEVIATION } },
}
// The critique also CLASSIFIES a rejection, because the loop-failure policy
// branches on it at the final attempt:
//   depth 'in-scope'       -> the red loop stays failed, the next loop starts,
//                             and the user is asked at run end about one more
//                             attempt (args.extra_attempts).
//   depth 'needs-own-flow' -> too deep for another attempt: the run does NOT
//                             stop; the close-out routes this sub-plan to its
//                             own research -> implement pass.
//   critical_defect true   -> the defect defines the next steps and needs the
//                             user: a serial run stops HERE (before the next
//                             loop) and the driver notifies the user.
const CRIT_SCHEMA = {
  type: 'object', required: ['approved', 'summary', 'findings_file', 'depth',
                             'critical_defect', 'deviations'],
  properties: { approved: { type: 'boolean' }, summary: { type: 'string' },
                findings_file: { type: 'string' },
                depth: { type: 'string', enum: ['in-scope', 'needs-own-flow'] },
                critical_defect: { type: 'boolean' },
                next_steps: { type: 'string' },
                deviations: { type: 'array', items: DEVIATION } },
}

// D-24 / ECONOMICS-6: one line, not a policy. Measured over this project's own
// recorded sessions: Bash carried 50.6% of all tool-result VOLUME, on 16,786
// Bash calls against 3,005 Read calls (5.6:1 by count). Most of that volume is
// source read through `cat`/`sed`/`head`, which bypasses Read's offset/limit
// windowing and the harness's own truncation accounting. One sentence in the
// shared preamble is the whole intervention — anything more polices the
// irreducible core (GD-D7).
const READ_DISCIPLINE =
  'Read files with the Read tool (offset/limit on long files) rather than cat/sed/head through Bash.'

const CONTEXT = `
Project: ${PROJECT_DIR}. ${ARGS.context || 'TASK_SPECIFIC_CONTEXT (goal, constraints, out-of-scope areas).'}
${READ_DISCIPLINE}
Working tree may hold unrelated in-flight changes — never revert/commit/stash;
touch only files owned by THIS sub-plan.
`

// The `[monitor]` marker is line 1 of every prompt below and is FENCED
// (GD-D1a): decision_watcher.py and aggregator/agents.py derive
// plan/stage/role/attempt from it with zero LLM cooperation, so trimming,
// renaming or moving it turns every derived event for that agent into an
// unnamed bucket. It is the one line in this file a token-reduction pass may
// not touch.
//
// Attempt N>1 hands the implementer FILE PATHS, not inlined findings: the fresh
// agent starts with clean context and reads the durable source of truth itself.
// `notes` is the one inline exception — a refusing implementer (done=false)
// leaves no findings file, and this runtime has no filesystem to write one, so
// its reason rides along as prompt text instead of dying with the agent.
const implPrompt = (sp, attempt, findingsFiles, notes) => `
[monitor] plan=${sp.id} stage=implement role=impl attempt=${attempt}
You are the IMPLEMENTER for sub-plan ${sp.id} (${sp.title}), a fresh subagent —
everything you need is in this prompt and on disk.
${CONTEXT}
Sub-plan ownership — you may ONLY modify these files (plus their tests):
${sp.files.map(f => '- ' + f).join('\n')}

READ FIRST: ${sliceFile(sp)} — your plan slice. It carries the shared global
decisions verbatim, then the ordered items this sub-plan owns
(${sp.finding_ids.join(', ')}), the files it owns, and the halves owned
elsewhere. Implement EVERY item exactly per the decided approach; the global
decisions bind you even where your file is only one half of a cross-file item.
If that file does not exist, stop and return done=false naming it — never guess
your scope from the full plan. Then read the research findings files the slice
cites.
${(findingsFiles.length || (notes && notes.length)) ? `A previous attempt failed its gates.${findingsFiles.length ? ` READ these gate/critique findings files and
address EVERY item still applicable to the current tree (verify against the tree
— earlier items may already be fixed):
${findingsFiles.map(f => '- ' + f).join('\n')}` : ''}${notes && notes.length ? `
Prior-attempt notes (inline — a refusing implementer leaves no findings file):
${notes.map(n => '- ' + n).join('\n')}` : ''}` : ''}

Then implement the items matching existing repo style, and write/extend tests.
Sanity-check ONLY your own work (run just the tests you touched; syntax-check the
files you changed).
Return \`items\`: ONE entry per owned plan item (${sp.finding_ids.join(', ')}) —
\`id\` exactly as written there, \`status\` done|partial|skipped, and \`note\` in
≤120 chars: what you built for it, or for partial/skipped what is missing and
why. This is a report row, not a report: the prose stays in \`summary\`. Omitting
an item does not hide it — it renders as UNREPORTED, which reads worse than a
truthful \`skipped\`.
`

const gateFindingsFile = (sp, attempt) => `${FINDINGS}/${sp.id}-test-attempt-${attempt}.md`
const critFindingsFile = (sp, attempt) => `${FINDINGS}/${sp.id}-critique-attempt-${attempt}.md`

const gatePrompt = (sp, attempt, impl, file) => `
[monitor] plan=${sp.id} stage=test role=test attempt=${attempt}
You are a READ-ONLY TEST GATE for sub-plan ${sp.id}: never edit source or tests;
only run and inspect. Findings files are task state — writing yours is REQUIRED.
${CONTEXT}
Implementer changed: ${JSON.stringify(impl.files_changed)}.
1. Targeted suites for ${sp.id} (must be 100% green): ${TARGETED_TEST_COMMAND}
2. Full suite regression gate: ${FULL_SUITE_COMMAND} (known baseline: ${BASELINE_NOTES} —
   those do not fail the gate; any OTHER failure is NEW and fails it).
3. Verify against ${sliceFile(sp)}, your sub-plan's slice: every owned item
   present in the tree; tests assert the intended behavior (not tautologies); no
   edits outside the sub-plan's owned files (git status against the ownership list).
4. Write FULL findings to ${file} (mkdir -p ${FINDINGS} first): every failure with
   test id, traceback essence, why it is attributable to the change, and a concrete
   fix suggestion. On pass, write the green evidence summary there too.
5. Return \`deviations\`: one entry per place the TREE differs from the slice —
   \`id\` the plan item id it belongs to (or the file path for an edit no item
   asked for), \`kind\` missing|differs|extra, \`what\` in ≤120 chars. An empty
   array is the right answer when the tree matches the slice; it is a diagram
   label, so the reasoning stays in ${file}.
${TEST_HINTS ? `Test hints for this run (apply them): ${TEST_HINTS}\n` : ''}`

const critPrompt = (sp, attempt, impl, gate, file) => `
[monitor] plan=${sp.id} stage=critique role=critique attempt=${attempt}
You are a READ-ONLY ADVERSARIAL REVIEWER for sub-plan ${sp.id}: never edit source.
Try hard to REJECT. Findings files are task state — writing yours is REQUIRED.
${CONTEXT}
Review ONLY the diff of: ${JSON.stringify(impl.files_changed)} (git diff -- <files>),
against ${sliceFile(sp)} — your sub-plan's slice, carrying the shared global
decisions and this sub-plan's items — and the research findings they cite.
Test gate said: ${JSON.stringify(gate.summary)} (details: ${gate.findings_file}).
Attack checklist: ${REVIEW_CHECKLIST}.
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
- deviations: one entry per place the DIFF differs from the slice — \`id\` the
  plan item id (or the file for an unasked-for change), \`kind\`
  missing|differs|extra, \`what\` in ≤120 chars. Report the gap even when you
  approve: an approved review with a real \`differs\` is exactly what the
  report's delta column exists to show. Empty array when the diff matches.
`

// ---- Per-cycle visual report: ONE artifact after EVERY impl->test->critique cycle ----
// Rendered DETERMINISTICALLY by the cycle reporter daemon (`touch-cycle-reporter`,
// started by `touch-run bind` alongside the watcher; it carries no placeholders,
// so it is run by name and never copied). The workflow runtime has NO filesystem
// or Node API access, so the script CANNOT write pages itself, and an LLM scribe
// would be non-deterministic. The daemon tails the run journal, correlates every
// structured result to (plan, stage, attempt) via the [monitor] markers — zero
// LLM cooperation — renders report/cycles/<sp>-cycle-<N>.html + index.html
// leading with the requirement -> implemented -> Δ diagram (the divider's
// finding_ids against this cycle's `items` and `deviations`) and then the WHY
// (verdict summaries + findings files embedded as evidence, on failure AND
// success), and emits the loop-terminal `plan done|failed` status event when
// it sees a loop close (a REAL verdict at the published cap — not the retired
// GD-10 phase-advance inference). The script carries only the CLASSIFICATION
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
  // The loop-terminal `plan done|failed` event is cycle_reporter.py's, read off
  // the journal at this same loop exit (GD-D5/D-14). `failed` there is a REAL
  // verdict — the gates rejected every attempt — unlike the retired heuristic
  // that inferred failure from a phase advance (R-58).
  log(`${sp.id} loop closed ${success ? 'green' : 'red'} on attempt ${attempt}/${cap} (${classification})`)
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
        type: 'object', required: ['id', 'title', 'files', 'finding_ids', 'slice_file'],
        properties: {
          id: { type: 'string' }, title: { type: 'string' },
          files: { type: 'array', items: { type: 'string' } },
          finding_ids: { type: 'array', items: { type: 'string' } },
          slice_file: { type: 'string' },
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
READ-ONLY for source; you write the partition file and one slice per sub-plan.
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
Then write ONE PLAN SLICE per sub-plan to
${TASK}/plan/${TASK_NAME}-subplan-<id>.md, and return its path as that
sub-plan's \`slice_file\`. A slice is: the plan's global-decisions section
VERBATIM (identical in every slice), then ONLY that sub-plan's ordered items
copied in full, then its owned files and the halves owned elsewhere. The
implementer, test gate and critique agents read the SLICE and are not handed
the full plan — anything they need must be in it, and nothing another sub-plan
owns should be.
`

// REFUSE an incomplete spec before spending anything (D-12). This is defense in
// depth UNDER `touch-run verify`, not a duplicate of it: `verify` preflights the
// spec FILE, this checks what the runtime actually INJECTED — and the two fail
// in different places, because a launcher can hand a verified spec to the wrong
// script or hand this one nothing at all. Narrating the problem and continuing
// costs more here than anywhere else in Touch: the divider would partition
// whatever it found at `/ABS/PATH/TO/PROJECT/.touch/local-orchestrators/task/`
// and every gated loop after it would run against invented sub-plans. The
// literals above stay as documentation of the SHAPE a spec must fill (SKILLS-14
// leaves `plugin_root` as shipped, so it is deliberately not required here).
if (!ARGS.project_dir || !ARGS.task) {
  throw new Error(
    'run-spec incomplete: project_dir and task are required — launch through ' +
    '`touch-run start --spec <file>`; this script is copied byte-for-byte and ' +
    'its /ABS/PATH defaults are placeholders, never a working configuration')
}

phase('Divide')
log(`touch implement: task=${TASK_NAME} project=${PROJECT_DIR} plugin=${PLUGIN_ROOT}`)
const divide = await agentR(dividePrompt(), {
  model: 'fable', effort: 'high',
  label: 'divide', phase: 'Divide', schema: DIVIDE_SCHEMA,
})
if (!Array.isArray(divide.subplans) || !divide.subplans.length) {
  throw new Error('divider produced no sub-plans — cannot implement')
}
// Deterministic isolation guard: one file, exactly one owner.
const owner = {}
for (const sp of divide.subplans) for (const f of sp.files) {
  if (owner[f]) {
    throw new Error(`partition not isolated: ${f} owned by ${owner[f]} and ${sp.id}`)
  }
  owner[f] = sp.id
}
// D-23: no slice, no loop. A missing slice would silently degrade every
// downstream agent to "read whatever you can find", which is the failure the
// slice exists to prevent — and it is cheaper to stop here than to discover it
// three gated attempts later.
const sliceless = divide.subplans.filter(sp => !sp.slice_file).map(sp => sp.id)
if (sliceless.length) {
  throw new Error(`divider wrote no plan slice for ${sliceless.join(', ')} — ` +
    `every sub-plan needs one (D-23)`)
}
const SUBPLANS = divide.subplans
// The run's plan-card count (divide + N sub-plans + finalgate) is DECLARED by
// the deterministic emitters, not from here: cycle_reporter.py re-declares
// ORCH_PLANS_TOTAL at the divide close and touch-run seeds it from the spec,
// both folded as a monotonic max so a re-declaration is idempotent (GD-D11).

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

const finalGateFindings = (a) => `${FINDINGS}/finalgate-attempt-${a}.md`
const finalGatePrompt = (attempt, file) => `
[monitor] plan=finalgate stage=sweep role=test attempt=${attempt}
You are the READ-ONLY FINAL AGGREGATE GATE over the whole change-set: never edit
source or tests. Findings files are task state — writing yours is REQUIRED.
${CONTEXT}
Every sub-plan loop closed green; merged changed files: ${JSON.stringify(allFiles)}.
1. Run the FULL suite (all sub-plans' tests together): ${FULL_SUITE_COMMAND}
   (known baseline: ${BASELINE_NOTES}).
2. Syntax-check every changed file as applicable.
3. Scope audit: git status — no edits outside the planned files.
4. Write FULL findings to ${file}: each failure with command, output essence, fix
   suggestion, and the OWNING sub-plan id; on pass, the green evidence.
5. Return \`deviations\` (same shape as a per-loop gate): \`id\` the OWNING
   sub-plan id here, since a merged sweep sees integration, not items;
   \`kind\` missing|differs|extra, \`what\` in ≤120 chars. Empty when green.
${TEST_HINTS ? `Test hints for this run (apply them): ${TEST_HINTS}\n` : ''}`
// The final-gate fixer is a fresh IMPLEMENTER scoped to the whole change-set
// (role=impl) — the impl->test loop, not a standalone gate->fixer. It stays on
// opus: fable is reserved for the gate REVIEWER above. This is the ONE spawn
// that reads the WHOLE plan rather than a slice (D-23): a cross-file
// integration slip is by definition outside any single sub-plan's slice.
const finalFixPrompt = (attempt, findingsFile) => `
[monitor] plan=finalgate stage=implement role=impl attempt=${attempt}
You are the FINAL-GATE FIXER, a fresh subagent. The aggregate sweep over the
merged change-set failed after all per-sub-plan loops were green — likely a
cross-file integration slip.
${CONTEXT}
READ FIRST: ${findingsFile} (the sweep's findings), then ${PLAN_FILE} global
decisions. Fix every finding, editing only files within the planned change-set
(so far: ${JSON.stringify(allFiles)}). Keep every sub-plan's intended items
intact — reconcile, don't revert. Rerun the failing commands until green.
Return \`items\`: one entry per sweep finding you addressed — \`id\` the OWNING
sub-plan id, \`status\` done|partial|skipped, \`note\` in ≤120 chars.
`

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
  log(`aggregate sweep ${finalGate.passed ? 'green' : `not green after ${FINALGATE_ATTEMPTS} attempts`}`)
} else {
  log(`skipping final gate: ${failed.map(f => f.id).join(', ')} did not close green`)
}

// The run close — the Orchestrator badge, the ACTIVE line and the daemon stop —
// is `touch-run close`'s (D-13), and the watcher's own layered close (D-07)
// settles the badge even if the driver never runs it. This script's terminal
// contribution is the structured return below: the journal `result` the
// watcher and the reporter both read.
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
//                       research -> implement pass, not attempts.
const status = criticalStop ? 'stopped-critical'
  : failed.length ? 'awaiting-user' : 'complete'
log(criticalStop
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
