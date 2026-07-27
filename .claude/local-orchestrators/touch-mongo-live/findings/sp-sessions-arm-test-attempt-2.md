# sp-sessions-arm — test gate, attempt 2 — **PASS**

Read-only gate. No source or test file was edited by this agent.

Implementer-declared change set:
- `/home/laniakea/Projects/touch/aggregator/sessions.py` (1208 lines, 51611 B, mtime 2026-07-26 03:45:57Z)
- `/home/laniakea/Projects/touch/tests/test_sessions.py` (1063 lines, 56266 B, mtime 2026-07-26 03:44:21Z)

## 1. Targeted suite (owned by this sub-plan)

```
cd /home/laniakea/Projects/touch && python3 tests/test_sessions.py
→ exit 0 — "all sessions tests passed"
```

**26 test functions, 161 individual `ok:` assertions, 0 failures.**
All 26 defined `test_*` functions are actually invoked from `main()` (checked
programmatically: defined 26 / invoked 26, none orphaned), and `main()` exits
`sys.exit(1)` whenever `failures` is non-empty — so a red assertion really does
fail the file.

Item coverage:

**R-25 (base, as amended)** — `test_slug_rule_reproduces_the_directory_names_on_disk`
(slug arithmetic against real on-disk names, incl. the doubled `--` of a nested
slug), `test_proc_start_is_field_22_even_when_comm_has_spaces_and_parens`
(parses past the last `)`, and shows the naive `split()[21]` reads the wrong
field), `test_registry_tolerates_lost_found_and_zero_byte_files`,
`test_a_reused_pid_is_not_a_live_session`,
`test_a_registry_entry_for_another_project_is_out_of_scope`,
`test_history_scope_is_the_project_field_not_a_slug_guess` (scope is the
`project` field, not a slug guess; missing `history.jsonl` is empty, not an
error), `test_session_aliases_widen_scope_in_both_plausible_formats`,
`test_alias_closure_is_cycle_safe_and_bounded` (NUL/`..` refused *and counted*,
cycles terminate, closure capped at 32 with the cap counted).

**R-46 (amendment)** — `test_the_project_dir_yields_six_documents_exactly_one_live`
(6 docs, exactly one `live:<pid>-<procStart>`; sessionId lives in `sessionIds`,
never in `_id`), `test_the_four_foreign_slug_dirs_are_not_ingested` (zero
foreign ids; records that an unscoped enumerator would have found 14),
`test_the_transcriptless_seventh_session_is_sources_empty` (`sources: []`
present as a field; empty ≠ missing; history prompt text never reaches a
document), `test_promotion_annotates_the_hist_doc_and_rewrites_no_id`
(`promotedTo` set, `hist:` `_id` untouched, live doc gains the id via
`$addToSet`).

**SD-1 / SD-11 / GD-21 / GD-24 / GD-26 / GD-28** —
`test_mappers_are_registered_pure_and_write_only_sessions` (purity by AST
inspection: no I/O, no clock reads; an op for any collection but `sessions` is
refused in code; `"pymongo" not in source`), `test_the_algebra_is_order_independent`
(normal/shuffled/reversed fingerprint identically; double ingest is a fixed
point), `test_a_disappeared_source_is_a_field_not_a_removal` (GD-26
`present:false`), `test_source_elements_have_a_pinned_field_order`,
`test_class_and_provenance_are_immutables_not_verdicts`,
`test_the_union_is_gd24s_and_refs_owns_both_grammars`,
`test_a_rebuild_through_mirror_reproduces_the_scan` (wipe + `--rebuild`
reproduces a byte-identical fingerprint),
`test_mirror_sources_answer_only_for_paths_they_own`,
`test_claude_root_agrees_with_mirrors`,
`test_the_per_path_seam_does_not_reread_the_alias_closure`.

### Attempt-1 critique items — landed and covered by a test

- **M1** (absurd registry timestamp killed the whole pass): `_epoch_ms` now
  wraps the conversion — `except (OverflowError, OSError, ValueError): return
  None` (`sessions.py:559-563`) — and the refusal is counted via a new
  `skipped["registry_bad_timestamp"]` (`:294`, incremented at `:632`). New test
  `test_an_absurd_registry_timestamp_cannot_kill_the_pass` drives `1e18`,
  `inf`, `nan`, `10**19`, negative and a string, asserts the entry survives
  with no timestamp, both refusals counted (2), the full 6-document pass
  completes, and no timestamp is fabricated on the live doc.
- **M2** (promotion unreachable through the real seam): the module docstring
  gained an explicit **"Not yet wired"** section (`sessions.py:92-107`) naming
  who must supply `Prior`, and a new test
  `test_a_promotion_is_inert_on_the_wired_path` asserts the wired path yields
  nothing today — the mapper test is now labelled a unit test of the contract,
  not end-to-end evidence.
