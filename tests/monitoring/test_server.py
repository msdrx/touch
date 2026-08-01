#!/usr/bin/env python3
"""Stdlib-only tests for monitor_server.py (run: python3 test_server.py).

No pytest, no omnigent imports. Uses an ephemeral throwaway state dir under
/tmp/claude-1000 and never touches the live monitor's events.jsonl. Asserts fail
loudly (AssertionError -> non-zero exit).
"""
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# The module under test is named through `tests/_roots.py` (GD-U1/GD-U6): this
# file lives in `tests/monitoring/`, the module it loads does not.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
sys.path.insert(0, os.path.dirname(HERE))
from _roots import MON                                  # noqa: E402

MODULE_PATH = os.path.join(str(MON), "monitor_server.py")

# Resolve STATE_DIR/PORT at import to a throwaway dir so nothing touches the
# live task folder. No server is started (main() is never called).
_TMP_BASE = os.environ.get("TMPDIR") or "/tmp/claude-1000"
os.makedirs(_TMP_BASE, exist_ok=True)
_STATE_DIR = tempfile.mkdtemp(prefix="srvtest-", dir=_TMP_BASE)
os.environ["ORCH_STATE_DIR"] = _STATE_DIR
os.environ.pop("ORCH_PORT", None)

# `--write-gold` regenerates the golden snapshot fixture (see
# test_snapshot_matches_the_golden_fixture). It is stripped from argv BEFORE
# the module is imported, because monitor_server reads argv[1] as its port at
# import time and would exit with "invalid port from argv".
_WRITE_GOLD = "--write-gold" in sys.argv
if _WRITE_GOLD:
    sys.argv = [a for a in sys.argv if a != "--write-gold"]


def _load_module():
    spec = importlib.util.spec_from_file_location("monitor_server_undertest", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ms = _load_module()


def test_read_frames_torn_line():
    """A torn trailing line is deferred, never dropped (SERVER-1 / D5)."""
    path = os.path.join(_STATE_DIR, "torn.jsonl")
    a = json.dumps({"ts": "a", "plan": "p1"})
    b = json.dumps({"ts": "b", "plan": "p2"})
    with open(path, "wb") as f:
        f.write((a + "\n" + b + "\n").encode())
        f.write(b'{"ts":"c","pla')  # partial, no newline
    frames, off = ms.read_frames(path, 0)
    assert frames == [a.encode(), b.encode()], frames
    # offset must stop before the partial line
    assert off == len((a + "\n" + b + "\n").encode()), off
    # complete the torn line + append another record
    c = json.dumps({"ts": "c", "plan": "p3"})
    d = json.dumps({"ts": "d", "plan": "p4"})
    with open(path, "wb") as f:
        f.write((a + "\n" + b + "\n" + c + "\n" + d + "\n").encode())
    frames2, off2 = ms.read_frames(path, off)
    # the middle event c must reappear intact, never lost
    assert frames2 == [c.encode(), d.encode()], frames2
    assert off2 == len((a + "\n" + b + "\n" + c + "\n" + d + "\n").encode()), off2


def test_read_frames_no_complete_line():
    """Only a partial line present -> no frames, offset unchanged."""
    path = os.path.join(_STATE_DIR, "partial.jsonl")
    with open(path, "wb") as f:
        f.write(b'{"ts":"x"')
    frames, off = ms.read_frames(path, 0)
    assert frames == [], frames
    assert off == 0, off


def test_read_frames_truncation_sentinel():
    """size < offset returns the -1 truncation sentinel (SERVER-6 / D10)."""
    path = os.path.join(_STATE_DIR, "trunc.jsonl")
    with open(path, "wb") as f:
        f.write(b'{"a":1}\n{"b":2}\n')
    frames, off = ms.read_frames(path, 999999)  # offset far past EOF
    assert frames == [], frames
    assert off == -1, off
    # a matching-size or growing file never signals truncation
    _, off2 = ms.read_frames(path, 0)
    assert off2 != -1, off2


def test_read_frames_missing_file():
    frames, off = ms.read_frames(os.path.join(_STATE_DIR, "nope.jsonl"), 5)
    assert frames == [] and off == 5, (frames, off)


def test_resolve_port_bad_argv():
    """Non-integer argv -> clean SystemExit, not a raw ValueError (SERVER-2)."""
    saved = sys.argv
    try:
        sys.argv = ["ms", "notaport"]
        raised = None
        try:
            ms.resolve_port()
        except SystemExit as e:
            raised = e
        except ValueError as e:  # pragma: no cover - would be the bug
            raise AssertionError(f"leaked ValueError instead of SystemExit: {e}")
        assert raised is not None, "expected SystemExit on bad argv port"
    finally:
        sys.argv = saved


def test_resolve_port_bad_env():
    """Non-integer ORCH_PORT -> clean SystemExit (SERVER-2)."""
    os.environ["ORCH_PORT"] = "abc"
    try:
        raised = None
        try:
            ms.resolve_port()
        except SystemExit as e:
            raised = e
        assert raised is not None, "expected SystemExit on bad ORCH_PORT"
    finally:
        os.environ.pop("ORCH_PORT", None)


def test_resolve_port_good():
    saved = sys.argv
    try:
        sys.argv = ["ms", "9999"]
        assert ms.resolve_port() == 9999
    finally:
        sys.argv = saved


def test_task_status_precedence_and_tokens():
    """Badge precedence + token sums include cache_write."""
    path = os.path.join(_STATE_DIR, "status.jsonl")
    lines = [
        {"ts": "1", "plan": "sp-a", "stage": "plan", "state": "running", "detail": "go"},
        {"ts": "2", "plan": "sp-a", "stage": "plan", "state": "done", "detail": "ok",
         "tokens": {"in": 10, "out": 5, "cached": 2, "cache_write": 3}},
        {"ts": "3", "plan": "sp-b", "stage": "plan", "state": "failed", "detail": "boom",
         "tokens": {"in": 1, "out": 1, "cached": 1, "cache_write": 1}},
    ]
    with open(path, "wb") as f:
        for ln in lines:
            f.write((json.dumps(ln) + "\n").encode())
    out = ms.task_status(path)
    # a failed plan wins while orchestrator card is open and nothing runs
    assert out["status"] == "failed", out
    assert out["tokens"] == {"in": 11, "out": 6, "cached": 3, "cache_write": 4}, out["tokens"]
    # ...but LIVE ACTIVITY WINS: a plan still running keeps the flow running
    # even after another plan exhausted its attempts and closed failed
    path2 = os.path.join(_STATE_DIR, "status-live.jsonl")
    lines2 = lines + [
        {"ts": "4", "plan": "sp-c", "stage": "plan", "state": "running", "detail": "loop on"},
    ]
    with open(path2, "wb") as f:
        for ln in lines2:
            f.write((json.dumps(ln) + "\n").encode())
    assert ms.task_status(path2)["status"] == "running", ms.task_status(path2)


def test_task_status_orchestrator_done_wins():
    path = os.path.join(_STATE_DIR, "status2.jsonl")
    lines = [
        {"ts": "1", "plan": "sp-a", "stage": "plan", "state": "failed", "detail": "x"},
        {"ts": "2", "plan": "orchestrator", "stage": "complete", "state": "done", "detail": "fin"},
    ]
    with open(path, "wb") as f:
        for ln in lines:
            f.write((json.dumps(ln) + "\n").encode())
    out = ms.task_status(path)
    assert out["status"] == "done", out  # orchestrator card is authoritative


def test_task_status_continuation_reopens_stale_close():
    """FRONTEND-6 (server half): one folder hosts several phases, so activity
    appended past an earlier phase's `orchestrator complete done` must flip the
    tile back to running — a sub-plan `plan running/queued` event (seed lines
    included) or a running-state orchestrator spawn chip both count; terminal
    sub-plan closes and token ticks do not."""
    path = os.path.join(_STATE_DIR, "status3.jsonl")
    lines = [
        {"ts": "1", "plan": "sp-a", "stage": "plan", "state": "done", "detail": "ok"},
        {"ts": "2", "plan": "orchestrator", "stage": "complete", "state": "done", "detail": "fin"},
        {"ts": "3", "plan": "sp-b", "stage": "plan", "state": "running", "detail": "phase 2"},
    ]
    with open(path, "wb") as f:
        for ln in lines:
            f.write((json.dumps(ln) + "\n").encode())
    out = ms.task_status(path)
    assert out["status"] == "running", out
    # a watcher spawn chip on the orchestrator card reopens it too
    path2 = os.path.join(_STATE_DIR, "status4.jsonl")
    lines2 = [
        {"ts": "1", "plan": "orchestrator", "stage": "complete", "state": "done", "detail": "fin"},
        {"ts": "2", "plan": "orchestrator", "stage": "sp-b", "state": "running",
         "detail": "spawn sp-b impl attempt 1"},
    ]
    with open(path2, "wb") as f:
        for ln in lines2:
            f.write((json.dumps(ln) + "\n").encode())
    assert ms.task_status(path2)["status"] == "running", ms.task_status(path2)
    # trailing terminal closes / token ticks after the run close do NOT reopen
    path3 = os.path.join(_STATE_DIR, "status5.jsonl")
    lines3 = [
        {"ts": "1", "plan": "sp-a", "stage": "plan", "state": "running", "detail": "go"},
        {"ts": "2", "plan": "orchestrator", "stage": "complete", "state": "done", "detail": "fin"},
        {"ts": "3", "plan": "sp-a", "stage": "plan", "state": "done", "detail": "late settle"},
        {"ts": "4", "plan": "orchestrator", "stage": "tokens", "state": "running",
         "detail": "late tick", "tokens": {"in": 1, "out": 1}},
    ]
    with open(path3, "wb") as f:
        for ln in lines3:
            f.write((json.dumps(ln) + "\n").encode())
    assert ms.task_status(path3)["status"] == "done", ms.task_status(path3)


def test_ws_frame_lengths():
    """Length-encoding sanity for the three size classes (guards CLOSE change)."""
    small = ms.ws_frame(b"x" * 10)
    assert small[0] == 0x81 and small[1] == 10, small[:2]
    assert small[2:] == b"x" * 10

    mid = ms.ws_frame(b"y" * 200)
    assert mid[0] == 0x81 and mid[1] == 126, mid[:2]
    assert int.from_bytes(mid[2:4], "big") == 200
    assert len(mid) == 4 + 200

    big = ms.ws_frame(b"z" * 70000)
    assert big[0] == 0x81 and big[1] == 127, big[:2]
    assert int.from_bytes(big[2:10], "big") == 70000
    assert len(big) == 10 + 70000

    # CLOSE frame opcode encodes in the header
    close = ms.ws_frame(b"", 0x8)
    assert close[0] == 0x88 and close[1] == 0, close[:2]


def test_parse_client_frames_close():
    """A 2-byte client CLOSE frame is detected (opcode 0x8) (SERVER-4)."""
    buf = bytearray([0x88, 0x00])  # FIN+CLOSE, unmasked, len 0
    assert ms.parse_client_frames(buf) is True
    assert len(buf) == 0, buf


def test_parse_client_frames_masked_pong_then_close():
    """A masked pong is skipped; a following CLOSE is still seen."""
    # masked pong: opcode 0xA, mask bit set, len 0, 4-byte key
    pong = bytearray([0x8A, 0x80, 0, 0, 0, 0])
    close = bytearray([0x88, 0x80, 0, 0, 0, 0])
    buf = pong + close
    assert ms.parse_client_frames(buf) is True
    assert len(buf) == 0, buf


def test_parse_client_frames_incomplete():
    """An incomplete frame body is left in the buffer for the next read."""
    # masked text frame declaring len 5 but only 3 body bytes present
    buf = bytearray([0x81, 0x85, 0, 0, 0, 0, ord("a"), ord("b"), ord("c")])
    before = bytes(buf)
    assert ms.parse_client_frames(buf) is False
    assert bytes(buf) == before, "incomplete frame must not be consumed"


def test_task_artifacts_listing():
    """Artifacts scan: .html/.md only, hidden skipped, reports sorted first."""
    d = tempfile.mkdtemp(prefix="arts-", dir=_TMP_BASE)
    os.makedirs(os.path.join(d, "findings"))
    os.makedirs(os.path.join(d, "report"))
    os.makedirs(os.path.join(d, ".hidden"))
    os.makedirs(os.path.join(d, "__pycache__"))
    for rel, data in (
        ("findings/sp-a-test-attempt-1.md", "# findings"),
        ("reviews-top.md", "# review"),
        ("report/final-report.html", "<h1>report</h1>"),
        ("events.jsonl", "{}"),                 # wrong ext -> excluded
        ("events.pre-fix.bak", "old"),          # wrong ext -> excluded
        (".hidden/secret.md", "no"),            # hidden dir -> excluded
        (".dotfile.md", "no"),                  # hidden file -> excluded
        ("__pycache__/x.md", "no"),             # pycache -> excluded
    ):
        path = os.path.join(d, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(data)
    arts = ms.task_artifacts(d)
    paths = [a["path"] for a in arts]
    assert paths == ["report/final-report.html", "findings/sp-a-test-attempt-1.md",
                     "reviews-top.md"], paths
    assert arts[0]["kind"] == "report" and arts[1]["kind"] == "note", arts
    assert all("size" in a and "mtime" in a for a in arts), arts


def test_safe_artifact_path_containment():
    """/file resolution: whitelist + realpath containment, no traversal."""
    d = tempfile.mkdtemp(prefix="safe-", dir=_TMP_BASE)
    os.makedirs(os.path.join(d, "findings"))
    good = os.path.join(d, "findings", "a.md")
    with open(good, "w") as f:
        f.write("x")
    outside = os.path.join(_TMP_BASE, "outside-artifact.md")
    with open(outside, "w") as f:
        f.write("secret")
    assert ms.safe_artifact_path(d, "findings/a.md") == os.path.realpath(good)
    assert ms.safe_artifact_path(d, "") is None
    assert ms.safe_artifact_path(d, "../outside-artifact.md") is None
    assert ms.safe_artifact_path(d, "findings/../../outside-artifact.md") is None
    assert ms.safe_artifact_path(d, outside) is None          # absolute path
    assert ms.safe_artifact_path(d, "events.jsonl") is None   # ext not whitelisted
    assert ms.safe_artifact_path(d, "findings/missing.md") is None
    assert ms.safe_artifact_path(d, "findings") is None       # dir, not file
    # a symlink pointing outside the task dir must not be served
    link = os.path.join(d, "findings", "leak.md")
    try:
        os.symlink(outside, link)
    except OSError:
        pass  # symlinks unavailable: containment still covered above
    else:
        assert ms.safe_artifact_path(d, "findings/leak.md") is None


def test_health_parse_failure_counter():
    """/health surfaces per-stream parse failures (R-10).

    A poisoned or torn line is skipped by the replay; without a counter the only
    symptom is a dashboard that silently disagrees with the file.
    """
    path = os.path.join(_STATE_DIR, "poisoned.jsonl")
    good = json.dumps({"ts": "1", "plan": "sp-a", "stage": "plan", "state": "done"})
    with open(path, "wb") as f:
        f.write((good + "\n").encode())
        f.write(b"{not json at all\n")
        f.write(b'"a bare string is not an event"\n')
        f.write((good + "\n").encode())
    base = ms.health_payload()["parse_failures_total"]
    out = ms.task_status(path)
    health = ms.health_payload()
    assert health["status"] == "ok", health
    # The counter is published under the path's DIGEST, never the path (F2 /
    # AUDIT-15 parity): /health is the one route with no token in front of it.
    assert health["parse_failures"].get(ms.path_digest(path)) == 2, health
    assert path not in health["parse_failures"], health
    assert health["parse_failures_total"] == base + 2, (base, health)
    # The good lines still render — a poisoned line degrades, never blocks.
    assert out["status"] == "done", out

    # A clean stream clears its entry instead of leaving a stale count behind.
    clean = os.path.join(_STATE_DIR, "clean.jsonl")
    with open(clean, "wb") as f:
        f.write((good + "\n").encode())
    ms.task_status(clean)
    assert ms.path_digest(clean) not in ms.health_payload()["parse_failures"], \
        ms.PARSE_FAILURES

    # m-2: so does a stream that DISAPPEARS (deleted or rotated after a poisoned
    # scan). The early return on stat failure used to skip the pop, so a gone
    # stream kept inflating parse_failures_total for the life of the server —
    # a permanently red probe with nothing left to fix.
    assert ms.PARSE_FAILURES.get(path) == 2, ms.PARSE_FAILURES
    os.remove(path)
    gone = ms.task_status(path)
    assert gone["status"] == "empty", gone
    assert ms.path_digest(path) not in ms.health_payload()["parse_failures"], \
        ms.PARSE_FAILURES
    assert ms.health_payload()["parse_failures_total"] == base, ms.PARSE_FAILURES
    ms.PARSE_FAILURES.pop(path, None)


def test_plan_states_last_event_wins():
    """SD-4/R-58: conflicting terminals resolve last-event-wins in FILE ORDER.

    A later corrective `plan done` beats an earlier fabricated `plan failed` for
    the same (plan, stage='plan') — and the earlier ts on the corrective line
    must not resurrect the failure, because order is file order, never ts sort.
    """
    path = os.path.join(_STATE_DIR, "conflict.jsonl")
    lines = [
        {"ts": "2026-07-25T18:44:09.000Z", "plan": "research", "stage": "plan",
         "state": "failed", "detail": "loop exited -> synthesis"},
        {"ts": "2026-07-25T18:00:00.000Z", "plan": "research", "stage": "plan",
         "state": "done", "detail": "all 5 researchers returned findings"},
    ]
    with open(path, "wb") as f:
        for ln in lines:
            f.write((json.dumps(ln) + "\n").encode())
    plan_states, last, tokens, failures = ms.replay_plan_states(path)
    assert plan_states["research"] == "done", plan_states
    assert failures == 0, failures
    assert ms.task_status(path)["status"] == "done", ms.task_status(path)
    # Same-state duplicates (RUNSTATE-7's dedup case) are a no-op, not a flip.
    dup = os.path.join(_STATE_DIR, "dup.jsonl")
    with open(dup, "wb") as f:
        for state in ("failed", "failed"):
            f.write((json.dumps({"ts": "1", "plan": "sp-a", "stage": "plan",
                                 "state": state}) + "\n").encode())
    assert ms.replay_plan_states(dup)[0]["sp-a"] == "failed"


# --- R-58 against the FROZEN REAL streams (skipped if the fixtures are absent:
#     the monitoring module must stay usable outside this repo).
#
# The corpus lives in the REPO (`<repo>/tests/fixtures/legacy`), never in the
# module, and the module ships without it. Resolve it by walking UP from this
# file rather than by a fixed count of `..` hops: this file sits at
# `<repo>/tests/monitoring/` here (GD-U6) and its ancestors differ in any other
# checkout, and a hop count that is right for one layout silently points at a
# stranger's directory in the other. When the walk finds nothing, every read
# skips with a printed reason and `run_all.sh` reports the count.
#
# The walk must not accept just any `tests/fixtures/legacy` it passes, though:
# installed under an unrelated project (or a home directory that happens to
# hold one), an unanchored walk would replay a STRANGER'S JSONL and assert
# Touch's R-58 invariants against it — a confusing red, or worse a coincidental
# green. So a candidate counts only when it carries the frozen corpus's OWN
# manifest (GD-P4) and that manifest names `legacy/` paths. That identifies the
# corpus by its own frozen bytes rather than by a repo layout the rename may
# move, and it also tells this module's `tests/fixtures/` (whose manifest lists
# only `snapshot-gold.json`) apart from the repo's.
SKIPS = []


def _skip(msg):
    print(f"  skip {msg}")
    SKIPS.append(msg)


def _is_repo_corpus(fixtures):
    """True when `fixtures` is the repo's frozen corpus, not a lookalike."""
    if not os.path.isdir(os.path.join(fixtures, "legacy")):
        return False
    manifest = os.path.join(fixtures, "MANIFEST.sha256")
    if not os.path.isfile(manifest):
        return False
    try:
        with open(manifest, encoding="utf-8", errors="replace") as fh:
            return any(" legacy/" in line or "\tlegacy/" in line for line in fh)
    except OSError:
        return False


def _find_repo_fixtures(start=None):
    """`<repo>/tests/fixtures/legacy` at or above `start`, or None."""
    d = HERE if start is None else os.path.abspath(start)
    while True:
        cand = os.path.join(d, "tests", "fixtures")
        if _is_repo_corpus(cand):
            return os.path.join(cand, "legacy")
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


_FIXTURES = _find_repo_fixtures()


def _fixture(name):
    """Path to a frozen legacy stream, or None (having printed why)."""
    if _FIXTURES is None:
        _skip(f"{name}: no <repo>/tests/fixtures/legacy above this module")
        return None
    path = os.path.join(_FIXTURES, name)
    if os.path.isfile(path):
        return path
    _skip(f"{name}: fixture absent")
    return None


def test_r58_real_streams_render_corrected():
    """The two corrected streams render `research` DONE, not the fabricated FAILED.

    These are verbatim bytes of this session's own runs: each holds the
    fabricated `plan failed "loop exited -> synthesis"` line AND the driver's
    later corrective `plan done`. Nothing rewrites them — the read rule does the
    work (SD-4).
    """
    for name in ("touch-full-recon-events.jsonl", "touch-mongo-live-events.jsonl"):
        path = _fixture(name)
        if not path:
            continue
        plan_states, last, tokens, failures = ms.replay_plan_states(path)
        assert plan_states.get("research") == "done", (name, plan_states)
        assert plan_states.get("synthesis") == "done", (name, plan_states)
        assert failures == 0, (name, failures)


def test_r58_uncorrected_failures_match_the_relabel_predicate():
    """Un-corrected fabricated failures stay `failed` here — and are exactly the
    lines the legacy re-labeler re-reads as "closed — no verdict".

    The forward fix stops NEW fabrications; the historic ones are re-labelled at
    read time by the legacy arm (GD-14/R-51, a different module). This test pins
    the handshake: every surviving `plan failed` in the affected streams carries
    the `loop exited ->` detail that the re-label predicate keys on.
    """
    for name in ("touch-aggregator-events.jsonl", "touch-mongo-live-events.jsonl"):
        path = _fixture(name)
        if not path:
            continue
        fabricated = []
        with open(path, "rb") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                ev = json.loads(raw)
                if ev.get("stage") == "plan" and ev.get("state") == "failed":
                    fabricated.append(ev)
        assert fabricated, f"{name}: expected the historic fabricated failures"
        for ev in fabricated:
            assert (ev.get("detail") or "").startswith("loop exited ->"), (name, ev)


def test_r58_genuine_failure_is_not_a_fabrication():
    """The user-killed run's `plan failed` lines must NOT match the re-label
    predicate — a real failure has to survive the fix (negative control)."""
    path = _fixture("touch-repo-recon-events.jsonl")
    if not path:
        return
    plan_states, _, _, failures = ms.replay_plan_states(path)
    assert plan_states.get("research") == "failed", plan_states
    with open(path, "rb") as f:
        details = [json.loads(r)["detail"] for r in f
                   if r.strip() and json.loads(r).get("stage") == "plan"
                   and json.loads(r).get("state") == "failed"]
    assert details, "expected the genuine failures"
    assert not any(d.startswith("loop exited ->") for d in details), details


def test_no_root_events_shortcircuit():
    """resolve_state_dir never resolves onto the module dir (SHELL-5 / D6 / CM-3).

    Two generations of this rule. The first said "no ROOT short-circuit": a
    stray ``ROOT/events.jsonl`` must not hijack auto-discovery. Item 04 took the
    remaining half — ROOT was still the FALLBACK when nothing else resolved, and
    in a packaged copy that is a write into a version-stamped cache. Now the
    daemon exits instead, so ``ROOT`` must not appear in the resolver at all.
    """
    import inspect
    src = inspect.getsource(ms.resolve_state_dir)
    assert "return ROOT" not in src, src
    assert "sys.exit(" in src, "an unresolvable state dir must exit, not fall back"
    for var in ("ORCH_STATE_DIR", "ORCH_TASKS_ROOT", "CLAUDE_PROJECT_DIR"):
        assert var in src, f"the exit message must name {var}"


# --------------------------------------------------------------------------
# Item 04 — one tasks-root resolver, duplicated verbatim in both daemons.
# --------------------------------------------------------------------------

WATCHER_PATH = os.path.join(str(MON), "decision_watcher.py")


def _function_source(path, name):
    """One top-level def's source text, read from the FILE (never imported).

    Reading the text is the point: importing decision_watcher would run its
    module body, and the two copies are pinned as *text*, so text is what this
    compares.
    """
    src = open(path, encoding="utf-8").read()
    start = src.index(f"\ndef {name}(") + 1
    end = src.index("\n\n\n", start)
    return src[start:end]


def test_the_two_daemons_ship_the_same_resolver_byte_for_byte():
    """CM-3: module independence forbids a shared import, so the copies are pinned.

    Same contract as FOLD_GEN's twin literal: one behaviour, two files, and a
    test that fails the moment they disagree by a single byte.
    """
    for name in ("resolve_tasks_root", "in_plugin_cache"):
        mine = _function_source(MODULE_PATH, name)
        theirs = _function_source(WATCHER_PATH, name)
        assert mine == theirs, f"{name}() has drifted between the two daemons"
        assert "ROOT" not in mine.replace("TASKS_ROOT", ""), \
            f"{name}() must be self-contained (no module-level ROOT reference)"


def _tasks_root(env, cwd):
    """Run the daemons' own resolver in a subprocess with a controlled env/cwd.

    Only the function is exec'd, never the module body (which would tail a
    journal); it is self-contained by the byte-equality test above. It no longer
    takes an ``as_file``: the ladder's fourth rung measured
    ``../../local-orchestrators`` from ``__file__`` and that rung is DELETED
    (G10), which `test_the_ladder_is_three_rungs` asserts as source text as well
    as behaviour.
    """
    import subprocess
    clean = {k: v for k, v in os.environ.items()
             if k not in ("ORCH_TASKS_ROOT", "CLAUDE_PROJECT_DIR", "ORCH_STATE_DIR")}
    # no .pyc droppings in the payload tree — the parent's
    # sys.dont_write_bytecode does not reach a child
    clean["PYTHONDONTWRITEBYTECODE"] = "1"
    clean.update(env)
    code = (
        "import os\n"
        f"src = open({WATCHER_PATH!r}, encoding='utf-8').read()\n"
        "body = src[src.index('\\ndef resolve_tasks_root('):"
        "src.index('\\ndef in_plugin_cache(')]\n"
        "ns = {'os': os}\n"
        "exec(body, ns)\n"
        "print(ns['resolve_tasks_root']())\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=clean, cwd=cwd,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _nearest_claude_marker(start):
    """The nearest ancestor of ``start`` holding a `.claude/`, or None.

    The resolver's third rung walks up looking for exactly this, so a test that
    wants "nothing resolves" has to know whether the temp tree it just built is
    actually isolated — under $TMPDIR that is an assumption, not a fact.
    """
    here = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(here, ".claude")):
            return os.path.join(here, ".claude")
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def test_tasks_root_resolution_order():
    """env > $CLAUDE_PROJECT_DIR > cwd walk-up, and `.touch/` is the state dir (G10).

    The MARKER dir and the STATE dir are deliberately different: the walk-up
    looks for `.claude/` (that is what marks a Claude Code project — `.touch/` is
    created by Touch and gitignored, so it cannot mark one) and then joins
    `.touch/local-orchestrators`. Every arm below asserts the pair.
    """
    import shutil
    base = tempfile.mkdtemp(prefix="tasksroot-", dir=_TMP_BASE)
    try:
        explicit = os.path.join(base, "explicit")
        project = os.path.join(base, "project")
        deep = os.path.join(project, "a", "b")
        os.makedirs(explicit)
        os.makedirs(os.path.join(project, ".claude"))
        os.makedirs(deep)
        want = os.path.join(project, ".touch", "local-orchestrators")
        # 1. $ORCH_TASKS_ROOT wins over everything, including the project.
        assert _tasks_root({"ORCH_TASKS_ROOT": explicit,
                            "CLAUDE_PROJECT_DIR": project}, deep) == explicit
        # 2. $CLAUDE_PROJECT_DIR beats the cwd walk-up (and does NOT need to
        #    exist: the anchor is the project, not a directory listing).
        assert _tasks_root({"CLAUDE_PROJECT_DIR": project}, deep) == want
        # 3. cwd walk-up finds the nearest .claude/ marker and joins .touch/.
        assert _tasks_root({}, deep) == want
        # ...and nothing was resolved under the MARKER dir. The old literal is
        # ASSEMBLED here (as `test_shell.py` assembles its own): sp-docs adds a
        # repo-wide grep backstop for it, and a test asserting its ABSENCE must
        # not be the one tracked file that carries it.
        legacy = ".claude/" + "local-orchestrators"
        assert legacy not in _tasks_root({}, deep)
        # 4. nothing at all: no env, no project, no marker above cwd -> "" (the
        #    caller exits 1; it never invents a root). The former FOURTH rung,
        #    a module-relative `../../local-orchestrators`, would have answered
        #    here — a sibling tree is planted to prove it no longer does.
        #    This arm only means what it says while NO ancestor of the throwaway
        #    tree holds a `.claude/`: one anywhere above $TMPDIR (this session's
        #    own scratchpad lives at /tmp/claude-1000/-home-laniakea-Projects-
        #    touch/…, one directory away from being exactly that) turns the cwd
        #    walk-up into a hit and flips "" to a real path. Assert the premise
        #    rather than assume it, and say so instead of failing on it.
        orphan = os.path.join(base, "orphan")
        os.makedirs(orphan)
        marker = _nearest_claude_marker(orphan)
        if marker:
            _skip(f"tasks-root arm 4: an ancestor of the temp tree holds {marker}")
            return
        os.makedirs(os.path.join(base, "pkg", "shared", "monitoring"))
        os.makedirs(os.path.join(base, "pkg", "local-orchestrators"))
        assert _tasks_root({}, orphan) == "", "an unresolvable root must be empty"
        assert os.listdir(os.path.join(base, "pkg", "local-orchestrators")) == [], \
            "the deleted module-relative rung must not have been consulted"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_the_ladder_is_three_rungs():
    """The module-relative fourth rung is GONE from the source (G10, LAYOUT-15).

    A packaged copy sits at `<plugin>/shared/monitoring`, so `../..` was the
    plugin root — resolving there would sweep sibling plugins looking for other
    people's task folders — and after GD-U1 there is nothing two levels above
    this directory to find anyway. Asserted as text as well as behaviour because
    the behavioural arm above depends on a marker-free temp tree and may skip.
    """
    src = _function_source(MODULE_PATH, "resolve_tasks_root")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith(("#", '"""', "*", "1.", "2.",
                                                   "3.")))
    assert "__file__" not in src, "the ladder no longer measures anything from __file__"
    assert "local-orchestrators" in src
    assert ".claude/" + "local-orchestrators" not in src, \
        "no rung joins the old .claude/ tasks root any more"
    assert src.count('".touch", "local-orchestrators"') == 2, \
        "rungs 2 and 3 both join .touch/local-orchestrators"
    assert 'return ""' in code, "the ladder must return '' when nothing resolves"


def test_the_bash_and_python_ladders_resolve_the_same_root():
    """status.sh and both daemons must agree, for the same env and cwd (I5).

    Three implementations of one decision (G10) — the bash resolver in
    `status.sh`, and the byte-pinned Python one in both daemons — so the pin
    between the two Python copies is not enough: this arm compares the bash
    answer with the Python answer directly, which is the only check that would
    catch `status.sh` being flipped to `.touch/` while a daemon was not (or the
    reverse).

    Rung 1 (`$ORCH_TASKS_ROOT`) is compared for ABSOLUTE values in the loop
    below, which is what every writer in this repo exports. A RELATIVE value is
    the one shape where the two spellings differ on the surface — bash echoes it
    verbatim, the Python resolver returns `os.path.abspath` of it — so the last
    arm asserts the invariant that actually holds there: read against the SAME
    cwd, the two answers are the same directory. Asserted rather than left
    implicit, so a future reader does not mistake the asymmetry for drift.
    """
    import shutil
    import subprocess
    status_sh = os.path.join(str(MON), "status.sh")
    base = tempfile.mkdtemp(prefix="ladderpair-", dir=_TMP_BASE)
    try:
        project = os.path.join(base, "project")
        deep = os.path.join(project, "a", "b")
        os.makedirs(os.path.join(project, ".claude"))
        os.makedirs(deep)
        src = open(status_sh, encoding="utf-8").read()
        body = src[src.index("resolve_tasks_root() {"):
                   src.index('if [ -n "${ORCH_STATE_DIR:-}" ]')]
        for env, cwd in (({"CLAUDE_PROJECT_DIR": project}, deep),   # rung 2
                         ({}, deep),                                # rung 3
                         ({"ORCH_TASKS_ROOT": os.path.join(base, "x")}, deep)):
            clean = {k: v for k, v in os.environ.items()
                     if k not in ("ORCH_TASKS_ROOT", "CLAUDE_PROJECT_DIR",
                                  "ORCH_STATE_DIR")}
            clean.update(env)
            proc = subprocess.run(
                ["bash", "-c", body + "\nresolve_tasks_root"],
                env=clean, cwd=cwd, capture_output=True, text=True)
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout.strip() == _tasks_root(env, cwd), \
                (env, cwd, proc.stdout.strip(), _tasks_root(env, cwd))
        # ...and a RELATIVE rung-1 value: bash prints it verbatim, Python
        # absolutises it, and the two still name the same directory when both are
        # read against the cwd they were resolved in.
        relative = os.path.join("rel", "runs")
        env = {"ORCH_TASKS_ROOT": relative}
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("ORCH_TASKS_ROOT", "CLAUDE_PROJECT_DIR",
                              "ORCH_STATE_DIR")}
        clean.update(env)
        proc = subprocess.run(["bash", "-c", body + "\nresolve_tasks_root"],
                              env=clean, cwd=deep, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == relative, proc.stdout
        python_answer = _tasks_root(env, deep)
        assert os.path.isabs(python_answer), python_answer
        assert os.path.realpath(os.path.join(deep, proc.stdout.strip())) == \
            os.path.realpath(python_answer), (proc.stdout.strip(), python_answer)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_in_plugin_cache_walks_up_to_a_plugin_manifest():
    base = tempfile.mkdtemp(prefix="plugincache-", dir=_TMP_BASE)
    try:
        root = os.path.join(base, "cache", "msdrx-tools", "touch", "0.1.0")
        os.makedirs(os.path.join(root, ".claude-plugin"))
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), "w") as f:
            f.write('{"name":"touch"}')
        deep = os.path.join(root, "shared", "monitoring", "does-not-exist-yet")
        assert ms.in_plugin_cache(deep) is True
        assert ms.in_plugin_cache(root) is True
        assert ms.in_plugin_cache(base) is False
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_the_token_file_is_refused_inside_a_plugin_cache():
    base = tempfile.mkdtemp(prefix="tokrefuse-", dir=_TMP_BASE)
    try:
        root = os.path.join(base, "0.1.0")
        state = os.path.join(root, "state")
        os.makedirs(os.path.join(root, ".claude-plugin"))
        os.makedirs(state)
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), "w") as f:
            f.write('{"name":"touch"}')
        assert ms.write_token_file(state) is None
        assert not os.path.exists(os.path.join(state, "monitor.json"))
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


