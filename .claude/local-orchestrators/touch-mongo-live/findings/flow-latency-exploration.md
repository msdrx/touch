# Flow-latency exploration — where the implement→test→critique loop spends its time

Analysis date: 2026-07-27 (UTC). Sources: `events.jsonl` (11,992 events,
2026-07-25 14:31 → 2026-07-27 08:27 UTC), `.watcher-state.json`, the
`wf_93250ff2-ddb` journal + agent transcripts, the per-attempt findings files,
and `orch-scripts/implement.workflow.js`. Read-only exploration; nothing was
changed. A continue-run was live during the analysis (sp-refs-mongostore
attempt 5), so late numbers are slightly conservative.

Method notes: "active" time = span of watcher token-movement ticks per agent
(first→last tick where cumulative tokens grew); "idle" = tracked-running time
with no token movement; "dead air" = wall intervals with no token movement in
ANY agent. Attempt numbers parsed from the `#N` in watcher labels.

## 1. Where the ~42 wall-hours went

| bucket | hours | notes |
|---|---|---|
| implementer active | 17.5 | 62 agent-runs, avg ~29 min each |
| critique active | 8.7 | 46 runs, avg ~13 min |
| test-gate active | 3.2 | 49 runs, avg ~8 min |
| research + synthesis | 1.2 | the research pass, fine |
| dead air (no tokens moving anywhere) | 14.2 | see §4 |
| idle inside "running" agents | ~14 (overlaps dead air) | stale/hung agents, mostly network-retry ghosts |

## 2. Retries are the #1 cost — and the critique gate is the retry engine

Active gated-loop time split by attempt number:

- attempt 1: **11.05 h** (54 agents)
- attempts ≥2: **18.05 h** (94 agents) — **62%** of loop time

Gate outcomes across the run: test gate failed **1** time in ~49 runs;
critique rejected **~70** times. Five sub-plans (mirror-deploy, agents-reducer,
custom-state, server-api, frontend) went 4-for-4 critique rejections and each
got an extra attempt.

Two distinct rejection populations (verified by reading the critique files):

- **Real blockers from spec ambiguity.** e.g. sp-agents-reducer attempt 4: the
  reducer read `runs.endedAt` as "harness said run ended" while `ingest`
  derives it from transcript activity — a genuine cross-module semantic bug the
  sub-plan spec never pinned. This class is *discovered at critique time* but
  is *created at divide/plan time*.
- **Major-severity churn with zero blockers.** sp-frontend was rejected 4×
  with **no blocker ever** — 1–2 "majors" per round against the
  `approved=true ONLY with zero blocker/major` bar, from a reviewer prompted
  to "Try hard to REJECT", reviewing a moving target (each fresh implementer
  re-touches things, presenting new major-grade surface each round).

Each rejection buys the most expensive unit in the system: a brand-new
implementer (~29 min, ~20M input tokens) that re-reads everything.

## 3. Inside one implementer: latency is API turns, not tools

Dissection of `sp-watcher-templates-firstwave` impl #5 (24.4 min transcript):

- **180 assistant/API turns**; 21.0 min waiting on model round-trips vs
  3.4 min executing tools. Median turn 4.3 s, p90 14.1 s, max 101 s.
- Tool profile: 55 Bash, 20 Read, **41 Edit**, 1 StructuredOutput — the
  one-small-edit-per-turn pattern. New multi-thousand-line files get built by
  dozens of sequential Edits, each paying a full round trip on a context that
  ends at ~220k tokens.
- Token accounting: ~20M cumulative input, **98% cache reads** — cheap per
  token, but the turn count is the wall-clock multiplier.

Reading burden per fresh agent: the prompts mandate reading the amendment plan
+ base plan + subplans file **in full** — 2,351 lines / ~148 KB — plus research
findings, plus ALL accumulated gate/critique findings files (`openFindings`
never prunes; late attempts carry up to 8 files, largely about already-closed
items the implementer must re-verify against the tree).

## 4. Dead air: ~14.2 h with no tokens moving anywhere

Top intervals (UTC):

- **6.5 h** 27th 00:30→07:02 — run paused overnight awaiting human resume.
- **2.1 h** 26th 06:37→08:44 and **1.9 h** 26th 09:30→11:23 — in-run stalls.
  The second is the sp-mirror-deploy cluster: `agentR` network retries (the
  "mobile uplink" guard) spawned agents that hung with zero token movement
  until the run was manually killed ~2 h later.
- ~30 more minutes across 32 smaller gaps (driver/inter-stage overhead is
  small — the loop itself is tight).

Nothing alerted during any of these. The monitoring answer (§6) matters more
than any per-turn micro-optimization for this bucket.

## 5. Optimization directions (serial sub-plan order preserved)

Ranked by measured prize:

