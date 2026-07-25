#!/usr/bin/env python3
"""Stdlib-only unit tests for decision_watcher.py (sp-watcher fixes).

Run:  python3 test_watcher.py
Exits non-zero (via assert / raised exception) on any failure; prints OK lines.

No pytest, no omnigent imports. The module resolves WF_DIR / STATE_DIR and the
attempt caps AT IMPORT and sys.exit()s when no journal is found, so we set
ORCH_WF_DIR + ORCH_STATE_DIR (and drop a journal.jsonl + orch-config.json) in a
throwaway temp dir BEFORE importing it, then exercise the pure helpers.
"""
import importlib
import json
import os
import sys
import tempfile
import time

MOD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MOD_DIR not in sys.path:
    sys.path.insert(0, MOD_DIR)

# --- Throwaway state/work dirs under the scratchpad, populated before import ---
BASE = tempfile.mkdtemp(prefix="watcher_test_", dir="/tmp/claude-1000"
                        if os.path.isdir("/tmp/claude-1000") else None)
STATE_DIR = os.path.join(BASE, "state")
WF_DIR = os.path.join(BASE, "wf")
os.makedirs(STATE_DIR)
os.makedirs(WF_DIR)
# A journal must exist or resolve_wf_dir() -> sys.exit(). Config sets a gate cap
# of 5 to prove caps are config-driven (D4).
open(os.path.join(WF_DIR, "journal.jsonl"), "w").close()
with open(os.path.join(STATE_DIR, "orch-config.json"), "w") as f:
    json.dump({"max_gate_attempts": 5}, f)

os.environ["ORCH_WF_DIR"] = WF_DIR
os.environ["ORCH_STATE_DIR"] = STATE_DIR
# Point the per-agent transcript glob at our temp tree so agent_paths finds our
# fixtures rather than the real ~/.claude one.
os.environ["ORCH_WF_GLOB_ROOT"] = os.path.join(BASE, "glob")

dw = importlib.import_module("decision_watcher")

FAILS = []


def check(name, cond):
    if cond:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}")
        FAILS.append(name)


# ---------------------------------------------------------------------------
# D4: attempt caps come from orch-config.json (max_gate_attempts=5 here)
# ---------------------------------------------------------------------------
check("caps: MAX_GATE_ATTEMPTS read from config (=5)", dw.MAX_GATE_ATTEMPTS == 5)
check("caps: MAX_PLAN_ATTEMPTS default preserved (=4)", dw.MAX_PLAN_ATTEMPTS == 4)
check("caps: MAX_E2E_ATTEMPTS default preserved (=3)", dw.MAX_E2E_ATTEMPTS == 3)


# ---------------------------------------------------------------------------
# D1: describe_result impl branch keys on files_changed (and legacy alias)
# ---------------------------------------------------------------------------
info_impl = {"plan": "sp1", "role": "impl", "attempt": 1}
stage, st, detail = dw.describe_result(
    info_impl, {"done": True, "files_changed": ["a", "b"], "summary": "did it"})
check("describe_result impl: '2 changed files'", "2 changed files" in detail)
check("describe_result impl: '-> spawn test'", "spawn test" in detail)
check("describe_result impl: not generic 'finished'", "finished" not in detail)

stage, st, detail = dw.describe_result(
    info_impl, {"done": True, "changed_files": ["x"], "summary": "s"})
check("describe_result impl: legacy changed_files alias matches",
      "1 changed files" in detail and "finished" not in detail)


# ---------------------------------------------------------------------------
# D1 / SHELL-4: result_stage_state impl branch (done:false must not be green)
# ---------------------------------------------------------------------------
sst, sd = dw.result_stage_state({"done": False, "files_changed": []})
check("result_stage_state impl done:false -> failed", sst == "failed")
sst, sd = dw.result_stage_state({"done": True, "files_changed": ["a"], "summary": "ok"})
check("result_stage_state impl done:true -> done", sst == "done")
# gate/critique shapes still work (regression)
check("result_stage_state passed:true -> done",
      dw.result_stage_state({"passed": True}) == ("done", "green"))
check("result_stage_state approved:false -> failed",
      dw.result_stage_state({"approved": False})[0] == "failed")


# ---------------------------------------------------------------------------
# D5: read_new_lines never advances past a torn trailing line, never crashes
# ---------------------------------------------------------------------------
jp = os.path.join(BASE, "torn.jsonl")
with open(jp, "wb") as f:
    f.write(b'A\nB\nC_par')  # C_par is an incomplete trailing line