# --------------------------------------------------------------------------
# Item 05 — security parity with the aggregator: loopback, token, Origin.
# --------------------------------------------------------------------------


def test_the_default_bind_is_loopback_and_open_is_an_explicit_opt_in():
    """GD-T8: reaching 0.0.0.0 takes a flag or an env var, never a default."""
    src = open(MODULE_PATH, encoding="utf-8").read()
    assert 'DEFAULT_HOST = "127.0.0.1"' in src
    assert 'OPEN_HOST = "0.0.0.0"' in src
    # the ONE place the open literal may appear is that constant
    assert src.count('"0.0.0.0"') == 1, "a stray 0.0.0.0 bind literal is back"
    assert 'start_server(handle, HOST, PORT)' in src
    assert ms.HOST == "127.0.0.1", ms.HOST
    # and the resolver honours both opt-ins without touching the default
    saved = list(sys.argv)
    try:
        sys.argv = [saved[0], "--open"]
        assert ms.resolve_host() == "0.0.0.0"
        sys.argv = [saved[0]]
        os.environ["ORCH_BIND"] = "10.1.2.3"
        assert ms.resolve_host() == "10.1.2.3"
    finally:
        os.environ.pop("ORCH_BIND", None)
        sys.argv = saved


def test_a_flag_argument_is_not_mistaken_for_the_port():
    """`monitor_server.py --open` must not read '--open' as argv's port.

    The assertion is about PARSING, so it must not depend on the ambient
    `orch-config.json`: `resolve_port()`'s third rung reads one from the state
    dir, and any task folder that pins a `port` would turn a green flag parse
    into a red "flag mistaken for the port" that is nothing of the sort. So:
    assert the flag never becomes the answer, and that an explicit numeric
    positional still wins outright.
    """
    saved = list(sys.argv)
    try:
        sys.argv = [saved[0], "--open", "--allow-origin", "http://x.example"]
        port = ms.resolve_port()
        assert isinstance(port, int), port
        assert port not in (0,) and str(port) not in ("--open", "--allow-origin"), port
        assert not ms.positional_args(), ms.positional_args()
        sys.argv = [saved[0], "9999", "--open"]
        assert ms.resolve_port() == 9999
        # the equals spelling is a flag too, and is likewise not a positional
        sys.argv = [saved[0], "--allow-origin=http://x.example", "9001"]
        assert ms.positional_args() == ["9001"], ms.positional_args()
        assert ms.resolve_port() == 9001
    finally:
        sys.argv = saved


def test_both_allowlist_flags_read_the_space_and_equals_spellings_and_the_env():
    """F11: a flag that parses and then silently does nothing is the worst case."""
    saved = list(sys.argv)
    try:
        sys.argv = [saved[0], "--allow-origin", "http://a.example",
                    "--allow-origin=http://b.example"]
        os.environ["ORCH_ALLOW_ORIGIN"] = "http://c.example, http://d.example"
        got = ms.flag_values("--allow-origin", "ORCH_ALLOW_ORIGIN")
        assert got == ["http://a.example", "http://b.example",
                       "http://c.example", "http://d.example"], got
        # ...and the same helper serves --allow-host, so neither can rot alone.
        sys.argv = [saved[0], "--allow-host=mybox"]
        os.environ["ORCH_ALLOW_HOST"] = "otherbox"
        assert ms.flag_values("--allow-host", "ORCH_ALLOW_HOST") == \
            ["mybox", "otherbox"]
    finally:
        os.environ.pop("ORCH_ALLOW_ORIGIN", None)
        os.environ.pop("ORCH_ALLOW_HOST", None)
        sys.argv = saved


def _boot_probe(env, argv, expr):
    """Import monitor_server.py in a subprocess under a controlled env/argv.

    `HOST`, `HOSTS` and `ORIGINS` are import-time module constants — deliberately,
    since the posture must not be reconfigurable at runtime — so NO in-process
    test can exercise a non-default configuration. That is exactly how the
    unreachable-escape-hatch bug (F1) shipped past a suite that only tested the
    refusal side. A fresh interpreter is the only honest probe.
    """
    import subprocess
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("ORCH_", "CLAUDE_"))}
    # no .pyc droppings in the payload tree — the parent's
    # sys.dont_write_bytecode does not reach a child, and the comprehension
    # above strips only ORCH_/CLAUDE_, so this has to be set explicitly
    clean["PYTHONDONTWRITEBYTECODE"] = "1"
    clean["ORCH_STATE_DIR"] = _STATE_DIR
    clean.update(env)
    code = (
        "import importlib.util, json, sys\n"
        f"sys.argv = {[sys.executable] + list(argv)!r}\n"
        f"spec = importlib.util.spec_from_file_location('probe', {MODULE_PATH!r})\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        f"print(json.dumps({expr}))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=clean,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_an_allow_listed_origin_and_host_are_accepted_on_a_reachable_bind():
    """F1: the documented escape hatch out of the 403 must actually work.

    An operator who binds a reachable address and browses the box BY NAME sends
    a Host header nothing derived can predict. Before the fix the Host gate
    returned before `ORIGINS` was ever consulted, so `--allow-origin` could not
    take effect and `--allow-host` was accepted-and-inert: the only way out of
    the new 403 was closed. Both halves are asserted here, on a real import.
    """
    env = {"ORCH_BIND": "192.168.1.5"}
    argv = ["--allow-origin", "http://mybox:8931"]
    head = {"host": "mybox:8931", "origin": "http://mybox:8931"}
    # 1. an explicitly allow-listed Origin satisfies the whole gate
    assert _boot_probe(env, argv, f"m.origin_refusal({head!r})") is None
    # 2. --allow-host extends the Host allowlist (and is no longer a dead flag)
    assert _boot_probe(env, ["--allow-host=mybox"],
                       f"m.origin_refusal({head!r})") is None
    assert "mybox" in _boot_probe(env, ["--allow-host=mybox"], "sorted(m.HOSTS)")
    # 3. ...and nothing was loosened: an un-listed foreign page is still refused
    assert _boot_probe(env, [], f"m.origin_refusal({head!r})") is not None
    evil = {"host": "192.168.1.5:8931", "origin": "http://evil.example"}
    assert _boot_probe(env, argv, f"m.origin_refusal({evil!r})") is not None
    # 4. an --open bind keeps its empty Host allowlist: the operator reaches it
    #    through whatever address was published, so a derived list is a guess.
    assert _boot_probe({}, ["--open"], "sorted(m.HOSTS)") == []


def test_health_publishes_no_filesystem_path_and_no_home_directory():
    """F2/AUDIT-15: /health is the one untokened route — it hashes every path.

    An events path spells out the machine's username, the project directory and
    the task roster. The aggregator hashes them on its own /health for this
    exact reason; item 05 is written as parity with that posture.
    """
    poisoned = os.path.join(_STATE_DIR, "health-hygiene.jsonl")
    good = json.dumps({"ts": "1", "plan": "sp-a", "stage": "plan", "state": "done"})
    with open(poisoned, "wb") as f:
        f.write((good + "\n").encode())
        f.write(b"{not json at all\n")
    ms.task_status(poisoned)
    try:
        blob = json.dumps(ms.health_payload())
        # The literal the critique named. Skipped only if the throwaway state
        # dir itself lives under /home, where the string would be the TEST's
        # own doing rather than a leak — the two structural checks below still
        # run, and they are the ones that actually decide it.
        if not _STATE_DIR.startswith("/home"):
            assert "/home" not in blob, blob
        assert _STATE_DIR not in blob, blob
        assert ".jsonl" not in blob, blob
        for key in ms.health_payload()["parse_failures"]:
            assert os.sep not in key, key
            assert len(key) == 12, key
        for key in ms.health_payload()["streams"]:
            assert os.sep not in key, key
    finally:
        ms.PARSE_FAILURES.pop(poisoned, None)
        os.remove(poisoned)


def test_the_token_is_per_boot_and_required_on_every_route_but_health():
    assert len(ms.TOKEN) >= 40, "expected a 256-bit urlsafe token"
    assert ms.OPEN_ROUTES == frozenset({"/health"}), ms.OPEN_ROUTES
    for route in ("/tasks", "/artifacts", "/file", "/ws"):
        assert ms.token_ok(route, {}, "") is False, route
        assert ms.token_ok(route, {}, f"token={ms.TOKEN}") is True, route
        assert ms.token_ok(route, {"x-orch-token": ms.TOKEN}, "") is True, route
        assert ms.token_ok(route, {"authorization": f"Bearer {ms.TOKEN}"}, "") is True, route
        assert ms.token_ok(route, {}, "token=" + "x" * len(ms.TOKEN)) is False, route
    assert ms.token_ok("/health", {}, "") is True


def test_the_token_comparison_is_constant_time():
    """A missing token and a wrong one must take the same path (no short-circuit)."""
    import inspect
    src = inspect.getsource(ms.token_ok)
    assert "hmac.compare_digest" in src, src
    assert 'presented = presented_token(headers, query, header_only=header_only) or ""' \
        in src, src
    # ...and the header-only carrier is a real restriction, not a parameter that
    # is accepted and ignored: a write's token may not ride in the query string
    # (W4), because the page's own URL carries it there.
    assert ms.presented_token({}, f"token={ms.TOKEN}") == ms.TOKEN
    assert ms.presented_token({}, f"token={ms.TOKEN}", header_only=True) == ""
    assert ms.presented_token({"x-orch-token": ms.TOKEN}, "", header_only=True) == ms.TOKEN
    assert ms.presented_token({"authorization": f"Bearer {ms.TOKEN}"}, "",
                              header_only=True) == ms.TOKEN


def test_the_ws_origin_allowlist_refuses_a_foreign_page():
    """A page on evil.example that resolves to 127.0.0.1 fails by NAME."""
    same = {"host": f"127.0.0.1:{ms.PORT}", "origin": f"http://127.0.0.1:{ms.PORT}"}
    assert ms.origin_refusal(same) is None
    # no Origin at all: a non-browser client, which still had to present a token
    assert ms.origin_refusal({"host": f"127.0.0.1:{ms.PORT}"}) is None
    foreign = {"host": f"127.0.0.1:{ms.PORT}", "origin": "http://evil.example"}
    assert "not allowed" in (ms.origin_refusal(foreign) or "")
    rebind = {"host": "evil.example", "origin": "http://evil.example"}
    assert "allowlist" in (ms.origin_refusal(rebind) or "")


def _serve_once(route, headers=(), query=""):
    """Drive ms.handle over a real loopback socket; return (status line, body)."""
    async def run():
        server = await asyncio.start_server(ms.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            head = [f"GET {route}{query} HTTP/1.1", f"Host: 127.0.0.1:{port}",
                    "Connection: close", *headers]
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode())
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(-1), 5)
            writer.close()
            top, _, body = raw.partition(b"\r\n\r\n")
            return top.split(b"\r\n")[0].decode(), body
        finally:
            server.close()
            await server.wait_closed()
    return _run(run())


