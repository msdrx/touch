# sp-sessions-arm — test gate, attempt 1 — **PASS**

Read-only gate. No source or test file was edited by this agent.

Implementer-declared change set:
- `/home/laniakea/Projects/touch/aggregator/sessions.py` (1078 lines)
- `/home/laniakea/Projects/touch/tests/test_sessions.py` (813 lines)

## 1. Targeted suite (owned by this sub-plan)

```
cd /home/laniakea/Projects/touch && python3 tests/test_sessions.py
→ exit 0 — "all sessions tests passed"
```

**23 test functions, 124 individual `ok:` assertions, 0 failures.**

Coverage maps 1:1 onto the sub-plan's two items:

R-25 (as amended)
- `test_slug_rule_reproduces_the_directory_names_on_disk` — slug arithmetic
  verified against *real* directory names on this machine, including the
  doubled `--` of a nested slug (not an approximation).
- `test_proc_start_is_field_22_even_when_comm_has_spaces_and_parens` — reads
  procfs field 22 past a `comm` containing `' '` and `')'`, and explicitly
  demonstrates that the naive `line.split()[21]` reads the *wrong* field. No
  procfs / bad pid / missing pid ⇒ `None`, never an exception.
- `test_registry_tolerates_lost_found_and_zero_byte_files` — `lost+found` and
  zero-byte registry files counted, not fatal; allowlisted registry fields pass
  through and `status` does **not** (GD-23: no liveness in a mirror doc).
- `test_a_reused_pid_is_not_a_live_session` — pid reuse falls to the historical
  arm and is counted as a stale pid.
- `test_a_registry_entry_for_another_project_is_out_of_scope`.
- `test_history_scope_is_the_project_field_not_a_slug_guess`.

R-46
- `test_the_project_dir_yields_six_documents_exactly_one_live` — **6 session
  documents, exactly one `live:`**, id `live:15934-4101211` i.e. (pid, procStart)
  keyed; the sessionId lives in `sessionIds`, never in `_id`; GD-28 provenance
  and GD-6 `class: observed` on every doc; every doc validates against GD-24's row.
- `test_the_four_foreign_slug_dirs_are_not_ingested` — the frozen tree really
  holds the foreign slugs, scope is one slug, **zero** foreign sessionIds
  ingested, and the test records that an unscoped enumerator would have found 14.
- `test_the_transcriptless_seventh_session_is_sources_empty` — `sources: []`
  present as a field (empty ≠ missing); unparseable history line counted, not
  fatal; history prompt text never reaches a document.
- `test_promotion_annotates_the_hist_doc_and_rewrites_no_id` — both docs stay
  queryable, the `hist:` `_id` is untouched, `promotedTo` points at the live doc,
  live doc reaches the sessionId via `$addToSet`, `$setOnInsert` identity intact,
  and a first pass emits no spurious promotion.
- `test_a_process_that_cleared_accumulates_session_ids` — one doc, two
  sessionIds, `$addToSet` not `$set`; both orders fingerprint identically.

Cross-cutting invariants also asserted (not tautologies — several are negative
tests that fail closed):
- `test_mappers_are_registered_pure_and_write_only_sessions` — SD-1 purity is
  checked by **AST inspection** (no I/O, no clock reads in `map_session`,
  `map_promotion`, `_identity_on_insert`, `_only_sessions`); GD-21 asserted by
  proving `sessions.py` imports no pymongo; an op for any collection other than
  `sessions` is refused *in code*.
- `test_the_algebra_is_order_independent` — normal/shuffled/reversed all
  fingerprint to `9dbce091b821ab32148acfd7fddeee22ddd2aca56ad7e3eef34dcbfcffc8d51b`;
  double ingest is a fixed point (upsert-only).
- `test_backfill_observations_carry_no_timestamp` — a historical op stores **no
  datetime at all**, so a backfill can never stamp history with the import
  clock; live arm uses `$min`/`$max`; absurd/missing registry ts ⇒ `None`, never
  `now()`.
- `test_a_rebuild_through_mirror_reproduces_the_scan` — wipe + `--rebuild`
  reproduces a byte-identical fingerprint (GD-30/R-42 rebuild criterion).
- `test_session_aliases_widen_scope_in_both_plausible_formats` +
  `test_alias_closure_is_cycle_safe_and_bounded` — three alias file formats,
  NUL/`..` entries refused *and counted*, cycles terminate, closure capped at 32
  with the cap counted rather than silent.
- `test_a_disappeared_source_is_a_field_not_a_removal` (GD-26 `present:false`),
  `test_source_elements_have_a_pinned_field_order`,
  `test_class_and_provenance_are_immutables_not_verdicts`,
  `test_the_union_is_gd24s_and_refs_owns_both_grammars`,
  `test_mirror_sources_answer_only_for_paths_they_own`,
  `test_claude_root_agrees_with_mirrors`.

## 2. Full-suite regression gate

