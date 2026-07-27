"""Mongo foundations: collections, indexes, upsert algebra, validators (R-44).

GD-24's table is the law here, and this module is the only place it exists in
code. Everything above it — the per-entity `MIRROR_MAPPERS` (SD-1), the mirror
runtime (R-45) — speaks in this vocabulary and nothing else, so a mapper cannot
invent a collection, a key rule, or an update operator on the side.

What this module is, in one line each
-------------------------------------
* **The table** — :data:`COLLECTIONS`: `_id` kinds (always `refs.ref_key`
  strings, never sub-documents; a row may have two, since `sessions` is a
  tagged union of `live:` and `hist:`), required fields — `provenance` among
  them wherever GD-28 declares an enum — BSON type pins and the index set. No
  TTL index exists anywhere, a test asserts the *data* rather than the prose,
  and `ensure_schema` reads the server's own indexes back so one added by hand
  cannot survive a boot (GD-26; MONGOSCHEMA-13).
* **The algebra** — `$max` / `$min` / `$addToSet` / `$setOnInsert`, plus `$set`
  fenced off the accumulable fields. `$inc` is refused outright: re-ingest
  after a `performRemoveByUuid` rewrite is *mandatory*, and summed deltas
  double (GD-25; `output_tokens` grows across the split records of one
  `message.id`, so first-wins under-reports 2.8× and `$set` is write-order
  dependent).
* **The same algebra, in memory** — :func:`apply_update` /
  :func:`apply_operations` / :func:`fingerprint`. This is what makes GD-25's
  acceptance test runnable with no database at all (normal / shuffled /
  reversed ⇒ one fingerprint AND expected counts), and what `--rebuild` (R-45)
  compares against. `tests/test_mongo_store.py` additionally runs the *same*
  operations through a real mongod when one is reachable and asserts the two
  fingerprints agree — an in-memory model that silently disagrees with the
  server is worse than none.
* **The shape guards** — `_raw`-wrapping for variable-key subtrees (dotted and
  `$`-prefixed keys are legal JSON and hostile BSON), an oversize stub above
  8 MB (never a silent drop), and `ts` supplied by the aggregator as a BSON
  Date plus the original string in `tsRaw` (GD-11(g): the server never invents
  a timestamp and `$natural`/ObjectId time orders nothing).
* **The two write shapes** — `bulk_upsert` (a batch of `{_id: key}` upserts) and
  `guarded_update` (ONE upsert behind a precondition: R-52's
  `{seq:{$lt:newSeq}}` head guard, GD-29's `{leaseExpiresAt:{$lt:now}}` lease).
  Both apply the same guards, so no caller downstream has a reason to reach for
  a raw collection handle — which is how a mapper would acquire the ability to
  invent a collection or an `_id` after all. Both are **synchronous**: GD-21
  puts the live path on `AsyncMongoClient`, and the async twin of each shape is
  `mirror.MongoBackend`'s (R-45), built out of the pure guards here. Handing an
  async handle to either raises `AsyncClientError` — a `MongoStoreError`, so the
  drainer's one `except` still holds.
* **The write-error rule** — `writeErrors` of an unordered bulk are ALWAYS
  inspected, and read three ways rather than two. Duplicate-key on an *identity*
  index is the signature of both idempotent replay (healthy) and two live
  writers racing one stream (a bug), so it is counted and surfaced rather than
  swallowed (GD-29). Duplicate-key on any other unique index — `slots.agentId`
  is the one GD-24 declares — is a *rejected write* whose reason is data: R-53
  renders it as a conflict. `split_write_errors` keeps the two apart and hands
  back the driver's own items, `keyPattern` included.

What this module is not
-----------------------
It never touches the filesystem (GD-15: `store.py` keeps sole ownership of
`.touch/`), never reads the clock, never opens a client at import time, and
holds no runtime state. The live async client, the bounded queue, the circuit
breaker, the writer lease and the generation sweep are `mirror.py`'s (R-45);
what lives here is the *description* those need — including
:data:`CLIENT_OPTIONS`, stated once so two modules cannot drift on the timeouts
that keep a dead mongod off the poll loop (MONGOSCHEMA-4's 30.1 s stall).

`pymongo` is imported **lazily, inside functions** (GD-21): every pure function
below works with nothing third-party installed, which is why the mirror
degrading to `mirror:"absent"` never fails startup and never blocks a test.
"""

from __future__ import annotations

import datetime
import hashlib
import json

from . import refs

__all__ = [
    "MongoStoreError",
    "SchemaError",
    "OperatorError",
    "MongoUnavailable",
    "AsyncClientError",
    "COLLECTIONS",
    "CLIENT_OPTIONS",
    "ALLOWED_OPS",
    "FORBIDDEN_OPS",
    "PROVENANCE",
    "OVERSIZE_LIMIT",
    "BSON_DOC_LIMIT",
    "RAW_FIELD",
    "RAW_FIELDS",
    "DUPLICATE_KEY",
    "IDENTITY_INDEXES",
    "collection_names",
    "spec_for",
    "check_id",
    "json_schema",
    "index_specs",
    "index_def",
    "wrap_raw",
    "unwrap_raw",
    "is_raw_wrapper",
    "prepare_document",
    "validate_document",
    "document_size",
    "guard_oversize",
    "ts_fields",
    "op_set",
    "op_max",
    "op_min",
    "op_add_to_set",
    "op_set_on_insert",
    "merge_ops",
    "validate_update",
    "apply_update",
    "apply_operations",
    "fingerprint",
    "counts",
    "pymongo_available",
    "client_options",
    "open_client",
    "ping",
    "ensure_schema",
    "bulk_upsert",
    "guarded_update",
    "GUARD_OPS",
    "classify_write_errors",
    "split_write_errors",
]


class MongoStoreError(Exception):
    """Base for every rejection this module makes."""


class SchemaError(MongoStoreError):
    """A document that cannot legally be stored (shape, type pin, or key)."""


class OperatorError(MongoStoreError):
    """An update that violates GD-25's algebra."""


class MongoUnavailable(MongoStoreError):
    """pymongo is absent or no mongod answered — degrade, never crash (GD-21)."""


class AsyncClientError(MongoStoreError):
    """A **synchronous** write shape was handed an async handle (GD-21).

    :func:`bulk_upsert` and :func:`guarded_update` are `MongoClient`-shaped:
    they read `result.matched_count` off the driver's return value. Handed an
    `AsyncMongoClient` — the client GD-21 puts the *live* path on — that value
    is an un-awaited coroutine, and the failure used to be an `AttributeError`
    from **outside** this module's hierarchy, i.e. exactly the shape a drainer
    written as `except MongoStoreError:` does not catch. The async twin of both
    shapes is `mirror.MongoBackend` (R-45), which reuses the pure guards here;
    see :func:`bulk_upsert`'s "Driver mode" note.
    """


# --- constants ------------------------------------------------------------

#: GD-21, verbatim. The 30 s server-selection default stalls the poll loop
#: (MONGOSCHEMA-4 measured 30.1 s against a dead port), and GD-30 gives Mongo a
#: 0 ms budget on the critical path — so the timeouts are part of the contract,
#: not tuning. `mirror.py` builds its `AsyncMongoClient` from this exact dict.
CLIENT_OPTIONS = {
    "serverSelectionTimeoutMS": 500,
    "connectTimeoutMS": 500,
    "socketTimeoutMS": 2000,
    "retryWrites": True,
}

#: GD-28's five-value orthogonal enum. `store.PROVENANCE` holds the same closed
#: set for the file side; `tests/test_mongo_store.py` asserts the two agree
#: rather than trusting this copy.
PROVENANCE = ("harness", "derived", "asserted", "touch", "unknown")

#: R-44: above this a document becomes a stub, never a silent drop. The BSON
#: hard cap is 16 MiB; the largest real transcript line in the frozen corpus is
#: 877 395 bytes (5 % of the cap), so the headroom is real but finite — which is
#: exactly why the guard exists rather than a comment saying it cannot happen.
OVERSIZE_LIMIT = 8 * 1024 * 1024
BSON_DOC_LIMIT = 16 * 1024 * 1024

#: The `_raw` wrapper's field names (see :func:`wrap_raw`).
RAW_FIELD = "_raw"
RAW_ENCODING_FIELD = "_rawEncoding"
RAW_KEYS_FIELD = "_rawKeys"
RAW_AUTO_FIELD = "_rawAuto"
RAW_ENCODING = "json"

#: Mongo's duplicate-key error code. Tolerated on replay, counted always (GD-29).
DUPLICATE_KEY = 11000

#: The unique indexes on which a duplicate key means GD-29's *identity* case —
#: "this document is already stored", i.e. idempotent replay landing on its own
#: output (healthy) or two live writers racing one stream (a bug the lease
#: exists to prevent). Those are the two readings GD-29 assigns the number.
#:
#: GD-24's table declares a THIRD unique index — `slots.agentId`, unique sparse
#: — and a duplicate there means something else entirely: two different slots
#: claiming one agentId, which R-53 renders as a `conflict` document. The write
#: did not happen either way, but "another slot owns this agent" is data, not a
#: writer-topology diagnostic, and folding it into the same integer would move
#: GD-29's "a nonzero steady state means a second writer or a key bug" number on
#: ordinary, expected slot traffic. :func:`split_write_errors` keeps them apart;
#: `tolerated_dups` remains the total (`custom_state.bind_slot` reads it as
#: "the claim was refused"), and `identity_dups` is GD-29's diagnostic.
IDENTITY_INDEXES = (("_id",), ("stream", "seq"))

#: mongod's "Document failed validation" — a `$jsonSchema` refusal. Named
#: because it is the one server error that must NOT be read as unavailability
#: (see :func:`_driver_error`): it says the document is wrong, not the server.
DOCUMENT_VALIDATION_FAILED = 121

#: GD-25's algebra. `$set` is present but fenced: it is the only way to write
#: GD-26's `gen` mark and the `retracted` retraction, and it is refused on any
#: field a collection declares accumulable.
ALLOWED_OPS = ("$max", "$min", "$addToSet", "$setOnInsert", "$set")

#: Named so a rejection can say *why*, not merely "not allowed".
FORBIDDEN_OPS = {
    "$inc": "accumulation is $max, never $inc — re-ingest is mandatory after a "
            "transcript rewrite and summed deltas double (GD-25)",
    "$push": "$push is not idempotent under replay; multi-valued fields are $addToSet",
    "$pull": "the mirror is upsert-only (GD-26)",
    "$pop": "the mirror is upsert-only (GD-26)",
    "$unset": "the mirror is upsert-only; disappearance is a field, never a removal (GD-26)",
    "$rename": "keys are the schema; renaming one at write time hides a mapper bug",
    "$bit": "not part of the algebra (GD-25)",
    "$currentDate": "the aggregator supplies every ts; the server never generates one (GD-11(g))",
}


# --- the GD-24 table ------------------------------------------------------


