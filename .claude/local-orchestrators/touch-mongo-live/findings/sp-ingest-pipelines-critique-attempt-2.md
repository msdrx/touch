# sp-ingest-pipelines — adversarial critique, attempt 2

**Verdict: REJECTED.** 2 blockers, 1 major, 3 minors, 2 nits.

**depth: in-scope** — both blockers are fixable inside
`aggregator/ingest.py` + `tests/test_ingest.py` + `tests/test_usage.py` with the
operator vocabulary `mongo_store` already exposes (I verified `$min` on
`usage.sessionId` and on `runs.launch` both pass `ms.validate_update`, so no
sp-05 file has to be touched). No architectural rework, no missing research.

**critical_defect: false** — the defect is one wrong operator choice on two
fields of two documents, not a wrong model. Nothing downstream is built on the
broken behaviour: `mirror.py` drives whatever ops the mapper returns, and the
sp-14 acceptance that would catch this has not been written yet.

Reviewed: full content of the three owned files (untracked tree ⇒ full-file
review, `git diff` is empty for them), against sp-08 in
`touch-mongo-live-subplans.md`, R-26/R-47/R-49/R-50 + GD-21…GD-30 in
`touch-mongo-live-plan.md`, R-26 in `touch-full-recon-plan.md`, and the
attempt-1 critique.

---

## Attempt-1 findings: disposition (all independently re-checked)

