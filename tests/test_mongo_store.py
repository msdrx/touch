#!/usr/bin/env python3
"""Stdlib-only tests for aggregator/mongo_store.py (R-44). Run as
`python3 test_mongo_store.py`; exits non-zero on failure. No pytest, no runner.

R-44's own test list is the spine:

* **the GD-25 acceptance test** — the frozen fixture corpus ingested normally,
  shuffled and reversed ⇒ identical fingerprint AND expected counts (the count
  half is not decoration: MONGOSCHEMA-16's probe lost 142 of 333 uuid-less
  records to a content-hash key while the fingerprint stayed stable);
* `explain()` asserts IXSCAN on the cursor query;
* static no-TTL and no-delete-verb guards;
* the dotted-key fixture stores, round-trips byte-identically, and is rejected
  unwrapped;
* the oversize fixture ⇒ stub.

Two arms, one file. The **pure arm** runs everywhere with nothing third-party
installed — that is GD-21's promise made executable. The **live arm** runs the
same operations against a real mongod when `TOUCH_MONGO_URI` points at one
(R-42's loopback+auth recipe) and **skips cleanly** otherwise; it exists because
an in-memory model of Mongo's upsert algebra that silently disagrees with Mongo
is worse than no model at all. It uses database `touch_test_<pid>` and drops
only the name it constructed (GD-27/GD-12's wrong-target invariant).
"""

import ast
import datetime
import json
import os
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[0]
FIX = HERE / "fixtures"
# The canonical trees are named through `tests/_roots.py`, never by a
# literal under REPO: GD-U1 moves them and this is the single flip point.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))

from aggregator import mongo_store as ms                       # noqa: E402
from aggregator import refs                                    # noqa: E402
from aggregator import store as store_mod                      # noqa: E402
from aggregator.mongo_store import (                           # noqa: E402
    COLLECTIONS,
    OVERSIZE_LIMIT,
    OperatorError,
    SchemaError,
    apply_operations,
    apply_update,
    counts,
    fingerprint,
    guard_oversize,
    json_schema,
    merge_ops,
    op_add_to_set,
    op_max,
    op_min,
    op_set,
    op_set_on_insert,
    prepare_document,
    ts_fields,
    unwrap_raw,
    validate_document,
    validate_update,
)

failures = []
skips = []

RECORD_TYPES = ("user", "assistant", "system", "attachment")   # GD-11(a)/GD-24

#: `bulk_upsert`'s zero result, spelled once. Every exit of that function
#: returns the SAME key set — a caller that reads `conflicts` on the failure
#: path and finds the key missing on the success path would have to guard every
#: access, which is how `errors` gets skipped.
ZERO_WRITE = {"matched": 0, "upserted": 0, "modified": 0, "tolerated_dups": 0,
              "identity_dups": 0, "tolerated": [], "conflicts": [], "errors": []}


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  SKIP: {msg}")
    skips.append(msg)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception as other:                                  # noqa: BLE001
        print(f"    (raised {type(other).__name__}: {other})")
        return False
    return False


class NoDb:
    """A database stub that fails loudly the moment a write path reaches it.

    Every refusal in `bulk_upsert` and `guarded_update` happens before `db` is
    subscripted, which is why they can be tested with no server and no driver at
    all: a guard that only fires when a database is reachable is a guard that
    never fires in CI.
    """

    name = "no-such-database"

    def __getitem__(self, name):
        raise AssertionError(f"the write path reached the database for {name!r}")


class NoPymongo:
    """A `sys.meta_path` finder that makes `pymongo` and `bson` UNIMPORTABLE.

    GD-21 is otherwise tested at *import* granularity only
    (`test_stdlib_only.py` subprocess-imports every module and asserts nothing
    third-party lands in `sys.modules`). The guarantee it actually has to make
    is at *call* granularity — "every pure function below works with nothing
    third-party installed" — and that half is invisible on any machine that
    happens to have pymongo. That is exactly how a `from pymongo import …`
    hoisted above a pure branch ships green everywhere and breaks only the
    `mirror:"absent"` deployment GD-21 exists to protect: it happened here once,
    between an empty-batch short-circuit and the import one line above it.
    """

    BLOCKED = ("pymongo", "bson")

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.BLOCKED:
            raise ImportError(f"blocked by NoPymongo for this test: {name}")
        return None


def same_moment(stored, expected):
    """Compare a Date read back from mongod with the aware one we wrote.

    pymongo decodes BSON Date as a **naive** UTC `datetime` unless a codec
    option says otherwise, while `ts_fields` (GD-11(g)) always produces an aware
    one — and Python calls a naive and an aware datetime unequal rather than
    raising. A bare `==` here would therefore be silently False forever, which
    makes it a *vacuous* assertion, not a failing one.
    """
    if isinstance(stored, datetime.datetime) and stored.tzinfo is None:
        stored = stored.replace(tzinfo=datetime.timezone.utc)
    return stored == expected


