#!/usr/bin/env python3
"""Stdlib-only tests for aggregator/agents.py — the node/graph join (R-28) and
the fragment/spawn assembly (R-48). The reducer half (R-54) is
`test_reducer.py`; both files test one module because R-54 names `agents.py`
"the reducer home" and one file, one owner (GD-15).

Run as `python3 test_agents.py`; exits non-zero on failure. No pytest, no runner.

R-28's and R-48's own test lists are the spine:

* adversarial marker fixture (line-1 `[touch]`, line-2 `[monitor]`, quoted
  markers in the body); a node with no marker at all; parent edges from
  `parent=` (R-28);
* ingest the two `a2fc883c96ff7b837` fixture files in BOTH orders ⇒ ONE
  document, two fragments in chain order, `firstTs 02:59:29.846Z`, token
  rollup = union; mutate the file ⇒ fileHint stale while the recordUuid lookup
  still resolves; a missing-meta fragment does not throw (R-48).

Plus the invariants only a test can hold in place:

* `sessionId` is never a grouping key — asserted on the live cross-session
  pair AND as a wall (`_only_ours`), because the two files that prove it are
  17 minutes apart in two session directories and a regression here produces
  two half-agents that both look plausible;
* GD-25's algebra over the whole frozen corpus: normal / shuffled / reversed
  ingest ⇒ identical fingerprint AND identical counts;
* the live-tail property the storage split exists for: re-observing a growing
  fragment adds ONE `fragments[]` element, not one per tick — and the two
  observations **commute**, because `mirror.py` batches one `_id`'s updates
  unordered and re-queues unwritten ones at the tail. Same for the two
  observations of a spawn whose parent transcript grew underneath it, and the
  same for a fragment observed before its first record is readable (which adds
  no element at all rather than a second, differently-shaped one);
* `--rebuild` and `--backfill` store the byte-identical document when two
  fragments' `.meta.json` and markers disagree: `assemble` has no conflict rule
  of its own, it folds through the store's `$min` (R-56's equivalence arm);
* every counter `_skips()` declares has a firing case, so "nothing was skipped"
  is an assertion and not a hope;
* SD-1 purity — the mappers do no I/O, read no clock, write no collection but
  `agents`, and build only operations that commute (no `$set`);
* the GD-9 grammar here and `decision_watcher.py`'s agree on real prompts, on
  real corpus text. Two copies of one grammar that drift are two grammars.
"""

import ast
import dataclasses
import datetime
import importlib.util
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
from _roots import MON, SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))

from aggregator import agents                                   # noqa: E402
from aggregator import ingest                                   # noqa: E402
from aggregator import mongo_store as ms                        # noqa: E402
from aggregator import refs                                     # noqa: E402
from aggregator import sessions as sess                         # noqa: E402
from aggregator.agents import (                                 # noqa: E402
    AgentObservation,
    AgentsError,
    Fragment,
    SpawnObservation,
    assemble,
    check_file_hint,
    file_hint,
    find_spawns,
    fragments_of,
    is_agent_id,
    labels_from_prompt,
    map_agent,
    map_agent_spawn,
    marker_window,
    node_key,
    order_fragments,
    parse_markers,
    read_fragment,
    read_meta,
    scan,
    spawn_record_filter,
    touch_marker_misplaced,
)

failures = []
skips = []

CORPUS = HERE / "fixtures" / "run-wf_829e6f58"
AGENT = "a2fc883c96ff7b837"
BIG = CORPUS / "dd469822-2546-47d9-aaa3-31db4cb705e8" / "subagents" / "workflows" / \
    "wf_829e6f58-b2f" / f"agent-{AGENT}.jsonl"
SMALL = CORPUS / "e423cd3c-f859-45af-9afd-0d6bdec9b4ac" / "subagents" / "workflows" / \
    "wf_829e6f58-b2f" / f"agent-{AGENT}.jsonl"
FIRST_TS = "2026-07-25T02:59:29.846Z"

#: The cwd every rooted test claims to run in, and the slug the CLI derives
#: from it. The per-path (`--backfill`) ownership test is
#: `sessions.scoped_dirs`, so a test that hands a source a path must put that
#: path under the slug of the cwd it passes — exactly as the real thing does.
#: `test_ingest.py` states the same two constants for the same reason.
OWNED_CWD = "/home/laniakea/Projects/touch"
OWNED_SLUG = sess.slug_for(OWNED_CWD)
FOREIGN_SLUG = "-tmp-claude-1000-liveio"
FIXTURE_SESSION = "dd469822-2546-47d9-aaa3-31db4cb705e8"


def linked_root(tmp, *slugs):
    """A `~/.claude`-shaped root whose project slugs symlink at :data:`CORPUS`.

    The frozen corpus is not laid out under `projects/<slug>/`, and the scope
    test is deliberately *rooted* (the anchor must BE `<root>/projects/<slug>`,
    not merely be named like one), so a test of the per-path arm has to build
    the real shape. Symlinked rather than copied: `os.walk` never descends here.
    """
    root = os.path.join(tmp, "claude")
    os.makedirs(os.path.join(root, "projects"), exist_ok=True)
    for slug in slugs:
        os.symlink(os.fspath(CORPUS), os.path.join(root, "projects", slug))
    sess.reset_scope_cache()
    return root


def slug_agent_path(root, slug, session=FIXTURE_SESSION):
    return os.path.join(root, "projects", slug, session, "subagents", "workflows",
                        "wf_829e6f58-b2f", f"agent-{AGENT}.jsonl")


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  SKIP: {msg}")
    skips.append(msg)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception as other:                                  # noqa: BLE001
        print(f"    (raised {type(other).__name__}: {other})")
        return False
    return False


def corpus_agent_paths():
    out = []
    for base, dirnames, filenames in os.walk(CORPUS):
        dirnames.sort()
        for name in sorted(filenames):
            if ingest.agent_id_for_path(name):
                out.append(os.path.join(base, name))
    return out


def build(fragments):
    """Apply `map_agent` for each fragment group, in the order given."""
    state = {}
    for group in fragments:
        ms.apply_operations(state, map_agent(assemble(list(group))))
    return state


# --- GD-9: the marker layer ----------------------------------------------


ADVERSARIAL = (
    "\n"
    "[touch] name=critic-2 parent=impl-1 root=touch-mongo role=critique attempt=3\n"
    "[monitor] plan=sp-agents-reducer stage=critique role=critic attempt=3 model=opus\n"
    "You are the CRITIC for sub-plan sp-agents-reducer, a fresh subagent.\n"
    "Quoted verbatim from the findings file you must read:\n"
    "    [monitor] plan=WRONG stage=WRONG role=WRONG attempt=99\n"
    "    [touch] name=not-me parent=nobody\n"
    "and the token [touch] appears in prose too.\n"
)


def test_the_marker_window_is_four_lines_and_leading_blanks_are_tolerated():
    print("test_the_marker_window_is_four_lines_and_leading_blanks_are_tolerated")
    window = marker_window(ADVERSARIAL)
    check(window.count("\n") == 3, "the window is exactly four physical lines")
    check("WRONG" not in window,
          "…and the quoted marker below it is outside (12 such files exist on disk)")

    monitor, touch = parse_markers(ADVERSARIAL)
    check(monitor and monitor["plan"] == "sp-agents-reducer",
          f"the real [monitor] marker parses: {monitor and monitor.get('plan')!r}")
    check(monitor and monitor["attempt"] == "3" and monitor["model"] == "opus",
          "…with unknown keys (model=) kept, so additions stay compatible (GD-9)")
    check(touch and touch["name"] == "critic-2" and touch["parent"] == "impl-1",
          "the [touch] identity marker parses on line 1")

    labels = labels_from_prompt(ADVERSARIAL)
    check(labels.plan == "sp-agents-reducer" and labels.attempt == 3,
          "labels_from_prompt merges both markers; attempt is an int")
    check(labels.name == "critic-2" and labels.parent == "impl-1" and labels.root == "touch-mongo",
          "…and carries R-28's parent edge from parent=")
    check(labels.unconventional is False, "a named agent is not `unconventional`")
    check(labels.extra == {"model": "opus"},
          f"unknown keys land in `extra`, not in the label set: {labels.extra}")

    # A marker with a payload BELOW the window is a real misplacement; the bare
    # token in prose is not.
    check(touch_marker_misplaced(ADVERSARIAL) is True,
          "a real [touch] marker below the window flags markerMisplaced (GD-9)")
    prose = "line1\nline2\nline3\nline4\nthe [touch] skill is documented in SKILL.md\n"
    check(touch_marker_misplaced(prose) is False,
          "…while prose that merely names the token is not a misplaced marker")


def test_two_markers_on_one_line_both_parse():
    print("test_two_markers_on_one_line_both_parse")
    monitor, touch = parse_markers("[touch] name=a root=r [monitor] plan=p role=impl attempt=1\n")
    check(monitor and monitor.get("plan") == "p", "the second marker on the line parses")
    check(touch and touch.get("name") == "a" and "plan" not in touch,
          "…and the first one's payload is cut at its own marker, not at end of line")


