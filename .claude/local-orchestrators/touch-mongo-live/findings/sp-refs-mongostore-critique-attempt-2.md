# sp-refs-mongostore — adversarial critique, attempt 2

**Verdict: REJECTED** — 1 blocker, 2 major, 5 minor, 5 nit.

Reviewed (full content; all four are new in an untracked tree, so there is no
`git diff` — `git status` shows `?? aggregator/`, `?? tests/`):

- `/home/laniakea/Projects/touch/aggregator/refs.py` (897 lines)
- `/home/laniakea/Projects/touch/aggregator/mongo_store.py` (1377 lines)
- `/home/laniakea/Projects/touch/tests/test_refs.py` (440 lines)
- `/home/laniakea/Projects/touch/tests/test_mongo_store.py` (1025 lines)

Against `plan/touch-mongo-live-subplans.md` §`sp-05 — refs-mongostore`;
amendment items **R-43**, **R-44**, **GD-21…GD-30**; base plan **GD-11/GD-15**;
shared decisions **SD-1, SD-2, SD-11**.

Every attempt-1 finding I could re-check is genuinely fixed, and fixed with a
real test rather than a restatement: **B1** (the `sessions` tagged union) is now
a `tuple` of `id_kinds` with both arms round-tripped through
`check_id`/`validate_document`/`apply_operations` and a rejection message that
names both grammars; **M1** (null-vs-absent) is now the *stricter* reading, with
`guard_oversize` omitting rather than nulling and a live-verified comment saying
why; **M3** (`$addToSet` field-order) is modelled on BSON identity, not Python
`==`; **M4** (mandatory provenance) is appended in `CollectionSpec.__init__` so a
future row cannot forget it, and proven server-side. The grammar/inverse pairing
in `refs.py`, the in-memory twin of the upsert algebra, and the non-vacuous
negative arms of the GD-25 acceptance test remain better than the item asked
for.

The blocker is the one the test gate found, reproduced here independently. The
two majors are its shadow: **the suite cannot see the blocker in the environment
it normally runs in**, and **the acceptance test's count half — the half that
exists to catch silent collapse — is not applied to the one collection whose
keying rule is newest.**

No source file was modified and no commit was made during this review.

---

## BLOCKER

### B1 — `bulk_upsert` imports pymongo one line *before* its own empty-batch short-circuit, so the pure-path suite aborts on a bare checkout

`aggregator/mongo_store.py:1351-1358`:

```python
    try:
        from pymongo import UpdateOne              # 1352
        from pymongo.errors import BulkWriteError  # 1353
    except ImportError as exc:
        raise MongoUnavailable(f"pymongo is not installed: {exc}") from None
    requests = [UpdateOne({"_id": key}, update, upsert=True) for key, update in checked]
    if not requests:                               # 1357  <-- one line too late
        return {"matched": 0, "upserted": 0, "modified": 0, "tolerated_dups": 0, "errors": []}
```

Reproduced with a genuinely pymongo-free interpreter
(`python3 -m venv --without-pip`):

```
$ <venv>/bin/python3 tests/test_refs.py        ; echo rc=$?   -> rc=0
$ <venv>/bin/python3 tests/test_mongo_store.py ; echo rc=$?   -> rc=1

  ...
  ok: the algebra is enforced here too ($inc is forbidden — GD-25)
Traceback (most recent call last):
  File "tests/test_mongo_store.py", line 396, in
       test_bulk_upsert_applies_the_same_guards_as_the_memory_pass
    check(ms.bulk_upsert(NoDb(), "records", []) == { ... })
  File "aggregator/mongo_store.py", line 1355, in bulk_upsert
    raise MongoUnavailable(...) from None
aggregator.mongo_store.MongoUnavailable: pymongo is not installed: No module named 'pymongo'
```

This is an **uncaught exception that aborts the whole file**, not one red
assertion: `test_gd25_acceptance_normal_shuffled_reversed`,
`test_the_disjoint_continuations_union`,
`test_dotted_keys_are_raw_wrapped_and_round_trip`,
`test_oversize_becomes_a_stub_never_a_drop`, `test_ts_is_supplied_by_the_aggregator`,
`test_no_delete_verbs_and_no_clock_in_the_module`, `test_client_options_are_gd21s`
and `test_live_mongod_arm` never run at all on a bare checkout — including every
GD-25, GD-26 and GD-28 assertion.

