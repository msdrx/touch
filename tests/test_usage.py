#!/usr/bin/env python3
"""Stdlib-only tests for the usage mirror (R-50). Run as `python3 test_usage.py`;
exits non-zero on failure. No pytest, no runner.

R-50's own test list: "corpus passes shuffled ⇒ identical totals; re-ingest after
a simulated `performRemoveByUuid` ⇒ totals unchanged; agentId-conflict counter
fires on a doctored fixture."

**The plan's `in 27 593 / out 1 062 413` figure is not asserted, on purpose.** It
was measured on the LIVE corpus (901 distinct `message.id`s — the amendment says
so where it derives the 2.8× under-report). The frozen subset sp-02 kept is
smaller: 328 message ids over `run-wf_829e6f58/`, and hard-coding a number
measured elsewhere would make this file fail for the one reason a test must never
fail — being right about a different corpus. What *is* asserted here is the
property that figure was evidence for, computed from the frozen bytes on every
run: `$max` accumulation is order-independent, and naive summing over-counts.

The corpus figures this file measures, recorded so a drift is visible in a diff
rather than only in a red test: `run-wf_829e6f58/` yields 667 assistant records
carrying usage over **328** distinct message ids; deduped `out` is 319 617 while
the naive sum over records is 343 648 (1.075×), and deduped `cached` is
28 491 668 against 55 603 295 naive (1.95×).

**The frozen corpus cannot express the shape that breaks GD-25's property, so
that shape is CONSTRUCTED here.** `run-wf_829e6f58/` holds two session
directories, but no `message.id` is shared between them (0 of its 328 —
:func:`test_the_identity_conflicts_are_reported_per_field` measures it), so an
order-independence test built only from it would report green whichever operator
`map_usage` chose for `sessionId`. On the live corpus three of 4 738 ids ARE
observed under two sessions (one agent's fragments, split by a `/clear`
mid-run), and that is what made `$setOnInsert:{…, sessionId, …}` order-dependent.
:func:`spanning_observations` builds that topology on disk and reads it through
the real `read_transcript`, and
:func:`test_the_set_on_insert_payload_never_varies_for_one_id` asserts the
underlying property — one `_id`, one `$setOnInsert` payload — which catches the
whole failure class on any corpus. The fixtures are sp-02's and frozen; nothing
here adds to them.

Every test but the last runs against `mongo_store`'s in-memory model, which is
under test here as much as the mapper is. `test_live_mongod_arm` replays the same
three orders through a real mongod when `TOUCH_MONGO_URI` names one (R-42's
loopback+auth recipe), because `$max` on a missing field, `$setOnInsert` beside
it on one upsert, `$min` deciding a contested `sessionId`, and `$group` over the
result are *server* semantics that no model can settle. It creates and drops only
its own `touch_test_usage_<pid>` database (GD-27), and with no server it skips
and everything else still stands (GD-21).
"""

import json
import os
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from aggregator import ingest                            # noqa: E402
from aggregator import mirror as mr                      # noqa: E402
from aggregator import mongo_store as ms                 # noqa: E402
from aggregator import refs                              # noqa: E402
from aggregator.ingest import (                          # noqa: E402
    USAGE_FIELDS,
    IngestError,
    UsageObservation,
    dedup_usage,
    map_usage,
    read_transcript,
    rollup,
    rollup_pipeline,
    usage_conflicts,
    usage_from_message,
)

FIX = REPO / "tests" / "fixtures"
RUN = FIX / "run-wf_829e6f58"
DD = "dd469822-2546-47d9-aaa3-31db4cb705e8"
E4 = "e423cd3c-f859-45af-9afd-0d6bdec9b4ac"
RUN_ID = "wf_829e6f58-b2f"
SPLIT = (RUN / DD / "subagents" / "workflows" / RUN_ID
         / "agent-a2fc883c96ff7b837.jsonl")

#: The `/clear`-mid-run topology, built rather than frozen (see the docstring).
#: The two sessions are the corpus's own two, because that is the realistic pair
#: — a `/clear` changes the sessionId and nothing else about where the run's
#: files land. `DD` sorts before `E4`, so `$min` must store `DD`.
SPAN_RUN = "wf_5ea70b12-c1e"
SPAN_AGENT = "a" + "9" * 16
SPAN_MESSAGE = "msg_01SpansTwoSessions"
SPAN_SESSIONS = (DD, E4)

failures = []
skips = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  skip: {msg}")
    skips.append(msg)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:                                            # noqa: BLE001
        return False
    return False


def corpus_observations():
    """Every usage observation in the frozen run corpus, in file order."""
    out = []
    for base, dirnames, filenames in os.walk(RUN):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(base, name)
            if ingest.is_transcript_path(path):
                out.extend(read_transcript(path, root=FIX).usage)
    return out


def assistant_line(uuid, message_id, out, ts, agent_id=SPAN_AGENT):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "agentId": agent_id, "timestamp": ts,
        "message": {"id": message_id, "role": "assistant",
                    "usage": {"input_tokens": 11, "output_tokens": out,
                              "cache_read_input_tokens": 3,
                              "cache_creation_input_tokens": 0}}})