class CollectionSpec:
    """One row of GD-24's collection table.

    ``id_kind`` is a **list** of `refs` kinds, because one row of the table is a
    tagged union: `sessions._id` is `live:<pid>-<procStart>` *or*
    `hist:<sessionId>`, and a key is legal if any declared kind produces it. A
    single-valued field would have made the whole historical half of the session
    registry unwritable through this module (the `hist:` grammar exists in
    `refs.py` and `refs.collection_of("histSession")` is `sessions`), and the
    failure would have surfaced in the *next* sub-plan rather than in the one
    that owns the table.

    ``provenance``, when declared, is also **required**: GD-11 as amended by the
    amendment's §2 table makes the field mandatory (GD-28), and a document
    without one answers neither `{provenance:"harness"}` nor
    `{provenance:"derived"}` — it would vanish from every provenance-filtered
    query *and* from the "writer unknown" bucket alike. It is appended here
    rather than repeated in fifteen `required=` tuples so a row added later
    cannot forget it. Two rows declare **no** provenance and are exempt on
    purpose — `writers` and `cursors` are aggregator-internal bookkeeping with
    no upstream writer to attribute (each says so in its own `note`); every row
    that mirrors content declares one, and therefore requires one.

    ``accumulable`` is the fenced set: fields that may only be reached by
    `$max`/`$min`/`$addToSet`, never `$set`. ``set_fields`` are the
    `$addToSet`-built arrays — sets, so :func:`fingerprint` sorts them and only
    them. (Sorting every array instead would make GD-25's shuffled-pass
    assertion blind to a real ordering regression; sorting none would make it
    fail on the sets, which genuinely do land in ingest order — in Mongo as
    well as here.)

    ``raw_paths`` are the *declared* variable-key subtrees (see
    :func:`prepare_document`) — declared rather than merely detected so the
    stored shape of a field is stable across instances:
    `snapshot.trackedFileBackups` has dotted keys in most records and clean
    ones in some, and a field that is sometimes a sub-document and sometimes a
    wrapper is unqueryable.
    """

    __slots__ = ("name", "id_kinds", "required", "types", "provenance", "indexes",
                 "set_fields", "accumulable", "raw_paths", "note")

    def __init__(self, name, id_kind, *, required=(), types=None, provenance=None,
                 indexes=(), set_fields=(), accumulable=(), raw_paths=(), note=""):
        self.name = name
        if id_kind is None:
            self.id_kinds = ()
        elif isinstance(id_kind, str):
            self.id_kinds = (id_kind,)
        else:
            self.id_kinds = tuple(id_kind)
        self.types = dict(types or {})
        self.provenance = tuple(provenance) if provenance else None
        required = tuple(required)
        if self.provenance and "provenance" not in required:
            required += ("provenance",)
        self.required = required
        self.indexes = tuple(indexes)
        self.set_fields = frozenset(set_fields)
        self.accumulable = frozenset(accumulable)
        self.raw_paths = tuple(raw_paths)
        self.note = note

    @property
    def id_kind(self):
        """The first declared `_id` kind, for messages and single-kind rows."""
        return self.id_kinds[0] if self.id_kinds else None


def index_def(*keys, **options):
    """An index definition: key list + options, with TTL structurally excluded."""
    if "expireAfterSeconds" in options:
        # Not a test — a wall. GD-26: no TTL index on any Touch collection,
        # ever. The mirror exists *because* the CLI deletes history; a TTL would
        # re-import that destruction on a timer nobody is watching.
        raise SchemaError("expireAfterSeconds is forbidden on every Touch collection (GD-26)")
    return {"keys": tuple(keys), "options": dict(options)}


#: BSON type pins (GD-24). Ints are pinned to `["int","long"]` deliberately:
#: pymongo encodes a Python int as int32 below 2^31 and int64 above it, so a
#: bare `"int"` pin would reject a `byteOffset` past 2 GiB *only on large
#: files* — the worst possible failure schedule.
_INT = ["int", "long"]
_STR = "string"
_DATE = "date"
_ARRAY = "array"
_BOOL = "bool"
#: Sub-documents GD-24 names by shape (`agents.spawn`, `runs.harnessTotals`).
#: Pinning the container, never its members: the fields inside stay open, which
#: is the same open-tail rule `json_schema` keeps for undeclared properties.
_OBJ = "object"

#: The variable-key subtrees that show up on mirrored harness records. Paths are
#: matched from the document root with array indices transparent, so
#: `body.message.content.input` covers every content block's `input` map.
_HARNESS_RAW_PATHS = (
    "body.message.content.input",
    "body.toolUseResult",
    "body.snapshot.trackedFileBackups",
    "body.input",
    "message.content.input",
    "toolUseResult",
    "snapshot.trackedFileBackups",
    "input",
)

