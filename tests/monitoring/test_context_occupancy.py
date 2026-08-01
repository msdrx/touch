#!/usr/bin/env python3
"""Producer tests for live context OCCUPANCY (LC-05, GD-LC-1..GD-LC-6/12).

Run:  python3 test_context_occupancy.py
Exits non-zero on any failure; prints one `ok`/`FAIL`/`skip` line per check.
Stdlib only, no pytest, no runner — registration is by `run_all.sh`'s glob.

What is under test is `decision_watcher.py` as the SOLE producer of the
additive `agent.ctx` block: the arithmetic (GD-LC-1), the qualifying-row
selection rule (GD-LC-2), the compaction branch (GD-LC-3), the wire shape
(GD-LC-4), the declared-only capacity (GD-LC-6) and — the arm that matters
most — the honest-unknown taxonomy (GD-LC-12), where unknown is spelled as the
KEY BEING ABSENT and never as `0`.

Occupancy is a LEVEL at an instant. `agent.tokens` is SPEND. Confusing the two
is CC-STORES-4's exact trap and the reason arm 13 exists.

The module resolves WF_DIR / STATE_DIR at IMPORT and `sys.exit()`s when it
finds no journal, so a throwaway run dir is prepared BEFORE it is imported —
the same preamble `tests/monitoring/test_watcher.py` and
`tests/test_token_crosscheck.py` use, for the same reason.
"""
import atexit
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

# The module under test is named through `tests/_roots.py` (GD-U1/GD-U6): this
# file lives in `tests/monitoring/`, the module it imports does not.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _roots import MON, REPO                              # noqa: E402

MOD_DIR = str(MON)
if MOD_DIR not in sys.path:
    sys.path.insert(0, MOD_DIR)

FIX = REPO / "tests" / "fixtures"
CTX_FIX = FIX / "context"

# --- Throwaway state/work dirs, populated before import --------------------
BASE = tempfile.mkdtemp(prefix="ctxocc_test_", dir="/tmp/claude-1000"
                        if os.path.isdir("/tmp/claude-1000") else None)
# Swept whatever the exit path: this suite runs on every gate, and a per-run
# temp tree left behind is how a /tmp exhaustion incident starts (one on record).
atexit.register(shutil.rmtree, BASE, ignore_errors=True)
STATE_DIR = os.path.join(BASE, "state")
WF_DIR = os.path.join(BASE, "wf")
GLOB_ROOT = os.path.join(BASE, "glob")
os.makedirs(STATE_DIR)
os.makedirs(WF_DIR)
open(os.path.join(WF_DIR, "journal.jsonl"), "w").close()
with open(os.path.join(STATE_DIR, "orch-config.json"), "w") as _f:
    json.dump({}, _f)

os.environ["ORCH_WF_DIR"] = WF_DIR
os.environ["ORCH_STATE_DIR"] = STATE_DIR
os.environ["ORCH_WF_GLOB_ROOT"] = GLOB_ROOT
# The declared window must come from the CONFIG in this process: an inherited
# ORCH_CONTEXT_WINDOW would pin it at import and make arms 10-12 vacuous. The
# env-pin itself is proved in its own subprocess arm, which is the only honest
# way to test an import-time constant.
os.environ.pop("ORCH_CONTEXT_WINDOW", None)

import decision_watcher as dw                             # noqa: E402

WF_NAME = os.path.basename(os.path.normpath(WF_DIR))

FAILS = []
SKIPS = []


def check(name, cond):
    if cond:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}")
        FAILS.append(name)


def skip(name):
    """Record a check that could not run, and say so in one printed line.

    Fixture-backed arms replay the repo's FROZEN corpus, which the monitoring
    module never carries: outside a repo checkout it is simply absent, and
    skipping loudly is the only honest answer. `tests/run_all.sh` counts these
    lines so a green suite never silently means "the corpus vanished".
    """
    print(f"skip - {name}")
    SKIPS.append(name)


