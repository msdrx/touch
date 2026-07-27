# sp-ingest-pipelines — test gate, attempt 1 (this pass) — **PASS**

> The earlier pass's attempt-1 findings for this sub-plan were archived to
> `sp-ingest-pipelines-test-attempt-1-prior-pass.md` before this file was written.

Read-only gate. No source or test file in the repo was edited; no commit was made.
0 new failures, 0 ownership violations.
Owned suites 100 % green — including the live-mongod arm, run for real against a
containerised `mongo:7`. The full suite carries 2 pre-existing RED files
(`tests/test_mirror.py`, `tests/test_sessions.py`) that are **proven
non-attributable** to this change set.

Environment: Python 3.13, pymongo 4.17.0 present, Docker daemon available,
`TOUCH_MONGO_URI` unset by default.

Implementer's changed set (both sub-plan-owned):
`aggregator/ingest.py` (2 688 lines), `tests/test_ingest.py` (2 043 lines).
`tests/test_usage.py` is also owned by sp-08 but was not touched this attempt
(mtime 07-26 08:53); it was run anyway and is green.

---

## 1. Targeted suites (sp-ingest-pipelines owned) — GREEN

Run from the repo root, standalone executables, stdlib only:

| suite | rc | `ok:` assertions | test functions | skips |
|---|---|---|---|---|
| `tests/test_ingest.py` | **0** | 226 | 38 | 1 (live Mongo arm, unset URI) |
| `tests/test_usage.py`  | **0** | 78  | 14 | 1 (live Mongo arm, unset URI) |

The single skip in each is the designed conditional arm and it announces itself:
`skip: live Mongo arm: TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)`.

### 1a. The live-mongod arm actually executed (not merely "skips cleanly")

Started per the `docs/mongo.md` R-42 recipe, on a private port so the two
in-flight sp-05/sp-06 containers were left alone:

```
docker run -d --name touch-mongo-sp08 -p 127.0.0.1:27317:27017 \
  -e MONGO_INITDB_ROOT_USERNAME=touchadmin -e MONGO_INITDB_ROOT_PASSWORD=<random> \
  mongo:7 --auth
```

Both suites re-run with `TOUCH_MONGO_URI` set — rc 0 both. Selected
server-settled evidence:

```
ok: mongod stores the same bytes in every ingest order:
    {'normal': 'dbc5a0a5…', 'reversed': 'dbc5a0a5…', 'shuffled': 'dbc5a0a5…'}
ok: two launch records of ONE runId store the same stop handle on the server in
    either arrival order (wgm4nvzgk) — the $min-per-leaf claim, settled by mongod
    rather than by the model
ok: one message.id under two sessions reads back as ONE document, the same one in
    either arrival order, holding the $min session (dd469822…) — GD-25 on the server
    for the shape the frozen fixtures cannot express
ok: …with the $max still accumulating across the two fragments and the $setOnInsert
    agentId untouched beside the $min
ok: mongod's $group returns exactly the computed rollup (7 agents)
ok: dropping only the database this test constructed: touch_test_153732 (GD-27)
```

Container removed afterwards (`docker rm -f touch-mongo-sp08`); `touch-mongo-sp05`
and `touch-mongo-sp06` untouched and still running.

### 1b. Bare-checkout posture: pymongo absent

`aggregator.ingest` imports with `pymongo` blocked at the meta-path (the driver is
lazily imported inside `mongo_store` functions per GD-21), and both owned suites
are rc 0 with the driver unimportable:

```
ingest rc=0   usage rc=0    (pymongo/bson/dns blocked via a sitecustomize meta-path finder)
```

So the GD-21 / R-56 no-driver, no-mongod arm holds.

---

## 2. Full-suite regression gate — PASS (no NEW failure)

17 files run (4 monitoring + 13 repo):

