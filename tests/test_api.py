#!/usr/bin/env python3
"""Stdlib-only tests for the read API and the socket contract (R-31 / R-55).
Run as `python3 test_api.py`; exits non-zero on failure. No pytest, no runner.

R-31's own test list is "unknown session/run/id ⇒ 404; a bare `after=` without
a stream selector ⇒ 400; pagination round-trip without duplicates", and R-55's
is "reconnect mid-stream ⇒ no duplicate events, counters equal full-replay
totals; backfill burst carries `live:false`". Both lists are here, plus the
clauses those lists imply and would not otherwise pin:

* every id goes through ONE validator — malformed is 400, unknown is 404, and
  the two never swap;
* the bounded default window replays the current run whole and caps the rest,
  publishing `oldest`/`truncated` so "load older" has somewhere to go;
* token frames coalesce ≥1 s and the survivors are **absolute** — the property
  that makes `(stream, seq)` resume safe (a summed delta replay would be
  silently low, which is why R-55 calls the two a package);
* `/api/query` answers from the in-memory model with `source: "memory"` when no
  Mongo query source is injected, and the UI therefore never depends on Mongo.

The fixture state is built through the **real mappers** (`sessions.map_session`,
`ingest.map_record`, …) folded with `mongo_store.apply_operations`, so these
tests assert against the documents the mirror actually stores rather than
against a hand-written idea of them.
"""

import datetime
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from aggregator import agents as agents_mod                        # noqa: E402
from aggregator import ingest as ingest_mod                        # noqa: E402
from aggregator import mongo_store as ms                           # noqa: E402
from aggregator import server as server_mod                        # noqa: E402
from aggregator import sessions as sessions_mod                    # noqa: E402
from aggregator import store as store_mod                          # noqa: E402
from aggregator.server import (                                    # noqa: E402
    DEFAULT_REPLAY_EVENTS,
    MAX_REPLAY_EVENTS,
    MAX_TICK_EVENTS,
    Api,
    Auth,
    HttpError,
    ReadModel,
    TokenCoalescer,
    WsSession,
    parse_cursor_params,
    replay_window,
    valid_id,
)

failures = []
TMPDIRS = []

SID_LIVE = "11111111-1111-4111-8111-111111111111"
SID_HIST = "22222222-2222-4222-8222-222222222222"
SID_MISSING = "33333333-3333-4333-8333-333333333333"
AGENT_A = "a2fc883c96ff7b837"
AGENT_B = "dd469822c0f1e2a34"
RUN_ID = "wf_test1234-abc"
STREAM = f"run:{RUN_ID}"

FIX = REPO / "tests" / "fixtures"
#: The frozen live-run-shape session: ten `.jsonl` files under one sessionId,
#: nine of them numbering their lines from 1. It is here because the property
#: it exhibits — a `lineNo` shared by nine documents — cannot be hand-built
#: without deciding in advance that it matters.
LIVE_DIR = FIX / "mirror" / "live-run-shape" / "a8d43bb1-0313-45d4-8784-4827af443ead"
LIVE_SID = "a8d43bb1-0313-45d4-8784-4827af443ead"


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def tmpdir(name):
    path = tempfile.mkdtemp(prefix=f"touch-{name}-")
    TMPDIRS.append(path)
    return path


def body(response):
    return json.loads(response.body.decode("utf-8"))


def ts(minute, second=0):
    return datetime.datetime(2026, 7, 25, 3, minute, second,
                             tzinfo=datetime.timezone.utc)


# --- the fixture state ----------------------------------------------------


def build_state():
    """The mirror memory model, built through the real mappers."""
    state = {}
    ops = []

    ops += sessions_mod.map_session(sessions_mod.SessionObservation(
        session_id=SID_LIVE, pid=622, proc_start="10028", cwd="/repo",
        slugs=("-repo",), sources=(sessions_mod.Source(path=f"projects/-repo/{SID_LIVE}.jsonl"),),
        first_ts=ts(20), last_ts=ts(59)))
    ops += sessions_mod.map_session(sessions_mod.SessionObservation(
        session_id=SID_HIST, sources=(sessions_mod.Source(path=f"projects/-repo/{SID_HIST}.jsonl"),),
        first_ts=ts(10), last_ts=ts(15)))

    for line in range(1, 6):
        spill = None
        tool_use_ids = ()
        if line == 3:
            tool_use_ids = ("toolu_spill01",)
        ops += ingest_mod.map_record(ingest_mod.RecordObservation(
            uuid=f"{line:08d}-0000-4000-8000-000000000000", session_id=SID_LIVE,
            type="assistant" if line % 2 else "user", line_no=line,
            byte_offset=line * 100, body={"role": "assistant", "text": f"line {line}"},
            ts=ts(20 + line), tool_use_ids=tool_use_ids, agent_id=AGENT_A if line > 3 else None,
            spill=spill))
    ops += ingest_mod.map_stream_meta(ingest_mod.StreamMetaObservation(
        session_id=SID_LIVE, line_no=6, byte_offset=600, type="mode", body={"mode": "plan"},
        ts=ts(26)))

    ops += ingest_mod.map_run(ingest_mod.RunObservation(
        run_id=RUN_ID, session_ids=(SID_LIVE,), task_id="wehl89qzc",
        workflow_name="implement", started_at=ts(20)))
    for ordinal, (key, agent, seen) in enumerate((("research/impl", AGENT_A, True),
                                                  ("research/test", AGENT_B, False))):
        ops += ingest_mod.map_run_node(ingest_mod.RunNodeObservation(
            run_id=RUN_ID, key=key, ordinal=0, journal_seq=ordinal, agent_id=agent,
            result_seen=seen, result={"passed": True} if seen else None,
            started_at=ts(21), ended_at=ts(30) if seen else None))
    for agent, last in ((AGENT_A, ts(30)), (AGENT_B, ts(31))):
        ops += agents_mod.map_agent(agents_mod.AgentObservation(
            agent_id=agent, run_id=RUN_ID, sessions=(SID_LIVE,),
            files=(f"projects/-repo/{SID_LIVE}/subagents/agent-{agent}.jsonl",),
            fragments=(agents_mod.Fragment(
                agent_id=agent, session_id=SID_LIVE,
                path=f"projects/-repo/{SID_LIVE}/subagents/agent-{agent}.jsonl",
                first_uuid="00000001-0000-4000-8000-000000000000",
                last_uuid="00000002-0000-4000-8000-000000000000",
                line_count=12, record_count=12, first_record_ts=ts(21), last_ts=last),),
            first_ts=ts(21), last_ts=last, unconventional=False))
    return ms.apply_operations(state, ops)


def corpus_state():
    """The `live-run-shape` session, ingested through the real mapper.

    Every one of its `.jsonl` files is read — the session transcript **and**
    each `subagents/**/agent-*.jsonl` — because that is what makes `lineNo`
    non-unique per session, which is the whole point of paging on it here.
    """
    ops = list(sessions_mod.map_session(sessions_mod.SessionObservation(
        session_id=LIVE_SID, pid=911, proc_start="20055", cwd="/repo",
        slugs=("-repo",),
        sources=(sessions_mod.Source(path=f"projects/-repo/{LIVE_SID}.jsonl"),),
        first_ts=ts(10), last_ts=ts(59))))
    for base, dirnames, filenames in os.walk(LIVE_DIR):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(".jsonl"):
                continue
            scan = ingest_mod.read_transcript(os.path.join(base, name),
                                              session_id=LIVE_SID, root=str(FIX))
            for observation in scan.records:
                ops.extend(ingest_mod.map_record(observation))
    return ms.apply_operations({}, ops)


def build_store(root, *, records=12, tokens=0):
    """A `.touch/` store with one run stream (plus a session stream)."""
    store = store_mod.Store(root)
    specs = []
    for n in range(records):
        specs.append({"kind": "node", "provenance": "harness",
                      "ref": {"runId": RUN_ID, "key": "research/impl", "ordinal": 0},
                      "data": {"n": n}, "source": "ingest", "ts": None})
    if specs:
        store.append_many(STREAM, specs)
    for n in range(tokens):
        store.append(STREAM, kind="token", provenance="harness",
                     ref={"agentId": AGENT_A},
                     data={"tokens": {"in": 10 * (n + 1), "out": n + 1,
                                      "cached": 0, "cache_write": 0}})
    store.append("session:622-10028", kind="session", provenance="harness",
                 ref={"pid": 622, "procStart": "10028"}, data={"hello": True})
    return store


def make_api(*, store=None, state=None, tasks_root=None, claude_root=None,
             query_source=None):
    model = ReadModel(state=state if state is not None else build_state(), store=store,
                      tasks_root=tasks_root, claude_root=claude_root,
                      query_source=query_source, reduce_ttl=0)
    api = Api(model, auth=Auth("t0ken"))
    return api


