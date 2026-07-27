# sp-refs-mongostore — adversarial critique, attempt 1

**Verdict: REJECTED** — 1 blocker, 4 major, 4 minor, 4 nit.

Reviewed (full content; all four are new in an untracked tree):

- `/home/laniakea/Projects/touch/aggregator/refs.py`
- `/home/laniakea/Projects/touch/aggregator/mongo_store.py`
- `/home/laniakea/Projects/touch/tests/test_refs.py`
- `/home/laniakea/Projects/touch/tests/test_mongo_store.py`

Against: `plan/touch-mongo-live-subplans.md` §`sp-05 — refs-mongostore`;
amendment items **R-43**, **R-44** and **GD-21…GD-30**; base plan GD-11/GD-15;
shared decisions SD-1, SD-2, SD-11.

Both owned suites are green as the test gate reported (`test_refs.py` exit 0,
`test_mongo_store.py` exit 0 with a clean skip of the live arm). The findings
below are things the suites do not cover; every one of them was **reproduced**,
and the three server-side claims were verified against the live `mongo:7`
container already running for this sub-plan (`touch-mongo-sp05`, loopback+auth,
R-42 shape) using throwaway databases `touch_critique_probe`/`…probe2` that were
constructed and dropped by name (GD-27/GD-12). No source file was modified; no
commit was made.

The work is genuinely good — the grammar/inverse pairing in `refs.py`, the
in-memory twin of the upsert algebra, the `_raw` scoping decision and the
non-vacuous negative arm of the GD-25 acceptance test are all better than the
item asked for. The blocker is a single missing row-shape in the table, and
three of the four majors are the same root cause seen from different angles:
**the in-memory model is the thing under test, and it is more permissive than
the server in ways the live arm happens not to hit.**

---

## BLOCKER

### B1 — `sessions` is a tagged union with TWO `_id` grammars; only one is storable

`aggregator/mongo_store.py:254-264` (`CollectionSpec.id_kind = "session"`),
consumed by `aggregator/mongo_store.py:617-643` (`check_id`).

GD-24's table, verbatim:

| collection | `_id` |
|---|---|
| `sessions` | `live:<pid>-<procStart>` **\|** `hist:<sessionId>` |

`CollectionSpec.id_kind` is single-valued, so `check_id("sessions", …)` parses
every sessions `_id` through the `session` kind only. `refs.hist_session_key()`
exists, is tested in `test_refs.py`, and `refs.collection_of("histSession")` is
`"sessions"` — but no document carrying that `_id` can pass this module.

Reproduced:

```
$ python3 -c '...'
hist id: hist:292fc08c-923d-4ab4-8ff2-a9572417dbc8 | live id: live:622-10028
  check_id sessions OK:   live:622-10028
  check_id sessions FAIL: hist:292fc08c-923d-4ab4-8ff2-a9572417dbc8
      -> sessions: _id 'hist:…' is not a valid session key:
         key 'hist:…' does not start with 'live:'
apply_operations   on a hist: session doc -> SchemaError
validate_document  on a hist: session doc -> SchemaError
```

**Failure scenario.** sp-07 (`sessions.py`, R-46) maps a historical session —
the arm R-25 discovery produces for every `~/.claude/projects/<slug>/*.jsonl`
transcript with no live process — builds `_id = refs.hist_session_key(sid)` as
R-46 requires, and hands it to `apply_operations` / `validate_document` /
(after fix M2) `bulk_upsert`. Every historical session raises `SchemaError`.
The whole `hist:` half of the session registry is unwritable, and the failure
lands in the *next* sub-plan rather than here, where the table is owned.

Why the suite misses it: `test_the_table_is_gd24s` asserts
`refs.collection_of(spec.id_kind) == name`, which is satisfied by `"session"`
alone; nothing round-trips a `hist:` id through `check_id`. `test_refs.py`
covers the kind in isolation, so the gap is exactly on the seam between the two
owned files.

**Fix.** Make `id_kind` a tuple (`("session", "histSession")` for `sessions`,
single-element elsewhere) and have `check_id` accept a key that is canonical
under **any** declared kind, reporting all of them in the rejection message.
Add to `test_mongo_store.py`: both arms of the union round-trip
`check_id`/`validate_document`, and a bogus `sessions` id is still rejected.

---

