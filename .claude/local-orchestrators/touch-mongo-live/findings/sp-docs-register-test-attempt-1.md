# sp-docs-register — test gate, attempt 1 — PASS

Read-only gate. No source or test file was edited.

## 1. Targeted suites (owned by sp-docs-register) — 100% green

Run from the repo root, stdlib only, no services, no `TOUCH_MONGO_URI`:

| suite | rc | evidence |
|---|---|---|
| `python3 tests/test_register.py` | 0 | 7 test functions, ~20 `ok:` lines, `all findings-register tests passed` |
| `python3 tests/test_docs.py` | 0 | 15 test functions, ~70 `ok:` lines, `all documentation guards passed` |

Highlights that map straight onto the sub-plan's items:

* **R-06 / GD-17 (register):** the corpus is globbed off disk
  (`ORCH.glob("*/findings/research-*.md")`, 344 findings) and diffed against the
  parsed register rows (344 rows) three ways — `missing: []`, `dupes: []`,
  `phantom: []`. The R-58 alias `SKILLS-1 ≡ RUNSTATE-4 ≡ PRODUCT-7` is asserted
  present on one line and to name R-58; the `touch-repo-recon` vs
  `touch-full-recon` SKILLS-1 namespace collision is asserted to be two rows
  with different dispositions. The five touch-mongo-live reports are covered.
* **R-38 (anchor repair):** `test_plan_d8_is_split` asserts the plan labels
  D8.1/D8.2 and that `inception.md` uses the split labels;
  `test_d8_is_never_cited_bare` asserts no register disposition cites a bare D8;
  `test_inception_usage_correction` asserts the "copied onto every split record"
  wording is gone and replaced by the running-counter + `$max` fold.
* **R-05 (docs truth pass):** `omnigent` absent from CLAUDE.md/inception.md as a
  fact claim; true inventory names `aggregator/`, `touch-visual/`, `tests/`,
  `docs/`, `.claude/settings.json`, `statusline.sh`, `jq`; both serve blocks
  labelled (legacy 8931 / Touch 8932) with the ports stated *reserved*; deduped
  token figure ≈29.5M in / 316k out with its source named.
* **R-40:docs / SD-5:** the "when a run ends, stop its watcher" rule is in
  CLAUDE.md and GD-1's scoped commit gate is asserted in *both* CLAUDE.md and
  the plan file.
* **R-33:** README verb table asserted row-by-row (start/terminate/stop/restart/
  pause), deterministic vs model-mediated split, and — the non-tautological one
  — `test_readme_pause_is_always_qualified` scans every *unquoted* mention of
  "pause" and requires a status qualifier (`bad: []`), while still requiring the
  verbatim original intent to survive, labelled as a quote.
  `docs/control-semantics.md` is asserted to define all three GD-6 session
  classes, cover all five verbs, distinguish run-level from per-agent stop, name
  the Workflow run-level stop handle, and record the observed-session 403.
* **R-57:docs:** the per-session-collection ask appears in the user's own words
  in README, is marked declined, and names the indexed-field replacement;
  CLAUDE.md names the GD-21 pymongo exception with the pinned version and both
  allowed importers (`mongo_store.py`, `mirror.py`);
  `test_no_published_mongo_port` sweeps README/CLAUDE/inception/docs/mongo.md and
  requires every `sbx ports … 27017` mention to be a prohibition and every
  `0.0.0.0` database example to be forbidden, plus the R-42 loopback recipe.
* **R-04 (probes):** `probes.md` exists and is asserted to carry the run date,
  the verbatim commands, the pinned CLI version, and each individual result
  (hot-reload, `agents --json`, `run_in_background`, pymongo, 40573,
  `$jsonSchema`).

Non-tautology check: both files assert against **real repo state** —
`REPO = Path(__file__).resolve().parents[1]` then `path.read_text(...)` for the
doc guards, and a disk glob compared against the parsed register for R-06. No
fixture is written by the test and then read back.

## 2. Full-suite regression gate — no NEW failure

```
cd /home/laniakea/Projects/touch && rc=0
for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done
for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc
```

Bare environment: no daemons started, no `TOUCH_MONGO_URI`, no mongod. Result —
26 of 28 files green, 2 red:

* Monitoring module (baseline, all green): `test_frontend`, `test_server`,
  `test_shell`, `test_watcher`.