def test_an_untokened_request_is_401_on_every_gated_route():
    for route in ("/tasks", "/artifacts", "/file"):
        status, body = _serve_once(route)
        assert status.startswith("HTTP/1.1 401"), (route, status)
        assert b"token" in body, (route, body)
    # ...and /health answers without one, so a supervisor can still probe.
    status, body = _serve_once("/health")
    assert status.startswith("HTTP/1.1 200"), status
    assert json.loads(body), body
    # ...and the page itself is open: it is what CARRIES the token.
    status, body = _serve_once("/")
    assert status.startswith("HTTP/1.1 200"), status


def test_a_tokened_request_passes_and_a_foreign_origin_upgrade_is_403():
    status, body = _serve_once("/tasks", query=f"?token={ms.TOKEN}")
    assert status.startswith("HTTP/1.1 200"), status
    assert "tasks" in json.loads(body), body
    # WS upgrade, correct token, foreign Origin -> 403 before any 101.
    status, body = _serve_once(
        "/ws", query=f"?token={ms.TOKEN}",
        headers=["Upgrade: websocket", "Connection: Upgrade",
                 "Sec-WebSocket-Key: " + base64.b64encode(b"0123456789abcdef").decode(),
                 "Sec-WebSocket-Version: 13", "Origin: http://evil.example"])
    assert status.startswith("HTTP/1.1 403"), status
    # ...and an untokened upgrade never even learns the Origin policy: 401.
    status, _ = _serve_once(
        "/ws", headers=["Upgrade: websocket", "Connection: Upgrade",
                        "Sec-WebSocket-Key: " + base64.b64encode(b"0123456789abcdef").decode(),
                        "Sec-WebSocket-Version: 13"])
    assert status.startswith("HTTP/1.1 401"), status


def test_the_token_file_is_written_0600():
    import stat
    base = tempfile.mkdtemp(prefix="tokfile-", dir=_TMP_BASE)
    try:
        path = ms.write_token_file(base)
        assert path == os.path.join(base, "monitor.json"), path
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, oct(mode)
        payload = json.loads(open(path).read())
        assert payload["token"] == ms.TOKEN
        assert payload["host"] == ms.HOST and payload["port"] == ms.PORT
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_the_page_plumbs_the_token_through_every_gated_url():
    """monitor.html is never executed here — the assertion is on source text."""
    html = open(os.path.join(str(MON), "monitor.html"), encoding="utf-8").read()
    assert 'const TOKEN = new URLSearchParams(location.search).get("token") || "";' in html
    assert 'withToken("/tasks")' in html
    assert 'withToken("/artifacts?task="' in html
    assert 'withToken("/file?task="' in html
    assert 'tokenParam("&")' in html, "the WS url must carry the token"
    # navigate() rewrites the query string; dropping the token there would 401
    # the page out of its own next route.
    nav = html[html.index("function navigate("):html.index("function route(")]
    assert "tokenParam(" in nav, nav
    # F4: an <a href> is a gated URL too. onclick/preventDefault only covers a
    # plain left-click — ctrl/cmd-click, middle-click, "open in new tab" and
    # "copy link address" all hand the browser the raw href. Assert no href
    # assignment anywhere carries a bare task query, so the NEXT one cannot
    # repeat it either.
    hrefs = [ln.strip() for ln in html.splitlines() if ".href = " in ln]
    assert hrefs, "no href assignments found — has the page been restructured?"
    for line in hrefs:
        assert "withToken(" in line or "fileUrl(" in line, line
    assert 'sl.href = withToken("?task="' in html, "the stats link lost its token"
    # ...and fileUrl is only a pass because it tokens the URL itself.
    assert 'return withToken("/file?task="' in html


def test_the_page_says_token_instead_of_failing_silently():
    """F3: a 401 must not render as an empty dashboard that reconnects forever.

    Both halves are asserted, because each alone is still silent: `/tasks` has
    to branch on the RESPONSE before `.json()` (a 401 body is not JSON, so the
    old code threw straight into an empty `catch`), and a `/ws` upgrade the
    server never accepted has to STOP — the browser hands JS no status code for
    a rejected handshake, so "never opened + no token" is the only evidence
    there is, and without it the page opens a fresh socket every 1-10 s forever.
    """
    html = open(os.path.join(str(MON), "monitor.html"), encoding="utf-8").read()
    assert 'id="authBanner"' in html, "no banner element to put the reason in"
    assert "function showAuthBanner(" in html
    assert "TOKEN_HINT" in html and "monitor.json" in html, \
        "the hint must name where the token actually lives"
    tasks = html[html.index("async function refreshTasks("):
                 html.index('document.getElementById("taskSel").onchange')]
    assert "res.status === 401" in tasks, tasks
    assert "showAuthBanner(" in tasks, tasks
    assert tasks.index("res.status === 401") < tasks.index("res.json()"), \
        "the status must be checked BEFORE .json() throws into the catch"
    close = html[html.index("  ws.onclose = () => {"):html.index("  ws.onerror = ")]
    assert "everOpened" in close and "authRefused" in close, close
    assert "refused = true" in close, close
    assert close.index("refused = true") < close.index("armReconnect(retry)"), \
        "the stop must come before the retry, or it never stops"


def test_report_html_from_file_is_sandboxed_without_scripts():
    """F8: the report tab is served at a URL that CONTAINS the per-boot token.

    An opaque origin stops it reading same-origin responses; it does not stop a
    script reading its own `location.search` and posting it out. Reports here
    are static, so scripts are what gets given up.
    """
    src = open(MODULE_PATH, encoding="utf-8").read()
    # The string is now a NAMED constant, byte-identical with the aggregator's
    # `FILE_CSP` — GD-20's verbatim twin, made machine-checkable across the two
    # servers (SECURITY-4; the cross-server comparison itself lives in the
    # consolidated memory suite, which can import both).
    assert ms.FILE_CSP == "sandbox", ms.FILE_CSP
    assert "allow-scripts" not in ms.FILE_CSP, ms.FILE_CSP
    assert ms.NO_REFERRER == "no-referrer", ms.NO_REFERRER
    assert 'FILE_CSP = "sandbox"' in src, "the constant must be a bare literal"
    assert 'f"Content-Security-Policy: {FILE_CSP}\\r\\n"' in src, \
        "the /file HTML branch must send the constant"
    assert 'f"Referrer-Policy: {NO_REFERRER}\\r\\n"' in src, src
    # the header itself, not the prose explaining why it is gone
    for line in src.splitlines():
        assert not (line.lstrip().startswith(("b\"", "b'", 'f"')) and "allow-scripts" in line), \
            f"a script in a report can lift the token: {line.strip()}"


def test_the_startup_line_does_not_print_the_token_into_a_0644_log():
    """F6: drivers redirect stdout into `<task-dir>/daemon.log`, mode 0644.

    Printing the secret there undoes the 0600 `write_token_file()` takes care to
    create. A TTY is a human reading a terminal; a log gets a fingerprint —
    unless the token file could not be written at all, in which case stdout is
    the only copy there is and printing it is the lesser failure.
    """
    import inspect
    src = inspect.getsource(ms.main)
    assert "sys.stdout.isatty() or not token_path" in src, src
    assert src.index("token_path = write_token_file(") < src.index("isatty()"), \
        "the token file must be written before the print decides what to say"


def test_token_is_not_reported_as_an_unhonoured_ws_parameter():
    """The page always sends `&token=` on /ws; a false "ignored" note on every
    connection would train the operator to ignore the note that matters."""
    src = open(MODULE_PATH, encoding="utf-8").read()
    marker = 'if k not in ("task", "v", "snap", "from", "sig", "token")'
    assert marker in src, "token must be a KNOWN /ws query parameter"


# --------------------------------------------------------------------------
# M6/M9/M12 — the Stream registry, the unified fold, the snapshot, /health.
#
# Everything below is UNIT level: no server is started and no socket is opened
# (the end-to-end protocol cases live in test_ws_e2e.py). The generated stream
# comes from gen_stream.py, the same deterministic corpus the e2e and perf
# suites use, so a failure here and a failure there describe the same bytes.
# --------------------------------------------------------------------------
sys.path.insert(0, HERE)
import gen_stream  # noqa: E402  (sibling helper, not a test module)

GOLD_DIR = os.path.join(HERE, "fixtures")
GOLD_SNAPSHOT = os.path.join(GOLD_DIR, "snapshot-gold.json")
GOLD_MANIFEST = os.path.join(GOLD_DIR, "MANIFEST.sha256")
GOLD_N, GOLD_SEED = 1200, 20260727


def _run(coro):
    """Each async case gets its own loop; the registry survives across them."""
    return asyncio.run(coro)


def _write(name, lines) -> str:
    path = os.path.join(_STATE_DIR, name)
    with open(path, "wb") as f:
        f.write(("\n".join(lines) + "\n").encode())
    return path


def _append(path, lines) -> None:
    with open(path, "ab") as f:
        f.write(("\n".join(lines) + "\n").encode())


def _fresh_stream(path):
    """A Stream with no history, even if an earlier case used the same path."""
    ms.Stream._REGISTRY.pop(os.path.abspath(path), None)
    return ms.Stream.get(path)


def test_stream_fold_equals_replay_plan_states():
    """M6(a) — the anti-drift guard: incremental fold == the reference scan.

    ``replay_plan_states``/``task_status`` stay in the file precisely so this
    equality is executable. It is asserted after EVERY append, not just at the
    end, because an incremental fold that is right only at the end is a fold
    that is wrong for the whole live run.
    """
    lines = gen_stream.make_stream(4_000)
    path = _write("fold-equal.jsonl", lines[:1_500])

    async def go():
        stream = _fresh_stream(path)
        for extra in (lines[1_500:2_600], lines[2_600:]):
            await stream.refresh()
            ref_states, ref_last, ref_tok, ref_fail = ms.replay_plan_states(path)
            assert stream.fold.plan_states == ref_states, "badge states drifted"
            assert stream.fold.last == ref_last, "last meaningful event drifted"
            assert stream.fold.tok == ref_tok, (stream.fold.tok, ref_tok)
            assert stream.fold.parse_failures == ref_fail
            assert stream.status() == ms.task_status(path), "verdict drifted"
            _append(path, extra)
        await stream.refresh()
        assert stream.status() == ms.task_status(path)
        assert stream.fold.ev_count == len(lines), stream.fold.ev_count
        assert stream.offset == os.path.getsize(path), stream.offset

    _run(go())


def test_stream_split_keeps_a_control_byte_inside_one_record():
    """M6(d) — records are newline-delimited, full stop (SERVER-READ-9).

    ``bytes.splitlines()`` also breaks on \\v/\\f/\\x1c-\\x1e, so ONE physical
    line carrying a stray control byte used to become TWO frames, both invalid
    JSON. Beside ``test_read_frames_torn_line``, for the offset-carrying split.
    """
    ev = {"ts": "2026-07-28T08:00:00.000Z", "plan": "sp-a", "stage": "impl",
          "state": "running", "detail": "vertical\x0btab inside one line"}
    raw = json.dumps(ev)
    path = _write("ctrl.jsonl", [raw])
    recs = ms.split_records(open(path, "rb").read(), 0)
    assert len(recs) == 1, recs
    assert recs[0][0] == raw.encode(), recs[0][0]
    assert recs[0][1] == os.path.getsize(path), recs[0]
    assert len(ms.read_records(path, 0)[0]) == 1
    assert len(ms.read_frames(path, 0)[0]) == 1


def test_stream_records_carry_true_byte_offsets_across_blank_lines():
    """The cursor is a BYTE offset: blank lines count, stripped bytes do not."""
    a, b = json.dumps({"ts": "1", "plan": "p"}), json.dumps({"ts": "2", "plan": "p"})
    path = os.path.join(_STATE_DIR, "offsets.jsonl")
    with open(path, "wb") as f:
        f.write((a + "\n\n" + b + "\n").encode())
    recs, off = ms.read_records(path, 0)
    assert [r[0] for r in recs] == [a.encode(), b.encode()], recs
    assert recs[0][1] == len(a) + 1, recs[0]
    assert recs[1][1] == off == os.path.getsize(path), (recs[1], off)


def test_stream_resets_once_on_truncation_rotation_and_rewrite():
    """M6(c) — every identity break re-folds from 0, exactly once each.

    Three ways a stream stops being the stream we were reading, all of which
    used to look like an append: it shrank, it was replaced (new inode), or it
    was rewritten in place with a first 4 KB that happens to be identical (the
    wipe-and-rerun of a run whose header lines are deterministic). The third is
    why the scan also checks CONTINUITY at the cursor.
    """
    short, long_ = gen_stream.make_stream(40), gen_stream.make_stream(120)
    path = _write("reset.jsonl", long_)

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        assert stream.resets == 0, stream.resets
        base_ev = stream.fold.ev_count

        _write("reset.jsonl", short)              # truncation: size < offset
        await stream.refresh()
        assert stream.resets == 1, stream.resets
        assert stream.fold.ev_count == len(short), stream.fold.ev_count

        os.remove(path)                           # rotation: new inode
        _write("reset.jsonl", long_)
        await stream.refresh()
        assert stream.resets == 2, stream.resets
        assert stream.fold.ev_count == len(long_) == base_ev

        # rewrite in place, SAME inode, LARGER, identical first 4 KB
        assert ms.stream_sig(path) == ms.stream_sig(path)
        with open(path, "wb") as f:
            f.write(("\n".join(gen_stream.make_stream(300)) + "\n").encode())
        await stream.refresh()
        assert stream.resets == 3, "an in-place rewrite is not an append"
        assert stream.fold.ev_count == 300, stream.fold.ev_count
        assert stream.offset == os.path.getsize(path)

    _run(go())


def test_snapshot_offset_stops_at_the_last_newline():
    """M9(c)/DATA-MODEL-4 — a torn tail is never folded, in either half.

    If the fold's offset landed inside a line the writer is still finishing,
    the completed line would be folded AND replayed on the tail: the plan's
    token counter would be permanently high for that connection, with nothing
    on screen to hint at it.
    """
    lines = gen_stream.make_stream(60)
    path = _write("torn-snap.jsonl", lines[:-1])
    complete = os.path.getsize(path)
    with open(path, "ab") as f:
        f.write(lines[-1][:60].encode())          # partial, no newline

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        snap = json.loads(stream.snapshot_bytes())
        assert snap["offset"] == complete, (snap["offset"], complete)
        assert snap["evCount"] == len(lines) - 1, snap["evCount"]
        with open(path, "ab") as f:                # complete the torn line
            f.write(lines[-1][60:].encode() + b"\n")
        await stream.refresh()
        snap2 = json.loads(stream.snapshot_bytes())
        assert snap2["offset"] == os.path.getsize(path)
        assert snap2["evCount"] == len(lines), snap2["evCount"]

    _run(go())


def test_snapshot_carries_every_field_the_page_reads():
    """M9(f)/WS-PROTOCOL-5 — field completeness is a correctness matter.

    A field omitted here does not error: it silently blanks a stats tile or
    mis-renders a badge. ``finishedMs``, ``roles``, ``quietCount`` and
    ``planTotal`` each feed one, and every ordered map must be an ARRAY of
    pairs (DATA-MODEL-13).
    """
    lines = gen_stream.make_stream(2_000)
    path = _write("snap-fields.jsonl", lines)

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        snap = json.loads(stream.snapshot_bytes())
        for key in ("m", "kind", "foldGen", "sig", "offset", "evCount",
                    "quietCount", "planTotal", "parseFailures", "plans", "timeplan"):
            assert key in snap, key
        assert snap["m"] == "snapshot" and snap["kind"] == "monitor-snapshot"
        assert snap["foldGen"] == ms.FOLD_GEN
        assert snap["evCount"] == len(lines) and snap["quietCount"] > 0
        assert snap["planTotal"] >= 1, snap["planTotal"]
        assert isinstance(snap["plans"], list) and snap["plans"], "plans is an array"
        finished = roles = agents = 0
        for pid, p in snap["plans"]:
            assert isinstance(pid, str) or pid is None, pid
            for key in ("title", "state", "firstTs", "lastTs", "tok", "stages",
                        "agents", "roles", "log", "logTotal"):
                assert key in p, (pid, key)
            for ordered in ("stages", "agents", "roles"):
                assert isinstance(p[ordered], list), (pid, ordered)
                assert all(isinstance(pair, list) and len(pair) == 2
                           for pair in p[ordered]), (pid, ordered)
            assert set(p["tok"]) == {"in", "out", "cached", "write"}, p["tok"]
            roles += len(p["roles"])
            for aid, row in p["agents"]:
                agents += 1
                assert len(aid) == 17, aid          # full id, never shortId
                for key in ("label", "started", "state", "runtime", "finishedMs",
                            "tokens", "ctx"):
                    assert key in row, (aid, key)
                if row["state"] in ("done", "failed"):
                    finished += row["finishedMs"] is not None
        assert agents > 10 and roles > 3, (agents, roles)
        assert finished > 5, "terminal agents must carry finishedMs"
        tp = snap["timeplan"]
        for key in ("segs", "runs", "summary", "tailTicks"):
            assert key in tp, key
        for key in ("t0", "end", "upMs", "idleMs", "downMs", "stallCount",
                    "stallMax", "live"):
            assert key in tp["summary"], key
        assert all(s["kind"] in ("up", "idle", "down") for s in tp["segs"])

    _run(go())


def test_snapshot_tokens_are_absolute_and_exactly_equal_the_delta_sum():
    """GD-C — sum(every delta) == sum(last absolute per (plan, agent)).

    The snapshot's per-plan counters are the summed deltas the page keeps; the
    agent rows are last-wins absolutes. The two models must agree per plan and
    in total or the tail composition (snapshot absolutes + tail deltas) is not
    exact. Asserted, never assumed — this is an empirical property of the
    watcher, pinned here over a generated stream with stale agents in it.
    """
    lines = gen_stream.make_stream(3_000)
    path = _write("snap-tokens.jsonl", lines)

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        snap = json.loads(stream.snapshot_bytes())
        model_a, model_b = gen_stream.token_models(lines)
        for pid, p in snap["plans"]:
            if pid not in model_a and pid not in model_b:
                continue
            want = model_a.get(pid, {k: 0 for k in gen_stream.TOKEN_KEYS})
            got = {"in": p["tok"]["in"], "out": p["tok"]["out"],
                   "cached": p["tok"]["cached"], "cache_write": p["tok"]["write"]}
            assert got == want, (pid, got, want)
            absolute = {k: 0 for k in gen_stream.TOKEN_KEYS}
            for _aid, row in p["agents"]:
                for k in gen_stream.TOKEN_KEYS:
                    absolute[k] += (row["tokens"] or {}).get(k) or 0
            assert absolute == want, (pid, absolute, want)
        totals = {k: sum(p["tok"]["write" if k == "cache_write" else k]
                         for _i, p in snap["plans"]) for k in gen_stream.TOKEN_KEYS}
        assert totals == stream.fold.tok, (totals, stream.fold.tok)

    _run(go())


