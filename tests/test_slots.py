#!/usr/bin/env python3
"""Stdlib-only tests for the `slots` arm of aggregator/custom_state.py (R-53,
the SINGLE name↔agentId hop).
Run as `python3 test_slots.py`; exits non-zero on failure. No pytest.

R-53's own test list, one test per clause:

* pre-spawn state keyed by NAME binds when the marker lands (pending → bound);
* a duplicate bind ⇒ a `conflict` document carrying BOTH agentIds, and the
  process is still alive — `DuplicateKeyError` is caught and counted, never
  raised, because the data that causes it is agent-authored and an ingest loop
  that a subagent can kill is not an ingest loop (CUSTOMSTATE-9);
* two same-named roots in DIFFERENT sessions do not cross-link (`sessionKey` is
  the first component of the `_id` — CUSTOMSTATE-10);
* a markerless node is `orphaned` after the TTL, and immediately once its run
  is terminal — a normal outcome, rendered honestly, never hidden (D13/GD-7).

Plus the two invariants only a test can hold in place: the state machine is
**monotone**, so replaying evidence in any order lands on the same document
(GD-25); and the ledger line the skill file tells agents to write actually
carries the fields this arm keys on (`root`, `sessionKey`) — the R-53 amendment
to the `orchestrate` skill's `SKILL.md`, asserted here so the code and the
instruction cannot drift apart.

The **live arm** creates the real unique sparse `{agentId:1}` index on a mongod
and proves the collision comes back as a tolerated duplicate rather than an
exception; it skips cleanly when `TOUCH_MONGO_URI` is unset (GD-21).
"""

import ast
import contextlib
import datetime
import json
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[0]
# The canonical trees are named through `tests/_roots.py`, never by a
# literal under REPO: GD-U1 moves them and this is the single flip point.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import PAYLOAD, SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))

from aggregator import custom_state as cs                       # noqa: E402
from aggregator import mongo_store as ms                        # noqa: E402
from aggregator import refs                                     # noqa: E402
from aggregator.custom_state import (                           # noqa: E402
    BIND_CHANNELS,
    PENDING_TTL_SECONDS,
    RESOLUTION_RANK,
    RESOLUTIONS,
    SlotError,
    SlotObservation,
    SlotTable,
    bind_write,
    conflict_write,
    map_slot,
    orphan_write,
    resolution_of,
    slot_id,
)

failures = []
skipped = []

MODULE = SRC / "aggregator" / "custom_state.py"
# The skill MOVED into the shipping subtree and lost its `touch-` prefix (item
# 09: a plugin skill invokes as `/<plugin>:<skill>`, so `touch-orchestrate`
# inside a plugin named `touch` read `/touch:touch-orchestrate`). One canonical
# copy, in the payload — this constant follows it.
SKILL = PAYLOAD / "skills" / "orchestrate" / "SKILL.md"
AGENT = "a2fc883c96ff7b837"
AGENT2 = "b1de44f0c1e2a3b45"
SESSION = "622-10028"                 # <pid>-<procStart>, the live session key
OTHER_SESSION = "700-99312"
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


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception as other:                                  # noqa: BLE001
        print(f"    (raised {type(other).__name__}: {other})")
        return False
    return False


def later(seconds):
    return T0 + datetime.timedelta(seconds=seconds)


def slot_obs(**overrides):
    fields = dict(session_key=SESSION, root="auth", name="auth_impl1", attempt=1,
                  role="impl", parent="auth", ts=T0)
    fields.update(overrides)
    return SlotObservation(**fields)


@contextlib.contextmanager
def slot_sets_declared():
    """Declare this module's `$addToSet` sets on the `slots` spec, temporarily.

    GD-25's oracle (`mongo_store.fingerprint`) sorts an array only when the
    owning `CollectionSpec` names it in `set_fields`; `slots` names none, so two
    replay orders of the same bind evidence fingerprint differently purely
    because `agentIds`/`evidence` land in arrival order. The one-line correction
    belongs to `aggregator/mongo_store.py`, which sp-05 owns and this sub-plan
    may not edit — it is recorded, with the exact tuple, in
    `findings/sp-custom-state-slots-set-fields-deviation.md`.

    Installing it here (and restoring it after) is deliberate: it makes the
    assertion below measure THIS module's order-independence rather than the
    missing declaration, it prints when it had to patch so the deviation is
    never silent, and the day sp-05 pastes the tuple this becomes a no-op
    instead of a test that has to change.
    """
    spec = ms.spec_for("slots")
    before = spec.set_fields
    missing = sorted(set(cs.SLOT_SET_FIELDS) - set(before))
    if missing:
        print(f"    (deviation: mongo_store's `slots` spec declares set_fields=(); "
              f"installing {missing} for this assertion — sp-05's one-line paste)")
        spec.set_fields = frozenset(set(before) | set(cs.SLOT_SET_FIELDS))
    try:
        yield missing
    finally:
        spec.set_fields = before


class Marker:
    """An `agents.Labels`-shaped stand-in (the duck type `slot_from_labels` reads)."""

    def __init__(self, **fields):
        for name in ("name", "parent", "root", "role", "attempt"):
            setattr(self, name, fields.get(name))


# --- the state machine ----------------------------------------------------


def test_pre_spawn_state_binds_when_the_marker_lands():
    print("test_pre_spawn_state_binds_when_the_marker_lands")
    table = SlotTable()
    obs = slot_obs()
    table.observe(obs)
    key = obs.key
    check(key == "slot:622-10028|auth|auth_impl1|001",
          f"the slot _id is sessionKey-first, attempt zero-padded (GD-24): {key}")
    doc = table.slot(key)
    check(doc["resolution"] == "pending" and doc["resolutionRank"] == 0,
          f"a slot with no agentId starts pending: {doc['resolution']}")
    check(doc["pendingSince"] == T0,
          "…with pendingSince, so 'how long has this been waiting' is answerable")
    check("agentId" not in doc,
          "…and NO agentId: it is unknowable before spawn, which is why slots exist")

    result = table.bind(key, AGENT, by="marker", at=later(2), task_id="task-77")
    check(result.resolution == "bound", f"the marker binds it: {result.resolution}")
    doc = table.slot(key)
    check(doc["agentId"] == AGENT and doc["boundBy"] == "marker",
          f"…recording which channel bound it: {doc.get('boundBy')}")
    check(doc["resolution"] == "bound" and doc["resolutionRank"] == RESOLUTION_RANK["bound"],
          "…and the state machine advanced, rank and word together")
    check(doc["taskId"] == "task-77",
          "…carrying the taskId that makes a stop actionable (GD-8's Agent-tool profile)")
    check(table.counters["bound"] == 1 and table.counters["conflict"] == 0,
          f"…counted once: {table.counters}")

    # The name channel is the point: custom state addressed the slot by NAME
    # before the agentId existed, and the very same _id now joins to the agent.
    check(cs.validate_ref_id(key) == key,
          "a slot key is a legal custom-state refId, so state written pre-spawn joins")


