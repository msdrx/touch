# sp-refs-mongostore — adversarial critique, attempt 5

**Verdict: REJECTED** — 0 blocker, 1 major, 3 minor, 3 nit.
depth: in-scope. critical_defect: false.

Files reviewed (full content; all four are new in an untracked tree, so there is
no diff to read):

- `/home/laniakea/Projects/touch/aggregator/refs.py` (972 lines)
- `/home/laniakea/Projects/touch/aggregator/mongo_store.py` (2040 lines)
- `/home/laniakea/Projects/touch/tests/test_refs.py` (578 lines)
- `/home/laniakea/Projects/touch/tests/test_mongo_store.py` (1997 lines)

Against: `plan/touch-mongo-live-subplans.md` §"sp-05 — refs-mongostore",
amendment items **R-43** and **R-44** plus **GD-21…GD-30**, base plan GD-1…GD-20,
and SD-1 / SD-2 / SD-11.

---

## What I re-ran rather than trusted

- `python3 tests/test_refs.py` → rc=0 (all checks pass).
- `python3 tests/test_mongo_store.py` → rc=0, 360 `ok:` lines, live arm skipping
  cleanly on `TOUCH_MONGO_URI` absent. The gate's three-arm claim is consistent
  with what I see here for the two ambient arms.
- Ownership: `ls -l` on `aggregator/*.py` + the two test files. Only
  `refs.py` (07-27 07:54), `mongo_store.py` (07:58), `test_refs.py` (08:04) and
  `test_mongo_store.py` (08:11) carry mtimes in this attempt's window; every
  other `aggregator/` file is 07-25/07-26. `git rev-parse HEAD` is still
  `579446e`; nothing committed.
- Attempt-4's MAJOR **M1** (secondary-unique-index dup counted as tolerated) and
  its m1/m2/m3/n1/n2/n3 are all really addressed in the tree
  (`split_write_errors`, `AsyncClientError`, `_positional_component`,
  `sessions.sourceState`, `_numeric_normal`, the guard probe, the four added
  pins). I did not re-raise any of them.
- Four behaviours I asserted by running the module rather than by reading it:
  `prepare_document` on its own output; `store.classify_ref(refs.canonical_ref(x))`;
  `merge_ops` with an identical repeated operator; and the two probes in M1 below.

The one major is exactly the kind a green run cannot show, because the arm that
would show it is the arm that skips by default.

---

## MAJOR

### M1 — the memory model is GD-25's oracle and it accepts documents mongod refuses: neither `apply_operations` nor `bulk_upsert` ever checks a required field or a bsonType pin