COLLECTIONS = {
    "sessions": CollectionSpec(
        # GD-24's table gives this row TWO `_id` grammars, and both are storable:
        # `live:<pid>-<procStart>` for a session with a running process, and
        # `hist:<sessionId>` for every `~/.claude/projects/<slug>/*.jsonl`
        # transcript whose process is gone (R-25's historical arm, R-46's
        # immutable ids). A key is legal if EITHER produces it.
        "sessions", ("session", "histSession"),
        required=("class",),
        # `sources` is pinned like the two other declared arrays: it is already
        # a set field and an accumulable, so the only thing standing between it
        # and a scalar was `apply_update`'s $addToSet refusal — a client-side
        # rule, on the write path only.
        #
        # GD-26's source clause has TWO halves and they need two fields, which
        # is the whole reason `sourceState` exists beside `sources`:
        #
        #   * `sources` is the append-only *identity* set — which files this
        #     session was ever observed through. `$addToSet`, hence accumulable,
        #     hence unrevisable: the operator has no way to edit an element it
        #     already holds. That is right for history and wrong for state.
        #   * `sourceState` is the revisable half — GD-26's `present:false,
        #     lastSeenTs` "set by a stat pass". A sub-document keyed by
        #     `refs.escape_field_key(path)`, so an ordinary `$set`/`$max` on
        #     `sourceState.<key>.present` addresses ONE source and the answer to
        #     "is this file still there" is one value, not an array the reader
        #     has to date-order and de-contradict.
        #
        # Written the other way — `present`/`lastSeenTs` inside the `$addToSet`
        # elements only — a source that disappears cannot be *revised*: every
        # stat pass adds another element, the array grows at the stat pass's
        # frequency, and two elements for one path disagree with no ordering
        # guarantee (`$addToSet` has none by construction). The decision belongs
        # here rather than in sp-07 because sp-07 owns `sessions.py` and not
        # this table (GD-15), so it would have met the wall in a file it cannot
        # fix — the same hazard `CollectionSpec`'s own docstring argues against
        # for `id_kind`.
        types={"pid": _INT, "procStart": _STR, "sessionIds": _ARRAY, "cwd": _STR,
               "slugs": _ARRAY, "class": _STR, "sources": _ARRAY,
               "sourceState": _OBJ, "firstTs": _DATE, "lastTs": _DATE},
        provenance=("harness", "derived"),
        indexes=(index_def(("sessionIds", 1)), index_def(("lastTs", -1))),
        set_fields=("sessionIds", "slugs", "sources"),
        accumulable=("firstTs", "lastTs", "sessionIds", "slugs", "sources"),
        note="tagged union live:<pid>-<procStart> | hist:<sessionId> (R-46); "
             "sources[] is the append-only identity set, sourceState.<escaped path> "
             "is GD-26's revisable {present,lastSeenTs} that a stat pass $sets",
    ),
    "records": CollectionSpec(
        "records", "uuid",
        required=("sessionId", "type"),
        types={"sessionId": _STR, "agentId": _STR, "type": _STR, "ts": _DATE,
               "tsRaw": _STR, "parentUuid": _STR, "toolUseId": _STR,
               "lineNo": _INT, "byteOffset": _INT, "gen": _INT, "retracted": _BOOL},
        provenance=("harness", "derived"),
        indexes=(index_def(("sessionId", 1), ("ts", 1)),
                 index_def(("agentId", 1), ("ts", 1)),
                 index_def(("parentUuid", 1)),
                 index_def(("toolUseId", 1), sparse=True)),
        raw_paths=_HARNESS_RAW_PATHS,
        note="uuid-keyed rewritable source: retraction, never deletion (GD-26)",
    ),
    "stream_meta": CollectionSpec(
        "stream_meta", "streamMeta",
        required=("sessionId", "lineNo", "type"),
        types={"sessionId": _STR, "lineNo": _INT, "type": _STR, "render": _BOOL,
               "ts": _DATE, "tsRaw": _STR, "byteOffset": _INT, "gen": _INT},
        provenance=("harness", "derived"),
        indexes=(index_def(("sessionId", 1), ("lineNo", 1)),),
        raw_paths=_HARNESS_RAW_PATHS,
        note="every uuid-less type, positionally (R-47); queue-operation carries render:false",
    ),
    "agents": CollectionSpec(
        "agents", "agentId",
        required=(),
        types={"agentType": _STR, "model": _STR, "spawnDepth": _INT, "description": _STR,
               "toolUseId": _STR, "runId": _STR, "sessions": _ARRAY, "files": _ARRAY,
               "fragments": _ARRAY, "root": _STR, "name": _STR, "spawn": _OBJ,
               "firstTs": _DATE, "lastTs": _DATE},
        provenance=("harness", "derived"),
        indexes=(index_def(("runId", 1)),
                 index_def(("toolUseId", 1), sparse=True),
                 index_def(("root", 1), ("name", 1))),
        set_fields=("sessions", "files", "fragments"),
        accumulable=("firstTs", "lastTs", "sessions", "files", "fragments"),
        note="17-hex agentId; sessionId is NEVER a grouping key here (R-48)",
    ),
    "runs": CollectionSpec(
        "runs", "run",
        required=(),
        types={"taskId": _STR, "workflowName": _STR, "transcriptDir": _STR,
               "scriptPath": _STR, "sessionIds": _ARRAY, "status": _STR,
               "harnessTotals": _OBJ, "startedAt": _DATE, "endedAt": _DATE},
        provenance=("harness", "derived"),
        indexes=(index_def(("startedAt", -1)),),
        set_fields=("sessionIds",),
        accumulable=("startedAt", "endedAt", "sessionIds"),
        note="harnessTotals is namespaced and display-only, never summed (GD-24)",
    ),
    "run_nodes": CollectionSpec(
        "run_nodes", "runNode",
        required=("runId", "key", "ordinal"),
        # `result` is pinned to the CONTAINER type its `raw_paths` declaration
        # guarantees: a declared raw path is wrapped unconditionally by
        # `prepare_document`, so what reaches the server is always the `_raw`
        # wrapper sub-document, whatever the journal's own `result` was. GD-24
        # names it `result{}` and the pin is what makes that the server's rule
        # too — a writer that skipped `prepare_document` and stored the bare
        # string would otherwise leave the field sometimes an object and
        # sometimes not, which is the unqueryable shape `raw_paths` exists for.
        #
        # `journalSeq` is deliberately NOT accumulable, and the reason is a
        # property of the writer rather than an oversight: `ingest.map_run_node`
        # emits exactly ONE observation per `(runId, key, ordinal)` — the node,
        # already folded from its `started`/`finished` journal lines — so the
        # value is a pure function of the `_id` and `$set` cannot be
        # write-order dependent on it. Fencing it would make the sole writer of
        # this collection unable to write it at all (`_split_ops` puts every
        # non-immutable, non-accumulator field under `$set`), which is a real
        # break traded for a hypothetical one.
        types={"runId": _STR, "key": _STR, "ordinal": _INT, "journalSeq": _INT,
               "agentId": _STR, "resultSeen": _BOOL, "result": _OBJ,
               "startedAt": _DATE, "endedAt": _DATE},
        provenance=("harness", "derived"),
        indexes=(index_def(("runId", 1), ("journalSeq", 1)), index_def(("agentId", 1))),
        accumulable=("startedAt", "endedAt"),
        raw_paths=("result",),
        note="ordinal is journal-derived and stored, never a DB counter (GD-7 amended); "
             "journalSeq is $set because one observation per node decides it",
    ),
    "usage": CollectionSpec(
        "usage", "usage",
        required=("sessionId",),
        types={"in": _INT, "out": _INT, "cached": _INT, "cache_write": _INT,
               "agentId": _STR, "sessionId": _STR, "runId": _STR,
               "ts": _DATE, "tsRaw": _STR},
        provenance=("harness", "derived"),
        indexes=(index_def(("agentId", 1)), index_def(("sessionId", 1)), index_def(("runId", 1))),
        accumulable=("in", "out", "cached", "cache_write"),
        note="_id = message.id; four fields $max, ids $setOnInsert (GD-25/R-50)",
    ),
    "events": CollectionSpec(
        "events", "event",
        required=("stream", "seq", "source", "provenance", "kind"),
        # `ref{}` and `data{}` are GD-24's own spelling for this row, and both
        # are containers whose MEMBERS are open (the ref union's tail, GD-11)
        # while the container itself is not: `raw_paths` below declares
        # `data.custom`/`data.input`, which assumes `data` is a sub-document,
        # and `store.py` guarantees both are dicts on the file side. The pin is
        # what makes the server hold the same assumption.
        types={"stream": _STR, "seq": _INT, "ts": _DATE, "tsRaw": _STR, "source": _STR,
               "provenance": _STR, "kind": _STR, "refId": _STR,
               "ref": _OBJ, "data": _OBJ},
        # The full five-value enum, unlike its sibling mirror collections:
        # `events` is the projection of the `.touch/` WAL, which legally carries
        # `asserted` (agent-written) and `unknown` (legacy) lines. Pinning it to
        # {harness,derived} would reject the very records GD-28 exists to label.
        provenance=PROVENANCE,
        indexes=(index_def(("stream", 1), ("seq", 1), unique=True),
                 index_def(("kind", 1), ("ts", -1))),
        raw_paths=("data.custom", "data.input"),
        note="touch-events-v2 mirror; _id is byte-identical to store.cursor_key",
    ),
    "legacy_events": CollectionSpec(
        "legacy_events", "legacyEvent",
        required=("task", "lineNo", "provenance"),
        types={"task": _STR, "lineNo": _INT, "ts": _DATE, "tsRaw": _STR, "plan": _STR,
               "stage": _STR, "state": _STR, "detail": _STR, "title": _STR,
               "provenance": _STR},
        # GD-28's no-guess rule for the 12-of-130 already-unattributable lines:
        # `agent`/`tokens` shapes ⇒ derived, `title` ⇒ asserted, else unknown.
        # `harness` is impossible here and the pin says so.
        provenance=("derived", "asserted", "unknown"),
        indexes=(index_def(("task", 1), ("lineNo", 1)),
                 index_def(("task", 1), ("plan", 1), ("stage", 1))),
        note="positional _id; safe only because events.jsonl is never rewritten",
    ),
    "custom_state_events": CollectionSpec(
        "custom_state_events", "customStateEvent",
        required=("kind", "seq", "provenance"),
        # Same container pins as `events`, for the same reason: GD-24 names
        # `ref{}` and `data.custom{}` on this row, and `data.custom` is a
        # declared raw path — which is a statement about `data` being a
        # sub-document as much as about its member.
        types={"kind": _STR, "refId": _STR, "sessionKey": _STR, "seq": _INT,
               "ts": _DATE, "tsRaw": _STR, "author": _STR, "provenance": _STR,
               "stream": _STR, "ref": _OBJ, "data": _OBJ},
        provenance=("asserted", "touch"),
        indexes=(index_def(("refId", 1), ("seq", 1)), index_def(("kind", 1), ("ts", -1))),
        raw_paths=("data.custom",),
        note="append-only; the writer has no code path emitting harness (GD-28)",
    ),
    "custom_state": CollectionSpec(
        "custom_state", "customState",
        required=("refId", "kind", "seq", "provenance"),
        types={"refId": _STR, "kind": _STR, "seq": _INT, "derived": _BOOL,
               "fromSeq": _INT, "ts": _DATE, "tsRaw": _STR, "provenance": _STR},
        provenance=("asserted", "touch"),
        indexes=(index_def(("refId", 1)), index_def(("kind", 1))),
        accumulable=("seq",),
        raw_paths=("data.custom",),
        # `fromSeq` is `$set` and stays that way: it is part of the head's
        # payload, and the payload is written by `guarded_update` behind the
        # `{$lt: order}` precondition below — a losing event applies NOTHING, so
        # `$set` here is not write-order dependent, it is guard-order dependent,
        # which is the property R-52 asks for. Fencing it as accumulable would
        # break the only writer (`custom_state.head_write` `$set`s the payload
        # whole, which is the one shape that can replace a head completely) and
        # would buy nothing the guard does not already give.
        note="derived head (R-52): seq advances by $max, so the advance is idempotent "
             "and needs no filter; the head's PAYLOAD — fromSeq included — is written by "
             "guarded_update behind {seq:{$lt:newSeq}} so a late old event never "
             "clobbers a fresher head",
    ),
    "slots": CollectionSpec(
        "slots", "slot",
        required=("sessionKey", "root", "name", "attempt", "resolution"),
        types={"sessionKey": _STR, "root": _STR, "name": _STR, "parent": _STR,
               "role": _STR, "attempt": _INT, "agentId": _STR, "taskId": _STR,
               "runNode": _STR, "boundBy": _STR, "resolution": _STR,
               "pendingSince": _DATE},
        provenance=("derived", "touch"),
        indexes=(index_def(("agentId", 1), unique=True, sparse=True),
                 index_def(("sessionKey", 1), ("root", 1), ("name", 1), ("attempt", 1))),
        note="the single name<->agentId hop; orphaned is normal (R-53)",
    ),
    "derived": CollectionSpec(
        "derived", None,
        required=("reducerVersion", "derivedFromSeq"),
        types={"reducerVersion": _STR, "derivedFromSeq": _INT},
        provenance=("derived",),
        indexes=(),
        note="reducer-owned; dropped and rebuilt on version mismatch (GD-23)",
    ),
    "writers": CollectionSpec(
        "writers", "writer",
        required=("holderPid", "holderBoot", "leaseExpiresAt"),
        types={"holderPid": _INT, "holderBoot": _STR, "leaseExpiresAt": _DATE},
        indexes=(),
        note="GD-29 writer lease; _id only. Aggregator-internal bookkeeping, not "
             "mirrored content: deliberately EXEMPT from GD-28's provenance (there is "
             "no upstream writer to attribute). Acquired with guarded_update("
             "require={'leaseExpiresAt': {'$lt': now}}).",
    ),
    "cursors": CollectionSpec(
        "cursors", "cursor",
        required=("offset",),
        types={"offset": _INT, "stDev": _INT, "stIno": _INT, "size": _INT,
               "lastSeq": _INT, "gen": _INT, "updatedTs": _DATE},
        indexes=(),
        note="per-source watermark; the checkpoint identity R-23/SD-10 uses. Like "
             "`writers`, aggregator-internal and deliberately EXEMPT from GD-28: a "
             "cursor records where THIS process stopped reading, not who wrote a record.",
    ),
}


def collection_names():
    """Every collection in GD-24's table, sorted."""
    return tuple(sorted(COLLECTIONS))


def spec_for(collection) -> CollectionSpec:
    spec = COLLECTIONS.get(collection)
    if spec is None:
        raise SchemaError(
            f"unknown collection {collection!r} — GD-24's table is closed "
            f"({', '.join(collection_names())})"
        )
    return spec


def index_specs(collection):
    """Index definitions for ``collection`` as `{keys, options}` dicts."""
    return spec_for(collection).indexes


def json_schema(collection) -> dict:
    """The `$jsonSchema` validator for ``collection``.

    The single most load-bearing line is `_id: {bsonType: "string"}`: it is
    GD-24's opening law enforced *by the server*, so a mapper that ever hands a
    sub-document `_id` gets a write error instead of two documents that differ
    only by field order.

    Declared fields are pinned; undeclared ones are allowed through
    (`additionalProperties` stays open), because GD-11's union is open at the
    tail and a validator that rejects tomorrow's field would make the mirror the
    thing that blocks ingest.
    """
    spec = spec_for(collection)
    properties = {"_id": {"bsonType": _STR}}
    for field, bson_type in sorted(spec.types.items()):
        properties[field] = {"bsonType": bson_type}
    if spec.provenance:
        properties["provenance"] = {"bsonType": _STR, "enum": list(spec.provenance)}
    # `required` is projected, not merely declared: a rule the client checks and
    # the server does not is a rule that holds only for writers that went
    # through this module, and `mirror.py` is not the only thing that will ever
    # hold a client (rebuild tooling, the shell, a future backfill script).
    required = ["_id"] + [f for f in spec.required if f != "_id"]
    schema = {"bsonType": "object", "required": required, "properties": properties}
    return {"$jsonSchema": schema}


# --- variable-key subtrees (`_raw` wrapping) ------------------------------


def _has_own_hostile_key(value) -> bool:
    """True if ``value``'s **own** keys include a dotted or `$`-prefixed one.

    Own keys, not any descendant's: a hostile key deep inside must wrap the
    smallest sub-document that actually has it, or one dotted backup path would
    wrap an entire record body and make every ordinary field
    (`sessionId`, `type`, `timestamp`) unqueryable.
    """
    return isinstance(value, dict) and any(
        (not isinstance(key, str)) or "." in key or key.startswith("$") for key in value
    )


def wrap_raw(value, *, auto=False) -> dict:
    """Wrap a variable-key subtree as `{_raw: "<json>", …}`.

    CONVO-11's primary option — store the whole raw line as a BSON string — was
    declined (it doubles storage against the measured 0.53× parsed shape;
    CONVO-16). Byte fidelity is preserved where it is actually at risk instead:
    only the subtrees whose *keys* are data (`trackedFileBackups` is keyed by
    file path, tool `input` by whatever the tool defines). NUL bytes are legal
    in BSON string values, so only key shape needs the wrap.

    The encoding is compact JSON with key order preserved, which makes
    wrap→unwrap→wrap a byte-identical round trip and keeps the subtree readable
    in the shell.

    A subtree JSON cannot encode is a :class:`SchemaError` like every other
    refusal in this module — not the bare `TypeError` `json.dumps` raises. A
    drainer written as `except MongoStoreError:` (the whole reason the hierarchy
    exists) would miss that one and die on the tick. It is *not* encoded through
    `_json_default` instead: a Date tagged as `"!date:…"` would come back out of
    `unwrap_raw` as a string, and a round trip that silently changes a type is
    worse than a rejection. Today every declared `raw_path` is fed from
    `json.loads` output, so this is a wall for tomorrow's caller.
    """
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise SchemaError(
            f"{RAW_FIELD} subtree is not JSON-encodable: {exc} — wrap only the parsed "
            f"harness values the mirror actually stores"
        ) from None
    doc = {
        RAW_FIELD: text,
        RAW_ENCODING_FIELD: RAW_ENCODING,
        RAW_KEYS_FIELD: len(value) if isinstance(value, (dict, list)) else 0,
    }
    if auto:
        # An auto-wrap is a *declaration gap*, not a failure: the data is safe,
        # and the count of these is how the declared list gets extended before
        # anybody notices a query that cannot reach a field.
        doc[RAW_AUTO_FIELD] = True
    return doc


