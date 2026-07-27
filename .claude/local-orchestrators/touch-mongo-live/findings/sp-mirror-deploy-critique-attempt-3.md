# sp-mirror-deploy — adversarial critique, attempt 3

**Verdict: REJECTED.** 2 major, 4 minor, 2 nits. `depth: in-scope`,
`critical_defect: false`.

Reviewed (full content — the tree is untracked, `git diff` is empty):

- `aggregator/mirror.py` (2 464 lines)
- `docs/mongo.md` (315 lines)
- `tests/test_mirror.py` (1 480 lines)
- `tests/test_mongo_deploy.py` (857 lines)

Against `plan/touch-mongo-live-subplans.md` §`sp-06 — mirror-deploy`,
`touch-mongo-live-plan.md` GD-21…GD-30 + R-42/R-45/R-57, and
`touch-full-recon-plan.md` GD-1…GD-20.

I re-ran both owned suites myself: `tests/test_mirror.py` rc=0,
`tests/test_mongo_deploy.py` rc=0 (the live docker arm **ran**, it did not skip —
`mongo:7` is present locally, the documented recipe provisioned, the documented
role bootstrap executed, and the server refused `delete` on `records`). Ownership
is clean: only the four owned files carry post-implementer mtimes,
`aggregator/__init__.py`, `tests/run_all.sh` and every sibling module are
untouched, `git log -1` is still `579446e`, nothing committed. The two
attempt-2 majors are genuinely closed — I verified the GD-24 schema-field
exemption is *derived* from `refs.KIND_SPECS` + `mongo_store.COLLECTIONS`
(`"sessionKey" in mr.SCHEMA_FIELD_NAMES` → True), and that the scrub runs once,
on the drainer side, with a call-count assertion rather than an AST proxy.

Both majors below are things I reproduced by running the module, not readings.

---

## MAJOR 1 — `tick()` and `start()` do raise: any non-`MongoUnavailable` failure on the lease path kills the drainer task while `/health` keeps saying `live`