def test_the_grammar_matches_decision_watchers_on_a_real_prompt():
    print("test_the_grammar_matches_decision_watchers_on_a_real_prompt")
    module = MON / "decision_watcher.py"
    if not module.exists():                                     # pragma: no cover
        skip("decision_watcher.py is not in this checkout")
        return
    spec = importlib.util.spec_from_file_location("dw_under_test", module)
    watcher = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(watcher)
    except Exception as exc:                                    # noqa: BLE001
        skip(f"decision_watcher.py did not import ({type(exc).__name__}: {exc})")
        return

    # Real prompt text from the frozen corpus, plus the adversarial fixture.
    scan_ = ingest.read_transcript(str(BIG), root=str(CORPUS))
    real = agents.prompt_text_of(sorted(scan_.records, key=lambda r: r.line_no)[0].body)
    check(real.lstrip().startswith("[monitor]"),
          "the frozen corpus's own spawn prompt opens with a [monitor] marker")
    for name, text in (("the real prompt", real), ("the adversarial fixture", ADVERSARIAL)):
        check(watcher.marker_window(text) == marker_window(text),
              f"the two windows agree on {name}")
        check(watcher.parse_markers(text) == parse_markers(text),
              f"…and the two parsers return the same fields on {name}")
        check(watcher.touch_marker_misplaced(text) == touch_marker_misplaced(text),
              f"…and agree on markerMisplaced for {name}")


def test_a_node_exists_with_no_marker_at_all(tmp=None):
    print("test_a_node_exists_with_no_marker_at_all")
    fragment = Fragment(agent_id="b" * 17, session_id="s1", path="s1/agent-bb.jsonl",
                        first_uuid="u1", last_uuid="u2", line_count=2, record_count=2)
    obs = assemble([fragment])
    check(obs.labels is None, "a markerless fragment carries no labels")
    check(obs.unconventional is True,
          "…and the agent is flagged `unconventional` (R-28: the common case today)")
    collection, key, update = map_agent(obs)[0]
    check((collection, key) == ("agents", "b" * 17),
          "…and a document exists anyway: harness facts create nodes, markers only label (GD-7)")
    doc = ms.apply_update(None, update, _id=key, collection="agents")
    check(doc.get("unconventional") is True, "…carrying the flag the UI renders the agentId for")
    check("name" not in doc and "labels" not in doc,
          "…and no invented name: a missing marker degrades the label, never the node")


def test_labels_are_a_layer_never_an_identity():
    print("test_labels_are_a_layer_never_an_identity")
    check(node_key(agent_id=AGENT) == refs.agent_key(AGENT),
          "an Agent-tool node is identified by its full 17-hex agentId (GD-7)")
    check(node_key(run_id="wf_x", key="impl", ordinal=2) == "wf_x|impl|0002",
          "a Workflow node is identified by (runId, key, ordinal) (GD-7)")
    check(raises(AgentsError, node_key, agent_id="a2fc883c"),
          "an 8-hex legacy id is refused here — it is namespaced legacy:<task>:<id8> (GD-11)")
    check(raises(AgentsError, node_key, run_id="wf_x", key="impl"),
          "…and a node with neither identity is a caller bug, not a nameless node")
    check(is_agent_id(AGENT) and not is_agent_id("A2FC883C96FF7B837"),
          "identity hex has exactly one spelling (lowercase, 17 chars)")

    # NIT 6 of attempt 3's critique: `node_ref`/`node_key` were exported and
    # documented as GD-7's identity while every real `_id` came from
    # `refs.agent_key` — an identity function nothing on the write path called,
    # so no test of it proved anything about a stored document. Both mappers
    # now key through it, asserted as source AND as behaviour.
    tree = ast.parse((SRC / "aggregator" / "agents.py").read_text(encoding="utf-8"))
    for name in ("map_agent", "map_agent_spawn"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        calls = {n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        check("node_key" in calls,
              f"{name} keys its upsert through the exported identity: {sorted(calls)}")
    keyed = map_agent(AgentObservation(agent_id=AGENT))[0][1]
    spawned = map_agent_spawn(SpawnObservation(agent_id=AGENT, record_uuid="u"))[0][1]
    check(keyed == spawned == node_key(agent_id=AGENT) == refs.agent_key(AGENT),
          f"…and the two writers of one document agree on its _id: {keyed!r}")


# --- R-48: fragments, chain order, union writes ---------------------------


def test_the_cross_session_pair_is_one_agent_in_chain_order():
    print("test_the_cross_session_pair_is_one_agent_in_chain_order")
    big = read_fragment(str(BIG), root=str(CORPUS))
    small = read_fragment(str(SMALL), root=str(CORPUS))
    check(big.line_count == 223 and small.line_count == 2,
          f"the frozen pair is 223 + 2 lines ({big.line_count} + {small.line_count})")
    check(big.session_id != small.session_id,
          "…in two different session directories (the /clear mid-run shape)")
    check(small.first_parent_uuid == big.last_uuid,
          "…stitched by parentUuid -> uuid: the small one continues the big one")

    for order, name in (((big, small), "directory order"), ((small, big), "reversed")):
        obs = assemble(list(order))
        check(len(obs.fragments) == 2, f"one agent, two fragments ({name})")
        check([f.session_id for f in obs.fragments] == [big.session_id, small.session_id],
              f"…in CHAIN order, not {name}")
        check(ms.ts_fields(obs.first_ts)["tsRaw"] == FIRST_TS,
              f"…firstTs is the chain head's {FIRST_TS} ({name})")

    state_a = build([[big], [small]])
    state_b = build([[small], [big]])
    state_c = build([[big, small]])
    keys = set(state_a["agents"]) | set(state_b["agents"]) | set(state_c["agents"])
    check(keys == {AGENT}, f"ONE document for both files, however they arrive: {sorted(keys)}")
    check(ms.fingerprint(state_a) == ms.fingerprint(state_b) == ms.fingerprint(state_c),
          "…byte-identical whichever order they were observed in (GD-25)")

    doc = state_a["agents"][AGENT]
    check(doc["sessions"] == sorted({big.session_id, small.session_id}),
          "sessions[] is the union of both directories ($addToSet)")
    check(len(doc["files"]) == 2, "files[] is the union of both paths")
    ordered = fragments_of(doc)
    check([f["sessionId"] for f in ordered] == [big.session_id, small.session_id],
          "fragments_of() re-derives chain order from the STORED document")
    check(all({"sessionId", "path", "firstUuid", "lastUuid", "lineCount"} <= set(f)
              for f in ordered),
          "…in exactly R-48's shape, so the storage split is invisible to a reader")
    check([f["lineCount"] for f in ordered] == [223, 2],
          f"…with the tips recombined: {[f['lineCount'] for f in ordered]}")


def test_the_meta_bearing_fragment_wins_without_seeing_the_other():
    print("test_the_meta_bearing_fragment_wins_without_seeing_the_other")
    big = read_fragment(str(BIG), root=str(CORPUS))
    small = read_fragment(str(SMALL), root=str(CORPUS))
    check(big.has_meta and not small.has_meta,
          "the live pair IS the case: the 2-line continuation has no .meta.json")
    check(assemble([small]).agent_type is None,
          "a missing-meta fragment does not throw and invents nothing (R-48)")
    for order in ((big, small), (small, big)):
        doc = ms.apply_operations({}, [])
        for fragment in order:
            ms.apply_operations(doc, map_agent(assemble([fragment])))
        stored = doc["agents"][AGENT]
        check(stored.get("agentType") == "workflow-subagent" and stored.get("model") == "opus",
              f"…and the meta survives observation order {[f.line_count for f in order]}")


GROWING_AGENT = "c" * 17
GROWING_FIRST = "11111111-1111-4111-8111-111111111111"
GROWING_SECOND = "22222222-2222-4222-8222-222222222222"


def growing_fragment(root):
    """One `agent-<id>.jsonl` and the observation that appends a line to it.

    Returns `(path, append)`: the file holds one record, and calling `append()`
    adds the second. The live-tail shape the storage split exists for — a poll
    tick re-reads a file that grew since the last one.
    """
    directory = Path(root) / "projects" / "slug" / "sess-1" / "subagents" / "workflows" / "wf_1"
    directory.mkdir(parents=True)
    path = directory / f"agent-{GROWING_AGENT}.jsonl"
    template = json.loads(BIG.read_text(encoding="utf-8").split("\n", 1)[0])
    first = dict(template, uuid=GROWING_FIRST, parentUuid=None)
    second = dict(template, uuid=GROWING_SECOND, parentUuid=GROWING_FIRST,
                  type="assistant", timestamp="2026-07-25T03:00:00.000Z")
    path.write_text(json.dumps(first) + "\n", encoding="utf-8")

    def append():
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(second) + "\n")
    return path, append


def test_a_growing_fragment_adds_one_element_not_one_per_tick():
    print("test_a_growing_fragment_adds_one_element_not_one_per_tick")
    # The live-tail property the storage split exists for (module docstring).
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path, append = growing_fragment(root)
        early = map_agent(assemble([read_fragment(str(path), root=str(root))]))
        append()
        late = map_agent(assemble([read_fragment(str(path), root=str(root))]))

        state = ms.apply_operations(ms.apply_operations({}, early), late)
        doc = state["agents"][GROWING_AGENT]
        check(len(doc["fragments"]) == 1,
              f"two observations of one growing file leave ONE fragment element, "
              f"got {len(doc['fragments'])} (a per-tick element is 4/s of unbounded array)")
        check(fragments_of(doc)[0]["lineCount"] == 2,
              "…and the tip advanced instead, so R-48's reader shape reports it")

        # BLOCKER 1 of attempt 1's critique: the tip must survive being applied
        # in the OTHER order. `mirror._take_batches` keeps two updates of one
        # `_id` as two operations in one unordered bulk, and `mirror._requeue`
        # appends unwritten ones at the tail — both justified by "the algebra
        # commutes". A `$set` leaf does not, and the reversed order used to
        # store line 1's uuid beside `lineCount: 2`.
        reversed_state = ms.apply_operations(ms.apply_operations({}, late), early)
        check(ms.fingerprint(reversed_state) == ms.fingerprint(state),
              "…and the two observations commute: identical fingerprint in either "
              "application order (GD-25 — mongod applies an unordered bulk as it likes)")
        for name, built in (("forward", state), ("reversed", reversed_state)):
            tip = fragments_of(built["agents"][GROWING_AGENT])[0]
            check(tip["lineCount"] == 2 and tip["lastUuid"] == GROWING_SECOND,
                  f"…with a COHERENT tip in {name} order: the uuid belongs to the "
                  f"line lineCount counts to ({tip.get('lineCount')}, "
                  f"{tip.get('lastUuid')})")
        stored_tip = list(doc["fragmentTips"].values())[0]
        check("lastUuid" not in stored_tip,
              f"…because no bare lastUuid is STORED — it rides inside lastMark, whose "
              f"order is the monotone counter's: {sorted(stored_tip)}")
        check(agents.tip_uuid(stored_tip["lastMark"]) == GROWING_SECOND,
              "…which unpacks back to the uuid R-48 names")
        check("lastMark" not in fragments_of(doc)[0],
              "…and the encoding never escapes fragments_of(): readers see lastUuid")


def backdated_fragment(root):
    """The same growing file, but the appended record is stamped EARLIER.

    Not a doctored curiosity: of the 177 transcripts with ≥2 timestamps on this
    machine, 27 are non-monotonic and 20 have `min(ts) != ts[0]` — the harness
    writes records out of order. The shape matters because `TranscriptScan`'s
    `first_ts` is the MINIMUM over the file, so this append moves it, while the
    first record's own timestamp cannot move.
    """
    directory = Path(root) / "projects" / "slug" / "sess-1" / "subagents" / "workflows" / "wf_1"
    directory.mkdir(parents=True)
    path = directory / f"agent-{GROWING_AGENT}.jsonl"
    template = json.loads(BIG.read_text(encoding="utf-8").split("\n", 1)[0])
    first = dict(template, uuid=GROWING_FIRST, parentUuid=None,
                 timestamp="2026-07-25T03:00:10.000Z")
    second = dict(template, uuid=GROWING_SECOND, parentUuid=GROWING_FIRST,
                  type="assistant", timestamp="2026-07-25T03:00:05.000Z")
    path.write_text(json.dumps(first) + "\n", encoding="utf-8")

    def append():
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(second) + "\n")
    return path, append