#: A wrapper's complete field set. Nothing else may ride inside one.
RAW_FIELDS = frozenset((RAW_FIELD, RAW_ENCODING_FIELD, RAW_KEYS_FIELD, RAW_AUTO_FIELD))


def is_raw_wrapper(value) -> bool:
    """True for a dict of **exactly** the wrapper's shape — nothing more.

    Recognised by shape rather than by the presence of two keys, because
    :func:`_check_keys` treats a wrapper as opaque and stops walking it: a dict
    that merely *carries* `_raw` and `_rawEncoding` alongside hostile sibling
    keys would otherwise be a way to smuggle a dotted or `$`-prefixed key past
    the one guard that exists to catch them. The wrapper is the module's only
    sanctioned escape hatch, so its door is narrow.
    """
    return (isinstance(value, dict)
            and isinstance(value.get(RAW_FIELD), str)
            and value.get(RAW_ENCODING_FIELD) == RAW_ENCODING
            and set(value) <= RAW_FIELDS)


def unwrap_raw(value):
    """Inverse of :func:`wrap_raw`; returns the original subtree.

    A wrapper that does not decode is a :class:`SchemaError`, not the bare
    `JSONDecodeError` `json.loads` raises, for `wrap_raw`'s reason and one
    stronger: this direction reads wrappers back **out of the database**
    (`--rebuild`, R-45), where a truncated or hand-edited `_raw` is not a
    programmer error in this process. :func:`is_raw_wrapper` only proves the
    field is a `str`, so the decode is the first thing that can meet bad bytes.
    """
    if not is_raw_wrapper(value):
        raise SchemaError(f"not a {RAW_FIELD} wrapper: {value!r}")
    try:
        return json.loads(value[RAW_FIELD])
    except ValueError as exc:
        raise SchemaError(
            f"{RAW_FIELD} wrapper does not decode: {exc} — the stored subtree is "
            f"truncated or was edited by hand"
        ) from None


def _path_matches(path, declared):
    """Path match with array indices transparent (`a.content.input` vs `a.content.2.input`)."""
    parts = [p for p in path if not isinstance(p, int)]
    return ".".join(parts) == declared


def _walk_wrap(node, declared, path, autowrap, report):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            child = path + [key]
            if any(_path_matches(child, d) for d in declared):
                out[key] = wrap_raw(value)
                report["declared"] += 1
            elif autowrap and _has_own_hostile_key(value):
                out[key] = wrap_raw(value, auto=True)
                report["auto"] += 1
                report["auto_paths"].append(".".join(str(p) for p in child))
            else:
                out[key] = _walk_wrap(value, declared, child, autowrap, report)
        return out
    if isinstance(node, list):
        return [_walk_wrap(item, declared, path + [i], autowrap, report)
                for i, item in enumerate(node)]
    return node


def prepare_document(collection, doc, *, autowrap=True):
    """Return ``(document, report)`` with variable-key subtrees `_raw`-wrapped.

    Declared paths (`CollectionSpec.raw_paths`) are wrapped unconditionally, so
    a field's stored shape does not depend on whether *this* instance happened
    to contain a dotted key. Anything else carrying dotted or `$`-prefixed keys
    is wrapped too when ``autowrap`` (the default) and marked `_rawAuto` — the
    mirror must never drop data because a declaration list was incomplete, and
    the marker is how the gap gets found. With ``autowrap=False`` such a subtree
    is left in place, and :func:`validate_document` then rejects the document —
    which is the arm the tests use to prove the rejection exists.
    """
    spec = spec_for(collection)
    report = {"declared": 0, "auto": 0, "auto_paths": []}
    if not isinstance(doc, dict):
        raise SchemaError("a document must be a dict")
    return _walk_wrap(doc, spec.raw_paths, [], autowrap, report), report


def _check_keys(node, path):
    if isinstance(node, dict):
        if is_raw_wrapper(node):
            return                                  # declared wrapper: opaque by design
        for key, value in node.items():
            if not isinstance(key, str):
                raise SchemaError(f"non-string key at {'.'.join(path) or '<root>'}: {key!r}")
            if key.startswith("$") or "." in key:
                raise SchemaError(
                    f"dotted or $-prefixed key {key!r} at {'.'.join(path) or '<root>'} "
                    f"outside a declared {RAW_FIELD} wrapper (R-44/MONGOSCHEMA-8)"
                )
            _check_keys(value, path + [key])
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _check_keys(item, path + [str(i)])


_PY_TYPES = {
    "string": str,
    "bool": bool,
    "date": datetime.datetime,
    "array": list,
    "object": dict,
}


def _type_ok(value, bson_type):
    types = bson_type if isinstance(bson_type, list) else [bson_type]
    for name in types:
        if name == "null":
            # `$jsonSchema` treats null as a type, not as absence — so a field
            # that may legally be null says so, in the pin, explicitly.
            if value is None:
                return True
        elif name in ("int", "long"):
            if isinstance(value, int) and not isinstance(value, bool):
                return True
        elif name == "bool":
            if isinstance(value, bool):
                return True
        elif name == "double":
            if isinstance(value, float):
                return True
        else:
            py = _PY_TYPES.get(name)
            if py is not None and isinstance(value, py) and not isinstance(value, bool):
                return True
    return False


def check_id(collection, key) -> str:
    """Assert ``key`` is an `_id` this collection's grammar can produce (SD-11).

    Parses the key back through `refs.parse_ref_key` and rebuilds it: equality
    proves it came from `refs.ref_key` — right escaping, right padding, right
    kind. This is the executable form of "all `_id`s are strings from
    `refs.ref_key`", which is otherwise a rule every mapper is trusted to
    remember. Collections whose ids are reducer-owned (`derived`) are exempt by
    declaration, not by accident.

    A collection may declare several kinds (`sessions` is GD-24's one tagged
    union); the key is accepted if **any** of them produces it, and a rejection
    reports what each one made of it, because "not a session key" is useless
    when there are two session grammars.
    """
    spec = spec_for(collection)
    if not isinstance(key, str) or not key:
        raise SchemaError(f"{collection}: _id must be a non-empty string (GD-24)")
    if not spec.id_kinds:
        return key
    reasons = []
    for kind in spec.id_kinds:
        try:
            rebuilt = refs.ref_key(refs.parse_ref_key(kind, key))
        except refs.RefError as exc:
            reasons.append(f"{kind}: {exc}")
            continue
        if rebuilt == key:
            return key
        reasons.append(f"{kind}: not canonical (refs.ref_key would emit {rebuilt!r})")
    raise SchemaError(
        f"{collection}: _id {key!r} is not a canonical "
        f"{' | '.join(spec.id_kinds)} key — every _id comes from refs.ref_key "
        f"(SD-11/GD-24): " + "; ".join(reasons)
    )


def validate_document(collection, doc):
    """Validate a prepared document against GD-24's pins. Returns ``doc``.

    Checks, in the order a failure is most likely: `_id` present, a **string**
    (GD-24's opening law, mirrored client-side so a mapper bug fails in the test
    suite and not only against a live server) and canonical for the collection's
    kind; no dotted/`$` keys outside a declared wrapper; declared fields carry
    their pinned BSON type; `provenance` inside the collection's enum; required
    fields present.
    """
    spec = spec_for(collection)
    if not isinstance(doc, dict):
        raise SchemaError("a document must be a dict")
    if "_id" not in doc:
        raise SchemaError(f"{collection}: document has no _id")
    if not isinstance(doc["_id"], str):
        raise SchemaError(
            f"{collection}: _id must be a string produced by refs.ref_key, got "
            f"{type(doc['_id']).__name__} — a sub-document _id is field-order "
            f"sensitive and inserts as two documents (GD-24)"
        )
    check_id(collection, doc["_id"])
    _check_keys({k: v for k, v in doc.items() if k != "_id"}, [])
    for field, bson_type in spec.types.items():
        # Presence, not truthiness, and no escape for an explicit `null`:
        # `$jsonSchema` applies a `bsonType` pin to a property whenever it is
        # PRESENT, and `null` has bsonType "null", which fails an
        # `["int","long"]` pin. Skipping None here would make this validator
        # disagree with the one this module installs on the server — the client
        # would pass a document the server then refuses, which is the worst
        # possible split (it fails at write time, in the mirror, off the test
        # path). A field that may legally be null pins `"null"` explicitly.
        if field in doc and not _type_ok(doc[field], bson_type):
            raise SchemaError(
                f"{collection}.{field} must be bsonType {bson_type}, got "
                f"{type(doc[field]).__name__} (GD-24 type pin) — an explicit null "
                f"is a value the server's $jsonSchema refuses too; omit the field"
            )
    if spec.provenance is not None and "provenance" in doc:
        if doc["provenance"] not in spec.provenance:
            raise SchemaError(
                f"{collection}.provenance must be one of {list(spec.provenance)} "
                f"(GD-28), got {doc['provenance']!r}"
            )
    missing = [f for f in spec.required if f not in doc]
    if missing:
        raise SchemaError(f"{collection}: missing required field(s) {missing}")
    return doc


# --- size guard -----------------------------------------------------------


def document_size(doc) -> int:
    """Approximate stored size of ``doc`` in bytes.

    Compact UTF-8 JSON, used as a proxy for the BSON encoding so the guard works
    with pymongo absent (GD-21). The two differ by framing bytes and by numeric
    width, on the order of a few percent — irrelevant against a threshold set at
    half the hard limit, and the exact encoded size is checked by the server
    anyway. Dates serialize through :func:`_json_default`.

    A value the proxy cannot encode is a :class:`SchemaError` like every other
    refusal here (`wrap_raw`'s rule, applied to the other direction): a value
    Mongo could not store either must not reach `mirror.py`'s drainer — written
    as `except MongoStoreError:` — as a bare `TypeError` that kills the tick.
    """
    try:
        return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":"),
                              default=_json_default).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"document is not storable: {exc}") from None