**File:** `aggregator/mirror.py:1698` (`Mirror.acquire`'s `except` clause), with
the escape sites at `aggregator/mirror.py:1655` (`start`),
`aggregator/mirror.py:1912` and `aggregator/mirror.py:1919` (`tick`).

`Mirror.acquire()` catches exactly one exception type:

```python
        except ms.MongoUnavailable as exc:
            self._record_failure(exc)
            return False
```

But `Backend.guarded_update` — the only call inside it — has three other exits:

- `AsyncBackend.guarded_update` → `ms._driver_error(...)`, which returns
  `SchemaError` (a `MongoStoreError`, **not** a `MongoUnavailable`) whenever
  mongod answers code 121, i.e. the server's `$jsonSchema` refusing the
  `writers` document (`aggregator/mongo_store.py:1600`);
- `ms.validate_document(collection, candidate)` at `aggregator/mirror.py:1347`,
  which raises `MongoStoreError`;
- anything the driver raises that is not a `PyMongoError` — and the module's own
  prose names the real specimen at `aggregator/mirror.py:1957`:
  *"`RuntimeError: Cannot use AsyncMongoClient in different event loop` is the
  real specimen: not a MongoStoreError, not a MongoUnavailable, and fatal to the
  task if it escapes — which would leave a process whose mirror is dead while
  `/health` still claimed `live`."*

That defence exists **only** around `bulk_upsert`. The lease path sits on the
same tick, one branch earlier, and is unguarded — so the exact outcome the
comment describes is what happens. `tick()`'s docstring says *"One drain cycle.
Returns a small report; never raises"* (`mirror.py:1872`) and `start()`'s says
*"Every failure here is a **state**, never an exception"* (`mirror.py:1603`).
Both are false.

**Failure scenario (executed against this checkout, not hypothesised):**

```python
b = mr.MemoryBackend()
m = mr.Mirror(mr.MongoConfig("u", "touch_test"), backend=b, lease_ttl=30.0)
asyncio.run(m.start(ensure_schema=False))          # -> 'live'
m._lease["expiresAt"] = m.clock().isoformat()...   # force a renewal this tick
b.fail = RuntimeError("Cannot use AsyncMongoClient in different event loop")
asyncio.run(m.tick())
# !! tick RAISED: RuntimeError Cannot use AsyncMongoClient in different event loop
```

Same with `ms.SchemaError("writers: the server's $jsonSchema refused this
guarded update")` → `!! tick2 RAISED: SchemaError`. And `start()` itself:
`!! start RAISED: RuntimeError driver surprise`.

Driven through the real drainer, the consequence is the documented worst case:

```
drainer done? True | health state: live | queued: 1 | written: 0
  task exception: RuntimeError('Cannot use AsyncMongoClient in different event loop')
```

The `run()` task is dead, its exception is never retrieved (asyncio swallows it
until GC), `/health` reports `state:"live"` with `lastError:null` forever, and
every subsequent `enqueue` accumulates in a queue nobody drains until it
overflows into `dropped` — a counter *"nobody looks at until the state tells
them to"*, by this module's own words. Reachable on any tick: the lease renews
whenever less than 50 % of the 30 s TTL remains, i.e. at least every ~15 s.

This is a direct breach of GD-22 (`/health` reports `degraded|down|absent` when
the mirror is not working), GD-30 (the breaker/degrade ladder is what a failure
is supposed to reach), and GD-21's "never fails startup" for the `start()` half.

**Fix (one gated attempt):**

1. In `Mirror.acquire`, widen the guard to the same shape `tick` already uses for
   `bulk_upsert` — `except ms.MongoUnavailable` → `_record_failure`;
   `except Exception as exc:  # noqa: BLE001` → `_record_failure(exc)` and
   `return False`, so a driver surprise degrades and the breaker takes over.
2. Wrap the two `await self.acquire()` call sites and the `start()` one so a
   `False` return is the only outcome either can see.
3. Belt: wrap the body of `run()`'s `await self.tick()` in
   `try/except Exception` → `self._record_failure(exc)` and continue, so the
   long-lived task can never die silently regardless of which branch surprises
   it.
4. Test it: `MemoryBackend(fail=RuntimeError(...))` and
   `MemoryBackend(fail=ms.SchemaError(...))` with a due lease ⇒ `tick()` returns
   a report, `state` is `degraded`/`down`, and a `run()` task is still alive and
   writing after the failure clears. Today every `fail=` in `tests/test_mirror.py`
   is a `ms.MongoUnavailable`, which is why this passed the gate.

---

## MAJOR 2 — GD-27's document backstop skips the whole `ref` sub-document on an invariant nothing enforces, and GD-24 deliberately makes `ref` an open tail

**File:** `aggregator/mirror.py:862-882` (`scrub_op_update`), with the claim
restated at `aggregator/mirror.py:309`, `aggregator/mirror.py:313`,
`aggregator/mirror.py:871` and in `docs/mongo.md:205-206`.

```python
    return {operator: {field: (value if field == REF_FIELD else scrub_value(value))
                       for field, value in fields.items()}
            for operator, fields in update.items()}
```

The justification given, three times in code and once in the docs, is:

> *"Its shape, its key set and its value grammar are already fixed by
> `refs.validate_ref`, so it is the one place in a Touch document where nothing
> arbitrary — and therefore no quoted credential — can appear."*
> (`docs/mongo.md:205`: *"skipped whole: its shape and values are already fixed
> by `refs.validate_ref`, so nothing arbitrary can hide in it"*)

Two independent problems:

**(a) Nothing on the write path calls `refs.validate_ref`.**
`grep -n validate_ref aggregator/*.py` finds it defined in `refs.py`/`store.py`
and mentioned only in `mirror.py`'s comments — `mongo_store.validate_update`
never touches it (`mongo_store.py` contains no reference to a `ref` field's
shape at all). A mapper that builds `ref` by hand instead of via
`refs.canonical_ref` gets it stored verbatim, unscrubbed, unvalidated.

**(b) Even if it were called, GD-24 says `ref` has an open tail.**
GD-24: *"Unknown ref shapes: retained under `ref` with `kind:"unknown"`, no
`refId`, excluded from joins (GD-11 open tail preserved)."* `refs.validate_ref`
implements exactly that — *"Unknown shapes pass through untouched"*
(`refs.py:741`) — and `refs.canonical_ref` copies every key of an unknown ref
through with only a sort applied (`refs.py:771-775`). So the one sub-document
the backstop exempts is, by design, the one that may carry arbitrary
agent-authored keys.

**Failure scenario (executed):**

```python
hostile = refs.canonical_ref({"kind": "unknown",
                              "authToken": "sk-ant-api03-AAAA…",
                              "password": "hunter2"})
upd = ms.op_set({"stream": "s", "seq": 2, "source": "touch",
                 "provenance": "asserted", "kind": "k", "ref": hostile})
mr.scrub_op_update(upd)["$set"]["ref"]
# {'kind': 'unknown',
#  'authToken': 'sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
#  'password': 'hunter2'}
refs.validate_ref(hostile)   # -> 'unknown'  (nothing pinned, nothing refused)
```

Both credentials pass the backstop untouched and land in `events` /
`custom_state_events`. In an upsert-only mirror (GD-26) that is permanent, and
`/health` and every read API subsequently serve it. The consumers that will
produce such refs are downstream sub-plans that are *told* to trust this
guarantee: SD-8/R-53's control-intent ingest reads agent-written control files,
and GD-24 mandates retaining shapes it cannot classify.

**Fix (one gated attempt):** make the exemption conditional on the property it
claims, instead of unconditional on the field name:

```python
def _scrub_ref(value):
    """Exempt only a ref whose kind pins its field set (GD-24's closed union).
    `kind:"unknown"` is GD-11's open tail — arbitrary keys — so it is scrubbed
    like any other payload."""
    if not isinstance(value, dict):
        return scrub_value(value)
    try:
        kind = refs.classify(value)
    except refs.RefError:
        kind = "unknown"
    return value if kind not in ("none", "unknown") else scrub_value(value)
```

and call it for `field == REF_FIELD`. This keeps attempt-2's fix intact — `slot`
and `customState` refs classify to a known kind and stay byte-identical, so
`{"ref.sessionKey": …}` still joins, and `runNode.key` (the one declared ref
field the value-exempt rule would otherwise corrupt) is still protected — while
closing the open tail. Then fix `docs/mongo.md:205-206` to say what the code
actually does ("a ref of a *declared* kind is skipped whole; an
`kind:\"unknown\"` ref is scrubbed like any other payload"), and add the
hostile-unknown-ref case to
`test_the_scrub_never_corrupts_a_schema_field_or_a_ref`.

---

## MINOR 1 — a failed lease renewal that is not a refusal lets the same tick write anyway

**File:** `aggregator/mirror.py:1918-1922`.

```python
        if self._lease["held"] and self._lease_due():
            await self.acquire()
            if self.state == STATE_REFUSED:
                report["skipped"] = STATE_REFUSED
                return report
```

`acquire()` returns `False` for two different reasons: lost race (⇒ `REFUSED`,
handled) and `MongoUnavailable` (⇒ `_record_failure`, state `degraded` until the
breaker opens). In the second case the tick falls through and calls
`bulk_upsert` with a lease that was **not** renewed and may already have
expired — precisely what GD-29 forbids ("a process that cannot hold the lease
refuses to mirror"). It is mostly self-limiting (if the server is unreachable
the writes fail too), but a partial outage that affects `writers` only is
enough. Fix: branch on the boolean, not on the state —
`if not await self.acquire(): report["skipped"] = self.state; return report`.

## MINOR 2 — requeued operations are scrubbed again on every retry

**File:** `aggregator/mirror.py:1852-1869` (`_requeue`) with
`aggregator/mirror.py:1840` (`validate_op(op, source="queue")`).

`_requeue` puts the **already-scrubbed** `MirrorOp` back on the queue, and the
next drain runs `validate_op(..., scrub=True)` over it again. The result is
correct (the scrub is idempotent) but it re-pays the 8.79 ms-per-550 KB walk
this attempt's own headline change exists to pay exactly once — on every tick of
an outage, for every op in flight. `_take_batches`' docstring ("This is also
where GD-27's document backstop runs — the *only* place it runs") and
`validate_op`'s "the drainer runs it exactly ONCE per operation" are both
inaccurate under retry. Fix: mark scrubbed ops (a `_scrubbed` flag on the
`MirrorOp` subclass, or requeue via a private path that skips the scrub) and
assert it in `test_a_transient_outage_requeues_rather_than_losing_writes` with
the same call-counting technique attempt 3 already introduced.

## MINOR 3 — `enqueue` books every refusal under `refused_no_lease`

**File:** `aggregator/mirror.py:1746-1749`.

```python
        if self.state == STATE_REFUSED or (stream is not None
                                           and stream != self._lease["stream"]):
            self.stats["refused_no_lease"] += len(ops)
```

`STATE_REFUSED` has three causes the module is careful to keep apart everywhere
else (`_lease_lost` exists exactly for that): a lost lease, a zero-user mongod
(GD-27), and a schema Touch will not write to. All three land in a counter named
`refused_no_lease`, which `/health` publishes under `counters`. An operator
reading "42 refused_no_lease" against an unauthenticated mongod will look for a
second writer that does not exist. Fix: `refused_no_lease` only when
`self._lease_lost` or the stream mismatched; otherwise a `refused_policy`
counter (and document both in `docs/mongo.md`'s `/health` list — note the field
list there is asserted equal to `health()` by
`test_health_is_r45s_block_and_carries_no_credential`, but `counters`' keys are
not, so that test will not catch the doc drift for you).

## MINOR 4 — the deny-list is never actually exercised by the backfill walk, and the test that says it is cannot fail

**Files:** `aggregator/mirror.py:2326-2345` (`iter_backfill_sources`),
`tests/test_mongo_deploy.py:510-520`.

The walk yields only names ending `.jsonl`; every basename in `DENY_BASENAMES`
ends `.json`. So `deny(path)` can never be true for a candidate, and the test

```python
        (root / "projects" / ".credentials.json").write_text("{}", encoding="utf-8")
        found = mr.iter_backfill_sources(root)
    check([Path(p).name for p in found] == ["session.jsonl"], …)
```

passes identically with the deny-list deleted — the `.jsonl` filter alone
produces the result. The *claim* ("GD-27's deny-list is applied here, at the
source") is therefore untested. Fix: pass a `deny=` that records its calls and
assert it was consulted for `session.jsonl`, or assert
`iter_backfill_sources(root, deny=lambda p: p.endswith("session.jsonl")) == []`
so the parameter is load-bearing in the test as it is in the code.

## NIT 1 — `redact`'s literal pass ignores secrets shorter than 3 characters

`aggregator/mirror.py:381`: `if isinstance(secret, str) and len(secret) >= 3`.
A 1–2 character URI password survives the literal pass. The structural pass
still covers the URI form, so this is cosmetic — but the threshold deserves the
one-line comment the rest of this module gives every other constant.

## NIT 2 — `discover_mappers`' `ModuleNotFoundError` filter matches on the leaf name

`aggregator/mirror.py:748`: `if (exc.name or "").split(".")[-1] != name: raise`.
An entity module named `legacy` that fails because a *third-party* top-level
module also called `legacy` is missing would be silently skipped rather than
raised. Vanishingly unlikely with the five names in `ENTITY_MODULES`; comparing
against the fully-qualified `f"{package}.{name}"` removes the ambiguity entirely.

---

## What I checked and found correct (so the next attempt does not churn it)

- **GD-21.** `pymongo` appears only inside function bodies
  (`AsyncBackend.connect`, `bulk_upsert`, `guarded_update`, `update_many`,
  `delete_many`, `drop_collection`) — never at module scope; `import aggregator.mirror`
  succeeds with no third-party package installed, and absence resolves to
  `STATE_ABSENT` with a truthful `lastError`. Client options come from
  `ms.client_options()`, not re-spelled locally.
- **GD-22 / GD-30.** `enqueue` is synchronous, has no `await`, cannot raise, and
  drops+counts+degrades on a full queue. The breaker demonstrably stops the tick
  before it touches the driver (proven by `backend.calls`, not by timing).
- **GD-24 / GD-25.** Every `_id` goes through `refs.ref_key`; `validate_op` runs
  `spec_for`/`check_id`/`validate_update` at the registry boundary; no `$inc`
  anywhere; `_take_batches` deliberately does **not** collapse two updates to one
  `_id`, which is the right call under `$max`/`$addToSet`.
- **GD-26.** No forbidden delete verb is *called* (AST, not grep, so the prose
  explaining the rule does not trip it); `delete_many` refuses everything but
  `stream_meta` and `drop_collection` everything but `derived`, in both backends'
  own bodies; `_assert_scoped` refuses `{}` and gen-only filters; no
  `expireAfterSeconds`, no `$unset` even in prose. The live arm proves the
  *server* refuses `delete` on `records` under the documented role.
- **GD-27.** 0600/0400 accepted, every group/other/exec bit refused, symlink
  refused, `save_credentials` uses `O_EXCL|0600` at open time. `database_name`
  is `touch_<sha1(realpath)[:8]>`, fenced to `touch_` **with** the underscore
  (`touchdown_prod`, `touchy`, bare `touch` all refused). No connection-string
  literal or hardcoded `27017` under `aggregator/`. `/health` carries no URI, no
  password (both spellings), and `holderBoot` is a hash rather than the raw
  `boot_id`. Tests build `touch_test_<pid>` and drop only that.
- **GD-28 / GD-29.** No `provenance:"harness"` is ever emitted here; the lease is
  a real conditional write proven against a real mongod, tolerated dups are
  counted rather than swallowed, and the TTL re-take is implemented and tested.
- **R-42 / R-57 docs.** The `docker run` recipe is *parsed out of the page* and
  re-run with only the identifiers changed, so weakening the documentation
  weakens the container the assertions run against. `0.0.0.0` and bare
  `-p 27017:27017` appear only inside prohibition paragraphs, judged per
  paragraph. All four measured growth numbers, the no-TTL law, "Mongo down is a
  non-event", `--rebuild`/`--backfill`, and "never publish 27017" are present.
  The `/health` field list on the page is asserted equal to `health()` in both
  directions.
- **Ownership.** Four files touched, nothing else; no commit; no Mongo residue
  (the gate's container and volume are gone); `tests/run_all.sh` globs, so both
  new suites are in the full run without editing a file this sub-plan does not
  own.

## Verdict fields

- `approved`: **false**
- `depth`: **in-scope** — both majors are local edits to `mirror.py` (one
  `except` clause plus a `run()` guard; one conditional in `scrub_op_update`),
  one doc sentence, and three tests. No sub-plan boundary is crossed and no new
  research is needed.
- `critical_defect`: **false** — nothing here corrupts the store or invalidates
  the remaining sub-plans' design. MAJOR 2 does have a forward edge worth noting
  to the next implementer: sp-07…sp-11 are being told, in code comments and in
  `docs/mongo.md`, that `ref` is a safe subtree, so fixing it before those
  mappers are written is cheaper than after.