def test_an_out_of_order_timestamp_does_not_duplicate_a_fragment():
    print("test_an_out_of_order_timestamp_does_not_duplicate_a_fragment")
    # MAJOR 1 of attempt 2's critique. `fragments[]` is an `$addToSet` of the
    # fragment's IDENTITY, and the whole justification for that (D-1) is that
    # every member is a property of the first record, which append cannot
    # change. `firstTs` was `TranscriptScan.first_ts` — the minimum over the
    # WHOLE file — so one appended record stamped before the current minimum
    # rewrote the identity and `$addToSet` kept both spellings of one file,
    # permanently (GD-26 forbids the delete that would repair it).
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path, append = backdated_fragment(root)
        before = read_fragment(str(path), root=str(root))
        early = map_agent(assemble([before]))
        append()
        after = read_fragment(str(path), root=str(root))
        late = map_agent(assemble([after]))

        check(ms.ts_fields(after.first_ts)["tsRaw"] != ms.ts_fields(before.first_ts)["tsRaw"],
              f"the append genuinely moved the scan minimum: "
              f"{ms.ts_fields(before.first_ts)['tsRaw']} -> "
              f"{ms.ts_fields(after.first_ts)['tsRaw']}")
        check(after.first_record_ts == before.first_record_ts,
              "…while the FIRST RECORD's own timestamp did not move, because a "
              "record already written cannot be rewritten by an append")
        check(before.identity() == after.identity(),
              f"…so the identity is byte-identical across the two observations: "
              f"{sorted(before.identity())}")

        state = ms.apply_operations(ms.apply_operations({}, early), late)
        doc = state["agents"][GROWING_AGENT]
        check(len(doc["fragments"]) == 1,
              f"ONE element for one file even when a later record is stamped "
              f"earlier than the first: {doc['fragments']}")
        check(len(fragments_of(doc)) == 1,
              f"…so sp-12/sp-13 are handed the file once, not twice: "
              f"{len(fragments_of(doc))}")
        check(fragments_of(doc)[0]["lineCount"] == 2,
              "…with the tip advanced, exactly as in the monotone case")
        check(ms.ts_fields(doc["firstTs"])["tsRaw"] == "2026-07-25T03:00:05.000Z",
              f"…while the AGENT's firstTs is still the minimum — that one is `$min` "
              f"and wants it: {ms.ts_fields(doc['firstTs'])['tsRaw']}")
        reversed_state = ms.apply_operations(ms.apply_operations({}, late), early)
        check(ms.fingerprint(reversed_state) == ms.fingerprint(state),
              "…and the two observations still commute (GD-25)")

    # A document written by the PREVIOUS shape already holds both spellings,
    # and GD-26 forbids the delete that would remove one. The reader repairs it
    # — the only place it can be repaired — so sp-12 and sp-13 see one file once
    # whatever the collection holds.
    legacy_doc = {
        "_id": GROWING_AGENT,
        "fragments": [
            {"sessionId": "s", "path": "p", "firstUuid": GROWING_FIRST,
             "firstTs": datetime.datetime(2026, 7, 25, 3, 0, 10,
                                          tzinfo=datetime.timezone.utc)},
            {"sessionId": "s", "path": "p", "firstUuid": GROWING_FIRST,
             "firstTs": datetime.datetime(2026, 7, 25, 3, 0, 5,
                                          tzinfo=datetime.timezone.utc)},
        ],
        "fragmentTips": {GROWING_FIRST: {"lineCount": 2, "records": 2,
                                         "lastMark": agents.tip_mark(2, GROWING_SECOND)}},
    }
    repaired = fragments_of(legacy_doc)
    check(len(repaired) == 1,
          f"a document that already holds both spellings still reads as ONE "
          f"fragment: {len(repaired)}")
    check(repaired[0]["lastUuid"] == GROWING_SECOND and repaired[0]["lineCount"] == 2,
          "…with its tip intact, because the collapse happens before the tips are joined")
    check(ms.ts_fields(repaired[0]["firstTs"])["tsRaw"] == "2026-07-25T03:00:05.000Z",
          f"…and the two spellings resolve through the store's own `$min`, so two "
          f"readers cannot disagree: {ms.ts_fields(repaired[0]['firstTs'])['tsRaw']}")

    # The clean case is unchanged: on the frozen corpus the two timestamps are
    # the same value, so nothing about R-48's stored shape moved.
    big = read_fragment(str(BIG), root=str(CORPUS))
    check(big.first_record_ts == big.first_ts,
          "a monotone transcript has one answer to both questions (the frozen pair)")
    check(ms.ts_fields(big.identity()["firstTs"])["tsRaw"] == FIRST_TS,
          f"…and the stored identity still carries {FIRST_TS}")


def test_a_fragment_with_no_first_record_writes_no_phantom_element():
    print("test_a_fragment_with_no_first_record_writes_no_phantom_element")
    # MAJOR 4 of attempt 1's critique: the tailer sees `agent-<id>.jsonl` the
    # moment it is created, which is BEFORE the first record is complete. A
    # `{path: …}` element now and a `{sessionId, path, firstUuid, …}` element
    # one tick later are two BSON sub-documents, so `$addToSet` keeps both —
    # permanently, since GD-26 forbids the delete that would fix it.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        directory = root / "projects" / "slug" / "sess-1" / "subagents" / "workflows" / "wf_1"
        directory.mkdir(parents=True)
        path = directory / f"agent-{'e' * 17}.jsonl"
        path.write_text("", encoding="utf-8")

        counters = {}
        empty = assemble([read_fragment(str(path), root=str(root))], skipped=counters)
        check(counters.get("no_first_record") == 1,
              "an unreadable first record is COUNTED, not silently shaped around")
        check(empty.files == () and empty.sessions == (),
              f"…and contributes no files/sessions entry either: "
              f"{empty.files} / {empty.sessions}")
        check(len(empty.fragments) == 1,
              "…while the fragment is still carried, because a file we can see but "
              "not yet read is a fact a diagnostic wants")

        state = ms.apply_operations({}, map_agent(empty))
        doc = state["agents"]["e" * 17]
        check("fragments" not in doc,
              f"no phantom element reaches the document: {doc.get('fragments')}")
        check(doc.get("provenance") == agents.PROVENANCE,
              "…but the AGENT exists: a harness fact creates the node (R-28/GD-7)")

        template = json.loads(BIG.read_text(encoding="utf-8").split("\n", 1)[0])
        path.write_text(json.dumps(dict(template, uuid=GROWING_FIRST, parentUuid=None)) + "\n",
                        encoding="utf-8")
        ms.apply_operations(state, map_agent(assemble([read_fragment(str(path),
                                                                     root=str(root))])))
        doc = state["agents"]["e" * 17]
        check(len(doc.get("fragments") or []) == 1,
              f"…and the next tick adds exactly ONE, not a second: "
              f"{len(doc.get('fragments') or [])}")
        check(len(fragments_of(doc)) == 1 and fragments_of(doc)[0]["firstUuid"] == GROWING_FIRST,
              "…which is the real one, with the identity the first line now states")
        ms.validate_document("agents", doc)