# --------------------------------------------------------------------------
# Staging helpers. Every specimen is written where agent_paths() looks for it —
# <GLOB_ROOT>/<project>/<session>/subagents/workflows/<wf>/agent-<id>.jsonl —
# because GD-LC-2 resolves the agentId FROM THE PATH and verifies the record's
# own `agentId` against it. A fixture read in place (its `ctx-` name is
# deliberate, see PROVENANCE.md) would not be addressable that way.
# --------------------------------------------------------------------------
def transcript_path(agent_id, session="s1"):
    d = os.path.join(GLOB_ROOT, "proj", session, "subagents", "workflows", WF_NAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"agent-{agent_id}.jsonl")


def write_rows(agent_id, rows, session="s1", mode="w"):
    """Write/append JSON records as the harness writes them: one per line."""
    path = transcript_path(agent_id, session)
    with open(path, mode) as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    return path


def stage_fixture(name, agent_id, session="s1", lines=None):
    """Copy a frozen `context/` specimen in under its agentId-encoded name.

    `lines` truncates the copy to a prefix, which is how GD-LC-3's provisional
    branch is reached without a second frozen file (PROVENANCE.md says so).
    Returns None when the corpus is absent so the caller can skip loudly.
    """
    src = CTX_FIX / name
    if not src.is_file():
        return None
    raw = src.read_bytes().split(b"\n")
    if lines is not None:
        raw = raw[:lines]
    path = transcript_path(agent_id, session)
    with open(path, "wb") as fh:
        for line in raw:
            if line:
                fh.write(line + b"\n")
    return path


def reset():
    """Forget every memo, so each arm reads its own bytes and nothing else."""
    dw._USAGE_CACHE.clear()
    dw._LAST_CONTEXT.clear()


def assistant(agent_id, mid, ts, prompt_in, cache_write, cache_read,
              out=100, model="claude-opus-5", usage_extra=None):
    """One billed assistant record in the harness's shape."""
    usage = {"input_tokens": prompt_in,
             "cache_creation_input_tokens": cache_write,
             "cache_read_input_tokens": cache_read,
             "output_tokens": out}
    if usage_extra:
        usage.update(usage_extra)
    return {"parentUuid": None, "isSidechain": True, "agentId": agent_id,
            "type": "assistant", "uuid": f"u-{mid}", "timestamp": ts,
            "message": {"id": mid, "model": model, "role": "assistant",
                        "type": "message", "usage": usage,
                        "content": [{"type": "text", "text": "..."}]}}


def read(agent_id):
    """The reading the watcher would emit for this agent, or None."""
    dw.agent_tokens(agent_id)          # the funnel that folds the occupancy
    return dw.ctx_field(agent_id)


def on_stderr(fn, *args):
    """`(result, captured stderr)` — the watcher warns there and nowhere else."""
    err = io.StringIO()
    saved = sys.stderr
    try:
        sys.stderr = err
        result = fn(*args)
    finally:
        sys.stderr = saved
    return result, err.getvalue()


def emitted():
    if not os.path.isfile(dw.EVENTS):
        return []
    return [json.loads(ln) for ln in open(dw.EVENTS) if ln.strip()]


SRC_TEXT = open(os.path.join(MOD_DIR, "decision_watcher.py")).read()


# --------------------------------------------------------------------------
# Arm 1 — GD-LC-1: the arithmetic, on hand-written bytes
# --------------------------------------------------------------------------
reset()
write_rows("a000000000000ar01", [
    assistant("a000000000000ar01", "msg_arith1", "2026-07-31T10:00:00.000Z",
              5, 1000, 20000, out=999)])
arith = read("a000000000000ar01")
check("arm 1: used == input + cache_creation + cache_read (the documented "
      "statusline formula, GD-LC-1)",
      arith is not None and arith["used"] == 21005)
check("arm 1: output_tokens is EXCLUDED — occupancy is the prompt, not the turn",
      arith is not None and arith["used"] != 21005 + 999)
