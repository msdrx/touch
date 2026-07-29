#!/usr/bin/env python3
"""Stdlib-only tests for aggregator/refs.py (R-43, the `ref_key` canonicalizer).
Run as `python3 test_refs.py`; exits non-zero on failure. No pytest, no runner.

R-43's own test list is the spine:

* every ref shape built twice with **different dict insertion orders** ⇒ equal
  `_id`, one document (GD-24's opening law: `{s,n}` and `{n,s}` are two
  different BSON sub-documents, probed independently three times —
  MONGOSCHEMA-6 ≡ CUSTOMSTATE-4 ≡ LIVEFLOW-2);
* the type pins round-trip (`procStart` **string**, `ordinal`/`seq`/`pid` int,
  `bool` is not an int here);
* escaping round-trips a task name containing `#|:%`.

Plus the invariants only a test can hold in place:

* SD-11 says every `_id` in the system comes from `ref_key`; `store.cursor_key`
  emits the same grammar file-side, so the two are proven **byte-identical**
  here (store.py:400 hands that proof to this sub-plan by name);
* the GD-11 ref union in `store.REF_SHAPES` and the union kinds here are the
  same seven shapes — two copies of a union that drift are two unions;
* the module is pure: no I/O, no clock, no third-party import (SD-1 requires
  the mappers that import it to stay pure too).
"""

import ast
import itertools
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[0]
# The canonical trees are named through `tests/_roots.py`, never by a
# literal under REPO: GD-U1 moves them and this is the single flip point.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))

from aggregator import refs                                    # noqa: E402
from aggregator import store as store_mod                      # noqa: E402
from aggregator.refs import (                                  # noqa: E402
    KIND_SPECS,
    MAX_COMPONENT_CHARS,
    MAX_KEY_BYTES,
    PADDED_INTS,
    RefError,
    UnknownRefError,
    canonical_ref,
    classify,
    collection_of,
    escape_component,
    escape_field_key,
    escape_stream,
    legacy_agent_id,
    parse_ref_key,
    ref_id,
    ref_key,
    unescape_component,
    unescape_field_key,
    validate_ref,
)

failures = []

AGENT = "a2fc883c96ff7b837"                    # a real 17-hex agentId
UUID = "081b28a7-aee9-43dc-935d-1586407f232e"  # a real record uuid
SESSION = "292fc08c-923d-4ab4-8ff2-a9572417dbc8"
NASTY = "touch#recon|v2:stage%1"               # every structural char, in one name


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception as other:                                  # noqa: BLE001
        print(f"    (raised {type(other).__name__}: {other})")
        return False
    return False


#: One well-formed instance of every kind in GD-24's table. Written out rather
#: than generated so a kind added without a test fixture fails the coverage
#: check below instead of being silently exercised by a stub.
SAMPLES = {
    "uuid": {"uuid": UUID},
    "toolUseId": {"toolUseId": "toolu_01ABCdef"},
    "agentId": {"agentId": AGENT},
    "runNode": {"runId": "wf_829e6f58-b2f", "key": "research", "ordinal": 2},
    "session": {"pid": 622, "procStart": "10028"},
    "orchAgent": {"root": "touch-mongo-live", "name": "sp-refs-mongostore", "attempt": 1},
    "legacyPlan": {"task": NASTY, "plan": "sp-05", "stage": "implement", "attempt": 2},
    "histSession": {"sessionId": SESSION},
    "run": {"runId": "wf_829e6f58-b2f"},
    "usage": {"messageId": "msg_01XyZ"},
    "streamMeta": {"sessionId": SESSION, "lineNo": 180},
    "event": {"stream": "run:wf_829e6f58-b2f", "seq": 12},
    "customStateEvent": {"stream": "custom-state", "seq": 7},
    "customState": {"refId": "custom-state#000000000007", "stateKey": "note#1"},
    "legacyEvent": {"task": NASTY, "lineNo": 130},
    "slot": {"sessionKey": "622-10028", "root": "touch", "name": "impl", "attempt": 1},
    "writer": {"stream": "run:wf_829e6f58-b2f"},
    "cursor": {"streamId": "/home/x/.claude/projects/a/b.jsonl"},
}


