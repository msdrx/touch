# sp-refs-mongostore — adversarial critique, attempt 4

**Verdict: REJECTED** — 0 blocker, 2 major, 3 minor, 3 nit.

Reviewed (full content; all four files are new in an untracked tree, so there is
no `git diff` — `git status` shows `?? aggregator/`, `?? tests/`):

- `/home/laniakea/Projects/touch/aggregator/refs.py` (927 lines)
- `/home/laniakea/Projects/touch/aggregator/mongo_store.py` (1714 lines)
- `/home/laniakea/Projects/touch/tests/test_refs.py` (528 lines)
- `/home/laniakea/Projects/touch/tests/test_mongo_store.py` (1635 lines)

Against `plan/touch-mongo-live-subplans.md` §`sp-05 — refs-mongostore`;
amendment items **R-43**, **R-44**, **GD-21…GD-30**; base plan **GD-11/GD-15**;
shared decisions **SD-1, SD-2, SD-11**.

## What I re-ran rather than trusted

- Both owned suites on the ambient interpreter: `tests/test_refs.py` rc=0
  (230 checks), `tests/test_mongo_store.py` rc=0 (313 checks, live arm skipping
  cleanly).
- Both suites on a genuinely pymongo-free interpreter
  (`python3 -m venv --without-pip`, `import pymongo` → `ModuleNotFoundError`):
  rc=0 and rc=0. GD-21 holds at import *and* call granularity.
- Full regression, all eight files: `test_bootstrap` 65 ok, `test_fixtures` 181,
  `test_stdlib_only` 21, `test_store` 161, `test_tailer` 91, `test_ws` 128,
  `test_refs` 230, `test_mongo_store` 313 — 0 failures anywhere.
- The live arm against the running `mongo:7` (`127.0.0.1:27117`,
  `authSource=admin`, R-42 recipe): rc=0, 339 checks, `touch_test_67708`
  constructed and dropped.
- Four independent probes of my own against the same mongod, in
  `touch_test_probe_<pid>` databases I constructed and dropped (M1, M2, m2, n3
  below are each reproduced output, not reasoning).

**Attempt-3's findings are genuinely closed.** M1 is fixed at the level it was
made: `guarded_update` no longer rides a precondition on an `upsert=True`
`update_one`; it does the conditional update with `upsert=False`, and only
attempts a create when `apply_update(None, update)` alone satisfies
`spec.required`. Both of GD-29's partial shapes now return `acquired:False`
instead of `MongoUnavailable`, and both are asserted **against the real
validator** (`_live_checks:1534-1552` for the lease renewal, `:1568-1575` for
the payload-only head). The `Recorder` stub answers both round trips now, so the
suite can see the second one. m1 is closed at all four sites (`document_size`,
`guard_oversize`, `fingerprint`/`_json_default`, `unwrap_raw`) and pinned by
`test_every_refusal_stays_inside_the_exception_hierarchy`. n1 (`^[1-9]\d*-\d+$`),
n2 (the four missing bsonType pins) and n3 (the guard-message rewrite) are all
in, each with an assertion.

Ownership is clean: only the four owned files carry mtimes in this attempt's
window (`refs.py` 23:27, `test_refs.py` 23:32, `test_mongo_store.py` 23:34,
`mongo_store.py` 23:36); `store.py`/`tailer.py`/`ws.py`/`__init__.py` and the
other suites are older. `git rev-parse HEAD` is still `579446e` — no commit,
correctly. `grep -rE 'mongodb(\+srv)?://' aggregator/` is empty. No source file
was modified during this review.

The two majors below are both things a green run cannot show, because in both
cases the suite exercises the one call shape that happens to work.

---

## MAJOR

### M1 — a duplicate key on a **secondary** unique index is counted as a tolerated dup, so a lost write is reported as success with `errors: []`

`aggregator/mongo_store.py:1442-1458` (`classify_write_errors`), `:1519-1528`
(`bulk_upsert`'s `BulkWriteError` arm), `:1707-1710` (`guarded_update`'s
`DuplicateKeyError` arm), `:1583-1586` (`_guard_lost`).

