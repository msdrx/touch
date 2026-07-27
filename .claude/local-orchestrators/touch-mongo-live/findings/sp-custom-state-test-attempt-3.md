# sp-custom-state — test gate, attempt 3 — **PASS**

Read-only gate. No source or test file was edited; only run and inspected.
Environment: Python 3.13, no `TOUCH_MONGO_URI` set, no mongod running, pymongo
importable from the prior pass's install.

## 1. Targeted suites (owned by sp-custom-state) — 100% green

Run from the repo root, standalone executables:

| suite | result | assertions |
|---|---|---|
| `python3 tests/test_custom_state.py` | **rc 0**, `all custom-state checks passed` | 195 `ok:`, 1 clean `SKIP` |
| `python3 tests/test_slots.py` | **rc 0**, `all slots checks passed` | 143 `ok:`, 1 clean `SKIP` |

Both skips are the R-42/GD-21 live arms and skip *cleanly* with an explicit
reason, which is the required no-mongod behaviour:

```
SKIP: live custom-state arm: TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)
SKIP: live slots arm: TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)
```

No monitoring-module test is owned by this sub-plan, so none was run as a
targeted suite (all four are covered by the regression gate below).

## 2. Full-suite regression gate — no NEW failure

22 files: the four `.claude/shared/monitoring/tests/test_*.py`, each from its own
directory, then 18 `tests/test_*.py` from the repo root.

- **PASS (20):** monitoring `test_frontend`, `test_server`, `test_shell`,
  `test_watcher`; repo `test_agents`, `test_bootstrap`, `test_custom_state`,
  `test_fixtures`, `test_ingest`, `test_legacy`, `test_mongo_deploy`,
  `test_mongo_store`, `test_reducer`, `test_refs`, `test_slots`,
  `test_stdlib_only`, `test_store`, `test_tailer`, `test_usage`, `test_ws`.
