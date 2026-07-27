# sp-ingest-pipelines — adversarial critique, attempt 1 (resumed pass)

**Verdict: APPROVED** — 0 blocker, 0 major, 4 minor, 6 nit.
**depth: in-scope.** **critical_defect: false.**

Reviewed in full (untracked tree, so whole-file review rather than `git diff`):

* `/home/laniakea/Projects/touch/aggregator/ingest.py` — 2 688 lines
* `/home/laniakea/Projects/touch/tests/test_ingest.py` — 2 043 lines

against sp-08 in `plan/touch-mongo-live-subplans.md:241-268`; amendment items
R-47 / R-49 / R-50 (`plan/touch-mongo-live-plan.md:684-813`) and R-44/R-45's
store-and-mirror rules they must satisfy; base R-26
(`touch-full-recon-plan.md:641-657`); GD-7, GD-11, GD-15, GD-21…GD-30.

Ownership is clean. Only the two owned files carry this attempt's mtimes
(11:44 / 11:48); every other `aggregator/*.py` and `tests/*.py` predates it
(`mirror.py` 11:29 = the interrupted sp-mirror-deploy attempt, not this one),
`docs/mongo.md` untouched, HEAD still `579446e`. No commit, no stray artifact.

> **Note on this file.** An earlier round of this same sub-plan left a
> `…critique-attempt-1.md` (REJECTED, 1 blocker) and a `…critique-attempt-2.md`
> (REJECTED, 2 blockers) on disk. The first has been preserved as
> `sp-ingest-pipelines-critique-attempt-1-prior-round-superseded.md`; attempt-2's
> file is untouched. Their dispositions against the current tree are in
> §"Regression check" below — every one of them is fixed.

---

## 1. What I tried to break, and could not

I did not take the test gate's word for the load-bearing properties. These are
independent re-derivations run against the **real** `~/.claude` corpus, not the
frozen fixtures:

1. **GD-25 over the whole in-scope live corpus.** Drove all five
   `MIRROR_SOURCES` with `root=~/.claude`, `cwd=/home/laniakea/Projects/touch`
   — 27 920 observations → `{records: 16 742, run_nodes: 122, runs: 8,
   stream_meta: 1 055, usage: 5 034}` — and fingerprinted normal / reversed /
   shuffled(seed 7) through `mongo_store.apply_operations`. **All three
   fingerprints identical.** This corpus is 25× the fixtures and contains both
   shapes the module says break naive operators.
2. **The GD-7 multi-journal deviation, on the run it was written for.**
   `wf_1a3ffcdd-c60` genuinely has two `journal.jsonl` files; 40 `started`
   records across them yield **40 distinct `(runId,key,ordinal)` ids** — zero
   collisions — and all six single-journal runs are numbered exactly as before.
   `_ordinal_offsets` does what its docstring claims, on the data it claims it
   for.
3. **R-49's acceptance, literally.** `wf_455b348c-e17` ⇒ 9 nodes across 6 keys,
   ordinals `0,0,0,0,0,0,1,1,1`, 7 resultless nodes, `skipped` all zero.
4. **R-50's identity claims.** Re-measured the live corpus independently of the
   module: 5 033 distinct `message.id`s, **0 divergent `agentId`, 0 divergent
   `runId`, 3 divergent `sessionId`** — exactly the figures the docstrings
   assert, which is precisely why `sessionId` is `$min` and the other two are
   `$setOnInsert`. Also confirmed that **only** `assistant` records carry
   `message.usage` / `message.id` (10 006 / 10 006), so no token-bearing record
   type is missed by the `type == "assistant"` gate at `ingest.py:1177`.
5. **`records._id = uuid` collision risk (my main suspected GD-25 hole).**
   Scanned every uuid-bearing record on disk: 16 760 records, 16 760 distinct
   uuids, **0 uuids appearing in more than one file**. So the `$set` of
   `lineNo`/`byteOffset`/`sessionId` on a uuid-keyed document has exactly one
   writer per `_id`, and the "rotated copy re-keys a record at a different line"
   attack does not exist on this harness. Closed.
