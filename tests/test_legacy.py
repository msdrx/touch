#!/usr/bin/env python3
"""Stdlib-only tests for aggregator/legacy.py (R-27 + R-51 + R-58's read-time
half). Run as `python3 test_legacy.py`; exits non-zero on failure. No pytest,
no runner.

The item's own test lists are the spine, and every one of them is asserted
against **verbatim frozen bytes** (`tests/fixtures/legacy/`, R-03/R-41) rather
than against a hand-written sample — these streams are the only specimens of
their defects in existence:

R-27  the two-wave respawn yields distinct ordinals; `plan|failed
      "loop exited -> synthesis"` renders "closed — no verdict";
      `touch-repo-recon`'s phantom running agents close stale from the terminal
      complete event; duplicate stage terminals dedupe to one record keeping
      `agentDetail`; a line/size bound on the token fold.
R-51  N docs for N lines including the two byte-identical duplicates; the
      unattributable lines carry `provenance:"unknown"`; the artifact registry
      lists a folder's files with correct digests.
R-58  the three affected streams replay with **zero `failed` badges** on
      research or synthesis, the failed-then-done fixture renders `done`, and
      `touch-repo-recon`'s genuine failures stay `failed` — honesty runs both
      ways (D13).

`fixtures/legacy/anchors.json` is used as the contract wherever it states a
number (line counts, unattributable counts, duplicate-terminal pairs, respawn
waves, ts inversions, conflicting terminals): `tests/test_fixtures.py` verifies
those anchors against the bytes, so asserting against them here means a rule
change cannot pass by editing a constant in this file.
"""

import ast
import hashlib
import json
import math
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
from _roots import ORCH_REL, SRC      # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))

from aggregator import legacy as lg                            # noqa: E402
from aggregator import mongo_store as ms                       # noqa: E402
from aggregator import paths                                   # noqa: E402
from aggregator import refs                                    # noqa: E402

FIXTURES = HERE / "fixtures" / "legacy"
ANCHORS = json.loads((FIXTURES / "anchors.json").read_text(encoding="utf-8"))
STREAMS = ANCHORS["streams"]

#: The three streams R-58 names: each fabricated a `research plan failed` badge
#: while every researcher had succeeded. `touch-repo-recon` is deliberately NOT
#: in this set — its failures are genuine (a user kill) and must survive.
AFFECTED = ("touch-aggregator", "touch-full-recon", "touch-mongo-live")

failures = []


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


def stream_path(task):
    return FIXTURES / f"{task}-events.jsonl"


def load(task):
    """Parse one frozen stream; assert the line count anchors agree."""
    events = lg.read_events(stream_path(task), task)
    return events


def reduced(task, **kwargs):
    return lg.reduce_events(load(task), task=task, **kwargs)


def task_tree(root, task="demo-task", *, config=None, events=None, files=()):
    """A synthetic task folder — the shape RUNSTATE-13 says is not uniform."""
    path = os.path.join(root, task)
    os.makedirs(path, exist_ok=True)
    if events is not None:
        with open(os.path.join(path, lg.EVENTS_FILE), "w", encoding="utf-8") as handle:
            handle.write(events)
    if config is not None:
        with open(os.path.join(path, lg.CONFIG_FILE), "w", encoding="utf-8") as handle:
            json.dump(config, handle)
    for rel, body in files:
        target = os.path.join(path, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(body)
    return path


def line(**fields):
    return json.dumps(fields) + "\n"


# --- parsing: the file's own order, the file's own spelling ---------------
def test_every_frozen_line_parses_and_keeps_its_position():
    print("test_every_frozen_line_parses_and_keeps_its_position")
    for task, anchors in STREAMS.items():
        name = task.replace("-events.jsonl", "")
        events = load(name)
        check(len(events) == anchors["lines"],
              f"{name}: {len(events)} events for {anchors['lines']} frozen lines")
        check([event.line_no for event in events] == list(range(1, len(events) + 1)),
              f"{name}: line numbers are file positions, 1-based and gapless")
        check(all(event.ok for event in events),
              f"{name}: every line is a JSON object")
        # RUNSTATE-6: two writers, two ISO spellings, and the file is append-
        # ordered but NOT timestamp-ordered. Both are asserted, because a reader
        # that sorts by ts reorders the spawn/observe pairs.
        inversions = [event.line_no for previous, event in zip(events, events[1:])
                      if previous.ts and event.ts and event.ts < previous.ts]
        check(inversions == anchors["ts_inversions"],
              f"{name}: the anchored ts inversions are exactly {inversions}")
        spellings = {event.ts_raw[-1] for event in events if event.ts_raw}
        check(spellings == {"Z", "0"},
              f"{name}: both writers' ts spellings survive verbatim ({spellings})")


def test_an_anchored_line_reads_exactly_as_the_anchor_says():
    print("test_an_anchored_line_reads_exactly_as_the_anchor_says")
    for task, anchors in STREAMS.items():
        name = task.replace("-events.jsonl", "")
        events = load(name)
        for anchor in anchors["anchors"]:
            event = events[anchor["line"] - 1]
            ok = (event.plan == anchor["plan"] and event.stage == anchor["stage"]
                  and event.state == anchor["state"]
                  and event.detail.startswith(anchor["detail_startswith"]))
            check(ok, f"{name}:{anchor['line']} is {anchor['what']}")


def test_a_broken_line_is_kept_and_a_torn_tail_is_not():
    print("test_a_broken_line_is_kept_and_a_torn_tail_is_not")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, lg.EVENTS_FILE)
        good = line(ts="2026-07-25T00:00:00.000Z", plan="p", stage="s",
                    state="running", detail="one")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(good)
            handle.write("{not json\n")
            handle.write("\n")
            handle.write(good)
            handle.write('{"ts": "2026-07-25T00:00:0')      # killed mid-append
        events = lg.read_events(path, "t")
        check([event.line_no for event in events] == [1, 2, 4],
              f"the blank line yields no event and renumbers nothing: "
              f"{[event.line_no for event in events]}")
        check(not events[1].ok and events[1].raw == "{not json",
              "a malformed line is KEPT with its bytes — data is never dropped (GD-26)")
        check(all(event.line_no != 5 for event in events),
              "…while the torn final line (no newline, incomplete) is not stored: "
              "a positional _id must not be minted for half a line")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, lg.EVENTS_FILE)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(good.rstrip("\n"))                 # complete, just unterminated
        check(len(lg.read_events(path, "t")) == 1,
              "…but a complete final line with no trailing newline IS stored")

    check(lg.read_events(os.path.join(tmp, "gone.jsonl"), "t") == (),
          "a missing stream is (), not an error (RUNSTATE-13: the layout varies)")


def test_an_unparseable_ts_does_not_lose_the_line():
    print("test_an_unparseable_ts_does_not_lose_the_line")
    event = lg.parse_line("t", 1, line(ts="yesterday", plan="p", stage="s",
                                       state="done", detail="d"))
    check(event.ok and event.ts is None and event.ts_error,
          "a line with an unreadable ts still says what happened")
    reduction = lg.reduce_events([event], task="t")
    check(reduction.nodes and reduction.nodes[0].state == "done",
          "…and the result it reports is still counted")


def test_out_of_enum_states_map_to_info_and_are_never_dropped():
    print("test_out_of_enum_states_map_to_info_and_are_never_dropped")
    # RUNSTATE-16: `status.sh` validates nothing, so out-of-enum states exist.
    event = lg.parse_line("t", 1, line(ts="2026-07-25T00:00:00.000Z", plan="p",
                                       stage="s", state="WAT", detail="d"))
    check(event.state == "info" and event.state_raw == "WAT",
          "an out-of-enum state maps to `info`, keeping the original")
    op = lg.map_legacy_event(lg.LegacyEventObservation.from_event(event))[0]
    stored = op[2]["$set"]
    check(stored["state"] == "info" and stored["stateRaw"] == "WAT",
          "…and the mirror stores both, so the mapping is auditable")


# --- GD-28: provenance never guesses -------------------------------------
def test_provenance_follows_the_no_guess_rule_and_the_anchored_counts():
    print("test_provenance_follows_the_no_guess_rule_and_the_anchored_counts")
    for task, anchors in STREAMS.items():
        name = task.replace("-events.jsonl", "")
        events = load(name)
        unknown = [event for event in events
                   if event.provenance == lg.PROVENANCE_UNKNOWN]
        check(len(unknown) == anchors["unattributable"],
              f"{name}: {len(unknown)} unattributable lines, as anchored")
        if "unattributable_in_first_130" in anchors:
            first = sum(1 for event in unknown if event.line_no <= 130)
            check(first == anchors["unattributable_in_first_130"],
                  f"{name}: {first} of them in the first 130 lines (CUSTOMSTATE-3)")
        check(all(event.provenance in ("derived", "asserted", "unknown")
                  for event in events),
              f"{name}: `harness` is structurally impossible on a legacy line")
        for event in events:
            shape = ("derived" if event.agent is not None or event.tokens is not None
                     else "asserted" if event.title else "unknown")
            if event.provenance != shape:
                check(False, f"{name}:{event.line_no} attributed {event.provenance}, "
                             f"shape says {shape}")
                break
        else:
            check(True, f"{name}: every line follows the shape rule exactly")


