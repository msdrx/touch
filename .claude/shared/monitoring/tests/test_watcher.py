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
import shutil
import signal
import subprocess
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
SKIPS = []


def check(name, cond):
    if cond:
        print(f"ok   - {name}")
    else:
        print(f"FAIL - {name}")
        FAILS.append(name)


def skip(name):
    """Record a check that could not run, and say so in one printed line.

    The arms below replay REPO fixtures (`<repo>/tests/fixtures/**`), which the
    monitoring module never carries: outside this repo — a clean checkout, a
    packaged plugin — they are simply absent, and skipping loudly is the only
    honest answer. `tests/run_all.sh` counts these lines so a green suite never
    silently means "the corpus vanished".
    """
    print(f"skip - {name}")
    SKIPS.append(name)


def find_repo_fixtures():
    """`<repo>/tests/fixtures`, found by walking up — or None.

    Walking up rather than counting `..` hops: the module sits at
    `<repo>/.claude/shared/monitoring/` here and one level shallower inside a
    plugin, and a hop count that is right for one layout points at a stranger's
    directory in the other. None means "not in a repo checkout" and every arm
    below skips.

    The `legacy/` child is the discriminator, not `tests/fixtures` itself: this
    module has its own `tests/fixtures/` (the golden snapshot), which the walk
    passes through first and must not mistake for the repo corpus.
    """
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        cand = os.path.join(d, "tests", "fixtures")
        if os.path.isdir(os.path.join(cand, "legacy")):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


REPO_FIXTURES = find_repo_fixtures() or os.path.join(BASE, "no-repo-fixtures")


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

# SD-10: checkpoint identity is (st_dev, st_ino, size, offset), not the path
# alone — a journal REPLACED in place by a LARGER file keeps size >= offset, so
# the size-only shrink check cannot see it.
with open(dw.STATE, "w") as f:
    json.dump({"offset": 42, "journal": dw.JOURNAL, "journal_id": "1:999999999",
               "plans": {"sp1": "running"}, "agents": {"x": {}}}, f)
loaded = dw.load_state()
check("SD-10: a replaced journal (foreign inode) resets the offset to 0",
      loaded["offset"] == 0)
check("SD-10: a replaced journal clears derived plan state",
      loaded.get("plans", {}) == {})
with open(dw.STATE, "w") as f:
    json.dump({"offset": 42, "journal": dw.JOURNAL,
               "journal_id": dw.journal_identity(), "plans": {"sp1": "running"}}, f)
check("SD-10: the SAME journal+inode preserves the checkpoint",
      dw.load_state()["offset"] == 42)
check("SD-10: journal_identity is dev:ino", dw.journal_identity().count(":") == 1)
check("SD-10: a missing journal has no identity",
      dw.journal_identity(os.path.join(BASE, "nope.jsonl")) is None)
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
# WRITE-SIDE-10: the transcript parse is INCREMENTAL — appended bytes only
#
# agent_tokens() used to json.loads every line of every transcript copy on every
# ~1 s poll tick (4.5-11.0 ms per call on a 1 MB transcript, and growing with
# it). The arms below assert the WORK, not the wall clock (GD-G): how many lines
# were parsed, and that the totals are identical to a full re-read.
# ---------------------------------------------------------------------------
class _CountingJson:
    """json shim that counts loads() calls; everything else passes through."""

    def __init__(self, real):
        self._real = real
        self.loads_calls = 0

    def loads(self, *a, **k):
        self.loads_calls += 1
        return self._real.loads(*a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)


def counted_tokens(agent_id):
    """(totals, json.loads calls) for one agent_tokens() call."""
    real, counter = dw.json, _CountingJson(dw.json)
    dw.json = counter
    try:
        return dw.agent_tokens(agent_id), counter.loads_calls
    finally:
        dw.json = real


inc_path = os.path.join(big_dir, "agent-inc1.jsonl")
with open(inc_path, "w") as f:
    for i in range(3):
        f.write(json.dumps({"type": "assistant", "message": {
            "id": f"m{i}", "usage": {"input_tokens": 10, "output_tokens": 1}}}) + "\n")
(inc_in, _, _, inc_out), first_calls = counted_tokens("inc1")
check("incremental: cold read sums the whole file (in=30)", inc_in == 30)
check("incremental: the cold read parsed all 3 lines", first_calls == 3)
(inc_in2, _, _, _), warm_calls = counted_tokens("inc1")
check("incremental: an unchanged transcript is not re-parsed at all",
      warm_calls == 0 and inc_in2 == 30)
with open(inc_path, "a") as f:
    f.write(json.dumps({"type": "assistant", "message": {
        "id": "m3", "usage": {"input_tokens": 40, "output_tokens": 4}}}) + "\n")
(inc_in3, _, _, inc_out3), grow_calls = counted_tokens("inc1")
check("incremental: only the APPENDED line is parsed (1 call, not 4)",
      grow_calls == 1)
check("incremental: the totals match a full re-read (in=70)", inc_in3 == 70)
check("incremental: output totals too (out=7)", inc_out3 == 7)
# A torn tail (the harness is mid-append) is never consumed: the offset stops at
# the last newline, so the partial line is re-read once it completes.
with open(inc_path, "a") as f:
    f.write('{"type": "assistant", "message": {"id": "m4", "usage": {"input_')
(torn_in, _, _, _), torn_calls = counted_tokens("inc1")
check("incremental: a torn trailing line is deferred, not parsed",
      torn_in == 70 and torn_calls == 0)
with open(inc_path, "a") as f:
    f.write('tokens": 5, "output_tokens": 1}}}\n')
(healed_in, _, _, _), healed_calls = counted_tokens("inc1")
check("incremental: the completed line is recovered intact (in=75)",
      healed_in == 75 and healed_calls == 1)
# A transcript that SHRANK is different bytes: rescan from 0, like the journal
# tailer does with its checkpoint.
with open(inc_path, "w") as f:
    f.write(json.dumps({"type": "assistant", "message": {
        "id": "z0", "usage": {"input_tokens": 3, "output_tokens": 1}}}) + "\n")
(shrunk_in, _, _, _), shrunk_calls = counted_tokens("inc1")
check("incremental: a shrunken transcript is re-read from byte 0 (in=3)",
      shrunk_in == 3 and shrunk_calls == 1)
# The id-less fallback key stays unique across incremental reads (WATCHER-8
# survives): two id-less rows written in two separate reads must both count.
noid_path = os.path.join(big_dir, "agent-noid1.jsonl")
with open(noid_path, "w") as f:
    f.write(json.dumps({"type": "assistant",
                        "message": {"usage": {"input_tokens": 11}}}) + "\n")
dw.agent_tokens("noid1")
with open(noid_path, "a") as f:
    f.write(json.dumps({"type": "assistant",
                        "message": {"usage": {"input_tokens": 22}}}) + "\n")
noid_in, _, _, _ = dw.agent_tokens("noid1")
check("incremental: id-less rows read in separate passes both count (in=33)",
      noid_in == 33)
# The parse splits on \n and NOTHING else. str.splitlines() also splits on
# U+2028/U+2029 (and \x0b \x0c \x1c-\x1e \x85), and those two are legal
# UNESCAPED inside a JSON string — json.dumps(ensure_ascii=False) emits them raw,
# as the harness that writes these transcripts does. Under splitlines() such a
# row is torn into fragments that all fail json.loads, and because the offset has
# already advanced past them the billed row is dropped from this agent's total
# FOREVER: a silent, permanent under-report of the one number the token law rests
# on. This arm is the regression guard.
sep_path = os.path.join(big_dir, "agent-sep1.jsonl")
with open(sep_path, "w", encoding="utf-8") as f:
    f.write(json.dumps({"type": "assistant", "message": {
        "id": "s0", "text": "para\u2028graph\u2029break",  # raw separators, escaped here
        "usage": {"input_tokens": 500, "output_tokens": 5}}},
        ensure_ascii=False) + "\n")
check("splitlines: a raw U+2028/U+2029 in an assistant message is still ONE line",
      open(sep_path, "rb").read().count(b"\n") == 1)
sep_in, _, _, sep_out = dw.agent_tokens("sep1")
check("splitlines: ...so its billed row still counts in full (in=500, out=5)",
      sep_in == 500 and sep_out == 5)
# ...and the per-line fallback key stays aligned: an id-less row FOLLOWING a
# separator-bearing one must count once, not twice, across incremental reads.
with open(sep_path, "a", encoding="utf-8") as f:
    f.write(json.dumps({"type": "assistant",
                        "message": {"usage": {"input_tokens": 30}}}) + "\n")
sep_grown, _, _, _ = dw.agent_tokens("sep1")
sep_again, _, _, _ = dw.agent_tokens("sep1")
check("splitlines: an id-less row after it counts exactly once (in=530, stable)",
      sep_grown == 530 and sep_again == 530)

# The shrink guard compares against the OFFSET actually consumed, never a
# separately tracked size: the file can grow between the stat() and the read(),
# so a stored size sits BELOW the offset, and a later genuine truncation to a
# point between the two would slip past a size-based guard, seek beyond EOF and
# freeze this transcript's totals until it grew back past the stale offset.
shrink_path = os.path.join(big_dir, "agent-shrink1.jsonl")
shrink_rows = [json.dumps({"type": "assistant", "message": {
    "id": f"k{i}", "usage": {"input_tokens": 100}}}) + "\n" for i in range(4)]
with open(shrink_path, "w") as f:
    f.write("".join(shrink_rows))
check("shrink: the cold read sums the whole file (in=400)",
      dw.agent_tokens("shrink1")[0] == 400)
shrink_cache = dw._USAGE_CACHE[shrink_path]
check("shrink: the cache records only the consumed offset — no second size field",
      shrink_cache["offset"] == os.path.getsize(shrink_path)
      and "size" not in shrink_cache)
with open(shrink_path, "w") as f:  # truncated BELOW the offset we consumed
    f.write(shrink_rows[0] + shrink_rows[1])
check("shrink: a file truncated below that offset re-derives from byte 0 (in=200)",
      dw.agent_tokens("shrink1")[0] == 200)


# ---------------------------------------------------------------------------
# M1 / GD-D: the token-tick cadence ceiling (ORCH_TOKEN_TICK_SECS)
# ---------------------------------------------------------------------------
check("cadence: the shipped default is 15s", dw.TOKEN_TICK_DEFAULT == 15)
check("cadence: 15s stays far inside the page's 120s idle threshold",
      dw.TOKEN_TICK_DEFAULT <= 30)

_saved_tick, _saved_env = dw.TOKEN_TICK_SECS, dw._TOKEN_TICK_ENV
dw._TOKEN_TICK_ENV = None
check("cadence: token_tick_secs is read from orch-config.json",
      dw.apply_caps({"token_tick_secs": 7})[-1] == 7 and dw.TOKEN_TICK_SECS == 7)
check("cadence: an absent key keeps the default",
      dw.apply_caps({})[-1] == dw.TOKEN_TICK_DEFAULT)
_warns = len(dw._CFG_WARNINGS)
check("cadence: a garbage value falls back to the default, no raise",
      dw.apply_caps({"token_tick_secs": "often"})[-1] == dw.TOKEN_TICK_DEFAULT)
check("cadence: ...and queues a deferred warning",
      len(dw._CFG_WARNINGS) > _warns)
del dw._CFG_WARNINGS[_warns:]
check("cadence: a negative value reads as 0 (always due), never a frozen counter",
      dw.apply_caps({"token_tick_secs": -5})[-1] == 0)
dw._TOKEN_TICK_ENV = 3
check("cadence: ORCH_TOKEN_TICK_SECS PINS the value against a config re-publish",
      dw.apply_caps({"token_tick_secs": 60})[-1] == 3)
dw._TOKEN_TICK_ENV = _saved_env
dw.apply_caps(dw.read_config())  # restore the suite's import-time caps
dw.TOKEN_TICK_SECS = _saved_tick
check("cadence: the suite's caps are restored after the knob arms",
      dw.MAX_GATE_ATTEMPTS == 5)

# The env pin is established by the ENVIRONMENT, not only by the module global
# the arms above set by hand: one subprocess each closes that loop honestly, and
# proves the max(0, ...) clamp exists on the env path too (the config path is
# asserted above) — a typo must read as "always due", never freeze a counter.
def _tick_in_env(value):
    out = subprocess.run(
        [sys.executable, "-c",
         "import decision_watcher as d; print(d.TOKEN_TICK_SECS)"],
        env=dict(os.environ, ORCH_TOKEN_TICK_SECS=value),
        cwd=MOD_DIR, capture_output=True, text=True)
    return out.stdout.strip()


check("cadence: ORCH_TOKEN_TICK_SECS is read from the environment at import (=7)",
      _tick_in_env("7") == "7")
check("cadence: a negative env value clamps to 0 on the env path too",
      _tick_in_env("-5") == "0")

now = 1_000_000.0
# The ceiling is a defaulted ARGUMENT, so each arm states the value it means
# instead of assigning a module global that every later arm (and the live
# watcher's own poll loop) also reads.
check("cadence: an agent with no window entry is DUE (checkpoint predates the knob)",
      dw.token_tick_due("a1", now, {}, secs=15))
check("cadence: inside the window it is NOT due",
      not dw.token_tick_due("a1", now, {"a1": now - 5}, secs=15))
check("cadence: at the window boundary it is due again",
      dw.token_tick_due("a1", now, {"a1": now - 15}, secs=15))
check("cadence: a backwards clock step is due, not frozen for the difference",
      dw.token_tick_due("a1", now, {"a1": now + 300}, secs=15))
# The absent-window rule is the ONLY exemption M1 grants. A broader "has never
# published a counter" exemption would keep an agent with no billable activity on
# the 1 s poll — glob + stat + parse — until ABANDON_QUIET_SECS (1200 s by
# default), ~1200 reads where the cadence budgets ~80: precisely the cost this
# item exists to remove. Once an agent's window is stamped it is throttled like
# every other, emitted counter or not.
check("cadence: a stamped window throttles even an agent that has never "
      "published a counter (exactly one exemption, not two)",
      not dw.token_tick_due("never-emitted", now, {"never-emitted": now - 1}, secs=15))
check("cadence: 0 is the escape hatch — every tick is due (today's behaviour)",
      dw.token_tick_due("a1", now, {"a1": now}, secs=0))
# ...and with the argument omitted the LIVE global decides, which is what makes
# a refresh_caps() re-tune reach the running poll loop at all.
dw.TOKEN_TICK_SECS = 30
check("cadence: an omitted ceiling reads the live global, so a mid-run re-tune "
      "takes effect on the next tick",
      not dw.token_tick_due("a1", now, {"a1": now - 20})
      and dw.token_tick_due("a1", now, {"a1": now - 31}))
dw.TOKEN_TICK_SECS = _saved_tick

# WRITE-SIDE-2/10, guarded as source text: in the live tick block the cadence
# gates the transcript READ, and the non-zero-delta guard still sits between that
# read and the emit — so the ceiling can SUPPRESS a line and can never
# MANUFACTURE one. A cadence that could manufacture a tick would erase every
# stall segment the timeplan draws (all 17 on the measured run).
tick_src = open(os.path.join(MOD_DIR, "decision_watcher.py")).read()
tick_block = tick_src[tick_src.index('if state["running"]:'):]
# find(), not index(): a removal must fail as a check line, not as a traceback.
i_due = tick_block.find("token_tick_due(")
i_read = tick_block.find("agent_tokens(aid)")
i_guard = tick_block.find("if din or dout or dcached or dwrite:")
check("WRITE-SIDE-10: the throttle precedes the transcript READ (it gates the "
      "read, not just the emit)", -1 < i_due < i_read)
check("WRITE-SIDE-2: the non-zero-delta guard still sits between the read and "
      "the emit (a ceiling, never a floor)", -1 < i_read < i_guard)
# The load-bearing negative: nothing emits between the throttle and that guard,
# so no timer-driven line can exist.
i_emit = tick_block.find("emit(", i_due)
check("WRITE-SIDE-2: no emit( between the throttle and the delta guard — the "
      "cadence has no path to a heartbeat", i_emit == -1 or i_emit > i_guard)
check("M1: the journal tail poll is untouched at 1s (spawn latency is contract)",
      "def poll_sleep(seconds: float = 1.0" in tick_src)