6. **`read_launch`'s predicate against reality.** Surveyed every
   `toolUseResult` in `~/.claude/projects` carrying a top-level string `runId`:
   10 of 10 are genuine launches (`status:async_launched`,
   `taskType:local_workflow`). See minor 3 for why the predicate is still looser
   than R-49's.
7. **Storage sizing sanity.** Stored BSON is 1.16× raw transcript bytes
   pre-compression (4 443 326 / 3 833 935 over the fixture corpus) — consistent
   with GD-22's measured `0.53×` on-disk basis once WiredTiger compression
   applies. Recorded because the full-`body`-plus-extracted-fields shape looks
   like double storage at first glance and is not.
8. **Static posture.** No `$unset`, no `deleteOne/deleteMany/drop`, no
   `expireAfterSeconds`, no `$inc`, no driver name, no `now()`/`time.time`/
   `utcnow`, no Mongo client — all absent by grep and by the module's own static
   tests (`test_the_module_has_no_clock`, `test_every_id_comes_from_refs`).
9. **Suite.** `python3 tests/test_ingest.py` → 226 ok / 0 fail in 4.8 s, and the
   live-mongod arm **skips cleanly** with no `TOUCH_MONGO_URI` and no pymongo.

**On tautology.** The test file survives the charge where it matters.
`test_the_algebra_is_order_independent` and
`test_the_set_on_insert_payload_never_varies_for_one_id` both construct the
`/clear`-split corpus (`clear_split_root`) that the frozen fixtures
*structurally cannot* express, and the latter closes by reconstructing R-50's
literal pre-fix payload to prove the property fires on the real defect rather
than restating the fix. `test_the_frozen_corpus_buckets_without_collapse`
recounts from the raw bytes and compares **distinct `_id` counts**, so it
catches MONGOSCHEMA-1's silent-collapse class even though it reuses
`bucket_of` — whose table is pinned separately, one type at a time, in the test
immediately above it. `test_two_launch_records_of_one_run_do_not_race_for_the_
stop_handle` and `test_a_foreign_slug_holding_the_same_run_id_contributes_
nothing` are both real negative tests.

---

## 2. Findings

### minor 1 — `bad_uuid` is a counter no code path can raise, and the demotion it exists for is silent
`aggregator/ingest.py:407` (declaration), `:526-533` (`bucket_of`), `:1173`
(the branch that should count it).

`bucket_of`'s predicate is *type ∧ lowercase-uuid*, so a
`user|assistant|system|attachment` record whose `uuid` is malformed — the
uppercase spelling `tests/test_ingest.py:297` deliberately exercises — is
demoted to a positional `stream_meta` document that still reports
`type:"user"`. **Nothing counts that demotion.** `_skips()` declares
`"bad_uuid": 0` with the comment *"a uuid-typed record whose uuid refs
rejects"*, and it is the only one of the sixteen counters never incremented
(grep: exactly one occurrence in the file).

That is the shape the module argues against twice elsewhere — at `:1388-1395`
(*"a lost verdict that increments nothing is invisible to `/health`"*) and at
`:1185-1198` (*"a counter no code path can raise is a silent anomaly"*). A
uuid-bearing type arriving with a broken uuid is a CLI regression an operator
must see; today it is indistinguishable from a `mode` line.

**Fix.** In `read_transcript`, in the `else:` arm at `:1173`:

```python
else:
    if record.get("type") in RECORD_TYPES and record.get("uuid") is not None:
        scan.skipped["bad_uuid"] += 1
    positional(line, str(record.get("type") or UNPARSED_TYPE), ...)
```

and extend the uppercase-uuid arm of `test_the_bucket_table_is_the_only_decider`
with a `read_transcript` call asserting `skipped["bad_uuid"] == 1` and
`skipped["bad_uuid"] == 0` on the frozen corpus.

### minor 2 — R-44's oversize guard is applied by two of the five mappers
`aggregator/ingest.py:2121` and `:2159` (guarded) vs `:2213` (`map_usage`),
`:2284-2286` (`map_run`), `:2354-2357` (`map_run_node`) (unguarded).