def with_kind(kind):
    ref = dict(SAMPLES[kind])
    ref["kind"] = kind
    return ref


# --- GD-24's opening law --------------------------------------------------
def test_key_is_independent_of_dict_order():
    print("test_key_is_independent_of_dict_order")
    # The hazard, stated first so the assertion below means something: two dicts
    # with the same items in different order are DIFFERENT BSON documents, and
    # Mongo inserted them as two documents in all three probes.
    check(json.dumps({"s": 1, "n": 2}) != json.dumps({"n": 2, "s": 1}),
          "the encoded form of a sub-document depends on field order (the GD-24 hazard)")

    for kind, sample in sorted(SAMPLES.items()):
        keys = list(sample)
        orders = list(itertools.permutations(keys))[:6]
        built = set()
        canonical = set()
        for order in orders:
            ref = {k: sample[k] for k in order}
            ref["kind"] = kind
            built.add(ref_key(ref))
            canonical.add(json.dumps(canonical_ref(ref)))
        check(len(built) == 1,
              f"{kind}: {len(orders)} insertion orders ⇒ one _id ({built.pop() if len(built)==1 else built})")
        check(len(canonical) == 1,
              f"{kind}: …and one byte-stable ref sub-document")


def test_every_kind_is_covered_and_keyed_by_a_string():
    print("test_every_kind_is_covered_and_keyed_by_a_string")
    check(set(SAMPLES) == set(KIND_SPECS),
          f"every kind in the table has a sample "
          f"(missing: {sorted(set(KIND_SPECS) - set(SAMPLES))}, "
          f"stale: {sorted(set(SAMPLES) - set(KIND_SPECS))})")
    for kind in sorted(SAMPLES):
        key = ref_key(with_kind(kind))
        check(isinstance(key, str) and key, f"{kind}: _id is a non-empty string, never a document")


def test_round_trip_through_parse():
    print("test_round_trip_through_parse")
    for kind in sorted(SAMPLES):
        ref = with_kind(kind)
        key = ref_key(ref)
        parsed = parse_ref_key(kind, key)
        check(parsed == canonical_ref(ref),
              f"{kind}: parse_ref_key(ref_key(x)) == canonical x")
        check(ref_key(parsed) == key, f"{kind}: and rebuilding the key is stable")


# --- escaping -------------------------------------------------------------
def test_escaping_round_trips_the_structural_characters():
    print("test_escaping_round_trips_the_structural_characters")
    for text in (NASTY, "%", "%25", "%2525", "a:b|c#d", "", "ünïcode ✓", "100%|#:"):
        check(unescape_component(escape_component(text)) == text,
              f"escape/unescape round-trips {text!r}")
    check(escape_component("%25") == "%2525",
          "escaping is a single pass: '%25' becomes '%2525', so it decodes back to '%25'")
    check(unescape_component("%2525") == "%25",
          "…and unescaping is a single pass too (no double-unescape)")
    for ch in "#|:":
        check(ch not in escape_component(f"a{ch}b"),
              f"a raw {ch!r} never survives into a key component")
    check(escape_component("a%b") == "a%25b",
          "'%' survives only as the escape introducer — that is what makes the mapping reversible")

    task_key = ref_key({"kind": "legacyEvent", "task": NASTY, "lineNo": 7})
    check("#" == task_key[task_key.rindex("#")] and task_key.count("#") == 1,
          f"a task name full of separators still yields ONE structural '#': {task_key}")
    check(parse_ref_key("legacyEvent", task_key)["task"] == NASTY,
          "…and the raw task name comes back exactly (GD-14 escaping round-trip)")

    legacy_id = legacy_agent_id(NASTY, "a1b2c3d4")
    check(validate_ref({"agentId": legacy_id}) == "agentId",
          f"legacy_agent_id builds an id the 17-hex validator exempts: {legacy_id}")
    check(raises(RefError, ref_key, {"agentId": "legacy:touch#recon:a1b2c3d4"}),
          "…and an unescaped legacy agent id is rejected, not silently keyed")


