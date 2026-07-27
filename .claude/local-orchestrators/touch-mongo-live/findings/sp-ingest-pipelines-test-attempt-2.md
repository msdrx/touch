# sp-ingest-pipelines — test gate, attempt 2 — **PASS**

Read-only gate. No source or test file was edited by this gate.

Implementer's declared change-set:
- `/home/laniakea/Projects/touch/aggregator/ingest.py`
- `/home/laniakea/Projects/touch/tests/test_ingest.py`
- `/home/laniakea/Projects/touch/tests/test_usage.py`

---

## 1. Targeted suites (owned files) — GREEN

Run from the repo root; stdlib-only, standalone executables.

| suite | exit | test fns | `check(...)` sites | skips |
|---|---|---|---|---|
| `python3 tests/test_ingest.py` | 0 | 33 | 185 | 1 (live-mongod arm, clean) |
| `python3 tests/test_usage.py` | 0 | 12 | 65 | 1 (live-mongod arm, clean) |

Both footers read `all … tests passed`.

The only skip in the default environment is `test_live_mongod_arm`
("TOUCH_MONGO_URI is not set — R-42's loopback+auth recipe") in each file.
There is one further *conditional* skip in `test_ingest.py:539` (the
containment arm degrades honestly if the frozen pointers do not name this
machine's `~/.claude` root) — it did **not** fire here; the arm ran and asserted.

### Attempt-1 critique nits are demonstrably fixed in the tests/code

- **n1 (skip-then-assert)** — `test_the_launch_tool_use_result_is_the_taskid_join`
  no longer calls `skip()`. A new `note()` helper (`tests/test_ingest.py:121-128`)
  prints the fixture-gap remark without polluting the skip list; all eleven
  assertions of that arm run and are green. The footer no longer misreports the
  arm as skipped.
- **n2 (absolute `source_path` in `_launch_scan`)** — now
  `source_path=_rel(root, scan.path)` (`aggregator/ingest.py:2308`), matching the
  two journal call sites (`:1474`, `:1566`). No absolute `source_path` remains.
- **n3 (a journal `result` with no `key` dropped silently)** — an explicit
  `elif kind == "result":` fallthrough (`aggregator/ingest.py:1334-1341`) now
  increments `skipped["unmatched_result"]`, and `test_a_result_attaches_by_agent_id_and_never_guesses`
  asserts both that the counter fires and that no node is attached by guesswork.
- **B1 (per-path arm ignored R-25 project scope)** — covered by
  `test_backfill_and_rebuild_see_exactly_the_same_files`, which shows the backfill
  walk *sees* the foreign slug's 5 files and yields **zero** observations from
  them (the same rooted `sessions.scoped_dirs` test the rebuild arm gets), and
  that `--backfill` and `--rebuild` produce identical counts **and** bytes
  (`{'records': 1090, 'run_nodes': 7, 'runs': 1, 'usage': 328}`).
- **m3 (conflict counter with no runtime path)** — new
  `test_the_conflict_counter_has_a_runtime_path` in `test_usage.py` drives the
  counter through the scan, asserts the key is pre-declared, that the stored
  `agentId` is unchanged ($setOnInsert), that the ordinary split-record case does
  *not* fire it, and that the frozen corpus raises none.

## 2. Full-suite regression gate — GREEN (17/17)

```
cd /home/laniakea/Projects/touch && rc=0
for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done
for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc
```

`SUITE RC=0`. All pass:

- monitoring module (baseline four): `test_frontend`, `test_server`,
  `test_shell`, `test_watcher`
- repo (thirteen): `test_bootstrap`, `test_fixtures`, `test_ingest`,
  `test_mirror`, `test_mongo_deploy`, `test_mongo_store`, `test_refs`,
  `test_sessions`, `test_stdlib_only`, `test_store`, `test_tailer`,
  `test_usage`, `test_ws`

No baseline failure and no new failure. `test_stdlib_only.py` and
`test_mirror.py` — the two suites most sensitive to a module registering new
mirror sources/mappers — are green, so `ingest.py`'s five registrations still
do not disturb sp-06's mirror contract.

### 2a. Bare-checkout arm (GD-21 / R-56: no third-party packages) — GREEN 13/13

`pymongo==4.17.0` is installed in this environment, so the default run alone
does not prove the no-driver posture. Re-ran all 13 repo suites under a
`sitecustomize.py` meta-path hook whose `find_spec` raises `ImportError` for
`pymongo`, `bson`, `gridfs`, `dns` (block verified to fire before running):

```
PYTHONPATH=<blocker> python3 tests/test_<x>.py   →   13/13 PASS   (BARE RC=0)
```

Both owned suites pass with the driver absent; `grep -c 'pymongo\|bson'
aggregator/ingest.py` = **0**, so the GD-21 exception stays confined to
`mongo_store.py` / `mirror.py`. Mongo-dependent arms skip cleanly rather than
erroring.

### 2b. Live-mongod arm (R-42) — GREEN, executed rather than skipped

A `mongo:7` container from an earlier sub-plan is up on the R-42 loopback+auth
recipe (`touch-mongo-sp05`, `127.0.0.1:27117`). With
`TOUCH_MONGO_URI=mongodb://touch:touchpw@127.0.0.1:27117/?authSource=admin`
both owned suites pass.

`test_ingest.py::test_live_mongod_arm`
- identical stored-byte fingerprint for normal / reversed / shuffled ingest
  (`dbc5a0a5…6de742` three times) — GD-25 order-independence **on the server**,
  not only in the model
- 9 run-node documents survive a real `$setOnInsert` on the `_id`'s components
- mongod stores the launch sub-document beside the snapshot's own fields and its
  `$jsonSchema` accepts the shape; both arrival orders read back as the same
  document (the m1/`runs`-disjointness fix, verified server-side)
- drops only the database it created (`touch_test_141872`), per GD-27

`test_usage.py::test_live_mongod_arm` — green; `$max` commutative server-side,
stored totals equal the in-memory model, conflicting `agentId` never overwrites,
`$group` rollup matches the computed figure, drops only its own test database.

Both arms skip cleanly with the URI unset (verified in §1), so the R-56
no-mongod posture holds.

## 3. Plan verification

Against `plan/touch-mongo-live-subplans.md` §"sp-08 — ingest-pipelines" and items
R-26 (all six amendments), R-47, R-49, R-50, plus GD-24/25/26/28, SD-1, SD-11.

**Ownership — clean.** `aggregator/` and `tests/` are still untracked
directories, so `git diff` cannot discriminate; mtimes were used. The three
declared files are the only ones touched in this loop's window
(`ingest.py` 05:53, `test_usage.py` 06:01, `test_ingest.py` 06:04); the next
most recent files are `sessions.py` 04:10 / `test_sessions.py` 04:14 (the prior
sp-sessions-arm loop) and `mirror.py` 02:45. `git status --porcelain` for
tracked paths is unchanged apart from the orchestrator's own `events.jsonl`.
The explicit prohibition list holds: `agents.py` does not exist; `sessions.py`,
`legacy.py`, `mirror.py` untouched.

**R-47 (12-type bucket table).** `test_the_bucket_table_is_the_only_decider`
proves the decision is by TYPE even when a meta type carries a uuid, that an
unknown/future type buckets positionally rather than being dropped (GD-26), and
that an uppercase uuid is refused as a key. On the frozen corpus:
1157 uuid-bearing ⇒ `records`, 30 uuid-less ⇒ `stream_meta`, counts recomputed
from the bytes; a second pass changes no byte. `sessionId` injection from the
path (with the source recorded and the path overruling a contradicting in-line
claim), `lineNo` + `byteOffset` on every document with the offset at the line's
first byte, `queue-operation` uniquely `render:false` and never deduped against
its `user` twin — all asserted.

**R-26, six amendments.** uuid-less keying `<sessionId>#<line:08d>` with
uniqueness/ascension; cross-session agent assembly deferred (a mapper writing
`agents` is structurally refused; the allowed set is exactly the five GD-24
collections); no clock (AST assertion, hits `[]`; grep: 0 `datetime.now` /
`time.time` / `utcnow`); snapshot back-fill never an error (the live-run fixture
has no `workflows/` dir — the miss is counted, the run doc is still built from
the first journal `started`, nothing fabricated); `agentCount` →
`harnessTotals.nodeCount`, display-only, neither name at top level; tokens as
upserted docs (see R-50). Persisted-output regex fires on the real spill
pointers and not on the 12 real false-positive files; containment is realpath-
rooted (symlink escape, same-named `tool-results/` outside the root, bare
relative pointer, wrong depth, and `..` all refused; both sides resolved; the
un-rooted case counted as a *different* fact from an escape) — the attempt-1 M1.
The `tool-results/` scan is keyed `(sessionId, basename)`, an unpointed spill
stays `linkedToolUseId: None`, and an absent directory is not an exception.

**R-49.** Run doc from the FIRST journal `started`; ordinal = 0-based count of
preceding `started` with the same key, asserted verbatim against R-49's
acceptance on `wf_455b348c-e17` (9 nodes, 6 keys, ordinals `0,0,0,0,0,0,1,1,1`);
`journalSeq` = physical line; no DB counter. Launch `toolUseResult` persisted on
`runs` under `launch{}` (taskId / transcriptDir / scriptPath / workflowName) as
the session→run join and run-level stop handle, disjoint from the snapshot's
fields so arrival order cannot decide the document. Results attach by `agentId`
with an oldest-un-resulted fallback and counted no-match cases (both
"agentId unknown" and the new "no key"). No `state` on any node (GD-23).

**R-50.** `_id = message.id` via `refs.usage_key`; update uses exactly
`$setOnInsert` / `$max` / `$min`, four token fields under `$max`, ids immutable;
no `$inc` in the module. `mongo_store.validate_update` refuses a `$set` on `out`
— structural, demonstrated. Rollups are `$group`/`$sum` over absolute documents,
grouping keys restricted to the three indexed fields, and the harness's own
`totalTokens` (1089990) is explicitly shown *not* to be substituted.

**Non-tautology.** Assertions are measurements recomputed from the frozen bytes
with negative arms: 1157/30 bucket split, an 877765-byte line stored whole,
667 observations → 328 message ids (naive vs deduped over-count 1.95× on
`cached`), 12 false-positive spill files, 4 foreign slug directories excluded,
order-independence as a fingerprint over 1765 operations *plus* equal counts
(the half that catches a silent collapse), the rebuild seam (`path=None`)
reproducing the direct walk record-for-record (1090), and a read memo
invalidated by a same-size rewrite via `(dev, ino, size, mtime)`. No check
compares a value to itself or restates a module constant.

## 4. Failures

None. No new failure, no baseline regression, no skip concealing a defect.

## 5. Verdict

**PASS** — targeted suites green (33 + 12 test functions, 250 checks),
full suite 17/17 green, bare-checkout no-driver arm 13/13 green, live-mongod
arm executed and green, ownership boundary clean, all owned items present and
behaviorally asserted, and every attempt-1 critique nit (n1/n2/n3) plus the B1
scope finding visibly addressed in code and covered by a test.