DISAGREE_AGENT = "f" * 17
DISAGREE_HEAD = "aaaaaaaa-0000-4000-8000-000000000001"
DISAGREE_TAIL = "bbbbbbbb-0000-4000-8000-000000000002"


def disagreeing_fragments(root):
    """Two fragments of ONE agent whose `.meta.json` AND markers disagree.

    The frozen corpus cannot express this: `BIG` has meta and a marker, `SMALL`
    has neither, so there is nothing to disagree about and the absent-vs-present
    case is the only one it tests. The shape here is the present-vs-different
    one, and it is the shape that told the two ingest arms apart: the
    chain-first fragment's `[monitor]` marker carries no `name=`, the
    continuation's `[touch]` marker does.
    """
    template = json.loads(BIG.read_text(encoding="utf-8").split("\n", 1)[0])

    def write(session, uuid, parent, prompt, meta, ts):
        directory = (Path(root) / "projects" / "slug" / session / "subagents" /
                     "workflows" / "wf_1")
        directory.mkdir(parents=True)
        path = directory / f"agent-{DISAGREE_AGENT}.jsonl"
        record = dict(template, uuid=uuid, parentUuid=parent, type="user", timestamp=ts,
                      sessionId=session, message={"role": "user", "content": prompt})
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        (directory / f"agent-{DISAGREE_AGENT}.meta.json").write_text(
            json.dumps(meta), encoding="utf-8")
        return path

    head = write("11111111-1111-4111-8111-aaaaaaaaaaaa", DISAGREE_HEAD, None,
                 "\n[monitor] plan=sp-x stage=impl role=impl attempt=1\nbody",
                 {"agentType": "workflow-subagent", "model": "opus"},
                 "2026-07-25T04:00:00.000Z")
    tail = write("22222222-2222-4222-8222-bbbbbbbbbbbb", DISAGREE_TAIL, DISAGREE_HEAD,
                 "\n[touch] name=critique parent=root\n"
                 "[monitor] plan=sp-x stage=critique role=critic attempt=1\nbody",
                 {"agentType": "general-purpose", "model": "haiku"},
                 "2026-07-25T04:10:00.000Z")
    return head, tail


def test_rebuild_and_backfill_resolve_a_disagreement_identically():
    print("test_rebuild_and_backfill_resolve_a_disagreement_identically")
    # MAJOR 3 of attempt 1's critique. `assemble` used chain-first precedence
    # and the mapper uses `$min`, so the `--rebuild` arm DROPPED a name the
    # harness had stated and flagged a named agent `unconventional: true`,
    # while `--backfill` got it right. R-56's wipe/rebuild-equivalence arm
    # compares exactly these two.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        head_path, tail_path = disagreeing_fragments(root)
        head = read_fragment(str(head_path), root=str(root))
        tail = read_fragment(str(tail_path), root=str(root))
        check(tail.first_parent_uuid == head.last_uuid,
              "the fixture is genuinely chained: the continuation follows the head")
        check(head.labels is not None and head.labels.name is None,
              "…the chain-FIRST fragment's marker states no name=")
        check(tail.labels is not None and tail.labels.name == "critique",
              "…and the continuation's does")

        counters = {}
        rebuilt = assemble([head, tail], skipped=counters)
        check(counters.get("meta_conflict") == 2,
              f"both disagreeing meta fields are counted: {counters.get('meta_conflict')}")
        check(counters.get("marker_conflict") == 1,
              f"…and the second marker-bearing fragment is counted: "
              f"{counters.get('marker_conflict')}")
        check(rebuilt.labels.name == "critique" and rebuilt.unconventional is False,
              f"a name the harness STATED is never dropped by the rebuild arm, and a "
              f"named agent is not unconventional: {rebuilt.labels.name!r} / "
              f"{rebuilt.unconventional}")

        rebuild_state = ms.apply_operations({}, map_agent(rebuilt))
        for order in ((head, tail), (tail, head)):
            backfill_state = {}
            for fragment in order:
                ms.apply_operations(backfill_state, map_agent(assemble([fragment])))
            check(ms.fingerprint(backfill_state) == ms.fingerprint(rebuild_state),
                  f"…and --backfill in {[f.first_uuid[:4] for f in order]} order stores the "
                  f"byte-identical document a --rebuild does (R-56/GD-25)")
        doc = rebuild_state["agents"][DISAGREE_AGENT]
        check(doc["name"] == "critique" and doc["unconventional"] is False,
              f"…which renders by name, not as a raw 17-hex id: {doc.get('name')!r}")
        check(doc["agentType"] == "general-purpose" and doc["model"] == "haiku",
              f"…with the meta resolved by $min, the same rule on both arms: "
              f"{doc.get('agentType')!r}/{doc.get('model')!r}")
        ms.validate_document("agents", doc)


def test_an_unchained_fragment_is_kept_and_counted():
    print("test_an_unchained_fragment_is_kept_and_counted")
    a = Fragment(agent_id="d" * 17, session_id="s1", path="a.jsonl", first_uuid="u1",
                 last_uuid="u2", first_parent_uuid=None,
                 first_ts=datetime.datetime(2026, 7, 25, tzinfo=datetime.timezone.utc))
    orphan = Fragment(agent_id="d" * 17, session_id="s2", path="b.jsonl", first_uuid="u9",
                      last_uuid="u10", first_parent_uuid="GONE",
                      first_ts=datetime.datetime(2026, 7, 26, tzinfo=datetime.timezone.utc))
    counters = {}
    ordered = order_fragments([orphan, a], skipped=counters)
    check([f.path for f in ordered] == ["a.jsonl", "b.jsonl"],
          "a fragment whose parent is gone (performCompactTranscript) becomes its own chain "
          "head, ordered by (firstTs, path) — never dropped")
    check(not counters.get("unchained_fragment"),
          "…and that is not an anomaly: a hole in the middle leaves two honest chains")

    # A genuine cycle has no head at all, and a walk that only follows heads
    # would silently lose both fragments.
    left = Fragment(agent_id="d" * 17, session_id="s1", path="l.jsonl",
                    first_uuid="x1", last_uuid="x2", first_parent_uuid="y2")
    right = Fragment(agent_id="d" * 17, session_id="s2", path="r.jsonl",
                     first_uuid="y1", last_uuid="y2", first_parent_uuid="x2")
    counters = {}
    ordered = order_fragments([left, right], skipped=counters)
    check(len(ordered) == 2 and counters.get("unchained_fragment") == 2,
          "a cycle keeps every fragment and counts it: an agent with a broken chain "
          "is still an agent (GD-26 — data is never dropped quietly)")


