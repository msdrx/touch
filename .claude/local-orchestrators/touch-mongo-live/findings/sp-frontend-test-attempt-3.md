# sp-frontend — test gate, attempt 3

**Verdict: PASS.** Targeted suite 100% green (33 tests, 311 `ok:`, 0 FAIL,
0 skip with node present; 282 `ok:` + 2 clean `skip:` and rc 0 with node
absent). Full-suite regression: **no new failure** — the only two red files
are the string-identical pre-existing baseline pair owned by sp-mirror-deploy
and sp-sessions-arm. Ownership clean; no commits (HEAD still `579446e`).

Environment: Python 3.13, `TOUCH_MONGO_URI` unset, no services running,
no third-party packages required by this sub-plan's suite.

Implementer's changed set — all four sub-plan-owned per
`plan/touch-mongo-live-subplans.md` §"sp-13 — frontend":
`touch-visual/index.html`, `touch-visual/app.js`, `touch-visual/style.css`,
`tests/test_touch_frontend.py`.

---

## 1. Targeted suite (sp-frontend owned) — GREEN

```
$ cd /home/laniakea/Projects/touch && python3 tests/test_touch_frontend.py
… 311 `ok:` lines, 0 FAIL, 0 SKIP
all touch-visual source guards passed
rc 0
```

* 33 `def test_*` defined, 33 registered in `main()` — **zero orphaned tests**
  (verified by AST-free regex cross-check of the `main()` body against the
  defined set). Up from 25 tests / 232 `ok:` at attempt 2.
* 201 `check(...)` call sites.
* Two conditional arms (`test_the_page_parses_as_javascript`,
  `test_the_page_behaves_when_it_is_actually_driven`) are guarded by
  `shutil.which("node")`.

### Bare-checkout / no-node arm — verified, not assumed

Re-ran with a stripped `PATH` containing only symlinks to
`python3/sh/env/bash/grep/cat/ls` (node genuinely unreachable):

```
rc 0
  skip: no node on PATH — the static guards above still apply
  skip: no node on PATH — the static guards above still apply, but NOTHING here has been executed
282 ok:
```

The suite degrades to the static-source guards and still exits 0 — the
"green on a bare checkout, no services, no third-party packages" requirement
holds. No pymongo/mongod dependency exists in this file at all.

## 2. Full-suite regression gate

```
$ cd /home/laniakea/Projects/touch && rc=0; \
  for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done; \
  for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc
```

- **PASS (23):** monitoring baseline four (`test_frontend`, `test_server`,
  `test_shell`, `test_watcher`, each from their own dir) plus `test_agents`,
  `test_api`, `test_bootstrap`, `test_custom_state`, `test_fixtures`,
  `test_ingest`, `test_legacy`, `test_mongo_deploy`, `test_mongo_store`,
  `test_reducer`, `test_refs`, `test_server_core`, `test_slots`,
  `test_stdlib_only`, `test_store`, `test_tailer`, `test_touch_frontend`,
  `test_usage`, `test_ws`.

- **FAIL (2) — pre-existing baseline, NOT attributable:**
  - `tests/test_mirror.py` rc 1, `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` rc 1, `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

  Attribution (unchanged from attempts 1–2, re-verified):
  1. **String-identical** to the baseline recorded in
     `sp-frontend-test-attempt-1.md`/`-2.md` and earlier in
     `sp-server-api-test-attempt-4.md`, `sp-custom-state-test-attempt-4.md`,
     `sp-agents-reducer-test-attempt-4.md` — same suites, same counts, same
     messages.
  2. **No causal path.** The failures live in `aggregator/mirror.py` and
     `aggregator/sessions.py`; nothing under `touch-visual/` is imported, read
     or executed by those suites, and the frontend exports no Python symbol.
  3. **Mtimes.** `find aggregator tests docs touch-visual -newermt "2026-07-26
     21:00"` returns exactly `tests/test_touch_frontend.py` (22:04),
     `touch-visual/style.css` + `index.html` (22:05), `touch-visual/app.js`
     (22:06), plus one `__pycache__` artefact. `aggregator/mirror.py` 11:29,
     `aggregator/sessions.py` 04:10, `tests/test_mirror.py` 02:44,
     `tests/test_sessions.py` 04:14 — hours older, untouched.
  4. Both suites reach their `TOUCH_MONGO_URI is not set` arm and skip cleanly
     (GD-21/R-56 no-mongod behaviour intact) — these are logic failures, not
     missing-driver leakage.
  5. `tests/test_stdlib_only.py` green — no third-party import added.

## 3. Plan conformance (sp-13, R-22:frontend, R-32, R-55, GD-20/23)

