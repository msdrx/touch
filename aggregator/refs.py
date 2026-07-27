"""The one `_id`/ref canonicalizer (R-43) — GD-24's grammar, in code.

GD-24 opens with the law this module exists to enforce:

    A BSON sub-document is never used as `_id` or as an equality-match key.
    (`{s,n}` vs `{n,s}` insert as two distinct documents; probed and
    reproduced by three perspectives — MONGOSCHEMA-6 ≡ CUSTOMSTATE-4 ≡
    LIVEFLOW-2.) Every `_id` is a **string** produced by one shared
    `ref_key()` canonicalizer with a fixed grammar; components are stored as
    ordinary indexed fields alongside; structured refs are queried by dot
    notation only.

So: one function builds every key in the system (SD-11), and it is a pure
function of the ref's *values*, never of its dict insertion order. A second
function, :func:`canonical_ref`, emits the same components as a flat
sub-document in **fixed field order**, which is what goes on the document as
`ref{kind,…}`; the scalar `refId` beside it is :func:`ref_id` (GD-24's
"flat + denormalized", DBRef declined).

This module is **pure**: no I/O, no pymongo, no clock, no environment. It is
imported by `mongo_store.py`, `mirror.py` and by every entity module's
`MIRROR_MAPPERS` (SD-1), so anything it touches becomes a dependency of all of
them. `tests/test_refs.py` asserts that emptiness structurally.

Two key rules, not one (LIVEFLOW-8)
-----------------------------------
Transcripts are rewritten in place (`performRemoveByUuid`,
`performCompactTranscript`), event logs are not. So:

* **uuid/content keys for rewritable sources** — `records._id = <uuid>`,
  `usage._id = <message.id>`: re-ingest of a rewritten file lands on the same
  documents (idempotent), and GD-26's generation sweep retracts what vanished.
* **positional keys only for append-only sources** — `stream_meta`,
  `events`, `legacy_events`. `legacy:<task>#<line:08d>` is safe *precisely*
  because CLAUDE.md forbids ever deleting or rewriting an `events.jsonl`
  (MONGOSCHEMA-7); the schema now depends on that rule.

Grammar
-------
Components are joined with `|`, positional suffixes with `#`, and a namespace
prefix is separated with `:`. Those four characters plus `%` are therefore
**structural**, and any user-chosen component (a task folder name, an agent
`root`/`name`, a workflow node `key`, a custom-state `stateKey`) is
percent-escaped before it is joined::

    %  -> %25      #  -> %23      |  -> %7C      :  -> %3A

Escaping is a single pass in both directions, so `%2525` decodes to `%25` and
not to `%` (the classic double-unescape bug). Callers always pass **raw**
components; `ref_key` escapes and :func:`parse_ref_key` unescapes. Escaping a
component twice is a caller bug that this module cannot see, so it is stated
here and tested there.

Integer components that **order** documents are zero-padded, so lexicographic
`_id` order equals numeric order and `_id`-range scans agree with
`(stream, seq)` cursors — both then IXSCAN (LIVEFLOW-3; dotted-`_id` queries
COLLSCAN, which is why nothing here builds a sub-document key):

    seq 12  |  lineNo 8  |  ordinal 4  |  attempt 3

Integers that merely *identify* (`pid`) are not padded: nothing ever range-scans
by pid, and `live:00000622-10028` would be a worse id than `live:622-10028`.
A value too large for its width widens rather than truncates — order is then
only preserved within a width class, which for 10^12 events on one stream is a
theoretical concern and a silently wrong id is not.

Kinds
-----
`classify()` recognises the seven GD-11 ref-union members by key set alone —
that union is open at the tail, so an unrecognised shape is *retained* with
``kind:"unknown"`` and **no** `refId` (never an error, never a join). Every
other kind in GD-24's table is addressed by an explicit ``kind`` field,
because their key sets collide (`{stream,seq}` is both an `events` id and a
`custom_state_events` id) and guessing between two collections is exactly the
wrong-target hazard GD-12 forbids. Helper constructors (`event_key`,
`record_key`, …) are the ergonomic form of the same thing.

`collection` on a kind means "a document with this `_id` lives there". Some
kinds legitimately have none: a `toolUseId` ref resolves through a sparse
index, not an `_id`, and `orchAgent`/`legacyPlan` name a *grouping*, not a
document. Those produce a stable ref key (usable for dedup, caches and
`ref{}`) but `ref_id()` returns None for them, exactly as it does for unknown
shapes.
"""

from __future__ import annotations

import re