def _ctx_ev(aid, ctx=None, **agent):
    """One watcher-shaped agent tick for the ctx fold cases (GD-LC-4 wire shape)."""
    a = {"id": aid, "label": "impl #1", "state": "running",
         "tokens": {"in": 1000, "out": 10, "cached": 0, "cache_write": 0}}
    a.update(agent)
    if ctx is not None:
        a["ctx"] = ctx
    return json.dumps({"ts": "2026-07-31T08:00:00.000Z", "plan": "sp-a",
                       "stage": "impl", "state": "running", "detail": "tick",
                       "agent": a})


def _ctx_row(fold, aid):
    snap = fold.snapshot("sig0123456789abc", 0)
    plans = dict(snap["plans"])
    return dict(plans["sp-a"]["agents"])[aid], snap


def test_an_agent_with_no_context_reading_serialises_null_never_zero():
    """GD-LC-4/UI-CARDS-12 — unknown is the KEY BEING ABSENT on the wire.

    Its fold-internal spelling is ``None``, and it must survive into the
    snapshot as JSON ``null``. Never ``{}`` (truthy-shaped downstream, and an
    invitation to reconstruct a ``{"used": 0}``), never ``{"used": 0}`` — a
    rendered 0 reads as "this agent's context is empty", which is the opposite
    of "I do not know". 30 of 649 measured transcripts end on a ``<synthetic>``
    row with no reading at all; every one of them must render as a dash.
    """
    aid = "a" * 17
    fold = ms.Fold()
    fold.apply(_ctx_ev(aid).encode())
    row, snap = _ctx_row(fold, aid)
    assert "ctx" in row, "the key must be present in the row so the page can hydrate it"
    assert row["ctx"] is None, row["ctx"]
    assert row["ctx"] != {} and row["ctx"] != {"used": 0}
    assert '"ctx": null' in json.dumps(snap, indent=1), \
        "the snapshot must serialise a missing reading as null"


def test_context_is_not_a_counter_a_compaction_must_be_allowed_to_lower_it():
    """GD-LC-4/UI-CARDS-7 — the fold is last-wins, NON-monotonic, no merge.

    Every other token path here is deliberately monotonic (``token_deltas``
    clamps, the page and the fold sum). That is right for cumulative billing and
    wrong for occupancy: a ``/compact`` drops the level by design. If ctx were
    passed through counter-shaped logic the gauge would pin at the
    pre-compaction high forever — an agent showing "near limit" while 79 % of
    its window is free, which is a stable, plausible, wrong number.

    The second half is the no-merge rule: a whole-object replace, so a `cap`
    that was declared for one model cannot survive into a reading that no longer
    carries one.
    """
    aid = "b" * 17
    fold = ms.Fold()
    stamp = "2026-07-31T08:00:00.000Z"
    fold.apply(_ctx_ev(aid, {"used": 90000, "at": stamp, "peak": 90000,
                             "cap": 200000}).encode())
    row, _ = _ctx_row(fold, aid)
    assert row["ctx"]["used"] == 90000, row["ctx"]

    # the compaction: a LOWER level, with peak preserved by the writer
    fold.apply(_ctx_ev(aid, {"used": 12000, "at": stamp, "peak": 90000}).encode())
    row, snap = _ctx_row(fold, aid)
    assert row["ctx"]["used"] == 12000, (row["ctx"], "context is not a counter")
    assert row["ctx"]["peak"] == 90000, row["ctx"]
    assert "cap" not in row["ctx"], \
        "whole-object replace: a partial merge would keep a stale cap alive"
    assert json.loads(json.dumps(snap)) == snap, "the row must stay JSON-serialisable"

    # a tick with no reading leaves the previous one standing (it ages by its
    # own `at`); it never resets to zero and never clears the key
    fold.apply(_ctx_ev(aid).encode())
    row, _ = _ctx_row(fold, aid)
    assert row["ctx"]["used"] == 12000, row["ctx"]


def test_snapshot_log_budget_is_global_with_a_per_plan_floor():
    """M9(g)/GD-F — 1,500 lines or 400 KB, largest-recent-first, floor 20.

    A per-plan cap multiplies by plan count, and plan count is what grows at
    100k events; this is the bound that does not. Truncation is disclosed
    (``logTotal`` per plan, ``logTruncated`` overall) so the page can offer the
    full-replay reconnect instead of silently dropping history.
    """
    loud = []
    for i in range(120):
        pid = f"sp-{i % 40}"
        loud.append(json.dumps({"ts": "2026-07-28T08:00:00.000Z", "plan": pid,
                                "stage": "plan", "state": "running",
                                "detail": "open"}))
    for i in range(4_000):
        pid = f"sp-{i % 40}"
        loud.append(json.dumps({"ts": "2026-07-28T08:00:%02d.000Z" % (i % 60),
                                "plan": pid, "stage": "impl", "state": "running",
                                "detail": f"line {i} " + "x" * 120}))
    path = _write("budget.jsonl", loud)

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        snap = json.loads(stream.snapshot_bytes())
        shipped = sum(len(p["log"]) for _i, p in snap["plans"])
        total = sum(p["logTotal"] for _i, p in snap["plans"])
        assert total == len(loud), (total, len(loud))
        assert shipped <= ms.LOG_BUDGET_LINES, shipped
        assert snap["logTruncated"] is True, "truncation must be disclosed"
        # No escape clause: the floor exists precisely for the case where the
        # budget binds, so an assert that gives up when it binds asserts
        # nothing. Every card with history ships some of it (m-1).
        for pid, p in snap["plans"]:
            if p["logTotal"]:
                assert len(p["log"]) > 0, f"{pid} ships a blank card"
            assert len(p["log"]) <= p["logTotal"], pid
        body = len(json.dumps([p["log"] for _i, p in snap["plans"]]).encode())
        assert body <= ms.LOG_BUDGET_BYTES * 1.2, body
        # a small stream ships everything and says so
        small = _write("budget-small.jsonl", gen_stream.make_stream(200))
        s2 = _fresh_stream(small)
        await s2.refresh()
        snap2 = json.loads(s2.snapshot_bytes())
        assert snap2["logTruncated"] is False, snap2["logTruncated"]

        # ...and M9(g)'s stated scale, where a FLAT floor stops being a floor:
        # 145 plans x 20 lines is 2,900 against a 1,500-line budget, so a floor
        # pass that walks largest-first spends the whole budget on the plans
        # that need it least and leaves ~70 cards blank (m-1). Twenty of them
        # are loud, so the remainder pass has somewhere to go.
        many = []
        for i in range(145):
            many.append(json.dumps({"ts": "2026-07-28T08:00:00.000Z",
                                    "plan": f"sp-{i}", "stage": "plan",
                                    "state": "running", "detail": "open"}))
        for i in range(3_000):
            many.append(json.dumps({"ts": "2026-07-28T08:00:%02d.000Z" % (i % 60),
                                    "plan": f"sp-{i % 20}", "stage": "impl",
                                    "state": "running", "detail": f"loud {i}"}))
        s3 = _fresh_stream(_write("budget-145.jsonl", many))
        await s3.refresh()
        snap3 = json.loads(s3.snapshot_bytes())
        assert len(snap3["plans"]) == 145, len(snap3["plans"])
        blank = [pid for pid, p in snap3["plans"]
                 if p["logTotal"] and not p["log"]]
        assert not blank, f"{len(blank)} cards ship no log at all: {blank[:5]}"
        shipped3 = sum(len(p["log"]) for _i, p in snap3["plans"])
        assert shipped3 <= ms.LOG_BUDGET_LINES, shipped3
        # the remainder still favours the loud plans over the quiet ones
        loud_lines = sum(len(p["log"]) for pid, p in snap3["plans"]
                         if int(pid.split("-")[1]) < 20)
        assert loud_lines > shipped3 / 2, (loud_lines, shipped3)

    _run(go())


def test_fold_sweeps_open_cards_on_run_complete_and_never_invents_a_badge():
    """M9(e)/GD-F run-complete sweep + R-58, on the card fold.

    The page settles every still-open sub-plan card on a run-level ``complete``
    (monitor.html:632-640) and the server's fold now matches. What it must NOT
    do is synthesize: a plan with no terminal event of its own keeps the state
    its last event carried, and a run that ended without a verdict never grows
    a fabricated ``failed``.
    """
    lines = [
        {"ts": "2026-07-28T08:00:00.000Z", "plan": "sp-a", "stage": "plan",
         "state": "running", "detail": "go"},
        {"ts": "2026-07-28T08:00:01.000Z", "plan": "sp-a", "stage": "impl",
         "state": "running", "detail": "impl 1",
         "agent": {"id": "a" + "1" * 16, "label": "impl #1", "state": "running"}},
        {"ts": "2026-07-28T08:00:02.000Z", "plan": "sp-b", "stage": "plan",
         "state": "queued", "detail": "queued"},
        {"ts": "2026-07-28T08:00:03.000Z", "plan": "orchestrator",
         "stage": "complete", "state": "done", "detail": "closed - no verdict"},
    ]
    path = _write("sweep.jsonl", [json.dumps(x) for x in lines])

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        snap = json.loads(stream.snapshot_bytes())
        plans = dict(snap["plans"])
        assert plans["sp-a"]["state"] == "done", plans["sp-a"]["state"]
        assert plans["sp-b"]["state"] == "done", plans["sp-b"]["state"]
        assert plans["orchestrator"]["state"] == "done"
        assert "failed" not in json.dumps(snap), "nothing may fabricate a failure"
        agents = dict(plans["sp-a"]["agents"])
        row = agents["a" + "1" * 16]
        assert row["state"] == "stale", row       # frozen, not "done", not running
        assert dict(plans["sp-a"]["roles"])["impl"]["state"] == "stale"
        # /tasks keeps its own (badge-only) verdict, unchanged by the sweep
        assert stream.status()["status"] == "done", stream.status()

    _run(go())


