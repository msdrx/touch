#!/usr/bin/env python3
"""Stdlib-only tests for the ONE reducer (R-54), in aggregator/agents.py.
Run as `python3 test_reducer.py`; exits non-zero on failure. No pytest, no runner.

R-54's own test list is the spine:

* a fixture whose last observation is 10 minutes old ⇒ `unknown`; the SAME
  fixture with a faked `now()` inside the window ⇒ `running` — which is what
  proves the state is *derived* and not stored (GD-23);
* a five-sibling fan-out with one dead ⇒ the run closes, four `done`, one
  `unknown`, **zero** `failed` (the five same-attempt siblings of the run that
  produced this plan are the specimen);
* `reducerVersion` bump ⇒ `derived` is dropped and rebuilt, same output;
* "API answer == page render" for the frozen-stale case: the rendered string is
  a FIELD of the derived document, so the two cannot disagree.

Plus the invariants only a test can hold in place:

* `failed` is not in the reducer's vocabulary at all — a failing verdict is a
  verdict, never a liveness state (R-58: the fabricated FAILED badge, made
  unreachable on the read side as well as at the writer);
* `monitor.html`'s `freezePlan` rule now lives here: a run that closed with
  rows still "running" freezes them, and the page no longer decides that;
* topology is optional (SD-9): absent ⇒ "attempt 3" with no denominator and no
  next-stage arrow; present ⇒ both, read strictly from GD-24's `custom_state`
  shape and with no import of the module that will write it;
* the reducer is pure over `(state, now)`: no filesystem, no database, one
  clock and it comes in as an argument.
"""

import ast
import copy
import datetime
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

from aggregator import agents                                   # noqa: E402
from aggregator import legacy                                   # noqa: E402
from aggregator import mongo_store as ms                        # noqa: E402
from aggregator import refs                                     # noqa: E402
from aggregator import sessions as sess                         # noqa: E402
from aggregator.agents import (                                 # noqa: E402
    DONE,
    FAILED,
    IDLE_LIMIT_SECONDS,
    NODE_STATES,
    REDUCER_VERSION,
    RUNNING,
    UNKNOWN,
    Topology,
    apply_derived,
    attempt_label,
    derived_id,
    liveness,
    needs_rebuild,
    reduce,
    topology_index,
    verdict_of,
)

failures = []

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 7, 25, 3, 0, tzinfo=UTC)
RUN = "wf_829e6f58-b2f"
#: The session the frozen corpus's agents live in — the same id `fanout` puts on
#: every agent's `sessions[]`, so a `sessions` bucket built for it really does
#: join to them (which is the seam MAJOR 1 of attempt 3's critique lived in).
SESSION = "dd469822-2546-47d9-aaa3-31db4cb705e8"
SLUG = "-home-laniakea-Projects-touch"
CWD = "/home/laniakea/Projects/touch"


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def minutes(count):
    return datetime.timedelta(minutes=count)


def fanout(dead=1, *, ended=None, run_status=None):
    """Five same-attempt siblings on one run; `dead` of them never resulted.

    The live specimen R-54 names: a research fan-out whose driver was killed,
    leaving siblings with no result and no further activity. Their transcripts
    stop; nothing ever writes a verdict; the page ticked them as running.
    """
    state = {"agents": {}, "run_nodes": {}, "runs": {RUN: {"_id": RUN, "startedAt": T0}}}
    if ended is not None:
        state["runs"][RUN]["endedAt"] = ended
    if run_status is not None:
        state["runs"][RUN]["status"] = run_status
    for index in range(5):
        agent_id = "a%016x" % index
        alive = index >= (5 - dead)
        node_key = f"{RUN}|research|{index:04d}"
        state["agents"][agent_id] = {
            "_id": agent_id, "provenance": "harness", "runId": RUN,
            "sessions": [SESSION],
            "firstTs": T0, "lastTs": T0 + minutes(1),
            "labels": {"plan": "research", "stage": f"perspective{index}",
                       "role": "research", "attempt": 1},
            "unconventional": True,
        }
        state["run_nodes"][node_key] = {
            "_id": node_key, "provenance": "harness", "runId": RUN, "key": "research",
            "ordinal": index, "journalSeq": index, "agentId": agent_id,
            "resultSeen": not alive, "startedAt": T0, "attempt": 1,
        }
        if not alive:
            state["run_nodes"][node_key]["endedAt"] = T0 + minutes(2)
            state["run_nodes"][node_key]["result"] = {"summary": "findings written"}
    return state


# --- the derivation itself ------------------------------------------------