def test_the_w_field_wins_over_the_shape_rules():
    print("test_the_w_field_wins_over_the_shape_rules")
    # R-39's forward fix: no frozen line carries `w`, so this is the only place
    # the future path can be asserted.
    agentish = dict(ts="2026-07-25T00:00:00.000Z", plan="p", stage="s",
                    state="done", detail="d")
    check(lg.parse_line("t", 1, line(**agentish)).provenance == "unknown",
          "without `w`, a bare five-key line is honestly unattributable")
    check(lg.parse_line("t", 1, line(w="agent", **agentish)).provenance == "asserted",
          "`w:agent` ⇒ asserted, no shape guessing needed")
    check(lg.parse_line("t", 1, line(w="watcher", **agentish)).provenance == "derived",
          "`w:watcher` ⇒ derived")
    tokens = dict(agentish, tokens={"in": 1, "out": 2, "cached": 0, "cache_write": 0})
    check(lg.parse_line("t", 1, line(w="agent", **tokens)).provenance == "asserted",
          "an explicit `w` beats the token shape (status.sh can carry tokens too)")
    check(lg.parse_line("t", 1, line(w="future", **tokens)).provenance == "derived",
          "an unknown `w` value falls back to the shape rules, never a fifth value")


# --- GD-14: identity is synthesized --------------------------------------
def test_the_two_wave_respawn_becomes_distinct_ordinals():
    print("test_the_two_wave_respawn_becomes_distinct_ordinals")
    # `touch-repo-recon` is the only two-wave sample in existence (RUNSTATE-2).
    reduction = reduced("touch-repo-recon")
    waves = STREAMS["touch-repo-recon-events.jsonl"]["respawn_waves"]
    for key, lines in waves.items():
        plan, _, stage = key.partition("/")
        nodes = [node for node in reduction.nodes
                 if node.plan == plan and node.stage == stage]
        agents = {node.agent_id for node in nodes}
        check(len(nodes) == len(agents),
              f"{key}: {len(nodes)} node(s), {len(agents)} distinct agent id(s) "
              f"from spawn lines {lines}")
        check([node.ordinal for node in nodes] == list(range(len(nodes))),
              f"…with ordinals {[node.ordinal for node in nodes]} — "
              f"a respawn is a new node, not a flickering one")
    v0task = [node for node in reduction.nodes if node.stage == "v0task"]
    check(len(v0task) == 2 and v0task[0].agent_id != v0task[1].agent_id,
          "the two `v0task` agents 9 minutes apart are two nodes (RUNSTATE-8.2)")
    check(len(reduction.nodes) == 9,
          f"nine agent entries, six stage names: {len(reduction.nodes)} nodes")


def test_a_status_sh_running_row_and_the_watcher_spawn_are_one_node():
    print("test_a_status_sh_running_row_and_the_watcher_spawn_are_one_node")
    # Both writers announce the same spawn ("scanning models perspective", then
    # "research attempt 1 spawned" with the agent attached). Counting each as a
    # spawn would double every node in every stream.
    reduction = reduced("touch-mongo-live")
    convo = [node for node in reduction.nodes if node.stage == "convo"]
    check(len(convo) == 1 and convo[0].ordinal == 0,
          f"one node for the convo stage, not two ({len(convo)})")
    check(convo[0].agent_id == refs.legacy_agent_id("touch-mongo-live", "a1451612"),
          "…and it adopted the agent the watcher named")


def test_agent_ids_are_namespaced_and_both_widths_join():
    print("test_agent_ids_are_namespaced_and_both_widths_join")
    reduction = reduced("touch-aggregator")
    ids = {node.agent_id for node in reduction.nodes}
    check(all(one.startswith("legacy:touch-aggregator:") for one in ids),
          "an 8-hex event id becomes GD-14's `legacy:<task>:<id8>`")
    check(refs.classify(refs.parse_ref_key("agentId", sorted(ids)[0])) == "agentId",
          "…which `refs` accepts as an agentId (the 17-hex validator's exemption)")

    full = "a2fc883c96ff7b837"
    stream = (
        line(ts="2026-07-25T00:00:00.000Z", plan="research", stage="sessiondata",
             state="running", detail="spawned",
             agent={"id": "a2fc883c", "label": "research #1"})
        + line(ts="2026-07-25T00:00:01.000Z", plan="research", stage="sessiondata",
               state="done", detail="found 20 findings",
               agent={"id": full, "label": "research #1",
                      "tokens": {"in": 5, "out": 1, "cached": 0, "cache_write": 0}})
    )
    events = [lg.parse_line("t", index, text)
              for index, text in enumerate(stream.splitlines(), 1)]
    joined = lg.reduce_events(events, task="t")
    check(joined.stats["prefix_joins"] == 1,
          "a stream carrying BOTH id widths for one agent joins by unique prefix")
    check({node.agent_id for node in joined.nodes} == {full},
          "…onto the full 17-hex id, the 8-hex form being display only (RUNSTATE-3)")
    check(all(record.agent_id == full for record in joined.tokens),
          "…and the token rollup follows the join, so one agent is not two rows")

    ambiguous = events + [lg.parse_line("t", 3, line(
        ts="2026-07-25T00:00:02.000Z", plan="research", stage="other", state="done",
        detail="d", agent={"id": "a2fc883c96ff7b999", "label": "x"}))]
    check(lg.reduce_events(ambiguous, task="t").stats["prefix_joins"] == 0,
          "a prefix matching two full ids joins nothing — ambiguity is not resolved "
          "by picking one")


def test_a_run_id_is_synthesized_when_the_config_does_not_name_one():
    print("test_a_run_id_is_synthesized_when_the_config_does_not_name_one")
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "local-orchestrators")
        wf = os.path.join(tmp, "claude", "projects", "slug", "s", "workflows",
                          "wf_455b348c-e17")
        os.makedirs(wf)
        task_tree(root, "configured", config={"wf_dir": wf}, events="")
        task_tree(root, "bare", events="")
        folders = {folder.task: folder
                   for folder in lg.discover_tasks(root, claude_root=os.path.join(tmp, "claude"))}
        check(folders["configured"].run_id == "wf_455b348c-e17",
              "runId = basename(orch-config.wf_dir) when the config names one")
        check(folders["bare"].run_id == "legacy:bare",
              "…else `legacy:<task-folder>` (RUNSTATE-2: the stream carries none)")
        node_ref = lg.NodeState(task="bare", run_id="legacy:bare", plan="research",
                                stage="models", ordinal=1)
        check(node_ref.ref_id() == refs.run_node_key("legacy:bare", "research/models", 1),
              "a node's refId is built by refs.ref_key like every other id (SD-11)")


# --- D-04 / GD-D12: the harness join's pure half -------------------------
def test_a_synthetic_run_id_can_never_join_a_harness_run():
    print("test_a_synthetic_run_id_can_never_join_a_harness_run")
    check(lg.is_synthetic_run_id("legacy:bare"),
          "`legacy:<task>` is recognised as SYNTHESIZED — a folder that named no "
          "wf_dir has no run to join, and the prefix says so without re-reading "
          "the config")
    check(not lg.is_synthetic_run_id("wf_455b348c-e17"),
          "…while a real runId is not")
    check(not lg.is_synthetic_run_id(None) and not lg.is_synthetic_run_id(17),
          "…and a missing or non-string id is not synthetic either (it is simply "
          "not joinable), never a crash")


def test_the_plans_denominator_comes_from_the_dividers_own_result():
    print("test_the_plans_denominator_comes_from_the_dividers_own_result")
    divider = {"subplans": [{"id": "sp-01"}, {"id": "sp-02"}, {"id": "sp-03"}]}
    check(lg.subplan_count(divider) == 3, "a divider result states its sub-plan count")
    check(lg.derive_plans_total([{"passed": True}, divider, {"summary": "x"}]) == 5,
          "…and the denominator is N + 2 — the divide card and the final gate "
          "(GD-D11: the same N + 2 cycle_reporter declares at the divide close)")
    check(lg.derive_plans_total([{"passed": True}]) is None,
          "a run whose divider has not returned derives NOTHING rather than "
          "guessing — the wire hint stays the only number")
    check(lg.subplan_count("not a dict") is None and lg.subplan_count({"subplans": 4}) is None,
          "a non-dict result and a non-list `subplans` answer None, not an error: "
          "'this is not a divider's result' is the ordinary case")


