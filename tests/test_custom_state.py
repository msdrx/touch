#!/usr/bin/env python3
"""Stdlib-only tests for aggregator/custom_state.py (R-52, the custom-state WAL,
the append-only events collection and the derived head).
Run as `python3 test_custom_state.py`; exits non-zero on failure. No pytest.

R-52's own test list is the spine, one test per clause:

* 3 out-of-order writes ⇒ head = highest seq, log has 3;
* an unknown `refId` is rejected (agents / run_nodes / slots grammars only —
  plus the ONE documented widening for `topology`, which the reducer joins by
  `refs.run_key(runId)` and by nothing else);
* Mongo wipe + WAL replay reproduces both collections exactly;
* drop `custom_state`, rebuild, document-for-document equal;
* the writer has no code path to a mirrored-fact provenance (asserted by call
  **and** by walking this module's own AST, because a rule enforced only at one
  call site is a rule the next branch forgets);
* annotations reject at 16 KB with a 413 rather than truncating;
* deletes are tombstone events (and no delete verb appears in the module at
  all — GD-26);
* ONE events + ONE head collection installation-wide, kind-discriminated.

Two arms, one file, exactly as `test_mongo_store.py`: the **pure arm** runs
everywhere with nothing third-party installed (GD-21's promise, executable), and
the **live arm** proves the in-memory guarded-write model matches a real mongod
when `TOUCH_MONGO_URI` points at one (R-42's loopback+auth recipe), skipping
cleanly otherwise.
"""

import ast
import datetime
import json
import os
import random
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[0]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from aggregator import custom_state as cs                       # noqa: E402
from aggregator import legacy                                   # noqa: E402
from aggregator import mirror as mr                             # noqa: E402
from aggregator import mongo_store as ms                        # noqa: E402
from aggregator import refs                                     # noqa: E402
from aggregator import store as store_mod                       # noqa: E402
from aggregator.custom_state import (                           # noqa: E402
    ANNOTATION_LIMIT,
    AUTHOR,
    CustomStateError,
    CustomStateObservation,
    KINDS,
    PROVENANCE,
    PayloadTooLarge,
    RefRejected,
    Writer,
    head_write,
    map_custom_state,
    rebuild_heads,
    replay,
)

failures = []
skipped = []

MODULE = REPO / "aggregator" / "custom_state.py"
SOURCE = MODULE.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
SKILL = REPO / ".claude" / "skills" / "touch-orchestrate" / "SKILL.md"
FINDINGS = REPO / ".claude" / "local-orchestrators" / "touch-mongo-live" / "findings"
DEVIATION = FINDINGS / "sp-custom-state-head-driver-deviation.md"
SET_FIELDS_DEVIATION = FINDINGS / "sp-custom-state-slots-set-fields-deviation.md"
HEAD_ORDER_DEVIATION = FINDINGS / "sp-custom-state-head-order-deviation.md"

AGENT = "a2fc883c96ff7b837"                       # a real 17-hex agentId
AGENT2 = "b1de44f0c1e2a3b45"
SESSION_KEY = "622-10028"                         # <pid>-<procStart>
RUN = "wf_1a3ffcdd-c60"                           # a real workflow run id
T0 = datetime.datetime(2026, 7, 25, 3, 20, 0, tzinfo=datetime.timezone.utc)
WIRE = "2026-07-25T03:20:00.000Z"


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  SKIP: {msg}")
    skipped.append(msg)


def have_note(path, msg):
    """Assert a run-history deviation note is present; True when readable.

    `.claude/local-orchestrators/` is gitignored and untracked, so these notes
    exist in the working tree that produced them and in no clean checkout — a
    bare `read_text()` here crashed every clone (RENAME-SCOPE-15 /
    AGGREGATOR-VISUAL-9). An absent findings folder skips with a printed
    reason; a findings folder that IS on disk without its note still FAILS,
    because that is a note somebody deleted.
    """
    if path.is_file():
        print(f"  ok: {msg}")
        return True
    if not FINDINGS.is_dir():
        skip(f"{path.name}: run history is gitignored — absent on a clean checkout")
        return False
    check(False, msg)
    return False


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception as other:                                  # noqa: BLE001
        print(f"    (raised {type(other).__name__}: {other})")
        return False
    return False


class Temp:
    """A throwaway `.touch/` root; every test that writes gets its own."""

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="touch-custom-state-")
        return os.path.join(self.path, ".touch")

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
        return False


def agent_ref(agent=AGENT):
    return {"agentId": agent}


def wal_with_three(root, *, state_key="note"):
    """Three events for one `(refId, stateKey)`, seq 1..3, values 1..3."""
    writer = Writer(root=root)
    for value in (1, 2, 3):
        writer.append("agent_state", state_key=state_key, ref=agent_ref(),
                      custom={"value": value},
                      ts=f"2026-07-25T03:20:0{value}.000Z")
    return writer


# --- R-52's literal test list ---------------------------------------------


def test_three_out_of_order_writes_leave_the_head_at_the_highest_seq():
    print("test_three_out_of_order_writes_leave_the_head_at_the_highest_seq")
    with Temp() as root:
        writer = wal_with_three(root)
        observations = writer.observations()
        check([o.seq for o in observations] == [1, 2, 3],
              "the WAL assigns per-file seq 1..3 through store.py's own machinery")

        fingerprints, counts = set(), set()
        for order in ([0, 1, 2], [2, 1, 0], [1, 0, 2], [2, 0, 1]):
            state = replay([observations[i] for i in order])
            fingerprints.add(ms.fingerprint(state))
            counts.add(tuple(sorted(ms.counts(state).items())))
        check(len(fingerprints) == 1,
              f"every ingest order yields ONE fingerprint (GD-25): {len(fingerprints)} seen")
        check(len(counts) == 1 and dict(next(iter(counts))) ==
              {"custom_state": 1, "custom_state_events": 3},
              f"…and the log has 3 documents while the head has 1: {next(iter(counts))}")

        state = replay(observations)
        head = next(iter(state["custom_state"].values()))
        check(head["seq"] == 3 and head["fromSeq"] == 3,
              f"the head is the highest seq, not the last written: seq={head.get('seq')}")
        payload = ms.unwrap_raw(head["data"]["custom"])
        check(payload == {"value": 3},
              f"…and carries that event's payload: {payload}")
        check(head["derived"] is True,
              "…marked derived:true, so nothing mistakes the reduction for the log")


def test_a_late_old_write_never_clobbers_a_fresher_head():
    print("test_a_late_old_write_never_clobbers_a_fresher_head")
    with Temp() as root:
        observations = wal_with_three(root).observations()
        state = replay(observations)
        acquired = cs.apply_guarded(state, head_write(observations[0]))
        check(acquired is False,
              "a seq-1 event arriving after seq 3 loses its guard (R-52's {seq:{$lt:n}})")
        head = next(iter(state["custom_state"].values()))
        check(ms.unwrap_raw(head["data"]["custom"]) == {"value": 3},
              "…and the head still holds the newest payload, not the late one")
        write = head_write(observations[2])
        order = cs.head_order(cs.WAL_STREAM, 3)
        check("$set" in write.update
              and not {"seq", cs.HEAD_ORDER_FIELD} & set(write.update["$set"]),
              "neither `seq` nor the order is ever $set — both are accumulables the "
              "document's identity depends on (GD-25)")
        check(write.update["$max"] == {"seq": 3, cs.HEAD_ORDER_FIELD: order},
              f"…they advance by $max, which needs no filter of its own — and they are "
              f"the ONLY accumulated fields: a guarded update applies nothing when its "
              f"guard loses, so a `$max` clock beside them would see only the winners "
              f"and depend on arrival order after all: {write.update['$max']}")
        check(write.require == {cs.HEAD_ORDER_FIELD: {"$lt": order}},
              f"…and the payload rides a strict `$lt` guard — `$lte` would let an event "
              f"rewrite its own head on re-ingest and still not be the contract: "
              f"{write.require}")
        # R-52's literal `{seq:{$lt:newSeq}}` is the SAME predicate within one
        # stream, which is the whole reason the composite is legal here: its
        # primary component is that zero-padded seq.
        same_stream = [cs.head_order(cs.WAL_STREAM, n) for n in (1, 2, 3, 10, 11)]
        check(same_stream == sorted(same_stream),
              f"within one stream the order sorts exactly as `seq` does, zero-padding "
              f"included (10 after 2, not before it): {same_stream}")
        stored = {cs.HEAD_ORDER_FIELD: order}
        check(not any(cs._guard_matches(stored, head_write(o).require)
                      for o in observations),
              "…so no seq-1, seq-2 or re-applied seq-3 event beats a stored seq-3 head, "
              "exactly as `{seq:{$lt:3}}` would have decided it")