`aggregator/mongo_store.py:1150` (`validate_update`), `:1368-1379`
(`apply_operations`), `:1762-1772` (`bulk_upsert`'s guard block);
`tests/test_mongo_store.py:1417-1436` (the acceptance assertions).

`validate_document` is the only place in this module that knows about
`spec.required`, `spec.provenance` and `spec.types`. **Nothing on the update path
calls it.** `bulk_upsert` runs `spec_for` + `check_id` + `validate_update`, and
`apply_operations` runs `check_id` + `apply_update`; both then hand the result
to, respectively, the wire and `fingerprint`/`counts`.

Two probes, run against this tree:

```python
k  = refs.record_key("081b28a7-aee9-43dc-935d-1586407f232e")
op = ms.merge_ops(ms.op_set_on_insert({"sessionId": "s", "type": "user"}))  # no provenance
st = ms.apply_operations({}, [("records", k, op)])
ms.counts(st)          # {'records': 1}      <- counted
ms.fingerprint(st)     # 'a84bef8f8a…'       <- fingerprinted
ms.validate_document("records", st["records"][k])
#   SchemaError: records: missing required field(s) ['provenance']
ms.bulk_upsert(NoDb(), "records", [(k, op)])   # reaches the driver: no client refusal
```

```python
bad = ms.merge_ops(ms.op_max({"out": "260"}),                       # a STRING
                   ms.op_set_on_insert({"sessionId": "s", "provenance": "harness"}))
ms.validate_update(bad, "usage", _id=refs.usage_key("msg_1"))       # accepted
#   -> apply_operations stores {'out': '260'} and fingerprints it
#   -> validate_document("usage", …) would have said:
#      usage.out must be bsonType ['int','long'], got str (GD-24 type pin)
```

Why this is a major and not a nit, in the module's own words:

- This file states its defect criterion three separate times — `_set_path`
  (`:1273-1283`), `_positional_component` (`:1125-1142`) and `_numeric_normal`
  (`:1419-1435`) each refuse something mongod refuses, with the argument that *"a
  model more permissive than the server certifies a fingerprint no mongod can
  reproduce"*. Required fields and bsonType pins are the two largest instances of
  exactly that, and they are the two the model does not apply. GD-25's acceptance
  test can therefore return a stable fingerprint and equal counts over a state
  that contains documents the server will reject one at a time.
- `json_schema` (`:644-648`) makes the mirror-image argument in the other
  direction — *"required is projected, not merely declared: a rule the client
  checks and the server does not is a rule that holds only for writers that went
  through this module"*. Here it is a rule the **server** checks and the client
  does not, which is the same split pointed at the test suite instead of at the
  database.
- The consequence lands downstream, not here. sp-07…sp-11 write the `MIRROR_MAPPERS`
  (SD-1) against this vocabulary, and their suites are pure-arm: R-44's own
  acceptance criterion is *"All Mongo tests skip cleanly without a reachable
  mongod"*, and this file's live arm does skip (verified above — it skipped in my
  run). A mapper that emits `procStart` as an int, `seq` as a string, or forgets
  `provenance` on one branch ships **green** through every default run; on a real
  mirror each such write comes back as a fatal code-121 item in `bulk_upsert`'s
  `errors[]`, i.e. those entities are silently absent from the mirror and the
  reason is visible only in `/health`.
- `aggregator/mongo_store.py` has exactly one owner and this is its last gated
  attempt (GD-15). Every later sub-plan would have to work around the gap in a
  file it may not touch.

**Fix (bounded, and it does not break the current corpus):**

1. In `validate_update`, when `collection` is known, apply `_type_ok` to the
   value of each field under `$set` / `$setOnInsert` / `$max` / `$min` whose name
   matches a `spec.types` key **exactly** (no dotted paths — `sourceState.<k>.present`
   must stay legal under the `object` pin on `sourceState`), and skip `$addToSet`
   entirely (its value is an element, not the array the pin describes). This is
   safe for partial/payload-only updates because it only inspects the values that
   are actually present.
2. Add a `validate_state(state)` helper (a loop of `validate_document` over
   `{collection: {_id: doc}}`) rather than making `apply_operations` strict —
   a payload-only `guarded_update` legitimately builds an incomplete document in
   memory, so strictness belongs at the end of a full ingest, not per op.
3. Call it from `test_gd25_acceptance_normal_shuffled_reversed` on the `normal`
   state before fingerprinting, and add the negative arm (drop `provenance` from
   one mapper branch ⇒ the acceptance test fails), so the assertion is not
   vacuous.

I checked that (1) will not break the acceptance mapper today: all 38 `started`
journal lines in the frozen corpus carry an `agentId`, so
`op_set({"agentId": entry.get("agentId")})` never emits `None`.

---

## MINOR

### m1 — `prepare_document` is not idempotent: it double-wraps an already-wrapped subtree

`aggregator/mongo_store.py:669` (`wrap_raw`), `:759-777` (`_walk_wrap`), `:780-796`.

```python
p1, _ = ms.prepare_document("records", {"toolUseResult": {"a.b": 1}})
p2, _ = ms.prepare_document("records", p1)
p2["toolUseResult"]["_raw"]
#   '{"_raw":"{\\"a.b\\":1}","_rawEncoding":"json","_rawKeys":1}'
```

A declared `raw_path` is wrapped *unconditionally* (that is the right rule for
shape stability), which means a wrapper is wrapped too. The reachable shape is
two preparers on one document: the test's own `mapper_ops`
(`tests/test_mongo_store.py:1307`) calls `prepare_document` and hands the result
into `op_set_on_insert`, and `mirror.py` (R-45, sp-06) is the natural second
caller for anything reaching the wire. The data is not lost, but the stored shape
differs from the first pass's, so a document written through one path and
re-written through the other is not the same document — in a module whose entire
premise is that replay is a no-op.