def test_the_denominator_fold_is_the_monotonic_max_it_always_was():
    print("test_the_denominator_fold_is_the_monotonic_max_it_always_was")
    check(lg.fold_plans_total(5, 7) == 7 and lg.fold_plans_total(7, 5) == 7,
          "the fold is a max in either order — the wire value is an EARLY HINT and "
          "a later, larger derivation wins")
    check(lg.fold_plans_total(None, 7) == 7 and lg.fold_plans_total(7, None) == 7,
          "…a missing source never erases a known denominator")
    check(lg.fold_plans_total(None, None) is None,
          "…and with nothing known the answer is None, not 0 (which would render "
          "as a denominator)")
    check(lg.fold_plans_total(7, 0, -3, True) == 7,
          "a zero, a negative and a bool are not denominators and cannot shrink one")


def test_the_derived_denominator_is_reconciled_into_the_reduction():
    print("test_the_derived_denominator_is_reconciled_into_the_reduction")
    # GD-D10: the fold lands in the module that owns the reduction, not in the
    # API projection. A derivation applied while a response is built is one
    # every other reader of the same reduction silently does without.
    rows = (line(ts="2026-07-25T00:00:01.000Z", plan="sp-01", stage="implement",
                 state="running", detail="attempt 1", plans_total=3),
            line(ts="2026-07-25T00:00:02.000Z", plan="sp-02", stage="implement",
                 state="running", detail="attempt 1"))
    reduction = lg.reduce_events(
        [lg.parse_line("t", n, text) for n, text in enumerate(rows, 1)], task="t")

    check(reduction.plans["sp-01"].plans_total == 3
          and reduction.plans["sp-02"].plans_total is None,
          "the wire hint reduces exactly as it did — one card declared 3, the other "
          "declared nothing")
    check(lg.reconcile_plans_total(reduction, 5) == 5,
          "the derived denominator is returned, so a caller can publish the number it "
          "joined on")
    check(reduction.plans["sp-01"].plans_total == 5
          and reduction.plans["sp-02"].plans_total == 5,
          "…and EVERY card carries it: the hint of 3 was early, not wrong, and a card "
          "that declared nothing is not a card with no denominator")
    check(lg.reconcile_plans_total(reduction, 4) == 4
          and reduction.plans["sp-01"].plans_total == 5,
          "a smaller later value never shrinks a card — the same monotonic max the "
          "event fold applies, reached from the other source")
    check(lg.reconcile_plans_total(reduction, None) is None
          and reduction.plans["sp-01"].plans_total == 5,
          "…and nothing derived changes nothing, which is what a run with no divider "
          "result must do")


# --- GD-14 re-labels + SD-4 ----------------------------------------------
def test_the_fabricated_failed_badge_becomes_closed_no_verdict():
    print("test_the_fabricated_failed_badge_becomes_closed_no_verdict")
    # RUNSTATE-4's original specimen: six researchers done, a 52 KB plan on
    # disk, and line 571 says the research plan failed.
    reduction = reduced("touch-aggregator")
    research = reduction.plans["research"]
    check(research.badge == lg.CLOSED_STATE and research.label == lg.CLOSED_NO_VERDICT,
          f"`plan failed` + `loop exited ->` + all agents resulted ⇒ "
          f"{research.label!r}")
    check(research.derived_from_legacy,
          "…marked derived_from_legacy: a reader can always tell Touch's "
          "conclusion from the stream's words")
    check(research.badge_line == 571, f"…anchored at line {research.badge_line}")
    check(any("closed — no verdict" in note for note in reduction.notes),
          "…and it is stated in a note the card can render (D13)")


def test_a_genuine_failure_keeps_its_badge():
    print("test_a_genuine_failure_keeps_its_badge")
    # `touch-repo-recon:101,102` — a user kill. Honesty runs both ways: the
    # re-labeller that paints these green is as wrong as the watcher that
    # painted the others red.
    reduction = reduced("touch-repo-recon")
    check(reduction.badge_of("research") == "failed",
          "`stopped by user before completion` stays failed — no `loop exited ->`")
    check(reduction.badge_of("synthesis") == "failed",
          "`run stopped before synthesis started` stays failed")
    check(reduction.plans["research"].relabel is None,
          "…and neither is marked derived_from_legacy")


def test_sd4_last_event_wins_on_conflicting_plan_terminals():
    print("test_sd4_last_event_wins_on_conflicting_plan_terminals")
    for name in ("touch-full-recon", "touch-mongo-live"):
        anchors = STREAMS[f"{name}-events.jsonl"]["conflicting_plan_terminals"]
        reduction = reduced(name)
        for conflict in anchors:
            plan = reduction.plans[conflict["plan"]]
            if conflict["corrective_done"] is None:
                check(plan.badge == lg.CLOSED_STATE,
                      f"{name}/{conflict['plan']}: fabricated failed at line "
                      f"{conflict['failed']} with no correction ⇒ closed — no verdict")
                continue
            check(plan.badge == "done" and plan.badge_line == conflict["corrective_done"],
                  f"{name}/{conflict['plan']}: the corrective done at line "
                  f"{conflict['corrective_done']} beats the failed at line "
                  f"{conflict['failed']} (SD-4, last-event-wins in FILE order)")
            check((conflict["failed"], "failed") in plan.conflicting,
                  "…and the beaten terminal is recorded, not erased")

    # The rule is about file order, not about `done` beating `failed`: a
    # corrective line that arrives FIRST must not win.
    reversed_pair = [
        lg.parse_line("t", 1, line(ts="2026-07-25T00:00:00.000Z", plan="research",
                                   stage="plan", state="done", detail="early done")),
        lg.parse_line("t", 2, line(ts="2026-07-25T00:00:01.000Z", plan="research",
                                   stage="plan", state="failed",
                                   detail="really failed: disk full")),
    ]
    check(lg.reduce_events(reversed_pair, task="t").badge_of("research") == "failed",
          "…and it is symmetric: the LAST terminal wins, whatever it says")


def test_r58_zero_failed_badges_on_the_three_affected_streams():
    print("test_r58_zero_failed_badges_on_the_three_affected_streams")
    for name in AFFECTED:
        reduction = reduced(name)
        painted = {plan: state.badge for plan, state in reduction.plans.items()
                   if plan in ("research", "synthesis")}
        check("failed" not in painted.values(),
              f"{name}: research/synthesis badges are {painted} — zero `failed`")
    check(reduced("touch-mongo-live").badge_of("research") == "done",
          "the failed-then-done fixture renders `done` (R-58's acceptance line)")
    check(reduced("touch-repo-recon").badge_of("research") == "failed",
          "…and the stream whose failure was real is untouched by all of it")


def test_an_unresulted_sibling_blocks_the_relabel():
    print("test_an_unresulted_sibling_blocks_the_relabel")
    # GD-14 conditions the re-label on "all stage agents resulted". A plan that
    # advanced while an agent was still running has NOT been proven green.
    events = [
        lg.parse_line("t", 1, line(ts="2026-07-25T00:00:00.000Z", plan="research",
                                   stage="a", state="running", detail="spawned",
                                   agent={"id": "a0000001", "label": "r #1"})),
        lg.parse_line("t", 2, line(ts="2026-07-25T00:00:01.000Z", plan="research",
                                   stage="plan", state="failed",
                                   detail="loop exited -> synthesis")),
    ]
    reduction = lg.reduce_events(events, task="t")
    check(reduction.badge_of("research") == "failed",
          "one agent still running ⇒ the badge is not re-labelled")
    check(reduction.plans["research"].relabel is None,
          "…and nothing claims to be derived from legacy")


def test_the_terminal_complete_closes_phantom_agents_stale():
    print("test_the_terminal_complete_closes_phantom_agents_stale")
    # RUNSTATE-9: the watcher's stale-close is gated on the attempt strictly
    # increasing, and the 13:50 respawn reused attempt 1 — so the 13:41 agents
    # stay `running` in the stream forever.
    reduction = reduced("touch-repo-recon")
    states = [node.state for node in reduction.nodes]
    check("running" not in states,
          f"after the terminal orchestrator|complete no node is still running: {states}")
    stale = [node for node in reduction.nodes if node.relabel == lg.STALE_STATE]
    superseded = [node for node in reduction.nodes
                  if node.relabel == lg.SUPERSEDED_STATE]
    check(len(stale) == 4 and len(superseded) == 3,
          f"the 7 never-closed agents split into {len(superseded)} superseded "
          f"(a later sibling spawned) and {len(stale)} stale (the run ended)")
    check(all(node.derived_from_legacy for node in stale + superseded),
          "…every one of them marked derived_from_legacy, never `failed` (D13)")
    check(all(node.state != "failed" for node in reduction.nodes),
          "an abandoned agent did not fail, it was abandoned")