def spanning_observations(tmp):
    """One agent's fragments under TWO session directories, read from bytes.

    MONGOSCHEMA-9's shape and the reason `usage.sessionId` is `$min`: a `/clear`
    gives the process a new sessionId mid-run, the same `agent-<id>.jsonl`
    continues under a second session directory, and a `message.id` split across
    the boundary is observed under two `sessionId`s. Written to disk and read
    back through the real :func:`read_transcript` — the session ids come from the
    paths exactly as they do in production, not from hand-built dataclasses that
    could agree with the mapper by construction.

    Returns the observations of both fragments, in walk order.
    """
    root = os.path.join(tmp, "claude")
    out = []
    counter = 0
    for index, session in enumerate(SPAN_SESSIONS):
        directory = os.path.join(root, "projects", "-fixture", session,
                                 "subagents", "workflows", SPAN_RUN)
        os.makedirs(directory)
        lines = []
        for tokens, message in ((10 + 80 * index, SPAN_MESSAGE),
                                (7, f"msg_only_in_fragment_{index}")):
            counter += 1
            lines.append(assistant_line(
                f"081b28a7-aee9-43dc-935d-{counter:012x}", message, tokens,
                f"2026-07-25T0{3 + index}:2{counter}:00.000Z"))
        path = os.path.join(directory, f"agent-{SPAN_AGENT}.jsonl")
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        ingest.reset_read_cache()
        out.extend(read_transcript(path, root=root).usage)
    return out


def state_of(observations):
    registry = mr.discover_mappers(["ingest"])
    ops = []
    for obs in observations:
        ops.extend(mr.map_observation(registry, "usage", obs))
    return ms.apply_operations({}, ops)


def ops_of(observations):
    registry = mr.discover_mappers(["ingest"])
    ops = []
    for obs in observations:
        ops.extend(mr.map_observation(registry, "usage", obs))
    return ops


def totals_of(state):
    docs = state.get("usage", {}).values()
    return {name: sum(doc.get(name, 0) for doc in docs) for name in USAGE_FIELDS}


# --- the extraction ------------------------------------------------------


def test_the_four_keys_are_always_four():
    print("test_the_four_keys_are_always_four")
    tokens = usage_from_message({"usage": {"output_tokens": 7}})
    check(tokens == {"in": 0, "out": 7, "cached": 0, "cache_write": 0},
          "a usage block with one key yields all four, defaulting to 0 (GD-11) — "
          "three keys would make 'no cache reads' and 'unknown' the same document")
    check(usage_from_message({"usage": {"input_tokens": None}})["in"] == 0,
          "an explicit null is 0, which is what the CLI means by it")
    for bad in ({"input_tokens": "12"}, {"output_tokens": 1.5},
                {"output_tokens": True}):
        check(usage_from_message({"usage": bad}) is None,
              f"a non-integer count refuses the whole record rather than coercing "
              f"({bad}) — $max over a coerced 0 is a silently wrong total")
    check(usage_from_message({}) is None and usage_from_message("x") is None,
          "a message with no usage is not a token record")

    real = None
    for line in Path(SPLIT).read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") == "assistant":
            real = record
            break
    tokens = usage_from_message(real["message"])
    usage = real["message"]["usage"]
    check(tokens == {"in": usage["input_tokens"], "out": usage["output_tokens"],
                     "cached": usage["cache_read_input_tokens"],
                     "cache_write": usage["cache_creation_input_tokens"]},
          "…and the four map to the harness's own four names, verbatim")


def test_split_records_of_one_message_grow():
    print("test_split_records_of_one_message_grow")
    # The empirical basis of GD-25's `$max` rule, re-measured on the frozen bytes
    # rather than quoted: `output_tokens` GROWS across the split records of one
    # message.id, so first-wins under-reports and `$set` is order-dependent.
    by_message = {}
    for line in Path(SPLIT).read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        tokens = usage_from_message(message)
        if tokens is None or not message.get("id"):
            continue
        by_message.setdefault(message["id"], []).append(tokens["out"])
    growing = {mid: values for mid, values in by_message.items()
               if len(values) > 1 and values[0] != values[-1]}
    check(growing,
          f"{len(growing)} of {len(by_message)} message ids on this one transcript "
          f"have a growing `out` across their split records")
    sample = next(iter(growing.values()))
    check(sample[-1] > sample[0],
          f"…and it grows, so first-wins under-reports: {sample[0]} -> {sample[-1]}")
    check(all(values == sorted(values) for values in growing.values()),
          "…monotonically, which is what makes $max both correct and order-free")


# --- the algebra ---------------------------------------------------------