- **M3** (`/clear` join claimed but not implemented): test renamed to
  `test_two_session_ids_on_one_live_id_merge_via_add_to_set`, i.e. it now says
  it exercises the mapper's algebra rather than a discovery behaviour.
- **m1** ($setOnInsert payload disagreement): resolved by removing `sources`
  from the immutables entirely — the empty case is now `{$addToSet: {sources:
  {$each: []}}}` (`sessions.py:1067-1077`), which creates `sources: []` on
  upsert. The assertion is now unfiltered (`promo == map_session(hist)[0][2]
  ["$setOnInsert"]`, verbatim, with an explicit `"sources" not in promo` check
  and a promotion-first ordering check that the resulting doc still has
  `sources == []`). The stated mongod-7 conflict rationale is a claim about
  server behaviour I did not re-run against a live mongod — flagging it for the
  critique gate, not as a failure.

## 2. Full-suite regression gate

```
cd /home/laniakea/Projects/touch && rc=0
for t in .claude/shared/monitoring/tests/test_*.py; do (cd $(dirname $t) && python3 $(basename $t)) || rc=1; done
for t in tests/test_*.py; do python3 "$t" || rc=1; done
exit $rc     → RC=0
```

15/15 files pass, zero failures:
`test_frontend`, `test_server`, `test_shell`, `test_watcher` (monitoring
baselines, all green) + `test_bootstrap`, `test_fixtures`, `test_mirror`,
`test_mongo_deploy`, `test_mongo_store`, `test_refs`, `test_sessions`,
`test_stdlib_only`, `test_store`, `test_tailer`, `test_ws`.

No new failures; no baseline failures either.

### Bare-checkout arm (GD-21 / R-56) — verified, not assumed

`pymongo 4.17.0` is installed in this sandbox, so a plain run does not prove the
requirement. The gate re-ran the whole repo suite under a `sitecustomize.py`
meta-path finder whose `find_spec` raises `ImportError` for `pymongo*`, `bson*`
and `gridfs*`. The blocker was first proved effective
(`import pymongo → ImportError: blocked: pymongo`; note the older `find_module`
hook is silently ignored on Python 3.13, so `find_spec` is the only form that
works here):

```
PYTHONPATH=<blocker-dir> python3 tests/test_<x>.py   → 11/11 PASS, RC=0
```

`tests/test_sessions.py` is green with pymongo genuinely unimportable, and so is
every other repo test — the Mongo-dependent arms skip cleanly. No mongod was
started; nothing hung or timed out.

## 3. Plan conformance and ownership

- Both owned files exist and are substantive; no other file in `aggregator/`,
  `tests/` or `docs/` was written after the previous sub-plan's last write
  (`mirror.py` 02:45:56Z). Newest others: `test_mirror.py` 02:44, `mongo.md`
  02:39 — all older than this attempt. `aggregator/` and `tests/` are untracked
  as whole directories so `git status` cannot discriminate per file; mtimes can,
  and they show exactly the two declared files touched.
- `aggregator/mirror.py` was **not** modified: `sessions.py` plugs into the
  pre-existing generic mapper/source discovery seams by declaring
  `MIRROR_MAPPERS` (`:1127`) / `MIRROR_SOURCES` (`:1205`) on its own module —
  SD-1's registry pattern, satisfied without touching another sub-plan's file.
  `test_claude_root_agrees_with_mirrors` re-asserts that `mirror.py` imports
  `sessions` only dynamically.
- Frozen fixtures intact: `cd tests/fixtures && sha256sum -c MANIFEST.sha256`
  → 0 non-`OK` lines.
- No commits made (SD-6); working tree otherwise untouched; unrelated `.claude/`
  in-flight state left alone.

## 4. Observations (non-blocking, for the critique gate)

1. The mongod-7 `$setOnInsert`/`$addToSet` "would create a conflict at
   'sources'" rationale in `sessions.py:1065-1076` is asserted in prose; the
   test only pins the in-repo memory-model behaviour. If a live-mongod arm is
   cheap for the critique gate, it is the one claim here worth re-running.
2. In-scope transcripts are still synthesized by the test inside a temp
   `~/.claude` (sp-02 froze only the foreign slug dirs and the real
   `sessions/15934.json`); no assertion depends on transcript bytes, since this
   module never reads transcript content (GD-15 gives that to `ingest.py`). The
   test docstring states the trade-off.
3. Runtime is a few seconds; no network, no Docker, no mongod required.

## Verdict

**PASS** — targeted suite 100% green (26 tests / 161 assertions), full suite
15/15 green, bare-checkout (no-pymongo) arm green, no ownership violations,
assertions substantive rather than tautological, and all four attempt-1
critique findings have landing code plus a covering test.
