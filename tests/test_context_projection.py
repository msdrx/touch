#!/usr/bin/env python3
"""Stdlib-only tests for 8932's context-occupancy PROJECTION (LC-10/LC-11).
Run as `python3 test_context_projection.py`; exits non-zero on failure. No
pytest, no runner — the file is executable and glob-registered by `run_all.sh`.

8932 measures nothing. `decision_watcher.py` is the sole producer of
`agent.ctx` (GD-LC-5); the aggregator ADOPTS the block off `events.jsonl`,
carries it through `legacy.py`'s reduction, names it in `server.py`'s explicit
field lists and renders the absolute number in `app.js`. Everything asserted
here is a property of that consumer chain, and the chain has exactly two ways
to fail silently — both of which this file exists to catch:

* **the explicit-field-list trap.** `server.py`'s node payload and its
  seven-field token payload are hand-written dicts. A perfect reduction that is
  never named there reaches the browser as nothing at all, and a blank 8932
  card beside a populated 8931 one is R-58's cousin: the number exists, the
  page says otherwise, and nothing anywhere reports a fault.
* **the absent-vs-zero trap.** Occupancy is a LEVEL, not a total. `0` does not
  mean "empty window" — a fresh window already holds 21k–45k tokens before its
  first word (measured over 610 agents), so a rendered `0` is a fabrication of
  the exact class this repo calls R-58. Unknown is spelled by the key being
  ABSENT, at every hop: `None` out of the reduction, `null` on the wire, and
  nothing at all on the card.

The arms are LC-11's five, plus the fold rule the first one is really about:

1. last-READING-wins, never a sum — a rising-then-falling sequence ends on the
   FALLING value (occupancy is non-monotonic; a compaction lowers it, and any
   max/sum/clamp here is a lie about a real event);
2. the server payloads carry the field at all;
3. replay determinism — reduced twice, read in two chunks, and with every
   mtime altered, byte-identical each time;
4. no `ctx` key ⇒ absent, not zero; a later line with one ⇒ present; and the
   later ABSENCE of one does not erase the reading already held;
5. `/health.context` — `absent` with no reading, `events` with one, and the
   serialised block carries no agent id, no path and no session id.

Plus the two guards that are not in that list and would otherwise be untested:
a malformed block is dropped WHOLE and counted (half a reading is a made-up
one), and `app.js` renders the number with no window constant, no percentage
and no `|| 0` coercion anywhere near it.
"""

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The canonical trees are named through `tests/_roots.py`, never by a literal
# under REPO: GD-U1 moves them and that is the single flip point. No `_roots`
# change is needed for this file — it reads the trees the anchors already name.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))

from aggregator import legacy as lg                            # noqa: E402
from aggregator import server as server_mod                    # noqa: E402
from aggregator.server import Api, Auth, ReadModel             # noqa: E402

JS_PATH = SRC / "touch-visual" / "app.js"

#: A 17-hex agentId, the width the widened watcher writes.
AGENT_A = "a2fc883c96ff7b837"
AGENT_B = "dd469822c0f1e2a34"

failures = []
TMPDIRS = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def tmpdir(name):
    path = tempfile.mkdtemp(prefix=f"touch-ctx-{name}-")
    TMPDIRS.append(path)
    return path


def line(**fields):
    return json.dumps(fields) + "\n"


def ctx(used, at, **extra):
    """One `agent.ctx` block in GD-LC-4's shape."""
    block = {"used": used, "at": at}
    block.update(extra)
    return block


def tick(agent_id, seconds, *, tokens_in, ctx_block=None, plan="sp-a",
         stage="implement", state="running", quiet=True):
    """One watcher token tick — the line `agent.ctx` rides (GD-LC-5).

    Deliberately built as a token-STAGE line: zero new event kinds and zero new
    event lines is the standing invariant, so if the reading ever needs a line
    of its own to be testable here, the feature has already been broken.
    """
    agent = {"id": agent_id, "label": f"{stage} #1",
             "tokens": {"in": tokens_in, "out": 0, "cached": 0, "cache_write": 0}}
    if ctx_block is not None:
        agent["ctx"] = ctx_block
    return line(ts=f"2026-07-31T21:{seconds:02d}:00.000Z", plan=plan, stage="tokens",
                state=state, detail="", w="watcher", quiet=quiet, agent=agent)