R-44 states the rule for a **document**, not for a collection: *"oversize
guard: document > 8 MB ⇒ stub `{oversize:true, bytes, sourcePath, byteOffset}`
(never silently dropped)"* (`touch-mongo-live-plan.md:702-706`). `map_record`
and `map_stream_meta` honour it; the three run/usage mappers do not — and two of
the fields they write are unbounded in principle. `run_nodes.result` is the
journal's verbatim agent-authored `result` (GD-11 polymorphic: object *or* free
string), and `runs.phases` / `runs.summary` / `launch.summary` come from a
snapshot whose sibling `script` field is already ~15 KB of source text on all
three frozen specimens. Above the 16 MiB BSON cap the outcome is not R-44's
*marked* record — it is a mongod write rejection on a collection GD-26 forbids
cleaning up. Converting that into a stub is the entire purpose of the guard.

`usage` documents are genuinely fixed-size and may stay unguarded — but then say
so in `map_usage`, because the asymmetry currently reads as an oversight.

No test covers the guard firing from this module:
`test_the_oversize_line_is_stored_whole` (`tests/test_ingest.py:551-561`)
asserts the real 877 KB line does **not** trip it.

**Fix.** In `map_run` and `map_run_node`, between `prepare_document` and
`_split_ops`:

```python
prepared, _report = ms.prepare_document("run_nodes", doc)
prepared, _size = ms.guard_oversize("run_nodes", prepared,
                                    source_path=obs.source_path)
```

and add one arm building a `RunNodeObservation` with a >8 MB `result` string,
asserting `oversize is True`, `bytes > limit`, and that
`_id`/`runId`/`key`/`ordinal` survive on the stub (they are in
`guard_oversize`'s `spec.required` keep-list).

### minor 3 — `read_launch` fires on *any* `toolUseResult` with a `runId`, and `refs.run_key` applies no runId grammar
`aggregator/ingest.py:708-737` (predicate at `:725-727`); `refs.py:497-498`
(`_build_run` is a bare `escape_component(runId)` — no validator).

R-49's join is the launch record: `{status:"async_launched",
taskType:"local_workflow", taskId, runId, transcriptDir, scriptPath,
workflowName}`. The implemented predicate is only *"`toolUseResult` is a dict
and `runId` is a non-empty string"*. The other tool results whose entire subject
is a runId are the harness's own workflow-control tools (TaskGet / TaskStop /
TaskList shapes) — and one of those would mint a permanent `runs` document
(GD-26 forbids deleting it) and `$min`-merge `launch.status` / `launch.summary`
from a **status query** into the field amended GD-8 designates as the run-level
stop handle. `runs._id` would also accept an arbitrary string, since the run ref
grammar is a bare escape with no `wf_` shape check.

Latent, not live: 10 of 10 `runId`-bearing tool results on disk are real
launches. That is why this is minor — but it is one tool-result shape away from
unrecoverable rows, and this module already argues (`:2513-2521`) that
wrong-target writes here are *not undoable*.

**Fix.** Gate the parse and count the refusal:

```python
if result.get("taskType") not in (None, "local_workflow"):
    return None                      # count `foreign_launch`
if not _RUN_ID_RE.match(run_id):     # ^wf_[0-9a-f]{8}-[0-9a-f]{3}$
    return None