def test_the_same_fixture_is_running_or_unknown_depending_only_on_now():
    print("test_the_same_fixture_is_running_or_unknown_depending_only_on_now")
    state = fanout(dead=5)                                      # nobody resulted
    inside = reduce(state, now=T0 + minutes(2))
    outside = reduce(state, now=T0 + minutes(11))
    check(all(node["state"] == RUNNING for node in inside.nodes.values()),
          "inside the window every node is running")
    check(all(node["state"] == UNKNOWN for node in outside.nodes.values()),
          "…and ten minutes later every one of them is unknown — same bytes, different clock")
    check(all("idle 10m" in node["label"] for node in outside.nodes.values()),
          f"…rendered 'unknown — idle 10m' (D13): "
          f"{sorted({n['label'] for n in outside.nodes.values()})}")
    check(state == fanout(dead=5),
          "…and the reduction mutated nothing: no state field was written back (GD-23)")

    for doc in list(state["agents"].values()) + list(state["run_nodes"].values()):
        check("state" not in doc and "liveness" not in doc,
              f"the mirror document {doc['_id']} carries observations only, never a state")
        break                                                   # one sample line is enough
    check(not any("state" in doc for doc in state["agents"].values()),
          "…checked across every agent document")


def test_the_three_state_predicate():
    print("test_the_three_state_predicate")
    now = T0 + minutes(10)
    cases = (
        (dict(result_seen=True, result_ts=T0), DONE, "a result observed is done"),
        (dict(last_activity=now - datetime.timedelta(seconds=30)), RUNNING,
         "no result + recent activity is running"),
        (dict(last_activity=T0), UNKNOWN, "no result + 10 minutes of silence is unknown"),
        (dict(), UNKNOWN, "nothing observed at all is unknown, never running"),
        (dict(last_activity=now - datetime.timedelta(seconds=30), session_active=False),
         UNKNOWN, "…and GD-10's conjunct: a warm transcript in an idle session is unknown"),
        (dict(last_activity=now - datetime.timedelta(seconds=30), session_active=None),
         RUNNING, "…while an UNOBSERVED session must not demote a warm transcript"),
    )
    for kwargs, expected, message in cases:
        got = liveness(now=now, **kwargs)
        check(got.state == expected, f"{message} (got {got.state!r}: {got.reason})")
    check(all(liveness(now=now, **kwargs).state in NODE_STATES for kwargs, _e, _m in cases),
          f"every answer is one of {NODE_STATES}")
    check("failed" not in NODE_STATES,
          "`failed` is not a liveness state — it is a verdict, and never invented (R-58)")

    boundary = liveness(now=now, last_activity=now - datetime.timedelta(
        seconds=IDLE_LIMIT_SECONDS))
    check(boundary.state == RUNNING,
          f"exactly {IDLE_LIMIT_SECONDS}s idle is still running; the rule is 'more than'")


def session_bucket(*, hist=True, live_last_ts=None, session_id=SESSION):
    """A `sessions` bucket built by `sessions.map_session`, never by hand.

    The point of the whole test below: attempt 3's reducer suite only ever
    reduced states with NO `sessions` collection, so `session_active` was `None`
    in every arm and GD-10's conjunct was exercised solely by calling
    :func:`liveness` directly. Building the bucket through the mapper that
    actually writes it is what surfaced the real shapes —

    * the **historical** document, which `sessions.py` writes for every
      transcript on disk and which carries no `lastTs` at all (that arm writes
      none, and `ingest.COLLECTIONS` has no `sessions` either);
    * the **live registry** document, whose `lastTs` is the entry's `updatedAt`
      and is not refreshed on anything like a 180 s cadence — the one live entry
      on this machine belonged to the session that was running at review time
      and its heartbeat was six hours old.
    """
    state = {}
    common = dict(cwd=CWD, slugs=(SLUG,), session_ids=(session_id,), sources=())
    if hist:
        ms.apply_operations(state, sess.map_session(sess.SessionObservation(
            session_id=session_id, **common)))
    if live_last_ts is not None:
        ms.apply_operations(state, sess.map_session(sess.SessionObservation(
            session_id=session_id, pid=15934, proc_start="8371220",
            last_ts=live_last_ts, registry={"name": "touch"}, **common)))
    return state.get("sessions", {})


