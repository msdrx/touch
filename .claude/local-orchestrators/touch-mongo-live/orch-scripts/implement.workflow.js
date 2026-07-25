// touch-mongo-live implement workflow — divide the Mongo adoption amendment
// (R-38…R-58) PLUS the normative base it needs into isolated feature-sub-plans
// (Fable divider), then implement them through gated impl->test->critique
// loops, SERIALLY, then a final aggregate gate over the merged change-set.
// Protocol per .claude/skills/implement-plan/templates/implement.workflow.js:
//   * the sub-plan partition comes from the Divide phase (fable) — the gated
//     loops are a pure function of its structured output
//   * gates are READ-ONLY for source, but MUST write findings files to the task folder
//   * implementer attempts are brand-new agents that READ findings files from disk
//   * once new findings land, prior-attempt agents are stale — never reuse or resume them
// args = { plan_file?, parallel? } — sub-plans are DERIVED here, never handed in
export const meta = {
  name: 'touch-mongo-live-implement',
  description: 'Implement the Mongo adoption amendment (R-38-R-58) plus its normative base via gated loops',
  phases: [
    { title: 'Divide', detail: 'fable divider: amendment + normative base -> isolated feature-sub-plans', model: 'fable' },
    { title: 'Implement', model: 'opus' },
    { title: 'Test', model: 'opus' },
    { title: 'Critique', model: 'opus' },
    { title: 'FinalGate', model: 'fable' },
  ],
}

const REPO = '/home/laniakea/Projects/touch'
const TASK = REPO + '/.claude/local-orchestrators/touch-mongo-live'
const FINDINGS = TASK + '/findings'
const S = REPO + '/.claude/shared/monitoring/status.sh'
const MAX_ATTEMPTS = 4

const ARGS = typeof args === 'string' ? JSON.parse(args) : (args || {})
const PLAN_FILE = ARGS.plan_file || TASK + '/plan/touch-mongo-live-plan.md'
const NORMATIVE_PLAN = REPO + '/.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md'
const SUBPLANS_FILE = TASK + '/plan/touch-mongo-live-subplans.md'
const PARALLEL_MODE = ARGS.parallel === true   // never shadow the parallel() global

// Quote every path interpolation so a path with a space cannot split the env
// assignment / arg list. Keep agent-filled <summary> text single-line, no
// double quotes (m-orchestrator SKILL.md).
const statusCmd = (plan, stage, state, msg) =>
  `ORCH_STATE_DIR="${TASK}" bash "${S}" "${plan}" ${stage} ${state} "${msg}"`

const IMPL_SCHEMA = {
  type: 'object', required: ['done', 'files_changed', 'summary'],
  properties: { done: { type: 'boolean' },
                files_changed: { type: 'array', items: { type: 'string' } },
                summary: { type: 'string' } },
}
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

const TARGETED_TEST_COMMAND =
  'run every test file this sub-plan owns directly (stdlib, standalone, non-zero exit on failure): ' +
  'monitoring-module tests from their own dir (cd .claude/shared/monitoring/tests && python3 test_<x>.py); ' +
  'repo tests from the repo root (python3 tests/test_<x>.py)'
const FULL_SUITE_COMMAND =
  `cd "${REPO}" && rc=0; ` +
  `for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done; ` +
  `for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc`
const BASELINE_NOTES =
  'all four monitoring tests (test_server, test_watcher, test_shell, test_frontend) are green at baseline; ' +
  'the suite must stay green on a bare checkout with NO services running and NO third-party packages installed — ' +
  'Mongo-dependent tests must skip cleanly when pymongo or mongod is absent (GD-21/R-56 no-mongod arm)'

const REVIEW_CHECKLIST =
  'GD-21 dependency policy (pymongo==4.17.0 imported lazily ONLY in aggregator/mongo_store.py + aggregator/mirror.py; every other module imports clean on bare stdlib); ' +
  'GD-22 Mongo never on the liveness path (no blocking DB I/O in the poll loop; live view fully functional with Mongo down); ' +
  'GD-24 string _ids via the ref_key grammar only, no BSON subdocument _id or equality-match subdocument keys; ' +
  'GD-25 upsert algebra ($max/$addToSet/$min/$setOnInsert only; no $inc, no bare $set on accumulables; deltas wire-only); ' +
  'GD-26 no delete verbs / no $unset / no TTL index anywhere (single scoped stream_meta deleteMany exception); ' +
  'GD-27 security (loopback-only mongod recipe, auth required, zero-users refusal, 0600 secrets, no credential in repo/events/health/API/prompts); ' +
  'GD-28 provenance pins ({asserted,touch} for custom state, {harness,derived} for mirror; no guessing); ' +
  'GD-29 no agent ever holds a Mongo client (file appends only; aggregator is the sole writer, lease-guarded); ' +
  'GD-30 latency budget (bounded queue, breaker, O(delta) ticks); ' +
  'GD-15 one file one owner respected; tests assert real behavior (not tautologies) and skip cleanly without mongod; ' +
  'no edits outside the sub-plan ownership list; no needless rewrites beyond scope; docs match implemented behavior'