def guard_oversize(collection, doc, *, source_path=None, byte_offset=None,
                   limit=OVERSIZE_LIMIT):
    """Return ``(doc, size)`` unchanged, or a stub above ``limit`` (R-44).

    A stub keeps the `_id` and the collection's key fields, so the document
    still joins, still sorts and still shows up in a count — the record is
    *marked*, never dropped. Provenance and `ts` ride along for the same reason.

    An absent `source_path`/`byte_offset` is **omitted**, never written as
    `null`: `byteOffset` is pinned to `["int","long"]` on `records` and
    `stream_meta`, and a present null fails that pin on the server. Writing one
    would have inverted R-44's rule — "oversize ⇒ stub, never silently dropped"
    — into "oversize ⇒ guaranteed write rejection", making the guard that exists
    to keep the record the thing that loses it.
    """
    size = document_size(doc)
    if size <= limit:
        return doc, size
    spec = spec_for(collection)
    keep = ("_id", "provenance", "ts", "tsRaw", "gen") + spec.required
    stub = {k: doc[k] for k in keep if k in doc}
    stub.update({"oversize": True, "bytes": size})
    stub.update({k: v for k, v in (("sourcePath", source_path),
                                   ("byteOffset", byte_offset)) if v is not None})
    return stub, size


# --- timestamps -----------------------------------------------------------

_TS_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ")