# ---------------------------------------------------------------------------
# M2 / GD-D: flush_agent_tokens — the one unthrottled force-flush path
# ---------------------------------------------------------------------------
def emitted_events():
    if not os.path.isfile(dw.EVENTS):
        return []
    return [json.loads(ln) for ln in open(dw.EVENTS) if ln.strip()]


# The seeded window is this arm's anti-vacuity: the drop below has to remove
# something that was really there.
flush_state = {"tok_emitted": {}, "tok_tick_at": {"f1": 1.0}, "agents": {},
               "running": []}
info_a = {"plan": "sp-x", "stage": "implement", "role": "impl", "attempt": 1}
before = len(emitted_events())
dw.flush_agent_tokens(flush_state, "f1", info_a, totals=(500, 40, 10, 20))
flushed = emitted_events()[before:]
check("M2: a force-flush emits one tokens line", len(flushed) == 1)
check("M2: ...carrying the WIRE DELTA", flushed[0]["tokens"]["in"] == 500)
check("M2: ...and the agent's ABSOLUTE cumulative (GD-C)",
      flushed[0]["agent"]["tokens"] == {"in": 500, "out": 20, "cached": 40,
                                        "cache_write": 10})
check("M2: ...attributed to the agent AND its plan (WRITE-SIDE-5)",
      flushed[0]["agent"]["id"] == "f1" and flushed[0]["plan"] == "sp-x")
check("M2: the baseline advanced to the cumulative",
      flush_state["tok_emitted"]["f1"]["in"] == 500)
check("M2: the flush DROPS the cadence window — a flushed agent has stopped "
      "being ticked, so keeping it would grow the checkpoint by one dead entry "
      "per agent for the life of the run",
      "f1" not in flush_state["tok_tick_at"])
before = len(emitted_events())
dw.flush_agent_tokens(flush_state, "f1", info_a, totals=(500, 40, 10, 20))
check("M2: a second flush with nothing new stays SILENT",
      len(emitted_events()) == before)
dw.flush_agent_tokens(flush_state, "f1", info_a, totals=(500, 40, 10, 20), force=True)
forced = emitted_events()[before:]
check("M2: force=True states the total anyway (the rollup's closing line)",
      len(forced) == 1 and forced[0]["tokens"]["in"] == 0
      and forced[0]["agent"]["tokens"]["in"] == 500)
before = len(emitted_events())
dw.flush_agent_tokens(flush_state, "f1", info_a, totals=(400, 30, 5, 10))
check("M2: a regressed transcript never emits a negative delta (D7)",
      len(emitted_events()) == before
      and flush_state["tok_emitted"]["f1"]["in"] == 500)
# The exit sweep is the same helper over everything still in flight.
sweep_state = {"tok_emitted": {"s1": {"in": 100}}, "tok_tick_at": {},
               "agents": {"s1": info_a}, "running": ["s1"]}
_real_agent_tokens = dw.agent_tokens
dw.agent_tokens = lambda aid: (900, 0, 0, 0)
before = len(emitted_events())
try:
    dw.sweep_running_tokens(sweep_state)
finally:
    dw.agent_tokens = _real_agent_tokens
swept = emitted_events()[before:]
check("M2: the exit sweep flushes an agent that would never emit again",
      len(swept) == 1 and swept[0]["tokens"]["in"] == 800
      and swept[0]["agent"]["tokens"]["in"] == 900)
before = len(emitted_events())
dw.sweep_running_tokens({"tok_emitted": {}, "tok_tick_at": {}, "agents": {},
                         "running": []})
check("M2: an empty running list sweeps nothing", len(emitted_events()) == before)

# Every flush site is a point where the agent stops being ticked, so its
# per-transcript parse caches are dead weight from there on. Without the drop,
# the measured 167-agent run retains order-1e5 entries (four ints plus a message
# key each) for agents that resulted or were stale-closed hours earlier.
evict_path = os.path.join(big_dir, "agent-evict1.jsonl")
with open(evict_path, "w") as f:
    f.write(json.dumps({"type": "assistant", "message": {
        "id": "e0", "usage": {"input_tokens": 60}}}) + "\n")
check("evict: a read populates the parse cache (the arm's precondition)",
      dw.agent_tokens("evict1")[0] == 60 and evict_path in dw._USAGE_CACHE)
dw.flush_agent_tokens({"tok_emitted": {}, "tok_tick_at": {}, "agents": {},
                       "running": []}, "evict1", info_a)
check("evict: the terminal flush drops that agent's parse caches",
      evict_path not in dw._USAGE_CACHE)
check("evict: ...and dropping is lossless — a re-read rebuilds the same total",
      dw.agent_tokens("evict1")[0] == 60)
# Eviction reads the cache's own keys, never a second agent_paths() glob: the
# glob over the whole projects tree is the cost this pass exists to remove (it
# would be paid twice on every result, stale close and swept agent), and a
# transcript copy pruned or rotated away between the last read and the flush is
# no longer IN the glob — a glob-driven eviction would strand that entry for the
# daemon's whole life, since this cache has no other eviction path.
gone_path = os.path.join(big_dir, "agent-gone1.jsonl")
with open(gone_path, "w") as f:
    f.write(json.dumps({"type": "assistant", "message": {
        "id": "g0", "usage": {"input_tokens": 40}}}) + "\n")
dw.agent_tokens("gone1")
check("evict: the vanished-transcript arm has an entry to evict (precondition)",
      gone_path in dw._USAGE_CACHE)
os.remove(gone_path)  # pruned/rotated away under us, as agent_paths' union can
_real_agent_paths, _glob_calls = dw.agent_paths, []
dw.agent_paths = lambda aid: (_glob_calls.append(aid), _real_agent_paths(aid))[1]
try:
    dw.drop_usage_cache("gone1")
finally:
    dw.agent_paths = _real_agent_paths
check("evict: a transcript that DISAPPEARED still loses its cache entry",
      gone_path not in dw._USAGE_CACHE)
check("evict: ...and the eviction cost no second glob of the projects tree",
      _glob_calls == [])

# token_deltas is the shared arithmetic behind every emit site (D7).
d, b = dw.token_deltas({"in": 100, "out": 50, "cached": 10, "cache_write": 5},
                       80, 8, 3, 40)
check("token_deltas: every shrunk component clamps to 0",
      d == {"in": 0, "out": 0, "cached": 0, "cache_write": 0})
check("token_deltas: no baseline component regresses",
      b == {"in": 100, "out": 50, "cached": 10, "cache_write": 5})
d2, b2 = dw.token_deltas({"in": 100}, 250, 20, 10, 7)
check("token_deltas: growth is reported in full",
      d2 == {"in": 150, "out": 7, "cached": 20, "cache_write": 10})
check("token_deltas: and the new baseline is the cumulative", b2["in"] == 250)


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


# ---------------------------------------------------------------------------
# R-07: config ints tolerate garbage (deferred warning, never a crash)
# ---------------------------------------------------------------------------
check("_int_cfg: missing key -> default", dw._int_cfg({}, "nope", 7) == 7)
check("_int_cfg: good value parsed", dw._int_cfg({"k": "9"}, "k", 7) == 9)
_before = len(dw._CFG_WARNINGS)
check("_int_cfg: garbage value -> default, no raise",
      dw._int_cfg({"max_gate_attempts": "three"}, "max_gate_attempts", 3) == 3)
check("_int_cfg: garbage value queues a deferred warning",
      len(dw._CFG_WARNINGS) == _before + 1
      and "max_gate_attempts" in dw._CFG_WARNINGS[-1])
check("_int_env: unset -> default", dw._int_env("ORCH_NO_SUCH_VAR_XYZ", 42) == 42)


# ---------------------------------------------------------------------------
# GD-11 / R-10: detail is capped at 1 KB at the writer
# ---------------------------------------------------------------------------
long_detail = "x" * 9000
capped = dw.cap_detail(long_detail)
check("cap_detail: >1KB detail truncated to the cap", len(capped) == dw.DETAIL_CAP)
check("cap_detail: truncation is visible ('...')", capped.endswith("..."))
check("cap_detail: short detail untouched", dw.cap_detail("hi") == "hi")


# ---------------------------------------------------------------------------
# R-39: every watcher-written line carries w=watcher (additive to the 5-key shape)
# ---------------------------------------------------------------------------
_events_before = os.path.getsize(dw.EVENTS) if os.path.exists(dw.EVENTS) else 0
dw.emit("watcher", "info", "unit-test attribution line")
with open(dw.EVENTS) as f:
    f.seek(_events_before)
    emitted = json.loads(f.read().strip().splitlines()[-1])
check("R-39: watcher line carries w=watcher", emitted.get("w") == "watcher")
check("R-39: five-key core shape preserved",
      all(k in emitted for k in ("ts", "plan", "stage", "state", "detail")))
# The detail cap applies on the emit path too, not just in cap_detail().
dw.emit("watcher", "info", "y" * 5000)
with open(dw.EVENTS) as f:
    capped_ev = json.loads(f.read().strip().splitlines()[-1])
check("R-10: emit truncates detail to the cap",
      len(capped_ev["detail"]) == dw.DETAIL_CAP)


# ---------------------------------------------------------------------------
# GD-9 / R-13: marker grammar — window, order-independence, [touch] identity
# ---------------------------------------------------------------------------
prompt_ordered = ("\n[monitor] plan=research stage=liveio role=research attempt=1\n"
                  "You are a READ-ONLY researcher.\n")
mon, touch = dw.parse_markers(prompt_ordered)
check("GD-9: monitor marker parsed from the window",
      mon == {"plan": "research", "stage": "liveio", "role": "research", "attempt": "1"})
# Order-independent kv pairs + unknown keys tolerated (model=, phase=, ledger=).
mon2, _ = dw.parse_markers(
    "[monitor] attempt=3 role=impl model=opus plan=sp-x stage=implement phase=Implement\n")
check("GD-9: fields are order-independent",
      mon2["plan"] == "sp-x" and mon2["role"] == "impl" and mon2["attempt"] == "3")
check("GD-9: unknown/extra keys survive parsing",
      mon2.get("model") == "opus" and mon2.get("phase") == "Implement")
# A marker BELOW the window is quoted prose and must never be used.
prose = ("\n[monitor] plan=real stage=s role=impl attempt=1\n"
         "line two\nline three\nline four\n"
         "quoted finding: [monitor] plan=LEAKED stage=x role=critique attempt=9\n")
mon3, _ = dw.parse_markers(prose)
check("GD-9: marker outside the 4-line window is ignored", mon3["plan"] == "real")
# [touch] + [monitor] on adjacent lines parse into ONE record.
both = ("\n[touch] name=impl-a parent=root root=touch-x ledger=state/spawn-ledger.jsonl\n"
        "[monitor] plan=sp-a stage=implement role=impl attempt=2\n")
mon4, touch4 = dw.parse_markers(both)
check("GD-9: adjacent [touch]+[monitor] both parse",
      mon4["plan"] == "sp-a" and touch4["name"] == "impl-a" and touch4["root"] == "touch-x")
# Shape-independent: BOTH markers on ONE line must still parse (the old
# to-end-of-line match swallowed the monitor fields into the touch record).
same_line = ("[touch] name=impl-a root=touch-x "
             "[monitor] plan=sp-a stage=implement role=impl attempt=2\n")
mon5, touch5 = dw.parse_markers(same_line)
check("GD-9: two markers on ONE line both parse",
      bool(mon5) and mon5["plan"] == "sp-a" and mon5["role"] == "impl"
      and bool(touch5) and touch5["name"] == "impl-a")
# ...and a marker's fields still stop at its own line end: prose on the NEXT
# window line must not leak key=value pairs into the marker record.
leaky = ("[monitor] plan=sp-a stage=implement role=impl attempt=1\n"
         "You are the IMPLEMENTER for sub-plan sp-a (title=BOGUS mode=hostile)\n")
mon6, _ = dw.parse_markers(leaky)
check("GD-9: prose under the marker line does not leak fields into it",
      "title" not in mon6 and "mode" not in mon6 and mon6["plan"] == "sp-a")

# GD-9: `marker-misplaced` means a REAL [touch] marker below the window — a
# prompt that merely QUOTES the token (a findings file pasted into a critique
# prompt) is prose and must not be flagged.
head = "[monitor] plan=sp-a stage=implement role=impl attempt=1\nl2\nl3\nl4\n"
check("GD-9: a prose mention of the [touch] token is not a misplaced marker",
      dw.touch_marker_misplaced(head + "the critique quotes the [touch] convention\n")
      is False)
check("GD-9: a REAL [touch] marker below the window IS misplaced",
      dw.touch_marker_misplaced(head + "[touch] name=leaked root=other\n") is True)
check("GD-9: a [touch] marker INSIDE the window is not misplaced",
      dw.touch_marker_misplaced(both) is False)


# ---------------------------------------------------------------------------
# R-13: labels are stage-qualified so parallel siblings stay distinct
# ---------------------------------------------------------------------------
siblings = [{"plan": "research", "role": "research", "attempt": 1, "stage": s}
            for s in ("convo", "sessionjsonl", "mongoschema", "customstate",
                      "liveflow", "priorart")]
labels = {dw.agent_label(i) for i in siblings}
check("R-13: six parallel researchers get six distinct labels", len(labels) == 6)
check("R-13: label reads <stage>:<role> #<attempt>",
      dw.agent_label(siblings[0]) == "convo:research #1")
check("R-13: unclassified agent falls back to the short id",
      dw.agent_label(None, "a2fc883c96ff7b837") == "a2fc883c")

# Full 17-hex agentId is identity; the 8-char form is display-only (shortId).
blk = dw.agent_block("a2fc883c96ff7b837", siblings[0], "running", started="T0")
check("R-13: agent_block id is the FULL agentId", blk["id"] == "a2fc883c96ff7b837")
check("R-13: 8-char form travels as shortId", blk["shortId"] == "a2fc883c")
check("R-13: agent_block carries the label + state",
      blk["label"] == "convo:research #1" and blk["state"] == "running")
blk_unknown = dw.agent_block("b1c2d3e4f5a6b7c8d", None, "running")
check("GD-7: unclassified agent still gets a node, flagged unconventional",
      blk_unknown["id"] == "b1c2d3e4f5a6b7c8d" and blk_unknown["unconventional"] is True)
ident_info = dict(siblings[0], identity={"name": "impl-a", "root": "touch-x"})
check("GD-9: [touch] identity rides along as labels",
      dw.agent_block("a" * 17, ident_info, "running")["identity"]["name"] == "impl-a")
misplaced = dw.agent_block("a" * 17, dict(siblings[0], marker_misplaced=True), "running")
check("GD-9: misplaced [touch] marker flags the node, never drops it",
      misplaced["flags"] == ["marker-misplaced"])


# ---------------------------------------------------------------------------
# R-13: STAGE_HINT tolerates the templates' quoting
# ---------------------------------------------------------------------------
quoted = ('FIRST run: ORCH_STATE_DIR="/repo/.claude/local-orchestrators/t" '
          'bash "/repo/.claude/shared/monitoring/status.sh" "sp-watcher" implement '
          'running "attempt 1: implementing"')
hits = dw.STAGE_HINT.findall(quoted)
check("R-13: quoted statusCmd yields the stage", hits and hits[-1] == "implement")
check("R-13: unquoted legacy form still parses",
      dw.STAGE_HINT.findall("bash status.sh research liveio running x")[-1] == "liveio")


# ---------------------------------------------------------------------------
# R-08 / GD-10 / R-58: the close predicate — a verdict-less plan closes DONE
# ---------------------------------------------------------------------------
check("GD-10: decisive True -> done",
      dw.close_state_for("p", {"p": True}, {}) == "done")
check("GD-10: decisive False -> failed (a real verdict still fails)",
      dw.close_state_for("p", {"p": False}, {"p": True}) == "failed")
check("GD-10: no verdict + last result ok -> done (closed, no verdict)",
      dw.close_state_for("research", {}, {"research": True}) == "done")
check("GD-10: no verdict + last result failed -> failed",
      dw.close_state_for("research", {}, {"research": False}) == "failed")
check("GD-10: nothing known at all -> failed (never a silent green)",
      dw.close_state_for("ghost", {}, {}) == "failed")
check("GD-10: verdict-less close is labelled honestly",
      "closed, no verdict" in dw.close_detail("research", {}, "loop exited -> synthesis"))
check("GD-10: decided close carries no no-verdict label",
      dw.close_detail("p", {"p": True}, "base") == "base")