const CONTEXT = `
Repo: ${REPO} — "Touch", a web page for visualizing and managing subagents in
Claude Code sessions (README.md).
CURRENT TREE STATE (verified): NO application source exists yet — no
aggregator/, tests/, docs/, touch-visual/. Git repo on master with ZERO
commits. Root holds README.md, CLAUDE.md, inception.md, .gitignore. The one
working component is the monitoring module .claude/shared/monitoring/
(monitor_server.py, decision_watcher.py, status.sh, monitor.html, monitoring.md,
tests/ — 4 green stdlib test files). The skills
.claude/skills/execute-research/ and .claude/skills/implement-plan/ hold the
templates/*.workflow.js some items amend.
TWO plan files govern this pass, and BOTH bind:
- AMENDMENT (normative for Mongo + this pass's scope): ${PLAN_FILE}
  (GD-21…GD-30, items R-38…R-58; its §2 dispositions amend the base plan).
- BASE PLAN (normative for everything else): ${NORMATIVE_PLAN}
  (GD-1…GD-20, items R-01…R-37).
Environment facts: Python 3.13, stdlib-only policy with the single GD-21
pymongo exception; pip installs work (pymongo==4.17.0 + dnspython); a Docker
daemon is available (mongo:7 pulls and runs — use the R-42 loopback+auth
recipe for any live-mongod test arm, and always skip cleanly without it);
no pytest — every test file is a standalone executable.
Git rules: the working tree is the live repo — never revert/stash; NEVER
commit, with ONE exception: the sub-plan that owns normative R-02 (repository
bootstrap) performs exactly the commits that item specifies.
Working tree may hold unrelated in-flight state under .claude/ (orchestrator
task folders, running daemons) — leave it alone; touch only files owned by
THIS sub-plan.
`

// When a gate/critique agent dies mid-run it writes no findings file; the loop
// writes a placeholder so the next implementer still gets a (minimal) handoff.
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

// Attempt N>1 hands the implementer FILE PATHS, not inlined findings.
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
2. ${PLAN_FILE} — §1 global decisions (GD-21…GD-30) AND §2 dispositions AND
   every item matching your ids (${sp.finding_ids.join(', ')}).
3. ${NORMATIVE_PLAN} — its global decisions (GD-1…GD-20) and every base item
   your ids name. Where your sub-plan owns both a base item and its amendment
   extension for the same file, implement them as ONE coherent feature exactly
   per both specs; the amendment's §2 disposition wins on any conflict.