def test_the_upsert_is_max_setoninsert_and_nothing_else():
    print("test_the_upsert_is_max_setoninsert_and_nothing_else")
    obs = UsageObservation(message_id="msg_01", session_id=DD,
                           tokens={"in": 1, "out": 2, "cached": 3, "cache_write": 4},
                           agent_id="a" * 17, run_id=RUN_ID,
                           ts="2026-07-25T03:20:00.500Z")
    (collection, key, update), = map_usage(obs)
    check(collection == "usage" and key == refs.usage_key("msg_01"),
          "`_id` is the message.id, through refs.usage_key (GD-24/SD-11)")
    check(set(update) == {"$setOnInsert", "$max", "$min"},
          f"the update uses exactly $setOnInsert / $max / $min: {sorted(update)}")
    check(set(update["$max"]) == set(USAGE_FIELDS),
          "…with all four token fields under $max (R-50)")
    check(update["$setOnInsert"] == {"provenance": "harness",
                                     "agentId": "a" * 17, "runId": RUN_ID},
          "…and the two ids that never move as immutables — R-50's 'never "
          "overwrite' expressed as an operator")
    check(update["$min"]["sessionId"] == DD
          and "sessionId" not in update["$setOnInsert"],
          "…while `sessionId` is $min, a STATED deviation from R-50's literal "
          "`$setOnInsert:{agentId, sessionId, runId}`: that list is justified by "
          "'a message.id never spans agents', which is true of agents and false "
          "of sessions, and $setOnInsert is first-writer-wins (see "
          "test_a_message_id_that_spans_two_sessions_is_order_free)")
    check(update["$min"]["ts"] is not None,
          "…and `ts` beside it, the earliest observation of the message rather "
          "than whichever split record was written first")
    check("$inc" not in update and "tsRaw" not in json.dumps(update, default=str),
          "no $inc (re-ingest after a rewrite would double it) and no tsRaw (a "
          "usage document has many source records; storing one spelling names the "
          "wrong one)")

    spec = ms.spec_for("usage")
    check(set(USAGE_FIELDS) <= spec.accumulable,
          "mongo_store fences all four as accumulable, so a $set on one is refused "
          "by validate_update — the rule is structural, not a convention")
    check(raises(ms.OperatorError, ms.validate_update,
                 {"$set": {"out": 5}}, "usage"),
          "…demonstrated: $set on `out` is refused")


def test_shuffled_and_reversed_ingest_give_identical_totals():
    print("test_shuffled_and_reversed_ingest_give_identical_totals")
    observations = corpus_observations()
    check(len(observations) > 600,
          f"the frozen run corpus yields {len(observations)} usage observations")

    normal = state_of(observations)
    reverse = state_of(list(reversed(observations)))
    shuffled = list(observations)
    random.Random(50).shuffle(shuffled)
    mixed = state_of(shuffled)

    prints = {ms.fingerprint(normal), ms.fingerprint(reverse), ms.fingerprint(mixed)}
    check(len(prints) == 1,
          "normal / reversed / shuffled ⇒ byte-identical usage documents (GD-25)")
    totals = totals_of(normal)
    check(totals_of(reverse) == totals == totals_of(mixed),
          f"…and identical totals: {totals}")
    check(len(normal["usage"]) == len({o.message_id for o in observations}),
          f"one document per distinct message.id ({len(normal['usage'])}) — the "
          f"message-id dedup, expressed as a key rather than as a set (GD-20)")

    naive = {name: sum(o.tokens[name] for o in observations) for name in USAGE_FIELDS}
    check(naive["out"] > totals["out"] and naive["cached"] > totals["cached"],
          f"…and naive summing over records over-counts: out {naive['out']} vs "
          f"{totals['out']}, cached {naive['cached']} vs {totals['cached']}")
    check(dedup_usage(observations) ==
          {doc["_id"]: {name: doc[name] for name in USAGE_FIELDS}
           for doc in normal["usage"].values()},
          "the in-memory dedup and the mirrored documents agree exactly — the live "
          "view's number and the mirror's number are one number (GD-22)")

    # The arm above is the regression floor and NOT the invariant: every message
    # id of this fixture lives in exactly one session, so the property would hold
    # whatever operator `sessionId` got. The shape that breaks it is real on the
    # live corpus, so the corpus is re-run with it mixed in.
    with tempfile.TemporaryDirectory() as tmp:
        together = observations + spanning_observations(tmp)
    check(len({o.session_id for o in together if o.message_id == SPAN_MESSAGE}) == 2,
          "the mixed corpus really does observe one message.id under two "
          "sessionIds — the shape being asserted about exists")
    mixed_orders = [state_of(together), state_of(list(reversed(together)))]
    scrambled = list(together)
    random.Random(51).shuffle(scrambled)
    mixed_orders.append(state_of(scrambled))
    check(len({ms.fingerprint(one) for one in mixed_orders}) == 1,
          "…and all three orders STILL fingerprint identically with it mixed in: "
          "`sessionId` is $min, so a live tail (arrival order) and a --rebuild "
          "(sorted-path order) store the same document (GD-25/R-55)")
    spanned = mixed_orders[0]["usage"][refs.usage_key(SPAN_MESSAGE)]
    check(spanned["sessionId"] == min(SPAN_SESSIONS) and spanned["out"] == 90,
          f"…storing the earliest-sorting session and the $max of the counts "
          f"({spanned['sessionId'][:8]}…, out={spanned['out']}) — arbitrary, but "
          f"the same on every pass, which is the whole requirement")