def test_a_field_key_escapes_what_a_bson_key_cannot_hold():
    """`escape_field_key` is the OTHER escaper, and the two are not substitutes.

    GD-24's grammar escapes `% # | :` because those are the `_id` string's own
    separators. A BSON **field name** does not care about any of them and cannot
    hold `.` (Mongo's path separator) or a leading `$` (an operator) — and
    `mongo_store._check_keys` rejects both, which is right and is why the
    escape has to exist somewhere.

    GD-26's `sessions.sourceState` is the caller: keyed by a source path, and
    every transcript path ends in `.jsonl`. Escaping one with the wrong function
    is the failure this pair exists to make visible, so neither is a superset of
    the other and both round-trip.
    """
    print("test_a_field_key_escapes_what_a_bson_key_cannot_hold")
    paths = ("projects/-home-laniakea-Projects-touch/292fc08c.jsonl",
             "a.b$c%d", "$where", "..", "%2E", NASTY, "no-hostile-characters")
    for text in paths:
        escaped = escape_field_key(text)
        check(unescape_field_key(escaped) == text,
              f"escape/unescape round-trips {text!r} → {escaped!r}")
        check("." not in escaped and "$" not in escaped,
              f"…and neither '.' nor '$' survives into the key: {escaped!r}")
    check(escape_field_key("%2E") == "%252E",
          "escaping is a single pass, so an already-escaped-looking input is not "
          "mistaken for one of its own outputs")
    check(escape_field_key("plain") == "plain",
          "…and a key with nothing hostile in it is left exactly as it was")

    # The two escapers are deliberately different: neither is a superset.
    check(escape_field_key("a:b") == "a:b" and escape_component("a:b") != "a:b",
          "a ':' is structural in the _id grammar and ordinary in a field name")
    check(escape_component("a.b") == "a.b" and escape_field_key("a.b") != "a.b",
          "…and a '.' is the reverse, which is why using one for the other is a bug "
          "rather than merely redundant")

    check(raises(RefError, escape_field_key, "with\na newline"),
          "a control character is rejected here too — a key that tears a JSONL line "
          "in the file store the mirror projects can never become a key")
    check(raises(RefError, escape_field_key, ""),
          "…and an empty field key names nothing, so it is refused rather than stored")
    check(raises(RefError, escape_field_key, 7),
          "…and a non-string is a RefError like every other refusal in this module")
    check(raises(RefError, escape_field_key, "x" * (MAX_COMPONENT_CHARS + 1)),
          f"…and the same {MAX_COMPONENT_CHARS}-char cap applies: a key is an index key")


def test_component_bounds():
    print("test_component_bounds")
    check(raises(RefError, ref_key, {"kind": "run", "runId": "wf\n829"}),
          "a control character in a component is rejected (it would tear a JSONL line)")
    check(raises(RefError, ref_key, {"kind": "run", "runId": "x" * (MAX_COMPONENT_CHARS + 1)}),
          f"a component over {MAX_COMPONENT_CHARS} chars is rejected — an _id is an index key")
    check(ref_key({"kind": "run", "runId": "x" * MAX_COMPONENT_CHARS}),
          "…and one exactly at the cap is fine")

    # The component cap is not the cap that protects the index entry: a `slot`
    # key joins FOUR components, so four legal components make one illegal _id.
    # MAX_KEY_BYTES is the only guard on that, and it is on the encoded bytes,
    # not the characters — a name of astral-plane emoji is four bytes each.
    fat = {"kind": "slot", "sessionKey": "622-10028", "root": "r" * MAX_COMPONENT_CHARS,
           "name": "n" * MAX_COMPONENT_CHARS, "attempt": 1}
    check(raises(RefError, ref_key, fat),
          f"four components at the {MAX_COMPONENT_CHARS}-char cap still exceed the "
          f"{MAX_KEY_BYTES}-byte key cap, and the WHOLE key is what becomes an index entry")
    try:
        ref_key(fat)
    except RefError as exc:
        check(str(MAX_KEY_BYTES) in str(exc) and "bytes" in str(exc),
              f"…and the rejection names the byte cap it hit: {exc}")
    slim = dict(fat, name="n")
    check(len(ref_key(slim).encode("utf-8")) <= MAX_KEY_BYTES and ref_key(slim),
          "…while a key that fits is built normally")
    wide = {"kind": "run", "runId": "🜛" * 400}
    check(raises(RefError, ref_key, wide),
          "…and the cap counts UTF-8 bytes: 400 four-byte characters are under the "
          "component cap and over the key cap")