def test_a_mid_stream_complete_does_not_close_a_later_invocation():
    print("test_a_mid_stream_complete_does_not_close_a_later_invocation")
    # RUNSTATE-2: one folder's stream spans several script invocations.
    # `touch-mongo-live` completes its research run at line 298 and keeps
    # appending; the implement agent spawned at line 318 is still running.
    reduction = reduced("touch-mongo-live")
    node = [one for one in reduction.nodes if one.plan == "sp-repo-bootstrap"][0]
    check(node.state == "running" and node.relabel is None,
          f"a node spawned AFTER the run-close is still running, not stale "
          f"({node.state})")
    done = [one for one in reduction.nodes if one.plan == "research"]
    check(all(one.state == "done" for one in done),
          "…while the completed research nodes keep their observed results")


# --- RUNSTATE-7: the twin write ------------------------------------------
def test_duplicate_stage_terminals_dedupe_and_keep_the_agents_words():
    print("test_duplicate_stage_terminals_dedupe_and_keep_the_agents_words")
    for task, anchors in STREAMS.items():
        name = task.replace("-events.jsonl", "")
        pairs = anchors["duplicate_stage_terminals"]
        reduction = reduced(name)
        check(reduction.stats["deduped_terminals"] == len(pairs),
              f"{name}: {len(pairs)} anchored duplicate terminal pair(s), "
              f"{reduction.stats['deduped_terminals']} deduped")

    reduction = reduced("touch-aggregator")
    node = [one for one in reduction.nodes if one.stage == "agentgraph"][0]
    check(node.detail == "research #1: 17 findings",
          f"watcher-wins on the detail (it is the deterministic source): "
          f"{node.detail!r}")
    check(node.agent_detail == "found 17 findings",
          f"…and the agent's own words are kept as agentDetail: {node.agent_detail!r}")
    check(len([one for one in reduction.nodes if one.stage == "agentgraph"]) == 1,
          "one logical completion, one node — an `N stages done` counter cannot "
          "double-count")


def test_watcher_wins_is_only_for_same_state_duplicates():
    print("test_watcher_wins_is_only_for_same_state_duplicates")
    # SD-4 states this explicitly so the legacy adapter cannot resurrect the
    # failed badge through the dedup rule.
    events = [
        lg.parse_line("t", 1, line(ts="2026-07-25T00:00:00.000Z", plan="p",
                                   stage="plan", state="failed",
                                   detail="loop exited -> next",
                                   agent={"id": "a0000001", "label": "x"})),
        lg.parse_line("t", 2, line(ts="2026-07-25T00:00:01.000Z", plan="p",
                                   stage="plan", state="done", detail="corrected")),
    ]
    reduction = lg.reduce_events(events, task="t")
    check(reduction.badge_of("p") == "done",
          "a watcher-written `failed` does NOT win over a later agent-written `done`")
    check(reduction.stats["deduped_terminals"] == 0
          and reduction.stats["conflicting_terminals"] == 1,
          "…the two are a conflict resolved by order, not a duplicate resolved "
          "by writer")


# --- RUNSTATE-12: the token fold -----------------------------------------
def test_the_token_fold_is_lossless_and_bounded():
    print("test_the_token_fold_is_lossless_and_bounded")
    for name in ("touch-aggregator", "touch-mongo-live"):
        reduction = reduced(name)
        stats = reduction.stats
        check(stats["token_records"] < stats["token_lines"] / 4,
              f"{name}: {stats['token_lines']} token lines fold to "
              f"{stats['token_records']} records (RUNSTATE-12: 91 % of the stream "
              f"is delta noise)")
        for record in reduction.tokens:
            check(set(record.tokens) == set(lg.TOKEN_KEYS), "") \
                if set(record.tokens) != set(lg.TOKEN_KEYS) else None
        check(all(set(record.tokens) == set(lg.TOKEN_KEYS)
                  for record in reduction.tokens),
              f"{name}: every token record carries all four keys (RUNSTATE-14)")

    # Lossless: the fold takes the LAST cumulative value in a window, never a
    # sum of deltas — summing deltas is the double count GD-25 forbids.
    events = []
    for index in range(1, 7):
        events.append(lg.parse_line("t", index, line(
            ts=f"2026-07-25T00:00:0{index}.000Z", plan="p", stage="tokens",
            state="info", detail="running", quiet=True,
            tokens={"in": 10, "out": 1, "cached": 0, "cache_write": 0},
            agent={"id": "a0000001", "label": "x",
                   "tokens": {"in": 10 * index, "out": index,
                              "cached": 0, "cache_write": 0}})))
    folded = lg.reduce_events(events, task="t", token_window=3600)
    check(len(folded.tokens) == 1, f"six delta lines in one window ⇒ "
                                   f"{len(folded.tokens)} record")
    check(folded.tokens[0].tokens == {"in": 60, "out": 6, "cached": 0, "cache_write": 0},
          f"…carrying the last CUMULATIVE total, not a sum: {folded.tokens[0].tokens}")
    check(folded.tokens[0].folded == 6, "…and saying how many lines it stands for")
    windows = lg.reduce_events(events, task="t", token_window=2)
    check(1 < len(windows.tokens) <= len(events),
          f"a narrower throttle window keeps more records, never more than the "
          f"lines it folds ({len(windows.tokens)} from {len(events)})")
    check(windows.tokens[-1].tokens["in"] == 60,
          "…and the last one still states the true total")


def test_a_token_line_that_cannot_fold_losslessly_is_kept_whole():
    print("test_a_token_line_that_cannot_fold_losslessly_is_kept_whole")
    # The 9 non-quiet `tokens` lines in the corpus are an agent's FINAL total,
    # and a token line naming no agent carries no cumulative copy to take.
    events = [
        lg.parse_line("t", 1, line(ts="2026-07-25T00:00:01.000Z", plan="p",
                                   stage="tokens", state="info", detail="total",
                                   tokens={"in": 7, "out": 2, "cached": 1,
                                           "cache_write": 3})),
        lg.parse_line("t", 2, line(ts="2026-07-25T00:00:02.000Z", plan="p",
                                   stage="tokens", state="info", detail="total",
                                   tokens={"in": 9, "out": 2, "cached": 0,
                                           "cache_write": 0})),
    ]
    reduction = lg.reduce_events(events, task="t")
    check(len(reduction.tokens) == 2,
          f"neither is folded away ({len(reduction.tokens)} records)")
    check(reduction.tokens[0].tokens["in"] == 7 and reduction.tokens[0].agent_id is None,
          "…and an unattributable token line is recorded as exactly that")
    check(not any(record.absolute for record in reduction.tokens),
          "…and is marked `absolute=False`: it is that line's own delta, so a "
          "reader must sum it rather than fold it latest-wins")


# --- GD-C: the fold equals the stream, plan by plan -----------------------
def _zero_tokens():
    return {key: 0 for key in lg.TOKEN_KEYS}


def _number(value):
    """`Number(value)` for the shapes a JSON token field can hold.

    The page writes `Number(tokens[k])`, which accepts more than an `int`: a
    numeric STRING coerces, `null` is 0, a bool is 0/1, an object is NaN. The
    replica below is only evidence about the page if it coerces the same way,
    so it does — rather than type-checking and quietly diverging.

    Two JS corners are deliberately not reproduced, both unreachable here:
    `Number("0x10")` is 16 (Python's `float` refuses), and `Number("Infinity")`
    is Infinity where Python also accepts `"inf"`. Nothing emits a string token
    field at all — `legacy._tokens()` coerces to `int` and `server.py` ships
    those ints — so the divergence has no input that reaches it.
    """
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return value
    if value is None:
        return 0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return float(text)
        except ValueError:
            return float("nan")
    return float("nan")


def _add_tokens(into, tokens):
    """The page's own arithmetic: finite, positive, four keys, never a subtraction."""
    for key in lg.TOKEN_KEYS:
        value = _number((tokens or {}).get(key))
        if math.isfinite(value) and value > 0:
            into[key] += value
    return into