def test_timeplan_classifies_idle_and_stall_like_the_page():
    """The mirrored TP thresholds do the same thing on both sides.

    Gaps classify by whether a plan run was open when they began: nothing open
    past TP_IDLE_MS is idle (between runs), open and silent past TP_STALL_MS is
    a stall. Quiet token ticks count as activity — folding them away is what
    invents outages that never happened (DATA-MODEL-8, PRIOR-ART-TOUCH-13).
    """
    def ev(sec, **kw):
        base = {"ts": "2026-07-28T08:%02d:%02d.000Z" % (sec // 60, sec % 60),
                "plan": "sp-a", "stage": "impl", "state": "running",
                "detail": "tick"}
        base.update(kw)
        return json.dumps(base)

    lines = [
        ev(0, stage="plan", state="running"),      # run opens
        ev(30),                                    # working
        ev(30 + 300),                              # 5 min silent WITH a run open
        ev(30 + 300 + 5, stage="plan", state="done"),
        ev(30 + 300 + 5 + 400),                    # >2 min with nothing open
    ]
    path = _write("timeplan.jsonl", lines)

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        tp = json.loads(stream.snapshot_bytes())["timeplan"]
        kinds = [s["kind"] for s in tp["segs"]]
        assert "down" in kinds, kinds       # the 5 min stall while open
        assert "idle" in kinds, kinds       # the gap with nothing open
        assert kinds[0] == "up", kinds
        summary = tp["summary"]
        assert summary["stallCount"] == 1, summary
        assert summary["stallMax"] >= 300_000, summary
        assert summary["idleMs"] >= 400_000, summary
        assert summary["live"] is False, summary
        assert summary["end"] - summary["t0"] == (30 + 300 + 5 + 400) * 1000
        assert len(tp["tailTicks"]) == len(lines), tp["tailTicks"]

    _run(go())


def test_timeplan_incremental_fold_equals_a_one_shot_fold():
    """The reorder window makes the incremental timeplan a left fold.

    The page sorts its ticks; a stream folded byte-by-byte cannot. Feeding the
    same ticks in one pass and in five appends must produce the same segments,
    or a resumed dashboard would draw a different strip from a fresh one.
    """
    lines = gen_stream.make_stream(3_000)
    one = _write("tp-one.jsonl", lines)
    many = _write("tp-many.jsonl", lines[:400])

    async def go():
        a = _fresh_stream(one)
        await a.refresh()
        b = _fresh_stream(many)
        for i in range(400, 3_000, 650):
            await b.refresh()
            _append(many, lines[i:i + 650])
        await b.refresh()
        ta = json.loads(a.snapshot_bytes())["timeplan"]
        tb = json.loads(b.snapshot_bytes())["timeplan"]
        assert ta["segs"] == tb["segs"], (len(ta["segs"]), len(tb["segs"]))
        assert ta["runs"] == tb["runs"]
        assert ta["summary"] == tb["summary"]

    _run(go())


def test_snapshot_is_cached_per_sig_and_offset():
    """PRIOR-ART-TOUCH-4 — never rebuilt per connect, only per new byte."""
    lines = gen_stream.make_stream(300)
    path = _write("snap-cache.jsonl", lines)

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        first = stream.snapshot_bytes()
        assert stream.snap_builds == 1, stream.snap_builds
        assert stream.snapshot_bytes() is first, "same bytes, no rebuild"
        assert stream.snap_builds == 1, stream.snap_builds
        _append(path, gen_stream.make_stream(20, seed=5))
        await stream.refresh()
        second = stream.snapshot_bytes()
        assert second is not first and stream.snap_builds == 2
        assert json.loads(second)["offset"] > json.loads(first)["offset"]

    _run(go())


def test_batch_frames_honour_both_caps():
    """M8(c) — batches close on the count cap AND on the byte cap."""
    small = [json.dumps({"i": i}).encode() for i in range(1_201)]
    frames = ms.batch_frames(small)
    assert len(frames) == 3, len(frames)
    counts = [len(json.loads(f)) for f in frames]
    assert counts == [ms.BATCH_MAX_EVENTS, ms.BATCH_MAX_EVENTS, 201], counts
    assert [ev["i"] for f in frames for ev in json.loads(f)] == list(range(1_201))

    fat = [json.dumps({"d": "x" * 20_000}).encode() for _ in range(40)]
    frames = ms.batch_frames(fat)
    assert all(len(f) <= ms.BATCH_MAX_BYTES for f in frames), \
        [len(f) for f in frames]
    assert sum(len(json.loads(f)) for f in frames) == 40
    assert len(frames) > 1, "the byte cap must bind before the count cap here"

    # a poisoned line only ever costs itself, never the batch around it — and
    # the filter is a separate step, so the caller knows how many events it
    # actually framed without re-parsing a frame (m-4)
    poisoned = [b'{"ok":1}', b'{not json', b'[1,2,3]', b'{"ok":2}']
    kept = ms.keep_parseable(poisoned)
    assert kept == [b'{"ok":1}', b'{"ok":2}'], kept
    assert len(json.loads(ms.batch_frames(kept)[0])) == 2
    assert ms.batch_frames([]) == []


def test_health_reports_the_stream_registry():
    """M12/SERVER-READ-11 — the read side's work is externally observable."""
    lines = gen_stream.make_stream(150)
    path = _write("health-streams.jsonl", lines)

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        health = ms.health_payload()
        assert health["status"] == "ok"
        assert "parse_failures" in health, health          # unchanged keys
        streams = health["streams"]
        assert stream.task in streams, sorted(streams)
        row = streams[stream.task]
        for key in ("offset", "events", "bytes_read", "events_folded", "refreshes",
                    "resets", "blob_bytes", "snapshot_bytes", "clients",
                    "last_refresh_ms"):
            assert key in row, key
        assert row["events"] == len(lines) and row["offset"] == os.path.getsize(path)
        assert row["bytes_read"] == os.path.getsize(path), row
        for key in ("ws_clients", "ws_active", "events_sent", "snapshots_sent",
                    "page_hits", "uptime_s", "fold_gen"):
            assert key in health["stats"], key
        assert health["stats"]["fold_gen"] == ms.FOLD_GEN
        # m-5: a v2 prelude delivers a FOLD of n events and bumps `events_sent`
        # by zero, so the two counters are published side by side rather than
        # leaving a v2-heavy deployment looking idle.
        assert "snapshots" in ms.stats_line(), ms.stats_line()

    _run(go())


def test_stats_line_reads_the_registry_not_the_disk():
    """SERVER-READ-14 — the shutdown line stopped re-reading a 5.6 MB file."""
    import inspect
    src = inspect.getsource(ms.stats_line)
    assert "open(" not in src, "stats_line must not touch the disk"
    assert "_REGISTRY" in src, src
    line = ms.stats_line()
    assert "events streamed" in line and "\n" not in line, line
    assert "streams folded" in line or "no stream folded" in line, line


def test_resolve_task_does_not_answer_an_unknown_name_with_the_default():
    """SERVER-READ-10 — the wrong-answer bug, at its source."""
    name, state_dir, known = ms.resolve_task("")
    assert known and state_dir == ms.STATE_DIR, (name, state_dir)
    name, state_dir, known = ms.resolve_task("task=no-such-task-xyz")
    assert not known and name == "no-such-task-xyz", (name, known)
    # the v1 websocket fallback is deliberately unchanged (compatibility floor)
    assert ms.resolve_task_dir("task=no-such-task-xyz") == ms.STATE_DIR


def test_tasks_payload_live_matches_the_full_scan():
    """`/tasks` from the registry is the same answer, cheaper.

    ``discover_tasks()`` covers the REAL task folders of this repo, and one of
    them may be a run that is appending right now — so the live fold and a full
    re-scan taken a millisecond apart can legitimately differ by the events in
    between. The equality is therefore bracketed: a full scan before, the live
    payload, a full scan after, and every field must match one END of that
    bracket. A genuine fold divergence matches neither, in any of three tries.
    """
    async def go():
        keys = ("status", "last", "tokens")
        for _attempt in range(3):
            before = {t["name"]: t for t in ms.tasks_payload()["tasks"]}
            live = await ms.tasks_payload_live()
            after_payload = ms.tasks_payload()
            after = {t["name"]: t for t in after_payload["tasks"]}
            assert live["default"] == after_payload["default"]
            assert {t["name"] for t in live["tasks"]} == set(after)
            drift = []
            for entry in live["tasks"]:
                name = entry["name"]
                got = tuple(entry[k] for k in keys)
                ends = [tuple(ref[k] for k in keys)
                        for ref in (before.get(name), after.get(name)) if ref]
                if got not in ends:
                    drift.append(name)
            if not drift:
                return
        raise AssertionError(f"live fold differs from the full scan: {drift}")

    _run(go())


def test_fold_gen_is_a_bare_integer_literal_in_the_source():
    """DATA-MODEL-9 — the literal the page's copy is compared against.

    The cross-file equality assert lives in test_frontend.py (it owns
    monitor.html); this half only guarantees there IS a plain integer literal
    here to compare with, in the documented form.
    """
    src = open(MODULE_PATH).read()
    assert isinstance(ms.FOLD_GEN, int), type(ms.FOLD_GEN)
    assert f"FOLD_GEN = {ms.FOLD_GEN}\n" in src, "FOLD_GEN must be a bare literal"
    # the timeplan thresholds are mirrored literals too, and part of the fold
    assert ms.TP_IDLE_MS == 120000 and ms.TP_STALL_MS == 240000
    assert f"TP_IDLE_MS = {ms.TP_IDLE_MS}" in src and f"TP_STALL_MS = {ms.TP_STALL_MS}" in src


def _gold_snapshot() -> str:
    """Build the golden snapshot payload deterministically (no clocks, no I/O)."""
    fold = ms.Fold()
    for line in gen_stream.make_stream(GOLD_N, seed=GOLD_SEED):
        fold.apply(line.encode())
    blob = ("\n".join(gen_stream.make_stream(GOLD_N, seed=GOLD_SEED)) + "\n").encode()
    sig = hashlib.sha256(blob[:ms.SIG_BYTES]).hexdigest()[:16]
    return json.dumps(fold.snapshot(sig, len(blob)), indent=1, sort_keys=True) + "\n"


def test_snapshot_matches_the_golden_fixture():
    """M9(a)/DATA-MODEL-9 — the fold's output, frozen with a sha256 manifest.

    The fixture is generated, not copied: ``gen_stream`` is deterministic in
    ``(n, seed)``, so the whole artifact is reproducible from two integers and
    costs the repo 200 KB once. It is the thing that turns "the snapshot schema
    changed" from a silent wire break into a red test. Regenerate deliberately
    — ``python3 test_server.py --write-gold`` — and bump FOLD_GEN in BOTH files
    if the change is a fold-rule change rather than an additive field.

    The module must stay usable outside this repo, so an absent fixture skips.
    """
    if not os.path.isfile(GOLD_SNAPSHOT):
        _skip("snapshot-gold.json: fixture absent (--write-gold to make it)")
        return
    want = open(GOLD_SNAPSHOT).read()
    assert _gold_snapshot() == want, "snapshot fold drifted from the golden fixture"
    manifest = open(GOLD_MANIFEST).read().split()
    digest = hashlib.sha256(want.encode()).hexdigest()
    assert digest == manifest[0], (digest, manifest[0])
    gold = json.loads(want)
    assert gold["foldGen"] == ms.FOLD_GEN, (gold["foldGen"], ms.FOLD_GEN)
    assert gold["evCount"] == GOLD_N and gold["plans"], gold["evCount"]


# --------------------------------------------------------------------------
# Attempt-2 regressions: the defects the attempt-1 suite was green over.
# Each case below reproduces one of them at unit level; the two that are only
# visible on the wire (the v1 replay overrun, the poisoned v2 tail batch) are
# pinned end to end in test_ws_e2e.py beside the v1 floor.
# --------------------------------------------------------------------------

def _tp_ev(sec, **kw) -> str:
    base = {"ts": "2026-07-28T%02d:%02d:%02d.000Z" % (8 + sec // 3600,
                                                      sec % 3600 // 60, sec % 60),
            "plan": "sp-a", "stage": "impl", "state": "running", "detail": "tick"}
    base.update(kw)
    return json.dumps(base)


def _tp_ms(sec) -> int:
    return ms.parse_ts_ms(json.loads(_tp_ev(sec))["ts"])


def _tp_client_refold(tp: dict, live_ticks: list, seed=None) -> dict:
    """The documented client rule, as code: slice, clip, seed, re-fold.

    ``TimePlan.build``'s contract verbatim — take ``segs[:atSegs]`` with the
    last one clipped to ``prevAt``, ``runs[:atRuns]``, seed ``open``/``prev``
    from the CHECKPOINT (``openAt``/``prevAt``, the state at ``tailFrom``), then
    fold ``tailTicks`` and the live ticks. Counts, not timestamps: the committed
    entries are a prefix of what ships, and a pending tick sharing a millisecond
    with the last committed one makes any time comparison ambiguous.

    ``seed`` overrides the checkpoint's open map, which is how the "the field is
    load-bearing" arms show what an empty one costs.
    """
    at_prev = tp["prevAt"]
    kept = []
    for s in tp["segs"][:tp["atSegs"]]:
        t1 = min(s["t1"], at_prev) if at_prev is not None else s["t1"]
        if t1 > s["t0"]:
            kept.append({"kind": s["kind"], "t0": s["t0"], "t1": t1})
    open_at = dict(tp["openAt"]) if seed is None else dict(seed)
    state = {"segs": kept, "runs": [dict(r) for r in tp["runs"][:tp["atRuns"]]],
             "open": open_at,
             "prev": at_prev,
             "t0": tp["summary"]["t0"]}
    for tick in list(tp["tailTicks"]) + live_ticks:
        ms._tp_step(state, tick)
    return state


def test_timeplan_ships_the_open_run_map_so_a_hydrated_strip_equals_a_replayed_one():
    """GD-F fidelity — ``open`` is state a client cannot re-derive.

    ``tpRender`` classifies a gap as idle or down from ``open.size`` and draws
    every still-open run as an in-flight bar. A snapshot cut while a run is
    open therefore has to carry the open map, or the hydrated strip renders a
    live stall as benign idle time and loses the running plan's bar for good
    (a later ``plan done`` closes nothing, because the plan was never open on
    the client's side).
    """
    head = [_tp_ev(0, stage="plan", state="running")]
    head += [_tp_ev(i * 10) for i in range(1, 81)]     # 800 s of working time
    tail = [_tp_ev(800 + 300),                          # 5 min silent, run OPEN
            _tp_ev(800 + 305, stage="plan", state="done")]
    cut_path = _write("tp-open-head.jsonl", head)
    all_path = _write("tp-open-all.jsonl", head + tail)

    async def go():
        a = _fresh_stream(cut_path)
        await a.refresh()
        tp = json.loads(a.snapshot_bytes())["timeplan"]
        assert tp["open"] == [["sp-a", _tp_ms(0)]], tp["open"]
        assert tp["summary"]["live"] is True, tp["summary"]
        # the opening tick is OLDER than the tail window: it cannot be
        # recovered from tailTicks, which is exactly why `open` has to ship
        assert all(t["t"] > _tp_ms(0) for t in tp["tailTicks"]), tp["tailTicks"]

        b = _fresh_stream(all_path)
        await b.refresh()
        full = json.loads(b.snapshot_bytes())["timeplan"]
        assert full["open"] == [], full["open"]
        live = [{"t": _tp_ms(1100)},
                {"t": _tp_ms(1105), "plan": "sp-a", "open": False}]

        hydrated = _tp_client_refold(tp, live)
        assert hydrated["segs"] == full["segs"], (hydrated["segs"], full["segs"])
        assert hydrated["runs"] == full["runs"], (hydrated["runs"], full["runs"])
        assert any(s["kind"] == "down" for s in hydrated["segs"]), hydrated["segs"]

        # ...and the field is load-bearing, not decoration: without it the same
        # bytes render the stall as idle and lose the run entirely.
        blind = _tp_client_refold(tp, live, seed={})
        assert blind["runs"] == [], blind["runs"]
        assert not any(s["kind"] == "down" for s in blind["segs"]), blind["segs"]

    _run(go())


def test_timeplan_ships_the_committed_checkpoint_not_just_the_end_state():
    """M-3 — a run that opens AND closes inside the tail window ships once.

    ``segs``/``runs``/``open`` describe the fold at the OFFSET; ``tailTicks``
    starts at ``tailFrom``. Re-folding the window onto the end state applies
    its own ticks a second time: a run whose open tick and close tick both live
    in the window is already in ``runs``, so the open tick re-opens it and the
    close tick appends a SECOND identical run. The checkpoint keys
    (``openAt``/``prevAt``/``atSegs``/``atRuns``) are the state at ``tailFrom``,
    which is the only instant the window can be replayed onto.

    Both cases are asserted together, because a fix that truncates ``runs`` at
    the boundary instead would LOSE the other one (a run opened before the
    window and closed inside it lives in neither ``runs[:atRuns]`` nor the end
    state's ``open`` map).
    """
    head = [_tp_ev(0, stage="plan", state="running")]      # sp-a opens at t=0
    head += [_tp_ev(i * 10) for i in range(1, 101)]        # 1,000 s of ticks
    tail = [_tp_ev(1010, plan="sp-b", stage="plan", state="running"),
            _tp_ev(1030, plan="sp-b", stage="plan", state="done"),
            _tp_ev(1040, stage="plan", state="done"),      # sp-a closes: before-
            _tp_ev(1050)]                                  # -the-window opener
    path = _write("tp-checkpoint.jsonl", head + tail)

    async def go():
        s = _fresh_stream(path)
        await s.refresh()
        tp = json.loads(s.snapshot_bytes())["timeplan"]
        # both runs are inside the tail window, so both are re-derivable...
        assert tp["tailTicks"], tp["tailTicks"]
        assert tp["tailFrom"] <= _tp_ms(1010), (tp["tailFrom"], _tp_ms(1010))
        assert len(tp["runs"]) == 2, tp["runs"]
        # ...and the checkpoint is BEFORE them: sp-a still open, sp-b unknown
        assert dict(tp["openAt"]) == {"sp-a": _tp_ms(0)}, tp["openAt"]
        assert tp["atRuns"] == 0, tp["atRuns"]
        assert tp["prevAt"] <= tp["tailFrom"], (tp["prevAt"], tp["tailFrom"])
        assert 0 <= tp["atSegs"] <= len(tp["segs"]), (tp["atSegs"], tp["segs"])

        hydrated = _tp_client_refold(tp, [])
        assert hydrated["runs"] == tp["runs"], (hydrated["runs"], tp["runs"])
        assert hydrated["segs"] == tp["segs"], (hydrated["segs"], tp["segs"])
        assert hydrated["open"] == {}, hydrated["open"]

        # the end-state map is what the OLD contract shipped; re-folding onto it
        # is exactly the duplication this test exists to forbid
        naive = {"segs": [dict(x) for x in tp["segs"]],
                 "runs": [dict(r) for r in tp["runs"]],
                 "open": dict(tp["open"]), "prev": tp["summary"]["end"],
                 "t0": tp["summary"]["t0"]}
        for tick in tp["tailTicks"]:
            ms._tp_step(naive, tick)
        assert len(naive["runs"]) > len(tp["runs"]), \
            "the end state cannot be the re-derivation seed"

    _run(go())


def test_the_tail_tick_window_has_a_hard_count_ceiling():
    """m-4 — TP_TAIL_MS bounds the window in time; a burst needs a count too."""
    tp = ms.TimePlan()
    t0 = _tp_ms(0)
    for i in range(4_000):
        tp.note(t0 + i)            # 4,000 ticks inside one millisecond-ish burst
    assert len(tp.pending) <= ms.TP_TAIL_MAX, len(tp.pending)
    built = tp.build()
    assert len(built["tailTicks"]) <= ms.TP_TAIL_MAX, len(built["tailTicks"])
    assert tp.segs, "the ceiling must have committed something"
    # Committing early must not change what the strip says — and proving that
    # needs a run where the ceiling did NOT fire, or the comparison is two runs
    # of the same code path (m-2). Raising the constant is what makes the second
    # run keep every tick pending until `build()` folds them in one pass.
    real_max = ms.TP_TAIL_MAX
    try:
        ms.TP_TAIL_MAX = 100_000
        one_shot = ms.TimePlan()
        for i in range(4_000):
            one_shot.note(t0 + i)
        assert len(one_shot.pending) == 4_000, len(one_shot.pending)
        assert one_shot.segs == [], "this run must commit nothing early"
        deferred = one_shot.build()
    finally:
        ms.TP_TAIL_MAX = real_max
    assert deferred["segs"] == built["segs"], (deferred["segs"], built["segs"])
    assert deferred["runs"] == built["runs"]
    assert deferred["summary"] == built["summary"]


def test_the_snapshot_truncation_disclosure_survives_a_cache_hit():
    """n-4 — the disclosure belongs to the BYTES, not to the fold."""
    async def go():
        fat = [json.dumps({"ts": "2026-07-28T08:00:%02d.000Z" % (i % 60),
                           "plan": "p%d" % (i % 30), "stage": "impl",
                           "state": "running", "detail": "d" * 200})
               for i in range(4_000)]
        big = _fresh_stream(_write("snap-trunc-a.jsonl", fat))
        await big.refresh()
        big.snapshot_bytes()
        assert big.snap_truncated is True, "a cut log is disclosed"
        small = _fresh_stream(_write("snap-trunc-b.jsonl", fat[:10]))
        await small.refresh()
        small.snapshot_bytes()
        assert small.snap_truncated is False, small.snap_truncated
        # a CACHE HIT must report the cached payload's disclosure, not whatever
        # the fold's attribute was left at by the last build anywhere
        big.fold.log_truncated = False
        assert big.snapshot_bytes() is big.snap_cache[1]
        assert big.snap_truncated is True, "cache hit lost the disclosure"

    _run(go())


def test_the_blob_offset_never_claims_bytes_it_did_not_frame():
    """m-5 — a blob is a COMPLETE byte prefix or the next v1 replay has a hole."""
    lines = gen_stream.make_stream(200)
    path = _write("blob-prefix.jsonl", lines)

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        async with stream.sync_lock():
            await stream.ensure_blob()
        assert stream.blob_offset == stream.offset == os.path.getsize(path)
        framed = stream.blob_framed

        # the pathological arm: the file becomes unreadable between the fold and
        # the blob build, so read_records returns nothing. The old code moved
        # blob_offset to the fold's offset anyway and the missing range was
        # never framed again — a silent gap in every later replay.
        stream.blob = bytearray()
        stream.blob_offset = stream.blob_lines = 0
        real = ms.read_records
        ms.read_records = lambda p, o: ([], o)
        try:
            async with stream.sync_lock():
                await stream.ensure_blob()
        finally:
            ms.read_records = real
        assert stream.blob_offset == 0, stream.blob_offset
        assert stream.blob_framed == framed, "nothing was framed"
        async with stream.sync_lock():
            await stream.ensure_blob()
        assert stream.blob_offset == stream.offset
        assert stream.blob_lines == len(lines), stream.blob_lines

    _run(go())


def test_the_blob_line_count_is_cleared_by_eviction_too():
    """m-1 — `_reset` and `_evict_blob` must agree, or events_sent inflates."""
    lines = gen_stream.make_stream(120)
    path = _write("blob-evict-lines.jsonl", lines)

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        async with stream.sync_lock():
            await stream.ensure_blob()
        assert stream.blob_lines == len(lines), stream.blob_lines
        stream.blob_subs = 0
        stream.blob_idle_at = ms.time.monotonic() - ms.BLOB_IDLE_SECS - 1
        await stream.refresh()
        assert len(stream.blob) == 0 and stream.blob_offset == 0
        assert stream.blob_lines == 0, stream.blob_lines
        async with stream.sync_lock():
            await stream.ensure_blob()
        assert stream.blob_lines == len(lines), \
            "a rebuilt blob counts its own lines, not the evicted one's too"

    _run(go())


def test_fold_apply_reports_whether_the_line_parsed():
    """M-1 — the fold is the ONE parser, so it is the one authority on poison."""
    fold = ms.Fold()
    good = json.dumps({"ts": "2026-07-28T08:00:00.000Z", "plan": "p",
                       "stage": "impl", "state": "running"}).encode()
    assert fold.apply(good) is True
    assert fold.apply(b'{"not json') is False
    assert fold.apply(b'[1, 2, 3]') is False          # JSON, but not an event
    assert fold.parse_failures == 2, fold.parse_failures
    assert fold.ev_count == 1, fold.ev_count


def test_agent_attempt_is_read_from_the_label_exactly_like_the_page():
    """n-3 — monitor.html:518-519: role falls back to the id, attempt never does."""
    fold = ms.Fold()
    fold.apply(json.dumps({"ts": "2026-07-28T08:00:00.000Z", "plan": "p",
                           "stage": "impl", "state": "running",
                           "agent": {"id": "impl #7", "state": "running"}}).encode())
    roles = fold.plans["p"]["roles"]
    assert list(roles) == ["impl"], list(roles)         # role: label or id
    assert roles["impl"]["attempt"] == 1, roles["impl"]  # attempt: label only
    fold.apply(json.dumps({"ts": "2026-07-28T08:00:01.000Z", "plan": "p",
                           "stage": "impl", "state": "running",
                           "agent": {"id": "a1", "label": "gate #3",
                                     "state": "done"}}).encode())
    assert fold.plans["p"]["roles"]["gate"]["attempt"] == 3
    # ...including the parts of parseInt that are easy to leave out: it skips
    # leading whitespace and takes one sign, and it stops at the first
    # non-ASCII-digit (n-1). Whatever the page reads off a label, this reads.
    cases = [("gate # 4", 4), ("gate #+5", 5), ("gate #6x", 6), ("gate # ", 1),
             ("gate #-2", 1), ("gate #٣", 1)]   # Math.max keeps 1 for both
    for i, (label, want) in enumerate(cases):
        f2 = ms.Fold()
        f2.apply(json.dumps({"ts": "2026-07-28T08:00:0%d.000Z" % i, "plan": "p",
                             "stage": "impl", "state": "running",
                             "agent": {"id": "a%d" % i, "label": label,
                                       "state": "running"}}).encode())
        got = f2.plans["p"]["roles"]["gate"]["attempt"]
        assert got == want, (label, got, want)


def test_health_keeps_every_stream_when_two_folders_share_a_basename():
    """n-2 — a name collision must not drop a stream from /health."""
    a = _write("collide-a.jsonl", gen_stream.make_stream(20))
    b = _write("collide-b.jsonl", gen_stream.make_stream(30))

    async def go():
        sa, sb = _fresh_stream(a), _fresh_stream(b)
        await sa.refresh()
        await sb.refresh()
        assert sa.task == sb.task, (sa.task, sb.task)   # same parent folder
        streams = ms.health_payload()["streams"]
        # The collision tie-breaker is the path DIGEST, not the path (F2).
        keys = (sa.task, ms.path_digest(sa.path), ms.path_digest(sb.path))
        rows = [r for k, r in streams.items() if k in keys]
        assert len(rows) >= 2, sorted(streams)
        assert sa.path not in streams and sb.path not in streams, sorted(streams)
        events = {r["events"] for r in rows}
        assert {sa.fold.ev_count, sb.fold.ev_count} <= events, (events, rows)

    _run(go())


class _FakeWriter:
    """A StreamWriter stand-in whose ``drain()`` yields on cue.

    The whole point of the v1 replay bug is what happens WHILE ``drain()`` is
    suspended, and a real socket only suspends when its send buffer fills —
    which is a property of the kernel, not of the code under test. Here the
    suspension is the test: the hook runs exactly once, at a known point.
    """

    def __init__(self, chunks: list, on_drain=None):
        self.chunks, self.on_drain = chunks, on_drain

    def write(self, data) -> None:
        self.chunks.append(bytes(data))

    async def drain(self) -> None:
        if self.on_drain is not None:
            await self.on_drain()


def test_v1_replay_never_overruns_the_history_it_captured():
    """B1 — an append during a v1 replay must not be sent twice.

    ``blob_end`` is captured under the stream lock, but a slice END that is not
    clamped to it reaches past it: ``drain()`` yields, the stream's tailer
    appends freshly framed lines to the same blob, and the last slice ships
    lines that are already queued for the tail. The client then folds them
    twice — double-counted tokens, duplicated log rows, duplicated ticks — on
    the compatibility floor that monitor.html still speaks today.
    """
    lines = gen_stream.make_stream(2_000)          # ~900 KB: several chunks
    path = _write("v1-overrun.jsonl", lines)
    extra = gen_stream.make_stream(30, seed=77)
    expected = sum(len(ms.ws_frame(ln.encode())) for ln in lines)
    assert expected > 2 * ms.WRITE_CHUNK, expected

    async def go():
        stream = _fresh_stream(path)
        chunks: list = []
        fired = {"n": 0}

        async def on_drain():
            if fired["n"]:
                return
            fired["n"] = 1
            _append(path, extra)
            await stream.refresh()     # folds, broadcasts AND extends the blob

        closed = asyncio.Event()
        task = asyncio.create_task(
            ms._stream_v1(_FakeWriter(chunks, on_drain), stream, closed))
        for _ in range(400):
            if task.done() or sum(len(c) for c in chunks) >= expected:
                break
            await asyncio.sleep(0.01)
        closed.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        joined = b"".join(chunks)
        assert fired["n"], "the drain hook never ran; the setup is wrong"
        assert stream.blob_lines == len(lines) + len(extra), stream.blob_lines
        assert len(joined) == expected, (len(joined), expected)
        for ln in extra:
            assert ln.encode() not in joined, "a TAIL line arrived in the replay"
        for ln in lines[:50] + lines[-50:]:
            assert joined.count(ln.encode()) == 1, ln[:60]

    _run(go())


def test_read_window_walks_a_prefix_without_materialising_it():
    """m-2 — the snap=0 replay reads in windows and never past the fold offset."""
    lines = gen_stream.make_stream(500)
    path = _write("window.jsonl", lines)
    size = os.path.getsize(path)
    _append(path, gen_stream.make_stream(50, seed=9))   # bytes past the fold

    seen, pos, windows = [], 0, 0
    while pos < size:
        recs, nxt = ms.read_window(path, pos, size, max_bytes=4096)
        assert nxt > pos, (pos, nxt)
        assert all(end <= size for _l, end in recs), recs[-1]
        seen += [line for line, _e in recs]
        pos = nxt
        windows += 1
    assert windows > 1, "the point is that it took several reads"
    assert seen == [ln.encode() for ln in lines], (len(seen), len(lines))
    assert pos == size, (pos, size)
    # a record longer than the window grows the read instead of skipping it
    fat = json.dumps({"ts": "2026-07-28T08:00:00.000Z", "plan": "p",
                      "detail": "x" * 9000})
    fat_path = _write("window-fat.jsonl", [fat])
    recs, nxt = ms.read_window(fat_path, 0, os.path.getsize(fat_path),
                               max_bytes=512)
    assert [line for line, _e in recs] == [fat.encode()], recs
    assert nxt == os.path.getsize(fat_path), nxt
    assert ms.read_window(path, size, size) == ([], size)


def test_a_young_stream_is_not_a_new_stream_on_every_append():
    """B2 — the sig hashes the WHOLE file below SIG_BYTES, so it is not an id.

    ``stream_sig`` reads ``min(size, 4096)`` bytes, so while a stream is younger
    than 4 KB — the first ~20-40 ``status.sh`` lines of EVERY run, i.e. exactly
    when someone opens the dashboard to watch it start — every append changes
    the digest. Comparing it there fires the protocol's one destructive signal
    on an ordinary append: full re-fold from 0, blob and snapshot dropped, and
    every subscribed socket closed (the truncation sentinel). The three-tier
    check keeps its teeth: ``size < offset`` and ``dev:ino`` still reset, and
    the cursor-continuity window still catches the same-header wipe-and-rerun.
    """
    lines = gen_stream.make_stream(200)
    path = _write("young.jsonl", lines[:1])
    assert os.path.getsize(path) < ms.SIG_BYTES, os.path.getsize(path)

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        assert stream.sig_short is True, "the head is shorter than SIG_BYTES"
        async with stream.sync_lock():
            sub = stream.subscribe()
        first_sig = stream.sig
        sigs = {first_sig}
        for i in range(1, 6):
            _append(path, lines[i:i + 1])
            assert os.path.getsize(path) < ms.SIG_BYTES, "still young"
            await stream.refresh()
            sigs.add(stream.sig)
            assert stream.resets == 0, f"append {i} was read as a new stream"
            assert sub.closed is False, f"append {i} closed a live socket"
            assert stream.fold.ev_count == i + 1, stream.fold.ev_count
        got = [line for batch in sub.queue for line, _end, _ok in batch]
        assert got == [ln.encode() for ln in lines[1:6]], len(got)
        assert len(sigs) > 1, "the digest does move; it is just not an identity"
        stream.unsubscribe(sub)

        # ...and once the head is settled, a changed sig IS an identity break
        _append(path, lines[6:])
        await stream.refresh()
        assert os.path.getsize(path) > ms.SIG_BYTES
        assert stream.sig_short is False and stream.resets == 0
        settled = stream.sig
        with open(path, "r+b") as f:      # same size, same inode, new head —
            f.seek(0)                     # only the sig can catch this one
            f.write(b" ")
        await stream.refresh()
        assert stream.sig != settled, "the head really did change"
        assert stream.resets == 1, "a settled head that changes is a new stream"
        assert stream.fold.ev_count == len(lines) - 1, stream.fold.ev_count
        ms.PARSE_FAILURES.pop(stream.path, None)     # the byte we broke

    _run(go())


def test_a_hopeless_subscriber_is_closed_at_a_cap_that_clears_one_burst():
    """n-3 — the queue cap is a bound on RAM, not a hazard for a real tick.

    A subscriber that stops draining holds every line the stream reads, so the
    cap has to be low enough to matter (~9 MB of retained line bytes here,
    where 200,000 was ~90 MB — an order past the ~12 MB/client pathology
    SERVER-READ-5 measured) and high enough to clear the largest LEGITIMATE
    single broadcast: one refresh hands a subscriber everything it read in one
    step, and the e2e burst case appends 6,000 events at once.
    """
    stream = _fresh_stream(_write("pending-cap.jsonl", gen_stream.make_stream(20)))
    assert ms.MAX_PENDING_EVENTS > ms.MAX_TICK_EVENTS, ms.MAX_PENDING_EVENTS
    assert ms.MAX_PENDING_EVENTS >= 10_000, "one 6,000-event append must fit"
    sub = ms.Subscriber()
    stream.subscribers.add(sub)
    batch = [(b'{"ts":"1"}', i, True) for i in range(ms.MAX_TICK_EVENTS)]
    ticks = 0
    while not sub.closed:
        stream._broadcast(batch)
        ticks += 1
        assert ticks < 100, (ticks, sub.pending_events)
    assert ticks > 1, "one legitimate max-size tick must never close a socket"
    assert sub.pending_events > ms.MAX_PENDING_EVENTS, sub.pending_events
    assert sub.closed is True, "a hopeless reader must be cut loose"
    assert sub not in stream.subscribers, "and dropped from the broadcast set"


def test_v1_refuses_a_connection_whose_blob_is_not_a_complete_prefix():
    """m-3 — replay ends at ``blob_offset``; the tail starts at the fold's.

    Those are the same byte on every healthy path, and the m-5 fix (a blob may
    no longer CLAIM a prefix it did not write) is what makes them able to
    differ: an unreadable file mid-build leaves the blob behind the fold. The
    old code subscribed anyway, so the client got ``[0, blob_offset)`` and then
    ``[offset, ...)`` with the range between them silently never delivered.
    """
    lines = gen_stream.make_stream(120)
    path = _write("v1-prefix-guard.jsonl", lines)

    async def go():
        stream = _fresh_stream(path)
        await stream.refresh()
        chunks: list = []
        real = ms.read_records
        ms.read_records = lambda p, o: ([], o)       # the build reaches nothing
        try:
            await ms._stream_v1(_FakeWriter(chunks), stream, asyncio.Event())
        finally:
            ms.read_records = real
        assert chunks == [], "no partial history may go out"
        assert stream.subscribers == set(), "and no tail may be started"
        assert stream.blob_offset == 0 and stream.offset > 0

        # the healthy path is unchanged: prefix complete, everything delivered
        chunks = []
        closed = asyncio.Event()
        task = asyncio.create_task(
            ms._stream_v1(_FakeWriter(chunks), stream, closed))
        await asyncio.sleep(0.05)
        closed.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert stream.blob_offset == stream.offset == os.path.getsize(path)
        assert b"".join(chunks) == b"".join(ms.ws_frame(ln.encode())
                                            for ln in lines)

    _run(go())


def test_the_cold_fold_reads_a_long_stream_in_bounded_windows():
    """n-4 — a reset must not materialise the whole file (M9's own caution).

    ``_scan``'s reset arm used to ``f.read()`` everything and hand back one list
    of records: ~45 MB plus list overhead at the 100k headroom target, all live
    at once on the thread pool. Now each step reads at most ``SCAN_WINDOW``,
    folds it, broadcasts it, and says whether more remains — and the fold that
    comes out the other end is byte-for-byte the same one.
    """
    lines = gen_stream.make_stream(600)
    path = _write("cold-window.jsonl", lines)
    size = os.path.getsize(path)
    window = max(1024, size // 7)

    steps, offset, known = 0, 0, {}
    while True:
        res = ms._scan(path, offset, known, max_bytes=window)
        assert len(res["records"]) > 0, res
        span = res["new_offset"] - (0 if res["reset"] else offset)
        assert span <= window + ms.TAIL_CHECK_BYTES, (span, window)
        offset = res["new_offset"]
        known = {"sig": res["sig"], "sig_short": res["sig_short"],
                 "dev_ino": res["dev_ino"], "mtime_ns": res["mtime_ns"],
                 "tail": res["tail"]}
        steps += 1
        if not res["more"]:
            break
        assert steps < 50, "the walk must terminate"
    assert steps >= 5, f"the point is that it took several bounded reads: {steps}"
    assert offset == size, (offset, size)

    async def go():
        stream = _fresh_stream(path)
        real_window = ms.SCAN_WINDOW
        real_scan, calls = ms._scan, {"n": 0}

        def counted(*a, **kw):
            calls["n"] += 1
            return real_scan(*a, **kw)

        ms.SCAN_WINDOW, ms._scan = window, counted
        try:
            await stream.refresh()             # ONE call, several steps
        finally:
            ms.SCAN_WINDOW, ms._scan = real_window, real_scan
        assert calls["n"] == steps, (calls["n"], steps)
        assert stream.offset == size, (stream.offset, size)
        assert stream.refreshes == 1, "a windowed cold fold is still one refresh"
        assert stream.bytes_read == size, stream.bytes_read
        assert stream.events_folded == len(lines), stream.events_folded
        ref_states, ref_last, ref_tok, _f = ms.replay_plan_states(path)
        assert stream.fold.plan_states == ref_states
        assert stream.fold.last == ref_last and stream.fold.tok == ref_tok
        # a record longer than the window grows the read instead of stalling
        fat = json.dumps({"ts": "2026-07-28T08:00:00.000Z", "plan": "p",
                          "stage": "impl", "state": "running",
                          "detail": "x" * 5_000})
        fat_path = _write("cold-window-fat.jsonl", [fat])
        res = ms._scan(fat_path, 0, {}, max_bytes=256)
        assert [r[0] for r in res["records"]] == [fat.encode()], res["records"]
        assert res["more"] is False and res["new_offset"] == \
            os.path.getsize(fat_path)

        # ...and it still grows when the window opens with the continuity
        # prefix, whose own trailing newline would otherwise satisfy the growth
        # loop and defer the oversized record forever.
        _append(fat_path, [fat.replace("x" * 5_000, "y" * 5_000)])
        fat_stream = _fresh_stream(fat_path)
        ms.SCAN_WINDOW = 256
        try:
            await fat_stream.refresh()
            assert fat_stream.offset == os.path.getsize(fat_path), \
                fat_stream.offset
            assert fat_stream.fold.ev_count == 2, fat_stream.fold.ev_count
            _append(fat_path, [fat.replace("x" * 5_000, "z" * 5_000)])
            await fat_stream.refresh()               # now WITH a continuity tail
            assert fat_stream.fold.ev_count == 3, fat_stream.fold.ev_count
            assert fat_stream.offset == os.path.getsize(fat_path)
        finally:
            ms.SCAN_WINDOW = real_window

    _run(go())


def test_repo_fixture_resolution_is_anchored():
    """The walk-up finds the repo corpus, and REFUSES a stranger's lookalike.

    Without this arm the `None` branch would be dead code everywhere it is
    gated: `tests/fixtures/**` is tracked, so it is present in the working tree
    AND in a `git archive` checkout, and both layouts resolve. Here the resolver
    runs against synthetic trees so both outcomes are actually exercised.
    """
    with tempfile.TemporaryDirectory(dir=_TMP_BASE) as td:
        # 1. nothing above at all -> None
        deep = os.path.join(td, "empty", "a", "b")
        os.makedirs(deep)
        assert _find_repo_fixtures(deep) is None, "bare tree must not resolve"

        # 2. a LOOKALIKE: tests/fixtures/legacy with no frozen manifest above
        #    it — someone else's project, or a home dir that happens to hold
        #    one. Must be refused, not replayed.
        fake = os.path.join(td, "stranger")
        os.makedirs(os.path.join(fake, "tests", "fixtures", "legacy"))
        start = os.path.join(fake, "shared", "monitoring", "tests")
        os.makedirs(start)
        assert _find_repo_fixtures(start) is None, \
            "a tests/fixtures/legacy without the frozen manifest is not our corpus"

        # 3. ...and one whose MANIFEST.sha256 lists only its own snapshot (the
        #    shape of THIS module's fixtures dir) is refused for the same reason.
        with open(os.path.join(fake, "tests", "fixtures", "MANIFEST.sha256"), "w") as fh:
            fh.write("0" * 64 + "  snapshot-gold.json\n")
        assert _find_repo_fixtures(start) is None, \
            "a manifest that names no legacy/ path is not the repo corpus"

        # 4. the real shape resolves, from any depth below it
        with open(os.path.join(fake, "tests", "fixtures", "MANIFEST.sha256"), "a") as fh:
            fh.write("1" * 64 + "  legacy/touch-full-recon-events.jsonl\n")
        got = _find_repo_fixtures(start)
        assert got == os.path.join(fake, "tests", "fixtures", "legacy"), got

    # 5. and in THIS checkout it resolves to the repo's own corpus (or to None
    #    in a packaged copy that ships no corpus — both are legitimate).
    if _FIXTURES is not None:
        assert os.path.isdir(_FIXTURES), _FIXTURES
        assert _is_repo_corpus(os.path.dirname(_FIXTURES))


# --------------------------------------------------------------------------
# I10-I12/I14 — the FILE PLANE: the memory routes, their auth and transport
# rules, the G7 write path, `/health`'s memory block.
#
# Every arm here drives `ms.handle` over a real loopback socket (the module's
# own HTTP parsing is half of what is under test: methods, bodies, 405, 411,
# 415), with `ms.MEMORY_ROOT` / `ms.MEMORY_WRITE` swapped to a throwaway tree —
# never the repo's own `.touch/memory`, which a session actually reads.
#
# The consolidated end-to-end pass (both postures, the node+vm page harness, the
# cross-server `FILE_CSP` equality check) is a separate additive file by GD-U6;
# what lives here is the per-rule coverage of THIS module's own code.
# --------------------------------------------------------------------------


def _memory_tree(prefix="memtest-"):
    """A throwaway `<base>/.touch/memory`, returned with its base."""
    base = tempfile.mkdtemp(prefix=prefix, dir=_TMP_BASE)
    return base, os.path.join(base, ".touch", "memory")


class _memory_root:
    """Point the module at a throwaway memory root, with the plane on or off.

    A context manager and not a fixture: `MEMORY_ROOT`/`MEMORY_WRITE` are
    module globals read at CALL time (which is what lets an operator's flag and
    a test's temp tree use the same code path), so every arm must put them back.
    """

    def __init__(self, root, write=True):
        self.root = root
        self.write = write

    def __enter__(self):
        self.saved = (ms.MEMORY_ROOT, ms.MEMORY_WRITE)
        ms.MEMORY_ROOT, ms.MEMORY_WRITE = self.root, self.write
        # `/health`'s alignment answer is memoised per root for a couple of
        # seconds (MEMORY_HEALTH_TTL); a test tree is short-lived and may reuse a
        # path, so the cache is dropped on the way in AND out — no arm may inherit
        # another arm's answer.
        ms._MEMORY_ALIGN_CACHE.clear()
        return self.root

    def __exit__(self, *exc):
        ms.MEMORY_ROOT, ms.MEMORY_WRITE = self.saved
        ms._MEMORY_ALIGN_CACHE.clear()
        return False


def _http(method, target, headers=(), body=None, query_token=False,
          header_token=True, origin=True, content_type="application/json",
          write_marker=None):
    """One request over a real socket. Returns `(status, headers, body bytes)`.

    The defaults are what the page sends for a WRITE: the token in
    `X-Orch-Token`, `X-Touch-Write: 1`, a same-origin `Origin` built from the
    ephemeral port, and a JSON content type. Every one of them is switchable,
    because each is a rule with its own arm below.
    """
    if write_marker is None:
        write_marker = method in ("POST", "PUT", "DELETE")

    async def run():
        server = await asyncio.start_server(ms.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            path = target
            if query_token:
                path += ("&" if "?" in path else "?") + f"token={ms.TOKEN}"
            head = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}",
                    "Connection: close"]
            if header_token:
                head.append(f"X-Orch-Token: {ms.TOKEN}")
            if write_marker:
                head.append("X-Touch-Write: 1")
            if origin is True:
                head.append(f"Origin: http://127.0.0.1:{port}")
            elif origin:
                head.append(f"Origin: {origin}")
            raw = b"" if body is None else json.dumps(body).encode()
            if raw:
                if content_type:
                    head.append(f"Content-Type: {content_type}")
                head.append(f"Content-Length: {len(raw)}")
            elif content_type and method in ("POST", "PUT"):
                head.append(f"Content-Type: {content_type}")
            head.extend(headers)
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + raw)
            await writer.drain()
            data = await asyncio.wait_for(reader.read(-1), 10)
            writer.close()
            top, _, payload = data.partition(b"\r\n\r\n")
            lines = top.decode("latin1").split("\r\n")
            status = int(lines[0].split()[1])
            got = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    got[key.strip().lower()] = value.strip()
            return status, got, payload
        finally:
            server.close()
            await server.wait_closed()

    return _run(run())


def _json(out):
    """The JSON body of a `_http` answer, asserting it IS json (UI-1/UI-4)."""
    status, headers, body = out
    assert "application/json" in headers.get("content-type", ""), \
        (status, headers.get("content-type"), body[:80])
    assert headers.get("cache-control") == "no-store", headers
    for key in headers:
        assert not key.startswith("access-control-"), \
            f"the memory group must never emit CORS headers ({key})"
    return status, json.loads(body)


def _seed(root, name, text):
    """Write a memory file the way anything but this server would."""
    os.makedirs(root, mode=0o700, exist_ok=True)
    with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
        handle.write(text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_memory_routes_dispatch_on_the_method():
    """SERVER-1/SECURITY-2: a known route on the wrong method is 405 + Allow.

    The bug this closes is not hypothetical: `POST /tasks` and `DELETE /` both
    answered as GETs, so a `/memory/file?...&op=delete`-shaped URL in the address
    bar would have been a clickable, prefetchable, `<img src>`-able mutation.
    """
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            status, body = _json(_http("POST", "/api/memory/list",
                                       body={"content": "x\n"}))
            assert status == 405, (status, body)
            assert body["allow"] == ["GET"], body
            status, headers, _ = _http("PATCH", "/api/memory/file?name=a.md",
                                       body={"content": "x\n"})
            assert status == 405, status
            allow = headers.get("allow", "")
            for verb in ("GET", "POST", "PUT", "DELETE"):
                assert verb in allow, (verb, allow)
            # ...and a GET on the file route is a READ, never a delete: the table
            # is keyed by (method, route), so there is no verb smuggling.
            _seed(root, "a.md", "hi\n")
            status, body = _json(_http("GET", "/api/memory/file?name=a.md",
                                       query_token=True, header_token=False))
            assert status == 200 and body["content"] == "hi\n", body
            assert os.path.isfile(os.path.join(root, "a.md"))
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_an_unknown_memory_route_is_a_json_404_not_the_dashboard():
    """SERVER-7/UI-1: the HTML fallback must never own a path under the prefix.

    `GET /nope` still serves the page — that is the existing behaviour and out of
    scope (SERVER-1b) — but a `fetch` typo under `/api/memory/` used to get 200
    + 151 KB of HTML, which a client's `res.json()` turns into a silent empty
    render.
    """
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            status, body = _json(_http("GET", "/api/memory/fiel?name=a.md"))
            assert status == 404 and body["category"] == "unknown-route", body
            status, body = _json(_http("GET", "/api/memory"))
            assert status == 404, (status, body)
            # the untouched fallback, for contrast
            status, headers, page = _http("GET", "/nope", query_token=True)
            assert status == 200 and "text/html" in headers["content-type"]
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_a_memory_write_needs_a_header_token_a_marker_and_an_origin():
    """G5/W2/W3/W4: four independent gates, each refused on its own."""
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            payload = {"content": "hi\n"}
            # 1. the token may NOT ride in the query string on a write (W4).
            #    ...and the API's own 401 is JSON, so the page raises its auth
            #    banner instead of reporting "this build has no memory API".
            status, body = _json(_http(
                "PUT", "/api/memory/file?name=a.md", body=payload,
                query_token=True, header_token=False))
            assert status == 401, (status, body)
            assert body["category"] == "unauthorized", body
            assert "X-Orch-Token" in body["reason"], body
            # ...while the same URL is fine for a READ.
            _seed(root, "a.md", "hi\n")
            status, _ = _json(_http("GET", "/api/memory/file?name=a.md",
                                    query_token=True, header_token=False))
            assert status == 200, status
            # 2. a foreign Origin is refused (DNS rebinding / cross-site post).
            status, body = _json(_http("PUT", "/api/memory/file?name=a.md",
                                       body=payload, origin="http://evil.example"))
            assert status == 403 and body["category"] == "origin", body
            # 3. an ABSENT Origin is fine on a read and refused on a write (W3).
            status, _ = _json(_http("GET", "/api/memory/list", origin=False))
            assert status == 200, status
            status, body = _json(_http("PUT", "/api/memory/file?name=a.md",
                                       body=payload, origin=False))
            assert status == 403 and body["category"] == "origin", body
            # 4. and the custom marker header a simple cross-origin request
            #    cannot set (W2).
            status, body = _json(_http("PUT", "/api/memory/file?name=a.md",
                                       body=payload, write_marker=False))
            assert status == 403 and body["category"] == "write-marker", body
            assert open(os.path.join(root, "a.md")).read() == "hi\n", \
                "no refused write may have touched the file"
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_requires_write_auth_is_a_positive_predicate_over_the_route_table():
    """SECURITY-16/W4: not `route not in OPEN_ROUTES`, which is fail-open."""
    assert ms.requires_write_auth("PUT", "/api/memory/file") is True
    assert ms.requires_write_auth("post", "/api/memory/file") is True
    assert ms.requires_write_auth("DELETE", "/api/memory/file") is True
    assert ms.requires_write_auth("GET", "/api/memory/file") is False
    assert ms.requires_write_auth("GET", "/api/memory/list") is False
    assert ms.requires_write_auth("PUT", "/tasks") is False
    # derived FROM the table, so a new write entry is covered the moment it exists
    for (method, route), op in ms.MEMORY_ROUTES.items():
        assert ms.requires_write_auth(method, route) is (op in ms.MEMORY_WRITE_OPS)
    assert ms.OPEN_ROUTES == frozenset({"/health"}), \
        "the memory group must not have widened the open-route set"


def test_the_memory_body_reader_is_bounded_and_explicit():
    """SERVER-2/SECURITY-14/W9: 411 without a length, 413 over the cap, 400 chunked."""
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            status, body = _json(_http("POST", "/api/memory/file?name=a.md"))
            assert status == 411 and body["category"] == "no-length", body
            status, body = _json(_http(
                "POST", "/api/memory/file?name=a.md",
                headers=[f"Content-Length: {ms.MAX_MEMORY_BODY_BYTES + 1}"]))
            assert status == 413 and body["category"] == "body-too-large", body
            assert not os.path.exists(os.path.join(root, "a.md")), \
                "an over-cap body must be refused BEFORE it is read"
            status, body = _json(_http("POST", "/api/memory/file?name=a.md",
                                       body={"content": "x\n"},
                                       headers=["Transfer-Encoding: chunked"]))
            assert status == 400 and body["category"] == "chunked", body
            status, body = _json(_http("POST", "/api/memory/file?name=a.md",
                                       body={"content": "x\n"},
                                       content_type="text/plain"))
            assert status == 415 and body["category"] == "content-type", body
            # a length longer than the bytes that arrive is a 400, never a
            # truncated instruction file
            async def short():
                server = await asyncio.start_server(ms.handle, "127.0.0.1", 0)
                port = server.sockets[0].getsockname()[1]
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    head = ["POST /api/memory/file?name=a.md HTTP/1.1",
                            f"Host: 127.0.0.1:{port}", "Connection: close",
                            f"X-Orch-Token: {ms.TOKEN}", "X-Touch-Write: 1",
                            f"Origin: http://127.0.0.1:{port}",
                            "Content-Type: application/json",
                            "Content-Length: 400"]
                    writer.write(("\r\n".join(head) + "\r\n\r\n").encode()
                                 + b'{"content": "x')
                    await writer.drain()
                    writer.write_eof()
                    data = await asyncio.wait_for(reader.read(-1), 10)
                    writer.close()
                    return int(data.split(b" ")[1])
                finally:
                    server.close()
                    await server.wait_closed()
            assert _run(short()) == 400, "a short body must be a 400"
            assert not os.path.exists(os.path.join(root, "a.md"))
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_status_text_never_labels_an_unnamed_status_ok():
    """SERVER-8: `STATUS_TEXT.get(status, "OK")` would send `409 OK`."""
    assert ms.status_text(409) == "Conflict"
    assert ms.status_text(411) == "Length Required"
    assert ms.status_text(422) == "Unprocessable Content"
    assert ms.status_text(599) == "Server Error"
    assert ms.status_text(299) == "Success"
    assert "OK" not in ms.status_text(599)
    # ...and the fallback is DERIVED from the code (its class), so it cannot
    # contradict the status the way a hand-written default did.
    src = open(MODULE_PATH, encoding="utf-8").read()
    assert "_STATUS_CLASS.get(status // 100" in src, src
    for status in (200, 201, 400, 401, 403, 404, 405, 409, 411, 412, 413, 415,
                   422, 503):
        assert status in ms.STATUS_TEXT, status


def test_the_flat_namespace_kills_traversal_and_config_shaped_names():
    """G7 step 1 / W7: refused BEFORE any filesystem call."""
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            _seed(root, "a.md", "hi\n")
            for name in ("..%2f..%2fserver.json", "../../server.json",
                         "a/b.md", "settings.json", "settings.local.json",
                         "hook.py", "run.sh", ".hidden.md", "notes.txt",
                         "x" * 70 + ".md", "", "MEMORY.md%00.txt"):
                status, body = _json(_http(
                    "GET", "/api/memory/file?name=" + name, query_token=True,
                    header_token=False))
                assert status == 400, (name, status, body)
                assert body["category"] == "bad-name", (name, body)
            # ...and the same rule guards a write, so nothing lands outside
            status, body = _json(_http("POST", "/api/memory/file?name=../evil.md",
                                       body={"content": "x\n"}))
            assert status == 400 and body["category"] == "bad-name", body
            assert not os.path.exists(os.path.join(base, ".touch", "evil.md"))
            assert sorted(os.listdir(root)) == ["a.md"], os.listdir(root)
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)
    # ...and G7 step 1 is "not path handling ... before any filesystem call", so a
    # refused name on a FRESH checkout must leave no filesystem effect at all —
    # not even the memory root the write would have created (M-2, attempt 1: the
    # seeded root above cannot see this, which is why it gets its own tree).
    base, root = _memory_tree(prefix="memfresh-")
    try:
        with _memory_root(root):
            assert not os.path.isdir(root), "the fresh tree must start absent"
            status, body = _json(_http("POST", "/api/memory/file?name=../evil.md",
                                       body={"content": "x\n"}))
            assert status == 400 and body["category"] == "bad-name", body
            assert not os.path.isdir(root), \
                "a refused name must not create the memory root as a side effect"
            assert not os.path.isdir(os.path.dirname(root)), \
                "...nor its .touch/ parent"
            # ...while a legitimate create on the same fresh tree DOES build it,
            # so the arm above is about ordering and not about a disabled writer
            status, body = _json(_http("POST", "/api/memory/file?name=first.md",
                                       body={"content": "x\n"}))
            assert status == 201, (status, body)
            assert os.path.isfile(os.path.join(root, "first.md"))
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_a_planted_symlink_is_refused_and_never_followed():
    """G7 step 2 / SERVER-5 / W6: a symlink in the memory dir is not a memory file.

    Any local process — including an agent — can plant one, and following it
    turns a memory save into an arbitrary-file overwrite.
    """
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            os.makedirs(root, mode=0o700, exist_ok=True)
            outside = os.path.join(base, "outside.md")
            with open(outside, "w") as handle:
                handle.write("do not touch\n")
            os.symlink(outside, os.path.join(root, "link.md"))
            status, body = _json(_http("GET", "/api/memory/file?name=link.md",
                                       query_token=True, header_token=False))
            assert status == 409 and body["category"] == "symlink", body
            status, body = _json(_http("PUT", "/api/memory/file?name=link.md",
                                       body={"content": "owned\n",
                                             "ifMatch": "0" * 64}))
            assert status == 409 and body["category"] == "symlink", body
            assert open(outside).read() == "do not touch\n", "the link was followed"
            # ...and the row for it is listed, honestly unwritable, WITHOUT
            # publishing the target's size or mtime.
            status, listing = _json(_http("GET", "/api/memory/list"))
            row = [r for r in listing["files"] if r["name"] == "link.md"][0]
            assert row["writable"] is False and "symlink" in row["reason"], row
            assert row["size"] == 0 and row["mtime_ns"] == 0, row
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_the_read_path_refuses_home_claude_and_a_plugin_cache():
    """G7 step 3 / PROTOCOL-7 / Part D-9: `~/.claude` is a read-only tap, always.

    The refusal is an explicit ancestor check and not a consequence of the
    containment rule, so it survives a memory root that is itself configured to
    point inside `~/.claude` — which is exactly what a hand-written
    `autoMemoryDirectory` might do.
    """
    base = tempfile.mkdtemp(prefix="memhome-", dir=_TMP_BASE)
    saved_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = base
        inside = os.path.join(base, ".claude", "projects", "x", "memory")
        os.makedirs(inside, mode=0o700)
        try:
            ms.safe_memory_path(inside, "MEMORY.md")
            raise AssertionError("a root under ~/.claude must be refused")
        except ms.MemoryRefusal as exc:
            assert exc.status == 403 and exc.category == "home-claude", exc.category
        # a plugin cache, by the same shape (W8, PROTOCOL-6, Part D-8)
        cache = os.path.join(base, "cache", "touch", "0.2.0")
        os.makedirs(os.path.join(cache, ".claude-plugin"), exist_ok=True)
        with open(os.path.join(cache, ".claude-plugin", "plugin.json"), "w") as handle:
            handle.write('{"name":"touch"}')
        cached_root = os.path.join(cache, ".touch", "memory")
        try:
            ms.safe_memory_path(cached_root, "MEMORY.md")
            raise AssertionError("a root inside a plugin cache must be refused")
        except ms.MemoryRefusal as exc:
            assert exc.status == 403 and exc.category == "plugin-cache", exc.category
        # ...and a legitimate root still resolves, so the arms above mean something
        ok = os.path.join(base, "proj", ".touch", "memory")
        os.makedirs(ok, mode=0o700)
        assert ms.safe_memory_path(ok, "MEMORY.md") == os.path.join(
            os.path.realpath(ok), "MEMORY.md")
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_a_plugin_cache_root_disables_the_whole_family():
    """SERVER-16: the family refuses loudly rather than writing into a cache."""
    base = tempfile.mkdtemp(prefix="memcache-", dir=_TMP_BASE)
    try:
        os.makedirs(os.path.join(base, ".claude-plugin"), exist_ok=True)
        with open(os.path.join(base, ".claude-plugin", "plugin.json"), "w") as handle:
            handle.write('{"name":"touch"}')
        root = os.path.join(base, "0.2.0", ".touch", "memory")
        with _memory_root(root):
            assert "plugin cache" in ms.memory_unavailable()
            status, body = _json(_http("GET", "/api/memory/list"))
            assert status == 503 and body["category"] == "memory-unavailable", body
            status, body = _json(_http("POST", "/api/memory/file?name=a.md",
                                       body={"content": "x\n"}))
            assert status == 503, (status, body)
            assert not os.path.exists(root), "nothing may be created in a cache"
            health = ms.health_payload()["memory"]
            assert health["present"] is False and health["writable"] is False
        # ...and an unresolved project is the same shape with its own sentence
        with _memory_root(""):
            status, body = _json(_http("GET", "/api/memory/list"))
            assert status == 503, status
            assert "no project root" in body["reason"], body
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_the_write_plane_is_off_by_default():
    """G6/W14/SECURITY-1: reads live, writes refused, and the list says why."""
    base, root = _memory_tree()
    try:
        with _memory_root(root, write=False):
            _seed(root, "MEMORY.md", "# index\n")
            status, listing = _json(_http("GET", "/api/memory/list"))
            assert status == 200, status
            assert listing["memoryWrite"] is False, listing
            # `writable` is the ROOT's own answer and stays true: the page words
            # its disabled affordance from whichever of the two is false, so
            # folding them would print the wrong reason.
            assert listing["writable"] is True, listing
            assert listing["files"][0]["writable"] is False, listing["files"]
            for method, target, body in (
                    ("POST", "/api/memory/file?name=new.md", {"content": "x\n"}),
                    ("PUT", "/api/memory/file?name=MEMORY.md",
                     {"content": "x\n", "ifMatch": "0" * 64}),
                    ("DELETE", "/api/memory/file?name=MEMORY.md&ifMatch=" + "0" * 64,
                     None)):
                status, payload = _json(_http(method, target, body=body))
                assert status == 403, (method, status, payload)
                assert payload["category"] == "write-plane-off", payload
            assert open(os.path.join(root, "MEMORY.md")).read() == "# index\n"
            assert not os.path.exists(os.path.join(root, "new.md"))
            assert ms.health_payload()["memoryWrite"] == "off"
        with _memory_root(root, write=True):
            assert ms.health_payload()["memoryWrite"] == "on"
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_the_write_flag_is_read_from_argv_and_the_env():
    """The default-off decision has exactly two documented ways to be turned on."""
    saved_argv, saved_env = list(sys.argv), os.environ.get("TOUCH_ALLOW_MEMORY_WRITE")
    try:
        os.environ.pop("TOUCH_ALLOW_MEMORY_WRITE", None)
        sys.argv = ["monitor_server.py"]
        assert ms.memory_write_enabled() is False
        sys.argv = ["monitor_server.py", "--allow-memory-write"]
        assert ms.memory_write_enabled() is True
        # ...and the flag is not mistaken for a port (it starts with `-`)
        assert ms.positional_args() == []
        sys.argv = ["monitor_server.py"]
        for value, want in (("1", True), ("on", True), ("true", True),
                            ("0", False), ("", False), ("nope", False)):
            os.environ["TOUCH_ALLOW_MEMORY_WRITE"] = value
            assert ms.memory_write_enabled() is want, (value, want)
    finally:
        sys.argv = saved_argv
        if saved_env is None:
            os.environ.pop("TOUCH_ALLOW_MEMORY_WRITE", None)
        else:
            os.environ["TOUCH_ALLOW_MEMORY_WRITE"] = saved_env


def test_the_write_path_is_atomic_optimistic_and_0600():
    """G7 steps 4/5/6 + SECURITY-15: create, save, the two refusals, the modes."""
    import stat as stat_mod
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            status, created = _json(_http("POST", "/api/memory/file?name=MEMORY.md",
                                          body={"content": "# index\n"}))
            assert status == 201, (status, created)
            assert created["sha256"] == hashlib.sha256(b"# index\n").hexdigest()
            assert os.path.isfile(os.path.join(root, "MEMORY.md"))
            assert stat_mod.S_IMODE(os.stat(root).st_mode) == 0o700, "dir mode"
            assert stat_mod.S_IMODE(
                os.stat(os.path.join(root, "MEMORY.md")).st_mode) == 0o600
            assert not [n for n in os.listdir(root) if ".tmp-" in n], \
                "the temp file must not survive the replace"
            # a create never overwrites (the page words a restore's 409 from this)
            status, body = _json(_http("POST", "/api/memory/file?name=MEMORY.md",
                                       body={"content": "x\n"}))
            assert status == 409 and body["category"] == "exists", body
            assert open(os.path.join(root, "MEMORY.md")).read() == "# index\n"
            # a save carries the sha it read, and the answer's sha is adoptable
            status, saved = _json(_http("PUT", "/api/memory/file?name=MEMORY.md",
                                        body={"content": "# index\n\nhi\n",
                                              "ifMatch": created["sha256"]}))
            assert status == 200, (status, saved)
            status, again = _json(_http("PUT", "/api/memory/file?name=MEMORY.md",
                                        body={"content": "# index\n\nhi again\n",
                                              "ifMatch": saved["sha256"]}))
            assert status == 200 and again, (status, again)
            # ...a stale sha is a 409 that PUBLISHES the current state
            status, conflict = _json(_http("PUT", "/api/memory/file?name=MEMORY.md",
                                           body={"content": "clobber\n",
                                                 "ifMatch": created["sha256"]}))
            assert status == 409 and conflict["category"] == "precondition", conflict
            for key in ("sha256", "mtime_ns", "size", "content"):
                assert key in conflict, (key, conflict)
            assert conflict["content"] == "# index\n\nhi again\n", conflict
            # ...and a conflict against bytes that are NOT valid UTF-8 publishes
            # the sha and the size but no content: the page's reload-then-save
            # exit would otherwise write replacement characters back over bytes
            # it never actually read.
            with open(os.path.join(root, "MEMORY.md"), "wb") as handle:
                handle.write(b"\xff\xfe not utf-8\n")
            status, conflict = _json(_http("PUT", "/api/memory/file?name=MEMORY.md",
                                           body={"content": "clobber\n",
                                                 "ifMatch": created["sha256"]}))
            assert status == 409 and "content" not in conflict, conflict
            assert conflict["sha256"] and conflict["size"], conflict
            os.remove(os.path.join(root, "MEMORY.md"))
            _seed(root, "MEMORY.md", "# index\n\nhi again\n")
            assert open(os.path.join(root, "MEMORY.md")).read() == "# index\n\nhi again\n"
            # ...and no precondition at all — or `"*"` — is a 412, not a write
            for if_match in (None, "*"):
                payload = {"content": "clobber\n"}
                if if_match:
                    payload["ifMatch"] = if_match
                status, body = _json(_http("PUT", "/api/memory/file?name=MEMORY.md",
                                           body=payload))
                assert status == 412 and body["category"] == "no-precondition", body
            assert open(os.path.join(root, "MEMORY.md")).read() == "# index\n\nhi again\n"
            # ...and a MALFORMED precondition is the same named 412 — the point
            # being that it is an ANSWER at all. `hmac.compare_digest` raises
            # TypeError on a non-ASCII `str`, and a TypeError inside this handler
            # is caught by nothing: the coroutine would die and the caller would
            # read ZERO bytes off the socket, which is exactly the "this build has
            # no memory API" misreport G5's JSON-always contract exists to delete.
            for bad in ("é" * 64, "Z" * 64, "0" * 63, "0" * 65,
                        "0" * 64 + "\n", " " + "0" * 64, "0" * 64 * 2,
                        "F" * 64, 12345, None, True):
                status, body = _json(_http("PUT", "/api/memory/file?name=MEMORY.md",
                                           body={"content": "clobber\n",
                                                 "ifMatch": bad}))
                assert status == 412 and body["category"] == "no-precondition", \
                    (repr(bad), status, body)
                assert open(os.path.join(root, "MEMORY.md")).read() == \
                    "# index\n\nhi again\n", repr(bad)
            # ...including one arrived at through the QUERY string, which is where
            # a percent-encoded non-ASCII byte gets in without touching the body
            status, body = _json(_http(
                "PUT", "/api/memory/file?name=MEMORY.md&ifMatch=" + "%C3%A9" * 64,
                body={"content": "clobber\n"}))
            assert status == 412 and body["category"] == "no-precondition", body
            assert open(os.path.join(root, "MEMORY.md")).read() == "# index\n\nhi again\n"
            # the per-file cap, on the bytes (not only on Content-Length)
            status, body = _json(_http("POST", "/api/memory/file?name=big.md",
                                       body={"content": "x" * (ms.MAX_MEMORY_BYTES + 1)}))
            assert status == 413 and body["category"] == "too-large", body
            assert not os.path.exists(os.path.join(root, "big.md"))
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_content_hygiene_refuses_by_category_and_never_echoes_the_match():
    """G7 step 7 / W10 / PROTOCOL-16: these bytes become model instructions."""
    base, root = _memory_tree()
    # assembled, never literal: a real token or URI in this file would be found
    # by `tests/test_publish_hygiene.py`'s scan of every tracked file
    blob = ("abcdefghijklmnopqrstuvwxyz" + "0123456789" + "ABCDEFG")
    uri = "mongodb://" + "touch" + ":" + "hunter2x" + "@" + "127.0.0.1:27017/db"
    try:
        with _memory_root(root):
            cases = (
                ("import-directive", "read @/etc/passwd for the details\n"),
                ("html-comment", "notes\n\n<!-- hidden from the model -->\n"),
                ("token-shape", blob + "\n"),
                ("credentialed-uri", uri + "\n"),
                ("nul-byte", "before\x00after\n"),
                ("lone-cr", "one\rtwo\n"),
                ("pinned", "---\npinned: true\n---\n\nhi\n"),
                # An UNTERMINATED fence does not hide its tail (N-2, attempt 1):
                # CommonMark runs an unclosed fence to the end of the document, so
                # a lenient validator in front of a strict loader is the one
                # direction where the disagreement SECURITY-6 forbids would be
                # exploitable. The tail is re-scanned as prose.
                ("import-directive", "notes\n```\n@/etc/passwd\n"),
                ("import-directive", "notes\n```\ncode\n```\nmore\n```\n@/etc/x\n"),
            )
            for category, content in cases:
                status, body = _json(_http("POST", "/api/memory/file?name=x.md",
                                           body={"content": content}))
                assert status in (400, 422), (category, status, body)
                assert body["category"] == category, (category, body)
                blob_body = json.dumps(body)
                assert blob not in blob_body, "a token-shaped line was echoed back"
                assert "hunter2x" not in blob_body, "a password was echoed back"
                assert not os.path.exists(os.path.join(root, "x.md"))
            # ...and the shapes that are NOT hazards still land: an @-import
            # inside a code span is documentation, a CRLF file is just a file,
            # and an inline comment mid-sentence is content the model does see.
            for name, content in (("ok1.md", "use `@./notes.md` to import\n"),
                                  ("ok2.md", "one\r\ntwo\r\n"),
                                  ("ok3.md", "text <!-- inline --> more text\n"),
                                  ("ok4.md", "```\n@/etc/passwd\n```\n"),
                                  # an unterminated fence is re-scanned, not
                                  # refused: what it holds is what decides
                                  ("ok5.md", "notes\n```\nplain code\n")):
                status, body = _json(_http("POST", "/api/memory/file?name=" + name,
                                           body={"content": content}))
                assert status == 201, (name, status, body)
            # `pinned` with the confirmation the UI spells in words
            status, body = _json(_http(
                "POST", "/api/memory/file?name=pin.md",
                body={"content": "---\npinned: true\n---\n\nhi\n",
                      "allowPinned": True}))
            assert status == 201, (status, body)
            assert "pinned: true" in open(os.path.join(root, "pin.md")).read()
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_frontmatter_is_never_invented_and_modified_is_stamped_when_it_exists():
    """SERVER-14/DOCS-16: two rules that are one decision about the CLI's contract."""
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            status, _ = _json(_http("POST", "/api/memory/file?name=plain.md",
                                    body={"content": "# plain\n"}))
            assert status == 201, status
            body = open(os.path.join(root, "plain.md")).read()
            assert body == "# plain\n", body
            assert not body.startswith("---"), "frontmatter must never be invented"
            status, _ = _json(_http("POST", "/api/memory/file?name=front.md",
                                    body={"content": "---\ntitle: x\n---\n\nbody\n"}))
            assert status == 201, status
            stamped = open(os.path.join(root, "front.md")).read()
            assert "modified:" in stamped, stamped
            assert stamped.count("modified:") == 1, stamped
            assert stamped.startswith("---\ntitle: x\n"), stamped
            assert stamped.endswith("\n\nbody\n"), stamped
            # a second write refreshes the SAME key rather than stacking another
            sha = hashlib.sha256(stamped.encode()).hexdigest()
            status, _ = _json(_http("PUT", "/api/memory/file?name=front.md",
                                    body={"content": stamped.replace("body", "body2"),
                                          "ifMatch": sha}))
            assert status == 200, status
            again = open(os.path.join(root, "front.md")).read()
            assert again.count("modified:") == 1, again
            # exactly one trailing newline, server-side (UI-3)
            status, _ = _json(_http("POST", "/api/memory/file?name=nl.md",
                                    body={"content": "text"}))
            assert status == 201
            assert open(os.path.join(root, "nl.md")).read() == "text\n"
            sha = hashlib.sha256(b"text\n").hexdigest()
            status, _ = _json(_http("PUT", "/api/memory/file?name=nl.md",
                                    body={"content": "text\n\n\n\n", "ifMatch": sha}))
            assert status == 200
            assert open(os.path.join(root, "nl.md")).read() == "text\n"
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_delete_is_a_move_and_a_save_keeps_the_prior_bytes():
    """G7 steps 8/9 / W11/W12/UI-7: nothing on this plane destroys the only copy."""
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            first = _seed(root, "note.md", "one\n")
            status, saved = _json(_http("PUT", "/api/memory/file?name=note.md",
                                        body={"content": "two\n", "ifMatch": first}))
            assert status == 200, (status, saved)
            history = os.path.join(root, ".history", "note.md")
            kept = sorted(os.listdir(history))
            assert len(kept) == 1, kept
            assert open(os.path.join(history, kept[0])).read() == "one\n", \
                "the bytes a save replaced must be recoverable"
            status, gone = _json(_http(
                "DELETE", "/api/memory/file?name=note.md&ifMatch=" + saved["sha256"]))
            assert status == 200 and gone["deleted"] is True, gone
            assert gone["trash"].startswith(".trash/note.md/"), gone
            assert not os.path.exists(os.path.join(root, "note.md"))
            trashed = os.path.join(root, gone["trash"])
            assert open(trashed).read() == "two\n", "the deleted bytes must be kept"
            import stat as stat_mod
            assert stat_mod.S_IMODE(os.stat(trashed).st_mode) == 0o600
            assert stat_mod.S_IMODE(
                os.stat(os.path.join(root, ".trash")).st_mode) == 0o700, \
                "every level of the side trees is 0700, not just the leaf"
            # ...and the file is gone from the list, while the side trees are not
            # listed as memory files (they are directories, and the list is flat)
            status, listing = _json(_http("GET", "/api/memory/list"))
            assert [row["name"] for row in listing["files"]] == [], listing
            # a delete needs the same precondition a save does
            second = _seed(root, "note.md", "three\n")
            status, body = _json(_http(
                "DELETE", "/api/memory/file?name=note.md&ifMatch=" + "0" * 64))
            assert status == 409 and body["category"] == "precondition", body
            assert os.path.isfile(os.path.join(root, "note.md"))
            status, body = _json(_http("DELETE", "/api/memory/file?name=note.md"))
            assert status == 412, (status, body)
            assert os.path.isfile(os.path.join(root, "note.md"))
            # ...and a MALFORMED one is the same named 412 rather than a dead
            # connection: on DELETE the carrier is the query string, so a
            # percent-encoded non-ASCII value reaches the comparison URL-decoded
            # (`hmac.compare_digest` raises TypeError on such a `str`).
            for raw in ("%C3%A9" * 64, "Z" * 64, "0" * 63, "%2A"):
                status, body = _json(_http(
                    "DELETE", "/api/memory/file?name=note.md&ifMatch=" + raw))
                assert status == 412 and body["category"] == "no-precondition", \
                    (raw, status, body)
                assert open(os.path.join(root, "note.md")).read() == "three\n", raw
            assert second
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_a_refusal_names_the_most_specific_reason_the_request_earned():
    """G7's hazards run VALIDATE -> DECIDE -> COMMIT, and the order is observable.

    The step numbers in `memory_mutate` cannot execute 1...10 (step 4 IS the
    atomic write), so the grouping is the contract and this arm is what holds it:
    content hygiene runs AFTER existence and the precondition, so a request that
    could not have succeeded anyway hears the reason it could not, rather than a
    body complaint that would send the operator to fix the wrong thing.
    """
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            os.makedirs(root, mode=0o700, exist_ok=True)
            # the baseline: with nothing else wrong, the body's problem IS the
            # answer — so every arm below is about precedence, not about a
            # hygiene check that stopped working
            status, body = _json(_http("POST", "/api/memory/file?name=x.md",
                                       body={"content": "read @/etc/passwd\n"}))
            assert status == 400 and body["category"] == "import-directive", body
            # a save of unhygienic content to a file that is NOT there: 404, not
            # 400 — the file's absence outranks the body's problem
            status, body = _json(_http("PUT", "/api/memory/file?name=ghost.md",
                                       body={"content": "read @/etc/passwd\n",
                                             "ifMatch": "0" * 64}))
            assert status == 404 and body["category"] == "missing", body
            # ...a stale precondition outranks it too
            first = _seed(root, "note.md", "one\n")
            status, body = _json(_http("PUT", "/api/memory/file?name=note.md",
                                       body={"content": "read @/etc/passwd\n",
                                             "ifMatch": "0" * 64}))
            assert status == 409 and body["category"] == "precondition", body
            assert open(os.path.join(root, "note.md")).read() == "one\n"
            assert first
            # ...and a create into a directory already at its file cap is a 413,
            # not a 400: the caps (G7 step 6) precede hygiene (step 7)
            for index in range(ms.MAX_MEMORY_FILES):
                _seed(root, f"n{index}.md", "x\n")
            status, body = _json(_http("POST", "/api/memory/file?name=new.md",
                                       body={"content": "read @/etc/passwd\n"}))
            assert status == 413 and body["category"] == "too-many-files", body
            assert not os.path.exists(os.path.join(root, "new.md"))
            # ...and an existing name outranks both of them, because existence is
            # the first thing the lock decides
            status, body = _json(_http("POST", "/api/memory/file?name=n0.md",
                                       body={"content": "read @/etc/passwd\n"}))
            assert status == 409 and body["category"] == "exists", body
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_the_untokened_health_route_does_not_reparse_settings_per_request():
    """`/health` has no token in front of it and runs inline on the event loop.

    The memory block's only non-`stat` work is the `aligned` answer — up to three
    settings files opened and JSON-parsed — so it is memoised for
    `MEMORY_HEALTH_TTL` seconds on THIS route: an unauthenticated poller must not
    be able to buy that work per request, in front of the live `/ws` stream. The
    tokened list route is deliberately NOT cached, because an operator who has
    just corrected `settings.local.json` is entitled to see it in the next refresh.
    """
    import time
    base, root = _memory_tree()
    real = ms.memory_settings_value
    calls = []
    saved_env = {name: os.environ.pop(name, None) for name in ms.MEMORY_ENV_OVERRIDES}
    try:
        def counted():
            calls.append(1)
            return real()

        ms.memory_settings_value = counted
        with _memory_root(root):
            _seed(root, "MEMORY.md", "# index\n")
            ms._MEMORY_ALIGN_CACHE.clear()
            for _ in range(5):
                assert "aligned" in ms.health_payload()["memory"]
            assert len(calls) == 1, \
                f"/health parsed the settings layers {len(calls)} times for 5 probes"
            # ...and it is a CACHE, not a one-shot: past the TTL it reads again
            ms._MEMORY_ALIGN_CACHE[root] = (
                time.monotonic() - ms.MEMORY_HEALTH_TTL - 1, (None, "stale"))
            assert "aligned" in ms.health_payload()["memory"]
            assert len(calls) == 2, calls
            # ...and the tokened route answers from the files every single time
            before = len(calls)
            for _ in range(3):
                status, listing = _json(_http("GET", "/api/memory/list"))
                assert status == 200, status
                assert "aligned" in listing, listing
            assert len(calls) == before + 3, (before, len(calls))
    finally:
        ms.memory_settings_value = real
        ms._MEMORY_ALIGN_CACHE.clear()
        for name, value in saved_env.items():
            if value is not None:
                os.environ[name] = value
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_a_symlinked_root_or_side_directory_is_refused():
    """G7 step 2's rule where a per-FILE check cannot reach it.

    `safe_memory_path` refuses a symlinked target, and documents that the ROOT is
    trusted — because `realpath(root)` follows a symlinked root by design and
    `memory_makedirs` accepts an existing directory at every level. So the two
    containers are checked where they are resolved instead: the root once, for the
    whole family, and `.history`/`.trash` at the write that would move bytes
    through them.
    """
    base, root = _memory_tree(prefix="memlink-")
    try:
        elsewhere = os.path.join(base, "elsewhere")
        os.makedirs(elsewhere, mode=0o700)
        os.makedirs(os.path.dirname(root), mode=0o700, exist_ok=True)
        os.symlink(elsewhere, root)
        with _memory_root(root):
            assert "symlink" in ms.memory_unavailable(), ms.memory_unavailable()
            status, body = _json(_http("GET", "/api/memory/list"))
            assert status == 503 and body["category"] == "memory-unavailable", body
            status, body = _json(_http("POST", "/api/memory/file?name=a.md",
                                       body={"content": "x\n"}))
            assert status == 503, (status, body)
            assert os.listdir(elsewhere) == [], \
                "nothing may be written through a symlinked root"
        os.unlink(root)
        # ...and a symlinked `.trash` is refused at the delete that would follow it
        os.makedirs(root, mode=0o700)
        first = _seed(root, "note.md", "one\n")
        os.symlink(elsewhere, os.path.join(root, ".trash"))
        with _memory_root(root):
            status, body = _json(_http(
                "DELETE", "/api/memory/file?name=note.md&ifMatch=" + first))
            assert status == 409 and body["category"] == "symlink", body
            assert open(os.path.join(root, "note.md")).read() == "one\n", \
                "a refused delete must leave the file exactly where it was"
            assert os.listdir(elsewhere) == [], elsewhere
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def _assert_file_plane_writes_no_stream():
    """No function of the file plane may touch the plan-card stream (Part D-6).

    An AST walk and not a grep, because the section's own comments and docstrings
    NAME `events.jsonl` and `touch-status` — they have to, or the next reader
    cannot know the rule exists — and a prose mention must not read as a write.
    Docstrings are skipped; every other string constant, name and attribute in
    every memory function is checked.
    """
    import ast
    tree = ast.parse(open(MODULE_PATH, encoding="utf-8").read())
    checked = 0
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not (node.name.startswith("memory_") or node.name in (
                "safe_memory_path", "read_memory_body", "requires_write_auth",
                "is_memory_route", "resolve_memory_root")):
            continue
        checked += 1
        body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                                 and isinstance(node.body[0].value, ast.Constant)
                                 ) else node.body
        for inner in body:
            for child in ast.walk(inner):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    for banned in ("events.jsonl", "touch-status", "status.sh"):
                        assert banned not in child.value, (node.name, banned)
                if isinstance(child, ast.Name):
                    assert child.id not in ("EVENTS", "STATE_DIR", "subprocess"), \
                        (node.name, child.id)
    assert checked >= 15, f"only {checked} memory functions were walked"