check("arm 1: `at` is the SOURCE record's own timestamp, never the emit moment",
      arith is not None and arith["at"] == "2026-07-31T10:00:00.000Z")
check("arm 1: the model travels verbatim (it is the cap lookup key)",
      arith is not None and arith["model"] == "claude-opus-5")
check("arm 1: no percentage on the wire — pct is client-derivable (GD-LC-4)",
      arith is not None and "pct" not in arith and "percent" not in arith)


# --------------------------------------------------------------------------
# Arm 2 — GD-LC-2: latest by TIMESTAMP, not last-in-file (catches dict order)
# --------------------------------------------------------------------------
reset()
aid = "a000000000000ar02"
write_rows(aid, [
    assistant(aid, "msg_shuf1", "2026-07-31T11:00:00.000Z", 1, 10000, 0),
    assistant(aid, "msg_shuf3", "2026-07-31T11:20:00.000Z", 1, 500, 60000),
    assistant(aid, "msg_shuf2", "2026-07-31T11:10:00.000Z", 1, 300, 30000)])
shuffled = read(aid)
check("arm 2: the greatest-TIMESTAMP row wins, not the last line in the file",
      shuffled is not None and shuffled["used"] == 60501)
check("arm 2: ...and not the newest insertion into the parse memo",
      shuffled is not None and shuffled["used"] != 30301)

# The identity verify has no fixture (0 mismatches in 1,769 frozen records), so
# it is constructed inline — GD-LC-2's "dropped and counted, NEVER attributed".
reset()
aid = "a000000000000ar2b"
_mismatch_before = dw._CTX_COUNTS["agent_id_mismatch"]
write_rows(aid, [
    assistant(aid, "msg_mine1", "2026-07-31T11:30:00.000Z", 1, 100, 9000),
    assistant("astrangerstranger", "msg_theirs1", "2026-07-31T11:40:00.000Z",
              1, 100, 500000)])
foreign, warned = on_stderr(read, aid)
check("arm 2: a row stamped with ANOTHER agentId is dropped — a foreign 500k "
      "prompt never lands on this agent's meter",
      foreign is not None and foreign["used"] == 9101)
check("arm 2: ...counted, and announced on the first occurrence (0 in the "
      "measured corpus, so the first wild one must be visible)",
      dw._CTX_COUNTS["agent_id_mismatch"] == _mismatch_before + 1
      and ("never" in warned if _mismatch_before == 0 else True))
reset()
aid = "a000000000000ar2c"
write_rows(aid, [{"parentUuid": None, "type": "assistant",
                  "uuid": "u-noaid", "timestamp": "2026-07-31T11:50:00.000Z",
                  "message": {"id": "msg_noaid1", "model": "claude-opus-5",
                              "role": "assistant", "type": "message",
                              "usage": {"input_tokens": 1,
                                        "cache_creation_input_tokens": 100,
                                        "cache_read_input_tokens": 7000,
                                        "output_tokens": 9}}}])
check("arm 2: a record carrying NO agentId is unverifiable, not mismatched — "
      "it sits in the file the glob resolved from an agentId",
      (read(aid) or {}).get("used") == 7101)


# --------------------------------------------------------------------------
# Arm 3 — a streaming re-flush is ONE turn: same id, growing output
# --------------------------------------------------------------------------
reset()
aid = "a000000000000ar03"
seen = []
for n, out in enumerate((12, 340, 1201)):
    write_rows(aid, [assistant(aid, "msg_stream1",
                               f"2026-07-31T12:00:0{n}.000Z", 2, 4000, 40000,
                               out=out)], mode="w" if n == 0 else "a")
    seen.append(read(aid)["used"])
check("arm 3: a re-flushed message (one id, growing output, identical prompt) "
      "is ONE reading, unchanged", seen == [44002, 44002, 44002])


# --------------------------------------------------------------------------
# Arm 4 — GD-LC-3: a compaction LOWERS occupancy; latest != max
# --------------------------------------------------------------------------
reset()
aid = "ac0badc0ffee00001"
if stage_fixture("ctx-agent-compaction-boundary.jsonl", aid) is None:
    skip("arm 4: compaction fixture (tests/fixtures/context/ absent)")