def _delta_sum_per_plan(task):
    """Ground truth: sum EVERY line's top-level `tokens`, per plan.

    This is what `.claude/shared/monitoring/monitor.html` displays
    (`p.tokIn += ev.tokens.in || 0`), read straight off the frozen bytes with no
    reducer in the way — so it cannot inherit a reducer bug.
    """
    totals = {}
    with open(stream_path(task), "r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue                    # a parse failure has no tokens either
            if isinstance(event.get("tokens"), dict):
                _add_tokens(totals.setdefault(event.get("plan"), _zero_tokens()),
                            event["tokens"])
    return totals


def _last_cumulative_per_plan(task):
    """GD-C's model B: per (plan, agent.id) keep the LAST `agent.tokens` seen on
    ANY event — a terminal carries a higher cumulative than the agent's last
    quiet tick — and add the agent-less deltas to nothing."""
    latest = {}
    with open(stream_path(task), "r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            agent = event.get("agent")
            if isinstance(agent, dict) and isinstance(agent.get("tokens"), dict):
                latest[(event.get("plan"), agent.get("id"))] = agent["tokens"]
    totals = {}
    for (plan, _agent), tokens in latest.items():
        _add_tokens(totals.setdefault(plan, _zero_tokens()), tokens)
    return totals


def _rollup_list_per_plan(records, *, collapse_agentless=False):
    """A line-for-line replica of `touch-visual/app.js::rollupList`, per plan.

    `collapse_agentless=True` is the pre-fix arm — kept so the assertion below
    can prove it still has teeth rather than passing vacuously.
    """
    latest = {}
    totals = {}
    for record in records:
        key = (record.plan, record.stage, record.agent_id, record.label)
        if record.agent_id is None and not collapse_agentless:
            _add_tokens(totals.setdefault(record.plan, _zero_tokens()), record.tokens)
            continue
        latest[key] = record
    for key, record in latest.items():
        _add_tokens(totals.setdefault(record.plan, _zero_tokens()), record.tokens)
    return totals


def test_the_token_rollup_equals_the_streams_own_delta_sum():
    print("test_the_token_rollup_equals_the_streams_own_delta_sum")
    # GD-C's executable half. The pre-existing fold test asserts a RATIO on the
    # corpus and an exact total only on a synthetic six-line stream where every
    # line carries a cumulative — the arm that always worked. It could not see
    # that Touch's page showed 880,162,277 where the monitor showed 894,901,067
    # for the same bytes (PRIOR-ART-TOUCH-2/-3), so the shortfall passed green.
    # These three models must agree, per plan and in total, on every frozen
    # stream, or the page and the monitor disagree about the same file again.
    caught = 0
    for task in sorted(name.replace("-events.jsonl", "") for name in STREAMS):
        deltas = _delta_sum_per_plan(task)
        cumulative = _last_cumulative_per_plan(task)
        check(deltas == cumulative,
              f"{task}: summing every delta == summing the last cumulative per "
              f"(plan, agent), plan by plan — {len(deltas)} plans")

        records = reduced(task).tokens
        check(_rollup_list_per_plan(records) == deltas,
              f"{task}: …and the reduction rolled up the way `rollupList` rolls "
              f"it up lands on the same numbers, so the page shows the truth")

        # The old rule (agent-less records in the latest-wins map) is never
        # HIGHER than the truth, and on today's corpora it is strictly lower —
        # the defect this test exists to catch.
        before = _rollup_list_per_plan(records, collapse_agentless=True)
        lost = sum(deltas[plan]["in"] - before.get(plan, _zero_tokens())["in"]
                   for plan in deltas)
        if lost > 0:
            caught += 1
        check(lost >= 0, f"{task}: the pre-fix rollup never over-reported")

        # The wire carries no `absolute` key (`server.py`'s token payload is an
        # explicit seven-field dict), so `rollupList` reads the distinction off
        # `agentId` alone. `legacy._fold_tokens` derives `absolute` from
        # `agent_id`, making the two equivalent by construction — pin both
        # directions so a fold that starts inferring it from the cumulative
        # again (the shape that over-counts, below) fails here first.
        check(all(record.absolute == (record.agent_id is not None)
                  for record in records),
              f"{task}: `absolute` is False on exactly the agent-less records, "
              f"which is what makes the page's `agentId`-only test correct")
    # Deliberately `>= 1`, not `== len(STREAMS)`: this pass's own write side
    # (M1) emits the terminal residual WITH an `agent` block, so a corpus frozen
    # from a post-M1 run has no agent-less token lines and would drop `caught`
    # below the stream count — a correct suite going red for a reason unrelated
    # to the code under test. The corpus-independent teeth are the synthetic
    # streams in the next test, which cannot drift.
    check(caught >= 1,
          f"the collapsing key really did lose tokens on {caught} of "
          f"{len(STREAMS)} frozen streams — the assertions above are not vacuous")


def test_the_rollup_neither_drops_nor_doubles_an_unattributable_line():
    print("test_the_rollup_neither_drops_nor_doubles_an_unattributable_line")
    # Two synthetic streams, built here so the teeth are deterministic and do
    # not depend on what a future frozen corpus happens to contain.

    # (a) The drop the fix removes: several token lines naming no agent at all,
    # on one plan. They share the rollup key `plan|stage|None|None`, so a
    # latest-wins fold keeps the last (30) and loses the rest.
    agentless = [
        lg.parse_line("t", index, line(ts=f"2026-07-25T00:00:0{index}.000Z",
                                       plan="p", stage="tokens", state="info",
                                       detail="delta", quiet=True,
                                       tokens={"in": 10 * index, "out": 0,
                                               "cached": 0, "cache_write": 0}))
        for index in (1, 2, 3)]
    records = lg.reduce_events(agentless, task="t").tokens
    check(len(records) == 3 and not any(r.absolute for r in records),
          f"three unattributable token lines stay three delta records "
          f"({len(records)} records)")
    check(_rollup_list_per_plan(records)["p"]["in"] == 60,
          "…and the rollup sums them: 10 + 20 + 30 = 60")
    check(_rollup_list_per_plan(records, collapse_agentless=True)["p"]["in"] == 30,
          "…where the pre-fix collapsing key kept only the last one (30), which "
          "is the shortfall this test has teeth against")

    # (b) The double-count the fix PREVENTS, and the reason `absolute` is
    # derived from `agent_id` rather than from the presence of a cumulative.
    # These lines carry `agent.tokens` — a cumulative — but the id is neither
    # 8- nor 17-hex, so `agent_ref_id()` cannot key it and `agentId` is null on
    # the wire. Reading them as absolutes files them under one collapsing key;
    # summing them (what the page does with an agent-less record) would add a
    # monotonic sequence of cumulatives: 100 + 300 + 600 = 1000 for a truth of
    # 600, an error that GROWS with run length. Neither may happen.
    unkeyable = [
        lg.parse_line("t", index,
                      line(ts=f"2026-07-25T00:00:0{index}.000Z", plan="p",
                           stage="tokens", state="info", detail="delta",
                           quiet=True,
                           agent={"id": "3f2b1c8e-4c5e-4f01-9abc-000000000001",
                                  "label": "research/a",
                                  "tokens": {"in": total, "out": 0, "cached": 0,
                                             "cache_write": 0}},
                           tokens={"in": delta, "out": 0, "cached": 0,
                                   "cache_write": 0}))
        for index, (delta, total) in
        enumerate(((100, 100), (200, 300), (300, 600)), 1)]
    check(all(event.agent_ref_id() is None and event.agent_tokens is not None
              for event in unkeyable),
          "an id that is neither 8- nor 17-hex carries a cumulative that no "
          "reader can attribute (agent_ref_id ⇒ None)")
    records = lg.reduce_events(unkeyable, task="t").tokens
    check(not any(record.absolute for record in records),
          "…so the fold refuses to call it absolute: `absolute` is "
          "`cumulative is not None AND agent_id is not None`, not the first half")
    check(_rollup_list_per_plan(records)["p"]["in"] == 600,
          "…and the page lands on 600 — the record carries its own delta, so "
          "summing is right; summing the cumulatives would have read 1000")
    check(all(record.absolute == (record.agent_id is not None)
              for record in records),
          "…keeping `absolute == (agentId is not None)` true on a stream shape "
          "no frozen corpus contains")


def test_the_rollup_replica_still_matches_the_page():
    print("test_the_rollup_replica_still_matches_the_page")
    # `_rollup_list_per_plan` is only evidence about the page while it is the
    # same rule as the page's. Source text, the house convention for app.js
    # (tests/test_touch_frontend.py) — the JS is never executed by Python.
    source = (SRC / "touch-visual" / "app.js").read_text(encoding="utf-8")
    start = source.find("function rollupList(")
    check(start != -1, "app.js still defines rollupList")
    end = source.find("\n}", start)
    # Without this arm an unfound terminator (-1) makes `body` the rest of the
    # file, and every substring check below silently searches all of app.js.
    check(end != -1, "…and the function body is delimited, so the checks below "
                     "read rollupList rather than the rest of the file")
    body = source[start:end]
    check("entry.agentId === undefined || entry.agentId === null" in body,
          "rollupList singles out the agent-less records by name")
    divert, mapset = body.find("whole.push("), body.find("latest.set(")
    # `divert < mapset` alone passes when `whole.push(` is ABSENT (-1 < n) —
    # exactly when the divert this assertion protects has been deleted.
    check(divert != -1 and mapset != -1 and divert < mapset,
          "…and diverts them BEFORE the latest-wins map, so they cannot share "
          "the collapsing `plan|stage|null|null` key")
    check(" - " not in body,
          "…while still never subtracting on a token field (GD-25)")


# --- RUNSTATE-13 / GD-14: folders, controls, archive labels ---------------
def test_a_plan_only_folder_is_its_own_kind_with_no_controls():
    print("test_a_plan_only_folder_is_its_own_kind_with_no_controls")
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "local-orchestrators")
        task_tree(root, "touch-monitor-spawn", files=(("plan/x-plan.md", "# plan"),))
        task_tree(root, "ran", events=line(ts="2026-07-25T00:00:00.000Z", plan="p",
                                           stage="plan", state="done", detail="d"))
        folders = {folder.task: folder for folder in lg.discover_tasks(root)}
        check(folders["touch-monitor-spawn"].kind == "plan-only",
              "a folder with a plan and no stream is `plan only / never run`")
        check(not folders["touch-monitor-spawn"].controls,
              "…and offers no join/pause/stop (there is nothing to control)")
        check(folders["ran"].kind == "run" and folders["ran"].controls,
              "…while a folder with a stream is a run")
        reduction = lg.reduce_task(folders["touch-monitor-spawn"])
        check(reduction.kind == "plan-only" and reduction.events == (),
              "it reduces to an empty reduction, not to an error or an `empty task`")


def test_the_archive_label_is_derived_not_constant():
    print("test_the_archive_label_is_derived_not_constant")
    with tempfile.TemporaryDirectory() as tmp:
        claude = os.path.join(tmp, "claude")
        present = os.path.join(claude, "projects", "slug", "s", "workflows", "wf_a")
        os.makedirs(present)
        pruned = os.path.join(claude, "projects", "slug", "s", "workflows", "wf_gone")
        foreign = os.path.join(tmp, "elsewhere", "wf_b")
        labels = {
            "present": lg.archive_label(present, claude_root=claude),
            "archived": lg.archive_label(pruned, claude_root=claude),
            "foreign": lg.archive_label(foreign, claude_root=claude),
            "unrecorded": lg.archive_label(None, claude_root=claude),
        }
        for expected, label in labels.items():
            check(label.state == expected,
                  f"{expected}: {label.label!r}")
        check(labels["present"].path == present,
              "a present source renders via its full path")
        check(labels["foreign"].path == foreign and labels["foreign"].state == "foreign",
              "a foreign source displays its path and is never globbed (PLANS-5)")
        check(set(lg.ARCHIVE_LABELS) == set(lg.ARCHIVE_STATES),
              "every state has a rendered label")


def test_a_broken_config_does_not_break_the_folder():
    print("test_a_broken_config_does_not_break_the_folder")
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "local-orchestrators")
        path = task_tree(root, "bad", events="")
        with open(os.path.join(path, lg.CONFIG_FILE), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        folder = lg.discover_tasks(root)[0]
        check(folder.config_error and folder.run_id == "legacy:bad",
              "an unreadable config yields a visible reason and a synthesized runId")
        check(folder.archive.state == "unrecorded",
              "…and an honest archive label rather than a guess")


def test_the_watcher_checkpoint_is_never_read():
    print("test_the_watcher_checkpoint_is_never_read")
    # GD-14/RUNSTATE-5: `.watcher-state.json` contradicts its own stream and is
    # never closed on kill. Asserted three ways, because "we just don't" is not
    # an assertion.
    source = (SRC / "aggregator" / "legacy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = [node for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and node.value == ".watcher-state.json"]
    check(len(literals) == 1,
          f"the filename is a literal exactly once in the module — the exclusion "
          f"constant, everything else is prose ({len(literals)} occurrence(s))")
    check(lg.WATCHER_STATE_FILE in lg.NEVER_REGISTERED,
          "…and that constant is the never-registered set")

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "local-orchestrators")
        path = task_tree(root, "t", events="", files=(
            (".watcher-state.json", '{"agents": {}}'),
            ("findings/f.md", "note")))
        registered = {artifact.path for artifact in lg.iter_artifacts(
            lg.task_folder(path))}
        check(".watcher-state.json" not in registered,
              f"the artifact registry does not digest it either: {sorted(registered)}")
        check(lg.iter_artifact_observations(
            os.path.join(path, ".watcher-state.json"), root=root) == [],
              "…not even when a backfill hands it the path directly")


# --- R-51: the mirror arm -------------------------------------------------
def test_n_documents_for_n_lines_including_the_identical_ones():
    print("test_n_documents_for_n_lines_including_the_identical_ones")
    name = "touch-mongo-live"
    raw = stream_path(name).read_bytes().splitlines()
    duplicates = len(raw) - len(set(raw))
    check(duplicates == 2,
          f"the frozen stream really does hold {duplicates} byte-identical "
          f"duplicate line(s) — the case a content key would collapse")
    events = load(name)
    ops = [op for event in events
           for op in lg.map_legacy_event(lg.LegacyEventObservation.from_event(event))]
    check(len(ops) == len(events) == STREAMS[f"{name}-events.jsonl"]["lines"],
          f"{len(ops)} operations for {len(events)} lines")
    check(len({op[1] for op in ops}) == len(ops),
          "…every `_id` distinct: identity is POSITION, never content or ts")
    state = {}
    ms.apply_operations(state, ops)
    check(ms.counts(state) == {"legacy_events": len(events)},
          f"…and {len(events)} documents land: {ms.counts(state)}")

    stamps = [event.ts_raw for event in events]
    check(len(stamps) - len(set(stamps)) > 20,
          "duplicate timestamps are normal (measured: up to 27 per file), which "
          "is why a ts key is forbidden too")


def test_the_positional_id_is_the_gd24_grammar():
    print("test_the_positional_id_is_the_gd24_grammar")
    event = lg.parse_line("touch-mongo-live", 275, line(
        ts="2026-07-25T14:44:09.738Z", plan="research", stage="plan",
        state="failed", detail="loop exited -> synthesis"))
    key = lg.map_legacy_event(lg.LegacyEventObservation.from_event(event))[0][1]
    check(key == "legacy:touch-mongo-live#00000275",
          f"`legacy:<task>#<line:08d>`: {key}")
    check(ms.check_id("legacy_events", key) == key,
          "…and mongo_store agrees it came from refs.ref_key (SD-11)")
    check(refs.parse_ref_key("legacyEvent", key) == {"kind": "legacyEvent",
                                                     "task": "touch-mongo-live",
                                                     "lineNo": 275},
          "…round-tripping back to its components")

    nasty = "touch#recon|v2:stage%1"
    event = lg.parse_line(nasty, 7, line(ts="2026-07-25T00:00:00.000Z", plan="p",
                                         stage="s", state="done", detail="d"))
    key = lg.map_legacy_event(lg.LegacyEventObservation.from_event(event))[0][1]
    check(refs.parse_ref_key("legacyEvent", key)["task"] == nasty,
          f"GD-14's percent-escaping round-trips a task name holding `% # | :`: {key}")


def test_the_mirror_arm_is_idempotent_under_every_order():
    print("test_the_mirror_arm_is_idempotent_under_every_order")
    # GD-25's acceptance shape, applied to this arm: normal, shuffled, reversed
    # and doubled ingest must produce one identical fingerprint AND identical
    # counts (the count assertion is what catches silent collapse).
    events = load("touch-full-recon")
    ops = [op for event in events
           for op in lg.map_legacy_event(lg.LegacyEventObservation.from_event(
               event, source_path="touch-full-recon/events.jsonl"))]
    baseline = {}
    ms.apply_operations(baseline, ops)
    print_fp = ms.fingerprint(baseline)
    for label, order in (("shuffled", random.Random(7).sample(ops, len(ops))),
                         ("reversed", list(reversed(ops))),
                         ("doubled", ops + ops)):
        state = {}
        ms.apply_operations(state, order)
        check(ms.fingerprint(state) == print_fp and ms.counts(state) == ms.counts(baseline),
              f"{label} ingest ⇒ identical fingerprint and counts")


def test_the_mapper_writes_only_its_own_two_collections():
    print("test_the_mapper_writes_only_its_own_two_collections")
    check(lg.COLLECTIONS == ("legacy_events", "custom_state_events"),
          "legacy.py declares the two collections GD-24 gives the legacy arm")
    check(raises(lg.LegacyError, lg._only_ours, [("agents", "a" * 17, {})]),
          "an operation against a harness collection is refused structurally — "
          "a synthesized 8-hex identity never becomes an `agents` document")
    reduction = reduced("touch-mongo-live")
    kinds = {kind for kind, _obs in reduction.observations()}
    check(kinds == {"legacyEvent"},
          f"the reduction offers verbatim lines and nothing derived: {kinds}")


# --- R-51: the artifact registry -----------------------------------------
def test_the_artifact_registry_lists_the_folder_with_correct_digests():
    print("test_the_artifact_registry_lists_the_folder_with_correct_digests")
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "local-orchestrators")
        bodies = {
            "findings/research-a-attempt-1.md": "# findings\n",
            "plan/task-plan.md": "# plan\n",
            "report/uml.html": "<h1>report</h1>",
            "orch-scripts/implement.workflow.js": "// script\n",
            "orch-config.json": '{"port": 8931}',
            "events.jsonl": line(ts="2026-07-25T00:00:00.000Z", plan="p",
                                 stage="plan", state="done", detail="d"),
            "notes.txt": "loose file",
        }
        path = task_tree(root, "demo", files=tuple(bodies.items()))
        artifacts = lg.iter_artifacts(lg.task_folder(path), root=root)
        registry = {artifact.path: artifact for artifact in artifacts}
        check(set(registry) == set(bodies),
              f"every file in the folder is registered: {sorted(registry)}")
        for rel, body in bodies.items():
            expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
            check(registry[rel].sha256 == expected and registry[rel].size == len(body),
                  f"{rel}: digest and size are the file's own")
        kinds = {rel: artifact.kind for rel, artifact in registry.items()}
        check(kinds["findings/research-a-attempt-1.md"] == "findings"
              and kinds["plan/task-plan.md"] == "plan"
              and kinds["report/uml.html"] == "report"
              and kinds["orch-scripts/implement.workflow.js"] == "script"
              and kinds["orch-config.json"] == "config"
              and kinds["events.jsonl"] == "log"
              and kinds["notes.txt"] == "other",
              f"R-51's kinds come from the path alone: {kinds}")
        check(all(artifact.kind in lg.ARTIFACT_KINDS for artifact in artifacts),
              "…and every kind is in the declared set")

        ops = [op for artifact in artifacts for op in lg.map_artifact(artifact)]
        state = {}
        ms.apply_operations(state, ops)
        check(ms.counts(state) == {"custom_state_events": len(bodies)},
              f"one document per file: {ms.counts(state)}")
        doc = state["custom_state_events"][ops[0][1]]
        check(doc["kind"] == "artifact" and doc["provenance"] == "touch",
              "the document is a `custom_state_events` kind `artifact` (R-51)")
        check(set(doc["artifact"]) == {"kind", "path", "sha256", "size", "mtime"},
              f"…carrying paths and digests only: {sorted(doc['artifact'])}")
        # R-51's "paths + digests only, never bodies" is the whole point of the
        # registry, so it is asserted against the serialized state rather than
        # against the mapper's field list: a body smuggled into any nested key,
        # under any name, still shows up here.
        blob = json.dumps(state, default=str)
        check(not any(body in blob for body in bodies.values() if body.strip()),
              "…and never a file's body")


