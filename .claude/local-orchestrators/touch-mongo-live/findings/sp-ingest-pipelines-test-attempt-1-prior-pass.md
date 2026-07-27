# sp-ingest-pipelines — test gate, attempt 1 — **PASS**

Read-only gate. No source or test file was edited.

Implementer's declared change-set:
- `/home/laniakea/Projects/touch/aggregator/ingest.py`
- `/home/laniakea/Projects/touch/tests/test_ingest.py`
- `/home/laniakea/Projects/touch/tests/test_usage.py`

---

## 1. Targeted suites (owned files) — GREEN

Run from the repo root, stdlib, standalone executables:

| suite | exit | assertions | skips |
|---|---|---|---|
| `python3 tests/test_ingest.py` | 0 | 143 `check(...)` over 25 test functions | 2, both clean and explained |
| `python3 tests/test_usage.py` | 0 | 57 `check(...)` over 10 test functions | 1, clean |

Skips observed in the default (no-`TOUCH_MONGO_URI`) environment:
- `test_the_launch_tool_use_result_is_the_taskid_join` — "no frozen fixture
  carries a launch `toolUseResult`; shape taken from `292fc08c….jsonl:57`
  (`w4hiywrt6` / `wf_930e210a-6da`), verbatim in `read_launch`". This is a
  documented fixture gap, not a silenced failure — the surrounding 9 checks in
  that same function still execute and assert the join, the `taskId` stop
  handle, the `runs` fields and the negative cases.
- `test_live_mongod_arm` in both files — R-42 arm, skipped when
  `TOUCH_MONGO_URI` is unset. Verified live below.

## 2. Full-suite regression gate — GREEN (17/17)

```
cd /home/laniakea/Projects/touch && rc=0
for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done
for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc
```

`RC=0`. All green:

- monitoring module: `test_frontend`, `test_server`, `test_shell`, `test_watcher`
- repo: `test_bootstrap`, `test_fixtures`, `test_ingest`, `test_mirror`,
  `test_mongo_deploy`, `test_mongo_store`, `test_refs`, `test_sessions`,
  `test_stdlib_only`, `test_store`, `test_tailer`, `test_usage`, `test_ws`

Notably `test_stdlib_only.py` and `test_mirror.py` — the two suites most likely
to be broken by a new module registering mirror sources/mappers — are green, so
`ingest.py`'s five mapper registrations did not disturb sp-06's mirror contract.

### 2a. Bare-checkout arm (no third-party packages) — GREEN

`pymongo==4.17.0` *is* installed in this environment, so the default run does
not by itself prove the GD-21/R-56 no-driver posture. Re-ran all 13 repo suites
with a `sitecustomize.py` meta-path hook that raises `ImportError` for
`pymongo`, `bson`, `gridfs` and `dns` (verified the block actually fires — the
naive `find_module` shim is ignored on Python 3.13, `find_spec` was used):

```
PYTHONPATH=<blocker> python3 tests/test_*.py   →   13/13 PASS
```

Both owned suites pass with the driver unavailable; nothing in `ingest.py`
imports `pymongo` or `bson` (grep: 0 hits), so the GD-21 exception stays
confined to `mongo_store.py` / `mirror.py`.

### 2b. Live-mongod arm — GREEN (executed, not skipped)

A `mongo:7` container from a prior sub-plan was already up on the R-42
loopback+auth recipe (`touch-mongo-sp05`, `127.0.0.1:27117`, root `touch`).
Re-ran the two owned suites with
`TOUCH_MONGO_URI=mongodb://touch:touchpw@127.0.0.1:27117/?authSource=admin`:

`test_ingest.py::test_live_mongod_arm`
- identical stored-byte fingerprint for normal / reversed / shuffled ingest
  (`dbc5a0a5…6de742` three times) — GD-25 order-independence on the server, not
  only in the model
- 9 run-node documents survive a real `$setOnInsert` on the `_id`'s components
- drops only the database it created (`touch_test_138731`), per GD-27

`test_usage.py::test_live_mongod_arm`
- identical fingerprint across the three orders (`5183d310…8371fb`) — `$max` is
  commutative server-side
- stored totals equal the in-memory model exactly:
  `{'in': 6511, 'out': 319617, 'cached': 28491668, 'cache_write': 1220240}`
- 667 observations collapse to 328 upserts (message-id dedup as a key)
- a conflicting `agentId` never overwrites the stored one on the server
- a full second pass moves no total (re-ingest-after-rewrite property)
- mongod's `$group` returns exactly the computed 7-agent rollup
- drops only `touch_test_usage_138746`

Both suites also skip this arm cleanly when the URI is unset (verified in §1),
so the R-56 no-mongod arm holds.

## 3. Plan verification

Against `plan/touch-mongo-live-subplans.md` §"sp-08 — ingest-pipelines" and
items R-26 / R-47 / R-49 / R-50 (+ GD-24, GD-25, GD-28, SD-1, SD-11).

**Ownership — clean.** `aggregator/` and `tests/` are still untracked
directories, so `git diff` cannot discriminate; used mtimes instead. The three
declared files are the only ones touched in this loop's window
(`ingest.py` 05:21, `test_ingest.py` 05:20, `test_usage.py` 05:24); the next
most recent files are `sessions.py`/`test_sessions.py` at 04:10/04:14 (the
preceding sp-sessions-arm loop) and `mirror.py` at 02:45. `git status` for
tracked paths is unchanged from the loop start apart from the orchestrator's own
`events.jsonl`. `agents.py` does not exist, and `sessions.py`, `legacy.py`,
`mirror.py` were not touched — the explicit "do not let it touch" list holds.