def get(api, path):
    return api.get(path, {"authorization": "Bearer t0ken"})


# --- id validation --------------------------------------------------------


def test_one_validator_400s_malformed_and_404s_unknown():
    print("test_one_validator_400s_malformed_and_404s_unknown")
    api = make_api()
    check(get(api, "/api/session/timeline?session=not-a-uuid").status == 400,
          "a malformed session id is 400, not 404")
    check(get(api, f"/api/session/timeline?session={SID_MISSING}").status == 404,
          "a well-formed but unobserved session id is 404")
    check(get(api, "/api/run/graph?run=wf_nope").status == 404, "an unknown run is 404")
    check(get(api, "/api/run/graph?run=%20bad%20id").status == 400, "a malformed runId is 400")
    check(get(api, f"/api/run/node?run={RUN_ID}&agent=deadbeef").status == 400,
          "an 8-hex agent id is malformed for the 17-hex validator (400)")
    check(get(api, f"/api/run/node?run={RUN_ID}&agent=00000000000000000").status == 404,
          "a well-formed unknown agentId is 404")
    check(get(api, "/api/session/timeline").status == 400,
          "a missing required id is 400 (nothing is defaulted)")
    check(get(api, f"/api/session/timeline?session={SID_LIVE}&session={SID_HIST}").status == 400,
          "a repeated parameter is 400 — first-wins would be a silent wrong target")
    try:
        valid_id("session", SID_LIVE)
        ok = True
    except HttpError:
        ok = False
    check(ok, "the shared validator accepts a real uuid")
    check(get(api, "/api/session/timeline?session=AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"
              ).status == 404,
          "an uppercase-hex uuid is well-formed (400 is not it) and unobserved (404 is)")


# --- sessions -------------------------------------------------------------


def test_sessions_lists_both_classes():
    print("test_sessions_lists_both_classes")
    api = make_api()
    payload = body(get(api, "/api/sessions"))
    kinds = {row["id"]: row["kind"] for row in payload["sessions"]}
    check(payload["count"] == 2, "both sessions are listed")
    check(kinds.get("live:622-10028") == "live", "the live arm is labelled live")
    check(kinds.get(f"hist:{SID_HIST}") == "historical",
          "the historical arm is listed, labelled, not hidden")
    check(payload["sessions"][0]["id"] == "live:622-10028",
          "rows sort by last activity, newest first")
    live_only = body(get(api, "/api/sessions?live=1"))
    check(live_only["count"] == 1, "?live=1 filters to the live arm")
    check(get(api, "/api/sessions").status == 200 and
          "token" not in get(api, "/api/sessions").body.decode(),
          "no session response ever carries the token")


# --- timeline -------------------------------------------------------------


def page_timeline(api, session_id, *, limit, pages=200, extra=""):
    """Walk the timeline with the cursor the API itself handed back."""
    seen = []
    since, since_id = 0, ""
    for _ in range(pages):
        page = body(get(api, f"/api/session/timeline?session={session_id}"
                             f"&since={since}&sinceId={urllib.parse.quote(since_id, safe='')}"
                             f"&limit={limit}{extra}"))
        seen.extend(r["_id"] for r in page["records"])
        if not page["hasMore"]:
            return seen, page
        cursor = (since, since_id)
        since, since_id = page["nextSince"], page["nextSinceId"]
        if (since, since_id) <= cursor:
            raise AssertionError("the cursor did not advance")
    raise AssertionError("pagination did not terminate")


def test_timeline_pages_without_duplicates():
    print("test_timeline_pages_without_duplicates")
    api = make_api()
    seen, _ = page_timeline(api, SID_LIVE, limit=2)
    check(len(seen) == len(set(seen)), "a pagination round-trip yields no duplicate record")
    check(len(seen) == 5, "and no gaps: every fixture record appears exactly once")
    full = body(get(api, f"/api/session/timeline?session={SID_LIVE}&limit=1000"))
    check([r["_id"] for r in full["records"]] == seen,
          "paged order equals unpaged order (lineNo, an explicit field — R-47)")
    bare = body(get(api, f"/api/session/timeline?session={SID_LIVE}&since=3"))
    check([r["lineNo"] for r in bare["records"]] == [3, 4, 5],
          "a `since=` with no sinceId is the position BEFORE line 3, not after it: "
          "half a cursor re-reads a line rather than losing the rest of its group, "
          "and re-reading is the failure a client can see")


def test_the_timeline_cursor_is_the_whole_sort_key_not_a_prefix_of_it():
    print("test_the_timeline_cursor_is_the_whole_sort_key_not_a_prefix_of_it")
    # `lineNo` is unique per FILE, not per session: one sessionId's records are
    # ingested from its transcript and from every subagent file beside it, each
    # numbered from 1. A `> lineNo` cursor therefore discards whatever is left
    # of the group a page boundary lands inside — and no cursor the client
    # holds can ever reach it again. Paging on `(lineNo, _id)`, which is the
    # order the rows are already sorted in, cannot skip.
    api = make_api(state=corpus_state())
    full = body(get(api, f"/api/session/timeline?session={LIVE_SID}&limit=1000"))
    unpaged = [r["_id"] for r in full["records"]]
    check(len(unpaged) == 671, f"the frozen corpus session holds 671 records ({len(unpaged)})")
    groups = {}
    for row in full["records"]:
        groups.setdefault(row["lineNo"], []).append(row["_id"])
    collisions = [line for line, ids in groups.items() if len(ids) > 1]
    check(len(collisions) == 100 and max(len(ids) for ids in groups.values()) == 9,
          f"100 of its line numbers carry nine documents each — the shape a hand-built "
          f"fixture cannot have ({len(collisions)} groups)")

    seen, _ = page_timeline(api, LIVE_SID, limit=100)
    check(len(seen) == len(set(seen)), "a paged round-trip yields no duplicate")
    check(set(seen) == set(unpaged),
          f"and loses nothing: set equality with the unpaged answer "
          f"({len(set(seen))} of {len(set(unpaged))})")
    check(seen == unpaged, "in exactly the unpaged order, page boundaries included")
    odd, _ = page_timeline(api, LIVE_SID, limit=7)
    check(odd == unpaged,
          "and with a page size that cannot align with a group boundary either")


def test_timeline_omits_bodies_until_asked():
    print("test_timeline_omits_bodies_until_asked")
    api = make_api()
    page = body(get(api, f"/api/session/timeline?session={SID_LIVE}"))
    check(all("body" not in r for r in page["records"]),
          "bodies are omitted by default (the corpus holds an 872 KB line)")
    check(page["bodies"] is False, "and the response says so rather than looking empty")
    full = body(get(api, f"/api/session/timeline?session={SID_LIVE}&full=1"))
    check(any("body" in r for r in full["records"]), "?full=1 returns bodies")
    meta = body(get(api, f"/api/session/timeline?session={SID_LIVE}&meta=1"))
    kinds = {r["collection"] for r in meta["records"]}
    check(kinds == {"records", "stream_meta"},
          "?meta=1 merges the positional stream_meta rows, each labelled by collection")
    check(len(meta["records"]) == 6, "all six lines are present — nothing collapsed (R-47)")


# --- events / cursors -----------------------------------------------------


def test_a_bare_after_is_not_a_cursor():
    print("test_a_bare_after_is_not_a_cursor")
    root = tmpdir("events")
    api = make_api(store=build_store(root))
    response = get(api, "/api/events?after=3")
    check(response.status == 400, "a bare after= without a stream selector is 400 (GD-11)")
    check("cursor is (stream, seq)" in body(response)["message"],
          "and the message says why, in the rule's own words")
    check(get(api, "/api/events").status == 400, "no selector at all is 400")
    check(get(api, f"/api/events?stream={STREAM}&run={RUN_ID}").status == 400,
          "two selectors are 400 — there is no priority order to guess with")
    check(get(api, f"/api/events?run={RUN_ID}&after=notanumber").status == 400,
          "a non-numeric after= is 400")


def test_an_unobserved_run_or_stream_is_404_not_an_empty_list():
    print("test_an_unobserved_run_or_stream_is_404_not_an_empty_list")
    # `run:<anything>` is a well-formed stream id, so this arm cannot lean on
    # the syntactic validator the way `session=` leans on its uuid: without an
    # existence check it answers 200 with `records: []` and a `head` cursor for
    # a run that has never existed — a made-up fact about a made-up run, which
    # is the wrong-target answer wearing a success code (GD-12/R-31).
    root = tmpdir("unknown")
    api = make_api(store=build_store(root))
    unknown = get(api, "/api/events?run=wf_totally-unknown")
    check(unknown.status == 404, "an unobserved run= is 404, not 200 with an empty list")
    check("wf_totally-unknown" in body(unknown)["message"],
          "and the message names the run that was not found")
    check(get(api, "/api/events?stream=run:nope").status == 404,
          "the same for a stream= nobody has appended to")
    check(get(api, "/api/events?stream=not+a+stream").status == 400,
          "a malformed stream id is still 400 — malformed and unknown never swap")
    check(get(api, f"/api/events?run={RUN_ID}").status == 200,
          "and an observed run still answers")