# run_outcome: a research-shaped run (findings only, no verdict) closes done.
research_state = {"plans": {"research": "running", "synthesis": "running"},
                  "running": [], "decisive": {},
                  "last_result_ok": {"research": True, "synthesis": True}}
check("R-08: verdict-less research run closes done (was: never closed)",
      dw.run_outcome(research_state) == "done")
check("R-08: an agent still running keeps the run open",
      dw.run_outcome(dict(research_state, running=["a1"])) is None)
mixed = {"plans": {"sp-a": "done", "sp-b": "running"}, "running": [],
         "decisive": {"sp-a": True, "sp-b": False}, "last_result_ok": {"sp-b": True}}
check("R-08: a rejected plan still fails the run", dw.run_outcome(mixed) == "failed")
# The reserved orchestrator card never votes on the run's own outcome.
check("R-08: orchestrator card excluded from the fold",
      dw.run_outcome({"plans": {"orchestrator": "running", "research": "done"},
                      "running": [], "decisive": {}, "last_result_ok": {}}) == "done")

# Source guards: every close site goes through the predicate, and the sequenced
# heuristic is gated on strategy=="serial" (GD-10).
main_src = inspect.getsource(dw.main)
check("R-08: sequenced close is gated on STRATEGY == serial",
      'STRATEGY == "serial"' in main_src)
check("R-08: sequenced close uses close_state_for, not a decisive-only ternary",
      'st = close_state_for(prev' in main_src
      and 'st = "done" if state["decisive"].get(prev) else "failed"' not in main_src)
check("R-08: settle pass uses close_state_for",
      "close_state_for(plan, state[\"decisive\"]" in main_src)
check("R-08: a terminal badge reopens on a later spawn (failed too)",
      'state["plans"][info["plan"]] in ("done", "failed")' in main_src)
check("R-08: last_result_ok is recorded from each result",
      'state["last_result_ok"][info["plan"]] = sst != "failed"' in main_src)


# ---------------------------------------------------------------------------
# R-08: final-gate decision text is keyed on (plan, role) — no phantom critique
# ---------------------------------------------------------------------------
fg_info = {"plan": "finalgate", "role": "test", "attempt": 1}
_, fg_state, fg_detail = dw.describe_result(fg_info, {"passed": True, "summary": "green"})
check("R-08: finalgate PASS closes done", fg_state == "done")
check("R-08: finalgate PASS text names run completion", "run complete" in fg_detail)
check("R-08: finalgate PASS text mentions no critique",
      "critique" not in fg_detail.lower())
_, fg_state2, fg_detail2 = dw.describe_result(fg_info, {"passed": False, "summary": "red"})
check("R-08: finalgate FAIL spawns a fixer and re-gates",
      fg_state2 == "failed" and "re-gate" in fg_detail2
      and "critique" not in fg_detail2.lower())
_, _, fg_last = dw.describe_result({"plan": "finalgate", "role": "test",
                                    "attempt": dw.MAX_FINALGATE_ATTEMPTS},
                                   {"passed": False})
check("R-08: last finalgate attempt says exhausted, not re-gate",
      "exhausted" in fg_last)
# A per-sub-plan test gate is unchanged: critique DOES follow it.
_, _, sp_detail = dw.describe_result({"plan": "sp-a", "role": "test", "attempt": 1},
                                     {"passed": True})
check("R-08: a sub-plan test gate still spawns critique", "critique" in sp_detail)

# The final-gate FIXER is an impl role too, and no test stage follows it: the
# loop re-runs the sweep itself, so "-> spawn test" would name a phantom stage.
_, _, fx_detail = dw.describe_result({"plan": "finalgate", "role": "impl", "attempt": 1},
                                     {"done": True, "files_changed": ["a"], "summary": "s"})
check("R-08: the finalgate fixer re-gates instead of spawning a phantom test",
      f"re-gate 2/{dw.MAX_FINALGATE_ATTEMPTS}" in fx_detail
      and "spawn test" not in fx_detail)
_, _, fx_last = dw.describe_result({"plan": "finalgate", "role": "impl",
                                    "attempt": dw.MAX_FINALGATE_ATTEMPTS},
                                   {"done": True, "files_changed": []})
check("R-08: the last finalgate fixer promises no further re-gate",
      "no re-gate left" in fx_last)
_, _, sp_impl = dw.describe_result({"plan": "sp-a", "role": "impl", "attempt": 1},
                                   {"done": True, "files_changed": ["x"]})
check("R-08: a per-sub-plan implementer still spawns test", "spawn test" in sp_impl)


# ---------------------------------------------------------------------------
# R-40: self-exit needs BOTH a terminal complete event and a quiet journal
# ---------------------------------------------------------------------------
check("R-40: quiet without a terminal complete -> stay alive",
      dw.should_exit(99999, False) is False)
check("R-40: terminal complete but journal still busy -> stay alive",
      dw.should_exit(0, True, window=120) is False)
check("R-40: terminal complete + quiet window elapsed -> exit",
      dw.should_exit(121, True, window=120) is True)

ev_dir = os.path.join(BASE, "r40")
os.makedirs(ev_dir, exist_ok=True)
ev_none = os.path.join(ev_dir, "no-complete.jsonl")
with open(ev_none, "w") as f:
    f.write(json.dumps({"plan": "research", "stage": "plan", "state": "done"}) + "\n")
check("R-40: no complete event in the stream -> not terminal",
      dw.stream_terminal_close(ev_none) is False)
ev_done = os.path.join(ev_dir, "complete.jsonl")
with open(ev_done, "w") as f:
    f.write(json.dumps({"plan": "research", "stage": "plan", "state": "done"}) + "\n")
    f.write("this line is not json at all\n")
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete",
                        "state": "done", "detail": "run done", "w": "agent"}) + "\n")
check("R-40: driver-written orchestrator complete counts as terminal",
      dw.stream_terminal_close(ev_done) is True)
check("R-40: a missing stream is not terminal",
      dw.stream_terminal_close(os.path.join(ev_dir, "nope.jsonl")) is False)

# M1: the EXIT is authorized ONLY by an externally written close (w="agent").
# The watcher's own inferred close (state["run_complete"]) closes the BADGE and
# self-heals on the next spawn; it must never stop the process, because a harness
# stall between agents is indistinguishable from a finished run and exiting
# self-heals nothing.
check("R-40/M1: a driver-written (w=agent) close authorizes the exit",
      dw.exit_authorized(ev_done) is True)
ev_watcher = os.path.join(ev_dir, "watcher-close.jsonl")
with open(ev_watcher, "w") as f:
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete", "state": "done",
                        "detail": "run done (watcher-detected end)",
                        "w": "watcher"}) + "\n")
check("R-40/M1: the watcher's OWN complete event does not authorize the exit",
      dw.exit_authorized(ev_watcher) is False
      and dw.stream_terminal_close(ev_watcher) is True)
ev_unattributed = os.path.join(ev_dir, "unattributed.jsonl")
with open(ev_unattributed, "w") as f:
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete",
                        "state": "done"}) + "\n")
check("R-40/M1: an unattributed close (no w) does not authorize the exit either",
      dw.exit_authorized(ev_unattributed) is False)
check("R-40/M1: should_exit is fed exit_authorized, so a badge alone cannot exit",
      dw.should_exit(99999, dw.exit_authorized(ev_watcher)) is False)

# LAST-EVENT-WINS in file order, scoped to the reserved `orchestrator` id: a
# close followed by a reopen (or by plan cards moving again) is not terminal.
ev_reopened = os.path.join(ev_dir, "reopened.jsonl")
with open(ev_reopened, "w") as f:
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete",
                        "state": "done", "detail": "run done"}) + "\n")
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete",
                        "state": "running", "detail": "run resumed"}) + "\n")
check("R-40: a close followed by `complete running` is NOT terminal",
      dw.stream_terminal_close(ev_reopened) is False)
ev_replan = os.path.join(ev_dir, "replan.jsonl")
with open(ev_replan, "w") as f:
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete",
                        "state": "done"}) + "\n")
    f.write(json.dumps({"plan": "sp-b", "stage": "plan", "state": "running",
                        "detail": "first agent spawned"}) + "\n")
check("R-40: a plan card moving after the close is NOT terminal",
      dw.stream_terminal_close(ev_replan) is False)
ev_reclosed = os.path.join(ev_dir, "reclosed.jsonl")
with open(ev_reclosed, "w") as f:
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete", "state": "done"}) + "\n")
    f.write(json.dumps({"plan": "sp-b", "stage": "plan", "state": "done"}) + "\n")
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete", "state": "failed"}) + "\n")
check("R-40: the LAST orchestrator complete decides (reopened, then failed)",
      dw.stream_terminal_close(ev_reclosed) is True)
ev_foreign = os.path.join(ev_dir, "foreign.jsonl")
with open(ev_foreign, "w") as f:
    f.write(json.dumps({"plan": "sp-a", "stage": "complete", "state": "done"}) + "\n")
check("R-40: a `complete` under a non-reserved plan id is not a run close",
      dw.stream_terminal_close(ev_foreign) is False)

# M-1: the SHIPPED ordering. QUIET_SECS (60) < EXIT_QUIET_SECS (120), so the
# watcher's own settle pass ALWAYS runs after the driver's close and always emits
# these two lines. Treating either of them as "the run is live again" cancelled
# the authorization in every normal run: the watcher then sat until the ABANDONED
# window (20 min by default) and exited claiming no driver close ever came.
ev_settled = os.path.join(ev_dir, "settled_after_close.jsonl")
with open(ev_settled, "w") as f:
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete", "state": "done",
                        "detail": "run done", "w": "agent"}) + "\n")
    f.write(json.dumps({"plan": "research", "stage": "plan", "state": "done",
                        "detail": "run done: settling open plan", "w": "watcher"}) + "\n")
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete", "state": "done",
                        "detail": "watcher-detected end", "w": "watcher"}) + "\n")
check("M-1: the watcher's own settle events do not cancel the driver's close",
      dw.exit_authorized(ev_settled) is True)
check("M-1: a settle `plan done` is a close, not a sign of life",
      dw.stream_terminal_close(ev_settled, writer="agent") is True)
ev_moving = os.path.join(ev_dir, "moving_after_close.jsonl")
with open(ev_moving, "w") as f:
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete", "state": "done",
                        "w": "agent"}) + "\n")
    f.write(json.dumps({"plan": "sp-b", "stage": "plan", "state": "queued",
                        "detail": "seeded", "w": "agent"}) + "\n")
check("M-1: a plan card MOVING (queued) after the close still cancels it",
      dw.exit_authorized(ev_moving) is False)
ev_watcher_only = os.path.join(ev_dir, "watcher_close_only.jsonl")
with open(ev_watcher_only, "w") as f:
    f.write(json.dumps({"plan": "research", "stage": "plan", "state": "done",
                        "w": "watcher"}) + "\n")
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete", "state": "done",
                        "w": "watcher"}) + "\n")
check("M-1: a foreign-writer close is NEUTRAL — it cannot authorize an exit",
      dw.exit_authorized(ev_watcher_only) is False)

# m-3: the fold the settle pass uses to adopt closes the STREAM already carries.
ev_closes = os.path.join(ev_dir, "plan_closes.jsonl")
with open(ev_closes, "w") as f:
    f.write(json.dumps({"plan": "research", "stage": "plan", "state": "running"}) + "\n")
    f.write(json.dumps({"plan": "research", "stage": "plan", "state": "failed",
                        "detail": "an earlier, corrected verdict"}) + "\n")
    f.write(json.dumps({"plan": "research", "stage": "plan", "state": "done",
                        "detail": "6/6 researchers returned", "w": "agent"}) + "\n")
    f.write(json.dumps({"plan": "sp-b", "stage": "implement", "state": "done"}) + "\n")
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete", "state": "done"}) + "\n")
closes = dw.stream_plan_closes(ev_closes)
check("m-3: last-event-wins in FILE order (the corrective done beats the failed)",
      closes.get("research") == "done")
check("m-3: a non-`plan` stage never closes a card", "sp-b" not in closes)
check("m-3: the reserved orchestrator id is not a plan card",
      "orchestrator" not in closes)
check("m-3: since_offset scopes the fold to this session",
      dw.stream_plan_closes(ev_closes, os.path.getsize(ev_closes)) == {})

# m-1: ONE resolver — the path refresh_caps watches and the values it applies
# must come from the SAME file, or repairing a corrupt config reloads nothing.
cfg_dir = os.path.join(BASE, "cfgresolve")
os.makedirs(cfg_dir, exist_ok=True)
_saved_state_dir = dw.STATE_DIR
try:
    dw.STATE_DIR = cfg_dir
    corrupt = os.path.join(cfg_dir, "orch-config.json")
    with open(corrupt, "w") as f:
        f.write("{ this is not json")
    path, values = dw.resolve_config()
    check("m-1: a corrupt config is still the watched path (repair takes effect)",
          path == corrupt)
    check("m-1: its values fall back to defaults, never to another file's",
          values == {})
    check("m-1: config_path() and read_config() agree on the file",
          dw.config_path() == corrupt and dw.read_config() == {})
    check("m-1: the unreadable config is reported, not swallowed",
          any("cannot read" in w and corrupt in w for w in dw._CFG_WARNINGS))
    with open(corrupt, "w") as f:
        json.dump({"max_plan_attempts": 11}, f)
    check("m-1: repairing the SAME file yields its values",
          dw.resolve_config() == (corrupt, {"max_plan_attempts": 11}))
finally:
    dw.STATE_DIR = _saved_state_dir

# ---------------------------------------------------------------------------
# FRONTEND-6 (stream side): the Orchestrator BADGE question, distinct from the
# exit question. One task folder hosts several phases, so the stream can end
# on an earlier phase's close; stream_badge_closed() is what arms the startup
# heal (next spawn -> `complete running`). Unlike stream_terminal_close, only
# orchestrator badge events (stage plan/complete) move the verdict.
# ---------------------------------------------------------------------------
badge_closed = os.path.join(ev_dir, "badge-closed.jsonl")
with open(badge_closed, "w") as f:
    f.write(json.dumps({"plan": "orchestrator", "stage": "plan", "state": "running"}) + "\n")
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete", "state": "done"}) + "\n")
check("FRONTEND-6: a stream ending on `complete done` reads badge-closed",
      dw.stream_badge_closed(badge_closed) is True)
with open(badge_closed, "a") as f:   # sub-plan cards moving do NOT reopen the badge
    f.write(json.dumps({"plan": "sp-b", "stage": "plan", "state": "running"}) + "\n")
    f.write(json.dumps({"plan": "orchestrator", "stage": "sp-b", "state": "running",
                        "detail": "spawn sp-b impl attempt 1"}) + "\n")
check("FRONTEND-6: sub-plan `plan` events and spawn chips do not reopen the badge",
      dw.stream_badge_closed(badge_closed) is True)
with open(badge_closed, "a") as f:   # ...but a badge-level reopen does
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete", "state": "running",
                        "detail": "run resumed: new agent spawned"}) + "\n")
check("FRONTEND-6: a `complete running` reopen clears the badge close",
      dw.stream_badge_closed(badge_closed) is False)
badge_replan = os.path.join(ev_dir, "badge-replan.jsonl")
with open(badge_replan, "w") as f:
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete", "state": "done"}) + "\n")
    f.write(json.dumps({"plan": "orchestrator", "stage": "plan", "state": "running",
                        "detail": "next phase launching"}) + "\n")
check("FRONTEND-6: a driver-seeded orchestrator `plan running` also reopens it",
      dw.stream_badge_closed(badge_replan) is False)
check("FRONTEND-6: a missing stream is not badge-closed",
      dw.stream_badge_closed(os.path.join(ev_dir, "nope.jsonl")) is False)
check("FRONTEND-6: startup arms the stale-close reopen through run_complete",
      'state["run_complete"] = "stale-stream-close"' in inspect.getsource(dw.main)
      and "stream_badge_closed()" in inspect.getsource(dw.main))