```

Keep the `taskType is None` tolerance if a future CLI dropping the field should
still join; do **not** keep the runId-grammar tolerance. Add a negative arm to
`test_the_launch_tool_use_result_is_the_taskid_join` feeding a
`{"runId": "...", "status": "completed"}` status-shaped result and asserting
zero run observations.

### minor 4 — the walk memos are unbounded module globals that outlive the walk, and nothing in production releases them
`aggregator/ingest.py:2403-2404` (declaration), `:2407-2413`
(`reset_read_cache`), `:2542-2550` (`_transcript_walk`), `:2600-2611`
(`_run_scans`).

`_transcript_walk` materialises a `TranscriptScan` — a full parsed `body` dict
per record — for **every** in-scope transcript and stores the whole list in a
module global that is never dropped. Measured with `tracemalloc` on the fixture
corpus: **9 767 058 bytes retained for 3 694 455 bytes of transcripts — 2.64×**,
held for the life of the process. Extrapolated to the 63 MB
`~/.claude/projects` in this sandbox that is ~165 MB pinned. `Mirror.rebuild`
additionally materialises every mapped op before dropping `derived`
(`mirror.py:2400-2405`, its own stated tradeoff), so peak is roughly double.

`reset_read_cache()` exists but is called **only** from `tests/test_ingest.py`
— grep across `aggregator/` finds no production caller. The contrast with the
sibling memo is the tell: `_JOURNAL_KEYS` is capped at 32 with a two-line
rationale (`:1486-1491`) while the memo three orders of magnitude larger has
neither a cap nor a release. The explanatory note at `:2371-2399` justifies why
the memo exists and never states what it costs.

Today `--rebuild` is a short-lived CLI (`mirror.main`), so this is not a live
server leak — but sp-12/sp-13 will want a rebuild endpoint, and the memo will
then be a permanent per-process retention of the corpus.

**Fix.** Either (a) clear `_TRANSCRIPT_WALK["scans"]` / `_RUN_WALK["scans"]`
once the last registered source has consumed the generation (keep the key, drop
the payload), or (b) state the ~2.6× retention in the `:2371-2399` note and have
`mirror.rebuild` call `ingest.reset_read_cache()` in a `finally:` — with a test
asserting the memo is empty after a rebuild.

### nit 5 — an unparsable line's bytes are not stored, only its position
`aggregator/ingest.py:351-354` (the claim), `:1120` (the call), `:2152` (the
mapper's `body` is conditional and never set on this path).

`UNPARSED_TYPE`'s comment says *"It is stored, positionally, with the parse
error — GD-26: data is never dropped quietly"*. What actually reaches
`stream_meta` is `{type:"_unparsed", parseError, lineNo, byteOffset,
sessionId}` — the line's text exists only in a file that GD-26/SD-10's whole
premise says may be rewritten under you. `tests/test_ingest.py:522-548` asserts
the document exists and never asserts the content, so the gap is invisible to
the suite.

**Fix.** Thread `line.text` through `positional()` onto the observation and into
the document as `raw` (`guard_oversize` already bounds it) — or soften the
comment to "its position and its parse error are stored". The first is cheap and
matches the stated rule.

### nit 6 — `_normalized.dropped:["session_id"]` is a persisted claim the boundary does not make
`aggregator/ingest.py:86-87` and `:2026-2027` (the claim), `:2042-2043` (the
field), `tests/test_ingest.py:431` (the assertion that contradicts it).

The docstring says the snake-case duplicate *"is dropped at the boundary"* and
R-47 says *"`session_id` dropped, noted in `_normalized`"*. Verified on a
synthetic record: `doc["body"]["session_id"] == "snake"` — it is stored
verbatim, and the test asserts exactly that ("the body keeps the source's own
bytes"). What actually happens is that the key is *not promoted* to a top-level
field. That is defensible (GD-26 argues against dropping bytes), but a stored
`_normalized.dropped` list naming a field the store still holds is a claim the
document cannot back, which is the discipline GD-28 exists for.

**Fix.** Rename the key (`"notPromoted"`), or keep `dropped` and actually pop
`session_id` out of `body`, preserving the value under
`_normalized.droppedValues`. Either way, make one of the docstring and the
behaviour move.

### nit 7 — `usage_conflicts` cannot see the presence/absence half of the invariant it is cited to prove
`aggregator/ingest.py:1971-1972` (`if value is None: continue`), `:1948-1951`
(the "0 of 4 738" claim), `:2192-2196` (the claim that justifies the operator).

`map_usage` keeps `agentId`/`runId` in `$setOnInsert` on the grounds that
`usage_conflicts` reports zero divergent ids. But `usage_conflicts` skips `None`
by design (*"a field absent from one observation is silence, not a claim"*), so
it structurally cannot report the *other* way the payload varies: one
observation of a `message.id` naming an `agentId` and another not. That also
makes the `$setOnInsert` payload order-dependent — the module's own rule at
`:2182-2188`.

I re-measured: **0 mixed-with-`None`** for both `agentId` and `runId` across
5 033 live message ids, so nothing is wrong today. The hole is in the argument
and its detector, not in the data.

**Fix.** Report a third state from `usage_conflicts` (`partial`: observed both
with and without), and add a two-observation arm to
`test_the_set_on_insert_payload_never_varies_for_one_id` — one
`UsageObservation` with `agent_id=SPLIT_AGENT`, one with `agent_id=None`, same
`message_id` — asserting `varying` fires (it will: the payload dicts differ).
Then decide whether `agentId` should also be `$min` or whether the mapper should
refuse a partial observation.

### nit 8 — `_split_ops`' stated rule and three of five callers disagree
`aggregator/ingest.py:2049-2052`.

*"`provenance` and the `_id`'s own components are `$setOnInsert`"* — but only
`map_run_node` passes `immutable=("runId","key","ordinal")`. `map_stream_meta`
stores `sessionId` and `lineNo`, which *are* the two members of
`refs.stream_meta_key`, as `$set`. Harmless (both are pure functions of the
`_id`, so the fingerprint is unaffected), but the sentence reads as an invariant
and is not one. Pass `immutable=("sessionId", "lineNo")` from
`map_stream_meta`, or rewrite the sentence as "…are `$setOnInsert` where the
caller declares them".

### nit 9 — the launch arm silently widens the meaning of `runs.sessionIds`
`aggregator/ingest.py:2642` vs the docstring at `:2230-2233`.

`map_run` explains `sessionIds` as `$addToSet` *"because a run genuinely spans
sessions"* — the set `find_run_dirs` globs. `_launch_scan` additionally adds the
**launching** session, which by construction has no
`subagents/workflows/<runId>/` directory. A consumer iterating
`runs.sessionIds` to locate the run's transcripts gets one entry that resolves
to nothing, and `$addToSet` makes it permanent (GD-26). Either record it as
`launch.sessionId` and leave `session_ids=()` on the launch arm, or state in
`map_run`'s docstring that the set means "sessions that observed this run,
including the one that launched it".

### nit 10 — the killed run's stated failure reason is read and discarded
`aggregator/ingest.py:1751-1768` (`_run_observation`).

`wf_455b348c-e17.json` carries `error` (304 chars — the reason behind
`status:"killed"`) and `result` (a dict on both completed snapshots); neither is
mapped. R-49 does not name them, so this is coverage rather than a deviation —
but the killed-run rendering the plan cares about (R-54's unknown/stale nodes)
will show `status:"killed"` with no reason, while the fact was on disk and
already parsed. Consider adding `error`, and `result` under a namespaced key
(GD-11-polymorphic, like the node one) — or record in the docstring that both
are deliberately out of sp-08's scope.

---

## 3. Regression check — the prior round's findings against this tree

Every finding from the two superseded critiques was re-verified against the
current code. All eleven are fixed, and I confirmed the fixes behaviourally, not
by reading the changelog.

| Prior | Claim | Status in this tree |
|---|---|---|
| a1 **B1** | per-path (`--backfill`) source arm applies no project scope | **fixed** — `_in_scope` (`:2503-2529`) on both the transcript and journal arms; `test_backfill_and_rebuild_see_exactly_the_same_files` proves the walk *sees* 5 foreign-slug files and yields 0 observations from them |
| a1 **M1** | `spill_containment` was a basename test | **fixed** — rooted + `realpath` on both sides + exact 5-component shape; no root ⇒ `False`, counted `unrooted_spill` (`:595-632`) |
| a1 m1 | contested `runs` scalars via `$set` | **fixed** — launch namespaced and every leaf `$min` (`:2296-2318`) |
| a1 m2 | `tsRaw` re-derived from the parsed datetime | **fixed** — the string is carried out of `_record_ts` and re-applied in `_ts_pair` (`:1221-1239`, `:2079-2091`) |
| a1 m3 | agentId-conflict counter had no runtime path | **fixed** — raised in `read_transcript` for all three identity fields (`:1199-1208`) |
| a1 m4 | `link_spills` matched on basename alone | **fixed** — keys `(session_id, basename)`, and a session-less pointer links nothing (`:1314-1318`) |
| a1 m5 | rebuild re-read the corpus once per source | **fixed** — walk memos; `test_the_rebuild_walk_is_read_once_not_once_per_source` asserts 8 reads over 8 files. (Introduced minor 4 above.) |
| a1 m6 / n3 | unkeyable-positional and keyless-`result` deviations unrecorded | **fixed** — both counted and documented as deviations with handoffs (`:66-79`, `:1388-1395`) |
| a1 n1 / n2 | test mis-reported a skip; `_launch_scan` stored an absolute path | **fixed** — `note()` vs `skip()` (`tests/test_ingest.py:122-130`); `_rel(root, scan.path)` (`:2652`) |
| a2 **B1** | `usage.sessionId` `$setOnInsert` breaks GD-25 on real data | **fixed** — `$min`; re-proved order-free over 27 920 live observations |
| a2 **B2** | two launch records race for `launch.taskId` | **fixed** — per-leaf `$min` + `duplicate_launch` counter; `test_two_launch_records_of_one_run_do_not_race_for_the_stop_handle` |
| a2 **M2** | the acceptance corpus could not contain either failing shape | **fixed** — `clear_split_root` builds the `/clear`-split shape, and the test asserts the shape exists before asserting about it (`tests/test_ingest.py:1518-1521`) |
| a2 m1' | `find_snapshot`/`find_run_dirs` globbed `projects/*/*` unscoped | **fixed** — `_run_scope` + `_within_scope`; `test_a_foreign_slug_holding_the_same_run_id_contributes_nothing` |
| a2 m3' / m4' | conflict counter watched the wrong field; run-level fields from one journal | **fixed** — all three fields counted; `read_run`'s docstring now names the single-writer argument, and `_ordinal_offsets` closes the two-journal collision (verified: 40 started ⇒ 40 ids) |
| a2 n1' / n2' | docstrings claimed more than the code enforced | **fixed** |

---

## 4. Checklist disposition

| Attack | Result |
|---|---|
| GD-21 — pymongo lazy, only in `mongo_store`/`mirror`; others clean on bare stdlib | **pass** — the driver name does not appear in `ingest.py`; module imports with no driver; live arm skips |
| GD-22 — Mongo never on the liveness path | **pass** — no DB I/O anywhere; the `lines=` seam keeps a tick O(bytes appended) |
| GD-24 — string `_id`s via the ref grammar only | **pass** — all five mappers key through `refs.*_key`; no BSON subdocument `_id`, no subdocument equality match |
| GD-25 — `$max/$addToSet/$min/$setOnInsert` only, no `$inc`, no bare `$set` on accumulables, deltas wire-only | **pass** — and **verified order-free on 27 920 real observations**, not just the fixtures |
| GD-26 — no delete verb, no `$unset`, no TTL | **pass** by grep; the one legal `stream_meta` delete correctly stays in `mirror.py` |
| GD-27 — security | **pass** — containment is rooted + realpath'd + exact-shape, "no root ⇒ False"; no credential surface in this module |
| GD-28 — provenance pins `{harness, derived}` for the mirror, no guessing | **pass** (`PROVENANCE="harness"`, `$setOnInsert`) — except the cosmetic claim in nit 6 |
| GD-29 — no agent holds a client; aggregator is the sole writer | **pass** |
| GD-30 — bounded work, O(delta) ticks | **pass** on the tick; see minor 4 for memory retained off the tick |
| GD-15 — one file, one owner | **pass** — `_only_ours` is a structural gate, not a convention; nothing outside the two owned files changed |
| tests assert real behaviour, skip cleanly without mongod | **pass** — corroborated by the adversarial-corpus arms and by my independent re-derivations |
| no needless rewrite beyond scope | **pass** |
| docs match implemented behaviour | 3 cosmetic mismatches (nits 5, 6, 8) |

All ten findings are local edits inside `aggregator/ingest.py` and
`tests/test_ingest.py`; none needs research, redesign, or a sub-plan boundary
change → **depth: in-scope**. The schema, key grammar, bucket table, ordinal
rule and operator choices are correct and proven on real data, so nothing here
would corrupt or waste the remaining sub-plans' work → **critical_defect:
false**. Approved with the four minors carried forward as follow-up work for
sp-14/sp-15 acceptance (or a trivial follow-on attempt if the driver prefers
them closed now).