def test_a_zero_limit_cannot_produce_an_endless_page():
    print("test_a_zero_limit_cannot_produce_an_endless_page")
    api = make_api(store=build_store(tmpdir("limit0")))
    page = body(get(api, f"/api/events?run={RUN_ID}&limit=0"))
    check(page["count"] == 1 and page["cursor"] is not None,
          "limit=0 is clamped to one record, so a hasMore loop always has a cursor "
          "to advance with — an empty page claiming hasMore never terminates")
    timeline = body(get(api, f"/api/session/timeline?session={SID_LIVE}&limit=0"))
    check(timeline["count"] == 1 and timeline["nextSince"] == 1,
          "same clamp on the timeline, and its cursor advances too")


def test_a_valueless_flag_is_the_hand_typed_form_and_means_true():
    print("test_a_valueless_flag_is_the_hand_typed_form_and_means_true")
    api = make_api()
    page = body(get(api, f"/api/session/timeline?session={SID_LIVE}&full"))
    check(page["bodies"] is True,
          "`?full` with no value is what a human types, and it means true — "
          "keep_blank_values exists so this reaches the parameter, not so it is dropped")
    check(body(get(api, f"/api/session/timeline?session={SID_LIVE}&full=0"))["bodies"] is False,
          "and an explicit 0 still means false")


def test_events_pages_forwards_and_backwards():
    print("test_events_pages_forwards_and_backwards")
    root = tmpdir("events2")
    api = make_api(store=build_store(root, records=12))
    first = body(get(api, f"/api/events?run={RUN_ID}&limit=5"))
    check([r["seq"] for r in first["records"]] == [1, 2, 3, 4, 5], "forward paging starts at seq 1")
    check(first["hasMore"] is True, "and reports there is more")
    second = body(get(api, f"/api/events?run={RUN_ID}&after=5&limit=5"))
    check([r["seq"] for r in second["records"]] == [6, 7, 8, 9, 10],
          "after= resumes exactly after the cursor, no duplicate, no gap")
    older = body(get(api, f"/api/events?run={RUN_ID}&before=6&limit=3"))
    check([r["seq"] for r in older["records"]] == [3, 4, 5],
          "before= is R-55's load-older arm: the three records just before the cursor")
    check(older["hasOlder"] is True, "and it says whether there is more history")
    check(first["cursor"] == store_mod.cursor_key(STREAM, 5),
          "the cursor token is the (stream, seq) grammar, byte-identical to the events _id")

    # `cursor` used to mean "where to continue" going forwards and "the newest
    # record of the page you just got" going backwards — one name, two opposite
    # meanings, one endpoint. A page that walks a truncation with the cursor it
    # was handed would step *forwards* into records it already had.
    check(older["cursor"] == store_mod.cursor_key(STREAM, 3),
          "backwards, the cursor is the OLDEST record on the page — the next before=")
    walked, cursor, pages = [], 6, 0
    while cursor is not None and pages < 20:
        page = body(get(api, f"/api/events?run={RUN_ID}&before={cursor}&limit=2"))
        walked = [r["seq"] for r in page["records"]] + walked
        pages += 1
        cursor = (store_mod.parse_cursor_key(page["cursor"])[1]
                  if page["cursor"] and page["hasOlder"] else None)
    check(walked == [1, 2, 3, 4, 5],
          "so feeding it straight back walks the history to its start, once each: "
          f"{walked}")


def test_events_of_a_historical_session_is_not_a_fallback():
    print("test_events_of_a_historical_session_is_not_a_fallback")
    root = tmpdir("events3")
    api = make_api(store=build_store(root))
    live = body(get(api, f"/api/events?session={SID_LIVE}"))
    check(live["stream"] == "session:622-10028",
          "a live session maps to its own .touch/ stream")
    hist = body(get(api, f"/api/events?session={SID_HIST}"))
    check(hist["stream"] is None and hist["records"] == [],
          "a historical session has no stream — an empty answer, never another session's")
    check(get(api, f"/api/events?session={SID_MISSING}").status == 404,
          "an unobserved session is 404, never a fallback (GD-12)")


# --- run graph / node -----------------------------------------------------


def test_run_graph_serves_the_reducers_output():
    print("test_run_graph_serves_the_reducers_output")
    api = make_api()
    payload = body(get(api, f"/api/run/graph?run={RUN_ID}"))
    check(payload["counts"] == {"nodes": 2, "agents": 2}, "both nodes and both agents are joined")
    node = payload["nodes"][0]
    check(set(node) == {"id", "observed", "derived"},
          "an observation and the verdict about it stay in separate objects (GD-23)")
    states = {row["observed"]["key"]: row["derived"]["state"] for row in payload["nodes"]}
    check(states == {"research/impl": "done", "research/test": "unknown"},
          "the resulted node is done; the resultless one idle past 180s is unknown")
    check("failed" not in set(states.values()),
          "a resultless node is never `failed` — R-58's fabricated badge, refused at the API too")
    check(node["derived"]["state"] in agents_mod.NODE_STATES,
          "the state came from the reducer's own vocabulary, not from this file")
    check(payload["derived"]["state"] == "done" and payload["derived"]["closed"] is True,
          "the run carries the reducer's own close verdict")
    check("attemptLabel" in node["derived"] and "nextStage" in node["derived"],
          "optional keys are present as null — sp-13 reads no-value as null, never absent")


def test_run_node_resolves_spawn_without_reading_the_file():
    print("test_run_node_resolves_spawn_without_reading_the_file")
    state = build_state()
    # A spawn with a fileHint pointing at a file that does not exist: R-48's
    # stale arm, which must stay a label rather than an error.
    state["agents"][AGENT_A]["spawn"] = {
        "recordUuid": "00000001-0000-4000-8000-000000000000",
        "toolUseId": "toolu_spawn01",
        "fileHint": {"path": "projects/-repo/gone.jsonl", "line": 42,
                     "stDev": 1, "ino": 2, "size": 3},
    }
    api = make_api(state=state, claude_root=tmpdir("root"))
    payload = body(get(api, f"/api/run/node?run={RUN_ID}&agent={AGENT_A}"))
    check(payload["node"]["observed"]["agentId"] == AGENT_A, "the node is the one asked for")
    check(payload["spawn"]["hint"]["valid"] is False,
          "a hint whose file is gone is reported invalid, not raised")
    check(payload["spawn"]["record"] is True,
          "and the jump still resolves — by recordUuid, through records (R-48)")
    check(payload["agent"]["fragments"] and
          payload["agent"]["fragments"][0]["lineCount"] == 12,
          "fragments come back recombined via agents.fragments_of, the reader's contract")
    check(get(api, f"/api/run/node?run={RUN_ID}&agent={AGENT_B}").status == 200,
          "a node with no spawn observation is still served")


# --- spilled tool results -------------------------------------------------