def ts_fields(ts) -> dict:
    """`{"ts": <BSON Date>, "tsRaw": "<original string>"}` (GD-11(g)).

    The aggregator supplies every timestamp; this module has no clock and no
    default. `tsRaw` keeps the source's own spelling — legacy streams carry
    mixed formats (RUNSTATE-6) and a normalized Date is a lossy answer to
    "what did the file say".
    """
    if isinstance(ts, datetime.datetime):
        moment = ts if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc)
        raw = moment.astimezone(datetime.timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
    elif isinstance(ts, str) and ts.strip():
        raw = ts
        text = ts.strip()
        moment = None
        for fmt in _TS_FORMATS:
            try:
                moment = datetime.datetime.strptime(text, fmt).replace(
                    tzinfo=datetime.timezone.utc)
                break
            except ValueError:
                continue
        if moment is None:
            try:
                moment = datetime.datetime.fromisoformat(
                    (text[:-1] + "+00:00") if text.endswith("Z") else text)
            except ValueError:
                raise SchemaError(f"unparseable ts {ts!r}") from None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
    else:
        raise SchemaError(f"ts must be a datetime or a non-empty string, got {ts!r}")
    # BSON Date is millisecond-resolution; truncating here rather than letting
    # the driver do it keeps the in-memory model and the server byte-equal
    # (fingerprints are compared across the two).
    moment = moment.astimezone(datetime.timezone.utc)
    moment = moment.replace(microsecond=(moment.microsecond // 1000) * 1000)
    return {"ts": moment, "tsRaw": raw}


# --- the update algebra ---------------------------------------------------


def op_set(fields) -> dict:
    return {"$set": dict(fields)}


def op_max(fields) -> dict:
    return {"$max": dict(fields)}


def op_min(fields) -> dict:
    return {"$min": dict(fields)}


def op_add_to_set(fields) -> dict:
    return {"$addToSet": dict(fields)}


def op_set_on_insert(fields) -> dict:
    """Immutables, written only by the operation that creates the document.

    **Every** operation targeting a given `_id` must carry the *same*
    `$setOnInsert` payload. It is the one operator in the algebra that is
    order-dependent by construction — whichever operation arrives first wins,
    and if only some of them carry the immutables, the document's contents
    depend on ingest order. That is exactly what GD-25's shuffled/reversed pass
    detects, and `tests/test_mongo_store.py` asserts the detection works by
    building the inconsistent case on purpose.
    """
    return {"$setOnInsert": dict(fields)}


def _conflicting_path(field, seen):
    """The already-seen field ``field`` collides with, or None.

    Mongo's rule is about *paths*, not names: `$set:{spawn: …}` together with
    `$max:{"spawn.b": …}` is "Updating the path 'spawn.b' would create a
    conflict at 'spawn'", and comparing names alone waves that through. So a
    field conflicts with any seen field that is equal to it or is a dotted
    prefix of it in either direction — `spawn` vs `spawn.fileHint` is the
    realistic instance, since GD-24 gives `agents` a
    `spawn{recordUuid,toolUseId,fileHint}` sub-document.
    """
    for other in seen:
        if field == other or field.startswith(other + ".") or other.startswith(field + "."):
            return other
    return None


def merge_ops(*ops, collection=None) -> dict:
    """Merge op builders into one update document.

    Merging is where the "conflict at path" class of Mongo errors is born, so
    the same field twice — under one operator with different values, under two
    operators at all, or once as a sub-document and once as a path inside it —
    is refused here rather than at the server, where it would surface as one
    failed write in an unordered bulk of five hundred.

    That conflict rule is **all** this function enforces. The rest of GD-25 —
    the forbidden operators and the `$set` fence over a collection's accumulable
    fields — is :func:`validate_update`'s, and it runs on every real path
    (:func:`apply_update`, :func:`bulk_upsert`, :func:`guarded_update`) whether a
    mapper asked for it or not. Mappers call this one directly, so pass
    ``collection=`` to get the whole check here instead of one call later.
    """
    merged = {}
    seen = {}
    for op in ops:
        if not op:
            continue
        for operator, fields in op.items():
            for field, value in fields.items():
                other = _conflicting_path(field, seen)
                if other is not None:
                    raise OperatorError(
                        f"field {field!r} conflicts with {other!r} (under {seen[other]} "
                        f"and {operator}) — Mongo rejects the conflicting path, and two "
                        f"writers of one field is a mapper bug either way"
                    )
                seen[field] = operator
            merged.setdefault(operator, {}).update(fields)
    if collection is not None:
        validate_update(merged, collection)
    return merged


def _top(field):
    return field.split(".", 1)[0]


def _positional_component(field):
    """The all-digit path component of ``field``, or None.

    `sources.0.present` is a legal *server* update — mongod indexes into the
    array and writes the element. It is not part of this algebra, and the
    asymmetry is the reason: :func:`apply_update` is GD-25's oracle, and
    `_set_path` deliberately refuses to descend into a list (a model more
    permissive than the server would certify a fingerprint mongod cannot
    reproduce, so it errs the other way). Accepting the path in
    :func:`validate_update` while refusing it in :func:`apply_update` would
    split one write path in half: `bulk_upsert` would send to the wire an
    update the memory pass cannot replay, and `--rebuild`'s comparison (R-45)
    plus the GD-25 acceptance test would silently stop covering that mapper.

    So the two halves agree here, at the door, and they agree on "no": array
    elements are revised by rewriting the set (`$addToSet` of a new identity)
    or by moving the mutable state into a keyed sub-document — which is what
    `sessions.sourceState` is, and why it exists.
    """
    for part in field.split("."):
        if part.isdigit():
            return part
    return None


def validate_update(update, collection=None, *, _id=None) -> dict:
    """Validate an update document against GD-25's algebra. Returns it.

    Rejects: any operator outside :data:`ALLOWED_OPS` (with `$inc` and the
    delete verbs named individually so the message carries the reason), a field
    under two operators or two conflicting paths, `$set` on a field the
    collection declares accumulable, and dotted/`$` keys inside a value that is
    not a declared wrapper.

    A named ``collection`` goes through :func:`spec_for`, so a typo'd name
    (`"usagez"`, `"run_node"`) raises instead of *silently disabling* the
    accumulable fence — GD-24's table is closed, and an unrecognised name is
    the wrong-target hazard GD-12 exists for, not a collection without rules.

    ``_id``, when the caller knows the key it is upserting on, is checked
    against `$setOnInsert._id`. That operator is the one place `_id` may legally
    be written, and mongod refuses an upsert whose filter and `$setOnInsert`
    disagree (*"Performing an update on the path '_id' would modify the
    immutable field '_id'"*, verified against mongod 7). Left unchecked, the
    memory model would quietly resolve the disagreement in the filter's favour
    and fingerprint a document the server refused to write.
    """
    if not isinstance(update, dict) or not update:
        raise OperatorError("an update must be a non-empty dict of operators")
    spec = spec_for(collection) if collection is not None else None
    seen = {}
    for operator, fields in update.items():
        if operator in FORBIDDEN_OPS:
            raise OperatorError(f"{operator} is forbidden: {FORBIDDEN_OPS[operator]}")
        if operator not in ALLOWED_OPS:
            raise OperatorError(
                f"{operator!r} is not part of the algebra — allowed: {list(ALLOWED_OPS)} (GD-25)"
            )
        if not isinstance(fields, dict) or not fields:
            raise OperatorError(f"{operator} must carry a non-empty field map")
        for field, value in fields.items():
            if not isinstance(field, str) or not field or field.startswith("$"):
                raise OperatorError(f"{operator}: unusable field name {field!r}")
            if field == "_id" and operator != "$setOnInsert":
                raise OperatorError("_id is immutable; it may only be $setOnInsert")
            positional = _positional_component(field)
            if positional is not None:
                raise OperatorError(
                    f"{operator}: positional array path {field!r} (component "
                    f"{positional!r}) is not part of the algebra — the memory model is "
                    f"GD-25's oracle and cannot replay one, so accepting it here would "
                    f"send to the wire an update `apply_operations` refuses. Revise a "
                    f"set by adding a new element, or keep the mutable state in a keyed "
                    f"sub-document (sessions.sourceState is that shape)"
                )
            if field == "_id" and _id is not None and value != _id:
                raise OperatorError(
                    f"$setOnInsert._id {value!r} disagrees with the key being upserted "
                    f"{_id!r} — mongod answers \"Performing an update on the path '_id' "
                    f"would modify the immutable field '_id'\" and fails the write "
                    f"(GD-24: the key is the identity)"
                )
            other = _conflicting_path(field, seen)
            if other is not None:
                raise OperatorError(
                    f"field {field!r} conflicts with {other!r} (under {seen[other]} "
                    f"and {operator}) — Mongo refuses the conflicting path"
                )
            seen[field] = operator
            if spec is not None and operator == "$set" and _top(field) in spec.accumulable:
                raise OperatorError(
                    f"$set on accumulable field {field!r} of {collection} — it is "
                    f"write-order dependent; use $max/$min/$addToSet (GD-25/SD-11)"
                )
            if operator == "$addToSet" and isinstance(value, dict) and "$each" in value:
                if set(value) != {"$each"} or not isinstance(value["$each"], list):
                    raise OperatorError("$addToSet accepts a value or {'$each': [...]}")
                for item in value["$each"]:
                    _check_keys({"v": item}, [field])
            else:
                _check_keys({"v": value}, [field])
    return update


#: Mongo's canonical type ordering, restricted to the types Touch stores. It is
#: reproduced (rather than assumed away) because `$max` on a field that changed
#: type — a `null` that became a Date — must resolve the same way in the memory
#: model as on the server, or the two fingerprints diverge on exactly the
#: records nobody thought about.
_TYPE_RANK = ((type(None), 1), (bool, 8), (int, 2), (float, 2), (str, 3),
              (dict, 4), (list, 5), (datetime.datetime, 9))


def _rank(value):
    for py_type, rank in _TYPE_RANK:
        if isinstance(value, py_type):
            return rank
    return 6


def _compare(left, right):
    """-1/0/1 in BSON canonical order (numbers < strings < … < dates)."""
    lr, rr = _rank(left), _rank(right)
    if lr != rr:
        return -1 if lr < rr else 1
    if isinstance(left, datetime.datetime) and left.tzinfo is None:
        left = left.replace(tzinfo=datetime.timezone.utc)
    if isinstance(right, datetime.datetime) and right.tzinfo is None:
        right = right.replace(tzinfo=datetime.timezone.utc)
    if left == right:
        return 0
    try:
        return -1 if left < right else 1
    except TypeError:
        # Same rank, incomparable (two dicts): fall back to canonical text so
        # the result is at least total and deterministic.
        return -1 if _canonical_text(left) < _canonical_text(right) else 1


def _get_path(doc, field):
    node = doc
    for part in field.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def _set_path(doc, field, value):
    """Write ``value`` at a dotted path, creating only *absent* sub-documents.

    A path whose intermediate already holds a scalar is refused, because mongod
    refuses it: `{spawn: 5}` updated with `$max:{"spawn.b": 1}` answers *"Cannot
    create field 'b' in element {spawn: 5}"* and fails the whole update.
    Overwriting the scalar with `{b: 1}` here — the obvious, wrong, convenient
    thing — would make this model MORE permissive than the server it is the
    oracle for (GD-25), so the acceptance test would certify a fingerprint no
    mongod can reproduce. The document is never left half-written: the caller
    works on a copy and the exception propagates before it is stored.
    """
    parts = field.split(".")
    node = doc
    for depth, part in enumerate(parts[:-1]):
        if part in node:
            nxt = node[part]
            if not isinstance(nxt, dict):
                raise OperatorError(
                    f"cannot create field {parts[depth + 1]!r} in element "
                    f"{'.'.join(parts[:depth + 1])!r}: it holds a "
                    f"{type(nxt).__name__}, not a sub-document — mongod refuses the "
                    f"whole update rather than replacing the value"
                )
        else:
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def apply_update(doc, update, *, _id=None, collection=None) -> dict:
    """Apply ``update`` to ``doc`` (None ⇒ upsert-insert). Returns the new doc.

    The in-memory twin of the server's upsert, and the whole reason GD-25's
    acceptance test needs no database: `$max`/`$min` compare in BSON canonical
    order, `$addToSet` appends only what is not deep-equal already present, and
    `$setOnInsert` fires only on the insert. Pure — ``doc`` is not mutated.
    """
    validate_update(update, collection, _id=_id)
    inserting = doc is None
    out = {} if inserting else _deep_copy(doc)
    for field, value in update.get("$setOnInsert", {}).items():
        if inserting:
            _set_path(out, field, _deep_copy(value))
    for field, value in update.get("$set", {}).items():
        _set_path(out, field, _deep_copy(value))
    for field, value in update.get("$max", {}).items():
        current, present = _get_path(out, field)
        if not present or _compare(value, current) > 0:
            _set_path(out, field, _deep_copy(value))
    for field, value in update.get("$min", {}).items():
        current, present = _get_path(out, field)
        if not present or _compare(value, current) < 0:
            _set_path(out, field, _deep_copy(value))
    for field, value in update.get("$addToSet", {}).items():
        items = value["$each"] if isinstance(value, dict) and "$each" in value else [value]
        current, present = _get_path(out, field)
        if present and not isinstance(current, list):
            # Same rule as `_set_path`'s, in the other operator that can meet a
            # value of the wrong shape: mongod answers "Cannot apply $addToSet to
            # non-array field" and fails the update rather than replacing it.
            raise OperatorError(
                f"$addToSet on {field!r}, which holds a {type(current).__name__} and "
                f"not an array — mongod refuses the whole update"
            )
        array = list(current) if present else []
        # BSON equality, not Python's: the server compares sub-documents
        # field-by-field IN ORDER, so `{"a":1,"b":2}` and `{"b":2,"a":1}` are
        # two distinct elements of a `$addToSet` set. Python's `==` says they
        # are one. Deduping with `item not in array` would therefore have made
        # this model *more* permissive than mongod in the one operator whose
        # entire job is set semantics — and `sessions.sources` is both a set
        # field and a sub-document array (GD-26's `sources[].present:false`),
        # so the divergence is reachable, not theoretical.
        seen = {_bson_identity(item) for item in array}
        for item in items:
            identity = _bson_identity(item)
            if identity not in seen:
                seen.add(identity)
                array.append(_deep_copy(item))
        _set_path(out, field, array)
    if _id is not None:
        out["_id"] = _id
    return out


def _deep_copy(value):
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def apply_operations(state, operations):
    """Apply ``(collection, _id, update)`` triples to an in-memory ``state``.

    ``state`` is ``{collection: {_id: doc}}`` — the shape :func:`fingerprint`
    and :func:`counts` read. Returns it, mutated, so a caller can fold several
    passes together (which is what a replay is).
    """
    for collection, key, update in operations:
        check_id(collection, key)
        bucket = state.setdefault(collection, {})
        bucket[key] = apply_update(bucket.get(key), update, _id=key, collection=collection)
    return state


# --- fingerprint ----------------------------------------------------------


def _json_default(value):
    """Encode a Date; refuse anything else — inside the module's own hierarchy.

    A :class:`SchemaError` rather than `json.dumps`' bare `TypeError`, so the
    three public functions that reach this encoder (:func:`document_size`,
    :func:`guard_oversize` through it, :func:`fingerprint`) all fail the way
    `wrap_raw` does and a drainer written as `except MongoStoreError:` catches
    every one of them.
    """
    if isinstance(value, datetime.datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
        return "!date:" + moment.astimezone(datetime.timezone.utc).isoformat(
            timespec="milliseconds")
    raise SchemaError(
        f"unstorable value of type {type(value).__name__}: {value!r} — Mongo has no "
        f"BSON encoding for it either"
    )


def _canonical_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=_json_default)


def _bson_text(value):
    """Like :func:`_canonical_text` but with key order **preserved**.

    That is BSON's own notion of equality for a sub-document, which is what
    `$addToSet` dedupes on and what `{s,n}` vs `{n,s}` is about (GD-24).
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      default=_json_default)


def _numeric_normal(value):
    """``value`` with integral floats folded onto ints, recursively.

    BSON equality compares numbers **across** its numeric types: `1` (int32)
    and `1.0` (double) are one value to mongod, so `$addToSet` treats a second
    `{n: 1.0}` as already present when `{n: 1}` is. Their JSON spellings differ,
    so :func:`_bson_text` alone called them two elements — the direction
    `_set_path`'s docstring names a defect, since a model more permissive than
    the server certifies a fingerprint no mongod can reproduce. Reachable
    wherever a harness JSON value is sometimes written `1` and sometimes `1.0`
    inside a set field (`fragments`, `sources`).

    Only *dedup identity* is normalized, never the stored value and never the
    fingerprint: pymongo encodes a Python int as int32/int64 and a float as a
    double, so what comes back off the wire is the type that was written, and
    the oracle must keep seeing that difference. Booleans are left alone —
    `true` and `1` are NOT equal in BSON, and `bool` is an `int` in Python.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _numeric_normal(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_numeric_normal(item) for item in value]
    return value


def _bson_identity(value):
    """A hashable stand-in for BSON equality; total for anything storable."""
    try:
        return ("j", _bson_text(_numeric_normal(value)))
    except (TypeError, ValueError, SchemaError):
        # Not JSON-encodable (and so not something the mirror can store either).
        # Still needs a deterministic identity rather than an exception here,
        # because the rejection belongs to validate_document, not to dedup.
        # `SchemaError` is in the list because `_json_default` now raises one:
        # dedup must not start throwing just because the refusal moved into the
        # module's hierarchy.
        return ("r", repr(value))


def _canonical_doc(collection, doc):
    spec = COLLECTIONS.get(collection)
    out = {}
    for key, value in doc.items():
        if spec is not None and key in spec.set_fields and isinstance(value, list):
            # Sorted by the order-insensitive text first (so a sub-document
            # whose fields arrived in a different order still sorts to the same
            # place across a memory pass and a read-back from the server), with
            # the order-SENSITIVE text as the tie-break, so two elements that
            # BSON keeps distinct never sort ambiguously.
            value = sorted(value, key=lambda item: (_canonical_text(item), _bson_text(item)))
        out[key] = value
    return out


def fingerprint(state) -> str:
    """sha256 over every document, sorted by `(collection, _id)` — GD-25's oracle.

    Arrays declared as `$addToSet` sets are sorted before hashing; every other
    array keeps its order, so a real ordering regression (`fragments[]` stitched
    by directory order instead of the `parentUuid → uuid` chain) still shows up.
    Datetimes hash through a tagged ISO form at millisecond resolution, which is
    BSON Date's own resolution — that is what lets the same fingerprint be
    computed over documents read back from a live mongod.

    A document carrying a value neither JSON nor BSON can encode raises a
    :class:`SchemaError` that **names it** (`collection` + `_id`): the oracle
    runs over thousands of documents, and "unstorable value of type set" with no
    address is a bug report nobody can act on.
    """
    digest = hashlib.sha256()
    for collection in sorted(state):
        digest.update(("\x1e" + collection + "\x1e").encode("utf-8"))
        bucket = state[collection]
        for key in sorted(bucket):
            try:
                text = _canonical_text(_canonical_doc(collection, bucket[key]))
            except (TypeError, ValueError) as exc:
                raise SchemaError(f"{collection} {key!r}: not fingerprintable: {exc}") from None
            except SchemaError as exc:
                raise SchemaError(f"{collection} {key!r}: {exc}") from None
            digest.update(text.encode("utf-8"))
            digest.update(b"\x1f")
    return digest.hexdigest()


def counts(state) -> dict:
    """`{collection: n}` — the assertion that catches *silent collapse*.

    MONGOSCHEMA-16's probe lost 142 of 333 uuid-less records to a content-hash
    key while the fingerprint stayed stable, because a fingerprint of fewer
    documents is still a fingerprint. Counts are the other half of the test.
    """
    return {name: len(bucket) for name, bucket in sorted(state.items())}


# --- pymongo-facing (lazy import, GD-21) ----------------------------------


def pymongo_available() -> bool:
    """True if pymongo can be imported. Never raises (GD-21: absence degrades)."""
    try:
        import pymongo  # noqa: F401
    except Exception:
        return False
    return True


def client_options(**overrides) -> dict:
    """:data:`CLIENT_OPTIONS` with explicit overrides — one source of truth."""
    options = dict(CLIENT_OPTIONS)
    options.update(overrides)
    return options


def open_client(uri, **overrides):
    """A **synchronous** client for schema bootstrap, rebuild tooling and tests.

    The live path uses `AsyncMongoClient` inside the one asyncio process and is
    `mirror.py`'s (GD-21/R-45) — pymongo is blocking unless the async client is
    used, and nothing here may be called from the poll loop. Raises
    :class:`MongoUnavailable` when pymongo is absent, so callers degrade rather
    than crash.
    """
    try:
        from pymongo import MongoClient
    except ImportError as exc:
        raise MongoUnavailable(f"pymongo is not installed: {exc}") from None
    if not uri:
        raise MongoUnavailable("no Mongo URI (TOUCH_MONGO_URI / .touch/mongo.json)")
    return MongoClient(uri, **client_options(**overrides))


def ping(client) -> bool:
    """True if the server answers within the GD-21 timeouts. Never raises."""
    try:
        client.admin.command("ping")
    except Exception:
        return False
    return True


def ensure_schema(db, *, collections=None, validate=True):
    """Create/repair collections, validators and indexes. Idempotent.

    Returns a per-collection report. Existing collections are updated with
    `collMod` rather than dropped — this function must be safe to run against a
    populated mirror on every boot.
    """
    try:
        from pymongo import ASCENDING, DESCENDING
        from pymongo.errors import OperationFailure
    except ImportError as exc:
        raise MongoUnavailable(f"pymongo is not installed: {exc}") from None
    direction = {1: ASCENDING, -1: DESCENDING}
    names = list(collections or collection_names())
    existing = set(db.list_collection_names())
    report = {}
    for name in names:
        spec = spec_for(name)
        schema = json_schema(name) if validate else None
        if name not in existing:
            options = {"validator": schema, "validationLevel": "strict",
                       "validationAction": "error"} if schema else {}
            db.create_collection(name, **options)
        elif schema:
            try:
                db.command({"collMod": name, "validator": schema,
                            "validationLevel": "strict", "validationAction": "error"})
            except OperationFailure as exc:                     # pragma: no cover
                report.setdefault(name, {})["collModError"] = str(exc)
        created = []
        for index in spec.indexes:
            keys = [(field, direction[order]) for field, order in index["keys"]]
            created.append(db[name].create_index(keys, **index["options"]))
        # `create_index` is additive, so GD-26's "no TTL index on any Touch
        # collection, ever" is only enforced over the definitions *this* module
        # emits — an index added by an older version, by hand, or by a well-meant
        # shell session survives every boot invisibly. Read them back and refuse:
        # a TTL here silently re-imports the CLI's own destruction of history,
        # which is the exact thing the mirror exists to prevent.
        stray = [i.get("name") for i in db[name].list_indexes() if "expireAfterSeconds" in i]
        if stray:
            raise SchemaError(
                f"{name}: index(es) {stray} carry expireAfterSeconds — no TTL index on "
                f"any Touch collection, ever (GD-26). Drop it before starting the mirror."
            )
        report[name] = dict(report.get(name, {}), indexes=created)
    return report


def _is_identity_dup(item) -> bool:
    """True if this duplicate-key item is a dup on an IDENTITY index.

    The driver hands the violated index's key pattern through on every 11000
    (`keyPattern: {'agentId': 1}`, `errmsg: … index: agentId_1 …`), so the
    distinction :data:`IDENTITY_INDEXES` draws is a fact about the error and
    not a guess. When the pattern is missing entirely — an older server, a
    hand-built error object — the item is read as an identity dup, because
    `_id` is the only unique index most collections have and the conservative
    reading of an unlabelled duplicate is the common one.
    """
    pattern = item.get("keyPattern")
    if not isinstance(pattern, dict) or not pattern:
        return True
    return tuple(pattern) in IDENTITY_INDEXES


def split_write_errors(error) -> dict:
    """A `BulkWriteError`'s items, split three ways. Pure; never raises.

    Returns `{"tolerated": [...], "conflicts": [...], "fatal": [...]}`, each a
    list of the driver's own write-error items with `keyPattern`/`errmsg`
    intact — which is the half :func:`classify_write_errors` cannot express,
    since its tolerated side is an `int` and an integer cannot say *which*
    index refused the write.

    * **tolerated** — 11000 on `_id` or on `{stream, seq}`: GD-29's two
      readings, healthy replay and a racing second writer.
    * **conflicts** — 11000 on any other unique index. The document was
      **rejected**; the write did not happen. Today that is `slots.agentId`
      (unique sparse), where a duplicate is R-53's conflict signal: two slots
      claiming one agent, a normal and renderable outcome rather than a
      writer-topology fault.
    * **fatal** — everything else, unchanged.

    Nothing is swallowed on any of the three paths (R-44: `writeErrors` of an
    unordered bulk are ALWAYS inspected and surfaced) — the split decides what
    a caller is told the failure *means*, never whether it is told at all.
    """
    details = getattr(error, "details", None) or {}
    tolerated, conflicts, fatal = [], [], []
    for item in details.get("writeErrors") or []:
        if item.get("code") != DUPLICATE_KEY:
            fatal.append(item)
        elif _is_identity_dup(item):
            tolerated.append(item)
        else:
            conflicts.append(item)
    return {"tolerated": tolerated, "conflicts": conflicts, "fatal": fatal}


def classify_write_errors(error):
    """Split a `BulkWriteError` into (duplicate-key count, fatal errors).

    GD-29: duplicate-key means two different things — idempotent replay landing
    on its own output (healthy, a burst of these at startup is normal) and two
    live writers racing one stream (a bug). So it is never swallowed: it is
    counted, returned, and `/health` publishes the steady-state number.

    The count is **every** 11000, conflicts included, and that is a compatibility
    contract rather than a simplification: `mirror.MongoBackend` unpacks this
    pair for its async twin of `bulk_upsert`, and `custom_state.bind_slot`
    (R-53) reads the resulting `tolerated_dups` as "the agentId claim was
    refused, so write the conflict document". Use :func:`split_write_errors`
    when the *meaning* matters — it returns the items themselves, keyPattern
    included, and `bulk_upsert` surfaces both readings side by side.
    """
    split = split_write_errors(error)
    return len(split["tolerated"]) + len(split["conflicts"]), split["fatal"]


def _no_writes(**extra) -> dict:
    """The zero result, in ONE place so every exit reports the same key set."""
    result = {"matched": 0, "upserted": 0, "modified": 0, "tolerated_dups": 0,
              "identity_dups": 0, "tolerated": [], "conflicts": [], "errors": []}
    result.update(extra)
    return result


def _sync_result(value, collection, action):
    """``value`` if the driver was synchronous; otherwise a hierarchy error.

    See :class:`AsyncClientError`. The coroutine is closed before raising, so an
    `AsyncMongoClient` handed to a sync shape does not additionally leave a
    "coroutine was never awaited" RuntimeWarning behind the real message.
    """
    if hasattr(value, "__await__"):
        close = getattr(value, "close", None)
        if callable(close):
            close()
        raise AsyncClientError(
            f"{collection}: {action} got an awaitable back — this is the SYNCHRONOUS "
            f"write shape and it was handed an async handle (GD-21's AsyncMongoClient). "
            f"The async twin is mirror.MongoBackend (R-45); a sync client here, or "
            f"asyncio.to_thread around this call, are the other two answers"
        )
    return value


def bulk_upsert(db, collection, operations, *, ordered=False):
    """Upsert ``(_id, update)`` pairs, ALWAYS inspecting `writeErrors` (R-44).

    Returns `{"matched", "upserted", "modified", "tolerated_dups",
    "identity_dups", "tolerated", "conflicts", "errors"}`.
    An unordered bulk that half-fails returns success to a caller who does not
    read `writeErrors`; that is the failure mode this signature exists to make
    impossible.

    **Three readings of a duplicate key, not two.** `tolerated_dups` is every
    11000 — the number `custom_state.bind_slot` reads as "the claim was
    refused". `identity_dups` is the subset on `_id`/`{stream,seq}`, which is
    the number GD-29 means when it says a burst at startup is healthy and a
    nonzero steady state is a second writer or a key bug. `conflicts` carries
    the rest as the driver's own items, `keyPattern` intact, because a duplicate
    on `slots.agentId` is a rejected write whose *reason* — which index, which
    value — is what R-53 renders (see :func:`split_write_errors`).

    **Driver mode: this function is synchronous.** It reads `matched_count` off
    `bulk_write`'s return value, so it needs a `MongoClient`-shaped handle.
    GD-21 puts the *live* path on `AsyncMongoClient`; the async twin of this
    shape is `mirror.MongoBackend.bulk_upsert` (R-45), which re-uses the pure
    guards below verbatim rather than reaching for a raw handle. Handed an async
    handle, this one raises :class:`AsyncClientError` — inside the hierarchy, so
    the drainer's `except MongoStoreError:` still holds — rather than the
    `AttributeError` an un-awaited coroutine used to produce.

    Every operation goes through the **same** two guards :func:`apply_operations`
    applies in memory — :func:`spec_for` (GD-24's table is closed) and
    :func:`check_id` (SD-11: every `_id` comes from `refs.ref_key`). Without
    them the two halves of GD-25's own oracle would enforce different laws, and
    the strict half would be the one that never touches the database: a mapper
    with a typo'd collection name would have mongod create a brand-new
    collection with no validator, no indexes and no `_id` pin, accumulating a
    shadow collection nobody queries (GD-12's wrong-target hazard), while a
    hand-built `_id` that never saw `refs.ref_key` would key a document nothing
    can ever join to. Both are refused *before* pymongo is even imported, so the
    guard is testable with nothing third-party installed.

    Size is **not** checked here: `guard_oversize` is the caller's to apply
    (`mirror.py`, R-45), because only the caller knows the `sourcePath` and
    `byteOffset` the stub must carry to stay traceable.

    Failures a caller may still see: :class:`SchemaError`/:class:`OperatorError`
    from the guards above, and :class:`MongoUnavailable` for *any* driver-level
    failure — an absent pymongo, an unreachable server, a timeout. Per-write
    failures are never raised: they come back in `errors` (fatal) and
    `tolerated_dups` (duplicate keys, GD-29), because half of an unordered bulk
    succeeding is a result, not an exception.
    """
    spec_for(collection)
    checked = []
    for key, update in operations:
        if not isinstance(key, str):
            raise SchemaError(
                f"{collection}: _id must be a string from refs.ref_key, got "
                f"{type(key).__name__} (GD-24)"
            )
        check_id(collection, key)
        validate_update(update, collection, _id=key)
        checked.append((key, update))
    if not checked:
        # ABOVE the import, and that ordering is the contract: `mirror.py` drains
        # a bounded queue into per-collection batches, and a tick where one
        # collection has nothing pending must be a no-op on a deployment with no
        # pymongo — the `mirror:"absent"` degrade GD-21 explicitly supports. An
        # empty batch was never going to touch the network; it must not touch the
        # import either.
        return _no_writes()
    try:
        from pymongo import UpdateOne
        from pymongo.errors import BulkWriteError, PyMongoError
    except ImportError as exc:
        raise MongoUnavailable(f"pymongo is not installed: {exc}") from None
    requests = [UpdateOne({"_id": key}, update, upsert=True) for key, update in checked]
    try:
        result = _sync_result(db[collection].bulk_write(requests, ordered=ordered),
                              collection, "bulk write")
    except BulkWriteError as exc:
        split = split_write_errors(exc)
        details = exc.details or {}
        return _no_writes(
            matched=details.get("nMatched", 0),
            upserted=details.get("nUpserted", 0),
            modified=details.get("nModified", 0),
            tolerated_dups=len(split["tolerated"]) + len(split["conflicts"]),
            identity_dups=len(split["tolerated"]),
            tolerated=split["tolerated"],
            conflicts=split["conflicts"],
            errors=split["fatal"],
        )
    except PyMongoError as exc:
        # MongoUnavailable's docstring promises "pymongo is absent OR no mongod
        # answered". AutoReconnect / ServerSelectionTimeoutError / NetworkTimeout
        # are the second half, and letting them through raw would mean the class
        # meant one thing at this call site and another at every other.
        raise MongoUnavailable(f"{collection}: bulk write failed: {exc}") from None
    return _no_writes(
        matched=result.matched_count,
        upserted=len(result.upserted_ids or {}),
        modified=result.modified_count,
    )


#: Comparisons a :func:`guarded_update` precondition may use. A narrow door on
#: purpose: `require` is a *precondition on one document*, not a query language.
GUARD_OPS = ("$lt", "$lte", "$gt", "$gte", "$ne", "$in", "$nin", "$exists")


def _guard_filter(key, require=None) -> dict:
    """`{_id: key}` plus the caller's preconditions, checked. Pure."""
    filter_ = {"_id": key}
    for field, condition in (require or {}).items():
        if not isinstance(field, str) or not field or field.startswith("$"):
            raise SchemaError(f"guard: unusable field name {field!r}")
        if field == "_id":
            raise SchemaError(
                "guard: _id is the key, not a precondition — a guarded update "
                "addresses exactly one document (GD-24)"
            )
        if isinstance(condition, dict):
            # EVERY dict precondition is read as a comparison expression, so an
            # equality match against a sub-document is not expressible here (and
            # would be a GD-24 violation anyway: `{s,n}` and `{n,s}` are two
            # different equality keys). The message says so rather than listing
            # operators at a reader who wanted equality and is not going to find
            # one that helps.
            unknown = sorted(op for op in condition if op not in GUARD_OPS)
            if unknown:
                raise SchemaError(
                    f"guard on {field!r}: a dict precondition is read as a comparison "
                    f"expression and {unknown} is not one of {list(GUARD_OPS)} — an "
                    f"equality match on a SUB-DOCUMENT is not supported at all (a "
                    f"sub-document is never an equality-match key, GD-24)"
                )
            for value in condition.values():
                _check_keys({"v": value}, [field])
        else:
            _check_keys({"v": condition}, [field])
        filter_[field] = condition
    return filter_


def _guard_lost(dups, *, conflicts=(), identity_dups=None):
    """The one shape a lost guard has: nothing matched, nothing written.

    ``conflicts`` carries the driver's error item when the duplicate key came
    from a **secondary** unique index — `slots.agentId` — so a caller can tell
    "another document already owns this value" from "I lost the race for this
    `_id`". Both wrote nothing; only one of them is GD-29's diagnostic.
    """
    conflicts = list(conflicts)
    return {"matched": 0, "upserted": 0, "modified": 0, "acquired": False,
            "tolerated_dups": dups,
            "identity_dups": dups - len(conflicts) if identity_dups is None
            else identity_dups,
            "conflicts": conflicts}


def _dup_conflict(exc):
    """`[item]` if ``exc`` is a duplicate key on a secondary unique index, else `[]`.

    A `DuplicateKeyError` carries ONE write error rather than a list, so its
    details are read directly instead of through :func:`split_write_errors`.
    """
    details = getattr(exc, "details", None) or {}
    return [] if _is_identity_dup(details) else [details]


def _driver_error(collection, action, exc):
    """Translate a driver exception into this module's hierarchy.

    Everything the driver raises is :class:`MongoUnavailable` — the degrade
    signal GD-21 promises and GD-30's breaker counts — with ONE exception:
    :data:`DOCUMENT_VALIDATION_FAILED`, which is mongod stating that the
    DOCUMENT is wrong, not that the server is gone. Counting a `$jsonSchema`
    refusal as unavailability would trip the breaker (`mirror: "degraded"`,
    then `"down"`) on a perfectly healthy mongod, and the one write that was
    actually broken would be the last thing anybody suspected.
    """
    if getattr(exc, "code", None) == DOCUMENT_VALIDATION_FAILED:
        return SchemaError(
            f"{collection}: the server's $jsonSchema refused this {action} "
            f"(code {DOCUMENT_VALIDATION_FAILED}): {exc}"
        )
    return MongoUnavailable(f"{collection}: {action} failed: {exc}")


def guarded_update(db, collection, key, update, *, require=None, upsert=True):
    """ONE conditional upsert, behind the same guards :func:`bulk_upsert` applies.

    GD-24's own table implies two writes `bulk_upsert`'s `{_id: key}` filter
    cannot express, and both belong to the sub-plans immediately downstream:

    * **R-52's derived head.** `custom_state.seq` advances by `$max` (idempotent,
      needing no filter), but the head's *payload* may only be written by a
      newer event: `require={"seq": {"$lt": new_seq}}`, so a late old write never
      clobbers a fresher head.
    * **GD-29's writer lease.** `writers` is acquired with
      `require={"leaseExpiresAt": {"$lt": now}}` — the holder renews, a second
      live writer must fail to acquire rather than steal the stream.

    Exporting the shape here is the point: without it those callers would
    hand-roll `update_one` against a raw collection handle, which is exactly the
    bypass :func:`bulk_upsert`'s docstring argues must not exist — and they would
    discover, one sub-plan away from the table that decides it, that
    `custom_state` fences `$set` off `seq` anyway.

    Returns `{"matched", "upserted", "modified", "acquired", "tolerated_dups",
    "identity_dups", "conflicts"}` — the last two reading a duplicate key the
    same way :func:`bulk_upsert` does, so a caller does not have to know which
    of the two write shapes produced the result it is inspecting.
    ``acquired`` is the answer a lease caller actually wants: **False means
    nothing was written** — either the document exists and did not satisfy
    ``require``, or it does not exist and this update is not a create. It is a
    normal outcome (a lost lease race, a late-arriving custom-state event), so
    it is *returned*, never raised: :class:`MongoUnavailable` is GD-30's breaker
    signal, and answering a healthy race with it takes the mirror down over
    steady-state traffic.

    Delivering that contract is why a precondition does **not** ride on an
    `upsert=True` `update_one`. Under one, a guard that matches nothing becomes
    an INSERT attempt built from the filter's equality fields plus the update —
    and if the update is a *partial* one, that insert fails the collection's
    `$jsonSchema` (code 121) before it can fail on the duplicate `_id`. Both of
    GD-29's own call shapes are partial (`custom_state` writes only the head's
    payload behind `{seq: {$lt: n}}`; a lease renewal writes only the expiry),
    so the normal case came back as a driver failure. The order here is instead:

    1. the conditional update, **never inserting** — a match is the acquire, in
       one round trip, with no read-then-write window for a third writer;
    2. on no match, when ``upsert``, a create — but only if the update alone
       yields a document the collection would accept, because a partial payload
       write is not a create. The create is an explicit `insert_one`, so "the
       document already exists" comes back as the duplicate key GD-29 requires
       be counted rather than swallowed, and never as validation noise.

    Between (1) and (2) sits one `find_one({_id}, {_id: 1})`. Without it, a
    guard that lost to a document which *does* exist still attempted the create,
    and the duplicate key that came back was counted — so a steady stream of
    R-52's late events (normal traffic) produced a steady stream of "tolerated
    dups" on a perfectly healthy single writer, inverting the one number GD-29
    asks to be read as "a second writer or a key bug". The projection makes the
    probe cheap, it only runs on the no-match path, and a document created in
    the gap still lands as the duplicate key GD-29 requires be counted — that
    one really is two writers racing.

    Neither step retries: a create that raced a concurrent create reports a lost
    guard, and the caller retries on its next tick (the lease loop has one).
    Retrying in here would be a loop whose exit condition is another writer's
    behaviour, held open across two round trips inside a single call.

    **Driver mode: synchronous**, exactly as :func:`bulk_upsert` — the async
    twin is `mirror.MongoBackend.guarded_update` (R-45), and an async handle
    here raises :class:`AsyncClientError` rather than escaping the hierarchy.
    """
    spec = spec_for(collection)
    check_id(collection, key)
    validate_update(update, collection, _id=key)
    filter_ = _guard_filter(key, require)
    # Every guard above is pure and runs before this import, for the same reason
    # bulk_upsert's empty batch does: the refusals must be testable, and
    # reachable, with nothing third-party installed (GD-21).
    try:
        from pymongo.errors import DuplicateKeyError, PyMongoError
    except ImportError as exc:
        raise MongoUnavailable(f"pymongo is not installed: {exc}") from None
    handle = db[collection]
    if not require:
        # No precondition: this IS `bulk_upsert`'s write, for one document.
        try:
            result = _sync_result(handle.update_one(filter_, update, upsert=upsert),
                                  collection, "guarded update")
        except DuplicateKeyError as exc:
            return _guard_lost(1, conflicts=_dup_conflict(exc))
        except PyMongoError as exc:
            raise _driver_error(collection, "guarded update", exc) from None
        upserted = 0 if result.upserted_id is None else 1
        return {"matched": result.matched_count, "upserted": upserted,
                "modified": result.modified_count,
                "acquired": bool(result.matched_count or upserted),
                "tolerated_dups": 0, "identity_dups": 0, "conflicts": []}
    try:
        result = _sync_result(handle.update_one(filter_, update, upsert=False),
                              collection, "guarded update")
    except PyMongoError as exc:
        raise _driver_error(collection, "guarded update", exc) from None
    if result.matched_count:
        return {"matched": result.matched_count, "upserted": 0,
                "modified": result.modified_count, "acquired": True,
                "tolerated_dups": 0, "identity_dups": 0, "conflicts": []}
    if not upsert:
        return _guard_lost(0)
    candidate = apply_update(None, update, _id=key, collection=collection)
    missing = [field for field in spec.required if field not in candidate]
    if missing:
        # The update cannot stand alone as a document, so it is a PAYLOAD write
        # (R-52's head note, GD-29's renewal), not a create. Its guard matched
        # nothing; that is the whole answer, and it is `acquired: False`. Trying
        # the insert anyway would ask mongod a question whose only two answers
        # are "duplicate key" and "failed validation" — one of them read as a
        # dead server by every caller.
        return _guard_lost(0)
    validate_document(collection, candidate)
    try:
        existing = _sync_result(handle.find_one({"_id": key}, {"_id": 1}),
                                collection, "guard probe")
    except PyMongoError as exc:
        raise _driver_error(collection, "guard probe", exc) from None
    if existing is not None:
        # The document is there and the guard did not match it: an ordinary lost
        # race (R-52's late event, a lease held by somebody else). There is
        # nothing to create and nothing to count — the duplicate key an attempted
        # insert would return here is not GD-29's "two writers on one stream",
        # it is this call's own guard, restated as an error.
        return _guard_lost(0)
    try:
        _sync_result(handle.insert_one(candidate), collection, "guarded create")
    except DuplicateKeyError as exc:
        # A document appeared between the probe and the insert — that IS the
        # racing second writer, and GD-29 forbids swallowing it as much as it
        # forbids crashing on it. A duplicate on a *secondary* unique index is
        # reported separately: the create lost to another document's claim on a
        # value, not to another writer's claim on this `_id`.
        return _guard_lost(1, conflicts=_dup_conflict(exc))
    except PyMongoError as exc:
        raise _driver_error(collection, "guarded create", exc) from None
    return {"matched": 0, "upserted": 1, "modified": 0, "acquired": True,
            "tolerated_dups": 0, "identity_dups": 0, "conflicts": []}