def test_reingest_after_a_rewrite_leaves_totals_unchanged():
    print("test_reingest_after_a_rewrite_leaves_totals_unchanged")
    # `performRemoveByUuid` truncates and rewrites a transcript, so a full
    # re-ingest under a new generation is MANDATORY (GD-26/SD-10) — which is
    # exactly why token accounting may not be an $inc counter.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "0b6c1c2a-0000-4000-8000-00000000abcd.jsonl")
        lines = Path(SPLIT).read_text(encoding="utf-8").splitlines()[:60]
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        before = state_of(read_transcript(path).usage)
        totals_before = totals_of(before)

        # The rewrite: drop two records from the middle, keeping the rest.
        rewritten = lines[:20] + lines[22:]
        Path(path).write_text("\n".join(rewritten) + "\n", encoding="utf-8")
        ingest.reset_read_cache()
        after_ops_state = state_of(read_transcript(path).usage)

        merged = ms.apply_operations({}, [])
        registry = mr.discover_mappers(["ingest"])
        ops = []
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        ingest.reset_read_cache()
        for obs in read_transcript(path).usage:
            ops.extend(mr.map_observation(registry, "usage", obs))
        Path(path).write_text("\n".join(rewritten) + "\n", encoding="utf-8")
        ingest.reset_read_cache()
        for obs in read_transcript(path).usage:
            ops.extend(mr.map_observation(registry, "usage", obs))
        merged = ms.apply_operations({}, ops)

        check(totals_of(merged) == totals_before,
              f"a full re-ingest ON TOP of the first pass leaves the totals "
              f"unchanged: {totals_of(merged)} == {totals_before}")
        check(len(merged["usage"]) >= len(after_ops_state["usage"]),
              "…and the removed records' documents are still there — the mirror "
              "exists BECAUSE the CLI deletes history (GD-26: retraction, not "
              "deletion), so a rewind never subtracts a token")
        doubled = ms.apply_operations({}, ops + ops)
        check(totals_of(doubled) == totals_before,
              "…and ingesting everything twice more still does not move them, "
              "which an $inc counter could not claim")


def test_the_identity_conflicts_are_reported_per_field():
    print("test_the_identity_conflicts_are_reported_per_field")
    # Per FIELD, because the three identity fields mean three different things:
    # an `agentId` (or `runId`) disagreement is an anomaly nothing on the corpus
    # produces, while a `sessionId` span is ordinary `/clear` topology. A counter
    # that reports only the first watches the one field that never moves.
    base = {"message_id": "msg_conflict", "session_id": DD,
            "tokens": {"in": 1, "out": 2, "cached": 0, "cache_write": 0}}
    first = UsageObservation(agent_id="a" * 17, **base)
    second = UsageObservation(agent_id="b" * 17, **base)
    check(usage_conflicts([first]) == {},
          "one agent per message id is no conflict, and prints as an empty dict")
    check(usage_conflicts([first, first]) == {},
          "…nor is the same agent observed twice")
    conflicts = usage_conflicts([first, second])
    check(conflicts == {"msg_conflict": {"agentId": ("a" * 17, "b" * 17)}},
          f"a message.id under TWO agents is reported under the field that "
          f"disagreed, with both ids: {conflicts}")

    spanning = UsageObservation(agent_id="a" * 17, message_id="msg_conflict",
                                session_id=E4, tokens=base["tokens"])
    check(usage_conflicts([first, spanning]) ==
          {"msg_conflict": {"sessionId": (DD, E4)}},
          "…and the SAME function reports a sessionId span under `sessionId` — "
          "the divergence that actually happens on the live corpus (3 ids), which "
          "the agentId-only counter could never have shown")
    both = usage_conflicts([first, second, spanning])
    check(set(both["msg_conflict"]) == {"agentId", "sessionId"},
          f"…and an id that diverges on two fields reports both, so an anomaly is "
          f"never masked by the benign span beside it: {sorted(both['msg_conflict'])}")
    check(usage_conflicts([first, UsageObservation(
        agent_id="a" * 17, message_id="msg_conflict", session_id=DD,
        run_id=RUN_ID, tokens=base["tokens"])]) == {},
        "a field one observation simply does not carry is silence, not a second "
        "value: None never becomes a conflict")

    state = state_of([first, second])
    doc = state["usage"][refs.usage_key("msg_conflict")]
    check(doc["agentId"] == "a" * 17,
          "…and the stored agentId is NEVER overwritten ($setOnInsert) — a "
          "message.id never spans agents, so the second claim is the anomaly")
    reverse = state_of([second, first])
    check(reverse["usage"][refs.usage_key("msg_conflict")]["agentId"] == "b" * 17,
          "the conflicting agentId arm IS order-dependent, which is precisely why "
          "it is counted and surfaced rather than silently resolved")
    check(ms.fingerprint(state_of([first, spanning]))
          == ms.fingerprint(state_of([spanning, first])),
          "the sessionId arm is NOT order-dependent — same anomaly reporting, "
          "different operator, because one of the two is a real topology the "
          "mirror has to store deterministically rather than an error to surface")

    real = corpus_observations()
    diverged = usage_conflicts(real)
    fields = sorted({name for row in diverged.values() for name in row})
    check(diverged == {},
          f"the frozen corpus diverges on no field at all over its "
          f"{len({o.message_id for o in real})} ids (fields seen: {fields})")
    check(len({o.session_id for o in real}) == 2
          and not any("sessionId" in row for row in diverged.values()),
          "…and that is a fact about THESE fixtures, not the invariant: the run "
          "spans two session directories and still shares no message.id between "
          "them, which is why the spanning shape above is constructed rather than "
          "looked for here (the live corpus has 3 such ids)")