def test_the_bind_is_idempotent_and_never_demoted():
    print("test_the_bind_is_idempotent_and_never_demoted")
    table = SlotTable()
    obs = slot_obs()
    table.observe(obs)
    table.bind(obs.key, AGENT, by="marker", at=later(2))
    before = dict(table.slot(obs.key))
    table.bind(obs.key, AGENT, by="ledger", at=later(3))
    check(table.slot(obs.key)["agentId"] == AGENT,
          "re-binding the same agentId changes nothing that matters")
    check(table.slot(obs.key)["boundBy"] == before["boundBy"],
          "…and the guard keeps the first channel: rank 2 is not < rank 2")
    table.observe(slot_obs(ts=later(5)))
    check(table.slot(obs.key)["resolution"] == "bound",
          "a late `pending` observation cannot undo a bind (monotone rank, GD-25)")
    check(resolution_of(table.slot(obs.key), now=later(10_000)) == "bound",
          "…and no amount of elapsed time orphans a bound slot")


def test_a_duplicate_bind_writes_a_conflict_with_both_ids_and_the_process_lives():
    print("test_a_duplicate_bind_writes_a_conflict_with_both_ids_and_the_process_lives")
    # (a) two agentIds for ONE slot — the copy-pasted marker.
    table = SlotTable()
    first = slot_obs()
    table.observe(first)
    table.bind(first.key, AGENT, by="marker", at=later(1))
    result = table.bind(first.key, AGENT2, by="marker", at=later(2))
    check(result.resolution == "conflict",
          f"a second, different agentId for one slot is a conflict: {result.resolution}")
    doc = table.slot(first.key)
    check(sorted(doc["conflictAgentIds"]) == sorted([AGENT, AGENT2]),
          f"…recording BOTH ids, so the render can show what collided: "
          f"{doc.get('conflictAgentIds')}")
    check(doc["resolution"] == "conflict" and doc["agentId"] == AGENT,
          "…and the losing bind never rewrote agentId (the unique index is untouched)")

    # (b) ONE agentId claimed by two slots — the unique sparse index's own case.
    second = slot_obs(name="auth_impl2")
    table.observe(second)
    result = table.bind(second.key, AGENT, by="ledger", at=later(3))
    check(result.duplicate_key is True,
          "a second slot claiming a bound agentId is the duplicate-key case")
    check(result.resolution == "conflict",
          f"…answered with a conflict document: {result.resolution}")
    doc = table.slot(second.key)
    check(doc["conflictAgentIds"] == [AGENT] and doc["conflictWith"] == [first.key],
          f"…naming the agentId AND the slot already holding it: {doc.get('conflictWith')}")
    check(table.counters["duplicate_key"] == 1 and table.counters["conflict"] == 2,
          f"…counted, both of them: {table.counters}")
    check(table.slot(first.key)["agentId"] == AGENT,
          "…and the slot that legitimately holds the id is untouched")

    # The whole point of CUSTOMSTATE-9: none of that raised.
    third = slot_obs(name="auth_impl3")
    table.observe(third)
    check(table.bind(third.key, AGENT2, by="description", at=later(4)).resolution
          in RESOLUTIONS,
          "the table is still working afterwards — a collision never kills the tailer")
    check(table.bind("slot:1-1|x|y|001", AGENT, by="marker").resolution == "unknown",
          "…and binding a slot nobody observed is refused, not invented")
    check(table.counters["rejected"] == 1, f"…counted too: {table.counters}")


def test_a_half_written_conflict_reads_as_a_conflict_not_as_pending():
    print("test_a_half_written_conflict_reads_as_a_conflict_not_as_pending")
    # Both conflict paths write the unguarded EVIDENCE first and the guarded
    # STATE second (deliberately: the guard cannot advance a slot that is already
    # `conflict`, and a third colliding id must still be recorded). So a process
    # that dies between the two calls — or a guard that finds nothing to advance —
    # leaves a document carrying conflict evidence and `resolution: "pending"`.
    holder = slot_obs(name="auth_impl9").key
    for label, evidence in (
            ("the cross-slot shape (this id is bound elsewhere)",
             lambda key: cs.conflict_evidence_op(key, [AGENT], conflict_with=[holder])),
            ("…and the shape with no holder to name",
             lambda key: cs.conflict_evidence_op(key, [AGENT]))):
        table = SlotTable()
        obs = slot_obs()
        table.observe(obs)
        ms.apply_operations(table.state, [evidence(obs.key)])
        doc = table.slot(obs.key)
        check(doc["resolution"] == "pending",
              f"{label}: the STORED word still says pending — the second write never "
              f"landed: {doc['resolution']}")
        check(resolution_of(doc) == "conflict",
              f"…yet the slot reads as a conflict, because only a conflict path ever "
              f"writes those fields: {resolution_of(doc)}")
        check(table.sweep(now=later(PENDING_TTL_SECONDS + 1), terminal=[obs.key]) == [],
              "…so the sweep leaves it alone rather than promoting a contested stop to "
              "`orphaned` — 'went nowhere' said about a stop that went to two agents")
        check("orphanReason" not in table.slot(obs.key),
              f"…and nothing on the document claims it did: {sorted(table.slot(obs.key))}")

    # The mapper's own evidence field is NOT that signal: `agentIds` is what an
    # observation accumulates, and a slot with one claim and no collision is an
    # ordinary pending slot. (This is why the two fields are named apart.)
    plain = SlotTable()
    seen = slot_obs(agent_id=AGENT, bound_by="marker")
    plain.observe(seen)
    doc = plain.slot(seen.key)
    check(doc["agentIds"] == [AGENT] and resolution_of(doc) == "pending",
          f"an observation's `agentIds` evidence is not a conflict: "
          f"{resolution_of(doc)}")
    check(resolution_of(dict(doc, conflictAgentIds=[AGENT])) == "conflict",
          "…while one `conflictAgentIds` entry IS one, even without a second id: the "
          "cross-slot loser records exactly one, and reading it as pending is how a "
          "contested slot became an orphan")


