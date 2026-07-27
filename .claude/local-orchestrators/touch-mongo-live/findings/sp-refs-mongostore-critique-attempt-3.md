# sp-refs-mongostore — adversarial critique, attempt 3

**Verdict: REJECTED** — 0 blocker, 1 major, 1 minor, 3 nit.

Reviewed (full content; all four are new in an untracked tree, so there is no
`git diff` — `git status` shows `?? aggregator/`, `?? tests/`):

- `/home/laniakea/Projects/touch/aggregator/refs.py` (916 lines)
- `/home/laniakea/Projects/touch/aggregator/mongo_store.py` (1564 lines)
- `/home/laniakea/Projects/touch/tests/test_refs.py` (515 lines)
- `/home/laniakea/Projects/touch/tests/test_mongo_store.py` (1449 lines)

Against `plan/touch-mongo-live-subplans.md` §`sp-05 — refs-mongostore`;
amendment items **R-43**, **R-44**, **GD-21…GD-30**; base plan **GD-11/GD-15**;
shared decisions **SD-1, SD-2, SD-11**.

**Every attempt-2 finding is genuinely fixed, and fixed at the level it was
made.** B1's short-circuit is above the import *and* pinned by an AST assertion
comparing `min(returns)` to `min(imports)`, so the ordering cannot silently
regress. M1's call-granularity arm exists (`NoPymongo` on `sys.meta_path`), and
it is a real arm rather than an environment coincidence — it runs on the
pymongo-having interpreter and asserts the whole pure surface, including that
the fingerprint is byte-equal with the driver blocked. M2 is closed on both
halves: the retry journal is in `transcripts()`, `expected_counts` grew an
independent `run_nodes`/`runs` derivation (`max(starts, results)` read straight
from the journals), and the derivation is shown load-bearing by
`len(node_ids) == 9 and len(node_keys) == 6`. m1 (uppercase hex), m2
(`guarded_update`), m3 (`wrap_raw`), m4 (`_set_path` over a scalar,
`$setOnInsert._id` vs the key), m5 (`PyMongoError` → `MongoUnavailable`), n1
(the two exempt rows say so), n2 (grep → AST), n3 (`MAX_KEY_BYTES`), n4
(`merge_ops(collection=)`), n5 (wipe target asserted) are all in.

I re-ran everything rather than trusting the gate: both owned suites rc=0 on the
ambient interpreter, both rc=0 on a genuinely pymongo-free `venv --without-pip`,
and the full eight-file suite rc=0. I also connected the review to the same live
`mongo:7` (`127.0.0.1:27117`, `authSource=admin`, R-42 recipe) the gate used, in
a `touch_test_probe_<pid>` database I constructed and dropped.

The one major is something the gate could not have seen from a green run,
because the suite's live R-52 arm exercises the one call shape that happens to
work. I reproduced it against mongod, twice, on both consumers the module names
by name.

No source file was modified and no commit was made during this review.

---

## MAJOR

### M1 — `guarded_update`'s `acquired:False` contract is false for a *partial* update: a normal lost race raises `MongoUnavailable`, the exact class `mirror.py`'s breaker treats as a dead server