# --- type pins ------------------------------------------------------------
def test_bson_type_pins():
    print("test_bson_type_pins")
    check(ref_key({"pid": 622, "procStart": "10028"}) == "live:622-10028",
          "session key is live:<pid>-<procStart> with the GD-24 separator '-'")
    check(raises(RefError, ref_key, {"pid": 622, "procStart": 10028}),
          "procStart as an int is rejected: GD-24 pins it to bsonType string")
    check(raises(RefError, ref_key, {"pid": "622", "procStart": "10028"}),
          "pid as a string is rejected: GD-24 pins it to bsonType int")
    check(raises(RefError, ref_key,
                 {"runId": "wf_1", "key": "research", "ordinal": "2"}),
          "ordinal as a string is rejected (GD-7/GD-24 int pin)")
    check(raises(RefError, ref_key,
                 {"runId": "wf_1", "key": "research", "ordinal": True}),
          "…and bool is not an int here, even though Python says it is")
    check(raises(RefError, ref_key,
                 {"runId": "wf_1", "key": "research", "ordinal": -1}),
          "a negative ordinal is rejected")
    check(raises(RefError, ref_key, {"uuid": "not-a-uuid"}),
          "a non-UUID uuid is a malformed KNOWN shape ⇒ hard rejection (GD-11)")
    check(raises(RefError, ref_key, {"agentId": "a2fc883c"}),
          "an 8-hex agentId is rejected: the full 17-hex id is identity (GD-7/CONVO-10)")
    parsed = parse_ref_key("session", "live:622-10028")
    check(isinstance(parsed["pid"], int) and isinstance(parsed["procStart"], str),
          "parsing a session key restores int pid and string procStart")

    # A composite `sessionKey` is pinned to what the `session` grammar can
    # actually emit. It was laxer than the `pid` field it is built from, so a
    # slot could name a session that cannot exist and
    # {sessionKey,root,name,attempt} would index a join target with no other side.
    check(raises(RefError, ref_key, {"pid": 0, "procStart": "1"}),
          "pid 0 is rejected: the pin is >= 1")
    check(refs.slot_key("622-10028", "r", "n", 1).startswith("slot:622-10028|"),
          "…a slot naming a real session key is built")
    for bad in ("0-1", "0622-10028", "-1", "622"):
        check(raises(RefError, refs.slot_key, bad, "r", "n", 1),
              f"…and a slot naming sessionKey {bad!r} is rejected — no session document "
              f"could ever carry that key, so the join target cannot exist")


def test_padding_makes_lexicographic_order_numeric():
    print("test_padding_makes_lexicographic_order_numeric")
    keys = [ref_key({"kind": "event", "stream": "custom-state", "seq": n})
            for n in (0, 2, 9, 10, 99, 100, 1000, 999999)]
    check(keys == sorted(keys),
          "seq-padded event ids sort lexicographically in numeric order (LIVEFLOW-3)")
    lines = [ref_key({"kind": "streamMeta", "sessionId": SESSION, "lineNo": n})
             for n in (0, 9, 10, 100)]
    check(lines == sorted(lines), "…and so do lineNo-padded stream_meta ids")
    widths = {
        "seq": ref_key({"kind": "event", "stream": "custom-state", "seq": 3}).rsplit("#", 1)[1],
        "lineNo": ref_key({"kind": "streamMeta", "sessionId": SESSION,
                           "lineNo": 3}).rsplit("#", 1)[1],
        "ordinal": ref_key({"kind": "runNode", "runId": "wf_1", "key": "k",
                            "ordinal": 3}).rsplit("|", 1)[1],
        "attempt": ref_key({"kind": "orchAgent", "root": "r", "name": "n",
                            "attempt": 3}).rsplit("|", 1)[1],
    }
    for field, rendered in sorted(widths.items()):
        check(rendered == f"{3:0{PADDED_INTS[field]}d}",
              f"{field} is padded to refs.PADDED_INTS[{field!r}]={PADDED_INTS[field]} ({rendered})")
    wide = ref_key({"kind": "event", "stream": "custom-state", "seq": 10 ** 13})
    check(wide.endswith(str(10 ** 13)),
          "a value past its width widens rather than truncates (a wrong id is worse than a wide one)")


