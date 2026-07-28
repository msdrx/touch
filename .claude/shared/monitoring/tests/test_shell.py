#!/usr/bin/env python3
"""Stdlib-only tests for sp-shell fixes (status.sh + implement-plan implement
workflow template + docs). Run as `python3 test_shell.py`; exits non-zero on the first failure.
No pytest, no omnigent imports. Uses ephemeral dirs under /tmp/claude-1000.
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
STATUS_SH = REPO / ".claude/shared/monitoring/status.sh"
WATCHER_PY = REPO / ".claude/shared/monitoring/decision_watcher.py"
TEMPLATE = REPO / ".claude/skills/implement-plan/templates/implement.workflow.js"
RESEARCH_TEMPLATE = REPO / ".claude/skills/execute-research/templates/research.workflow.js"
MONITORING_MD = REPO / ".claude/shared/monitoring/monitoring.md"
M_SKILL = REPO / ".claude/skills/m-orchestrator/SKILL.md"
D_SKILL = REPO / ".claude/skills/implement-plan/SKILL.md"
GITIGNORE = REPO / ".gitignore"

TMP_ROOT = "/tmp/claude-1000"
os.makedirs(TMP_ROOT, exist_ok=True)

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def run_status(state_dir, args, extra_env=None, unset_state_dir=False, script=None):
    env = {k: v for k, v in os.environ.items() if k not in ("ORCH_STATE_DIR", "ORCH_TITLE")}
    if not unset_state_dir:
        env["ORCH_STATE_DIR"] = str(state_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script or STATUS_SH), *args],
        env=env, capture_output=True, text=True,
    )


# --- status.sh: creates missing state dir + appends one valid JSON line (SHELL-6)
def test_status_creates_state_dir():
    print("test_status_creates_state_dir")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        # A not-yet-created nested dir under the fresh base.
        state_dir = os.path.join(base, "does", "not", "exist", "yet")
        check(not os.path.isdir(state_dir), "state dir does not exist before call")
        proc = run_status(state_dir, ["myplan", "implement", "running", "attempt 1: go"])
        check(proc.returncode == 0, "status.sh exits 0")
        check(os.path.isdir(state_dir), "status.sh created the missing state dir")
        events = os.path.join(state_dir, "events.jsonl")
        check(os.path.isfile(events), "events.jsonl was created")
        lines = Path(events).read_text().splitlines()
        check(len(lines) == 1, f"exactly one event line appended (got {len(lines)})")
        obj = json.loads(lines[0])
        check(obj["plan"] == "myplan" and obj["stage"] == "implement"
              and obj["state"] == "running" and obj["detail"] == "attempt 1: go",
              "event fields round-trip")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- status.sh: hostile detail lands as literal one-line escaped JSON (injection guard)
def test_status_injection_safe():
    print("test_status_injection_safe")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        sentinel = os.path.join(base, "PWNED")
        hostile = f'$(touch {sentinel}) `touch {sentinel}` "quote" and\na newline'
        proc = run_status(state_dir, ["p", "s", "running", hostile])
        check(proc.returncode == 0, "status.sh exits 0 on hostile detail")
        check(not os.path.exists(sentinel), "command substitution did NOT execute (no PWNED file)")
        events = Path(state_dir) / "events.jsonl"
        raw = events.read_text()
        # File must be exactly one physical line (newline escaped inside JSON).
        check(raw.count("\n") == 1, "output is a single physical line (trailing newline only)")
        obj = json.loads(raw.splitlines()[0])
        check(obj["detail"] == hostile, "detail preserved verbatim (incl. newline as literal)")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- status.sh: ORCH_STATE_DIR unset warns on stderr (SHELL-5). Use a COPY of the
#     script in a throwaway dir so the fallback write never touches the real module dir.
def test_status_unset_warns():
    print("test_status_unset_warns")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        script_copy = os.path.join(base, "status.sh")
        shutil.copy(STATUS_SH, script_copy)
        proc = run_status(None, ["p", "s", "running", "hi"], unset_state_dir=True, script=script_copy)
        check(proc.returncode == 0, "status.sh still exits 0 when ORCH_STATE_DIR unset")
        check("ORCH_STATE_DIR unset" in proc.stderr, "warning emitted to stderr when unset")
        # Fallback write lands next to the copied script, NOT the real module dir.
        check(os.path.isfile(os.path.join(base, "events.jsonl")),
              "fallback wrote events.jsonl next to the (copied) script")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- status.sh: ORCH_PLANS_TOTAL declares the run's plan-card total (additive,
#     best-effort like ORCH_TITLE: garbage warns and is omitted, never fails)
def test_status_plans_total():
    print("test_status_plans_total")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        events = Path(state_dir) / "events.jsonl"
        proc = run_status(state_dir, ["divide", "plan", "done", "15 sub-plans"],
                          extra_env={"ORCH_PLANS_TOTAL": "17"})
        check(proc.returncode == 0, "status.sh exits 0 with ORCH_PLANS_TOTAL set")
        obj = json.loads(events.read_text().splitlines()[-1])
        check(obj.get("plans_total") == 17, "plans_total lands as an integer")
        check(obj["plan"] == "divide" and obj["state"] == "done",
              "five-key core event shape preserved alongside plans_total")
        proc = run_status(state_dir, ["p", "s", "info", "hi"],
                          extra_env={"ORCH_PLANS_TOTAL": "not-a-number"})
        check(proc.returncode == 0, "a garbage total does not fail the caller")
        check("ORCH_PLANS_TOTAL" in proc.stderr, "a garbage total warns on stderr")
        obj = json.loads(events.read_text().splitlines()[-1])
        check("plans_total" not in obj, "a garbage total is omitted, event still appended")
        proc = run_status(state_dir, ["p", "s", "info", "hi"])
        obj = json.loads(events.read_text().splitlines()[-1])
        check("plans_total" not in obj, "unset env leaves the key out entirely")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- loop.workflow.js static assertions
def test_template_static():
    print("test_template_static")
    src = TEMPLATE.read_text()
    # SHELL-2 / D2: Test marker is role=test, and no gate:run remains in the reference loop.
    check("stage=test role=test attempt=" in src, "Test marker line reads role=test")
    check("role=gate:run" not in src, "no role=gate:run remains in the reference loop")
    # SHELL-10: statusCmd quotes the path interpolations.
    check('ORCH_STATE_DIR="${TASK}" bash "${S}" "${plan}"' in src,
          "statusCmd quotes ${TASK}/${S}/${plan}")
    # SHELL-8: died-gate fallback findings_file is NOT the empty string.
    check("findings_file: ''" not in src and 'findings_file: ""' not in src,
          "no empty-string findings_file fallback remains")
    check("writePlaceholderFindings" in src,
          "died-gate fallback writes/points at a placeholder findings file")
    # Both death paths (gate + critique) route through the placeholder helper.
    check(src.count("await writePlaceholderFindings(") >= 2,
          "both gate and critique death fallbacks use the placeholder")


# --- docs static assertions
def test_docs_static():
    print("test_docs_static")
    md = MONITORING_MD.read_text()
    ms = M_SKILL.read_text()
    ds = D_SKILL.read_text()

    # cache_write in token schema blocks of both docs.
    check("cache_write" in md, "monitoring.md documents cache_write")
    check("cache_write" in ms, "m-orchestrator SKILL.md documents cache_write")
    # stale in the state enum of both docs.
    check("failed|info|stale" in md, "monitoring.md state enum includes stale")
    check("done|failed|info|stale" in ms, "m-orchestrator SKILL.md state enum includes stale")
    # files_changed added to the shape-key list in both docs.
    check("fixed_ids`/`files_changed`" in md, "monitoring.md shape list includes files_changed")
    check("fixed_ids`/`files_changed`" in ms, "m-orchestrator SKILL.md shape list includes files_changed")
    # agent sub-object documented in both docs.
    check('"agent"' in md and '"runtime"' in md, "monitoring.md documents the agent sub-object")
    check('"agent"' in ms and '"runtime"' in ms, "m-orchestrator SKILL.md documents the agent sub-object")
    # config-driven caps noted (D4/#11) in monitoring.md and implement-plan SKILL.md.
    check("max_gate_attempts" in md, "monitoring.md notes config attempt caps")
    check("max_gate_attempts" in ds, "implement-plan SKILL.md notes config attempt caps")
    # M2: the config is re-read while the watcher runs (it starts before the
    # orchestrator script that publishes the caps).
    check("re-reads this file" in md,
          "monitoring.md says the watcher re-reads orch-config.json while running")
    # m4 / R-13: the agent sub-object's real key set, and what `id` means now.
    for key in ('"shortId"', '"identity"', '"flags"', '"unconventional"'):
        check(key in md, f"monitoring.md documents the agent block key {key}")
    check("full 17-hex agentId" in md and "shortId`" in md,
          "monitoring.md pins identity to the full agentId, shortId as display only")
    check("legacy:<task>:<id8>" in md,
          "monitoring.md records the 8-hex legacy id consequence for readers")
    # R-40 lifecycle: what stops a watcher, what does not, and the escape hatch.
    check("stop its watcher" in md, "monitoring.md states the run-close/stop rule")
    check("ORCH_EXIT_QUIET_SECS" in md and "ORCH_ABANDON_QUIET_SECS" in md
          and "ORCH_NO_SELF_EXIT" in md,
          "monitoring.md documents both exit windows and the opt-out")
    check('`"w":"agent"`' in md or '(`"w":"agent"`)' in md,
          "monitoring.md says only a script-written close authorizes the exit")
    check("watcher.pid" in md and "echo $!" in md,
          "monitoring.md documents the watcher.pid launch-side half")
    # R-08/GD-10: the doc must not still promise the retired sequenced close.
    check("sequenced close is **retired" in md and "closed, no verdict" in md,
          "monitoring.md records the retired sequenced close + no-verdict close")
    check("serial advance ->" in md,
          "monitoring.md documents the legacy sequenced close's own detail text")
    # n-2: with both templates publishing `parallel`/`sequential`, the watcher's
    # STRATEGY=="serial" branch has no live producer. Say so, or a future reader
    # "fixes" a template to emit `serial` and resurrects R-58.
    check("no reference template publishes it" in md,
          "monitoring.md marks the legacy `serial` branch as legacy-config-only")
    # M-1: what may and may not cancel the driver's close — the doc described the
    # intended behavior, not the shipped one, for four attempts.
    check("plan card MOVING" in md and "does not" in md,
          "monitoring.md states that a plan card CLOSING is not liveness")
    # M-2: signalling the watcher is safe because it drains first.
    check("ORCH_DRAIN_SECS" in md and "DRAIN" in md,
          "monitoring.md documents the shutdown drain and its window")
    # M1/M14 (WRITE-SIDE-12/-13): the token-tick cadence knob, under BOTH
    # spellings — the env var that PINS it and the orch-config key the watcher
    # re-reads live. An operator who finds only one of them cannot tune a run.
    check("ORCH_TOKEN_TICK_SECS" in md,
          "monitoring.md documents the ORCH_TOKEN_TICK_SECS env knob")
    check("token_tick_secs" in md,
          "monitoring.md documents the token_tick_secs orch-config key")
    # The cadence is a CEILING. A future reader who mistakes it for a heartbeat
    # interval erases every stall segment the timeplan derives from gaps.
    check("ceiling, never a floor" in md,
          "monitoring.md states the cadence is a ceiling, not a heartbeat")
    # M14 (DATA-MODEL-11): the token asymmetry. `tokens` is a delta and
    # `agent.tokens` is absolute; the doc used to say so for only one of them,
    # and a reader who assumes symmetry is wrong in one direction or the other.
    check("**delta** (not absolute)" in md,
          "monitoring.md still says the top-level tokens value is a delta")
    check("ABSOLUTE running total, last-event-wins" in md,
          "monitoring.md says agent.tokens is the absolute running total")
    # M14 (WS-PROTOCOL-11): the reserved control key and the v1/v2 framing. The
    # file schema is normative and unchanged; `m` is the one name spent so that
    # additive EVENT keys stay legal.
    check("Wire framing" in md,
          "monitoring.md has a wire-framing section for the /ws protocol")
    check("Reserved control key `m`" in md and "Events never carry `m`" in md,
          "monitoring.md reserves the control key `m` and bars it from events")
    check("additive event keys remain legal" in md,
          "monitoring.md keeps additive event keys legal beside the reserved key")
    check("server-declared, never sniffed" in md,
          "monitoring.md pins version negotiation to the server's first frame")
    # The refusal is quoted by the DISCRIMINATOR the server really sends, not
    # by an abbreviated frame literal a reader could turn into a wrong assert:
    # the shipped hello also carries v/task/foldGen (`_stream_v2`'s
    # unknown-task branch).
    check('"error":"unknown-task"' in md,
          "monitoring.md documents the v2 unknown-task refusal")
    # Its ENVELOPE is pinned by names in proximity, not by the 62 characters of
    # contiguous pseudo-JSON the doc happens to print today: the literal sits
    # inside a hand-wrapped numbered list, and re-wrapping it — or spelling the
    # placeholder `"task":"<name>"` instead of `"task":…` — must not fail a test
    # whose subject is still true. What matters is that the refusal is shown as
    # a `hello` carrying the fold generation, not as a bare error string.
    check(re.search(r'"m":"hello".{0,60}"error":"unknown-task"', md, re.S)
          and '"foldGen"' in md,
          "monitoring.md prints the unknown-task refusal as a hello with foldGen")
    # Whitespace-normalised: the sentence is prose and may re-wrap; what must
    # not change is that the catalogue is closed at these four names TODAY
    # while unknown `m` values stay ignorable — a reader that treats an
    # unrecognised control frame as an event breaks on the next added shape.
    check(re.search(r"control catalogue is exactly four shapes — `hello`,\s+"
                    r"`snapshot`,\s+`tail`,\s+`cursor`", md),
          "monitoring.md enumerates the four v2 control frames")
    check(re.search(r"\*\*ignore\*\* any other\s+`m` value rather than treat "
                    r"it as an event", md),
          "monitoring.md keeps the `m` space forward-compatible for readers")
    # The resume rule: content sig + BYTE offset, and a wipe-and-rerun is
    # refused rather than silently tailing a foreign stream at a stale offset.
    # Pinned by the two JSON NAMES, not by a pseudo-JSON spelling: the doc
    # prints proper `"key": value` literals, and an assert keyed on the exact
    # punctuation would break on any later reflow of the same true claim.
    check("sig-mismatch" in md and "fromApplied" in md,
          "monitoring.md ties wipe-and-rerun to the sig-based resume refusal")
    check(re.search(r"`offset` is a\s+\*\*byte\*\* offset — never a line number",
                    md),
          "monitoring.md pins the cursor offset to bytes, not line numbers")
    # A digest over less than SIG_BYTES is not an identity yet (`_scan`'s
    # `sig_short` / `sig_is_identity` pair) — a doc that omits this teaches a
    # reader to trust a young stream's sig across an append.
    check("sig_short" in md,
          "monitoring.md notes the short-head sig caveat")
    # WS-PROTOCOL-14: the tail poll is no longer a fixed 0.5 s. The doc must
    # not pin a constant the server contradicts after 60 s of quiet.
    check(re.search(r"0\.5 s while the stream is moving and back off to\s+"
                    r"2 s after ~60 s of quiet", md),
          "monitoring.md documents the idle poll backoff, not a fixed 0.5 s")
    # The v1/v2 switch is `params.get("v") == "2"`, so `v=1`, `v=3` and
    # `v=banana` all take the v1 path. "no `v` in the query" read as if only the
    # absent parameter did, leaving a client that pins `v=1` — the natural
    # reading of "protocol v1" — undocumented.
    check(re.search(r"\*\*v1 — anything but `v=2` in the query", md),
          "monitoring.md scopes v1 to anything but the exact string v=2")
    # There is NO truncation frame: `read_events`' `-1` is server-internal, it
    # trips `_reset`, which sets `sub.closed`; the socket then gets the same
    # bare CLOSE any teardown sends. A client implementer must not go hunting
    # for a sentinel frame that never travels.
    check(re.search(r"no sentinel\s+\*frame\* on the wire", md)
          and "server-internal" in md,
          "monitoring.md does not invent a truncation sentinel frame")
    # `snap` grammar: `1` is legal AND the default, and an unrecognised value is
    # coerced to `1` and named in `ignored` — the hello paragraph promises every
    # unhonoured parameter is named, so the one that can be silently rewritten
    # has to say so.
    check("[&snap=0|1|verify]" in md,
          "monitoring.md's v2 grammar admits the default snap=1")
    check(re.search(r"an unrecognised `snap` value falls back to\s+`1` and is "
                    r"listed in the hello's `ignored`", md),
          "monitoring.md states the snap coercion and its disclosure")
    # An accepted resume is NOT "no frames": the gap between the client's
    # cursor and the server's offset still travels as ordinary array frames
    # (`_stream_v2`'s `if from_applied:` branch sets `replay_from`). The old
    # wording said "neither".
    check(re.search(r"An accepted resume sends no snapshot; if\s+the client's "
                    r"cursor is behind the server's offset, the gap travels "
                    r"first as\s+ordinary array frames", md),
          "monitoring.md states that an accepted resume still ships the gap")
    # The cursor follows every tick that CONSUMED events, poison included
    # (`_tail_loop_client`'s `if sent and v2:` guard) — a reader keyed on
    # "n > 0" resumes stale.
    check(re.search(r"after every tick that \*\*consumed\*\* events", md)
          and "`n: 0`" in md,
          "monitoring.md ties the cursor frame to consumed, not delivered, events")
    # `0` disables the ceiling; it does not turn the watcher into a heartbeat.
    check(re.search(r"a line on every poll\s+tick that has a non-zero delta",
                    md),
          "monitoring.md keeps `0` behind the non-zero-delta guard")
    # A rejected resume gets the prelude ITS MODE calls for — under `snap=0`
    # that is raw history, no snapshot at all (`_stream_v2`'s else-branch sets
    # `replay_from = 0`). "Always a snapshot" is wrong in the one mode an
    # operator reaches for when a resume is misbehaving.
    check(re.search(r"answered with the full\s+prelude the mode calls for", md),
          "monitoring.md does not promise a snapshot for every rejected resume")
    # M14/M1 cross-section consistency: the TIMEPLAN's cadence rationale and
    # the Token-math knob description must quote the SAME ceiling. They sit
    # ~40 lines apart and the old "every few seconds" line survived one edit
    # pass while its sibling was fixed — the two readings cannot both be true,
    # and an operator holding the stale one mis-reads short gaps as outages.
    check(re.search(r"Token ticks land at most once per\s+agent per "
                    r"`token_tick_secs` \(default 15 s\)", md),
          "monitoring.md's timeplan section quotes the real tick ceiling")
    # The Token-math half needs its OWN verbatim pin: a count alone cannot
    # detect the half-applied fix it is supposed to guard (the token occurs in
    # the orch-config row and the quiet bullet too, so a `>= 2` survives
    # deleting BOTH sections this assert names).
    check(re.search(r"Live ticks are throttled per agent\s+"
                    r"\(`token_tick_secs`, default 15 s\)", md),
          "monitoring.md's Token-math section quotes the same tick ceiling")
    check(md.count("`token_tick_secs`") >= 3,
          "monitoring.md names token_tick_secs in the config row, the timeplan "
          "and token math")
    # The THIRD statement of the same ceiling: the Timestamps bullet. It used to
    # promise "live events lag ≤1 s", which the shipped cadence misses by 15× —
    # the ceiling gates the transcript READ (`token_tick_due` sits before
    # `agent_tokens`) and the tick is emitted with no `ts`, so `emit()` stamps
    # the observation moment. 91 % of a measured stream is `stage=="tokens"`,
    # so this is the common case, not an edge one.
    check("live events lag ≤1 s" not in md,
          "monitoring.md no longer promises a ≤1 s stamp for every live event")
    check(re.search(r"a token tick up\s+to `token_tick_secs` \(default 15 s\) "
                    r"after the transcript growth", md),
          "monitoring.md's Timestamps bullet dates a token tick by the ceiling")
    # …and the socket poll is attributed to the SOCKET. `POLL_SECS`/
    # `IDLE_POLL_SECS` are monitor_server.py constants: they change when the
    # page is told, never a timestamp already on disk (the watcher's own
    # journal poll is `poll_sleep(seconds=1.0)`, deliberately left at 1 s).
    check(re.search(r"Delivery to the page adds the socket poll", md)
          and re.search(r"cannot move a stamp already\s+written to disk", md),
          "monitoring.md separates delivery latency from the watcher's stamp")
    # An accepted resume skips the SNAPSHOT, not "the prelude": `hello` and the
    # ONE `{"m":"tail",…}` boundary are written unconditionally (`_stream_v2`).
    # The Behavior-notes summary is what most readers actually read, so it must
    # not restate the error the normative section above it already corrects.
    check(re.search(r"carries a valid cursor skips the \*\*snapshot\*\*", md)
          and "boundary frame, then resumes the tail" in md,
          "monitoring.md's reconnect summary skips the snapshot, not the prelude")
    # The LAST "full replay on connect" claim in the file (the completed-tasks
    # bullet) describes the same act as the connect bullet 60 lines above it,
    # which v2 turned into a hydration. Both or neither — a survivor here is the
    # same half-applied-fix shape as the timeplan/token-math pair above.
    check(re.search(r"hydrates \(or, on a\s+v1 socket or under `\?snap=0`, "
                    r"replays\) the full event history on connect", md),
          "monitoring.md's never-delete bullet describes hydration, not replay")
    check(re.search(r"replays the full\s+event history on connect", md) is None,
          "no unconditional full-replay-on-connect claim survives in monitoring.md")


# --- R-39: every status.sh line is attributed to its writer
def test_status_writer_attribution():
    print("test_status_writer_attribution")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        proc = run_status(state_dir, ["p", "s", "running", "hi"])
        check(proc.returncode == 0, "status.sh exits 0")
        obj = json.loads((Path(state_dir) / "events.jsonl").read_text().splitlines()[0])
        check(obj.get("w") == "agent", 'agent-written line carries "w":"agent"')
        # Additive: the five-key core shape is unchanged.
        check(all(k in obj for k in ("ts", "plan", "stage", "state", "detail")),
              "five-key core event shape preserved alongside w")
        # ORCH_TITLE still rides along (no key was displaced).
        proc = run_status(state_dir, ["p", "plan", "queued", "seeded"],
                          extra_env={"ORCH_TITLE": "Phase 1"})
        titled = json.loads((Path(state_dir) / "events.jsonl").read_text().splitlines()[1])
        check(titled.get("title") == "Phase 1" and titled.get("w") == "agent",
              "title and w coexist")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- R-10 / GD-11: detail capped at 1 KB at the writer
def test_status_detail_cap():
    print("test_status_detail_cap")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        run_status(state_dir, ["p", "s", "info", "z" * 9000])
        obj = json.loads((Path(state_dir) / "events.jsonl").read_text().splitlines()[0])
        check(len(obj["detail"]) == 1024, f"detail capped at 1 KB (got {len(obj['detail'])})")
        check(obj["detail"].endswith("..."), "truncation is visible in the detail")
        run_status(state_dir, ["p", "s", "info", "short"])
        obj2 = json.loads((Path(state_dir) / "events.jsonl").read_text().splitlines()[1])
        check(obj2["detail"] == "short", "a short detail is untouched")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- R-10: out-of-enum state warns but still appends (best-effort writer)
def test_status_bad_state_warns_but_writes():
    print("test_status_bad_state_warns_but_writes")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        proc = run_status(state_dir, ["p", "s", "exploded", "odd"])
        check(proc.returncode == 0, "unknown state does not fail the caller")
        check("unknown state" in proc.stderr, "unknown state warns on stderr")
        obj = json.loads((Path(state_dir) / "events.jsonl").read_text().splitlines()[0])
        check(obj["state"] == "exploded", "the event is still appended verbatim")
        for good in ("queued", "running", "done", "failed", "info", "stale"):
            p = run_status(state_dir, ["p", "s", good, "x"])
            check("unknown state" not in p.stderr, f"'{good}' is in the enum (no warning)")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- R-10: concurrent appends lose/tear no line (smoke test, NOT the lock guard)
def test_status_concurrent_appends_are_atomic():
    print("test_status_concurrent_appends_are_atomic")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        os.makedirs(state_dir)
        env = {k: v for k, v in os.environ.items() if k != "ORCH_TITLE"}
        env["ORCH_STATE_DIR"] = state_dir
        writers = 24
        # NOTE (M-3): this arm does NOT prove the lock. GD-11's writer-side cap
        # truncates every detail to 1 KB BEFORE the write, so 9000 chars in means
        # a ~1.1 KB line out — comfortably inside one atomic append, and this
        # scenario passes verbatim with fcntl.flock deleted from status.sh
        # (measured). R-10's stated ">8 KiB per writer" acceptance test is
        # unsatisfiable once the cap exists. What this arm does prove is that 24
        # concurrent writers lose no line, duplicate none and leave no torn tail;
        # the lock itself is guarded behaviorally by
        # test_status_append_waits_for_the_lock and at the source by
        # test_append_sites_take_lock_ex.
        procs = [subprocess.Popen(
            ["bash", str(STATUS_SH), f"plan{i}", "stage", "running",
             f"{i}-" + "d" * 9000],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for i in range(writers)]
        for p in procs:
            p.wait()
        raw = (Path(state_dir) / "events.jsonl").read_text()
        lines = raw.splitlines()
        check(len(lines) == writers, f"every writer's line survived ({len(lines)}/{writers})")
        check(raw.endswith("\n"), "file ends on a line boundary (no torn tail)")
        plans = set()
        torn = 0
        for ln in lines:
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                torn += 1
                continue
            plans.add(ev["plan"])
            if len(ev["detail"]) > 1024:
                torn += 1
        check(torn == 0, "zero torn/unparseable lines")
        check(len(plans) == writers, f"all {writers} distinct writers present")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- R-10 (M-3): the append really takes LOCK_EX — a contended writer WAITS
def test_status_append_waits_for_the_lock():
    print("test_status_append_waits_for_the_lock")
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        os.makedirs(state_dir)
        events = Path(state_dir) / "events.jsonl"
        events.write_text("")
        env = {k: v for k, v in os.environ.items() if k != "ORCH_TITLE"}
        env["ORCH_STATE_DIR"] = state_dir
        # Hold LOCK_EX on the events file from THIS process, then start one
        # status.sh. If the writer takes the lock it must block; if the lock were
        # removed it would append immediately — which is exactly the difference
        # the 24-writer arm above cannot see.
        with open(events, "a") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            proc = subprocess.Popen(
                ["bash", str(STATUS_SH), "locked", "stage", "running", "held"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                proc.wait(timeout=1.5)
                blocked = False
            except subprocess.TimeoutExpired:
                blocked = True
            check(blocked, "a status.sh append BLOCKS while the events lock is held")
            check(events.read_text() == "",
                  "nothing was appended behind the held lock")
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        # Released: the writer must now complete on its own and land its line.
        try:
            proc.wait(timeout=10)
            completed = True
        except subprocess.TimeoutExpired:  # pragma: no cover - would hang the suite
            proc.kill()
            completed = False
        check(completed and proc.returncode == 0,
              "the writer completes once the lock is released")
        lines = [ln for ln in events.read_text().splitlines() if ln.strip()]
        check(len(lines) == 1 and json.loads(lines[0])["plan"] == "locked",
              "the queued line lands exactly once, after the release")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- R-10 (M-3): both append sites take LOCK_EX, and both tolerate no fcntl (m-4)
def test_append_sites_take_lock_ex():
    print("test_append_sites_take_lock_ex")
    status_src = STATUS_SH.read_text()
    watcher_src = WATCHER_PY.read_text()
    for name, src in (("status.sh", status_src), ("decision_watcher.py", watcher_src)):
        check("fcntl.flock" in src and "LOCK_EX" in src,
              f"{name}: the append takes an exclusive flock")
        check("LOCK_UN" in src, f"{name}: and releases it")
        # m-4: a best-effort writer must not be killed by a missing fcntl. Both
        # writers degrade to an unlocked append instead of failing every call.
        check(bool(re.search(r"try:[^\n]*\n(?:\s*#[^\n]*\n)*\s*import fcntl\b", src))
              and "except ImportError" in src,
              f"{name}: fcntl is imported defensively, not hard-required")
        check(re.search(r"if fcntl is not None:\s*\n\s*fcntl\.flock", src),
              f"{name}: the lock is skipped (not fatal) when fcntl is absent")


# --- M1: the argv pattern both templates use is injection-proof AND lossless
def test_status_argv_call_is_injection_safe():
    print("test_status_argv_call_is_injection_safe")
    if shutil.which("node") is None:
        print("  skip: node not available")
        return
    base = tempfile.mkdtemp(dir=TMP_ROOT)
    try:
        state_dir = os.path.join(base, "state")
        sentinel = os.path.join(base, "PWNED")
        # A sub-plan id shaped like divider-agent output that breaks OUT of the
        # double quotes a shell-string command would have wrapped it in.
        hostile = f'sp-a" ; touch {sentinel} ; echo "'
        script = os.path.join(base, "run.js")
        # Verbatim the templates' runStatus call shape: argv + env, no shell, and
        # the child's stderr captured (status.sh warns there and still exits 0, so
        # a discarded stream is the one way the call fails silently — n1).
        Path(script).write_text(
            "const cp = require('node:child_process')\n"
            "const [S, TASK, plan, state] = process.argv.slice(2)\n"
            "const r = cp.spawnSync('bash', [S, String(plan), 'implement',"
            " String(state), 'attempt 1: go'],"
            " { env: { ...process.env, ORCH_STATE_DIR: TASK }, encoding: 'utf8' })\n"
            "if (r.error) { throw r.error }\n"
            "process.stdout.write('STDERR:' + (r.stderr || '').trim())\n")
        proc = subprocess.run(
            ["node", script, str(STATUS_SH), state_dir, hostile, "running"],
            capture_output=True, text=True)
        check(proc.returncode == 0,
              f"argv status call exits 0 on a hostile plan id ({proc.stderr.strip()[:80]})")
        check(not os.path.exists(sentinel),
              "no command substitution executed (no PWNED file)")
        obj = json.loads((Path(state_dir) / "events.jsonl").read_text().splitlines()[0])
        check(obj["plan"] == hostile, "the hostile plan id lands verbatim, unmangled")
        check(obj["stage"] == "implement" and obj["state"] == "running",
              "the quote never split the arg list (stage/state intact)")
        check(proc.stdout.strip() == "STDERR:",
              "a good call produces no warning to log")
        # n1: the same shape surfaces status.sh's stderr warning instead of
        # swallowing it (an out-of-enum state warns and still appends).
        warned = subprocess.run(
            ["node", script, str(STATUS_SH), state_dir, "sp-ok", "exploded"],
            capture_output=True, text=True)
        check("unknown state" in warned.stdout,
              "the caller can see (and log) status.sh's stderr warning")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- R-09 / R-58: both templates emit the terminal plan + run events themselves
def test_templates_emit_terminal_events():
    print("test_templates_emit_terminal_events")
    for path in (TEMPLATE, RESEARCH_TEMPLATE):
        src = path.read_text()
        name = path.name
        # Script-emitted, not agent-emitted: the events must come from a call the
        # script makes, at a fixed control-flow point.
        check("const runStatus" in src, f"{name}: has a script-side status emitter")
        # The run-close state must be a VARIABLE, never a literal: a hardcoded
        # 'done' painted a thrown run green on the home grid (the mirror image of
        # the fabricated `failed` badge R-58 exists to kill).
        check(re.search(r"runStatus\(\s*'orchestrator',\s*'complete',\s*state\b", src),
              f"{name}: the run-close event carries the run's real state")
        check(not re.search(r"runStatus\(\s*'orchestrator',\s*'complete',\s*'", src),
              f"{name}: no hardcoded orchestrator-complete state")
        # Both arms of the contract, per template: same closeRun arity in both, a
        # 'done' close on the success path, 'failed' on every throw path.
        check(re.search(r"const closeRun = async \(state, summary\)", src),
              f"{name}: closeRun(state, summary) — same shape in both templates")
        check("closeRun('failed'" in src,
              f"{name}: the throw path closes the run FAILED")
        check("closeRun('done'" in src
              or re.search(r"closeRun\(\w+ \? 'done' : 'failed'", src),
              f"{name}: the success path closes the run done")
        check(re.search(r"runStatus\([^)]*'plan',\s*'done'", src),
              f"{name}: emits a terminal `plan done`")
        # R-40: the epilogue must never name-match kill other tasks' daemons, and
        # must never stop the SHARED monitor server (one server serves all tasks).
        check("pkill" not in src, f"{name}: no pkill in the epilogue (wrong-target kill)")
        check("watcher.pid" in src, f"{name}: the watcher is stopped by recorded pid")
        check("monitor.pid" not in src,
              f"{name}: the shared monitor server is never killed per task")
        # m3: a RECORDED pid is not enough — a stale pid file is the same
        # wrong-target hazard as a name-matched kill, so the target is verified
        # before the signal and the kill is skipped when it cannot be verified.
        check("/proc/${pid}/cmdline" in src,
              f"{name}: the pid is verified against /proc before signalling")
        check(re.search(r"includes\('decision_watcher'\)", src),
              f"{name}: only a real decision_watcher is signalled")
        check(src.index("cmdline") < src.index("process.kill"),
              f"{name}: verification happens BEFORE process.kill")
        # M1: status events are executed as argv, never as a shell string — the
        # plan id / detail can be agent-authored (divider output, file paths).
        check("['-c'" not in src and '["-c"' not in src,
              f"{name}: no `bash -c` execution of a status command")
        check(re.search(r"(spawnSync|execFileSync)\('bash', \[S,", src),
              f"{name}: status.sh is invoked with argv, not a shell string")
        check("ORCH_STATE_DIR: TASK" in src,
              f"{name}: the state dir travels in the child env, not in a shell string")
        # n1: status.sh warns on stderr and still exits 0 — the warning must be
        # captured and logged, never discarded with stdio:'ignore'.
        check("stdio: 'ignore'" not in src,
              f"{name}: the status call does not discard status.sh's stderr")
        check("r.stderr" in src and "log(" in src,
              f"{name}: a status.sh warning is logged, not swallowed")
        # plans_total: each template declares the run's expected plan-card
        # count at a fixed control-flow point (env, never argv), so dashboards
        # can render progress over ALL plans, unstarted ones included.
        check("ORCH_PLANS_TOTAL" in src,
              f"{name}: declares the run's plan-card total via ORCH_PLANS_TOTAL")
        check(re.search(r"\.\.\.\(extraEnv \|\| \{\}\)", src),
              f"{name}: extra status env rides the child env, not the argv contract")
        # R-09: caps/strategy published to orch-config.json so the watcher quotes
        # the real numbers. The watcher re-reads the file while running, so the
        # comment must not claim (nor rely on) publishing before daemon start.
        check("orch-config.json" in src, f"{name}: publishes orch-config.json")
        check("strategy" in src, f"{name}: publishes the strategy key")
        # m1/GD-10: `serial` is the LEGACY opt-in that re-enables the retired
        # sequenced plan-close heuristic. No template may stamp a new run with it.
        check(not re.search(r"strategy:\s*'serial'", src)
              and "'parallel' : 'serial'" not in src,
              f"{name}: never publishes the legacy strategy 'serial'")
        # M2: the comments must credit the real R-58 fix (the watcher's close
        # predicate), not the config write — a maintainer who believes the old
        # claim could "simplify" the predicate and resurrect the defect.
        check("close_state_for" in src,
              f"{name}: names close_state_for() as what prevents the fabricated badge")
        check("what stops a fabricated" not in src,
              f"{name}: no longer claims the strategy key is what fixes R-58")

    impl = TEMPLATE.read_text()
    check("max_plan_attempts: MAX_ATTEMPTS" in impl,
          "implement template publishes MAX_ATTEMPTS as max_plan_attempts")
    check("max_finalgate_attempts: FINALGATE_ATTEMPTS" in impl,
          "implement template publishes the final-gate cap")
    check(re.search(r"runStatus\(sp\.id,\s*'plan',\s*'failed',\s*`attempts exhausted", impl),
          "implement template emits `plan failed \"attempts exhausted N/N\"`")
    check("FINALGATE_ATTEMPTS; fga++" in impl,
          "the final-gate loop bound is the published cap, not a literal")

    research = RESEARCH_TEMPLATE.read_text()
    check(re.search(r"runStatus\('research',\s*'plan',\s*'done'", research),
          "research template closes the research plan at the barrier")
    check(re.search(r"runStatus\('synthesis',\s*'plan',\s*'done'", research),
          "research template closes the synthesis plan from the script")
    check("statusCmd('synthesis', 'plan', 'done'" not in research,
          "the synthesis plan-done is no longer left to the agent's prompt")
    # n-4: with zero reports there is nothing to synthesize. Spawning synthesis
    # anyway produced a second failure while the log read as a normal run.
    zero_branch = research[research.index("no researcher returned"):
                           research.index("phase('Synthesize')")]
    check("closeRun('failed'" in zero_branch and "throw new Error" in zero_branch,
          "the zero-report branch closes the run and throws, never spawns synthesis")
    # M-2: the epilogue signals the watcher immediately, which is only safe
    # because the watcher drains on SIGTERM. Say so where the signal is written,
    # so neither side is "simplified" away in isolation.
    for path in (TEMPLATE, RESEARCH_TEMPLATE):
        src = path.read_text()
        check("DRAIN" in src or "drain" in src,
              f"{path.name}: the epilogue documents the watcher's SIGTERM drain")


# --- .gitignore entries (R-01 + R-42's Mongo additions, SD-3) + negatives
def test_gitignore():
    print("test_gitignore")
    gi = GITIGNORE.read_text()
    check(".claude/shared/monitoring/events.jsonl" in gi,
          ".gitignore ignores the module-dir events.jsonl")
    check(".claude/shared/monitoring/.watcher-state.json" in gi,
          ".gitignore ignores the module-dir .watcher-state.json")
    # SD-3: the verbatim entry list, asserted here and written by the bootstrap.
    for entry in (".touch/", ".touch*/", ".claude/settings.local.json", "*.pid",
                  ".claude/local-orchestrators/*/.watcher-state.json",
                  "mongo-data/", "mongo-dump/", "*.bson"):
        check(entry in gi, f".gitignore contains {entry}")
    # 2026-07-27 amendment: the whole per-task run-state tree is ignored and
    # untracked (kept on disk only). `git check-ignore` exits 0 when ignored.
    def ignored(rel):
        return subprocess.run(["git", "check-ignore", "-q", rel], cwd=REPO,
                              capture_output=True).returncode == 0
    if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=REPO,
                      capture_output=True).returncode != 0:
        print("  skip: not a git repo")
        return
    check(ignored(".claude/local-orchestrators/"),
          ".claude/local-orchestrators/ itself is ignored")
    check(ignored(".claude/local-orchestrators/touch-full-recon/events.jsonl"),
          "events.jsonl under .claude/local-orchestrators/ is ignored")
    check(ignored(".claude/local-orchestrators/touch-full-recon/.watcher-state.json"),
          "watcher checkpoints under local-orchestrators ARE ignored")
    check(ignored(".touch/x") and ignored("mongo-data/x") and ignored("dump.bson"),
          "Touch runtime state and Mongo dumps are ignored")


def main():
    for t in (test_status_creates_state_dir, test_status_injection_safe,
              test_status_unset_warns, test_status_writer_attribution,
              test_status_detail_cap, test_status_bad_state_warns_but_writes,
              test_status_concurrent_appends_are_atomic,
              test_status_append_waits_for_the_lock,
              test_append_sites_take_lock_ex,
              test_status_argv_call_is_injection_safe, test_status_plans_total,
              test_template_static, test_templates_emit_terminal_events,
              test_docs_static, test_gitignore):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all sp-shell tests passed")


if __name__ == "__main__":
    main()