def result(agent_id, seconds, *, ctx_block=None, plan="sp-a", stage="implement",
           state="done"):
    """The agent's terminal row — a NODE event, which is what a card renders."""
    agent = {"id": agent_id, "label": f"{stage} #1"}
    if ctx_block is not None:
        agent["ctx"] = ctx_block
    return line(ts=f"2026-07-31T21:{seconds:02d}:00.000Z", plan=plan, stage=stage,
                state=state, detail="done", w="watcher", agent=agent)


def reduce_text(text, task="ctx-task"):
    root = tmpdir("reduce")
    folder = os.path.join(root, task)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, lg.EVENTS_FILE), "w", encoding="utf-8") as handle:
        handle.write(text)
    return lg.reduce_task(lg.task_folder(folder))


def node_of(reduction, agent_id):
    for node in reduction.nodes:
        if agent_id in (node.agent_ids or ()) or node.agent_id == agent_id:
            return node
    return None


def records_of(reduction, agent_id):
    return [r for r in reduction.tokens if r.agent_id == agent_id]


def make_api(tasks_root):
    model = ReadModel(state={}, store=None, tasks_root=tasks_root, reduce_ttl=0)
    return Api(model, auth=Auth("t0ken")), model


def get(api, path):
    return api.get(path, {"authorization": "Bearer t0ken"})


def payload_of(response):
    return json.loads(response.body.decode("utf-8"))


# --- arm 1: the fold rule -------------------------------------------------
def test_the_fold_is_last_reading_wins_and_never_a_sum():
    print("test_the_fold_is_last_reading_wins_and_never_a_sum")
    # Rising, then FALLING — the shape a compaction produces. The tempting
    # implementations (sum the readings, or keep the max) both survive a
    # monotonically rising stream and are both wrong forever after the first
    # compaction, which is why the specimen falls.
    stream = (
        tick(AGENT_A, 1, tokens_in=100, ctx_block=ctx(120000, "2026-07-31T21:01:00.000Z")) +
        tick(AGENT_A, 2, tokens_in=200, ctx_block=ctx(180000, "2026-07-31T21:02:00.000Z")) +
        tick(AGENT_A, 3, tokens_in=300,
             ctx_block=ctx(41000, "2026-07-31T21:03:00.000Z", src="compact")) +
        result(AGENT_A, 4, ctx_block=ctx(41000, "2026-07-31T21:03:00.000Z", src="compact"))
    )
    reduction = reduce_text(stream)
    node = node_of(reduction, AGENT_A)
    check(node is not None and node.ctx is not None, "the node carries a reading")
    check(node.ctx["used"] == 41000,
          "the node's reading is the LAST one (41000), not the largest (180000) "
          "— occupancy is non-monotonic and a compaction legitimately lowers it")
    check(node.ctx["used"] != 120000 + 180000 + 41000,
          "…and emphatically not a sum: two readings of one window are one "
          "quantity measured twice, not two quantities")
    check(node.ctx.get("src") == "compact",
          "the provenance of a non-usage-row reading survives the fold")

    records = records_of(reduction, AGENT_A)
    latest = [r for r in records if r.ctx][-1]
    check(latest.ctx["used"] == 41000,
          "the token records fold the same way — last reading wins per agent")
    check(all(r.ctx is None or r.ctx["used"] in (120000, 180000, 41000) for r in records),
          "…and every surviving block is one the stream actually stated, "
          "never an average, a max or a running total")


def test_a_later_line_without_a_reading_does_not_erase_the_one_held():
    print("test_a_later_line_without_a_reading_does_not_erase_the_one_held")
    # GD-LC-12: a pruned or unreadable transcript means the PREVIOUS reading
    # stands, visibly aged by its own `at`. It does not mean the window emptied,
    # so the absence of a block on one tick may not zero or clear the card. This
    # is byte-for-byte what `monitor_server.py`'s `Fold._agent` does with the
    # same key, and the two folds must not diverge.
    stream = (
        tick(AGENT_A, 1, tokens_in=100, ctx_block=ctx(90000, "2026-07-31T21:01:00.000Z")) +
        tick(AGENT_A, 2, tokens_in=200) +
        result(AGENT_A, 3)
    )
    reduction = reduce_text(stream)
    node = node_of(reduction, AGENT_A)
    check(node.ctx is not None and node.ctx["used"] == 90000,
          "the held reading survives a later line that carries none")
    check(node.ctx["at"] == "2026-07-31T21:01:00.000Z",
          "…still stamped with its OWN source instant, never re-stamped — that "
          "stamp is the only thing that makes the staleness visible")