def test_every_mutation_leaves_one_audit_line_and_no_plan_event():
    """G7 step 10 / PROTOCOL-20 / Part D-5/D-6.

    The audit log is the file plane's own record — beside the memory dir, so the
    `.gitignore` carve keeps it ignored and the list route never sees it — and it
    is emphatically NOT the plan-card stream: a memory edit must not write an
    `events.jsonl` line, and must never fabricate a badge.
    """
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            sha = _seed(root, "note.md", "one\n")
            status, saved = _json(_http("PUT", "/api/memory/file?name=note.md",
                                        body={"content": "two\n", "ifMatch": sha}))
            assert status == 200
            status, _ = _json(_http(
                "DELETE", "/api/memory/file?name=note.md&ifMatch=" + saved["sha256"]))
            assert status == 200
            audit = os.path.join(base, ".touch", "memory-audit.jsonl")
            lines = [json.loads(ln) for ln in open(audit).read().splitlines() if ln]
            assert [ln["op"] for ln in lines] == ["update", "delete"], lines
            for line in lines:
                assert line["w"] == "monitor", line
                assert set(line) == {"ts", "op", "name", "bytes", "sha256", "w"}, line
                assert line["name"] == "note.md"
            import stat as stat_mod
            assert stat_mod.S_IMODE(os.stat(audit).st_mode) == 0o600
            # nothing under the memory root looks like a stream, and no event
            # file was written anywhere in the tree
            for dirpath, _dirs, files in os.walk(base):
                for name in files:
                    assert name != "events.jsonl", os.path.join(dirpath, name)
            assert not os.path.exists(os.path.join(root, "memory-audit.jsonl")), \
                "the audit log lives BESIDE the memory dir, not inside it"
            _assert_file_plane_writes_no_stream()
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_every_growing_collection_on_the_file_plane_is_capped():
    """G7 step 6 / W9 / W11: the directory, the history folders, the audit log.

    Driven as units rather than through 100 HTTP round trips — the caps are the
    thing under test, not the transport, and a test that took a hundred sockets
    to say so would be the first one anybody deleted.
    """
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            os.makedirs(root, mode=0o700, exist_ok=True)
            # 1. the file-count cap on create
            rows = [{"name": f"n{i}.md", "size": 10}
                    for i in range(ms.MAX_MEMORY_FILES)]
            try:
                ms.memory_dir_caps(root, rows, 10)
                raise AssertionError("the file-count cap did not fire")
            except ms.MemoryRefusal as exc:
                assert exc.status == 413 and exc.category == "too-many-files"
            # 2. the directory byte cap on create
            fat = [{"name": "big.md", "size": ms.MAX_MEMORY_DIR_BYTES}]
            try:
                ms.memory_dir_caps(root, fat, 1)
                raise AssertionError("the directory byte cap did not fire")
            except ms.MemoryRefusal as exc:
                assert exc.status == 413 and exc.category == "dir-too-large"
            # ...and a directory under both caps is accepted, so the arms mean
            # something
            ms.memory_dir_caps(root, rows[:2], 10)
            # 3. history revisions per file
            for n in range(ms.MEMORY_HISTORY_KEEP + 6):
                ms.memory_snapshot(root, ".history", "note.md",
                                   f"revision {n}\n".encode())
            kept = os.listdir(os.path.join(root, ".history", "note.md"))
            assert len(kept) <= ms.MEMORY_HISTORY_KEEP, len(kept)
            # 4. the audit log, trimmed to whole lines
            audit = os.path.join(base, ".touch", "memory-audit.jsonl")
            with open(audit, "w") as handle:
                handle.write(('{"ts": "x", "op": "update", "name": "n.md", '
                              '"bytes": 1, "sha256": "0", "w": "monitor"}\n')
                             * (ms.MEMORY_AUDIT_BYTES // 40))
            assert os.path.getsize(audit) > ms.MEMORY_AUDIT_BYTES
            ms.memory_audit(root, "update", "n.md", b"x\n")
            assert os.path.getsize(audit) <= ms.MEMORY_AUDIT_BYTES, \
                os.path.getsize(audit)
            body = open(audit).read()
            assert body.startswith("{") and body.endswith("}\n"), body[:60]
            for line in body.splitlines():
                json.loads(line)          # every survivor is a whole line
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_the_list_route_reports_the_index_budget_the_way_the_cli_measures_it():
    """SERVER-13/DOCS-14: caps disclosed, and measured with frontmatter stripped."""
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            over = ("---\ntitle: x\n---\n" + "<!-- a comment -->\n"
                    + "line\n" * (ms.MEM_INDEX_LINES + 5))
            _seed(root, "MEMORY.md", over)
            _seed(root, "topic.md", "just a note\n")
            status, listing = _json(_http("GET", "/api/memory/list"))
            assert status == 200, status
            assert listing["limits"] == {
                "maxBytes": ms.MAX_MEMORY_BYTES, "maxFiles": ms.MAX_MEMORY_FILES,
                "indexLines": 200, "indexBytes": 25600}, listing["limits"]
            rows = {row["name"]: row for row in listing["files"]}
            assert rows["MEMORY.md"]["isIndex"] is True
            assert rows["MEMORY.md"]["overLoadLimit"] is True, rows["MEMORY.md"]
            assert rows["MEMORY.md"]["hasFrontmatter"] is True
            assert rows["topic.md"]["isIndex"] is False
            assert rows["topic.md"]["overLoadLimit"] is False
            assert rows["topic.md"]["lines"] == 1, rows["topic.md"]
            # the index row is FIRST, and every field the page reads is present
            assert listing["files"][0]["name"] == "MEMORY.md"
            for row in listing["files"]:
                for key in ("name", "size", "mtime_ns", "lines", "isIndex",
                            "overLoadLimit", "hasFrontmatter", "writable", "reason"):
                    assert key in row, (key, row)
            # ...and the measurement itself strips what the CLI strips: the same
            # file measures UNDER the limit once its body is short enough, even
            # with frontmatter and a block comment adding lines.
            fits = "---\ntitle: x\n---\n" + "<!-- c -->\n" + "line\n" * 10
            lines, size = ms.memory_index_budget(fits)
            assert lines == 10 and size == len(("line\n" * 10).encode()), (lines, size)
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_the_list_route_publishes_alignment_from_the_documented_layers():
    """SERVER-4: the CLI's silent rejection is the whole reason this field exists."""
    base, root = _memory_tree()
    settings = os.path.join(base, ".claude", "settings.local.json")
    saved = os.environ.get("CLAUDE_PROJECT_DIR")
    try:
        os.makedirs(os.path.dirname(settings), exist_ok=True)
        os.environ["CLAUDE_PROJECT_DIR"] = base
        with _memory_root(root):
            # 1. nothing configured: NOT aligned, and the sentence names the default
            status, listing = _json(_http("GET", "/api/memory/list"))
            assert listing["aligned"] is False, listing
            assert "default" in listing["effective"], listing
            # 2. the absolute path this server serves: aligned
            with open(settings, "w") as handle:
                json.dump({"autoMemoryDirectory": root}, handle)
            status, listing = _json(_http("GET", "/api/memory/list"))
            assert listing["aligned"] is True, listing
            assert listing["effective"] == root, listing
            # 3. the obvious thing to write — a RELATIVE path — which the CLI
            #    rejects with no error and no warning (DOCS-1). Reported as
            #    not-aligned, naming the fallback, rather than believed.
            with open(settings, "w") as handle:
                json.dump({"autoMemoryDirectory": ".touch/memory"}, handle)
            status, listing = _json(_http("GET", "/api/memory/list"))
            assert listing["aligned"] is False, listing
            assert "not an absolute path" in listing["effective"], listing
            # 4. somewhere else entirely: not aligned, and the page can name it
            other = os.path.join(base, "elsewhere")
            with open(settings, "w") as handle:
                json.dump({"autoMemoryDirectory": other}, handle)
            status, listing = _json(_http("GET", "/api/memory/list"))
            assert listing["aligned"] is False and listing["effective"] == other
            # 5. an UNDOCUMENTED env override outranks every layer, so alignment
            #    becomes unknowable rather than falsely confident (DOCS-13).
            os.environ["CLAUDE_MEMORY_STORES"] = "somewhere"
            try:
                status, listing = _json(_http("GET", "/api/memory/list"))
                assert listing["aligned"] is None, listing
                assert "CLAUDE_MEMORY_STORES" in listing["effective"], listing
            finally:
                os.environ.pop("CLAUDE_MEMORY_STORES", None)
            # ...and the validator itself, on the shapes the CLI checks
            assert ms.memory_effective_dir("/abs/path") == "/abs/path"
            assert ms.memory_effective_dir("relative/path") is None
            assert ms.memory_effective_dir("") is None
            assert ms.memory_effective_dir("/a") is None       # under 3 chars
            assert ms.memory_effective_dir("//host/share") is None
            assert ms.memory_effective_dir("/ok/path\x00") is None
            assert ms.memory_effective_dir("~/mem") == os.path.expanduser("~/mem")
    finally:
        if saved is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = saved
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_health_publishes_the_memory_block_without_a_path_or_a_filename():
    """SERVER-10: `/health` is the untokened route; a topic name is a disclosure."""
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            _seed(root, "MEMORY.md", "# index\n")
            _seed(root, "acme-migration-blockers.md", "secret project\n")
            payload = ms.health_payload()
            block = payload["memory"]
            assert set(block) == {"present", "writable", "aligned", "files",
                                  "bytes", "indexOverLimit"}, block
            assert block["files"] == 2 and block["bytes"] > 0, block
            assert payload["memoryWrite"] in ("on", "off")
            blob = json.dumps(payload)
            assert "acme-migration-blockers" not in blob, blob
            assert "MEMORY.md" not in blob, blob
            assert root not in blob, blob
            assert ".touch" not in blob, blob
            assert "/" not in json.dumps(block), block
            # ...and an over-budget index is reported as a boolean, from ONE
            # file read: /health is untokened, so it may not walk the tree the
            # way the tokened list route does.
            _seed(root, "MEMORY.md", "line\n" * (ms.MEM_INDEX_LINES + 1))
            assert ms.health_payload()["memory"]["indexOverLimit"] is True
            # ...and the tokened list route is where the names DO appear
            status, listing = _json(_http("GET", "/api/memory/list"))
            assert "acme-migration-blockers.md" in [r["name"] for r in listing["files"]]
            assert listing["root"] == root
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_discover_tasks_never_treats_memory_or_sessions_as_a_task():
    """SERVER-9: a mis-set tasks root must not make the memory dir a "task".

    With `ORCH_TASKS_ROOT` one level off — `.touch/` instead of
    `.touch/local-orchestrators/` — every sibling would become a selectable
    task, `/artifacts?task=memory` would list every memory file as a note and
    `/file?task=memory&path=MEMORY.md` would serve them through the artifact
    reader: a second read path for the memory tree with none of the memory rules.
    """
    base = tempfile.mkdtemp(prefix="discover-", dir=_TMP_BASE)
    saved = ms.TASKS_ROOT
    try:
        touch = os.path.join(base, ".touch")
        for name in ("memory", "sessions", "runs", "local-orchestrators",
                     ".history"):
            os.makedirs(os.path.join(touch, name), exist_ok=True)
        os.makedirs(os.path.join(touch, "local-orchestrators", "sp-real"),
                    exist_ok=True)
        ms.TASKS_ROOT = touch
        found = set(ms.discover_tasks())
        assert "memory" not in found, found
        assert "sessions" not in found, found
        assert "runs" not in found, found
        assert ".history" not in found, found
        assert "local-orchestrators" in found, found
        # ...and over the CORRECT root, the one real task folder is all there is
        ms.TASKS_ROOT = os.path.join(touch, "local-orchestrators")
        assert set(ms.discover_tasks()) - {ms.DEFAULT_TASK} == {"sp-real"}
    finally:
        ms.TASKS_ROOT = saved
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_the_memory_page_is_served_with_no_referrer_and_only_at_its_own_route():
    """G4/SECURITY-5: one more document, and the token must not leak out of it."""
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            status, headers, body = _http("GET", "/memory", query_token=True,
                                          header_token=False, origin=False)
            assert status == 200, status
            assert "text/html" in headers["content-type"], headers
            assert headers.get("referrer-policy") == "no-referrer", headers
            assert headers.get("cache-control") == "no-store", headers
            assert b"memory manager" in body or b"<title>" in body, body[:120]
            # the page is a gated route like any other: no token, no page
            status, _headers, _body = _http("GET", "/memory", header_token=False)
            assert status == 401, status
            # ...and the dashboard itself carries the same policy, because its
            # URL carries the same token
            status, headers, _ = _http("GET", "/")
            assert status == 200 and headers.get("referrer-policy") == "no-referrer"
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_the_memory_read_route_answers_json_with_the_shape_the_page_reads():
    """G5's canonical read shape — the page refuses anything that is not JSON."""
    base, root = _memory_tree()
    try:
        with _memory_root(root):
            text = "---\ntitle: t\n---\n\n# index\n\nbody\n"
            sha = _seed(root, "MEMORY.md", text)
            status, body = _json(_http("GET", "/api/memory/file?name=MEMORY.md",
                                       query_token=True, header_token=False))
            assert status == 200, status
            assert set(body) == {"name", "content", "size", "sha256", "mtime_ns",
                                 "hasFrontmatter"}, body
            assert body["content"] == text and body["sha256"] == sha
            assert body["hasFrontmatter"] is True
            assert body["size"] == len(text.encode())
            assert isinstance(body["mtime_ns"], int) and body["mtime_ns"] > 0
            # a name that is not there is a named 404, not an empty 200
            status, body = _json(_http("GET", "/api/memory/file?name=nope.md",
                                       query_token=True, header_token=False))
            assert status == 404 and body["category"] == "missing", body
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def _write_gold() -> int:
    os.makedirs(GOLD_DIR, exist_ok=True)
    body = _gold_snapshot()
    with open(GOLD_SNAPSHOT, "w") as f:
        f.write(body)
    with open(GOLD_MANIFEST, "w") as f:
        f.write(f"{hashlib.sha256(body.encode()).hexdigest()}  snapshot-gold.json\n")
    print(f"wrote {GOLD_SNAPSHOT} ({len(body)} B)")
    return 0


def run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e!r}")
    print()
    for message in SKIPS:
        print(f"skipped: {message}")
    if failed:
        print(f"\n{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed ({len(SKIPS)} skipped)")


if __name__ == "__main__":
    if _WRITE_GOLD:
        raise SystemExit(_write_gold())
    run_all()