def test_an_artifact_id_is_stable_content_addressed_and_insert_only():
    print("test_an_artifact_id_is_stable_content_addressed_and_insert_only")
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "local-orchestrators")
        path = task_tree(root, "demo", files=(("findings/a.md", "one"),
                                              ("findings/b.md", "two")))
        folder = lg.task_folder(path)
        first = {artifact.path: artifact.slot for artifact in lg.iter_artifacts(folder)}
        again = {artifact.path: artifact.slot for artifact in lg.iter_artifacts(folder)}
        check(first == again, "a re-scan of an unchanged folder reproduces every slot")

        with open(os.path.join(path, "findings", "aa.md"), "w", encoding="utf-8") as h:
            h.write("inserted between a and b")
        after = {artifact.path: artifact.slot for artifact in lg.iter_artifacts(folder)}
        check(all(after[rel] == slot for rel, slot in first.items()),
              "…and adding a file renumbers NOTHING (a positional rank would "
              "silently re-point existing documents at other files)")

        ops = [op for artifact in lg.iter_artifacts(folder) for op in lg.map_artifact(artifact)]
        check(all(set(op[2]) == {"$setOnInsert"} for op in ops),
              "every field is $setOnInsert: no update, no delete, insert-only (R-52)")
        state = {}
        ms.apply_operations(state, ops)
        before = ms.fingerprint(state)
        ms.apply_operations(state, ops)
        check(ms.fingerprint(state) == before,
              "…so replaying the registry changes nothing at all (GD-25)")

        with open(os.path.join(path, "findings", "a.md"), "w", encoding="utf-8") as h:
            h.write("one, amended")
        changed = [artifact for artifact in lg.iter_artifacts(folder)
                   if artifact.path == "findings/a.md"][0]
        check(changed.slot != first["findings/a.md"],
              "a changed file is a new observation with a new `_id` …")
        ms.apply_operations(state, lg.map_artifact(changed))
        check(ms.counts(state)["custom_state_events"] == 4,
              "…appended beside the old one, which is what append-only means")