def test_the_reading_is_whole_object_replace_with_no_merge():
    print("test_the_reading_is_whole_object_replace_with_no_merge")
    # A partial merge is the subtle trap: it keeps a stale `cap` alive across a
    # model switch, and the card then draws a ratio against a window that model
    # never had.
    stream = (
        tick(AGENT_A, 1, tokens_in=100,
             ctx_block=ctx(90000, "2026-07-31T21:01:00.000Z",
                           model="claude-opus-5", cap=1000000, peak=90000)) +
        tick(AGENT_A, 2, tokens_in=200,
             ctx_block=ctx(95000, "2026-07-31T21:02:00.000Z", model="other-model")) +
        result(AGENT_A, 3)
    )
    node = node_of(reduce_text(stream), AGENT_A)
    check(node.ctx["used"] == 95000 and node.ctx.get("model") == "other-model",
          "the newest block wins whole")
    check("cap" not in node.ctx,
          "…and the previous block's `cap` does NOT survive into it (no merge): "
          "an undeclared window is unknown, never inherited")
    check("peak" not in node.ctx, "…nor its `peak`")


# --- arm 4: absent is absent ---------------------------------------------
def test_no_reading_is_absent_not_zero():
    print("test_no_reading_is_absent_not_zero")
    stream = (tick(AGENT_A, 1, tokens_in=100) + result(AGENT_A, 2))
    reduction = reduce_text(stream)
    node = node_of(reduction, AGENT_A)
    check(node is not None, "the agent still has a node — no reading is not no agent")
    check(node.ctx is None,
          "no `agent.ctx` anywhere in the stream ⇒ the field is None, NOT 0 — a "
          "529-killed agent that never billed a turn has an unknown occupancy, "
          "not an empty window")
    check(all(r.ctx is None for r in records_of(reduction, AGENT_A)),
          "…and no token record invents one either")
    check(reduction.stats["context_readings"] == 0
          and reduction.stats["context_agents"] == 0
          and reduction.stats["context_agents_unresolved"] == 1,
          "the counts say so out loud: one agent observed, none resolved")

    later = stream + tick(AGENT_B, 3, tokens_in=10,
                          ctx_block=ctx(70000, "2026-07-31T21:03:00.000Z"),
                          plan="sp-b", stage="review")
    reduction = reduce_text(later)
    check(node_of(reduction, AGENT_A).ctx is None,
          "one agent's reading never leaks onto another's node")
    check(reduction.stats["context_agents"] == 1
          and reduction.stats["context_agents_unresolved"] == 1,
          "…and the two are counted apart")


