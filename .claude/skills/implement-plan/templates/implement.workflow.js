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

const REPO = '/ABS/PATH/TO/REPO'
const TASK = REPO + '/.claude/local-orchestrators/TASK_NAME'
const FINDINGS = TASK + '/findings'
const S = REPO + '/.claude/shared/monitoring/status.sh'
const MAX_ATTEMPTS = 4

// Hand-off is { plan_file?, parallel? } — ONE complete plan, never sub-plans;
// the Divide phase below derives them. Default strategy is SEQUENTIAL.
const ARGS = typeof args === 'string' ? JSON.parse(args) : (args || {})
const PLAN_FILE = ARGS.plan_file || TASK + '/plan/TASK_NAME-plan.md'
const SUBPLANS_FILE = TASK + '/plan/TASK_NAME-subplans.md'
const PARALLEL_MODE = ARGS.parallel === true   // never shadow the parallel() global

// Quote every path interpolation so a REPO/TASK/S path with a space cannot split
// the env assignment / arg list. Keep agent-filled <summary> text single-line,
// no double quotes (see m-orchestrator SKILL.md).
const statusCmd = (plan, stage, state, msg) =>
  `ORCH_STATE_DIR="${TASK}" bash "${S}" "${plan}" ${stage} ${state} "${msg}"`

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
const CRIT_SCHEMA = {
  type: 'object', required: ['approved', 'summary', 'findings_file'],
  properties: { approved: { type: 'boolean' }, summary: { type: 'string' },
                findings_file: { type: 'string' } },
}

const CONTEXT = `
Repo: ${REPO}. TASK_SPECIFIC_CONTEXT (goal, constraints, out-of-scope areas).
Working tree may hold unrelated in-flight changes — never revert/commit/stash;
touch only files owned by THIS sub-plan.
`

// When a gate/critique agent dies mid-run it writes no findings file; the loop
// writes a placeholder so the next implementer still gets a (minimal) handoff
// instead of an empty findings_file the openFindings guard would drop.
const writePlaceholderFindings = async (file, note) => {
  try {
    const fs = await import('node:fs')
    fs.mkdirSync(FINDINGS, { recursive: true })
    fs.writeFileSync(file, `# ${note}\n\n` +
      `The gate agent returned no result (crashed / killed), so nothing was\n` +
      `recorded. Next implementer: re-run the targeted + full suites yourself,\n` +
      `treat any new failure as still unaddressed, and re-verify the whole change.\n`)
  } catch (e) {
    log(`could not write placeholder findings ${file}: ${e}`)
  }
  return file
}

// Attempt N>1 hands the implementer FILE PATHS, not inlined findings: the fresh
// agent starts with clean context and reads the durable source of truth itself.
const implPrompt = (sp, attempt, findingsFiles) => `
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
${findingsFiles.length ? `4. A previous attempt failed its gates. READ these gate/critique findings files and
address EVERY item still applicable to the current tree (verify against the tree
— earlier items may already be fixed):
${findingsFiles.map(f => '- ' + f).join('\n')}` : ''}

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
LAST run: ${statusCmd(sp.id, 'critique', 'done', `attempt ${attempt}: <approved/rejected + finding count>`)} (state failed if rejected)
Return structured output only: approved, summary (short), findings_file (${file}).
`