else:
    compacted = read(aid)
    check("arm 4: the reading after a compaction is the post-compaction row "
          "(18,000), not the pre-compaction high",
          compacted is not None and compacted["used"] == 18000)
    check("arm 4: `max` over turns would read 120,000 — latest != max, which "
          "is the only place in the corpus the two rules differ",
          compacted is not None and compacted["used"] < compacted["peak"]
          and compacted["peak"] == 120000)
    check("arm 4: occupancy is NON-monotonic — nothing clamps it up to the peak",
          compacted is not None and compacted["used"] == 18000)
    check("arm 4: a usage row newer than the boundary carries NO src",
          compacted is not None and "src" not in compacted)

    # Truncated after the isCompactSummary line: the newest boundary now
    # outranks the newest qualifying row, which is GD-LC-3's provisional branch
    # (no usage row lands until the next API call; a naive last-row reader
    # overstates 19x for the whole gap).
    reset()
    stage_fixture("ctx-agent-compaction-boundary.jsonl", aid, lines=8)
    provisional = read(aid)
    check("arm 4: boundary newer than the newest usage row -> the reading is "
          "compactMetadata.postTokens",
          provisional is not None and provisional["used"] == 11970)
    check("arm 4: ...stamped with the BOUNDARY's own timestamp",
          provisional is not None
          and provisional["at"] == "2026-07-31T20:02:41.311Z")
    check("arm 4: ...and labelled src: compact",
          provisional is not None and provisional["src"] == "compact")
    check("arm 4: preTokens (120,030) is a DIFFERENT estimator and never "
          "reaches the wire",
          provisional is not None and provisional["used"] != 120030
          and provisional["peak"] == 120000)


# --------------------------------------------------------------------------
# Arm 5 — the cross-session fragment union, ordered explicitly (no mtime)
# --------------------------------------------------------------------------
frag_a = (FIX / "run-wf_829e6f58" / "dd469822-2546-47d9-aaa3-31db4cb705e8"
          / "subagents" / "workflows" / "wf_829e6f58-b2f"
          / "agent-a2fc883c96ff7b837.jsonl")
frag_b = (FIX / "run-wf_829e6f58" / "e423cd3c-f859-45af-9afd-0d6bdec9b4ac"
          / "subagents" / "workflows" / "wf_829e6f58-b2f"
          / "agent-a2fc883c96ff7b837.jsonl")
if not (frag_a.is_file() and frag_b.is_file()):
    skip("arm 5: frozen cross-session fragment pair (corpus absent)")
else:
    _real_paths = dw.agent_paths
    try:
        for order, label in (([str(frag_a), str(frag_b)], "oldest first"),
                             ([str(frag_b), str(frag_a)], "reversed")):
            reset()
            dw.agent_paths = lambda _aid, _o=order: list(_o)
            union = read("a2fc883c96ff7b837")
            check(f"arm 5: the union reads the newest fragment's 145,827 "
                  f"({label}) — the timestamp decides, never the path order "
                  f"and never mtime",
                  union is not None and union["used"] == 145827)
    finally:
        dw.agent_paths = _real_paths


# --------------------------------------------------------------------------
# Arm 6 — GD-LC-12: unknown is the KEY BEING ABSENT, never 0
# --------------------------------------------------------------------------
reset()
aid = "a5eeded000000000f"
if stage_fixture("ctx-agent-no-usable-turn.jsonl", aid) is None:
    skip("arm 6: no-usable-turn fixture (tests/fixtures/context/ absent)")
else:
    check("arm 6: a transcript with a float, a null, a bool and a <synthetic> "
          "529 row yields NO reading — never ctx 0 on a killed agent",
          read(aid) is None)
    block = dw.agent_block(aid, {"plan": "p", "stage": "s", "role": "impl",
                                 "attempt": 1}, "failed",
                           tokens={"in": 1, "out": 1, "cached": 0,
                                   "cache_write": 0},
                           ctx=dw.ctx_field(aid))
    check("arm 6: the emitted agent block carries no `ctx` KEY (absence, not "
          "falsiness — a {'used': 0} dict is truthy and must never be built)",
          "ctx" not in block)