* Repo suites green: `test_agents`, `test_api`, `test_bootstrap`,
  `test_custom_state`, `test_docs`, `test_e2e_sim`, `test_fixtures`,
  `test_ingest`, `test_legacy`, `test_mongo_deploy`, `test_mongo_store`,
  `test_reducer`, `test_refs`, `test_register`, `test_server_core`,
  `test_slots`, `test_stdlib_only`, `test_store`, `test_tailer`,
  `test_touch_frontend`, `test_usage`, `test_ws`.
* Mongo-dependent arms skipped cleanly (GD-21 / R-56 no-mongod arm), e.g.
  `test_live_mongod_arm` → `SKIP: live mirror arm: TOUCH_MONGO_URI is not set
  (R-42's loopback+auth recipe)`. A skip does not set rc.

### The two reds are pre-existing baseline, not attributable to this change

`tests/test_mirror.py` — rc 1, `FAILED (3)`:
* `…proven by the call count: the held ticks made no attempt`
* `the first generation lands`
* `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`

`tests/test_sessions.py` — rc 1, `FAILED (1)`:
* `wipe + --rebuild reproduces a byte-identical fingerprint`

Attribution evidence:

1. **Identical failure set, already on record.** The exact same four assertion
   strings are recorded as the known baseline in earlier gates of this same run —
   `findings/sp-frontend-test-attempt-4.md` §"Why the two reds are NOT
   attributable to this change" lists them verbatim, as do
   `sp-server-api-test-attempt-{2,4}.md`, `sp-legacy-arm-test-attempt-1.md`,
   `sp-e2e-acceptance-test-attempt-1.md`, `sp-custom-state-test-attempt-{2,4}.md`
   and `sp-ingest-pipelines-test-attempt-1.md`. Nothing new appeared and nothing
   previously green went red.
2. **Ownership.** The failures live in `mirror.py` (sp-06 mirror-deploy, the
   interrupted loop) and `sessions.py` (sp-07 sessions-arm) — neither file is
   owned or touched by sp-docs-register. They are drainer call-count and
   rebuild-fingerprint behaviours; no Markdown doc, no `test_docs.py`, and no
   `test_register.py` can influence them.
3. **Causal impossibility.** The implementer's whole change-set is Markdown docs
   plus two new standalone test files. Neither new test file is imported by any
   other suite (each repo test is a standalone executable), so the two reds
   cannot be a side effect of adding them.

Per the gate's own rule ("baseline failures do not fail the gate; any OTHER
failure is NEW and fails it"), the regression gate is **satisfied**.

## 3. Spec conformance

Checked against `plan/touch-mongo-live-subplans.md` §"sp-15 — docs-register"
and the matching items in the amendment plan + base plan.

Owned files, all present and non-trivial:

| file | lines | status |
|---|---|---|
| `README.md` | 156 | modified (+163/−… in diff) |
| `CLAUDE.md` | 235 | modified |
| `inception.md` | 346 | modified |
| `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md` | — | modified (+45) |
| `.claude/local-orchestrators/touch-full-recon/report/probes.md` | 249 | NEW |
| `.claude/local-orchestrators/touch-full-recon/plan/findings-register.md` | 531 | NEW |
| `docs/control-semantics.md` | 153 | NEW |
| `tests/test_register.py` | 202 | NEW |
| `tests/test_docs.py` | 293 | NEW |

Items R-04, R-05, R-06, R-38, R-33, R-40:docs, R-57:docs are each represented in
the tree and each carries at least one behavioural assertion (mapped above).
The sub-plan's stated static guards are all implemented and passing.

**Ownership boundary:** `git status --porcelain` shows the only tracked source
changes belonging to this attempt are `README.md`, `CLAUDE.md`, `inception.md`
and the base plan file; the new files are the five listed above. Every other
dirty path is prior-pass property — `.claude/shared/monitoring/*` and
`.claude/skills/*` (sp-03), `.claude/local-orchestrators/touch-mongo-live/*`
(live orchestrator state, findings, events.jsonl) — none of it produced by this
sub-plan. `test_shell.py` was **not** touched (it stays sp-03's), as required.
No commit was made.

## Verdict

**PASS.** Targeted suites 100% green (2/2 files, ~90 assertions). Full suite:
26/28 green, 2 red — both the documented pre-existing baseline in files this
sub-plan does not own. No ownership violation, no tautological assertions
detected, Mongo arms skip cleanly without pymongo/mongod.