__all__ = [
    "RefError",
    "UnknownRefError",
    "KIND_SPECS",
    "UNION_KINDS",
    "PADDED_INTS",
    "MAX_COMPONENT_CHARS",
    "MAX_KEY_BYTES",
    "escape_component",
    "unescape_component",
    "escape_field_key",
    "unescape_field_key",
    "escape_stream",
    "unescape_stream",
    "classify",
    "validate_ref",
    "canonical_ref",
    "ref_key",
    "ref_id",
    "ref_id_kinds",
    "parse_ref_key",
    "collection_of",
    "key_fields",
    "collections",
    "legacy_agent_id",
    "record_key",
    "stream_meta_key",
    "agent_key",
    "run_key",
    "run_node_key",
    "session_key",
    "hist_session_key",
    "usage_key",
    "event_key",
    "custom_state_event_key",
    "custom_state_key",
    "legacy_event_key",
    "slot_key",
]


class RefError(ValueError):
    """A malformed instance of a *known* ref shape (GD-11's hard-rejection half).

    A `ValueError` subclass because every caller is Touch's own code building a
    ref it just derived: this is a programmer error surfacing before a wrong key
    reaches a permanent store, not a data error to be tolerated.
    """


class UnknownRefError(RefError):
    """`ref_key` was asked for a key of a shape that has no grammar.

    Distinct from :class:`RefError` because unknown shapes are *retained*, not
    rejected (GD-11's open tail): `canonical_ref` keeps them, `ref_id` returns
    None, and only an explicit demand for a key fails.
    """


# --- escaping -------------------------------------------------------------

#: The structural characters of the GD-24 grammar. `%` first in spirit: the
#: mapping is applied per character in one pass, so an escape sequence produced
#: for one character is never rescanned.
_ESCAPES = {"%": "%25", "#": "%23", "|": "%7C", ":": "%3A"}
_UNESCAPE_RE = re.compile(r"%(25|23|7C|3A)")
_UNESCAPES = {"25": "%", "23": "#", "7C": "|", "3A": ":"}

#: A component this long is a bug upstream (a whole tool result pasted into a
#: name), and an `_id` is an index key: bound it here rather than discover the
#: bound in production. Values, not keys, are where big things belong.
MAX_COMPONENT_CHARS = 512
MAX_KEY_BYTES = 1024

#: Zero-padding widths for *ordering* integers (see module docstring).
PADDED_INTS = {"seq": 12, "lineNo": 8, "ordinal": 4, "attempt": 3}

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def escape_component(text) -> str:
    """Percent-escape `% # | :` in a user-chosen key component (GD-24/GD-14).

    Rejects control characters outright: a name containing a newline would tear
    a JSONL line in the file store the mirror is projecting, so it can never be
    allowed to *become* a key even though BSON would accept it.
    """
    if not isinstance(text, str):
        raise RefError(f"key component must be a string, got {type(text).__name__}")
    if _CONTROL_RE.search(text):
        raise RefError(f"key component contains a control character: {text!r}")
    if len(text) > MAX_COMPONENT_CHARS:
        raise RefError(
            f"key component is {len(text)} chars, over the {MAX_COMPONENT_CHARS} cap"
        )
    return "".join(_ESCAPES.get(ch, ch) for ch in text)


def unescape_component(text: str) -> str:
    """Inverse of :func:`escape_component`, single pass (`%2525` → `%25`)."""
    return _UNESCAPE_RE.sub(lambda m: _UNESCAPES[m.group(1)], text)


#: The characters BSON forbids in a *field name* — a different hostile set from
#: the `_id`-grammar one above, because the two live in different places. `.`
#: is Mongo's own path separator and `$` starts an operator, so a sub-document
#: keyed by a value that may contain either is unaddressable and gets rejected
#: by `mongo_store._check_keys` (R-44/MONGOSCHEMA-8). `%` is escaped first in
#: spirit for `escape_component`'s reason: one pass, so an escape sequence
#: produced for one character is never rescanned.
_FIELD_ESCAPES = {"%": "%25", ".": "%2E", "$": "%24"}
_FIELD_UNESCAPE_RE = re.compile(r"%(25|2E|24)")
_FIELD_UNESCAPES = {"25": "%", "2E": ".", "24": "$"}


def escape_field_key(text) -> str:
    """Percent-escape `% . $` so ``text`` can be a BSON **field name**.

    The one place this is needed today is GD-26's per-source state: a session's
    `sourceState` is keyed by the source's own path, and every transcript path
    ends in `.jsonl`. Keys are where Mongo's addressing lives — a dotted one is
    not a key with a dot in it, it is a *path* — so the escape happens once,
    here, rather than in each caller that happens to remember.

    Distinct from :func:`escape_component` on purpose: that one escapes the
    GD-24 `_id` grammar's own separators (`% # | :`), which a field name does
    not care about, and leaves `.` alone, which a field name cannot. Escaping
    with the wrong one of the two is the failure this pair exists to make
    visible, so neither is a superset of the other and both round-trip.
    """
    if not isinstance(text, str):
        raise RefError(f"field key must be a string, got {type(text).__name__}")
    if not text:
        raise RefError("field key must be non-empty — an empty BSON key names nothing")
    if _CONTROL_RE.search(text):
        raise RefError(f"field key contains a control character: {text!r}")
    if len(text) > MAX_COMPONENT_CHARS:
        raise RefError(f"field key is {len(text)} chars, over the {MAX_COMPONENT_CHARS} cap")
    return "".join(_FIELD_ESCAPES.get(ch, ch) for ch in text)