def test_a_session_may_promote_a_node_and_never_demote_it():
    print("test_a_session_may_promote_a_node_and_never_demote_it")
    # MAJOR 1 of attempt 3's critique. GD-10's conjunct read `sessions.lastTs`
    # and turned its ABSENCE into positive evidence of idleness, so every warm
    # agent of every real session reduced to `unknown — session idle` — a label
    # that blames the session, so the row looked explained while R-54's whole
    # subject ("a live agent renders running") was inverted on the live path.
    now = T0 + minutes(2)
    bare = reduce(fanout(dead=5), now=now)
    check({doc["state"] for doc in bare.agents.values()} == {RUNNING},
          "baseline: five warm siblings, no sessions collection at all, all running")

    # 1. The document the HISTORICAL arm writes for every transcript on disk.
    hist = fanout(dead=5)
    hist["sessions"] = session_bucket()
    check(all("lastTs" not in doc for doc in hist["sessions"].values()),
          f"the historical document really carries no lastTs: "
          f"{sorted(hist['sessions'])}")
    got = reduce(hist, now=now)
    check({doc["state"] for doc in got.agents.values()} == {RUNNING},
          f"…and observing it does not demote a single warm sibling: "
          f"{sorted({(d['state'], d['reason']) for d in got.agents.values()})}")
    check(got.runs[RUN]["closed"] is False,
          f"…so the run stays open instead of closing 'quiet': {got.runs[RUN]['reason']!r}")

    # 2. The live registry document, with the measured six-hour heartbeat.
    stale = fanout(dead=5)
    stale["sessions"] = session_bucket(live_last_ts=now - datetime.timedelta(hours=6))
    got = reduce(stale, now=now)
    check({doc["state"] for doc in got.agents.values()} == {RUNNING},
          "…and neither does a six-hour-old registry heartbeat: the field is not a "
          "liveness clock, and its age is not an observation of an idle session")
    check(all(doc["reason"].startswith("active") for doc in got.agents.values()),
          f"…the reason stays a fact about the node's own transcript: "
          f"{sorted({d['reason'] for d in got.agents.values()})}")
    check({doc["state"] for doc in got.nodes.values()} == {RUNNING},
          "…on the node population too, which reads the conjunct through its agent")

    # 3. A genuinely fresh heartbeat promotes — which is how the test tells
    #    "does not demote" apart from "ignores the field entirely".
    fresh_ts = now - datetime.timedelta(seconds=30)
    fresh = fanout(dead=5)
    fresh["sessions"] = session_bucket(live_last_ts=fresh_ts)
    check({doc["state"] for doc in reduce(fresh, now=now).agents.values()} == {RUNNING},
          "a fresh heartbeat keeps them running as well")
    check(agents._session_activity(fresh, now, IDLE_LIMIT_SECONDS) == {SESSION: True},
          "…and it IS read: the fresh session is in the activity map, as True")
    check(agents._session_activity(stale, now, IDLE_LIMIT_SECONDS) == {},
          "…while the stale and the timestampless ones leave the id UNOBSERVED, "
          "which is a third value and not False")
    check(agents._session_conjunct([SESSION], {SESSION: True}) is True
          and agents._session_conjunct([SESSION], {}) is None,
          "…so the conjunct handed to liveness() is True or None, never False")

    # 4. Promotion cannot revive: the conjunct only ever confirms a node whose
    #    own transcript is warm, so a fresh session does not resurrect a corpse.
    late = fanout(dead=5)
    late["sessions"] = session_bucket(live_last_ts=T0 + minutes(11))
    dead_now = reduce(late, now=T0 + minutes(11))
    check({doc["state"] for doc in dead_now.agents.values()} == {UNKNOWN},
          "a fresh session does not revive siblings silent for ten minutes")

    # 5. The PREDICATE keeps all three values. `False` is unreachable from the
    #    reducer today because nothing observes a session ending; when something
    #    does, this arm is where it lands (and it is still asserted meanwhile).
    warm = dict(last_activity=now - datetime.timedelta(seconds=30))
    check(liveness(now=now, session_active=False, **warm).state == UNKNOWN
          and liveness(now=now, session_active=None, **warm).state == RUNNING
          and liveness(now=now, session_active=True, **warm).state == RUNNING,
          "liveness() is still three-valued: only a POSITIVE idle observation demotes")


def test_five_siblings_one_dead_close_the_run_with_zero_failed():
    print("test_five_siblings_one_dead_close_the_run_with_zero_failed")
    reduction = reduce(fanout(dead=1), now=T0 + minutes(11))
    tally = reduction.runs[RUN]["nodes"]
    check(tally == {RUNNING: 0, DONE: 4, UNKNOWN: 1},
          f"four done, one unknown, none running: {tally}")
    check(reduction.runs[RUN]["closed"] is True,
          "…and the run CLOSES: an unknown node has left the running set (R-54)")
    check(reduction.runs[RUN]["state"] == DONE,
          f"…as done, not failed: {reduction.runs[RUN]['state']!r}")
    check(reduction.runs[RUN]["label"] == legacy.CLOSED_NO_VERDICT,
          f"…labelled {legacy.CLOSED_NO_VERDICT!r} — GD-10's words, and legacy.py's string")
    check(reduction.runs[RUN]["verdicts"] == {"passed": 0, "failed": 0},
          "…with no verdict claimed in either direction")
    blob = json.dumps(reduction.documents(), default=str)
    check('"failed"' not in blob.replace('"failed": 0', ""),
          "the whole reduction contains no `failed` state anywhere (R-58/LIVEFLOW-5)")

    # And with everybody alive, the run stays open — the close is a conclusion
    # about observations, not a timeout that always fires.
    open_run = reduce(fanout(dead=5), now=T0 + minutes(2)).runs[RUN]
    check(open_run["closed"] is False and open_run["state"] == RUNNING,
          f"five live siblings keep the run open: {open_run['reason']}")