def test_identity_hex_has_exactly_one_spelling():
    """Uppercase hex is a SECOND spelling of one identity, so it is rejected.

    `_AGENT_ID_RE` already took this position for the 17-hex agentId. A uuid,
    sessionId or parentUuid that differed only in case would give one record two
    canonical `_id`s — the duplicate GD-24's whole table exists to prevent — and,
    worse, a silent one: `ref_id_kinds` calls both spellings well-formed, so a
    `refId` an agent wrote uppercase into a `.touch/` control file (R-52) would
    dangle against `records` forever with nothing to report.

    Rejected, not normalized. Every other malformed known shape here is rejected
    (GD-11's hard-rejection half), and a silent `.lower()` would mean `ref_key`
    no longer round-trips to the bytes its caller handed in.
    """
    print("test_identity_hex_has_exactly_one_spelling")
    check(refs.record_key(UUID) == UUID, "a lowercase uuid keys a record verbatim")
    check(raises(RefError, ref_key, {"uuid": UUID.upper()}),
          "…and the SAME uuid spelled uppercase is refused, not silently keyed twice")
    try:
        ref_key({"uuid": UUID.upper()})
    except RefError as exc:
        check("LOWERCASE" in str(exc).upper(),
              f"…with a message that says which half is wrong: {exc}")
    for kind, field, value in (("streamMeta", "sessionId", SESSION),
                               ("histSession", "sessionId", SESSION)):
        ref = dict(SAMPLES[kind], kind=kind)
        ref[field] = value.upper()
        check(raises(RefError, ref_key, ref),
              f"{kind}: an uppercase {field} is refused on the same rule")
    # Validation is keyed by FIELD NAME, not by kind, so the pin reaches fields
    # that are stored beside a key rather than inside one (`parentUuid`,
    # `recordUuid` — GD-24 stores every component as an ordinary indexed field).
    for field in ("parentUuid", "recordUuid", "sessionId"):
        check(refs._validate_field(field, UUID) == UUID
              and raises(RefError, refs._validate_field, field, UUID.upper()),
              f"…and {field} inherits the same pin, since one rule per name is one rule "
              f"per concept")
    check(raises(RefError, ref_key, {"agentId": AGENT.upper()}),
          "the 17-hex agentId was already lowercase-only — this is the same rule, "
          "finally spelled the same way for the uuid family")
    check("uuid" in refs.ref_id_kinds(UUID) and "uuid" not in refs.ref_id_kinds(UUID.upper()),
          f"…and an uppercase uuid stops being RECOGNISED as a records _id "
          f"({refs.ref_id_kinds(UUID.upper())}), which is what a dangling custom-state "
          f"refId would have looked like: well-formed, joinable to nothing. It stays a "
          f"valid runs/usage key, and that is the documented limit of what a bare string "
          f"can prove — R-52's tighter check belongs to the writer")
    check(ref_key({"kind": "customState", "refId": refs.record_key(UUID),
                   "stateKey": "note"}),
          "…while a lowercase records refId builds a custom_state head normally")


# --- classification, unknown shapes, refId --------------------------------
def test_unknown_shapes_are_retained_never_keyed():
    print("test_unknown_shapes_are_retained_never_keyed")
    weird = {"galaxy": "andromeda", "n": 3}
    check(classify(weird) == "unknown", "an unrecognised shape classifies as unknown, not an error")
    check(canonical_ref(weird) == {"kind": "unknown", "galaxy": "andromeda", "n": 3},
          "…is retained verbatim under kind:'unknown' (GD-11's open tail)")
    check(list(canonical_ref({"n": 3, "galaxy": "a"})) == ["kind", "galaxy", "n"],
          "…with its keys sorted, so even an unknown ref is byte-stable")
    check(ref_id(weird) is None, "…carries no refId (GD-24: excluded from joins)")
    check(raises(UnknownRefError, ref_key, weird),
          "…and demanding a key for it fails loudly: there is no best-effort _id")
    check(classify({}) == "none" and classify(None) == "none",
          "an absent ref is 'none' (a stream-level log line has no target)")
    check(raises(UnknownRefError, classify, {"kind": "nosuchkind", "x": 1}),
          "an explicitly declared kind that does not exist is a programmer error")