def test_toolresult_rechecks_containment_at_serve_time():
    print("test_toolresult_rechecks_containment_at_serve_time")
    root = tmpdir("claude")
    spill_dir = os.path.join(root, "projects", "-repo", SID_LIVE, "tool-results")
    os.makedirs(spill_dir)
    spill_path = os.path.join(spill_dir, "abc123def.txt")
    with open(spill_path, "w") as fh:
        fh.write("the spilled output")

    state = build_state()
    ms.apply_operations(state, ingest_mod.map_record(ingest_mod.RecordObservation(
        uuid="00000009-0000-4000-8000-000000000000", session_id=SID_LIVE, type="user",
        line_no=9, byte_offset=900, body={"text": "tool result"}, ts=ts(29),
        tool_use_ids=("toolu_spill01",),
        spill=ingest_mod.SpillPointer(tool_use_id="toolu_spill01", path=spill_path,
                                      basename="abc123def.txt", contained=True,
                                      session_id=SID_LIVE))))
    api = make_api(state=state, claude_root=root)
    response = get(api, "/api/toolresult?id=toolu_spill01")
    check(response.status == 200 and response.body == b"the spilled output",
          "a contained spill is served")
    check(response.headers["Content-Security-Policy"] == "sandbox allow-scripts",
          "in a CSP sandbox, as text/plain with nosniff")
    check(get(api, "/api/toolresult?id=toolu_absent").status == 404,
          "an unrecorded toolUseId is 404")

    # The stored `contained` flag is a fact about ingest time; the request is
    # now. A server that trusted the flag would read whatever the path says.
    hostile = build_state()
    ms.apply_operations(hostile, ingest_mod.map_record(ingest_mod.RecordObservation(
        uuid="00000010-0000-4000-8000-000000000000", session_id=SID_LIVE, type="user",
        line_no=10, byte_offset=1000, body={}, ts=ts(29), tool_use_ids=("toolu_evil",),
        spill=ingest_mod.SpillPointer(tool_use_id="toolu_evil", path="/etc/passwd",
                                      basename="passwd", contained=True,
                                      session_id=SID_LIVE))))
    evil_api = make_api(state=hostile, claude_root=root)
    check(evil_api.get("/api/toolresult?id=toolu_evil",
                       {"authorization": "Bearer t0ken"}).status == 403,
          "a path outside tool-results/ is refused at serve time, flag or no flag")
    check(get(make_api(state=state, claude_root=None),
              "/api/toolresult?id=toolu_spill01").status == 403,
          "and with no root there is nothing to be contained by — refused, not 'unknown'")

    # The basename travels in a header and comes from agent-authored text; a
    # POSIX filename may hold a CRLF. Percent-encoding keeps it recoverable;
    # `Response.head_bytes` is what keeps it from being a second header.
    crlf = "ok.txt\r\nX-Injected: yes"
    crlf_path = os.path.join(spill_dir, "crlf.txt")
    with open(crlf_path, "w") as fh:
        fh.write("body")
    hostile_name = build_state()
    ms.apply_operations(hostile_name, ingest_mod.map_record(ingest_mod.RecordObservation(
        uuid="00000011-0000-4000-8000-000000000000", session_id=SID_LIVE, type="user",
        line_no=11, byte_offset=1100, body={}, ts=ts(29), tool_use_ids=("toolu_crlf",),
        spill=ingest_mod.SpillPointer(tool_use_id="toolu_crlf", path=crlf_path,
                                      basename=crlf, contained=True,
                                      session_id=SID_LIVE))))
    served = get(make_api(state=hostile_name, claude_root=root), "/api/toolresult?id=toolu_crlf")
    head = served.to_bytes().split(b"\r\n\r\n")[0].split(b"\r\n")
    check(served.status == 200 and not [l for l in head if l.startswith(b"X-Injected")],
          "a CRLF-bearing basename cannot become a header of its own")
    check(urllib.parse.unquote(served.headers["X-Touch-Basename"]) == crlf,
          "it is percent-encoded, so the page can still recover the real name")


# --- legacy task folders --------------------------------------------------


def test_tasks_lists_legacy_folders_with_their_derivation():
    print("test_tasks_lists_legacy_folders_with_their_derivation")
    root = tmpdir("orch")
    folder = os.path.join(root, "touch-demo")
    os.makedirs(folder)
    lines = [
        {"ts": "2026-07-25T03:20:00Z", "plan": "research", "stage": "plan",
         "state": "running", "detail": "starting"},
        {"ts": "2026-07-25T03:21:00Z", "plan": "research", "stage": "impl",
         "state": "running", "detail": "attempt 1", "agent": "aaaaaaaa"},
        {"ts": "2026-07-25T03:29:00Z", "plan": "research", "stage": "impl",
         "state": "done", "detail": "attempt 1 ok", "agent": "aaaaaaaa"},
        # The fabricated badge R-58 exists for, followed by the driver's own
        # correction: last-event-wins in file order (SD-4).
        {"ts": "2026-07-25T03:30:00Z", "plan": "research", "stage": "plan",
         "state": "failed", "detail": "loop exited -> synthesis"},
        {"ts": "2026-07-25T03:30:01Z", "plan": "research", "stage": "plan",
         "state": "done", "detail": "closed"},
    ]
    with open(os.path.join(folder, "events.jsonl"), "w") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    api = make_api(tasks_root=root)
    payload = body(get(api, "/api/tasks"))
    check(payload["count"] == 1 and payload["tasks"][0]["task"] == "touch-demo",
          "a task folder is listed")
    plan = payload["tasks"][0]["plans"]["research"]
    check(plan["badge"] != "failed",
          "the corrective `done` beats the earlier fabricated `failed` (SD-4/R-58)")
    check(payload["tasks"][0]["nodes"], "its nodes travel with it")
    check("archive" in payload["tasks"][0],
          "and the derived archive label, which is a fact about the source, not a constant")
    empty = make_api(tasks_root=None)
    check(body(get(empty, "/api/tasks"))["tasks"] == [],
          "with no configured root the answer is empty and says so — never a guess")


# --- /api/query -----------------------------------------------------------


class FakeQuerySource:
    """The injected Mongo read path, without a driver."""

    def __init__(self, docs):
        self.docs = docs
        self.calls = []

    def find(self, collection, criteria, limit=100):
        self.calls.append((collection, dict(criteria), limit))
        return self.docs[:limit]


def test_query_falls_back_to_memory_and_says_so():
    print("test_query_falls_back_to_memory_and_says_so")
    api = make_api()
    payload = body(get(api, "/api/query?collection=agents"))
    check(payload["source"] == "memory",
          "with no query source the answer comes from the in-memory model (GD-22)")
    check(payload["count"] == 2 and "never on this path" in payload["note"],
          "and the note states the fallback rather than leaving it to be inferred")
    check(get(api, "/api/query?collection=nope").status == 400,
          "an unknown collection is a 400 from the shared validator")
    check(get(api, '/api/query?collection=agents&filter={"$where":1}').status == 400,
          "operators are refused")
    check(get(api, '/api/query?collection=agents&filter={"a.b":1}').status == 400,
          "dotted paths are refused (LIVEFLOW-3: a dotted-_id query COLLSCANs)")
    filtered = body(get(api, '/api/query?collection=agents&filter={"runId":"%s"}' % RUN_ID))
    check(filtered["count"] == 2, "an equality filter selects")

    source = FakeQuerySource([{"_id": "x"}])
    api2 = make_api(query_source=source)
    payload2 = body(get(api2, "/api/query?collection=agents&limit=5"))
    check(payload2["source"] == "mongo" and source.calls[0][0] == "agents",
          "an injected query source is used and labelled mongo")

    # GD-21 keeps a driver out of server.py, so this arm is a seam — and today
    # the only implementation of it is the fake above. An interface with a fake
    # and no producer drifts, so the shape is pinned here and stated in the
    # docstring the next implementer will read.
    check(source.calls[0] == ("agents", {}, 5),
          "the provider is called as find(collection, criteria, limit=) — not query(), "
          "not find(collection, **kwargs)")
    check("find(collection: str, criteria: dict, limit: int) -> iterable[dict]"
          in server_mod.h_query.__doc__,
          "and h_query names that signature, so the Mongo read is written to match it")

    class GreedySource:
        """A provider that ignores `limit` — why the handler re-truncates."""

        def find(self, collection, criteria, limit=100):
            return [{"_id": n} for n in range(50)]

    over = body(get(make_api(query_source=GreedySource()),
                    "/api/query?collection=agents&limit=3"))
    check(over["count"] == 3,
          "a provider that ignores the limit is cut back here — it is a seam, not a promise")


# --- the wire (R-55) ------------------------------------------------------


def test_replay_window_is_bounded_and_publishes_its_edge():
    print("test_replay_window_is_bounded_and_publishes_its_edge")
    records = [{"seq": n} for n in range(1, 1001)]
    window, truncated, oldest = replay_window(records, limit=100)
    check(len(window) == 100 and window[-1]["seq"] == 1000,
          "the default window keeps the newest N")
    check(truncated is True and oldest == 901,
          "truncation is reported with the seq to load older from")
    whole, truncated2, oldest2 = replay_window(records, limit=100, whole=True)
    check(len(whole) == 1000 and truncated2 is False and oldest2 == 1,
          "the current run's stream replays whole — 'current run or last N, whichever larger'")
    explicit, _, _ = replay_window(records, limit=100, from_seq=995)
    check([r["seq"] for r in explicit] == [996, 997, 998, 999, 1000],
          "?from= is exclusive")
    huge, _, _ = replay_window(records, limit=10 ** 9)
    check(len(huge) == 1000, "an absurd limit is clamped, never trusted")


