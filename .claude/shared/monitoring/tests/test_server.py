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
    # a failed plan wins while orchestrator card is open
    assert out["status"] == "failed", out
    assert out["tokens"] == {"in": 11, "out": 6, "cached": 3, "cache_write": 4}, out["tokens"]


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
