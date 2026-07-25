# touch-full-recon — reconciled implementation plan

Synthesized 2026-07-25 from six research reports under
`.claude/local-orchestrators/touch-full-recon/findings/` (product, monitoring,
skills, plans, audit, runstate — cited below by finding id). Findings stay in
those files; this plan references them by id + path.

## 0. Authority and supersession

**This plan supersedes `touch-aggregator-plan.md` (T1–T23) and
`touch-monitor-spawn-plan.md` (P1–P12, G1–G9) as the implementable plan.**
Those two files are historical records; do not hand either to `implement-plan`
again (PLANS-1: they claim overlapping files under different module names and
`implement-plan` takes exactly ONE plan file). The design decisions D1–D14 in
the T-plan remain design law **except as amended below**.

Disposition of D1–D14:

| Decision | Status | Where |
|---|---|---|
| D1 owned/observed session classes | **AMENDED** — three classes: owned / cooperating / observed | GD-6 |
| D3 identity table | **AMENDED** — historical-session arm; legacy 8-hex exemption | GD-7, R-26, R-27 |
| D4 record shape / ref union | **AMENDED** — union stated once, 5 members, open-tail validator; legacy refs synthesized | GD-11, GD-14 |
| D5 `.touch/` state layout | **AMENDED** — control file becomes per-session, aggregator-owned; `server.json` holds live token 0600 | GD-6, R-34 |
| D6 tailer/no-auto-discovery | **STANDS + clarified** — stat-ing a configured `wf_dir` is not "discovery"; checkpoint identity `(st_dev, st_ino, size, offset)` | R-23, R-27 |
| D7 control verb table | **AMENDED** — "restart" gets ONE meaning; checkpoint is three-state | GD-4, R-35 |
| D8 "journal `result` opaque, never parsed" | **SUPERSEDED** — `result` is polymorphic (AUDIT-2); `totalTokens` ban stands and extends to snapshot back-fill | GD-11 |
| D9 security invariants | **STANDS** — and D9.3 now wins over T11 (query-string ids) | GD-12, GD-13 |
| D10 truncation sentinel | **STANDS + extended** to the watcher and Touch's tailer | R-07, R-23 |
| D13 honesty rules | **STANDS** — applied harder (legacy re-labels, disabled controls carry reasons) | GD-4, GD-14 |
| D2, D11, D12, D14 | **STAND** — unchallenged by this recon | — |

Item mapping (old → this plan): T1→R-01/R-22, T3→R-29, T4→R-30/R-31,
T5→R-24, T6→R-25, T7/T8→R-26 (amended by AUDIT-2/-4/-5/-6/-13), T11→R-31
(amended), T14→R-35 (amended), T16→R-32, T20→R-27 (amended), T21→R-13
(its "don't touch the watcher" clause is superseded), T23→R-33;
P1→R-22/R-01, P2→R-24, P3→R-25, P4→R-23, P5/P6→R-26/R-28 (P6's name-only tree
superseded by GD-7), P7→R-34/R-35 (gated), P8→R-30, P9→R-31 (amended),
P10→R-32 (Stop button moved to phase 4), P11→R-36 (gated), P12→R-37/R-33.
Any T/P item not named above (T2, T9, T13, T15, T17, T19, T22 tier) is
**deferred unchanged** under its original id — recorded here so the deferral is
deliberate, not lost (PLANS-1 requirement).

---

## 1. Global decisions (GD-1 … GD-20)

Decided once; downstream work must not diverge. Each cites the findings that
forced it.

### GD-1 — Repo safety gate (hard precondition)
**No `git add` or `git commit` in this repo until R-01 (.gitignore) is green.
No commit while any watcher is writing** (check
`ps -eo cmd | grep "[d]ecision_watcher"`). The first thing any implementation
session does is R-01. [PRODUCT-2, AUDIT-14, RUNSTATE-10, RUNSTATE-11]

### GD-2 — Git bootstrap: branch, identity, commit boundary
Branch is renamed `master` → `main` before commit #1 (environment PR default is
`main`; renaming later is more expensive). Repo-local `user.name`/`user.email`
are set (currently unset everywhere — verified again this run). Two commits:
**C1 "tooling and docs"** (`README.md`, `CLAUDE.md`, `inception.md`,
`.gitignore`, `.claude/settings.json`, `.claude/statusline.sh`,
`.claude/skills/**`, `.claude/shared/monitoring/**`); **C2 "orchestration
history"** (`.claude/local-orchestrators/**`), taken only when no watcher is
writing. [PRODUCT-3, RUNSTATE-11]

### GD-3 — Doc architecture: ONE README, an authority ladder
`README.md` becomes the human entry point (what Touch is, honest verb table,
how to run, where design docs live); P12's `README-touch.md` is **dropped**;
`docs/control-semantics.md` (T23 shape) holds the verb ladder detail. Authority
ladder, to be stated in CLAUDE.md: **this plan** → `touch-aggregator-plan.md`
(design law D1–D14 as amended) → `inception.md` (summary) → `README.md`
(intent) → `CLAUDE.md` (session guide). [PRODUCT-1, PRODUCT-10, PLANS-13]

### GD-4 — Control verbs: one vocabulary, one meaning of "restart"
The honest table (README + CLAUDE.md + SKILL.md + UI must all use it):
- **start** — deterministic (spawn).
- **terminate/kill** — deterministic ladder (owned sessions only).
- **stop (graceful)** — model-mediated; rendered
  `requested / pending — orchestrator busy / sent / confirmed` (never a bare
  `expired` while the driver is provably blocked — SKILLS-14).
- **restart** — **DECIDED: restart = re-invoke the workflow script with the
  stored partition (`subplans_file`) and `only:[ids]`; fresh agents, attempt
  numbering continues (`from_attempt`), Divide/derivation skipped.**
  `Workflow({resumeFromRunId})` is REJECTED as a restart meaning — it replays
  agents without re-executing them (SKILLS-6). README's and
  touch-orchestrate's wording both change to this single meaning.
- **pause** — does not exist without the hook gate; deferred (T15), never
  rendered for sessions where it cannot be honest. [PRODUCT-4, SKILLS-6, D13]

### GD-5 — Role→model table (user-directed mandate, carried forward)
Research / implementer / test-gate / critic roles: **Opus 5 at effort xhigh**.
Synthesizer, main user-terminal agent, final review agent: **Fable**.
**Divider: DECIDED — Fable** (it is a judgement-heavy, low-volume role exactly
like the synthesizer, and the shipped template already pins it fable; this
open decision is hereby closed explicitly, not silently). Effort caps stay
≤ xhigh. Applied to templates in R-21 and recorded in CLAUDE.md in R-05.
[PRODUCT-6; source: touch-repo-recon/orch-scripts/research.workflow.js:49]