def test_sessionid_is_never_a_grouping_key():
    print("test_sessionid_is_never_a_grouping_key")
    paths = corpus_agent_paths()
    check(len(paths) == 8, f"the frozen corpus holds 8 agent transcripts, found {len(paths)}")
    result = scan(root=str(CORPUS), paths=paths)
    check(len(result.agents) == 7,
          f"…which are SEVEN agents, because one is split across two sessions: "
          f"{len(result.agents)}")
    ids = {obs.agent_id for obs in result.agents}
    check(len(ids) == len(result.agents) == 7, "one document per agentId, never per (session, agent)")
    pair = next(obs for obs in result.agents if obs.agent_id == AGENT)
    check(len(pair.sessions) == 2, "…and the split one carries both sessionIds")

    source = (SRC / "aggregator" / "agents.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for name in ("map_agent", "map_agent_spawn"):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        keys = {n.func.attr for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.value.__class__ is ast.Name and n.func.attr.endswith("_key")}
        check(keys <= {"agent_key"},
              f"{name} builds only agent keys — no session key can enter an agents _id: {keys}")


def test_gd25_algebra_over_the_frozen_corpus():
    print("test_gd25_algebra_over_the_frozen_corpus")
    paths = corpus_agent_paths()
    fragments = [read_fragment(path, root=str(CORPUS)) for path in paths]

    def pass_over(order):
        state = {}
        for fragment in order:
            ms.apply_operations(state, map_agent(assemble([fragment])))
        return state

    normal = pass_over(fragments)
    shuffled = list(fragments)
    random.Random(20260725).shuffle(shuffled)
    reversed_ = list(reversed(fragments))
    grouped = {}
    for fragment in fragments:
        grouped.setdefault(fragment.agent_id, []).append(fragment)
    assembled = {}
    for group in grouped.values():
        ms.apply_operations(assembled, map_agent(assemble(group)))

    for name, state in (("shuffled", pass_over(shuffled)),
                        ("reversed", pass_over(reversed_)),
                        ("fully assembled", assembled),
                        ("re-ingested (idempotent second pass)", pass_over(fragments + fragments))):
        check(ms.fingerprint(state) == ms.fingerprint(normal),
              f"{name} ingest ⇒ identical fingerprint (GD-25)")
        check(ms.counts(state) == ms.counts(normal),
              f"…AND identical counts: {ms.counts(state)} vs {ms.counts(normal)}")
    check(ms.counts(normal) == {"agents": 7}, f"…which are 7 agents: {ms.counts(normal)}")
    for key, doc in normal["agents"].items():
        ms.validate_document("agents", doc)
    check(True, "every stored document validates against GD-24's pins")


def test_the_token_rollup_is_the_union_of_the_fragments():
    print("test_the_token_rollup_is_the_union_of_the_fragments")
    # R-48's "token rollup = union". Tokens are `usage` documents (R-50,
    # ingest.py's), so the assertion is that the two fragments' usage streams
    # union — deduped by message.id, which is what makes it a union and not a
    # sum over overlapping segments.
    big = ingest.read_transcript(str(BIG), root=str(CORPUS))
    small = ingest.read_transcript(str(SMALL), root=str(CORPUS))
    both = list(big.usage) + list(small.usage)
    union = ingest.rollup(both, by="agentId").get(AGENT)
    halves = (ingest.rollup(list(big.usage), by="agentId").get(AGENT),
              ingest.rollup(list(small.usage), by="agentId").get(AGENT))
    check(union is not None and all(halves),
          "both fragments carry usage records for the same agentId")
    check(union["out"] > halves[0]["out"] and union["out"] > halves[1]["out"],
          f"…and the union exceeds either half: {union['out']} > "
          f"{halves[0]['out']} / {halves[1]['out']}")
    ids = {obs.message_id for obs in big.usage} & {obs.message_id for obs in small.usage}
    check(not ids,
          f"…with no message.id in common — disjoint continuations, not two copies: {sorted(ids)}")
    check(ingest.rollup(list(reversed(both)), by="agentId").get(AGENT) == union,
          "…and the union is order-free (dedup by message.id, $max per field — GD-25)")


# --- R-48: the spawn locator ---------------------------------------------


SPAWN_SESSION = "08ffb13f-2e24-4c06-ac9b-f2e8d0a7d789"
SPAWN_AGENT = "a342353f7b157760b"
SPAWN_TOOL_USE = "toolu_01P7eU7dNUhEWqwMRxAwB5aG"


def spawn_fixture(root):
    """A minimal Agent-tool spawn pair, in the shape the corpus records it.

    Written here rather than added to `tests/fixtures/` because that tree has
    exactly one owner (sp-02) and this file is not it. The shape is copied from
    the live specimen: an `assistant` record carrying the `tool_use`, then the
    `user` record whose `toolUseResult` names the agentId.
    """
    directory = Path(root) / "projects" / "-tmp-slug"
    directory.mkdir(parents=True)
    path = directory / f"{SPAWN_SESSION}.jsonl"
    common = {"sessionId": SPAWN_SESSION, "cwd": "/tmp/x", "version": "2.1.220"}
    records = [
        dict(common, type="user", uuid="00000000-0000-4000-8000-000000000001",
             parentUuid=None, timestamp="2026-07-25T03:05:00.000Z",
             message={"role": "user", "content": "spawn one"}),
        dict(common, type="assistant", uuid="ab56a3de-a0cf-44bf-952d-9cbbdda7b7b5",
             parentUuid="00000000-0000-4000-8000-000000000001",
             timestamp="2026-07-25T03:06:00.000Z",
             message={"role": "assistant", "id": "msg_1", "content": [
                 {"type": "tool_use", "id": SPAWN_TOOL_USE, "name": "Agent",
                  "input": {"description": "Run bash command and report output",
                            "subagent_type": "general-purpose", "prompt": "echo hi"}}]}),
        dict(common, type="user", uuid="93c4a89e-3eb6-4b2d-83b4-9d755b9dff8c",
             parentUuid="ab56a3de-a0cf-44bf-952d-9cbbdda7b7b5",
             timestamp="2026-07-25T03:06:39.046Z",
             message={"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": SPAWN_TOOL_USE, "content": "hi"}]},
             toolUseResult={"status": "completed", "agentId": SPAWN_AGENT,
                            "agentType": "general-purpose",
                            "resolvedModel": "claude-haiku-4-5-20251001",
                            "totalTokens": 1234}),
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def test_the_spawn_locator_is_a_uuid_with_a_perishable_line_hint():
    print("test_the_spawn_locator_is_a_uuid_with_a_perishable_line_hint")
    with tempfile.TemporaryDirectory() as tmp:
        path = spawn_fixture(tmp)
        found = find_spawns(ingest.read_transcript(str(path), root=tmp), root=tmp)
        check(len(found) == 1, f"one (tool_use, tool_result) pair ⇒ one spawn: {len(found)}")
        spawn = found[0]
        check(spawn.agent_id == SPAWN_AGENT,
              "the agentId comes off toolUseResult — the only place the harness states the link")
        check(spawn.record_uuid == "ab56a3de-a0cf-44bf-952d-9cbbdda7b7b5",
              "…and recordUuid is the ASSISTANT record that launched it, not the result")
        check(spawn.tool_use_id == SPAWN_TOOL_USE, "…with the toolUseId that joins the pair")
        check(spawn.file_hint["line"] == 2,
              f"the file hint carries the LINE ({spawn.file_hint['line']}), and identity does not")

        state = ms.apply_operations({}, map_agent_spawn(spawn))
        doc = state["agents"][SPAWN_AGENT]
        ms.validate_document("agents", doc)
        check(doc["spawn"]["recordUuid"] == spawn.record_uuid,
              "the stored spawn is {recordUuid, toolUseId, sessionId, fileHint} (R-48)")
        check(doc.get("resultSeen") is True and doc.get("resultTs") is not None,
              "…and an observed completion is recorded as an observation, not as a state")
        check("state" not in doc and "status" not in doc,
              "…with no state field anywhere: liveness is the reducer's (GD-23)")

        status = check_file_hint(doc["spawn"]["fileHint"], root=tmp)
        check(status.valid, f"the hint validates while (stDev, ino, size) match: {status.reason}")

        with open(path, "a", encoding="utf-8") as handle:      # the file grows
            handle.write(json.dumps({"type": "system", "uuid": None}) + "\n")
        status = check_file_hint(doc["spawn"]["fileHint"], root=tmp)
        check(not status.valid and "size" in status.reason,
              f"…and goes stale the moment the file changes: {status.reason}")
        check(spawn_record_filter(doc["spawn"]) == {"_id": spawn.record_uuid},
              "…while 'jump to spawn' resolves by uuid — records.findOne, never a file re-read")

        # And the hint survives its own invalidation, for diagnostics (R-48).
        check(doc["spawn"]["fileHint"]["line"] == 2,
              "the stale hint is kept, not deleted (GD-26: disappearance is a field)")


def test_the_spawn_hint_is_order_free_and_coherent():
    print("test_the_spawn_hint_is_order_free_and_coherent")
    # BLOCKER 2 of attempt 1's critique. `fileHint` is stat'd from the PARENT
    # session transcript, which grows while the session is alive, so two
    # observations of one spawn genuinely disagree — and `$set` on the whole
    # sub-document stored whichever one mongod's unordered bulk applied last.
    with tempfile.TemporaryDirectory() as tmp:
        path = spawn_fixture(tmp)
        early = find_spawns(ingest.read_transcript(str(path), root=tmp), root=tmp)[0]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "system", "sessionId": SPAWN_SESSION,
                "uuid": "5c6d7e8f-0000-4000-8000-00000000000f",
                "parentUuid": "93c4a89e-3eb6-4b2d-83b4-9d755b9dff8c",
                "timestamp": "2026-07-25T03:07:00.000Z", "content": "x" * 64}) + "\n")
        late = find_spawns(ingest.read_transcript(str(path), root=tmp), root=tmp)[0]
        check(late.file_hint["size"] > early.file_hint["size"],
              f"the parent transcript GREW between the two observations of one spawn: "
              f"{early.file_hint['size']} -> {late.file_hint['size']}")
        check(early.record_uuid == late.record_uuid,
              "…while the spawn record itself did not move: the identity is the uuid")

        forward = ms.apply_operations(ms.apply_operations({}, map_agent_spawn(early)),
                                      map_agent_spawn(late))
        reverse = ms.apply_operations(ms.apply_operations({}, map_agent_spawn(late)),
                                      map_agent_spawn(early))
        check(ms.fingerprint(forward) == ms.fingerprint(reverse),
              "…and the two observations commute: identical fingerprint in either "
              "application order (mirror batches one _id's updates unordered)")
        for name, built in (("forward", forward), ("reversed", reverse)):
            hint = built["agents"][SPAWN_AGENT]["spawn"]["fileHint"]
            check(hint["size"] == late.file_hint["size"],
                  f"…storing the LATER observation in {name} order: {hint['size']}")
            check(check_file_hint(hint, root=tmp).valid,
                  f"…which is the one that still validates against the file on disk "
                  f"({name})")
        stored = forward["agents"][SPAWN_AGENT]["spawn"]
        # R-48's three named members plus `sessionId` (the launching session,
        # which is NOT an entry in `sessions[]`) and the launch's own account of
        # the agent. `agentType`/`resolvedModel` live in here rather than on the
        # top-level columns because the launch and the `.meta.json` spell them
        # differently — MINOR 2 of attempt 2's critique, asserted by
        # `test_the_two_mappers_do_not_fight_over_agent_type_and_model`.
        check(set(stored) == {"recordUuid", "toolUseId", "sessionId", "fileHint",
                              "agentType", "resolvedModel"},
              f"…and R-48's spawn shape is unchanged by the leaf-wise write: "
              f"{sorted(stored)}")
        check("model" not in forward["agents"][SPAWN_AGENT],
              "…with no top-level `model`: the launch's vocabulary stays namespaced")
        ms.validate_document("agents", forward["agents"][SPAWN_AGENT])