def test_a_bound_agent_is_registered_even_when_the_guard_does_not_fire():
    print("test_a_bound_agent_is_registered_even_when_the_guard_does_not_fire")
    # `state` is a public field: a driver may seed the table from the documents
    # the server already holds. If registration rode the rank guard, the FIRST
    # call on such a table — an idempotent re-bind of an already-bound slot —
    # would return without recording the holder, and the in-memory unique index
    # would then miss the very collision this class exists to catch.
    first = slot_obs()
    second = slot_obs(name="auth_impl2")
    stored = ms.apply_operations({}, [op for obs in (first, second)
                                      for op in map_slot(obs) if op[0] == "slots"])
    # Exactly what a server holds after a bind it made in an earlier process.
    cs.apply_guarded(stored, bind_write(first.key, AGENT, by="marker", at=T0))
    table = SlotTable(state=stored)
    check(table.slot(first.key)["agentId"] == AGENT
          and table.slot(first.key)["resolution"] == "bound",
          "the seeded table starts from a slot something else already bound")

    again = table.bind(first.key, AGENT, by="ledger", at=later(1))
    check(again.acquired is False and again.resolution == "bound",
          f"re-binding it is a no-op the result reports honestly: "
          f"acquired={again.acquired}")
    collision = table.bind(second.key, AGENT, by="marker", at=later(2))
    check(collision.resolution == "conflict" and collision.duplicate_key is True,
          f"…and the NEXT slot claiming that agentId is still answered with the "
          f"duplicate-key conflict, because the holder was registered anyway: "
          f"{collision.resolution}")
    check(table.slot(second.key)["conflictWith"] == [first.key],
          f"…naming the slot that holds it: {table.slot(second.key).get('conflictWith')}")
    check(table.slot(first.key)["agentId"] == AGENT,
          "…leaving the legitimate holder untouched")


def test_two_same_named_roots_in_different_sessions_do_not_cross_link():
    print("test_two_same_named_roots_in_different_sessions_do_not_cross_link")
    table = SlotTable()
    mine = slot_obs(session_key=SESSION)
    theirs = slot_obs(session_key=OTHER_SESSION)
    table.observe(mine)
    table.observe(theirs)
    check(mine.key != theirs.key,
          f"the same (root, name, attempt) in two sessions are two slots:\n"
          f"      {mine.key}\n      {theirs.key}")
    check(mine.key.startswith("slot:622-10028|") and theirs.key.startswith("slot:700-99312|"),
          "…because sessionKey is the FIRST component of the _id (CUSTOMSTATE-10)")
    table.bind(mine.key, AGENT, by="marker", at=later(1))
    check(table.slot(theirs.key).get("agentId") is None,
          "binding one session's slot leaves the other session's alone")
    check(table.slot(theirs.key)["resolution"] == "pending",
          "…and its state machine is untouched — no cross-session leak")
    check(len(table.state["slots"]) == 2,
          "…two documents, which is what stops one run's custom state binding onto "
          "another run's agents")

    # And the key is a lossless function of its components, escaping included.
    nasty = slot_obs(session_key=SESSION, root="touch#recon|v2", name="a:b%c")
    parsed = refs.parse_ref_key("slot", nasty.key)
    check(parsed["root"] == "touch#recon|v2" and parsed["name"] == "a:b%c",
          f"every structural character survives the round trip: {parsed}")


def test_a_markerless_node_is_orphaned_after_the_ttl_and_at_a_terminal():
    print("test_a_markerless_node_is_orphaned_after_the_ttl_and_at_a_terminal")
    check(PENDING_TTL_SECONDS == 300,
          f"the TTL is CUSTOMSTATE-9's 300 s (180 s idle + slack): {PENDING_TTL_SECONDS}")
    table = SlotTable()
    obs = slot_obs()
    table.observe(obs)
    check(table.resolution(obs.key, now=later(PENDING_TTL_SECONDS - 1)) == "pending",
          "inside the TTL the slot is still pending — Touch waits before concluding")
    check(table.sweep(now=later(PENDING_TTL_SECONDS - 1)) == [],
          "…and the sweep writes nothing")
    writes = table.sweep(now=later(PENDING_TTL_SECONDS + 1))
    check([w.key for w in writes] == [obs.key],
          f"past the TTL it is orphaned: {[w.key for w in writes]}")
    doc = table.slot(obs.key)
    check(doc["resolution"] == "orphaned" and doc["orphanReason"] == "no marker within TTL",
          f"…with the reason on the document, because the UI must say WHY (D13): "
          f"{doc.get('orphanReason')}")
    check(table.counters["orphaned"] == 1, f"…counted: {table.counters}")
    check("orphaned" in RESOLUTIONS,
          "…and `orphaned` is a first-class outcome, not an error state: GD-7 permits "
          "a node that never gets a marker, and an orphaned stop went nowhere")

    # A run that has ended does not need the TTL: the marker is not coming.
    table2 = SlotTable()
    fresh = slot_obs(name="auth_impl9")
    table2.observe(fresh)
    writes = table2.sweep(now=later(1), terminal=[fresh.key])
    check([w.key for w in writes] == [fresh.key],
          "a terminal run orphans its pending slots immediately, TTL or not")
    check(table2.slot(fresh.key)["orphanReason"] == "run terminal",
          f"…with the other reason: {table2.slot(fresh.key).get('orphanReason')}")

    # An orphan can still bind if the evidence turns up late: orphaned outranks
    # pending but not bound, which is what keeps late evidence usable.
    check(table2.bind(fresh.key, AGENT, by="ledger", at=later(2)).resolution == "bound",
          "late evidence still binds an orphan (rank 1 < 2), rather than being discarded")