```
cd /home/laniakea/Projects/touch && rc=0
for t in .claude/shared/monitoring/tests/test_*.py; do (cd $(dirname $t) && python3 $(basename $t)) || rc=1; done
for t in tests/test_*.py; do python3 "$t" || rc=1; done
exit $rc     → RC=0
```

15/15 files pass, zero failures:

| file | result |
|---|---|
| `.claude/shared/monitoring/tests/test_frontend.py` | PASS |
| `.claude/shared/monitoring/tests/test_server.py` | PASS |
| `.claude/shared/monitoring/tests/test_shell.py` | PASS |
| `.claude/shared/monitoring/tests/test_watcher.py` | PASS |
| `tests/test_bootstrap.py` | PASS |
| `tests/test_fixtures.py` | PASS |
| `tests/test_mirror.py` | PASS |
| `tests/test_mongo_deploy.py` | PASS |
| `tests/test_mongo_store.py` | PASS |
| `tests/test_refs.py` | PASS |
| `tests/test_sessions.py` | PASS |
| `tests/test_stdlib_only.py` | PASS |
| `tests/test_store.py` | PASS |
| `tests/test_tailer.py` | PASS |
| `tests/test_ws.py` | PASS |

No new failures; the four monitoring baselines stay green.

### No-third-party arm (GD-21 / R-56) — verified, not assumed

`pymongo 4.17.0` **is** installed in this sandbox, so a plain run does not prove
the bare-checkout requirement. The gate re-ran the repo suite with a
`sitecustomize.py` meta-path hook that raises `ImportError` for `pymongo*` and
`bson*`:

```
PYTHONPATH=<blocker-dir> python3 tests/test_*.py   → 11/11 PASS, RC=0
```

`tests/test_sessions.py` is green with pymongo unimportable, and so is every
other repo test — the Mongo-dependent arms skip cleanly. No mongod was started
for this gate; nothing hung or timed out.

## 3. Plan conformance and ownership

- Both owned files exist and are non-trivial: `aggregator/sessions.py` (44 KB)
  and `tests/test_sessions.py` (41 KB).
- Public API present for everything the sub-plan requires: `scan`, `map_session`,
  `map_promotion`, `MIRROR_MAPPERS`, `MIRROR_SOURCES`, `read_registry`,
  `read_alias_slugs`, `project_slugs`, `read_proc_start`, `discover_transcripts`,
  `read_history_sessions`, `session_id_for_path`, `slug_for`, `claude_root`,
  `iter_session_observations`, `iter_promotion_observations`, plus `Prior`,
  `Scan`, `Source`, `SessionObservation`, `PromotionObservation`, `SessionsError`.
- **No edits outside ownership.** `aggregator/` and `tests/` are still untracked
  as whole directories, so `git status` cannot discriminate per file; mtimes do:
  `sessions.py` 03:21:42 and `test_sessions.py` 03:23:01 are the only files
  newer than the previous sub-plan's last write (`mirror.py` 02:45:56). Every
  other `aggregator/*.py`, `tests/test_*.py` and `docs/mongo.md` is untouched.
- `aggregator/mirror.py` was **not** modified: `sessions.py` plugs into the
  pre-existing generic `discover_mappers()` / `iter_sources()` seams
  (`mirror.py:781`, `mirror.py:2473`) by declaring `MIRROR_MAPPERS` /
  `MIRROR_SOURCES` on its own module. The sub-plan's "mirror mapping in
  `mirror.py`" clause is satisfied without touching another sub-plan's file.
- Frozen fixtures intact: `cd tests/fixtures && sha256sum -c MANIFEST.sha256`
  → clean.
- No commits were made; working tree otherwise untouched.

## 4. Observations (non-blocking, for the critique gate)

1. **Fixture synthesis, documented and justified.** `tests/fixtures/mirror/
   discovery/projects/` freezes only the four *foreign* slug dirs plus the real
   `sessions/15934.json` registry entry (verified: real `pid 15934`,
   `procStart 4101211`, `cwd /home/laniakea/Projects/touch`, real sessionId).
   sp-02 froze no in-scope top-level `<sessionId>.jsonl` transcripts, so the six
   in-scope transcripts are created as empty files with the correct names inside
   a temp `~/.claude`. The module never reads transcript *contents* (GD-15 gives
   line parsing to `ingest.py`), so no assertion depends on the synthesized
   bytes — the load-bearing data (foreign slug ids, registry entry) is genuinely
   frozen and the eight foreign sessionIds in the test's `FOREIGN_IDS` were
   confirmed to match the eight `.jsonl` basenames actually on disk. The test's
   module docstring states this trade-off explicitly.
2. The transcriptless seventh session's `history.jsonl` is likewise synthesized
   by the test rather than frozen; the assertion it supports (`sources: []` is a
   present-but-empty field) is behavioural and does not depend on real bytes.
3. Runtime is fast (a few seconds); no network, no Docker, no mongod required.

## Verdict

**PASS** — targeted suite 100 % green (23 tests / 124 assertions), full suite
15/15 green, no-pymongo arm green, no ownership violations, assertions are
substantive rather than tautological.
