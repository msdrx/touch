# sp-legacy-arm — test gate, attempt 1 — **PASS**

Read-only gate. No source or test file was edited. All probes ran against
copies under the session scratchpad, never the live tree.

Implementer's changed set: `aggregator/legacy.py`, `tests/test_legacy.py`.

## 1. Targeted suite (owned) — GREEN

```
cd /home/laniakea/Projects/touch && python3 tests/test_legacy.py
→ exit 0 — "all legacy (R-27 / R-51 / R-58 read-time) tests passed"
```

38 `test_*` functions, several hundred individual `ok:` assertions. Coverage
maps onto the sub-plan's items:

- **R-27 / GD-14**: line-position parsing and retention (`test_every_frozen_line_parses_and_keeps_its_position`,
  `test_a_broken_line_is_kept_and_a_torn_tail_is_not`, `test_an_unparseable_ts_does_not_lose_the_line`,
  `test_out_of_enum_states_map_to_info_and_are_never_dropped`), synthesized
  runId (`test_a_run_id_is_synthesized_when_the_config_does_not_name_one`),
  in-memory ordinals across a two-wave respawn
  (`test_the_two_wave_respawn_becomes_distinct_ordinals`), two-writer dedup
  (`test_duplicate_stage_terminals_dedupe_and_keep_the_agents_words`,
  `test_watcher_wins_is_only_for_same_state_duplicates`), lossless token folding
  (`test_the_token_fold_is_lossless_and_bounded`,
  `test_a_token_line_that_cannot_fold_losslessly_is_kept_whole`),
  plan-only folders (`test_a_plan_only_folder_is_its_own_kind_with_no_controls`),
  derived archive label (`test_the_archive_label_is_derived_not_constant`),
  8-hex/17-hex join (`test_agent_ids_are_namespaced_and_both_widths_join`),
  and the never-read checkpoint (`test_the_watcher_checkpoint_is_never_read`).
- **R-51**: positional `_id` grammar asserted literally —
  `legacy:touch-mongo-live#00000275` — plus a `refs.parse_ref_key` round-trip
  and a `mongo_store.check_id` cross-check (`test_the_positional_id_is_the_gd24_grammar`);
  byte-identical duplicate lines stay distinct
  (`test_n_documents_for_n_lines_including_the_identical_ones`); GD-28 no-guess
  provenance (`test_provenance_follows_the_no_guess_rule_and_the_anchored_counts`,
  `test_the_w_field_wins_over_the_shape_rules`); artifact registry as
  `custom_state_events` with paths + sha256 + size only and
  `.watcher-state.json` excluded by name
  (`test_the_artifact_registry_lists_the_folder_with_correct_digests`,
  `test_an_artifact_id_is_stable_content_addressed_and_insert_only`);
  collection containment (`test_the_mapper_writes_only_its_own_two_collections`
  — `_only_ours` structurally refuses an `agents` write).
- **R-58 / SD-4 read-time half**: `test_the_fabricated_failed_badge_becomes_closed_no_verdict`,
  `test_sd4_last_event_wins_on_conflicting_plan_terminals`,
  `test_r58_zero_failed_badges_on_the_three_affected_streams`,
  and the negative controls `test_a_genuine_failure_keeps_its_badge` /
  `test_an_unresulted_sibling_blocks_the_relabel` (which are what stop the rule
  from degenerating into "relabel everything").
- **SD-1 purity**: `test_the_mapping_half_is_pure` walks the AST of all six pure
  functions and asserts no filesystem, no clock, no I/O; `test_the_reduction_is_a_pure_function_of_the_lines`
  asserts determinism node-for-node.
- **SD-11 / mirror contract**: `test_the_registry_matches_the_mirror_contract`
  and `test_the_sources_own_their_paths_and_nothing_else` drive the real
  `mirror.discover_mappers`, confirming the arm plugs in with no change on
  mirror's side and that the observation kind is `legacyArtifact` (not the
  already-taken `artifact`).