def test_a_zeroed_or_malformed_block_is_dropped_whole_and_counted():
    print("test_a_zeroed_or_malformed_block_is_dropped_whole_and_counted")
    # Every one of these is a block a well-meaning writer might emit. All are
    # refused: `0` is the lie, and a block missing a required field is not a
    # partial reading — it is no reading. Repairing one would manufacture
    # exactly the number the feature refuses to manufacture.
    bad = (
        ctx(0, "2026-07-31T21:01:00.000Z"),                       # the lie itself
        ctx(-1, "2026-07-31T21:01:00.000Z"),
        {"used": 90000},                                          # no `at`
        {"at": "2026-07-31T21:01:00.000Z"},                       # no `used`
        {"used": "90000", "at": "2026-07-31T21:01:00.000Z"},      # a string
        {"used": True, "at": "2026-07-31T21:01:00.000Z"},         # a bool is not an int
        {"used": 90000, "at": ""},
    )
    for index, block in enumerate(bad):
        reduction = reduce_text(tick(AGENT_A, 1, tokens_in=100, ctx_block=block)
                                + result(AGENT_A, 2))
        node = node_of(reduction, AGENT_A)
        check(node.ctx is None, f"malformed block #{index} is dropped whole: {block!r}")
        check(reduction.stats["context_invalid"] == 1,
              f"…and counted, never dropped in silence (#{index})")

    # A block with the required pair and a junk optional keeps the pair and
    # drops the junk — the optionals are decorations, never the reading.
    reduction = reduce_text(
        tick(AGENT_A, 1, tokens_in=100,
             ctx_block=ctx(90000, "2026-07-31T21:01:00.000Z", cap=0, model=17, peak=-3))
        + result(AGENT_A, 2))
    node = node_of(reduction, AGENT_A)
    check(node.ctx == {"used": 90000, "at": "2026-07-31T21:01:00.000Z"},
          "a valid reading with junk optionals keeps the reading and nothing else")
    check(reduction.stats["context_invalid"] == 0
          and reduction.stats["context_readings"] == 1,
          "…and counts as a reading, not as an invalid block")


def test_unknown_keys_never_ride_through_the_reduction():
    print("test_unknown_keys_never_ride_through_the_reduction")
    # `/health` publishes counts derived from this block on the ONE route that
    # answers without a token. A pass-through of arbitrary writer keys is how a
    # path or a session id reaches an unauthenticated response.
    reduction = reduce_text(
        tick(AGENT_A, 1, tokens_in=100,
             ctx_block=ctx(90000, "2026-07-31T21:01:00.000Z",
                           transcript="/home/u/.claude/projects/x/agent.jsonl",
                           sessionId="11111111-1111-4111-8111-111111111111"))
        + result(AGENT_A, 2))
    node = node_of(reduction, AGENT_A)
    check(set(node.ctx) <= {"used", "at", "model", "peak", "cap", "src"},
          "the reduction rebuilds the block from the keys it knows")
    check("transcript" not in node.ctx and "sessionId" not in node.ctx,
          "…so a writer's extra path or session id cannot ride along")


# --- arm 2: the explicit field lists --------------------------------------
def test_the_server_payload_actually_carries_the_field():
    print("test_the_server_payload_actually_carries_the_field")
    root = tmpdir("payload")
    folder = os.path.join(root, "ctx-task")
    os.makedirs(folder)
    with open(os.path.join(folder, lg.EVENTS_FILE), "w", encoding="utf-8") as handle:
        handle.write(
            tick(AGENT_A, 1, tokens_in=100,
                 ctx_block=ctx(90000, "2026-07-31T21:01:00.000Z",
                               model="claude-opus-5", peak=90000, cap=1000000))
            + result(AGENT_A, 2,
                     ctx_block=ctx(91000, "2026-07-31T21:02:00.000Z",
                                   model="claude-opus-5", peak=91000, cap=1000000))
            + tick(AGENT_B, 3, tokens_in=50, plan="sp-b", stage="review")
            + result(AGENT_B, 4, plan="sp-b", stage="review"))
    api, _model = make_api(root)
    task = payload_of(get(api, "/api/tasks"))["tasks"][0]

    nodes = {n["agentId"]: n for n in task["nodes"]}
    check("ctx" in nodes[AGENT_A],
          "the node payload NAMES `ctx` — it is an explicit field list, and a "
          "reduction the list does not name reaches the browser as nothing")
    check(nodes[AGENT_A]["ctx"]["used"] == 91000,
          "…carrying the last reading, whole")
    check(nodes[AGENT_A]["ctx"]["cap"] == 1000000,
          "…including the declared window when the producer declared one")
    check("ctx" in nodes[AGENT_B] and nodes[AGENT_B]["ctx"] is None,
          "an agent with no reading serialises `null` — the key is present and "
          "explicitly empty, never 0 and never quietly missing from the row")

    records = [t for t in task["tokens"] if t["agentId"] == AGENT_A]
    check(records and "ctx" in records[0],
          "the seven-field token payload names it too (it is now eight)")
    check(any(t["ctx"] and t["ctx"]["used"] == 90000 for t in records),
          "…carrying the reading the tick stated")

    # The wire is JSON, and the absent-vs-zero rule is about the BYTES.
    wire = get(api, "/api/tasks").body.decode("utf-8")
    check('"ctx": {"used": 0' not in wire and '"ctx":{"used":0' not in wire,
          "no `used: 0` reaches the wire from this server")