def test_the_artifact_stream_id_round_trips_any_folder_name():
    print("test_the_artifact_stream_id_round_trips_any_folder_name")
    for task in ("touch-mongo-live", "touch#recon|v2:stage%1", "a b/c", "ünïcode",
                 "100%-done"):
        stream = lg.artifact_stream(task)
        check(lg.task_of_artifact_stream(stream) == task,
              f"{task!r} ⇒ {stream!r} ⇒ back")
        key = refs.custom_state_event_key(stream, 42)
        check(ms.check_id("custom_state_events", key) == key,
              f"…and the `_id` built from it is canonical: {key}")
        check(refs.parse_ref_key("customStateEvent", key)["stream"] == stream,
              "…and parses back to the stream id")
    check(raises(lg.LegacyError, lg.artifact_stream, "x" * 400),
          "a task name too long to key is refused, never truncated into a "
          "colliding id")
    check(raises(lg.LegacyError, lg.task_of_artifact_stream, "session:x"),
          "…and a stream that is not an artifact stream is not decoded as one")


# --- the SD-1 seam --------------------------------------------------------
def test_the_sources_own_their_paths_and_nothing_else():
    print("test_the_sources_own_their_paths_and_nothing_else")
    with tempfile.TemporaryDirectory() as tmp:
        # The one root in this file shaped like the DEFAULT rather than a bare
        # fixture name — so it is spelled through the suite's single flip point
        # (`_roots.ORCH_REL`) instead of a literal that a move leaves behind.
        root = os.path.join(tmp, *ORCH_REL.parts)
        os.makedirs(root)
        path = task_tree(root, "demo", events=line(
            ts="2026-07-25T00:00:00.000Z", plan="p", stage="plan", state="done",
            detail="d"), files=(("findings/a.md", "note"),))
        events = lg.iter_legacy_event_observations(root=root)
        check(len(events) == 1 and events[0].task == "demo",
              "the rebuild arm (path=None) walks the orchestrator root")
        check(events[0].source_path == "demo/events.jsonl",
              f"…recording a ROOT-RELATIVE source path: {events[0].source_path!r} "
              f"(an absolute one would make the fingerprint machine-dependent)")
        artifacts = lg.iter_artifact_observations(root=root)
        check({artifact.path for artifact in artifacts} == {"events.jsonl",
                                                            "findings/a.md"},
              "…and the registry arm walks the same tree")

        own = os.path.join(path, lg.EVENTS_FILE)
        check(len(lg.iter_legacy_event_observations(own, root=root)) == 1,
              "handed its own file, the source answers for it")
        for foreign in ("/home/x/.claude/projects/slug/session.jsonl",
                        "/home/x/.claude/projects/slug/s/subagents/journal.jsonl",
                        os.path.join(tmp, "elsewhere", "events.jsonl")):
            check(lg.iter_legacy_event_observations(foreign, root=root) == []
                  and lg.iter_artifact_observations(foreign, root=root) == [],
                  f"…and returns nothing for a path it does not own: {foreign}")
        check(lg.is_legacy_stream_path(own)
              and not lg.is_legacy_stream_path(os.path.join(path, "findings", "a.md")),
              "ownership is decided from the path alone — one basename comparison, "
              "never a read (mirror.iter_backfill_observations' contract)")


def test_the_registry_matches_the_mirror_contract():
    print("test_the_registry_matches_the_mirror_contract")
    check(set(lg.MIRROR_MAPPERS) == {"legacyEvent", "legacyArtifact"},
          f"SD-1's registry: {sorted(lg.MIRROR_MAPPERS)}")
    check(set(lg.MIRROR_SOURCES) == set(lg.MIRROR_MAPPERS),
          "every mapped kind has a source (and no source is unmapped)")
    check("artifact" not in lg.MIRROR_MAPPERS,
          "the observation kind is `legacyArtifact`: `artifact` is also one of "
          "R-52's document kinds, and discover_mappers refuses a shared kind")
    from aggregator import mirror as mr                          # noqa: PLC0415
    registry = mr.discover_mappers()
    check({"legacyEvent", "legacyArtifact"} <= set(registry),
          "…and mirror.discover_mappers finds both without a change on its side")
    check(mr.map_observation(registry, "legacyEvent", lg.LegacyEventObservation(
        task="t", line_no=1, provenance="unknown")),
          "…and drives them through its own validation")