**It contradicts three things at once.** The sub-plan's own acceptance line
("All Mongo tests skip cleanly without a reachable mongod; **every module
imports without pymongo installed**"); GD-21 ("its absence … never blocks a
test"); and `bulk_upsert`'s own docstring at `mongo_store.py:1333-1334` —
"Both are refused *before* pymongo is even imported, so the guard is testable
with nothing third-party installed" — which is true of the two guards and false
of the very next call the new test makes. The assertion string
(`"an empty batch is a no-op, not a connection"`, `tests/test_mongo_store.py:396`)
states the intended contract exactly; the code is two lines away from it.

**Failure scenario beyond the test.** `mirror.py` (R-45) drains a bounded queue
into per-collection batches. A tick where one collection has no pending
operations calls `bulk_upsert(db, coll, [])`. On a deployment without pymongo —
the GD-21 `mirror:"absent"` degrade, which is a *supported* configuration —
that raises `MongoUnavailable` out of the drainer instead of returning the
documented zero-result, for a batch that was never going to touch the network.
GD-21's "absence degrades, never crashes" is exactly what breaks.

**Fix** (the gate's, and I agree with it verbatim): move the short-circuit above
the import and delete the now-dead one.

```python
        checked.append((key, update))
    if not checked:
        return {"matched": 0, "upserted": 0, "modified": 0,
                "tolerated_dups": 0, "errors": []}
    try:
        from pymongo import UpdateOne
        from pymongo.errors import BulkWriteError
    except ImportError as exc:
        raise MongoUnavailable(f"pymongo is not installed: {exc}") from None
    requests = [UpdateOne({"_id": key}, update, upsert=True) for key, update in checked]
    try:
        result = db[collection].bulk_write(requests, ordered=ordered)
```

Do not fix this by weakening the test to `raises(MongoUnavailable, …)`. The
docstring's contract is the correct one and the test is right.

---

## MAJOR

### M1 — nothing in either owned suite runs with pymongo unimportable, so B1's entire failure class is invisible in the environment the suite normally runs in

`tests/test_mongo_store.py:366-403` (the new guard test),
`tests/test_stdlib_only.py:167-192` (the only pymongo-absence arm that exists).

Verified: with the ambient interpreter (pymongo 4.17.0 installed) both owned
suites exit **0** and B1 is completely silent — the import succeeds, `requests`
is `[]`, the short-circuit fires, the assertion passes. The defect is only
observable if the interpreter happens not to have pymongo. That is not a test;
that is a coincidence of the runner's environment.

The existing coverage is at **import granularity** only:
`test_stdlib_only.test_every_module_imports_without_third_party_packages`
subprocess-imports each module and asserts nothing third-party lands in
`sys.modules`, and
`test_mongo_store.test_no_delete_verbs_and_no_clock_in_the_module:821-825`
asserts `pymongo` is in `lazy` and not in `top`. Both stay green with B1 in
place — I confirmed the AST guard passes on the current file. GD-21's guarantee
is not "the module imports"; it is "every pure function below works with nothing
third-party installed" (`mongo_store.py:51-53`), which is a **call**-granularity
claim with no test behind it.

Note this is also why B1 shipped in the first place: it was *introduced* by the
attempt-1 **M2** fix, in the same commit that added the test that documents the
contract it breaks.

**Failure scenario.** Any future edit that hoists a `from pymongo import …`
above a pure branch — `guard_oversize` growing a BSON-size cross-check,
`check_id` reaching for `bson.ObjectId`, `ensure_schema` gaining a pure
dry-run mode — reintroduces exactly this bug, ships green on every developer
machine that has pymongo, and only breaks on the degraded deployment GD-21
exists to protect.

**Fix.** Add a call-granularity arm to `tests/test_mongo_store.py` that blocks
the import rather than relying on the interpreter:

```python
class _NoPymongo:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("pymongo", "bson"):
            raise ImportError(f"blocked for this test: {name}")

def test_the_pure_path_works_with_pymongo_unimportable():
    blocker = _NoPymongo()
    saved = {k: v for k, v in sys.modules.items()
             if k.split(".")[0] in ("pymongo", "bson")}
    for name in saved: del sys.modules[name]
    sys.meta_path.insert(0, blocker)
    try:
        check(ms.bulk_upsert(NoDb(), "records", []) == {...},
              "an empty batch is a no-op with pymongo UNIMPORTABLE, not merely absent")
        check(raises(SchemaError, ms.bulk_upsert, NoDb(), "recordz", [(good, op)]), ...)
        check(raises(OperatorError, ms.bulk_upsert, NoDb(), "usage", [(k, {"$inc": {...}})]), ...)
        check(ms.pymongo_available() is False, "…and pymongo_available() answers False, never raises")
        check(raises(ms.MongoUnavailable, ms.bulk_upsert, NoDb(), "records", [(good, op)]),
              "…while a NON-empty batch degrades to MongoUnavailable, not ImportError")
        # plus one call into each other pure entry point: prepare_document,
        # validate_document, guard_oversize, ts_fields, apply_operations, fingerprint
    finally:
        sys.meta_path.remove(blocker); sys.modules.update(saved)
```

Run it *before* `test_live_mongod_arm` and restore `sys.modules` in a `finally`
so the live arm still works in the same process.

### M2 — GD-25's count half is never applied to `run_nodes`/`runs`, and the retry-topology fixture the test's own docstring credits is not in the corpus; the ordinal derivation is exercised only at ordinal 0

`tests/test_mongo_store.py:407-412` (`transcripts()`),
`:457-497` (`journal_ops`, docstring at `:464`),
`:560-596` (`expected_counts`), `:623-627` (the count assertions).

`journal_ops`'s docstring says:

> `ordinal` is the 0-based count of preceding `started` records with the same
> `key`, in file line order — derived while reading, stored, never a DB counter
> (restart-unsafe: MONGOSCHEMA-18). **The three-key retry fixture is what makes
> this more than a formality.**

That fixture is `tests/fixtures/mirror/wf_455b348c-e17/journal.jsonl` (three
keys with **two** `started` records each). `transcripts()` globs only
`fixtures/run-wf_829e6f58/**/*.jsonl` plus two named specimen files, so the
retry fixture is **not ingested**. Measured:

```
11 paths ingested by the acceptance test
wf_455b348c in corpus?           False
distinct ordinals exercised:     {0}
counts(normal):     {'agents': 7, 'records': 1091, 'run_nodes': 7, 'runs': 1,
                     'stream_meta': 34, 'usage': 328}
expected_counts():  {'records': 1091, 'stream_meta': 34, 'usage': 328, 'agents': 7}
```

Two independent holes, and they compound:

1. Every `(key, type)` pair in the ingested journal occurs exactly once, so
   `started.get(key, 0)` is always `0`. I replaced the whole derivation with a
   constant `ordinal = 0` and re-ran the corpus: **the fingerprint is
   byte-identical.** The GD-25 oracle cannot see the derivation at all.
2. `run_nodes` and `runs` are absent from `expected_counts`, so their counts are
   only ever compared *pass to pass* (`counts(shuffled) == got == …`), never to
   an expectation derived independently from the files. A keying rule that
   collapses N journal nodes into fewer documents collapses them identically on
   every pass — which is precisely MONGOSCHEMA-16's lesson, quoted in
   `counts()`'s own docstring at `mongo_store.py:1192-1199`: *"a fingerprint of
   fewer documents is still a fingerprint"*. The count half was invented for
   this and is not pointed at the collection that needs it.

I verified the fixture would in fact do work if included:

```
correct            run_nodes=9   fingerprint-stable-under-reversal=True
ordinal-always-0   run_nodes=6   fingerprint-stable-under-reversal=False
```

— i.e. 3 of 9 nodes silently merge, and (only because the collapsed docs then
take order-dependent `$set` values) reversal breaks. With the corpus as it
stands, neither signal exists.

**Failure scenario.** sp-08/sp-10 write the real journal mapper against this
test as the reference for GD-7-as-amended. An off-by-one or a dropped ordinal
merges every retried node onto its first attempt's `_id`. The `run_nodes`
documents for attempts 2..N vanish, `run_nodes.resultSeen`/`result` for the last
attempt overwrite the first, and the UI renders one node where the retry loop
ran three — with a green acceptance test the whole way, because the fingerprint
is stable and no count expectation exists.

**Fix**, both halves:

```python
def transcripts():
    paths = sorted((FIX / "run-wf_829e6f58").rglob("*.jsonl"))
    paths += sorted((FIX / "mirror" / "wf_455b348c-e17").rglob("journal.jsonl"))
    paths += [FIX / "mirror" / "records" / "file-history-snapshot-dotted.jsonl",
              FIX / "mirror" / "records" / "queue-operation-user-pair.jsonl"]
    return [p for p in paths if p.exists()]
```

and extend `expected_counts` with a `run_nodes`/`runs` expectation read straight
from the journals, without calling `journal_ops` (the independence rule the
docstring at `:566-570` already states for the other three):

```python
    nodes, runs = 0, set()
    for path in paths:
        if path.name != "journal.jsonl":
            continue
        runs.add(path.parent.name)
        starts, results = collections.Counter(), collections.Counter()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            key = entry.get("key")
            if key is None:
                continue
            (starts if entry.get("type") == "started" else results)[key] += 1
        nodes += sum(max(starts[k], results[k]) for k in set(starts) | set(results))
    return {..., "run_nodes": nodes, "runs": len(runs)}
```

Then assert `{0, 1} <= {u["$setOnInsert"]["ordinal"] for … in journal_ops(retry)}`
so the derivation is exercised above zero, and keep the docstring's claim only
once it is true.

(Note when wiring the retry journal in: `journal_ops` takes `run_id` from
`path.parent.name` — `wf_455b348c-e17` — and `session_of(path, {})` falls
through to `SPECIMEN_SESSION` for it, which is harmless for `runs.sessionIds`
but worth a comment rather than a surprise.)

---

## MINOR

### m1 — `_UUID_RE` accepts uppercase hex, so one record has two canonical `_id`s

`aggregator/refs.py:226-228`, `:258-262`; `_build_uuid` at `:394-395` returns the
value verbatim.

```
refs.record_key("081b28a7-…") != refs.record_key("081B28A7-…")   -> True
mongo_store.check_id("records", <both>)                          -> both ACCEPTED as canonical
```

`_AGENT_ID_RE` (`refs.py:229`) is strictly `[0-9a-f]{17}`, so the module already
takes the position that identity hex is lowercase — `uuid`, `sessionId`,
`parentUuid` and `recordUuid` just do not.

**Failure scenario.** GD-11's ref union member `{uuid}` is a shape *agents*
write into `.touch/` control and custom-state files (R-52's `refId` is a
`ref_key` output of exactly this kind). An agent that spells a UUID uppercase
produces a `refId` that no `records` document will ever carry, so the join is
permanently dangling and nothing reports it — `ref_id_kinds` says the key is
well-formed. If two ingest paths ever disagree on case, it is two `records`
documents for one transcript line, which is the duplicate GD-24 exists to
prevent.

**Fix.** Make `_UUID_RE` `[0-9a-f]` only (matching `_AGENT_ID_RE`) and let
`_uuid` reject the uppercase form with a message that says so — rejecting, not
normalizing, is this module's consistent stance everywhere else. Add to
`test_refs.py`: `raises(RefError, ref_key, {"uuid": UUID.upper()})`.

### m2 — `bulk_upsert` can only express `{_id: key}` filters, so the two *conditional* writes GD-24's own table implies have no guarded helper

`aggregator/mongo_store.py:1356`; rows `custom_state`
(`:417-427`, note *"derived head, seq-guarded `{seq:{$lt:newSeq}}` (R-52)"*) and
`writers` (`:447-453`, *"GD-29 writer lease"*).

`UpdateOne({"_id": key}, …, upsert=True)` is the only write shape this module
offers. R-52's seq-guarded head needs `{_id: k, seq: {$lt: n}}` and GD-29's
lease acquisition needs `{_id: stream, leaseExpiresAt: {$lt: now}}`. Neither is
reachable, so sp-06/sp-11 will hand-roll `update_one` against a raw collection
handle — which is precisely the bypass attempt-1's **M2** established must not
exist (`bulk_upsert`'s docstring at `:1324-1334` argues the case at length).
`custom_state.accumulable = ("seq",)` additionally forbids `$set` on `seq`,
so the obvious hand-rolled form is refused too, and the author will discover
both facts one sub-plan away from the table that caused them — the same seam
failure as attempt-1's B1.

**Fix.** Export the guarded shape from here while the table is the owner, e.g.

```python
def guarded_update(db, collection, key, update, *, require=None):
    """One conditional upsert with the same guards bulk_upsert applies."""
    spec_for(collection); check_id(collection, key); validate_update(update, collection)
    filter_ = dict({"_id": key}, **(require or {}))
    ...
```

and state in the `custom_state` note whether R-52's `seq` advance is `$max`
(idempotent, no filter needed) or `$set` behind the guard — if the latter, drop
`seq` from `accumulable` and say why.

### m3 — `wrap_raw` / `prepare_document` raise a bare `TypeError`, outside the `MongoStoreError` hierarchy every other rejection uses

`aggregator/mongo_store.py:543` (`json.dumps(value, …)` with no `default=`).

```
ms.wrap_raw({"a.b": datetime.now(timezone.utc)})            -> TypeError
ms.prepare_document("records", {..., "x": {"a.b": <dt>}})   -> TypeError
```

`document_size` (`:781-782`) passes `default=_json_default` and therefore never
has this problem; `wrap_raw` does not. Every other refusal in the module is a
`SchemaError`/`OperatorError`, so a drainer written as
`except MongoStoreError: …` misses this one and dies on the tick.

**Fix.** Either pass `default=_json_default` (and teach `unwrap_raw` to reverse
the `"!date:"` tag) or wrap the call:

```python
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError as exc:
        raise SchemaError(f"{RAW_FIELD} subtree is not JSON-encodable: {exc}") from None
```

Low reachability today (every declared `raw_path` is fed from `json.loads`
output), which is why this is minor and not major.

### m4 — two update shapes where the in-memory model silently accepts what mongod refuses

The model *is* the GD-25 oracle (`mongo_store.py:23-30`), so "more permissive
than mongod" is the failure mode the `$addToSet` fix already acknowledged.

1. `_set_path` (`:1038-1047`) creates a sub-document over a scalar:
   `apply_update({"spawn": 5}, {"$max": {"spawn.b": 1}})` → `{"spawn": {"b": 1}}`.
   mongod answers *"Cannot create field 'b' in element {spawn: 5}"*.
2. `validate_update` permits `$setOnInsert: {"_id": <other>}` (`:970-971`
   explicitly allows `_id` under that one operator), and `apply_update` then
   overwrites it with the filter's `_id` at `:1093-1094`. mongod raises
   *"the _id field cannot be changed"* on the upsert.

Both produce a fingerprint the server cannot reproduce, i.e. a green acceptance
test for an ingest that fails in production.

**Fix.** In `_set_path`, raise `OperatorError` when an intermediate exists and
is not a dict. In `validate_update`, require that `$setOnInsert["_id"]` — if
present — is only ever checked against the key by `bulk_upsert`/`apply_operations`
(both already have it) and reject a mismatch there.

### m5 — an unreachable server escapes `bulk_upsert` as a raw pymongo exception, never as `MongoUnavailable`

`aggregator/mongo_store.py:1359-1361`: only `BulkWriteError` is caught.
`AutoReconnect`, `ServerSelectionTimeoutError`, `NetworkTimeout` and
`OperationFailure` propagate untouched, contradicting `MongoUnavailable`'s own
docstring (`:126-127`: *"pymongo is absent **or no mongod answered** — degrade,
never crash"*), which promises the second half this function does not deliver.
`mirror.py` may legally catch pymongo exceptions (GD-21 names it), so this is a
contract smell rather than a break — but the class is declared here and should
mean the same thing at both call sites.

**Fix.** `except PyMongoError as exc: raise MongoUnavailable(str(exc)) from None`
after the `BulkWriteError` clause, and say in the docstring which exceptions a
caller may still see.

---

## NIT

### n1 — `writers` and `cursors` are the only rows with no `provenance` pin

`mongo_store.py:447-461`. Defensible (they are operational, not mirrored
content) but undocumented, and `CollectionSpec.__init__`'s "appended here rather
than repeated in fifteen `required=` tuples so a row added later cannot forget
it" reads as though every row has one. Add one clause to each `note=` saying
these two are aggregator-internal and deliberately exempt from GD-28.

### n2 — the source-text grep in the new guard test is a weak assertion

`tests/test_mongo_store.py:400-403` re-reads `mongo_store.py` and checks
`"check_id(" in body and "spec_for(" in body`. The five behavioural assertions
above it already prove the guards fire; the grep passes for a `check_id(` inside
a comment and fails for a rename. Harmless, but it is the one assertion in the
file that tests text rather than behaviour — and the blocker above lives four
lines from it.

### n3 — `MAX_KEY_BYTES` is never exercised

`refs.py:160`, `:769-771`. `test_component_bounds`
(`tests/test_refs.py:192-199`) covers `MAX_COMPONENT_CHARS` at and over the cap
but nothing covers the whole-key byte cap, which is the one that actually
protects the `_id` index entry (a `slot` key joins four 512-char components).
One assertion: a 4-component `slot` ref at the component cap raises `RefError`
naming the byte cap.

### n4 — `merge_ops` takes no `collection`, so the `$set`-on-accumulable fence never applies there

`mongo_store.py:908-933`. Safe today because `apply_update` and `bulk_upsert`
both call `validate_update(update, collection)` afterwards, but `merge_ops` is
the function mappers call directly and its docstring lists the conflict rule
without mentioning that the accumulable fence is somebody else's. One sentence
in the docstring, or an optional `collection=` passthrough.

### n5 — the live arm's `delete_many`/`drop_database` are correctly scoped but not asserted to be

`tests/test_mongo_store.py:922` (`db[collection].delete_many({})`) and `:877`
(`client.drop_database(name)`). Both operate on `touch_test_<pid>` and `:875`
already asserts the drop target's prefix; the per-collection wipe has only a
comment. GD-26's static guard covers `mongo_store.py` only, so a future edit
that points that loop at a real mirror database has nothing standing in its way.
Assert `db.name.startswith("touch_test_")` immediately before the wipe loop.

---

## Checks that came back clean

- **GD-21** — `refs.py` imports `{__future__, re}` (AST-asserted at
  `test_refs.py:388-408`); `mongo_store.py` module level is
  `{__future__, datetime, hashlib, json, aggregator}` with every `pymongo`
  import inside a function body; `test_stdlib_only.py` still green. The only
  breach is B1, which is a *call*-path breach, not an import one.
- **GD-22 / GD-30** — nothing here is on the poll loop: no module-level client,
  no runtime state, `CLIENT_OPTIONS` pinned to GD-21's 500/500/2000 verbatim and
  asserted; `open_client` is documented sync-only for bootstrap/rebuild/tests.
- **GD-24** — all 15 rows present and faithful; every `_id` grammar, padding
  width and index set matches the table, and I re-derived each row against the
  plan text. `refs.collection_of(kind)`/`COLLECTIONS[…].id_kinds` are asserted
  bidirectionally (`test_mongo_store.py:109-119`), which is what would have
  caught attempt-1's B1.
- **GD-25** — `$inc`/`$push`/`$pull`/`$pop`/`$unset`/`$rename`/`$bit`/`$currentDate`
  each refused with a named reason; the `$set` fence is per-collection and a
  typo'd collection name raises rather than disabling it; deltas are nowhere in
  the module. The order-independence oracle is real and its negative arm
  (inconsistent `$setOnInsert`) genuinely fails — see M2 for what it does *not*
  reach.
- **GD-26** — `index_def` refuses `expireAfterSeconds` structurally,
  `ensure_schema` reads the server's indexes back and refuses a hand-added TTL
  (proven live in the gate's run), and the AST + string-literal guard shows no
  delete verb is callable or spellable in the module.
- **GD-27** — no connection string, host, port or credential appears in
  `aggregator/`; the live arm takes its URI from `TOUCH_MONGO_URI`, constructs
  `touch_test_<pid>` and drops only that name; no skip/error message echoes the
  URI.
- **GD-28** — `provenance` is mandatory wherever declared, projected into the
  server's `required`, enum-pinned per row, `legacy_events` refuses `harness`
  and admits `unknown` (no guessing), `events` keeps the full five.
- **GD-29** — duplicate-key is counted and returned, never swallowed; the module
  holds no client and no lease state.
- **GD-15 / ownership** — only the four owned files carry mtimes in this
  attempt's window (`refs.py`/`test_refs.py` 22:31, `test_mongo_store.py` 22:32,
  `mongo_store.py` 22:33). Sibling `aggregator/` and `tests/` files are older;
  `.claude/` is untouched by this sub-plan; `.gitignore` (15:37), `CLAUDE.md`
  (22:03) and `.temp-develop/` (14:45–16:53) all predate the window. `git log`
  is unchanged at `579446e` — **no commit was made**, correctly.
- **Test honesty** — with pymongo installed and no `TOUCH_MONGO_URI`, both
  suites exit 0 and the live arm skips with a reason string; with a dead URI the
  skip happens inside the 500 ms GD-21 timeouts. `test_refs.py` exits 0 on the
  pymongo-free interpreter too.

## Bottom line

Fix B1 (two lines), then close M1 (the arm that makes B1's class visible where
the suite actually runs) and M2 (point GD-25's count half at `run_nodes`/`runs`
and put the retry fixture the docstring already credits into the corpus). The
five minors are worth taking in the same pass — m1 and m2 are both cheap now and
expensive once sp-07/sp-11 have built on top of them. The substance of R-43/R-44
is otherwise in place and independently verified.