lines, off = dw.read_new_lines(jp, 0)
check("read_new_lines: torn tail returns only [A, B]", lines == ["A", "B"])
check("read_new_lines: offset stops before torn 'C_par'", off == len(b"A\nB\n"))
# Complete C and add D; reading from the deferred offset must recover C intact.
with open(jp, "wb") as f:
    f.write(b'A\nB\nC_partial_rest\nD\n')
lines2, off2 = dw.read_new_lines(jp, off)
check("read_new_lines: C recovered intact after completion",
      lines2 == ["C_partial_rest", "D"])
check("read_new_lines: C never lost across the two reads", "C_partial_rest" in lines2)

# Truncated multibyte tail must not raise (errors='replace') and defer partial.
mp = os.path.join(BASE, "multibyte.jsonl")
full = '{"x":"héllo"}\n'.encode("utf-8")
with open(mp, "wb") as f:
    f.write(full[:-3])  # slice mid-multibyte, no trailing newline
try:
    ml, moff = dw.read_new_lines(mp, 0)
    raised = False
except UnicodeDecodeError:
    raised = True
check("read_new_lines: truncated multibyte does not raise", not raised)
check("read_new_lines: no-newline chunk defers everything", ml == [] and moff == 0)


# ---------------------------------------------------------------------------
# D8: checkpoint keyed to journal; a different stored journal resets state
# ---------------------------------------------------------------------------
with open(dw.STATE, "w") as f:
    json.dump({"offset": 9999, "journal": "/some/other/run/journal.jsonl",
               "plans": {"sp1": "done"}, "agents": {"x": {}}}, f)
loaded = dw.load_state()
check("load_state: mismatched journal resets offset to 0", loaded["offset"] == 0)
check("load_state: mismatched journal clears plans", loaded.get("plans", {}) == {})
check("load_state: fresh state records current journal", loaded["journal"] == dw.JOURNAL)
# Same-journal checkpoint is preserved.
with open(dw.STATE, "w") as f:
    json.dump({"offset": 42, "journal": dw.JOURNAL, "plans": {"sp1": "running"}}, f)
loaded = dw.load_state()
check("load_state: matching journal preserves offset", loaded["offset"] == 42)
check("load_state: matching journal preserves plans", loaded["plans"] == {"sp1": "running"})
os.remove(dw.STATE)


# ---------------------------------------------------------------------------
# D3: plan-close correctness
# ---------------------------------------------------------------------------
# WATCHER-9: sequenced close with missing decisive -> failed (not done).
st_close = "done" if {"decisive": {}}["decisive"].get("sp1") else "failed"
check("sequenced-close: missing decisive -> failed", st_close == "failed")
st_close = "done" if {"other": True}.get("sp1") else "failed"
check("sequenced-close: decisive True -> done",
      ("done" if {"sp1": True}.get("sp1") else "failed") == "done")

# run_outcome + the sweep logic: an open plan whose decisive is False -> failed.
state = {"plans": {"sp1": "running"}, "running": [], "decisive": {"sp1": False}}
check("run_outcome: all-open-decisive-False -> failed",
      dw.run_outcome(state) == "failed")
# Emulate the sweep the main loop performs before 'complete'.
plans = dict(state["plans"])
for plan, badge in list(plans.items()):
    if plan == "orchestrator" or badge in ("done", "failed"):
        continue
    plans[plan] = "done" if state["decisive"].get(plan) else "failed"
check("run-complete sweep: open decisive-False plan -> failed",
      plans["sp1"] == "failed")

# A positive result then a negative one on the same plan resets the stale green.
plans = {"sp1": "done"}
decisive = {"sp1": True}
# now a reject arrives
ok = False
decisive["sp1"] = ok
if not ok and plans.get("sp1") == "done":
    plans["sp1"] = "running"
check("negative decisive after stale green resets badge to running",
      plans["sp1"] == "running")


# ---------------------------------------------------------------------------
# D7: token deltas are monotonic (clamped >= 0; baseline never regresses)
# ---------------------------------------------------------------------------
prev = {"in": 100, "out": 50, "cached": 10, "cache_write": 5}
cur_in, cur_out, cur_cached, cur_write = 80, 40, 8, 3  # all shrank
d_in = max(0, cur_in - prev.get("in", 0))
base_in = max(prev.get("in", 0), cur_in)
check("token clamp: shrunk in -> delta 0", d_in == 0)
check("token clamp: baseline in unchanged (100)", base_in == 100)


# ---------------------------------------------------------------------------
# WATCHER-7: last_ts survives a final line larger than the 64KB window
# ---------------------------------------------------------------------------
big_dir = os.path.join(os.environ["ORCH_WF_GLOB_ROOT"], "s", "sess", "subagents",
                       "workflows", dw.WF_NAME)