#: The one frozen specimen that carries BOTH halves of an Agent-tool agent: the
#: session transcript with the `(tool_use, tool_result)` pair AND the agent's own
#: transcript with its `.meta.json`. `SPAWN_AGENT` is this agent.
LIVEIO_ROOT = HERE / "fixtures" / "mirror" / "discovery"
LIVEIO_SLUG = LIVEIO_ROOT / "projects" / "-tmp-claude-1000-liveio"
LIVEIO_SESSION = LIVEIO_SLUG / f"{SPAWN_SESSION}.jsonl"
LIVEIO_AGENT = LIVEIO_SLUG / SPAWN_SESSION / "subagents" / f"agent-{SPAWN_AGENT}.jsonl"


def test_the_two_mappers_do_not_fight_over_agent_type_and_model():
    print("test_the_two_mappers_do_not_fight_over_agent_type_and_model")
    # MINOR 2 + MINOR 4 of attempt 2's critique, on the one frozen specimen that
    # has both halves. The two mappers write ONE document from two sources, and
    # the sources speak two vocabularies for `model`: `.meta.json` says `opus`,
    # the launch result says `claude-opus-5[1m]`. Fed to one column under `$min`
    # the winner is BSON collation, not R-48's "the fragment that HAS meta wins".
    fragment = read_fragment(str(LIVEIO_AGENT), root=str(LIVEIO_ROOT))
    spawn = find_spawns(ingest.read_transcript(str(LIVEIO_SESSION), root=str(LIVEIO_ROOT)),
                        root=str(LIVEIO_ROOT))[0]
    check(spawn.agent_id == fragment.agent_id == SPAWN_AGENT,
          f"the frozen pair is one agent seen from both sides: {spawn.agent_id}")

    obs = assemble([fragment])
    check(obs.tool_use_id == spawn.tool_use_id == SPAWN_TOOL_USE,
          f"…and .meta.json states the SAME toolUseId the launch does: "
          f"{obs.tool_use_id} / {spawn.tool_use_id}")
    check(obs.description == spawn.description == "Run bash command and report output",
          f"…and the same description: {obs.description!r} / {spawn.description!r}")
    check(agents.META_FIELDS == ("agentType", "model", "spawnDepth",
                                 "description", "toolUseId"),
          f"…which is why both are read off the meta file at all: {agents.META_FIELDS}")

    forward = ms.apply_operations(ms.apply_operations({}, map_agent(obs)),
                                  map_agent_spawn(spawn))
    reverse = ms.apply_operations(ms.apply_operations({}, map_agent_spawn(spawn)),
                                  map_agent(obs))
    check(ms.fingerprint(forward) == ms.fingerprint(reverse),
          "the two mappers commute on one document (GD-25)")
    doc = forward["agents"][SPAWN_AGENT]
    ms.validate_document("agents", doc)
    check(doc["toolUseId"] == SPAWN_TOOL_USE and doc["description"] == obs.description,
          "…and the two columns both of them write are one harness fact stated "
          "twice, so `$min` over them is a no-op rather than a race")
    check(doc["agentType"] == "general-purpose"
          and doc["spawn"]["agentType"] == "general-purpose",
          f"…while agentType is the meta's, with the launch's own copy namespaced: "
          f"{doc.get('agentType')} / {doc['spawn'].get('agentType')}")
    check("model" not in doc and doc["spawn"]["resolvedModel"] == "claude-haiku-4-5-20251001",
          f"…and this meta states no model at all, so the resolved id is reported "
          f"as the LAUNCH's answer and never as the agent's: {doc.get('model')} / "
          f"{doc['spawn'].get('resolvedModel')}")

    # The disagreement itself, with the meta half of the live shape from
    # `a483cae616edffe81` (meta `model: opus`, launch `resolvedModel:
    # claude-opus-5[1m]`) — the pair the critique measured.
    check(min("opus", "claude-opus-5[1m]") == "claude-opus-5[1m]",
          "the collation accident is real: `$min` over the two spellings picks the "
          "resolved id in EITHER observation order, silently overriding R-48")
    metaful = dataclasses.replace(obs, model="opus")
    resolved = dataclasses.replace(spawn, model="claude-opus-5[1m]")
    for name, order in (("meta first", (map_agent(metaful), map_agent_spawn(resolved))),
                        ("spawn first", (map_agent_spawn(resolved), map_agent(metaful)))):
        state = {}
        for ops in order:
            ms.apply_operations(state, ops)
        stored = state["agents"][SPAWN_AGENT]
        check(stored["model"] == "opus",
              f"…so the meta-bearing fragment keeps the column ({name}): "
              f"{stored.get('model')}")
        check(stored["spawn"]["resolvedModel"] == "claude-opus-5[1m]",
              f"…and the launch's spelling is kept beside it, not instead of it "
              f"({name}): {stored['spawn'].get('resolvedModel')}")


def doctored_session(root, records):
    """A session transcript built from explicit records, for the counter cases."""
    directory = Path(root) / "projects" / "-tmp-slug"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{SPAWN_SESSION}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def result_record(uuid, parent, tool_use_id, agent_id, ts):
    return {"sessionId": SPAWN_SESSION, "cwd": "/tmp/x", "version": "2.1.220",
            "type": "user", "uuid": uuid, "parentUuid": parent, "timestamp": ts,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": "hi"}]},
            "toolUseResult": {"status": "completed", "agentId": agent_id}}


def test_every_declared_skip_counter_has_a_firing_case():
    print("test_every_declared_skip_counter_has_a_firing_case")
    # MINOR 6 + MINOR 7 of attempt 1's critique: six of the nine counters had no
    # test on either side, and `spawn_agent_conflict` was structurally
    # unreachable — the launch was popped before the check, so a second result
    # took the early return and was miscounted as `spawn_without_tool_use`.
    # `_skips()`'s whole argument is that "nothing was skipped" is assertable,
    # which is only true if every key can also be made to fire.
    fired = set()

    def fire(name, counters):
        check(counters.get(name), f"{name} fires: {counters.get(name)}")
        if counters.get(name):
            fired.add(name)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # not_an_agent_file — a `read_fragment` direct-call counter only.
        counters = agents._skips()
        check(raises(AgentsError, read_fragment, str(root / "notes.txt"),
                     skipped=counters),
              "read_fragment refuses a path with no 17-hex agentId")
        fire("not_an_agent_file", counters)

        # unreadable_meta — the file exists and is not a JSON object.
        directory = root / "projects" / "slug" / "s1" / "subagents" / "workflows" / "wf_1"
        directory.mkdir(parents=True)
        broken = directory / f"agent-{'1' * 17}.jsonl"
        template = json.loads(BIG.read_text(encoding="utf-8").split("\n", 1)[0])
        broken.write_text(json.dumps(dict(template, uuid=GROWING_FIRST,
                                          parentUuid=None)) + "\n", encoding="utf-8")
        (directory / f"agent-{'1' * 17}.meta.json").write_text("[]", encoding="utf-8")
        counters = agents._skips()
        read_fragment(str(broken), root=str(root), skipped=counters)
        fire("unreadable_meta", counters)

        # no_first_record — the file exists, the first line does not yet.
        empty = directory / f"agent-{'2' * 17}.jsonl"
        empty.write_text("", encoding="utf-8")
        counters = agents._skips()
        assemble([read_fragment(str(empty), root=str(root))], skipped=counters)
        fire("no_first_record", counters)

        # meta_conflict + marker_conflict — two fragments that disagree.
        head_path, tail_path = disagreeing_fragments(root / "disagree")
        counters = agents._skips()
        assemble([read_fragment(str(head_path), root=str(root / "disagree")),
                  read_fragment(str(tail_path), root=str(root / "disagree"))],
                 skipped=counters)
        fire("meta_conflict", counters)
        fire("marker_conflict", counters)

        # unchained_fragment — a cycle, which has no chain head at all.
        counters = agents._skips()
        order_fragments(
            [Fragment(agent_id="d" * 17, session_id="s1", path="l.jsonl",
                      first_uuid="x1", last_uuid="x2", first_parent_uuid="y2"),
             Fragment(agent_id="d" * 17, session_id="s2", path="r.jsonl",
                      first_uuid="y1", last_uuid="y2", first_parent_uuid="x2")],
            skipped=counters)
        fire("unchained_fragment", counters)

        # unstattable_hint — the file vanished between the read and the stat.
        counters = agents._skips()
        check(file_hint(str(root / "gone.jsonl"), 3, skipped=counters) is None,
              "a hint for a file that is no longer there is None, not a guess")
        fire("unstattable_hint", counters)

    with tempfile.TemporaryDirectory() as tmp:
        # spawn_without_result — a launch whose agent has not answered.
        path = spawn_fixture(tmp)
        lines = path.read_text(encoding="utf-8").splitlines()[:2]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        counters = agents._skips()
        find_spawns(ingest.read_transcript(str(path), root=tmp), root=tmp, skipped=counters)
        fire("spawn_without_result", counters)

    with tempfile.TemporaryDirectory() as tmp:
        # spawn_without_tool_use — a result whose launch record is gone
        # (`performCompactTranscript` removed it).
        path = doctored_session(tmp, [result_record(
            "93c4a89e-3eb6-4b2d-83b4-9d755b9dff8c", None, SPAWN_TOOL_USE,
            SPAWN_AGENT, "2026-07-25T03:06:39.046Z")])
        counters = agents._skips()
        found = find_spawns(ingest.read_transcript(str(path), root=tmp), root=tmp,
                            skipped=counters)
        check(found == (), "…and it yields no spawn: the launch record is what recordUuid IS")
        fire("spawn_without_tool_use", counters)

    with tempfile.TemporaryDirectory() as tmp:
        # spawn_agent_conflict — ONE toolUseId, TWO agentIds. Doctored, because
        # the corpus does not contain the case; the counter exists to make it
        # visible if the harness ever produces it (R-50 uses the same shape).
        path = spawn_fixture(tmp)
        records = [json.loads(line) for line in
                   path.read_text(encoding="utf-8").splitlines()]
        records.append(result_record("7f2b1a4c-0000-4000-8000-00000000000e",
                                     records[-1]["uuid"], SPAWN_TOOL_USE,
                                     "b" + "0" * 16, "2026-07-25T03:07:39.046Z"))
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        counters = agents._skips()
        found = find_spawns(ingest.read_transcript(str(path), root=tmp), root=tmp,
                            skipped=counters)
        fire("spawn_agent_conflict", counters)
        check(not counters["spawn_without_tool_use"],
              f"…and the conflict is reported ONCE, not also as a missing tool_use: "
              f"{counters['spawn_without_tool_use']}")
        check(len(found) == 1,
              f"…while the first, unambiguous pair still produces its spawn: {len(found)}")

    check(fired == set(agents._skips()),
          f"every declared counter has a firing case; missing: "
          f"{sorted(set(agents._skips()) - fired)}")