def test_the_conflict_counter_has_a_runtime_path():
    print("test_the_conflict_counter_has_a_runtime_path")
    # `usage_conflicts` is a pure function over a LIST of observations, and
    # `mirror.py` maps observations one at a time and accumulates none — so
    # nothing in production ever called it and R-50's "increment a conflict
    # counter" was, at runtime, never incremented. The within-one-file case is
    # raised where a stream IS visible: the scan that produced it.
    session = "0b6c1c2a-0000-4000-8000-00000000abcd"

    def assistant(uuid, agent_id, message_id, out):
        return json.dumps({
            "type": "assistant", "uuid": uuid, "agentId": agent_id,
            "timestamp": "2026-07-25T03:20:00.000Z",
            "message": {"id": message_id, "role": "assistant",
                        "usage": {"input_tokens": 1, "output_tokens": out,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0}}})

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, f"{session}.jsonl")
        Path(path).write_text("\n".join([
            assistant("081b28a7-aee9-43dc-935d-158640000001", "a" * 17, "msg_x", 10),
            assistant("081b28a7-aee9-43dc-935d-158640000002", "a" * 17, "msg_x", 40),
            assistant("081b28a7-aee9-43dc-935d-158640000003", "b" * 17, "msg_x", 90),
        ]) + "\n", encoding="utf-8")
        scan = read_transcript(path)

    check(scan.skipped["usage_agent_conflict"] == 1,
          "one message.id observed under two agentIds raises the scan's counter — "
          "a counter no code path can increment is a silent anomaly")
    check("usage_agent_conflict" in ingest._skips(),
          "…and the key is declared with the rest, so 'nothing was skipped' stays "
          "printable rather than being a missing dict key")
    check(usage_conflicts(scan.usage) == {"msg_x": {"agentId": ("a" * 17, "b" * 17)}},
          "…and the function still reports WHICH ids disagreed and on which field, "
          "which the counter alone cannot")
    check(scan.skipped["usage_session_span"] == 0
          and scan.skipped["usage_run_conflict"] == 0,
          "…while the other two counters stay at zero: three fields, three "
          "counters, and a scan says which one moved")

    doc = state_of(scan.usage)["usage"][refs.usage_key("msg_x")]
    check(doc["agentId"] == "a" * 17 and doc["out"] == 90,
          "…while the stored document is unchanged by the anomaly: agentId is "
          "$setOnInsert (never overwritten) and `out` is still the $max")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, f"{session}.jsonl")
        Path(path).write_text("\n".join([
            assistant("081b28a7-aee9-43dc-935d-158640000001", "a" * 17, "msg_x", 10),
            assistant("081b28a7-aee9-43dc-935d-158640000002", "a" * 17, "msg_x", 40),
        ]) + "\n", encoding="utf-8")
        clean = read_transcript(path)
    check(clean.skipped["usage_agent_conflict"] == 0,
          "the ordinary split-record case — same agent, one message.id, growing "
          "output_tokens — is not a conflict and does not fire it")

    # The session counter's own runtime path. In-file, a record's own `sessionId`
    # wins over the path's for a `records` document, so two records of one file
    # CAN claim two sessions — and `usage_session_span` is the counter that says
    # so. (The common case is cross-file and needs a caller that accumulates
    # observations; that gap is `map_usage`'s stated handoff, not a missing path.)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, f"{session}.jsonl")
        first = json.loads(assistant(
            "081b28a7-aee9-43dc-935d-158640000001", "a" * 17, "msg_s", 10))
        second = json.loads(assistant(
            "081b28a7-aee9-43dc-935d-158640000002", "a" * 17, "msg_s", 40))
        first["sessionId"], second["sessionId"] = DD, E4
        Path(path).write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n",
                              encoding="utf-8")
        spanned = read_transcript(path)
    check(spanned.skipped["usage_session_span"] == 1
          and spanned.skipped["usage_agent_conflict"] == 0,
          "one message.id under two sessionIds raises `usage_session_span` and "
          "NOT the anomaly counters — the expected topology is named as itself")
    stored = state_of(spanned.usage)["usage"][refs.usage_key("msg_s")]
    check(stored["sessionId"] == min(DD, E4) and stored["out"] == 40,
          f"…and the document it produces is the $min session with the $max "
          f"tokens ({stored['sessionId'][:8]}…), which is the counter reporting a "
          f"fact rather than reporting damage")

    for base, dirnames, filenames in os.walk(RUN):
        dirnames.sort()
        for name in sorted(filenames):
            one = os.path.join(base, name)
            if not ingest.is_transcript_path(one):
                continue
            raised = read_transcript(one, root=FIX).skipped
            if raised["usage_agent_conflict"] or raised["usage_run_conflict"]:
                check(False, f"{one} reports an identity conflict")
                return
    check(True, "and no file of the frozen corpus raises either anomaly counter — "
                "they are detectors, not descriptions of the corpus")


def test_the_ts_is_the_earliest_observation():
    print("test_the_ts_is_the_earliest_observation")
    early = UsageObservation(message_id="msg_ts", session_id=DD,
                             tokens=dict.fromkeys(USAGE_FIELDS, 0),
                             ts="2026-07-25T03:00:00.000Z")
    late = UsageObservation(message_id="msg_ts", session_id=DD,
                            tokens=dict.fromkeys(USAGE_FIELDS, 0),
                            ts="2026-07-25T03:09:00.000Z")
    one = state_of([early, late])["usage"][refs.usage_key("msg_ts")]
    two = state_of([late, early])["usage"][refs.usage_key("msg_ts")]
    check(one["ts"] == two["ts"],
          "the ts is $min, so the two split records give the same document either way")
    check(one["ts"].isoformat().startswith("2026-07-25T03:00:00"),
          f"…and it is the EARLIEST observation of that message ({one['ts']})")
    check("tsRaw" not in one,
          "…with no tsRaw: several records contributed, and naming one of their "
          "spellings as THE source would be a lie (GD-11(g) pairs the two)")


