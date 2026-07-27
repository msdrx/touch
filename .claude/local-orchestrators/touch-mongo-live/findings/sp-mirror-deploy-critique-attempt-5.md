# sp-mirror-deploy — adversarial critique, attempt 5

**Verdict: REJECTED.** 1 major, 5 minor, 2 nits. `depth: in-scope`,
`critical_defect: false`.

Reviewed (full content — all three are untracked, `git diff` is empty):

- `aggregator/mirror.py` (2 946 lines)
- `docs/mongo.md` (356 lines)
- `tests/test_mirror.py` (2 215 lines)

Against `plan/touch-mongo-live-subplans.md` §`sp-06 — mirror-deploy`,
`touch-mongo-live-plan.md` GD-21…GD-30 + R-42/R-45/R-57, and
`touch-full-recon-plan.md` GD-1…GD-20.

**Ownership is clean.** Only these three files carry this attempt's mtimes
(08:59–09:04); `aggregator/refs.py`, `aggregator/mongo_store.py`,
`tests/test_refs.py`, `tests/test_mongo_store.py` carry 07:54–08:11 (the
preceding sub-plan's attempt) and `tests/test_mongo_deploy.py` was not touched
at all this round. `git log -1` is still `579446e`; nothing committed.

**Every finding below was reproduced by running this checkout.** Probe scripts
are inlined so the next implementer can re-run them.

## Attempt 4's three majors are genuinely closed — I re-ran each

- **attempt-4 MAJOR 1** (writing with no lease). `_lease_required` now exists
  (`mirror.py:1676`, set from the argument in `_start` at `mirror.py:1864`), the
  tick branches on the *requirement* (`mirror.py:2236`) and there is a real belt
  before `_take_batches` (`mirror.py:2258`). The new arm at
  `tests/test_mirror.py:672` uses the `FlakyLease` shape (only `writers` fails)
  and asserts `bulk_upsert == 0`, that the lease was actually *attempted*, and
  that the process is not wedged afterwards. Load-bearing.
- **attempt-4 MAJOR 2** (`classify` → `validate_ref`). `mirror.py:527` is now
  `refs.validate_ref`, which is the function that enforces the closed key set
  (`refs.py:795-797`) and the per-field pins. The by-hand forged ref is tested
  at `tests/test_mirror.py:1184-1208`, at `scrub_op_update` **and** at
  `validate_op`, and the declared-kind fast path still returns the identical
  object (`kept_ref is slot`, `tests/test_mirror.py:1166`) so the
  `{"ref.sessionKey": …}` join survives. The four prose sites were corrected
  (`mirror.py:324-337`, `mirror.py:485-522`, `mirror.py:1010-1014`,
  `docs/mongo.md:230-240`).
- **attempt-4 MAJOR 3** (idle mirror stuck `down`). `acquire()` calls
  `_record_success()` on its success path (`mirror.py:1958`), the empty-batch
  early return is gone, and `_settle` (`mirror.py:2341`) is reached by every
  tick that got past the lease gate. `tests/test_mirror.py:803-846` tests
  **both** halves — a work-free tick with a fresh lease promotes nothing
  (fail-open guard) and a work-free tick that renews the lease recovers with
  `bulk_upsert == 0`.
- attempt-4 MINOR 1 (`lastError` on recovery, `mirror.py:2377`), MINOR 2
  (`ScrubbedOp` unexported + downgraded in `Mapper.__call__`, `mirror.py:135`,
  `mirror.py:821`), MINOR 3 (`rebuild` guards its three driver calls,
  `mirror.py:2596-2607` + `_report_read`), MINOR 4 (`backfill`'s defensive
  unpack `mirror.py:2684-2702`, `enqueue`'s coercion `mirror.py:1998-2015`) and
  NIT 1 (`allow_empty_reinsert`, `mirror.py:2475`) are all closed with real
  arms. NIT 2 (`_take_batches` catches `Exception`) is closed at
  `mirror.py:2122`.

I re-ran the owned suite (26 tests green), `test_docs.py` and
`test_stdlib_only.py`: all pass.

---

## MAJOR 1 — the lease guards **only** the tick: `sweep()`, `rebuild()`'s drop and `save_cursor()` write under no lease at all, so a process in `STATE_REFUSED` still runs the retraction `updateMany` and Touch's ONE legal `deleteMany`

**Files:** `aggregator/mirror.py:2475` (`sweep`, the whole body),
`aggregator/mirror.py:2597` (`rebuild`'s `drop_collection`),
`aggregator/mirror.py:2445` (`save_cursor`). Contrast with the two places that
*do* guard: `aggregator/mirror.py:2236` and `aggregator/mirror.py:2258`.

Attempt 4's fix made the queue→`bulk_upsert` path airtight. It did not touch the
three write paths that never went through the queue. Nothing in `sweep`,
`rebuild` or `save_cursor` reads `_lease_required`, `_lease["held"]`,
`self.state` or `breaker_open` — they call the backend directly.

The module's own invariant #5 states the rule without a qualifier
(`mirror.py:50-52`):

> *"a process that cannot hold the lease refuses to mirror while remaining
> perfectly able to serve reads"*

and `docs/mongo.md:253` sells the same thing to the operator ("write when
another process holds the **writer lease**" is in the refusals table). Both are
false for the destructive half of the API.

**Failure scenario (executed against this checkout):**

```python
import asyncio, datetime
from aggregator import mirror as mr, mongo_store as ms, refs
def run(c): return asyncio.run(c)
SESSION = "11111111-2222-4333-8444-555555555555"
shared = {}
now = datetime.datetime(2026, 7, 25, 12, 0, tzinfo=datetime.timezone.utc)
holder = mr.Mirror(mr.MongoConfig("u","touch_test"), backend=mr.MemoryBackend(shared), clock=lambda: now)
loser  = mr.Mirror(mr.MongoConfig("u","touch_test"), backend=mr.MemoryBackend(shared), clock=lambda: now)
loser._lease["holderPid"] = holder._lease["holderPid"] + 1
run(holder.acquire()); run(loser.acquire())          # loser -> 'refused', held=False

rec  = refs.ref_key({"kind":"uuid","uuid":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"})
meta = refs.ref_key({"kind":"streamMeta","sessionId":SESSION,"lineNo":1})
run(holder.backend.bulk_upsert("records",     [(rec,  ms.op_set({"sessionId":SESSION,"type":"user","provenance":"harness","gen":1}))]))
run(holder.backend.bulk_upsert("stream_meta", [(meta, ms.op_set({"sessionId":SESSION,"lineNo":1,"type":"x","provenance":"harness","gen":1}))]))

print(run(loser.sweep({"sessionId": SESSION}, 2, allow_empty_reinsert=True)))
print(shared["records"][rec].get("retracted"), list(shared["stream_meta"]))
```

```
loser state: refused  held: False  lease_required: True
BEFORE: retracted= None   stream_meta= ['11111111-…#00000001']
LOSER sweep report: {'retracted': 1, 'renumbered': 1, 'reinserted': 0}
AFTER:  retracted= True   stream_meta= []          # the one legal delete, executed with no lease
counters refused_no_lease: 0
```

The same process's `enqueue` refuses every operation and books it under
`refused_no_lease`, and its `tick` returns `skipped:'refused'` — so the module
is simultaneously refusing the *harmless* writes and performing the destructive
ones, and the counter that is supposed to be the tell reads **0**.

`rebuild` is the same hole with a bigger blast radius (executed):

```
rebuild report: {'replayed': 0, 'droppedDerived': True, 'rejected': 0}
derived present: False
```

A lease-refused process drops the reducer-owned `derived` collection and then
replays nothing, because every tick it makes afterwards refuses — which is
exactly the "neither the old projection nor the new one" state `rebuild`'s own
docstring (`mirror.py:2576-2579`) argues it must never leave behind. `main()`
happens to gate on `mirror.state == STATE_LIVE` (`mirror.py:2915`), so the CLI
is safe *today*; the API is not, and `main()` is not the only caller the module
declares itself for.

`save_cursor` completes an `upserted:1` on `cursors` from the refused process
too — less severe (a watermark, not history), but it is two processes writing
one per-`streamId` document, which is precisely the race the lease exists for.

**Reachability.** No production caller exists yet — `grep -rn "\.sweep(\|save_cursor("
aggregator/` finds only `tests/`. That is *why* this is major rather than
blocker: it is a contract the next modules wired to SD-10 will call, and SD-10
names `mirror.py` as the lease holder, so the guard belongs here and not in five
callers. Fixing it after they are written costs five diffs and a retracted
`records` collection.

**Fix (one gated attempt):**

1. One helper beside `_lease_due`:

   ```python
   def _may_write(self) -> bool:
       """GD-29's gate, for the write paths that do not go through the queue."""
       return not self._lease_required or self._lease["held"]
   ```

2. `sweep`: refuse before any driver call — `if not self._may_write(): raise
   MirrorError(...)` (or return a `{"skipped": "no-lease"}` report and count
   `refused_no_lease`; a raise is defensible here because `sweep` already raises
   `SweepScopeError`/`MirrorError` for every other precondition, and SD-10's
   caller is not the poll loop). Do the same in `save_cursor`.
3. `rebuild`: refuse **before** `drop_collection`, with `droppedDerived: False`
   and the reason on `/health` — the same shape the failed-drop path already
   returns (`mirror.py:2605-2607`).
4. Tests: extend `test_two_writers_on_one_stream_and_the_second_refuses`
   (`tests/test_mirror.py:850`) with the probe above — the refused process's
   `sweep`/`rebuild`/`save_cursor` must leave `retracted` unset, `stream_meta`
   intact and `derived` present, and the backend's `update_many`/`delete_many`/
   `drop_collection`/`guarded_update` call counts must not move.
   `live_mirror()` already takes a real lease (`tests/test_mirror.py:189`), so
   the existing sweep and cursor tests keep passing unchanged.

---

## MINOR 1 — `_scrub_ref` raises `TypeError` out of GD-27's backstop, against its own "Validation never raises out of here"

**File:** `aggregator/mirror.py:526-529`, claim at `aggregator/mirror.py:520-522`.

```python
    try:
        kind = refs.validate_ref(value)
    except refs.RefError:
        kind = "unknown"
```

`refs.validate_ref` → `refs.classify` does `declared in KIND_SPECS`
(`refs.py:766`), which is a dict membership test on an unvalidated value.
Executed:

```python
>>> mr._scrub_ref({"kind": ["uuid"], "password": "hunter2"})
TypeError: unhashable type: 'list'
```

It is caught downstream (`_take_batches`'s `except Exception`,
`mirror.py:2122`), so the operation is counted as `rejected` and nothing leaks —
fail-closed, which is the right direction. But `scrub_op_update` is a public
function (`__all__` does not carry it, yet `Mapper`/`stamp_*`/`validate_op` all
route through it), a caller invoking it directly gets the traceback, and the
docstring promising totality is now wrong.

**Fix:** widen to `except Exception` (with the existing comment adjusted: a ref
the module cannot even classify is the one to scrub, not to trust), and add
`mr._scrub_ref({"kind": ["uuid"], "password": "hunter2"})["password"] ==
mr.REDACTED` to the degenerate-shapes line at `tests/test_mirror.py:1171`.

## MINOR 2 — `enqueue` splits a bare `(collection, key, update)` triple into three operations and returns `accepted=3`

**File:** `aggregator/mirror.py:1998-2002`.

```python
        if isinstance(ops, MirrorOp):
            ops = [ops]
        elif not isinstance(ops, (list, tuple)):
            ...
```

A plain triple **is** a tuple, so it takes the "iterable of operations" path and
each of its three elements is queued as an operation. Executed:

```
enqueue(("records", key, update)) -> accepted: 3   qsize: 3
after tick:  written: 0   rejected: 3
```

This is the exact defect attempt-4's MINOR 4 fix was written to remove — *"a
count that lied, followed by a `rejected` one tick later"* (`mirror.py:1992-1994`)
— surviving in the one shape the fix did not consider. And it is a natural call:
`tests/test_mirror.py:1075` itself enqueues bare triples (in a list), so the
triple is plainly a supported input type; only the arity-1 spelling misbehaves.

**Fix:** detect the operation shape rather than the container type — e.g.
`if isinstance(ops, MirrorOp) or (isinstance(ops, tuple) and len(ops) == 3 and
isinstance(ops[0], str) and isinstance(ops[2], dict)): ops = [ops]`. Assert
`enqueue(("records", key, upd)) == 1` and `written == 1` in
`test_enqueue_never_blocks_never_raises_and_never_awaits`
(`tests/test_mirror.py:274`).

## MINOR 3 — four prose sites and one `main()` branch still say the entity modules do not exist; all five are on disk and all thirteen mappers/sources resolve

**Files:** `aggregator/mirror.py:769-770`, `aggregator/mirror.py:836`
("that is the state of four of the five today"), `aggregator/mirror.py:2761-2763`
("None of the five exists yet, so this yields nothing today"),
`aggregator/mirror.py:2859-2860` ("Yields nothing today, because none of the five
entity modules exists"), and the dead branch at `aggregator/mirror.py:2925-2928`.

Executed against this checkout:

```
mappers: ['agent','agentSpawn','customState','legacyArtifact','legacyEvent','record',
          'run','runNode','session','sessionPromotion','slot','streamMeta','usage']
sources: ['session','sessionPromotion','record','streamMeta','usage','run','runNode',
          'legacyEvent','legacyArtifact','agent','agentSpawn','customState','slot']
```

`aggregator/{sessions,ingest,legacy,agents,custom_state}.py` all exist and all
declare `MIRROR_MAPPERS` **and** `MIRROR_SOURCES`. The repo's standing rule is
that prose in these files is a contract, and this prose tells the next reader
that `--rebuild`/`--backfill` are no-ops when in fact they now drive the whole
corpus. The `main()` branch is worse than stale: if it ever fires it calls
`note_error` on a mirror that is `STATE_LIVE`, publishing exactly the
`live` + `lastError` pair `docs/mongo.md:44-47` offers as a literal alert
contract — and "nothing to replay" is commentary, which is what `note()`
(`mirror.py:1711`) exists for.

**Fix:** correct the four docstrings to the present tense ("the five entity
modules declare these; a module that is absent is skipped"), and change
`mirror.py:2926` from `note_error(...)` to `note(...)`.

## MINOR 4 — `docs/mongo.md` states the `live` ⇒ `lease.held` implication unconditionally; the code qualifies it, and the unqualified pair is producible

**File:** `docs/mongo.md:47-49` vs `aggregator/mirror.py:1763-1767`.

> *"`live` likewise implies `lease.held` … the pair `state:"live"` beside
> `lease:{held:false}` is a shape `/health` cannot produce."*

The docstring is careful — *"whenever a lease is required"* — and the code is
correct. The page is not. Executed:

```python
m = mr.Mirror(mr.MongoConfig("u","touch_test"), backend=mr.MemoryBackend({}))
run(m.start(ensure_schema=False, acquire_lease=False))   # -> 'live'
m.health()["state"], m.health()["lease"]["held"]         # -> ('live', False)
```

`server.py` does not construct a `Mirror` today, so no production caller reaches
it — but `start(acquire_lease=False)` is a supported, tested public argument
(`tests/test_mirror.py:763`), and sp-12 is being handed a page that says the
shape is impossible.

**Fix:** one clause on `docs/mongo.md:48` — "…implies `lease.held` for any
mirror that requires a lease (every deployment; `start(acquire_lease=False)` is
a test-only opt-out)". Or, better, assert the invariant *as documented* in
`test_health_is_r45s_block_and_carries_no_credential` and make `health()` report
the requirement (`lease["required"]`) so the page can stay absolute.

## MINOR 5 — a rejected operation leaves `state:"live"` beside a non-null `lastError` for a tick

**File:** `aggregator/mirror.py:2136-2138` (`_take_batches` counts + `note_error`
but never `_degrade`s) with `aggregator/mirror.py:2372-2375` (`_settle` vetoes
promotion on `report["rejected"]`, so it also never *clears*).

Executed:

```
tick report: {... 'rejected': 1 ...}
health: state='live'  lastError="MapperError: queue: '$bogus' is not part of the algebra …"
next tick: state='live' lastError=None
```

The mirror was `live`, the poison operation set `lastError`, and `_settle`
declined to touch the state — so the block published the contradiction
`docs/mongo.md:44-47` says an alert rule may read literally. It self-heals on
the very next tick (250 ms), which is why this is minor rather than a repeat of
attempt-4 MINOR 1; but `/health` is scraped, and `flush()` returns on an empty
queue, so the window is real.

**Fix:** pick one and state it. Either `_take_batches`'s rejection path calls
`self._degrade()` (a refused document *is* a degradation for that tick, and the
`_settle` veto then reads consistently), or `_settle` clears `lastError`
whenever it declines to promote for a `rejected`-only reason. Assert whichever
you choose in `test_the_scrub_runs_once_per_operation_and_off_the_poll_loop`'s
neighbourhood.

## NIT 1 — `docs/mongo.md`'s "Never mirrored" list omits `.touch/mongo.json`, which the code denies

`DENY_BASENAMES` (`aggregator/mirror.py:246-247`) contains `mongo.json`, and its
own comment (`mirror.py:242-245`) enumerates only the other three. The page
(`docs/mongo.md:216-217`) does the same. The credentials file this page is
*about* is the one omission in both. Add it to the constant's comment and to
`docs/mongo.md:216`; `tests/test_mongo_deploy.py:518` already asserts all four
names, so the page is the only thing behind.

## NIT 2 — `stamp_gen` preserves the `ScrubbedOp` marker for collections it does not stamp, against `ScrubbedOp`'s docstring

`aggregator/mirror.py:1034-1039`: the downgrade to `MirrorOp` happens only
inside the `if op.collection in ("records", "stream_meta")` branch, so an
`agents`/`usage` operation that arrived as a `ScrubbedOp` leaves as one. That is
harmless — the update did not grow, so the marker is still true — but
`ScrubbedOp`'s docstring says flatly *"The two stampers deliberately do **not**
preserve the marker"* (`mirror.py:939-941`), and `stamp_backfill`
(`mirror.py:1056`) does downgrade unconditionally. Either downgrade in `stamp_gen`
too (one `MirrorOp(*op)`), or narrow the docstring to say the marker survives
only when the stamper changed nothing.

---

## What I checked and found correct (so the next attempt does not churn it)

- **GD-21.** No module-scope `pymongo`/`bson` import anywhere under
  `aggregator/`; every `from pymongo …` in `mirror.py` is inside a function
  body; options come from `ms.client_options()`. `tests/test_stdlib_only.py`
  passes, and the live arm skips with a named reason on a bare checkout.
- **GD-22 / GD-30.** `enqueue` contains no `await` and cannot raise (the
  generator-materialisation path catches `Exception`, `mirror.py:2003`); the
  breaker is checked before every driver touch including the lease re-take
  (`mirror.py:2210`); `Mapper.__call__` passes `scrub=False`, and
  `tests/test_mirror.py:1211` proves 0 walks on the loop side.
- **GD-24 / GD-25.** Every `_id` is a `refs.ref_key`; `validate_op` runs
  `spec_for`/`check_id`/`validate_update`; no `$inc` and no `$unset` anywhere
  (asserted over the AST *and* the raw text, `tests/test_mirror.py:1328-1337`);
  `_take_batches` deliberately does not collapse two updates to one `_id`;
  `save_cursor`'s `$set` is argued and correct.
- **GD-26.** No delete verb is called outside the two guarded doors; both
  backends refuse every collection but `stream_meta` for `delete_many` and every
  name but `derived` for `drop_collection`, in their own bodies (AST-asserted);
  `_assert_scoped` refuses `{}` and gen-only filters; `allow_empty_reinsert`
  closes attempt-4's NIT 1 properly.
- **GD-27.** 0600/0400 accepted and every group/other/exec bit refused;
  symlinks refused; `save_credentials` uses `O_EXCL|0600` at open time;
  `database_name` is `touch_<sha1(realpath)[:8]>` fenced *with* the underscore;
  `redact` runs structural-then-literal with `_MIN_LITERAL_SECRET` argued;
  `secrets` carries both spellings of the password; `holderBoot` is a digest,
  never the raw `boot_id`; the deny-list is consulted before the extension
  filter in `iter_backfill_sources`.
- **GD-28 / GD-29 (except MAJOR 1).** No `provenance:"harness"` is emitted here;
  the lease is a real conditional write; tolerated dups are counted;
  `refused_no_lease` / `refused_policy` are split correctly and the
  `docs/mongo.md:59-70` list is asserted equal to `health()["counters"]` in both
  directions, as is the block's field list against `docs/mongo.md:38-41`.
- **R-42 / R-57 docs.** §0 "Mongo down is a non-event" incl. the new
  recovers-without-traffic paragraph, the loopback+auth recipe, the
  least-privilege role, the derived DB name, "Never publish 27017", the
  rebuild/backfill commands, the four measured growth numbers, the no-TTL law
  and teardown are all present and match the code.
- **Test quality.** The suite asserts behaviour, not tautologies: AST walks for
  the delete/TTL/`$inc` laws, call-count assertions rather than timings for the
  breaker, real lease acquisition in the fixture, and doc↔code equality in both
  directions. `tests/test_mirror.py:672` and `:803` are genuine regression arms
  for attempt 4's majors, not restatements of them.

---

## Verdict fields

- `approved`: **false**
- `depth`: **in-scope** — MAJOR 1 is one predicate plus three call sites and one
  test arm; every minor is a line or a sentence. No sub-plan boundary is
  crossed, no new research is needed, and `live_mirror()` already holds a real
  lease so the existing tests need no rework.
- `critical_defect`: **false** — nothing here corrupts the store as it stands
  (the unguarded paths have no production caller yet) or invalidates the
  remaining sub-plans' design. It is worth telling the next implementer that
  MAJOR 1 has a forward edge: SD-10 hands `sweep` to the ingest side, so the
  guard is cheaper to add before that wiring exists than after.