def test_socket_replays_then_switches_then_tails():
    print("test_socket_replays_then_switches_then_tails")
    root = tmpdir("ws")
    store = build_store(root, records=6)
    session = WsSession(ReadModel(state={}, store=store), window=DEFAULT_REPLAY_EVENTS)
    hello = session.hello()
    check(hello["type"] == "hello" and hello["live"] is False and hello["mode"] == "replay",
          "the handshake declares replay mode and is itself not live")
    frames = session.replay()
    check(frames and all(f["live"] is False for f in frames),
          "every replayed frame carries live:false — sp-13 paints it once, no animation")
    check(all(f["cursor"] == store_mod.cursor_key(f["stream"], f["seq"]) for f in frames),
          "each frame carries its (stream, seq) cursor")
    switch = session.switch()
    check(switch["type"] == "mode" and switch["live"] is True,
          "one mode frame marks the replay->tail boundary")
    check(session.tick() == [], "nothing new means no frames")
    store.append(STREAM, kind="node", provenance="harness",
                 ref={"runId": RUN_ID, "key": "research/impl", "ordinal": 0},
                 data={"fresh": True})
    live = session.tick()
    check(len(live) == 1 and live[0]["live"] is True,
          "an appended record arrives as a live frame")
    check(live[0]["seq"] == frames[-1]["seq"] + 1, "and continues the seq, without a gap")
    # The table's last frame is the transport's: `test_server_core.py`'s
    # `test_the_idle_marker_is_sent_and_a_subscribe_is_answered_in_order` proves
    # the socket sends it. Here the *shape* is pinned, so the two cannot drift.
    check(_contract_frame_keys("tick") == {"type", "live", "ts"},
          "the contract's idle keepalive marker is a three-key frame")


def _contract_frame_keys(frame_type):
    """The keys the module docstring's wire-contract table shows on one frame.

    The table is normative — sp-13 restates it verbatim — so it is read here as
    data rather than trusted as prose.
    """
    doc = server_mod.__doc__
    start = doc.index('{"type":"%s"' % frame_type)
    end = doc.find('{"type":', start + 1)           # the next frame in the table
    if end == -1:                                   # the last one: to the blank line
        end = doc.index("\n\n", start)
    return set(re.findall(r'"([A-Za-z]+)":', doc[start:end]))


def test_the_load_older_anchors_are_on_the_frame_that_can_know_them():
    print("test_the_load_older_anchors_are_on_the_frame_that_can_know_them")
    # `HttpServer.stream` sends hello *before* it replays, and the window is
    # chosen per stream as the replay runs — so a hello carrying
    # `oldest`/`truncated` can only ever carry `{}`. sp-13 restates this
    # contract verbatim: it would read an empty `truncated`, conclude nothing
    # was cut, and never render the "load older" affordance R-55 requires,
    # while the real anchors sailed past on the mode frame. A missing key
    # fails loudly in development; an empty one is a silently wrong UI.
    root = tmpdir("anchors")
    store = build_store(root, records=6)             # the run stream: current, replayed whole
    session_stream = "session:622-10028"
    for n in range(50):                              # not current: capped by the window
        store.append(session_stream, kind="session", provenance="harness",
                     ref={"pid": 622, "procStart": "10028"}, data={"n": n})
    session = WsSession(ReadModel(state={}, store=store), window=5)

    hello = session.hello()
    check("oldest" not in hello and "truncated" not in hello,
          "hello carries no load-older anchor at all — it cannot know one yet")
    check(hello["cursors"] == {},
          "its cursors are the client-supplied position, empty on a fresh connect")

    frames = session.replay()
    switch = session.switch()
    capped = [f for f in frames if f["stream"] == session_stream]
    check(switch["truncated"] == {session_stream: True},
          "the mode frame declares the stream the window cut, and only that one")
    check(switch["oldest"][session_stream] == capped[0]["seq"],
          "with the real seq of the oldest record actually sent — where load-older starts")
    check(switch["cursors"][session_stream] == capped[-1]["seq"],
          "and the cursors the replay ended on, which is what a client resumes from")
    check(STREAM not in switch["truncated"],
          "the current run replayed whole, so nothing is claimed to be cut")

    # The drift this test exists to end: the normative table said `hello`.
    hello_doc, mode_doc = _contract_frame_keys("hello"), _contract_frame_keys("mode")
    check(not ({"oldest", "truncated"} & hello_doc),
          "the wire contract does not advertise the anchors on hello")
    check({"oldest", "truncated", "cursors"} <= mode_doc,
          "it advertises them on the mode frame — the first frame that can carry them")
    check(hello_doc <= set(hello) and mode_doc <= set(switch),
          "and every key the contract shows on a frame is a key the code puts there")


def test_the_current_run_is_the_newest_not_the_alphabetically_largest():
    print("test_the_current_run_is_the_newest_not_the_alphabetically_largest")
    # `store.streams()` ends in `sorted(found)` over a listdir, so its order is
    # alphabetical and run ids are `wf_<random hex>`. Taking its last element
    # replays some other run whole, truncates the one the operator is watching,
    # and publishes the wrong id as `currentRun` to sp-13.
    root = tmpdir("current")
    store = store_mod.Store(root)
    chronological = ["wf_829e6f58-b2f", "wf_b297177a-d11", "wf_455b348c-e17"]
    for index, run in enumerate(chronological):
        stream = f"run:{run}"
        store.append_many(stream, [
            {"kind": "node", "provenance": "harness",
             "ref": {"runId": run, "key": "research/impl", "ordinal": 0},
             "data": {"n": n}, "source": "ingest", "ts": None} for n in range(6)])
        moment = 1_700_000_000 + index
        os.utime(store.stream_path(stream), (moment, moment))
    newest = f"run:{chronological[-1]}"
    check(sorted(store.streams())[-1] != newest,
          "the fixture is the trap: the newest run is NOT the alphabetically last")

    session = WsSession(ReadModel(state={}, store=store), window=2)
    check(session.hello()["currentRun"] == newest,
          "the current run is the most recently written stream, by an observed fact")
    counts = {}
    for frame in session.replay():
        counts[frame["stream"]] = counts.get(frame["stream"], 0) + 1
    check(counts[newest] == 6,
          "R-55's consequence: the current run replays whole, all six records")
    check(all(counts[f"run:{run}"] == 2 for run in chronological[:-1]),
          "while every other stream is capped at the window — the right way round")


def test_from_is_applied_or_reported_never_silently_dropped():
    print("test_from_is_applied_or_reported_never_silently_dropped")
    root = tmpdir("from")
    store = build_store(root, records=6)         # a run stream and a session stream
    model = ReadModel(state={}, store=store)
    ambiguous = WsSession(model, from_seq=3)
    hello = ambiguous.hello()
    check(hello["from"] == 3 and hello["fromApplied"] is False,
          "?from= across more than one stream has no meaning (seq is per stream) and "
          "the handshake says so — after the 101 there is no status code left")
    selected = WsSession(model, from_seq=3, streams=[STREAM])
    check(selected.hello()["fromApplied"] is True, "paired with one stream it applies")
    check([f["seq"] for f in selected.replay()] == [4, 5, 6],
          "and it is exclusive, as R-55 specifies")

    # Three cases, and `fromApplied:false` alone told them apart from neither:
    # `?from=abc` (unparseable), `?from=3` against three streams (unusable), and
    # no `?from=` at all. Every other unusable parameter comes back raw.
    malformed = WsSession(model, from_rejected="abc", streams=[STREAM]).hello()
    check(malformed["from"] is None and malformed["fromRejected"] == "abc",
          "a ?from= that does not parse is echoed raw, not dropped in silence")
    check(ambiguous.hello()["fromRejected"] is None,
          "an unusable-but-valid ?from= is a different case: it is on `from`, not on the reject")
    check(WsSession(model).hello()["fromRejected"] is None
          and WsSession(model).hello()["from"] is None,
          "and no ?from= at all is a third — all three are distinguishable on the frame")


def test_one_malformed_cursor_costs_only_itself():
    print("test_one_malformed_cursor_costs_only_itself")
    # `parse_cursor_params` used to raise on the FIRST bad entry, and its only
    # caller — a handshake that has already returned 101 and has no status code
    # left — swallowed the raise and started from `{}`. A client that got two
    # of three cursors right therefore lost all three and was re-sent records it
    # already held: R-55's named failure ("reconnect mid-stream ⇒ no duplicate
    # events") reached by a typo, and the same silent-drop class `?from=` was
    # already fixed for.
    root = tmpdir("cursors")
    store = build_store(root, records=20)
    good = store_mod.cursor_key(STREAM, 10)
    accepted, rejected = parse_cursor_params(
        {"cursor": [good, "garbage", "run:wf_x#not-a-seq"]})
    check(accepted == {STREAM: 10},
          "the well-formed cursor is kept — parsing is per entry, not all-or-nothing")
    check(rejected == ["garbage", "run:wf_x#not-a-seq"],
          "and the raw rejects come back, both of them, in the order given")

    session = WsSession(ReadModel(state={}, store=store), cursors=accepted,
                        cursors_rejected=rejected)
    hello = session.hello()
    check(hello["cursorsRejected"] == rejected,
          "the handshake NAMES what it could not use — after the 101 that is the only refusal left")
    check(hello["cursors"] == {STREAM: 10} and hello["resumed"] is True,
          "while the good pair is the session's position")
    replayed = [f["seq"] for f in session.replay() if f["stream"] == STREAM]
    check(replayed == list(range(11, 21)),
          "so the resume still resumes: ten held records are not replayed over a typo")

    # The docstring used to promise a 400 no caller could ever produce.
    check("400" not in parse_cursor_params.__doc__,
          "and the docstring no longer describes a status code this path cannot send")