def test_a_message_id_that_spans_two_sessions_is_order_free():
    print("test_a_message_id_that_spans_two_sessions_is_order_free")
    # R-50's `$setOnInsert:{agentId, sessionId, runId}` is justified by "a
    # message.id never spans AGENTS". True — and it says nothing about sessions.
    # Three of the live corpus's 4 738 ids are observed under two sessionIds
    # (one agent's fragments, split by a `/clear` mid-run), and $setOnInsert is
    # first-writer-wins, so with `sessionId` in it a live tail and a --rebuild
    # store DIFFERENT documents for those ids. That is GD-25's acceptance
    # property (R-44) and R-55's wipe/rebuild equivalence, both failing on real
    # data. This is that shape, on disk, read through the real reader.
    with tempfile.TemporaryDirectory() as tmp:
        observations = spanning_observations(tmp)
    spanning = [obs for obs in observations if obs.message_id == SPAN_MESSAGE]
    check(len(spanning) == 2
          and {obs.session_id for obs in spanning} == set(SPAN_SESSIONS)
          and len({obs.agent_id for obs in spanning}) == 1,
          f"one agent, one message.id, two session directories — the topology "
          f"exists before anything is asserted about it "
          f"({[obs.session_id[:8] for obs in spanning]})")

    forward = state_of(spanning)
    backward = state_of(list(reversed(spanning)))
    check(ms.fingerprint(forward) == ms.fingerprint(backward),
          "the two arrival orders produce byte-identical documents — the "
          "acceptance property R-44 names, on the shape that breaks it")
    doc = forward["usage"][refs.usage_key(SPAN_MESSAGE)]
    check(doc["sessionId"] == min(SPAN_SESSIONS),
          f"…because `sessionId` is $min: the earliest-sorting of the two "
          f"({doc['sessionId'][:8]}…), arbitrary but the same every pass")
    check(doc["out"] == 90 and doc["agentId"] == SPAN_AGENT,
          "…with the $max of the split counts and the agent that owns them, "
          "neither of which the deviation disturbs")

    # And the fix is not cosmetic: the pre-fix operator really would flip.
    pre_fix = [{**update["$setOnInsert"], "sessionId": obs.session_id}
               for obs in spanning for _c, _k, update in map_usage(obs)]
    check(pre_fix[0]["sessionId"] != pre_fix[1]["sessionId"],
          f"R-50's literal payload would differ between the two observations "
          f"({pre_fix[0]['sessionId'][:8]}… vs {pre_fix[1]['sessionId'][:8]}…), so "
          f"the stored document would be chosen by whichever fragment was read "
          f"first — the defect this arm detects rather than restates")

    # The divergence is not merely tolerated, it is reported (R-50's counter).
    check(usage_conflicts(observations) ==
          {SPAN_MESSAGE: {"sessionId": tuple(SPAN_SESSIONS)}},
          "…and the span is reported by field, so 'expected topology' and "
          "'anomaly' stay two different statements about one stream")


def test_the_set_on_insert_payload_never_varies_for_one_id():
    print("test_the_set_on_insert_payload_never_varies_for_one_id")
    # The property behind the arm above, and the one that catches the whole
    # $setOnInsert failure class on ANY corpus: the operator is first-writer-wins,
    # so two operations on one `_id` whose payloads differ make the stored
    # document depend on ingest order. Six lines, no fixture dependency.
    def varying(observations):
        seen = {}
        out = []
        for _collection, key, update in ops_of(observations):
            payload = update.get("$setOnInsert")
            if payload is None:
                continue
            if seen.setdefault(key, payload) != payload:
                out.append((key, seen[key], payload))
        return seen, out

    corpus = corpus_observations()
    seen, diverging = varying(corpus)
    check(len(seen) == 328 and not diverging,
          f"over the frozen corpus's {len(seen)} usage ids, no two operations "
          f"disagree about their $setOnInsert payload: {diverging[:1]}")
    with tempfile.TemporaryDirectory() as tmp:
        spanning = spanning_observations(tmp)
    _seen, diverging = varying(spanning)
    check(not diverging,
          f"…nor on the `/clear`-split shape, where one message.id is observed "
          f"under two sessions — the case that made it fail: {diverging[:1]}")
    _seen, diverging = varying(corpus + spanning)
    check(not diverging,
          "…nor over the two mixed, which is the state a real installation is in")


# --- rollups -------------------------------------------------------------


def test_rollups_are_sums_over_documents_never_counters():
    print("test_rollups_are_sums_over_documents_never_counters")
    observations = corpus_observations()
    by_agent = rollup(observations, "agentId")
    check(len(by_agent) == 7,
          f"the run's seven agents each get a rollup (got {len(by_agent)})")

    state = state_of(observations)
    grand = totals_of(state)
    summed = {name: sum(row[name] for row in by_agent.values())
              for name in USAGE_FIELDS}
    check(summed == grand,
          f"Σ over agents == Σ over documents: {summed}")

    by_run = rollup(observations, "runId")
    check(set(by_run) == {RUN_ID},
          f"…and the run rollup is one group (got {sorted(by_run)})")
    snapshot = json.loads((RUN / E4 / "workflows" / f"{RUN_ID}.json").read_text())
    check(by_run[RUN_ID]["out"] != snapshot["totalTokens"],
          f"the computed rollup is NOT the harness's `totalTokens` "
          f"({snapshot['totalTokens']}) — that figure is display-only and is never "
          f"substituted for a computed one (GD-11/R-26)")

    # The cross-session fragment: per-FILE rollups under-report, which is why
    # the key is the agent and never the file or the session (MONGOSCHEMA-9).
    whole = rollup(observations, "agentId")["a2fc883c96ff7b837"]
    one_file = rollup(read_transcript(SPLIT, root=FIX).usage, "agentId")
    check(whole["out"] > one_file["a2fc883c96ff7b837"]["out"],
          "the a2fc883c agent's total spans both of its disjoint continuations; "
          "a per-file rollup under-reports (R-48's specimen, R-50's consequence)")

    check(raises(IngestError, rollup, observations, "sessionKey"),
          "a rollup key outside the three INDEXED grouping fields is refused, not "
          "silently computed on an unindexed field")