// One full impl->test->critique loop for a single sub-plan. Fresh implementer
// every attempt; the handoff is ONLY through findings files + the current tree.
const runLoop = async (sp) => {
  let attempt = 0
  let openFindings = []   // findings-file paths from every failed gate so far
  let impl = null, gate = null, crit = null
  let success = false
  while (!success && attempt < MAX_ATTEMPTS) {
    attempt++
    log(`${sp.id} attempt ${attempt}/${MAX_ATTEMPTS}${openFindings.length ? ` (open findings: ${openFindings.length})` : ''}`)

    impl = await agent(implPrompt(sp, attempt, openFindings), {
      model: 'opus', effort: attempt >= 3 ? 'xhigh' : 'high',
      label: `${sp.id}:impl:${attempt}`, phase: 'Implement', schema: IMPL_SCHEMA,
    })
    if (!impl || !impl.done) { continue }

    const gateFile = gateFindingsFile(sp, attempt)
    gate = await agent(gatePrompt(sp, attempt, impl, gateFile), {
      model: 'opus', effort: 'medium',
      label: `${sp.id}:gate:${attempt}`, phase: 'Test', schema: GATE_SCHEMA,
    })
    if (!gate) {
      gate = { passed: false, summary: 'gate agent died',
               findings_file: await writePlaceholderFindings(gateFile, `test gate crashed (attempt ${attempt}); rerun suites`) }
    }

    const critFile = critFindingsFile(sp, attempt)
    crit = await agent(critPrompt(sp, attempt, impl, gate, critFile), {
      model: 'opus', effort: 'high',
      label: `${sp.id}:critique:${attempt}`, phase: 'Critique', schema: CRIT_SCHEMA,
    })
    if (!crit) {
      crit = { approved: false, summary: 'critique agent died',
               findings_file: await writePlaceholderFindings(critFile, `critique crashed (attempt ${attempt}); re-review the change`) }
    }

    success = gate.passed && crit.approved
    if (!success) {
      // New findings landed: prior-attempt agents are now stale. Queue the files;
      // the NEXT fresh implementer reads them.
      if (!gate.passed && gate.findings_file) openFindings.push(gate.findings_file)
      if (!crit.approved && crit.findings_file) openFindings.push(crit.findings_file)
      openFindings = [...new Set(openFindings)]
    }
  }
  return { id: sp.id, success, attempts: attempt,
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
Write the partition to ${SUBPLANS_FILE} (mkdir -p its dir first): one section
per sub-plan — id (sp-<slug>), title, owned files, the ordered plan items /
finding ids it implements, and the shared decisions it must honor.
LAST run: ${statusCmd('divide', 'partition', 'done', 'N sub-plans')} (replace N)
Return structured output only: subplans (id/title/files/finding_ids each), subplans_file, summary.
`

phase('Divide')
const divide = await agent(dividePrompt(), {
  model: 'fable', effort: 'high',
  label: 'divide', phase: 'Divide', schema: DIVIDE_SCHEMA,
})
if (!divide || !Array.isArray(divide.subplans) || !divide.subplans.length) {
  throw new Error('divider produced no sub-plans — cannot implement')
}
// Deterministic isolation guard: one file, exactly one owner.
const owner = {}
for (const sp of divide.subplans) for (const f of sp.files) {
  if (owner[f]) throw new Error(`partition not isolated: ${f} owned by ${owner[f]} and ${sp.id}`)
  owner[f] = sp.id
}
const SUBPLANS = divide.subplans

// ---- Drive the sub-plans: SERIAL by default, PARALLEL only when instructed ----
log(`implementing ${SUBPLANS.length} feature-sub-plans (${PARALLEL_MODE ? 'PARALLEL' : 'SERIAL'}): ${SUBPLANS.map(s => s.id).join(', ')}`)
let results
if (PARALLEL_MODE) {
  // Opt-in only, and only for disjoint file ownership. Barrier: the final gate
  // sweeps the MERGED change-set.
  results = (await parallel(SUBPLANS.map(sp => () => runLoop(sp)))).filter(Boolean)
} else {
  // Default: one sub-plan at a time, each to green before the next starts.
  results = []
  for (const sp of SUBPLANS) {
    const r = await runLoop(sp)
    results.push(r)
    if (!r.success) log(`${sp.id} did not close green after ${r.attempts} attempts`)
  }
}

const failed = results.filter(r => !r.success)
const allFiles = [...new Set(results.flatMap(r => r.files_changed))]

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

let finalGate = { passed: false, summary: 'final gate not run' }
if (!failed.length) {
  phase('FinalGate')
  for (let fga = 1; fga <= 2; fga++) {
    const file = finalGateFindings(fga)
    // The final gate reviewer is the one implement-side agent that runs fable.
    finalGate = await agent(finalGatePrompt(fga, file), {
      model: 'fable', effort: 'medium',
      label: `finalgate:${fga}`, phase: 'FinalGate', schema: GATE_SCHEMA,
    })
    if (!finalGate) {
      finalGate = { passed: false, summary: 'final gate agent died',
                    findings_file: await writePlaceholderFindings(file, `final gate crashed (attempt ${fga}); rerun full sweep`) }
    }
    if (finalGate.passed) break
    if (fga < 2) {
      const fixer = await agent(finalFixPrompt(fga, finalGate.findings_file), {
        model: 'opus', effort: 'xhigh',
        label: `finalgate:fix:${fga}`, phase: 'FinalGate', schema: IMPL_SCHEMA,
      })
      if (!fixer || !fixer.done) break
    }
  }
} else {
  log(`skipping final gate: ${failed.map(f => f.id).join(', ')} did not close green`)
}

return { subplans: results, final_gate: finalGate,
         all_green: !failed.length && finalGate.passed,
         files_changed: allFiles }