`classify_write_errors` splits on the code alone:

```python
    if item.get("code") == DUPLICATE_KEY:
        tolerated += 1
```

Every 11000 is therefore "healthy replay or two writers racing one stream"
(GD-29's two cases). But GD-24's table declares **three** unique indexes, and
only one of them is `_id`:

- `events` `{stream:1, seq:1}` unique — a dup here really *is* GD-29's case;
- `slots` `{agentId:1}` **unique sparse** — a dup here is R-53's *conflict*: two
  different slots claiming one agentId. The document is **not written**, and the
  caller is told `tolerated_dups: 1, errors: []`.

Reproduced live, in a database I constructed, against the schema
`ensure_schema` itself installs:

```
first  slot: {'matched': 0, 'upserted': 1, 'modified': 0, 'tolerated_dups': 0, 'errors': []}
second slot (SAME agentId, unique sparse index):
             {'matched': 0, 'upserted': 0, 'modified': 0, 'tolerated_dups': 1, 'errors': []}
docs in slots: 1

raw writeError code: 11000 keyPattern: {'agentId': 1}
errmsg: E11000 duplicate key error collection: …slots index: agentId_1 dup key: { agentId: "a2fc883c96ff7b837" }

guarded_update secondary-unique conflict:
             {'matched': 0, 'upserted': 0, 'modified': 0, 'acquired': False, 'tolerated_dups': 1}
```

The driver hands the module `keyPattern: {'agentId': 1}` and the module throws
it away — `tolerated` is an `int`, so even a caller that wanted to distinguish
cannot: there is no per-item detail on the tolerated side at all, unlike
`errors`.

**Why this is R-44's problem and not sp-13's.** R-44's approach line is
"`writeErrors` of unordered bulks ALWAYS inspected and surfaced", and GD-29 is
explicit that a duplicate key is "counted and returned, never swallowed". Here
it is counted under the wrong meaning and the write it lost is surfaced nowhere.
This file's own test says so out loud —
`test_indexes_and_the_no_ttl_law:258-261`:

> "slots.agentId is unique AND sparse — a DuplicateKeyError there **is the
> conflict signal R-53 renders**, not a crash"

R-53 will be handed `tolerated_dups: 1` and nothing to render. Worse, GD-29's
diagnostic rule ("a nonzero steady state means a second writer or a key bug") is
inverted: the number now also moves on ordinary slot-binding conflicts, which
R-53 calls normal-and-renderable.

**Fix.** Split on the violated index, not on the code. The information is in the
error item on both paths:

```python
def _is_id_dup(item):
    """A dup on `_id` (or on a stream identity index) is GD-29's tolerated case.
    A dup on any OTHER unique index means the document was REJECTED and the
    conflict is data — R-53's slots.agentId binding above all."""
    pattern = item.get("keyPattern") or {}
    return list(pattern) in (["_id"], ["stream", "seq"])


def classify_write_errors(error):
    details = getattr(error, "details", None) or {}
    tolerated, conflicts, fatal = [], [], []
    for item in details.get("writeErrors") or []:
        if item.get("code") != DUPLICATE_KEY:
            fatal.append(item)
        elif _is_id_dup(item):
            tolerated.append(item)
        else:
            conflicts.append(item)          # a write that did NOT happen
    return tolerated, conflicts, fatal
```

…returning `{"tolerated_dups": len(tolerated), "tolerated": tolerated,
"conflicts": conflicts, "errors": fatal}` from `bulk_upsert`, and in
`guarded_update` re-raising (or reporting as a `conflicts` entry) a
`DuplicateKeyError` whose `details["keyPattern"]` is not `_id` — `acquired:False`
there currently means "the guard lost", and "another slot owns this agentId" is
not that.

Add the arm to `_live_checks` next to the existing `(stream, seq)` one, since
the existing one is precisely the index for which the current behaviour is
right:

```python
    dup = ms.bulk_upsert(db, "slots", [(other_slot_key, same_agent_binding)])
    check(dup["conflicts"] and not dup["tolerated_dups"],
          "two slots claiming ONE agentId is R-53's conflict, not a replay duplicate — "
          "the write did not happen and the caller is told which index refused it")
```

### M2 — GD-26's `sources[].present:false` cannot be written through this table at all, and `$addToSet` grows the array by one element per stat pass

`aggregator/mongo_store.py:320-326` (`sessions` row: `sources` in both
`set_fields` and `accumulable`), `:1072-1076` (the `$set` fence), `:1131-1159`
(`_set_path`).

GD-26 is normative and specific:

> **Source disappearance is a field, never a removal:** `sources[].present:false,
> lastSeenTs` **set by a stat pass** — the same three-state derived-archive-label
> vocabulary GD-14 already mandates.

`sources` is declared `accumulable`, so `$set` on it — or on any path under it —
is refused, and `$addToSet` is the only operator left. Three stat passes over
**one** source (measured, in-memory model):

```
sources after 3 stat passes:
    {'path': '/a/b.jsonl', 'present': True,  'lastSeenTs': 'T1'}
    {'path': '/a/b.jsonl', 'present': True,  'lastSeenTs': 'T2'}
    {'path': '/a/b.jsonl', 'present': False, 'lastSeenTs': 'T3'}
```

One source, three elements, and a reader that has to decide which of two
mutually contradictory `present` values is current — with no ordering guarantee,
which is the whole reason `$addToSet` was chosen. It grows without bound at the
stat pass's frequency (GD-30's budget lives one document away).