**Fix:** in `_walk_wrap`, short-circuit when `is_raw_wrapper(value)` (both for
declared paths and for the autowrap branch) and count it in the report as
`already`. `is_raw_wrapper` is already deliberately narrow (`set(value) <= RAW_FIELDS`),
so the "a harness subtree happens to look exactly like a wrapper" objection is
the same objection that door already answers.

### m2 — a ref that *declares* an unrecognised `kind` raises instead of being retained, which is GD-11's open tail closed at the one place data arrives from outside Touch

`aggregator/refs.py:764-770` (`classify`), `:780-801` (`validate_ref`),
`:804-826` (`canonical_ref`).

```python
refs.canonical_ref({"kind": "spaceship", "id": "x"})
#   UnknownRefError: unknown ref kind: 'spaceship'
```

GD-24 says *"Unknown ref shapes: retained under `ref` with `kind:"unknown"`, no
`refId`, excluded from joins (GD-11 open tail preserved)"*, and this module's own
docstring (`:71-73`) says *"an unrecognised shape is retained with
`kind:"unknown"` and no `refId` (never an error, never a join)"*. That holds for
an unrecognised **key set** and not for an unrecognised **declared kind** — and
the declared-kind case is precisely the one that arrives from outside this
codebase: R-52/R-53 custom-state and control-intent records are agent-authored
JSON, and `store.py` accepts such a `ref` on the file side without complaint
(see m3). So a line the file store durably accepted raises on the way into the
mirror. Worse, `RefError` is a `ValueError` and **not** a `MongoStoreError`, so it
escapes a drainer written as `except MongoStoreError:` — the failure mode
`mongo_store.py` spent three docstrings eliminating on its own side.

**Fix:** keep the hard raise in `ref_key` (there is no best-effort `_id`), but let
`classify`/`canonical_ref` retain an unrecognised declared kind as
`kind:"unknown"` with the remaining keys sorted, exactly as they do for an
unrecognised key set. If the typo-catching value of the current raise is worth
keeping, expose it as an explicit `classify(ref, strict=True)` used by the
ergonomic constructors only, and say in the docstring which callers use which.

### m3 — the "two copies of the union cannot drift" proof does not cover the one shape `refs.py` actually emits

`tests/test_refs.py:493-509` (and its claim at `:20-21`), against
`aggregator/refs.py:804-826` / `aggregator/store.py:231-248`.

`store.classify_ref` matches on `set(ref)` with no notion of a `kind` key, so:

```python
c = refs.canonical_ref({"uuid": "081b28a7-…"})   # {'kind': 'uuid', 'uuid': '081b28a7-…'}
refs.classify(c)          # 'uuid'
store.classify_ref(c)     # 'unknown'
store.validate_ref({"kind": "uuid", "uuid": "NOT-A-UUID"})   # 'unknown'  (no rejection)
```

The union-agreement test only ever passes *bare* `SAMPLES` dicts through both
modules, so it proves the two tables agree on a shape neither module stores. The
canonical form — the one GD-24 puts on the document as `ref{kind,…}` and the one
the `events` collection mirrors out of the `.touch/` WAL — is unclassifiable
file-side, which also means any ref carrying an explicit `kind` skips GD-11's
hard-rejection half on the file side entirely.