def test_a_failing_verdict_is_a_verdict_not_a_state():
    print("test_a_failing_verdict_is_a_verdict_not_a_state")
    state = fanout(dead=0)
    key = f"{RUN}|research|0000"
    state["run_nodes"][key]["result"] = {"passed": False, "detail": "3 tests red"}
    reduction = reduce(state, now=T0 + minutes(11))
    node = reduction.nodes[key]
    check(node["state"] == DONE, f"a node that resulted is done, whatever it said: {node['state']}")
    check(node["verdict"] == "failed", "…and the failure is carried as a verdict")
    check(reduction.runs[RUN]["verdicts"]["failed"] == 1, "…and tallied on the run")
    check(reduction.runs[RUN]["state"] == DONE and reduction.runs[RUN]["label"] != "failed",
          "…while the RUN's own state is still a state, not a badge borrowed from a verdict")

    # NIT 5 of attempt 2's critique: R-54 makes the label the page's render
    # string, so a run that closed with failing verdicts may not render the bare
    # word `done` — indistinguishable from a clean close. The tally is stated,
    # and it stays a tally.
    check(reduction.runs[RUN]["label"] == "done — 1 failed verdict(s)",
          f"…and the close says so: {reduction.runs[RUN]['label']!r}")
    check(reduction.runs[RUN]["label"] != legacy.CLOSED_NO_VERDICT,
          "…which is NOT 'closed — no verdict': there was a verdict, and it failed")
    check(reduction.runs[RUN]["state"] in NODE_STATES and FAILED not in NODE_STATES,
          "…while the state remains one of the three, and `failed` is not one of them")

    mixed = fanout(dead=0)
    mixed["run_nodes"][key]["result"] = {"passed": False}
    mixed["run_nodes"][f"{RUN}|research|0001"]["result"] = {"passed": False}
    mixed["run_nodes"][f"{RUN}|research|0002"]["result"] = {"approved": True}
    run = reduce(mixed, now=T0 + minutes(11)).runs[RUN]
    check(run["label"] == "done — 2 failed verdict(s)" and run["verdicts"]["passed"] == 1,
          f"…counting only the failures, beside a passing one: {run['label']!r} "
          f"{run['verdicts']}")
    check(verdict_of({"approved": True}) == "passed" and verdict_of({"passed": 1}) == "passed",
          "both decisive keys are read (decision_watcher.py:961-1025's vocabulary)")
    check(verdict_of("looks fine to me") is None and verdict_of({"summary": "x"}) is None,
          "…and a string or an undecided dict yields NO verdict, which settles done (GD-10)")


def test_freeze_to_stale_moved_into_the_reducer():
    print("test_freeze_to_stale_moved_into_the_reducer")
    # monitor.html's freezePlan: a card closing with rows still "running" means
    # those agents died without a result (killed driver) — freeze them instead
    # of ticking forever. R-54 moves the rule here so page and API agree.
    state = fanout(dead=5, ended=T0 + minutes(3))
    reduction = reduce(state, now=T0 + minutes(3))
    states = {node["state"] for node in reduction.nodes.values()}
    check(states == {UNKNOWN},
          f"a terminal run freezes rows that would otherwise still tick: {states}")
    check(all(node.get("frozen") for node in reduction.nodes.values()),
          "…marked `frozen`, so the reason is visible and not silently 'idle'")
    check(all("frozen at run close" in node["reason"] for node in reduction.nodes.values()),
          "…with monitor.html's own reason, in words")
    check(reduction.counters["frozen"] == 10,
          f"…counted over both populations (5 nodes + 5 agents): {reduction.counters['frozen']}")
    check(reduction.runs[RUN]["closed"] and reduction.runs[RUN]["reason"] == "terminal observation",
          "…and the run closes on the harness's own terminal")

    # Without the terminal, the same instant leaves them running: the freeze is
    # triggered by an observation, never by the clock alone.
    live = reduce(fanout(dead=5), now=T0 + minutes(3))
    check({node["state"] for node in live.nodes.values()} == {RUNNING},
          "…while the same fixture with no terminal keeps them running (no clock-only freeze)")


def test_api_answer_equals_page_render():
    print("test_api_answer_equals_page_render")
    # R-54's last acceptance line. The render string IS a field of the derived
    # document, so "the API answer" and "the page render" are the same bytes;
    # there is nothing for the frontend to re-derive (GD-23/R-55's source guard).
    # The frozen case needs a terminal run AND rows that would otherwise still
    # be ticking, so `now` sits inside the idle window: without the terminal
    # these five would render `running`, and it is the run's close that freezes
    # them (monitor.html's rule, now the reducer's).
    reduction = reduce(fanout(dead=1, ended=T0 + minutes(2)), now=T0 + minutes(2))
    docs = reduction.documents()
    check(all("label" in doc for doc in docs.values()),
          "every derived document carries its rendered label")
    frozen = [doc for doc in docs.values() if doc.get("frozen")]
    check(frozen, "the frozen-stale case is present in this fixture")
    for doc in frozen:
        check(doc["label"].startswith(UNKNOWN) and doc["state"] == UNKNOWN,
              f"…and label and state say the same thing: {doc['label']!r}")
    check(len({doc["label"] for doc in frozen}) == 1,
          "…identically for every frozen row, because one function produced them all")

    # Same input, same output: two readers of one reduction cannot disagree.
    again = reduce(fanout(dead=1, ended=T0 + minutes(2)), now=T0 + minutes(2))
    check(again.documents() == docs, "reducing the same state at the same instant is identical")