def test_two_streams_that_share_a_seq_still_leave_one_head():
    print("test_two_streams_that_share_a_seq_still_leave_one_head")
    # The corpus GD-25's oracle needs and the single-stream ones cannot supply:
    # `seq` is per-stream and positional (the WAL's counter; a control file's
    # LINE NUMBER), while the head is ONE space installation-wide. Two control
    # files whose line 1 stops the same slot — plus a WAL record at seq 1 for the
    # same (refId, stateKey) — are three events with one head id and one `seq`.
    # Built through the module's own documented sources, no test helper.
    with Temp() as root:
        base = os.path.dirname(root)
        ledger = write_lines(os.path.join(base, "task", "state", cs.LEDGER_FILE),
                             [ledger_line("auth_impl1")])
        index = cs.SlotIndex(cs.read_ledger_file(ledger))
        slot = cs.slot_id(SESSION_KEY, "auth", "auth_impl1", 1)

        first = cs.read_control_file(
            write_lines(os.path.join(base, "alpha", "c.jsonl"),
                        [{"action": "stop", "name": "auth_impl1", "note": "alpha"}]),
            "env", slots=index)
        second = cs.read_control_file(
            write_lines(os.path.join(base, "beta", "c.jsonl"),
                        [{"action": "stop", "name": "auth_impl1", "note": "beta"}]),
            "env", slots=index)
        writer = Writer(root=root)
        writer.append("control_intent", state_key="control_intent:stop", ref_id=slot,
                      custom={"action": "stop", "name": "auth_impl1", "note": "wal"},
                      ts=WIRE)
        observations = first + second + writer.observations()

        check(len(observations) == 3
              and len({o.stream for o in observations}) == 3
              and {o.seq for o in observations} == {1},
              f"three events, three streams, ONE seq — the tie the head has to break: "
              f"{[(o.stream, o.seq) for o in observations]}")
        heads = {cs.head_id(o.ref_id, o.state_key) for o in observations}
        check(len(heads) == 1,
              f"…all addressing one head `_id`, because a control line's stateKey comes "
              f"from its own verb and the refId from the name→slot hop: {heads}")

        orders = [(cs.head_order(o.stream, o.seq), o) for o in observations]
        expected = max(orders, key=lambda pair: pair[0])[1]
        fingerprints, counts, payloads = set(), set(), set()
        arrangements = [list(observations), list(reversed(observations))]
        for seed in range(6):
            shuffled = list(observations)
            random.Random(seed).shuffle(shuffled)
            arrangements.append(shuffled)
        for arrangement in arrangements:
            state = replay(arrangement)
            fingerprints.add(ms.fingerprint(state))
            counts.add(tuple(sorted(ms.counts(state).items())))
            head = next(iter(state["custom_state"].values()))
            payloads.add(json.dumps(ms.unwrap_raw(head["data"]["custom"]), sort_keys=True))
            rebuild_heads(state)                      # drop + replay the log
            fingerprints.add(ms.fingerprint(state))
        check(len(fingerprints) == 1,
              f"every arrival order — and a rebuild from the log after it — yields ONE "
              f"fingerprint (GD-25): {len(fingerprints)} seen")
        check(len(counts) == 1 and dict(next(iter(counts))) ==
              {"custom_state": 1, "custom_state_events": 3},
              f"…with all three lines still in the append-only log: {next(iter(counts))}")
        check(len(payloads) == 1
              and json.loads(next(iter(payloads)))["note"] == expected.custom["note"],
              f"…and the stored payload is the one the ORDER selects, not the one that "
              f"happened to arrive first: {next(iter(payloads))}")

        head = next(iter(replay(observations)["custom_state"].values()))
        check(head[cs.HEAD_ORDER_FIELD] == cs.head_order(expected.stream, 1)
              and head["stream"] == expected.stream and head["seq"] == 1,
              f"…the head names the stream it came from and keeps R-52's `seq` beside "
              f"the order: {head.get(cs.HEAD_ORDER_FIELD)}")

        # The naive guard is the one this corpus exists to fail: prove it would
        # have accepted BOTH events, i.e. that arrival order would have decided.
        naive = [{"seq": {"$lt": o.seq}} for o in observations]
        check(not any(cs._guard_matches({"seq": 1}, require) for require in naive),
              "a bare {seq:{$lt:newSeq}} refuses every one of them against a stored "
              "seq 1 — which is exactly how it lets arrival order pick the head")


def test_every_head_write_carries_one_fixed_key_set():
    print("test_every_head_write_carries_one_fixed_key_set")
    # `$set` overwrites the keys it carries and nothing else, and GD-26 leaves
    # this module no operator that removes a field — so if two events of one head
    # wrote different key sets, whatever the LOSER wrote would survive under the
    # winner's payload, and which of them inserted the document first would
    # decide it. The corpus is deliberately lopsided in every optional field.
    slot = cs.slot_id(SESSION_KEY, "auth", "auth_impl1", 1)
    corpus = [
        CustomStateObservation(kind="agent_state", stream=cs.WAL_STREAM, seq=1,
                               state_key="phase", ref_id=AGENT, custom={"phase": "a"}),
        CustomStateObservation(kind="agent_state", stream=cs.WAL_STREAM, seq=2,
                               state_key="phase", ref_id=AGENT, custom={"phase": "b"},
                               ts=WIRE, session_key=SESSION_KEY,
                               session_key_source="ledger"),
        CustomStateObservation(kind="control_intent", stream=cs.control_stream("x"),
                               seq=2, state_key="control_intent:stop", ref_id=slot,
                               custom={"action": "stop"}, provenance="asserted",
                               ts=WIRE, session_key=SESSION_KEY,
                               session_key_source="slots", attempt_source="resolved",
                               path_source="env"),
        CustomStateObservation(kind="annotation", stream=cs.WAL_STREAM, seq=3,
                               state_key="annotation:a1", ref_id=AGENT,
                               custom={"text": "x"}, tombstone=True, ts_raw="whenever"),
    ]
    writes = [head_write(obs) for obs in corpus]
    top = {frozenset(write.update["$set"]) for write in writes}
    check(len(top) == 1,
          f"every head write `$set`s the same top-level keys, however much the events "
          f"differ: {[sorted(keys) for keys in top]}")
    check(all(set(write.update) == {"$max", "$set"} for write in writes),
          f"…through exactly two operators: "
          f"{sorted({op for w in writes for op in w.update})}")
    check(all(cs.HEAD_ORDER_FIELD in write.update["$max"]
              and cs.HEAD_ORDER_FIELD not in write.update["$set"] for write in writes),
          "…with the order reached by `$max` and never by `$set` — the fence "
          "`custom_state` already has for `seq` (the paste is sp-05's; see the "
          "deviation note)")
    check(all(cs.HEAD_EVENT_FIELD in write.update["$set"] for write in writes),
          f"…and the per-event fields inside one sub-document `$set` replaces whole: "
          f"{cs.HEAD_EVENT_FIELD!r}")
    varying = {"sessionKey", "sessionKeySource", "attemptSource", "ts", "tsRaw"}
    check(not (next(iter(top)) & varying),
          f"…so none of {sorted(varying)} sits loose at the top level, where a stronger "
          f"event could not have replaced it: {sorted(next(iter(top)))}")

    # And the invariant those keys buy, measured rather than argued: one head,
    # two events, the weaker one carrying fields the stronger one does not.
    for order in ([0, 1], [1, 0]):
        state = replay([corpus[i] for i in order])
        head = next(iter(state["custom_state"].values()))
        check(head[cs.HEAD_EVENT_FIELD].get("sessionKey") == SESSION_KEY
              and head["fromSeq"] == 2,
              f"the newer event's attribution wins in either order: "
              f"{head[cs.HEAD_EVENT_FIELD]}")
    reversed_first = replay([corpus[1], corpus[0]])
    check(next(iter(reversed_first["custom_state"].values()))[cs.HEAD_EVENT_FIELD]
          == next(iter(replay([corpus[0], corpus[1]])["custom_state"].values()))[
              cs.HEAD_EVENT_FIELD],
          "…and the loser leaves nothing of itself behind when it inserted first")

    # The case that keeps the clock OUT of the accumulated set: `seq` and `ts`
    # disagree (a later line, an earlier timestamp — clocks and line numbers are
    # not the same order across files). A `$max` on `ts` would only see the
    # events whose guard fired, so it would answer differently per arrival order.
    early = CustomStateObservation(kind="agent_state", stream=cs.WAL_STREAM, seq=1,
                                   state_key="clock", ref_id=AGENT, custom={"n": 1},
                                   ts="2026-07-25T09:00:00.000Z")
    late = CustomStateObservation(kind="agent_state", stream=cs.WAL_STREAM, seq=2,
                                  state_key="clock", ref_id=AGENT, custom={"n": 2},
                                  ts="2026-07-25T03:00:00.000Z")
    both = [ms.fingerprint(replay(list(pair))) for pair in ((early, late), (late, early))]
    check(both[0] == both[1],
          "a higher-seq event with an EARLIER ts lands on one document either way")
    head = next(iter(replay([late, early])["custom_state"].values()))
    check(head["fromSeq"] == 2 and head[cs.HEAD_EVENT_FIELD]["tsRaw"]
          == "2026-07-25T03:00:00.000Z",
          f"…and the head carries the winning EVENT's own clock, not the newest one it "
          f"ever saw: {head[cs.HEAD_EVENT_FIELD].get('tsRaw')}")
    check("ts" not in head,
          f"…with no top-level clock to disagree with it: {sorted(head)}")