def test_a_harness_join_demotes_the_rows_without_losing_the_reading():
    print("test_a_harness_join_demotes_the_rows_without_losing_the_reading")
    # GD-D12 demotes `events.jsonl` rows to `assertedNodes` when a harness run
    # joins. A demotion is not a deletion, and the reading has to survive it —
    # otherwise the feature works on unjoined tasks only, which is the half of
    # the corpus nobody would notice losing.
    root = tmpdir("asserted")
    folder = os.path.join(root, "ctx-task")
    os.makedirs(folder)
    with open(os.path.join(folder, lg.EVENTS_FILE), "w", encoding="utf-8") as handle:
        handle.write(tick(AGENT_A, 1, tokens_in=100,
                          ctx_block=ctx(90000, "2026-07-31T21:01:00.000Z"))
                     + result(AGENT_A, 2))
    reduction = lg.reduce_task(lg.task_folder(folder))
    rows = server_mod._task_payload(reduction)["nodes"]
    demoted = [server_mod._asserted_node_row(row) for row in rows]
    check(demoted and demoted[0]["source"] == "asserted",
          "a demoted row is labelled `asserted`")
    check(demoted[0]["ctx"] is not None and demoted[0]["ctx"]["used"] == 90000,
          "…and keeps its reading — a demotion is never a deletion (GD-D12)")


# --- arm 3: replay determinism -------------------------------------------
def test_the_projection_is_deterministic_across_replays_chunks_and_mtimes():
    print("test_the_projection_is_deterministic_across_replays_chunks_and_mtimes")
    head = (tick(AGENT_A, 1, tokens_in=100,
                 ctx_block=ctx(120000, "2026-07-31T21:01:00.000Z", peak=120000)) +
            tick(AGENT_A, 2, tokens_in=200,
                 ctx_block=ctx(180000, "2026-07-31T21:02:00.000Z", peak=180000)))
    tail = (tick(AGENT_A, 3, tokens_in=300,
                 ctx_block=ctx(41000, "2026-07-31T21:03:00.000Z",
                               peak=180000, src="compact")) +
            result(AGENT_A, 4,
                   ctx_block=ctx(41000, "2026-07-31T21:03:00.000Z",
                                 peak=180000, src="compact")))
    whole = head + tail

    root = tmpdir("replay")
    folder = os.path.join(root, "ctx-task")
    os.makedirs(folder)
    events = os.path.join(folder, lg.EVENTS_FILE)

    def project():
        return json.dumps(server_mod._task_payload(
            lg.reduce_task(lg.task_folder(folder))), default=str, sort_keys=True)

    with open(events, "w", encoding="utf-8") as handle:
        handle.write(whole)
    once = project()
    twice = project()
    check(once == twice, "the same bytes reduce to the same projection, twice")

    # Written in two chunks — the append-only stream a live tail actually sees.
    with open(events, "w", encoding="utf-8") as handle:
        handle.write(head)
    partial = project()
    with open(events, "a", encoding="utf-8") as handle:
        handle.write(tail)
    chunked = project()
    check(chunked == once,
          "…and to the same projection when the same bytes arrive in two appends")
    check(partial != once,
          "…while the partial stream is genuinely a different (earlier) state, "
          "so the check above is not passing vacuously")

    # Every mtime moved. Nothing in the projection may depend on one: mtime
    # ordering is the tempting shortcut GD-LC-2 rules out by name, and a
    # dependency on it would make history re-render differently after a copy.
    for name in os.listdir(folder):
        os.utime(os.path.join(folder, name), (1, 1))
    check(project() == once, "…and identically again with every mtime rewritten")

    # A byte-identical copy in a differently-named tree, reduced through the
    # same path: the reading may not vary with where the folder sits.
    other_root = tmpdir("replay-copy")
    shutil.copytree(folder, os.path.join(other_root, "ctx-task"))
    copied = lg.reduce_task(lg.task_folder(os.path.join(other_root, "ctx-task")))
    check(node_of(copied, AGENT_A).ctx == json.loads(
        json.dumps(node_of(lg.reduce_task(lg.task_folder(folder)), AGENT_A).ctx)),
        "…and a copy of the folder yields the same reading")


