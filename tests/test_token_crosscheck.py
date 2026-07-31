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

The other half of D-05 — `--no-tokens`, which lets a deployment that has wired
the ingest tick make `ingest.rollup` the single REACHABLE implementation — is
exercised end to end in `tests/monitoring/test_watcher.py` (the live process,
its events, its agent blocks). What is asserted here is the unit contract the
watcher's own call sites rest on: a suppressed reading is ABSENT, never zero.

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
              test_no_tokens_suppresses_the_reading_rather_than_zeroing_it):
    _test()

print()
for message in SKIPS:
    print(f"skipped: {message}")
if FAILS:
    print(f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print(f"\nALL TOKEN CROSS-CHECK TESTS PASSED ({len(SKIPS)} skipped)")