def test_a_spawn_without_a_result_is_counted_not_guessed():
    print("test_a_spawn_without_a_result_is_counted_not_guessed")
    with tempfile.TemporaryDirectory() as tmp:
        path = spawn_fixture(tmp)
        lines = path.read_text(encoding="utf-8").splitlines()[:2]   # drop the result
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        counters = {}
        found = find_spawns(ingest.read_transcript(str(path), root=tmp), root=tmp,
                            skipped=counters)
        check(found == (),
              "a launch whose agent has not answered yet yields no document — the agentId "
              "is not knowable from this file, and guessing one is a fabricated join")
        check(counters.get("spawn_without_result") == 1, "…and it is counted, not silent")


def test_the_mapper_refuses_what_it_cannot_key():
    print("test_the_mapper_refuses_what_it_cannot_key")
    check(raises(AgentsError, map_agent, AgentObservation(agent_id="a2fc883c")),
          "an 8-hex id never reaches `agents` (R-48: 17-hex validated)")
    check(raises(AgentsError, map_agent_spawn,
                 SpawnObservation(agent_id=SPAWN_AGENT, record_uuid="")),
          "a spawn with no recordUuid is refused: an offset is not an identity")
    check(raises(AgentsError, assemble, []),
          "an agent is assembled from at least one fragment")
    check(raises(AgentsError, assemble,
                 [Fragment(agent_id="a" * 17, session_id="s", path="a"),
                  Fragment(agent_id="b" * 17, session_id="s", path="b")]),
          "fragments of two agentIds are never merged — the agentId IS the grouping key")
    check(raises(AgentsError, read_fragment, str(CORPUS / "nope.jsonl")),
          "a path with no 17-hex agentId is not an agent transcript")
    check(read_meta(str(CORPUS / "does-not-exist.meta.json")) is None,
          "…while a missing .meta.json is normal and returns None (R-48)")

    # NIT 9 of attempt 2's critique: `_as_observation` documents "or the plain
    # dict a replay/fixture hands back", and a serialized observation can only
    # carry its labels as a mapping. Without the coercion the mapper reached
    # `labels.fields()` on a dict and raised AttributeError — outside the
    # AgentsError funnel `mirror.Mapper` turns into a MapperError naming this
    # module, so the failure arrived without a module to blame.
    replayed = map_agent({
        "agent_id": "d" * 17,
        "fragments": [{"agent_id": "d" * 17, "session_id": "s", "path": "p",
                       "first_uuid": GROWING_FIRST, "line_count": 1,
                       "labels": {"name": "replayed", "unconventional": False}}],
        "labels": {"name": "replayed", "root": "touch", "unconventional": False},
        "unconventional": False,
    })
    doc = ms.apply_operations({}, replayed)["agents"]["d" * 17]
    check(doc.get("name") == "replayed" and doc.get("unconventional") is False,
          f"a replayed observation whose labels are a dict maps like a Labels: "
          f"{doc.get('name')!r}")
    check(raises(AgentsError, map_agent,
                 {"agent_id": "d" * 17, "labels": "not-a-mapping"}),
          "…and an unusable labels value is an AgentsError, the exception "
          "mirror.Mapper knows how to attribute")
    check(raises(AgentsError, map_agent, {"agent_id": "d" * 17, "nope": 1}),
          "…as is a dict carrying a field this module has no place for")

    # NIT 5 of attempt 3's critique: on the same replay path, `unconventional`
    # was taken from the dataclass DEFAULT (`True`) whenever the dict did not
    # restate it — so a replayed observation stored "no name" next to the name
    # it was carrying, inverting R-28's precedence on that path alone. The
    # coerced labels are the authority; `labels_from_prompt` derives the flag
    # the same way (`not name`), and `$min` still lets a real observation win.
    named = ms.apply_operations({}, map_agent(
        {"agent_id": "e" * 17, "labels": {"name": "impl", "unconventional": False}}
    ))["agents"]["e" * 17]
    check(named.get("name") == "impl" and named.get("unconventional") is False,
          f"a replayed agent whose labels state a name is NOT unconventional: "
          f"{named.get('unconventional')!r}")
    implied = ms.apply_operations({}, map_agent(
        {"agent_id": "e" * 17, "labels": {"name": "impl"}}))["agents"]["e" * 17]
    check(implied.get("unconventional") is False,
          "…and the flag is derived even when the replay dict omits it entirely")
    anonymous = ms.apply_operations({}, map_agent(
        {"agent_id": "e" * 17, "labels": {"plan": "sp-x"}}))["agents"]["e" * 17]
    check(anonymous.get("unconventional") is True,
          "…while a marker with no name= is still unconventional (R-28's common case)")
    check(map_agent({"agent_id": "e" * 17, "labels": {"name": "impl"},
                     "unconventional": True})[0][2]["$min"]["unconventional"] is True,
          "…and an explicit top-level flag is still believed: the derivation only "
          "fills a silence")

    # NIT 4: `fragmentTips.<firstUuid>.<leaf>` builds an update path out of
    # harness text. The live path cannot produce a bad component (a record whose
    # uuid fails ingest's pattern never becomes a fragment head), but the
    # documented dict-replay path can, and `ingest._launch_paths` refuses
    # exactly this hazard by name — a `.` becomes a nesting level, a leading `$`
    # an operator, and the stored shape of a field stops being stable (GD-24).
    def replay_with(first_uuid):
        return map_agent({"agent_id": "f" * 17,
                          "fragments": [{"agent_id": "f" * 17, "session_id": "s",
                                         "path": "p", "first_uuid": first_uuid,
                                         "line_count": 1}]})

    for bad in ("a.b", "$max", "x\x00y"):
        check(raises(AgentsError, replay_with, bad),
              f"a fragment head {bad!r} is refused as an AgentsError, not written as a path")
    good = replay_with("u1")[0][2]
    check(list(good["$max"]) == ["fragmentTips.u1.lineCount", "fragmentTips.u1.records"],
          f"…while a plain component still builds the two-level path: {list(good['$max'])}")