| green (15) | red (2) |
|---|---|
| monitoring: `test_frontend`, `test_server`, `test_shell`, `test_watcher` | `tests/test_mirror.py` (3 assertions) |
| repo: `test_bootstrap`, `test_fixtures`, **`test_ingest`**, `test_mongo_deploy`, `test_mongo_store`, `test_refs`, `test_stdlib_only`, `test_store`, `test_tailer`, **`test_usage`**, `test_ws` | `tests/test_sessions.py` (1 assertion) |

### The 2 red files are NOT this sub-plan's

Failing assertions:

- `test_mirror.py::test_the_breaker_holds_then_lets_the_mirror_recover`
  — `…proven by the call count: the held ticks made no attempt`
- `test_mirror.py::test_the_generation_sweep_retracts_and_never_deletes`
  — `the first generation lands` (`run(backend.counts()) == {"records": 3, "stream_meta": 3}`)
- `test_mirror.py::test_wipe_and_rebuild_produce_the_same_fingerprint`
  — `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`
- `test_sessions.py::test_a_rebuild_through_mirror_reproduces_the_scan`
  — `wipe + --rebuild reproduces a byte-identical fingerprint`

**Attribution proof (empirical, not argument).** The `aggregator/` + `tests/` +
`docs/` tree was copied to a scratchpad, `aggregator/ingest.py` was *deleted*
there, and the two suites re-run. Result — byte-identical failure sets:

```
mirror   rc=1  FAILED (3): held ticks made no attempt / the first generation lands /
                            identical counts {'records': 8} == {'records': 8, 'writers': 1}
sessions rc=1  FAILED (1): wipe + --rebuild reproduces a byte-identical fingerprint
```

With `ingest.py` present on the real tree the failure sets are the same, member
for member. Ingest is therefore not in the causal path.

**Root cause (informational, for the owning sub-plans).** All four assertions turn
on the mirror's own `writers` lease document appearing in the fake backend's
`counts()` / call tallies — e.g. `counts_before` is `{'records': 8, 'writers': 1}`
while the post-wipe rebuild yields `{'records': 8}`. That is `aggregator/mirror.py`
+ `tests/test_mirror.py` state, matching the prompt's warning about an
**interrupted sp-mirror-deploy attempt**: `mirror.py` (mtime 07-26 11:29) and
`docs/mongo.md` (11:28) are newer than the last green sp-mirror-deploy gate
(attempt 4 recorded "full suite 14/14 green"). `test_sessions` fails through the
same `mirror.rebuild(...)` seam it exercises. **Not fixed here — those files
belong to sp-06 / sp-07.**

---

## 3. Verification against the plans

### Ownership / scope of edits — PASS

`git log --oneline -1` is still `579446e` (no commits). Mtimes show only the two
declared files were written this attempt:

```
07-26 11:44 aggregator/ingest.py      ← this attempt
07-26 11:48 tests/test_ingest.py      ← this attempt
07-26 11:29 aggregator/mirror.py      ← sp-06's interrupted attempt (not ours)
07-26 04:10 aggregator/sessions.py    07-26 04:14 tests/test_sessions.py
07-26 08:53 tests/test_usage.py       (owned, unchanged this attempt)
```

`find . -newermt '-90 minutes'` outside `.claude/` is empty — the suites left no
stray artifacts in the repo, and the live arm dropped only the databases it created.

### Items — PASS