def test_a_wal_line_cannot_smuggle_an_author_past_the_read_door():
    print("test_a_wal_line_cannot_smuggle_an_author_past_the_read_door")
    check(cs.validate_author(None) == AUTHOR and cs.validate_author(AUTHOR) == AUTHOR,
          f"`author` is the literal {AUTHOR!r}, and a record that did not say is "
          f"normalised to it rather than left blank (CUSTOMSTATE-16)")
    check(raises(CustomStateError, cs.validate_author, "michael@host"),
          "…while a name is refused: Touch has no user identity model, and GD-13's "
          "token authenticates a browser rather than a person")

    # GD-29 contemplates agent-side file appends, so the WAL is not only this
    # module's own output: a line it did not write must not be able to put a
    # fabricated identity into `custom_state_events` or into the head.
    record = {"kind": "annotation", "seq": 1, "ts": WIRE, "provenance": "asserted",
              "ref": agent_ref(),
              "data": {"stateKey": "annotation:a1", "author": "michael@host",
                       "custom": {"text": "hi"}}}
    obs = CustomStateObservation.from_record(record)
    check(obs.author == "michael@host",
          "the reader preserves what the line said (it is evidence, not a guess)…")
    check(raises(CustomStateError, map_custom_state, obs)
          and raises(CustomStateError, head_write, obs),
          "…and BOTH write doors refuse it, so neither collection can hold it")
    mapper = mr.Mapper("customState", "custom_state", cs.map_custom_state)
    check(raises(mr.MapperError, mapper, obs),
          "…as a MapperError, which mirror.py counts in `stats['rejected']` — a refusal "
          "an operator can see, not a line quietly dropped (D13)")

    clean = dict(record, data=dict(record["data"], author=AUTHOR))
    state = replay([CustomStateObservation.from_record(clean)])
    doc = next(iter(state["custom_state_events"].values()))
    head = next(iter(state["custom_state"].values()))
    check(doc["author"] == AUTHOR and head["author"] == AUTHOR,
          f"…while the legal value travels onto both documents: {doc.get('author')}")

    with Temp() as root:
        writer = Writer(root=root)
        check(raises(CustomStateError, writer.append, "annotation", state_key="a",
                     ref=agent_ref(), custom={"text": "hi"}, author="michael@host"),
              "the write door is the same function, so the two cannot drift apart")
        check(writer.records() == [], "…and nothing reached the WAL")


def test_an_unknown_refid_is_rejected():
    print("test_an_unknown_refid_is_rejected")
    good = [
        ("agents", refs.agent_key(AGENT)),
        ("run_nodes", refs.run_node_key(RUN, "research", 0)),
        ("slots", cs.slot_id("622-10028", "auth", "auth_impl1", 1)),
    ]
    for label, key in good:
        check(cs.validate_ref_id(key) == key, f"a {label} key is an acceptable refId")

    for label, key in (("a bare word", "somewhere"),
                       ("a session key", refs.session_key(622, "10028")),
                       ("a usage/message id", refs.usage_key("msg_0123")),
                       ("a runs key", refs.run_key(RUN))):
        check(raises(RefRejected, cs.validate_ref_id, key),
              f"{label} is refused — a dangling state card is worse than a rejected one")

    check(cs.validate_ref_id(refs.run_key(RUN), kind="topology") == refs.run_key(RUN),
          "…except for `topology`, the ONE documented widening: the reducer joins a "
          "topology head by refs.run_key(runId) and by nothing else (sp-10's handoff)")
    check(cs.allowed_ref_kinds("topology") == cs.REF_KINDS + ("run",)
          and cs.allowed_ref_kinds("annotation") == cs.REF_KINDS,
          "…and the widening is one table entry, not a branch buried in a validator")

    # The widening is only reachable through a `topology` write, so a topology
    # head really does land in a shape `agents.topology_index` picks up.
    obs = CustomStateObservation(
        kind="topology", stream=cs.WAL_STREAM, seq=1, state_key="topology",
        ref_id=refs.run_key(RUN), custom={"maxAttempts": 5, "stages": ["research"]},
        ts=WIRE)
    state = replay([obs])
    head = next(iter(state["custom_state"].values()))
    check(head["refId"] == refs.run_key(RUN) and head["kind"] == "topology",
          "a topology head carries refId = refs.run_key(runId) and kind 'topology'")

    with Temp() as root:
        writer = Writer(root=root)
        check(raises(RefRejected, writer.append, "annotation", state_key="a",
                     ref_id="somewhere", custom={"text": "hi"}),
              "the WRITER refuses it too — the WAL never holds a line the mirror must refuse")
        check(raises(RefRejected, writer.append, "annotation", state_key="a",
                     ref={"toolUseId": "toolu_1"}, custom={"text": "hi"}),
              "…and a ref that names a grouping rather than a document is refused")
        check(writer.counters["rejected"] == 2 and writer.counters["appended"] == 0,
              f"…counted, and nothing was written: {writer.counters}")


def test_a_mongo_wipe_plus_wal_replay_reproduces_both_collections():
    print("test_a_mongo_wipe_plus_wal_replay_reproduces_both_collections")
    with Temp() as root:
        writer = Writer(root=root)
        writer.append("agent_state", state_key="phase", ref=agent_ref(),
                      custom={"phase": "implement"}, ts=WIRE)
        writer.annotate(agent_ref(), "looks wrong to me", annotation_id="ann-1", ts=WIRE)
        writer.append("tag", state_key="tag:review", ref=agent_ref(),
                      custom={"tag": "review"}, ts=WIRE)
        writer.append("agent_state", state_key="phase", ref=agent_ref(),
                      custom={"phase": "critique"}, ts=WIRE)

        # Populate a state, then WIPE it — the same dict, emptied, the way a
        # `drop_database` empties a server. Two replays from two fresh dicts
        # would be trivially equal and would prove nothing about a wipe.
        before = replay(writer.observations())
        check(ms.counts(before) == {"custom_state": 3, "custom_state_events": 4},
              f"…with the counts the log implies: {ms.counts(before)}")
        expected = ms.fingerprint(before)
        wiped = before
        for collection in list(wiped):
            wiped[collection].clear()
        check(ms.counts(wiped) == {"custom_state": 0, "custom_state_events": 0},
              f"the wipe really wiped: {ms.counts(wiped)}")
        after = replay(writer.observations(), wiped)
        check(ms.fingerprint(after) == expected,
              "a Mongo wipe followed by a WAL replay reproduces both collections exactly")
        check(after is wiped and ms.counts(after) == {"custom_state": 3,
                                                      "custom_state_events": 4},
              f"…into the emptied store itself, document for document: {ms.counts(after)}")

        shuffled = list(writer.observations())
        random.Random(7).shuffle(shuffled)
        check(ms.fingerprint(replay(shuffled)) == expected,
              "…and a shuffled replay lands on the same documents (GD-25)")


def test_drop_the_head_rebuild_and_it_is_document_for_document_equal():
    print("test_drop_the_head_rebuild_and_it_is_document_for_document_equal")
    with Temp() as root:
        writer = wal_with_three(root)
        writer.annotate(agent_ref(), "note", annotation_id="ann-1", ts=WIRE)
        state = replay(writer.observations())
        before = ms.fingerprint({"custom_state": state["custom_state"]})
        heads = dict(state["custom_state"])

        rebuild_heads(state)                         # drop + replay the log
        check(ms.fingerprint({"custom_state": state["custom_state"]}) == before,
              "dropping `custom_state` and rebuilding from the log is byte-identical")
        check(state["custom_state"].keys() == heads.keys(),
              "…document for document, by _id (CUSTOMSTATE-14's recovery procedure)")
        check(all(state["custom_state"][key] == heads[key] for key in heads),
              "…and field for field")


