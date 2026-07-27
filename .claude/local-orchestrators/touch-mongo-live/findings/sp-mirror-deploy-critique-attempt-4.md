# sp-mirror-deploy — adversarial critique, attempt 4

**Verdict: REJECTED.** 3 major, 4 minor, 2 nits. `depth: in-scope`,
`critical_defect: false`.

Reviewed (full content — the tree is untracked, `git diff` is empty for all four):

- `aggregator/mirror.py` (2 662 lines)
- `docs/mongo.md` (333 lines)
- `tests/test_mirror.py` (1 761 lines)
- `tests/test_mongo_deploy.py` (911 lines)

Against `plan/touch-mongo-live-subplans.md` §`sp-06 — mirror-deploy`,
`touch-mongo-live-plan.md` GD-21…GD-30 + R-42/R-45/R-57, and
`touch-full-recon-plan.md` GD-1…GD-20.

Ownership is clean: only the four owned files carry this attempt's mtimes,
`git log -1` is still `579446e`, nothing committed, no sibling module touched.

**Every finding below was reproduced by running this checkout**, not read off the
page. Scripts are inlined so the next implementer can re-run them.

Two of attempt 3's findings are genuinely closed and I will not re-litigate them:

- **attempt-3 MAJOR 1** (lease-path exception killing the drainer). `acquire`
  now has the broad `except Exception` (`mirror.py:1821`), `start`/`tick` wrap
  their bodies (`mirror.py:1706`, `mirror.py:2045`), `run()` has the belt
  (`mirror.py:2191`), and the new test at `tests/test_mirror.py:430` injects both
  real specimens and asserts the task survives. I re-ran it; it is load-bearing.
- **attempt-3 MINOR 4** (deny-list not load-bearing). `tests/test_mongo_deploy.py:536-560`
  now records the `deny` calls *and* proves a `.jsonl`-naming rule is honoured.
  Correct, and the walk asks `deny` before the extension filter
  (`mirror.py:2538`), which is the ordering that makes the claim true.

**attempt-3 MINOR 2** (`_requeue` re-scrubbing) is closed properly: `ScrubbedOp`
(`mirror.py:876`) is a type-as-flag, minted only by `validate_op`, and the
stampers drop it when they merge. See MINOR 2 below for the one hole it opens.

**attempt-3 MAJOR 2 is NOT closed** — it was narrowed by one case and the
remaining case is the one the fix's own docstring claims to have handled.

---

## MAJOR 1 — the drainer writes with **no writer lease at all** whenever the first `acquire()` fails for any reason other than a lost race, and `/health` then publishes `state:"live"` beside `lease:{held:false}`