Every other operator is closed to it:

```
$set  sources.0.present -> REFUSED: OperatorError: $set on accumulable field
                           'sources.0.present' of sessions
$max  sources.0.present -> validate_update ACCEPTS
                           apply_update REFUSES: cannot create field '0' in element
                           'sources': it holds a list, not a sub-document
$set  sources (whole)   -> REFUSED: OperatorError: $set on accumulable field 'sources'
```

…while mongod accepts the write the module refuses:

```
server after $max on sources.0.present: [{'path': '/a/b.jsonl', 'present': True}]   # no-op: False < True
server after $set on sources.0.present: [{'path': '/a/b.jsonl', 'present': False}]  # works
```

So the one operator that does the job on the server is the one this table fences
off, and the fenced-off decision belongs to **this** sub-plan. sp-07 owns
`sessions.py` and not `mongo_store.py` (GD-15), so it cannot fix the row it will
break against — the same "the failure surfaces in the *next* sub-plan rather
than in the one that owns the table" hazard `CollectionSpec`'s own docstring
(`:200-237`) argues against for `id_kind`.

**Fix** (either is cheap; the first keeps `$addToSet` honest):

1. Keep `sources` as an `$addToSet` set of **immutable identities** (path
   strings, or `{path}` only) and give the mutable per-source state its own
   keyed sub-document, which the existing dotted-path machinery already handles
   (it is `agents.spawn`'s shape):

```python
        types={..., "sources": _ARRAY, "sourceState": _OBJ},
        set_fields=("sessionIds", "slugs", "sources"),
        accumulable=("firstTs", "lastTs", "sessionIds", "slugs", "sources"),
        note="… GD-26's sources[].present/lastSeenTs live in sourceState.<escaped path>, "
             "which a stat pass writes with $set/$max; the sources set itself is immutable "
             "identities, because $addToSet cannot revise an element it already holds",
```

2. Or drop `sources` from `accumulable` and state in the row's `note` that the
   stat pass rewrites the whole array with `$set` (idempotent because the pass
   recomputes it from `stat()`, not from the stored value) — weaker, but it is
   at least expressible.

Whichever is chosen, add the assertion that would have caught it:

```python
    state = {}
    for present, ts in ((True, "T1"), (True, "T2"), (False, "T3")):
        apply_operations(state, [("sessions", key, <the stat-pass op>)])
    doc = state["sessions"][key]
    check(<exactly one entry for /a/b.jsonl, present False>,
          "a source that disappeared reads as absent ONCE, not as three contradictory "
          "elements that grow by one per stat pass (GD-26)")
```

---

## MINOR

### m1 — the two write shapes are silently sync-only, so GD-21's mandated `AsyncMongoClient` breaks them **outside** the `MongoStoreError` hierarchy

`aggregator/mongo_store.py:36-41` (the module docstring's promise), `:1516-1518`
(`bulk_write`), `:1674` / `:1685` / `:1706` (`update_one`/`insert_one`).

The module docstring says:

> **The two write shapes** — `bulk_upsert` … and `guarded_update` … Both apply
> the same guards, so **no caller downstream has a reason to reach for a raw
> collection handle** — which is how a mapper would acquire the ability to invent
> a collection or an `_id` after all.

GD-21 fixes the live driver: "**pymongo's async API (`AsyncMongoClient`)** inside
the one asyncio process". Handed one, both shapes fail, and not in the hierarchy
the drainer is documented to catch (`:1485-1490`, `test_every_refusal_…`):

```
bulk_upsert    with AsyncMongoClient RAISED AttributeError: 'coroutine' object has no attribute 'matched_count'
  is MongoStoreError? False
guarded_update with AsyncMongoClient RAISED AttributeError: 'coroutine' object has no attribute 'upserted_id'
  is MongoStoreError? False
```

`open_client` says "**synchronous** … for schema bootstrap, rebuild tooling and
tests" (`:1366-1374`), and that is honest — but nothing says it about the two
functions the docstring points `mirror.py` at, and their own docstrings name
`mirror.py` as the caller (`:1481-1483`). It fails loudly rather than silently,
so it is not a major; it does mean sp-06 either wraps every call in
`asyncio.to_thread` (legal under GD-21 only "if the async API is unusable") or
reaches for the raw handle this docstring says it must not.

**Fix.** State the contract where it is made, and pick one:

```python
    Driver mode: this function is **synchronous** — it is `bulk_write` on a
    sync `Collection`. GD-21 puts the live path on `AsyncMongoClient`, so
    `mirror.py`'s drainer either awaits an async twin (`bulk_upsert_async`,
    identical guards, `await handle.bulk_write(...)`) or calls this one inside
    `asyncio.to_thread` with a sync client. Handing an `AsyncMongoClient` to
    THIS function returns un-awaited coroutines and fails as an AttributeError,
    outside MongoStoreError.
```

An async twin is ~15 lines and keeps the "no raw handle downstream" promise
true; a documented `to_thread` contract keeps it honest. Silence is the only
answer that is wrong.

### m2 — `validate_update` accepts positional array paths that `apply_update` then refuses, so the two halves of one write path disagree

`aggregator/mongo_store.py:1018-1084` vs `:1131-1159`.

`validate_update({"$max": {"sources.0.present": False}}, "sessions")` passes;
`apply_update` on a document whose `sources` is a list raises `OperatorError`
("cannot create field '0' in element 'sources': it holds a list"). mongod accepts
the write (verified above). The direction is the safe one by `_set_path`'s own
rule — the model is *more restrictive* than the server, not more permissive —
but the split means `bulk_upsert` will send to the wire an update that
`apply_operations` cannot replay, so `--rebuild`'s comparison (R-45) and the
GD-25 oracle silently stop covering any mapper that uses a positional path.

**Fix.** Decide once, in `validate_update`: either refuse a path component that
is all digits ("positional array paths are not part of the algebra: the memory
model is the GD-25 oracle and cannot replay one"), or teach `_set_path`/
`_get_path` to index a list. Refusing is the smaller change and matches the
module's stance elsewhere.

### m3 — the `$set` fence has holes on two fields the module's own writers accumulate

`aggregator/mongo_store.py:380-390` (`run_nodes`, `accumulable=("startedAt",
"endedAt")`), `:442-454` (`custom_state`, `accumulable=("seq",)`).

`run_nodes.journalSeq` is written with `$min` by this suite's own mapper
(`tests/test_mongo_store.py:962`) precisely because "the `started` line (always
the earlier one) wins whichever operation happens to insert the document"
(`:943-947`) — that is the definition of an accumulable, and `$set` on it is
accepted today. `custom_state.fromSeq` is the same shape one collection over:
GD-24 gives the head `derived:true, fromSeq`, and a head whose `seq` advances by
`$max` while `fromSeq` is `$set` is write-order dependent in exactly the way the
row's own `note` says it must not be.

**Fix.** `accumulable=("startedAt", "endedAt", "journalSeq")` and
`accumulable=("seq", "fromSeq")`, with one assertion each in
`test_forbidden_operators`:

```python
    check(raises(OperatorError, validate_update, op_set({"journalSeq": 3}), "run_nodes"),
          "journalSeq is a $min — the earlier journal line wins whichever operation "
          "inserts the node, which is what makes the shuffled pass agree")
```

---

## NIT

### n1 — the attempt-3 pin fix stopped three fields short

`aggregator/mongo_store.py:402-416` (`events`), `:431-441`
(`custom_state_events`), `:380-390` (`run_nodes`).

GD-24's table names `ref{}` and `data{}` on `events`, `ref{}` and
`data.custom{}` on `custom_state_events`, and `result` on `run_nodes` — the same
`{}`-shaped key fields that earned `spawn`, `harnessTotals` and `sources` their
pins last round. None of the three carries one, so the container type is
unenforced on the server while its declared `raw_paths` assume it is a
sub-document. Add `{"ref": _OBJ, "data": _OBJ}` to the two event rows and
`{"result": _OBJ}` to `run_nodes` (members stay open, per `json_schema`'s
open-tail rule), and extend the loop at `tests/test_mongo_store.py:197-205`.

### n2 — a normal R-52 late event inflates the number GD-29 reads as "a second writer or a key bug"

`aggregator/mongo_store.py:1707-1710`.

When the guard loses to a head that already exists **and** the update happens to
carry the full identity, the create is attempted and comes back a duplicate key,
counted. That is the shape `_live_checks:1558-1563` sends twice in a loop, so a
steady stream of late custom-state events produces a steady stream of tolerated
dups on a perfectly healthy single writer. The payload-only shape (the one R-52
actually describes) correctly returns `_guard_lost(0)`, so the exposure is
narrow — but the counter's documented meaning is "a burst at startup is healthy,
a nonzero steady state means a second writer or a key bug", and this is neither.
Either skip the create when `update_one` already proved the guard was evaluated
against an existing document (a `find_one({"_id": key}, {"_id": 1})` is one more
round trip and settles it), or say in the docstring that a guard lost to an
existing document counts a dup by design.

### n3 — `$addToSet` dedup distinguishes `1` from `1.0`; mongod does not

`aggregator/mongo_store.py:1267-1288` (`_bson_text` / `_bson_identity`),
`:1198-1211`.

```
server fragments: [{'n': 1}]
memory fragments: [{'n': 1}, {'n': 1.0}]
```

BSON equality compares numbers across int/double; `_bson_text` compares their
JSON spellings. This is the direction `_set_path`'s docstring calls a defect —
"a model MORE permissive than the server it is the oracle for … would certify a
fingerprint no mongod can reproduce" — and it is reachable in principle wherever
a harness JSON value is sometimes `1` and sometimes `1.0` inside a set field
(`fragments`, `sources`). It does not occur on the frozen corpus (the live arm's
fingerprint matches the server byte for byte), which is why this is a nit and not
more. Normalize integral floats in `_bson_identity`, or state the limit in the
docstring.

---

## Checks that came back clean

- **GD-21** — `refs.py` imports `{__future__, re}` only (AST-asserted, plus a
  banned-call and no-clock walk); every `pymongo` import in `mongo_store.py` is
  inside a function body, re-derived through `test_stdlib_only.imports_of` so the
  two guards cannot rot apart. Both suites rc=0 on a `venv --without-pip`
  interpreter, and `test_the_pure_path_works_with_pymongo_unimportable` blocks
  the import on `sys.meta_path` (restored in a `finally`) so the arm runs on a
  pymongo-having machine too. `CLIENT_OPTIONS` is GD-21's
  500/500/2000/retryWrites verbatim and an override does not mutate it.
- **GD-22 / GD-30** — no module-level client, no runtime state, no clock (AST
  guard on `now`/`utcnow`/`time`/`monotonic`); nothing here can block a poll
  loop. (m1 above is about *which* client, not about blocking.)
- **GD-24** — all 15 rows, re-derived row by row against the plan text: `_id`
  grammars, padding widths, index sets. `sessions` is a real tagged union with
  both arms round-tripped through `check_id`/`validate_document`/
  `apply_operations`; `refs.collection_of(kind)` ↔ `COLLECTIONS[…].id_kinds` is
  asserted bidirectionally. Escaping is exact on the hard cases: `run:a:b` keeps
  its first colon structural, `a:b` and `a%3Ab` stay distinct, a `custom_state`
  `_id` over an `events` refId splits at the right `#`, `legacyplan:t|p||` keeps
  fixed arity, and `ref_key == store.cursor_key` byte for byte on five stream
  shapes. The uppercase-hex rejection now covers the whole uuid family.
- **GD-25** — `$inc`/`$push`/`$pull`/`$pop`/`$unset`/`$rename`/`$bit`/
  `$currentDate` each refused with a named reason; a typo'd collection raises
  rather than disabling the fence; deltas appear nowhere. The acceptance test is
  non-vacuous in both halves: the retry journal is in the corpus,
  `{0,1} <= ordinals`, `9 nodes over 6 keys`, `expected_counts` derives
  `run_nodes`/`runs` from the journals without calling `journal_ops`, and the two
  negative arms genuinely fail. BSON type ranking is correct (bool 8 ahead of int
  2, dates 9) and `$addToSet` dedupes on field-order-sensitive identity (modulo
  n3).
- **GD-26** — `index_def` refuses `expireAfterSeconds` structurally;
  `ensure_schema` reads the server's indexes back and refuses a hand-added TTL
  (re-confirmed live); the AST walk plus the string-literal scan means no delete
  verb is callable *or* spellable in the module. (M2 is about the `sources`
  clause of the same GD, not about deletes.)
- **GD-27** — `grep -rE 'mongodb(\+srv)?://' aggregator/` empty; no host, port
  or credential anywhere; the live arm takes its URI from `TOUCH_MONGO_URI`,
  constructs `touch_test_<pid>`, asserts the prefix immediately above both the
  per-collection wipe and the drop, and no skip or error message echoes the URI.
- **GD-28** — `provenance` is appended in `CollectionSpec.__init__` so a new row
  cannot forget it, projected into the server's `required` (proven server-side:
  mongod refuses a `records` document without one), enum-pinned per row,
  `legacy_events` refuses `harness` and admits `unknown`, `events` keeps the full
  five, and the two exempt rows say why in their `note=`.
- **GD-29 (lease half)** — the writer lease is exercised against the real server
  through all four states (acquire-on-create, contended, lapsed takeover,
  partial renewal) and the loser writes nothing. The module holds no client and
  no lease state. (M1 is about the *other* meaning of a duplicate key.)
- **GD-15 / ownership / no-commit** — only the four owned files have mtimes in
  the attempt window; `git rev-parse HEAD` unchanged at `579446e`; no commit.
- **Regression** — all eight suites rc=0, no new failures.

## Bottom line

R-43 is done and I have no finding against `refs.py` beyond the shared oracle
nits. R-44 is close: attempt-3's M1 is properly fixed and proven against a real
validator, and the whole GD-25 acceptance apparatus is the real thing.

Two things still stand between this and approval, and both are cases where the
suite drives the one shape that works. **M1**: every 11000 is called a tolerated
duplicate, so `slots.agentId` — a unique index this table declares and this
test file calls "the conflict signal R-53 renders" — loses its write and reports
success. **M2**: `sources` is fenced as accumulable, which leaves `$addToSet` as
the only writer and makes GD-26's `sources[].present:false` unwritable, with the
array growing one element per stat pass in the meantime; sp-07 will meet that
wall in a file it does not own. Fix those two (each is a small, local change with
one assertion), and take m1–m3 in the same pass — m1 in particular is a
one-paragraph docstring that decides how sp-06 is allowed to call this module.