def test_the_writer_has_no_code_path_to_a_mirrored_fact_provenance():
    print("test_the_writer_has_no_code_path_to_a_mirrored_fact_provenance")
    forbidden = [p for p in store_mod.PROVENANCE if p not in PROVENANCE]
    check(sorted(forbidden) == ["derived", "harness", "unknown"],
          f"GD-28's five values minus custom state's two leaves {forbidden}")
    for value in forbidden:
        check(raises(CustomStateError, cs.validate_provenance, value),
              f"validate_provenance refuses {value!r} — the one door this module has")
    check(cs.validate_provenance("touch") == "touch"
          and cs.validate_provenance("asserted") == "asserted",
          "…and accepts exactly the two GD-28 pins custom state to")

    with Temp() as root:
        writer = Writer(root=root)
        for value in forbidden:
            check(raises(CustomStateError, writer.append, "agent_state", state_key="s",
                         ref=agent_ref(), custom={}, provenance=value),
                  f"Writer.append refuses provenance={value!r} before a byte reaches the file")
        check(writer.records() == [],
              "…and the WAL is still empty, so the rejection is not a post-hoc filter")
        check(store_mod.Store.stream_provenance(cs.WAL_STREAM) == frozenset(PROVENANCE),
              "store.py pins the same two values file-side (the second, independent leg)")
        check(ms.spec_for("custom_state_events").provenance == PROVENANCE
              and ms.spec_for("custom_state").provenance == PROVENANCE,
              "…and $jsonSchema pins them server-side (the third)")

    # The structural leg: no provenance literal anywhere in the module is
    # outside the enums. A rule enforced at one call site is a rule the next
    # branch forgets, and CUSTOMSTATE-15 asks for structure rather than care.
    allowed = set(PROVENANCE) | {cs.SLOT_PROVENANCE}
    seen = []
    for node in ast.walk(TREE):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "provenance"
                        and isinstance(value, ast.Constant)):
                    seen.append(value.value)
        elif isinstance(node, ast.keyword) and node.arg == "provenance":
            if isinstance(node.value, ast.Constant):
                seen.append(node.value.value)
    check(seen and set(seen) <= allowed,
          f"every provenance literal in custom_state.py is inside {sorted(allowed)}: {seen}")


def test_annotations_reject_at_16kb_rather_than_truncating():
    print("test_annotations_reject_at_16kb_rather_than_truncating")
    # The NUMBER, not only the behaviour: every assertion below builds its
    # payload from the constant, so without this line widening the cap to 16 MB
    # is a passing mutation, and CUSTOMSTATE-16's 16 KB is part of the contract.
    check(ANNOTATION_LIMIT == 16 * 1024,
          f"the cap is CUSTOMSTATE-16's 16 KB, stated as a number: {ANNOTATION_LIMIT}")
    with Temp() as root:
        writer = Writer(root=root)
        prose = "x" * (ANNOTATION_LIMIT + 1)
        try:
            writer.annotate(agent_ref(), prose, annotation_id="ann-1", ts=WIRE)
            raised = None
        except PayloadTooLarge as exc:
            raised = exc
        check(raised is not None, "an over-cap annotation is refused")
        check(getattr(raised, "status", None) == 413,
              f"…with a 413 the API can serve directly: {getattr(raised, 'status', None)}")
        check(raised.size > ANNOTATION_LIMIT and raised.limit == ANNOTATION_LIMIT,
              "…and it says how big the payload was and what the cap is")
        check(writer.records() == [],
              "…and nothing was written: prose is rejected, never silently shortened")

        ok = writer.annotate(agent_ref(), "x" * 100, annotation_id="ann-1", ts=WIRE)
        check(ok["data"]["custom"]["text"] == "x" * 100,
              "…while an under-cap annotation is stored verbatim")
        check(ok["data"]["author"] == AUTHOR == "local",
              f"author is the literal 'local' — Touch has no user identity (D13): {AUTHOR!r}")
        check(raises(CustomStateError, writer.append, "annotation", state_key="a",
                     ref=agent_ref(), custom={"text": "hi"}, author="michael"),
              "…and a caller cannot invent one")

        # The cap is the annotation's, not the machine payload's: GD-11's 1 KB
        # detail rule exists for shell/JS embedding and has no claim over prose,
        # and the machine path keeps `store.py`'s stub-don't-raise behaviour.
        big = {"blob": "y" * (ANNOTATION_LIMIT * 2)}
        record = writer.append("agent_state", state_key="blob", ref=agent_ref(),
                               custom=big, ts=WIRE)
        check(record["data"]["custom"]["blob"] == big["blob"],
              "a machine payload over 16 KB is NOT capped — only user prose is")


def test_deletes_are_tombstone_events_and_no_delete_verb_exists():
    print("test_deletes_are_tombstone_events_and_no_delete_verb_exists")
    with Temp() as root:
        writer = Writer(root=root)
        writer.annotate(agent_ref(), "wrong call here", annotation_id="ann-1", ts=WIRE)
        writer.tombstone("annotation", state_key=cs.annotation_state_key("ann-1"),
                         ref=agent_ref(), ts="2026-07-25T03:21:00.000Z")
        observations = writer.observations()
        check(len(observations) == 2 and observations[1].tombstone is True,
              "a delete is an appended event, never a removal (GD-26/CUSTOMSTATE-14)")
        state = replay(observations)
        head = next(iter(state["custom_state"].values()))
        check(head["tombstone"] is True,
              "…the head records the retraction, so readers can hide it")
        check(ms.counts(state)["custom_state_events"] == 2,
              "…and the log still holds what it said before it was deleted")

        earlier = ms.unwrap_raw(
            state["custom_state_events"][
                refs.custom_state_event_key(cs.WAL_STREAM, 1)]["data"]["custom"])
        check(earlier["text"] == "wrong call here",
              "…verbatim: 'what did it say when it was deleted' stays answerable")

    verbs = ["deleteOne(", "deleteMany(", "drop_collection", "$unset", "remove("]
    present = [verb for verb in verbs if verb in SOURCE]
    check(not present,
          f"no delete verb appears anywhere in custom_state.py (GD-26): {present}")


def test_the_events_collection_is_insert_only_and_installation_wide():
    print("test_the_events_collection_is_insert_only_and_installation_wide")
    obs = CustomStateObservation(kind="agent_state", stream=cs.WAL_STREAM, seq=7,
                                 state_key="phase", ref=agent_ref(),
                                 custom={"phase": "x"}, ts=WIRE)
    ops = map_custom_state(obs)
    check(len(ops) == 1 and ops[0][0] == "custom_state_events",
          "one event ⇒ exactly one custom_state_events operation")
    check(list(ops[0][2]) == ["$setOnInsert"],
          f"…and every field is $setOnInsert — no update path exists: {list(ops[0][2])}")
    check(ops[0][1] == refs.custom_state_event_key(cs.WAL_STREAM, 7),
          "…keyed <stream>#<seq:012d> through refs, positional over an append-only source")

    # Re-mapping the same line is a tolerated duplicate that changes nothing.
    state = ms.apply_operations({}, ops)
    first = ms.fingerprint(state)
    ms.apply_operations(state, map_custom_state(obs))
    check(ms.fingerprint(state) == first,
          "re-ingesting a line costs one tolerated duplicate and changes nothing (GD-25)")

    # ONE events + ONE head collection for the whole installation (CUSTOMSTATE-17).
    others = [
        CustomStateObservation(kind="ledger", stream=cs.ledger_stream("touch-mongo-live"),
                               seq=1, state_key="ledger",
                               ref_id=cs.slot_id("622-10028", "auth", "auth_impl1", 1),
                               custom={"name": "auth_impl1"}, provenance="asserted", ts=WIRE),
        CustomStateObservation(kind="control_intent", stream=cs.control_stream("700-9"),
                               seq=1, state_key="control_intent:stop",
                               ref_id=cs.slot_id("700-9", "other", "other_impl1", 1),
                               custom={"action": "stop"}, provenance="asserted", ts=WIRE),
    ]
    state = replay([obs] + others)
    check(sorted(state) == ["custom_state", "custom_state_events"],
          f"three scopes, two collections — never one per task or session: {sorted(state)}")
    kinds = sorted({doc["kind"] for doc in state["custom_state_events"].values()})
    check(kinds == ["agent_state", "control_intent", "ledger"],
          f"…discriminated by kind, which is what makes one collection enough: {kinds}")
    check(set(KINDS) >= set(kinds) and len(KINDS) == 8,
          "…out of R-52's closed eight-kind list")
    check(raises(CustomStateError, cs.validate_kind, "whatever"),
          "an unrecognised kind is refused: it would be a document nothing queries")