def test_a_failed_stream_selector_serves_nothing_not_everything():
    print("test_a_failed_stream_selector_serves_nothing_not_everything")
    # `[s for s in asked if _is_stream(s)] or None` turned "you asked for one
    # target and none of it parsed" into "serve every target" — GD-12's
    # never-fall-back-to-another-target rule broken by the query parser, which
    # is exactly the monitor's silent STATE_DIR fallback in a new place.
    root = tmpdir("selector")
    store = build_store(root, records=4)
    model = ReadModel(state={}, store=store)
    check(len(WsSession(model, streams=None).streams()) == 2,
          "no selector at all still serves the whole store — the fixture has two streams")
    empty = WsSession(model, streams=[], streams_rejected=["run:nonexistent#bad"])
    check(empty.streams() == [] and empty.replay() == [],
          "a selector that matched nothing serves NOTHING: a superset is the wrong answer")
    check(empty.hello()["streamsRejected"] == ["run:nonexistent#bad"],
          "and the handshake names the selector it threw away")
    check(WsSession(model, streams=[STREAM]).streams() == [STREAM],
          "a selector that matched serves exactly it")


def test_a_stream_born_after_the_switch_is_backfilled_not_animated():
    print("test_a_stream_born_after_the_switch_is_backfilled_not_animated")
    root = tmpdir("late")
    store = build_store(root, records=2)
    session = WsSession(ReadModel(state={}, store=store))
    session.replay()
    session.switch()
    session.tick()
    late_run = "wf_late0001-aaa"
    late = f"run:{late_run}"
    store.append_many(late, [
        {"kind": "node", "provenance": "harness",
         "ref": {"runId": late_run, "key": "k", "ordinal": 0},
         "data": {"n": n}, "source": "ingest", "ts": None} for n in range(3)])
    first = [f for f in session.tick() if f.get("stream") == late]
    events = [f for f in first if f["type"] == "event"]
    check(len(events) == 3 and all(f["live"] is False for f in events),
          "a stream that appears after the mode switch replays its backlog as "
          "backfill — sp-13 keys off the live flag alone and cannot tell otherwise")
    check([f["type"] for f in first] == ["anchors", "event", "event", "event"],
          "led by the anchors frame that says where that backfill starts")
    store.append(late, kind="node", provenance="harness",
                 ref={"runId": late_run, "key": "k", "ordinal": 0}, data={"n": 3})
    after = [f for f in session.tick() if f.get("stream") == late]
    check(len(after) == 1 and after[0]["live"] is True,
          "and only what arrives after that is live")


def test_a_late_streams_truncation_is_published_not_just_recorded():
    print("test_a_late_streams_truncation_is_published_not_just_recorded")
    # `switch()` is the only emitter of oldest/truncated and it runs once,
    # before the tail. A stream born *after* it — an ingest pass backfilling a
    # newly discovered transcript, which is the normal way a stream is born —
    # had its window computed and its anchors written into session state that
    # nothing would ever publish: 55 of 60 records cut off the wire, and a page
    # rendering the stream told nothing, with no seq to call
    # `/api/events?stream=&before=` with. The `hello.truncated` failure,
    # reached through the late-stream door.
    root = tmpdir("late-cut")
    store = build_store(root, records=2)
    session = WsSession(ReadModel(state={}, store=store), window=5)
    session.replay()
    switch = session.switch()
    session.tick()
    born = "session:9999-123456"
    store.append_many(born, [
        {"kind": "session", "provenance": "harness",
         "ref": {"pid": 9999, "procStart": "123456"},
         "data": {"n": n}, "source": "ingest", "ts": None} for n in range(60)])
    frames = [f for f in session.tick() if f.get("stream") == born]
    events = [f for f in frames if f["type"] == "event"]
    anchors = [f for f in frames if f["type"] == "anchors"]
    check(len(events) == 5 and all(f["live"] is False for f in events),
          "the window still caps the backfill at five frames, painted once")
    check(events[0]["seq"] == 56, "so 55 of the 60 records never reach this client")
    check(len(anchors) == 1 and anchors[0]["oldest"] == 56 and anchors[0]["truncated"] is True,
          "and an anchors frame says so, naming the seq load-older must start from")
    check(frames[0] is anchors[0],
          "before the frames it describes — the edge of the window arrives with it")
    check(born not in switch["truncated"] and born not in switch["oldest"],
          "the mode frame could not have carried it: the stream did not exist yet")
    check(_contract_frame_keys("anchors") <= set(anchors[0]),
          "and every key the normative table shows on an anchors frame is really on it")


def test_a_selector_for_a_run_that_has_not_started_is_labelled_unobserved():
    print("test_a_selector_for_a_run_that_has_not_started_is_labelled_unobserved")
    # A `?stream=` is checked for grammar only, so a typo'd run id is served —
    # correctly, because a client may connect before its run starts. What is not
    # correct is `currentRun` naming it: that is the id sp-13 labels the page
    # header with, and `/api/events` answers 404 for exactly this input rather
    # than "publish a made-up fact about a made-up run" (its own words).
    root = tmpdir("ghost")
    store = build_store(root, records=4)
    model = ReadModel(state={}, store=store)
    ghost = "run:wf_doesnotexist"
    session = WsSession(model, streams=[ghost])
    hello = session.hello()
    check(hello["streams"] == [ghost] and hello["streamsRejected"] == [],
          "a well-formed selector for an unwritten stream is served, not refused — "
          "that is how a page waits for a run to start")
    check(hello["streamsUnobserved"] == [ghost],
          "but it is named as unobserved instead of being presented as an observation")
    check(hello["currentRun"] is None,
          "and it is never the current run: the header would name a run nobody has seen")
    check(session.replay() == [], "nothing is replayed, because there is nothing yet")

    both = WsSession(model, streams=[ghost, STREAM]).hello()
    check(both["currentRun"] == STREAM and both["streamsUnobserved"] == [ghost],
          "beside a real run, the observed one is the current run and only the ghost is named")
    check(WsSession(model).hello()["streamsUnobserved"] == [],
          "with no selector every stream served is one the store has")


def test_one_tick_cannot_write_an_unbounded_burst():
    print("test_one_tick_cannot_write_an_unbounded_burst")
    # `replay()` was capped from the start; the tail was not. Everything
    # `store.follow` returned in one 250 ms tick became one frame each, written
    # and drained back-to-back on every connected socket before the loop could
    # sleep — and a bulk append is not exotic: an ingest catching up after a
    # restart is exactly how a burst is born. GD-30 asks for a bounded queue.
    root = tmpdir("burst")
    store = store_mod.Store(root)
    run = "wf_burst001-aa"
    stream = f"run:{run}"

    def specs(count, start=0):
        return [{"kind": "node", "provenance": "harness",
                 "ref": {"runId": run, "key": "k", "ordinal": 0},
                 "data": {"n": start + n}, "source": "ingest", "ts": None}
                for n in range(count)]

    store.append_many(stream, specs(3))
    session = WsSession(ReadModel(state={}, store=store), window=10)
    session.replay()
    session.switch()
    session.tick()
    store.append_many(stream, specs(MAX_TICK_EVENTS + 7, start=3))

    first = [f for f in session.tick() if f["type"] == "event"]
    check(len(first) == MAX_TICK_EVENTS,
          "one tick emits at most MAX_TICK_EVENTS frames for a stream, not all 5007")
    check(session.capped == 1, "and it records that it capped rather than hiding it")
    check(session.cursors[stream] == first[-1]["seq"],
          "the cursor stops exactly where the cap fell — it names what was sent")
    second = [f for f in session.tick() if f["type"] == "event"]
    check(len(second) == 7, "the carried remainder arrives on the next tick")
    seqs = [f["seq"] for f in first] + [f["seq"] for f in second]
    check(seqs == list(range(4, 4 + MAX_TICK_EVENTS + 7)),
          "contiguous across the boundary: no gap, and no record delivered twice")
    check(session.tick() == [], "and a drained carry does not replay itself")