def unescape_field_key(text: str) -> str:
    """Inverse of :func:`escape_field_key`, single pass (`%2525` → `%25`)."""
    return _FIELD_UNESCAPE_RE.sub(lambda m: _FIELD_UNESCAPES[m.group(1)], text)


#: Stream ids are the one component with an *internal structural* `:` — they
#: read `run:<runId>`, `session:<key>`, `custom-state`. `store.cursor_key`
#: (R-24, file side) splits at the first `:`, escapes both halves and rejoins;
#: this repeats that byte for byte so a `.touch/` cursor token and an `events`
#: `_id` are the same string. `tests/test_refs.py` proves it by importing both.
_STREAM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+@=,%-]{0,199}$")


def escape_stream(stream) -> str:
    """Escape a stream id, keeping only its first `:` structural (see above)."""
    if not isinstance(stream, str) or not stream:
        raise RefError("stream id must be a non-empty string")
    if not _STREAM_RE.match(stream):
        raise RefError(f"unusable stream id: {stream!r}")
    if ".." in stream:
        raise RefError(f"stream id may not contain '..': {stream!r}")
    for part in stream.split(":"):
        if part in ("", ".", ".."):
            raise RefError(f"stream id component {part!r} is not a usable name: {stream!r}")
    prefix, sep, rest = stream.partition(":")
    return escape_component(prefix) + sep + escape_component(rest)


def unescape_stream(text: str) -> str:
    prefix, sep, rest = text.partition(":")
    return unescape_component(prefix) + sep + unescape_component(rest)


# --- field validators -----------------------------------------------------
#
# Validation is keyed by *field name*, not by kind: GD-24 stores every key
# component as an ordinary indexed field of the same name across collections
# (`runId` is one thing everywhere), so one rule per name is one rule per
# concept — and a kind that later reuses a name inherits its pin for free.

#: Identity hex is **lowercase**, in one spelling only. `_AGENT_ID_RE` already
#: took that position for the 17-hex agentId; a uuid/sessionId/parentUuid that
#: differed only in case would otherwise produce two canonical `_id`s for one
#: record — the exact duplicate GD-24 exists to prevent — and a `refId` spelled
#: uppercase in a `.touch/` control file (R-52) would dangle forever while
#: `ref_id_kinds` still called it well-formed. Case is *rejected*, never
#: normalized: this module rejects everywhere else too, and a silent
#: `.lower()` would mean the key no longer round-trips to the bytes the caller
#: handed in. (`store.py`'s file-side validator is laxer; it validates refs
#: rather than producing `_id`s, and SD-11 makes this module the producer.)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UUID_ANYCASE_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_AGENT_ID_RE = re.compile(r"^[0-9a-f]{17}$")                      # GD-7, full 17-hex
_LEGACY_AGENT_RE = re.compile(r"^legacy:(?P<task>[^:]*):(?P<id8>[0-9a-f]{8})$")  # GD-14
#: A composite `<pid>-<procStart>`, pinned to exactly what the `session`
#: grammar can emit: `pid` is validated with `minimum=1` and rendered by `%d`,
#: so neither `0-1` nor a leading-zero `0622-1` is a key `ref_key` will ever
#: produce. Accepting them here would let a `slots` document name a
#: `sessionKey` no `sessions` document can carry — and
#: `{sessionKey:1,root:1,name:1,attempt:1}` would index a join target that
#: cannot exist. The module rejects rather than tolerates everywhere else.
_SESSION_KEY_RE = re.compile(r"^[1-9]\d*-\d+$")                   # <pid>-<procStart>
_PROC_START_RE = re.compile(r"^\d+$")                             # /proc/<pid>/stat f22