# SESSION SCOPE: a complete event that was ALREADY in the stream when the
# watcher started belongs to an earlier phase of the same task folder (research,
# then implement-plan on the same events.jsonl). It must never end this session.
# The scan is cached on (size, mtime): it sits in the ~1s liveness loop and
# events.jsonl grows without bound, so an unchanged stream must not be re-read.
cache_path = os.path.join(ev_dir, "cached.jsonl")
line_done = json.dumps({"plan": "orchestrator", "stage": "complete", "state": "done"}) + "\n"
line_info = json.dumps({"plan": "orchestrator", "stage": "complete", "state": "info"}) + "\n"
with open(cache_path, "w") as f:
    f.write(line_done)
_st = os.stat(cache_path)
check("R-40: the stream scan reads a terminal close", dw.stream_terminal_close(cache_path) is True)
with open(cache_path, "w") as f:     # same length, mtime restored: cache must hold
    f.write(line_info)
os.utime(cache_path, ns=(_st.st_atime_ns, _st.st_mtime_ns))
check("m1: an unchanged (size, mtime) stream is served from cache, not re-read",
      dw.stream_terminal_close(cache_path) is True and bool(dw._TERMINAL_CACHE))
with open(cache_path, "w") as f:     # now the mtime really moves
    f.write(line_info)
check("m1: a changed stream invalidates the cache",
      dw.stream_terminal_close(cache_path) is False)

_baseline = os.path.getsize(ev_done)   # what the watcher records at startup
check("R-40: a complete event already present at startup is not this session's",
      dw.exit_authorized(ev_done, _baseline) is False)
with open(ev_done, "a") as f:
    f.write(json.dumps({"plan": "orchestrator", "stage": "complete",
                        "state": "failed", "detail": "this run failed",
                        "w": "agent"}) + "\n")
check("R-40: a complete appended AFTER the baseline does end the session",
      dw.exit_authorized(ev_done, _baseline) is True)

# A STALE terminal event must not kill a live watcher: one task folder hosts
# several phases, so an earlier `orchestrator complete done` is still in the
# stream while the next run is in flight.
check("R-40: an in-flight agent blocks the exit (stale complete event)",
      dw.journal_quiescent({"running": ["a1"], "plans": {"sp-a": "running"},
                            "decisive": {}, "last_result_ok": {}}) is False)
check("R-40: an open plan with no agent in flight is resolvable (settles, then exits)",
      dw.journal_quiescent({"running": [], "plans": {"sp-a": "running"},
                            "decisive": {}, "last_result_ok": {}}) is True)
check("R-40: a finished run is quiescent",
      dw.journal_quiescent({"running": [], "plans": {"research": "done"},
                            "decisive": {}, "last_result_ok": {"research": True}}) is True)
# GD-10: "this journal produced no run at all" is UNKNOWN, never a verdict — a
# watcher started before the driver's first spawn (the documented order) has no
# plans yet and must not exit out from under the run it was started for.
check("R-40: an empty plan set is unknown, NOT exitable",
      dw.journal_quiescent({"running": [], "plans": {}}) is False)
_exit_src = inspect.getsource(dw.main)
# m1: the O(stream) terminal scan must sit BEHIND the O(1) pre-check, so an idle
# run does not re-read a growing events.jsonl once a second.
check("R-40: the exit site pre-checks BEFORE scanning the stream",
      "exit_precheck(state, quiet_for)" in _exit_src
      and _exit_src.index("exit_precheck(state, quiet_for)")
      < _exit_src.index("exit_authorized(EVENTS"))
_SETTLED = {"running": [], "plans": {"research": "done"}, "decisive": {},
            "last_result_ok": {"research": True}}
check("R-40: the pre-check passes a settled, long-quiet run",
      dw.exit_precheck(_SETTLED, 10 ** 6) is True)
check("R-40: the pre-check blocks while an agent is in flight",
      dw.exit_precheck(dict(_SETTLED, running=["a1"]), 10 ** 6) is False)
# The two windows are configured independently: gating on EXIT_QUIET_SECS alone
# would make a lowered ORCH_ABANDON_QUIET_SECS unreachable inside its own window.
check("R-40: the pre-check gates on the SHORTER of the two exit windows",
      dw.exit_precheck(_SETTLED, min(dw.EXIT_QUIET_SECS, dw.ABANDON_QUIET_SECS)) is True
      and dw.exit_precheck(_SETTLED,
                           min(dw.EXIT_QUIET_SECS, dw.ABANDON_QUIET_SECS) - 1) is False)
check("R-40: the terminal check is scoped to this session's baseline",
      "exit_authorized(EVENTS, events_baseline)" in _exit_src)
check("R-40/M1: main() never exits on the watcher's own badge inference",
      "terminal_complete_seen" not in _exit_src)
check("R-40: the self-exit honors the ORCH_NO_SELF_EXIT opt-out",
      "not NO_SELF_EXIT" in inspect.getsource(dw.exit_precheck))
check("SD-10: the tail loop rebuilds on shrink OR inode replacement",
      'if size < state["offset"] or rotated:' in _exit_src)
# m1/SD-4: the retained LEGACY sequenced close must not reuse the historic
# `loop exited ->` detail — that phrase is the signature SD-4's read-time
# re-labeler keys on, so a new run emitting it would have its genuine close
# re-read as "closed — no verdict".
check("m1/SD-4: the sequenced close says 'serial advance ->', not 'loop exited ->'",
      "f\"serial advance -> {info['plan']}\"" in _exit_src
      and "f\"loop exited -> {info['plan']}\"" not in _exit_src)

# --- m2 / R-40: an ABANDONED run (killed session) still stops its watcher ------
check("m2: abandoned_exit needs a settled run (no run_complete -> False)",
      dw.abandoned_exit({}, 10 ** 6) is False)
check("m2: abandoned_exit needs the LONG window, not the exit window",
      dw.abandoned_exit({"run_complete": "done"}, dw.EXIT_QUIET_SECS + 1) is False)
check("m2: abandoned_exit fires after the long window",
      dw.abandoned_exit({"run_complete": "done"}, dw.ABANDON_QUIET_SECS) is True)
check("m2: the abandon window is an order of magnitude above the exit window",
      dw.ABANDON_QUIET_SECS >= 10 * dw.EXIT_QUIET_SECS)
# An agent that never gets a journal `result` (its session was killed) keeps
# `running` non-empty forever, which blocks the settle pass and the exit. It is
# closed `stale` only when the journal AND its own transcript have both been
# silent for the long window — a 30-minute implementer that is still writing must
# never be misjudged.
_idle = {"live": 1.0, "gone": dw.ABANDON_QUIET_SECS + 1, "notranscript": None}
check("m2: below the window nothing is abandoned",
      dw.abandoned_agents(["live", "gone"], 5.0, _idle.get) == [])
check("m2: a still-writing transcript keeps its agent alive",
      dw.abandoned_agents(["live"], dw.ABANDON_QUIET_SECS, _idle.get) == [])
check("m2: an idle transcript past the window is abandoned",
      dw.abandoned_agents(["live", "gone"], dw.ABANDON_QUIET_SECS, _idle.get) == ["gone"])
check("m2: an agent with no transcript at all is abandoned (unknown, not running)",
      dw.abandoned_agents(["notranscript"], dw.ABANDON_QUIET_SECS, _idle.get)
      == ["notranscript"])
check("m2: transcript_idle_for reports None for an agent with no transcript",
      dw.transcript_idle_for("no-such-agent-at-all") is None)
_idle_real = dw.transcript_idle_for("small")   # written by the WATCHER-7 arm above
check("m2: transcript_idle_for measures a real transcript's age",
      _idle_real is not None and _idle_real < 600)
check("m2: main() closes abandoned agents stale before settling",
      "abandoned_agents(state[\"running\"], quiet_for)" in _exit_src
      and '"stale"' in _exit_src)


# ---------------------------------------------------------------------------
# M2 / D4 / R-09: caps + strategy are re-read WHILE running
#
# The documented launch order starts the daemons BEFORE the orchestrator script,
# and the script publishes its caps/strategy from inside the run — so an
# import-only resolution means the watcher narrates its own defaults forever.
# ---------------------------------------------------------------------------
_cfg_path = os.path.join(STATE_DIR, "orch-config.json")
check("M2: config_path resolves the file the watcher reads",
      dw.config_path() == _cfg_path)
_caps_before = (dw.MAX_PLAN_ATTEMPTS, dw.MAX_FINALGATE_ATTEMPTS, dw.STRATEGY)
check("M2: an unchanged config is not re-read", dw.refresh_caps() is None)
with open(_cfg_path, "w") as f:
    json.dump({"max_gate_attempts": 5, "max_plan_attempts": 9,
               "max_finalgate_attempts": 7, "strategy": "parallel"}, f)
os.utime(_cfg_path, ns=(time.time_ns(), time.time_ns() + 10 ** 9))
_moved = dw.refresh_caps()
check("M2: a changed config is picked up mid-run", _moved is not None)
check("M2: the new caps are live", dw.MAX_PLAN_ATTEMPTS == 9
      and dw.MAX_FINALGATE_ATTEMPTS == 7)
check("M2: the new strategy is live", dw.STRATEGY == "parallel")
_, _, _fg_text = dw.describe_result({"plan": "finalgate", "role": "test", "attempt": 1},
                                    {"passed": False})
check("M2: the decision narration quotes the RELOADED cap, not the built-in 2",
      "re-gate 2/7" in _fg_text)
check("M2: re-reading again without a change returns None",
      dw.refresh_caps() is None)
with open(_cfg_path, "w") as f:      # restore the suite's baseline config
    json.dump({"max_gate_attempts": 5}, f)
os.utime(_cfg_path, ns=(time.time_ns(), time.time_ns() + 2 * 10 ** 9))
dw.refresh_caps()
check("M2: caps restored to the suite baseline",
      (dw.MAX_PLAN_ATTEMPTS, dw.MAX_FINALGATE_ATTEMPTS, dw.STRATEGY) == _caps_before)
check("M2: a garbage value in a RELOAD keeps the default and does not raise",
      dw.apply_caps({"max_plan_attempts": "nine"})[0] == 4)
dw.apply_caps(dw.read_config())
check("M2: the poll loop refreshes the config", "refresh_caps()" in _exit_src)


# ---------------------------------------------------------------------------
# R-58: replay the REAL research journals -> zero fabricated `failed` badges
#
# The two frozen journals are this session's own runs: touch-full-recon
# (wf_930e210a, 6 researchers + 1 synthesizer) and touch-mongo-live
# (wf_cca84d59, 5 + 1). Both fan-outs returned findings and NO gate verdict,
# which is exactly the shape that made the old rule invent
# `plan failed "loop exited -> synthesis"` while every researcher had succeeded.
#
# This is a RULES replay: it drives the module's own predicates over the real
# journal entries with a marker stub in place of the agent transcripts (which
# this fixture deliberately does not carry). The full file-level e2e replay with
# transcripts is the acceptance sub-plan's arm (R-56/R-16), not this one's.
# ---------------------------------------------------------------------------
R58_REPLAY = os.path.join(REPO_FIXTURES, "mirror", "r58-replay",
                          "292fc08c-923d-4ab4-8ff2-a9572417dbc8",
                          "subagents", "workflows")
R58_JOURNALS = {
    "touch-full-recon": os.path.join(R58_REPLAY, "wf_930e210a-6da", "journal.jsonl"),
    "touch-mongo-live": os.path.join(R58_REPLAY, "wf_cca84d59-933", "journal.jsonl"),
}


def stub_classify(entries):
    """agentId -> marker info, reconstructed from each agent's result shape.

    Stands in for reading the real prompts: a `findings` result is a researcher
    on plan `research`, a `plan_file` result is the synthesizer on plan
    `synthesis` — the markers those prompts actually carried.
    """
    info = {}
    order = {}
    for e in entries:
        aid = e.get("agentId", "")
        if e.get("type") != "result":
            order.setdefault(aid, len(order))
            continue
        r = e.get("result") or {}
        if "plan_file" in r:
            info[aid] = {"plan": "synthesis", "role": "synth", "attempt": 1,
                         "stage": "synthesize"}
        else:
            info[aid] = {"plan": "research", "role": "research", "attempt": 1,
                         "stage": f"p{order.get(aid, 0)}"}
    return info


def retired_rule(plan, decisive, last_result_ok):
    """The RULE THIS SUB-PLAN RETIRED: a plan with no decisive verdict -> failed.

    Used as the control below, replayed over the SAME real journals, so the
    "zero failed badges" assertions cannot be vacuous.
    """
    return "done" if decisive.get(plan) else "failed"


def replay_journal(path, strategy=None, rule=None, sequenced=None, marker=None):
    """Replay a journal through the watcher's plan-close rules.

    ``rule`` swaps in a different close predicate (``retired_rule`` for the
    control); ``sequenced`` forces the legacy "a new plan starting closes the
    previous one" heuristic, which the retired code applied UNGATED and the
    current code gates on ``strategy == "serial"`` (GD-10). ``marker`` overrides
    the result-shape stub with an explicit agentId -> marker map.

    The advance detail differs by rule on purpose: the retired code wrote
    ``loop exited -> <plan>`` (the historic signature SD-4's re-labeler keys on),
    the retained legacy heuristic writes ``serial advance -> <plan>`` so a new
    run's genuine close cannot collide with it (m1/SD-4).

    Returns (badge_events, outcome) where badge_events are the (plan, state,
    detail) triples the watcher would emit with stage="plan".
    """
    close = rule or dw.close_state_for
    advance = "loop exited ->" if rule else "serial advance ->"
    if sequenced is None:
        sequenced = strategy == "serial"
    entries = [json.loads(ln) for ln in open(path) if ln.strip()]
    marker = marker or stub_classify(entries)
    plans, decisive, last_result_ok, running = {}, {}, {}, []
    last_plan = None
    badges = []
    for e in entries:
        aid = e.get("agentId", "")
        info = marker.get(aid)
        if not info:
            continue
        plan = info["plan"]
        if e.get("type") == "started":
            running.append(aid)
            if sequenced and last_plan and last_plan != plan \
                    and plans.get(last_plan) == "running":
                st = close(last_plan, decisive, last_result_ok)
                plans[last_plan] = st
                badges.append((last_plan, st,
                               dw.close_detail(last_plan, decisive,
                                               f"{advance} {plan}")))
            last_plan = plan
            if plan not in plans:
                plans[plan] = "running"
                badges.append((plan, "running", "first agent spawned"))
            elif plans[plan] in ("done", "failed"):
                plans[plan] = "running"
                badges.append((plan, "running",
                               f"loop continues: {info['role']} attempt "
                               f"{info['attempt']} spawned"))
        elif e.get("type") == "result":
            if aid in running:
                running.remove(aid)
            result = e.get("result")
            sst, _ = dw.result_stage_state(result)
            last_result_ok[plan] = sst != "failed"
            if isinstance(result, dict) and ("passed" in result or "approved" in result):
                ok = bool(result.get("passed") or result.get("approved"))
                decisive[plan] = ok
                if ok:
                    plans[plan] = "done"
                    badges.append((plan, "done",
                                   f"{info['role']} attempt {info['attempt']} green"))
                elif plans.get(plan) == "done":
                    plans[plan] = "running"
                    badges.append((plan, "running",
                                   f"{info['role']} attempt {info['attempt']} "
                                   "rejected -> reopened"))
    state = {"plans": plans, "running": running, "decisive": decisive,
             "last_result_ok": last_result_ok}
    if rule is None:
        outcome = dw.run_outcome(state)   # the real function, not a re-derivation
    else:
        effective = [v if v in ("done", "failed") else close(p, decisive, last_result_ok)
                     for p, v in plans.items() if p != "orchestrator"]
        outcome = None if (running or not effective) else (
            "done" if all(v == "done" for v in effective) else "failed")
    for plan, badge in list(plans.items()):
        if badge in ("done", "failed"):
            continue
        st = close(plan, decisive, last_result_ok)
        plans[plan] = st
        badges.append((plan, st, dw.close_detail(plan, decisive,
                                                 f"run {outcome}: settling open plan")))
    return badges, outcome