def test_reconnect_resumes_without_duplicates():
    print("test_reconnect_resumes_without_duplicates")
    root = tmpdir("resume")
    store = build_store(root, records=20)
    model = ReadModel(state={}, store=store)
    full = WsSession(model, window=1000)
    every = full.replay()
    run_frames = [f for f in every if f["stream"] == STREAM]

    cut = run_frames[9]["seq"]
    resumed = WsSession(model, cursors={STREAM: cut})
    again = [f for f in resumed.replay() if f["stream"] == STREAM]
    check([f["seq"] for f in again] == list(range(cut + 1, 21)),
          "a reconnect replays exactly the records after the client's (stream, seq)")
    check(not ({f["seq"] for f in again} & {f["seq"] for f in run_frames[:10]}),
          "no record is delivered twice")
    check(resumed.hello()["resumed"] is True, "the handshake says it resumed")
    seen = [f["seq"] for f in run_frames[:10]] + [f["seq"] for f in again]
    check(seen == [f["seq"] for f in run_frames],
          "the union of the two sessions equals a full replay — no gap either")


def test_a_resume_deeper_than_the_cap_is_declared_not_silently_gapped():
    print("test_a_resume_deeper_than_the_cap_is_declared_not_silently_gapped")
    # "No duplicates, no gap" is true only up to MAX_REPLAY_EVENTS: a cursor
    # 6000 records back is served the newest 5000 and the shortfall is real.
    # The behaviour is right — an unbounded replay is how a page dies — but the
    # gap must be *declared*, or the client believes it holds a contiguous
    # stream and never fetches the middle.
    root = tmpdir("deep")
    store = store_mod.Store(root)
    deep_run = "wf_deep0001-aa"
    deep = f"run:{deep_run}"
    store.append_many(deep, [
        {"kind": "node", "provenance": "harness",
         "ref": {"runId": deep_run, "key": "k", "ordinal": 0},
         "data": {"n": n}, "source": "ingest", "ts": None}
        for n in range(MAX_REPLAY_EVENTS + 1000)])
    session = WsSession(ReadModel(state={}, store=store), cursors={deep: 1})
    frames = [f for f in session.replay() if f["stream"] == deep]
    switch = session.switch()
    check(len(frames) == MAX_REPLAY_EVENTS,
          "a resume is capped at MAX_REPLAY_EVENTS — 'explicit' bounds where, never how big")
    check(frames[0]["seq"] == 1001 and frames[-1]["seq"] == MAX_REPLAY_EVENTS + 1000,
          "and it is the NEWEST window that is served, not the oldest")
    check(switch["truncated"].get(deep) is True and switch["oldest"][deep] == 1001,
          "the shortfall is declared with the seq the page must load older from")
    check("MAX_REPLAY_EVENTS" in server_mod.__doc__.split("**resume**")[1].split("*")[0],
          "and the contract's resume clause says so rather than promising no gap at all")


def test_a_held_token_frame_holds_the_cursor_behind_it():
    print("test_a_held_token_frame_holds_the_cursor_behind_it")
    # `tick` advanced the cursor before asking the coalescer, so the published
    # position could name a seq that was never sent. The hold lives in this
    # session's coalescer and dies with the socket: a client that adopted that
    # cursor and reconnected inside the ≤1 s window skipped the record forever
    # — and for a finished agent's last token record that is a stale count on
    # the page with nothing in the UI able to notice.
    root = tmpdir("held")
    store = store_mod.Store(root)
    tok_run = "wf_tok00000-aaa"
    stream = f"run:{tok_run}"

    def emit(total):
        store.append(stream, kind="token", provenance="harness",
                     ref={"agentId": AGENT_A},
                     data={"tokens": {"in": total, "out": 1, "cached": 0, "cache_write": 0}})

    model = ReadModel(state={}, store=store)
    session = WsSession(model, coalesce=1.0)
    session.replay()
    session.switch()
    emit(10)
    session.tick(now=100.0)
    emit(20)
    session.tick(now=100.1)
    check(session.cursors[stream] == 2,
          "two token frames have gone out, so the cursor is on the second")

    emit(30)
    emit(40)
    inside = session.tick(now=100.2)
    check(inside == [] and session.coalescer.pending == 1,
          "the next two are inside the ≥1 s window: one is held, the other supersedes it")
    check(session.cursors[stream] == 2,
          "and the published cursor stays behind the held record — it names only what was sent")

    resumed = WsSession(model, cursors=dict(session.cursors))
    seqs = [f["seq"] for f in resumed.replay() if f["stream"] == stream]
    check(seqs == [3, 4],
          "so a client that adopts it and reconnects is still sent the held record, "
          "instead of skipping a count that only this dead session ever held")

    released = session.tick(now=101.5)
    check([f["seq"] for f in released] == [4] and
          released[0]["record"]["data"]["tokens"]["in"] == 40,
          "after the window the LAST absolute value is released")
    check(session.cursors[stream] == 4,
          "and only then does the cursor move — onto 4, not the superseded 3, because "
          "an absolute record is the whole count and losing its predecessor loses nothing")


def test_tokens_coalesce_and_stay_absolute():
    print("test_tokens_coalesce_and_stay_absolute")
    coalescer = TokenCoalescer(window=1.0)
    first = {"seq": 1, "kind": "token", "ref": {"agentId": AGENT_A},
             "data": {"tokens": {"in": 10, "out": 1, "cached": 0, "cache_write": 0}}}
    second = dict(first, seq=2, data={"tokens": {"in": 20, "out": 2, "cached": 0,
                                                 "cache_write": 0}})
    third = dict(first, seq=3, data={"tokens": {"in": 30, "out": 3, "cached": 0,
                                                "cache_write": 0}})
    check(coalescer.offer(STREAM, first, 100.0) is first, "the first record goes out at once")
    check(coalescer.offer(STREAM, second, 100.2) is None, "a second inside the window is held")
    check(coalescer.offer(STREAM, third, 100.4) is None, "and superseded by the third")
    due = coalescer.due(101.5)
    check([r["seq"] for _, r in due] == [3],
          "one frame is released after the window and it is the LAST absolute value")
    check(due[0][1]["data"]["tokens"]["in"] == 30,
          "30 — the newest absolute count, never a sum of deltas (GD-25/R-55)")
    check(coalescer.coalesced == 1 and coalescer.pending == 0,
          "the superseded record is counted, not silently lost")

    other = {"seq": 4, "kind": "token", "ref": {"agentId": AGENT_B}, "data": {}}
    check(coalescer.offer(STREAM, other, 100.5) is other,
          "coalescing is per (stream, ref): another agent is not throttled by this one")

    # `pending_floor` defaulted a missing seq to 0, so the floor was -1 and
    # `_advance`'s `seq > cursor` could never hold: one seq-less line from a
    # foreign writer froze the stream's published cursor until the window
    # elapsed. Store-written records always carry a seq — which is what makes
    # this defensive, and exactly why it must not be the arm that stops a cursor.
    stray = TokenCoalescer(window=1.0)
    stray.offer(STREAM, {"seq": 7, "kind": "token", "ref": {"agentId": AGENT_B},
                         "data": {}}, 200.0)
    seqless = {"kind": "token", "ref": {"agentId": AGENT_B}, "data": {}}
    check(stray.offer(STREAM, seqless, 200.1) is None,
          "a second record inside the window is held, seq or no seq")
    check(stray.pending_floor(STREAM) is None,
          "a held record with no usable seq yields no floor at all — never 0")
    session = WsSession(ReadModel(state={}, store=None))
    session.coalescer = stray
    session._advance(STREAM, 9)
    check(session.cursors.get(STREAM) == 9,
          "so the cursor still moves for the frames that did go out")


def test_socket_tick_is_incremental_and_survives_a_rewrite():
    print("test_socket_tick_is_incremental_and_survives_a_rewrite")
    root = tmpdir("tick")
    store = build_store(root, records=4)
    session = WsSession(ReadModel(state={}, store=store))
    session.replay()
    session.switch()
    session.tick()
    path = store.stream_path(STREAM)
    lines = open(path, "rb").read()
    # An in-place rewrite (the shrink SD-10 names): the tailer re-reads from
    # zero and the cursor is what stops a live client seeing it all twice.
    with open(path, "wb") as fh:
        fh.write(lines)
    frames = session.tick()
    check(frames == [], "a rewrite that changed nothing replays nothing to a live client")
    store.append(STREAM, kind="node", provenance="harness",
                 ref={"runId": RUN_ID, "key": "research/impl", "ordinal": 0}, data={})
    check(len(session.tick()) == 1, "and the next real append still arrives exactly once")