def test_the_state_machine_is_monotone_under_any_replay_order():
    print("test_the_state_machine_is_monotone_under_any_replay_order")
    check(RESOLUTION_RANK == {"pending": 0, "orphaned": 1, "bound": 2, "conflict": 3},
          f"severity order, declared once: {RESOLUTION_RANK}")
    check(sorted(RESOLUTION_RANK) == sorted(RESOLUTIONS),
          "…covering exactly R-53's four states")

    observations = [
        slot_obs(ts=T0), slot_obs(ts=later(5), role="implementer"),
        slot_obs(ts=later(9), task_id="task-77"),
        slot_obs(ts=later(3), run_node=refs.run_node_key("wf_x", "impl", 0)),
        # The evidence SETS. `$addToSet` is the only operator here whose stored
        # result is arrival-ordered, so a corpus in which every set stays empty
        # (no `agent_id`, no `bound_by`) cannot see the one thing this assertion
        # is for. Two distinct ids and two distinct channels, minimum.
        slot_obs(ts=later(1), agent_id=AGENT, bound_by="ledger"),
        slot_obs(ts=later(7), agent_id=AGENT2, bound_by="marker"),
        slot_obs(ts=later(4), agent_id=AGENT, bound_by="description"),
    ]
    key = observations[0].key

    emitted = {name for obs in observations for op in map_slot(obs) if op[0] == "slots"
               for name in op[2].get("$addToSet", {})}
    emitted |= set(conflict_write(key, [AGENT], conflict_with=[AGENT2])
                   .update.get("$addToSet", {}))
    emitted |= set(cs.conflict_evidence_op(key, [AGENT], conflict_with=[AGENT2])[2]
                   .get("$addToSet", {}))
    check(emitted == set(cs.SLOT_SET_FIELDS),
          f"SLOT_SET_FIELDS names exactly the `$addToSet` sets this module builds on "
          f"`slots` — the tuple the owning spec has to declare, kept honest by deriving "
          f"it from the writers: {sorted(emitted)} vs {sorted(cs.SLOT_SET_FIELDS)}")

    orders = []
    for seed in range(5):
        shuffled = list(observations)
        random.Random(seed).shuffle(shuffled)
        orders.append(shuffled)
    orders.append(list(reversed(observations)))
    states = [ms.apply_operations({}, [op for obs in order for op in map_slot(obs)])
              for order in orders]
    docs = [state["slots"][key] for state in states]
    check(all(len(doc.get("agentIds") or ()) == 2 and len(doc.get("evidence") or ()) == 3
              for doc in docs),
          f"the corpus really populates the sets — otherwise this test proves nothing "
          f"about them: agentIds={docs[0].get('agentIds')} evidence={docs[0].get('evidence')}")
    check(all(sorted(doc.get(field) or ()) == sorted(docs[0].get(field) or ())
              for doc in docs for field in cs.SLOT_SET_FIELDS),
          "…and every order observes the same set MEMBERS")
    plain = [{name: value for name, value in doc.items()
              if name not in cs.SLOT_SET_FIELDS} for doc in docs]
    check(all(other == plain[0] for other in plain),
          "…while every non-set field is byte-identical, so a set's element order is the "
          "only thing replay order can still move")
    with slot_sets_declared():
        fingerprints = {ms.fingerprint(state) for state in states}
    check(len(fingerprints) == 1,
          f"every evidence order yields ONE document under GD-25's own oracle, once the "
          f"sets are declared: {len(fingerprints)} seen")

    state = states[-1]
    doc = docs[-1]
    check(doc["pendingSince"] == T0 and doc["lastSeenTs"] == later(9),
          "…$min holds the earliest evidence and $max the latest")
    check(doc["role"] == "implementer",
          f"…and a scalar settles by $max, not by whichever line arrived last: {doc['role']}")
    for op in (op for obs in observations for op in map_slot(obs)):
        check(set(op[2]) <= {"$setOnInsert", "$min", "$max", "$addToSet"},
              f"the mapper uses only order-independent operators: {sorted(op[2])}")


def test_a_bind_cannot_lower_what_an_observation_raised():
    print("test_a_bind_cannot_lower_what_an_observation_raised")
    key = slot_obs().key
    high_node = refs.run_node_key("wf_x", "impl", 0)
    low_node = refs.run_node_key("wf_a", "impl", 0)

    def observe_high(table):
        table.observe(slot_obs(ts=later(9), task_id="t9", run_node=high_node))

    def bind_low(table):
        table.bind(key, AGENT, by="marker", at=later(1), task_id="t1", run_node=low_node)

    def run(steps):
        table = SlotTable()
        table.observe(slot_obs(ts=T0))          # the slot a bind is allowed to touch
        for step in steps:
            step(table)
        return table.slot(key)

    first, second = run([observe_high, bind_low]), run([bind_low, observe_high])
    check(first == second,
          f"a bind and an observation of the same slot land on ONE document in either "
          f"order — the failure GD-25 names verbatim ('$set is write-order dependent'): "
          f"taskId {first.get('taskId')!r} vs {second.get('taskId')!r}")
    check(first["taskId"] == "t9" and first["runNode"] == high_node,
          f"…and the accumulation wins, rather than whichever write happened to be last: "
          f"{first.get('taskId')!r} / {first.get('runNode')!r}")

    write = bind_write(key, AGENT, by="marker", task_id="t1", run_node=high_node)
    check(not set(write.update.get("$set", {})) & set(cs.ADVANCE_MAX_FIELDS),
          f"a transition never `$set`s a field the mapper accumulates: "
          f"{sorted(write.update.get('$set', {}))}")
    check(set(write.update.get("$max", {})) == set(cs.ADVANCE_MAX_FIELDS),
          f"…it carries them as a `$max` leg of the same guarded update: "
          f"{sorted(write.update.get('$max', {}))}")
    check(set(cs.ADVANCE_MAX_FIELDS) == {"taskId", "runNode"},
          f"…which is exactly the overlap between the mapper's `$max` scalars and the "
          f"advance's payload: {list(cs.ADVANCE_MAX_FIELDS)}")
    check(set(write.update.get("$set", {})) >= {"resolution", "resolutionRank", "boundBy"},
          f"…while the conclusion itself stays a `$set`, made monotone by the rank guard "
          f"rather than by an operator: {sorted(write.update.get('$set', {}))}")