4. The research findings files referenced by those items (paths in the plans).
${findingsFiles.length ? `5. A previous attempt failed its gates. READ these gate/critique findings files and
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
1. Targeted suites for ${sp.id} (must be 100% green): ${TARGETED_TEST_COMMAND}
2. Full suite regression gate: ${FULL_SUITE_COMMAND}
   Known baseline: ${BASELINE_NOTES} — baseline failures do not fail the gate;
   any OTHER failure is NEW and fails it.
3. Verify against ${SUBPLANS_FILE} section "${sp.id}" and the matching items in
   ${PLAN_FILE} + ${NORMATIVE_PLAN}: every owned item present in the tree; tests
   assert the intended behavior (not tautologies); no edits outside the
   sub-plan's owned files (git status against the ownership list).
4. Write FULL findings to ${file} (mkdir -p ${FINDINGS} first): every failure with
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
Review ONLY the diff of: ${JSON.stringify(impl.files_changed)} (git diff -- <files>;
for files new in this untracked tree, review the full file content),
against ${SUBPLANS_FILE} section "${sp.id}", the matching items in ${PLAN_FILE}
and ${NORMATIVE_PLAN}, and the research findings they cite.
Test gate said: ${JSON.stringify(gate.summary)} (details: ${gate.findings_file}).
Attack checklist: ${REVIEW_CHECKLIST}.
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
  let openFindings = []
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
      if (!gate.passed && gate.findings_file) openFindings.push(gate.findings_file)
      if (!crit.approved && crit.findings_file) openFindings.push(crit.findings_file)
      openFindings = [...new Set(openFindings)]
    }
  }
  return { id: sp.id, success, attempts: attempt,
           files_changed: (impl && impl.files_changed) || [],
           gate, critique: crit, open_findings: success ? [] : openFindings }
}

// ---- Divide: the Fable divider derives the sub-plans from BOTH plans ----
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
You are the DIVIDER — the divide-and-conquer analyst for this pass.
READ-ONLY for source; you write exactly one partition file in task state.
FIRST run: ${statusCmd('divide', 'partition', 'running', 'partitioning amendment plus base into sub-plans')}
${CONTEXT}
READ FULLY, in order:
1. ${PLAN_FILE} — the AMENDMENT: §0 authority/scope, §1 GD-21…GD-30, §2
   dispositions of existing law, §3 ordered items R-38…R-58 with their hard
   sequencing constraints and its explicit "Note for the divider".
2. ${NORMATIVE_PLAN} — the BASE plan: GD-1…GD-20 and items R-01…R-37.
3. Skim the real tree to confirm the state brief above (ls the repo root;
   the monitoring module and its tests exist; nothing else does).

SCOPE OF THIS PASS — derive the partition to deliver R-38…R-58 COMPLETELY,
which on this empty tree necessarily pulls in base items:
- Base phase 0 (R-01…R-06): preconditions of R-42+; not done on disk.
- The R-58 first wave: base R-08 + R-09 + R-13, grouped by file ownership with
  base R-07/R-10 and amendment R-39/R-40 (they edit the same monitoring files —
  the amendment's divider note mandates this grouping).
- Base phase 3 items (R-22…R-33) whose files amendment items extend or depend
  on — aggregator/*, touch-visual/*, tests/*, docs — including store.py per
  R-24 (base spec stands unchanged; Mongo does not replace it; nothing in the
  amendment touches it).
OUT of scope this pass: base R-11/R-12 (monitor server/dashboard fixes — no
amendment item touches those files), base phase 2 (R-18…R-21) and phase 4
(R-34…R-37) — gated on probes/R-20; amendment arms that depend on R-20 use the
TOUCH_CONTROL_PATHS fallback exactly as their items specify. Record these
exclusions in the partition file so nobody re-litigates them.

Partition rules:
- FILE OWNERSHIP is the isolation rule: every touched file belongs to exactly
  one sub-plan across the WHOLE partition.
- Where an amendment item extends a base file that does not exist yet, the
  owning sub-plan implements the base item AND the extension as one feature;
  list both ids in finding_ids.
- A cross-file item is split into per-file halves, each half restating the
  shared decision so the halves cannot drift.
- Honor the amendment's hard sequencing (§3): order the subplans array in
  EXECUTION ORDER — the driver runs them serially in array order. First wave
  (phase 0 + R-38…R-41 + R-58 scope) strictly before M1+; M1 foundations
  before M2 pipelines; M2 before M3/M4.
- Optimize the cut: cohesive features, minimal cross-sub-plan coupling, no
  sub-plan too broad to implement and review in one gated loop.
Write the partition to ${SUBPLANS_FILE} (mkdir -p its dir first): one section
per sub-plan — id (sp-<slug>), title, owned files, the ordered plan items /
finding ids it implements (amendment + base), the shared decisions it must
honor, and the scope-exclusions record.
LAST run: ${statusCmd('divide', 'partition', 'done', 'N sub-plans')} (replace N)
Return structured output only: subplans (id/title/files/finding_ids each, in
execution order), subplans_file, summary.
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

// ---- Drive the sub-plans: SERIAL (default) ----
log(`implementing ${SUBPLANS.length} feature-sub-plans (${PARALLEL_MODE ? 'PARALLEL' : 'SERIAL'}): ${SUBPLANS.map(s => s.id).join(', ')}`)
let results
if (PARALLEL_MODE) {
  results = (await parallel(SUBPLANS.map(sp => () => runLoop(sp)))).filter(Boolean)
} else {
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
1. Run the FULL suite (all sub-plans' tests together): ${FULL_SUITE_COMMAND}
2. Syntax-check every changed file as applicable (python3 -m py_compile; node --check for JS).
3. Run the R-56 acceptance arms as far as the environment allows (no-mongod arm
   is mandatory; mirror arm only if a mongod is provisionable via the R-42
   recipe, else record the skip).
4. Scope audit: git status — no edits outside the planned files.
5. Write FULL findings to ${file}: each failure with command, output essence, fix
   suggestion, and the OWNING sub-plan id; on pass, the green evidence.
LAST run: ${statusCmd('finalgate', 'sweep', 'done', `attempt ${attempt}: <passed/failed>`)} (state failed if gate fails)
Return structured output only: passed, summary (short), findings_file (${file}).
`
// The final-gate fixer is a fresh IMPLEMENTER scoped to the whole change-set
// (role=impl). It stays on opus: fable is reserved for the gate REVIEWER above.
const finalFixPrompt = (attempt, findingsFile) => `
[monitor] plan=finalgate stage=implement role=impl attempt=${attempt}
You are the FINAL-GATE FIXER, a fresh subagent. The aggregate sweep over the
merged change-set failed after all per-sub-plan loops were green — likely a
cross-file integration slip.
FIRST run: ${statusCmd('finalgate', 'implement', 'running', `attempt ${attempt}: fixing sweep failures`)}
${CONTEXT}
READ FIRST: ${findingsFile} (the sweep's findings), then ${PLAN_FILE} §1 global
decisions and ${NORMATIVE_PLAN} global decisions. Fix every finding, editing only
files within the planned change-set (so far: ${JSON.stringify(allFiles)}). Keep
every sub-plan's intended items intact — reconcile, don't revert. Rerun the
failing commands until green.
LAST run: ${statusCmd('finalgate', 'implement', 'done', `attempt ${attempt}: <one-line summary>`)}
Return structured output only: done, files_changed, summary.
`

let finalGate = { passed: false, summary: 'final gate not run' }
if (!failed.length) {
  phase('FinalGate')
  for (let fga = 1; fga <= 2; fga++) {
    const file = finalGateFindings(fga)
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