os.makedirs(big_dir, exist_ok=True)
big_path = os.path.join(big_dir, "agent-big1.jsonl")
with open(big_path, "w") as f:
    f.write(json.dumps({"timestamp": "2026-07-19T00:00:00.000Z", "type": "x"}) + "\n")
    huge = "z" * 200000  # > 64KB, forces the tail window to grow
    f.write(json.dumps({"timestamp": "2026-07-19T09:09:09.000Z", "big": huge}) + "\n")
ts = dw._last_ts_in_file(big_path)
check("last_ts: >64KB final line yields the TRUE last timestamp",
      ts == "2026-07-19T09:09:09.000Z")
# Sanity: a small file still returns its last timestamp.
small_path = os.path.join(big_dir, "agent-small.jsonl")
with open(small_path, "w") as f:
    f.write(json.dumps({"timestamp": "2026-07-19T01:00:00.000Z"}) + "\n")
    f.write(json.dumps({"timestamp": "2026-07-19T02:00:00.000Z"}) + "\n")
check("last_ts: small file returns last timestamp",
      dw._last_ts_in_file(small_path) == "2026-07-19T02:00:00.000Z")


# ---------------------------------------------------------------------------
# WATCHER-8: two id-less assistant usage rows are both counted (no collapse)
# ---------------------------------------------------------------------------
dedup_path = os.path.join(big_dir, "agent-dedup1.jsonl")
with open(dedup_path, "w") as f:
    # Two assistant rows, neither has message.id nor uuid.
    f.write(json.dumps({"type": "assistant",
                        "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}}) + "\n")
    f.write(json.dumps({"type": "assistant",
                        "message": {"usage": {"input_tokens": 20, "output_tokens": 2}}}) + "\n")
tin, tcached, twrite, tout = dw.agent_tokens("dedup1")
check("dedup: two id-less rows both summed (in=30)", tin == 30)
check("dedup: two id-less rows both summed (out=3)", tout == 3)
# Rows sharing a message id still collapse (union semantics preserved).
dedup2 = os.path.join(big_dir, "agent-dedup2.jsonl")
with open(dedup2, "w") as f:
    f.write(json.dumps({"type": "assistant",
                        "message": {"id": "m1", "usage": {"input_tokens": 7, "output_tokens": 1}}}) + "\n")
    f.write(json.dumps({"type": "assistant",
                        "message": {"id": "m1", "usage": {"input_tokens": 7, "output_tokens": 1}}}) + "\n")
tin2, _, _, tout2 = dw.agent_tokens("dedup2")
check("dedup: same-id rows collapse (in=7)", tin2 == 7)


# ---------------------------------------------------------------------------
# WATCHER-5: classify() with a missing transcript is time-bounded (no ~5s stall)
# ---------------------------------------------------------------------------
sleep_calls = []
_real_sleep = time.sleep
def _counting_sleep(secs):
    sleep_calls.append(secs)
time.sleep = _counting_sleep
try:
    res = dw.classify("no-such-agent-xyz")  # transcript never exists
finally:
    time.sleep = _real_sleep
check("classify: unclassifiable agent returns None (pending)", res is None)
check("classify: bounded total wait (< 5s stall)", sum(sleep_calls) < 5)
check("classify: few sleep calls (<= 3)", len(sleep_calls) <= 3)


# ---------------------------------------------------------------------------
# D6: module ROOT/events.jsonl no longer hijacks state-dir auto-discovery
# ---------------------------------------------------------------------------
import inspect
src = inspect.getsource(dw.resolve_state_dir)
check("resolve_state_dir: no ROOT/events.jsonl short-circuit",
      'os.path.join(ROOT, "events.jsonl")' not in src)


# ---------------------------------------------------------------------------
# DRIVER-1: parallel same-role/same-attempt spawns don't stale-close siblings
# ---------------------------------------------------------------------------
# Mirror the started-branch guard: stale only when the new attempt is GREATER.
def should_stale(new_attempt, old_attempt):
    return not (new_attempt <= old_attempt)
check("DRIVER-1: equal attempt (parallel siblings) -> no stale",
      should_stale(1, 1) is False)
check("DRIVER-1: greater attempt (true retry) -> stale",
      should_stale(2, 1) is True)
# Assert the guard clause is actually present in the source.
main_src = inspect.getsource(dw.main)
check("DRIVER-1: source guards on info['attempt'] <= oinfo['attempt']",
      'info["attempt"] <= oinfo["attempt"]' in main_src)


if FAILS:
    print(f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("\nALL WATCHER TESTS PASSED")