- **FAIL (2) — pre-existing baseline, not attributable to this change:**
  - `tests/test_mirror.py`, `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py`, `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

Loop rc is therefore 1, but every failing string belongs to a loop that closed
RED with open findings (`sp-mirror-deploy`, `sp-sessions-arm`) and is recorded
character-for-character in `sp-agents-reducer-test-attempt-4.md` §2 and in this
sub-plan's own `-test-attempt-1.md` / `-test-attempt-2.md`.

**Attribution re-proven for THIS attempt, not inherited.** Because
`custom_state.py` registers two SD-1 mapper kinds (`customState`, `slot`) that
`mirror.discover_mappers` picks up dynamically, "same strings as last time" is
not sufficient evidence. The ablation was repeated against the attempt-3 tree:
`aggregator/`, `tests/`, `docs/` copied to a scratchpad; this sub-plan's three
files (`aggregator/custom_state.py`, `tests/test_custom_state.py`,
`tests/test_slots.py`) **deleted**; `__pycache__` removed; both suites re-run:

```
tests/test_mirror.py    → FAILED (3)  — identical three strings
tests/test_sessions.py  → FAILED (1)  — identical string
```

The failures reproduce with sp-11 entirely absent. Baseline failures do not fail
this gate; no other failure appeared.

## 3. Verification against the plans

Owned items: **R-52** and **R-53**
(`plan/touch-mongo-live-plan.md` §"Phase M3"; `plan/touch-mongo-live-subplans.md`
§sp-11). Every plan-stated test clause has a named, non-tautological assertion in
the tree:

| plan clause | asserting test |
|---|---|
| 3 out-of-order writes ⇒ head = highest seq, log keeps 3 | `test_three_out_of_order_writes_leave_the_head_at_the_highest_seq` |
| `{seq:{$lt:newSeq}}` — a late old write never clobbers | `test_a_late_old_write_never_clobbers_a_fresher_head` (+ `test_live_head_guard_matches_the_model`, live arm) |
| unknown `refId` rejected against agents/run_nodes/slots grammars | `test_an_unknown_refid_is_rejected`, `test_a_ref_and_a_refid_that_disagree_are_refused` |
| Mongo wipe + WAL replay reproduces both collections exactly | `test_a_mongo_wipe_plus_wal_replay_reproduces_both_collections` |
| drop `custom_state`, rebuild, document-for-document equal | `test_drop_the_head_rebuild_and_it_is_document_for_document_equal` |
| writer cannot emit `provenance:"harness"` (unit-asserted) | `test_the_writer_has_no_code_path_to_a_mirrored_fact_provenance` |
| annotations `author:"local"`, 16 KB cap REJECTS 413 | `test_annotations_reject_at_16kb_rather_than_truncating` |
| deletes are tombstone events | `test_deletes_are_tombstone_events_and_no_delete_verb_exists` |
| ONE events + ONE head collection installation-wide, append/insert-only | `test_the_events_collection_is_insert_only_and_installation_wide`, `test_the_module_writes_only_its_own_three_collections` |
| WAL rides `store.py`'s existing append machinery; `store.py` UNCHANGED | `test_the_wal_stream_is_the_durable_one_store_already_names`, `test_store_py_was_not_edited_by_this_sub_plan` |
| R-53 `pending\|bound\|orphaned\|conflict` + `pendingSince` | `test_pre_spawn_state_binds_when_the_marker_lands`, `test_a_markerless_node_is_orphaned_after_the_ttl_and_at_a_terminal` |
| DuplicateKeyError ⇒ `conflict` with BOTH ids, caught, counted, tailer lives | `test_a_duplicate_bind_writes_a_conflict_with_both_ids_and_the_process_lives`, `test_a_third_collision_is_recorded_and_the_result_says_what_it_wrote`, `test_live_duplicate_key_is_tolerated_not_raised` |
| unique sparse `agentId` index; name side indexed | `test_the_schema_indexes_the_hop_in_both_directions` |
| same name in two sessions must not cross-link | `test_two_same_named_roots_in_different_sessions_do_not_cross_link` |
| SD-8 `TOUCH_CONTROL_PATHS` + `pathSource` | `test_control_paths_are_configured_and_the_path_is_never_restated`, `test_a_control_line_in_the_skill_files_own_shape_is_ingested` |
| SKILL.md ledger amendment (`root`, `sessionKey <pid>-<procStart>`, path-derived legacy) | `test_the_ledger_line_carries_root_and_sessionkey`, `test_ledger_ingest_uses_the_stated_session_and_derives_the_rest`, `test_a_session_key_is_only_derived_under_a_directory_the_layout_names` |

Spot-checked for tautology: `test_the_writer_has_no_code_path_to_a_mirrored_fact_provenance`
exercises three independent legs (`validate_provenance` rejection, `Writer.append`
rejecting *before* a byte reaches the WAL with the file asserted still empty, and
an AST walk proving no `provenance` literal in the module escapes the two-value
enum) — behavioural, not a constant compared to itself. The slots suite likewise
drives real state-machine transitions and reports counters
(`{'observed': 1, 'bound': 1, 'conflict': 1, 'orphaned': 0, 'duplicate_key': 0, 'rejected': 0}`).

Attempt-2 critique items all have a named owner test now:
m1 → `test_the_session_key_source_settles_by_trust_not_by_alphabet`;
m2 → `test_a_ledger_line_is_the_agents_claim_and_touchs_own_record_says_touch`;
m3 → `test_control_paths_are_configured_and_the_path_is_never_restated`;
m4 → `test_the_ledger_reader_counts_every_line_it_drops` (missing-`attempt` line
produces NO slot, counted by reason);
M2 → `test_a_bind_cannot_lower_what_an_observation_raised`;
M1 → handed off, see below.

### Ownership check (git status vs. the ownership list)

Modified/created by this attempt, and nothing else:

- `aggregator/custom_state.py` (mtime 07-26 17:27)
- `tests/test_custom_state.py` (17:23)
- `tests/test_slots.py` (17:21)
- `.claude/skills/touch-orchestrate/SKILL.md` — `git diff --stat`: **13 insertions,
  1 deletion, confined to the :52-56 ledger-line block** (adds `root` +
  `sessionKey`, documents the pre-amendment `sessionKeySource:"path"` fallback).
  No other section touched.
- `.claude/local-orchestrators/touch-mongo-live/findings/sp-custom-state-slots-set-fields-deviation.md`
  (task state, allowed).

Every other file under `aggregator/` and `tests/` carries an mtime from an
earlier sub-plan (≤ 15:32). `git status --porcelain` shows no non-`.claude`
change outside `aggregator/`, `tests/`, `docs/`, `.gitignore`, `CLAUDE.md`
(the latter three pre-existing from earlier sub-plans). No commit was made.

### Carried deviation (not a gate failure)

`sp-custom-state-slots-set-fields-deviation.md` records a one-line GD-25 gap in
`mongo_store.COLLECTIONS["slots"]` (`set_fields=()` while this module writes four
`$addToSet` arrays). The file is owned by **sp-05**, so this sub-plan correctly
did not edit it; the handoff paste is written out, `custom_state.SLOT_SET_FIELDS`
is exported for it, and `test_slots.py` derives the emitted `$addToSet` keys and
asserts they match the exported tuple, so the note cannot go stale silently. The
suite installs the declaration only for the duration of the fingerprint
assertion and prints a `deviation:` line when it must. Recorded here so the
aggregate gate can see it; it does not fail this gate.

## Verdict

**PASS.** Both owned suites green (338 assertions, 2 clean skips). Full-suite
regression introduces no new failure — the only two failures are the proven
`sp-mirror-deploy` / `sp-sessions-arm` baseline, re-verified by ablation against
this attempt's tree. Ownership boundaries respected.
