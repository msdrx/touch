#!/usr/bin/env python3
"""D-05 — ONE token-accounting truth, cross-checked. Stdlib only, no pytest.

Run as `python3 test_token_crosscheck.py`; exits non-zero on failure.

Touch counts the same tokens twice, in two files that may never import each
other: `aggregator/ingest.py` (the 8932 read model) and
`shared/monitoring/decision_watcher.py` (the 8931 live view, deliberately
stdlib-standalone). Two implementations of one pure function drift silently —
so this file replays the FROZEN transcript corpus through BOTH and asserts they
agree, exactly the discipline `tests/test_agents.py` already applies to the
marker grammar.

**What "agree" means here, and why it is not field-for-field** (GD-M2, the
run-2 correction to D-05 as written): the two do not store the same quantity.
`ingest` keeps the four fields DISJOINT (`in <- input_tokens`), while the
watcher's `in` is the TOTAL input volume (fresh + cache writes + cache reads) —
measured 30,501,886 vs 225 on one real agent, the delta exactly
`cached + cache_write`. A test asserting field-for-field equality fails on every
non-trivial transcript and would push an implementer to "fix" ingest, breaking
GD-11's four-field contract and every stored `usage` document. The invariant is
therefore:

    watcher.in == ingest.in + ingest.cached + ingest.cache_write
    watcher.out == ingest.out
    watcher.cached == ingest.cached
    watcher.cache_write == ingest.cache_write

and it is asserted on TOTALS over a transcript, never per message id: the
watcher's fold is order-dependent today (last-wins per key, where ingest folds
`$max` per `message.id`), and pinning an equality the mechanism cannot
guarantee is how a green suite starts lying. Restructuring that fold is M-01, a
later pass; :func:`test_the_known_fold_gap_is_directional` states the gap as an
inequality that holds both before and after it, so this file needs no edit when
M-01 lands.

**The second quantity: context OCCUPANCY** (LC-06, GD-LC-11). The same two
trees now also count how FULL an agent's window is, and that reading is a LEVEL
at an instant — the three-component prompt sum of ONE row, the qualifying turn
with the greatest `(timestamp, path order, line)` (GD-LC-1/GD-LC-2) — where
everything above is SPEND, the sum over every turn. The two are unrelated
numbers about the same bytes and neither may be derived from the other. The
invariant, the same relation as GD-M2.2's but restricted to that one row rather
than summed over all of them, is:

    watcher.ctx.used == ingest.in + ingest.cached + ingest.cache_write
                        OF THE GREATEST-TIMESTAMP QUALIFYING ROW

— never `watcher.ctx.used == ingest.in`, which is AGENT-IDENTITY-12's trap in
one line: on a cache-heavy turn ingest's `in` is the fresh input alone (a 2
against a 17,202 occupancy on the bytes :func:`…_is_a_level_not_a_total` writes).
Both sides compute it from their OWN field names, because GD-LC-11 forbids the
two implementations importing each other and prescribes deliberate duplication
pinned equal HERE. `aggregator/ingest.py` is itself untouched by this feature
(GD-LC-10: 8932 ADOPTS `agent.ctx` off the events stream and measures nothing),
so the aggregator side is GD-LC-10's read-time PROJECTION, stated executably in
this file over `ingest`'s own reader and its own field mapping. A third
spelling of the same rule lives offline in `aggregator/costs.py`
(`Turn.context_qualifies`); it is deliberately not pinned here — its own
docstring records that as a claim maintained by hand, and changing that is a
`costs.py` edit, not this file's.

The other half of D-05 — `--no-tokens`, which lets a deployment that has wired
the ingest tick make `ingest.rollup` the single REACHABLE implementation — is
exercised end to end in `tests/monitoring/test_watcher.py` (the live process,
its events, its agent blocks). What is asserted here is the unit contract the
watcher's own call sites rest on: a suppressed reading is ABSENT, never zero.
(The occupancy twin of that rule — `--no-tokens` suppresses `ctx` too, one
switch and no third state — is `tests/monitoring/test_context_occupancy.py`'s
arm 7 and is not restated here.)

Skips are loud and counted by `tests/run_all.sh`: the frozen corpus is repo-only
material, so in a packaged copy or a fixtures-less checkout every corpus arm
prints `skip` rather than inventing agreement.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import MON, REPO, SRC                   # noqa: E402

FIX = REPO / "tests" / "fixtures"
#: LC-01's frozen occupancy specimens. Named `ctx-agent-*.jsonl` so they stay
#: OUT of :func:`corpus_transcripts`' `agent-*.jsonl` glob: they are hand-built
#: shapes chosen to be hostile, and folding them into the corpus arm would let
#: a deliberately unreadable transcript answer a question about real bytes.
CTX_FIX = FIX / "context"

# decision_watcher resolves WF_DIR / STATE_DIR at IMPORT and sys.exit()s with no
# journal, so a throwaway run dir is prepared before it is imported — the same
# preamble tests/monitoring/test_watcher.py uses, for the same reason.
_BASE = tempfile.mkdtemp(prefix="crosscheck_",
                         dir="/tmp/claude-1000" if os.path.isdir("/tmp/claude-1000") else None)
# Swept on the way out, whatever the exit path: this suite runs on every gate,
# and a per-run temp tree left behind is how a /tmp exhaustion incident starts
# (this repo has one on record). ignore_errors, because a cleanup that can fail
# the suite would be worse than the leak.
atexit.register(shutil.rmtree, _BASE, ignore_errors=True)
_WF = os.path.join(_BASE, "wf")
os.makedirs(_WF)
os.makedirs(os.path.join(_BASE, "state"))
open(os.path.join(_WF, "journal.jsonl"), "w").close()
os.environ["ORCH_WF_DIR"] = _WF
os.environ["ORCH_STATE_DIR"] = os.path.join(_BASE, "state")
os.environ["ORCH_WF_GLOB_ROOT"] = os.path.join(_BASE, "glob")

sys.path.insert(0, str(MON))
sys.path.insert(0, str(SRC))

import decision_watcher as dw                       # noqa: E402
from aggregator import ingest                       # noqa: E402

FAILS = []
SKIPS = []


def check(name, cond):
    if cond:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}")
        FAILS.append(name)


def skip(name):
    print(f"skip - {name}")
    SKIPS.append(name)


# --- the two implementations, each reduced to one four-tuple ---------------

def watcher_totals(path):
    """`(in, cached, cache_write, out)` the way decision_watcher counts them.

    Reads ONE file through the module's own incremental parser, so the thing
    under test is the shipped code path and not a re-implementation of it. The
    cache is per-path and this file reads each path once.
    """
    rows = dw._transcript_usage(path)
    return (sum(r[0] for r in rows.values()), sum(r[1] for r in rows.values()),
            sum(r[2] for r in rows.values()), sum(r[3] for r in rows.values()))


def ingest_totals(path):
    """`{in,out,cached,cache_write}` the way aggregator.ingest counts them.

    `read_transcript` -> `rollup`, i.e. the real mirror pipeline: dedup by
    `message.id` FIRST (a `$max` fold per field), then sum. Grouped by agentId
    and re-summed here because one FILE is one agent's fragment and the group
    key is not what this cross-check is about.
    """
    scan = ingest.read_transcript(path)
    totals = dict.fromkeys(ingest.USAGE_FIELDS, 0)
    for group in ingest.rollup(scan.usage).values():
        for field in ingest.USAGE_FIELDS:
            totals[field] += group[field]
    return totals


def agrees(path):
    """`(ok, watcher tuple, ingest dict)` under the GD-M2.2 invariant."""
    w_in, w_cached, w_write, w_out = watcher_totals(path)
    ing = ingest_totals(path)
    ok = (w_out == ing["out"] and w_cached == ing["cached"]
          and w_write == ing["cache_write"]
          and w_in == ing["in"] + ing["cached"] + ing["cache_write"])
    return ok, (w_in, w_cached, w_write, w_out), ing


def corpus_transcripts():
    """Every frozen agent transcript, or [] outside a repo checkout."""
    if not FIX.is_dir():
        return []
    return sorted(FIX.glob("**/agent-*.jsonl"))


# --- the corpus arm -------------------------------------------------------

def test_the_two_implementations_agree_over_the_frozen_corpus():
    """Every frozen transcript, one at a time and then in aggregate."""
    print("test_the_two_implementations_agree_over_the_frozen_corpus")
    paths = corpus_transcripts()
    if not paths:
        skip("cross-check over the frozen corpus: tests/fixtures is absent "
             "(clean checkout / packaged copy)")
        return
    disagree = []
    w_sum = [0, 0, 0, 0]
    i_sum = dict.fromkeys(ingest.USAGE_FIELDS, 0)
    for path in paths:
        ok, watcher, ing = agrees(str(path))
        if not ok:
            disagree.append((path.name, watcher, ing))
        for i in range(4):
            w_sum[i] += watcher[i]
        for field in ingest.USAGE_FIELDS:
            i_sum[field] += ing[field]
    check(f"D-05: all {len(paths)} frozen transcripts satisfy the GD-M2.2 "
          f"invariant (disagreements: {disagree[:2]})", not disagree)
    # In aggregate, so a corpus that grew a compensating pair of errors still
    # has to answer for the total.
    check("D-05: the corpus TOTAL agrees on out",  w_sum[3] == i_sum["out"])
    check("D-05: the corpus TOTAL agrees on cache reads", w_sum[1] == i_sum["cached"])
    check("D-05: the corpus TOTAL agrees on cache writes", w_sum[2] == i_sum["cache_write"])
    check("D-05: watcher `in` is ingest's in+cached+cache_write, in aggregate "
          "(the ONE deliberate difference — GD-M2.1, not a defect to fix)",
          w_sum[0] == i_sum["in"] + i_sum["cached"] + i_sum["cache_write"])
    check("D-05: the corpus actually carries usage (a green run over zeros "
          "would prove nothing)", w_sum[0] > 0 and w_sum[3] > 0)


# --- constructed shapes ---------------------------------------------------

def write_transcript(name, rows):
    """One synthetic transcript; `rows` are `(message id, usage dict)` pairs."""
    path = os.path.join(_BASE, name)
    with open(path, "w") as f:
        for message_id, usage in rows:
            f.write(json.dumps({
                "type": "assistant", "timestamp": "2026-07-30T00:00:00.000Z",
                "sessionId": "11111111-2222-3333-4444-555555555555",
                "message": {"id": message_id, "model": "claude-opus-5",
                            "usage": usage}}) + "\n")
    return path


def usage(fresh=0, out=0, cached=0, write=0):
    return {"input_tokens": fresh, "output_tokens": out,
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": write}


def test_a_constructed_transcript_agrees_field_by_field():
    """The invariant, on bytes this file wrote, so it holds with no corpus."""
    print("test_a_constructed_transcript_agrees_field_by_field")
    path = write_transcript("plain.jsonl", [
        ("m1", usage(fresh=100, out=10, cached=1000, write=50)),
        ("m2", usage(fresh=7, out=3)),
    ])
    ok, watcher, ing = agrees(path)
    check("D-05: a plain two-message transcript satisfies the invariant", ok)
    check("D-05: ...on the numbers this file actually wrote",
          ing == {"in": 107, "out": 13, "cached": 1000, "cache_write": 50}
          and watcher == (1157, 1000, 50, 13))


def test_the_known_fold_gap_is_directional():
    """The one shape where the two folds differ, stated so M-01 cannot break it.

    Streaming writes ONE `message.id` several times with `output_tokens`
    GROWING. `ingest.rollup` folds `$max` per id (first-wins under-reports
    output by 79.9% measured, which is why it does not). The watcher's cache is
    keyed by the same id but LAST-WINS, so its answer depends on the order the
    bytes arrived — that is CC-SESSIONS-4's finding and M-01's work, not this
    file's.

    Asserted as an INEQUALITY in the direction the gap can only go: last-wins
    can never exceed a max-fold. It holds today, and it still holds the day
    M-01 makes the watcher order-free and turns it into equality — so this
    cross-check never has to be edited to keep telling the truth.
    """
    print("test_the_known_fold_gap_is_directional")
    path = write_transcript("stream.jsonl", [
        ("m1", usage(fresh=10, out=100)),
        ("m1", usage(fresh=10, out=250)),
        ("m1", usage(fresh=10, out=40)),   # a late partial re-flush
    ])
    (_, _, _, w_out) = watcher_totals(path)
    ing = ingest_totals(path)
    check("D-05: ingest folds a re-flushed message id with $max (never "
          "first-wins, never a sum)", ing["out"] == 250)
    check("D-05: the watcher's total for a re-flushed id never EXCEEDS the "
          "max-fold (the gap is one-directional — M-01 closes it)",
          w_out <= ing["out"])
    check("D-05: neither implementation ever SUMS a re-flushed id (that is the "
          "2.09x over-count the corpus recorded)", w_out < 390 and ing["out"] < 390)


def test_the_id_less_row_divergence_is_stated_not_asserted_away():
    """The SECOND known divergence: a billed row carrying no `message.id`.

    The watcher counts it (its cache falls back to a per-row key), `ingest`
    drops it (`_usage_observation` returns None with no id). That is not a
    defect to "fix" in either direction from this file:

      * ingest's usage document IS keyed by the message id (`refs.usage_key`,
        R-50 / GD-24) — an id-less row has no key, and inventing one would put
        an unaddressable document in the mirror;
      * the watcher is an in-memory fold with no key space to protect, so
        dropping the row there would under-report a real bill.

    So the honest statement is the DIRECTION plus the corpus fact, the same
    shape :func:`test_the_known_fold_gap_is_directional` uses: the watcher's
    total can only be ≥ ingest's, and the frozen corpus contains **zero**
    id-less billed rows, which is why the corpus arm above is an equality and
    not a bound. If a future harness starts emitting them, that last assertion
    fails and this comment is the explanation waiting for whoever reads it.
    """
    print("test_the_id_less_row_divergence_is_stated_not_asserted_away")
    path = os.path.join(_BASE, "noid.jsonl")
    with open(path, "w") as f:
        for i in range(2):
            f.write(json.dumps({
                "type": "assistant", "timestamp": "2026-07-30T00:00:00.000Z",
                "uuid": f"u{i}", "sessionId": "11111111-2222-3333-4444-555555555555",
                "message": {"model": "claude-opus-5",
                            "usage": usage(fresh=5, out=5)}}) + "\n")
    (w_in, _, _, w_out) = watcher_totals(path)
    ing = ingest_totals(path)
    check("D-05: the watcher counts both id-less rows", w_in == 10 and w_out == 10)
    check("D-05: ingest drops an id-less row (its usage document is keyed by "
          "message.id — R-50)", ing["in"] == 0 and ing["out"] == 0)
    check("D-05: the divergence is one-directional — the watcher can only be "
          "the LARGER of the two",
          w_out >= ing["out"] and w_in >= ing["in"] + ing["cached"] + ing["cache_write"])
    corpus_noid = 0
    for corpus_path in corpus_transcripts():
        for line in corpus_path.read_text(errors="replace").splitlines():
            try:
                message = (json.loads(line) or {}).get("message")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                corpus_noid += 0 if message.get("id") else 1
    if not corpus_transcripts():
        skip("the id-less-row census over the frozen corpus: tests/fixtures "
             "is absent (packaged copy or fixtures-less checkout)")
    else:
        check("D-05: ...and the frozen corpus has NO id-less billed row, which "
              "is why the corpus arm asserts equality", corpus_noid == 0)


# --- occupancy: the two implementations, again, on the OTHER quantity ------
#
# Everything above counts SPEND. Everything below reads a LEVEL: how full one
# agent's context window was at one instant (GD-LC-1). Same bytes, same two
# trees, same "they may never import each other" — so the same discipline.

#: Ingest's own names for GD-LC-1's three prompt components. `out` is
#: deliberately absent: occupancy EXCLUDES `output_tokens`, and the day someone
#: "corrects" that by adding it, this tuple is where the correction has to be
#: argued rather than slipped in.
PROMPT_FIELDS = ("in", "cached", "cache_write")


def ingest_prompt_total(usage):
    """GD-LC-1's three-component sum in INGEST's field names, or None.

    Two layers, and the split between them is the point.
    `ingest.usage_from_message` is used unmodified — it is the aggregator's own
    statement of which wire key is which Touch field (`ingest._USAGE_SOURCE`),
    and reusing it is what makes this side a second SPELLING of the rule rather
    than a transcription of the watcher's. The strictness on top of it is
    GD-LC-2's, because the two answer different questions: a STORED usage
    document wants all four keys always (GD-11), so `usage_from_message` writes
    a `null` component as 0 and is right to; an occupancy LEVEL with an
    unreadable component is not a level at all, and 0 is precisely the lie this
    feature exists to refuse. `type(v) is not int` and never `isinstance`,
    because `bool` is an `int` subclass and would read `true` as 1.
    `tests/fixtures/context/ctx-agent-no-usable-turn.jsonl` carries a float, a
    null and a bool for exactly this function.
    """
    if not isinstance(usage, dict):
        return None
    for name in PROMPT_FIELDS:
        if type(usage.get(ingest._USAGE_SOURCE[name])) is not int:
            return None
    mapped = ingest.usage_from_message({"usage": usage})
    if mapped is None:
        return None
    total = sum(mapped[name] for name in PROMPT_FIELDS)
    # A zero prompt is not an occupancy: a `<synthetic>` row bills nothing, and
    # `ctx 0` on a killed agent's card is a fabrication (GD-LC-12).
    return total if total > 0 else None


def ingest_candidates(path):
    """Every qualifying occupancy reading of one transcript, in line order.

    GD-LC-10's read-time projection, computed over the lines `ingest.py` itself
    reads (`tailer.read_complete_lines` -> `ingest.parse_line`) and through
    `ingest.usage_from_message`'s mapping. It is written HERE rather than in the
    module because GD-LC-10 leaves `ingest.py` untouched — 8932 adopts
    `agent.ctx` off the events stream and measures nothing — so this file is
    where the aggregator's arithmetic is executable at all, and the equality
    below is the only thing keeping it honest.

    GD-LC-2's five clauses, each in this tree's own terms: `type ==
    "assistant"`; a `msg_`-prefixed `message.id` (ingest keys its usage document
    by that id — R-50 — so a row without one is not addressable here either); a
    prompt total > 0; `message.model != "<synthetic>"`; and the record's own
    `agentId` verified against the one `ingest.agent_id_for_path` reads out of
    the FILE NAME, so a foreign row is dropped rather than attributed. A
    `usage.iterations` list of length > 1 reads `iterations[-1]`, which is
    unambiguously one API call's prompt — the top level of such a row is the
    aggregate of several and describes a prompt that never existed.

    A `compact_boundary` is deliberately NOT read: it is a `system` record with
    no `message.usage`, so no usage document exists for it and no projection
    over them can see one. That gap is stated, not papered over, in
    :func:`test_the_compaction_gap_is_the_shape_a_usage_projection_cannot_see`.
    """
    path_agent = ingest.agent_id_for_path(path)
    candidates = []
    for line in ingest.tailer.read_complete_lines(path):
        record, _error = ingest.parse_line(line.text)
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        ts = record.get("timestamp")
        if not isinstance(ts, str) or not ts:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id.startswith("msg_"):
            continue
        if message.get("model") == "<synthetic>":
            continue
        recorded = record.get("agentId")
        if path_agent and isinstance(recorded, str) and recorded != path_agent:
            continue
        usage = message.get("usage")
        iterations = usage.get("iterations") if isinstance(usage, dict) else None
        if isinstance(iterations, list) and len(iterations) > 1:
            usage = iterations[-1]
        used = ingest_prompt_total(usage)
        if used is None:
            continue
        model = message.get("model")
        candidates.append((ts, line.line_no, used,
                           model if isinstance(model, str) else None))
    return candidates


def ingest_occupancy(path):
    """The aggregator side's reading: greatest `(timestamp, line)`, or None.

    `sort ts desc, limit 1` — never `max` over the readings themselves. The two
    coincide on every transcript that has not compacted, which is why `max` is
    the tempting implementation and why
    :func:`test_the_compaction_specimen_separates_latest_from_max` exists.
    """
    candidates = ingest_candidates(path)
    if not candidates:
        return None
    ts, _line, used, model = max(candidates, key=lambda c: (c[0], c[1]))
    return {"used": used, "at": ts, "model": model}


def stored_usage_occupancy(path):
    """The same projection run over the STORED `usage` documents alone.

    Literally `sort ts desc, limit 1, project in+cached+cache_write` against
    `ingest.read_transcript(...).usage` — the documents the Mongo mirror
    actually writes, with no access to the transcript line behind them. It is
    here to be COMPARED with :func:`ingest_occupancy`, never to replace it: see
    :func:`test_the_stored_usage_document_cannot_answer_two_of_the_shapes`.
    """
    best = None
    for obs in ingest.read_transcript(path).usage:
        if obs.ts is None or not obs.message_id.startswith("msg_"):
            continue
        used = sum(obs.tokens[name] for name in PROMPT_FIELDS)
        if used <= 0:
            continue
        if best is None or obs.ts > best[0]:
            best = (obs.ts, used)
    return None if best is None else best[1]


def watcher_occupancy(path, agent_id):
    """`agent.ctx` the way decision_watcher derives it, for ONE transcript.

    The shipped path end to end — the incremental parse notes the candidates,
    `_fold_context` picks the greatest `(timestamp, path order, line)` across
    the agent's fragments, `ctx_field` renders the wire block — for the same
    reason :func:`watcher_totals` calls `_transcript_usage`: the thing under
    test has to be the code that runs.

    The agent's entry is popped first because `peak` deliberately folds the
    PREVIOUS reading in (it is the one monotone key, and a compaction must not
    lower the high-water mark it just dropped from), so a call that inherited a
    peak from the transcript read before it would be reading its own history.
    """
    dw._LAST_CONTEXT.pop(agent_id, None)
    dw._transcript_usage(path)
    dw._fold_context(agent_id, [path])
    return dw.ctx_field(agent_id)


def occupancy_agrees(path, agent_id):
    """`(ok, watcher block, ingest reading)` under the LC-06 invariant.

    Absence is part of the equality, not an exemption from it: a transcript
    that resolves on one side and not the other is a disagreement, because
    "unknown" is a value here (GD-LC-12 — the key is ABSENT, never 0).

    All three of the fields a reader acts on are compared, not just the number:
    `at` because it is the SOURCE row's own stamp and two sides that agreed on
    a total while disagreeing about which row it came from would be agreeing by
    luck, and `model` because it is `cap`'s lookup key (GD-LC-6) — the wrong
    model is the wrong denominator, which is a percentage nobody can audit.
    """
    watcher = watcher_occupancy(path, agent_id)
    projection = ingest_occupancy(path)
    if (watcher is None) != (projection is None):
        return False, watcher, projection
    if watcher is None:
        return True, watcher, projection
    return (watcher["used"] == projection["used"]
            and watcher["at"] == projection["at"]
            and watcher.get("model") == projection["model"]), watcher, projection


def context_specimen(name):
    """One frozen LC-01 specimen, or None outside a repo checkout."""
    path = CTX_FIX / name
    return path if path.is_file() else None


def test_the_two_implementations_agree_on_occupancy_over_the_frozen_corpus():
    """The LC-06 equality, per transcript and then as a census of the corpus."""
    print("test_the_two_implementations_agree_on_occupancy_over_the_frozen_corpus")
    paths = corpus_transcripts()
    if not paths:
        skip("occupancy cross-check over the frozen corpus: tests/fixtures is "
             "absent (clean checkout / packaged copy)")
        return
    disagree = []
    resolved = 0
    level_over_spend = []
    for index, path in enumerate(paths):
        # A fresh synthetic agent id per transcript: these files are one
        # agent's fragment each, and reusing an id would carry `peak` across
        # two unrelated windows.
        ok, watcher, projection = occupancy_agrees(str(path), f"crosscheck{index:03d}")
        if not ok:
            disagree.append((path.name, watcher, projection))
        if watcher:
            resolved += 1
            if watcher["used"] > watcher_totals(str(path))[0]:
                level_over_spend.append(path.name)
    check(f"LC-06: all {len(paths)} frozen transcripts agree on occupancy — the "
          f"same row, the same sum (disagreements: {disagree[:2]})", not disagree)
    check(f"LC-06: ...and all {len(paths)} of them actually RESOLVE one "
          f"(resolved {resolved}; a green run over two Nones would prove "
          "nothing)", resolved == len(paths))
    check("LC-06: the level is never the spend — occupancy is one row's prompt "
          f"and can only be <= the sum over every row ({level_over_spend[:2]})",
          not level_over_spend)


def test_the_occupancy_census_is_why_this_arm_is_an_equality():
    """The two corpus facts that license an equality instead of a bound.

    `latest` is only well defined if every billed record is dated and no two
    share a moment inside one file; otherwise the greatest-timestamp rule has a
    tie to break and the two implementations could break it differently (the
    watcher orders by the raw STRING, this file's projection by the same
    string, `ingest` itself by a parsed Date). The corpus says both counts are
    zero, so the tie-break is unreachable and the arm above is an equality
    rather than "agrees up to ordering". If a future harness starts emitting
    undated or same-millisecond rows, this is the assertion that fails first
    and this docstring is the explanation waiting for whoever reads it.
    """
    print("test_the_occupancy_census_is_why_this_arm_is_an_equality")
    paths = corpus_transcripts()
    if not paths:
        skip("the occupancy census over the frozen corpus: tests/fixtures is "
             "absent (packaged copy or fixtures-less checkout)")
        return
    billed = undated = shared = 0
    for path in paths:
        seen = set()
        for line in path.read_text(errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            message = record.get("message")
            if not (isinstance(message, dict)
                    and isinstance(message.get("usage"), dict)):
                continue
            billed += 1
            ts = record.get("timestamp")
            if not isinstance(ts, str) or not ts:
                undated += 1
                continue
            if ts in seen:
                shared += 1
            seen.add(ts)
    check(f"LC-06: 0 of {billed} billed records lack a timestamp (undated "
          f"{undated})", undated == 0)
    check(f"LC-06: 0 timestamps are shared within a file across those {billed} "
          f"records (shared {shared}) — which is why this arm is an equality "
          "and not a bound", shared == 0)


def write_ctx_transcript(name, rows, agent_id=None):
    """One synthetic transcript for the LEVEL arm; rows are `(ts, id, usage)`.

    Separate from :func:`write_transcript` on purpose: that one writes every row
    at ONE timestamp with ids the occupancy rule does not accept (`m1`), which
    is fine for a total and meaningless for a latest-row reading. Each row here
    carries the agent id in the record, so the identity clause has something to
    verify against the file name.
    """
    path = os.path.join(_BASE, name)
    with open(path, "w") as handle:
        for index, (ts, message_id, row_usage) in enumerate(rows):
            record = {"type": "assistant", "timestamp": ts,
                      # A REAL uuid shape: `ingest.bucket_of` refuses anything
                      # else to the `records` bucket, and a specimen that is
                      # quietly invisible to half of ingest proves less than it
                      # looks like it proves.
                      "uuid": f"c0ffee00-0000-4000-8000-{index:012d}",
                      "sessionId": "11111111-2222-3333-4444-555555555555",
                      "message": {"id": message_id, "model": "claude-opus-5",
                                  "usage": row_usage}}
            if agent_id:
                record["agentId"] = agent_id
            handle.write(json.dumps(record) + "\n")
    return path


def test_a_constructed_occupancy_agrees_and_is_a_level_not_a_total():
    """The invariant on bytes this file wrote, so it holds with no corpus.

    Three rows, the last one LOWER than the second — the shape a compaction
    produces and the shape a `max` implementation gets wrong. A fourth row
    stamped with somebody else's `agentId` gets its own copy of the file, so
    that the spend figures asserted here stay about three rows: the identity
    clause is the LEVEL rule's (GD-LC-2), and the token fold has no equivalent
    because it is fed one agent's paths by the caller.
    """
    print("test_a_constructed_occupancy_agrees_and_is_a_level_not_a_total")
    mine = "c0ffee0000000c001"
    rows = [
        ("2026-07-30T01:00:01.000Z", "msg_c1", usage(fresh=3, out=700, write=30000)),
        ("2026-07-30T01:00:02.000Z", "msg_c2", usage(fresh=2, out=900, cached=30003,
                                                     write=1200)),
        ("2026-07-30T01:00:03.000Z", "msg_c3", usage(fresh=2, out=400, cached=17000,
                                                     write=200)),
    ]
    path = write_ctx_transcript(f"agent-{mine}.jsonl", rows, agent_id=mine)
    ok, watcher, projection = occupancy_agrees(path, mine)
    check("LC-06: a constructed transcript agrees on occupancy", ok)
    check("LC-06: ...on the number this file actually wrote — the LAST row's "
          "2+17000+200, not the largest row's 31,205",
          watcher["used"] == 17202 and projection["used"] == 17202)
    check("LC-06: ...stamped with that row's OWN timestamp, never the read "
          "moment", watcher["at"] == "2026-07-30T01:00:03.000Z")
    check("LC-06: `peak` is the one monotone aggregate and is NOT the reading "
          "(31,205 > 17,202 — a level that went down is a fact, not a fault)",
          watcher["peak"] == 31205)
    check("LC-06: no window is declared here, so no `cap` travels — a bar with "
          "a guessed denominator is the R-58 defect (GD-LC-6.4/6.5)",
          "cap" not in watcher)
    ing = ingest_totals(path)
    check("LC-06: the occupancy is ingest's in+cached+cache_write OF THAT ROW, "
          "and comparing it against ingest's `in` (7 here — every row's fresh "
          "input) would be AGENT-IDENTITY-12's trap, not a check",
          watcher["used"] != ing["in"] and ing["in"] == 7)
    check("LC-06: ...and the SPEND over the same bytes is a different number "
          "entirely (78,410 vs 17,202) — the two never derive from each other",
          watcher_totals(path)[0] == 78410)
    ok_totals, _, _ = agrees(path)
    check("LC-06: the GD-M2.2 spend invariant still holds on these bytes too "
          "(one transcript, two independent contracts)", ok_totals)
    # The identity clause, on its own copy: a NEWER row belonging to another
    # agent. Attributing it would be the worst kind of fabrication — a real
    # number, from the wrong window. (The watcher also COUNTS the drop; that
    # assertion is `tests/monitoring/test_context_occupancy.py`'s, not this
    # file's.)
    foreign = "c0ffee0000000c002"
    intruded = write_ctx_transcript(f"agent-{foreign}.jsonl", rows, agent_id=foreign)
    with open(intruded, "a") as handle:
        handle.write(json.dumps({
            "type": "assistant", "timestamp": "2026-07-30T01:00:04.000Z",
            "uuid": "c0ffee00-0000-4000-8000-000000000004",
            "agentId": "b0000000000000002",
            "sessionId": "11111111-2222-3333-4444-555555555555",
            "message": {"id": "msg_foreign", "model": "claude-opus-5",
                        "usage": usage(fresh=999999, out=1)}}) + "\n")
    ok, watcher, projection = occupancy_agrees(intruded, foreign)
    check("LC-06: a NEWER row stamped with another agentId is dropped by both "
          "sides — still 17,202, never the 999,999 it claims",
          ok and watcher["used"] == 17202 and projection["used"] == 17202)


def test_the_compaction_specimen_separates_latest_from_max():
    """LC-01.1: the one specimen where `max` and `latest` cannot both be right.

    Named apart from the corpus arm on purpose. `max`-over-turns agrees with
    `latest` on 100 % of the real corpus, so a wrong implementation is green
    everywhere until the first compaction — at which point occupancy legitimately
    GOES DOWN and `max` reports a window that emptied hours ago as still full.
    A red run here says "compaction", not "the corpus disagrees".
    """
    print("test_the_compaction_specimen_separates_latest_from_max")
    path = context_specimen("ctx-agent-compaction-boundary.jsonl")
    if path is None:
        skip("the compaction arm: tests/fixtures/context is absent (packaged "
             "copy or fixtures-less checkout)")
        return
    ok, watcher, projection = occupancy_agrees(str(path), "compaction-specimen")
    candidates = [used for _ts, _line, used, _model in ingest_candidates(str(path))]
    check("LC-06: both implementations agree on the compaction specimen", ok)
    check("LC-06: ...and both pick LATEST — 18,000, the row after the boundary",
          watcher["used"] == 18000 and projection["used"] == 18000)
    check("LC-06: ...where `max` over the same turns reads 120,000, so the two "
          f"rules are measurably different here ({max(candidates)})",
          max(candidates) == 120000 and watcher["used"] < max(candidates))
    check("LC-06: `peak` keeps the 120,000 high-water mark the reading dropped "
          "from — the ONE sanctioned aggregate, and it is not the reading",
          watcher["peak"] == 120000)
    check("LC-06: the newest record here is a usage row, so no `src` label "
          "travels (`src: compact` is the provisional branch only)",
          "src" not in watcher)


def test_the_compaction_gap_is_the_shape_a_usage_projection_cannot_see():
    """The one shape the two sides DIVERGE on, stated rather than asserted away.

    Between a `compact_boundary` and the next API call no usage row lands at
    all, so for the whole gap the newest usage row describes a window that no
    longer exists — a naive last-row reader overstates 19x (measured; 10x on
    these bytes). GD-LC-3 gives the live half the better answer: read
    `compactMetadata.postTokens`, stamp it with the BOUNDARY's own timestamp,
    label it `src: "compact"`.

    The aggregator side cannot reach that, and not by oversight: a boundary is a
    `system` record with no `message.usage`, so it produces no usage document
    for any projection to sort. That is exactly why GD-LC-10 has 8932 ADOPT the
    emitted `agent.ctx` off the events stream instead of recomputing it — the
    watcher's reading travels, and history gains it. The direction is the
    statement: during the gap a usage-document projection can only be the
    LARGER of the two, never the smaller, so this divergence can overstate a
    window and can never invent an empty one.

    The specimen is the frozen file truncated at its boundary — a PREFIX of
    those bytes, which is literally what the file looked like during the gap.
    """
    print("test_the_compaction_gap_is_the_shape_a_usage_projection_cannot_see")
    source = context_specimen("ctx-agent-compaction-boundary.jsonl")
    if source is None:
        skip("the compaction-gap arm: tests/fixtures/context is absent "
             "(packaged copy or fixtures-less checkout)")
        return
    lines = [line for line in source.read_text().splitlines() if line.strip()]
    cut = None
    for index, line in enumerate(lines):
        record = json.loads(line)
        if (record.get("type") == "system"
                and record.get("subtype") == "compact_boundary"):
            cut = index
            break
    if cut is None:
        check("LC-06: the compaction specimen carries a compact_boundary "
              "record (without one this arm asserts nothing)", False)
        return
    # Keep the `isCompactSummary` user line the harness writes right after the
    # boundary: it is part of the shape, and it carries no usage either.
    while cut + 1 < len(lines) and json.loads(lines[cut + 1]).get("isCompactSummary"):
        cut += 1
    path = os.path.join(_BASE, "compaction-gap.jsonl")
    with open(path, "w") as handle:
        handle.write("\n".join(lines[:cut + 1]) + "\n")
    watcher = watcher_occupancy(path, "compaction-gap")
    projection = ingest_occupancy(path)
    check("LC-06: in the gap the watcher reads the boundary's postTokens "
          "(11,970), not the 120,000 row that preceded it",
          watcher["used"] == 11970)
    check("LC-06: ...stamped with the BOUNDARY's own timestamp and labelled "
          "`src: compact`, so the provenance of a non-usage-row reading is on "
          "the wire", watcher["src"] == "compact"
          and watcher["at"] == "2026-07-31T20:02:41.311Z")
    check("LC-06: ...and `peak` still remembers the 120,000 it dropped from",
          watcher["peak"] == 120000)
    check("LC-06: the usage-document projection cannot see a compact_boundary "
          "(a `system` record writes no usage document), so it holds the stale "
          "120,000", projection["used"] == 120000)
    check("LC-06: the divergence is one-directional — during a gap the "
          "projection can only be the LARGER, which is why 8932 ADOPTS the "
          "emitted block rather than recomputing one (GD-LC-10)",
          projection["used"] > watcher["used"])


def test_the_stored_usage_document_cannot_answer_two_of_the_shapes():
    """Why the aggregator side of this cross-check is not a four-field lookup.

    GD-LC-10 describes 8932's occupancy as a read-time projection over the
    `usage` collection — `sort ts desc, limit 1, project in+cached+cache_write`.
    Run literally, over the stored documents alone, that projection is right on
    every real transcript in the corpus and wrong on two of LC-01's specimens,
    because a `usage` document is a four-field `$max` fold that has thrown away
    the two things GD-LC-2's rule still needs:

      * `usage.iterations` — a multi-iteration row stores its TOP LEVEL, which
        is the aggregate of several API calls. That is the right number for
        SPEND (every iteration was billed) and a prompt that never existed as a
        level: 65,690 stored against a 22,131 reading, a 3x overstatement;
      * a component the level cannot read — `usage_from_message` writes a `null`
        as 0 because a stored document needs all four keys (GD-11), so a row
        whose `cache_read_input_tokens` is `null` still lands, and the
        projection reports 24,502 for an agent whose window is genuinely
        unknowable. Fabricating a plausible number where the honest answer is
        "the key is absent" is the R-58 defect class exactly.

    So the equality above is stated against :func:`ingest_occupancy`, which
    carries GD-LC-2's clauses, and this test pins the difference so that
    whoever implements the Mongo-side projection reads it BEFORE shipping a
    silently wrong one. Nothing here is a defect in `ingest.py`: its documents
    answer the money question correctly and GD-LC-10 leaves it untouched.
    """
    print("test_the_stored_usage_document_cannot_answer_two_of_the_shapes")
    iterations = context_specimen("ctx-agent-iterations-multi.jsonl")
    unusable = context_specimen("ctx-agent-no-usable-turn.jsonl")
    if iterations is None or unusable is None:
        skip("the stored-document arm: tests/fixtures/context is absent "
             "(packaged copy or fixtures-less checkout)")
        return
    ok, watcher, projection = occupancy_agrees(str(iterations), "iterations-specimen")
    check("LC-06: both implementations read a multi-iteration row as "
          "iterations[-1] — one API call's prompt (22,131)",
          ok and watcher["used"] == 22131 and projection["used"] == 22131)
    check("LC-06: ...where the STORED four fields sum to 65,690, the top-level "
          "aggregate — right for spend, a prompt that never existed as a level",
          stored_usage_occupancy(str(iterations)) == 65690)
    ok, watcher, projection = occupancy_agrees(str(unusable), "unusable-specimen")
    check("LC-06: a transcript with no readable turn is unknown on BOTH sides — "
          "the block is ABSENT, never a zero", ok and watcher is None
          and projection is None)
    check("LC-06: ...where the stored documents would answer 24,502, because a "
          "`null` component is stored as 0 (GD-11) and a level cannot be",
          stored_usage_occupancy(str(unusable)) == 24502)
    elsewhere = corpus_transcripts() + [
        context_specimen("ctx-agent-retry-attempt1.jsonl"),
        context_specimen("ctx-agent-retry-attempt2.jsonl"),
        context_specimen("ctx-agent-compaction-boundary.jsonl")]
    everywhere_else = []
    for path in elsewhere:
        if path is None:
            continue
        reading = ingest_occupancy(str(path))
        if (reading or {}).get("used") != stored_usage_occupancy(str(path)):
            everywhere_else.append(path.name)
    check("LC-06: ...and on every other frozen transcript the two agree, so "
          f"the gap is those two shapes and not a general one ({everywhere_else[:2]})",
          not everywhere_else)


def test_no_tokens_suppresses_the_reading_rather_than_zeroing_it():
    """`--no-tokens`: a suppressed reading is ABSENT, never a rendered zero.

    The live-process half (no `tokens` events, spawns and results intact) is in
    `tests/monitoring/test_watcher.py`. What matters here is the unit contract
    every call site rests on: `token_totals` returns None, and None makes the
    `agent` block's `tokens` key vanish. A 0 would read as "this agent burned
    nothing" on every dashboard that folds `agent.tokens` last-wins.
    """
    print("test_no_tokens_suppresses_the_reading_rather_than_zeroing_it")
    check("D-05: the token plane is ON by default (the watcher is the live "
          "view's source until 8932 convergence)", dw.NO_TOKENS is False)
    saved = dw.NO_TOKENS
    try:
        dw.NO_TOKENS = True
        check("D-05: token_totals reports absence, not zero",
              dw.token_totals("a" * 17) is None)
        check("D-05: an absent reading yields no `tokens` field",
              dw.tokens_field(None) is None)
        block = dw.agent_block("a" * 17, {"plan": "p", "role": "r", "attempt": 1,
                                          "stage": "r"}, "done",
                               tokens=dw.tokens_field(dw.token_totals("a" * 17)))
        check("D-05: ...so the agent block carries no tokens key at all",
              "tokens" not in block)
        state = {"agents": {}, "tok_emitted": {}, "tok_tick_at": {"a": 1.0}}
        emitted = []
        real_emit = dw.emit
        dw.emit = lambda *a, **k: emitted.append((a, k))
        try:
            dw.flush_agent_tokens(state, "a", force=True)
        finally:
            dw.emit = real_emit
        check("D-05: even a FORCED flush writes nothing with the plane off",
              emitted == [])
        check("D-05: ...and the cadence window is still evicted (the call site "
              "is still where the agent stops being ticked)",
              "a" not in state["tok_tick_at"])
    finally:
        dw.NO_TOKENS = saved
    check("D-05: reading `tokens` is restored afterwards",
          dw.tokens_field((1, 2, 3, 4)) == {"in": 1, "out": 4, "cached": 2,
                                            "cache_write": 3})


for _test in (test_the_two_implementations_agree_over_the_frozen_corpus,
              test_a_constructed_transcript_agrees_field_by_field,
              test_the_known_fold_gap_is_directional,
              test_the_id_less_row_divergence_is_stated_not_asserted_away,
              test_the_two_implementations_agree_on_occupancy_over_the_frozen_corpus,
              test_the_occupancy_census_is_why_this_arm_is_an_equality,
              test_a_constructed_occupancy_agrees_and_is_a_level_not_a_total,
              test_the_compaction_specimen_separates_latest_from_max,
              test_the_compaction_gap_is_the_shape_a_usage_projection_cannot_see,
              test_the_stored_usage_document_cannot_answer_two_of_the_shapes,
              test_no_tokens_suppresses_the_reading_rather_than_zeroing_it):
    _test()

print()
for message in SKIPS:
    print(f"skipped: {message}")
if FAILS:
    print(f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print(f"\nALL TOKEN CROSS-CHECK TESTS PASSED ({len(SKIPS)} skipped)")