def test_ref_id_is_none_for_groupings():
    print("test_ref_id_is_none_for_groupings")
    for kind in ("toolUseId", "orchAgent", "legacyPlan"):
        check(ref_id(with_kind(kind)) is None and ref_key(with_kind(kind)),
              f"{kind}: names a grouping, so it has a stable key but no refId")
        check(collection_of(kind) is None, f"…and no collection")
    for kind in ("uuid", "agentId", "event", "slot"):
        check(ref_id(with_kind(kind)) == ref_key(with_kind(kind)),
              f"{kind}: refId is the target document's _id")


def test_colliding_key_sets_require_an_explicit_kind():
    print("test_colliding_key_sets_require_an_explicit_kind")
    ambiguous = {"stream": "custom-state", "seq": 7}
    check(classify(ambiguous) == "unknown",
          "{stream,seq} is both an events id and a custom_state_events id ⇒ never guessed (GD-12)")
    check(refs.event_key("custom-state", 7) == ref_key({"kind": "event", **ambiguous}),
          "the helper spells the kind for the caller")
    check(collection_of("event") == "events"
          and collection_of("customStateEvent") == "custom_state_events",
          "…and the two kinds address different collections with the same grammar")
    check(raises(RefError, ref_key, {"kind": "uuid", "uuid": UUID, "extra": 1}),
          "an unexpected field on a known kind is rejected (silent drops are how ids diverge)")


def test_a_ref_id_is_itself_a_ref_key():
    """The `refId` half of a `custom_state` `_id` is checked, not assumed.

    `custom_state._id` is `<refId>#<stateKey>` (GD-24), and only the right half
    was ever verified — `stateKey` is escaped and round-tripped, while `refId`
    was any opaque string at all. It is by construction another `ref_key`
    output, so the grammar can say so, which is what makes R-52's seq-guarded
    head keys self-checking.

    What this cannot decide is *which* entity a refId names: a `runs`, `usage`
    or `cursors` key is an escaped arbitrary string, so any plain word is a
    syntactically valid refId for one of them. R-52's tighter rule (agents /
    run_nodes / slots only) belongs to the writer, not to the grammar.
    """
    print("test_a_ref_id_is_itself_a_ref_key")
    agent = refs.agent_key(AGENT)
    check("agentId" in refs.ref_id_kinds(agent),
          f"an agents _id is recognised as one: {refs.ref_id_kinds(agent)}")
    check(set(refs.ref_id_kinds("custom-state#000000000007"))
          >= {"event", "customStateEvent"},
          "a {stream,seq} key answers for BOTH collections that share the grammar — "
          "which is why the caller decides and this never guesses (GD-12)")
    check(refs.ref_id_kinds("") == () and refs.ref_id_kinds(None) == (),
          "…and an empty key names nothing")

    key = ref_key({"kind": "customState", "refId": agent, "stateKey": "note#1"})
    check(parse_ref_key("customState", key)["refId"] == agent,
          f"a custom_state _id round-trips its refId: {key}")
    check(raises(RefError, ref_key,
                 {"kind": "customState", "refId": "run|s#1", "stateKey": "note"}),
          "a refId carrying raw structural characters is rejected — it could not "
          "have come from ref_key, so nothing will ever join to it")
    check(raises(RefError, ref_key,
                 {"kind": "customState", "refId": "sess#180", "stateKey": "note"}),
          "…and so is one whose zero-padding no key grammar would have produced")
    check(ref_key({"kind": "customState", "refId": "wf_829e6f58-b2f", "stateKey": "note"}),
          "…while a bare runs/usage key is accepted: an arbitrary string IS a valid "
          "runId, and pretending otherwise would reject legitimate refs")