# --- the derived collection ----------------------------------------------


def test_derived_documents_are_droppable_and_versioned():
    print("test_derived_documents_are_droppable_and_versioned")
    state = fanout(dead=1)
    state["events"] = {"touch#000000000042": {"_id": "touch#000000000042", "stream": "touch",
                                              "seq": 42, "source": "aggregator",
                                              "provenance": "derived", "kind": "x"}}
    state["custom_state_events"] = {
        "custom#000000000007": {"_id": "custom#000000000007", "stream": "custom", "seq": 7,
                                "provenance": "asserted", "kind": "topology"}}
    reduction = reduce(state, now=T0 + minutes(11))
    check(reduction.derived_from_seq == 42,
          f"derivedFromSeq is the highest seq observed over both streams: "
          f"{reduction.derived_from_seq}")
    # NIT 7 of attempt 2's critique: `events` and `custom_state_events` are
    # INDEPENDENT counters (GD-24 keys both `<stream>#<seq:012d>`), so this
    # single maximum is provenance — "how far had the mirror got" — and not a
    # cursor either stream can resume from. R-55's resume is the client's
    # `(stream, seq)` pair and does not read this field; the docstring says so.
    check(reduce({"custom_state_events": state["custom_state_events"]},
                 now=T0).derived_from_seq == 7,
          "…and the custom stream alone answers 7, which is why the pair is not "
          "recoverable from the maximum")
    check(reduce(state, now=T0 + minutes(11), derived_from_seq=5).derived_from_seq == 5,
          "…while a caller that HAS a watermark passes it in and is believed")

    apply_derived(state, reduction)
    docs = state["derived"]
    check(len(docs) == 11, f"5 agents + 5 nodes + 1 run = 11 derived documents: {len(docs)}")
    for key, doc in docs.items():
        ms.validate_document("derived", doc)
        check(doc["reducerVersion"] == REDUCER_VERSION and doc["derivedFromSeq"] == 42,
              f"{key} carries reducerVersion + derivedFromSeq (GD-23)")
        check(doc["provenance"] == "derived", f"{key} is provenance:derived (GD-28)")
        break
    check(all(doc["reducerVersion"] == REDUCER_VERSION for doc in docs.values()),
          "…checked across every document")
    check(derived_id("agentState", "a%016x" % 0) in docs,
          "…keyed <kind>:<refId>, with refId also a field so no join parses an _id")
    check(all(doc["refId"] and doc["kind"] for doc in docs.values()),
          "…and both components are stored as fields")

    check(needs_rebuild(state, REDUCER_VERSION) is False, "a matching version needs no rebuild")
    check(needs_rebuild(state, "99") is True,
          "…and a bumped reducerVersion does (GD-23: dropped and rebuilt, never migrated)")

    bumped = reduce(state, now=T0 + minutes(11), reducer_version="99")
    apply_derived(state, bumped)
    check(needs_rebuild(state, "99") is False, "…after which nothing stale remains")
    before = {key: {k: v for k, v in doc.items() if k != "reducerVersion"}
              for key, doc in docs.items()}
    after = {key: {k: v for k, v in doc.items() if k != "reducerVersion"}
             for key, doc in state["derived"].items()}
    check(before == after, "…and the rebuilt output is the same conclusion under a new version")


def test_the_drop_is_a_drop_and_touches_nothing_else():
    print("test_the_drop_is_a_drop_and_touches_nothing_else")
    state = fanout(dead=1)
    apply_derived(state, reduce(state, now=T0 + minutes(11)))
    state["derived"]["ghost:1"] = {"_id": "ghost:1", "kind": "ghost", "refId": "1",
                                   "provenance": "derived", "reducerVersion": "0",
                                   "derivedFromSeq": 0}
    mirror_before = copy.deepcopy({k: v for k, v in state.items() if k != "derived"})
    apply_derived(state, reduce(state, now=T0 + minutes(11)))
    check("ghost:1" not in state["derived"],
          "a conclusion the current reducer no longer draws disappears (GD-23's drop)")
    check({k: v for k, v in state.items() if k != "derived"} == mirror_before,
          "…and the mirror collections are untouched: they are upsert-only (GD-26)")