def test_the_module_writes_only_its_own_three_collections():
    print("test_the_module_writes_only_its_own_three_collections")
    check(cs.COLLECTIONS == ("custom_state_events", "custom_state", "slots"),
          f"GD-15's fence, declared: {cs.COLLECTIONS}")
    check(raises(CustomStateError, cs._only_ours, [("agents", "x", {})]),
          "…and enforced: an `agents` write from here is a refusal, not a review comment")
    for name in cs.COLLECTIONS:
        check(name in ms.COLLECTIONS, f"{name} is one of GD-24's declared collections")


# --- SD-8: control paths are configured, and never restated ---------------


def test_control_paths_are_configured_and_the_path_is_never_restated():
    print("test_control_paths_are_configured_and_the_path_is_never_restated")
    check(cs.control_paths({}) == [],
          "with nothing configured the source yields nothing — the honest answer today")
    check(cs.control_paths({cs.CONTROL_PATHS_ENV: ""}) == [],
          "…and an empty variable is not a path")
    paths = cs.control_paths({cs.CONTROL_PATHS_ENV: f"/a/x.jsonl{os.pathsep}/b/y.jsonl"})
    check([p for p, _ in paths] == ["/a/x.jsonl", "/b/y.jsonl"],
          f"a configured list is read from {cs.CONTROL_PATHS_ENV} (SD-8): {paths}")
    check({source for _, source in paths} == {"env"},
          "…and every entry records where it came from")

    # CUSTOMSTATE-11: the skill file and the base plan already disagree about
    # the control-file path; a third statement here would make it three.
    for literal in ("control.jsonl", "<TOUCH_STATE_DIR>", "sessions/"):
        check(literal not in SOURCE,
              f"custom_state.py never restates the control-file path ({literal!r})")

    with Temp() as root:
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, "sessions", "622-10028")
        os.makedirs(path, exist_ok=True)
        control = os.path.join(path, "c.jsonl")
        with open(control, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"action": "stop", "name": "auth_impl1",
                                     "root": "auth", "attempt": 1, "ts": WIRE}) + "\n")
            handle.write(json.dumps({"ack": "stop", "name": "auth_impl1", "root": "auth",
                                     "attempt": 1, "taskId": "t1", "result": "stopped",
                                     "ts": WIRE}) + "\n")
            handle.write("not json\n")
        observations = cs.read_control_file(control, "env")
        check([o.kind for o in observations] == ["control_intent", "control_ack"],
              f"an intent and its ack, in file order: {[o.kind for o in observations]}")
        check([o.seq for o in observations] == [1, 2],
              "…keyed by line number, positional over an append-only file")
        check(all(o.path_source == "env" for o in observations),
              "…each carrying pathSource, so a later relocation is a config change")
        check(all(o.session_key == "622-10028" for o in observations),
              "…and a sessionKey derived from the containing path")
        check(all(o.session_key_source == "path" for o in observations),
              "…recorded as derived, never presented as something the writer stated")
        check(observations[0].ref_id == cs.slot_id("622-10028", "auth", "auth_impl1", 1),
              "…pointing at the SLOT: a control intent addresses an agent by name (R-53)")
        state = replay(observations)
        check(ms.counts(state)["custom_state_events"] == 2,
              "…and both land in the one events collection, kind-discriminated")


def skill_control_lines():
    """The two control shapes, lifted from SKILL.md's own text.

    Deliberately NOT restated here: the previous attempt's fixture carried
    `root` and `attempt` fields that no control writer emits, so the ingest arm
    read zero lines of the only format that exists and the test still passed.
    Taking the shapes from the file that specifies them is the only version of
    this fixture that can catch that.
    """
    text = SKILL.read_text(encoding="utf-8")
    out = {}
    for match in re.finditer(r"\{[^{}\n]*\}", text):
        try:
            payload = json.loads(match.group(0))
        except ValueError:
            continue
        if not isinstance(payload, dict) or "name" not in payload:
            continue
        if "action" in payload and "intent" not in out:
            out["intent"] = payload
        elif "ack" in payload and "ack" not in out:
            out["ack"] = payload
    return out


def write_lines(path, payloads):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write((payload if isinstance(payload, str) else json.dumps(payload)) + "\n")
    return path


def ledger_line(name, *, root="auth", attempt=1, session=SESSION_KEY):
    return {"name": name, "parent": root, "root": root, "role": "impl",
            "attempt": attempt, "taskId": f"t-{name}-{attempt}",
            "sessionKey": session, "ts": WIRE}


def test_a_control_line_in_the_skill_files_own_shape_is_ingested():
    print("test_a_control_line_in_the_skill_files_own_shape_is_ingested")
    shapes = skill_control_lines()
    check(set(shapes) == {"intent", "ack"},
          f"SKILL.md still specifies both control shapes: {sorted(shapes)}")
    check(not any(key in shapes["intent"] for key in ("root", "sessionKey", "attempt")),
          f"…and an intent line carries a NAME and nothing else of the address: "
          f"{sorted(shapes['intent'])} — which is why `slots` exists")

    with Temp() as root:
        base = os.path.dirname(root)
        # The ledger channel: the same agent, retried — attempt 3 is the live one.
        ledger = write_lines(os.path.join(base, "task", "state", cs.LEDGER_FILE),
                             [ledger_line("auth_impl1", attempt=1),
                              ledger_line("auth_impl1", attempt=3),
                              ledger_line("auth_review1")])
        index = cs.SlotIndex(cs.read_ledger_file(ledger))
        check(len(index) == 3, f"three spawns observed, indexed by name: {len(index)}")

        intent = dict(shapes["intent"], name="auth_impl1")
        ack = dict(shapes["ack"], name="auth_impl1", taskId="t-auth_impl1-3",
                   result="stopped", ts=WIRE)
        control = write_lines(os.path.join(base, "elsewhere", "c.jsonl"), [intent, ack])
        counters = cs.new_counters()
        observations = cs.read_control_file(control, "env", slots=index, counters=counters)

        check([o.kind for o in observations] == ["control_intent", "control_ack"],
              f"the documented shapes are INGESTED, not dropped: "
              f"{[o.kind for o in observations]}")
        expected = cs.slot_id("622-10028", "auth", "auth_impl1", 3)
        check([o.ref_id for o in observations] == [expected, expected],
              f"…addressed through the name→slot hop, to the LIVE attempt: "
              f"{[o.ref_id for o in observations]}")
        check(all(o.attempt_source == "resolved" for o in observations),
              "…with the inference recorded (attemptSource), never presented as stated")
        check(all(o.session_key == "622-10028" and o.session_key_source == "slots"
                  for o in observations),
              f"…and the session comes from the slot, attributed to it: "
              f"{[o.session_key_source for o in observations]}")
        check(not expected.endswith("|001"),
              f"the old default of attempt 1 is gone: a stop addressed to a slot that "
              f"ended two attempts ago is worse than a skipped line: {expected}")
        check(counters["parsed"] == 2 and counters["read"] == 2,
              f"…and the reader counts what it read and parsed: {counters}")

        # A stated attempt is obeyed rather than resolved.
        stated = write_lines(os.path.join(base, "stated", "c.jsonl"),
                             [dict(intent, attempt=1)])
        first = cs.read_control_file(stated, "env", slots=index)
        check(first and first[0].ref_id == cs.slot_id("622-10028", "auth", "auth_impl1", 1)
              and first[0].attempt_source == "stated",
              f"a line that states its attempt is taken at its word: "
              f"{first and first[0].attempt_source}")
        check(first and first[0].session_key_source == "slots",
              f"…and with nothing on the line or in the path to say WHOSE session it is, "
              f"the attribution is the hop that supplied it: "
              f"{first and first[0].session_key_source}")

        # The other two branches of the same honesty field: a line that states
        # its own session, and one whose session came out of the containing
        # path. Labelling either of them `slots` would credit Touch with an
        # attribution the writer made (CUSTOMSTATE-10).
        whole = write_lines(os.path.join(base, "whole", "c.jsonl"),
                            [dict(intent, root="auth", attempt=3, sessionKey="622-10028")])
        told = cs.read_control_file(whole, "env", slots=index)
        check(told and told[0].session_key_source == "ledger"
              and told[0].attempt_source == "stated",
              f"a line that states its sessionKey is attributed to the writer: "
              f"{told and told[0].session_key_source}")
        under_session = write_lines(
            os.path.join(base, "sessions", "622-10028", "c.jsonl"),
            [dict(intent, root="auth", attempt=3)])
        walked = cs.read_control_file(under_session, "env", slots=index)
        check(walked and walked[0].session_key == "622-10028"
              and walked[0].session_key_source == "path",
              f"…and one whose session came from the containing directory says so: "
              f"{walked and walked[0].session_key_source}")

        # Both of those state their whole address, so they take the branch that
        # needs no hop. The SAME three labels have to hold on the resolved
        # branch, where the attempt comes from the index: a line that stated its
        # own session must not be credited to the hop just because the hop
        # supplied the attempt (CUSTOMSTATE-10 — the field is about who said
        # what, not about which branch computed it).
        half_told = write_lines(os.path.join(base, "half", "c.jsonl"),
                                [dict(intent, sessionKey="622-10028")])
        half = cs.read_control_file(half_told, "env", slots=index)
        check(half and half[0].session_key_source == "ledger"
              and half[0].attempt_source == "resolved",
              f"a line that states its sessionKey but leaves the attempt to the hop is "
              f"still attributed to the writer: {half and half[0].session_key_source} / "
              f"{half and half[0].attempt_source}")
        half_walked = write_lines(
            os.path.join(base, "sessions", "622-10028", "half.jsonl"), [dict(intent)])
        walked_half = cs.read_control_file(half_walked, "env", slots=index)
        check(walked_half and walked_half[0].session_key_source == "path"
              and walked_half[0].attempt_source == "resolved",
              f"…and one under a session directory keeps `path` on the same branch, "
              f"rather than reporting the hop that only supplied its attempt: "
              f"{walked_half and walked_half[0].session_key_source}")
        check({o.session_key_source for o in observations} == {"slots"},
              f"…which leaves `slots` for the case it is actually true of: a line with "
              f"nothing on it and nothing in its path: "
              f"{{{[o.session_key_source for o in observations]}}}")

        # And the mapper accepts the result: an addressed line becomes a document.
        state = replay(observations)
        check(ms.counts(state)["custom_state_events"] == 2,
              "…and both land in the one events collection")
        doc = sorted(state["custom_state_events"].values(), key=lambda d: d["seq"])[0]
        check(doc["attemptSource"] == "resolved" and doc["refId"] == expected,
              f"…carrying the address AND how it was reached: {doc.get('attemptSource')}")