def test_the_session_key_source_settles_by_trust_not_by_alphabet():
    print("test_the_session_key_source_settles_by_trust_not_by_alphabet")
    check(cs.SESSION_KEY_SOURCE_RANK == {"path": 0, "slots": 1, "marker": 2, "ledger": 3},
          f"the trust order is declared once, as a rank: {cs.SESSION_KEY_SOURCE_RANK}")
    check(sorted(cs.SESSION_KEY_SOURCE_RANK) == sorted(cs.SESSION_KEY_SOURCES),
          "…covering exactly the four channels a sessionKey can come from")
    check(max("ledger", "path") == "path",
          "…which is the whole point: `$max` over the LABELS settles by alphabet, and the "
          "alphabet ranks a directory Touch read above the writer's own statement")

    stated = slot_obs(session_key_source="ledger", ts=T0)
    derived = slot_obs(session_key_source="path", ts=later(4))
    forward = ms.apply_operations({}, [op for obs in (stated, derived)
                                       for op in map_slot(obs)])
    backward = ms.apply_operations({}, [op for obs in (derived, stated)
                                        for op in map_slot(obs)])
    doc = forward["slots"][stated.key]
    check(doc == backward["slots"][stated.key],
          "either order lands on one document (the rank is still `$max`)")
    check(cs.session_key_source_of(doc) == "ledger",
          f"…and the slot keeps the most directly STATED channel, not the alphabetically "
          f"largest one: {cs.session_key_source_of(doc)}")
    check("sessionKeySource" not in doc and doc.get("sessionKeySourceRank") == 3,
          f"…stored as the rank, because that is the field `$max` can settle honestly: "
          f"{sorted(doc)}")
    check(raises(SlotError, cs.session_key_source_rank, "vibes"),
          "a channel nobody declared is refused, not quietly filed under the weakest one")

    # The per-EVENT document still records the literal channel of its own line:
    # the rank answers "how directly was this slot's session stated, over
    # everything observed", which is a different question.
    stream = cs.ledger_stream("task")
    line = slot_obs(session_key_source="path", stream=stream, seq=2)
    events = ms.apply_operations({}, [op for op in map_slot(line)
                                      if op[0] == "custom_state_events"])
    event = next(iter(events["custom_state_events"].values()))
    check(event["sessionKeySource"] == "path",
          f"the ledger EVENT keeps the channel of the line it is: "
          f"{event.get('sessionKeySource')}")