# --- arm 5: /health -------------------------------------------------------
def test_health_names_the_rung_and_redacts_everything_else():
    print("test_health_names_the_rung_and_redacts_everything_else")
    root = tmpdir("health")
    folder = os.path.join(root, "ctx-task")
    os.makedirs(folder)
    with open(os.path.join(folder, lg.EVENTS_FILE), "w", encoding="utf-8") as handle:
        handle.write(tick(AGENT_A, 1, tokens_in=100) + result(AGENT_A, 2))
    api, _model = make_api(root)

    block = payload_of(get(api, "/health"))["context"]
    check(block["source"] == "absent",
          "before any projection the rung is `absent` — the pre-feature "
          "condition an operator has to be able to see")

    get(api, "/api/tasks")
    block = payload_of(get(api, "/health"))["context"]
    check(block["source"] == "absent",
          "a projected task with no reading is STILL absent, not `events` — "
          "'the watcher never emitted one' and 'it emitted one' are different "
          "words, not the same zeros")
    check(block["agentsUnresolved"] == 1 and block["agentsWithReading"] == 0,
          "…and the unresolved agent is counted rather than hidden")

    with open(os.path.join(folder, lg.EVENTS_FILE), "w", encoding="utf-8") as handle:
        handle.write(tick(AGENT_A, 1, tokens_in=100,
                          ctx_block=ctx(90000, "2026-07-31T21:01:00.000Z",
                                        model="claude-opus-5"))
                     + result(AGENT_A, 2,
                              ctx_block=ctx(90000, "2026-07-31T21:01:00.000Z",
                                            model="claude-opus-5")))
    get(api, "/api/tasks")
    block = payload_of(get(api, "/health"))["context"]
    check(block["source"] == "events",
          "once a reading has arrived the rung is `events` — READ from "
          "events.jsonl, never measured here")
    check(block["agentsWithReading"] == 1 and block["agentsUnresolved"] == 0,
          "…and the counts move with it")
    check(block["multiIterationTurns"] is None,
          "multiIterationTurns is null, not 0: the watcher counts that branch "
          "on its own stderr and it never travels on the wire, so a 0 here "
          "would claim a measurement this server cannot make")
    check(isinstance(block.get("note"), str) and block["note"],
          "the block says in words which rung it is")

    # The redaction posture of `h_health` is not negotiable for this key.
    serialised = json.dumps(block)
    check(AGENT_A not in serialised and AGENT_B not in serialised,
          "no agent id appears in the block")
    check(not re.search(r"\b[0-9a-f]{17}\b", serialised),
          "…no 17-hex id of any kind")
    check(not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                        r"[0-9a-f]{4}-[0-9a-f]{12}", serialised),
          "…no session uuid")
    check("/" not in serialised.replace("\\/", "") and "ctx-task" not in serialised,
          "…no path and no task name")
    check(all(isinstance(value, (int, str)) or value is None
              for value in block.values()),
          "…and nothing but counts, one enum, one null and one note")

    # `/health` is the one route that answers WITHOUT a token, so the block has
    # to be there for an unauthenticated caller too.
    open_block = payload_of(api.get("/health", {}))["context"]
    check(open_block["source"] == "events",
          "the block is served on the unauthenticated route, same shape")


def test_health_counts_do_not_multiply_when_the_page_polls():
    print("test_health_counts_do_not_multiply_when_the_page_polls")
    # `/api/tasks` re-reduces every folder on every poll (10 s health, 30 s
    # tasks). Running totals would multiply one run's agents by the number of
    # times the page asked, and the operator would read a rising fault where
    # nothing changed.
    root = tmpdir("poll")
    folder = os.path.join(root, "ctx-task")
    os.makedirs(folder)
    with open(os.path.join(folder, lg.EVENTS_FILE), "w", encoding="utf-8") as handle:
        handle.write(tick(AGENT_A, 1, tokens_in=100,
                          ctx_block=ctx(90000, "2026-07-31T21:01:00.000Z"))
                     + result(AGENT_A, 2))
    api, _model = make_api(root)
    for _ in range(4):
        get(api, "/api/tasks")
    block = payload_of(get(api, "/health"))["context"]
    check(block["agentsWithReading"] == 1 and block["readings"] == 1,
          "four polls of the same folder still report one agent and one reading")