reset()
empty = transcript_path("a000000000000ar06")
open(empty, "w").close()
check("arm 6: an EMPTY transcript is unknown, not zero",
      read("a000000000000ar06") is None)
reset()
check("arm 6: an ABSENT transcript is unknown, not zero",
      read("a000000000000nope") is None)
reset()
write_rows("a000000000000ar6b", [
    {"type": "user", "agentId": "a000000000000ar6b",
     "timestamp": "2026-07-31T13:00:00.000Z",
     "message": {"role": "user", "content": "\n[monitor] plan=p role=impl attempt=1\n"}}])
check("arm 6: spawned with no assistant turn yet is unknown, not zero",
      read("a000000000000ar6b") is None)


# --------------------------------------------------------------------------
# Arm 7 — --no-tokens suppresses ctx too: one switch, one read, no third state
# --------------------------------------------------------------------------
reset()
aid = "a000000000000ar07"
write_rows(aid, [assistant(aid, "msg_notok1", "2026-07-31T14:00:00.000Z",
                           3, 5000, 50000)])
_saved_no_tokens = dw.NO_TOKENS
try:
    dw.NO_TOKENS = True
    check("arm 7: --no-tokens reads no transcript at all (token_totals is None)",
          dw.token_totals(aid) is None and not dw._LAST_CONTEXT)
    # Even with a reading already in hand, the flag suppresses the field:
    # context is a by-product of exactly the parse D-05 turns off.
    dw._LAST_CONTEXT[aid] = {"used": 55003, "at": "2026-07-31T14:00:00.000Z",
                             "model": "claude-opus-5", "peak": 55003}
    check("arm 7: ...and ctx_field stays None even so — no third state",
          dw.ctx_field(aid) is None)
finally:
    dw.NO_TOKENS = _saved_no_tokens
reset()
check("arm 7: with the flag off the same bytes DO resolve (anti-vacuity)",
      (read(aid) or {}).get("used") == 55003)


# --------------------------------------------------------------------------
# Arm 8 — GD-LC-2: usage.iterations len > 1 reads iterations[-1]
# --------------------------------------------------------------------------
reset()
aid = "adeadbeef00000002"
_multi_before = dw._CTX_COUNTS["iterations_multi"]
if stage_fixture("ctx-agent-iterations-multi.jsonl", aid) is None:
    skip("arm 8: iterations fixture (tests/fixtures/context/ absent)")
else:
    iters, iter_warned = on_stderr(read, aid)
    check("arm 8: a len-3 iterations list reads iterations[-1] (22,131), one "
          "API call's prompt",
          iters is not None and iters["used"] == 22131)
    check("arm 8: the top-level SUM (65,690) is a prompt that never existed "
          "and never reaches the wire",
          iters is not None and iters["used"] != 65690)
    check("arm 8: the named counter records the branch, and the first wild "
          "occurrence is announced on stderr rather than merely tallied",
          dw._CTX_COUNTS["iterations_multi"] == _multi_before + 1
          and ("iterations[-1]" in iter_warned if _multi_before == 0 else True))

reset()
aid = "a000000000000ar08"
write_rows(aid, [assistant(aid, "msg_iter1", "2026-07-31T15:00:00.000Z",
                           2, 21687, 0, usage_extra={"iterations": [
                               {"input_tokens": 2, "cache_creation_input_tokens": 21687,
                                "cache_read_input_tokens": 0, "output_tokens": 118}]})])
check("arm 8: a len-1 iterations list is read at the TOP level (all 7,256 "
      "sampled rows agree there)", (read(aid) or {}).get("used") == 21689)