def test_sd1_the_mappers_are_pure_and_write_only_agents():
    print("test_sd1_the_mappers_are_pure_and_write_only_agents")
    source = (SRC / "aggregator" / "agents.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned_calls = {"open", "print", "input"}
    banned_attrs = ("os.", "time.", "random.", "subprocess.", "socket.")
    reached = {}

    def body_of(name):
        return next(n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)

    for name in ("map_agent", "map_agent_spawn"):
        fn = body_of(name)
        calls = {n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        check(not (calls & banned_calls), f"{name} calls nothing that touches the world: {calls}")
        attrs = {f"{n.value.id}.{n.attr}" for n in ast.walk(fn)
                 if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        check(not any(a.startswith(banned_attrs) for a in attrs),
              f"…and reads neither the filesystem nor the clock: {sorted(attrs)}")
        reached[name] = attrs
    check(not any("datetime" in a for a in reached["map_agent"]),
          "a mapper has no clock: a document's contents are a function of the file")

    # BLOCKER 1 + BLOCKER 2 of attempt 1's critique, as a structural guard
    # rather than as one more example. `mirror._take_batches` keeps two updates
    # of one `_id` as two operations in ONE unordered bulk and `mirror._requeue`
    # appends unwritten operations at the tail — both on the explicit ground
    # that this algebra commutes. `$set` does not, and `mongo_store`'s
    # accumulable fence cannot catch a field the `agents` spec never declared
    # (`fragmentTips`, `spawn`). `derived` is exempt and is not written here:
    # GD-23 drops and rebuilds that collection wholesale.
    commuting = {"op_max", "op_min", "op_add_to_set", "op_set_on_insert"}
    for name in ("map_agent", "map_agent_spawn"):
        used = {n.func.attr for n in ast.walk(body_of(name))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr.startswith("op_")}
        check(used <= commuting,
              f"{name} builds only commuting operations: {sorted(used)}")

    check("pymongo" not in source,
          "no database driver is named in this file at all (GD-21: only two modules may)")
    # Called verbs, not prose: the module DOCUMENTS the drop-and-rebuild rule
    # (GD-23) and a grep over raw text would fail on the documentation of the
    # very rule it is checking (`test_mirror.py` makes the same distinction).
    called = sorted({n.func.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
                    & {"delete_one", "delete_many", "drop", "drop_collection",
                       "remove", "replace_one", "find_one_and_delete"})
    check(not called,
          f"no delete/drop verb is ever CALLED: the mirror is upsert-only (GD-26) — {called}")
    for operator in ("$unset", "$inc"):
        check(f'"{operator}"' not in source and f"'{operator}'" not in source,
              f"…and no {operator} literal: not part of the algebra (GD-25)")

    # The wall itself, exercised rather than read.
    check(raises(AgentsError, agents._only_ours, [("records", "x", {})]),
          "_only_ours refuses a foreign collection (GD-15: records are ingest.py's)")
    check(raises(AgentsError, agents._only_ours, [("derived", "x", {})]),
          "…including `derived`: GD-23 gives derived state exactly one writer, the reducer")
    check(agents.COLLECTIONS == ("agents",), "…and the allowed set is one collection")


def test_the_sources_seam_matches_mirrors_contract():
    print("test_the_sources_seam_matches_mirrors_contract")
    check(set(agents.MIRROR_MAPPERS) == set(agents.MIRROR_SOURCES) == {"agent", "agentSpawn"},
          "every registered kind has both a mapper and a source (SD-1)")
    # `path=None` is --rebuild; a concrete path is --backfill and must return
    # nothing for a file this kind does not own.
    with tempfile.TemporaryDirectory() as tmp:
        root = linked_root(tmp, OWNED_SLUG, FOREIGN_SLUG)
        owned = slug_agent_path(root, OWNED_SLUG)
        foreign = slug_agent_path(root, FOREIGN_SLUG)
        check(agents.iter_agent_observations(owned, cwd=OWNED_CWD, root=root),
              "the per-path arm reads an agent transcript it owns")
        check(agents.iter_agent_observations(os.path.join(root, "x.jsonl"),
                                             cwd=OWNED_CWD, root=root) == [],
              "…and returns nothing for a path it does not own (never raises)")
        # MINOR 5 of attempt 1's critique: `mirror.iter_backfill_sources` walks
        # all of `<root>/projects` with no slug filter, so without this test
        # `--backfill` mirrors four foreign projects' agents that `--rebuild`
        # excludes — and GD-26 forbids deleting what landed.
        check(agents.iter_agent_observations(foreign, cwd=OWNED_CWD, root=root) == [],
              "…and applies R-25's scope: an agent under a FOREIGN slug is not ours, "
              "so --backfill and --rebuild see the same corpus (R-56)")
        check(agents.iter_agent_spawn_observations(owned, cwd=OWNED_CWD, root=root) == [],
              "the spawn arm skips agent transcripts: a spawn lives in the PARENT session")
        session_file = os.path.join(root, "projects", FOREIGN_SLUG,
                                    "e423cd3c-f859-45af-9afd-0d6bdec9b4ac.jsonl")
        check(agents.iter_agent_spawn_observations(session_file, cwd=OWNED_CWD,
                                                   root=root) == [],
              "…and applies the same scope: a foreign session transcript names "
              "foreign agentIds, and a spawn document creates the row it points at")
    sess.reset_scope_cache()


def copied_root(tmp, *slugs):
    """A `~/.claude`-shaped root holding a real COPY of the frozen corpus.

    `linked_root` symlinks, which is right for the per-path arm and useless for
    the whole-corpus one: `os.walk` does not descend a symlinked directory, so a
    `--rebuild` over a linked root sees nothing at all.
    """
    root = os.path.join(tmp, "claude")
    os.makedirs(os.path.join(root, "projects"), exist_ok=True)
    for slug in slugs:
        shutil.copytree(os.fspath(CORPUS), os.path.join(root, "projects", slug))
    sess.reset_scope_cache()
    return root


def test_the_two_rebuild_arms_read_the_corpus_once():
    print("test_the_two_rebuild_arms_read_the_corpus_once")
    # NIT 8 of attempt 2's critique. SD-1 registers two sources and
    # `mirror.iter_sources` calls both, so a `--rebuild` walked and re-parsed
    # every agent transcript AND every session transcript twice — once for the
    # fragments, once for the spawns. Both arms now go through the memo
    # `ingest._transcript_walk` already keeps for its own sources.
    original = ingest.read_transcript
    reads = []

    def counting(path, *args, **kwargs):
        reads.append(os.fspath(path))
        return original(path, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        root = copied_root(tmp, OWNED_SLUG)
        agent_file = slug_agent_path(root, OWNED_SLUG)
        ingest.read_transcript = counting
        try:
            first = agents.iter_agent_observations(None, cwd=OWNED_CWD, root=root)
            after_agents = len(reads)
            agents.iter_agent_spawn_observations(None, cwd=OWNED_CWD, root=root)
            after_spawns = len(reads)

            check(first and after_agents > 0,
                  f"the rebuild arm reads the corpus and finds agents: "
                  f"{len(first)} agents from {after_agents} reads")
            check(after_spawns == after_agents,
                  f"…and the second source re-reads NOTHING: {after_spawns} total "
                  f"reads for both arms, not {after_agents * 2}")
            check(len(set(reads)) == after_agents,
                  f"…each file read exactly once: {after_agents} reads over "
                  f"{len(set(reads))} distinct files")

            # The memo may never outlive the file it memoized: a live tail
            # appends four times a second, and a cache that survives that is a
            # cache that serves a stale tip forever.
            before = next(o for o in first if o.agent_id == AGENT)
            with open(agent_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "system", "uuid": None}) + "\n")
            grown = agents.iter_agent_observations(None, cwd=OWNED_CWD, root=root)
            check(len(reads) > after_spawns,
                  f"an appended file busts the walk key and is re-read: "
                  f"{len(reads)} > {after_spawns}")
            after = next(o for o in grown if o.agent_id == AGENT)
            lines_before = sum(f.line_count for f in before.fragments)
            lines_after = sum(f.line_count for f in after.fragments)
            check(lines_after == lines_before + 1,
                  f"…and the new line is visible, not memoized away: "
                  f"{lines_before} -> {lines_after}")

            # NIT 7 of attempt 3's critique: `_corpus_scans` sells memo sharing
            # as the property of the function, and the explicit-paths arm went
            # straight to `ingest.read_transcript` — so a caller naming a file
            # ingest had already read this generation re-parsed it. Both arms
            # now go through the memo `ingest.py`'s own per-path source fills.
            ingest.iter_record_observations(agent_file, cwd=OWNED_CWD, root=root)
            mark = len(reads)
            picked = agents.scan(paths=[agent_file], root=root)
            check(len(reads) == mark,
                  f"scan(paths=[…]) re-reads nothing ingest already has in hand: "
                  f"{len(reads)} reads, still {mark}")
            check([o.agent_id for o in picked.agents] == [AGENT],
                  f"…and still answers with the agent: "
                  f"{[o.agent_id for o in picked.agents]}")
        finally:
            ingest.read_transcript = original
    sess.reset_scope_cache()


def main():
    for test in (
        test_the_marker_window_is_four_lines_and_leading_blanks_are_tolerated,
        test_two_markers_on_one_line_both_parse,
        test_the_grammar_matches_decision_watchers_on_a_real_prompt,
        test_a_node_exists_with_no_marker_at_all,
        test_labels_are_a_layer_never_an_identity,
        test_the_cross_session_pair_is_one_agent_in_chain_order,
        test_the_meta_bearing_fragment_wins_without_seeing_the_other,
        test_a_growing_fragment_adds_one_element_not_one_per_tick,
        test_an_out_of_order_timestamp_does_not_duplicate_a_fragment,
        test_a_fragment_with_no_first_record_writes_no_phantom_element,
        test_rebuild_and_backfill_resolve_a_disagreement_identically,
        test_an_unchained_fragment_is_kept_and_counted,
        test_sessionid_is_never_a_grouping_key,
        test_gd25_algebra_over_the_frozen_corpus,
        test_the_token_rollup_is_the_union_of_the_fragments,
        test_the_spawn_locator_is_a_uuid_with_a_perishable_line_hint,
        test_the_spawn_hint_is_order_free_and_coherent,
        test_the_two_mappers_do_not_fight_over_agent_type_and_model,
        test_every_declared_skip_counter_has_a_firing_case,
        test_a_spawn_without_a_result_is_counted_not_guessed,
        test_the_mapper_refuses_what_it_cannot_key,
        test_sd1_the_mappers_are_pure_and_write_only_agents,
        test_the_sources_seam_matches_mirrors_contract,
        test_the_two_rebuild_arms_read_the_corpus_once,
    ):
        test()
    print()
    for message in skips:
        print(f"skipped: {message}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print("all agents (R-28/R-48) tests passed")


if __name__ == "__main__":
    main()