def test_the_operation_list_is_a_total_overwrite_of_each_derived_document():
    print("test_the_operation_list_is_a_total_overwrite_of_each_derived_document")
    # MAJOR 2 of attempt 3's critique, and the method's first direct test.
    # `apply_derived` clears the bucket and then applies `operations()`; a live
    # caller (sp-06/sp-12) can only apply the operations, because `mirror.py`
    # drops `derived` on --rebuild and never on a tick. With a conditionally
    # emitted key the two paths diverge and GD-26 forbids the `$unset` that
    # would repair the server's copy — so the memory model that is
    # mongo_store's declared oracle was the MORE correct of the two, and no
    # acceptance test could see it.
    first = reduce(fanout(dead=5, ended=T0 + minutes(2)), now=T0 + minutes(2))
    second = reduce(fanout(dead=5, ended=T0 + minutes(2)), now=T0 + minutes(12))
    agent = derived_id("agentState", "a%016x" % 0)
    check(first.documents()[agent]["frozen"] is True
          and first.documents()[agent]["reason"] == "frozen at run close",
          "tick 1: the run closed while the row was warm, so the row is frozen")
    check(second.documents()[agent]["frozen"] is False
          and second.documents()[agent]["reason"].startswith("idle"),
          f"tick 2, ten minutes later: not frozen — plainly idle: "
          f"{second.documents()[agent]['reason']!r}")
    check(second.counters["frozen"] == 0,
          "…and the reduction's own counter agrees that nothing is frozen")

    server = {}
    ms.apply_operations(server, first.operations())
    ms.apply_operations(server, second.operations())
    memory = {}
    apply_derived(memory, first)
    apply_derived(memory, second)
    check(server["derived"][agent]["frozen"] is False,
          "…so the SERVER document does not keep a `frozen: true` the reducer "
          "retracted (sp-13 renders that flag)")
    check(ms.fingerprint(server) == ms.fingerprint(memory),
          "the operation list folded twice == apply_derived twice, by fingerprint — "
          "the model and the wire cannot disagree about a conclusion")
    check(server["derived"] == memory["derived"],
          "…document for document, not merely by digest")

    # The other instance the critique names: `idleSeconds` beside a `done` that
    # has no result timestamp. A node the harness marked resulted without an
    # `endedAt` yields `idleSeconds: None`, and the stale 60 must not survive.
    late = fanout(dead=5)
    running = reduce(late, now=T0 + minutes(2))
    for doc in late["run_nodes"].values():
        doc["resultSeen"] = True                    # resulted; no endedAt written
    resulted = reduce(late, now=T0 + minutes(2))
    check(running.documents()[agent]["idleSeconds"] == 60
          and resulted.documents()[agent]["state"] == DONE,
          f"tick 1 is running with 60s idle; tick 2 resulted: "
          f"{running.documents()[agent]['idleSeconds']} / "
          f"{resulted.documents()[agent]['state']}")
    check(resulted.documents()[agent]["idleSeconds"] is None,
          "…and the second payload states the absence rather than omitting the key")
    wire, model = {}, {}
    for reduction in (running, resulted):
        ms.apply_operations(wire, reduction.operations())
        apply_derived(model, reduction)
    check(wire["derived"][agent]["idleSeconds"] is None
          and ms.fingerprint(wire) == ms.fingerprint(model),
          "…so no `idleSeconds: 60` lingers beside `state: done` on the server")

    # Totality as a property rather than as four examples: for one kind, every
    # document of every reduction carries the same key set, whatever it concluded.
    for kind in ("agentState", "nodeState", "runState"):
        shapes = {frozenset(doc) for reduction in (first, second, running, resulted)
                  for doc in reduction.documents().values() if doc["kind"] == kind}
        check(len(shapes) == 1,
              f"every {kind} document has one key set across every reduction: "
              f"{[sorted(s ^ set.intersection(*(set(x) for x in shapes))) for s in shapes] if len(shapes) > 1 else 'identical'}")
    check(all(name in first.documents()[agent] for name in
              ("idleSeconds", "frozen", "attemptLabel", "nextStage")),
          "…and the four optional keys are the ones that are always present")


# --- topology (SD-9) ------------------------------------------------------