**R-47 (12-type bucket table).** `RECORD_TYPES = ("user","assistant","system",
"attachment")` ⇒ `records` by uuid; `KNOWN_META_TYPES` + anything unknown ⇒
`stream_meta` positional. Tested as *behavior*, not restated as a constant:
`test_the_bucket_table_is_the_only_decider` proves the decision is by TYPE even
when a meta type carries a uuid, that an unknown/future type is bucketed
positionally rather than dropped (GD-26), and that an uppercase uuid is refused
as a key. `test_the_frozen_corpus_buckets_without_collapse` runs the table over
the frozen bytes: 1157 uuid-bearing ⇒ `records`, 30 uuid-less ⇒ `stream_meta`,
counts recomputed from the bytes rather than asserted against a literal, and a
second pass changes no byte. `sessionId` injection from the path, with the
source recorded on the document and the path winning over a contradicting
in-line claim, is covered by two dedicated tests. `lineNo` + `byteOffset` are
asserted present on every document with the offset pointing at the line's first
byte. `queue-operation` carries `render:false`, is the *only* type that does,
and its `user` twin stays a separate document.

**R-26 with all six amendments.**
- uuid-less keying → `<sessionId>#<line:08d>`, uniqueness/ascension asserted.
- cross-session agent assembly deferred → structurally enforced: a mapper that
  tries to write `agents` is refused, and the allowed set is exactly the five
  GD-24 collections (`COLLECTIONS`).
- journal timestamps from transcripts, `now()` forbidden → enforced on the AST
  (`test_the_module_has_no_clock`, hits `[]`), corroborated by grep: zero
  `datetime.now` / `time.time` / `utcnow` in `ingest.py`. Times demonstrated to
  come from the transcripts across a `/clear`-split two-session run.
- snapshot back-fill never an error → the live-run fixture has no `workflows/`
  dir at all; the miss is counted, the run doc is still built from the first
  journal `started`, and no status/totals are fabricated.
- `agentCount` → `harnessTotals.nodeCount`, display-only, and neither name
  survives at top level.
- tokens as upserted docs → §R-50 below.
- persisted-output regex + spill scan: the regex fires on the real frozen spill
  pointers, containment normalizes `..` before deciding, 12 real false-positive
  files do *not* fire, the `tool-results/` scan is keyed `(sessionId, basename)`,
  and an unpointed spill stays `linkedToolUseId: None` ("unlinked spilled
  output" is a renderable state, not an error). A missing `tool-results/` is not
  an exception.

**R-49 (runs/run_nodes).** Run doc from the FIRST journal `started`; ordinal is
the 0-based count of preceding `started` with the same key (asserted verbatim
against R-49's acceptance on `wf_455b348c-e17`: 9 nodes, 6 keys, ordinals
`0,0,0,0,0,0,1,1,1`), `journalSeq` = physical line, no DB counter. The launch
`toolUseResult` is persisted on `runs` with `taskId`/`transcriptDir`/
`scriptPath`/`workflowName` as the session→run join and run-level stop handle.
Results attach by `agentId` with an explicit oldest-un-resulted fallback and a
counted no-match case — a killed run's second attempt cannot inherit a verdict.
No `state` field is stored on any node (GD-23: liveness is read-time).

**R-50 (usage).** `_id = message.id` via `refs.usage_key`; the update uses
exactly `$setOnInsert` / `$max` / `$min` and nothing else, with all four token
fields under `$max` and the ids immutable. No `$inc` anywhere in the module
(the three `$inc` grep hits are prohibition docstrings). `mongo_store` fences
the four as accumulable, so a `$set` on `out` is *refused by validate_update* —
the rule is structural, demonstrated, not merely asserted. Rollups are `$group`
sums over absolute documents; a grouping key outside the three indexed fields is
refused; the harness's own `totalTokens` (1089990) is explicitly shown *not* to
be substituted for the computed figure. The agentId-conflict counter fires with
both ids and never overwrites.

**GD-25 / GD-28 / SD-1 / SD-11.** Order-independence is asserted as a
fingerprint over 1765 operations (normal/reversed/shuffled identical, *plus*
equal counts, which is the half that catches a silent collapse), and double
ingest is a no-op. Every `_id` parses back through `refs.ref_key`. Every
document carries `provenance: harness` as an immutable. No mapper does I/O
(asserted over the mapper set, empty violation list). Mirror sources answer only
for paths they own, including the subtle case that a snapshot's `workflows/`
directory is not a run dir.

**Non-tautology.** The assertions are measurements against the frozen corpus
with numbers recomputed from the bytes (1157/30 buckets, 877765-byte oversize
line stored whole, 667 observations → 328 message ids, naive-vs-deduped
over-count of 1.95× on `cached`, 12 real false-positive spill files, 4 foreign
slug directories excluded), plus negative arms for every rule. The rebuild seam
(`path=None`) is checked to reproduce the direct walk record-for-record (1090),
and the read memo is shown to be invalidated by a same-size rewrite via
`(dev, ino, size, mtime)`. No check compares a value to itself or restates a
module constant.

---

## 4. Failures

None. No new failure, no baseline regression, no skip that hides a defect.

## 5. Verdict

**PASS** — targeted suites green, full suite 17/17 green, bare-checkout
(no-pymongo) arm 13/13 green, live-mongod arm exercised and green, ownership
boundary clean, all owned items present and behaviorally asserted.