| # | attempt-1 finding | status |
|---|---|---|
| B1 | `--backfill` per-path arm unscoped | **FIXED, properly.** `_in_scope` (`ingest.py:2168`) + `_scope_anchor` (`:2147`) apply the rooted `sessions.scoped_dirs` test to the transcript arm (`:2223`), the journal arm (`:2252`) and the launch arm (`:2261`). I re-ran my attempt-1 reproduction: a foreign-slug-only root now yields `{}` observations through `mirror.iter_backfill_observations`, and the new `test_backfill_and_rebuild_see_exactly_the_same_files` asserts the *stronger* property (same counts **and** same bytes), which is R-55 stated as a seam property. |
| M1 | `spill_containment` not realpath-contained | **FIXED, properly.** `:553-590` is root-anchored, realpath-resolves both sides, and pins the exact 5-component shape. The replacement test (`test_containment_is_rooted_and_resolved_not_a_directory_name`) covers symlink escape, same-named dir outside the root, bare relative, wrong depth inside the root, `..`, and the symlinked-root positive. `unrooted_spill` is its own counter, distinct from `uncontained_spill`. This is a better fix than the one I proposed. |
| m1 | `runs` contested scalars via `$set` | **PARTLY fixed — and the residue is now blocker B2 below.** Namespacing launch under `launch{}` closes launch-vs-snapshot. It does not close launch-vs-**launch**, and the real corpus has that shape. |
| m2 | `tsRaw` re-derived | FIXED (`_record_ts` → `(ts, raw, error)` at `:1168`, `_ts_pair` at `:1832`). |
| m3 | conflict counter had no runtime path | Partly fixed: `skipped["usage_agent_conflict"]` (`:1152`) gives the within-file agentId case a runtime path, and the cross-file gap is a written handoff (`:109-113`). See minor **m3'** — the counter still watches the wrong field for the conflict that actually exists on disk. |
| m4 | `link_spills` keyed on basename | FIXED (`:1243-1270`, `(session_id, basename)`, unknown session links nothing). |
| m5 | rebuild re-reads the corpus | FIXED (`_transcript_walk`/`_RUN_WALK` at `:2207`/`:2265`, keyed on every file's `(dev, ino, size, mtime_ns)`); the new test asserts 8 reads over 8 files. |
| m6 | unratified `stream_meta` deviation | FIXED as documentation (`:66-79`, names the exact GD-24 amendment needed). |
| n1 | skip-then-assert | FIXED (`note()` helper, `tests/test_ingest.py:121`). |
| n2 | absolute `source_path` in `_launch_scan` | FIXED (`:2308`, `_rel`). |
| n3 | keyless journal `result` uncounted | FIXED (`unmatched_result`). |

I also re-verified the gate's own claims rather than trusting them:
`tests/test_ingest.py` rc=0, `tests/test_usage.py` rc=0, `tests/run_all.sh`
**17/17 green in 48 s**, only the `TOUCH_MONGO_URI` arm skipping. `ingest.py`
imports exactly `datetime, glob, json, os, re, dataclasses, __future__` plus
three sibling aggregator modules — no driver name (GD-21). Zero
`deleteOne|deleteMany|drop(|$unset|$inc|expireAfterSeconds` (GD-26), zero
`now(|time.time|utcnow|ingestedAt` outside prose (R-26 amendment 3).

---

## BLOCKERS

### B1 — `usage.sessionId` is written with `$setOnInsert`, but a `message.id` *does* span sessions on the live corpus: GD-25's acceptance property fails, reproduced

`aggregator/ingest.py:1927` (`map_usage`, `on_insert = {"sessionId": obs.session_id, …}`).

R-50's `$setOnInsert:{agentId, sessionId, runId}` is justified by "a `message.id`
never spans **agents**". That is true. It says nothing about sessions, and
`$setOnInsert` makes the *whole* payload first-writer-wins. `_split_ops`'
docstring (`:1803-1806`) states the governing rule itself: "`$setOnInsert` is the
one operator whose payload must not vary".

It varies. Probed over the **in-scope** transcript set (144 files, exactly what
`--rebuild` ingests — not the whole of `projects/`):

```
in-scope usage observations: 9153, distinct message ids: 4607
$setOnInsert-varying message ids: 3
  msg_011CdPtgjqzpmL4ti1bM5hE3  agent a28ddb3df3b1cf1dc  run wf_1a3ffcdd-c60
       sessionId 0ff58ac5-…f858   and   7e7386b8-…6887
  msg_011CdPvNu28Y2AsGMDWDuuWq  agent a2d7dd6be1461c53d  run wf_1a3ffcdd-c60
       sessionId 2c59e5dc-…9a84fb  and   500eaa77-…f858
  msg_011CdPkATis92oV22Av58M5s  agent a45a5c78def2f3576  run wf_1a3ffcdd-c60
       sessionId 5a868514-…7b1d6   and   e8ea27a3-…b3b068f2e
```

The two sources of each pair are two fragments of **one agent** under **two
session directories** — `…/<sessionA>/subagents/workflows/wf_1a3ffcdd-c60/agent-a28ddb3df3b1cf1dc.jsonl`
and `…/<sessionB>/…/agent-a28ddb3df3b1cf1dc.jsonl`. That is not an anomaly: it is
the documented `/clear`-mid-run topology this very module builds
`find_run_dirs` (`:1363`) for, and MONGOSCHEMA-9's "two `a2fc883c` files are
disjoint continuations" shape. `usage.sessionId` is simply not a function of
`message.id`.

Driven end-to-end through the real mapper and `mongo_store`'s own upsert model
(the same model the tests use), over all five sources on the in-scope corpus:

```
counts {'records': 15390, 'run_nodes': 108, 'runs': 7, 'stream_meta': 1008, 'usage': 4612}
fingerprint normal == reversed : False
fingerprint normal == shuffled : False
  records:     reversed-diff 0
  stream_meta: reversed-diff 0
  run_nodes:   reversed-diff 0
  usage:       reversed-diff 3   (sessionId flips)
```

This is **GD-25's named acceptance property** (R-44: "fingerprint over all
documents sorted by `_id` is identical on every pass"), failing on real data
today. It also breaks R-55's wipe/rebuild equivalence in the way that actually
matters in production: a live tail applies usage observations in file-arrival
order, a `--rebuild` applies them in sorted-path order, so the same installation
stores two different documents depending on how it got there. And nothing
notices — `usage_conflicts` (`:1719`) and `skipped["usage_agent_conflict"]`
(`:1152`) both watch `agentId`, which agrees in all three cases.

**Fix (in-scope, no sp-05 change).** Take `sessionId` out of `$setOnInsert` and
give it an order-free operator. I verified `ms.validate_update({"$min":
{"sessionId": …}}, "usage", _id=…)` is accepted, so the one-line version is:

```python
on_insert = {"provenance": PROVENANCE}          # agentId/runId may stay here
if obs.agent_id is not None: on_insert["agentId"] = obs.agent_id
if obs.run_id  is not None: on_insert["runId"]  = obs.run_id
ops = [ms.op_set_on_insert(on_insert), ms.op_max(tokens),
       ms.op_min({"sessionId": obs.session_id})]   # deterministic: BSON string min
```

and say in the docstring *why* `sessionId` is `$min` and not `$setOnInsert`
(an agent's fragments span sessions; the earliest-sorting id is an arbitrary but
**order-free** choice). If you would rather keep the full set, the honest shape
is `$addToSet: {sessionIds: …}` — but that needs `sessionIds` added to
`SPECS["usage"].set_fields` in `mongo_store.py` so `fingerprint` sorts it, and
that file is sp-05's; record it as a handoff instead of reaching across.

Either way, count the divergence: extend `usage_conflicts` to return
per-message-id conflicts for `sessionId` and `runId` as well as `agentId` (the
function already has the whole stream), and note in the module docstring that
`sessionId` divergence is *expected* while `agentId` divergence is an anomaly.

**Test to add** (`tests/test_usage.py`): two `UsageObservation`s with the same
`message_id`, same `agent_id`, **different** `session_id` — assert
`state_of([a, b])` and `state_of([b, a])` fingerprint identically. Then assert
it on the real shape: the corpus arm must stop being fixture-only (see M2).

---

### B2 — `runs.launch` is `$set` from *two* launch records of one `runId` on the live corpus, so the stored `launch.taskId` — amended GD-8's run-level stop handle — is chosen by walk order

`aggregator/ingest.py:1979-1981` (`map_run`), `:2278-2309` (`_launch_scan`).

`map_run`'s docstring now *asserts* the property the m1 fix was supposed to
establish (`:1958-1961`):

> So the launch's copy lands under `launch{}` and the two sources write
> **disjoint field sets**: order cannot matter […]

The two *sources* are disjoint. The `launch{}` field is not single-writer:
`_launch_scan` emits one `RunObservation` **per launch record**, and a transcript
can hold two launch records naming the same `runId`. One does, right now:

```
wf_455b348c-e17 — 2 launch records, both in
    ~/projects/-home-laniakea-Projects-touch/e423cd3c-…9b4ac.jsonl
    taskId wzd027fky   status async_launched
    taskId wgm4nvzgk   status async_launched
```

Through the real pipeline over the in-scope corpus (same probe as B1):

```
  runs: reversed-diff 1
      wf_455b348c-e17  launch.taskId  'wgm4nvzgk'  (normal)
                                      'wzd027fky'  (reversed)
```

Consequences, in order of seriousness:

1. **Amended GD-8 / CONVO-12.** `launch.taskId` is *the* run-level stop handle
   and the only deterministic main-session→run join. Touch will offer a "stop
   this run" control whose target depends on which order the walk read two lines
   of one file. One of the two ids is wrong, and nothing records that a second
   one was ever seen.
2. **GD-25 by construction**, the same rule B1 breaks — and this one is the very
   rule the m1 fix was written to satisfy, so the file now carries a docstring
   claim contradicted by its own corpus. That is worse than the original `$set`,
   which at least did not promise anything.
3. `_launch_scan`'s docstring ("Several launches in one file is normal … the
   first is the scan's `run` and the rest are its `extra_runs`") reads as though
   the multi-launch case were handled. It handles *several runIds*, not *several
   launches of one runId*.

**Fix (in-scope, no sp-05 change).** Make the field order-free. Two workable
shapes, both accepted by `ms.validate_update` (I checked):

* `ms.op_min({"launch": {...}})` — deterministic under BSON document
  comparison because `_launch_scan` builds the sub-document with a fixed field
  order; cheapest, but silently keeps one of two real taskIds; or
* better, keep `launch` for the min and additionally emit
  `$addToSet: {launchTaskIds: [...]}` so the second handle is not *lost* — a
  run that was launched twice is a fact the stop control needs. (`launchTaskIds`
  would want a `set_fields` entry in `mongo_store.py` for the fingerprint sort;
  if you would rather not reach into sp-05's file, use `$min` alone and write
  the `set_fields` request into this sub-plan's handoff.)

Whichever you pick, add a counter (`skipped["duplicate_launch"]` or similar) and
delete the "order cannot matter" sentence unless the code makes it true.

**Test to change** (`tests/test_ingest.py:913`,
`test_the_runs_document_is_order_independent_across_its_two_sources`): it tests
launch-vs-snapshot only. Add a launch-vs-launch arm with two *disagreeing*
`taskId`s for one `runId` — the exact `wf_455b348c-e17` pair, since it is real —
and assert the two orders fingerprint identically. Do the same in the live-mongod
arm at `tests/test_ingest.py:1505`, which today also only contradicts a snapshot.

---

## MAJOR

### M2 — the GD-25 acceptance tests run on a corpus that *structurally cannot* contain either failing shape, so both blockers were invisible to a green suite

`tests/test_ingest.py:1152` (`test_the_algebra_is_order_independent`),
`tests/test_usage.py:214` (`test_shuffled_and_reversed_ingest_give_identical_totals`).

Both build their observation set from **one** run fixture
(`RUN`/`-fixture` + `RUN_ID`) under **one** session directory. The two shapes
that break the property both require *two session directories*:

* B1 needs one agentId's fragments split across two sessions (`/clear` mid-run);
* B2 needs two launch records for one runId.

So the suite's strongest assertion — "normal / reversed / shuffled ingest
fingerprint IDENTICALLY (GD-25's acceptance property)" — is true of the fixture
and false of the machine it runs on, and reports green. That is not a missing
edge case; it is the acceptance test for the sub-plan's central invariant
measuring something narrower than the invariant.

Note the fixtures are sp-02's and are frozen — **do not** go add fixture files.
The in-scope fix is to build the adversarial shapes *in the test*, which both
suites already do comfortably elsewhere (`linked_root`, hand-built
`UsageObservation`s, `read_launch` from a literal dict):

1. a two-session `linked_root` where the same `agent-<id>.jsonl` basename
   appears under two session dirs carrying one shared `message.id`;
2. the two-launch record pair above;
3. keep the existing fixture arm as the regression floor.

Optionally add a third arm that is a *property* rather than a fixture: assert
that for every emitted op, `$setOnInsert`'s payload is deep-equal across all ops
sharing a `(collection, _id)`. That single assertion would have caught B1 on any
corpus, and it is ~6 lines.

---

## MINOR

### m1' — `find_snapshot` and `find_run_dirs` glob `<root>/projects/*/*/…`, the one pattern R-25-as-amended names as forbidden

`aggregator/ingest.py:1349-1360` and `:1363-1376`.

`_in_scope` now fences the *entry* points, and correctly. These two lookups
re-open the fence from inside: both glob every slug directory under
`<root>/projects`, and their results feed `runs.sessionIds` (`$addToSet`) and
node `startedAt`/`endedAt` (`read_run:1426-1446`). A foreign project that
happens to hold a directory of the same `wf_<12hex>` runId contributes its
sessionIds and its transcripts' timestamps to *this* project's run document.

R-26 does justify the *snapshot* glob explicitly ("the snapshot lands under
whichever session was current when the run ended"), and a runId collision across
projects is implausible, which is why this is minor and not a blocker. But the
scope rule is stated absolutely and the mitigation is currently "runIds are
unique enough".

**Fix.** Intersect both glob results with
`sess.scoped_dirs(sess.project_cwd(cwd, env), root)` — the anchor is already
computable with `_scope_anchor`. If you deliberately keep the snapshot glob
unscoped (R-26's clause), say so in the docstring in one line and scope
`find_run_dirs`, which has no such clause. Test: a foreign slug holding the same
runId contributes no sessionId and no node time.

### m3' — the conflict counter watches the one identity field that never diverges

`aggregator/ingest.py:1149-1154`, `:1719` (`usage_conflicts`).

Follow-on from B1, recorded separately because it survives whatever operator you
pick: on the live in-scope corpus `agentId` conflicts = **0** and `sessionId`
conflicts = **3**. The counter that exists never fires; the divergence that
happens is uncounted. `tests/test_usage.py:316` even asserts
`usage_conflicts(real) == {}` as evidence that "the invariant holds on the frozen
bytes" — which is true of the frozen bytes and misleading as a claim about the
invariant.

**Fix.** Return conflicts per field (`{message_id: {"sessionId": (a, b)}}`), keep
the agentId arm as the anomaly and label the sessionId arm as expected-and-benign
once B1's operator makes it harmless. Update the docstring's "A `message.id`
never spans agents" paragraph (`:104-113`) to add the sentence it is missing:
*and it does span sessions.*

### m4' — `read_run` merges other sessions' agent transcripts for node times but the run's `_id`-level fields still come from one journal only

`aggregator/ingest.py:1394-1487`.

Not a defect today (I verified: 6 journals, 6 distinct runIds, no runId has two
journals, so `runs` has exactly one journal writer). Recording it because the
docstring at `:1406-1411` explains why only `run_dir`'s own journal is read
(GD-7's ordinal is per-journal) without noting the consequence: **if** a second
`journal.jsonl` for one runId ever appears, `status`/`summary`/`phases` go back
to two `$set` writers and B2 recurs on a different field. One sentence in the
docstring naming that as the invariant being relied on ("exactly one journal per
runId; two would reintroduce the two-writer `$set` hazard") would make the next
reader check it.

---

## NITS

### n1' — `map_run`'s docstring makes a claim the code does not enforce

`aggregator/ingest.py:1958-1961`. Covered by B2, called out separately because
the sentence should be *deleted or made true* rather than softened: an in-file
assertion of an invariant is load-bearing documentation, and this one is now
falsified by the corpus in the same repo.

### n2' — `_launch_scan`'s "invariant this function must not break" paragraph names the wrong invariant

`aggregator/ingest.py:2286-2294`. It says the danger is *promoting a field to the
top level*. The danger that actually materialised is *two launches writing the
same namespaced field*. Both are true; only one is written down.

---

## Checklist disposition

| item | verdict |
|---|---|
| GD-21 dependency policy | PASS — imports are `datetime/glob/json/os/re/dataclasses` + 3 siblings; no driver name; module imports on bare stdlib |
| GD-22 Mongo off the liveness path | PASS — no client, no DB I/O in this module at all |
| GD-24 string `_id`s via `ref_key` only | PASS — all five keys via `refs.*_key`; no sub-document `_id`; `test_every_id_comes_from_refs` re-parses every one |
| GD-25 upsert algebra | **FAIL — B1 (`usage.sessionId` `$setOnInsert`) and B2 (`runs.launch` `$set`), both reproduced on the live in-scope corpus**; no `$inc`, deltas wire-only, `$max`/`$min`/`$addToSet` correct elsewhere |
| GD-26 no delete verbs / no TTL | PASS — zero `deleteOne\|deleteMany\|drop(\|$unset\|expireAfterSeconds` |
| GD-27 security | PASS — containment predicate now realpath-rooted (attempt-1 M1 fixed); no credential anywhere |
| GD-28 provenance | PASS — `PROVENANCE="harness"` under `$setOnInsert` on every op, asserted |
| GD-29 no agent holds a client | PASS |
| GD-30 latency budget | PASS — walk memos make a rebuild read each file once (8 reads / 8 files, asserted) |
| GD-15 one file one owner | PASS — `_only_ours` wall intact; mtimes show only the three owned files touched (ingest.py 05:53, test_usage.py 06:01, test_ingest.py 06:04) |
| R-26 six amendments | PASS |
| R-47 bucket table | PASS (the `stream_meta` deviation is now documented as a deviation with the exact GD-24 amendment it needs) |
| R-49 runs/run_nodes | PASS on ordinals/journalSeq/back-fill/no-state; **B2** on the launch join |
| R-50 usage | **B1** on `$setOnInsert`; **m3'** on the counter; `$max` algebra and rollups correct |
| R-25-amended project scope | PASS on the entry seams (attempt-1 B1 fixed and strengthened); **m1'** on the two internal globs |
| scope of edits | PASS |
| tests real, skip cleanly | Mostly PASS — behavioral, recount-from-bytes, strong negative arms, live-mongod arm skips cleanly; **M2** on the acceptance corpus |