def test_the_mapping_half_is_pure():
    print("test_the_mapping_half_is_pure")
    # SD-1: mappers do no I/O and read no clock. `tests/test_mirror.py` walks
    # every entity module for the driver import; this walks the mappers
    # themselves, which is the half a package-name grep cannot see.
    source = (SRC / "aggregator" / "legacy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned_calls = {"open", "print", "input"}
    banned_attrs = ("os.", "time.", "random.", "subprocess.", "socket.")
    pure = [node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and (node.name.startswith(("map_", "_split_ops", "provenance_of",
                                       "artifact_slot", "artifact_stream")))]
    check(len(pure) >= 5, f"found {len(pure)} functions on the pure side")
    for node in pure:
        called = {sub.func.id for sub in ast.walk(node)
                  if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)}
        check(not called & banned_calls,
              f"{node.name} calls nothing that touches the world "
              f"{sorted(called & banned_calls)}")
        attrs = {f"{sub.value.id}.{sub.attr}" for sub in ast.walk(node)
                 if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)}
        check(not any(attr.startswith(banned_attrs) for attr in attrs),
              f"{node.name} reads neither the filesystem nor the clock "
              f"{sorted(attr for attr in attrs if attr.startswith(banned_attrs))}")
    # The "no clock" half is asserted over the AST rather than over the source
    # text, because a substring search for `time.time` also matches
    # `datetime.timezone` — which this module legitimately uses to stamp a
    # file's own mtime — and a guard that fires on a correct line is a guard
    # somebody deletes. Both halves are checked: no clock is called, and the
    # `time` module is never imported (so no alias can smuggle one in).
    clock_calls = {"time.time", "time.time_ns", "time.monotonic",
                   "time.monotonic_ns", "time.perf_counter",
                   "datetime.datetime.now", "datetime.datetime.today",
                   "datetime.datetime.utcnow", "datetime.date.today"}

    def dotted(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        parts.append(node.id)
        return ".".join(reversed(parts))

    ticking = sorted({name for sub in ast.walk(tree)
                      if isinstance(sub, ast.Call)
                      for name in (dotted(sub.func),)
                      if name in clock_calls})
    imports_time = any(
        (isinstance(sub, ast.Import)
         and any(alias.name == "time" or alias.name.startswith("time.")
                 for alias in sub.names))
        or (isinstance(sub, ast.ImportFrom) and sub.module == "time")
        for sub in ast.walk(tree))
    check(not ticking and not imports_time,
          "the module has no clock at all: liveness is read-time, and it is the "
          f"reducer's (GD-23), so two clocks can never disagree {ticking}")


def test_the_reduction_is_a_pure_function_of_the_lines():
    print("test_the_reduction_is_a_pure_function_of_the_lines")
    events = load("touch-mongo-live")
    one = lg.reduce_events(events, task="touch-mongo-live")
    two = lg.reduce_events(list(events), task="touch-mongo-live")
    check(one.stats == two.stats, "two reductions of the same lines agree exactly")
    check([node.state for node in one.nodes] == [node.state for node in two.nodes],
          "…node for node")
    check(one.stats["lines"] == len(events) and one.stats["parse_errors"] == 0,
          "…and the stats account for every line")


def test_a_whole_root_scan_reduces_every_folder():
    print("test_a_whole_root_scan_reduces_every_folder")
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "local-orchestrators")
        os.makedirs(root)
        for name in AFFECTED:
            path = task_tree(root, name, events="")
            shutil.copyfile(stream_path(name), os.path.join(path, lg.EVENTS_FILE))
        task_tree(root, "plan-only", files=(("plan/p.md", "# p"),))
        reductions = {one.task: one for one in lg.scan(root)}
        check(set(reductions) == set(AFFECTED) | {"plan-only"},
              f"every folder is reduced, plan-only included: {sorted(reductions)}")
        for name in AFFECTED:
            check("failed" not in {reductions[name].badge_of(plan)
                                   for plan in ("research", "synthesis")},
                  f"{name}: still zero fabricated failures through the folder path")
        check(reductions["plan-only"].kind == "plan-only",
              "…and the plan-only folder keeps its kind through the scan")

        env = {"TOUCH_LEGACY_ROOT": root}
        check(lg.orchestrator_root(env=env) == os.path.abspath(root),
              "$TOUCH_LEGACY_ROOT selects the root for a daemon started anywhere")
        check(lg.orchestrator_root().endswith(lg.TASK_ROOT),
              "…and the default is the repo's own history folder")
        check(lg.TASK_ROOT == os.path.join(*ORCH_REL.parts),
              f"…which is `{os.path.join(*ORCH_REL.parts)}`, the suite's one "
              f"spelling of those two components (got {lg.TASK_ROOT!r})")
        # PROTOCOL-8: this used to be a SECOND resolver — it read
        # `$TOUCH_LEGACY_ROOT` and the daemons/hook read `$ORCH_TASKS_ROOT`, so
        # one cwd could give the dashboard and the API two different task lists.
        # It is now `paths.tasks_root` under another name, which is what makes
        # `touch-selfcheck`'s "they must agree" check able to pass at all.
        other = os.path.join(tmp, "elsewhere")
        check(lg.orchestrator_root(env={"ORCH_TASKS_ROOT": other})
              == os.path.abspath(other),
              "$ORCH_TASKS_ROOT — which this resolver used to ignore — is "
              "honoured, so the adapter and the daemons agree")
        check(lg.orchestrator_root(env={"ORCH_TASKS_ROOT": other,
                                        "TOUCH_LEGACY_ROOT": root})
              == os.path.abspath(other),
              "…with the stated precedence when both are set")
        check(lg.orchestrator_root(env=env)
              == paths.tasks_root(env=env),
              "orchestrator_root() is paths.tasks_root() under the adapter's "
              "own name — one ladder, not two that can drift")


def main():
    for test in (
        test_every_frozen_line_parses_and_keeps_its_position,
        test_an_anchored_line_reads_exactly_as_the_anchor_says,
        test_a_broken_line_is_kept_and_a_torn_tail_is_not,
        test_an_unparseable_ts_does_not_lose_the_line,
        test_out_of_enum_states_map_to_info_and_are_never_dropped,
        test_provenance_follows_the_no_guess_rule_and_the_anchored_counts,
        test_the_w_field_wins_over_the_shape_rules,
        test_the_two_wave_respawn_becomes_distinct_ordinals,
        test_a_status_sh_running_row_and_the_watcher_spawn_are_one_node,
        test_agent_ids_are_namespaced_and_both_widths_join,
        test_a_run_id_is_synthesized_when_the_config_does_not_name_one,
        test_a_synthetic_run_id_can_never_join_a_harness_run,
        test_the_plans_denominator_comes_from_the_dividers_own_result,
        test_the_denominator_fold_is_the_monotonic_max_it_always_was,
        test_the_derived_denominator_is_reconciled_into_the_reduction,
        test_the_fabricated_failed_badge_becomes_closed_no_verdict,
        test_a_genuine_failure_keeps_its_badge,
        test_sd4_last_event_wins_on_conflicting_plan_terminals,
        test_r58_zero_failed_badges_on_the_three_affected_streams,
        test_an_unresulted_sibling_blocks_the_relabel,
        test_the_terminal_complete_closes_phantom_agents_stale,
        test_a_mid_stream_complete_does_not_close_a_later_invocation,
        test_duplicate_stage_terminals_dedupe_and_keep_the_agents_words,
        test_watcher_wins_is_only_for_same_state_duplicates,
        test_the_token_fold_is_lossless_and_bounded,
        test_a_token_line_that_cannot_fold_losslessly_is_kept_whole,
        test_the_token_rollup_equals_the_streams_own_delta_sum,
        test_the_rollup_neither_drops_nor_doubles_an_unattributable_line,
        test_the_rollup_replica_still_matches_the_page,
        test_a_plan_only_folder_is_its_own_kind_with_no_controls,
        test_the_archive_label_is_derived_not_constant,
        test_a_broken_config_does_not_break_the_folder,
        test_the_watcher_checkpoint_is_never_read,
        test_n_documents_for_n_lines_including_the_identical_ones,
        test_the_positional_id_is_the_gd24_grammar,
        test_the_mirror_arm_is_idempotent_under_every_order,
        test_the_mapper_writes_only_its_own_two_collections,
        test_the_artifact_registry_lists_the_folder_with_correct_digests,
        test_an_artifact_id_is_stable_content_addressed_and_insert_only,
        test_the_artifact_stream_id_round_trips_any_folder_name,
        test_the_sources_own_their_paths_and_nothing_else,
        test_the_registry_matches_the_mirror_contract,
        test_the_mapping_half_is_pure,
        test_the_reduction_is_a_pure_function_of_the_lines,
        test_a_whole_root_scan_reduces_every_folder,
    ):
        test()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("all legacy (R-27 / R-51 / R-58 read-time) tests passed")


if __name__ == "__main__":
    main()