# --------------------------------------------------------------------------
# Arm 9 — the wire is not a delta, and the TERMINAL line carries the block
# --------------------------------------------------------------------------
reset()
aid = "a000000000000ar09"
state = {"tok_emitted": {}, "tok_tick_at": {}, "agents": {}, "running": []}
info = {"plan": "sp-ctx", "stage": "implement", "role": "impl", "attempt": 1}
write_rows(aid, [assistant(aid, "msg_lvl1", "2026-07-31T16:00:00.000Z",
                           5, 10000, 79995, out=500)])
before = len(emitted())
dw.flush_agent_tokens(state, aid, info, totals=dw.agent_tokens(aid), force=True)
high = emitted()[before:]
check("arm 9: the terminal flush line carries the ctx block — the parse memos "
      "are evicted before that emit, so the reading has to outlive them "
      "(WATCHER-EMIT-3)",
      len(high) == 1 and high[0]["agent"].get("ctx", {}).get("used") == 90000)
check("arm 9: the flush POPS the reading — an agent that has stopped being "
      "ticked leaves nothing behind to re-attach", aid not in dw._LAST_CONTEXT)
check("arm 9: the detail carries a short ` · ctx ` clause and no percentage",
      " · ctx 90.0k" in high[0]["detail"] and "%" not in high[0]["detail"])
check("arm 9: the detail obeys GD-11 — one line, no double quotes, under 1 KB",
      "\n" not in high[0]["detail"] and '"' not in high[0]["detail"]
      and len(high[0]["detail"]) <= dw.DETAIL_CAP)

# A compaction lands: the level GOES DOWN while spend goes up.
write_rows(aid, [assistant(aid, "msg_lvl2", "2026-07-31T16:05:00.000Z",
                           2, 11998, 0, out=600)], mode="a")
before = len(emitted())
dw.flush_agent_tokens(state, aid, info, totals=dw.agent_tokens(aid), force=True)
low = emitted()[before:]
low_ctx = low[0]["agent"].get("ctx", {}) if low else {}
check("arm 9: a LOWER second reading travels verbatim — never clamped up to "
      "the previous value (the D7 monotone rule must not touch occupancy)",
      low_ctx.get("used") == 12000)
check("arm 9: ...while `peak` keeps the high-water mark, recomputed from a "
      "full re-walk after the eviction", low_ctx.get("peak") == 90000)
check("arm 9: ...and the cumulative SPEND beside it still only grows",
      low[0]["agent"]["tokens"]["in"] == 102000)
check("arm 9: the reading is absolute, not a delta — 12,000 is a level, not "
      "12,000 more of anything", low_ctx.get("used") != 102000)


# --------------------------------------------------------------------------
# Arm 10 — GD-LC-6.1: DECLARED capacity, int form and {model: int} form
# --------------------------------------------------------------------------
reset()
aid = "a000000000000ar10"
write_rows(aid, [assistant(aid, "msg_cap1", "2026-07-31T17:00:00.000Z",
                           4, 20000, 130000)])