def test_an_unaddressable_control_line_is_skipped_and_counted():
    print("test_an_unaddressable_control_line_is_skipped_and_counted")
    shapes = skill_control_lines()
    with Temp() as root:
        base = os.path.dirname(root)
        ledger = write_lines(os.path.join(base, "task", "state", cs.LEDGER_FILE),
                             [ledger_line("auth_impl1", attempt=2),
                              ledger_line("dup_name", root="alpha"),
                              ledger_line("dup_name", root="beta")])
        index = cs.SlotIndex(cs.read_ledger_file(ledger))

        control = write_lines(os.path.join(base, "c", "c.jsonl"), [
            dict(shapes["intent"], name="nobody_spawned_this"),
            dict(shapes["intent"], name="dup_name"),
            "{ not json",
            json.dumps(["not", "an", "object"]),
            json.dumps({"name": "auth_impl1"}),            # neither action nor ack
            dict(shapes["intent"], name="auth_impl1", attempt="two"),
            dict(shapes["intent"], name="auth_impl1"),
        ])
        counters = cs.new_counters()
        observations = cs.read_control_file(control, "env", slots=index, counters=counters)
        check([o.ref_id for o in observations]
              == [cs.slot_id("622-10028", "auth", "auth_impl1", 2)],
              f"only the addressable line survives: {[o.ref_id for o in observations]}")
        check(counters["skipped_unaddressable"] == 1,
              f"a name nobody spawned is skipped and COUNTED, never invented: {counters}")
        check(counters["skipped_ambiguous"] == 1,
              f"…a name observed under two roots is ambiguous, and Touch does not pick "
              f"one (CUSTOMSTATE-10): {counters}")
        check(counters["skipped_malformed"] == 4,
              f"…and the unparsable, the non-object, the shapeless and the non-integer "
              f"attempt are counted apart from them: {counters}")
        check(counters["read"] == 7 and counters["parsed"] == 1
              and sum(counters[k] for k in counters if k.startswith("skipped")) == 6,
              f"every line read is accounted for — 'nothing happened yet' and "
              f"'everything I wrote was rejected' are distinguishable (D13): {counters}")
        check([o.seq for o in observations] == [7],
              f"…and a skipped line never renumbers its successors: "
              f"{[o.seq for o in observations]}")

        # With no slot set at all the arm is honest about it rather than silent.
        empty = cs.new_counters()
        check(cs.read_control_file(control, "env", counters=empty) == [],
              "with nothing observed, nothing is addressable")
        check(empty["skipped_unaddressable"] + empty["skipped_ambiguous"] == 3
              and empty["read"] == 7,
              f"…and the counters say exactly that, rather than reading as idle: {empty}")

        missing = cs.new_counters()
        check(cs.read_control_file(os.path.join(base, "nope.jsonl"), "env",
                                   counters=missing) == [],
              "a control path that is not there yields nothing")
        check(missing["unreadable"] == 1 and missing["read"] == 0,
              f"…counted as unreadable, which is not the same as empty: {missing}")


def test_two_control_files_under_like_named_folders_do_not_collide():
    print("test_two_control_files_under_like_named_folders_do_not_collide")
    shapes = skill_control_lines()
    with Temp() as root:
        base = os.path.dirname(root)
        ledger = write_lines(os.path.join(base, "task", "state", cs.LEDGER_FILE),
                             [ledger_line("auth_impl1")])
        index = cs.SlotIndex(cs.read_ledger_file(ledger))
        line = dict(shapes["intent"], name="auth_impl1")
        first = write_lines(os.path.join(base, "taskA", "state", "c.jsonl"), [line])
        second = write_lines(os.path.join(base, "taskB", "state", "c.jsonl"), [line])
        a = cs.read_control_file(first, "env", slots=index)
        b = cs.read_control_file(second, "env", slots=index)
        check(a and b and a[0].stream != b[0].stream,
              f"two files whose parent folders share a name are two streams:\n"
              f"      {a[0].stream}\n      {b[0].stream}")
        state = replay(a + b)
        check(ms.counts(state)["custom_state_events"] == 2,
              f"…so line 1 of each is a document, not one swallowed as a tolerated "
              f"duplicate of the other: {ms.counts(state)}")
        again = cs.read_control_file(first, "env", slots=index)
        check(again[0].stream == a[0].stream,
              "…while the same file always scopes to the same stream (the digest is "
              "of the resolved path, not of the run)")


def test_the_name_to_slot_index_is_built_once_per_backfill_not_once_per_file():
    print("test_the_name_to_slot_index_is_built_once_per_backfill_not_once_per_file")
    root = tempfile.mkdtemp(prefix="touch-custom-state-memo-")
    original = cs.read_ledger_file
    try:
        ledgers = [write_lines(os.path.join(root, f"task{n}", "state", cs.LEDGER_FILE),
                               [ledger_line(f"auth_impl{n}")]) for n in (1, 2, 3)]
        env = {cs.LEDGER_PATHS_ENV: os.pathsep.join(ledgers)}
        cs._SLOT_INDEX_MEMO.update(signature=None, index=None)

        first = cs.slot_index(env=env)
        check(len(first) == 3, f"three ledgers, three slots indexed: {len(first)}")
        check(cs.slot_index(env=env) is first,
              "an unchanged ledger set answers from the memo — the rebuild seam is off "
              "the liveness path, but a --backfill over n control files should not walk "
              "every ledger n times")
        check(cs.slot_index(env=env, memo=False) is not first,
              "…and a caller that would rather pay than reason about a cache can say so")
        write_lines(ledgers[0], [ledger_line("auth_impl1"), ledger_line("auth_impl9")])
        rebuilt = cs.slot_index(env=env)
        check(rebuilt is not first and len(rebuilt) == 4,
              f"…while an appended ledger invalidates it, because the signature is the "
              f"files' own (mtime, size): {len(rebuilt)}")

        # The call site the nit was actually about: one index for the whole walk.
        calls = []

        def counting(path, **kwargs):
            calls.append(path)
            return original(path, **kwargs)

        cs.read_ledger_file = counting
        controls = [write_lines(os.path.join(root, f"c{n}", "c.jsonl"),
                                [{"action": "stop", "name": f"auth_impl{n}"}])
                    for n in (1, 2, 3)]
        env = dict(env, **{cs.CONTROL_PATHS_ENV: os.pathsep.join(controls)})
        cs._SLOT_INDEX_MEMO.update(signature=None, index=None)
        for path in controls:
            cs.iter_custom_state_observations(path=path, root=root, env=env)
        check(len(calls) == 3,
              f"walking three control files reads each ledger ONCE, not once per file "
              f"(3, not 9): {len(calls)}")
        calls.clear()
        cs.iter_custom_state_observations(path=controls[0], root=root, env=env,
                                          slots=cs.SlotIndex())
        check(calls == [],
              f"…and a driver that already holds an index is not made to rebuild one: "
              f"{calls}")
    finally:
        cs.read_ledger_file = original
        cs._SLOT_INDEX_MEMO.update(signature=None, index=None)
        shutil.rmtree(root, ignore_errors=True)