## MAJOR

### M1 — `validate_document` treats an explicit `null` as "absent"; `guard_oversize` then emits documents the module's own `$jsonSchema` refuses

`aggregator/mongo_store.py:670` (`if field in doc and doc[field] is not None and not _type_ok(...)`),
`aggregator/mongo_store.py:704-724` (`guard_oversize`, `source_path=None, byte_offset=None`).

`$jsonSchema` `bsonType` applies to a property whenever it is **present**;
`null` has bsonType `"null"` and fails an `["int","long"]` pin. The client-side
validator skips the check for `None`, so the two validators disagree on every
pinned field, and `guard_oversize` manufactures the disagreement by default:
with `byte_offset` omitted the stub carries `byteOffset: None`, and `records`
and `stream_meta` both pin `byteOffset` to `_INT`
(`mongo_store.py:270`, `:283`).

Verified against the live mongod (validator built exactly as
`json_schema("records")` emits it, `validationLevel: strict`,
`validationAction: error`):

```
NULL byteOffset REJECTED  ("Document failed validation")
omitted byteOffset ACCEPTED
```

**Failure scenario.** `mirror.py` (sp-06, R-45) hits an >8 MB record, calls
`guard_oversize("records", doc, source_path=p)` — the exact call shape
`test_oversize_becomes_a_stub_never_a_drop:540` already uses — and pushes the
stub through `bulk_upsert`. The server rejects it with a
DocumentValidationFailure, `classify_write_errors` files it under `fatal`, and
the record is **not stored**. R-44's "oversize ⇒ stub, never silently dropped"
inverts into "oversize ⇒ guaranteed rejection": the guard that exists to make
the record survivable is the thing that makes it unwritable.

The test never catches it because the only stub-producing call in the suite
passes `byte_offset=4096` (`test_mongo_store.py:546`).

**Fix.** Two edits, both cheap:
1. `guard_oversize` — build the stub with `sourcePath`/`byteOffset` omitted
   when they are `None` (`{k: v for k, v in (…) if v is not None}`).
2. `validate_document` — drop the `is not None` escape: a *present* `None` on a
   pinned field must raise, because that is what the server does. (If a nullable
   field is genuinely wanted, pin it as `[…, "null"]` in `types`, explicitly.)
   Add a regression test: `validate_document("records", {…, "byteOffset": None})`
   raises, and `guard_oversize(..., source_path="p")` produces a stub with no
   `byteOffset` key at all.

### M2 — `bulk_upsert` — the only real write path — skips both guards `apply_operations` applies

`aggregator/mongo_store.py:1155-1194`, against `aggregator/mongo_store.py:983-994`.

`apply_operations` calls `check_id(collection, key)` per operation, which also
routes through `spec_for` and therefore enforces GD-24's **closed** table.
`bulk_upsert` does neither: it checks `isinstance(key, str)` and calls
`db[collection]`. Reproduced: `"check_id" in inspect.getsource(ms.bulk_upsert)`
→ `False`.

Worse, the `$set`-on-accumulable fence silently disappears for an unrecognised
collection name, because `validate_update` uses `COLLECTIONS.get(collection)`
(`mongo_store.py:846`) instead of `spec_for`:

```
>>> ms.validate_update({'$set': {'in': 1}}, 'usagez')
{'$set': {'in': 1}}      # accepted — the GD-25 fence is off
```

**Failure scenario.** A mapper with a typo'd collection name (`"usagez"`,
`"run_node"`) or a hand-built `_id` that did not come from `refs.ref_key`
(wrong padding, unescaped `#` in a task name) passes `bulk_upsert` cleanly:
mongod happily creates a brand-new collection with **no validator, no indexes,
no `_id` pin**, and the mirror accumulates a shadow collection nobody queries —
precisely the wrong-target hazard GD-12 exists for. The same operation would
have raised `SchemaError` in the in-memory pass, so the two halves of GD-25's
own oracle enforce different laws, and the strict half is the one that never
touches the database.

**Fix.** In `bulk_upsert`, call `spec_for(collection)` once up front and
`check_id(collection, key)` inside the loop (the cost is one regex parse per
operation, already paid on the in-memory side). Change `validate_update` to use
`spec_for` when `collection` is truthy so an unknown name raises instead of
disabling the fence. Test: `bulk_upsert` refuses a non-canonical `_id` and an
off-table collection **without** needing a live mongod (both raise before the
pymongo import path is reached, or assert with a stub `db`).