def function_def(tree, name):
    """The `ast.FunctionDef` for a top-level function, or None."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


# --- the GD-24 table ------------------------------------------------------
def test_the_table_is_gd24s():
    print("test_the_table_is_gd24s")
    expected = {"sessions", "records", "stream_meta", "agents", "runs", "run_nodes",
                "usage", "events", "legacy_events", "custom_state_events",
                "custom_state", "slots", "derived", "writers", "cursors"}
    check(set(COLLECTIONS) == expected,
          f"the 15 collections of GD-24's table, no more "
          f"(extra: {sorted(set(COLLECTIONS) - expected)}, "
          f"missing: {sorted(expected - set(COLLECTIONS))})")
    check(set(refs.collections()) <= set(COLLECTIONS),
          "every collection refs.py can address exists in the table")
    for name, spec in sorted(COLLECTIONS.items()):
        for kind in spec.id_kinds:
            check(refs.collection_of(kind) == name,
                  f"{name}: _id kind {kind!r} points back at it (a grammar addresses one collection)")
    for kind, ref_spec in sorted(refs.KIND_SPECS.items()):
        if ref_spec.collection:
            check(kind in COLLECTIONS[ref_spec.collection].id_kinds,
                  f"…and refs.{kind} (→ {ref_spec.collection}) is DECLARED there: a grammar "
                  f"refs.py can emit but mongo_store will not accept is an unwritable entity")
    check(COLLECTIONS["derived"].id_kinds == (),
          "derived is reducer-owned: its ids are not refs.py's business (GD-23)")

    schema = json_schema("records")["$jsonSchema"]
    check(schema["properties"]["_id"] == {"bsonType": "string"},
          "_id is pinned to bsonType string — GD-24's opening law, enforced server-side")
    for name in COLLECTIONS:
        props = json_schema(name)["$jsonSchema"]["properties"]
        check(props.get("_id") == {"bsonType": "string"}, f"{name}: same _id pin")
    check(json_schema("sessions")["$jsonSchema"]["properties"]["procStart"]
          == {"bsonType": "string"},
          "procStart is pinned to STRING (the clock-tick value from /proc/<pid>/stat f22)")
    check(json_schema("sessions")["$jsonSchema"]["properties"]["pid"]["bsonType"][0] == "int",
          "…and pid to int (BSON is type-strict — CUSTOMSTATE-4)")

    # The four key fields GD-24's table names that carried no pin. An unpinned
    # field is still stored (`additionalProperties` stays open for GD-11's tail)
    # — it just has no bsonType the SERVER will enforce, which is the half of
    # the guarantee a client-side validator cannot make.
    for name, field, bson_type in (("slots", "runNode", "string"),
                                   ("agents", "spawn", "object"),
                                   ("runs", "harnessTotals", "object"),
                                   ("sessions", "sources", "array"),
                                   # GD-24 spells these `ref{}`, `data{}`,
                                   # `data.custom{}` and `result{}`. Each is a
                                   # container a `raw_paths` declaration already
                                   # assumes is a sub-document — an assumption
                                   # only the server can hold for a writer that
                                   # skipped this module.
                                   ("events", "ref", "object"),
                                   ("events", "data", "object"),
                                   ("custom_state_events", "ref", "object"),
                                   ("custom_state_events", "data", "object"),
                                   ("run_nodes", "result", "object")):
        check(COLLECTIONS[name].types.get(field) == bson_type,
              f"{name}.{field} is pinned to bsonType {bson_type} (GD-24's own table names it)")
        check(json_schema(name)["$jsonSchema"]["properties"].get(field)
              == {"bsonType": bson_type},
              f"…and the pin reaches the server's $jsonSchema, not only this validator")
    check(raises(SchemaError, validate_document, "sessions",
                 {"_id": refs.session_key(622, "10028"), "class": "orchestrator",
                  "provenance": "harness", "sources": "one-path"}),
          "…so a `sources` that arrived as a scalar is refused instead of stored as one: "
          "it is a declared set field, and until now only apply_update's client-side "
          "$addToSet refusal stood between it and a string (GD-26's sources[].present)")
    check(COLLECTIONS["run_nodes"].types.get("result") == "object"
          and "result" in COLLECTIONS["run_nodes"].raw_paths,
          "…and run_nodes.result is pinned to the container `prepare_document` "
          "unconditionally produces for a declared raw path, so the field is never "
          "sometimes an object and sometimes a bare journal string")
    wrapped, _report = prepare_document("run_nodes", {"result": "a plain string"})
    check(ms.is_raw_wrapper(wrapped["result"]),
          "…which is not a claim about the journal: a bare string result IS wrapped, "
          "which is exactly why the object pin is true of what reaches the server")


def test_sessions_id_is_a_tagged_union():
    """GD-24 gives `sessions` TWO `_id` grammars. Both must be storable.

    The historical arm is not an edge case: R-25's discovery produces one
    `hist:<sessionId>` document for every transcript in the cwd slug whose
    process is gone, which on a real machine is most of them. A single-valued
    `id_kind` would have made every one of those documents unwritable — and the
    `SchemaError` would have landed in sp-07, one sub-plan away from the table
    that caused it.
    """
    print("test_sessions_id_is_a_tagged_union")
    live = refs.session_key(622, "10028")
    hist = refs.hist_session_key("292fc08c-923d-4ab4-8ff2-a9572417dbc8")
    check(live == "live:622-10028" and hist.startswith("hist:"),
          f"the two GD-24 grammars: {live} | {hist}")
    check(COLLECTIONS["sessions"].id_kinds == ("session", "histSession"),
          "…and both are declared on the sessions row")
    for key in (live, hist):
        check(ms.check_id("sessions", key) == key, f"check_id accepts {key}")
        doc = {"_id": key, "class": "orchestrator", "provenance": "harness"}
        validate_document("sessions", doc)
        check(True, f"…and a whole document keyed {key} validates")
        check(apply_operations({}, [("sessions", key,
                                     op_add_to_set({"sessionIds": "s"}))]),
              f"…and apply_operations mirrors it")
    check(raises(SchemaError, ms.check_id, "sessions", "hist:not-a-uuid"),
          "a bogus sessions _id is still rejected — the union widens the grammar, "
          "it does not disable it")
    check(raises(SchemaError, ms.check_id, "sessions", "live:622"),
          "…and so is a live: key missing its procStart")
    try:
        ms.check_id("sessions", "garbage")
    except SchemaError as exc:
        check("session:" in str(exc) and "histSession:" in str(exc),
              "…and the rejection reports BOTH grammars, since 'not a session key' "
              "is useless when there are two of them")


def test_a_disappeared_source_is_revisable_state_not_a_growing_array():
    """GD-26: "source disappearance is a field, never a removal".

    The clause names two things a stat pass sets — `present:false` and
    `lastSeenTs` — and *setting* is the operative word. An `$addToSet` array
    cannot revise an element it already holds, so if the mutable state lives
    inside the elements, every pass adds another one: the array grows at the
    stat pass's frequency and a reader meets two elements for one path with
    contradictory `present` values and no ordering guarantee (`$addToSet` has
    none by construction — that is the whole reason it was chosen).

    So the row carries both halves: `sources` is the append-only identity set,
    and `sourceState.<escaped path>` is the revisable state. The decision is
    this sub-plan's because sp-07 owns `sessions.py` and not GD-24's table
    (GD-15) — it would have met the wall in a file it cannot fix.
    """
    print("test_a_disappeared_source_is_revisable_state_not_a_growing_array")
    spec = COLLECTIONS["sessions"]
    check(spec.types.get("sourceState") == "object" and "sourceState" not in spec.accumulable,
          "sourceState is a pinned sub-document and NOT accumulable — a stat pass has to "
          "be able to $set it, which is the operator GD-26's clause names")
    check("sources" in spec.accumulable and "sources" in spec.set_fields,
          "…while `sources` stays the fenced $addToSet set: it is history, and history "
          "is not edited (GD-26 applied to the one array that can express it)")

    key = refs.session_key(622, "10028")
    path = "projects/-home-x/292fc08c-923d-4ab4-8ff2-a9572417dbc8.jsonl"
    field = refs.escape_field_key(path)
    check("." not in field and not field.startswith("$"),
          f"the path becomes a usable BSON key first — every transcript path ends in "
          f"'.jsonl', and a dotted key is not a key with a dot in it but a PATH: {field}")

    state = {}
    for present, seen in ((True, "2026-07-25T03:00:00.000Z"),
                          (True, "2026-07-25T04:00:00.000Z"),
                          (False, "2026-07-25T05:00:00.000Z")):
        apply_operations(state, [("sessions", key, merge_ops(
            op_set_on_insert({"class": "orchestrator", "provenance": "harness"}),
            op_add_to_set({"sources": {"$each": [{"path": path, "kind": "transcript"}]}}),
            op_set({f"sourceState.{field}.present": present}),
            op_max({f"sourceState.{field}.lastSeenTs": seen}),
            collection="sessions"))])
    doc = state["sessions"][key]
    check(doc["sources"] == [{"path": path, "kind": "transcript"}],
          f"a source that disappeared reads as ONE identity, not as three elements that "
          f"grow by one per stat pass: {doc['sources']}")
    check(doc["sourceState"][field] == {"present": False,
                                        "lastSeenTs": "2026-07-25T05:00:00.000Z"},
          f"…and its state reads absent ONCE, with the pass's own lastSeenTs — the two "
          f"halves of GD-26's sentence, both writable: {doc['sourceState'][field]}")
    validate_document("sessions", dict(doc))
    check(True, "…and the whole document still validates against the table")

    # The wall this replaced, stated as an assertion so the reasoning cannot rot
    # into a comment: with the state inside the elements there is no operator
    # that revises one.
    check(raises(OperatorError, validate_update,
                 {"$set": {"sources": [{"path": path, "present": False}]}}, "sessions"),
          "$set on the accumulable `sources` is still refused…")
    check(raises(OperatorError, validate_update,
                 {"$set": {"sources.0.present": False}}, "sessions"),
          "…and so is the positional path mongod would have accepted, because "
          "apply_update cannot replay one and the oracle and the wire must agree")


def test_indexes_and_the_no_ttl_law():
    print("test_indexes_and_the_no_ttl_law")
    events = COLLECTIONS["events"].indexes
    unique = [i for i in events if i["options"].get("unique")]
    check(any(i["keys"] == (("stream", 1), ("seq", 1)) for i in unique),
          "events carries the unique {stream:1, seq:1} index (GD-24)")
    slots = [i for i in COLLECTIONS["slots"].indexes if i["options"].get("unique")]
    check(slots and slots[0]["options"].get("sparse"),
          "slots.agentId is unique AND sparse — a DuplicateKeyError there is the "
          "conflict signal R-53 renders, not a crash")
    for name, spec in sorted(COLLECTIONS.items()):
        offenders = [i for i in spec.indexes if "expireAfterSeconds" in i["options"]]
        check(not offenders, f"{name}: no TTL index (GD-26: not on any Touch collection, ever)")
    check(raises(SchemaError, ms.index_def, ("ts", 1), expireAfterSeconds=86400),
          "…and the index constructor itself refuses expireAfterSeconds, so a "
          "future edit cannot add one by hand")


def test_provenance_pins_are_gd28s():
    print("test_provenance_pins_are_gd28s")
    check(tuple(ms.PROVENANCE) == tuple(store_mod.PROVENANCE),
          "the five-value provenance enum is the same on the file and Mongo sides (GD-28)")
    for name in ("custom_state_events", "custom_state"):
        check(COLLECTIONS[name].provenance == ("asserted", "touch"),
              f"{name} is pinned to {{asserted, touch}} — the writer has no path to 'harness'")
    for name in ("records", "sessions", "agents", "runs", "run_nodes", "usage", "stream_meta"):
        check(COLLECTIONS[name].provenance == ("harness", "derived"),
              f"{name} is pinned to {{harness, derived}} (GD-28)")
    check("unknown" in COLLECTIONS["legacy_events"].provenance
          and "harness" not in COLLECTIONS["legacy_events"].provenance,
          "legacy_events admits 'unknown' and refuses 'harness' — GD-28 forbids guessing "
          "an attribution for the 12 unattributable lines")
    check(set(COLLECTIONS["events"].provenance) == set(ms.PROVENANCE),
          "events keeps the full enum: it mirrors the .touch/ WAL, which legally "
          "carries asserted and unknown lines")
    doc = {"_id": refs.usage_key("msg_1"), "sessionId": "s", "provenance": "asserted"}
    check(raises(SchemaError, validate_document, "usage", doc),
          "a provenance outside a collection's enum is rejected client-side too")

    # GD-11 as amended by the amendment's §2 table: the field is MANDATORY, not
    # merely pinned. A document without one answers neither
    # {provenance:"harness"} nor {provenance:"derived"}, so it would vanish from
    # every provenance-filtered read AND from the "writer unknown" bucket alike
    # — invisible in exactly the query GD-28 invented the field for.
    samples = {
        "records": {"_id": refs.record_key("081b28a7-aee9-43dc-935d-1586407f232e"),
                    "sessionId": "s", "type": "user"},
        "sessions": {"_id": refs.session_key(622, "10028"), "class": "orchestrator"},
        "agents": {"_id": refs.agent_key("a2fc883c96ff7b837")},
        "runs": {"_id": refs.run_key("wf_1")},
        "usage": {"_id": refs.usage_key("msg_2"), "sessionId": "s"},
        "stream_meta": {"_id": refs.stream_meta_key(
            "292fc08c-923d-4ab4-8ff2-a9572417dbc8", 4), "sessionId": "s",
            "lineNo": 4, "type": "summary"},
        "run_nodes": {"_id": refs.run_node_key("wf_1", "research", 0),
                      "runId": "wf_1", "key": "research", "ordinal": 0},
        "custom_state": {"_id": refs.custom_state_key("custom-state#000000000007", "note"),
                         "refId": "custom-state#000000000007", "kind": "annotation", "seq": 7},
        "legacy_events": {"_id": refs.legacy_event_key("touch-mongo-live", 12),
                          "task": "touch-mongo-live", "lineNo": 12},
    }
    for name, spec in sorted(COLLECTIONS.items()):
        if not spec.provenance:
            continue
        check("provenance" in spec.required,
              f"{name}: provenance is REQUIRED, not optional (GD-11(e)/GD-28)")
        check("provenance" in json_schema(name)["$jsonSchema"]["required"],
              f"…and the server's own $jsonSchema requires it too, so a writer that "
              f"skipped this module cannot omit it either")
        if name in samples:
            check(raises(SchemaError, validate_document, name, samples[name]),
                  f"…and a {name} document without one is rejected")
    for name, spec in sorted(COLLECTIONS.items()):
        required = json_schema(name)["$jsonSchema"]["required"]
        check(required[0] == "_id" and set(spec.required) <= set(required),
              f"{name}: every declared required field reaches the server ({required})")


def test_ids_must_come_from_ref_key():
    print("test_ids_must_come_from_ref_key")
    check(ms.check_id("records", refs.record_key("081b28a7-aee9-43dc-935d-1586407f232e")),
          "a canonical uuid _id is accepted")
    check(raises(SchemaError, ms.check_id, "records", "not-a-uuid"),
          "an _id that ref_key could not have produced is rejected (SD-11)")
    check(raises(SchemaError, ms.check_id, "stream_meta", "sess#180"),
          "…including a positional id whose padding is wrong (lexicographic order "
          "would stop matching numeric order)")
    check(raises(SchemaError, ms.check_id, "stream_meta", "sess#notanumber"),
          "…and a key whose parser fails outright is a SchemaError like any other, "
          "not a bare ValueError escaping past every caller's except clause")
    check(raises(SchemaError, ms.check_id, "run_nodes", "wf_1|research"),
          "…nor an arity the grammar never emits")
    check(raises(SchemaError, validate_document, "records",
                 {"_id": {"sessionId": "s", "lineNo": 1}, "sessionId": "s", "type": "user"}),
          "a sub-document _id is refused before it can become two documents (GD-24)")
    check(ms.check_id("derived", "anything-the-reducer-likes"),
          "…and the reducer-owned collection is exempt by declaration")


# --- the algebra ----------------------------------------------------------
def test_forbidden_operators():
    print("test_forbidden_operators")
    check(raises(OperatorError, validate_update, {"$inc": {"in": 1}}),
          "$inc is refused: re-ingest after a transcript rewrite is mandatory and "
          "summed deltas double (GD-25)")
    for op in ("$push", "$pull", "$unset", "$rename", "$currentDate"):
        check(raises(OperatorError, validate_update, {op: {"x": 1}}),
              f"{op} is refused (upsert-only mirror, GD-26)")
    check(raises(OperatorError, validate_update, {"$setOnBanana": {"x": 1}}),
          "an operator nobody legislated is refused rather than passed through")
    check(raises(OperatorError, validate_update, {"$set": {"in": 1}}, "usage"),
          "$set on an accumulable field is refused — it is write-order dependent")
    check(validate_update({"$set": {"gen": 3}}, "records"),
          "…but $set on `gen` is legal: it is how GD-26's generation mark is written")
    check(validate_update({"$set": {"retracted": True}}, "records"),
          "…and retraction is a $set, never a delete (GD-26)")
    check(raises(OperatorError, merge_ops, op_max({"lastTs": 1}), op_set({"lastTs": 2})),
          "one field under two operators is caught here, not as one failed write "
          "inside an unordered bulk of five hundred")
    check(raises(OperatorError, validate_update, {"$set": {"_id": "x"}}, "records"),
          "_id is immutable; only $setOnInsert may write it")
    check(raises(SchemaError, validate_update,
                 {"$set": {"body": {"a.b": 1}}}, "records"),
          "a dotted key smuggled inside an update value is refused too")

    # Mongo's conflict rule is about PATHS, not names: the server answers
    # "Updating the path 'spawn.fileHint' would create a conflict at 'spawn'".
    # `agents.spawn{recordUuid,toolUseId,fileHint}` (GD-24) is the realistic
    # instance, and R-48 updates the hint separately from the identity.
    check(raises(OperatorError, merge_ops,
                 op_set({"spawn": {"recordUuid": "u"}}),
                 op_max({"spawn.fileHint": 3})),
          "a field and a path INSIDE it conflict, exactly as they do at the server")
    check(raises(OperatorError, validate_update,
                 {"$set": {"spawn": {}}, "$max": {"spawn.fileHint": 1}}, "agents"),
          "…and validate_update catches the same pair when the update was hand-built")
    check(validate_update({"$set": {"spawn.a": 1}, "$max": {"spawnX.b": 2}}, "agents"),
          "…while two paths that merely share a prefix STRING do not conflict")

    # merge_ops enforces the CONFLICT rule; the accumulable fence is
    # validate_update's and runs on every real write path either way. Mappers
    # call merge_ops directly, so it takes the collection as a passthrough
    # rather than leaving the reader to infer which half it checks.
    check(merge_ops(op_set({"gen": 1}), collection="records"),
          "merge_ops takes an optional collection and validates the merged update with it")
    check(raises(OperatorError, merge_ops, op_set({"in": 1}), collection="usage"),
          "…so the accumulable fence can fire at the merge, not one call later")
    check(merge_ops(op_set({"in": 1})),
          "…while without one it still only decides conflicts, exactly as documented")

    # GD-24's table is closed. Looking the spec up with .get() would make an
    # unknown collection a collection with no rules — the $set fence silently
    # off, and mongod happily creating an unvalidated, unindexed shadow.
    check(raises(SchemaError, validate_update, {"$set": {"in": 1}}, "usagez"),
          "a typo'd collection name raises instead of disabling the accumulable fence")

    # A positional array path is a legal server update and NOT part of this
    # algebra. The two halves of one write path must agree about it: accepting
    # it in validate_update while apply_update refuses it means `bulk_upsert`
    # sends to the wire an update `apply_operations` cannot replay, and
    # `--rebuild`'s comparison (R-45) plus the GD-25 oracle silently stop
    # covering any mapper that uses one.
    for update in ({"$set": {"sources.0.present": False}},
                   {"$max": {"sources.0.present": True}},
                   {"$set": {"fragments.12": "x"}}):
        check(raises(OperatorError, validate_update, update),
              f"a positional array path is refused at the door: {list(update.values())[0]}")
    check(raises(OperatorError, apply_update, {"sources": [{"present": True}]},
                 {"$set": {"sources.0.present": False}}, _id="live:622-10028"),
          "…which is the same answer apply_update gives, and that agreement is the point: "
          "the memory model is GD-25's oracle and cannot index into a list")
    check(validate_update({"$set": {"spawn.v2.fileHint": 1}}, "agents"),
          "…while a path component that merely CONTAINS digits is untouched — the rule "
          "is about a component that IS an index, not about the character class")

    # m3, disposed of rather than applied: two fields the module's own writers
    # $set, where fencing them would break the only writer each has and buy
    # nothing. The reason is a property of the writer, so it is asserted rather
    # than asserted-about.
    check(validate_update({"$set": {"journalSeq": 3}}, "run_nodes"),
          "$set on run_nodes.journalSeq is legal: `ingest.map_run_node` emits ONE "
          "observation per (runId,key,ordinal) — the node, already folded from its "
          "started/finished lines — so the value is a pure function of the _id and "
          "cannot be write-order dependent (and `_split_ops` has no other operator "
          "to reach it with)")
    check(validate_update({"$set": {"fromSeq": 3}}, "custom_state"),
          "…and $set on custom_state.fromSeq for the sibling reason: it is part of the "
          "head's payload, which `guarded_update` writes behind {$lt: order}, so a "
          "losing event applies NOTHING — guard-order dependent, which is what R-52 asks "
          "for, and the one shape that can replace a head completely")
    check(raises(OperatorError, validate_update, {"$set": {"seq": 8}}, "custom_state"),
          "…while `seq` itself, which advances by $max with no guard at all, stays fenced")


def test_apply_update_matches_the_algebra():
    print("test_apply_update_matches_the_algebra")
    doc = apply_update(None, merge_ops(op_max({"out": 100}),
                                       op_set_on_insert({"sessionId": "s"})), _id="msg_1")
    check(doc == {"sessionId": "s", "out": 100, "_id": "msg_1"}, "insert applies $max and $setOnInsert")
    doc = apply_update(doc, op_max({"out": 260}), _id="msg_1")
    check(doc["out"] == 260, "$max grows: output_tokens GROW across the split records "
                             "of one message.id (MONGOSCHEMA-2)")
    doc = apply_update(doc, op_max({"out": 100}), _id="msg_1")
    check(doc["out"] == 260, "…and never shrinks, so ingest order cannot under-report 2.8×")
    doc = apply_update(doc, op_set_on_insert({"sessionId": "OTHER"}), _id="msg_1")
    check(doc["sessionId"] == "s", "$setOnInsert does not fire on an existing document")
    doc = apply_update(doc, op_min({"firstTs": "2026-07-25T03:00:00.000Z"}), _id="msg_1")
    doc = apply_update(doc, op_min({"firstTs": "2026-07-25T04:00:00.000Z"}), _id="msg_1")
    check(doc["firstTs"] == "2026-07-25T03:00:00.000Z", "$min keeps the earliest")

    agent = None
    for value in (["a.jsonl"], ["b.jsonl"], ["a.jsonl"]):
        agent = apply_update(agent, op_add_to_set({"files": {"$each": value}}), _id="agentX")
    check(agent["files"] == ["a.jsonl", "b.jsonl"],
          "$addToSet unions and dedupes (the two a2fc883c files are disjoint "
          "continuations — $set would overwrite 223 records with 2, MONGOSCHEMA-9)")
    nested = apply_update(None, op_set({"spawn.fileHint": {"line": 4}}), _id="x")
    check(nested == {"spawn": {"fileHint": {"line": 4}}, "_id": "x"},
          "dotted paths address sub-documents, which is how a structured ref is "
          "queried at all (GD-24: dot notation only)")
    source = {"_id": "x", "files": ["a"]}
    apply_update(source, op_add_to_set({"files": "b"}), _id="x")
    check(source["files"] == ["a"], "apply_update is pure: the input document is not mutated")

    # $addToSet dedupes on BSON equality, which is field-ORDER sensitive — the
    # same hazard as GD-24's sub-document `_id`, one level down, in the one
    # operator whose whole job is set semantics. Python's `==` says these two
    # are equal; mongod stores two elements, and `sessions.sources` is both a
    # declared set field and a sub-document array (GD-26's sources[].present).
    ordered = apply_update(None, op_add_to_set(
        {"sources": {"$each": [{"path": "a", "present": True},
                               {"present": True, "path": "a"}]}}), _id="live:1-2")
    check(len(ordered["sources"]) == 2,
          "two sub-documents differing ONLY in field order are two set elements, "
          "as they are on the server (a model more permissive than mongod is worse "
          "than no model: it certifies a fingerprint the server does not produce)")
    again = apply_update(ordered, op_add_to_set({"sources": {"path": "a", "present": True}}),
                         _id="live:1-2")
    check(len(again["sources"]) == 2,
          "…and re-adding a byte-identical sub-document is still idempotent")

    # …but BSON equality compares numbers ACROSS int and double, where the JSON
    # spellings differ. Erring the other way here is the defect `_set_path`'s
    # docstring names: a model more permissive than the server certifies a
    # fingerprint no mongod can reproduce.
    numeric = apply_update(None, op_add_to_set(
        {"fragments": {"$each": [{"n": 1}, {"n": 1.0}, 2, 2.0]}}), _id="agentX")
    check(numeric["fragments"] == [{"n": 1}, 2],
          f"1 and 1.0 are ONE element, as they are on the server (BSON equality spans "
          f"the numeric types; their JSON spellings do not): {numeric['fragments']}")
    check(apply_update(None, op_add_to_set({"fragments": {"$each": [True, 1]}}),
                       _id="agentX")["fragments"] == [True, 1],
          "…while `true` and `1` stay two, because BSON does NOT equate them and "
          "Python's bool-is-an-int would have said otherwise")
    kept = apply_update(None, op_add_to_set({"fragments": {"$each": [1.5, 1.0]}}),
                        _id="agentX")["fragments"]
    check(kept == [1.5, 1.0] and isinstance(kept[1], float),
          f"…and the normalization is dedup identity ONLY: the stored value keeps the "
          f"type it was written with, because that is the type that comes back off the "
          f"wire and the oracle must keep seeing it: {kept}")

    # The model is GD-25's oracle, so "more permissive than mongod" is a defect
    # in it: it certifies a fingerprint the server would have refused to write.
    # All three refusals below were checked against a live mongod 7 first — it
    # answers "Cannot create field 'b' in element {spawn: 5}", "Cannot apply
    # $addToSet to non-array field" and "would modify the immutable field '_id'"
    # — so these are the server's rules restated, not invented strictness.
    check(raises(OperatorError, apply_update, {"spawn": 5}, op_max({"spawn.b": 1}), _id="x"),
          "creating a sub-document over a SCALAR is refused here because mongod refuses "
          "it (\"Cannot create field 'b' in element {spawn: 5}\") — silently replacing the "
          "5 with {b:1} would fingerprint an ingest that fails in production")
    check(apply_update({"spawn": {"a": 1}}, op_max({"spawn.b": 1}),
                       _id="x")["spawn"] == {"a": 1, "b": 1},
          "…while a real sub-document is still reached by dot notation")
    check(raises(OperatorError, apply_update, {"files": "one.jsonl"},
                 op_add_to_set({"files": "two.jsonl"}), _id="x"),
          "…and $addToSet onto a non-array is refused on the same principle")
    check(raises(OperatorError, apply_update, None,
                 op_set_on_insert({"_id": "OTHER", "sessionId": "s"}), _id="msg_1"),
          "a $setOnInsert._id disagreeing with the key is refused: mongod answers 'the _id "
          "field cannot be changed', while this model would have quietly preferred the key")
    check(apply_update(None, op_set_on_insert({"_id": "msg_1", "sessionId": "s"}),
                       _id="msg_1")["_id"] == "msg_1",
          "…while restating the same _id is legal, because it says nothing new")


def test_bulk_upsert_applies_the_same_guards_as_the_memory_pass():
    """The only real write path must enforce what `apply_operations` enforces.

    Both refusals happen **before** pymongo is imported and before `db` is
    touched, which is why this test can hand it a stub that raises on any
    attribute access: a guard that only fires when a database is reachable is a
    guard that never fires in CI.
    """
    print("test_bulk_upsert_applies_the_same_guards_as_the_memory_pass")
    op = merge_ops(op_set_on_insert({"sessionId": "s", "type": "user",
                                     "provenance": "harness"}))
    good = refs.record_key("081b28a7-aee9-43dc-935d-1586407f232e")
    check(raises(SchemaError, ms.bulk_upsert, NoDb(), "recordz", [(good, op)]),
          "an off-table collection name is refused — mongod would otherwise create a "
          "brand-new collection with no validator, no indexes and no _id pin (GD-12)")
    check(raises(SchemaError, ms.bulk_upsert, NoDb(), "records", [("not-a-uuid", op)]),
          "a hand-built _id that never saw refs.ref_key is refused (SD-11)")
    check(raises(SchemaError, ms.bulk_upsert, NoDb(), "stream_meta",
                 [("292fc08c-923d-4ab4-8ff2-a9572417dbc8#180", op)]),
          "…including one whose zero-padding is wrong, which would break _id-range cursors")
    check(raises(SchemaError, ms.bulk_upsert, NoDb(), "records", [({"u": 1}, op)]),
          "…and a sub-document _id never reaches the wire at all (GD-24)")
    check(raises(OperatorError, ms.bulk_upsert, NoDb(), "usage",
                 [(refs.usage_key("msg_1"), {"$inc": {"in": 5}})]),
          "the algebra is enforced here too ($inc is forbidden — GD-25)")
    check(raises(OperatorError, ms.bulk_upsert, NoDb(), "records",
                 [(good, {"$setOnInsert": {"_id": refs.record_key(
                     "11111111-2222-3333-4444-555555555555"),
                     "sessionId": "s", "type": "user", "provenance": "harness"}})]),
          "…and a $setOnInsert._id that disagrees with the key being upserted is refused: "
          "mongod answers \"would modify the immutable field '_id'\" and fails the write")
    check(ms.bulk_upsert(NoDb(), "records", []) == ZERO_WRITE,
          "an empty batch is a no-op, not a connection")

    # A source-level assertion, because the ordering it pins is invisible at
    # runtime on any machine that has pymongo. AST, not grep: a text search for
    # "check_id(" passes on a comment and fails on a rename.
    tree = ast.parse((SRC / "aggregator" / "mongo_store.py").read_text())
    body = function_def(tree, "bulk_upsert")
    called = {node.func.id for node in ast.walk(body)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    check({"spec_for", "check_id", "validate_update"} <= called,
          f"…and the guards are CALLED in bulk_upsert's own body, not merely tested "
          f"here (calls: {sorted(called)})")
    imports = [node.lineno for node in ast.walk(body)
               if isinstance(node, (ast.Import, ast.ImportFrom))]
    returns = [node.lineno for node in ast.walk(body) if isinstance(node, ast.Return)]
    check(imports and returns and min(returns) < min(imports),
          f"…and the empty-batch short-circuit RETURNS (line {min(returns)}) before pymongo "
          f"is imported (line {min(imports)}) — one line of ordering decides whether a bare "
          f"checkout can run this file at all")


def test_a_duplicate_key_is_read_by_which_index_refused_it():
    """GD-24 declares THREE unique indexes and only one of them is `_id`.

    A duplicate on `_id` or on `{stream, seq}` is GD-29's case: idempotent
    replay landing on its own output, or two live writers racing one stream. A
    duplicate on `slots.agentId` (unique sparse) is neither — it is two slots
    claiming one agent, the conflict R-53 renders, and the document was
    REJECTED. Splitting on the error code alone calls all three "tolerated",
    throws away the `keyPattern` the driver hands over, and leaves R-53 with an
    integer and nothing to render.

    `tolerated_dups` deliberately keeps counting every 11000 — `bind_slot` reads
    it as "the claim was refused" and `mirror.MongoBackend` unpacks
    `classify_write_errors`'s pair — so the fix is *additive*: the meaning
    arrives as `identity_dups` (GD-29's diagnostic) plus the items themselves.
    """
    print("test_a_duplicate_key_is_read_by_which_index_refused_it")

    def bulk_error(*items):
        return type("BulkWriteError", (Exception,), {})() if not items else type(
            "BulkWriteError", (Exception,), {"details": {"writeErrors": list(items)}})()

    on_id = {"index": 0, "code": 11000, "keyPattern": {"_id": 1},
             "errmsg": "E11000 duplicate key error … index: _id_"}
    on_stream = {"index": 1, "code": 11000, "keyPattern": {"stream": 1, "seq": 1},
                 "errmsg": "E11000 … index: stream_1_seq_1"}
    on_agent = {"index": 2, "code": 11000, "keyPattern": {"agentId": 1},
                "keyValue": {"agentId": "a2fc883c96ff7b837"},
                "errmsg": "E11000 … collection: …slots index: agentId_1"}
    fatal = {"index": 3, "code": 121, "errmsg": "Document failed validation"}

    split = ms.split_write_errors(bulk_error(on_id, on_stream, on_agent, fatal))
    check([i["index"] for i in split["tolerated"]] == [0, 1],
          f"a dup on `_id` or on {{stream,seq}} is GD-29's tolerated case — the two "
          f"readings that GD is about: {[i['index'] for i in split['tolerated']]}")
    check([i["index"] for i in split["conflicts"]] == [2]
          and split["conflicts"][0]["keyPattern"] == {"agentId": 1},
          f"…a dup on any OTHER unique index is a CONFLICT, carrying the index and the "
          f"value that refused the write: {split['conflicts']}")
    check([i["index"] for i in split["fatal"]] == [3],
          "…and everything that is not 11000 is fatal, unchanged")
    check(ms.classify_write_errors(bulk_error(on_id, on_agent, fatal))
          == (2, [fatal]),
          "classify_write_errors still returns (every-11000, fatal) — mirror.MongoBackend "
          "unpacks that pair for its async twin and custom_state.bind_slot reads the "
          "count as `the agentId claim was refused`, so the split is additive")
    check(ms.split_write_errors(bulk_error())["tolerated"] == [],
          "…and an error with no writeErrors at all splits into three empty lists "
          "rather than raising inside the drainer's except clause")
    check(ms._is_identity_dup({"code": 11000}) is True,       # noqa: SLF001
          "an 11000 with no keyPattern at all reads as an identity dup: `_id` is the "
          "only unique index most collections have, so that is the conservative reading")

    class Bulk:
        """A handle whose `bulk_write` fails the way an unordered bulk does."""

        def __init__(self, error):
            self.error = error

        def __getitem__(self, name):
            return self

        def bulk_write(self, requests, ordered=False):
            raise self.error

    if not ms.pymongo_available():
        skip("the BulkWriteError arm needs pymongo's exception class (GD-21)")
        return
    from pymongo.errors import BulkWriteError

    error = BulkWriteError({"nMatched": 1, "nUpserted": 0, "nModified": 0,
                            "writeErrors": [on_id, on_agent]})
    slot_key = refs.slot_key("622-10028", "auth", "impl", 1)
    result = ms.bulk_upsert(Bulk(error), "slots",
                            [(slot_key, op_set({"agentId": "a2fc883c96ff7b837"}))])
    check(result["tolerated_dups"] == 2 and result["identity_dups"] == 1,
          f"bulk_upsert counts every duplicate key AND says how many were identity dups — "
          f"GD-29's `a nonzero steady state means a second writer or a key bug` reads the "
          f"second number, which ordinary slot conflicts must not move: {result}")
    check(len(result["conflicts"]) == 1
          and result["conflicts"][0]["keyValue"] == {"agentId": "a2fc883c96ff7b837"},
          f"…and the rejected write comes back as data naming the agent that collided, "
          f"which is what R-53 renders: {result['conflicts']}")
    check(result["errors"] == [],
          "…while `errors` stays fatal-only, because bind_slot reads a non-empty one as "
          "`pending` and a slot conflict is not a write failure to retry")
    check(set(result) == set(ZERO_WRITE),
          f"…and every exit of bulk_upsert returns the SAME key set, so no caller has to "
          f"guard an access: {sorted(set(result) ^ set(ZERO_WRITE))}")


def test_the_pure_path_works_with_pymongo_unimportable():
    """GD-21 at CALL granularity — the granularity the promise is made at.

    "Every pure function below works with nothing third-party installed"
    (`mongo_store.py`'s own module docstring) is not "the module imports". The
    import half is asserted in `test_stdlib_only.py`; this is the other half,
    and it is deliberately not left to the runner's environment: it blocks the
    import rather than hoping the interpreter lacks pymongo, so the arm runs —
    and can fail — on a fully-provisioned developer machine.
    """
    print("test_the_pure_path_works_with_pymongo_unimportable")
    good = refs.record_key("081b28a7-aee9-43dc-935d-1586407f232e")
    op = merge_ops(op_set_on_insert({"sessionId": "s", "type": "user",
                                     "provenance": "harness"}))
    doc = {"_id": good, "sessionId": "s", "type": "user", "provenance": "harness",
           "body": {"snapshot": {"trackedFileBackups": {"/a/b.py": "x"}}}}
    prepared, _ = prepare_document("records", doc)
    before = fingerprint(apply_operations({}, [("records", good, op)]))

    blocker = NoPymongo()
    saved = {name: module for name, module in sys.modules.items()
             if name.split(".")[0] in NoPymongo.BLOCKED}
    for name in saved:
        del sys.modules[name]
    sys.meta_path.insert(0, blocker)
    try:
        check(ms.pymongo_available() is False,
              "with pymongo UNIMPORTABLE, pymongo_available() answers False and never raises")
        check(ms.bulk_upsert(NoDb(), "records", []) == ZERO_WRITE,
              "an empty batch is a no-op with pymongo unimportable, not merely absent — "
              "mirror.py drains an empty batch on every quiet tick, and GD-21's degraded "
              "deployment must survive it")
        check(raises(SchemaError, ms.bulk_upsert, NoDb(), "recordz", [(good, op)]),
              "…and the off-table collection guard still fires, before the import (GD-12)")
        check(raises(SchemaError, ms.bulk_upsert, NoDb(), "records", [("not-a-uuid", op)]),
              "…and the _id guard (SD-11)")
        check(raises(OperatorError, ms.bulk_upsert, NoDb(), "usage",
                     [(refs.usage_key("msg_1"), {"$inc": {"in": 5}})]),
              "…and the algebra (GD-25)")
        check(raises(ms.MongoUnavailable, ms.bulk_upsert, NoDb(), "records", [(good, op)]),
              "…while a NON-empty batch degrades to MongoUnavailable, never ImportError")
        check(raises(SchemaError, ms.guarded_update, NoDb(), "recordz", good, op),
              "guarded_update refuses an off-table collection before the import too")
        check(raises(ms.MongoUnavailable, ms.guarded_update, NoDb(), "records", good, op),
              "…and degrades identically once its guards pass")
        check(raises(ms.MongoUnavailable, ms.open_client, "mongodb://127.0.0.1:1/"),
              "…and open_client raises MongoUnavailable, not ImportError")

        again, report = prepare_document("records", doc)
        check(again == prepared and report["declared"] == 1,
              "prepare_document wraps the declared subtree identically")
        check(validate_document("records", again) is again, "validate_document still validates")
        check(ms.check_id("records", good) == good, "check_id still parses the grammar")
        check(unwrap_raw(again["body"]["snapshot"]["trackedFileBackups"]) == {"/a/b.py": "x"},
              "…and the _raw wrapper still round-trips")
        kept, size = guard_oversize("records", again)
        check(kept is again and size > 0, "guard_oversize still measures without BSON")
        check(ts_fields("2026-07-25T03:20:00.123Z")["ts"].microsecond == 123000,
              "ts_fields still parses")
        state = apply_operations({}, [("records", good, op)])
        check(fingerprint(state) == before,
              "…and the whole memory pass fingerprints to the SAME bytes it does with "
              "pymongo installed — the GD-25 oracle is the model, not the driver")
        check(counts(state) == {"records": 1}, "…with the counts half intact too")
        check(json_schema("records")["$jsonSchema"]["properties"]["_id"]
              == {"bsonType": "string"}, "…and the table still describes itself")
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(saved)
    check(blocker not in sys.meta_path,
          "…and the blocker is removed afterwards, so the live arm still runs in this process")


def test_guarded_update_is_the_conditional_write_shape():
    """The two conditional writes GD-24's table implies, with the same guards.

    `bulk_upsert` can only say `{_id: key}`. R-52's derived head needs
    `{_id: k, seq: {$lt: newSeq}}` and GD-29's lease needs
    `{_id: stream, leaseExpiresAt: {$lt: now}}`. If this module does not offer
    the shape, sp-06/sp-11 hand-roll `update_one` against a raw collection
    handle — the exact bypass `bulk_upsert`'s docstring argues must not exist —
    and they find out one sub-plan later that `custom_state` fences `$set` off
    `seq` too.
    """
    print("test_guarded_update_is_the_conditional_write_shape")
    ref_id = refs.agent_key("a2fc883c96ff7b837")
    head = refs.custom_state_key(ref_id, "annotation")
    update = merge_ops(op_max({"seq": 7}),
                       op_set({"refId": ref_id, "kind": "annotation",
                               "provenance": "asserted"}))
    check(raises(SchemaError, ms.guarded_update, NoDb(), "custom_statez", head, update),
          "an off-table collection is refused (GD-24's table is closed)")
    check(raises(SchemaError, ms.guarded_update, NoDb(), "custom_state", "note", update),
          "…and an _id that never saw refs.ref_key (SD-11)")
    check(raises(OperatorError, ms.guarded_update, NoDb(), "custom_state", head,
                 {"$inc": {"seq": 1}}),
          "…and GD-25's algebra reaches this path too")
    check(raises(OperatorError, ms.guarded_update, NoDb(), "custom_state", head,
                 op_set({"seq": 8})),
          "…including the accumulable fence: seq advances by $max, which is why R-52's "
          "guard is about the head's PAYLOAD and not about the counter")
    check(raises(SchemaError, ms.guarded_update, NoDb(), "custom_state", head, update,
                 require={"_id": "somewhere-else"}),
          "a guard may not re-address the document: _id is the key, not a precondition")
    check(raises(SchemaError, ms.guarded_update, NoDb(), "custom_state", head, update,
                 require={"seq": {"$gtz": 3}}),
          "…nor use a comparison nobody legislated")
    check(raises(SchemaError, ms.guarded_update, NoDb(), "custom_state", head, update,
                 require={"$where": "1"}),
          "…nor smuggle an operator in as a field name")

    if not ms.pymongo_available():
        skip("guarded_update's driver arm needs pymongo's exception classes (GD-21)")
        return

    from pymongo.errors import AutoReconnect, DuplicateKeyError, OperationFailure

    class Recorder:
        """A collection handle that records what reached the driver.

        It answers ALL THREE round trips the guarded write can make, because the
        interesting failures are entirely in the ones after the first: a stub
        that only ever says "matched" certifies the acquire path and is blind to
        every way a lost race can be misreported. ``exists`` is what the
        `find_one` probe finds — the difference between "the guard lost to a
        document that is already there" (nothing to create, nothing to count)
        and "the guard found nothing and the create is real".
        """

        def __init__(self, matched=1, on_update=None, on_insert=None, exists=False):
            self.calls = []
            self.inserted = []
            self.matched = matched
            self.on_update = on_update
            self.on_insert = on_insert
            self.exists = exists

        def __getitem__(self, name):
            self.name = name
            return self

        def update_one(self, filter_, update, upsert=True):
            self.calls.append(("update_one", filter_, update, upsert))
            if self.on_update is not None:
                raise self.on_update
            return type("Result", (), {"matched_count": self.matched,
                                       "modified_count": self.matched,
                                       "upserted_id": None})()

        def find_one(self, filter_, projection=None):
            self.calls.append(("find_one", filter_, projection, None))
            return {"_id": filter_["_id"]} if self.exists else None

        def insert_one(self, document):
            self.calls.append(("insert_one", document, None, None))
            if self.on_insert is not None:
                raise self.on_insert
            self.inserted.append(document)
            return type("Result", (), {"inserted_id": document["_id"]})()

        def verbs(self):
            return [call[0] for call in self.calls]

    class AsyncRecorder(Recorder):
        """A handle whose driver calls return coroutines, as `AsyncMongoClient` does."""

        def update_one(self, filter_, update, upsert=True):
            return self._coro()

        def bulk_write(self, requests, ordered=False):
            return self._coro()

        @staticmethod
        def _coro():
            async def pending():                                # pragma: no cover
                return None
            return pending()

    recorder = Recorder(matched=1)
    result = ms.guarded_update(recorder, "custom_state", head, update,
                               require={"seq": {"$lt": 7}})
    _, filter_, sent, upsert = recorder.calls[0]
    check(filter_ == {"_id": head, "seq": {"$lt": 7}},
          f"the precondition rides in the FILTER, so the SERVER decides the race and not "
          f"a read-then-write the next writer can interleave with: {filter_}")
    check(sent is update, "…and the update reaches the driver untouched")
    check(upsert is False,
          "…and the conditional write NEVER inserts: under upsert=True a guard that "
          "matches nothing becomes an INSERT attempt, and mongod answers a PARTIAL one "
          "with code 121 (failed validation) before it can answer duplicate _id")
    check(result["acquired"] is True and result["matched"] == 1
          and result["tolerated_dups"] == 0, f"…and a satisfied guard is acquired: {result}")
    check(recorder.verbs() == ["update_one"],
          "…in ONE round trip, with nothing else attempted")

    # The M1 shape, and the reason the ordering above is not a detail: R-52's own
    # note describes a write that carries the head's PAYLOAD only. Its guard-miss
    # insert is missing every required field, so under an upsert-shaped guard the
    # server answered 121 and a healthy lost race was reported as a dead server —
    # the exact class GD-30's breaker counts toward `mirror: "down"`.
    payload_only = op_set({"note": "older"})
    late_recorder = Recorder(matched=0, on_insert=OperationFailure(
        "Document failed validation", 121))
    late = ms.guarded_update(late_recorder, "custom_state", head, payload_only,
                             require={"seq": {"$lt": 3}})
    check(late["acquired"] is False and late["tolerated_dups"] == 0,
          f"a PAYLOAD-ONLY late write loses the guard and SAYS SO, rather than reporting "
          f"the server unreachable and tripping GD-30's breaker on normal traffic: {late}")
    check(late_recorder.verbs() == ["update_one"],
          "…and no insert is attempted at all: an update that cannot stand alone as a "
          "document is a payload write, not a create, so there is nothing to ask mongod")
    check(late_recorder.inserted == [], "…so nothing was written (acquired:False means that)")

    stream = "run:wf_829e6f58-b2f"
    lease_key = refs.ref_key({"kind": "writer", "stream": stream})
    while_valid = ts_fields("2026-07-25T03:00:00.000Z")["ts"]
    renewed_to = ts_fields("2026-07-25T06:00:00.000Z")["ts"]
    lease = op_set({"holderPid": 622, "holderBoot": "10028", "leaseExpiresAt": renewed_to})
    lost_recorder = Recorder(matched=0, on_insert=DuplicateKeyError("E11000 duplicate key"))
    lost = ms.guarded_update(lost_recorder, "writers", lease_key, lease,
                             require={"leaseExpiresAt": {"$lt": while_valid}})
    check(lost["acquired"] is False and lost["tolerated_dups"] == 1,
          "a lease already held comes back acquired:False and COUNTED — the guard matched "
          "nothing, the probe found nothing, so the create genuinely raced another writer, "
          "and GD-29 forbids swallowing that duplicate key as much as crashing on it")
    check(lost_recorder.verbs() == ["update_one", "find_one", "insert_one"]
          and lost_recorder.inserted == [],
          f"…and the loser wrote nothing at all: {lost_recorder.verbs()}")

    # n2: the OTHER way a guard loses, which is the common one. The document is
    # already there and simply did not satisfy the precondition — R-52's late
    # event, a lease somebody else holds. Attempting the create anyway turns a
    # perfectly healthy single writer's steady traffic into a steady stream of
    # "tolerated dups", inverting the one number GD-29 asks to be read as "a
    # second writer or a key bug".
    present = Recorder(matched=0, exists=True,
                       on_insert=AssertionError("the create must not be attempted"))
    already = ms.guarded_update(present, "writers", lease_key, lease,
                                require={"leaseExpiresAt": {"$lt": while_valid}})
    check(already["acquired"] is False and already["tolerated_dups"] == 0,
          f"a guard lost to a document that ALREADY EXISTS is acquired:False and counts "
          f"NO duplicate — the dup an attempted insert would return there is this call's "
          f"own guard restated as an error, not two writers on one stream: {already}")
    check(present.verbs() == ["update_one", "find_one"] and present.inserted == [],
          f"…and the create is not attempted at all, so GD-29's diagnostic stays a "
          f"diagnostic: {present.verbs()}")

    first = Recorder(matched=0)
    acquired = ms.guarded_update(first, "writers", lease_key, lease,
                                 require={"leaseExpiresAt": {"$lt": while_valid}})
    check(acquired["acquired"] is True and acquired["upserted"] == 1,
          f"…while the SAME call shape acquires an ABSENT lease, so a first acquisition "
          f"needs no separate unguarded create for a second writer to race: {acquired}")
    check(first.inserted[0] == {"holderPid": 622, "holderBoot": "10028",
                                "leaseExpiresAt": renewed_to, "_id": lease_key},
          f"…and the created document is the update plus its key — the PRECONDITION never "
          f"becomes data, which an upsert-built insert does for an equality guard: "
          f"{first.inserted[0]}")

    never = Recorder(matched=0)
    refused = ms.guarded_update(never, "writers", lease_key, lease, upsert=False,
                                require={"leaseExpiresAt": {"$lt": while_valid}})
    check(refused["acquired"] is False and never.verbs() == ["update_one"],
          "upsert=False keeps the create off the table entirely, for a caller that only "
          "ever renews something another code path created")

    check(raises(ms.MongoUnavailable, ms.guarded_update,
                 Recorder(on_update=AutoReconnect("pool paused")), "writers", lease_key, lease),
          "…while an unreachable server is MongoUnavailable, not a raw driver exception")
    check(raises(ms.MongoUnavailable, ms.guarded_update,
                 Recorder(matched=0, on_insert=AutoReconnect("pool paused")), "writers",
                 lease_key, lease, require={"leaseExpiresAt": {"$lt": while_valid}}),
          "…on EITHER round trip, so the degrade contract does not depend on which half "
          "of the guarded write met the dead server")
    check(raises(SchemaError, ms.guarded_update,
                 Recorder(matched=0, on_insert=OperationFailure("Document failed validation", 121)),
                 "writers", lease_key, lease,
                 require={"leaseExpiresAt": {"$lt": while_valid}}),
          "…and a $jsonSchema refusal that reaches the driver anyway (client and server "
          "validators disagreeing) is a SchemaError about the DOCUMENT — never the "
          "MongoUnavailable that would take a healthy mirror down over one bad write")

    # M1's other half: `guarded_update`'s duplicate key can also come from a
    # SECONDARY unique index. `acquired:False` there means "the guard lost",
    # which is not what "another slot already owns this agentId" is.
    secondary = DuplicateKeyError("E11000 duplicate key", 11000, {
        "code": 11000, "keyPattern": {"agentId": 1},
        "keyValue": {"agentId": "a2fc883c96ff7b837"},
        "errmsg": "E11000 duplicate key error collection: slots index: agentId_1"})
    slot_key = refs.slot_key("622-10028", "auth", "impl", 1)
    slot_update = op_set({"sessionKey": "622-10028", "root": "auth", "name": "impl",
                          "attempt": 1, "resolution": "bound",
                          "agentId": "a2fc883c96ff7b837", "provenance": "derived"})
    claimed = ms.guarded_update(Recorder(matched=0, on_insert=secondary), "slots",
                                slot_key, slot_update, require={"resolution": "pending"})
    check(claimed["acquired"] is False and claimed["tolerated_dups"] == 1
          and claimed["identity_dups"] == 0
          and claimed["conflicts"] and claimed["conflicts"][0]["keyPattern"] == {"agentId": 1},
          f"a duplicate on slots.agentId comes back as a CONFLICT naming the index that "
          f"refused it, not as an anonymous tolerated dup — R-53 renders that, and "
          f"GD-29's steady-state number must not move on it: {claimed}")

    # GD-21's client, met by the synchronous shape. The failure used to be an
    # AttributeError on a coroutine — outside the hierarchy `mirror.py` drains
    # with, i.e. the one shape that kills a tick instead of degrading it.
    check(raises(ms.AsyncClientError, ms.guarded_update, AsyncRecorder(), "writers",
                 lease_key, lease),
          "an AsyncMongoClient handed to the SYNC guarded write is an AsyncClientError — "
          "a MongoStoreError, so `except MongoStoreError:` still catches it (GD-21)")
    check(raises(ms.AsyncClientError, ms.bulk_upsert, AsyncRecorder(), "writers",
                 [(lease_key, lease)]),
          "…and the same for bulk_upsert, whose async twin is mirror.MongoBackend's")
    check(issubclass(ms.AsyncClientError, ms.MongoStoreError)
          and not issubclass(ms.AsyncClientError, ms.MongoUnavailable),
          "…and it is NOT MongoUnavailable: the server is fine, the caller passed the "
          "wrong kind of handle, and GD-30's breaker must not count it as a dead mongod")


def test_every_refusal_stays_inside_the_exception_hierarchy():
    """`mirror.py` drains its queue as `except MongoStoreError:` (GD-21/GD-30).

    Anything that escapes that hierarchy kills the tick instead of degrading the
    mirror, which is the whole reason the hierarchy exists. `wrap_raw` set the
    convention for a value JSON cannot encode; these are the other doors into
    the same encoder, plus the one that reads a wrapper back OUT of the database
    (`--rebuild`, R-45), where a truncated `_raw` is not a programmer error in
    this process and `is_raw_wrapper` only proves the field is a string.
    """
    print("test_every_refusal_stays_inside_the_exception_hierarchy")
    unstorable = {"_id": "081b28a7", "v": {1, 2}}                # a set: no BSON encoding
    truncated = {"_raw": "{not json", "_rawEncoding": "json", "_rawKeys": 1}
    for label, call in (
        ("wrap_raw", lambda: ms.wrap_raw({"v": {1, 2}})),
        ("document_size", lambda: ms.document_size(unstorable)),
        ("guard_oversize", lambda: guard_oversize("records", unstorable)),
        ("fingerprint", lambda: fingerprint({"records": {"081b28a7": unstorable}})),
        ("unwrap_raw", lambda: unwrap_raw(truncated)),
    ):
        try:
            call()
            check(False, f"{label}: an unstorable value was accepted")
        except ms.MongoStoreError as exc:
            check(True, f"{label} raises {type(exc).__name__} — a drainer written as "
                        f"`except MongoStoreError:` actually catches it")
        except Exception as other:                              # noqa: BLE001
            check(False, f"{label} escaped the hierarchy as {type(other).__name__}: {other}")
    try:
        fingerprint({"records": {"081b28a7": unstorable}})
    except SchemaError as exc:
        check("records" in str(exc) and "081b28a7" in str(exc),
              f"…and the fingerprint's refusal NAMES the document — the oracle runs over "
              f"thousands, and an unaddressed 'unstorable value of type set' is a bug "
              f"report nobody can act on: {exc}")
    check(unwrap_raw(ms.wrap_raw({"a.b": 1})) == {"a.b": 1},
          "…while a well-formed wrapper still round-trips: the funnel is a translation, "
          "not a new wall")


def test_an_unreachable_server_degrades_never_escapes():
    """`MongoUnavailable` promises "pymongo is absent OR no mongod answered".

    Only the first half was delivered: `BulkWriteError` was caught and every
    other `PyMongoError` — `AutoReconnect`, `ServerSelectionTimeoutError`,
    `NetworkTimeout`, `OperationFailure` — propagated raw, so the class meant one
    thing at this call site and another everywhere else it is documented.
    """
    print("test_an_unreachable_server_degrades_never_escapes")
    if not ms.pymongo_available():
        skip("the raw-driver-exception arm needs pymongo's exception classes (GD-21)")
        return
    from pymongo.errors import AutoReconnect

    good = refs.record_key("081b28a7-aee9-43dc-935d-1586407f232e")
    op = merge_ops(op_set_on_insert({"sessionId": "s", "type": "user",
                                     "provenance": "harness"}))

    class DeadCollection:
        def bulk_write(self, requests, ordered=False):
            raise AutoReconnect("connection pool paused")

        def update_one(self, filter_, update, upsert=True):
            raise AutoReconnect("connection pool paused")

    class DeadDb:
        name = "touch_test_dead"

        def __getitem__(self, name):
            return DeadCollection()

    check(raises(ms.MongoUnavailable, ms.bulk_upsert, DeadDb(), "records", [(good, op)]),
          "an unreachable server leaves bulk_upsert as MongoUnavailable (GD-21: degrade, "
          "never crash) — mirror.py's breaker catches one class, not four")
    check(raises(ms.MongoUnavailable, ms.guarded_update, DeadDb(), "records", good, op),
          "…and guarded_update answers the same way, so the contract does not depend on "
          "which write shape a caller picked")


# --- GD-25 acceptance -----------------------------------------------------
def transcripts():
    """Every frozen transcript, BOTH journals, plus the two single-record specimens.

    The retry journal (`mirror/wf_455b348c-e17`) is in the corpus deliberately:
    it is the only fixture where a `key` carries more than one `started` record,
    so it is the only place `journal_ops`' GD-7 ordinal derivation does any work
    at all. Without it every `(key, type)` pair occurs exactly once, the
    derivation is a constant 0, and replacing it with a literal `0` leaves the
    acceptance fingerprint byte-identical — a test that cannot see the rule its
    own docstring credits.
    """
    paths = sorted((FIX / "run-wf_829e6f58").rglob("*.jsonl"))
    paths += sorted((FIX / "mirror" / "wf_455b348c-e17").rglob("journal.jsonl"))
    paths += [FIX / "mirror" / "records" / "file-history-snapshot-dotted.jsonl",
              FIX / "mirror" / "records" / "queue-operation-user-pair.jsonl"]
    return [p for p in paths if p.exists()]


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

#: The dotted-key specimens carry no `sessionId` of their own; their index file
#: names the transcript every line came from, and R-47's rule is "sessionId
#: injected from path". This is that injection, spelled once.
SPECIMEN_SESSION = json.loads(
    (FIX / "mirror" / "records" / "file-history-snapshot-dotted.index.json").read_text()
)[0]["source"].removesuffix(".jsonl")


#: The two specimen files are *excerpts*: their lines came from a real
#: transcript at line numbers the excerpt does not reproduce. A positional key
#: is only correct against the source's own numbering (R-47: `lineNo` +
#: `byteOffset` on every mirrored record), so the true numbers are restored here
#: — the dotted file's from its index, the pair's from PROVENANCE.md (lines 65
#: and 67 of `292fc08c….jsonl`, 66 skipped on purpose).
LINE_NUMBERS = {
    "file-history-snapshot-dotted.jsonl": [
        row["line"] for row in json.loads(
            (FIX / "mirror" / "records" / "file-history-snapshot-dotted.index.json").read_text())
    ],
    "queue-operation-user-pair.jsonl": [65, 67],
}


def line_number(path, index):
    numbers = LINE_NUMBERS.get(path.name)
    return numbers[index] if numbers else index


def session_of(path, record):
    """`record.sessionId`, else the nearest UUID-named ancestor, else the specimen's."""
    if record.get("sessionId"):
        return record["sessionId"]
    for candidate in [path] + list(path.parents):
        stem = candidate.stem if candidate.suffix == ".jsonl" else candidate.name
        if _UUID_RE.match(stem):
            return stem
    return SPECIMEN_SESSION


def journal_ops(path):
    """Journal lines ⇒ `run_nodes` (+ its `runs` doc), keyed per GD-7 as amended.

    `ordinal` is the 0-based count of preceding `started` records with the same
    `key`, in file line order — derived while reading, stored, never a DB
    counter (restart-unsafe: MONGOSCHEMA-18). The retry fixture — three of its
    six keys carry two `started` records — is what makes this more than a
    formality, and `transcripts()` includes it for exactly that reason.

    It has no UUID-named ancestor directory, so `session_of` falls through to
    `SPECIMEN_SESSION` for it. That is harmless here (it only feeds
    `runs.sessionIds`, which this test never asserts a value for) and is stated
    rather than left as a surprise for the next reader.
    """
    run_id = path.parent.name
    ops = [("runs", refs.run_key(run_id),
            merge_ops(op_set_on_insert({"workflowName": run_id, "provenance": "harness"}),
                      op_add_to_set({"sessionIds": session_of(path, {})})))]
    started, resulted = {}, {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        entry = json.loads(line)
        key = entry.get("key")
        if key is None:
            continue
        # Every operation on a node carries the SAME $setOnInsert identity, and
        # `journalSeq` is a $min so the `started` line (always the earlier one)
        # wins whichever operation happens to insert the document. That is the
        # discipline op_set_on_insert() documents, and getting it wrong here is
        # exactly what the shuffled pass caught while this test was written.
        identity = op_set_on_insert({"runId": run_id, "key": key, "ordinal": None,
                                     "provenance": "harness"})
        if entry.get("type") == "started":
            ordinal = started.get(key, 0)
            started[key] = ordinal + 1
            extra = [op_set({"agentId": entry.get("agentId")})]
        else:
            ordinal = resulted.get(key, 0)
            resulted[key] = ordinal + 1
            extra = [op_set({"resultSeen": True})]
            if isinstance(entry.get("result"), (dict, list)):
                extra.append(op_set({"result": ms.wrap_raw(entry["result"])}))
        identity["$setOnInsert"]["ordinal"] = ordinal
        ops.append(("run_nodes", refs.run_node_key(run_id, key, ordinal),
                    merge_ops(identity, op_min({"journalSeq": line_no}), *extra)))
    return ops


def mapper_ops(path):
    """A minimal, test-local mapper: one `(collection, _id, update)` per line.

    Deliberately test-local — the real per-entity mappers are sp-07…sp-11's
    (SD-1). What is under test here is the *algebra* they are all required to
    speak: uuid keys for the four rewritable record types, positional keys for
    everything else (R-47), `$max`-upserted absolute token docs (R-50), and
    `$addToSet`/`$min`/`$max` for the agent union (R-48).
    """
    if path.name == "journal.jsonl":
        return journal_ops(path)
    agent_id = path.stem[len("agent-"):] if path.stem.startswith("agent-") else None
    ops = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            sid = session_of(path, record)
            line_no = line_number(path, line_no)
            ts = record.get("timestamp")
            times = ts_fields(ts) if ts else {}
            common = {"sessionId": sid, "type": record.get("type", "unknown"),
                      "lineNo": line_no, "provenance": "harness"}
            common.update(times)
            body, _ = prepare_document(
                "records" if record.get("uuid") else "stream_meta", {"body": record})
            if record.get("uuid") and record.get("type") in RECORD_TYPES:
                ops.append(("records", refs.record_key(record["uuid"]),
                            merge_ops(op_set_on_insert(dict(common, **body)),
                                      op_set({"gen": 1}))))
            else:
                ops.append(("stream_meta", refs.stream_meta_key(sid, line_no),
                            merge_ops(op_set_on_insert(dict(common, **body)),
                                      op_set({"gen": 1,
                                              "render": record.get("type") != "queue-operation"}))))
            usage = ((record.get("message") or {}).get("usage")
                     if isinstance(record.get("message"), dict) else None)
            message_id = (record.get("message") or {}).get("id") if isinstance(
                record.get("message"), dict) else None
            if usage and message_id:
                ops.append(("usage", refs.usage_key(message_id),
                            merge_ops(
                                op_max({"in": usage.get("input_tokens", 0),
                                        "out": usage.get("output_tokens", 0),
                                        "cached": usage.get("cache_read_input_tokens", 0),
                                        "cache_write": usage.get(
                                            "cache_creation_input_tokens", 0)}),
                                op_set_on_insert({"sessionId": sid, "provenance": "harness"}))))
            if agent_id:
                agent_ops = [op_add_to_set({"sessions": sid, "files": path.name})]
                if times:
                    agent_ops.append(op_min({"firstTs": times["ts"]}))
                    agent_ops.append(op_max({"lastTs": times["ts"]}))
                ops.append(("agents", refs.agent_key(agent_id),
                            merge_ops(*agent_ops, op_set_on_insert({"provenance": "harness"}))))
    return ops


def expected_counts(paths):
    """Counts derived straight from the files, independently of the mapper.

    "Independently" is the whole point, so nothing here calls `session_of()` or
    `line_number()`: an expectation built from the mapper's own helpers cancels
    the mapper's bugs on both sides of the assertion and still passes. The
    `records` and `usage` halves were always independent (a set of uuids, a set
    of `message.id`s); `stream_meta` is now too — it is a plain count of
    non-blank lines that are not uuid-keyed records, per file, summed.

    That count equals the number of distinct `(sessionId, lineNo)` keys only if
    no two mirrored lines collide on one key. If a keying rule ever collapses
    two lines into one document, this expectation stays put while the mapper's
    count drops, and the assertion fails — which is exactly the silent collapse
    MONGOSCHEMA-16's probe suffered (142 of 333 records lost to a content-hash
    key, fingerprint unchanged).

    `run_nodes`/`runs` are counted here for the same reason, and they are the
    ones that needed it most: `run_nodes` is the collection whose keying rule is
    newest (GD-7 as amended — a journal-derived `ordinal` in the `_id`), and a
    rule that merges every retried attempt onto its first attempt's document
    merges them identically on every pass, so a pass-to-pass comparison sees
    nothing. A node exists per `(key, ordinal)`, and the ordinals a key reaches
    are bounded by how many `started` records it has and how many `result`
    records it has — hence `max` of the two, read straight from the journal
    without calling `journal_ops`.
    """
    uuids, meta_lines, messages, agents = set(), 0, set(), set()
    nodes, runs = 0, set()
    for path in paths:
        if path.name == "journal.jsonl":
            runs.add(path.parent.name)                 # runs._id is the workflow dir name
            starts, results = {}, {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                key = entry.get("key")
                if key is None:
                    continue
                bucket = starts if entry.get("type") == "started" else results
                bucket[key] = bucket.get(key, 0) + 1
            nodes += sum(max(starts.get(key, 0), results.get(key, 0))
                         for key in set(starts) | set(results))
            continue                                   # journals are run_nodes, not records
        agent = path.stem[len("agent-"):] if path.stem.startswith("agent-") else None
        if agent:
            agents.add(agent)
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("uuid") and record.get("type") in RECORD_TYPES:
                uuids.add(record["uuid"])
            else:
                meta_lines += 1
            message = record.get("message")
            if isinstance(message, dict) and message.get("usage") and message.get("id"):
                messages.add(message["id"])
    return {"records": len(uuids), "stream_meta": meta_lines,
            "usage": len(messages), "agents": len(agents),
            "run_nodes": nodes, "runs": len(runs)}


def test_gd25_acceptance_normal_shuffled_reversed():
    print("test_gd25_acceptance_normal_shuffled_reversed")
    paths = transcripts()
    check(len(paths) >= 10, f"the frozen corpus is present ({len(paths)} files)")
    check(any(p.parent.name == "wf_455b348c-e17" for p in paths),
          "…including the retry journal, without which the GD-7 ordinal derivation is "
          "never exercised above 0 and the acceptance test cannot see it at all")
    ops = []
    for path in paths:
        ops.extend(mapper_ops(path))
    check(len(ops) > 500, f"{len(ops)} upsert operations derived from it")

    normal = apply_operations({}, ops)
    shuffled_ops = list(ops)
    random.Random(20260725).shuffle(shuffled_ops)
    shuffled = apply_operations({}, shuffled_ops)
    reversed_ = apply_operations({}, list(reversed(ops)))
    twice = apply_operations(apply_operations({}, ops), ops)

    base = fingerprint(normal)
    check(fingerprint(shuffled) == base, "shuffled ingest ⇒ identical fingerprint (GD-25)")
    check(fingerprint(reversed_) == base, "reversed ingest ⇒ identical fingerprint")
    check(fingerprint(twice) == base, "…and ingesting the whole corpus twice changes nothing")
    got = counts(normal)
    check(counts(shuffled) == got == counts(reversed_) == counts(twice),
          f"…with equal counts on every pass: {got}")

    want = expected_counts(paths)
    for collection, n in sorted(want.items()):
        check(got.get(collection) == n,
              f"{collection}: {got.get(collection)} documents == the {n} the files contain "
              f"(the count half of GD-25 — a fingerprint of fewer documents is still a fingerprint)")

    # The ordinal derivation, shown doing work. A constant `ordinal = 0` leaves
    # the fingerprint of a corpus without retries byte-identical, so the only
    # honest assertion is one that reads the retried keys directly.
    retry = FIX / "mirror" / "wf_455b348c-e17" / "journal.jsonl"
    node_ops = [op for op in journal_ops(retry) if op[0] == "run_nodes"]
    ordinals = {update["$setOnInsert"]["ordinal"] for _, _, update in node_ops}
    check({0, 1} <= ordinals,
          f"the retry journal exercises ordinals above zero: {sorted(ordinals)}")
    node_ids = {key for _, key, _ in node_ops}
    node_keys = {refs.parse_ref_key("runNode", key)["key"] for key in node_ids}
    check(len(node_ids) == 9 and len(node_keys) == 6,
          f"…and the derivation is load-bearing: {len(node_ids)} nodes over "
          f"{len(node_keys)} keys — stuck at 0 it would merge 3 retried attempts onto "
          f"their first attempt's _id, and both the fingerprint and every pass-to-pass "
          f"count would still agree (MONGOSCHEMA-16)")

    # The negative arm: the test must be able to FAIL. An inconsistent
    # $setOnInsert payload is the realistic mapper bug, and it is order-dependent
    # by construction, so a shuffled pass has to catch it.
    victim = next(op for op in ops if op[0] == "usage")
    poisoned = list(ops)
    poisoned.append(("usage", victim[1],
                     merge_ops(op_set_on_insert({"sessionId": "WRONG",
                                                 "provenance": "harness"}))))
    first = fingerprint(apply_operations({}, poisoned))
    last = fingerprint(apply_operations({}, list(reversed(poisoned))))
    check(first != last,
          "a mapper emitting $setOnInsert inconsistently DOES change the fingerprint "
          "under reordering — the acceptance test is not vacuous")

    dropped = [op for op in ops if op[0] != "usage"]
    check(counts(apply_operations({}, dropped)).get("usage") is None,
          "…and losing a whole keying rule shows up in the counts, not the fingerprint")


def test_the_disjoint_continuations_union():
    print("test_the_disjoint_continuations_union")
    pair = sorted(FIX.rglob("agent-a2fc883c96ff7b837.jsonl"))
    check(len(pair) == 2, f"the cross-session pair is present ({len(pair)} files)")
    if len(pair) != 2:
        return
    ops = mapper_ops(pair[0]) + mapper_ops(pair[1])
    forward = apply_operations({}, ops)
    backward = apply_operations({}, list(reversed(ops)))
    agent = forward["agents"][refs.agent_key("a2fc883c96ff7b837")]
    check(sorted(agent["sessions"]) == sorted(
        backward["agents"][refs.agent_key("a2fc883c96ff7b837")]["sessions"]),
        "both sessions survive in either order ($addToSet, not $set — MONGOSCHEMA-9)")
    check(len(agent["sessions"]) == 2,
          "the 223-record segment and the 2-record one are ONE agent across two sessions")
    check(fingerprint(forward) == fingerprint(backward),
          "…and the whole union fingerprints identically either way")
    check(agent["firstTs"] < agent["lastTs"],
          "$min/$max bracket the agent's activity window (GD-23: observations, no state field)")


# --- shape guards ---------------------------------------------------------
def test_dotted_keys_are_raw_wrapped_and_round_trip():
    print("test_dotted_keys_are_raw_wrapped_and_round_trip")
    path = FIX / "mirror" / "records" / "file-history-snapshot-dotted.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    check(len(lines) == 33, f"33 dotted-key specimens (got {len(lines)})")
    session = "292fc08c-923d-4ab4-8ff2-a9572417dbc8"
    for index, record in enumerate(lines):
        doc, report = prepare_document("stream_meta", {
            "_id": refs.stream_meta_key(session, index),
            "sessionId": session, "lineNo": index, "type": record["type"],
            "provenance": "harness", "body": record})
        validate_document("stream_meta", doc)
        restored = unwrap_raw(doc["body"]["snapshot"]["trackedFileBackups"])
        if restored != record["snapshot"]["trackedFileBackups"]:
            check(False, f"record {index}: _raw round-trip lost data")
            return
        if report["declared"] != 1 or report["auto"]:
            check(False, f"record {index}: expected exactly the declared wrap, got {report}")
            return
    check(True, "all 33 wrap at the DECLARED path and round-trip byte-identically")
    check(doc["body"]["messageId"] == lines[-1]["messageId"],
          "…and the wrap is scoped to the hostile subtree: ordinary fields stay queryable")

    hostile = {"_id": refs.stream_meta_key(session, 1), "sessionId": session, "lineNo": 1,
               "type": "t", "provenance": "harness", "extra": {"a.b": 1}}
    unwrapped, report = prepare_document("stream_meta", hostile, autowrap=False)
    check(raises(SchemaError, validate_document, "stream_meta", unwrapped),
          "an undeclared dotted key left unwrapped is REJECTED (R-44/MONGOSCHEMA-8)")
    wrapped, report = prepare_document("stream_meta", hostile)
    validate_document("stream_meta", wrapped)
    check(report["auto"] == 1 and report["auto_paths"] == ["extra"],
          "…and autowrap saves it while recording the declaration gap")
    check(unwrap_raw(wrapped["extra"]) == {"a.b": 1}, "…losslessly")
    check(raises(SchemaError, validate_document, "records",
                 {"_id": refs.record_key("081b28a7-aee9-43dc-935d-1586407f232e"),
                  "sessionId": "s", "type": "user", "body": {"$where": 1}}),
          "a $-prefixed key is refused on the same rule")

    # The wrapper is the module's only sanctioned escape hatch, and the key
    # walk stops at one — so it is recognised by SHAPE, not by the presence of
    # two fields. Otherwise any subtree could carry `_raw`/`_rawEncoding` as
    # decoration and smuggle its hostile siblings straight through.
    spoof = {"_id": refs.record_key("081b28a7-aee9-43dc-935d-1586407f232e"),
             "sessionId": "s", "type": "user", "provenance": "harness",
             "body": {"_raw": "{}", "_rawEncoding": "json", "evil.key": 1}}
    check(not ms.is_raw_wrapper(spoof["body"]),
          "a dict carrying the wrapper's fields AND hostile siblings is not a wrapper")
    check(raises(SchemaError, validate_document, "records", spoof),
          "…so its dotted sibling is caught, not skipped over")
    real = ms.wrap_raw({"a.b": 1})
    check(ms.is_raw_wrapper(real) and unwrap_raw(real) == {"a.b": 1},
          "…while a real wrapper is still opaque and still round-trips")
    check(ms.is_raw_wrapper(dict(real, _rawAuto=True)),
          "…and an auto-wrap (the declaration-gap marker) is a wrapper too")
    check(not ms.is_raw_wrapper({"_raw": {"a": 1}, "_rawEncoding": "json"}),
          "…and a non-string _raw is not a wrapper either")

    # Every refusal in this module is a MongoStoreError, including this one: a
    # drainer written as `except MongoStoreError:` — the reason the hierarchy
    # exists — would miss a bare TypeError and die on the tick.
    check(raises(SchemaError, ms.wrap_raw, {"a.b": object()}),
          "a subtree JSON cannot encode is a SchemaError, not a bare TypeError from "
          "inside json.dumps")
    check(raises(SchemaError, prepare_document, "records",
                 {"_id": refs.record_key("081b28a7-aee9-43dc-935d-1586407f232e"),
                  "toolUseResult": {"a.b": object()}}),
          "…and prepare_document surfaces it as one too, since that is where a mapper "
          "meets it")


def test_oversize_becomes_a_stub_never_a_drop():
    print("test_oversize_becomes_a_stub_never_a_drop")
    real = json.loads((FIX / "mirror" / "records" / "oversize-line.jsonl").read_text())
    doc = {"_id": refs.record_key(real["uuid"]), "sessionId": real["sessionId"],
           "type": real["type"], "provenance": "harness", "body": real}
    prepared, _ = prepare_document("records", doc)
    kept, size = guard_oversize("records", prepared, source_path="oversize-line.jsonl")
    check("oversize" not in kept and size > 800_000,
          f"the real 877 KB line is stored whole ({size} bytes) — headroom is real")
    validate_document("records", kept)

    huge = dict(doc, body={"blob": "x" * (OVERSIZE_LIMIT + 1)})
    stub, size = guard_oversize("records", huge, source_path="/p/big.jsonl", byte_offset=4096)
    check(stub["oversize"] is True and size > OVERSIZE_LIMIT,
          f"…and a document over 8 MB becomes a stub ({size} bytes)")
    check(stub["sourcePath"] == "/p/big.jsonl" and stub["byteOffset"] == 4096,
          "the stub says where the bytes are, so nothing is silently dropped")
    check(stub["_id"] == doc["_id"] and stub["sessionId"] == real["sessionId"],
          "…and keeps its _id and key fields, so it still joins and still counts")
    validate_document("records", stub)

    # A missing offset is an ABSENT field, never an explicit null. `byteOffset`
    # is pinned to ["int","long"] on `records` and `stream_meta`, and
    # $jsonSchema applies a bsonType pin to a property whenever it is PRESENT —
    # null has bsonType "null" and fails. A stub carrying byteOffset:null would
    # be rejected by the server on arrival, inverting R-44's "never silently
    # dropped" into "guaranteed rejection" for precisely the records the guard
    # exists to save. (Verified against mongod: null REJECTED, omitted ACCEPTED.)
    partial, _ = guard_oversize("records", huge, source_path="/p/big.jsonl")
    check("byteOffset" not in partial and partial["sourcePath"] == "/p/big.jsonl",
          "an omitted byte_offset leaves the field OUT of the stub, not null")
    validate_document("records", partial)
    check(True, "…so the stub the guard builds by default is one the server accepts")
    bare, _ = guard_oversize("records", huge)
    check("sourcePath" not in bare and "byteOffset" not in bare,
          "…and the same for an omitted source_path")
    validate_document("records", bare)
    check(raises(SchemaError, validate_document, "records",
                 dict(doc, byteOffset=None)),
          "a present null on a pinned field is refused client-side, because that is "
          "what the server's own $jsonSchema does — the two validators must not disagree")
    check(validate_document("records", dict(doc, byteOffset=4096)),
          "…while the same field with an int is fine")


def test_ts_is_supplied_by_the_aggregator():
    print("test_ts_is_supplied_by_the_aggregator")
    fields = ts_fields("2026-07-25T03:20:00.123Z")
    check(fields["tsRaw"] == "2026-07-25T03:20:00.123Z",
          "tsRaw keeps the source's own spelling (legacy streams carry mixed formats)")
    check(fields["ts"].tzinfo is not None and fields["ts"].microsecond == 123000,
          "…and ts is a tz-aware datetime at BSON Date's millisecond resolution")
    check(ts_fields("2026-07-25T03:20:00Z")["ts"].microsecond == 0,
          "a second-resolution legacy ts parses too")
    check(raises(SchemaError, ts_fields, "not a time"),
          "an unparseable ts is a loud rejection, never a silent now()")
    check(raises(SchemaError, ts_fields, None),
          "…and there is no default: the aggregator supplies every ts (GD-11(g))")


# --- static guards --------------------------------------------------------
def test_no_delete_verbs_and_no_clock_in_the_module():
    print("test_no_delete_verbs_and_no_clock_in_the_module")
    source = (SRC / "aggregator" / "mongo_store.py").read_text()
    tree = ast.parse(source)
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    delete_verbs = {"delete_one", "delete_many", "drop", "drop_database", "find_one_and_delete",
                    "remove"}
    check(not (called & delete_verbs),
          f"mongo_store.py calls no delete verb {sorted(called & delete_verbs)} — the "
          f"mirror is insert/upsert-only (GD-26); the one legal delete is "
          f"mirror.py's scoped stream_meta renumber")
    # GD-26 asks for a *grep*, and the AST walk above sees attribute names only:
    # `getattr(coll, "delete_" + "many")(...)` is invisible to it. Every string
    # constant in the module is therefore checked too — that is where a
    # reflective call has to spell its verb. (A plain text grep is useless here:
    # "drop" appears in a dozen lines of prose about never dropping records.)
    literals = {node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    smuggled = sorted(text for text in literals
                      if text in delete_verbs or "delete_" in text or "drop_" in text)
    check(not smuggled,
          f"…and no delete verb is SPELLED as a string either, so it cannot be reached "
          f"through getattr {smuggled}")
    check("$unset" not in {k for k in ms.ALLOWED_OPS},
          "…and $unset is not in the algebra either")
    check(not (called & {"now", "utcnow", "time", "monotonic"}),
          f"…and it never reads the clock {sorted(called & {'now', 'utcnow', 'time'})}")

    # GD-21: pymongo is imported lazily, never at module level. `imports_of` is
    # the stdlib guard's own parser — re-deriving it here with string matching is
    # how a guard rots.
    from test_stdlib_only import imports_of
    top, lazy = imports_of(tree)
    check("pymongo" not in top and "pymongo" in lazy,
          "pymongo is imported inside functions only, so this module imports with "
          "nothing third-party installed (GD-21)")
    check(top <= set(sys.stdlib_module_names) | {"aggregator", "__future__"},
          f"…and nothing else third-party rides along at module level: {sorted(top)}")


def test_client_options_are_gd21s():
    print("test_client_options_are_gd21s")
    check(ms.CLIENT_OPTIONS == {"serverSelectionTimeoutMS": 500, "connectTimeoutMS": 500,
                                "socketTimeoutMS": 2000, "retryWrites": True},
          "GD-21's client options verbatim — the 30 s default stalls the poll loop "
          "for 30.1 s against a dead port (MONGOSCHEMA-4)")
    check(ms.client_options(socketTimeoutMS=1)["socketTimeoutMS"] == 1
          and ms.CLIENT_OPTIONS["socketTimeoutMS"] == 2000,
          "…and an override does not mutate the shared dict")
    check(ms.pymongo_available() in (True, False),
          "pymongo_available() answers instead of raising (absence is a degrade, GD-21)")
    if not ms.pymongo_available():
        check(raises(ms.MongoUnavailable, ms.open_client, "mongodb://127.0.0.1:1/"),
              "…and opening a client without pymongo raises MongoUnavailable, not ImportError")


# --- live arm (skips cleanly) ---------------------------------------------
def live_database():
    """(db, client, name) against `TOUCH_MONGO_URI`, or (None, None, reason)."""
    uri = os.environ.get("TOUCH_MONGO_URI")
    if not uri:
        return None, None, "TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)"
    if not ms.pymongo_available():
        return None, None, "pymongo is not installed (GD-21: absence is legal)"
    try:
        client = ms.open_client(uri)
    except ms.MongoUnavailable as exc:
        return None, None, str(exc)
    if not ms.ping(client):
        client.close()
        return None, None, "no mongod answered within the GD-21 timeouts"
    # GD-27/GD-12: a name we construct, and the only one we will ever drop.
    name = f"touch_test_{os.getpid()}"
    return client[name], client, name


def test_live_mongod_arm():
    print("test_live_mongod_arm")
    db, client, name = live_database()
    if db is None:
        skip(f"live Mongo arm: {name}")
        return
    try:
        _live_checks(db, name)
    finally:
        check(name.startswith("touch_test_"),
              f"dropping only the database this test constructed: {name} (GD-27)")
        client.drop_database(name)
        client.close()


def _live_checks(db, name):
    from pymongo.errors import WriteError

    ms.ensure_schema(db)
    check(set(db.list_collection_names()) == set(COLLECTIONS),
          "ensure_schema created GD-24's collections")
    ms.ensure_schema(db)
    check(True, "…and running it again is a no-op (it must be safe on every boot)")

    for collection in ("events", "records", "slots"):
        for index in db[collection].list_indexes():
            if "expireAfterSeconds" in index:
                check(False, f"{collection}: a TTL index reached the server")
                return
    check(True, "no index on the server carries expireAfterSeconds (GD-26, read back)")
    events_indexes = {tuple(i["key"].items()): i for i in db["events"].list_indexes()}
    check(any(i.get("unique") for keys, i in events_indexes.items()
              if keys == (("stream", 1), ("seq", 1))),
          "the unique {stream:1,seq:1} index exists on the server")

    # GD-24's opening law, enforced by the server and not merely by us.
    try:
        db["records"].insert_one({"_id": {"s": "x", "n": 1}, "sessionId": "s", "type": "user"})
        check(False, "a sub-document _id was accepted by the server")
    except WriteError:
        check(True, "the server REFUSES a sub-document _id ({s,n} vs {n,s} would be "
                    "two documents — MONGOSCHEMA-6 ≡ CUSTOMSTATE-4 ≡ LIVEFLOW-2)")

    # The same operations, through a real mongod, in three orders.
    paths = transcripts()
    ops = []
    for path in paths:
        ops.extend(mapper_ops(path))
    memory = fingerprint(apply_operations({}, ops))
    orders = {"normal": ops, "reversed": list(reversed(ops))}
    shuffled = list(ops)
    random.Random(20260725).shuffle(shuffled)
    orders["shuffled"] = shuffled
    # GD-26's static no-delete guard covers `mongo_store.py`, not this file, and
    # the loop below wipes collections between passes. Assert the target is the
    # database this test CONSTRUCTED, immediately above the first call that a
    # future edit could point at a real mirror (GD-27/GD-12).
    check(db.name.startswith("touch_test_"),
          f"the per-collection wipe can only reach the constructed database: {db.name}")
    if not db.name.startswith("touch_test_"):
        return
    seen = {}
    for label, sequence in orders.items():
        for collection in set(op[0] for op in sequence):
            db[collection].delete_many({})          # test fixture reset, not mirror code
        batches = {}
        for collection, key, update in sequence:
            batches.setdefault(collection, []).append((key, update))
        dups = 0
        for collection, batch in batches.items():
            result = ms.bulk_upsert(db, collection, batch)
            if result["errors"]:
                check(False, f"{label}/{collection}: write errors {result['errors'][:1]}")
                return
            dups += result["tolerated_dups"]
        state = {c: {d["_id"]: d for d in db[c].find({})} for c in batches}
        seen[label] = (fingerprint(state), counts(state), dups)
    check(len({value[0] for value in seen.values()}) == 1,
          f"normal / shuffled / reversed ingest into a real mongod ⇒ ONE fingerprint "
          f"({', '.join(f'{k}={v[0][:8]}' for k, v in seen.items())})")
    check(len({tuple(sorted(value[1].items())) for value in seen.values()}) == 1,
          f"…and equal counts: {seen['normal'][1]}")
    check(seen["normal"][0] == memory,
          "…and the in-memory model agrees with the server byte for byte")

    # LIVEFLOW-3: both indexed forms IXSCAN; a dotted-`_id` query would COLLSCAN,
    # which is why no `_id` in this schema is a sub-document.
    stream = "run:wf_829e6f58-b2f"
    db["events"].insert_many([
        {"_id": refs.event_key(stream, n), "stream": stream, "seq": n, "source": "ingest",
         "provenance": "harness", "kind": "log"} for n in range(50)])
    plan = db["events"].find({"stream": stream, "seq": {"$gte": 40}}).explain()
    check("IXSCAN" in json.dumps(plan.get("queryPlanner", {})),
          "the (stream, seq) cursor query is an IXSCAN")
    plan = db["events"].find({"_id": {"$gte": refs.event_key(stream, 40)}}).explain()
    check("IXSCAN" in json.dumps(plan.get("queryPlanner", {})),
          "…and so is the zero-padded _id range scan, so both cursors agree (GD-24)")

    # GD-29: duplicate key is tolerated on replay AND counted, because it is also
    # the signature of two live writers racing one stream.
    result = ms.bulk_upsert(db, "events", [
        (refs.event_key(stream, 0),
         merge_ops(op_set_on_insert({"stream": stream, "seq": 0, "source": "ingest",
                                     "provenance": "harness", "kind": "log"})))])
    check(not result["errors"], "replaying an event we already stored is not an error")
    conflict = ms.bulk_upsert(db, "events", [
        (refs.event_key("control", 0),
         merge_ops(op_set_on_insert({"stream": stream, "seq": 0, "source": "ingest",
                                     "provenance": "harness", "kind": "log"})))])
    check(conflict["tolerated_dups"] == 1 and conflict["identity_dups"] == 1
          and not conflict["errors"] and not conflict["conflicts"],
          "a second writer landing on an existing (stream, seq) is COUNTED as a "
          "tolerated duplicate, never swallowed (GD-29) — and it is an IDENTITY dup, "
          "the reading that GD is about")

    # The other unique index GD-24 declares, against the real one. `slots.agentId`
    # is unique sparse, and a duplicate there is R-53's conflict: the write did
    # NOT happen, and telling the caller `tolerated_dups: 1, errors: []` with no
    # per-item detail loses both the document and the reason.
    def slot_claim(name, agent):
        key = refs.slot_key("622-10028", "auth", name, 1)
        return key, merge_ops(op_set_on_insert(
            {"sessionKey": "622-10028", "root": "auth", "name": name, "attempt": 1,
             "resolution": "bound", "provenance": "derived"}),
            op_set({"agentId": agent}))

    agent = "a2fc883c96ff7b837"
    first_slot = ms.bulk_upsert(db, "slots", [slot_claim("impl", agent)])
    check(first_slot["upserted"] == 1 and not first_slot["errors"],
          "the first slot claims the agent")
    second_slot = ms.bulk_upsert(db, "slots", [slot_claim("critic", agent)])
    check(second_slot["conflicts"] and second_slot["identity_dups"] == 0,
          f"…and a SECOND slot claiming the same agentId comes back as a conflict with "
          f"identity_dups 0 — the write did not happen, and GD-29's steady-state number "
          f"must not move on what R-53 calls normal: {second_slot['conflicts'][:1]}")
    check(second_slot["conflicts"][0].get("keyPattern") == {"agentId": 1},
          f"…naming the index that refused it, which is the thing R-53 renders: "
          f"{second_slot['conflicts'][0].get('keyPattern')}")
    check(second_slot["tolerated_dups"] == 1 and not second_slot["errors"],
          "…while `tolerated_dups` still counts it and `errors` stays empty, because "
          "custom_state.bind_slot reads exactly that pair (a non-empty `errors` there "
          "means `pending`, and a slot conflict is not a write to retry)")
    check(db["slots"].count_documents({"agentId": agent}) == 1,
          "…and exactly one slot holds the agent on the server")
    guarded_key, guarded_update_doc = slot_claim("auditor", agent)
    guarded_conflict = ms.guarded_update(db, "slots", guarded_key, guarded_update_doc,
                                         require={"resolution": "pending"})
    check(guarded_conflict["acquired"] is False
          and guarded_conflict["conflicts"]
          and guarded_conflict["identity_dups"] == 0,
          f"…and the OTHER write shape reads it the same way: a guarded create that lost "
          f"to another slot's claim on the agent is a conflict, not `I lost the race for "
          f"this _id` — same vocabulary from both doors: {guarded_conflict}")
    check(db["slots"].find_one({"_id": guarded_key}) is None,
          "…with nothing written, which is what acquired:False means everywhere")

    # GD-29's lease, against the real thing: the whole point of guarded_update is
    # that the SERVER decides the race, and "the guard matched nothing" is only
    # reported as a duplicate `_id` because the upsert then tried to insert one.
    # That is a claim about mongod's behaviour, so it is asserted against mongod.
    lease_key = refs.ref_key({"kind": "writer", "stream": stream})
    expires_at = ts_fields("2026-07-25T04:00:00.000Z")["ts"]      # the holder's expiry
    while_valid = ts_fields("2026-07-25T03:00:00.000Z")["ts"]     # a challenger's "now"
    once_lapsed = ts_fields("2026-07-25T05:00:00.000Z")["ts"]     # …an hour later
    renewed_to = ts_fields("2026-07-25T06:00:00.000Z")["ts"]
    held = ms.guarded_update(db, "writers", lease_key, op_set(
        {"holderPid": 622, "holderBoot": "10028", "leaseExpiresAt": expires_at}))
    check(held["acquired"] and held["upserted"] == 1,
          "an unheld lease is acquired by the upsert that creates it (GD-29)")
    contended = ms.guarded_update(db, "writers", lease_key, op_set(
        {"holderPid": 999, "holderBoot": "20000", "leaseExpiresAt": renewed_to}),
        require={"leaseExpiresAt": {"$lt": while_valid}})
    check(not contended["acquired"] and contended["tolerated_dups"] == 0,
          "…a second writer challenging a lease that has NOT lapsed does not acquire it, "
          "and counts NO duplicate: the guarded write probes for the document before "
          "creating one, so a lost race against a lease that plainly exists is reported "
          "as the lost race it is rather than as GD-29's two-writers signature")
    check(db["writers"].find_one({"_id": lease_key})["holderPid"] == 622,
          "…and the holder is untouched: the loser wrote nothing at all")
    taken = ms.guarded_update(db, "writers", lease_key, op_set(
        {"holderPid": 999, "holderBoot": "20000", "leaseExpiresAt": renewed_to}),
        require={"leaseExpiresAt": {"$lt": once_lapsed}})
    check(taken["acquired"] and taken["matched"] == 1
          and db["writers"].find_one({"_id": lease_key})["holderPid"] == 999,
          "…while the SAME call shape takes over once the lease has lapsed, in one round "
          "trip, with no read-then-write for a third writer to interleave with")

    # The renewal, which is the call GD-29's holder makes constantly and the one
    # the suite never sent: it writes ONLY the expiry, so its guard-miss insert
    # would be missing `holderPid`/`holderBoot` and the server would answer 121,
    # not duplicate-key. Both outcomes are asserted against the real validator.
    renewed_again = ts_fields("2026-07-25T07:00:00.000Z")["ts"]
    renewal = ms.guarded_update(db, "writers", lease_key,
                                op_set({"leaseExpiresAt": renewed_again}),
                                require={"holderPid": 999})
    check(renewal["acquired"] and same_moment(
        db["writers"].find_one({"_id": lease_key})["leaseExpiresAt"], renewed_again),
        "the holder renews by writing ONLY its expiry, behind an equality precondition on "
        "itself — a PARTIAL update, which is exactly why the guarded write is not shaped "
        "as an upsert (GD-29)")
    stale = ms.guarded_update(db, "writers", lease_key,
                              op_set({"leaseExpiresAt": once_lapsed}),
                              require={"holderPid": 1})
    check(stale["acquired"] is False and stale["tolerated_dups"] == 0,
          "…and a renewal from a writer that no longer holds the lease comes back "
          "acquired:False — a lost race, never MongoUnavailable, which GD-30's breaker "
          "would have counted toward taking a perfectly healthy mirror down")
    check(same_moment(db["writers"].find_one({"_id": lease_key})["leaseExpiresAt"],
                      renewed_again),
          "…and it wrote nothing")

    # R-52's other half, also server-side: a late old event must not clobber a
    # fresher head, and `seq` is $max so the counter itself needs no guard.
    ref_id = refs.agent_key("a2fc883c96ff7b837")
    head_key = refs.custom_state_key(ref_id, "annotation")
    for seq, body in ((7, "newer"), (3, "older")):
        ms.guarded_update(db, "custom_state", head_key,
                          merge_ops(op_max({"seq": seq}),
                                    op_set({"refId": ref_id, "kind": "annotation",
                                            "provenance": "asserted", "note": body})),
                          require={"seq": {"$lt": seq}})
    head = db["custom_state"].find_one({"_id": head_key})
    check(head["seq"] == 7 and head["note"] == "newer",
          "a late OLD custom-state write leaves the head alone — {seq:{$lt:newSeq}} is "
          "what makes the derived head order-independent (R-52)")
    late = ms.guarded_update(db, "custom_state", head_key, op_set({"note": "older"}),
                             require={"seq": {"$lt": 3}})
    check(late["acquired"] is False and late["tolerated_dups"] == 0,
          "…and the PAYLOAD-ONLY form of that write — the one `custom_state`'s own note "
          "describes, carrying no identity at all — loses the guard and SAYS SO against a "
          "real $jsonSchema, instead of reporting the server unreachable (GD-29/GD-30)")
    check(db["custom_state"].find_one({"_id": head_key})["note"] == "newer",
          "…with the head untouched, because acquired:False means nothing was written")

    # GD-11(e)/GD-28, proven where it counts: the SERVER refuses a mirrored
    # document with no provenance, not merely this module's validator.
    try:
        db["records"].insert_one({"_id": refs.record_key(
            "11111111-2222-3333-4444-555555555555"), "sessionId": "s", "type": "user"})
        check(False, "the server accepted a records document with no provenance")
    except WriteError:
        check(True, "the server REFUSES a mirrored document with no provenance — the "
                    "$jsonSchema requires it, so a writer that skipped this module "
                    "cannot omit it either (GD-28)")

    # GD-26's no-TTL law is enforced over the definitions this module emits;
    # `create_index` is additive, so an index added by an older version or by
    # hand survives every boot. ensure_schema must read them back. Done LAST:
    # the assertion is that the next boot REFUSES to start.
    db["records"].create_index([("ts", 1)], expireAfterSeconds=86400, name="ttl_by_hand")
    check(raises(SchemaError, ms.ensure_schema, db, collections=["records"]),
          "a TTL index someone added by hand makes the next ensure_schema REFUSE — "
          "a TTL here would re-import the CLI's own destruction of history on a timer "
          "nobody is watching (GD-26)")


def main():
    for test in (
        test_the_table_is_gd24s,
        test_sessions_id_is_a_tagged_union,
        test_a_disappeared_source_is_revisable_state_not_a_growing_array,
        test_indexes_and_the_no_ttl_law,
        test_provenance_pins_are_gd28s,
        test_ids_must_come_from_ref_key,
        test_forbidden_operators,
        test_apply_update_matches_the_algebra,
        test_bulk_upsert_applies_the_same_guards_as_the_memory_pass,
        test_a_duplicate_key_is_read_by_which_index_refused_it,
        test_the_pure_path_works_with_pymongo_unimportable,
        test_guarded_update_is_the_conditional_write_shape,
        test_every_refusal_stays_inside_the_exception_hierarchy,
        test_an_unreachable_server_degrades_never_escapes,
        test_gd25_acceptance_normal_shuffled_reversed,
        test_the_disjoint_continuations_union,
        test_dotted_keys_are_raw_wrapped_and_round_trip,
        test_oversize_becomes_a_stub_never_a_drop,
        test_ts_is_supplied_by_the_aggregator,
        test_no_delete_verbs_and_no_clock_in_the_module,
        test_client_options_are_gd21s,
        test_live_mongod_arm,
    ):
        test()
    print()
    for message in skips:
        print(f"skipped: {message}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("all mongo_store (R-44) tests passed")


if __name__ == "__main__":
    main()