def test_topology_is_optional_and_read_as_a_shape():
    print("test_topology_is_optional_and_read_as_a_shape")
    state = fanout(dead=1)
    bare = reduce(state, now=T0 + minutes(11))
    node = bare.nodes[f"{RUN}|research|0000"]
    check(node["attemptLabel"] == "attempt 1",
          f"absent topology ⇒ 'attempt N', no denominator (SD-9/D13): {node.get('attemptLabel')}")
    # The key is PRESENT and null, not absent: MAJOR 2 of attempt 3's critique.
    # `Reduction.operations()` `$set`s the payload it has and GD-26 forbids the
    # `$unset` that would retract a key, so a conditionally-emitted `nextStage`
    # would survive on the stored document after the topology was retracted.
    # "Renders no arrow" is `nextStage is None`, and sp-13 reads it that way.
    check(node["nextStage"] is None,
          "…and no next-stage arrow Touch cannot substantiate — null, and always present")
    check(bare.counters["topology_missing"] == 1,
          "…counted per run, because it is the normal arm for every pre-R-19 run")

    # GD-24's shape, written by hand: `custom_state` head, kind `topology`,
    # payload under data.custom. No import of custom_state.py — SD-9 is a
    # shape, not a code dependency, and that module does not exist yet.
    state["custom_state"] = {
        f"{RUN}#topology": {
            "_id": f"{RUN}#topology", "refId": RUN, "kind": "topology", "seq": 1,
            "provenance": "asserted", "derived": True, "fromSeq": 1,
            "data": {"custom": {"maxAttempts": 5,
                                "stages": ["research", "synthesis", "implement"],
                                "stageAttempts": {"synthesis": 2}}},
        }
    }
    index = topology_index(state)
    check(list(index) == [RUN], f"the head is indexed by refId: {list(index)}")
    with_topology = reduce(state, now=T0 + minutes(11))
    node = with_topology.nodes[f"{RUN}|research|0000"]
    check(node["attemptLabel"] == "attempt 1 of 5",
          f"…and supplies the denominator: {node.get('attemptLabel')}")
    check(node["nextStage"] == "synthesis", f"…and the next stage: {node.get('nextStage')}")
    check(with_topology.counters["topology_missing"] == 0, "…and the counter clears")

    topology = index[RUN]
    check(topology.denominator("synthesis") == 2,
          "a per-stage attempt cap overrides the global one")
    check(topology.next_stage("implement") is None,
          "…and the last stage has no arrow, rather than wrapping to the first")
    check(attempt_label(None) is None and attempt_label("two") == "attempt two",
          "an unparsable attempt is rendered verbatim, never coerced to 1")
    check(attempt_label(3, Topology(max_attempts=None)) == "attempt 3",
          "…and a topology with no cap still yields no fraction")

    # NIT 6 of attempt 2's critique: SD-9 fixes the topology head's SHAPE and
    # says nothing about its refId, while the join here is `refs.run_key(runId)`
    # and nothing else. A writer (sp-11) that keys its head by a `{task, plan,
    # stage}` ref — legal under amended GD-11 — would leave every run on the
    # "absent topology" arm forever, silently. Asserted here so the contract is
    # a test and not an assumption; stated for sp-11 in the deviation file.
    foreign = dict(state)
    foreign["custom_state"] = {
        "task#topology": {
            "_id": "task#topology", "refId": refs.ref_key({"task": "touch", "plan": "sp-x"}),
            "kind": "topology", "seq": 1, "provenance": "asserted",
            "data": {"custom": {"maxAttempts": 5, "stages": ["research"]}}}}
    other = reduce(foreign, now=T0 + minutes(11))
    check(other.nodes[f"{RUN}|research|0000"]["attemptLabel"] == "attempt 1",
          "a topology keyed by any other ref does not join — no denominator appears")
    check(other.counters["topology_missing"] == 1,
          "…and the run is counted as having none, which is the honest report")
    check(list(topology_index(state)) == [refs.run_key(RUN)],
          f"…because the index is keyed by the head's refId and the reducer looks it "
          f"up by refs.run_key(runId): {refs.run_key(RUN)}")


# --- key discipline (SD-11) -----------------------------------------------


def test_every_run_lookup_goes_through_refs_run_key():
    print("test_every_run_lookup_goes_through_refs_run_key")
    # MINOR 8 of attempt 1's critique. `runs` and the topology index are keyed
    # by `refs.run_key`; `agents.runId` and `run_nodes.runId` carry the runId
    # raw. The two agree only while a runId contains none of `% # | :`, which
    # `wf_<hex>` happens not to — so a mixed lookup passes every existing test
    # and silently switches the freeze rule off on the first id that does.
    weird = "wf_a%b#c"
    key = refs.run_key(weird)
    check(key != weird, f"the fixture runId genuinely escapes: {weird!r} -> {key!r}")

    state = fanout(dead=5, ended=T0 + minutes(3))
    state["runs"] = {key: {"_id": key, "runId": weird, "startedAt": T0,
                           "endedAt": T0 + minutes(3)}}
    for doc in state["agents"].values():
        doc["runId"] = weird
    for doc in state["run_nodes"].values():
        doc["runId"] = weird
    state["custom_state"] = {
        f"{key}#topology": {
            "_id": f"{key}#topology", "refId": key, "kind": "topology", "seq": 1,
            "provenance": "asserted", "data": {"custom": {
                "maxAttempts": 5, "stages": ["research", "synthesis"]}}}}

    reduction = reduce(state, now=T0 + minutes(3))
    check(list(reduction.runs) == [key],
          f"ONE run entry, keyed by the escaped id — not one escaped and one raw: "
          f"{sorted(reduction.runs)}")
    run = reduction.runs[key]
    check(run["nodeCount"] == 5 and run["startedAt"] == T0,
          f"…carrying both the nodes and the run document's own fields: "
          f"{run['nodeCount']} / {run['startedAt']}")
    check(run["runId"] == weird,
          f"…with the harness's own spelling as a FIELD, so no reader unescapes an "
          f"_id (LIVEFLOW-3): {run.get('runId')!r}")
    check(run["terminalObserved"] is True,
          "…and the terminal observation is found, so the freeze rule still fires")
    states = {node["state"] for node in reduction.nodes.values()}
    check(states == {UNKNOWN} and all(n.get("frozen") for n in reduction.nodes.values()),
          f"…which is the point: a missed terminal leaves five dead rows ticking: {states}")
    node = reduction.nodes[f"{RUN}|research|0000"]
    check(node["attemptLabel"] == "attempt 1 of 5",
          f"…and topology still joins, because it is looked up by the same key: "
          f"{node.get('attemptLabel')}")
    check(all(doc["refId"] == key for doc in reduction.documents().values()
              if doc["kind"] == "runState"),
          "…and `derived`'s refId is a refs key, as derived_id's contract states")

    # MINOR 3 of attempt 3's critique. `ingest.map_run` stores the runId AS the
    # `_id` and never as a field (`COLLECTIONS["runs"]` declares no `runId`), so
    # the raw spelling normally arrives from an agent or a node that names the
    # run — and a run with neither is exactly the arm reduce() documents as
    # normal and live: the journal's first `started` creates the run document
    # before it creates any node. That run is the freshest one on the page and
    # its payload said `runId: null`.
    empty = {"runs": {key: {"_id": key, "startedAt": T0}}}
    only = reduce(empty, now=T0 + minutes(1)).runs[key]
    check(only["runId"] == weird,
          f"a run with no nodes yet still names itself, through the grammar's "
          f"proven inverse: {only.get('runId')!r} (not None, not the escaped {key!r})")
    check(only["reason"] == "no nodes observed yet" and only["closed"] is False,
          f"…in the arm that is normal and open, not an error: {only['reason']!r}")
    check(agents._raw_run_id("x" * 600) is None,
          "…and a key the run grammar cannot parse yields None rather than a guess")