### M3 — `$addToSet` in the memory model uses Python equality; Mongo uses field-order-sensitive BSON equality

`aggregator/mongo_store.py:962-969` (`if item not in array`).

The model dedupes `{"a":1,"b":2}` against `{"b":2,"a":1}`; the server does not —
which is GD-24's opening hazard one level down, in the one operator whose whole
job is set semantics. Verified on the live mongod:

```
addToSet subdoc count on server: 2
addToSet subdoc count in model : 1
```

This is not hypothetical for this schema. `sessions.sources` is declared a
`$addToSet` set field *and* accumulable (`mongo_store.py:261-262`), and GD-26
specifies `sources[]` as **sub-documents** (`sources[].present:false,
lastSeenTs` set by a stat pass). So the first mapper to implement GD-26's
source-disappearance rule lands squarely on the divergence.

**Failure scenario.** `fingerprint(apply_operations({}, ops))` certifies
"normal / shuffled / reversed ⇒ one fingerprint", the live arm computes a
different fingerprint from the server's documents, and `--rebuild`'s
equivalence comparison (R-45) reports a spurious mismatch — or, if only the
memory arm runs (the default, no mongod), a real duplication bug ships
undetected. GD-25's oracle is only worth what its agreement with the server is
worth, and this module states that agreement as its own contract
(`mongo_store.py:24-27`).

**Fix.** Compare with the same canonical encoding `fingerprint` already uses:
dedupe on `_canonical_text(item)` **plus** raw key order — i.e. compare
`json.dumps(item, separators=…)` without `sort_keys`, so two dicts differing
only in field order are two elements, exactly as BSON has it. Add the two-order
sub-document case as a test, and (separately) reconsider whether `sources`
should be `$addToSet` at all: a set of mutable sub-documents accumulates one
element per state change, so GD-26's `present:false` flip wants a keyed
`$set`/`$max` on `sources.<escaped-path>.present`, not a set union.

### M4 — `provenance` is pinned but not mandatory on 9 of the 12 collections that declare it

`aggregator/mongo_store.py:253-423` (`required=` tuples) and
`aggregator/mongo_store.py:465` (`"required": ["_id"]`).

GD-11 as amended by the amendment's §2 table: "**(e) mandatory `provenance`
field (GD-28)**". GD-28: "Structural enforcement, all three cheap … `$jsonSchema`
pins `custom_state*` to `{asserted,touch}` and mirror collections to
`{harness,derived}`". The enum pins are correct and well tested. The
*mandatory* half is missing:

```
no-provenance records doc accepted: True
records required: ('sessionId', 'type')
json_schema required: ['_id']
provenance optional in: sessions, records, stream_meta, agents, runs,
                        run_nodes, usage, slots, derived
```

`events`, `legacy_events`, `custom_state_events` and `custom_state` do require
it — so the inconsistency reads as an oversight, not a decision; no comment
defends it. Note also that `spec.required` is **never** projected into
`json_schema`'s `required` list, so *no* declared required field is enforced by
the server for *any* collection — a document with only an `_id` is accepted by
the validator this module installs.

**Failure scenario.** A mapper omits `provenance` on `records`. Nothing rejects
it, client or server. Later, GD-28's reader helper ("takes a provenance filter
with **no default**") returns those documents from **no** filter at all: they
answer neither `{provenance:"harness"}` nor `{provenance:"derived"}`, so
mirrored harness facts silently vanish from every provenance-filtered query and
from the "writer unknown" bucket alike.

**Fix.** Add `"provenance"` to `required` for every collection whose spec
declares a provenance enum, and emit `spec.required` (plus `provenance`) into
`json_schema()["$jsonSchema"]["required"]` so the server enforces it too.
Extend `test_provenance_pins_are_gd28s` with: for every collection with a
provenance enum, a document lacking `provenance` is rejected, and
`json_schema(name)["$jsonSchema"]["required"]` contains it.

---

## MINOR

### m1 — nested-path conflicts are not caught where the docstring promises they are

`aggregator/mongo_store.py:806-829` (`merge_ops`), `:836-879` (`validate_update`).

