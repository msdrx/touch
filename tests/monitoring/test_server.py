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


def _tasks_root(env, cwd, as_file=WATCHER_PATH):
    """Run the daemons' own resolver in a subprocess with a controlled env/cwd.

    ``as_file`` is the ``__file__`` the function believes it has — that is what
    the legacy rung measures ``../../local-orchestrators`` from, so pointing it
    somewhere outside a repo is how the "only if it already exists" arm is
    exercised without moving the real file. Only the function is exec'd, never
    the module body (which would tail a journal); it is self-contained by the
    byte-equality test above.
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
        f"ns = {{'os': os, '__file__': {as_file!r}}}\n"
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
    """env > $CLAUDE_PROJECT_DIR > cwd walk-up > legacy-only-if-it-exists."""
    import shutil
    base = tempfile.mkdtemp(prefix="tasksroot-", dir=_TMP_BASE)
    try:
        explicit = os.path.join(base, "explicit")
        project = os.path.join(base, "project")
        deep = os.path.join(project, "a", "b")
        os.makedirs(explicit)
        os.makedirs(os.path.join(project, ".claude"))
        os.makedirs(deep)
        # 1. $ORCH_TASKS_ROOT wins over everything, including the project.
        assert _tasks_root({"ORCH_TASKS_ROOT": explicit,
                            "CLAUDE_PROJECT_DIR": project}, deep) == explicit
        # 2. $CLAUDE_PROJECT_DIR beats the cwd walk-up (and does NOT need to
        #    exist: the anchor is the project, not a directory listing).
        assert _tasks_root({"CLAUDE_PROJECT_DIR": project}, deep) == \
            os.path.join(project, ".claude", "local-orchestrators")
        # 3. cwd walk-up finds the nearest .claude/ marker.
        assert _tasks_root({}, deep) == \
            os.path.join(project, ".claude", "local-orchestrators")
        # 4a. the legacy rung, from a daemon that DOES have a sibling tasks dir.
        legacy_home = os.path.join(base, "pkg", "shared", "monitoring")
        os.makedirs(legacy_home)
        os.makedirs(os.path.join(base, "pkg", "local-orchestrators"))
        orphan = os.path.join(base, "orphan")
        os.makedirs(orphan)
        # Arms 4a and 4b both need `orphan` to be genuinely marker-free: the cwd
        # walk-up is rung 3 and would answer before either of them.
        marker = _nearest_claude_marker(orphan)
        if marker:
            _skip(f"tasks-root arms 4a/4b: an ancestor of the temp tree holds {marker}")
            return
        assert _tasks_root({}, orphan,
                           as_file=os.path.join(legacy_home, "decision_watcher.py")) == \
            os.path.join(base, "pkg", "local-orchestrators")
        # 4b. ...and nothing at all: no env, no project, no marker above cwd, no
        #     legacy dir -> "" (the caller exits 1; it never invents a root).
        #     This arm only means what it says while NO ancestor of the throwaway
        #     tree holds a `.claude/`: one anywhere above $TMPDIR (this session's
        #     own scratchpad lives at /tmp/claude-1000/-home-laniakea-Projects-
        #     touch/…, one directory away from being exactly that) turns the cwd
        #     walk-up into a hit and flips "" to a real path. Assert the premise
        #     rather than assume it, and say so instead of failing on it.
        lonely = os.path.join(base, "lonely", "shared", "monitoring")
        os.makedirs(lonely)
        assert _tasks_root({}, orphan,
                           as_file=os.path.join(lonely, "decision_watcher.py")) == "", \
            "an unresolvable root must be empty"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_the_legacy_rung_is_taken_only_when_the_directory_exists():
    """The `../../local-orchestrators` rung is in-repo compatibility, not a guess.

    A packaged copy sits at `<plugin>/shared/monitoring`, so `../..` is the
    plugin root — globbing there would sweep sibling plugins looking for other
    people's task folders.
    """
    src = _function_source(MODULE_PATH, "resolve_tasks_root")
    assert 'return legacy if os.path.isdir(legacy) else ""' in src, src


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
    assert 'presented = presented_token(headers, query) or ""' in src, src


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
    assert r'b"Content-Security-Policy: sandbox\r\n"' in src, "sandbox header gone"
    # the header itself, not the prose explaining why it is gone
    for line in src.splitlines():
        assert not (line.lstrip().startswith(("b\"", "b'")) and "allow-scripts" in line), \
            f"a script in a report can lift the token: {line.strip()}"
    assert r'b"Referrer-Policy: no-referrer\r\n"' in src, src


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
                            "tokens"):
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