def test_the_group_pipeline_is_the_same_sum_server_side():
    print("test_the_group_pipeline_is_the_same_sum_server_side")
    pipeline = rollup_pipeline("agentId", match={"runId": RUN_ID})
    check(pipeline[0] == {"$match": {"runId": RUN_ID}},
          "a filtered rollup matches first")
    group = pipeline[-1]["$group"]
    check(group["_id"] == "$agentId",
          "…and groups by the indexed field, not by a computed key")
    check(all(group[name] == {"$sum": f"${name}"} for name in USAGE_FIELDS),
          "every token field is a $sum over the absolute documents (R-50) — never "
          "an $inc counter that could drift from them")
    check("messages" in group, "…with the document count, so a total is auditable")
    check(raises(IngestError, rollup_pipeline, "cwd"),
          "an unindexed grouping field is refused here too")
    check(json.loads(json.dumps(pipeline)) == pipeline,
          "the pipeline is plain JSON-able data — unit-testable with no database "
          "driver installed, which is GD-21's posture")


def test_the_measured_corpus_figures():
    print("test_the_measured_corpus_figures")
    # Recorded rather than asserted against the plan's live-corpus number (see the
    # module docstring). These are computed from the frozen bytes every run, so a
    # change to the extraction shows up as a diff in this printout, and the
    # RELATIONS between them are what is checked.
    observations = corpus_observations()
    deduped = dedup_usage(observations)
    totals = {name: sum(row[name] for row in deduped.values()) for name in USAGE_FIELDS}
    naive = {name: sum(o.tokens[name] for o in observations) for name in USAGE_FIELDS}
    print(f"    observations={len(observations)} messageIds={len(deduped)}")
    print(f"    deduped={totals}")
    print(f"    naive   ={naive}")
    check(len(deduped) == 328 and len(observations) == 667,
          f"the frozen run corpus is 667 usage records over 328 message ids "
          f"(got {len(observations)} / {len(deduped)})")
    check(all(naive[name] >= totals[name] for name in USAGE_FIELDS),
          "naive summing is never lower than the deduped total, on every field")
    check(naive["cached"] > totals["cached"] * 1.5,
          f"…and on `cached` it is nearly double ({naive['cached'] / totals['cached']:.2f}×), "
          f"which is the over-count the dedup exists for")


# --- the same algebra, on a real mongod ----------------------------------


def test_live_mongod_arm():
    print("test_live_mongod_arm")
    # Everything above runs `$max`/`$setOnInsert` through `mongo_store`'s memory
    # model. That model is the thing under test as much as the mapper is, and the
    # one claim it cannot settle is whether **mongod** agrees: `$max` against a
    # missing field, `$setOnInsert` on an upsert that also `$max`es, and `$group`
    # over the stored documents are all server semantics. Without a server the arm
    # skips and every in-memory assertion still stands (GD-21).
    uri = os.environ.get("TOUCH_MONGO_URI")
    if not uri:
        skip("live Mongo arm: TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)")
        return
    if not ms.pymongo_available():
        skip("live Mongo arm: pymongo is not installed (GD-21: absence is legal)")
        return
    try:
        client = ms.open_client(uri)
    except ms.MongoUnavailable as exc:
        skip(f"live Mongo arm: {exc}")
        return
    if not ms.ping(client):
        client.close()
        skip("live Mongo arm: no mongod answered within the GD-21 timeouts")
        return
    name = f"touch_test_usage_{os.getpid()}"
    try:
        _live_checks(client[name])
    finally:
        check(name.startswith("touch_test_"),
              f"dropping only the database this test constructed: {name} (GD-27)")
        if name.startswith("touch_test_"):
            client.drop_database(name)
        client.close()