def test_the_verdict_vocabulary_is_exported():
    print("test_the_verdict_vocabulary_is_exported")
    # NIT 12 of attempt 1's critique: sp-12's API and sp-13's page render these
    # three strings, and a second spelling of a user-visible label is a second
    # label — the same argument that makes CLOSED_NO_VERDICT an import from
    # legacy.py rather than a literal.
    exported = set(agents.__all__)
    check({"PASSED", "FAILED", "VERDICT_KEYS", "CLOSED_NO_VERDICT", "NODE_STATES"}
          <= exported,
          f"the verdict vocabulary travels with the state vocabulary: "
          f"{sorted(exported & {'PASSED', 'FAILED', 'VERDICT_KEYS', 'CLOSED_NO_VERDICT'})}")
    check(all(hasattr(agents, name) for name in agents.__all__),
          "…and every exported name exists")
    check(agents.CLOSED_NO_VERDICT is legacy.CLOSED_NO_VERDICT,
          "…with the closed-no-verdict label still legacy.py's one string")
    check(FAILED not in NODE_STATES,
          "…while `failed` stays out of the state vocabulary entirely (R-58)")


# --- purity ---------------------------------------------------------------


def test_the_reducer_is_pure_over_state_and_now():
    print("test_the_reducer_is_pure_over_state_and_now")
    source = (SRC / "aggregator" / "agents.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    reducer = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "reduce")
    calls = {n.func.id for n in ast.walk(reducer)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check(not (calls & {"open", "print", "input"}),
          f"reduce touches no file and prints nothing: {sorted(calls)}")
    attrs = {f"{n.value.id}.{n.attr}" for n in ast.walk(reducer)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    check(not any(a.startswith(("os.", "json.", "subprocess.")) for a in attrs),
          f"…and reads neither the filesystem nor a socket: {sorted(a for a in attrs if '.' in a)}")
    check("now" in [a.arg for a in reducer.args.kwonlyargs],
          "…and takes its clock as an argument, which is what makes the derivation provable")

    # One decision site. If a second function started deciding a state, the page
    # and the API could disagree again — which is the defect GD-23 closes.
    deciders = sorted(n.name for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef)
                      and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                              and c.func.id == "Liveness" for c in ast.walk(n)))
    check(deciders == ["liveness", "reduce"],
          f"only the predicate and the reducer that freezes ever construct a state: {deciders}")


def main():
    for test in (
        test_the_same_fixture_is_running_or_unknown_depending_only_on_now,
        test_the_three_state_predicate,
        test_a_session_may_promote_a_node_and_never_demote_it,
        test_five_siblings_one_dead_close_the_run_with_zero_failed,
        test_a_failing_verdict_is_a_verdict_not_a_state,
        test_freeze_to_stale_moved_into_the_reducer,
        test_api_answer_equals_page_render,
        test_derived_documents_are_droppable_and_versioned,
        test_the_drop_is_a_drop_and_touches_nothing_else,
        test_the_operation_list_is_a_total_overwrite_of_each_derived_document,
        test_topology_is_optional_and_read_as_a_shape,
        test_every_run_lookup_goes_through_refs_run_key,
        test_the_verdict_vocabulary_is_exported,
        test_the_reducer_is_pure_over_state_and_now,
    ):
        test()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("all reducer (R-54) tests passed")


if __name__ == "__main__":
    main()