for task, journal in R58_JOURNALS.items():
    if not os.path.isfile(journal):
        skip(f"R-58 replay for {task}: fixture missing ({journal})")
        continue
    for strategy in (None, "serial"):
        badges, outcome = replay_journal(journal, strategy=strategy)
        tag = f"{task} (strategy={strategy or 'default'})"
        failed_badges = [b for b in badges if b[1] == "failed"]
        check(f"R-58: {tag} replays with ZERO failed plan badges",
              failed_badges == [])
        research = [b for b in badges if b[0] == "research" and b[1] in ("done", "failed")]
        check(f"R-58: {tag} research plan closes done",
              bool(research) and research[-1][1] == "done")
        check(f"R-58: {tag} research close is labelled 'closed, no verdict'",
              bool(research) and "closed, no verdict" in research[-1][2])
        synth = [b for b in badges if b[0] == "synthesis" and b[1] in ("done", "failed")]
        check(f"R-58: {tag} synthesis plan closes done",
              bool(synth) and synth[-1][1] == "done")
        check(f"R-58: {tag} run closes done", outcome == "done")
    # Anti-tautology CONTROL: replay the SAME journal through the retired rule
    # (decisive-only close, ungated sequenced heuristic) and show it really does
    # fabricate the `failed` badges the assertions above deny.
    old_badges, old_outcome = replay_journal(journal, rule=retired_rule, sequenced=True)
    old_failed = [b for b in old_badges if b[1] == "failed"]
    check(f"R-58: {task} — the retired rule fabricates failed badges (control)",
          {b[0] for b in old_failed} >= {"research", "synthesis"})
    check(f"R-58: {task} — the fabricated badge carries the 'loop exited ->' text",
          any("loop exited ->" in b[2] for b in old_failed))
    check(f"R-58: {task} — the retired rule fails the whole run (control)",
          old_outcome == "failed")
    new_badges, new_outcome = replay_journal(journal)
    check(f"R-58: {task} — the fix flips both: zero failed badges, run done",
          [b for b in new_badges if b[1] == "failed"] == [] and new_outcome == "done")


# ---------------------------------------------------------------------------
# R-08: two INTERLEAVED parallel sub-plans produce no spurious badge and no flap
# (the configuration the retired ungated heuristic corrupted).
# ---------------------------------------------------------------------------
INTER_PATH = os.path.join(BASE, "interleaved.jsonl")
with open(INTER_PATH, "w") as f:
    for entry in (
        {"type": "started", "agentId": "a1"},
        {"type": "started", "agentId": "b1"},
        {"type": "result", "agentId": "a1",
         "result": {"done": True, "files_changed": ["x"], "summary": "a impl"}},
        {"type": "started", "agentId": "a2"},
        {"type": "result", "agentId": "b1",
         "result": {"done": True, "files_changed": ["y"], "summary": "b impl"}},
        {"type": "started", "agentId": "b2"},
        {"type": "result", "agentId": "a2",
         "result": {"passed": True, "summary": "green", "findings_file": "fa"}},
        {"type": "result", "agentId": "b2",
         "result": {"passed": True, "summary": "green", "findings_file": "fb"}},
    ):
        f.write(json.dumps(entry) + "\n")
INTER_MARKER = {
    "a1": {"plan": "sp-a", "role": "impl", "attempt": 1, "stage": "implement"},
    "a2": {"plan": "sp-a", "role": "test", "attempt": 1, "stage": "test"},
    "b1": {"plan": "sp-b", "role": "impl", "attempt": 1, "stage": "implement"},
    "b2": {"plan": "sp-b", "role": "test", "attempt": 1, "stage": "test"},
}
inter_badges, inter_outcome = replay_journal(INTER_PATH, marker=INTER_MARKER)
check("R-08: interleaved parallel sub-plans emit zero failed badges",
      [b for b in inter_badges if b[1] == "failed"] == [])
check("R-08: both interleaved sub-plans close done", inter_outcome == "done")
seen_terminal, flap = set(), []
for _plan, _st, _detail in inter_badges:
    if _st in ("done", "failed"):
        seen_terminal.add(_plan)
    elif _plan in seen_terminal:
        flap.append((_plan, _st))
check("R-08: no running->done->running badge flap on either plan", flap == [])
ctrl_badges, _ = replay_journal(INTER_PATH, marker=INTER_MARKER,
                                rule=retired_rule, sequenced=True)
check("R-08: the retired ungated heuristic DOES corrupt this interleaving (control)",
      any(b[1] == "failed" and "loop exited ->" in b[2] for b in ctrl_badges))


# ---------------------------------------------------------------------------
# R-07 / R-39 / R-40: live-process arms (subprocess; the loop is a while True)
# ---------------------------------------------------------------------------
WATCHER = os.path.join(MOD_DIR, "decision_watcher.py")