def test_a_session_key_is_only_derived_under_a_directory_the_layout_names():
    print("test_a_session_key_is_only_derived_under_a_directory_the_layout_names")
    check(cs.session_key_from_path("/srv/sessions/622-10028/state/x.jsonl") == "622-10028",
          "a `<pid>-<procStart>` under a session directory is the session (CUSTOMSTATE-10)")
    for path in ("/srv/reports/2026-07/x.jsonl", "/srv/1-2/x.jsonl",
                 "/home/u/proj/622-10028/x.jsonl"):
        check(cs.session_key_from_path(path) is None,
              f"…while a date-, version- or otherwise-named folder is NOT a session, "
              f"however well it matches the grammar: {path}")
    check(cs.SESSION_PATH_PARENTS == ("sessions", "session", ".touch"),
          f"the directories that may name a session are declared once, and the tuple is "
          f"pinned WHOLE: widening it (a `state/` or `runs/` entry) turns every "
          f"`<int>-<int>` folder under that name into a phantom session with an "
          f"addressable, never-bindable slot, which is the failure the tuple exists to "
          f"prevent — and removing an entry only loses attribution: {cs.SESSION_PATH_PARENTS}")
    for parent in ("state", "runs", "reports", "plan"):
        path = f"/srv/{parent}/622-10028/x.jsonl"
        check(cs.session_key_from_path(path) is None,
              f"…so a `{parent}/` folder is not a session directory: {path}")
    check("slots" in cs.SESSION_KEY_SOURCES and "path" in cs.SESSION_KEY_SOURCES,
          f"…and every way a session can be reached is nameable on the document: "
          f"{cs.SESSION_KEY_SOURCES}")


def test_a_ref_and_a_refid_that_disagree_are_refused():
    print("test_a_ref_and_a_refid_that_disagree_are_refused")
    other = refs.agent_key(AGENT2) if hasattr(refs, "agent_key") else AGENT2
    check(raises(RefRejected, cs.resolve_ref_id, agent_ref(AGENT), other),
          "a ref{} and a refId pointing at different entities is refused, not stored: "
          "every reader joins on one of them and none of them compares (GD-24)")
    check(cs.resolve_ref_id(agent_ref(AGENT), AGENT) == AGENT,
          "…while agreeing halves pass, and the scalar is what is stored")
    with Temp() as root:
        writer = Writer(root=root)
        check(raises(RefRejected, writer.append, "agent_state", state_key="s",
                     ref=agent_ref(AGENT), ref_id=other, custom={}),
              "…and the WAL never holds the contradiction either")
        check(writer.records() == [], "…nothing was written")

    # A ref this module cannot key is a rejection, and it has to leave through
    # this module's own hierarchy: `mirror.Mapper` converts `CustomStateError`
    # into a counted refusal and lets anything else escape the ingest tick.
    record = {"kind": "annotation", "seq": 1,
              "ref": {"kind": "agent", "agentId": "not-seventeen-hex"},
              "data": {"stateKey": "annotation:a1"}}
    try:
        CustomStateObservation.from_record(record)
        raised = None
    except BaseException as exc:                                # noqa: BLE001
        raised = exc
    check(isinstance(raised, CustomStateError)
          and not isinstance(raised, refs.RefError),
          f"a WAL record whose ref{{}} cannot be keyed leaves through THIS module's "
          f"hierarchy, so one malformed line is a counted refusal rather than a foreign "
          f"exception out of the tick: {type(raised).__name__}")


def test_the_head_and_the_bind_have_a_named_driver_handoff():
    print("test_the_head_and_the_bind_have_a_named_driver_handoff")
    docstring = ast.get_docstring(TREE) or ""
    for phrase in ("head_write", "bind_slot", "test-only surface"):
        check(phrase in docstring,
              f"the module docstring states the handoff in its own words ({phrase!r})")
    check("sp-12" in docstring,
          "…and NAMES the sub-plan that must drive them, rather than leaving the gap "
          "to be discovered by whoever queries an empty head")
    have_note(DEVIATION,
              f"…with the same handoff recorded where the run's other deviations are: "
              f"{DEVIATION.name}")
    drivers = {"head_write", "bind_slot", "rebuild_heads", "apply_guarded"}
    callers = []
    for module in sorted((REPO / "aggregator").glob("*.py")):
        if module.name == "custom_state.py":
            continue
        text = module.read_text(encoding="utf-8")
        callers.extend(f"{module.name}:{name}" for name in drivers if f"{name}(" in text)
    check(not callers,
          f"…and the gap is real as documented — no other module drives them yet: "
          f"{callers}")

    # The second handoff, same shape: the `slots` sets this module builds must
    # be declared by the spec that owns them, and that spec is sp-05's file.
    for phrase in ("SLOT_SET_FIELDS", "set_fields", "sp-05"):
        check(phrase in docstring,
              f"the docstring states the set_fields handoff too ({phrase!r})")
    if have_note(SET_FIELDS_DEVIATION,
                 f"…recorded beside the other deviation: {SET_FIELDS_DEVIATION.name}"):
        text = SET_FIELDS_DEVIATION.read_text(encoding="utf-8")
        check(all(name in text for name in cs.SLOT_SET_FIELDS)
              and "mongo_store.py" in text and "sp-05" in text,
              "…naming the owner and the exact tuple, so the fix is a paste rather than a "
              "re-derivation")

    # The third, same shape: the head's order field wants `custom_state`'s
    # accumulable fence, and that spec is sp-05's too.
    check("HEAD_ORDER_FIELD" in docstring and "accumulable" in docstring,
          "the docstring states the head-order handoff as well")
    if have_note(HEAD_ORDER_DEVIATION,
                 f"…recorded beside the others: {HEAD_ORDER_DEVIATION.name}"):
        order_text = HEAD_ORDER_DEVIATION.read_text(encoding="utf-8")
        check(cs.HEAD_ORDER_FIELD in order_text and "accumulable" in order_text
              and "mongo_store.py" in order_text and "sp-05" in order_text,
              f"…naming the owner and the exact line, {cs.HEAD_ORDER_FIELD!r} included")
    check(ms.spec_for("custom_state").accumulable >= {"seq"},
          "…while `seq`'s own fence is already there, which is what the note asks the "
          "order to join")


# --- the seams this module shares with others ------------------------------


def test_stream_escaping_agrees_with_the_legacy_arm():
    print("test_stream_escaping_agrees_with_the_legacy_arm")
    names = ["touch-mongo-live", "a b/c", "täsk", "x%y", "with#hash|pipe:colon"]
    for name in names:
        check(cs._stream_safe(name) == legacy._stream_safe(name),
              f"the outer stream escaping is byte-identical to legacy.py's for {name!r}")
        check(cs._stream_unsafe(cs._stream_safe(name)) == name,
              f"…and round-trips: {name!r}")
        stream = cs.ledger_stream(name)
        check(cs.scope_of_stream(stream) == name,
              f"ledger_stream inverts exactly: {stream}")
        check(refs.custom_state_event_key(stream, 1).startswith(refs.escape_stream(stream)),
              "…and the escaped id is what refs would build from it")
    check(raises(CustomStateError, cs.control_stream, "x" * 300),
          "a scope too long to key a stream is refused, never truncated into a collision")


def test_the_guard_matcher_agrees_with_the_mirrors():
    print("test_the_guard_matcher_agrees_with_the_mirrors")
    cases = [
        ({}, {"seq": {"$lt": 5}}),
        ({"seq": 3}, {"seq": {"$lt": 5}}),
        ({"seq": 7}, {"seq": {"$lt": 5}}),
        ({"resolutionRank": 2}, {"resolutionRank": {"$lt": 2}}),
        ({"resolutionRank": 0}, {"resolutionRank": {"$lt": 2}}),
        ({"a": 1}, {"a": 1}),
        ({}, {"a": {"$ne": 1}}),
        ({}, {"a": {"$exists": False}}),
    ]
    for doc, require in cases:
        check(cs._guard_matches(doc, require) == mr._matches(doc, require),
              f"the in-memory guard matches mirror._matches for {doc} vs {require}")
    check(cs._guard_matches({}, {"seq": {"$lt": 5}}) is False,
          "…including Mongo's missing-field rule: {seq:{$lt:n}} does not match a fresh head")