def test_subscribe_resumes_and_never_acks_a_position_it_did_not_send():
    print("test_subscribe_resumes_and_never_acks_a_position_it_did_not_send")
    # The contract makes `subscribe` a co-equal arm of resume ("...or as a
    # {"type":"subscribe","cursors":{...}} message — and gets exactly the
    # records after them"). It used to `cursors.update(accepted)` and answer
    # `{"accepted": …}`: the tail reads `follow` from a checkpoint already at
    # EOF, so a rewound cursor replayed NOTHING while the ack said it applied,
    # and the session's published position was left behind records it had
    # already delivered — so the next reconnect re-sent them as duplicates,
    # R-55's named failure reached through the API that exists to prevent it.
    root = tmpdir("sub")
    store = build_store(root, records=10)
    model = ReadModel(state={}, store=store)
    session = WsSession(model, window=1000)
    session.replay()
    session.switch()
    session.tick()
    check(session.cursors[STREAM] == 10, "ten records have been delivered on this socket")

    frames = session.subscribe({"type": "subscribe", "cursors": {STREAM: 3}})
    ack = frames[-1]
    events = [f for f in frames if f["type"] == "event"]
    check([f["seq"] for f in events] == [4, 5, 6, 7, 8, 9, 10],
          "a rewound cursor re-delivers exactly the range it names — the documented "
          "mechanism does the documented thing")
    check(all(f["live"] is False for f in events),
          "as backfill: a re-send is not new activity and sp-13 must not animate it")
    check([f["type"] for f in frames][0] == "anchors" and events[0]["seq"] == 4,
          "with its own anchors frame, because `mode` is long gone and cannot be re-sent")
    check(ack["type"] == "subscribed",
          "and the ack comes LAST — a cursor is never announced before the records it names")
    check(ack["accepted"] == {STREAM: 3} and ack["backfilled"] == {STREAM: 7},
          "the ack says what it accepted and how much it re-sent")
    check(ack["cursors"][STREAM] == 10 and session.cursors[STREAM] == 10,
          "and the position ends where it began: everything the ack names has been sent")
    check(_contract_frame_keys("subscribed") <= set(ack),
          "every key the normative table shows on the ack is a key the code puts there")

    ahead = session.subscribe({"type": "subscribe", "cursors": {STREAM: 99}})
    refused = ahead[-1]
    check(len(ahead) == 1 and refused["accepted"] == {},
          "a cursor AHEAD of this socket's position replays nothing and is not adopted")
    check(refused["rejected"][0]["cursor"] == store_mod.cursor_key(STREAM, 99)
          and "ahead" in refused["rejected"][0]["reason"]
          and "10" in refused["rejected"][0]["reason"],
          "it is refused by name, with the position the socket really holds")
    check(session.cursors[STREAM] == 10 and refused["cursors"][STREAM] == 10,
          "because an ack that names a seq the socket never sent talks the client "
          "into skipping records for good")

    junk = session.subscribe({"type": "subscribe",
                              "cursors": {"not a stream!": 1, "run:x": -1, "run:y": True,
                                          "run:z": 10 ** 20, "x" * 400: 1}})
    check(junk[-1]["accepted"] == {} and len(junk[-1]["rejected"]) == 5,
          "malformed pairs are refused one by one, each with a reason — never fatal, "
          "never silent")
    check(len(junk) == 1, "and nothing is replayed for a pair that was not accepted")
    check(all(len(r["cursor"]) <= server_mod.MAX_REJECT_ECHO for r in junk[-1]["rejected"]),
          "the echo of an unusable cursor is truncated: a socket message has no length "
          "limit of its own, and naming what we could not use may not mean mirroring "
          "a megabyte back")
    check(any("seq" in r["reason"] for r in junk[-1]["rejected"]),
          "a seq too large to spell as a 12-digit cursor is one of them — accepting it "
          "would create a position no client could ever hand back")

    scoped = WsSession(model, streams=[STREAM])
    other = scoped.subscribe({"type": "subscribe", "cursors": {"session:622-10028": 0}})
    check(other[-1]["accepted"] == {}
          and "selection" in other[-1]["rejected"][0]["reason"],
          "a pair outside the socket's ?stream= selection is refused too: it would be "
          "backfilled once and then never tailed")

    store.append(STREAM, kind="node", provenance="harness",
                 ref={"runId": RUN_ID, "key": "research/impl", "ordinal": 0}, data={})
    tail = [f["seq"] for f in session.tick() if f["stream"] == STREAM]
    check(tail == [11],
          "and the tail continues from where it was: three subscribes did not corrupt "
          "the position, and nothing arrives twice")

    fresh = WsSession(model, window=1000)
    adopted = fresh.subscribe({"type": "subscribe", "cursors": {STREAM: 8}})
    check([f["seq"] for f in adopted if f["type"] == "event"] == [9, 10, 11],
          "on a socket with no position for the stream, the pair is the client's own "
          "?cursor= arriving over the wire: it is adopted and served from")
    check(fresh.cursors[STREAM] == 11, "and becomes the position")


def test_health_reports_tailers_and_the_mirror_block():
    print("test_health_reports_tailers_and_the_mirror_block")
    root = tmpdir("health")
    store = build_store(root, records=2)

    class FakeMirror:
        def health(self):
            return {"state": "degraded", "lastError": "no route to host", "notes": [],
                    "queued": 3, "dropped": 1, "tolerated_dups": 0, "lease": {},
                    "backend": "memory", "db": "touch_abc", "counters": {}}

    model = ReadModel(state=build_state(), store=store, mirror=FakeMirror(),
                      tailers={"journal": {"path": "/x/journal.jsonl", "alive": False,
                                           "missing": True, "parseFailures": 2}},
                      reduce_ttl=0)
    api = Api(model, auth=Auth("t0ken"))
    payload = body(api.get("/health"))
    check(payload["mirror"]["state"] == "degraded" and payload["mirror"]["dropped"] == 1,
          "the R-45 mirror block is served verbatim")
    check(payload["tailers"][0]["missing"] is True,
          "a tailer whose target is gone is visible (AUDIT-15), not silently polling")
    check("path" not in payload["tailers"][0] and payload["tailers"][0]["target"],
          "named by a stable hash, never by its path — /health takes no token")
    check("parseFailures" in payload, "parse failures are reported")
    check(payload["collections"]["records"] == 5,
          "collection sizes come from the same memory model the API serves")

    absent = Api(ReadModel(state={}, store=None), auth=Auth("t0ken"))
    check(body(absent.get("/health"))["mirror"]["state"] == "absent",
          "no mirror configured reports absent — never an error (GD-22)")


def main():
    try:
        for t in (test_one_validator_400s_malformed_and_404s_unknown,
                  test_sessions_lists_both_classes,
                  test_timeline_pages_without_duplicates,
                  test_the_timeline_cursor_is_the_whole_sort_key_not_a_prefix_of_it,
                  test_timeline_omits_bodies_until_asked,
                  test_a_bare_after_is_not_a_cursor,
                  test_an_unobserved_run_or_stream_is_404_not_an_empty_list,
                  test_a_zero_limit_cannot_produce_an_endless_page,
                  test_a_valueless_flag_is_the_hand_typed_form_and_means_true,
                  test_events_pages_forwards_and_backwards,
                  test_events_of_a_historical_session_is_not_a_fallback,
                  test_run_graph_serves_the_reducers_output,
                  test_run_node_resolves_spawn_without_reading_the_file,
                  test_toolresult_rechecks_containment_at_serve_time,
                  test_tasks_lists_legacy_folders_with_their_derivation,
                  test_query_falls_back_to_memory_and_says_so,
                  test_replay_window_is_bounded_and_publishes_its_edge,
                  test_socket_replays_then_switches_then_tails,
                  test_the_load_older_anchors_are_on_the_frame_that_can_know_them,
                  test_the_current_run_is_the_newest_not_the_alphabetically_largest,
                  test_from_is_applied_or_reported_never_silently_dropped,
                  test_one_malformed_cursor_costs_only_itself,
                  test_a_failed_stream_selector_serves_nothing_not_everything,
                  test_a_stream_born_after_the_switch_is_backfilled_not_animated,
                  test_a_late_streams_truncation_is_published_not_just_recorded,
                  test_a_selector_for_a_run_that_has_not_started_is_labelled_unobserved,
                  test_one_tick_cannot_write_an_unbounded_burst,
                  test_reconnect_resumes_without_duplicates,
                  test_a_resume_deeper_than_the_cap_is_declared_not_silently_gapped,
                  test_a_held_token_frame_holds_the_cursor_behind_it,
                  test_tokens_coalesce_and_stay_absolute,
                  test_socket_tick_is_incremental_and_survives_a_rewrite,
                  test_subscribe_resumes_and_never_acks_a_position_it_did_not_send,
                  test_health_reports_tailers_and_the_mirror_block):
            t()
    finally:
        for path in TMPDIRS:
            shutil.rmtree(path, ignore_errors=True)
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all read API + wire tests passed")


if __name__ == "__main__":
    main()
