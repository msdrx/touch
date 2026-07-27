# sp-custom-state — test gate, attempt 1

**Verdict: PASS.** Both owned suites 100 % green (including their live-mongod
arms, driven under a real authenticated `mongo:7`); 0 NEW failures in the full
suite; 0 ownership violations; 0 commits; nothing in the repo was edited by this
gate (all probes ran on copies under the scratchpad).

Environment: Python 3.13, pymongo 4.17.0 present, Docker daemon available,
`TOUCH_MONGO_URI` unset for the baseline run.

Implementer's changed set — all four are exactly the files
`plan/touch-mongo-live-subplans.md` §"sp-11 — custom-state" assigns:
`aggregator/custom_state.py`, `tests/test_custom_state.py`,
`tests/test_slots.py`, `.claude/skills/touch-orchestrate/SKILL.md`.

---

## 1. Targeted suites — GREEN

Run from the repo root, stdlib only, standalone executables.

| suite | rc | `ok:` assertions | skips |
|---|---|---|---|
| `python3 tests/test_custom_state.py` | **0** | 135 | 1 (live arm, no URI) |
| `python3 tests/test_slots.py` | **0** | 94 | 1 (live arm, no URI) |

Final lines: `all custom-state checks passed`, `all slots checks passed`.

17 `def test_*` in `test_custom_state.py`, 12 in `test_slots.py`; every one is
reached (each prints its own name and all names appear in the output).

### 1a. The live-mongod arms were actually exercised (R-42 / R-56)

The two skips are the designed conditional arms. Rather than accept them on
faith, this gate stood up the R-42 recipe verbatim — loopback-only publish,
`--auth`, root credentials from `openssl rand`, named volume — as
`touch-mongo-sp11` on `127.0.0.1:27317`, exported `TOUCH_MONGO_URI`, and re-ran
both suites. Both went green with the arms live, and the container + volume were
removed afterwards:

`tests/test_custom_state.py::test_live_head_guard_matches_the_model`
```
ok: mongod's guard agrees with the model: seq=3
ok: …and the payload is the newest event's, not the last one attempted
ok: …with all three lines still in the append-only log
```
This is R-52's stated test ("3 out-of-order writes ⇒ head = highest seq, log has
3") verified against a real server's `{seq:{$lt:newSeq}}` update, not only
against the in-process model.

`tests/test_slots.py::test_live_duplicate_key_is_tolerated_not_raised`
```
ok: the first bind acquires: bound
ok: …and the second is a tolerated duplicate, never raised: conflict
ok: …counted, per GD-29's exposed tolerated-dup number:
      {'bound': 1, 'duplicate_key': 1, 'conflict': 1}
ok: …recording the id that collided without ever claiming it
ok: the tailer lives: a collision is a document, not a crash
```
That is R-53's `DuplicateKeyError` clause proven against the real unique sparse
`{agentId:1}` index — the one assertion an in-process fake cannot honestly make.

**No-mongod arm still clean.** With `TOUCH_MONGO_URI` unset both suites skip with
an explicit line and rc 0; `tests/test_stdlib_only.py` is rc 0 and
`aggregator/custom_state.py` imports no driver
(`test_the_module_is_pure_and_carries_no_driver` passes).

## 2. Full-suite regression gate — no NEW failure

22 files: the four `.claude/shared/monitoring/tests/test_*.py` each from its own
directory, then 18 `tests/test_*.py` from the repo root.

- **PASS (20):** monitoring `test_frontend`, `test_server`, `test_shell`,
  `test_watcher`; repo `test_agents`, `test_bootstrap`, `test_custom_state`,
  `test_fixtures`, `test_ingest`, `test_legacy`, `test_mongo_deploy`,
  `test_mongo_store`, `test_reducer`, `test_refs`, `test_slots`,
  `test_stdlib_only`, `test_store`, `test_tailer`, `test_usage`, `test_ws`.