# --- the page -------------------------------------------------------------
def test_the_page_renders_the_number_and_nothing_it_did_not_measure():
    print("test_the_page_renders_the_number_and_nothing_it_did_not_measure")
    body = JS_PATH.read_text(encoding="utf-8")
    check("function ctxOf(" in body,
          "the page validates the block through one named reader")
    check("node.ctx" in body or "row && row.ctx" in body,
          "…and reads it off the row the server sends")
    check('"ctx " + fmtInt(' in body,
          "the absolute number is rendered, grouped like every other count")

    # The single most important guard: there is NO window constant on this page
    # to divide by, so no percentage can be fabricated when `cap` is absent.
    # 200000 is the specific value that renders a healthy 522k agent as
    # "261 % full" — the R-58 defect the whole plan exists to avoid.
    for constant in ("200000", "200_000", "1000000", "1_000_000", "204800"):
        check(constant not in body,
              f"no window constant {constant} anywhere in app.js")
    reader = body.split("function ctxOf(")[1].split("function ctxTitle(")[0]
    title = body.split("function ctxTitle(")[1].split("\n}\n")[0]
    render = body.split("const ctx = ctxOf(node);")[1].split("\n        }")[0]
    check("%" not in reader + title + render,
          "…and neither the reader, the hover text nor the render site derives "
          "a percentage: 8932 is the number-only surface, the gauge is 8931's "
          "contract")
    check("/" not in render.split("ctx \" + fmtInt(")[1].split("\n")[0],
          "…and the rendered label is a bare number, with no `/cap` denominator")

    check("|| 0" not in reader and "| 0" not in reader,
          "no `|| 0` or `| 0` coercion in the reader — 0 is the one value this "
          "field may never be given")
    check("used <= 0" in reader,
          "…a non-positive `used` invalidates the block instead of rendering")
    check("Number.isFinite" in reader,
          "…and validation is finite-checked, snapNum-style")


def test_the_page_never_aggregates_the_reading():
    print("test_the_page_never_aggregates_the_reading")
    body = JS_PATH.read_text(encoding="utf-8")
    # Every cross-agent aggregate of occupancy is a fabrication: two agents have
    # two separate windows and their levels do not add. The rollup family above
    # `ctxOf` is for SPEND, and nothing may quietly recruit `ctx` into it.
    check("totals[k] += entry.ctx" not in body and "ctx.used +" not in body,
          "the reading is never summed into a rollup")
    check("TOKEN_KEYS" not in body.split("function ctxOf(")[1].split("function ctxTitle(")[0],
          "…and the token-key machinery does not reach into it")
    check(body.count("ctxOf(") >= 2,
          "the reader is defined and used, so the guards above are not dead text")


def main():
    try:
        for test in (
            test_the_fold_is_last_reading_wins_and_never_a_sum,
            test_a_later_line_without_a_reading_does_not_erase_the_one_held,
            test_the_reading_is_whole_object_replace_with_no_merge,
            test_no_reading_is_absent_not_zero,
            test_a_zeroed_or_malformed_block_is_dropped_whole_and_counted,
            test_unknown_keys_never_ride_through_the_reduction,
            test_the_server_payload_actually_carries_the_field,
            test_a_harness_join_demotes_the_rows_without_losing_the_reading,
            test_the_projection_is_deterministic_across_replays_chunks_and_mtimes,
            test_health_names_the_rung_and_redacts_everything_else,
            test_health_counts_do_not_multiply_when_the_page_polls,
            test_the_page_renders_the_number_and_nothing_it_did_not_measure,
            test_the_page_never_aggregates_the_reading,
        ):
            test()
    finally:
        for path in TMPDIRS:
            shutil.rmtree(path, ignore_errors=True)
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("all context-projection (LC-10 / LC-11) tests passed")


if __name__ == "__main__":
    main()
