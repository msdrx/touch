#!/usr/bin/env python3
"""Stdlib-only tests for monitor_server.py (run: python3 test_server.py).

No pytest, no omnigent imports. Uses an ephemeral throwaway state dir under
/tmp/claude-1000 and never touches the live monitor's events.jsonl. Asserts fail
loudly (AssertionError -> non-zero exit).
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.abspath(os.path.join(HERE, "..", "monitor_server.py"))

# Resolve STATE_DIR/PORT at import to a throwaway dir so nothing touches the
# live task folder. No server is started (main() is never called).
_TMP_BASE = os.environ.get("TMPDIR") or "/tmp/claude-1000"
os.makedirs(_TMP_BASE, exist_ok=True)
_STATE_DIR = tempfile.mkdtemp(prefix="srvtest-", dir=_TMP_BASE)
os.environ["ORCH_STATE_DIR"] = _STATE_DIR
os.environ.pop("ORCH_PORT", None)


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
    assert health["parse_failures"].get(path) == 2, health
    assert health["parse_failures_total"] == base + 2, (base, health)
    # The good lines still render — a poisoned line degrades, never blocks.
    assert out["status"] == "done", out

    # A clean stream clears its entry instead of leaving a stale count behind.
    clean = os.path.join(_STATE_DIR, "clean.jsonl")
    with open(clean, "wb") as f:
        f.write((good + "\n").encode())
    ms.task_status(clean)
    assert clean not in ms.health_payload()["parse_failures"], ms.PARSE_FAILURES

    # m-2: so does a stream that DISAPPEARS (deleted or rotated after a poisoned
    # scan). The early return on stat failure used to skip the pop, so a gone
    # stream kept inflating parse_failures_total for the life of the server —
    # a permanently red probe with nothing left to fix.
    assert ms.PARSE_FAILURES.get(path) == 2, ms.PARSE_FAILURES
    os.remove(path)
    gone = ms.task_status(path)
    assert gone["status"] == "empty", gone
    assert path not in ms.health_payload()["parse_failures"], ms.PARSE_FAILURES
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
_FIXTURES = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..",
                                         "tests", "fixtures", "legacy"))


def _fixture(name):
    path = os.path.join(_FIXTURES, name)
    return path if os.path.isfile(path) else None


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
            print(f"  skip {name}: fixture absent")
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
            print(f"  skip {name}: fixture absent")
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
        print("  skip touch-repo-recon-events.jsonl: fixture absent")
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
    """resolve_state_dir has no ROOT events.jsonl short-circuit (SHELL-5 / D6)."""
    import inspect
    src = inspect.getsource(ms.resolve_state_dir)
    assert "return ROOT" in src  # only the final empty fallback
    # the abandoned short-circuit pattern must be gone
    assert "events.jsonl\")):\n        return ROOT" not in src
    assert src.count("return ROOT") == 1, src


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
    if failed:
        print(f"\n{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