1. **Cut retry volume (~18 h pool).**
   - *Verdict semantics:* split REJECT into `reject-blocker` (full fresh loop,
     as today) vs `reject-major-only` (a targeted FIXER agent scoped to the
     listed findings — still a brand-new agent, but with a narrow prompt and
     no full plan re-read; ~10 min instead of ~50 min cycle). sp-frontend-class
     churn (4 loops, zero blockers) collapses under this rule.
   - *Spec pre-gate:* one cheap read-only "ambiguity hunt" per sub-plan before
     impl #1, hunting exactly the killer class: cross-module semantic contracts
     the section leaves undefined (the `endedAt` bug). Findings feed impl #1's
     prompt as pinned decisions.
   - *Findings hygiene:* critiques already re-verify prior findings; make each
     critique emit a `still-open` delta and hand attempt N **only that**,
     instead of the ever-growing file pile.
2. **Cheapen/parallelize the gates (~7 h + part of critique).**
   - The test gate failed once in ~49 runs and critiques re-run the suites
     independently anyway. Either (a) make suite-running deterministic — a
     daemon/script in the cycle_reporter mold runs the suites and writes the
     findings file, LLM only interprets on red; or (b) run test + critique
     **concurrently within an attempt** (both read-only, different findings
     files; only the prompt-embedded `gate.summary` goes — critique reproduces
     those numbers itself anyway). Sub-plan serialism is untouched.
   - Trim the gate's step 3 (semantic plan-verification) — it duplicates the
     critique's job; keep the gate mechanical (suites + scope audit).
3. **Reduce implementer turn count (~17.5 h pool, maybe 30–40% of it).**
   - Prompt directives: batch independent Reads/Bash in one turn; `Write`
     whole new files instead of dozens of incremental `Edit`s; batch edits.
     41 Edits × ~7 s round trip on a new untracked file is pure ceremony.
   - Have the divider embed each sub-plan's verbatim item texts + relevant GDs
     in the subplans file (it already writes one), so fresh agents stop
     re-reading 148 KB of full plans. Keep the full plans cited for the
     critique to check drift against.
4. **Kill dead air (~10 h of the 14.2).**
   - Stall watchdog in `decision_watcher`: no token movement for N min on a
     tracked-running agent → `stalled` event + push notification. The 2 h
     mirror-deploy hang becomes a 5-minute alert.
   - Terminal/blocked-state notifications so overnight human-gated resumes
     don't sleep 6.5 h.
5. **Reconcile the effort ladder.** The script already runs impl at
   high→xhigh(attempt≥3), gate at medium, critique at high — a de-escalation
   from GD-5's blanket xhigh that appears to have cost nothing (the one test
   failure was real and caught). Codify rather than drift.

## 5b. Defect found while exploring: re-opened loops keep a stale "running" status

Observed live on 2026-07-27: `sp-watcher-templates-firstwave` and
`sp-refs-mongostore` both closed decisively at their raised cap (attempt 5
critique rejections, majors only) yet still show `running` on the dashboard.

Mechanism: (1) the continue-run's re-opened loop emits
`plan/running — "loop continues: critique attempt N spawned"` on the plan's
own stream; (2) the terminal `plan done|failed` close is cycle_reporter's job,
but its `.cycle-reporter-state.json` `emitted` set is keyed by **plan name
alone** and already contains every plan from the first run's closures — the
`extra_attempts` cap raise re-opens the loop, but nothing re-arms the
once-only close emitter; (3) the watcher records the real verdict
(`attempts exhausted → plan FAILED`) under the `orchestrator` stream
(`orchestrator/<sp>/failed`), which does not update the sub-plan's status.

Same family as R-58 (status integrity at loop close), inverse polarity: R-58
was a fabricated FAILED; this is a missing real verdict. Candidate fix: key
`emitted` by `(plan, cap)` — the cap is already re-read every poll, so a
re-open naturally re-arms exactly one more close event. Not fixed in this
pass (exploration only).

## 6. Granular monitoring — all feasible from data already on disk

`decision_watcher` already tails `agent-<id>.jsonl` transcripts for usage; the
same lines carry **every tool_use block with timestamps**. Extensions, all
stdlib, all read-only on `~/.claude`:

- **Live activity line per agent:** last tool + target (`Edit
  aggregator/agents.py`, `Bash python3 tests/test_x.py`), turn counter,
  context size. Emit as a new low-rate event kind; render beside the token
  line in monitor.html.
- **Stall/health badges:** token-stall timer, tool-error streaks, "agent
  returned null" retry markers surfaced instead of silent ghost agents.
- **Per-stage timers with history:** cycle_reporter already renders per-cycle
  HTML; add attempt duration vs. run median so a 2 h implement shows red at
  a glance.
- **Turn-latency histogram per agent** (API wait vs tool wait, from transcript
  timestamps) — makes the 180-turn pathology visible live instead of in a
  post-mortem.