- **GD-14 percent-escaping**: exercised with the adversarial task name
  `touch#recon|v2:stage%1`, asserted by round-trip rather than by string shape.

### Non-tautology check (mutation probe)

A scratchpad copy of `aggregator/legacy.py` was mutated
(`node.derived_from_legacy = True` → `False`). `tests/test_legacy.py` went red
on the copy:

```
FAILED (1):
  - …every one of them marked derived_from_legacy, never `failed` (D13)
```

A second mutation (`:08d` → `:07d`) was a no-op, correctly: the id is formatted
by `refs.ref_key` (sp-refs-mongostore's property); the only `:08d` literals in
`legacy.py` are docstrings. The test asserts the grammar through `refs`, which
is the right seam.

### Degraded-environment arm (GD-21 / R-56)

`tests/test_legacy.py` imports only stdlib + `aggregator.{legacy,mongo_store,refs}`
(and `mirror` lazily, inside one test). With `pymongo` import forced to fail via
a shadowing stub on `PYTHONPATH`, the file still exits 0:

```
PYTHONPATH=<stub-with-raising-pymongo> python3 tests/test_legacy.py → RC=0
```

`aggregator/legacy.py` itself imports nothing third-party. `tests/test_stdlib_only.py`
is green, so the policy guard also accepts the new module.

## 2. Full-suite regression gate — no NEW failures

18 files run (4 monitoring from their own dir, 14 repo tests from the root).

Green (16): all four monitoring tests (`test_frontend`, `test_server`,
`test_shell`, `test_watcher`), plus `test_bootstrap`, `test_fixtures`,
`test_ingest`, `test_legacy`, `test_mongo_deploy`, `test_mongo_store`,
`test_refs`, `test_stdlib_only`, `test_store`, `test_tailer`, `test_usage`,
`test_ws`. The Mongo arms skipped cleanly (`TOUCH_MONGO_URI` unset).

Red (2 files, 4 assertions) — **pre-existing, not attributable to this change**:

- `tests/test_mirror.py` (sp-mirror-deploy, closed RED):
  - `…proven by the call count: the held ticks made no attempt`
  - `the first generation lands`
  - `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`
- `tests/test_sessions.py` (sp-sessions-arm, closed RED):
  - `wipe + --rebuild reproduces a byte-identical fingerprint`

Attribution was established, not assumed. Because `legacy.py` registers new
mappers that `mirror.discover_mappers` picks up automatically, a plausible
mechanism for a *new* cross-sub-plan break existed, so it was tested directly:
`aggregator/` + `tests/` were copied to the scratchpad, `aggregator/legacy.py`
and `tests/test_legacy.py` deleted, and the two files re-run. Both reproduced
the **identical** failure sets:

```
tests/test_mirror.py   → FAILED (3)  [same three assertions]
tests/test_sessions.py → FAILED (1)  [same assertion]
```

So neither failure count nor identity changes with the sub-plan's files
removed — these are baseline reds owned by sp-mirror-deploy and sp-sessions-arm.
Per the gate's baseline clause they do not fail this gate, and per the tree
rules they were not touched.

## 3. Ownership

`aggregator/legacy.py` (mtime 12:45:44) and `tests/test_legacy.py` (12:51:50)
are the only two files in `aggregator/`, `tests/`, `docs/` with a mtime in this
attempt's window; the next most recent are `tests/test_ingest.py` (11:48) and
`aggregator/mirror.py` (11:29), both from earlier loops. `git status` shows no
newly modified tracked file (the whole `aggregator/`/`tests/`/`docs/` trees are
still untracked as a unit, consistent with the prior pass). Nothing under
`.claude/` was written apart from this findings file and the two `status.sh`
event lines.

## Verdict

**PASS** — owned suite 100% green, no new regressions, every owned item present
in the tree and asserted behaviourally rather than tautologically.