| item | evidence |
|---|---|
| **GD-21** stdlib-only | `ingest.py` imports `datetime/glob/json/os/re/dataclasses` + 3 siblings; no driver name in the module; imports and both suites pass with pymongo blocked; `test_stdlib_only.py` green |
| **GD-22** Mongo off the liveness path | no client, no DB I/O anywhere in the module |
| **GD-24** string `_id`s via `refs` only | `test_every_id_comes_from_refs` re-parses every emitted key |
| **GD-25** upsert algebra | `$inc` appears only in prose forbidding it; the two attempt-2 critique bugs are **fixed** — `usage.sessionId` is now `$min` (`ms.op_min(order_free)` in `map_usage`) and every launch leaf is `$min` under a namespaced `launch.<field>` path (`_launch_paths` + `ms.op_min` in `map_run`). Order-independence asserted in the model **and** on mongod. |
| **GD-26** no delete verbs / no TTL | `deleteOne\|deleteMany\|drop(\|$unset\|expireAfterSeconds` → 0 hits |
| **GD-27** security | live arm drops only its own `touch_test_*` db; containment predicate realpath-rooted (`test_containment_is_rooted_and_resolved_not_a_directory_name`) |
| **GD-28** provenance | `PROVENANCE="harness"` under `$setOnInsert` on every op (visible in the mutation-run payload dumps) |
| **GD-30** latency | `test_the_rebuild_walk_is_read_once_not_once_per_source`: 8 reads over 8 files for all five sources together |
| **R-26** six amendments | uuid-less positional keying; journal ts from transcripts (`test_the_module_has_no_clock`); snapshot back-fill non-fatal (`test_a_live_run_has_no_snapshot_and_that_is_not_an_error`); tokens as upserted docs; `tool-results/` `(sessionId, basename)` scan (`test_the_tool_results_scan_surfaces_unlinked_spills`) |
| **R-47** 12-type bucket table | `RECORD_TYPES = ("user","assistant","system","attachment")`; `queue-operation` in `NO_RENDER_TYPES`, never deduped against its `user` twin; `lineNo`/`byteOffset` asserted by `test_positions_are_stored_on_every_document`; sessionId injected from path with `_normalized.sessionIdSource` |
| **R-49** runs / run_nodes | position-derived ordinals + `journalSeq` (`test_journal_ordinals_are_position_derived`), snapshot back-fill without clobber, launch join as the only session→run join, duplicate launches counted |
| **R-50** usage | `_id = message.id`, `$max` on the four token fields, agentId conflict counted never overwritten, rollups as `$group` sums proven equal to the local sum on a real server |
| **R-25 amended** scope | foreign-slug directories contribute zero observations through both the per-path and the rebuild seam (4 such directories exist on this machine) |
| **R-55** wipe/rebuild equivalence | `--backfill` and `--rebuild` produce the same counts *and* the same bytes over the 1 090-record live corpus |

### Tests are behavioral, not tautological — PASS (mutation-verified)

Four mutations applied to a scratchpad copy of `ingest.py` (the repo tree was
never edited); every one was caught:

| mutation | result |
|---|---|
| `ms.op_min(launch)` → `ms.op_set(launch)` in `map_run` (re-introduces critique-attempt-2 **B2**) | rc 1, `FAIL: two launches that disagree on taskId and summary fingerprint identically in either order…` + 1 more |
| `usage.sessionId` moved from `$min` to `$setOnInsert` (re-introduces **B1**) | rc 1, 3 FAILs incl. `…the /clear-split corpus fingerprints identically in all three orders too` plus a dump of the two divergent payloads |
| `sessionId` silently dropped from the usage payload | rc 1, hard `KeyError: 'sessionId'` in `test_the_algebra_is_order_independent` |
| `RECORD_TYPES` widened with `"queue-operation"` (R-47 bucket table) | rc 1, `FAIL: queue-operation ⇒ stream_meta even carrying a uuid — the table is by TYPE` |

Note on the second mutation: the pure "does it fingerprint the same" arm alone
would not catch a *drop* of the field (a dropped field is trivially order-free) —
the companion value witness (`doc["sessionId"] == min(SPLIT_SESSIONS)`) is what
closes that hole. It is present. No action required; recorded so the property and
its value-witness stay paired through any future refactor.

---

## 4. Failures attributable to this change set

**None.**

## 5. Reproduction commands

```bash
cd /home/laniakea/Projects/touch
python3 tests/test_ingest.py     # rc 0, 226 ok, 1 designed skip
python3 tests/test_usage.py      # rc 0,  78 ok, 1 designed skip
rc=0; for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done
for t in tests/test_*.py; do python3 "$t" || rc=1; done; exit $rc   # rc 1 from the 2 pre-existing red files only
```