`merge_ops`' docstring: "the same field twice … is refused **here** rather than
at the server, where it would surface as one failed write in an unordered bulk
of five hundred". The collision check is on exact field names, so `$set: {"spawn": …}`
together with `$max: {"spawn.b": …}` passes. The server does not:

```
model : nested conflict ACCEPTED
server: Updating the path 'spawn.b' would create a conflict at 'spawn'
```

Fix: compare dotted prefixes — reject when one field is a prefix path of
another under a different (or the same) operator. Test the `spawn` /
`spawn.fileHint` pair, which is the realistic instance (GD-24 gives `agents` a
`spawn{recordUuid,toolUseId,fileHint}` sub-document).

### m2 — `is_raw_wrapper` is spoofable, so `_check_keys` can be short-circuited

`aggregator/mongo_store.py:513-515`, used at `:573`.

`_check_keys` returns early for any dict that merely *has* `_raw` and
`_rawEncoding == "json"` — including one that also carries hostile sibling
keys. Reproduced: a `records` document with
`body = {"_raw": "{}", "_rawEncoding": "json", "evil.key": 1}` passes
`validate_document`.

Realistic only for adversarial or unusual harness content, hence minor — but
the wrapper is the module's only sanctioned escape hatch, so it should be
recognised by shape, not by the presence of two keys. Fix: require the wrapper
to have *exactly* the wrapper fields (`_raw`, `_rawEncoding`, `_rawKeys`,
optional `_rawAuto`) before treating it as opaque; anything else is an ordinary
sub-document and gets walked.

### m3 — the `stream_meta` half of `expected_counts` restates the mapper's own keying rule

`tests/test_mongo_store.py:397-421` vs `:337-394`.

`expected_counts` calls the same `session_of()` and `line_number()` helpers as
`mapper_ops` and counts distinct `(session_of(...), line_number(...))` tuples —
which *is* the mapper's key. A bug in either helper cancels on both sides and
the count assertion still passes. The `records` (uuid set) and `usage`
(`message.id` set) halves are genuinely independent; only `stream_meta` is not.

The comment at `:413-415` acknowledges the intent but not the coupling. Fix:
derive the `stream_meta` expectation from a source that does not go through the
mapper's helpers — e.g. total non-blank lines minus lines whose
`uuid`+`type ∈ RECORD_TYPES`, per file, summed, with the two excerpt files'
known line counts hard-coded (they already are, in `LINE_NUMBERS`).

### m4 — `bulk_upsert` never applies `guard_oversize`

`aggregator/mongo_store.py:1155-1194`.

R-44 makes the oversize stub part of the store contract, and this module owns
both halves, but nothing joins them: the guard fires only if a caller remembers
to call it. Combined with M1 the practical result is that oversize handling is
currently unreachable-and-broken rather than merely unwired.

Fix: either document explicitly (in the `bulk_upsert` docstring) that
`guard_oversize` is `mirror.py`'s to call and name it there, or size-check each
update's payload inside `bulk_upsert` and refuse rather than let a 16 MiB BSON
error be the first signal.

---

## NIT

### n1 — `check_id` does not verify the `refId` half of a `custom_state` key

`aggregator/refs.py:482-484`. `_parse_custom_state` splits at the last `#` and
returns the left side as an opaque string, so `check_id("custom_state",
"garbage#note")` passes. Since `refId` is by construction another `ref_key`
output, the grammar could assert it (e.g. any of the id-bearing kinds parses
it), which would make R-52's seq-guarded head keys self-checking.

### n2 — `ensure_schema` never reconciles indexes that already exist on the server

`aggregator/mongo_store.py:1099-1133`. An index created by an older version (or
by hand, including a TTL one) survives every boot: `create_index` is additive.
GD-26's "no TTL index on any Touch collection, ever" is enforced over the
definitions this module emits, and read back only in the live test arm. Cheap
hardening: after `create_index`, scan `list_indexes()` and raise (or report)
on any `expireAfterSeconds`.

### n3 — `ts_fields` leans on conditional-expression precedence inside a call

`aggregator/mongo_store.py:757-759`:
`fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)` parses as
`fromisoformat((text[:-1] + "+00:00") if … else text)`, which is what is meant —
but the reader has to prove that. Parenthesise it.

### n4 — the no-delete-verb guard matches attribute names only