`store.py` belongs to sp-04 and must not be edited here. What *is* in scope is
the test's claim: assert the canonical form both ways and pin the asymmetry as a
named, tested fact (`store.classify_ref(canonical_ref(x)) == "unknown"`, with a
comment naming the file-side fix as sp-04's), so the next reader of
`test_refs.py:20-21` is not told a drift is impossible when it is present.

---

## NIT

### n1 — `open_client` lets driver exceptions escape the hierarchy on a bad URI

`aggregator/mongo_store.py:1537-1552`. `MongoClient(uri, …)` raises `InvalidURI`,
`ConfigurationError` or a bare `ValueError` for a malformed URI, and only the
absent-pymongo case is translated. The realistic input is a hand-edited
`.touch/mongo.json` written from R-42's recipe. Verified that none of pymongo's
messages echo the password (checked four malformed URIs including an unescaped
`@` and `/` in the password), so this is not a GD-27 leak — only a hierarchy
hole. **Fix:** wrap the constructor in `except (PyMongoError, ValueError)` →
`MongoUnavailable`, and keep the URI out of the message.

### n2 — `merge_ops`' docstring is stricter than it reads

`aggregator/mongo_store.py:1084-1099`. The prose says a field twice is refused
"under one operator with different values"; the code refuses identical values
under the same operator too (`merge_ops(op_max({"lastTs":1}), op_max({"lastTs":1}))`
raises). The behaviour is defensible; the sentence is not. Align one to the other.

### n3 — an all-digit path component is refused even where no array exists

`aggregator/mongo_store.py:1125-1147`. `_positional_component` scans every
component, so `sourceState.123.present` is rejected — and `refs.escape_field_key`
does not escape digits, so a source path whose escaped key is all digits is
unwritable through the algebra. Narrow today (every transcript path ends in
`.jsonl` ⇒ `%2E` in the key), but it is a wall in the one sub-document GD-26
added for revisable state. Either restrict the check to fields whose top
component is pinned `array`, or say in the docstring that a keyed sub-document
may not use an all-digit key and have `escape_field_key` escape one.

---

## Checks that came back clean

- **GD-21** — `pymongo` appears only inside function bodies
  (`open_client`, `ensure_schema`, `bulk_upsert`, `guarded_update`,
  `pymongo_available`); `refs.py` imports `re` and nothing else. The
  `NoPymongo` meta-path blocker in `test_mongo_store.py:120-139` tests the
  promise at *call* granularity, including the empty-batch short-circuit's
  return-before-import ordering asserted by AST rather than by grep.
- **GD-22 / GD-30** — no clock, no filesystem, no module-level client, no
  state; `CLIENT_OPTIONS` is stated once and asserted verbatim.
- **GD-24** — all 15 rows present with the plan's `_id` grammars, key fields and
  index sets; `sessions` is a real tagged union; `_id` is pinned to
  `bsonType:"string"` on every row and projected into `$jsonSchema`; `check_id`
  round-trips through `refs.ref_key`; no sub-document `_id` or equality-match
  sub-document key is constructible (`_guard_filter` refuses one by name).
- **GD-25** — `$inc`/`$push`/`$pull`/`$unset`/`$rename`/`$currentDate` refused with
  reasons; `$set` fenced over each row's `accumulable`; the acceptance test runs
  normal/shuffled/reversed/twice with equal fingerprints AND counts against
  independently derived expectations, has a non-vacuous negative arm, and the
  retry journal makes the GD-7 ordinal derivation load-bearing (9 nodes over 6
  keys).
- **GD-26** — no delete verb, no `$unset`, no `expireAfterSeconds` anywhere;
  `index_def` refuses TTL structurally; `ensure_schema` reads the server's own
  indexes back and refuses to boot over a hand-added TTL; the AST guard checks
  string literals too, so a `getattr`-spelled verb is covered.
- **GD-27** — no URI, host, port or credential literal in either module; the live
  arm constructs `touch_test_<pid>` and drops only that name, with the
  `startswith` assertion sited immediately above the wipe loop.
- **GD-28** — `provenance` is appended in `CollectionSpec.__init__` so a new row
  cannot forget it; the two exempt rows (`writers`, `cursors`) each say why in
  their own note; the enums match the amendment's per-collection assignment and
  `store.PROVENANCE` is compared rather than duplicated.
- **GD-29** — three readings of a duplicate key, `identity_dups` kept apart from
  ordinary slot conflicts, `classify_write_errors` preserved as a compatibility
  pair, both readings proven from both write doors (and, per the gate, against a
  real mongod).
- **GD-15 / SD-1 / SD-2 / SD-11** — no file I/O, no `.touch/` access, no imports
  outside the two owned modules; ownership window clean; nothing committed.

## Bottom line

R-43 is in good shape; m2/m3 are its only marks and both are small. R-44 is one
change away: the module has built a careful in-memory oracle and then left the
two coarsest server rules — "these fields must be there" and "this field is an
int" — out of it, on the one path (`bulk_upsert` / `apply_operations`) that every
downstream mapper will use and on the one arm (pure) that every downstream test
will run. Fix M1, fold in m1–m3 and the three nits, and this closes.