def run_watcher(state_dir, wf_dir, env_extra=None, wait=6.0, during=None,
                after=2.0, until=None, when=None, glob_root=None):
    """Start the real watcher on a throwaway journal.

    Returns ``(exited, returncode, stderr)`` — the watcher's loop is a
    ``while True``, so "did it exit on its own inside the window" IS the
    assertion for R-40. ``during`` appends to the journal / event stream WHILE the
    watcher is live (session-scoped terminal detection can only be tested that
    way).

    Three knobs keep every arm's cost proportional to the work it actually needs
    instead of to a hardcoded window (the whole suite runs on every gate):

    * ``when`` — a predicate polled every 50 ms; ``during`` fires as soon as it
      returns True. This is how an arm STATES the precondition its stimulus needs
      (usually ``watcher_online``: the baseline is captured) rather than
      approximating it with a sleep long enough to "probably" cover it. Strictly
      stronger than a timer: a watcher that never reaches the precondition no
      longer passes on a lucky delay.
    * ``after`` — seconds; the fallback deadline for ``when`` (fire anyway), and
      the plain trigger delay when no ``when`` is given.
    * ``until`` — polled every 50 ms once ``during`` has fired; the child is
      stopped as soon as it returns True, so an arm proving a POSITIVE costs the
      time it needs and not the whole window. Arms proving a NEGATIVE ("it stays
      alive") cannot poll for their assertion — the window IS the assertion — so
      they size it from ``negative_window()`` below instead of a literal.

    Sets ``run_watcher.stimulus_latency`` = seconds from ``during()`` firing to
    the child's own exit (None if it never fired or never exited); that is the
    measurement ``negative_window()`` calibrates against. ``run_watcher.proc`` is
    the live child, so a ``during`` stimulus can also SIGNAL it — which is how the
    M-2 shutdown-drain arms reproduce `closeRun`'s epilogue.

    ``ORCH_DRAIN_SECS`` defaults to 0 here (one final tail+emit pass, no extra
    slack): every negative arm ends by terminating the child, and the shipped
    3 s window would be paid on each of them for nothing. The arm that actually
    tests the drain sets its own value.
    """
    env = dict(os.environ)
    env["ORCH_STATE_DIR"] = state_dir
    env["ORCH_WF_DIR"] = wf_dir
    env["ORCH_WF_GLOB_ROOT"] = glob_root or os.path.join(BASE, "glob")
    env["ORCH_DRAIN_SECS"] = "0"
    env.update(env_extra or {})
    proc = subprocess.Popen([sys.executable, WATCHER], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    run_watcher.proc = proc
    run_watcher.stimulus_latency = None
    deadline = time.time() + wait
    fired = during is None
    fired_at = None
    trigger = time.time() + after
    while time.time() < deadline:
        if proc.poll() is not None:              # exited on its own: done polling
            _, err = proc.communicate()
            if fired_at is not None:
                run_watcher.stimulus_latency = time.time() - fired_at
            return True, proc.returncode, err
        # `when` first: an arm that STATES its precondition must not have the
        # stimulus delivered early by the `after` fallback deadline.
        if not fired and ((when is not None and when()) or time.time() >= trigger):
            fired, fired_at = True, time.time()
            during()
        if fired and until is not None and until():
            break
        time.sleep(0.05)
    proc.terminate()
    try:
        _, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()
        _, err = proc.communicate()
    return False, proc.returncode, err


def watcher_online(state_dir):
    """Predicate: the watcher's startup heartbeat has landed.

    The heartbeat is emitted immediately AFTER `events_baseline` is recorded
    (decision_watcher.main), so it is the observable proof that the process is
    past import, has captured its session baseline and is polling. Every arm
    whose stimulus has to be appended *after* the baseline states this as
    ``when=`` rather than approximating startup with a delay: strictly stronger
    (a watcher that never comes online no longer passes on a lucky sleep) and
    it costs startup, not a whole window.
    """
    return lambda: any(e["stage"] == "watcher" and "online" in (e.get("detail") or "")
                       for e in events_of(state_dir))


def negative_window(latency, floor=3.0, factor=4.0):
    """How long must a watcher be watched to conclude it is NOT going to exit?

    Negative arms have nothing to poll for, so their window IS the assertion and
    a literal would be either flaky or (as it was) needlessly slow. Instead they
    are calibrated from a MEASURED positive: ``latency`` is how long the
    identically-configured sibling arm took to exit once its stimulus landed, so
    ``factor`` times that is a window in which the same watcher would comfortably
    have exited. ``floor`` keeps several of the watcher's 1 s poll ticks inside
    the window even when the measurement is very small.
    """
    return max(floor, factor * latency) if latency else floor


def make_run(name, journal_lines="", state_files=None):
    base = os.path.join(BASE, "proc", name)
    state_dir, wf_dir = os.path.join(base, "state"), os.path.join(base, "wf")
    os.makedirs(wf_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(wf_dir, "journal.jsonl"), "w") as f:
        f.write(journal_lines)
    for rel, text in (state_files or {}).items():
        path = os.path.join(state_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    return state_dir, wf_dir


def events_of(state_dir):
    path = os.path.join(state_dir, "events.jsonl")
    if not os.path.isfile(path):
        return []
    out = []
    for raw in open(path):
        raw = raw.strip()
        if raw:
            out.append(json.loads(raw))
    return out


# R-07: a not-yet-created NESTED state dir must not swallow the first event.
nested_state = os.path.join(BASE, "proc", "nested", "a", "b", "c", "state")
nested_wf = os.path.join(BASE, "proc", "nested", "wf")
os.makedirs(nested_wf, exist_ok=True)
open(os.path.join(nested_wf, "journal.jsonl"), "w").close()
check("R-07: nested state dir does not exist before the watcher starts",
      not os.path.isdir(nested_state))
exited, rc, err = run_watcher(nested_state, nested_wf, wait=8.0,
                              until=lambda: bool(events_of(nested_state)))
evs = events_of(nested_state)
check("R-07: watcher created the nested state dir", os.path.isdir(nested_state))
check("R-07: the first heartbeat event was written",
      bool(evs) and evs[0]["stage"] == "watcher" and "online" in evs[0]["detail"])
check("R-39: the heartbeat line carries w=watcher",
      bool(evs) and evs[0].get("w") == "watcher")

# R-07: a garbage cap in orch-config.json keeps the watcher alive, with a warning
# on stderr AFTER the heartbeat (deferred resolution).
bad_state, bad_wf = make_run(
    "badcfg", state_files={"orch-config.json": '{"max_gate_attempts": "three"}'})
exited, rc, err = run_watcher(bad_state, bad_wf, wait=8.0,
                              until=lambda: bool(events_of(bad_state)))
bad_evs = events_of(bad_state)
check("R-07: watcher survives a non-integer cap (no crash at import)", bool(bad_evs))
check("R-07: it still tails (the heartbeat landed, not a traceback)",
      bool(bad_evs) and "online" in bad_evs[0]["detail"])
check("R-07: the bad cap is reported on stderr, after the heartbeat",
      "max_gate_attempts" in err and "default 3" in err)
check("R-07: no traceback on stderr", "Traceback" not in err)

# R-07 / D10: a journal shorter than the checkpoint offset triggers a rebuild.
trunc_state, trunc_wf = make_run("trunc", journal_lines='{"type":"noop"}\n')
journal_path = os.path.join(trunc_wf, "journal.jsonl")
with open(os.path.join(trunc_state, "orch-config.json"), "w") as f:
    json.dump({}, f)
with open(os.path.join(trunc_state, ".watcher-state.json"), "w") as f:
    json.dump({"offset": 99999, "journal": journal_path, "agents": {},
               "plans": {"sp-a": "running"}, "decisive": {}}, f)
def _rebuilt_checkpoint():
    """The rebuild is done when the checkpoint has been re-derived from the
    shrunken journal (offset rewound to 0, then re-consumed to its real size)."""
    try:
        with open(os.path.join(trunc_state, ".watcher-state.json")) as f:
            return json.load(f)["offset"] == os.path.getsize(journal_path)
    except (OSError, json.JSONDecodeError, KeyError):
        return False


exited, rc, err = run_watcher(trunc_state, trunc_wf, wait=10.0,
                              until=_rebuilt_checkpoint)
trunc_evs = events_of(trunc_state)
check("R-07: shrunken journal emits the rebuild event",
      any(e["stage"] == "watcher" and "truncated" in e["detail"] for e in trunc_evs))
with open(os.path.join(trunc_state, ".watcher-state.json")) as f:
    rebuilt = json.load(f)
# Rewound to 0, then the (short) journal is re-consumed from the top: the
# checkpoint must end at the journal's real size, never at the stale 99999.
check("R-07: offset rewound and re-derived from the shrunken journal",
      rebuilt["offset"] == os.path.getsize(journal_path))
check("R-07: derived plan state cleared on truncation", rebuilt["plans"] == {})

# A one-agent run: enough journal + transcript for the watcher to derive a plan
# (an EMPTY plan set is "unknown" and never exitable).
ONE_RUN = (json.dumps({"type": "started", "agentId": "a1"}) + "\n"
           + json.dumps({"type": "result", "agentId": "a1",
                         "result": {"findings": [], "findings_file": "f",
                                    "summary": "s"}}) + "\n")


def write_transcript(wf_dir, agent_id, marker):
    """Minimal agent transcript: agent_paths() finds it next to the journal."""
    with open(os.path.join(wf_dir, f"agent-{agent_id}.jsonl"), "w") as f:
        f.write(json.dumps({"type": "user", "timestamp": "2026-07-25T00:00:00.000Z",
                            "message": {"content": marker + "\nprompt body\n"}}) + "\n")


def append_complete(state_dir, detail="run done"):
    def _append():
        with open(os.path.join(state_dir, "events.jsonl"), "a") as f:
            f.write(json.dumps({"ts": "2026-07-25T00:00:01.000Z",
                                "plan": "orchestrator", "stage": "complete",
                                "state": "done", "detail": detail, "w": "agent"}) + "\n")
    return _append


SELF_EXIT_ROUTES = ("run closed by the driver", "run abandoned")


def self_exited(events):
    """Did the watcher stop itself through one of R-40's two routes?

    NOT "does any event say exiting": since M-2 a SIGTERM'd watcher drains and
    announces its own clean stop, and every negative arm ends by terminating the
    child — so the substring alone would now be true everywhere. What these arms
    deny is the SELF-exit, so they name its two details.
    """
    return any(e["stage"] == "watcher"
               and any(r in (e.get("detail") or "") for r in SELF_EXIT_ROUTES)
               for e in events)


# R-40: a driver `orchestrator complete` appended DURING this session ends it.
# ORCH_ABANDON_QUIET_SECS is pushed out of the way so only the AUTHORIZED route
# can fire here; QUIET_SECS is left at 1 s deliberately — the settle pass is no
# longer something an arm has to suppress to see the authorized exit (M-1), and
# with the exit window at 0 s this arm ends before the settle can even run.
# (The full shipped ordering — settle AFTER the driver's close — is the arm
# below, which is where M-1 would be caught.)
LONG = {"ORCH_QUIET_SECS": "999", "ORCH_ABANDON_QUIET_SECS": "999"}
exit_state, exit_wf = make_run("exit", journal_lines=ONE_RUN)
write_transcript(exit_wf, "a1",
                 "[monitor] plan=research stage=probe role=research attempt=1")
exited, rc, err = run_watcher(exit_state, exit_wf,
                              {"ORCH_QUIET_SECS": "1", "ORCH_ABANDON_QUIET_SECS": "999",
                               "ORCH_EXIT_QUIET_SECS": "0"},
                              wait=15.0, during=append_complete(exit_state),
                              when=watcher_online(exit_state), after=5.0)
# This arm is the calibration reference for the two negative arms below: it is
# the same watcher, same env, given an AUTHORIZED close — so how long it took to
# act on it bounds how long a watcher that must NOT act needs to be watched.
AUTH_EXIT_LATENCY = run_watcher.stimulus_latency
check("R-40: a driver complete appended during the session exits the watcher", exited)
check("R-40: exit is clean (rc 0)", exited and rc == 0)
check("R-40: the exit is announced in the stream, by its route",
      any(e["stage"] == "watcher" and "exiting" in e["detail"]
          and "run closed by the driver" in e["detail"]
          for e in events_of(exit_state)))

# M-1 (the shipped ordering, end to end): QUIET_SECS < EXIT_QUIET_SECS, so the
# watcher's settle pass fires BETWEEN the driver's close and the exit window and
# appends a `plan done` plus its OWN `orchestrator complete done`. Before the fix
# those two lines reset the terminal flag, `exit_authorized` went permanently
# False and the run only stopped on the 10x ABANDONED window — with a detail
# claiming no driver close had ever come. This arm is the one that catches it.
ord_state, ord_wf = make_run("settleorder", journal_lines=ONE_RUN)
write_transcript(ord_wf, "a1",
                 "[monitor] plan=research stage=probe role=research attempt=1")
exited, rc, err = run_watcher(
    ord_state, ord_wf,
    {"ORCH_QUIET_SECS": "1", "ORCH_EXIT_QUIET_SECS": "3",
     "ORCH_ABANDON_QUIET_SECS": "999"},
    wait=30.0, during=append_complete(ord_state, "wf complete: 1 plan"),
    when=watcher_online(ord_state), after=2.0)
ord_evs = events_of(ord_state)
driver_ix = next((i for i, e in enumerate(ord_evs)
                  if e["stage"] == "complete" and e.get("w") == "agent"), None)
# Anti-vacuity: the ordering this arm exists to exercise must really be present.
check("M-1: the watcher's own settle events landed AFTER the driver's close",
      driver_ix is not None
      and any(e["stage"] == "plan" and e["state"] == "done" and e.get("w") == "watcher"
              for e in ord_evs[driver_ix + 1:])
      and any(e["stage"] == "complete" and e.get("w") == "watcher"
              for e in ord_evs[driver_ix + 1:]))
check("M-1: the authorized exit still fires with the settle pass in between",
      exited and rc == 0)
check("M-1: and it exits by the DRIVER route, not the abandoned one",
      any(e["stage"] == "watcher" and "run closed by the driver" in e["detail"]
          for e in ord_evs)
      and not any("run abandoned" in (e.get("detail") or "") for e in ord_evs))

# m-3: the settle pass must ADOPT a close the script already wrote, not append a
# second one labelled "(closed, no verdict)" — the script-verified close would
# otherwise be contradicted, forever, in the durable record.
dup_state, dup_wf = make_run("settledup", journal_lines=ONE_RUN)
write_transcript(dup_wf, "a1",
                 "[monitor] plan=research stage=probe role=research attempt=1")


def _script_closes_research():
    """Exactly what runStatus('research','plan','done', …) appends."""
    with open(os.path.join(dup_state, "events.jsonl"), "a") as f:
        f.write(json.dumps({"ts": "2026-07-25T00:00:02.000Z", "plan": "research",
                            "stage": "plan", "state": "done",
                            "detail": "6/6 researchers returned", "w": "agent"}) + "\n")


exited, rc, err = run_watcher(
    dup_state, dup_wf,
    {"ORCH_QUIET_SECS": "1", "ORCH_EXIT_QUIET_SECS": "999",
     "ORCH_ABANDON_QUIET_SECS": "999"},
    wait=30.0, during=_script_closes_research,
    when=watcher_online(dup_state), after=2.0,
    until=lambda: any(e["stage"] == "complete" and e["state"] in ("done", "failed")
                      for e in events_of(dup_state)))
dup_evs = events_of(dup_state)
research_closes = [e for e in dup_evs if e["stage"] == "plan"
                   and e.get("plan") == "research" and e["state"] in ("done", "failed")]
check("m-3: the settle pass ran (the run closed on the watcher's inference)",
      any(e["stage"] == "complete" and e["state"] in ("done", "failed")
          for e in dup_evs))
check(f"m-3: exactly ONE close for the script-closed plan "
      f"(got {len(research_closes)})", len(research_closes) == 1)
check("m-3: and it is the SCRIPT's verified close, not a 'no verdict' duplicate",
      len(research_closes) == 1 and research_closes[0].get("w") == "agent"
      and "closed, no verdict" not in (research_closes[0].get("detail") or ""))

# M-2: `closeRun` appends the terminal event and SIGTERMs the watcher immediately
# — ~0.2 s after the harness wrote the LAST agent's journal `result`, while the
# poll loop sleeps up to a second. The signal must arm a DRAIN, not kill the
# process where it stands, or that agent's stage chip, decision line and entire
# token usage never reach the append-only record.
DRAIN_RESULT = json.dumps({"type": "result", "agentId": "a1",
                           "result": {"findings": [], "findings_file": "f",
                                      "summary": "s"}}) + "\n"


def write_billed_transcript(wf_dir, agent_id, marker, tokens=4000):
    """A transcript with a real usage row, so the token event has something to say."""
    with open(os.path.join(wf_dir, f"agent-{agent_id}.jsonl"), "w") as f:
        f.write(json.dumps({"type": "user", "timestamp": "2026-07-25T00:00:00.000Z",
                            "message": {"content": marker + "\nprompt body\n"}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {
            "id": "msg_drain_1",
            "usage": {"input_tokens": tokens, "output_tokens": 700,
                      "cache_read_input_tokens": 120,
                      "cache_creation_input_tokens": 60}}}) + "\n")


def result_then_signal(wf_dir, sig):
    """Append the final result, then signal the watcher — `closeRun`'s race.

    No sleep between the two: the smaller the gap, the less chance an ordinary
    poll tick reads the result on its own, which is what makes the SIGKILL
    control below a real control rather than a coin flip.
    """
    def _fire():
        with open(os.path.join(wf_dir, "journal.jsonl"), "a") as f:
            f.write(DRAIN_RESULT)
        run_watcher.proc.send_signal(sig)
    return _fire


def spawn_seen(state_dir, plan="research"):
    return lambda: any(e.get("plan") == plan and e["stage"] != "plan"
                       and e["state"] == "running" for e in events_of(state_dir))


drain_state, drain_wf = make_run(
    "drain", journal_lines=json.dumps({"type": "started", "agentId": "a1"}) + "\n")
write_billed_transcript(drain_wf, "a1",
                        "[monitor] plan=research stage=probe role=research attempt=1")
exited, rc, err = run_watcher(
    drain_state, drain_wf,
    {"ORCH_QUIET_SECS": "999", "ORCH_ABANDON_QUIET_SECS": "999",
     "ORCH_EXIT_QUIET_SECS": "999", "ORCH_DRAIN_SECS": "1"},
    wait=30.0, during=result_then_signal(drain_wf, signal.SIGTERM),
    when=spawn_seen(drain_state), after=10.0)
drain_evs = events_of(drain_state)
check("M-2: the SIGTERM'd watcher exits cleanly (rc 0), not on the signal",
      exited and rc == 0)
check("M-2: the final agent's result survived the signal",
      any(e.get("plan") == "research" and e["stage"] == "probe"
          and e["state"] == "done" for e in drain_evs))
check("M-2: the orchestrator decision line survived too",
      any(e.get("plan") == "orchestrator" and e["stage"] == "research"
          for e in drain_evs))
drain_tokens = [e for e in drain_evs if e["stage"] == "tokens"
                and (e.get("tokens") or {}).get("in")]
check("M-2: and its token usage entered the totals (deltas are wire-only)",
      bool(drain_tokens) and sum(e["tokens"]["in"] for e in drain_tokens) >= 4000)
check("M-2: the drain announces why it stopped",
      any(e["stage"] == "watcher" and "stop signal" in (e.get("detail") or "")
          for e in drain_evs))
# M2 / WRITE-SIDE-5 on the same stream: the per-agent result rollup used to be
# the ONE token line with no `agent` block at all (144 of them on the measured
# run), so a replay that folds quiet ticks away could attribute none of it.
drain_rollup = [e for e in drain_evs
                if e["stage"] == "tokens" and not e.get("quiet")]
check("M2: the result rollup token line carries agent attribution",
      bool(drain_rollup)
      and all((e.get("agent") or {}).get("id") == "a1" for e in drain_rollup))
check("M2: ...with the agent's ABSOLUTE cumulative on it (4180 = 4000+120+60)",
      bool(drain_rollup)
      and drain_rollup[-1]["agent"]["tokens"]["in"] == 4180)

# CONTROL for the arm above: SIGKILL cannot be handled, so it reproduces exactly
# the pre-fix behavior. Same journal, same stimulus, same instant — if these
# events showed up here too, the arm above would be proving nothing.
kill_state, kill_wf = make_run(
    "drainctl", journal_lines=json.dumps({"type": "started", "agentId": "a1"}) + "\n")
write_billed_transcript(kill_wf, "a1",
                        "[monitor] plan=research stage=probe role=research attempt=1")
exited, rc, err = run_watcher(
    kill_state, kill_wf,
    {"ORCH_QUIET_SECS": "999", "ORCH_ABANDON_QUIET_SECS": "999",
     "ORCH_EXIT_QUIET_SECS": "999"},
    wait=30.0, during=result_then_signal(kill_wf, signal.SIGKILL),
    when=spawn_seen(kill_state), after=10.0)
kill_evs = events_of(kill_state)
check("M-2 control: the unhandleable signal did kill the watcher", exited and rc != 0)
check("M-2 control: it had monitored the spawn before dying",
      any(e.get("plan") == "research" and e["state"] == "running" for e in kill_evs))
check("M-2 control: and the result it never drained is ABSENT — so the arm above "
      "is not passing on a lucky poll tick",
      not any(e["stage"] == "probe" and e["state"] == "done" for e in kill_evs))


# ---------------------------------------------------------------------------
# M1 / M2 live: the cadence ceiling, its escape hatch, and the force-flush paths
#
# These run the REAL watcher, because the cadence lives inside main()'s poll
# loop: what is asserted is the shape of the stream it appended (how many quiet
# lines, which deltas, which cumulative) — work, never wall clock (GD-G).
# ---------------------------------------------------------------------------
STARTED_A1 = json.dumps({"type": "started", "agentId": "a1"}) + "\n"
CADENCE_MARKER = "[monitor] plan=sp-a stage=implement role=impl attempt=1"


def usage_row(msg_id, tokens_in, tokens_out=0):
    return json.dumps({"type": "assistant", "message": {
        "id": msg_id, "usage": {"input_tokens": tokens_in,
                                "output_tokens": tokens_out}}}) + "\n"


def write_metered_transcript(wf_dir, agent_id, marker, first=1000):
    """Marker + ONE usage row: the first live tick has exactly `first` in-tokens.

    Only ``input_tokens`` are used so every delta in these arms is an exact,
    readable number (``in`` is the total input volume: fresh + cache r/w).
    """
    with open(os.path.join(wf_dir, f"agent-{agent_id}.jsonl"), "w") as f:
        f.write(json.dumps({"type": "user", "timestamp": "2026-07-25T00:00:00.000Z",
                            "message": {"content": marker + "\nprompt body\n"}}) + "\n")
        f.write(usage_row("msg_1", first))


def append_usage(wf_dir, agent_id, msg_id, tokens_in):
    with open(os.path.join(wf_dir, f"agent-{agent_id}.jsonl"), "a") as f:
        f.write(usage_row(msg_id, tokens_in))


def token_lines(state_dir, quiet, agent_id="a1"):
    return [e for e in events_of(state_dir)
            if e["stage"] == "tokens" and bool(e.get("quiet")) is quiet
            and (e.get("agent") or {}).get("id") == agent_id]


def wire_total(state_dir, agent_id="a1"):
    """Everything the stream ever said about this agent's input tokens."""
    return sum((e.get("tokens") or {}).get("in") or 0
               for e in events_of(state_dir)
               if e["stage"] == "tokens"
               and (e.get("agent") or {}).get("id") == agent_id)


def gdc_fold(events):
    """GD-C's two ways of reading one stream: ``(delta sums, cumulative sums)``.

    Per plan: the sum of every wire ``tokens`` delta, versus the sum of the LAST
    cumulative ``agent.tokens`` seen per (plan, agent) on ANY event. They must
    agree — that equality is what lets a folded replay drop quiet ticks and
    still show the true totals, and it only holds if every agent that stops
    being ticked (result, stale close, exit) flushed on its way out.
    """
    deltas, absolute = {}, {}
    for ev in events:
        plan = ev.get("plan") or "orchestrator"
        if ev.get("tokens"):
            deltas[plan] = deltas.get(plan, 0) + (ev["tokens"].get("in") or 0)
        agent = ev.get("agent") or {}
        if agent.get("id") and isinstance(agent.get("tokens"), dict):
            absolute[(plan, agent["id"])] = agent["tokens"].get("in") or 0
    cumulative = {}
    for (plan, _aid), value in absolute.items():
        cumulative[plan] = cumulative.get(plan, 0) + value
    return deltas, cumulative


def settled(pred, tail=1.5):
    """``pred`` has held for ``tail`` seconds — an EXACT count needs a tail.

    ``until=`` stops the child the instant its predicate is true, so an arm that
    asserts "exactly N lines" while polling for the Nth one is asserting what
    the harness enforced. Keeping the watcher alive for a poll tick or two past
    the line it was waiting for makes an N+1st line observable, so the count is
    the code's, not the harness's — at the cost of one idle interval, not of the
    whole window.
    """
    seen_at = []

    def _ready():
        if not pred():
            return False
        if not seen_at:
            seen_at.append(time.time())
        return time.time() - seen_at[0] >= tail
    return _ready


def check_gdc(label, events):
    deltas, cumulative = gdc_fold(events)
    check(f"GD-C: {label} — delta sum == cumulative sum, per plan "
          f"({deltas} vs {cumulative})", deltas == cumulative)
    check(f"GD-C: {label} — the equality is not vacuous (tokens really flowed)",
          bool(deltas) and sum(deltas.values()) > 0)


# M1 (a): two transcript growths inside ONE cadence window collapse into one
# line whose delta is their sum. The window is short (2 s) because what is being
# proved is the coalescing, not the number.
coal_state, coal_wf = make_run("cadence-coalesce", journal_lines=STARTED_A1)
write_metered_transcript(coal_wf, "a1", CADENCE_MARKER)


def _two_growths():
    append_usage(coal_wf, "a1", "msg_2", 2000)
    append_usage(coal_wf, "a1", "msg_3", 3000)


exited, rc, err = run_watcher(
    coal_state, coal_wf,
    dict(LONG, ORCH_EXIT_QUIET_SECS="999", ORCH_TOKEN_TICK_SECS="2"),
    wait=25.0, during=_two_growths, after=10.0,
    # The stimulus must land AFTER the first tick, or there would be nothing to
    # coalesce: the first tick for an agent always emits.
    when=lambda: bool(token_lines(coal_state, quiet=True)),
    # settled(), not the bare predicate: the count below must be the watcher's,
    # not the moment the harness pulled the plug — the tail keeps it polling
    # past the second line (and past the 2 s ceiling) so a third would show up.
    until=settled(lambda: len(token_lines(coal_state, quiet=True)) >= 2))
coal_quiet = token_lines(coal_state, quiet=True)
check(f"M1: the first tick for a new agent emits at once (got {len(coal_quiet)} lines)",
      bool(coal_quiet) and coal_quiet[0]["tokens"]["in"] == 1000)
check("M1: two growths inside one window emit exactly ONE more line",
      len(coal_quiet) == 2)
check("M1: ...whose delta is their SUM (2000+3000), nothing dropped",
      len(coal_quiet) == 2 and coal_quiet[1]["tokens"]["in"] == 5000)
check("M1: ...and it states the ABSOLUTE running total (GD-C)",
      len(coal_quiet) == 2 and coal_quiet[1]["agent"]["tokens"]["in"] == 6000)
check_gdc("coalesced ticks", events_of(coal_state))

# M1 (b): ORCH_TOKEN_TICK_SECS=0 reproduces today's behaviour exactly — one line
# per transcript growth. Same staged stimulus as the ceiling arm below, so the
# two differ ONLY in the knob.
STAGE_GAP = 2.0


def staged_growths(wf_dir, gate_on=None, timeout=20.0):
    """Two growths separated by a poll interval.

    Under the ``0`` escape hatch they are two lines; under a ceiling above the
    gap they are one. ``gate_on`` is a predicate the second growth waits for —
    the state dir's first growth having reached the wire. The arm that proves
    "no coalescing" must be event-driven, not sleep-driven (GD-G): on a loaded
    box a missed poll would merge two appends the code would have reported
    separately, and the arm would fail for a reason that has nothing to do with
    the escape hatch. The ceiling arm has nothing to poll for (the whole point is
    that nothing is emitted), so it keeps the timed gap.
    """
    def _fire():
        append_usage(wf_dir, "a1", "msg_2", 2000)
        if gate_on is None:
            time.sleep(STAGE_GAP)
        else:
            deadline = time.time() + timeout
            while time.time() < deadline and not gate_on():
                time.sleep(0.05)
        append_usage(wf_dir, "a1", "msg_3", 3000)
    return _fire


zero_state, zero_wf = make_run("cadence-zero", journal_lines=STARTED_A1)
write_metered_transcript(zero_wf, "a1", CADENCE_MARKER)
exited, rc, err = run_watcher(
    zero_state, zero_wf,
    dict(LONG, ORCH_EXIT_QUIET_SECS="999", ORCH_TOKEN_TICK_SECS="0"),
    wait=45.0,
    during=staged_growths(
        zero_wf, gate_on=lambda: len(token_lines(zero_state, quiet=True)) >= 2),
    after=10.0,
    when=lambda: bool(token_lines(zero_state, quiet=True)),
    until=lambda: len(token_lines(zero_state, quiet=True)) >= 3)
zero_deltas = [e["tokens"]["in"] for e in token_lines(zero_state, quiet=True)]
check(f"M1: ORCH_TOKEN_TICK_SECS=0 is the escape hatch — a separate line per "
      f"growth, as before the knob (got {zero_deltas})",
      len(zero_deltas) >= 3 and zero_deltas[0] == 1000)
check("M1: ...and the escape hatch is lossless too (1000+2000+3000)",
      sum(zero_deltas) == 6000)
check_gdc("unthrottled ticks", events_of(zero_state))

# M1 (a)/(c) + M2 (b): the SAME stimulus under a ceiling far above it emits ONE
# line — and the tokens it withheld are not lost: the SIGTERM drain's sweep
# flushes them with the agent's cumulative. A negative arm (nothing must be
# emitted) has nothing to poll for, so its window is the stimulus plus a
# calibrated idle stretch, in the negative_window() house style.
CAP_WINDOW = 2.0 + STAGE_GAP + negative_window(AUTH_EXIT_LATENCY)
cap_state, cap_wf = make_run("cadence-cap", journal_lines=STARTED_A1)
write_metered_transcript(cap_wf, "a1", CADENCE_MARKER)
exited, rc, err = run_watcher(
    cap_state, cap_wf,
    dict(LONG, ORCH_EXIT_QUIET_SECS="999", ORCH_TOKEN_TICK_SECS="999",
         ORCH_DRAIN_SECS="1"),
    wait=CAP_WINDOW, during=staged_growths(cap_wf), after=2.0,
    when=lambda: bool(token_lines(cap_state, quiet=True)),
    # Ends the arm EARLY only if the ceiling leaked, so a failure is cheap and a
    # pass costs the full window it is asserting over.
    until=lambda: len(token_lines(cap_state, quiet=True)) >= 2)
cap_quiet = token_lines(cap_state, quiet=True)
cap_flush = token_lines(cap_state, quiet=False)
check(f"M1: a 999s ceiling suppresses every tick after the first "
      f"(window {CAP_WINDOW:.1f}s, got {len(cap_quiet)})", len(cap_quiet) == 1)
check("M2: the drain's exit sweep flushes what the ceiling withheld",
      bool(cap_flush) and cap_flush[-1]["tokens"]["in"] == 5000)
check("M2: ...stating the agent's absolute cumulative, not just a delta",
      bool(cap_flush) and cap_flush[-1]["agent"]["tokens"]["in"] == 6000)
check("GD-D: throttling is LOSSLESS — every withheld token still reached the wire",
      wire_total(cap_state) == 6000)
check_gdc("ceiling + drain sweep", events_of(cap_state))

# M2 (a): a stale/abandoned close. Its agent never results, so before this fix
# it stated no total and flushed nothing — on the measured run that was 15 of
# 167 agents and 9.14% of the input tokens, alive only inside quiet ticks. The
# ceiling is set absurdly high so the growth can reach the wire ONLY through the
# stale flush: the arm cannot pass by accident.
stale_flush_state, stale_flush_wf = make_run("staleflush", journal_lines=STARTED_A1)
write_metered_transcript(stale_flush_wf, "a1", CADENCE_MARKER)
exited, rc, err = run_watcher(
    stale_flush_state, stale_flush_wf,
    {"ORCH_QUIET_SECS": "1", "ORCH_EXIT_QUIET_SECS": "1",
     "ORCH_ABANDON_QUIET_SECS": "3", "ORCH_TOKEN_TICK_SECS": "999"},
    wait=40.0, during=lambda: append_usage(stale_flush_wf, "a1", "msg_2", 5000),
    after=10.0, when=lambda: bool(token_lines(stale_flush_state, quiet=True)))
stale_evs = events_of(stale_flush_state)
stale_rows = [e for e in stale_evs if e["state"] == "stale"
              and (e.get("agent") or {}).get("id") == "a1"]
check("M2: the abandoned agent is closed stale (the arm's precondition)",
      bool(stale_rows))
check("M2: the stale row states the agent's TRUE total, not silence",
      bool(stale_rows) and (stale_rows[-1]["agent"].get("tokens") or {}).get("in") == 6000)
stale_flush = token_lines(stale_flush_state, quiet=False)
check("M2: ...and a flushing delta line lands with it",
      bool(stale_flush) and stale_flush[-1]["tokens"]["in"] == 5000)
check("M2: exactly one quiet tick, so the 5000 lived nowhere else (anti-vacuity)",
      len(token_lines(stale_flush_state, quiet=True)) == 1)
check("M2: the stale-closed agent's usage reached the totals in full",
      wire_total(stale_flush_state) == 6000)
check_gdc("stale/abandoned close", stale_evs)

# M2 (a), the OTHER stale close: DRIVER-1's respawn branch, where a same-role
# spawn at a GREATER attempt closes the predecessor that never returned a result.
# It is a different code path from the abandon-quiet close above — journal-driven
# rather than idle-driven, and stamped with the NEW spawn's timestamp — and it
# carries the same obligation: an agent that leaves `running` without a result is
# never ticked again, so that is its last chance to state a total. The ceiling is
# absurdly high so a1's growth can reach the wire ONLY through the stale flush.
RESPAWN_MARKER_1 = "[monitor] plan=sp-r stage=implement role=impl attempt=1"
RESPAWN_MARKER_2 = "[monitor] plan=sp-r stage=implement role=impl attempt=2"
resp_state, resp_wf = make_run("respawn-stale", journal_lines=STARTED_A1)
write_metered_transcript(resp_wf, "a1", RESPAWN_MARKER_1)


def _respawn():
    """a1 accrues (withheld by the ceiling), then attempt 2 spawns over it."""
    append_usage(resp_wf, "a1", "msg_2", 5000)
    write_metered_transcript(resp_wf, "a2", RESPAWN_MARKER_2, first=700)
    with open(os.path.join(resp_wf, "journal.jsonl"), "a") as f:
        f.write(json.dumps({"type": "started", "agentId": "a2"}) + "\n")


def _respawn_closed():
    evs = events_of(resp_state)
    stale = any(e["state"] == "stale" and (e.get("agent") or {}).get("id") == "a1"
                for e in evs)
    # Both, so the poll can't catch the stale row before its flush line lands.
    return stale and bool(token_lines(resp_state, quiet=False, agent_id="a1"))


exited, rc, err = run_watcher(
    resp_state, resp_wf,
    dict(LONG, ORCH_EXIT_QUIET_SECS="999", ORCH_TOKEN_TICK_SECS="999"),
    wait=40.0, during=_respawn, after=10.0,
    # The stimulus waits for a1's first tick: without it there is nothing
    # withheld, and the arm would prove only that a stale row exists.
    when=lambda: bool(token_lines(resp_state, quiet=True, agent_id="a1")),
    until=_respawn_closed)
resp_evs = events_of(resp_state)
resp_stale = [e for e in resp_evs if e["state"] == "stale"
              and (e.get("agent") or {}).get("id") == "a1"]
resp_flush = token_lines(resp_state, quiet=False, agent_id="a1")
check("M2: a higher-attempt respawn stale-closes its predecessor (DRIVER-1)",
      bool(resp_stale))
check("M2: the respawn stale row states a1's TRUE cumulative, not silence",
      bool(resp_stale)
      and (resp_stale[-1]["agent"].get("tokens") or {}).get("in") == 6000)
check("M2: ...and the withheld delta lands with it (5000)",
      bool(resp_flush) and resp_flush[-1]["tokens"]["in"] == 5000)
check("M2: exactly one quiet tick for a1, so the 5000 lived nowhere else",
      len(token_lines(resp_state, quiet=True, agent_id="a1")) == 1)
check("M2: the respawn-closed agent's usage reached the totals in full",
      wire_total(resp_state, "a1") == 6000)
check_gdc("respawn stale close", resp_evs)

# D7 on the stale rows: a stale close may never publish a RAW reading.
# agent_paths() unions transcript COPIES, so a pruned or rotated copy can shrink
# the union — and when it has, the flush that follows the stale row is SILENT
# (every delta clamps to 0), leaving that row as the last word on the agent's
# cumulative. A raw reading there would be the one place a counter goes
# backwards, and GD-C's "delta sum == last cumulative" equality would be false on
# a real stream, under-reporting the snapshot fold built on top of it.
#
# The stimulus is what a restart actually finds: a checkpoint whose baseline
# (50000) is far above what the surviving transcript now reads (1000), plus the
# events.jsonl that baseline came from.
D7_BASE = {"in": 50000, "out": 0, "cached": 0, "cache_write": 0}
D7_INFO = {"plan": "sp-d7", "stage": "implement", "role": "impl", "attempt": 1}
d7_state, d7_wf = make_run("stale-regress", journal_lines=STARTED_A1)
d7_journal = os.path.join(d7_wf, "journal.jsonl")
write_metered_transcript(d7_wf, "a1",
                         "[monitor] plan=sp-d7 stage=implement role=impl attempt=1")
with open(os.path.join(d7_state, ".watcher-state.json"), "w") as f:
    # offset at EOF: the `started` line is already consumed, so the seeded
    # agents/running/baseline stand exactly as a restart would find them.
    json.dump({"offset": os.path.getsize(d7_journal), "journal": d7_journal,
               "agents": {"a1": D7_INFO}, "running": ["a1"],
               "tok_emitted": {"a1": dict(D7_BASE)}, "tok_tick_at": {},
               "plans": {"sp-d7": "running"}, "decisive": {},
               "last_result_ok": {}}, f)
with open(os.path.join(d7_state, "events.jsonl"), "w") as f:
    f.write(json.dumps({"ts": "2026-07-25T00:00:00.000Z", "plan": "sp-d7",
                        "stage": "tokens", "state": "info",
                        "detail": "implement:impl #1 running", "w": "watcher",
                        "tokens": dict(D7_BASE), "quiet": True,
                        "agent": {"id": "a1", "shortId": "a1",
                                  "label": "implement:impl #1",
                                  "state": "running",
                                  "tokens": dict(D7_BASE)}}) + "\n")
exited, rc, err = run_watcher(
    d7_state, d7_wf,
    {"ORCH_QUIET_SECS": "1", "ORCH_EXIT_QUIET_SECS": "999",
     "ORCH_ABANDON_QUIET_SECS": "3", "ORCH_TOKEN_TICK_SECS": "999"},
    wait=40.0,
    until=lambda: any(e["state"] == "stale" for e in events_of(d7_state)))
d7_evs = events_of(d7_state)
d7_stale = [e for e in d7_evs if e["state"] == "stale"
            and (e.get("agent") or {}).get("id") == "a1"]
check("D7: the shrunken-transcript agent is stale-closed (the arm's precondition)",
      bool(d7_stale))
check("D7: the stale row publishes the CLAMPED baseline (50000), never the raw "
      "reading (1000) — no stale row may lower a counter",
      bool(d7_stale)
      and (d7_stale[-1]["agent"].get("tokens") or {}).get("in") == 50000)
check("D7: ...and the flush that follows stays silent, so that row IS the last "
      "word on the cumulative",
      not token_lines(d7_state, quiet=False))
check_gdc("stale close over a shrunken transcript", d7_evs)

# M2 / GD-D, the fourth force-flush site: an UNCLASSIFIED agent's result. The
# `started` branch appends every agent to `running` BEFORE it knows whether the
# prompt carries a [monitor] marker (GD-7: harness facts create the node), so an
# unclassified agent is ticked exactly like a classified one — under
# plan="orchestrator" — and its result ends the ticking just as finally. The
# hole this arm guards is total, not partial: the first tick a second after the
# spawn legitimately reads a transcript with no usage rows yet, which SPENDS the
# first-tick exemption and stamps the window, so an agent that finishes inside
# one ceiling would report nothing at all where the pre-cadence per-second tick
# reported essentially everything. GD-C cannot catch it — both sides of the
# equality under-report by the same amount — so the assertion is the TOTAL.
uncl_state, uncl_wf = make_run("unclassified-flush", journal_lines=STARTED_A1)
write_metered_transcript(uncl_wf, "a1", "no [monitor] marker in this prompt")


def _uncl_result():
    """a1 accrues (withheld by the ceiling), then the journal reports its result."""
    append_usage(uncl_wf, "a1", "msg_2", 5000)
    with open(os.path.join(uncl_wf, "journal.jsonl"), "a") as f:
        f.write(json.dumps({"type": "result", "agentId": "a1",
                            "result": {"summary": "s"}}) + "\n")


exited, rc, err = run_watcher(
    uncl_state, uncl_wf,
    dict(LONG, ORCH_EXIT_QUIET_SECS="999", ORCH_TOKEN_TICK_SECS="999"),
    wait=40.0, during=_uncl_result, after=15.0,
    # The stimulus waits for the first tick: without it there is nothing
    # withheld, and the arm would prove only that a result line exists.
    when=lambda: bool(token_lines(uncl_state, quiet=True)),
    until=lambda: bool(token_lines(uncl_state, quiet=False)))
uncl_evs = events_of(uncl_state)
check("GD-7: the unclassified agent still gets a node (the arm's precondition)",
      any((e.get("agent") or {}).get("unconventional") for e in uncl_evs))
uncl_flush = token_lines(uncl_state, quiet=False)
check("M2: an unclassified agent's result flushes what the ceiling withheld (5000)",
      bool(uncl_flush) and uncl_flush[-1]["tokens"]["in"] == 5000)
check("M2: ...stating its ABSOLUTE cumulative (6000), foldable like any other",
      bool(uncl_flush) and uncl_flush[-1]["agent"]["tokens"]["in"] == 6000)
check("M2: exactly one quiet tick, so the 5000 lived nowhere else (anti-vacuity)",
      len(token_lines(uncl_state, quiet=True)) == 1)
check("M2: the unclassified agent's usage reached the totals in full",
      wire_total(uncl_state) == 6000)
check_gdc("unclassified result flush", uncl_evs)

# WRITE-SIDE-10 / startup: the backfill re-reads EVERY agent in `tok_emitted`,
# which on a resumed watcher is every agent the run has ever tracked (167 on the
# measured run) against a handful still in flight. Those reads are terminal, so
# what they memoise must not survive startup — otherwise a restart re-creates
# exactly the order-1e5 retention drop_usage_cache() exists to prevent, on the
# one path where tok_emitted is large, and the cadence-window map grows with the
# LENGTH of the run instead of with concurrency.
bf_state, bf_wf = make_run("backfill-sweep", journal_lines=STARTED_A1)
write_metered_transcript(bf_wf, "r1", CADENCE_MARKER)   # still in flight
write_metered_transcript(bf_wf, "b1", CADENCE_MARKER)   # resulted long ago
bf_journal = os.path.join(bf_wf, "journal.jsonl")
with open(os.path.join(bf_state, ".watcher-state.json"), "w") as f:
    json.dump({"offset": os.path.getsize(bf_journal), "journal": bf_journal,
               "agents": {}, "running": ["r1"],
               "tok_emitted": {"r1": {}, "b1": {}},
               # A window inherited from the previous session, for an agent that
               # is no longer running: the sweep must clear it too.
               "tok_tick_at": {"b1": 1.0}, "plans": {}, "decisive": {},
               "last_result_ok": {}}, f)
# The probe cuts the process off at the first statement AFTER the backfill
# (install_stop_handlers), then reports what startup left behind: cache keys and
# checkpointed windows — work counters, no wall clock (GD-G).
BACKFILL_PROBE = """
import json, os
import decision_watcher as d


class _Cut(Exception):
    pass


def _cut(*a, **k):
    raise _Cut


d.install_stop_handlers = _cut
try:
    d.main()
except _Cut:
    pass
with open(os.path.join(d.STATE_DIR, ".watcher-state.json")) as f:
    windows = sorted(json.load(f).get("tok_tick_at") or {})
print(json.dumps({"cache": sorted(os.path.basename(p) for p in d._USAGE_CACHE),
                  "windows": windows}))
"""
bf_out = subprocess.run(
    [sys.executable, "-c", BACKFILL_PROBE],
    env=dict(os.environ, ORCH_STATE_DIR=bf_state, ORCH_WF_DIR=bf_wf,
             ORCH_WF_GLOB_ROOT=os.path.join(BASE, "glob")),
    cwd=MOD_DIR, capture_output=True, text=True)
bf = json.loads(bf_out.stdout.strip().splitlines()[-1]) if bf_out.stdout.strip() else {}
bf_tokens = [e for e in events_of(bf_state) if e["stage"] == "tokens"]
check("backfill: the startup pass really read both tracked agents (precondition)",
      {(e.get("agent") or {}).get("id") for e in bf_tokens} == {"r1", "b1"})
check("backfill: the in-flight agent keeps its parse memo (it will be ticked)",
      bf.get("cache") == ["agent-r1.jsonl"])
check("backfill: ...and the finished agent's memo is dropped — a restart must "
      "not retain one dead entry per agent the run ever tracked",
      "agent-b1.jsonl" not in (bf.get("cache") or []))
check("backfill: the checkpoint keeps a cadence window only for what is running",
      bf.get("windows") == ["r1"])

# R-40/M1: the SAME shape, but the only close is the WATCHER's own settle pass —
# it must keep running, and it must still be there to monitor the next spawn.
# This is the failure mode that lost live visibility: the watcher wrote
# "PASS -> spawn critique", inferred the run was over and exited, and the next
# sub-plan ran unmonitored.
own_state, own_wf = make_run("owncomplete", journal_lines=ONE_RUN)
write_transcript(own_wf, "a1",
                 "[monitor] plan=research stage=probe role=research attempt=1")
write_transcript(own_wf, "b1",
                 "[monitor] plan=sp-b stage=implement role=impl attempt=1")


def _late_spawn():
    with open(os.path.join(own_wf, "journal.jsonl"), "a") as f:
        f.write(json.dumps({"type": "started", "agentId": "b1"}) + "\n")


exited, rc, err = run_watcher(
    own_state, own_wf,
    {"ORCH_QUIET_SECS": "1", "ORCH_EXIT_QUIET_SECS": "1",
     "ORCH_ABANDON_QUIET_SECS": "999"},
    wait=20.0, during=_late_spawn, after=10.0,
    # The precondition is not "some seconds passed" but "the watcher has closed
    # the run on its OWN inference" — that close is what must not stop it, so the
    # late spawn is delivered the moment it is observed.
    when=lambda: any(e["stage"] == "complete" and e["state"] == "done"
                     for e in events_of(own_state)),
    until=lambda: any(e.get("plan") == "sp-b" and e["state"] == "running"
                      for e in events_of(own_state)))
own_evs = events_of(own_state)
check("R-40/M1: the watcher's OWN complete event does not stop it", not exited)
check("R-40/M1: it did close the badge (the inference still runs)",
      any(e["stage"] == "complete" and e["state"] == "done" for e in own_evs))
check("R-40/M1: it took neither self-exit route", not self_exited(own_evs))
check("R-40/M1: the post-close spawn is still monitored",
      any(e.get("plan") == "sp-b" and e["state"] == "running" for e in own_evs))

# ORCH_NO_SELF_EXIT: even an authorized close cannot stop an opted-out watcher.
# No `until` here on purpose: the appended close would satisfy any predicate
# instantly and the arm would pass without the opt-out ever mattering. The window
# is sized from what the SAME watcher without the opt-out actually needed
# (AUTH_EXIT_LATENCY, measured two arms up), so it is neither a guess nor a
# hardcoded sleep the suite pays on every gate.
NOEXIT_WINDOW = negative_window(AUTH_EXIT_LATENCY)
noexit_state, noexit_wf = make_run("noselfexit", journal_lines=ONE_RUN)
write_transcript(noexit_wf, "a1",
                 "[monitor] plan=research stage=probe role=research attempt=1")
exited, rc, err = run_watcher(
    noexit_state, noexit_wf,
    dict(LONG, ORCH_EXIT_QUIET_SECS="0", ORCH_NO_SELF_EXIT="1"),
    wait=NOEXIT_WINDOW, during=append_complete(noexit_state),
    when=watcher_online(noexit_state), after=2.0)
check(f"R-40: ORCH_NO_SELF_EXIT keeps the watcher alive through an authorized "
      f"close (window {NOEXIT_WINDOW:.1f}s; the unprotected sibling acted on the "
      f"same close in "
      f"{'n/a' if AUTH_EXIT_LATENCY is None else format(AUTH_EXIT_LATENCY, '.2f')}s)",
      not exited)
check("R-40: the opted-out watcher still saw the close it ignored",
      any(e["stage"] == "complete" for e in events_of(noexit_state)))

# m2: an ABANDONED run — the session was killed mid-agent, so the journal `result`
# never comes and no driver close ever will. The watcher must close the orphaned
# agent `stale`, settle the run and stop itself, instead of pinning a dead run
# forever (CONVO-14's three orphans).
ab_state, ab_wf = make_run(
    "abandoned",
    journal_lines=json.dumps({"type": "started", "agentId": "a1"}) + "\n")
write_transcript(ab_wf, "a1",
                 "[monitor] plan=sp-a stage=implement role=impl attempt=1")
exited, rc, err = run_watcher(
    ab_state, ab_wf,
    {"ORCH_QUIET_SECS": "1", "ORCH_EXIT_QUIET_SECS": "1",
     "ORCH_ABANDON_QUIET_SECS": "3"},
    wait=30.0)
ab_evs = events_of(ab_state)
check("m2: the abandoned run's watcher exited on its own", exited)
check("m2: it exited cleanly (rc 0)", exited and rc == 0)
check("m2: the never-resulted agent is closed stale, not left running",
      any(e["state"] == "stale" and "abandoned" in e["detail"] for e in ab_evs))
check("m2: the stale row keeps the agent's full id",
      any(e.get("agent", {}).get("id") == "a1" and e["state"] == "stale"
          for e in ab_evs))
check("m2: the exit says the run was abandoned, not that a driver closed it",
      any(e["stage"] == "watcher" and "abandoned" in e["detail"]
          and "exiting" in e["detail"] for e in ab_evs))
check("m2: the plan card was settled before the exit",
      any(e["stage"] == "plan" and e.get("plan") == "sp-a"
          and e["state"] in ("done", "failed") for e in ab_evs))

# M2: a config published AFTER the daemons started is picked up by the running
# watcher (the documented launch order guarantees this is the normal case).
cfg_state, cfg_wf = make_run("liveconfig", journal_lines=ONE_RUN)
write_transcript(cfg_wf, "a1",
                 "[monitor] plan=research stage=probe role=research attempt=1")


def _publish_config():
    with open(os.path.join(cfg_state, "orch-config.json"), "w") as f:
        json.dump({"max_plan_attempts": 9, "max_finalgate_attempts": 7,
                   "strategy": "parallel"}, f)


exited, rc, err = run_watcher(
    cfg_state, cfg_wf, dict(LONG, ORCH_EXIT_QUIET_SECS="999"),
    wait=15.0, during=_publish_config, after=5.0,
    # `when` is load-bearing here, not just cheaper: refresh_caps() compares
    # against the mtime it recorded AT IMPORT, so a config written before the
    # watcher is online would be picked up silently at import and no reload event
    # would ever be emitted. The heartbeat proves import is done.
    when=watcher_online(cfg_state),
    until=lambda: any("config reloaded" in e["detail"] for e in events_of(cfg_state)))
check("M2: the running watcher reloaded the caps the script published",
      any("config reloaded" in e["detail"] and "plan cap 9" in e["detail"]
          and "finalgate cap 7" in e["detail"] for e in events_of(cfg_state)))
check("M2: the reload records the published strategy",
      any("strategy parallel" in e["detail"] for e in events_of(cfg_state)))

# R-40 (session scope): a STALE complete event — already in events.jsonl when the
# watcher starts, from an earlier phase in the SAME task folder — must NOT kill a
# watcher whose journal then grows a new spawn.
stale_state, stale_wf = make_run(
    "stale", journal_lines=ONE_RUN,
    state_files={"events.jsonl": json.dumps(
        {"ts": "2026-07-24T00:00:00.000Z", "plan": "orchestrator", "stage": "complete",
         "state": "done", "detail": "an EARLIER phase closed here", "w": "agent"}) + "\n"})
write_transcript(stale_wf, "a1",
                 "[monitor] plan=research stage=probe role=research attempt=1")
write_transcript(stale_wf, "b1",
                 "[monitor] plan=sp-b stage=implement role=impl attempt=1")


def _spawn_more():
    with open(os.path.join(stale_wf, "journal.jsonl"), "a") as f:
        f.write(json.dumps({"type": "started", "agentId": "b1"}) + "\n")


exited, rc, err = run_watcher(stale_state, stale_wf,
                              dict(LONG, ORCH_EXIT_QUIET_SECS="0"),
                              wait=10.0, during=_spawn_more, after=5.0,
                              # The stale close is already in the stream, so what
                              # this arm needs before spawning is a watcher that
                              # has recorded its baseline past it.
                              when=watcher_online(stale_state),
                              until=lambda: any(
                                  e.get("plan") == "sp-b" and e["state"] == "running"
                                  for e in events_of(stale_state)))
check("R-40: a STALE complete event does not kill a live watcher", not exited)
stale_evs = events_of(stale_state)
check("R-40: the post-stale spawn is still monitored",
      any(e.get("plan") == "sp-b" and e["state"] == "running" for e in stale_evs))
check("R-40: the watcher took neither self-exit route",
      not self_exited(stale_evs))

stay_state, stay_wf = make_run("stay")
STAY_WINDOW = negative_window(AUTH_EXIT_LATENCY)
exited, rc, err = run_watcher(stay_state, stay_wf,
                              dict(LONG, ORCH_EXIT_QUIET_SECS="0"),
                              wait=STAY_WINDOW)
check(f"R-40: no terminal complete event -> the watcher stays alive "
      f"(window {STAY_WINDOW:.1f}s, exit window 0s)", not exited)


# ---------------------------------------------------------------------------
# R-58 through the REAL main(), not a re-implementation (m5)
#
# tests/fixtures/run-wf_829e6f58 is a frozen research run WITH transcripts: six
# researchers + one synthesizer, spread over two session dirs (a /clear rotated
# the session mid-run). The pre-fix watcher wrote
# `research plan failed "loop exited -> synthesis"` for this exact journal. Here
# the real process consumes the real bytes: markers are parsed from the real
# prompts, plans open and close through main()'s own branches, and the settle pass
# is the module's, so a regression INSIDE main() — the heuristic returning ungated,
# the close predicate narrowing back to decisive-only — fails this arm even if
# every unit predicate still passes.
# ---------------------------------------------------------------------------
FIXTURES = REPO_FIXTURES
WF_829 = os.path.join(FIXTURES, "run-wf_829e6f58",
                      "dd469822-2546-47d9-aaa3-31db4cb705e8", "subagents",
                      "workflows", "wf_829e6f58-b2f")
if not os.path.isfile(os.path.join(WF_829, "journal.jsonl")):
    skip(f"R-58 e2e replay: fixture missing ({WF_829})")
else:
    e2e_state = os.path.join(BASE, "proc", "r58e2e", "state")
    os.makedirs(e2e_state, exist_ok=True)
    exited, rc, err = run_watcher(
        e2e_state, WF_829,
        {"ORCH_QUIET_SECS": "1", "ORCH_EXIT_QUIET_SECS": "1",
         "ORCH_ABANDON_QUIET_SECS": "3"},
        wait=90.0, glob_root=FIXTURES)
    e2e = events_of(e2e_state)
    plan_events = [e for e in e2e if e["stage"] == "plan"]
    check("R-58 e2e: the real watcher emits ZERO failed plan badges on the real run",
          [e for e in plan_events if e["state"] == "failed"] == [])
    check("R-58 e2e: no event carries the fabricated 'loop exited ->' detail",
          not [e for e in e2e if "loop exited ->" in (e.get("detail") or "")])
    check("R-58 e2e: the research fan-out closes done",
          any(e.get("plan") == "research" and e["state"] == "done" for e in plan_events))
    check("R-58 e2e: the close is labelled 'closed, no verdict'",
          any(e.get("plan") == "research" and e["state"] == "done"
              and "closed, no verdict" in (e.get("detail") or "") for e in plan_events))
    check("R-58 e2e: the synthesis plan closes done",
          any(e.get("plan") == "synthesis" and e["state"] == "done" for e in plan_events))
    check("R-58 e2e: the run closes `orchestrator complete done`",
          any(e["stage"] == "complete" and e["state"] == "done" for e in e2e))
    # R-13 through main(): the six parallel researchers keep six distinct chips
    # and full-width ids, from the real prompts' markers.
    chips = {e["stage"] for e in e2e
             if e.get("plan") == "research" and e["stage"] not in ("plan", "tokens")}
    check(f"R-13 e2e: six researchers get six distinct stage chips (got {len(chips)})",
          len(chips) == 6)
    ids = {e["agent"]["id"] for e in e2e if e.get("agent")}
    check("R-13 e2e: every agent row carries the full 17-hex agentId",
          bool(ids) and all(len(i) == 17 for i in ids))
    check("R-13 e2e: shortId travels alongside, display-only",
          all(e["agent"]["shortId"] == e["agent"]["id"][:8]
              for e in e2e if e.get("agent")))
    check("R-40 e2e: with no driver close, the abandoned window stops it cleanly",
          exited and rc == 0)


# ---------------------------------------------------------------------------
# SD-4 / R-58 on the FROZEN EVENT STREAM: the failed-then-done correction lines
# this session's own runs left behind must read as `done` (last-event-wins in
# FILE order — the corrective line's ts is deliberately EARLIER in the real
# bytes, so a ts sort would resurrect the failure).
# ---------------------------------------------------------------------------
def fold_plan_states(path):
    """SD-4's read rule: last terminal event per (plan, stage='plan') wins."""
    states = {}
    with open(path) as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if ev.get("stage") == "plan" and ev.get("state") in ("done", "failed"):
                states[ev.get("plan")] = (ev["state"], ev.get("detail") or "")
    return states


LEGACY = os.path.join(FIXTURES, "legacy")
for name, plan in (("touch-mongo-live-events.jsonl", "research"),
                   ("touch-full-recon-events.jsonl", "research")):
    path = os.path.join(LEGACY, name)
    if not os.path.isfile(path):
        skip(f"SD-4 fold for {name}: fixture missing")
        continue
    folded = fold_plan_states(path)
    check(f"SD-4: {name} — the corrective done beats the fabricated failed",
          folded.get(plan, ("", ""))[0] == "done")
    check(f"SD-4: {name} — the fabricated failed line is really in the fixture",
          any('"state": "failed"' in ln and "loop exited ->" in ln
              for ln in open(path)))
# Negative control: the user-killed run's failures are REAL and must survive.
killed = os.path.join(LEGACY, "touch-repo-recon-events.jsonl")
if os.path.isfile(killed):
    check("SD-4: a genuinely failed run still folds to failed (control)",
          fold_plan_states(killed).get("research", ("", ""))[0] == "failed")
else:
    skip("SD-4 negative control: fixture missing")


shutil.rmtree(os.path.join(BASE, "proc"), ignore_errors=True)

print()
for message in SKIPS:
    print(f"skipped: {message}")
if FAILS:
    print(f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print(f"\nALL WATCHER TESTS PASSED ({len(SKIPS)} skipped)")