`tests/test_mongo_store.py:576-583`. `{node.func.attr}` catches
`coll.delete_many(...)` but not `getattr(coll, "delete_" + "many")(...)`, and
does not look at string literals. GD-26 calls for a *grep* test; adding a
source-text assertion (`"delete_many" not in source`, mirroring
`test_shell.py`'s genre) costs one line and closes the reflective hole.

---

## Checks that PASSED (recorded so the next attempt does not re-litigate them)

- **GD-21** — `refs.py` imports `{re, __future__}` only (AST-asserted);
  `mongo_store.py` imports `pymongo` inside functions only, verified through
  `test_stdlib_only.imports_of` rather than string matching; both files import
  and every pure function works with pymongo absent (confirmed: this reviewer's
  interpreter has no pymongo, and both suites are green).
- **GD-22 / GD-30** — nothing here opens a client at import, reads a clock, or
  runs a loop; `CLIENT_OPTIONS` is GD-21's four values verbatim and
  `client_options()` does not mutate the shared dict.
- **GD-24 table fidelity** — all 15 collections, and every index set matches the
  table row for row (`sessions`, `records`, `stream_meta`, `agents`, `runs`,
  `run_nodes`, `usage`, `events` incl. the unique `{stream:1,seq:1}`,
  `legacy_events`, `custom_state_events`, `custom_state`, `slots` incl. unique
  sparse `{agentId:1}`, `derived`, `writers`, `cursors`). `_id` grammars match
  GD-24's strings — with the single exception that is B1.
- **GD-24 order-independence** — every kind built in up to 6 insertion orders
  yields one `_id` and one byte-stable `ref{}`; parse/rebuild round-trips;
  `#|:%` in a task name round-trips; padding makes lexicographic order numeric
  and widens rather than truncates; `{stream,seq}` is never guessed between
  `events` and `custom_state_events`; unknown shapes are retained with no
  `refId` and never keyed.
- **SD-11 file/Mongo agreement** — `ref_key` for `event` is byte-identical to
  `store.cursor_key` across five stream shapes and three seqs, both parse back
  to the same `(stream, seq)`, the seven-member GD-11 union matches
  `store.REF_SHAPES` required/optional sets, and the GD-14 legacy-agent
  exemption behaves identically on both sides.
- **GD-25** — `$inc`, `$push`, `$pull`, `$pop`, `$unset`, `$rename`, `$bit`,
  `$currentDate` and any unlegislated operator are refused with a reason;
  `$set` is fenced off accumulables (per-collection); the acceptance test runs
  the frozen corpus normally / shuffled / reversed / twice ⇒ one fingerprint and
  equal counts, and its **negative arm is real** (an inconsistent
  `$setOnInsert` payload does change the fingerprint under reordering; dropping
  a keying rule shows up only in the counts). The live arm additionally proved
  the in-memory model agrees with mongod byte for byte on the corpus, asserted
  IXSCAN on both the `(stream,seq)` and the padded-`_id` range query, and got a
  server refusal for a sub-document `_id`.
- **GD-26** — no delete verb, no `$unset`, no `expireAfterSeconds` anywhere;
  `index_def` refuses TTL *constructively*, not just by test; the `$set`
  allowance is scoped and justified (`gen`, `retracted`).
- **GD-27 / GD-29** — no connection-string literal under `aggregator/`
  (grepped); no credential in source, tests, events or docstrings; the live arm
  reads `TOUCH_MONGO_URI`, uses `touch_test_<pid>` and drops only that
  constructed name; `writeErrors` are always inspected and duplicate-key is
  counted, never swallowed, with the racing-writer case exercised.
- **GD-15 / ownership** — only the four owned files were created; `store.py`,
  `tailer.py`, `ws.py`, `test_stdlib_only.py`, `run_all.sh` and the monitoring
  module are untouched (mtimes all predate this sub-plan's window); no commit
  was made; the working tree was not reverted or stashed.

## Non-gating observation

`CLAUDE.md` gained 8 lines at 22:03 inside this sub-plan's window (a "store
every generated deliverable in the task folder" rule under "Rules that bite").
It is unrelated to Mongo, matches no item in R-43/R-44, and reads as an
out-of-band user/session edit; `CLAUDE.md` is sp-15's file. Recorded, not
charged to this implementer — same conclusion the test gate reached.