### GD-6 — Three session classes; evidence-gated promotion
D1 amended: **owned** (Touch spawned it; PTY + deterministic verbs),
**cooperating** (observed + evidence of touch-orchestrate conformance:
an observed ack line in its control file or a `[touch]` marker in a live agent
prompt — never a user checkbox), **observed** (read-only; every control 403s
server-side; T14's "foreign sessions 403" becomes "non-cooperating sessions
403"). Model-mediated verbs only for cooperating; each rendered with the GD-4
state machine. [PLANS-2]

### GD-7 — Node identity: harness facts create nodes, markers label them
Node identity = `(runId, key, ordinal)` for Workflow agents, full 17-hex
`agentId` for Agent-tool agents — both harness-derived, both always present.
`name` / `parent` / `plan` / `stage` / `attempt` are labels in a separate
layer; a missing marker degrades the label, never the node. P6's name-only
tree and INTENT-13's "marker mandatory" direction are **superseded**. Legacy
cards join via a documented `(plan, stage, attempt) → node` mapping.
[AUDIT-11, MONITORING-4, RUNSTATE-8]

### GD-8 — Two run profiles; the journal is not the only event source
- **Workflow profile** (execute-research / implement-plan): deterministic
  source = `journal.jsonl` via `decision_watcher.py`; agents have no `taskId`,
  **stop is unavailable** and rendered disabled with that reason.
- **Agent-tool profile** (touch-orchestrate background spawns): deterministic
  source = `state/spawn-ledger.jsonl` (+ transcripts); `taskId` present,
  `TaskStop` works.
Touch ingests both into one store; `touch-orchestrate/SKILL.md` documents both
profiles explicitly (R-20). The implementer must first empirically confirm
where a `run_in_background` Agent-tool spawn writes its transcript and whether
tokens are recoverable (R-04). [MONITORING-3, SKILLS-13, SKILLS-14]

### GD-9 — One marker grammar
Markers are matched **per physical line** (`re.MULTILINE`), only within the
**first 4 physical lines** of the agent's oldest transcript's first `user`
record (real prompts start with `\n` — PLANS-10; "FIRST line" rules are
amended to "first lines, leading blank tolerated"). Fields parse as
**order-independent `key=value` pairs**; unknown keys are ignored (so
`ledger=`, `model=`, `phase=` can be added compatibly). `[monitor]`:
last-wins within the window. `[touch]`: must be within the window, else the
node is flagged `marker-misplaced`. A marker outside the window (quoted prose
— 12 false-positive files exist on disk today) is **never** used.
[PLANS-10, AUDIT-9, SKILLS-11]

### GD-10 — Plan-close and run-close semantics
A plan card closes only on (a) an explicit `status.sh <plan> plan done|failed`
or (b) the quiet-close settle pass; the serial-only `last_plan` heuristic is
retired for new runs (kept behind `strategy:"serial"` in `orch-config.json`
for legacy). A plan whose agents all resulted without a decisive verdict
settles **done** if the last result was not a failure ("closed, no verdict"),
never `failed`. Templates MUST emit terminal
`status.sh <plan> plan done` per plan and
`status.sh <run> orchestrator complete done "<summary>"` on the success path.
Touch treats "no complete event + journal quiet" as *unknown*, never running.
[SKILLS-1, SKILLS-2, SKILLS-3, RUNSTATE-4, PRODUCT-7]

### GD-11 — Canonical touch-events-v2 shapes
- **ref union** (stated ONCE, here):
  `{uuid} | {toolUseId} | {agentId} | {runId,key,ordinal} | {pid,procStart}`.
  Validator is **open at the tail**: unknown ref shapes are retained and
  passed through; hard rejection only for malformed instances of known shapes
  (non-17-hex agentId, non-UUID uuid). Legacy 8-hex ids are namespaced
  `legacy:<task>:<id8>` and exempt. [PLANS-3, PLANS-6]
- **seq** is per event-log file (per stream); a cursor is `(stream, seq)` —
  a bare seq is never a valid cursor. [PLANS-11]
- **ts**: writer emits exactly one format (`…Z`); readers normalize
  `Z→+00:00`; order = file line order, never ts sort. [RUNSTATE-6]
- **`result` is polymorphic**: schema-validated object when structured, free
  string otherwise; verdicts driven from dict keys per
  `decision_watcher.py:370-447`; string case rendered opaque. [AUDIT-2]
- **tokens**: record always carries all four keys
  `{in,out,cached,cache_write}` (default 0). Run-level tokens = Σ over nodes
  of per-node deduped totals; `<runId>.json.totalTokens`/`totalToolCalls` are
  display-only "harness reported" and never substituted. [RUNSTATE-14, AUDIT-13]
- **detail** strings: cap 1 KB at the writer, single-line; the real reason is
  shell/JS-template embedding, not JSON. [MONITORING-6, RUNSTATE-16]

### GD-12 — API shape: query-string ids, static route table
All endpoints use query-string ids with a `(method, route) → handler` dict and
default 404 — honoring D9.3 literally; T11's path-parameter forms are
rewritten (`/api/session/timeline?session=<uuid>&since=`,
`/api/run/graph?run=`, `/api/run/node?run=&agent=`,
`/api/toolresult?id=`). Unknown ids → 404; **never** fall back to another
task/session/stream (the monitor's silent STATE_DIR fallback is a wrong-target
hazard once names route controls). [PLANS-4, MONITORING-5]

### GD-13 — Security posture (decided before any control endpoint exists)
Touch server: binds **127.0.0.1 by default**; `0.0.0.0` is an explicit opt-in
flag documented with the `sbx ports` flow. Per-boot 256-bit token on every
route except `/health` (`hmac.compare_digest`), injected into the page at
serve time. **Origin/Host allowlist enforced at WS upgrade** (403 otherwise).
Read-only routes and control routes are separate groups. Ports by convention
(reserved, not occupied): 8931 legacy monitor, 8932 Touch. Legacy
`monitor_server.py` stays read-only and gains only the Origin allowlist
(token declined for it — see Discards). Copy verbatim from the monitor:
`safe_artifact_path` containment, CSP sandbox + nosniff on served files.
[MONITORING-2, PRODUCT-8]

### GD-14 — Legacy ingest rules (`source:"legacy"`)
- Synthesize refs: `runId` = `basename(orch-config.wf_dir)` if present else
  `legacy:<task-folder>`; `taskId` = folder name; `ordinal` = per-(plan,stage)
  counter incremented on each `state:"running"` spawn (two-wave respawns
  become distinct nodes). [RUNSTATE-2]
- Re-label at read time, marked `derived_from_legacy:true`: `plan failed`
  with detail matching `loop exited ->` and all stage agents resulted →
  **"closed — no verdict"**; an agent left running with a later sibling
  `started` on the same stage → **superseded**; run with terminal
  `orchestrator|complete` closes all non-terminal nodes **stale**.
  [RUNSTATE-4, RUNSTATE-9, AUDIT-10]
- Dedup duplicate stage terminals on `(task, plan, stage, terminal-state)`,
  watcher-wins, keep the agent's own detail as `data.agentDetail`.
  [RUNSTATE-7]
- Fold `quiet:true` token deltas: take the last cumulative `agent.tokens`,
  at most one token record per agent per throttle window. [RUNSTATE-12]
- Map out-of-enum states to `info`, never drop. [RUNSTATE-16]
- **`.watcher-state.json` is watcher-private; Touch never reads it** (it
  contradicts its own stream and is never closed on kill). [RUNSTATE-5, RUNSTATE-3]
- Task-folder tolerance: missing `orch-config.json`/`events.jsonl` ⇒
  "plan only / never run" kind, no controls. [RUNSTATE-13]
- Archive label is **derived, not constant**: `wf_dir` exists ⇒ "live source
  present" (render via full path); pruned ⇒ "archived — source transcripts
  unavailable"; foreign ⇒ display path, never glob. T20's unconditional label
  is superseded. [PLANS-5]

### GD-15 — Module layout (one file, exactly one owner)
`aggregator/` = `tailer.py`, `store.py`, `sessions.py`, `ingest.py`,
`legacy.py` (new), `agents.py`, `ws.py` (kept separate — most unit-testable
module), `server.py` (routes table + handlers), `control.py` (phase 4),
`hooks/touch-hook.sh` (phase 4). `touch-visual/` = `index.html`, `app.js`,
`style.css`. `tests/` one file per module + `run_all.sh`. P-plan's
fine-grained names win; T-plan's separate `ws.py` wins. [PLANS-1]

### GD-16 — Git policy for run state
`events.jsonl` per task: **tracked** (history; input to legacy ingest).
`.claude/local-orchestrators/*/.watcher-state.json`: **gitignored**
(derivable, mutating; the never-delete rule protects the stream, not the
checkpoint). Growth policy decided explicitly: streams stay uncompressed;
GD-14's token folding halves future volume; revisit only if the repo exceeds
~20 MB. [PRODUCT-9]

### GD-17 — Findings disposition register (all corpora)
The 51 `touch-repo-recon` findings and the 110 `touch-aggregator` findings get
a one-line disposition each (`→ item R-nn` | `→ GD-n` | `merged` | `rejected,
reason`), produced as `plan/findings-register.md` by R-06 with a static test
asserting every finding id under `.claude/local-orchestrators/*/findings/*.md`
appears exactly once. This run's ~90 findings are disposed inline by this
plan (§3). [AUDIT-1]

### GD-18 — Fixtures before features
The wf_829e6f58 corpus (journal, 9 agent transcripts incl. both copies of
`a2fc883c…`, meta stubs, snapshot, 3 `tool-results/*.txt`, the task
`events.jsonl`) is frozen into `tests/fixtures/` **before** the retention
sweep can delete it (R-03). All acceptance criteria are restated **per phase**
against fixtures, not against live `~/.claude` (Part F's single criterion
spanned three phases and was unreachable as written). [AUDIT-7, PLANS-6, PLANS-15, RUNSTATE-17]

### GD-19 — The control phase is gated on the hook probe
The 10-minute hook hot-reload probe (R-04) runs before any control item.
Branch: hot-reload **works** ⇒ the hook gate becomes v0's deterministic stop
for observed sessions; **doesn't** ⇒ the minimal owned-session spawner
(`--settings` + `--session-id` + env allowlist slice of T9, no PTY tier) is
pulled into the control phase and P11-class hooks depend on it. Until R-04
lands, R-34…R-36 are blocked, not sequenced. [AUDIT-3, PLANS-8]

### GD-20 — Copy-verbatim vs do-not-inherit (from the monitoring module)
**Copy verbatim:** append-only events + full replay; torn-tail handling
(cut at last `\n`, never advance past an incomplete line); monotonic token
deltas clamped ≥0 + message-id dedup across rotated transcripts;
`safe_artifact_path` + CSP sandbox; escape-first rendering; checkpoint keyed
to its source; module statelessness (`ORCH_STATE_DIR`-style selection).
**Do not inherit:** the network posture (GD-13); unknown-task fallback
(GD-12); 1 Hz full-transcript re-parse (make reads incremental); the
render-everything frontend loop and unbounded log; unlocked appends without a
length cap; the flat schema as a graph model (GD-7); the journal-only event
source (GD-8). [MONITORING appendix]

---

## 2. Ordered implementation items

Not partitioned into sub-plans — `implement-plan`'s divider owns that. Items
are ordered by dependency; phases mark gates. Note for the divider: R-07/R-08/
R-13 all edit `decision_watcher.py`; R-09/R-14/R-15/R-18 all edit the two
skill templates — group accordingly.

### Phase 0 — repo safety, fixtures, truth (nothing else may start first)

---

**R-01 — Harden `.gitignore` before anything creates `.touch/`**
- Files: `.gitignore:1-37` (additive edit);
  `.claude/shared/monitoring/tests/test_shell.py:155-161` (extend guard).
- Resolves: PRODUCT-2, AUDIT-14, RUNSTATE-10, PRODUCT-9 (aliases: INTENT-6).
- Approach: append `.touch/`, `.touch*/` (covers `TOUCH_STATE_DIR` variants),
  `.claude/settings.local.json`, `*.pid`,
  `.claude/local-orchestrators/*/.watcher-state.json` (GD-16). Strictly
  additive (`test_shell.py` does substring checks only — verified safe).
- Test: extend `test_gitignore` with the new entries **plus a negative
  assertion** that no rule ignores `.claude/local-orchestrators/` itself or
  `events.jsonl` under it.

**R-02 — Repository bootstrap: identity, `main`, two commits**
- Files: repo root; `.git/config` (local identity); empty dirs get `.gitkeep`
  (`touch-full-recon/report/`, `touch-aggregator/report/`,
  `touch-repo-recon/plan/` etc. — RUNSTATE-18).
- Resolves: PRODUCT-3, RUNSTATE-11, RUNSTATE-18.
- Approach: per GD-2 — set repo-local `user.name`/`user.email`; rename branch
  to `main`; commit C1 then C2, C2 only when no watcher is writing. `git
  stash create` on zero commits exits 1 with no sha (re-verified this run) —
  nothing before C1 may assume a HEAD exists.
- Test: a shell check in `tests/` asserting `git rev-parse HEAD` succeeds and
  `git check-ignore .touch/x` passes; C2 contains no `.watcher-state.json`.

**R-03 — Freeze the reference fixtures**
- Files: new `tests/fixtures/run-wf_829e6f58/` (journal, 9 agent transcripts
  incl. both `a2fc883c…` copies, 7 `.meta.json`, `wf_829e6f58-b2f.json`
  snapshot, 3 `tool-results/*.txt`); new `tests/fixtures/legacy/` (verbatim
  line selections from `touch-aggregator/events.jsonl` and
  `touch-repo-recon/events.jsonl` — the two-wave respawn, the
  `plan|failed "loop exited -> synthesis"` line, duplicate terminals, mixed
  ts formats); `touch-monitor-spawn/` noted as the plan-only-folder fixture.
- Resolves: AUDIT-7, RUNSTATE-17, PLANS-6 (fixture half), AUDIT-5/AUDIT-6
  (supplies their specimens).
- Approach: copy now — the corpus is on a retention-sweep deletion clock and
  is the only real specimen of a completed multi-session run (cross-session
  split, same agentId in two dirs, snapshot under the wrong session).
  Sanitize only if inspection finds credentials (it is repo source text).
- Test: a manifest test asserting the fixture set is complete and byte-stable
  (sha256 list checked in).

**R-04 — Run the settling probes; record results**
- Files: new `.claude/local-orchestrators/touch-full-recon/report/probes.md`.
- Resolves: AUDIT-3, PLANS-8, AUDIT-12 (probes 1, 2, 4), MONITORING-3
  (transcript-location probe), SKILLS-13 (taskId probe context).
- Approach: (1) hook hot-reload: start a probe session under /tmp, add a
  `PreToolUse` hook to its project settings *after* start, invoke a tool,
  observe (GD-19 branches on this); (2) hooks under an interactive PTY via
  `--settings`; (3) one settings file mixing `command` + `http` hook types;
  (4) `time claude agents --json`; (5) where a `run_in_background` Agent-tool
  spawn writes its transcript and whether `message.usage` rows exist there
  (GD-8 depends on it). Each result recorded with command + date
  (AUDIT-16's provenance convention).
- Test: none (evidence artifact); R-34+ cite it.

**R-05 — Docs truth pass (CLAUDE.md, README.md, inception.md)**
- Files: `CLAUDE.md:7-31` (inventory + pointers), `:104-114` (ports/serve),
  `:116-126` (rules; add daemon-shutdown bullet; reword the detail-string
  reason), `:127-130` (delete omnigent claim); `README.md:1-7` (verb table
  per GD-4, keep original intent para); `inception.md:54-56` (omnigent),
  `:140-174` (restart wording per GD-4), `:235-246` (run status + token
  figure → deduped ≈29.5M in / 316k out, source named).
- Resolves: PRODUCT-1, PRODUCT-4 (doc half), PRODUCT-5, RUNSTATE-1,
  PRODUCT-7 (doc half), PRODUCT-8, PRODUCT-10, PRODUCT-11, PRODUCT-12,
  AUDIT-15 (doc half), AUDIT-13 (doc half), PRODUCT-13 (note that
  local-orchestrator paths are historical artefacts).
- Approach: per GD-3/GD-4/GD-5; true inventory of the four task folders (what
  each is, whether complete, authoritative artifact); state the wf_dir paths
  are this repo's own runs and the join key to harness journals; two labelled
  serve blocks (legacy 8931 / Touch 8932 + GD-13 posture, ports *reserved*);
  name `.claude/settings.json` + `statusline.sh` + the `jq` exception; add
  "when a run ends, stop its watcher; leave state in place"; note `plan/` and
  `report/` may legitimately be empty.
- Test: new static guard (test_shell.py genre) asserting CLAUDE.md contains
  `inception.md`, `touch-aggregator-plan.md`, `touch-full-recon-plan.md`,
  `touch-orchestrate`, and does NOT contain `omnigent`.

**R-06 — Findings disposition register**
- Files: new `.claude/local-orchestrators/touch-full-recon/plan/findings-register.md`;
  new `tests/test_register.py` (~20 lines).
- Resolves: AUDIT-1.
- Approach: per GD-17 — one line per finding across all nine prior findings
  files + this run's six; dispositions reference this plan's ids.
- Test: static test that every `[A-Z]+-\d+` finding id in
  `.claude/local-orchestrators/*/findings/*.md` appears exactly once in the
  register.

### Phase 1 — monitoring module fixes (the substrate Touch inherits)

---

**R-07 — Watcher crash-safety and truncation handling**
- Files: `decision_watcher.py:40` (STATE_DIR resolution), `:150` (`emit`),
  `:106-113` (config ints), `:575` + `:494-512` (truncation).
- Resolves: MONITORING-1, MONITORING-11, MONITORING-8, RUNSTATE-15.
- Approach: `os.makedirs(STATE_DIR, exist_ok=True)` at resolution;
  `try/except OSError` around emit's write (stderr, don't die); `_int_cfg`
  helper with default + stderr warning (mirror SERVER-2), resolved after the
  first heartbeat emit; in the main loop `size < offset` ⇒ reset to 0, clear
  derived state, emit `watcher info "journal truncated — rebuilding"`.
- Test: `test_watcher.py`: nonexistent nested `ORCH_STATE_DIR` ⇒ first emit
  succeeds; `"max_gate_attempts":"three"` ⇒ watcher lives with default;
  shrunken journal ⇒ re-backfill event observed.

**R-08 — Plan-close/run-close semantics in the watcher**
- Files: `decision_watcher.py:639-663` (close/reopen), `:450-467`
  (`run_outcome`), `:748-753` (settle), `:386-389` (finalgate text),
  `:403-414`.
- Resolves: SKILLS-1 (watcher half), SKILLS-2 (watcher half), SKILLS-3,
  SKILLS-9, RUNSTATE-4 (forward fix; aliases PRODUCT-7, AUDIT-10 forward half).
- Approach: per GD-10 — track `last_result_ok[plan]`; close predicate
  `decisive.get(p) if p in decisive else last_result_ok.get(p, False)` in
  close, settle and `run_outcome`; reopen branch fires for `failed` as well
  as `done`; gate the `last_plan` heuristic on
  `orch-config.json:"strategy" == "serial"`; key final-gate decision text on
  `(plan, role)` — "PASS → run complete" / "FAIL → spawn fixer, re-gate N/2".
- Test: journal fixtures — research-shaped run (results = `{findings…}` only)
  closes `done` + complete `done`; two interleaved parallel sub-plans produce
  no spurious badge flap; `plan=finalgate role=test` detail mentions no
  critique.

**R-09 — Templates emit terminal events and publish their caps**
- Files: `execute-research/templates/research.workflow.js:145,159-161`;
  `implement-plan/templates/implement.workflow.js:30,251,284`.
- Resolves: SKILLS-2 (template half), SKILLS-1 (template half), SKILLS-10.
- Approach: script-emitted (not agent-emitted) `status.sh <plan> plan done`
  after each barrier/loop; `status.sh <run> orchestrator complete done
  "<summary>"` on the success path (driver call stays as idempotent
  backstop); adapted scripts write/merge `orch-config.json`
  (`max_plan_attempts` = `MAX_ATTEMPTS`, final-gate cap, `strategy`);
  `status.sh <sp.id> plan failed "attempts exhausted N/N"` on unsuccessful
  loop exit.
- Test: static guard — every `templates/*.workflow.js` defining
  `MAX_ATTEMPTS` also writes it to `orch-config.json` and contains the two
  terminal `status.sh` calls.

**R-10 — Event-stream write integrity**
- Files: `status.sh:28-49`; `decision_watcher.py:150-151`;
  `monitor_server.py:96-99` (+ `/health`).
- Resolves: MONITORING-6, RUNSTATE-16 (writer half).
- Approach: `flock` both append sites; truncate `detail` to 1 KB at the
  writer (GD-11); parse-failure counter surfaced via `/health`;
  `console.warn` in the browser's catch; `status.sh` warns on stderr for
  out-of-enum state but never fails (best-effort writer must not break an
  agent).
- Test: concurrent-append test with >8 KiB details asserting zero lost/torn
  lines; `/health` counter increments on a poisoned stream.

**R-11 — Monitor server correctness fixes**
- Files: `monitor_server.py:148-155` (`resolve_task_dir`), `:82,111,126-128,143`
  (token shapes), `:471` (`/file`), `:412-448` (WS upgrade).
- Resolves: MONITORING-5, RUNSTATE-14, MONITORING-12, MONITORING-2
  (legacy-scope half).
- Approach: unknown task ⇒ `None` ⇒ 404 on `/ws` `/tasks` `/artifacts`
  `/file` (never the STATE_DIR fallback); all four `/tasks` paths return the
  four-key token object; `os.stat` before `/file`, 413 above 8 MB, chunked
  below; Origin allowlist at WS upgrade (403 otherwise) per GD-13.
- Test: `test_server.py`: `resolve_task_dir("nope") is None`; 404 on unknown
  `?task=`; token-shape assertion on the empty path; oversize file ⇒ 413;
  cross-origin upgrade ⇒ 403.

**R-12 — Dashboard scalability + link whitelist**
- Files: `monitor.html:391,220-238,390` (render/log), `:473-475` (mdInline).
- Resolves: MONITORING-7, MONITORING-13.
- Approach: coalesce `render()` behind a rAF dirty flag; cap `p.log` at ~500
  rows; tighten the href whitelist to `/^(https?:\/\/|#|\.{0,2}\/(?!\/))/`.
- Test: `test_frontend.py` source guards: rAF pattern present, negative
  lookahead present, `//host` form absent.

**R-13 — Watcher identity, labels, and marker parsing**
- Files: `decision_watcher.py:121-124` (MARKER/STAGE_HINT), `:340-345`,
  `:550,557,636-638,777,787` (ids/labels), `:664-666` (unclassified).
- Resolves: RUNSTATE-3, RUNSTATE-8 (forward half), SKILLS-15, SKILLS-11,
  MONITORING-4 (emit half), PLANS-6 (forward half — this is amended T21;
  T21's "don't touch" clause superseded), AUDIT-9 (watcher half).
- Approach: emit the **full 17-hex agentId** (8-char form only as
  `shortId`); label = `<stage>:<role> #<attempt>` so parallel siblings are
  distinct; parse markers per GD-9 (order-independent kv, window rule,
  `[touch]` `name`/`parent`/`root`/`ledger` into an optional agent identity
  block; `model=`/`phase=` passed through); relax `STAGE_HINT` to tolerate
  the templates' quoting (`status\.sh"?\s+"?\S+?"?\s+"?([\w:-]+)"?\s+running`).
- Test: fixtures — six parallel agents get six distinct labels; the literal
  quoted `statusCmd` string yields the stage; `[touch]`+`[monitor]` adjacent
  lines parse into one record; quoted-marker prose in the body is ignored.

**R-14 — Validate divider/perspective ids in the templates**
- Files: `implement.workflow.js:213-230,263-269`;
  `research.workflow.js:41-45,71`.
- Resolves: SKILLS-4, SKILLS-12, SKILLS-16.
- Approach: enforce `/^sp-[a-z0-9]+(-[a-z0-9]+)*$/` on divider ids; reject
  reserved ids (`orchestrator`, `divide`, `finalgate`, `research`,
  `synthesis`) and reserved stage keys (`plan`, `complete`, `tokens`) for
  perspectives; assert uniqueness; normalize all owned paths
  `path.resolve(REPO, f)` → repo-relative, reject escapes, before the
  one-file-one-owner guard.
- Test: divider fixture with a traversal id ⇒ throws before any spawn;
  `a.py` vs `./a.py` in two sub-plans ⇒ guard throws.

**R-15 — Attempt bookkeeping never loses evidence**
- Files: `implement.workflow.js:105-108,148,172-176,183-196,207-209,289,299-303`.
- Resolves: SKILLS-7, SKILLS-8.
- Approach: `touchedFiles` set unioned across attempts feeds gate, critique
  and loop result; gate derives ground truth from `git status --porcelain`
  and cross-checks the self-report (mismatch = finding); on `!impl ||
  !impl.done` write `${FINDINGS}/${sp.id}-impl-attempt-<N>.md` + push into
  `openFindings` + emit `status.sh <sp.id> implement failed "attempt N: no
  result"`. Stopped-vs-crashed arbitration consults the control audit when it
  exists (phase 4); until then both consume an attempt (recorded).
- Test: two-attempt fixture — attempt-2 critique prompt names both files;
  dead-implementer fixture produces a findings file and a failed event.

**R-16 — Replace tautological tests; add end-to-end watcher coverage**
- Files: `tests/test_watcher.py:147-187,266-275`; `tests/test_server.py`.
- Resolves: MONITORING-10.
- Approach: extract `close_state_for(plan, decisive, last_result_ok)` and
  `should_stale(new, old)` as pure functions and test them for real; one e2e
  test: synthetic `journal.jsonl` + transcripts in a temp WF_DIR, bounded
  main-loop iterations, assert `events.jsonl` lines (spawn → result → plan
  close → run complete). This test also locks R-07/R-08 in.
- Test: is the test.

**R-17 — `monitoring.md` normative refresh**
- Files: `.claude/shared/monitoring/monitoring.md:19,30-46,135-138,158-169,172-191`;
  `tests/test_shell.py:131-151` (matching guards).
- Resolves: MONITORING-14, RUNSTATE-16 (doc half).
- Approach: add a Security posture section (honest: local dev tool, no auth;
  what changes before any write endpoint — GD-13); `detail` cap + rationale
  in the schema; add `watcher` to reserved stages; refresh the `/tasks` row;
  document the truncation-rebuild behaviour (R-07) and the terminal-event
  contract (GD-10); state the GD-9 marker grammar.
- Test: extend the existing static guards for the new strings.

### Phase 2 — protocol: topology, restart, touch-orchestrate

---

**R-18 — Marker emission fixed at the source; grammar mirrored into docs**
- Files: `research.workflow.js:66-67` (leading newline),
  `implement.workflow.js` prompt sites (~10), a shared `markerLine()`/
  `touchName()` helper per template; `touch-orchestrate/SKILL.md:39-48`;
  `m-orchestrator/SKILL.md:55-56,66-67`.
- Resolves: PLANS-10 (template half), AUDIT-9 (spec half), SKILLS-13 (marker
  emission half), SKILLS-11 (doc half).
- Approach: templates emit the marker as the true first line (no leading
  `\n`); one `markerLine()` builds `[monitor] plan= stage= role= attempt=
  model= phase=` and (when touch-naming) the `[touch] name= parent= root=
  ledger=` line above it; SKILL.md "FIRST line" wording amended per GD-9;
  m-orchestrator's stage-omission shortcut either deleted or documented with
  the quoting-tolerant rule.
- Test: static guard over `.claude/skills/**/templates/*.workflow.js` — no
  template literal opens with a newline before `[monitor]`/`[touch]`; both
  markers present at every prompt site.

**R-19 — Durable topology and restart re-entry**
- Files: `implement.workflow.js:32-37,248-269`;
  `research.workflow.js:41-45,139-152`; both SKILL.md Procedure sections;
  `m-orchestrator/SKILL.md:40-45`.
- Resolves: SKILLS-5, SKILLS-6, MONITORING-4 (schema half), PRODUCT-4
  (restart half; GD-4).
- Approach: templates write `<task-dir>/state/topology.json` when the
  deterministic list is known (research: before fan-out; implement: right
  after Divide) — `{task, strategy, max_attempts, phases[], plans:[{id,
  title, kind, files[], finding_ids[], expected_stages[]}]}`; Divide also
  writes `plan/<name>-subplans.json`; immediately seed
  `ORCH_TITLE="<title>" status.sh <id> plan queued` per plan (fixes
  m-orchestrator's seeding contract — its SKILL.md amended to "seed after the
  partition is known"); args gain `{subplans_file?, only?: string[],
  from_attempt?: number}` (implement) and `{only?}` (research):
  `subplans_file` skips Divide and reuses stored ids verbatim. Restart (GD-4)
  = re-invoke with `{subplans_file, only:[id]}`; document in both SKILL.md.
- Test: Divide phase against a stub divider ⇒ `topology.json` matches the
  partition; run with stored partition + `only:["sp-b"]` ⇒ no divider spawn,
  exactly one loop.

**R-20 — touch-orchestrate: two profiles, addressable control file, conformance test**
- Files: `touch-orchestrate/SKILL.md:37-83,95-96`; binding sentences in
  `execute-research/SKILL.md:36-38` and `implement-plan/SKILL.md:27-30`;
  new `tests/test_touch_standard.py`.
- Resolves: SKILLS-13, SKILLS-14, MONITORING-3, PLANS-7, PLANS-14 (aliases:
  prior SKILLS-1/-2/-7/-9, prior SKILLS-3/-4/-5/-10/-12, V0TASK-4/-5/-6 —
  register in R-06).
- Approach: per GD-8 — two explicit profiles (Agent-tool: background +
  `taskId` + `TaskStop`; Workflow: marker-only identity, ledger line with
  `"taskId": null` + `{wfRunId, wfKey, plan, stage}`, stop declared
  unavailable and rendered disabled with the reason). Control addressing per
  PLANS-7: control file is per-session and aggregator-owned
  (`<TOUCH_STATE_DIR>/sessions/<pid>-<procStart>/control.jsonl`); the skill
  writes one registration line into the ledger and carries `ledger=<abspath>`
  in its `[touch]` marker; the relative-path fallback ("`.touch/` else
  `<task-dir>`") is deleted. Ack vocabulary `stopped|not_found|already_done`;
  intent state "pending — orchestrator busy" replaces bare `expired` while
  the driver is blocked. Templates reconcile every null `agent()` result
  against the control/ack log before labelling (abort the research barrier or
  return `partial:true` rather than silently synthesizing on N-1).
- Test: `test_touch_standard.py` conformance on shared fixtures — the marker
  parser accepts the documented marker string; the ledger reader accepts the
  documented JSON line; the ack reducer accepts all three results; the
  unnamed-legacy-agent fixture (agent with only `description`) renders
  flagged, not invisible.

**R-21 — Apply the role→model table**
- Files: `research.workflow.js:141-152`; `implement.workflow.js:256-259,333-335`;
  `CLAUDE.md` (table, via R-05 coordination).
- Resolves: PRODUCT-6.
- Approach: per GD-5 — Opus 5 @ xhigh for research/impl/gate/critic; Fable
  for synthesizer/divider/final review; the pin recorded in CLAUDE.md so it
  survives context resets.
- Test: static guard — templates contain no model pin outside the GD-5 set.

### Phase 3 — Touch v0 (monitoring-only; NO control verbs)

---

**R-22 — Scaffold**
- Files: new `aggregator/__init__.py`, `touch-visual/` skeleton,
  `tests/run_all.sh`; NO `.gitignore` edit (done in R-01 — PLANS-13's
  duplicate-line trap avoided).
- Resolves: P1/T1 (amended), PLANS-13.
- Approach: layout per GD-15; stdlib-only (statusline's `jq` is the recorded
  exception); every file marked new/changed against the re-checked tree.
- Test: `run_all.sh` runs the (empty) suite green; imports resolve.

**R-23 — `aggregator/tailer.py`**
- Files: new `aggregator/tailer.py`, `tests/test_tailer.py`.
- Resolves: P4/T1-util; RUNSTATE-15 (Touch half), MONITORING-9 (do-not-inherit).
- Approach: copy the monitor's torn-tail semantics verbatim (GD-20);
  checkpoint identity `(st_dev, st_ino, size, offset)` per D6 — inode change
  or `size < offset` ⇒ full idempotent re-ingest from 0; reads are
  incremental (offset + partial state), never full re-parse per tick.
- Test: P4's fixture list + truncation/rotation cases + torn tail.

**R-24 — `aggregator/store.py` (touch-events-v2)**
- Files: new `aggregator/store.py`, `tests/test_store.py`.
- Resolves: P2/T5; PLANS-3, PLANS-11 (GD-11 shapes).
- Approach: single writer per stream; `seq` per event-log file, resumes from
  line count at boot; ref union open-tail validator (reject malformed known
  shapes, retain unknown shapes); one ts format `…Z`; token records always
  four keys; `flock` on append (GD-20 do-not-inherit unlocked appends).
- Test: ref validation both arms (malformed 17-hex rejected; unknown shape
  retained); two streams legally holding the same seq; `(stream, seq)` cursor
  round-trip; torn-tail write recovery.

**R-25 — `aggregator/sessions.py` (discovery + registry)**
- Files: new `aggregator/sessions.py`, `tests/test_sessions.py`.
- Resolves: P3/T6; AUDIT-17.
- Approach: live sessions keyed `(pid, procStart)` (`/proc/<pid>/stat` field
  22); **historical arm**: sessions keyed `sessionId`, `liveness:
  historical`, discovered from `projects/*/*.jsonl`, may be fragments
  (`/clear` splits), carry no controls; reconciliation when a registry entry
  names a sessionId; tolerate `lost+found` and zero-byte registry files
  (both on this machine).
- Test: injectable fake `/proc` + registry; the 1-registry-entry vs
  6-transcripts case; `lost+found` fixture.

**R-26 — `aggregator/ingest.py` (harness transcripts, journals, snapshots)**
- Files: new `aggregator/ingest.py`, `tests/test_ingest.py`.
- Resolves: P5/T7/T8 (read side); AUDIT-2, AUDIT-4, AUDIT-5, AUDIT-6,
  AUDIT-13 (rollup half), MONITORING-9 (dedup inheritance).
- Approach: message-id token dedup across rotated copies (copy verbatim —
  GD-20); `result` polymorphic per GD-11; persisted-output detection = parse
  `tool_result` content for `^<persisted-output>` +
  `Full output saved to: (?P<path>\S+)` — `toolUseResult.persistedOutputPath`
  does not exist (0 records on disk); the recorded path is agent-authored
  text — realpath-contain under `~/.claude/projects/*/*/tool-results/` only;
  snapshot resolved by `glob(~/.claude/projects/*/*/workflows/<runId>.json)`
  (never the launching session — it lands under the session current at run
  END); back-fill filters `workflowProgress` on `type=="workflow_agent"`,
  keys by `agentId` never `index`, never overwrites observed values with
  null, ingests `phases[]`; run tokens = Σ deduped per-node;
  `totalTokens` never substituted.
- Test: against the R-03 fixtures — the two `workflow_phase` null rows are
  ignored and all seven labels survive; the persisted-output regex fires on
  the three real spill files; snapshot found despite cross-session split;
  both `result` arms; run rollup ≈ the deduped figure, not 1,089,990.

**R-27 — `aggregator/legacy.py` (legacy `events.jsonl` adapter)** *(new item —
absent from both prior plans)*
- Files: new `aggregator/legacy.py`, `tests/test_legacy.py`.
- Resolves: RUNSTATE-2, RUNSTATE-3, RUNSTATE-4, RUNSTATE-5, RUNSTATE-6,
  RUNSTATE-7, RUNSTATE-8, RUNSTATE-9, RUNSTATE-12, RUNSTATE-13, AUDIT-10,
  PLANS-5, PLANS-6 (legacy half).
- Approach: the full GD-14 rule set — synthesized `runId`/`taskId`/`ordinal`;
  ts normalization + line-order seq; two-writer dedup; token folding;
  re-labels marked `derived_from_legacy:true`; never read
  `.watcher-state.json`; plan-only folders as their own kind; derived
  archive label (stat the configured `wf_dir`; three states); legacy agent
  ref `legacy:<task>:<id8>` exempt from the 17-hex validator, label derived
  from the event's top-level `stage`.
- Test: fixtures are verbatim real lines (R-03): the two-wave respawn yields
  distinct ordinals; `plan|failed "loop exited -> synthesis"` renders
  "closed — no verdict"; `touch-repo-recon`'s 7 phantom running agents close
  stale from the terminal complete event; duplicate stage terminals dedupe
  to one record keeping `agentDetail`; line/size bound asserted (token fold).

**R-28 — `aggregator/agents.py` (node/graph join)**
- Files: new `aggregator/agents.py`, `tests/test_agents.py`.
- Resolves: P6 (amended per GD-7), AUDIT-11, MONITORING-4 (consumer half).
- Approach: harness facts create nodes (`(runId,key,ordinal)` / full
  agentId); marker layer (GD-9 parser shared with R-13's rules) adds
  `name/parent/root` edges and labels; unnamed agents get `agentId` display
  + `unconventional` flag (the common case today); legacy join per GD-7.
- Test: adversarial marker fixture (line-1 `[touch]`, line-2 `[monitor]`,
  quoted markers in body); node exists with no marker at all; parent edges
  from `parent=`.

**R-29 — `aggregator/ws.py` (codec)**
- Files: new `aggregator/ws.py`, `tests/test_ws.py`.
- Resolves: T3 (kept separate per GD-15).
- Approach: pure-function frame encode/decode; RFC 6455 vectors.
- Test: RFC vectors, fragmented frames, masked client frames dropped unread
  (matching `monitor_server.py:279-310` behaviour).

**R-30 — `aggregator/server.py` (routes + auth posture)**
- Files: new `aggregator/server.py`, `tests/test_server_core.py`.
- Resolves: P8/T4/T12; MONITORING-2 (Touch half — GD-13), MONITORING-5
  (no-fallback inheritance).
- Approach: GD-13 in full — 127.0.0.1 default, opt-in `0.0.0.0`, per-boot
  token everywhere but `/health` (`hmac.compare_digest`), Origin/Host
  allowlist at WS upgrade, read-only vs control route groups; static
  `(method, route)` dict, default 404; `safe_artifact_path`-style containment
  + CSP sandbox + nosniff on any served file (copied verbatim); `/health`
  reports per-tailer liveness and parse-failure counters (AUDIT-15's rule:
  a tailer whose target is gone exits, never polls forever).
- Test: no-token ⇒ 401 on every route but `/health`; cross-origin WS ⇒ 403;
  unknown route/id ⇒ 404 (no fallback); a path segment after a registered
  route 404s.

**R-31 — Read API**
- Files: `aggregator/server.py` (routes), new `tests/test_api.py`.
- Resolves: P9/T11 (amended per GD-12), PLANS-4, PLANS-11 (API half).
- Approach: query-string endpoints only:
  `/api/sessions`, `/api/session/timeline?session=&since=`,
  `/api/events?session=&after=`, `/api/run/graph?run=`,
  `/api/run/node?run=&agent=`, `/api/toolresult?id=`, `/api/tasks` (legacy
  folders). Ids regex-validated by one shared helper; cursors are
  `(stream, seq)`.
- Test: unknown session/run/id ⇒ 404; a bare `after=` without a stream
  selector ⇒ 400; pagination round-trip without duplicates.

**R-32 — `touch-visual/` v0 frontend (read-only)**
- Files: new `touch-visual/index.html`, `app.js`, `style.css`,
  `tests/test_touch_frontend.py` (source guards).
- Resolves: P10 (amended — NO Stop button in this phase), T16; MONITORING-7
  (do-not-inherit), D13.
- Approach: sidebar (sessions incl. historical + legacy task folders per
  GD-14 kinds), per-session agent tree keyed per GD-7, token rollups from
  the computed sums; escape-first rendering (GD-20); render coalescing + capped
  log from day one; every degraded/derived state labelled (dashed provenance,
  `derived_from_legacy`, "closed — no verdict"); no control affordance of any
  kind renders in v0.
- Test: source guards mirroring test_frontend.py genre — escape-first
  pattern present, no `innerHTML` on event text, no control verb strings.

**R-33 — Docs for running Touch**
- Files: `README.md` (run section), new `docs/control-semantics.md`
  (verb ladder per GD-4, session classes per GD-6, forward-looking).
- Resolves: P12 (doc half), T23, PLANS-13 (doc target decision GD-3).
- Approach: one README (GD-3); serve/publish instructions per GD-13
  (token, ports reserved, `sbx ports` flow).
- Test: R-05's static guard extended: README contains the verb table and no
  "pause" promise without its status.

### Phase 4 — control plane (**blocked on R-04 probe results; do not sequence before**)

---

**R-34 — Control plumbing: session classes + addressable control files**
- Files: new `aggregator/control.py`, `tests/test_control.py`;
  `aggregator/sessions.py` (class field); coordinated SKILL.md edits landed
  in R-20.
- Resolves: PLANS-2, PLANS-7 (implementation half), G6/P7 (amended).
- Approach: GD-6 classes with the evidence rule; per-session control file
  `<TOUCH_STATE_DIR>/sessions/<pid>-<procStart>/control.jsonl`; intent state
  machine `requested / pending — orchestrator busy / sent / confirmed /
  failed(reason)`; non-cooperating sessions: every control 403s server-side.
- Test: promotion only on observed evidence (ledger registration line or
  live `[touch]` marker); intent on a blocked driver renders pending, not
  expired; 403 on observed-class control.

**R-35 — Stop verb + three-state checkpoint**
- Files: `aggregator/control.py`; T14's checkpoint logic.
- Resolves: T14 (amended), AUDIT-8, PLANS-9 (merged — probe this run:
  `git stash create` on zero commits exits **1**, no sha; empty output on a
  clean tree is ambiguous), P7.
- Approach: checkpoint result is `{sha | none | unavailable, reason}` —
  `unavailable` when `git rev-parse HEAD` fails / not a work tree /
  `stash create` non-zero or empty output; always also capture `git status
  --porcelain` (works with zero commits); never block the verb on the
  checkpoint; UI renders "no checkpoint — <reason>" (D13).
- Test: three arms against throwaway repos incl. the zero-commit one; stop
  on an Agent-tool-profile agent issues TaskStop via the cooperating model;
  stop on a Workflow-profile agent renders disabled with reason (GD-8).

**R-36 — Hook pack (delivery decided by the R-04 probe)**
- Files: new `aggregator/hooks/touch-hook.sh`, `tests/test_hooks.py`;
  `.claude/settings.json` (additive edit; `"timeout": 5`, opt-in default
  off — it is committed, session-wide config).
- Resolves: P11/T10 (amended), PLANS-8, AUDIT-12 (probes 1–2), PLANS-12
  (only if the gate is promoted: hook reads the token at invocation from
  `.touch/server.json`, mode 0600, path passed as a literal argument —
  D5's "token fingerprint" wording corrected to "token").
- Approach: per GD-19 branch — hot-reload works ⇒ hooks installable for
  observed sessions and the deterministic stop lands here; else the minimal
  owned-session spawner slice (T9 subset) is pulled forward and hooks ride
  `--settings` at spawn. Never ship this item while its delivery path is
  unverified.
- Test: hook fires in the delivery mode the probe validated; settings edit
  is additive and reversible.

**R-37 — End-to-end simulation + per-phase acceptance**
- Files: new `tests/test_e2e_sim.py` (P12 amended).
- Resolves: P12, Part F (restated per GD-18), PLANS-6 (acceptance half),
  AUDIT-7 (acceptance half).
- Acceptance, per phase, all against fixtures:
  - **Phase 1**: the R-16 e2e watcher test is green; replaying the
    `touch-aggregator` fixture stream through the fixed watcher rules yields
    no `failed` verdict for the research plan.
  - **Phase 3**: the wf_829e6f58 fixture renders six **distinctly labelled**
    researcher nodes via the full path (labels from meta + markers), correct
    deduped rollups, three-state liveness; the legacy path renders
    `touch-repo-recon` with stale-closed agents and "closed — no verdict";
    the live smoke check against `~/.claude` is manual, not acceptance.
  - **Phase 4**: one simulated cooperating session round-trips
    request→ack→stopped; one observed session proves the 403.

---

## 3. Findings disposition — this run

### Merged (same defect from multiple perspectives; both ids kept as aliases)
- PRODUCT-2 ≡ AUDIT-14 ≡ RUNSTATE-10 (gitignore/.touch) → R-01
- PRODUCT-3 ≡ RUNSTATE-11 (zero commits) → R-02
- PRODUCT-5 ≡ RUNSTATE-1 (omnigent falsehood; + PLANS-5's CLAUDE.md half) → R-05
- PRODUCT-7 ≡ SKILLS-1 ≡ RUNSTATE-4 (success recorded failed; forward fix
  R-08/R-09, legacy fix R-27 via AUDIT-10)
- PRODUCT-7 ≡ SKILLS-2 (no terminal complete event) → R-08/R-09
- PLANS-9 ≡ AUDIT-8 (stash checkpoint) → R-35
- PLANS-10 ≡ AUDIT-9 (marker anchoring/grammar) → GD-9, R-13, R-18
- PLANS-8 ≡ AUDIT-3 (hook hot-reload unprobed) → R-04, GD-19, R-36
- MONITORING-4 ≡ SKILLS-5 (no topology/graph model) → GD-7, R-19
- PRODUCT-4(restart) ≡ SKILLS-6 (restart inexpressible/ambiguous) → GD-4, R-19
- PLANS-6 ≡ AUDIT-7 (Part F unreachable / fixture on deletion clock) → R-03, GD-18, R-37
- RUNSTATE-3 ≡ MONITORING-4(id-emit) ≡ PLANS-6(17-hex) (truncated ids) → R-13, R-27
- PRODUCT-12 ≡ AUDIT-15 (daemon lifecycle) → R-05, R-30(health rule)
- MONITORING-8 ≡ RUNSTATE-15 (truncation stall) → R-07, R-23
- MONITORING-3 ≡ SKILLS-13 ≡ SKILLS-14 (event source vs stoppability — one
  decision, three aspects) → GD-8, R-20

### Discarded / corrected (with justification)
1. **PLANS-9's "exit 0" sub-claim** — falsified by probe this run (`git stash
   create` exits 1 on zero commits, with and without staged files); the
   finding's substance stands in R-35.
2. **PRODUCT-7's token sub-claim** ("total far larger", from the synthesizer's
   last event line) — superseded by AUDIT-13's provenance-carrying deduped
   measurement (≈29.5M in / 316k out); R-05 uses that number.
3. **Token-gating the legacy monitor** (MONITORING-2 option) — declined: it
   stays a read-only dev tool; it gets only the Origin allowlist (R-11). The
   full posture applies to Touch (GD-13).
4. **RUNSTATE-16's "reject out-of-enum states in status.sh"** — declined as
   rejection; warn-only adopted (a best-effort writer must never break an
   agent); the v2 writer does validate (R-24).
5. **PRODUCT-9's gzip growth policy** — declined for now; explicit policy in
   GD-16 (revisit at ~20 MB).
6. **`Workflow({resumeFromRunId})` as "restart"** (inception.md:159-163) —
   rejected: it replays without re-executing (SKILLS-6); GD-4 defines the one
   meaning.
7. **AUDIT-12 probe 3 (vendoring)** — reclassified from experiment to action:
   it belongs to deferred T2 (run the vendor step, commit artifacts), not to
   R-04.
8. **PRODUCT-13** (absolute paths in history) — not an item: history is a
   record; the only actionable part (templates derive REPO from cwd/env) is
   noted in R-05's doc line and satisfied by existing templates.

### Prior corpora
The 51 `touch-repo-recon` and 110 `touch-aggregator` findings are disposed via
GD-17 / R-06; the load-bearing ones already surface here through their
re-verifying aliases (prior SKILLS-1/-2/-7/-9 → R-20; V0TASK-4/-5/-6 → R-20/
R-34; INTENT-6 → R-01; INTENT-13 → superseded by GD-7; INTENT-14 → GD-11/R-05).

---

*37 items (R-01…R-37). Phase 0 must complete before any other phase; phase 4
is blocked on R-04's probe results. Hand this file — alone — to
`implement-plan`.*