read(aid)   # 150,004 for every capacity arm below
_saved_cfg = dw.read_config()
try:
    dw.apply_caps({})
    check("arm 10: undeclared is the DEFAULT — no cap, and that is correct, "
          "not degraded", "cap" not in dw.ctx_field(aid))
    dw.apply_caps({"context_window": 1000000})
    check("arm 10: an int context_window applies to every model",
          dw.ctx_field(aid).get("cap") == 1000000)
    dw.apply_caps({"context_window": {"claude-opus-5": 1000000,
                                      "claude-fable-5": 200000}})
    check("arm 10: a {model: int} map is keyed on the reading's own model",
          dw.ctx_field(aid).get("cap") == 1000000)
    dw.apply_caps({"context_window": {"claude-haiku-4-5-20251001": 200000}})
    check("arm 10: a map with no entry for this model declares nothing — no "
          "built-in model->window table, no neighbour's number",
          "cap" not in dw.ctx_field(aid))
    # The env PIN, proved where it actually lives: at import.
    env = dict(os.environ, ORCH_CONTEXT_WINDOW="500000",
               PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=MOD_DIR)
    probe = subprocess.run(
        [sys.executable, "-c",
         "import json, decision_watcher as d;"
         "d.apply_caps({'context_window': 200000});"
         "print(json.dumps([d.CONTEXT_WINDOW, d.context_cap('claude-opus-5')]))"],
        env=env, capture_output=True, text=True, timeout=60)
    pinned = json.loads(probe.stdout.strip() or "[null, null]")
    check("arm 10: ORCH_CONTEXT_WINDOW outranks the file, exactly as "
          "ORCH_TOKEN_TICK_SECS pins the tick",
          probe.returncode == 0 and pinned == [500000, 500000])
    env["ORCH_CONTEXT_WINDOW"] = '{"claude-opus-5": 1000000}'
    probe = subprocess.run(
        [sys.executable, "-c",
         "import json, decision_watcher as d;"
         "print(json.dumps(d.context_cap('claude-opus-5')))"],
        env=env, capture_output=True, text=True, timeout=60)
    check("arm 10: the env var carries the SAME grammar as the config key "
          "(one grammar, two sources)",
          probe.returncode == 0 and probe.stdout.strip() == "1000000")

    # ----------------------------------------------------------------------
    # Arm 11 — GD-LC-6.3: a contradicted window is OMITTED, and warns once
    # ----------------------------------------------------------------------
    dw._CTX_CAP_WARNED.clear()
    dw.apply_caps({"context_window": 50000})
    (first, second), warning = on_stderr(
        lambda: (dw.ctx_field(aid), dw.ctx_field(aid)))
    check("arm 11: used > cap -> `cap` is ABSENT from the wire (never a "
          ">100 % bar, never a clamp)",
          "cap" not in first and "cap" not in second)
    check("arm 11: ...the reading itself still travels — the measurement wins "
          "over the declaration", first.get("used") == 150004)
    check("arm 11: ...and exactly ONE stderr warning per (model, cap), not one "
          "per tick", warning.count("exceeds the declared window") == 1)

    # ----------------------------------------------------------------------
    # Arm 12 — GD-LC-6.2: bounds
    # ----------------------------------------------------------------------
    for bad in (999, 10_000_001):
        warned = len(dw._CFG_WARNINGS)
        dw.apply_caps({"context_window": bad})
        check(f"arm 12: a declared window of {bad} is out of bounds -> no cap "
              f"+ a deferred warning",
              dw.CONTEXT_WINDOW is None and len(dw._CFG_WARNINGS) > warned
              and "cap" not in dw.ctx_field(aid))
    warned = len(dw._CFG_WARNINGS)
    dw.apply_caps({"context_window": True})
    check("arm 12: `true` is refused — bool is an int SUBCLASS, and a 1-token "
          "window would render every agent as thousands of percent full",
          dw.CONTEXT_WINDOW is None and len(dw._CFG_WARNINGS) > warned)
    dw.apply_caps({"context_window": {"claude-opus-5": 1000000, "bad": "1M"}})
    check("arm 12: one bad entry drops ITSELF, not the whole map",
          dw.CONTEXT_WINDOW == {"claude-opus-5": 1000000})
    dw.apply_caps({"context_window": "not-a-window"})
    check("arm 12: an unparseable value leaves the window undeclared and says "
          "so", dw.CONTEXT_WINDOW is None)
finally:
    dw.apply_caps(_saved_cfg)


# --------------------------------------------------------------------------
# Arm 13 — the writer-honesty pair: the gauge is not fed the cumulative sum
# --------------------------------------------------------------------------
reset()
aid = "afeedface00000031"
if stage_fixture("ctx-agent-retry-attempt1.jsonl", aid) is None:
    skip("arm 13: retry fixture (tests/fixtures/context/ absent)")