`aggregator/mongo_store.py:1534-1540` (the docstring's promise), `:1553-1559`
(the `except` ladder), `:437-439` and `:465-468` (the two `note=` rows that name
the callers).

The docstring states the contract unconditionally:

> ``acquired`` is the answer a lease caller actually wants: False means the
> document exists and did **not** satisfy ``require``. With ``upsert=True``
> mongod reports exactly that as a duplicate key on `_id` (the filter matched
> nothing, so it tried to insert one) — counted and returned here, never
> raised, because a lost lease race is a normal outcome and GD-29 forbids
> swallowing a duplicate key silently.

That is only true when the update happens to carry **every field the
collection's `$jsonSchema` requires**. When it does not, the guard-miss insert
attempt fails *document validation* (code 121) before it can fail on the
duplicate `_id`, and 121 arrives as an `OperationFailure` — a `PyMongoError` —
so `:1558-1559` converts a healthy lost race into `MongoUnavailable`.

**Reproduced live, on the case `custom_state`'s own `note=` describes verbatim**
("the head's PAYLOAD is written by `guarded_update` behind `{seq:{$lt:newSeq}}`
so a late old event never clobbers a fresher head"). Head established at
`seq: 7` with the full identity, then a late old event writes only the payload:

```
head: {'kind':'annotation','note':'newer','provenance':'asserted',
       'refId':'a2fc883c96ff7b837','seq':7}

ms.guarded_update(db, "custom_state", head, op_set({"note":"older"}),
                  require={"seq": {"$lt": 3}})
  -> RAISED MongoUnavailable: custom_state: guarded update failed:
     Document failed validation, full error: {'index':0,'code':121,
     'errmsg':'Document failed validation', ...}

# the same call, upsert=False:
  -> {'matched':0,'upserted':0,'modified':0,'acquired':False,'tolerated_dups':0}
  head note unchanged: newer
```

Same result for GD-29's other named consumer — a lease **renewal/challenge**
that writes only the expiry (`writers.required` is
`('holderPid','holderBoot','leaseExpiresAt')`, and an equality precondition
contributes only its own field to the constructed insert):

```
ms.guarded_update(db, "writers", key,
                  op_set({"leaseExpiresAt": <new>}), require={"holderPid": 999})
  -> RAISED MongoUnavailable: writers: guarded update failed:
     Document failed validation, ... 'failingDocumentId': 'run:wf_x-1'
  holder still: 1
```

**Why this matters beyond a wrong exception type.** `MongoUnavailable` is the
degrade signal — GD-21's "absence/unreachable degrades, never crashes", and
GD-30's breaker keys on it ("after N consecutive failures, stop attempting
30 s", `/health` `mirror:"degraded"|"down"`). A second aggregator holding the
lease, or a burst of late-arriving custom-state events, is a **normal** steady
state; under this behaviour each one is counted as a driver failure, so a
perfectly healthy mongod trips the breaker and the mirror reports itself down.
That inverts the meaning of both `acquired` and `mirror.state` at once, and it
does so in the sub-plan *below* the one that owns the table — the seam failure
this module's own docstrings (`:1528-1532`, `:1408-1418`) argue at length must
not exist.

**Why the suite cannot see it.** `test_guarded_update_is_the_conditional_write_shape`
(`tests/test_mongo_store.py:574-661`) drives the driver arm through a `Recorder`
stub that never applies a validator, and `_live_checks`' R-52 arm
(`:1381-1386`) sends `merge_ops(op_max({"seq":seq}), op_set({"refId":…,"kind":…,
"provenance":…,"note":…}))` on **both** passes — the full identity every time,
so the guard-miss insert is always a valid document and always comes back as a
duplicate key. The lease arm (`:1361-1366`) does the same. The tests certify a
narrower contract than the docstring states.

**Fix** (pick one, and make the docstring true either way):

1. *Preferred* — make the guard shape mean what it says:

```python
    except DuplicateKeyError:
        return {"matched": 0, "upserted": 0, "modified": 0,
                "acquired": False, "tolerated_dups": 1}
    except OperationFailure as exc:
        # A guard miss under upsert=True becomes an INSERT attempt, and an
        # insert built from a partial update fails the collection's
        # $jsonSchema (121) before it can fail on the duplicate _id. That is
        # still "the document exists and did not satisfy `require`", not an
        # unreachable server — reporting it as MongoUnavailable would trip
        # GD-30's breaker on a normal lost race.
        if exc.code == 121 and require:
            return {"matched": 0, "upserted": 0, "modified": 0,
                    "acquired": False, "tolerated_dups": 0}
        raise MongoUnavailable(f"{collection}: guarded update failed: {exc}") from None
```

2. Or: when `require` is non-empty, issue `update_one(..., upsert=False)` and
   report `acquired = bool(result.matched_count)` — verified above to return
   exactly the documented result with no exception — and let a caller that also
   wants create-if-absent pass `require=None` for the creating call (which is
   what `_live_checks:1357` already does).

Whichever is chosen, extend the live arm with the payload-only shape, since that
is the shape R-52 and GD-29 actually describe:

```python
    late = ms.guarded_update(db, "custom_state", head_key,
                             op_set({"note": "older"}), require={"seq": {"$lt": 3}})
    check(late["acquired"] is False,
          "a PAYLOAD-ONLY late write loses the guard and says so, rather than "
          "reporting the server unreachable and tripping GD-30's breaker")
```

---

## MINOR

### m1 — attempt-2's m3 fix landed on `wrap_raw` only; three other entry points still throw outside the `MongoStoreError` hierarchy

`aggregator/mongo_store.py:804-815` (`document_size`), `:818-842`
(`guard_oversize`, via it), `:1256-1273` (`fingerprint`, via `_json_default` at
`:1207-1212`), `:610-614` (`unwrap_raw`).

`wrap_raw` now wraps `json.dumps`' `TypeError` in a `SchemaError` and argues the
case well (`:561-568`): *"A drainer written as `except MongoStoreError:` — the
whole reason the hierarchy exists — would miss that one and die on the tick."*
The identical argument applies to the four sites above, and they were not
touched. Measured:

```
ms.document_size({"_id":"x","v":{1,2}})              -> TypeError        (MongoStoreError? False)
ms.guard_oversize("records", {"_id":"x","v":b"..."}) -> TypeError        (MongoStoreError? False)
ms.fingerprint({"records":{"x":{"_id":"x","v":{1,2}}}}) -> TypeError     (MongoStoreError? False)
ms.unwrap_raw({"_raw":"{not json","_rawEncoding":"json","_rawKeys":1})
                                                     -> JSONDecodeError (MongoStoreError? False)
```

`unwrap_raw` is the one with real reachability: `--rebuild` (R-45) reads
wrappers back **out of the database**, where a truncated or hand-edited `_raw`
is not a programmer error in this process. `is_raw_wrapper` (`:604-607`) only
proves the field is a `str`, so the decode is unguarded.

**Fix.** One `except (TypeError, ValueError)` funnel at each site, mirroring
`wrap_raw`'s. For `unwrap_raw`:

```python
    try:
        return json.loads(value[RAW_FIELD])
    except ValueError as exc:
        raise SchemaError(f"{RAW_FIELD} wrapper does not decode: {exc}") from None
```

and for `document_size`/`fingerprint`, either raise `SchemaError` from
`_json_default` instead of `TypeError`, or say in each docstring that an
unstorable value escapes as a bare `TypeError` on purpose. Silence is the only
answer that is wrong, because `wrap_raw` now documents the opposite convention
five hundred lines above.

---

## NIT

### n1 — `_session_key` accepts a pid the `pid` pin forbids

`aggregator/refs.py:244` (`_SESSION_KEY_RE = r"^\d+-\d+$"`) vs `:360`
(`"pid": lambda n, v: _integer(n, v, minimum=1)`).

```
refs.session_key(0, "1")            -> RefError: ref.pid must be >= 1, got 0
refs.slot_key("0-1", "r", "n", 1)   -> 'slot:0-1|r|n|001'
```

So a `slots` document can name a `sessionKey` that the `session` grammar can
never emit, and `slots.{sessionKey:1,root:1,name:1,attempt:1}` would index a
join target that cannot exist. Harmless today (nothing constructs pid 0), and
the module's stance everywhere else is to reject rather than tolerate. Tighten
to `^[1-9]\d*-\d+$`, with one assertion in `test_refs.py`.

### n2 — four GD-24 key fields carry no bsonType pin

`aggregator/mongo_store.py:441-451` (`slots.runNode`), `:339-353`
(`agents.spawn`), `:354-365` (`runs.harnessTotals`), `:299-314`
(`sessions.sources`).

GD-24's table names `runNode?` on `slots`, `spawn{recordUuid,toolUseId,fileHint}`
on `agents` and `harnessTotals{}` on `runs`; GD-26 adds `sources[].present`. All
four are stored and none is in its row's `types`, so `additionalProperties`
carries them unpinned. `sources` is the notable one — it is already declared in
`set_fields`/`accumulable` (so the module knows it is an array) and
`apply_update`'s `$addToSet` non-array refusal (`:1153-1160`) is the only thing
standing between it and a scalar. Add `{"runNode": _STR, "spawn": "object",
"harnessTotals": "object", "sources": _ARRAY}` to the respective rows; the
sub-fields stay open, which is the open-tail rule `json_schema` already keeps.

### n3 — `_guard_filter` rejects a plain sub-document equality precondition with a message about comparison operators

`aggregator/mongo_store.py:1499-1505`.

```
ms._guard_filter(key, {"holder": {"pid": 1}})
  -> SchemaError: guard on 'holder': ['pid'] is not a comparison a precondition
     may use — allowed: ['$lt', '$lte', ...]
```

Any dict-valued precondition is read as an operator expression, so an equality
match against a sub-document is unreachable and the rejection blames the wrong
thing. The narrowness is defensible (`GUARD_OPS`' own comment argues `require`
is "a precondition on one document, not a query language"), but the message
should say *"a dict precondition is read as a comparison expression; an equality
match on a sub-document is not supported"* — a reader who wanted equality is
currently told to pick a different operator.

---

## Checks that came back clean

- **GD-21** — `refs.py` imports `{__future__, re}` only (AST-asserted); every
  `pymongo` import in `mongo_store.py` is inside a function body
  (`test_no_delete_verbs_and_no_clock_in_the_module` re-uses the stdlib guard's
  own `imports_of`, so the two cannot rot apart). Verified independently on a
  `python3 -m venv --without-pip` interpreter with no pymongo: `tests/test_refs.py`
  rc=0 and `tests/test_mongo_store.py` rc=0. Attempt-2's B1 class is now covered
  *inside* the suite by `test_the_pure_path_works_with_pymongo_unimportable`,
  which blocks the import on `sys.meta_path` and restores it in a `finally`, so
  the live arm still runs in the same process.
- **GD-22 / GD-30** — no module-level client, no runtime state, no clock (AST
  guard on `now`/`utcnow`/`time`/`monotonic`); `CLIENT_OPTIONS` is GD-21's
  500/500/2000/retryWrites verbatim and an override does not mutate the shared
  dict; `open_client` is documented sync-only for bootstrap/rebuild/tests.
  Nothing here can block a poll loop.
- **GD-24** — all 15 rows present, and I re-derived each `_id` grammar, padding
  width and index set against the plan text row by row. `sessions` is a real
  tagged union with both arms round-tripped through
  `check_id`/`validate_document`/`apply_operations`; `refs.collection_of(kind)`
  ↔ `COLLECTIONS[…].id_kinds` is asserted bidirectionally, so a grammar
  `refs.py` can emit but the table will not accept is impossible. Escaping is
  exact on the hard cases I probed independently: `run:a:b` keeps its first
  colon structural and escapes the second (`run:a%3Ab#000000000001`), `a:b` and
  `a%3Ab` stay distinct, a `custom_state` `_id` over an `events` refId
  (`custom-state#000000000007#note`) splits at the right `#`, and
  `legacyplan:t|p||` keeps fixed arity so an absent stage is never ambiguous.
- **GD-25** — `$inc`/`$push`/`$pull`/`$pop`/`$unset`/`$rename`/`$bit`/
  `$currentDate` each refused with a named reason; the `$set` fence is
  per-collection and a typo'd name raises rather than disabling it; deltas
  appear nowhere. The acceptance test is real and now non-vacuous in both
  halves: the retry journal is in the corpus, `{0,1} <= ordinals` holds, the
  9-nodes-over-6-keys assertion makes a stuck ordinal fail, `expected_counts`
  derives `run_nodes`/`runs` from the journals without calling `journal_ops`,
  and the two negative arms (inconsistent `$setOnInsert`; a dropped keying
  rule) genuinely fail. The in-memory model's BSON type ranking is correct
  (`bool` ranked 8 ahead of `int` 2, dates 9) and `$addToSet` dedupes on
  field-order-sensitive BSON identity.
- **GD-26** — `index_def` refuses `expireAfterSeconds` structurally;
  `ensure_schema` reads the server's indexes back and refuses a hand-added TTL
  (I re-confirmed the whole live path against `mongo:7`); the AST walk plus the
  string-literal scan means no delete verb is callable *or* spellable in the
  module.
- **GD-27** — `grep -rE 'mongodb(\+srv)?://' aggregator/` is empty; no host,
  port or credential anywhere in the module; the live arm takes its URI from
  `TOUCH_MONGO_URI`, constructs `touch_test_<pid>`, asserts the prefix
  immediately above both the per-collection wipe and the drop, and no skip or
  error message echoes the URI.
- **GD-28** — `provenance` is appended in `CollectionSpec.__init__` so a future
  row cannot forget it, projected into the server's `required` (proven
  server-side: mongod refuses a `records` document without one), enum-pinned per
  row, `legacy_events` refuses `harness` and admits `unknown` (no guessing),
  `events` keeps the full five, and the two exempt rows now say why in their
  `note=`.
- **GD-29** — duplicate-key is counted and returned, never swallowed; the module
  holds no client and no lease state. (M1 is about the *other* outcome of the
  same race, not this one.)
- **GD-15 / ownership** — only the four owned files carry mtimes in this
  attempt's window (`test_refs.py` 23:02, `mongo_store.py` 23:05,
  `test_mongo_store.py` 23:07, `refs.py` 22:54). Siblings under `aggregator/`
  and `tests/` are older; `.claude/` is untouched by this sub-plan; `.gitignore`
  (15:37) and `CLAUDE.md` (22:03) predate the window. `git log` is unchanged at
  `579446e` — **no commit was made**, correctly.
- **Regression** — the full suite is green: `test_bootstrap`, `test_fixtures`,
  `test_stdlib_only`, `test_store`, `test_tailer`, `test_ws`, `test_refs`,
  `test_mongo_store` all rc=0.

## Bottom line

R-43 and R-44 are substantively done and the previous round's findings are
closed properly rather than papered over. One thing stands between this and
approval: `guarded_update` promises an outcome it does not deliver for the two
call shapes it was written for, and the live arm never sends those shapes. Fix
M1 (a `code == 121` arm, or `upsert=False` when `require` is set) and add the
payload-only assertion that would have caught it; m1 and the three nits are
cheap to take in the same pass.