- **FAIL (2) — pre-existing baseline, not attributable to this change:**
  - `tests/test_mirror.py` (rc 1), `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` (rc 1), `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

**Attribution — proven, not assumed.** These strings are character-for-character
the baseline recorded in `sp-agents-reducer-test-attempt-4.md` §2 (and attempts
1–3 before it); both belong to loops that closed RED (`sp-mirror-deploy`,
`sp-sessions-arm`). Because `custom_state.py` registers two new SD-1 mapper
kinds (`customState`, `slot`) that `mirror.discover_mappers` picks up, "same
strings" was not treated as sufficient — the tree was copied to a scratchpad,
this sub-plan's three code/test files were **deleted**, and both suites re-run:

```
tests/test_mirror.py    → FAILED (3)  — identical three strings
tests/test_sessions.py  → FAILED (1)  — identical string
```

So the failures reproduce with sp-11 absent: sp-11 neither caused nor worsened
them. Baseline failures do not fail this gate.

`tests/run_all.sh` reproduces the same picture (fail-fast stops at
`test_mirror.py`; 6 passed before it).

## 3. Verification against the plans

Owned items are `R-52` and `R-53`
(`plan/touch-mongo-live-plan.md` §"Phase M3", `plan/touch-mongo-live-subplans.md`
§sp-11). Every clause has a matching, non-tautological assertion:

| plan clause | asserting test |
|---|---|
| 3 out-of-order writes ⇒ head = highest seq, log keeps 3 | `test_three_out_of_order_writes_leave_the_head_at_the_highest_seq` + live arm |
| `{seq:{$lt:newSeq}}` — a late old write never clobbers | `test_a_late_old_write_never_clobbers_a_fresher_head` |
| unknown `refId` rejected (agents/run_nodes/slots grammars) | `test_an_unknown_refid_is_rejected` |
| Mongo wipe + WAL replay reproduces both collections | `test_a_mongo_wipe_plus_wal_replay_reproduces_both_collections` |
| drop head, rebuild, document-for-document equal | `test_drop_the_head_rebuild_and_it_is_document_for_document_equal` |
| writer cannot emit `provenance:"harness"`; pinned `{asserted,touch}` | `test_the_writer_has_no_code_path_to_a_mirrored_fact_provenance` |
| annotations: `author:"local"` literal, caller cannot invent one; 16 KB cap **rejects** 413, never truncates; machine payloads uncapped | `test_annotations_reject_at_16kb_rather_than_truncating` |
| deletes are tombstone events, no delete verb | `test_deletes_are_tombstone_events_and_no_delete_verb_exists` |
| ONE events + ONE head collection installation-wide, kind-discriminated, insert-only | `test_the_events_collection_is_insert_only_and_installation_wide`, `test_the_module_writes_only_its_own_three_collections` |
| WAL-first at `.touch/custom-state.jsonl` via store.py's existing append machinery, `store.py` UNCHANGED | `test_the_wal_stream_is_the_durable_one_store_already_names`, `test_store_py_was_not_edited_by_this_sub_plan` |
| SD-1 mapper purity + no kind registered twice (GD-15) | `test_the_module_is_pure_and_carries_no_driver` |
| SD-8 `TOUCH_CONTROL_PATHS` + `pathSource` | `test_control_paths_are_configured_and_the_path_is_never_restated` |
| R-53 `pending\|bound\|orphaned\|conflict` + `pendingSince` | `test_pre_spawn_state_binds_when_the_marker_lands`, `test_a_markerless_node_is_orphaned_after_the_ttl_and_at_a_terminal` |
| bind monotone under any replay order (GD-25) | `test_the_bind_is_idempotent_and_never_demoted`, `test_the_state_machine_is_monotone_under_any_replay_order` |
| duplicate key ⇒ `conflict` with BOTH ids, caught + counted, tailer lives | `test_a_duplicate_bind_writes_a_conflict_with_both_ids_and_the_process_lives` + live arm |
| unique **sparse** `{agentId:1}`, name-side index, no TTL (GD-26) | `test_the_schema_indexes_the_hop_in_both_directions` |
| bind evidence channels (marker / ledger / `boundBy`) | `test_the_conclusion_is_a_guarded_write_and_the_evidence_is_a_triple`, `test_the_marker_channel_needs_a_name_a_root_and_an_integer_attempt` |
| same name in two sessions must not cross-link | `test_two_same_named_roots_in_different_sessions_do_not_cross_link` |
| pre-amendment ledger lines ⇒ `sessionKey` from path, `sessionKeySource:"path"` | `test_ledger_ingest_uses_the_stated_session_and_derives_the_rest` |

**SKILL.md scope.** `git diff` on `.claude/skills/touch-orchestrate/SKILL.md` is
+13/−1, confined to the `:52-56` ledger-line block: the JSON line gains `root`
and `sessionKey` (`<pid>-<procStart>`), plus prose stating the pre-amendment
`sessionKeySource:"path"` fallback. Exactly the amendment the sub-plan permits —
no other section touched, and no R-20 material leaked in (R-20 is not in this
pass).

### Anti-tautology probes (4 mutations, run on scratchpad copies only)

| mutation | caught? |
|---|---|
| A: drop the head write's `{seq:{$lt:obs.seq}}` guard | **yes** — 4 FAILs incl. `a seq-1 event arriving after seq 3 loses its guard`, and the GD-25 shuffled-replay fingerprint |
| B: widen `ANNOTATION_LIMIT` 16 KB → 16 MB | *no* — see the observation below |
| C: raise instead of writing a `conflict` on duplicate key | **yes** — `test_slots.py` aborts on the injected `RuntimeError` |
| D: make `validate_ref_id` a pass-through | **yes** — 6 FAILs incl. `the WRITER refuses it too — the WAL never holds a line the mirror must refuse` |

Three of four mutations are caught by a *named behavioural* assertion, so the
suites are driving real code paths, not restating constants.

## 4. Non-blocking observations (do NOT fail this gate)

1. **The 16 KB number itself is unpinned (probe B).**
   `test_annotations_reject_at_16kb_rather_than_truncating` derives its oversize
   payload from the imported `ANNOTATION_LIMIT`
   (`prose = "x" * (ANNOTATION_LIMIT + 1)`) and asserts
   `raised.limit == ANNOTATION_LIMIT`. The *behaviour* — reject-not-truncate,
   `status == 413`, nothing written, `author == "local"`, machine payload
   exempt — is genuinely asserted, which is what R-52 is about; but the cap's
   **value** slides with the constant, so changing 16 KB to 16 MB leaves the
   suite green. Suggested one-liner for whoever next owns the file:
   `check(ANNOTATION_LIMIT == 16 * 1024, "R-52's cap is 16 KB, stated as a number")`.
   Cosmetic — no plan clause is left unasserted.
2. **Missing `+x` bit.** `tests/test_custom_state.py` and `tests/test_slots.py`
   are `-rw-r--r--` while every other file in `tests/` is `-rwxr-xr-x`. Both
   carry `#!/usr/bin/env python3`, and `run_all.sh` invokes them as
   `$PY <file>`, so nothing breaks and no test enforces the bit — but the repo's
   "every test file is a standalone executable" convention would prefer
   `chmod +x`.

## 5. Ownership, git, and cleanliness

- `git status` shows exactly one newly-modified tracked file:
  `.claude/skills/touch-orchestrate/SKILL.md`. Every other modified tracked path
  (`.claude/shared/monitoring/*`, other skills, `CLAUDE.md`, `.gitignore`,
  `.claude/local-orchestrators/**`) was already dirty before this sub-plan and
  is unrelated in-flight orchestrator state.
- `aggregator/` and `tests/` are wholly untracked, so ownership was cross-checked
  by mtime: the three newest files in the tree are `aggregator/custom_state.py`
  (16:18), `tests/test_slots.py` (16:15), `tests/test_custom_state.py` (16:08),
  followed by `.claude/skills/touch-orchestrate/SKILL.md` (16:05); the next file
  down is sp-10's `aggregator/agents.py` at 15:32. No file outside the ownership
  list was written.
- No commit was made; `HEAD` is unchanged at `579446e`. Nothing was reverted or
  stashed. The gate created no file in the repo other than this findings file.
- The `mongo:7` container this gate started (`touch-mongo-sp11`, loopback
  `27317`) and its named volume were removed after use; the two pre-existing
  containers from earlier gates (`touch-mongo-sp05`, `touch-mongo-sp06`) were
  left untouched.