Every owned item is present in the tree and asserted non-tautologically —
the guards are cross-file comparisons against the Python counterparties
(`aggregator/agents.py`, `legacy.py`, `server.py`) or behavioural runs inside
a node `vm` DOM harness, not restatements of the source.

| Item | Evidence |
| --- | --- |
| R-22 skeleton | `test_the_three_files_are_where_the_server_serves_them_from` — paths cross-checked against the server's static map; `test_the_page_carries_the_serve_time_token_where_it_is_valid` |
| R-32 sidebar (sessions + legacy folders, GD-14 kinds) | `test_the_sidebar_lists_every_class_of_thing_the_store_knows`, `test_a_run_that_starts_after_the_handshake_reaches_the_sidebar` (driven) |
| R-32 agent tree keyed per GD-7 | `test_the_agent_tree_is_keyed_by_harness_facts` (asserts `(key, ordinal)` + full `agentId`), `test_the_agent_tree_is_drawn_as_containment` (driven: children nested in the parent card, every agent drawn once) |
| R-32 token rollups from computed sums | `test_token_rollups_are_sums_of_absolute_records`, `test_the_token_rollup_key_is_an_identity_not_a_display_string`; driven: `a later absolute token record replaces the earlier one, never adds to it` |
| GD-20 escape-first | `test_no_markup_sink_exists_in_the_page`, `test_the_stripper_sees_this_file_the_way_it_claims` (the guard proves it can see its own file), `test_no_liveness_class_is_attached_outside_a_whitelist` |
| R-32 coalescing + capped log | `test_the_render_is_coalesced_and_the_log_is_capped`, `test_every_growing_collection_is_capped`, `test_every_live_region_is_written_only_when_it_changes`, `test_a_region_is_rebuilt_only_when_it_changed`; driven: `the live log is pinned at its cap`, `three unchanged paints do not touch it` |
| R-32 degraded/derived labelling | `test_degraded_and_derived_states_are_labelled`, `test_the_class_whitelists_match_the_servers_vocabulary` (`NODE_STATE_CLASS == agents.NODE_STATES`, `LEGACY_STATE_CLASS == legacy.STATES + DERIVED_STATES`, `failed` absent from the derived whitelist per R-58) |
| R-32 NO control affordance in v0 | `test_no_control_verb_reaches_the_page` sweeps `pause/restart/terminate/kill/abort/interrupt` over the page **and** asserts the server's control route group is empty. Independent grep of `app.js`/`index.html` for those verbs: **zero hits** |
| R-55 replay paints once, no animation | `test_only_live_frames_animate` (real sweep over every CSS rule carrying `animation:`); driven: `a replayed row does not animate` / `a live row does` / `history is not animated` |
| R-55 no state inference in `app.js` (GD-23) | `test_the_page_never_infers_state` (no `Date.now`/`performance.now`/local reducer/idle threshold) |
| Wire contract restated verbatim (sp-12) | `test_the_wire_contract_is_restated_verbatim` (byte comparison of the frame block across files), `test_the_resume_cursor_is_the_servers_not_ours`, `test_the_resync_never_asks_to_be_moved_forward`, `test_the_load_older_anchors_come_from_the_frames_that_know_them` |
| GD-22 degradation | `test_the_page_degrades_without_mongo_and_says_why`, `test_the_notice_surface_states_the_current_cycle`, `test_the_expensive_route_is_polled_on_its_own_slow_cadence` |

### Attempt-2 critique items — closed in the tree

- **n6** (`stream_meta` ignored R-47's `render` field): closed — `app.js:1578`
  now renders `chip("prov-derived", "meta · not rendered", "the ingest marked
  this record non-renderable (R-47)")` when `record.render === false`.
- **n7** (ref-less token record collapsing into a `stream + "|"` slot): closed
  — `noteTokens` (`app.js:567`) now returns early on a missing/empty `ref`,
  increments `state.tokensRefless`, and the count is surfaced beside the sum.

## 4. Ownership / VCS

- Files modified in this attempt's window: exactly the four owned files
  (`find` result in §2.3). Nothing under `aggregator/`, `docs/`, or any other
  sub-plan's tests was touched.
- `git status` shows only the pre-existing in-flight `.claude/` orchestrator
  state (events, findings, orch-scripts, monitoring module) plus the untracked
  `aggregator/`, `docs/`, `tests/`, `touch-visual/` trees from earlier passes.
- `git log --oneline -1` → `579446e orchestration history`. **No commit made.**

---

**Gate result: PASS.** No new failure introduced; targeted suite fully green
in both the node-present and node-absent configurations.