else:
    totals = dw.agent_tokens(aid)
    ctx = dw.ctx_field(aid)
    tokens = dw.tokens_field(totals)
    check("arm 13: ctx.used < tokens.in over a multi-turn agent — the level is "
          "not the sum (measured 9.4x apart at 12 turns)",
          ctx["used"] < tokens["in"] and ctx["used"] == 148900
          and tokens["in"] == 274295)
    check("arm 13: ctx.used IS the last qualifying row's three-component sum",
          ctx["used"] == 1 + 9600 + 139299)
    # Its retry successor is a SEPARATE agent with its own fresh window: no
    # cross-agent aggregate of context exists, and merging them would fabricate
    # a level neither agent ever held (GD-LC-7).
    aid2 = "afeedface00000032"
    stage_fixture("ctx-agent-retry-attempt2.jsonl", aid2)
    dw.agent_tokens(aid2)
    ctx2 = dw.ctx_field(aid2)
    check("arm 13: each retry row is its own agent with its own meter — a "
          "fresh window is never empty, and never summed with its predecessor",
          ctx2["used"] == 41200 and ctx2["used"] < ctx["used"])
    check("arm 13: a fresh window starts full of system prompt + tools + "
          "CLAUDE.md, never at 0 (min 21,641 over 610 measured agents)",
          ctx2["peak"] >= 27140)

check("arm 13: the detail clause is omitted ENTIRELY when there is no reading "
      "— never `ctx 0`, never `ctx ?`", dw.ctx_detail(None) == "")
check("arm 13: with a reading it is one short clause, no percentage",
      dw.ctx_detail({"used": 148900}) == " · ctx 148.9k"
      and dw.ctx_detail({"used": 148900, "cap": 1000000})
      == " · ctx 148.9k/1000.0k")
check("arm 13: ...single line, no double quotes (GD-11 at the writer)",
      "\n" not in dw.ctx_detail({"used": 1, "cap": 2000})
      and '"' not in dw.ctx_detail({"used": 1, "cap": 2000}))

# The SPAWN line must not carry it: no turn exists yet, so there is nothing to
# say. Pinned as source text because the omission is a property of the call
# site, not of a value that happens to be falsy today.
_spawn = SRC_TEXT[SRC_TEXT.index("attempt {info['attempt']} spawned"):]
_spawn = _spawn[:_spawn.index("prev = state.get")]
check("arm 13: the spawn emit passes no ctx (GD-LC-12: no turn, no reading)",
      "ctx" not in _spawn)


# --------------------------------------------------------------------------
# Standing invariants of the feature, asserted as source text
# --------------------------------------------------------------------------
check("invariant: zero new event kinds — the reading rides the existing token "
      "tick, and `emit(\"tokens\"` is still the only stage it is written under",
      SRC_TEXT.count('emit("tokens"') == 3 and 'emit("ctx' not in SRC_TEXT)
check("invariant: _LAST_CONTEXT lives OUTSIDE _USAGE_CACHE, which "
      "flush_agent_tokens evicts before its emit",
      "_LAST_CONTEXT: dict[str, dict] = {}" in SRC_TEXT
      and SRC_TEXT.index("_LAST_CONTEXT: dict")
      != SRC_TEXT.index("_USAGE_CACHE: dict"))
check("invariant: nothing about the reading is persisted to .watcher-state.json "
      "(a restart re-reads the transcripts and recovers both level and peak)",
      "ctx" not in SRC_TEXT[SRC_TEXT.index("def save_state"):
                            SRC_TEXT.index("def save_state") + 1500])
check("invariant: no 200000 fallback and no model->window table anywhere",
      "200000" not in SRC_TEXT and "200_000" not in SRC_TEXT)
check("invariant: the compaction reduction site warns the next reader that "
      "`max` is the wrong instinct, and names the fixture that proves it",
      "the next reader's instinct will be" in SRC_TEXT
      and "ctx-agent-compaction-boundary.jsonl" in SRC_TEXT)


# --------------------------------------------------------------------------
print()
if SKIPS:
    print(f"{len(SKIPS)} skipped (corpus absent): " + ", ".join(SKIPS))
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all context-occupancy checks passed")