**File:** `aggregator/mirror.py:2084` (`_tick`'s lease branch), reached from
`aggregator/mirror.py:1763` (`_start`) and unguarded by
`aggregator/mirror.py:1881` (`enqueue`).

```python
        if self._lease["held"] and self._lease_due():
            if not await self.acquire():
                report["skipped"] = self.state
                return report
```

Attempt 4 fixed the *inner* condition (attempt-3 MINOR 1: branch on the boolean,
not the state — correct, and tested at `tests/test_mirror.py:531`). The **outer**
one is the hole: the whole lease branch is gated on `self._lease["held"]`, so if
the lease was never acquired the tick never tries to acquire it and never
declines to write. There is no other lease check between the queue and
`bulk_upsert`; `enqueue` refuses only on `state == STATE_REFUSED` or a stream
mismatch (`mirror.py:1881`), and a *failed* acquire produces `degraded`, not
`refused`.

`_start` reaches exactly that state:

```python
        if acquire_lease and not await self.acquire():
            return self.state          # mirror.py:1763 — returns `degraded`, held=False
```

Attempt 4's own widening of `acquire`'s `except` is what makes this easy to
reach: every non-race failure now becomes `_record_failure` → `degraded` →
`return False`, and `degraded` is a state `enqueue` accepts and `_tick` writes in.

**Failure scenario (executed against this checkout):**

```python
import asyncio, uuid as U
from aggregator import mirror as mr, mongo_store as ms, refs

class FlakyLease(mr.MemoryBackend):
    """ping/schema fine; only the `writers` write fails, then recovers."""
    def __init__(self, exc):
        super().__init__(); self.exc = exc
    async def guarded_update(self, collection, key, update, *, require=None, upsert=True):
        if collection == "writers" and self.exc is not None:
            raise self.exc
        return await super().guarded_update(collection, key, update,
                                            require=require, upsert=upsert)

def op():
    k = refs.ref_key({"kind": "uuid", "uuid": str(U.uuid4())})
    return mr.MirrorOp("records", k, ms.op_set(
        {"sessionId": "s1", "type": "user", "provenance": "harness"}))

for exc in (ms.MongoUnavailable("writers unreachable"),
            RuntimeError("Cannot use AsyncMongoClient in different event loop"),
            ms.SchemaError("writers: server $jsonSchema refused")):
    b = FlakyLease(exc)
    m = mr.Mirror(mr.MongoConfig("mongodb://u:p@127.0.0.1:27017/", "touch_test"), backend=b)
    st = asyncio.run(m.start(ensure_schema=False))   # -> 'degraded', lease held=False
    b.exc = None                                    # the blip clears
    m.enqueue([op()])
    asyncio.run(m.tick())
    print(st, m.state, m._lease["held"], m.stats["written"], b.calls["guarded_update"])
```

```
degraded live False 1 0        # MongoUnavailable
degraded live False 1 0        # RuntimeError  (the module's own named specimen)
degraded live False 1 0        # SchemaError
```

So: `written == 1`, `guarded_update` calls `== 0` (the lease was never taken and
never retried), and `health()` is

```
{'state': 'live', 'lease': {'held': False, 'expiresAt': None, ...}}
```

Three separate invariants break at once:

- **GD-29** — "a process that cannot hold the lease refuses to mirror while
  remaining perfectly able to serve reads" (`mirror.py:50-52`). This process
  cannot hold the lease and mirrors anyway. If another aggregator *does* hold it,
  these are the two live writers on one stream the lease exists to prevent, and
  the tolerated-dup counter that is supposed to be the tell reads 0 because the
  writes are upserts on disjoint uuids.
- **GD-22 / R-45** — `/health` publishes `state:"live"` next to
  `lease:{"held":false}`. That block is served verbatim by sp-12's `server.py`,
  and it is self-contradicting: nothing downstream can tell this apart from a
  healthy mirror.
- The module's own contract at `mirror.py:2084-2092`, whose comment explains at
  length why the *renewal* must not write under an unrenewed lease — while the
  never-renewed case walks past the same door.

Why the gate missed it: `tests/test_mirror.py:551-553` forces
`mirror._lease.update(held=True, …)` before exercising the renewal, and the
`start()`-is-total arm at `tests/test_mirror.py:479-491` asserts only that
`start` returns a state — it never ticks afterwards. `held=False` + a healthy
server is untested.

**Fix (one gated attempt):**

1. Record that a lease is *required*: set a `self._lease_required = bool(acquire_lease)`
   in `_start` (default True), or simply drop the `held` conjunct and let
   `_lease_due()` — which already returns `True` when `not held` (`mirror.py:1845`)
   — carry the branch:

   ```python
   if self._lease_required and self._lease_due():
       if not await self.acquire():
           report["skipped"] = self.state
           return report
   ```

   The flag is needed so `start(acquire_lease=False)` and the raw-`Mirror(...)`
   test fixtures keep working without silently acquiring.
2. Belt at the write: `if self._lease_required and not self._lease["held"]:
   report["skipped"] = "no-lease"; return report` immediately before
   `_take_batches`, so no future refactor of the branch above can re-open it.
3. Make `health()` incapable of the contradiction: `state == STATE_LIVE` must
   imply `lease["held"]` whenever a lease is required (assert it in the health
   test, `tests/test_mirror.py:1454`).
4. Test it with the script above — the `FlakyLease` backend (only `writers`
   fails, everything else healthy) is the shape none of the existing fakes have:
   `MemoryBackend.fail` fails *everything*, which hides this exactly the way a
   whole-server outage hides attempt-3's MINOR 1.

---

## MAJOR 2 — the `ref` exemption still rests on a property nothing checks: `_scrub_ref` classifies with `refs.classify`, which does **not** enforce the closed key set, so a hand-built declared-kind ref carries arbitrary secret keys straight into the store

**File:** `aggregator/mirror.py:501` (inside `_scrub_ref`), with the claim
restated at `aggregator/mirror.py:315-319`, `aggregator/mirror.py:484-487`,
`aggregator/mirror.py:963-966` and `docs/mongo.md:218-224`.

```python
    try:
        kind = refs.classify(value)
    except refs.RefError:
        kind = "unknown"
    return value if kind not in UNPINNED_REF_KINDS else scrub_value(value)
```

The justification, stated four times:

> *"A ref of one of GD-24's seven declared kinds has a closed key set and
> per-field value pins (`refs.KIND_SPECS`), so there is nowhere in it for a
> quoted credential to sit."* (`mirror.py:485-487`; `docs/mongo.md:218-220`:
> *"skipped whole: one of GD-24's seven union members has a closed key set and
> per-field value pins, so there is nowhere in it for a credential to sit"*)

`refs.classify` checks neither. Its own docstring says so — *"Name `ref`'s kind
**without validating its values**"* (`refs.py:704`) — and its declared-kind
branch is a bare membership test (`refs.py:719-722`): `kind` in `KIND_SPECS` ⇒
return it, whatever else the dict contains. The function that *does* enforce the
closed key set is `refs.validate_ref`, at `refs.py:747-748`:

```python
    extra = set(ref) - {"kind"} - set(spec.fields)
    if extra:
        raise RefError(f"ref kind {kind!r} has unexpected fields {sorted(extra)}")
```

and attempt 3's finding (a) — *nothing on the write path calls `validate_ref`* —
is still true: `grep -n validate_ref aggregator/mongo_store.py` is empty, and
`ms.validate_update` has no notion of a ref's shape. So the exemption is decided
by the one classifier that cannot see the violation.

**Failure scenario (executed):**

```python
import uuid as U
from aggregator import mirror as mr, mongo_store as ms, refs

hostile = {"kind": "uuid", "uuid": str(U.uuid4()),           # a DECLARED kind
           "authToken": "sk-ant-api03-" + "A"*30,
           "password": "hunter2"}
print(refs.classify(hostile))          # 'uuid'      -> exempt
refs.validate_ref(hostile)             # RefError: ref kind 'uuid' has unexpected
                                       #           fields ['authToken', 'password']

upd = ms.op_set({"stream": "s", "seq": 2, "source": "touch",
                 "provenance": "asserted", "kind": "k", "ref": hostile})
print(mr.scrub_op_update(upd)["$set"]["ref"])
key = refs.ref_key({"kind": "event", "stream": "s", "seq": 2})
print(mr.validate_op(("events", key, upd), scrub=True).update["$set"]["ref"])
```

```
uuid
{'kind': 'uuid', 'uuid': '8286…', 'authToken': 'sk-ant-api03-AAAA…', 'password': 'hunter2'}
{'kind': 'uuid', 'uuid': '8286…', 'authToken': 'sk-ant-api03-AAAA…', 'password': 'hunter2'}
```

Both credentials survive the backstop, `validate_op` accepts the operation, and
in an upsert-only mirror (GD-26) they are in `events` permanently and on every
read route thereafter. Attempt 4 closed the `kind:"unknown"` door and left the
`kind:"<declared>" + extra keys` door open — which is the *same* door, because
the ref that reaches here has not been through `canonical_ref` (that is precisely
attempt-3's point (a), and `canonical_ref` is what would have stripped the extras:
it calls `validate_ref` first, `refs.py:757`).

Reachability is not hypothetical: sp-07…sp-11 build refs, and they are being told
by three code comments and a documentation page that `ref` is a safe subtree, so a
mapper that assembles `{"kind": "agentId", "agentId": …, **passthrough}` is the
natural thing to write against this contract.

The new test cannot catch it because it builds its hostile ref *through the
constructor that strips extras*: `tests/test_mirror.py:876` is
`refs.canonical_ref({"kind": "unknown", …})`, and the negative case at
`tests/test_mirror.py:903` uses `{"kind": "not-a-real-kind"}` — a kind
`classify` refuses. Neither is a declared kind with an undeclared key.

**Fix (one word plus a test):** classify with the function that enforces the
claim. `validate_ref` raises `RefError` for extra fields and for a failed value
pin, and `_scrub_ref`'s existing `except refs.RefError: kind = "unknown"` already
routes that to the scrub — so the whole change is:

```python
    try:
        kind = refs.validate_ref(value)     # not classify: THIS is the function
    except refs.RefError:                   # that enforces the closed key set
        kind = "unknown"                    # (refs.py:747) and the value pins
```

`UnknownRefError` subclasses `RefError` (`refs.py:138`), so the `except` still
covers the bogus-kind case. Verify the declared-kind fast path is unchanged:
`_scrub_ref(slot) is slot` must still hold (it does — `validate_ref` returns the
kind and mutates nothing), so `{"ref.sessionKey": …}` keeps joining. Then:

- add the declared-kind-plus-extra-keys case to
  `test_the_scrub_never_corrupts_a_schema_field_or_a_ref`
  (`tests/test_mirror.py:792`), built **by hand**, not via `canonical_ref`;
- correct the four prose sites to say what the code then does — the exemption is
  for a ref that *validates* against its kind's spec, not one that merely names
  a known kind (`mirror.py:315-319`, `mirror.py:484-492`, `mirror.py:963-966`,
  `docs/mongo.md:218-224`).

---

## MAJOR 3 — after the breaker recovers, `/health` stays `down` **indefinitely** on an idle deployment: a successful lease renewal never clears the failure count, and a no-work tick returns before the state-clearing block

**File:** `aggregator/mirror.py:2100-2101` (`if not batches: return report`) with
`aggregator/mirror.py:1839-1842` (`acquire`'s success path, which never calls
`_record_success`) and `aggregator/mirror.py:2152` (the only `_record_success`
call site, after the batch loop).

`_record_success` — the only thing that resets `_failures` and lets the state be
promoted — is reachable **only** through a tick that actually had batches. An
idle mirror (empty queue, which is the steady state between transcript writes)
returns at `mirror.py:2101`, so:

- `_failures` stays at `breaker_failures`, so the *next* single failure re-opens a
  full 30 s breaker hold immediately rather than after N;
- `self.state` stays `STATE_DOWN` even though the server is answering — proven by
  the successful `guarded_update` on `writers` that the same tick just made.

**Failure scenario (executed):**

```python
import asyncio
from aggregator import mirror as mr, mongo_store as ms

clock = {"t": 1000.0}
b = mr.MemoryBackend()
m = mr.Mirror(mr.MongoConfig("u", "touch_test"), backend=b, monotonic=lambda: clock["t"])
asyncio.run(m.start(ensure_schema=False))            # live
b.fail = ms.MongoUnavailable("down")
for _ in range(3):                                   # trip the breaker
    m._lease["expiresAt"] = m.clock().isoformat().replace("+00:00", "Z")
    asyncio.run(m.tick())
b.fail = None; clock["t"] += 31.0                    # server healthy, hold lapsed
for _ in range(5): asyncio.run(m.tick())             # 5 healthy, work-free ticks
print(m.state, m._lease["held"], m.queue.qsize())
```

```
down True 0
```

Five consecutive ticks each renewed the lease against a healthy server — a real
round trip, `acquired: True` — and `/health` still reports `down` with the stale
`lastError: "MongoUnavailable: down"`. It clears only when a write happens to
arrive:

```python
m.enqueue([op()]); asyncio.run(m.tick())   # -> state 'live'
```

`docs/mongo.md:29` promises `degraded` for "mongod slow or erroring" and
`down` for "no mongod running". Neither describes this: nothing is wrong, and the
one route an operator pages on says the mirror is dead for as long as the session
is quiet. That is the same class of untruth as attempt-3's MAJOR 1 with the sign
reversed — fail-safe rather than fail-open, so no data is lost, but GD-22's
requirement is that `/health` be *truthful*, not that it be pessimistic, and this
one never self-heals without traffic.

**Fix (one gated attempt):**

1. Call `self._record_success()` on `acquire`'s success path
   (`mirror.py:1839-1842`) — it is a completed server round trip, and it is the
   only one an idle mirror makes.
2. Move the state-clearing block (`mirror.py:2153-2165`) so a work-free tick
   reaches it, i.e. run it before the `if not batches: return report` early exit
   (or drop that early return and let the empty `pending` loop fall through).
3. Test: the script above, asserting `m.state == STATE_LIVE` after the healthy
   work-free ticks and `m._failures == 0`. Extend
   `test_the_breaker_holds_then_lets_the_mirror_recover`
   (`tests/test_mirror.py:571`) — today it recovers *with* queued work, which is
   why this passed.

---

## MINOR 1 — a recovered mirror publishes `state:"live"` with a stale `lastError`, contradicting `docs/mongo.md`'s literal promise

**Files:** `aggregator/mirror.py:2153-2165` (the clean-tick block sets `state`
and nothing else) vs `docs/mongo.md:42-45`.

The page says, and offers as a contract:

> *"`lastError` is only ever a **fault** … A `live` mirror never publishes a
> `lastError`, so an alert rule can read that field literally."*

The re-take branch knows the rule and clears it (`mirror.py:2082`,
`self.last_error = None`). The ordinary degrade→recover path does not. Executed:

```
after work arrives: state=live written=1 lastError='MongoUnavailable: down'
```

An alert rule written against the documented contract fires forever after the
first transient blip. `tests/test_mirror.py:1513-1525` asserts the property only
for a mirror that never had a fault, so the recovery case is uncovered.

**Fix:** clear `self.last_error = None` in the clean-tick block alongside
`self.state = STATE_LIVE` (`mirror.py:2165`) — the counters stay, which is the
durable record the comment there already argues for — and assert
`health()["lastError"] is None` after a recovery in
`test_the_breaker_holds_then_lets_the_mirror_recover`.

## MINOR 2 — `ScrubbedOp` is an exported type whose mere presence disables GD-27's backstop, and `Mapper.__call__` honours it on mapper output, which by definition has never been scrubbed

**Files:** `aggregator/mirror.py:949-950` (`validate_op`), `aggregator/mirror.py:133`
(`__all__`), `aggregator/mirror.py:774` (`Mapper.__call__`).

```python
    if isinstance(item, ScrubbedOp):
        return ScrubbedOp(collection, key, update)      # never walked
```

The type-as-flag is the right call for `_requeue` (attempt-3 MINOR 2, closed
well). But `ScrubbedOp` is in `__all__`, and `Mapper.__call__` runs
`validate_op(item, …, scrub=False)` over whatever a mapper returned — so a
sp-07…sp-11 mapper that imports the exported class and yields
`ScrubbedOp(...)` triples (a natural thing to do when a module already imports
`MirrorOp` from here) silently opts its own payloads out of the only backstop
GD-27 has. Nothing validates the claim the type makes.

**Fix:** make the marker unforgeable at the boundary that can never legitimately
carry it — in `Mapper.__call__`, downgrade: `op = validate_op(item, …, scrub=False)`
then `if isinstance(op, ScrubbedOp): op = MirrorOp(*op)`. Mapper output has by
definition not been scrubbed. And drop `"ScrubbedOp"` from `__all__`
(`mirror.py:133`) — it is an internal transport marker, not part of the mapper
contract; `MirrorOp` is what SD-1 needs. Assert both in
`test_one_kind_has_one_owner_and_mapper_output_is_validated`
(`tests/test_mirror.py:1355`) with a mapper that tries the forge.

## MINOR 3 — `rebuild()` raises out of `main()` on a transient outage, against the module's "every failure is a state" posture

**File:** `aggregator/mirror.py:2363` (`drop_collection`), `aggregator/mirror.py:2374-2375`
(`counts()` / `fingerprint()`).

Three unguarded awaits on the driver in the one method an operator invokes from a
shell. Executed:

```
rebuild RAISED: MongoUnavailable derived: drop failed, server went away
rebuild(2) RAISED: MongoUnavailable read failed
```

`main()` (`mirror.py:2654`) calls it inside `asyncio.run`, so the operator gets a
traceback where the docstring promises *"the difference between an operator who
can see what happened and a traceback out of `asyncio.run`"* (`mirror.py:2344-2346`)
and `docs/mongo.md:267-277` promises a report on `/health`. The docstring's
"never raises" is scoped to *mapping* failures, so this is a gap rather than a
contradiction — but `--rebuild` is exactly the command run against a database
someone is fiddling with.

**Fix:** wrap the three calls: a failed `drop_collection` ⇒ `_record_failure` +
`droppedDerived:False` + return the report without replaying (dropping is the
precondition); a failed `counts`/`fingerprint` ⇒ `None` in the report plus a
`lastError`. Add an arm to `test_wipe_and_rebuild_produce_the_same_fingerprint`
(`tests/test_mirror.py:1200`) with a backend whose `drop_collection` raises.

## MINOR 4 — `backfill`'s item unpack and `enqueue`'s arg coercion both accept shapes they then mishandle

**Files:** `aggregator/mirror.py:2418`, `aggregator/mirror.py:1872`.

```python
            kind, observation, source = (item if len(item) == 3 else (*item, None))
```

`len(item)` requires a sized object (a generator from a streaming source raises
`TypeError`), and a 4-tuple raises `ValueError: too many values to unpack`
(executed) — out of a method whose whole design note is that a backfill of a
large corpus must not die on one item. Separately, `enqueue`
(`mirror.py:1872`) coerces any non-list/tuple to `[ops]`, so
`enqueue(op for op in …)` queues the *generator object* as one operation
(executed: `accepted=1`); it is later counted as `rejected` rather than lost, but
the accepted-count return value lies.

**Fix:** unpack defensively — `parts = tuple(item); kind, observation = parts[0], parts[1];
source = parts[2] if len(parts) > 2 else None`, and refuse a longer tuple as a
counted `rejected` with a message naming the item, not an exception. In `enqueue`,
coerce with `ops = list(ops) if isinstance(ops, (list, tuple, set, frozenset)) or
hasattr(ops, "__iter__") and not isinstance(ops, MirrorOp) else [ops]` — or more
simply, refuse a non-`MirrorOp` non-sequence explicitly.

## NIT 1 — `sweep(reinsert=())` permits the one legal delete with no re-insert

`aggregator/mirror.py:2297-2308`: the `stream_meta` `delete_many` runs
unconditionally, and `reinsert` defaults to `()`. GD-26 and the docstring frame
the delete and its re-insert as *one code path* (`mirror.py:2280-2283`), which is
what makes the exception defensible. A caller that forgets `reinsert=` gets a
bare scoped delete that the code presents as the legal one. A file that genuinely
shrank has nothing to re-insert, so this is a judgement call rather than a bug —
but it deserves either a keyword-only `allow_empty_reinsert=False` or one comment
saying the empty case is intentional.

## NIT 2 — a non-`MapperError` raised inside `_take_batches` loses the operations it already dequeued

`aggregator/mirror.py:1979-1987` catches `MapperError` only. `scrub_op_update`
walks user-controlled depth, so a pathologically nested payload raises
`RecursionError`, which escapes to `tick`'s blanket guard (`mirror.py:2047`) —
after the operations have left the queue and before they entered a batch. They
are recorded as a *failure*, not as `rejected` or `dropped`, so GD-26's "never
dropped quietly" is technically breached in a corner nobody will reach. Widening
that `except` to `Exception` and counting it as `rejected` costs one word.

---

## What I checked and found correct (so the next attempt does not churn it)

- **GD-21.** `import pymongo` appears only inside function bodies
  (`AsyncBackend.connect/bulk_upsert/guarded_update/update_many/delete_many/drop_collection`),
  never at module scope; options come from `ms.client_options()`. I re-ran both
  owned suites with a shadow `pymongo` module raising `ImportError`: rc=0 both,
  skipping cleanly. `Mirror` resolves absence to `STATE_ABSENT` with a truthful
  `lastError`, and `enqueue` books it under `skipped_absent`, not `dropped`.
- **GD-22 / GD-30.** `enqueue` contains no `await`, cannot raise, and
  drops+counts+degrades on a full queue; the breaker demonstrably stops the tick
  before it touches the driver (asserted on `backend.calls`, not on timing);
  `Mapper.__call__` passes `scrub=False`, so the 8.79 ms walk is off the poll
  loop, and the scrub-count test (`tests/test_mirror.py:910`) proves 0 walks on
  the loop side and exactly 8 on the drainer side for 8 operations.
- **GD-24 / GD-25.** Every `_id` goes through `refs.ref_key`; `validate_op` runs
  `spec_for`/`check_id`/`validate_update` at the registry boundary; no `$inc`
  anywhere; `_take_batches` deliberately does not collapse two updates to one
  `_id`; `save_cursor`'s `$set` is argued and correct (a shrink must rewind).
- **GD-26.** No delete verb is *called* outside the two guarded doors; both
  backends refuse every collection but `stream_meta` for `delete_many` and every
  name but `derived` for `drop_collection`, in their own bodies;
  `_assert_scoped` refuses `{}` and gen-only filters; no `expireAfterSeconds`,
  no `$unset`. The live arm proves the *server* refuses `delete` on `records`
  under the documented role, and that the documented role cannot drop `records`.
- **GD-27 (except MAJOR 2).** 0600/0400 accepted, every group/other/exec bit
  refused, symlink refused, `save_credentials` uses `O_EXCL|0600` at open time;
  `database_name` is `touch_<sha1(realpath)[:8]>` fenced to `touch_` *with* the
  underscore; no connection-string literal or bare `27017` under `aggregator/`;
  `/health` carries no URI and no password in either spelling; `holderBoot` is a
  digest, not the raw `boot_id`; `_MIN_LITERAL_SECRET` is now documented
  (`mirror.py:361-370`) with the structural-pass argument, and NIT 1 of attempt 3
  is properly closed; the deny-list is asked before the extension filter and is
  load-bearing in the test.
- **GD-28 / GD-29 (except MAJOR 1).** No `provenance:"harness"` is emitted here;
  the lease is a real conditional write proven against a real mongod; tolerated
  dups are counted, not swallowed; the TTL re-take is implemented, capped at once
  per TTL, and correctly restricted to `_lease_lost`.
- **`refused_no_lease` / `refused_policy`** (attempt-3 MINOR 3) is split
  correctly (`mirror.py:1880-1885`), published, and the `docs/mongo.md:47-58`
  list is asserted equal to `health()["counters"]` in both directions — the
  doc-drift hole is genuinely closed.
- **`discover_mappers`** (attempt-3 NIT 2) now compares the fully-qualified name
  (`mirror.py:805`), in both `discover_mappers` and `iter_sources`.
- **R-42 / R-57 docs.** The `docker run` recipe is parsed out of the page and
  re-run with only identifiers changed; `0.0.0.0` and bare `-p 27017:27017`
  appear only inside prohibition paragraphs; all four measured growth numbers,
  the no-TTL law, "Mongo down is a non-event", `--rebuild`/`--backfill`, and
  "never publish 27017" are present.
- **Ownership.** Four files, nothing else; no commit; `tests/run_all.sh` globs,
  so both suites are in the full run without editing a file this sub-plan does
  not own.

---

## Verdict fields

- `approved`: **false**
- `depth`: **in-scope** — MAJOR 1 is one condition plus a flag in `_tick`,
  MAJOR 2 is one identifier in `_scrub_ref` (`classify` → `validate_ref`) plus
  four prose corrections, MAJOR 3 is one `_record_success()` call and moving one
  early return. No sub-plan boundary is crossed and no new research is needed.
- `critical_defect`: **false** — nothing here corrupts the store or invalidates
  the remaining sub-plans' design. MAJOR 2 does carry a forward edge worth
  naming to the next implementer for the second attempt running: sp-07…sp-11 are
  being told, in three code comments and on a documentation page, that `ref` is
  a safe subtree. Fixing that claim before those mappers are written is cheaper
  than after, and cheaper still than an upsert-only store that already holds the
  credential.