def _live_checks(db):
    if not db.name.startswith("touch_test_"):
        return
    ms.ensure_schema(db)
    observations = corpus_observations()
    registry = mr.discover_mappers(["ingest"])
    ops = []
    for obs in observations:
        ops.extend(mr.map_observation(registry, "usage", obs))
    expected = totals_of(state_of(observations))

    orders = {"normal": ops, "reversed": list(reversed(ops))}
    shuffled = list(ops)
    random.Random(50).shuffle(shuffled)
    orders["shuffled"] = shuffled

    seen = {}
    for label, sequence in orders.items():
        db["usage"].delete_many({})                   # fixture reset, not mirror code
        result = ms.bulk_upsert(db, "usage",
                                [(key, update) for _coll, key, update in sequence])
        if result["errors"]:
            check(False, f"{label}: mongod refused {result['errors'][:1]}")
            return
        docs = {doc["_id"]: doc for doc in db["usage"].find({})}
        seen[label] = (ms.fingerprint({"usage": docs}),
                       {field: sum(doc.get(field, 0) for doc in docs.values())
                        for field in USAGE_FIELDS})

    fingerprints = {label: value[0] for label, value in seen.items()}
    check(len(set(fingerprints.values())) == 1,
          f"mongod stores the same bytes in every ingest order — `$max` is "
          f"commutative on the server too, not only in the model: {fingerprints}")
    check(all(value[1] == expected for value in seen.values()),
          f"…and the stored totals equal the in-memory model's exactly: {expected}")
    check(db["usage"].count_documents({}) == len(dedup_usage(observations)) == 328,
          "one document per message.id — 667 observations collapse to 328 upserts, "
          "which is the dedup expressed as a key rather than as an in-memory set")

    # $setOnInsert really does not overwrite: re-upsert every observation with a
    # doctored agentId and the stored one must not move (R-50's decision against
    # the (agentId, message.id) compound key).
    doctored = [UsageObservation(message_id=obs.message_id, session_id=obs.session_id,
                                 tokens=obs.tokens, agent_id="a" * 17,
                                 run_id=obs.run_id, ts=obs.ts)
                for obs in observations]
    before = {doc["_id"]: doc.get("agentId") for doc in db["usage"].find({})}
    ms.bulk_upsert(db, "usage", [(key, update) for obs in doctored
                                 for _c, key, update in map_usage(obs)])
    after = {doc["_id"]: doc.get("agentId") for doc in db["usage"].find({})}
    check(before == after and not any(v == "a" * 17 for v in after.values()),
          "a conflicting agentId never overwrites the stored one on the server — "
          "the disagreement is counted by usage_conflicts, never resolved by a write")
    check(totals_of({"usage": {doc["_id"]: doc for doc in db["usage"].find({})}})
          == expected,
          "…and the token totals are unchanged by that whole second pass, which is "
          "the re-ingest-after-performRemoveByUuid property against a real server")

    # The rollup pipeline is data everywhere else in this file; here it is run.
    server = {row["_id"]: {field: row[field] for field in USAGE_FIELDS}
              for row in db["usage"].aggregate(rollup_pipeline("agentId"))}
    check(server == rollup(observations, by="agentId"),
          f"mongod's $group returns exactly the computed rollup ({len(server)} agents) "
          f"— the read-time sum and the local one are one definition, not two")
    scoped = list(db["usage"].aggregate(
        rollup_pipeline("runId", match={"runId": RUN_ID})))
    check(len(scoped) == 1 and scoped[0]["messages"] == 328,
          "…and a $match-scoped rollup counts the documents it summed, so a total "
          "on the dashboard is auditable back to its rows")

    # The `/clear`-split id, decided by mongod rather than by the model: `$min`
    # on a string beside a `$max` and a `$setOnInsert` in ONE upsert is server
    # semantics, and it is the operator the whole sessionId deviation rests on.
    with tempfile.TemporaryDirectory() as tmp:
        spanning = spanning_observations(tmp)
    stored = []
    for order in (spanning, list(reversed(spanning))):
        db["usage"].delete_many({"_id": refs.usage_key(SPAN_MESSAGE)})  # reset
        result = ms.bulk_upsert(db, "usage", [(key, update) for obs in order
                                              for _c, key, update in map_usage(obs)])
        if result["errors"]:
            check(False, f"mongod refused the spanning upsert: {result['errors'][:1]}")
            return
        stored.append(db["usage"].find_one({"_id": refs.usage_key(SPAN_MESSAGE)}))
    check(stored[0] == stored[1] and stored[0]["sessionId"] == min(SPAN_SESSIONS),
          f"one message.id under two sessions reads back as ONE document, the "
          f"same one in either arrival order, holding the $min session "
          f"({stored[0]['sessionId'][:8]}…) — GD-25 on the server for the shape "
          f"the frozen fixtures cannot express")
    check(stored[0]["out"] == 90 and stored[0]["agentId"] == SPAN_AGENT,
          "…with the $max still accumulating across the two fragments and the "
          "$setOnInsert agentId untouched beside the $min")


def main():
    print("test_usage.py — R-50 (usage mirror: absolute $max documents)\n")
    for test in (test_the_four_keys_are_always_four,
                 test_split_records_of_one_message_grow,
                 test_the_upsert_is_max_setoninsert_and_nothing_else,
                 test_shuffled_and_reversed_ingest_give_identical_totals,
                 test_reingest_after_a_rewrite_leaves_totals_unchanged,
                 test_the_identity_conflicts_are_reported_per_field,
                 test_the_conflict_counter_has_a_runtime_path,
                 test_the_ts_is_the_earliest_observation,
                 test_a_message_id_that_spans_two_sessions_is_order_free,
                 test_the_set_on_insert_payload_never_varies_for_one_id,
                 test_rollups_are_sums_over_documents_never_counters,
                 test_the_group_pipeline_is_the_same_sum_server_side,
                 test_the_measured_corpus_figures,
                 test_live_mongod_arm):
        test()
        ingest.reset_read_cache()
    print()
    for message in skips:
        print(f"skipped: {message}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for one in failures:
            print(f"  - {one}")
        sys.exit(1)
    print("all usage tests passed")


if __name__ == "__main__":
    main()