def test_a_ledger_line_is_the_agents_claim_and_touchs_own_record_says_touch():
    print("test_a_ledger_line_is_the_agents_claim_and_touchs_own_record_says_touch")
    line = slot_obs(stream=cs.ledger_stream("task"), seq=1, task_id="t1")
    events = ms.apply_operations({}, [op for op in map_slot(line)
                                      if op[0] == "custom_state_events"])
    event = next(iter(events["custom_state_events"].values()))
    check(event["provenance"] == "asserted",
          f"a spawn-ledger line is an AGENT's claim, so its event is `asserted` — "
          f"labelling it `touch` would say Touch authored it (GD-28): "
          f"{event.get('provenance')}")
    check(event["kind"] == "ledger" and event["refId"] == line.key,
          f"…filed under the slot it is evidence about: {event.get('refId')}")
    slot = ms.apply_operations({}, [op for op in map_slot(line) if op[0] == "slots"])
    check(slot["slots"][line.key]["provenance"] == "derived",
          f"…while the slot DOCUMENT is Touch's own join over that evidence: "
          f"{slot['slots'][line.key].get('provenance')}")

    root = tempfile.mkdtemp(prefix="touch-slots-provenance-")
    try:
        writer = cs.Writer(root=root)
        record = writer.append("annotation",
                               state_key=cs.annotation_state_key("a1"),
                               ref_id=refs.agent_key(AGENT), custom={"text": "hi"})
        check(record["provenance"] == "touch",
              f"…and a record Touch itself authors is `touch`, which is the other half "
              f"of GD-28's split: {record.get('provenance')}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_conclusion_is_a_guarded_write_and_the_evidence_is_a_triple():
    print("test_the_conclusion_is_a_guarded_write_and_the_evidence_is_a_triple")
    key = slot_id(SESSION, "auth", "auth_impl1", 1)
    write = bind_write(key, AGENT, by="marker", at=T0)
    check(write.collection == "slots" and write.require == {"resolutionRank": {"$lt": 2}},
          f"a bind is guarded on the rank it advances past: {write.require}")
    check(orphan_write(key, reason="x").require == {"resolutionRank": {"$lt": 1}}
          and conflict_write(key, [AGENT]).require == {"resolutionRank": {"$lt": 3}},
          "…and so are the other two transitions, each at its own rank")
    check(raises(SlotError, bind_write, key, AGENT, by="telepathy"),
          f"boundBy is one of {list(BIND_CHANNELS)} — the three evidence channels R-53 names")
    check(raises(SlotError, conflict_write, key, []),
          "a conflict with no ids is not a conflict anyone can act on")

    ops = map_slot(slot_obs(agent_id=AGENT, bound_by="marker"))
    updates = [op[2] for op in ops if op[0] == "slots"]
    written = {name for update in updates for fields in update.values() for name in fields}
    check("agentId" not in written and "boundBy" not in written,
          f"the MAPPER never writes `agentId` — only the guarded bind touches the unique "
          f"index, so a colliding observation cannot fail an ingest tick: {sorted(written)}")
    doc = ms.apply_operations({}, ops)["slots"][slot_obs().key]
    check(doc["agentIds"] == [AGENT] and doc["evidence"] == ["marker"],
          f"…it accumulates the claim as evidence instead: {doc.get('agentIds')}")

    # The index-touching write is isolated, because `guarded_update` turns a
    # duplicate key into MongoUnavailable and R-53 requires "never raises".
    collection, claim_key, update = cs.claim_op(key, AGENT)
    check(collection == "slots" and claim_key == key and update == {"$set": {"agentId": AGENT}},
          f"claim_op is exactly the agentId write, alone: {update}")
    check("agentId" not in cs.bind_advance_write(key, by="marker").update["$set"],
          "…and the guarded advance beside it never touches the index")
    # A *write* of agentId is a field map handed to an op builder (or to the
    # `_slot_advance` helper that wraps one). A `find_one({"agentId": …})` is a
    # query and must not be counted, which is why this looks at the call and not
    # merely at every dict literal that mentions the field.
    builders = {"op_set", "op_max", "op_min", "op_add_to_set", "op_set_on_insert",
                "_slot_advance"}
    writers = set()
    for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            name = getattr(call.func, "attr", None) or getattr(call.func, "id", None)
            if name not in builders:
                continue
            for arg in call.args:
                if isinstance(arg, ast.Dict) and any(
                        isinstance(k, ast.Constant) and k.value == "agentId"
                        for k in arg.keys):
                    writers.add(node.name)
    check(writers == {"claim_op", "bind_write"},
          f"exactly two functions in the module write `agentId` — the isolated claim and "
          f"the in-memory whole-bind: {sorted(writers)}")


def test_the_marker_channel_needs_a_name_a_root_and_an_integer_attempt():
    print("test_the_marker_channel_needs_a_name_a_root_and_an_integer_attempt")
    good = cs.slot_from_labels(Marker(name="auth_impl1", root="auth", parent="auth",
                                      role="impl", attempt=2),
                               session_key=SESSION, agent_id=AGENT, ts=T0)
    check(good.key == slot_id(SESSION, "auth", "auth_impl1", 2),
          f"a [touch] marker becomes a slot observation: {good.key}")
    check(good.session_key_source == "marker" and good.bound_by == "marker",
          "…attributed to the channel it came from")
    check(raises(SlotError, cs.slot_from_labels,
                 Marker(name="auth_impl1", root="auth", attempt="two"),
                 session_key=SESSION),
          "an unparsable attempt= is not evidence: GD-9 keeps it verbatim, a slot _id "
          "needs an int, and inventing 1 would fabricate a spawn")
    check(raises(SlotError, cs.slot_from_labels, Marker(root="auth", attempt=1),
                 session_key=SESSION),
          "a node with no name= is unconventional, not a slot (GD-7/R-28)")
    check(raises(SlotError, cs.slot_from_labels, Marker(name="x", attempt=1),
                 session_key=SESSION),
          "…and one with no root= would collide across orchestrations")


# --- the ledger channel, and the SKILL.md amendment it depends on ---------


def test_the_ledger_line_carries_root_and_sessionkey():
    print("test_the_ledger_line_carries_root_and_sessionkey")
    text = SKILL.read_text(encoding="utf-8")
    line = None
    for block in text.split("```json"):
        candidate = block.split("```")[0].strip()
        if candidate.startswith("{") and '"name"' in candidate:
            line = candidate
            break
    check(line is not None, "SKILL.md still shows the spawn-ledger line as JSON")
    for field_name in ("name", "parent", "root", "role", "attempt", "taskId",
                       "sessionKey", "ts"):
        check(f'"{field_name}"' in (line or ""),
              f"…and it carries `{field_name}` (R-53's amendment)")
    check("<pid>-<procStart>" in (line or ""),
          "…with sessionKey spelled as the composite the session grammar emits")
    check("sessionKeySource" in text and '"path"' in text,
          "…and the file states the pre-amendment fallback rather than leaving Touch "
          "to guess (CUSTOMSTATE-10)")
    check("spawn-ledger.jsonl" in text and cs.LEDGER_FILE == "spawn-ledger.jsonl",
          "…at the path the ingest actually reads")


def test_ledger_ingest_uses_the_stated_session_and_derives_the_rest():
    print("test_ledger_ingest_uses_the_stated_session_and_derives_the_rest")
    root = tempfile.mkdtemp(prefix="touch-slots-")
    try:
        folder = os.path.join(root, ".claude", "local-orchestrators",
                              "touch-mongo-live", "state")
        os.makedirs(folder)
        path = os.path.join(folder, cs.LEDGER_FILE)
        with open(path, "w", encoding="utf-8") as handle:
            # amended line: states its own session
            handle.write(json.dumps({"name": "auth_impl1", "parent": "auth", "root": "auth",
                                     "role": "impl", "attempt": 1, "taskId": "t1",
                                     "sessionKey": SESSION, "ts": WIRE}) + "\n")
            # pre-amendment line: no root ⇒ unaddressable, skipped rather than guessed
            handle.write(json.dumps({"name": "auth_impl2", "parent": "auth", "role": "impl",
                                     "attempt": 1, "taskId": "t2", "ts": WIRE}) + "\n")
            handle.write("{ not json\n")
            handle.write(json.dumps({"name": "auth_impl3", "root": "auth", "attempt": 2,
                                     "taskId": "t3", "ts": WIRE}) + "\n")
        check(cs.is_ledger_path(path) and not cs.is_ledger_path(path + ".bak"),
              "a ledger is recognised from its path alone (the backfill contract)")

        observations = cs.read_ledger_file(path)
        check([o.name for o in observations] == ["auth_impl1"],
              f"only the addressable line survives — the unparsable one, the one with "
              f"no root, and the one whose session cannot be established anywhere are "
              f"skipped rather than guessed: {[o.name for o in observations]}")
        check([o.seq for o in observations] == [1],
              f"…and a surviving line keeps its LINE number as seq, so a skipped line "
              f"never renumbers its successors: {[o.seq for o in observations]}")
        check(observations[0].session_key_source == "ledger",
              "a line that states its sessionKey is attributed to the writer")

        # With a path-derived session available, the fallback fires and says so.
        session_dir = os.path.join(root, "sessions", SESSION, "state")
        os.makedirs(session_dir)
        second = os.path.join(session_dir, cs.LEDGER_FILE)
        shutil.copyfile(path, second)
        derived = cs.read_ledger_file(second)
        check([o.name for o in derived] == ["auth_impl1", "auth_impl3"],
              f"under a `<pid>-<procStart>` path the pre-amendment line becomes "
              f"addressable: {[o.name for o in derived]}")
        check([o.seq for o in derived] == [1, 4],
              f"…still keyed by line number, gaps included: {[o.seq for o in derived]}")
        check([o.session_key for o in derived] == [SESSION, SESSION],
              "a pre-amendment line takes its session from the containing path")
        check([o.session_key_source for o in derived] == ["ledger", "path"],
              f"…recorded as derived, never as a claim the writer made: "
              f"{[o.session_key_source for o in derived]}")

        table = SlotTable()
        for obs in derived:
            table.observe(obs)
        check(len(table.state["slots"]) == 2,
              "…and both become slots the mirror can join to")
        ops = [op for obs in derived for op in map_slot(obs)]
        events = [op for op in ops if op[0] == "custom_state_events"]
        check(len(events) == 2,
              f"…while the LINE itself also becomes one immutable event (kind `ledger`): "
              f"{len(events)}")
        state = ms.apply_operations({}, ops)
        kinds = {doc["kind"] for doc in state["custom_state_events"].values()}
        check(kinds == {"ledger"}, f"…in the one events collection: {kinds}")

        check(cs.iter_slot_observations(path=os.path.join(root, "nope.jsonl")) == [],
              "a source handed a path it does not own returns nothing (mirror's contract)")
        found = cs.ledger_paths(env={cs.LEDGER_PATHS_ENV: path})
        check(found == [path], f"an explicit ledger list overrides discovery: {found}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_ledger_reader_counts_every_line_it_drops():
    print("test_the_ledger_reader_counts_every_line_it_drops")
    root = tempfile.mkdtemp(prefix="touch-slots-counters-")
    try:
        folder = os.path.join(root, "task", "state")
        os.makedirs(folder)
        path = os.path.join(folder, cs.LEDGER_FILE)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": "auth_impl1", "root": "auth", "attempt": 1,
                                     "sessionKey": SESSION, "ts": WIRE}) + "\n")
            handle.write("{ not json\n")
            handle.write(json.dumps([1, 2, 3]) + "\n")
            handle.write(json.dumps({"name": "auth_impl2", "attempt": 1,
                                     "sessionKey": SESSION}) + "\n")     # no root
            handle.write(json.dumps({"name": "auth_impl3", "root": "auth",
                                     "attempt": "two", "sessionKey": SESSION}) + "\n")
            # No `attempt` AT ALL — the case a `payload.get("attempt", 1)` would
            # turn into a fabricated `|001` address. A missing attempt is not an
            # unreadable one, and it must reach the same refusal (R-53/D13).
            handle.write(json.dumps({"name": "auth_impl4", "root": "auth",
                                     "sessionKey": SESSION}) + "\n")
        counters = cs.new_counters()
        observations = cs.read_ledger_file(path, counters=counters)
        check([o.name for o in observations] == ["auth_impl1"],
              f"one addressable line: {[o.name for o in observations]}")
        check(not any(o.name == "auth_impl4" for o in observations),
              "a ledger line with no `attempt` produces NO slot — the reader has no "
              "default, in either direction, because names are logical and attempts are "
              "physical (touch-orchestrate)")
        check(counters["read"] == 6 and counters["parsed"] == 1,
              f"…out of six read: {counters}")
        check(counters["skipped_malformed"] == 4 and counters["skipped_unaddressable"] == 1,
              f"…and every drop is counted BY REASON, so an operator can tell 'nothing "
              f"spawned yet' from 'every line I wrote was rejected' (D13): {counters}")
        missing = cs.new_counters()
        check(cs.read_ledger_file(os.path.join(root, "gone.jsonl"), counters=missing) == []
              and missing["unreadable"] == 1,
              f"…and a ledger that cannot be opened is `unreadable`, not silence: {missing}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_name_to_slot_hop_is_the_index_control_lines_resolve_through():
    print("test_the_name_to_slot_hop_is_the_index_control_lines_resolve_through")
    first = slot_obs(attempt=1)
    latest = slot_obs(attempt=3)
    other = slot_obs(session_key=OTHER_SESSION, attempt=1)
    index = cs.SlotIndex([first, latest])
    address, reason = index.resolve("auth_impl1")
    check(reason == "resolved" and address.key == latest.key,
          f"a bare name resolves to the HIGHEST observed attempt — a stopped slot is "
          f"re-run as attempt+1, so the newest is the only one a stop reaches: {reason}")
    check(address.attempt_source == "resolved",
          f"…and says the attempt was inferred: {address.attempt_source}")
    check(index.resolve("auth_impl1", attempt=1)[0].key == first.key
          and index.resolve("auth_impl1", attempt=1)[0].attempt_source == "stated",
          "…while a stated attempt is obeyed and attributed to the line")
    check(index.resolve("auth_impl9")[1] == "unknown"
          and index.resolve("auth_impl9")[0] is None,
          "a name nobody spawned resolves to nothing — never to an invented slot")

    index.add(other)
    check(index.resolve("auth_impl1")[1] == "ambiguous",
          "the same name in two sessions is ambiguous, not a coin toss (CUSTOMSTATE-10)")
    check(index.resolve("auth_impl1", session_key=SESSION)[0].key == latest.key,
          "…and stating the session disambiguates it")

    # The index is fed by every shape the hop can be observed in.
    check(cs.SlotIndex([latest.key]).resolve("auth_impl1")[0].key == latest.key,
          "a slot KEY is enough to index the hop (the mirror's own documents)")
    stored = ms.apply_operations({}, map_slot(latest))["slots"][latest.key]
    check(cs.SlotIndex([stored]).resolve("auth_impl1")[0].key == latest.key,
          "…as is a stored `slots` document, so the resolver works off the collection")
    check(len(cs.SlotIndex([{"nonsense": True}, 17, None])) == 0,
          "…and anything that is not a slot is ignored rather than half-indexed")


def test_a_third_collision_is_recorded_and_the_result_says_what_it_wrote():
    print("test_a_third_collision_is_recorded_and_the_result_says_what_it_wrote")
    table = SlotTable()
    obs = slot_obs()
    table.observe(obs)
    table.bind(obs.key, AGENT, by="marker", at=later(1))
    first = table.bind(obs.key, AGENT2, by="marker", at=later(2))
    check(first.resolution == "conflict" and first.acquired is True,
          f"the first collision advances the state machine: acquired={first.acquired}")

    third = "c0ffee1234567890a"
    again = table.bind(obs.key, third, by="ledger", at=later(3))
    check(again.resolution == "conflict" and again.acquired is False,
          f"a THIRD colliding id cannot advance rank 3 past itself, and the result says "
          f"so instead of overstating the write: acquired={again.acquired}")
    doc = table.slot(obs.key)
    check(sorted(doc["conflictAgentIds"]) == sorted([AGENT, AGENT2, third]),
          f"…yet the id is still recorded: 'two agents collided here' when three did is "
          f"a lie the document must not tell: {doc['conflictAgentIds']}")
    check(doc["agentId"] == AGENT,
          "…and the unique index is still untouched by any of the losers")
    check(table.counters["conflict"] == 1,
          f"…counted once, because exactly one transition was written: {table.counters}")

    # The evidence half is order-independent on its own (GD-25).
    ops = [cs.conflict_evidence_op(obs.key, [AGENT2]),
           cs.conflict_evidence_op(obs.key, [third, AGENT])]
    for order in (ops, list(reversed(ops))):
        state = ms.apply_operations({}, [op for op in order])
        stored = state["slots"][obs.key]["conflictAgentIds"]
        check(sorted(stored) == sorted([AGENT, AGENT2, third]),
              f"the unguarded evidence write is $addToSet, so replay order cannot lose "
              f"an id: {sorted(stored)}")


def test_a_transition_stamps_its_own_clock_not_the_documents():
    print("test_a_transition_stamps_its_own_clock_not_the_documents")
    table = SlotTable()
    obs = slot_obs()
    table.observe(obs)
    table.bind(obs.key, AGENT, by="marker", at=later(30))
    doc = table.slot(obs.key)
    check(doc.get("resolvedTs") == later(30),
          f"the bind records WHEN it concluded, under its own name: "
          f"{doc.get('resolvedTs')}")
    check(doc.get("firstSeenTs") == T0 and doc.get("lastSeenTs") == T0,
          "…leaving the evidence clock ($min/$max from the mapper) alone")
    check("ts" not in doc,
          f"…and no bare `ts` pretends to be the document's own: {sorted(doc)}")


def test_the_schema_indexes_the_hop_in_both_directions():
    print("test_the_schema_indexes_the_hop_in_both_directions")
    spec = ms.COLLECTIONS["slots"]
    unique = [i for i in spec.indexes
              if i["keys"] == (("agentId", 1),) and i["options"].get("unique")]
    check(unique and unique[0]["options"].get("sparse"),
          f"`{{agentId:1}}` is unique AND sparse — pending slots have no agentId to "
          f"collide on: {unique}")
    compound = [i for i in spec.indexes
                if i["keys"] == (("sessionKey", 1), ("root", 1), ("name", 1), ("attempt", 1))]
    check(compound, f"…and the name side is indexed too, sessionKey first: {compound}")
    check("resolution" in spec.required and "provenance" in spec.required,
          f"…with the state machine mandatory on every document: {spec.required}")
    check(spec.provenance == ("derived", "touch") and cs.SLOT_PROVENANCE == "derived",
          f"a slot is Touch's own join over other people's evidence: {spec.provenance}")
    check(not any("expireAfterSeconds" in i["options"] for i in spec.indexes),
          "no TTL index, here or anywhere (GD-26)")


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


def test_live_duplicate_key_is_tolerated_not_raised():
    print("test_live_duplicate_key_is_tolerated_not_raised")
    db, client, name = live_database()
    if db is None:
        skip(f"live slots arm: {name}")
        return
    try:
        ms.ensure_schema(db, collections=["slots", "custom_state_events"])
        first = slot_obs()
        second = slot_obs(name="auth_impl2")
        operations = [(op[1], op[2]) for obs in (first, second)
                      for op in map_slot(obs) if op[0] == "slots"]
        ms.bulk_upsert(db, "slots", operations)
        check(db["slots"].count_documents({}) == 2, "two pending slots exist on the server")

        counters = {}
        result = cs.bind_slot(db, first.key, AGENT, by="marker", at=later(1),
                              task_id="task-77", counters=counters)
        check(result.resolution == "bound", f"the first bind acquires: {result.resolution}")
        stored = db["slots"].find_one({"_id": first.key})
        check(stored["agentId"] == AGENT and stored["resolution"] == "bound",
              f"…and the server holds both the claim and the advance: {stored['resolution']}")

        # The collision: a second slot claiming the same agentId. mongod answers
        # E11000 on the unique sparse index, and this must come back as DATA
        # rather than as an exception (CUSTOMSTATE-9). It is the whole reason the
        # index-touching write is isolated in `claim_op` and driven through
        # `bulk_upsert`: `guarded_update` turns E11000 into MongoUnavailable.
        result = cs.bind_slot(db, second.key, AGENT, by="ledger", at=later(2),
                              counters=counters)
        check(result.duplicate_key is True and result.resolution == "conflict",
              f"…and the second is a tolerated duplicate, never raised: {result.resolution}")
        check(counters.get("duplicate_key") == 1 and counters.get("conflict") == 1,
              f"…counted, per GD-29's exposed tolerated-dup number: {counters}")
        stored = db["slots"].find_one({"_id": second.key})
        check(stored["resolution"] == "conflict",
              f"…and the conflict document lands: {stored.get('resolution')}")
        check(stored["conflictAgentIds"] == [AGENT] and stored.get("agentId") is None,
              "…recording the id that collided without ever claiming it")
        check(stored["conflictWith"] == [first.key],
              f"…and naming the slot that holds it: {stored.get('conflictWith')}")
        check(db["slots"].find_one({"_id": first.key})["agentId"] == AGENT,
              "…leaving the legitimate holder alone")

        # And the process is alive: the next bind still works.
        third = slot_obs(name="auth_impl3")
        ms.bulk_upsert(db, "slots",
                       [(op[1], op[2]) for op in map_slot(third) if op[0] == "slots"])
        result = cs.bind_slot(db, third.key, AGENT2, by="marker", at=later(3),
                              counters=counters)
        check(result.resolution == "bound",
              "the tailer lives: a collision is a document, not a crash")
        check(cs.bind_slot(db, "slot:1-1|x|y|001", AGENT2, by="marker",
                           counters=counters).resolution == "unknown",
              "…and a bind for a slot nobody observed is refused, not created")

        # A THIRD claim on the already-conflicted slot: the rank guard has
        # nowhere to advance, so nothing is acquired — but the id is still
        # recorded, and the server's document matches SlotTable's exactly (n4).
        extra = "c0ffee1234567890a"
        result = cs.bind_slot(db, second.key, extra, by="marker", at=later(4),
                              counters=counters)
        check(result.resolution == "conflict" and result.acquired is False,
              f"a third collision writes no transition and says so: "
              f"acquired={result.acquired}")
        stored = db["slots"].find_one({"_id": second.key})
        check(sorted(stored["conflictAgentIds"]) == sorted([AGENT, extra]),
              f"…yet mongod holds every id that collided, not the first pair only: "
              f"{stored['conflictAgentIds']}")
        check(stored["resolution"] == "conflict" and stored.get("agentId") is None,
              "…with the state machine and the unique index both untouched")
    finally:
        if name.startswith("touch_test_"):
            client.drop_database(name)          # only the name we constructed (GD-12/GD-27)
        client.close()


TESTS = [
    test_pre_spawn_state_binds_when_the_marker_lands,
    test_the_bind_is_idempotent_and_never_demoted,
    test_a_duplicate_bind_writes_a_conflict_with_both_ids_and_the_process_lives,
    test_a_half_written_conflict_reads_as_a_conflict_not_as_pending,
    test_a_bound_agent_is_registered_even_when_the_guard_does_not_fire,
    test_two_same_named_roots_in_different_sessions_do_not_cross_link,
    test_a_markerless_node_is_orphaned_after_the_ttl_and_at_a_terminal,
    test_the_state_machine_is_monotone_under_any_replay_order,
    test_a_bind_cannot_lower_what_an_observation_raised,
    test_the_session_key_source_settles_by_trust_not_by_alphabet,
    test_a_ledger_line_is_the_agents_claim_and_touchs_own_record_says_touch,
    test_the_conclusion_is_a_guarded_write_and_the_evidence_is_a_triple,
    test_the_marker_channel_needs_a_name_a_root_and_an_integer_attempt,
    test_the_ledger_line_carries_root_and_sessionkey,
    test_ledger_ingest_uses_the_stated_session_and_derives_the_rest,
    test_the_ledger_reader_counts_every_line_it_drops,
    test_the_name_to_slot_hop_is_the_index_control_lines_resolve_through,
    test_a_third_collision_is_recorded_and_the_result_says_what_it_wrote,
    test_a_transition_stamps_its_own_clock_not_the_documents,
    test_the_schema_indexes_the_hop_in_both_directions,
    test_live_duplicate_key_is_tolerated_not_raised,
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
    print("\nall slots checks passed")