# --- agreement with the file side (SD-11) ---------------------------------
def test_event_id_is_byte_identical_to_store_cursor_key():
    print("test_event_id_is_byte_identical_to_store_cursor_key")
    for stream in ("custom-state", "run:wf_829e6f58-b2f", "session:622-10028",
                   "run:legacy:touch-repo-recon", "control"):
        for seq in (0, 7, 123456789012):
            mine = ref_key({"kind": "event", "stream": stream, "seq": seq})
            theirs = store_mod.cursor_key(stream, seq)
            check(mine == theirs, f"ref_key == store.cursor_key for {stream!r}#{seq}: {mine}")
            back_stream, back_seq = store_mod.parse_cursor_key(mine)
            parsed = parse_ref_key("event", mine)
            check((back_stream, back_seq) == (parsed["stream"], parsed["seq"]),
                  f"…and both parse it back to the same (stream, seq)")
    check(escape_stream("run:legacy:touch-repo-recon") == "run:legacy%3Atouch-repo-recon",
          "only the FIRST ':' of a stream id is structural, exactly as store.cursor_key has it")
    check(raises(RefError, escape_stream, "run:.."),
          "a stream id that names a directory traversal is rejected on both sides")


def test_the_ref_union_matches_the_file_side():
    print("test_the_ref_union_matches_the_file_side")
    union_here = {name: (set(spec.required), set(spec.optional))
                  for name, spec in KIND_SPECS.items() if spec.union}
    union_there = {name: (set(req), set(opt))
                   for name, (req, opt) in store_mod.REF_SHAPES.items()}
    check(set(union_here) == set(union_there),
          f"the same seven GD-11 union members exist on both sides "
          f"(here-only: {sorted(set(union_here) - set(union_there))}, "
          f"there-only: {sorted(set(union_there) - set(union_here))})")
    for name in sorted(set(union_here) & set(union_there)):
        check(union_here[name] == union_there[name],
              f"{name}: same required/optional key sets in refs.py and store.py")
    for name in sorted(set(union_here) & set(union_there)):
        sample = SAMPLES[name]
        check(store_mod.classify_ref(sample) == classify(sample) == name,
              f"{name}: both modules classify the same dict the same way")


def test_provenance_and_agent_exemptions_agree():
    print("test_provenance_and_agent_exemptions_agree")
    legacy_id = legacy_agent_id("touch-repo-recon", "a1b2c3d4")
    check(store_mod.validate_ref({"agentId": legacy_id}) == "agentId"
          and validate_ref({"agentId": legacy_id}) == "agentId",
          "the GD-14 legacy:<task>:<id8> exemption holds on both sides")
    check(raises(store_mod.RefError, store_mod.validate_ref, {"agentId": "a2fc883c"})
          and raises(RefError, validate_ref, {"agentId": "a2fc883c"}),
          "…and both reject an 8-hex agentId")


# --- purity (SD-1) --------------------------------------------------------
def test_the_module_is_pure():
    print("test_the_module_is_pure")
    tree = ast.parse((SRC / "aggregator" / "refs.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module.split(".")[0])
    check(imported <= {"re", "__future__"},
          f"refs.py imports only {sorted(imported)} — no I/O, no pymongo, no clock (SD-1)")

    banned = {"open", "print", "input", "exec", "eval"}
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    check(not (called & banned), f"…and calls nothing that touches the world {sorted(called & banned)}")

    attrs = {f"{node.value.id}.{node.attr}" for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)}
    check(not any(a.startswith(("os.", "time.", "datetime.", "random.")) for a in attrs),
          "…and reads neither the environment nor the clock: a key must be a function of its ref")


def main():
    for test in (
        test_key_is_independent_of_dict_order,
        test_every_kind_is_covered_and_keyed_by_a_string,
        test_round_trip_through_parse,
        test_escaping_round_trips_the_structural_characters,
        test_a_field_key_escapes_what_a_bson_key_cannot_hold,
        test_component_bounds,
        test_bson_type_pins,
        test_padding_makes_lexicographic_order_numeric,
        test_identity_hex_has_exactly_one_spelling,
        test_unknown_shapes_are_retained_never_keyed,
        test_ref_id_is_none_for_groupings,
        test_colliding_key_sets_require_an_explicit_kind,
        test_a_ref_id_is_itself_a_ref_key,
        test_event_id_is_byte_identical_to_store_cursor_key,
        test_the_ref_union_matches_the_file_side,
        test_provenance_and_agent_exemptions_agree,
        test_the_module_is_pure,
    ):
        test()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("all refs (R-43) tests passed")


if __name__ == "__main__":
    main()