def _text(name, value, *, allow_empty=False):
    if not isinstance(value, str):
        raise RefError(f"ref.{name} must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise RefError(f"ref.{name} must be a non-empty string")
    if _CONTROL_RE.search(value):
        raise RefError(f"ref.{name} contains a control character: {value!r}")
    if len(value) > MAX_COMPONENT_CHARS:
        raise RefError(f"ref.{name} is over the {MAX_COMPONENT_CHARS}-char cap")
    return value


def _integer(name, value, *, minimum=0):
    # bool is an int in Python and is never an ordinal, a pid or a seq.
    if isinstance(value, bool) or not isinstance(value, int):
        raise RefError(
            f"ref.{name} must be an int (GD-24 bsonType pin), got {value!r}"
        )
    if value < minimum:
        raise RefError(f"ref.{name} must be >= {minimum}, got {value}")
    return value


def _uuid(name, value):
    _text(name, value)
    if not _UUID_RE.match(value):
        if _UUID_ANYCASE_RE.match(value):
            raise RefError(
                f"ref.{name} must be a LOWERCASE UUID: {value!r} — uppercase hex is a "
                f"second spelling of one identity, so the two would key two documents "
                f"for one record; fix the writer rather than normalizing here (GD-24)"
            )
        raise RefError(f"ref.{name} is not a UUID: {value!r}")
    return value


def _agent_id(name, value):
    _text(name, value)
    if _AGENT_ID_RE.match(value):
        return value
    match = _LEGACY_AGENT_RE.match(value)
    if match:
        # The `<task>` half must already be escaped (GD-14) — a raw `:` there
        # would make the id's own colons ambiguous, and a raw `#`/`|` would
        # collide with the key grammar the id gets embedded in.
        task = match.group("task")
        if not task:
            raise RefError(f"ref.{name}: legacy agent id has an empty task: {value!r}")
        bad = [ch for ch in "#|" if ch in task]
        if bad:
            raise RefError(
                f"ref.{name}: legacy agent id task is not escaped (contains {bad}) — "
                f"build it with refs.legacy_agent_id(task, id8): {value!r}"
            )
        return value
    raise RefError(
        f"ref.{name} must be 17 hex chars or a legacy:<task>:<id8> id (GD-7/GD-14): {value!r}"
    )


def _proc_start(name, value):
    _text(name, value)
    if not _PROC_START_RE.match(value):
        raise RefError(
            f"ref.{name} must be the clock-tick STRING from /proc/<pid>/stat f22 "
            f"(GD-24 pins it to bsonType string, not int): {value!r}"
        )
    return value


def _session_key(name, value):
    _text(name, value)
    if not _SESSION_KEY_RE.match(value):
        raise RefError(
            f"ref.{name} must be <pid>-<procStart> with a pid >= 1 and no leading "
            f"zero — the same pin the `pid` field carries, so every sessionKey a "
            f"slot names is one the session grammar can actually emit: {value!r}"
        )
    return value


def _ref_id(name, value):
    """A `refId` is by construction another `ref_key` output — so check it.

    Without this a `custom_state` `_id` would be half-checked: the `stateKey`
    half is escaped and verified, while the `refId` half was any opaque string
    at all. :func:`ref_id_kinds` re-parses it, so a refId carrying a raw
    structural character, a control character, wrong padding or an
    over-cap component is rejected here rather than becoming a permanent head
    document nothing can join to.

    What it deliberately does **not** prove is *which* entity is named: a
    `runs`/`usage`/`cursors` key is an escaped arbitrary string, so any plain
    word is a syntactically valid refId for one of them. R-52's tighter rule
    (a custom-state refId must parse as an `agents`, `run_nodes` or `slots`
    key) is the writer's, and belongs with the writer.
    """
    _text(name, value)
    if not ref_id_kinds(value):
        raise RefError(
            f"ref.{name} is not a key any collection's grammar can produce: "
            f"{value!r} — a refId is another ref_key output (GD-24)"
        )
    return value


#: field name -> validator. Anything not listed is a plain non-empty string.
_FIELD_VALIDATORS = {
    "uuid": _uuid,
    "sessionId": _uuid,
    "parentUuid": _uuid,
    "recordUuid": _uuid,
    "agentId": _agent_id,
    "procStart": _proc_start,
    "sessionKey": _session_key,
    "refId": _ref_id,
    "pid": lambda n, v: _integer(n, v, minimum=1),
    "seq": _integer,
    "lineNo": _integer,
    "ordinal": _integer,
    "attempt": _integer,
}


def _validate_field(name, value):
    validator = _FIELD_VALIDATORS.get(name)
    if validator is not None:
        return validator(name, value)
    return _text(name, value)


# --- the kind table -------------------------------------------------------


class _Spec:
    """One row of GD-24's table: how a kind is keyed, and where it lives."""

    __slots__ = ("kind", "required", "optional", "collection", "union", "build", "parse", "note")

    def __init__(self, kind, required, build, parse, *, optional=(), collection=None,
                 union=False, note=""):
        self.kind = kind
        self.required = tuple(required)
        self.optional = tuple(optional)
        self.collection = collection
        self.union = union
        self.build = build
        self.parse = parse
        self.note = note

    @property
    def fields(self):
        return self.required + self.optional


def _pad(name, value):
    return f"{value:0{PADDED_INTS[name]}d}"


def _opt(ref, name):
    value = ref.get(name)
    return "" if value is None else escape_component(str(value))


# Each `build` takes the validated ref dict and returns the `_id`; each `parse`
# takes the key string and returns the raw components. They are written as a
# pair on purpose — a grammar with no inverse is a grammar nobody can audit,
# and `tests/test_refs.py` round-trips every kind through both.

def _build_uuid(r):
    return r["uuid"]


def _parse_uuid(key):
    return {"uuid": key}


def _build_tool(r):
    return "tool:" + escape_component(r["toolUseId"])


def _parse_tool(key):
    return {"toolUseId": unescape_component(_strip(key, "tool:"))}


def _build_agent(r):
    # An agentId is an *identifier*, not a user-chosen component: the 17-hex
    # form is the harness's, and the `legacy:<task>:<id8>` form (GD-14) already
    # arrives with its `<task>` escaped — :func:`legacy_agent_id` is the one
    # place that escaping happens, and `_agent_id` rejects an id that skipped
    # it. So the key is the id, verbatim, byte for byte, in both directions.
    return r["agentId"]


def _parse_agent(key):
    return {"agentId": key}


def legacy_agent_id(task, id8) -> str:
    """Build GD-14's `legacy:<task>:<id8>` agent id from a **raw** task name.

    The one escaping entry point for legacy agent ids: `<task>` is a user-chosen
    folder name, so `% # | :` in it are escaped here (GD-14), which is also what
    makes the surrounding colons unambiguously structural.
    """
    _text("task", task)
    id8 = _text("id8", id8)
    if not re.match(r"^[0-9a-f]{8}$", id8):
        raise RefError(f"legacy agent id8 must be 8 lowercase hex chars: {id8!r}")
    return "legacy:%s:%s" % (escape_component(task), id8)


def _build_run_node(r):
    return "|".join((escape_component(r["runId"]), escape_component(r["key"]),
                     _pad("ordinal", r["ordinal"])))


def _parse_run_node(key):
    parts = key.split("|")
    if len(parts) != 3:
        raise RefError(f"not a run_nodes key: {key!r}")
    return {"runId": unescape_component(parts[0]), "key": unescape_component(parts[1]),
            "ordinal": int(parts[2])}


def _build_session(r):
    return "live:%d-%s" % (r["pid"], r["procStart"])


def _parse_session(key):
    pid, _, proc = _strip(key, "live:").partition("-")
    return {"pid": int(pid), "procStart": proc}


def _build_hist_session(r):
    return "hist:" + r["sessionId"]


def _parse_hist_session(key):
    return {"sessionId": _strip(key, "hist:")}


def _build_run(r):
    return escape_component(r["runId"])


def _parse_run(key):
    return {"runId": unescape_component(key)}


def _build_usage(r):
    return escape_component(r["messageId"])


def _parse_usage(key):
    return {"messageId": unescape_component(key)}


def _build_stream_meta(r):
    return "%s#%s" % (r["sessionId"], _pad("lineNo", r["lineNo"]))


def _parse_stream_meta(key):
    session, _, line = key.rpartition("#")
    return {"sessionId": session, "lineNo": int(line)}


def _build_event(r):
    return "%s#%s" % (escape_stream(r["stream"]), _pad("seq", r["seq"]))


def _parse_event(key):
    stream, _, seq = key.rpartition("#")
    return {"stream": unescape_stream(stream), "seq": int(seq)}


def _build_custom_state(r):
    # `refId` is itself already a canonical key from this module, so it goes in
    # verbatim — escaping it would make the `_id` unsearchable by refId prefix
    # for no gain. `stateKey` is user-chosen and escaped, so it can never hold
    # a raw `#`, which makes the split at the LAST `#` exact.
    return "%s#%s" % (r["refId"], escape_component(r["stateKey"]))


def _parse_custom_state(key):
    ref_id_, _, state = key.rpartition("#")
    return {"refId": ref_id_, "stateKey": unescape_component(state)}


def _build_legacy_event(r):
    return "legacy:%s#%s" % (escape_component(r["task"]), _pad("lineNo", r["lineNo"]))


def _parse_legacy_event(key):
    body, _, line = key.rpartition("#")
    return {"task": unescape_component(_strip(body, "legacy:")), "lineNo": int(line)}


def _build_legacy_plan(r):
    # Fixed arity with empty slots for absent optionals: `a|b||` is
    # unambiguously "no stage, no attempt", where `a|b` would be ambiguous with
    # a future member. Optional components are never *silently* dropped.
    return "legacyplan:%s|%s|%s|%s" % (
        escape_component(r["task"]), escape_component(r["plan"]),
        _opt(r, "stage"),
        "" if r.get("attempt") is None else _pad("attempt", r["attempt"]),
    )


def _parse_legacy_plan(key):
    parts = _strip(key, "legacyplan:").split("|")
    if len(parts) != 4:
        raise RefError(f"not a legacyPlan key: {key!r}")
    out = {"task": unescape_component(parts[0]), "plan": unescape_component(parts[1])}
    if parts[2]:
        out["stage"] = unescape_component(parts[2])
    if parts[3]:
        out["attempt"] = int(parts[3])
    return out


def _build_orch_agent(r):
    return "orch:%s|%s|%s" % (escape_component(r["root"]), escape_component(r["name"]),
                              _pad("attempt", r["attempt"]))


def _parse_orch_agent(key):
    parts = _strip(key, "orch:").split("|")
    if len(parts) != 3:
        raise RefError(f"not an orchAgent key: {key!r}")
    return {"root": unescape_component(parts[0]), "name": unescape_component(parts[1]),
            "attempt": int(parts[2])}


def _build_slot(r):
    return "slot:%s|%s|%s|%s" % (
        escape_component(r["sessionKey"]), escape_component(r["root"]),
        escape_component(r["name"]), _pad("attempt", r["attempt"]))


def _parse_slot(key):
    parts = _strip(key, "slot:").split("|")
    if len(parts) != 4:
        raise RefError(f"not a slot key: {key!r}")
    return {"sessionKey": unescape_component(parts[0]), "root": unescape_component(parts[1]),
            "name": unescape_component(parts[2]), "attempt": int(parts[3])}


def _build_writer(r):
    return escape_stream(r["stream"])


def _parse_writer(key):
    return {"stream": unescape_stream(key)}


def _build_cursor(r):
    return escape_component(r["streamId"])


def _parse_cursor(key):
    return {"streamId": unescape_component(key)}


def _strip(key, prefix):
    if not key.startswith(prefix):
        raise RefError(f"key {key!r} does not start with {prefix!r}")
    return key[len(prefix):]


#: GD-24's table, one entry per kind. `union=True` marks the seven GD-11
#: ref-union members — the only shapes recognisable from their key set alone.
KIND_SPECS = {
    # --- GD-11 ref union (classifiable by key set) ------------------------
    "uuid": _Spec("uuid", ("uuid",), _build_uuid, _parse_uuid,
                  collection="records", union=True,
                  note="rewritable source: uuid key, retraction not deletion (GD-26)"),
    "toolUseId": _Spec("toolUseId", ("toolUseId",), _build_tool, _parse_tool,
                       union=True,
                       note="resolves through records.{toolUseId:1} sparse, not an _id"),
    "agentId": _Spec("agentId", ("agentId",), _build_agent, _parse_agent,
                     collection="agents", union=True),
    "runNode": _Spec("runNode", ("runId", "key", "ordinal"), _build_run_node, _parse_run_node,
                     collection="run_nodes", union=True,
                     note="ordinal is journal-derived (GD-7), never a DB counter"),
    "session": _Spec("session", ("pid", "procStart"), _build_session, _parse_session,
                     collection="sessions", union=True, note="the live arm of the tagged union"),
    "orchAgent": _Spec("orchAgent", ("root", "name", "attempt"), _build_orch_agent,
                       _parse_orch_agent, union=True,
                       note="GD-11(d)/CUSTOMSTATE-7; becomes a slot id once a sessionKey is known"),
    "legacyPlan": _Spec("legacyPlan", ("task", "plan"), _build_legacy_plan, _parse_legacy_plan,
                        optional=("stage", "attempt"), union=True,
                        note="GD-11(d); names a plan/stage grouping, not one document"),
    # --- explicit-kind ids (key sets collide; GD-12 forbids guessing) -----
    "histSession": _Spec("histSession", ("sessionId",), _build_hist_session, _parse_hist_session,
                         collection="sessions",
                         note="historical arm: never a grouping key for agent records (R-25)"),
    "run": _Spec("run", ("runId",), _build_run, _parse_run, collection="runs"),
    "usage": _Spec("usage", ("messageId",), _build_usage, _parse_usage, collection="usage",
                   note="message.id: $max-accumulated absolute doc (GD-25)"),
    "streamMeta": _Spec("streamMeta", ("sessionId", "lineNo"), _build_stream_meta,
                        _parse_stream_meta, collection="stream_meta",
                        note="positional: the one collection with a legal scoped delete (GD-26)"),
    "event": _Spec("event", ("stream", "seq"), _build_event, _parse_event, collection="events",
                   note="byte-identical to store.cursor_key (R-24)"),
    "customStateEvent": _Spec("customStateEvent", ("stream", "seq"), _build_event, _parse_event,
                              collection="custom_state_events",
                              note="same grammar as `event`, different collection: kind required"),
    "customState": _Spec("customState", ("refId", "stateKey"), _build_custom_state,
                         _parse_custom_state, collection="custom_state",
                         note="derived head, seq-guarded (R-52)"),
    "legacyEvent": _Spec("legacyEvent", ("task", "lineNo"), _build_legacy_event,
                         _parse_legacy_event, collection="legacy_events",
                         note="positional; safe because events.jsonl is never rewritten"),
    "slot": _Spec("slot", ("sessionKey", "root", "name", "attempt"), _build_slot, _parse_slot,
                  collection="slots", note="the single name<->agentId hop (R-53)"),
    "writer": _Spec("writer", ("stream",), _build_writer, _parse_writer, collection="writers",
                    note="GD-29 lease; same escaping as the events id prefix"),
    "cursor": _Spec("cursor", ("streamId",), _build_cursor, _parse_cursor, collection="cursors"),
}

#: The seven shapes `classify()` may infer from a bare dict.
UNION_KINDS = tuple(k for k, s in KIND_SPECS.items() if s.union)


def collection_of(kind):
    """Collection a key of ``kind`` addresses, or None (see module docstring)."""
    spec = KIND_SPECS.get(kind)
    return spec.collection if spec else None


def key_fields(kind):
    """(required, optional) field names of ``kind``, in canonical order."""
    spec = _spec(kind)
    return spec.required, spec.optional


def collections():
    """Every collection this module can address, sorted."""
    return tuple(sorted({s.collection for s in KIND_SPECS.values() if s.collection}))


def _spec(kind):
    spec = KIND_SPECS.get(kind)
    if spec is None:
        raise UnknownRefError(f"unknown ref kind: {kind!r}")
    return spec


# --- classification, validation, canonical form ---------------------------


def classify(ref) -> str:
    """Name ``ref``'s kind without validating its values.

    An explicit ``kind`` wins (and is checked to exist). Otherwise the key set
    is matched against the seven union members only. ``"none"`` for an empty
    ref (a stream-level event has no target), ``"unknown"`` for anything else —
    a *retained* outcome, not an error.
    """
    if not ref:
        return "none"
    if not isinstance(ref, dict):
        return "unknown"
    declared = ref.get("kind")
    if declared is not None:
        if declared in KIND_SPECS:
            return declared
        if declared == "unknown":
            return "unknown"
        raise UnknownRefError(f"unknown ref kind: {declared!r}")
    keys = set(ref) - {"kind"}
    for name in UNION_KINDS:
        spec = KIND_SPECS[name]
        required = set(spec.required)
        if keys == required or (required <= keys <= required | set(spec.optional)):
            return name
    return "unknown"


def validate_ref(ref) -> str:
    """Validate ``ref`` against its kind's field pins; return the kind.

    Hard rejection is limited to malformed instances of *known* shapes, exactly
    as GD-11 says: a non-17-hex agentId, a non-UUID uuid, an `int` procStart
    (GD-24 pins it to bsonType string), a `bool` ordinal. Unknown shapes pass
    through untouched.
    """
    kind = classify(ref)
    if kind in ("none", "unknown"):
        return kind
    spec = KIND_SPECS[kind]
    missing = [f for f in spec.required if ref.get(f) is None]
    if missing:
        raise RefError(f"ref kind {kind!r} is missing {missing}")
    extra = set(ref) - {"kind"} - set(spec.fields)
    if extra:
        raise RefError(f"ref kind {kind!r} has unexpected fields {sorted(extra)}")
    for field in spec.fields:
        if ref.get(field) is not None:
            _validate_field(field, ref[field])
    return kind


def canonical_ref(ref) -> dict:
    """The `ref{kind,…}` sub-document: fixed field order, validated values.

    Field order is fixed by the kind's spec, never by the caller's dict, which
    is the whole point — `{s,n}` and `{n,s}` encode to different BSON, and a
    ref that changes shape by insertion order re-creates the sub-document `_id`
    hazard one level down (GD-24). Unknown shapes are retained with their keys
    sorted, so even they are byte-stable, and carry no `refId`.
    """
    kind = validate_ref(ref)
    if kind == "none":
        return {}
    if kind == "unknown":
        out = {"kind": "unknown"}
        for key in sorted(k for k in ref if k != "kind"):
            out[key] = ref[key]
        return out
    spec = KIND_SPECS[kind]
    out = {"kind": kind}
    for field in spec.fields:
        if ref.get(field) is not None:
            out[field] = ref[field]
    return out


def ref_key(ref) -> str:
    """The GD-24 string key for ``ref`` — the ONE `_id` producer (SD-11).

    Independent of dict insertion order by construction (values are read by
    name). Raises :class:`UnknownRefError` for a shape with no grammar: there
    is no such thing as a "best effort" `_id`, and a fabricated one would
    silently merge two entities forever.
    """
    kind = validate_ref(ref)
    if kind in ("none", "unknown"):
        raise UnknownRefError(
            f"no key grammar for ref {ref!r} — unknown shapes are retained with "
            f"kind='unknown' and no refId (GD-24), never keyed"
        )
    key = KIND_SPECS[kind].build(ref)
    encoded = len(key.encode("utf-8"))
    if encoded > MAX_KEY_BYTES:
        raise RefError(f"_id is {encoded} bytes, over the {MAX_KEY_BYTES} cap: {key[:80]!r}…")
    return key


def ref_id(ref):
    """`refId` for ``ref``: its target document's `_id`, or None.

    None means "not a join target" — an unknown shape (GD-24: retained, no
    `refId`, excluded from joins) or a known shape that names a grouping rather
    than a document (`toolUseId`, `orchAgent`, `legacyPlan`).
    """
    kind = classify(ref)
    if kind in ("none", "unknown") or not collection_of(kind):
        return None
    return ref_key(ref)


def ref_id_kinds(key) -> tuple:
    """Kinds whose grammar canonically produces ``key`` — possibly empty.

    The inverse question to :func:`ref_id`: given a bare `_id` string, which
    document grammars could have emitted it? A key is accepted for a kind only
    if it parses **and** rebuilds byte-identically, so wrong padding or a raw
    structural character disqualifies it.

    Several kinds legitimately answer at once (`custom-state#000000000007` is a
    valid `events` id and a valid `custom_state_events` id — GD-12's reason for
    never guessing between them), which is why this returns the whole set and
    the caller decides. `customState` itself is excluded: a head keyed by
    another head is not a shape this schema has.
    """
    if not isinstance(key, str) or not key:
        return ()
    out = []
    for kind, spec in KIND_SPECS.items():
        if not spec.collection or kind == "customState":
            continue
        try:
            if ref_key(parse_ref_key(kind, key)) == key:
                out.append(kind)
        except RefError:
            continue
    return tuple(out)


def parse_ref_key(kind, key) -> dict:
    """Inverse of :func:`ref_key`: raw components back out of a key.

    Exists so the grammar is auditable and round-trippable — a rebuild
    (`--rebuild`, R-45) reads keys back, and an escaping rule with no proven
    inverse is how a task name containing `#|:%` quietly becomes two entities.
    """
    spec = _spec(kind)
    if not isinstance(key, str) or not key:
        raise RefError("key must be a non-empty string")
    try:
        parsed = spec.parse(key)
    except RefError:
        raise
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        # A grammar's inverse is a parser, and a parser fed a key from the wrong
        # grammar fails in whatever way it happens to fail — `int("abc")` raises
        # a bare ValueError. Every caller (`mongo_store.check_id` above all)
        # handles exactly one exception type, so the funnel is here: a malformed
        # key is a RefError, never a stray builtin nobody catches.
        raise RefError(f"key {key!r} is not a {kind} key: {exc}") from None
    parsed["kind"] = kind
    return canonical_ref(parsed)


# --- ergonomic constructors ----------------------------------------------
#
# Mappers (SD-1) call these; they are `ref_key` with the kind spelled once, so
# no caller has to remember whether `{stream,seq}` meant `events` or
# `custom_state_events`.

def record_key(uuid):
    return ref_key({"kind": "uuid", "uuid": uuid})


def stream_meta_key(session_id, line_no):
    return ref_key({"kind": "streamMeta", "sessionId": session_id, "lineNo": line_no})


def agent_key(agent_id):
    return ref_key({"kind": "agentId", "agentId": agent_id})


def run_key(run_id):
    return ref_key({"kind": "run", "runId": run_id})


def run_node_key(run_id, key, ordinal):
    return ref_key({"kind": "runNode", "runId": run_id, "key": key, "ordinal": ordinal})


def session_key(pid, proc_start):
    return ref_key({"kind": "session", "pid": pid, "procStart": proc_start})


def hist_session_key(session_id):
    return ref_key({"kind": "histSession", "sessionId": session_id})


def usage_key(message_id):
    return ref_key({"kind": "usage", "messageId": message_id})


def event_key(stream, seq):
    return ref_key({"kind": "event", "stream": stream, "seq": seq})


def custom_state_event_key(stream, seq):
    return ref_key({"kind": "customStateEvent", "stream": stream, "seq": seq})


def custom_state_key(ref_id_, state_key):
    return ref_key({"kind": "customState", "refId": ref_id_, "stateKey": state_key})


def legacy_event_key(task, line_no):
    return ref_key({"kind": "legacyEvent", "task": task, "lineNo": line_no})


def slot_key(session_key_, root, name, attempt):
    return ref_key({"kind": "slot", "sessionKey": session_key_, "root": root,
                    "name": name, "attempt": attempt})