def test_the_module_is_pure_and_carries_no_driver():
    print("test_the_module_is_pure_and_carries_no_driver")
    check("pymongo" not in SOURCE,
          "custom_state.py names no Mongo driver (GD-21: only mongo_store and mirror may)")
    functions = {node.name: node for node in ast.walk(TREE)
                 if isinstance(node, ast.FunctionDef)}
    io_names = {"open", "read_all", "stat", "listdir", "walk", "makedirs"}
    for name in ("map_custom_state", "map_slot", "head_write", "bind_write",
                 "orphan_write", "conflict_write", "_event_document"):
        node = functions.get(name)
        check(node is not None, f"{name} exists")
        calls = {getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                 for call in ast.walk(node) if isinstance(call, ast.Call)}
        check(not (calls & io_names),
              f"{name} does no I/O — SD-1 mappers are pure ({sorted(calls & io_names)})")
    check("MIRROR_MAPPERS" in SOURCE and "MIRROR_SOURCES" in SOURCE,
          "…and both SD-1 registries are declared beside each other")
    registry = mr.discover_mappers()
    check({"customState", "slot"} <= set(registry),
          f"mirror.discover_mappers finds both kinds: {sorted(registry)}")
    check(len({k for k in registry if registry[k].module == "custom_state"}) == 2,
          "…owned by this module, and no kind is registered twice (GD-15)")


def test_the_wal_stream_is_the_durable_one_store_already_names():
    print("test_the_wal_stream_is_the_durable_one_store_already_names")
    check(cs.WAL_STREAM in store_mod.DURABLE_STREAMS,
          "`custom-state` is fsync'd per append: it is the one dataset a rebuild "
          "from ~/.claude cannot reconstruct (R-52/GD-22)")
    with Temp() as root:
        writer = Writer(root=root)
        writer.append("agent_state", state_key="s", ref=agent_ref(), custom={}, ts=WIRE)
        path = writer.store.stream_path(cs.WAL_STREAM)
        check(os.path.basename(path) == "custom-state.jsonl" and os.path.isfile(path),
              f"…written to `.touch/custom-state.jsonl` (D5): {path}")
        with open(path, "r", encoding="utf-8") as handle:
            line = json.loads(handle.readline())
        check(tuple(line) == store_mod.RECORD_KEYS,
              f"…as a touch-events-v2 record in the fixed key order: {tuple(line)}")
        check(line["source"] == cs.SOURCE,
              f"…on a source slug store.py's open tail was designed for: {line['source']!r}")


def test_store_py_was_not_edited_by_this_sub_plan():
    print("test_store_py_was_not_edited_by_this_sub_plan")
    text = (REPO / "aggregator" / "store.py").read_text(encoding="utf-8")
    check("custom-state" in text and "R-52" in text,
          "store.py already anticipated this WAL — R-52 rides its existing machinery")
    imports = [node for node in ast.walk(ast.parse(text))
               if isinstance(node, (ast.Import, ast.ImportFrom))
               for alias in node.names if "custom_state" in alias.name]
    check(not imports,
          "…and it does not import this module: the dependency runs one way only "
          "(sp-04 owns store.py, and this sub-plan left it untouched)")


# --- live arm (skips cleanly) ---------------------------------------------


def live_database():
    """(db, client, name) against `TOUCH_MONGO_URI`, or (None, None, reason)."""
    uri = os.environ.get("TOUCH_MONGO_URI")
    if not uri:
        return None, None, "TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)"
    if not ms.pymongo_available():
        return None, None, "the driver is not installed (GD-21: absence is legal)"
    try:
        client = ms.open_client(uri)
    except ms.MongoUnavailable as exc:
        return None, None, str(exc)
    if not ms.ping(client):
        client.close()
        return None, None, "no mongod answered within the GD-21 timeouts"
    name = f"touch_test_{os.getpid()}"
    return client[name], client, name


def test_live_head_guard_matches_the_model():
    print("test_live_head_guard_matches_the_model")
    db, client, name = live_database()
    if db is None:
        skip(f"live custom-state arm: {name}")
        return
    try:
        ms.ensure_schema(db, collections=["custom_state_events", "custom_state"])
        with Temp() as root:
            observations = wal_with_three(root).observations()
            ms.bulk_upsert(db, "custom_state_events",
                           [(key, update) for _c, key, update in
                            (op for obs in observations for op in map_custom_state(obs))])
            for index in (2, 0, 1):                 # newest first, then the stale ones
                write = head_write(observations[index])
                ms.guarded_update(db, write.collection, write.key, write.update,
                                  require=write.require)
            stored = db["custom_state"].find_one()
            check(stored is not None and stored["seq"] == 3,
                  f"mongod's guard agrees with the model: seq={stored and stored.get('seq')}")
            check(json.loads(stored["data"]["custom"][ms.RAW_FIELD])["value"] == 3,
                  "…and the payload is the newest event's, not the last one attempted")
            check(db["custom_state_events"].count_documents({}) == 3,
                  "…with all three lines still in the append-only log")

            # The cross-stream tie, against the server: the guard is now a `$lt`
            # on a STRING, and mongod's comparison has to agree with the model's
            # or the two disagree about which event owns the head.
            def tie(agent, note, stream):
                return CustomStateObservation(
                    kind="agent_state", stream=stream, seq=1, state_key="tie",
                    ref_id=agent, custom={"note": note}, ts=WIRE)

            streams = (cs.WAL_STREAM, cs.control_stream("tie"))
            winner = max(streams, key=lambda s: cs.head_order(s, 1))
            for agent, order in ((AGENT, streams), (AGENT2, tuple(reversed(streams)))):
                for stream in order:
                    write = head_write(tie(agent, stream, stream))
                    ms.guarded_update(db, write.collection, write.key, write.update,
                                      require=write.require)
            stored = [db["custom_state"].find_one({"_id": cs.head_id(agent, "tie")})
                      for agent in (AGENT, AGENT2)]
            check(all(doc and doc["stream"] == winner for doc in stored),
                  f"mongod resolves the tie the same way in both directions — the order "
                  f"decides it, not the arrival: "
                  f"{[doc and doc.get('stream') for doc in stored]}")
            check(stored[0][cs.HEAD_ORDER_FIELD] == stored[1][cs.HEAD_ORDER_FIELD]
                  == cs.head_order(winner, 1),
                  "…and the stored order is the one the model computes")
    finally:
        if name.startswith("touch_test_"):
            client.drop_database(name)          # only the name we constructed (GD-12/GD-27)
        client.close()


TESTS = [
    test_three_out_of_order_writes_leave_the_head_at_the_highest_seq,
    test_a_late_old_write_never_clobbers_a_fresher_head,
    test_two_streams_that_share_a_seq_still_leave_one_head,
    test_every_head_write_carries_one_fixed_key_set,
    test_a_wal_line_cannot_smuggle_an_author_past_the_read_door,
    test_an_unknown_refid_is_rejected,
    test_a_mongo_wipe_plus_wal_replay_reproduces_both_collections,
    test_drop_the_head_rebuild_and_it_is_document_for_document_equal,
    test_the_writer_has_no_code_path_to_a_mirrored_fact_provenance,
    test_annotations_reject_at_16kb_rather_than_truncating,
    test_deletes_are_tombstone_events_and_no_delete_verb_exists,
    test_the_events_collection_is_insert_only_and_installation_wide,
    test_the_module_writes_only_its_own_three_collections,
    test_control_paths_are_configured_and_the_path_is_never_restated,
    test_a_control_line_in_the_skill_files_own_shape_is_ingested,
    test_an_unaddressable_control_line_is_skipped_and_counted,
    test_two_control_files_under_like_named_folders_do_not_collide,
    test_the_name_to_slot_index_is_built_once_per_backfill_not_once_per_file,
    test_a_session_key_is_only_derived_under_a_directory_the_layout_names,
    test_a_ref_and_a_refid_that_disagree_are_refused,
    test_the_head_and_the_bind_have_a_named_driver_handoff,
    test_stream_escaping_agrees_with_the_legacy_arm,
    test_the_guard_matcher_agrees_with_the_mirrors,
    test_the_module_is_pure_and_carries_no_driver,
    test_the_wal_stream_is_the_durable_one_store_already_names,
    test_store_py_was_not_edited_by_this_sub_plan,
    test_live_head_guard_matches_the_model,
]


if __name__ == "__main__":
    for test in TESTS:
        test()
    print()
    for message in skipped:
        print(f"skipped: {message}")
    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for message in failures:
            print(f"  - {message}")
        sys.exit(1)
    print("\nall custom-state checks passed")
