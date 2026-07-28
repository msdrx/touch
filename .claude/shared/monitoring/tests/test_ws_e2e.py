#!/usr/bin/env python3
"""End-to-end socket tests for monitor_server.py's /ws, plus gen_stream.py.

Run: ``python3 test_ws_e2e.py`` (stdlib only, no pytest, non-zero on failure).

Why this file exists — M5 / WS-PROTOCOL-12 / SERVER-READ-13 / PRIOR-ART-TOUCH-14.
Every other server test is a unit test of a helper: nothing starts the server,
speaks RFC 6455 to it, and asserts that a client receives exactly the stream's
lines. That is precisely the coverage a protocol change (batching, a snapshot
prelude, a boundary frame, a cursor) would ship without, and the properties
most likely to break silently — no gap, no duplicate, no reordering, and the
legacy floor staying byte-identical — are only visible end to end.

**This file is the compatibility floor.** The v1 contract pinned below
(connect with no ``v`` ⇒ one text frame per line, file order, byte-identical
payloads, no control frames, truncation closes, keepalive arrives) must keep
passing UNCHANGED once the v2 protocol lands; a later sub-plan extends this
file with v2 cases and may not weaken these.

House rules honoured here:

* **work, not wall-clock** (GD-G) — assertions count frames, bytes and events
  (`STATS["events_sent"]`), never elapsed time. Timeouts exist only so a hung
  socket fails loudly instead of hanging the suite.
* **ephemeral ports only** — the server under test is started in-process on
  port 0 with ``asyncio.start_server(ms.handle, ...)``. The live monitor on
  8931 (which is watching the very run that produced this file) is never
  bound, never touched, never killed.
* the throwaway ``$ORCH_STATE_DIR`` is created before importing the module and
  is the only events.jsonl any test writes.

The websocket framing is hand-rolled on both sides on purpose: server→client
frames are unmasked, so a ~30-line decoder is all a faithful client needs, and
the module's own ``ws_frame``/``parse_client_frames`` are deliberately NOT
reused — a test that encodes with the code under test cannot see an encoding
bug.
"""
import asyncio
import base64
import collections
import hashlib
import importlib.util
import json
import os
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.abspath(os.path.join(HERE, "..", "monitor_server.py"))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
FIXTURE = os.path.join(REPO, "tests", "fixtures", "legacy",
                       "touch-mongo-live-events.jsonl")

# Resolve STATE_DIR to a throwaway dir at import, exactly like test_server.py,
# so nothing here can read or write a real task folder's stream.
_TMP_BASE = os.environ.get("TMPDIR") or "/tmp/claude-1000"
os.makedirs(_TMP_BASE, exist_ok=True)
_STATE_DIR = tempfile.mkdtemp(prefix="wse2e-", dir=_TMP_BASE)
os.environ["ORCH_STATE_DIR"] = _STATE_DIR
os.environ.pop("ORCH_PORT", None)


def _load_module():
    spec = importlib.util.spec_from_file_location("monitor_server_wse2e", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ms = _load_module()
sys.path.insert(0, HERE)
import gen_stream  # noqa: E402  (sibling helper, not a test module)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_TEXT, OP_CLOSE, OP_PING = 0x1, 0x8, 0x9


# --------------------------------------------------------------------------
# raw websocket client
# --------------------------------------------------------------------------

def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def _decode(buf: bytearray) -> list:
    """Consume whole server→client frames from ``buf``; return [(opcode, payload)].

    Server frames are never masked (RFC 6455 §5.1), so anything arriving with
    the mask bit set is a protocol violation and is surfaced as such.
    """
    frames = []
    while len(buf) >= 2:
        fin = buf[0] & 0x80
        opcode = buf[0] & 0x0F
        masked = buf[1] & 0x80
        length = buf[1] & 0x7F
        idx = 2
        if length == 126:
            if len(buf) < idx + 2:
                break
            length = int.from_bytes(buf[idx:idx + 2], "big")
            idx += 2
        elif length == 127:
            if len(buf) < idx + 8:
                break
            length = int.from_bytes(buf[idx:idx + 8], "big")
            idx += 8
        assert not masked, "server->client frames must not be masked"
        if len(buf) < idx + length:
            break
        payload = bytes(buf[idx:idx + length])
        del buf[:idx + length]
        assert fin, "monitor_server never fragments; a non-FIN frame is a bug"
        frames.append((opcode, payload))
    return frames


_OPEN_PROBES = []   # closed by _shutdown, AFTER the server's handlers are gone


class WsProbe:
    """Minimal RFC 6455 client: hand-rolled handshake, hand-rolled decoder."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.buf = bytearray()
        self.frames = []
        self.eof = False

    @classmethod
    async def connect(cls, port: int, query: str = "", version: str = "13",
                      send_key: bool = True):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(b"0123456789abcdef").decode()
        head = [f"GET /ws{query} HTTP/1.1", f"Host: 127.0.0.1:{port}",
                "Upgrade: websocket", "Connection: Upgrade"]
        if send_key:
            head.append(f"Sec-WebSocket-Key: {key}")
        head.append(f"Sec-WebSocket-Version: {version}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        probe = cls(reader, writer)
        probe.head = raw
        probe.key = key
        _OPEN_PROBES.append(probe)
        return probe

    async def pull(self, want: int, timeout: float = 8.0) -> list:
        """Read until ``want`` frames have arrived (or EOF/timeout)."""
        while len(self.frames) < want and not self.eof:
            try:
                chunk = await asyncio.wait_for(self.reader.read(65536), timeout)
            except asyncio.TimeoutError:
                break
            if not chunk:
                self.eof = True
                break
            self.buf += chunk
            self.frames += _decode(self.buf)
        return self.frames

    async def drain_until_eof(self, timeout: float = 8.0) -> list:
        while not self.eof:
            try:
                chunk = await asyncio.wait_for(self.reader.read(65536), timeout)
            except asyncio.TimeoutError:
                break
            if not chunk:
                self.eof = True
                break
            self.buf += chunk
            self.frames += _decode(self.buf)
        return self.frames

    def texts(self) -> list:
        return [p for op, p in self.frames if op == OP_TEXT]

    def close(self):
        try:
            self.writer.close()
        except Exception:
            pass


async def _serve():
    """Start the module's own handler on an ephemeral loopback port."""
    server = await asyncio.start_server(ms.handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _shutdown(server):
    """Mirror main()'s teardown: websocket streams never end on their own.

    Order matters: the server's handler tasks are cancelled FIRST and only then
    are the client sockets closed. Closing a probe while its stream still has
    frames queued makes the transport log ``socket.send() raised exception`` —
    noise that would train a reader to ignore this suite's output.
    """
    server.close()
    for task in list(ms.CONNECTIONS):
        task.cancel()
    await asyncio.gather(*ms.CONNECTIONS, return_exceptions=True)
    await server.wait_closed()
    while _OPEN_PROBES:
        _OPEN_PROBES.pop().close()


def _write_events(lines, blank_lines: bool = False) -> bytes:
    """(Re)write the throwaway task's events.jsonl; return the exact bytes."""
    body = ""
    for i, line in enumerate(lines):
        body += line + "\n"
        if blank_lines and i % 37 == 36:
            body += "\n"          # the writer's occasional empty line
    blob = body.encode()
    with open(ms.EVENTS, "wb") as f:
        f.write(blob)
    return blob


def _append_events(lines) -> None:
    with open(ms.EVENTS, "ab") as f:
        f.write(("\n".join(lines) + "\n").encode())


class _FastSleep:
    """Proxy for monitor_server's ``asyncio`` with a shortened ``sleep``.

    The keepalive is TICK-counted (40 ticks × 0.5 s ≈ 20 s), so observing a
    ping otherwise costs a 20 s wall-clock wait — exactly the kind of test that
    gets deleted. Rebinding ``ms.asyncio`` swaps the name in monitor_server's
    globals only; the real asyncio module is untouched everywhere else, and the
    assertion is still on the frame, never on elapsed time.
    """

    def __init__(self, real, delay: float):
        self._real, self._delay = real, delay

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def sleep(self, delay, *args, **kwargs):
        return await self._real.sleep(self._delay, *args, **kwargs)


# --------------------------------------------------------------------------
# v1 socket contract — the compatibility floor
# --------------------------------------------------------------------------

def test_v1_handshake_is_rfc6455_and_bad_upgrades_never_get_the_page():
    async def run():
        server, port = await _serve()
        try:
            _write_events(gen_stream.make_stream(40))
            ok = await WsProbe.connect(port)
            assert ok.head.startswith(b"HTTP/1.1 101 Switching Protocols"), ok.head[:40]
            assert f"Sec-WebSocket-Accept: {_accept_key(ok.key)}".encode() in ok.head, ok.head
            assert b"Upgrade: websocket" in ok.head, ok.head

            nokey = await WsProbe.connect(port, send_key=False)
            assert nokey.head.startswith(b"HTTP/1.1 400"), nokey.head[:40]
            assert b"<html" not in nokey.head.lower(), "never serve the page body on /ws"

            old = await WsProbe.connect(port, version="8")
            assert old.head.startswith(b"HTTP/1.1 426"), old.head[:40]
            assert b"Sec-WebSocket-Version: 13" in old.head, old.head
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v1_replay_is_one_text_frame_per_line_byte_identical():
    """The v1 floor: N lines in file order ⇒ N text frames, same bytes."""
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(400)
            _write_events(lines)
            probe = await WsProbe.connect(port)
            assert probe.head.startswith(b"HTTP/1.1 101"), probe.head[:40]
            frames = await probe.pull(len(lines))
            assert len(frames) == len(lines), (len(frames), len(lines))
            assert all(op == OP_TEXT for op, _ in frames), \
                sorted({op for op, _ in frames})
            for i, (line, (_, payload)) in enumerate(zip(lines, frames)):
                assert payload == line.encode(), (i, payload[:80], line[:80])
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v1_replay_carries_no_control_frames_and_no_envelope_key():
    """No hello, no boundary, no ``m`` key — v1 is bare events (GD-B floor).

    This is the assertion the v2 work is measured against: a socket that does
    not ask for v=2 must keep seeing exactly what it sees today.
    """
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(120)
            _write_events(lines)
            probe = await WsProbe.connect(port)
            frames = await probe.pull(len(lines))
            first = json.loads(frames[0][1])
            assert first == json.loads(lines[0]), "the first frame is the first event"
            for op, payload in frames:
                assert op == OP_TEXT, op
                ev = json.loads(payload)
                assert isinstance(ev, dict), type(ev)
                assert "m" not in ev, ev            # reserved envelope key (GD-B)
                assert "ts" in ev and "plan" in ev, ev
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v1_blank_lines_are_skipped_and_a_torn_tail_is_deferred_then_delivered():
    """A half-written line is never framed early and never lost (D5)."""
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(80)
            head, torn = lines[:-1], lines[-1]
            _write_events(head, blank_lines=True)
            with open(ms.EVENTS, "ab") as f:
                f.write(torn[:40].encode())        # partial, no newline
            probe = await WsProbe.connect(port)
            frames = await probe.pull(len(head))
            assert len(frames) == len(head), (len(frames), len(head))
            payloads = [p for _, p in frames]
            assert payloads == [ln.encode() for ln in head], "blank lines are skipped"
            with open(ms.EVENTS, "ab") as f:
                f.write(torn[40:].encode() + b"\n")  # complete it
            frames = await probe.pull(len(head) + 1)
            assert len(frames) == len(head) + 1, len(frames)
            assert frames[-1][1] == torn.encode(), frames[-1][1][:80]
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v1_live_tail_appends_arrive_in_file_order():
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(60)
            _write_events(lines)
            probe = await WsProbe.connect(port)
            await probe.pull(len(lines))
            extra = gen_stream.make_stream(30, seed=99)
            _append_events(extra)
            frames = await probe.pull(len(lines) + len(extra))
            assert len(frames) == len(lines) + len(extra), len(frames)
            tail = [p for _, p in frames[len(lines):]]
            assert tail == [ln.encode() for ln in extra], "tail order is file order"
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v1_truncation_sentinel_closes_the_socket():
    """size < offset ⇒ the server closes so the client rebuilds cleanly (D10)."""
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(200)
            _write_events(lines)
            probe = await WsProbe.connect(port)
            await probe.pull(len(lines))
            _write_events(lines[:5])              # wipe-and-rerun: smaller file
            frames = await probe.drain_until_eof()
            closes = [f for f in frames if f[0] == OP_CLOSE]
            assert closes, "expected a CLOSE frame after truncation"
            assert probe.eof, "the socket must end, not keep streaming"
            assert len(probe.texts()) == len(lines), \
                f"no replay of the truncated file: {len(probe.texts())} vs {len(lines)}"
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v1_keepalive_ping_arrives():
    """A ping keeps an idle historical stream alive (frame-based assertion)."""
    async def run():
        server, port = await _serve()
        real = ms.asyncio
        ms.asyncio = _FastSleep(real, 0.002)
        try:
            _write_events(gen_stream.make_stream(20))
            probe = await WsProbe.connect(port)
            await probe.pull(20)
            frames = await probe.pull(len(probe.frames) + 1)
            pings = [p for op, p in frames if op == OP_PING]
            assert pings, "no keepalive ping in ~40 ticks"
            assert pings[0] == b"", pings[0]
        finally:
            ms.asyncio = real
            await _shutdown(server)

    asyncio.run(run())


def test_v1_events_sent_counts_events_not_frames():
    """The shutdown line's counter is per EVENT — batching must not inflate it.

    Work-based, not wall-clock: the counter is the server's own accounting of
    how much it streamed, and it is the number the v2 batching work has to keep
    honest (WS-PROTOCOL-14).
    """
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(150)
            _write_events(lines)
            before = ms.STATS["events_sent"]
            probe = await WsProbe.connect(port)
            frames = await probe.pull(len(lines))
            assert len(frames) == len(lines), len(frames)
            sent = ms.STATS["events_sent"] - before
            assert sent == len(lines), (sent, len(lines))
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v1_task_parameter_resolution_is_the_documented_fallback():
    """v1 keeps its fallback: an unknown ?task= is served the default stream.

    Recorded deliberately, because v2 changes exactly this (an unknown task is
    refused with a hello error) while v1 must not change at all.
    """
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(30)
            _write_events(lines)
            named = await WsProbe.connect(port, query=f"?task={ms.DEFAULT_TASK}")
            frames = await named.pull(len(lines))
            assert [p for _, p in frames] == [ln.encode() for ln in lines]

            unknown = await WsProbe.connect(port, query="?task=no-such-task-xyz")
            frames = await unknown.pull(len(lines))
            assert len(frames) == len(lines), len(frames)
            assert [p for _, p in frames] == [ln.encode() for ln in lines], \
                "v1 falls back to the default stream"
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v1_an_append_during_the_replay_is_delivered_exactly_once():
    """The one v1 property the M5 floor never asserted (added attempt 2).

    Replay and tail are two producers over one socket, and the handover between
    them is a byte offset captured under the stream lock. A line appended while
    the replay is suspended in ``drain()`` must land in exactly ONE of them: sent
    twice it double-counts every token delta and duplicates log rows on the live
    dashboard; sent zero times it is simply lost. The multiset is the assertion —
    a bare count would also pass if one line were dropped and another duplicated.

    Getting there needs REAL backpressure: ``StreamWriter.drain()`` only
    suspends while the transport is paused, so a replay that never fills a
    socket runs to completion without yielding and nothing can interleave. A
    tiny ``SO_SNDBUF`` on the LISTENING socket (inherited by every accepted one
    on Linux) plus a probe that reads nothing gets it in ~150 KB instead of
    ~10 MB. (The same defect is pinned deterministically, without the kernel in
    the loop, by ``test_server.py``'s
    ``test_v1_replay_never_overruns_the_history_it_captured``.)
    """
    async def run():
        server, port = await _serve()
        try:
            server.sockets[0].setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
            lines = gen_stream.make_stream(2_000)
            _write_events(lines)
            probe = await WsProbe.connect(port)
            probe.writer.get_extra_info("socket").setsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
            stream = ms.Stream.get(ms.EVENTS)
            # hand-built so every appended line is unique: gen_stream's opening
            # header is seed-independent, and a duplicate in the INPUT would
            # make the multiset assertion below meaningless.
            extra: list = []
            for i in range(6):
                await asyncio.sleep(0.05)
                batch = [json.dumps({"ts": "2026-07-28T09:%02d:%02d.000Z" % (i, j),
                                     "plan": "sp-a", "stage": "impl",
                                     "state": "running",
                                     "detail": f"appended {i}-{j}"})
                         for j in range(8)]
                _append_events(batch)
                await stream.refresh()
                extra += batch
            want = lines + extra
            assert len(set(want)) == len(want), "the input itself must be unique"
            await probe.pull(len(want))
            # ...and then ask for ONE more. A duplicate does not change the first
            # len(want) frames — the copies arrive AFTER them — so a test that
            # stops counting at len(want) cannot see the defect at all.
            frames = await probe.pull(len(want) + 1, timeout=1.0)
            payloads = [p for op, p in frames if op == OP_TEXT]
            counts = collections.Counter(payloads)
            assert len(payloads) == len(want), (len(payloads), len(want))
            assert counts.most_common(1)[0][1] == 1, \
                counts.most_common(3)          # nothing arrived twice
            assert payloads == [ln.encode() for ln in want], "file order, once each"
        finally:
            await _shutdown(server)

    asyncio.run(run())


# --------------------------------------------------------------------------
# gen_stream.py — the deterministic corpus the perf work is measured on
# --------------------------------------------------------------------------

def test_gen_stream_is_deterministic_and_seed_sensitive():
    a = gen_stream.make_stream(500)
    b = gen_stream.make_stream(500)
    assert a == b, "same arguments must give a byte-identical stream"
    c = gen_stream.make_stream(500, seed=7)
    assert a != c, "a different seed must give a different stream"
    assert len(a) == len(c) == 500


def test_gen_stream_emits_exactly_n_events_at_every_size():
    for n in (12, 50, 200, 1000, 5000):
        lines = gen_stream.make_stream(n)
        assert len(lines) == n, (n, len(lines))
        for ln in lines:
            ev = json.loads(ln)
            assert isinstance(ev, dict) and ev.get("ts") and ev.get("plan"), ln[:120]
            assert "\n" not in ln, "one event per line"


def test_gen_stream_composition_matches_the_measured_corpus():
    """91 % tokens / 90 % quiet / 92.5 % agent / ~458 B per line (GD-G)."""
    st = gen_stream.stream_stats(gen_stream.make_stream(12_000))
    assert abs(st["tokens_frac"] - 0.91) <= 0.015, st["tokens_frac"]
    assert abs(st["quiet_frac"] - 0.90) <= 0.015, st["quiet_frac"]
    assert abs(st["agent_frac"] - 0.925) <= 0.015, st["agent_frac"]
    # the mean line length is emergent (real watcher strings), so the band is
    # wider than the fractions: within 6 % of the corpus's 458 B.
    assert abs(st["mean_bytes"] - 458) / 458 <= 0.06, st["mean_bytes"]
    assert st["rollups"] > 0, "the agent-less rollup shape must be present"
    assert st["agents"] >= 100 and st["plans"] >= 10, st


def test_gen_stream_agent_tokens_are_cumulative_over_their_own_deltas():
    """agent.tokens is ABSOLUTE; the top-level tokens is that agent's DELTA."""
    running = {}
    seen = 0
    for line in gen_stream.make_stream(3_000):
        ev = json.loads(line)
        agent = ev.get("agent")
        if not (isinstance(agent, dict) and ev.get("stage") == "tokens"
                and agent.get("tokens")):
            continue
        key = (ev["plan"], agent["id"])
        acc = running.setdefault(key, {k: 0 for k in gen_stream.TOKEN_KEYS})
        for k in gen_stream.TOKEN_KEYS:
            acc[k] += (ev.get("tokens") or {}).get(k) or 0
        assert agent["tokens"] == acc, (key, agent["tokens"], acc)
        seen += 1
    assert seen > 100, seen


def test_gen_stream_token_models_agree_per_plan_and_in_total():
    """GD-C: sum of every delta == sum of last cumulative per (plan, agent).

    The property PRIOR-ART-TOUCH-1 verified on the real corpus, and the one the
    snapshot prelude's counters depend on. It is asserted, never assumed: if a
    future generator change breaks it, this is what says so.
    """
    st = gen_stream.stream_stats(gen_stream.make_stream(6_000))
    assert st["model_a"] == st["model_b"], (
        sorted(k for k in st["model_a"] if st["model_a"][k] != st["model_b"].get(k)))
    assert st["total_a"] == st["total_b"], (st["total_a"], st["total_b"])
    assert st["total_a"]["in"] > 0 and len(st["model_a"]) > 1, st["total_a"]


def test_gen_stream_folds_through_the_servers_own_replay():
    """The generated stream is a legal subject for monitor_server's fold.

    Zero parse failures, a `done` verdict, and — the load-bearing part —
    ``replay_plan_states`` (which sums EVERY delta, i.e. GD-C model A) agreeing
    exactly with the fold model B. That is the equality the snapshot builder
    will be held to, asserted here against production code that already exists.
    """
    path = os.path.join(_STATE_DIR, "folded.jsonl")
    st = gen_stream.write_stream(path, 4_000)
    plan_states, last, tokens, failures = ms.replay_plan_states(path)
    assert failures == 0, failures
    assert tokens == st["total_a"] == st["total_b"], (tokens, st["total_a"])
    assert last and last["stage"] == "complete" and last["state"] == "done", last
    assert plan_states.get("orchestrator") == "done", plan_states
    assert set(plan_states.values()) <= {"done", "failed", "running", "queued"}, plan_states
    assert ms.task_status(path)["status"] == "done", ms.task_status(path)


def test_gen_stream_shape_matches_the_frozen_fixture():
    """Shape fidelity cross-check against the committed 320-line corpus.

    Keys, agent-block keys, token sub-keys, states and the four structural
    line shapes must all be reproduced; the only extra keys allowed are the
    additive ones the current writer adds (``w``, ``shortId``, ``plans_total``).
    """
    if not os.path.exists(FIXTURE):
        print("  skip touch-mongo-live-events.jsonl: fixture absent")
        return
    with open(FIXTURE, "rb") as f:
        fixture = [json.loads(ln) for ln in f if ln.strip()]
    generated = [json.loads(ln) for ln in gen_stream.make_stream(4_000)]

    fx_keys = {k for r in fixture for k in r}
    gen_keys = {k for r in generated for k in r}
    assert fx_keys <= gen_keys, sorted(fx_keys - gen_keys)
    assert gen_keys - fx_keys <= {"w", "plans_total"}, sorted(gen_keys - fx_keys)

    fx_ag = {k for r in fixture if "agent" in r for k in r["agent"]}
    gen_ag = {k for r in generated if "agent" in r for k in r["agent"]}
    assert fx_ag <= gen_ag, sorted(fx_ag - gen_ag)
    assert gen_ag - fx_ag <= {"shortId"}, sorted(gen_ag - fx_ag)

    fx_tok = {k for r in fixture if r.get("tokens") for k in r["tokens"]}
    gen_tok = {k for r in generated if r.get("tokens") for k in r["tokens"]}
    assert fx_tok == gen_tok == set(gen_stream.TOKEN_KEYS), (fx_tok, gen_tok)

    fx_states = {r.get("state") for r in fixture}
    assert fx_states <= {r.get("state") for r in generated}, fx_states
    for stage in ("tokens", "plan"):
        assert any(r.get("stage") == stage for r in generated), stage

    def shapes(recs):
        return (
            any(r.get("stage") == "tokens" and r.get("quiet") and "agent" in r
                for r in recs),
            any(r.get("stage") == "tokens" and "agent" not in r for r in recs),
            any(r.get("agent", {}).get("started") for r in recs),
            any(r.get("agent", {}).get("runtime") for r in recs),
        )
    assert shapes(generated) == shapes(fixture) == (True, True, True, True), \
        (shapes(generated), shapes(fixture))


def test_gen_stream_ts_inversions_stay_bounded():
    """Timestamps are mildly non-monotonic — like the real thing, on purpose.

    Journal-derived lines are stamped earlier than their file position, which
    is why every fold in this repo is defined over FILE order. The generator
    reproduces that, bounded, so a timeplan test can rely on it without the
    stream becoming pathological.
    """
    from datetime import datetime
    stamps = []
    for line in gen_stream.make_stream(3_000):
        ts = json.loads(line)["ts"]
        stamps.append(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    inversions = [stamps[i - 1] - stamps[i]
                  for i in range(1, len(stamps)) if stamps[i] < stamps[i - 1]]
    assert inversions, "a perfectly sorted stream would not be faithful"
    assert len(inversions) / len(stamps) < 0.10, len(inversions) / len(stamps)
    assert max(inversions) < 5.0, max(inversions)
    assert stamps[-1] > stamps[0], "the run still moves forward overall"


# --------------------------------------------------------------------------
# v2 protocol — hello / snapshot / boundary / batched tail / cursor resume
#
# Added by sp-server-stream-v2 (M8, M9, M11). Nothing above this line changed:
# the v1 floor is asserted exactly as it was written before the server moved,
# and every case below opens its own socket on the same ephemeral server.
# --------------------------------------------------------------------------

def _decode_texts(probe) -> list:
    """Text frames as Python: control frames are objects, tails are arrays."""
    return [json.loads(p) for p in probe.texts()]


def _split_v2(msgs):
    """(hello, snapshot|None, boundary|None, events, cursors) from a v2 socket.

    Shape dispatch is the client's documented rule (GD-B): an ARRAY is events,
    an object with the reserved ``m`` key is control, a bare object is a legacy
    event. Anything else here is a protocol violation and the tests say so.
    """
    hello = snapshot = boundary = None
    events, cursors = [], []
    for i, msg in enumerate(msgs):
        if isinstance(msg, list):
            for ev in msg:
                assert isinstance(ev, dict) and "m" not in ev, ev
            events += msg
            continue
        assert isinstance(msg, dict), msg
        kind = msg.get("m")
        assert kind, f"a v2 socket never sends a bare event object: {msg}"
        if kind == "hello":
            assert i == 0, "hello must be the FIRST frame"
            hello = msg
        elif kind == "snapshot":
            snapshot = msg
        elif kind == "tail":
            assert boundary is None, "exactly ONE boundary frame"
            boundary = msg
        elif kind == "cursor":
            cursors.append(msg)
        else:
            raise AssertionError(f"unknown control frame: {msg}")
    return hello, snapshot, boundary, events, cursors


async def _http(port: int, path: str) -> tuple:
    """Minimal HTTP/1.1 GET; returns (status_line, body_bytes)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 f"Connection: close\r\n\r\n".encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(-1), 8)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return head.split(b"\r\n")[0], body


def test_v2_hello_snapshot_boundary_then_a_live_array_tail():
    """M8(b)/M9 — the whole v2 sequence, in order, once each.

    The boundary frame is the point: v1 clients guess where history ends with a
    600 ms timer, and a v2 client is TOLD. The snapshot that precedes it must
    already contain every card the replay would have built.
    """
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(800)
            _write_events(lines)
            probe = await WsProbe.connect(port, query="?v=2")
            await probe.pull(3)
            hello, snap, boundary, events, _cur = _split_v2(_decode_texts(probe))
            assert hello["v"] == 2 and "error" not in hello, hello
            assert hello["foldGen"] == ms.FOLD_GEN and len(hello["sig"]) == 16
            assert hello["fromApplied"] is False and hello["reason"] is None
            assert snap is not None and snap["kind"] == "monitor-snapshot"
            assert snap["evCount"] == len(lines), snap["evCount"]
            assert snap["foldGen"] == hello["foldGen"] == ms.FOLD_GEN
            assert snap["sig"] == hello["sig"] == boundary["cursor"]["sig"]
            assert boundary["cursor"]["offset"] == os.path.getsize(ms.EVENTS)
            assert boundary["n"] == len(lines), boundary
            assert events == [], "the snapshot path sends no raw history"
            # a snapshot is 20x smaller than the stream it replaces
            assert len(json.dumps(snap)) < os.path.getsize(ms.EVENTS) / 5

            extra = gen_stream.make_stream(20, seed=3)
            _append_events(extra)
            before = len(probe.frames)
            await probe.pull(before + 2)
            _h, _s, _b, tail, cursors = _split_v2(_decode_texts(probe))
            assert [json.dumps(e) for e in tail] == \
                [json.dumps(json.loads(x)) for x in extra], "tail order is file order"
            assert cursors and cursors[-1]["cursor"]["offset"] == \
                os.path.getsize(ms.EVENTS), cursors
            assert sum(c["n"] for c in cursors) == len(extra), cursors
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v2_snapshot_plus_tail_deltas_equal_a_full_replay():
    """GD-C — hydrate + tail composes EXACTLY, per plan and in total.

    Snapshot counters are absolute as of the cursor; tail events carry deltas.
    The composition is only exact if the cursor has no gap and no duplicate,
    which is what makes this the load-bearing assertion of the whole design.
    """
    async def run():
        server, port = await _serve()
        try:
            head = gen_stream.make_stream(600)
            _write_events(head)
            probe = await WsProbe.connect(port, query="?v=2")
            await probe.pull(3)
            tail_lines = gen_stream.make_stream(120, seed=11)
            _append_events(tail_lines)
            await probe.pull(len(probe.frames) + 2)
            _h, snap, _b, tail, _c = _split_v2(_decode_texts(probe))
            composed = {}
            for pid, p in snap["plans"]:
                composed[pid] = {"in": p["tok"]["in"], "out": p["tok"]["out"],
                                 "cached": p["tok"]["cached"],
                                 "cache_write": p["tok"]["write"]}
            for ev in tail:
                tok = ev.get("tokens")
                if not tok:
                    continue
                row = composed.setdefault(ev["plan"],
                                          {k: 0 for k in gen_stream.TOKEN_KEYS})
                for k in gen_stream.TOKEN_KEYS:
                    row[k] += tok.get(k) or 0
            model_a, _b2 = gen_stream.token_models(head + tail_lines)
            for pid, want in model_a.items():
                assert composed.get(pid) == want, (pid, composed.get(pid), want)
            assert len(tail) == len(tail_lines), (len(tail), len(tail_lines))
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v2_snap0_batches_every_event_exactly_once_in_file_order():
    """M8(b,c) — the operator's always-available fallback, and its caps.

    ``?snap=0`` is the escape hatch: no fold, no snapshot, just the whole
    stream — but batched, because 12k frames is what the frame-count cliff is
    made of. Byte-for-byte the same events, in the same order, exactly once.
    """
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(1_400)
            _write_events(lines)
            probe = await WsProbe.connect(port, query="?v=2&snap=0")
            await probe.pull(6)
            msgs = _decode_texts(probe)
            hello, snap, boundary, events, _c = _split_v2(msgs)
            assert snap is None, "snap=0 means no snapshot"
            assert hello["snap"] == "0"
            arrays = [m for m in msgs if isinstance(m, list)]
            assert len(arrays) >= 3, len(arrays)
            for arr in arrays:
                assert len(arr) <= ms.BATCH_MAX_EVENTS, len(arr)
                assert len(json.dumps(arr, separators=(",", ":")).encode()) \
                    <= ms.BATCH_MAX_BYTES * 1.05, "byte cap"
            assert [json.dumps(e) for e in events] == \
                [json.dumps(json.loads(x)) for x in lines], "order and content"
            assert boundary["n"] == len(lines), boundary
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v2_snap_verify_sends_the_snapshot_and_the_replay_to_compare():
    """DATA-MODEL-9 — one URL turns "the numbers look off" into proof."""
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(300)
            _write_events(lines)
            probe = await WsProbe.connect(port, query="?v=2&snap=verify")
            await probe.pull(4)
            hello, snap, boundary, events, _c = _split_v2(_decode_texts(probe))
            assert hello["snap"] == "verify"
            assert snap is not None and snap["evCount"] == len(lines)
            assert len(events) == len(lines), "the shadow replay is complete"
            assert boundary is not None
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v2_one_tick_is_capped_and_the_remainder_carries_over():
    """M8(d)/WS-PROTOCOL-10 — a re-seed burst cannot arrive as one tick.

    A watcher restart backfills thousands of events at once. The cap bounds one
    tick; the cursor stops where the cap fell, so the continuation has no gap
    and no duplicate — asserted here on the cursor frames themselves.
    """
    async def run():
        server, port = await _serve()
        try:
            real_cap = ms.MAX_TICK_EVENTS
            ms.MAX_TICK_EVENTS = 300         # same rule, one second of test time
            lines = gen_stream.make_stream(200)
            _write_events(lines)
            probe = await WsProbe.connect(port, query="?v=2")
            await probe.pull(3)
            burst = gen_stream.make_stream(700, seed=21)
            _append_events(burst)
            for _ in range(6):
                await probe.pull(len(probe.frames) + 1, timeout=2.0)
                _h, _s, _b, tail, cursors = _split_v2(_decode_texts(probe))
                if len(tail) >= len(burst):
                    break
            assert len(tail) == len(burst), (len(tail), len(burst))
            assert len(cursors) >= 3, [c["n"] for c in cursors]
            assert all(c["n"] <= 300 for c in cursors), [c["n"] for c in cursors]
            assert sum(c["n"] for c in cursors) == len(burst), cursors
            offsets = [c["cursor"]["offset"] for c in cursors]
            assert offsets == sorted(offsets), offsets
            assert offsets[-1] == os.path.getsize(ms.EVENTS)
            assert [json.dumps(e) for e in tail] == \
                [json.dumps(json.loads(x)) for x in burst], "no gap, no duplicate"
        finally:
            ms.MAX_TICK_EVENTS = real_cap
            await _shutdown(server)

    asyncio.run(run())


def test_v2_unknown_task_is_refused_on_the_hello():
    """M11(f)/SERVER-READ-10 — v2 never serves the default task by accident.

    After the 101 there is no status code left to refuse with, so the refusal
    is the hello. v1's fallback is deliberately untouched (asserted above).
    """
    async def run():
        server, port = await _serve()
        try:
            _write_events(gen_stream.make_stream(50))
            probe = await WsProbe.connect(port, query="?task=no-such-task-xyz&v=2")
            await probe.drain_until_eof(timeout=4.0)
            msgs = _decode_texts(probe)
            assert msgs and msgs[0]["m"] == "hello", msgs[:1]
            assert msgs[0]["error"] == "unknown-task", msgs[0]
            assert len(msgs) == 1, "no stream data for a task that does not exist"
            assert any(op == OP_CLOSE for op, _ in probe.frames), "must close"
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v2_unhonoured_query_parameters_are_named_on_the_hello():
    """M8(e)/GD-B — every parameter the server could not honour is named."""
    async def run():
        server, port = await _serve()
        try:
            _write_events(gen_stream.make_stream(40))
            probe = await WsProbe.connect(port, query="?v=2&fold=30&bogus=x")
            await probe.pull(3)
            hello = _decode_texts(probe)[0]
            assert sorted(hello["ignored"]) == ["bogus", "fold"], hello["ignored"]
            assert hello["fromApplied"] is False
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v2_cursor_resume_has_no_gap_and_no_duplicate():
    """M11(a) — the resume the 500 ms refresh option needs to be cheap.

    A reconnect with ``&from=&sig=`` gets the boundary immediately (no
    snapshot) and exactly the events it missed. Token-sum equality against a
    straight replay is the proof that "exactly" is exact.
    """
    async def run():
        server, port = await _serve()
        try:
            head = gen_stream.make_stream(500)
            _write_events(head)
            first = await WsProbe.connect(port, query="?v=2")
            await first.pull(3)
            _h, snap, boundary, _e, _c = _split_v2(_decode_texts(first))
            cursor = boundary["cursor"]
            first.close()

            missed = gen_stream.make_stream(60, seed=31)
            _append_events(missed)
            probe = await WsProbe.connect(
                port, query=f"?v=2&from={cursor['offset']}&sig={cursor['sig']}")
            await probe.pull(3)
            hello, snap2, boundary2, events, _c2 = _split_v2(_decode_texts(probe))
            assert hello["fromApplied"] is True, hello
            assert hello["reason"] is None, hello
            assert snap2 is None, "an accepted resume never re-sends a snapshot"
            assert [json.dumps(e) for e in events] == \
                [json.dumps(json.loads(x)) for x in missed], "exactly the gap"
            assert boundary2["cursor"]["offset"] == os.path.getsize(ms.EVENTS)

            model_a, _b = gen_stream.token_models(head + missed)
            composed = {}
            for pid, p in snap["plans"]:
                composed[pid] = {"in": p["tok"]["in"], "out": p["tok"]["out"],
                                 "cached": p["tok"]["cached"],
                                 "cache_write": p["tok"]["write"]}
            for ev in events:
                tok = ev.get("tokens")
                if not tok:
                    continue
                row = composed.setdefault(ev["plan"],
                                          {k: 0 for k in gen_stream.TOKEN_KEYS})
                for k in gen_stream.TOKEN_KEYS:
                    row[k] += tok.get(k) or 0
            for pid, want in model_a.items():
                assert composed.get(pid) == want, (pid, composed.get(pid), want)
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v2_resume_is_refused_mid_line_ahead_and_on_a_foreign_sig():
    """M11(b,c,d)/DATA-MODEL-10 — every failure falls back, none is silent.

    ``from`` is untrusted client input. A mid-line offset would frame garbage;
    an offset past ours is a client that knows more than the server; a foreign
    sig is the wipe-and-rerun. All three reply ``fromApplied:false`` with a
    reason and the full snapshot path — never a foreign tail on top of
    hydrated state.
    """
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(200)
            _write_events(lines)
            probe = await WsProbe.connect(port, query="?v=2")
            await probe.pull(3)
            _h, _s, boundary, _e, _c = _split_v2(_decode_texts(probe))
            good = boundary["cursor"]
            probe.close()

            cases = [
                (f"?v=2&from={good['offset'] - 5}&sig={good['sig']}", "mid-line"),
                (f"?v=2&from={good['offset'] + 4096}&sig={good['sig']}",
                 "offset-ahead"),
                (f"?v=2&from={good['offset']}&sig={'0' * 16}", "sig-mismatch"),
                (f"?v=2&from=notanumber&sig={good['sig']}", "bad-offset"),
                (f"?v=2&from={good['offset']}", "no-sig"),
            ]
            for query, want in cases:
                p = await WsProbe.connect(port, query=query)
                await p.pull(3)
                hello, snap, boundary2, events, _c2 = _split_v2(_decode_texts(p))
                assert hello["fromApplied"] is False, (query, hello)
                assert hello["reason"] == want, (query, hello["reason"], want)
                assert snap is not None, f"{query}: must fall back to a snapshot"
                assert snap["evCount"] == len(lines), (query, snap["evCount"])
                assert events == [], query
                p.close()
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v2_wipe_and_rerun_refuses_the_stale_cursor():
    """M11(b)/DATA-MODEL-5 — the documented reset, at the byte offset it hits.

    Stop the daemons, delete events.jsonl, re-seed, restart: within minutes the
    new stream passes the old offset. Without a content identity the dashboard
    would tail a foreign stream from hydrated state of the old run, with no
    error anywhere.
    """
    async def run():
        server, port = await _serve()
        try:
            old = gen_stream.make_stream(300)
            _write_events(old)
            probe = await WsProbe.connect(port, query="?v=2")
            await probe.pull(3)
            _h, _s, boundary, _e, _c = _split_v2(_decode_texts(probe))
            stale = boundary["cursor"]
            probe.close()

            os.remove(ms.EVENTS)                     # wipe...
            fresh = gen_stream.make_stream(900, seed=77)
            _write_events(fresh)                     # ...and re-run, larger
            assert os.path.getsize(ms.EVENTS) > stale["offset"]

            p = await WsProbe.connect(
                port, query=f"?v=2&from={stale['offset']}&sig={stale['sig']}")
            await p.pull(3)
            hello, snap, _b, events, _c2 = _split_v2(_decode_texts(p))
            assert hello["fromApplied"] is False, hello
            assert hello["reason"] == "sig-mismatch", hello
            assert hello["sig"] != stale["sig"], "a new run is a new identity"
            assert snap["evCount"] == len(fresh), snap["evCount"]
            assert events == [], events
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v2_events_sent_counts_events_not_frames_under_batching():
    """WS-PROTOCOL-14 — batching must not deflate the shutdown counter."""
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(900)
            _write_events(lines)
            before = ms.STATS["events_sent"]
            probe = await WsProbe.connect(port, query="?v=2&snap=0")
            await probe.pull(5)
            _h, _s, boundary, events, _c = _split_v2(_decode_texts(probe))
            assert len(events) == len(lines)
            assert ms.STATS["events_sent"] - before == len(lines), \
                (ms.STATS["events_sent"] - before, len(lines))
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v2_a_poisoned_line_costs_only_itself_not_its_batch():
    """R-10 across the batching boundary (added attempt 2).

    Under v1 an unparseable line costs exactly one event: it is one frame, the
    client's ``JSON.parse`` throws, the page swallows it. Concatenated into a v2
    array frame it would cost the WHOLE frame — up to 500 good events lost in a
    ``catch (e) {}`` — and the live tail is precisely where a malformed line is
    most likely (a ``status.sh`` detail with a stray byte). Both sockets are
    driven here at once, so the asymmetry is asserted, not assumed.
    """
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(60)
            _write_events(lines)
            v2 = await WsProbe.connect(port, query="?v=2")
            v1 = await WsProbe.connect(port)
            await v2.pull(3)
            await v1.pull(len(lines))
            poison = b'{"ts":"2026-07-28T09:00:00.000Z", NOT JSON'
            good_a = gen_stream.make_stream(12, seed=31)
            good_b = gen_stream.make_stream(12, seed=32)
            with open(ms.EVENTS, "ab") as f:
                f.write(("\n".join(good_a) + "\n").encode())
                f.write(poison + b"\n")
                f.write(("\n".join(good_b) + "\n").encode())

            await v2.pull(len(v2.frames) + 2)
            for payload in v2.texts():
                try:
                    json.loads(payload)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise AssertionError(f"unparseable v2 frame: {exc}") from None
            _h, _s, _b, events, cursors = _split_v2(_decode_texts(v2))
            good = [json.loads(x) for x in good_a + good_b]
            assert events == good, (len(events), len(good))
            assert cursors and cursors[-1]["cursor"]["offset"] == \
                os.path.getsize(ms.EVENTS), cursors
            assert sum(c["n"] for c in cursors) == len(good), cursors

            # v1 is untouched: the bad line still arrives, as its own frame
            await v1.pull(len(lines) + len(good) + 1)
            tail = [p for op, p in v1.frames if op == OP_TEXT][len(lines):]
            assert poison in tail, tail[:3]
            assert len(tail) == len(good) + 1, len(tail)
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_a_young_stream_keeps_its_sockets_open_across_appends():
    """B2 on the wire — the first 4 KB of a run must not flap the socket.

    ``stream_sig`` hashes the whole file while it is shorter than 4 KB, so the
    digest changes on every append. Treating that as an identity break fires
    the truncation sentinel — the one destructive signal in the protocol — on an
    ordinary append, for the first ~20-40 lines of EVERY run: the page reconnects,
    runs ``tpReset()``/``statsReset()`` and replays, over and over, at the poll
    cadence. The M5 floor cannot see it because it writes its corpus (27 KB+)
    before connecting; here BOTH wire versions watch a stream grow from one line.
    """
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(30)
            _write_events(lines[:1])
            assert os.path.getsize(ms.EVENTS) < ms.SIG_BYTES
            v1 = await WsProbe.connect(port)
            v2 = await WsProbe.connect(port, query="?v=2")
            await v1.pull(1)
            await v2.pull(3)
            hello, snap, boundary, _e, _c = _split_v2(_decode_texts(v2))
            assert snap["evCount"] == 1, snap["evCount"]

            for i in range(1, 6):
                _append_events(lines[i:i + 1])
                assert os.path.getsize(ms.EVENTS) < ms.SIG_BYTES, "still young"
                await v1.pull(i + 1)
                await v2.pull(len(v2.frames) + 1)

            assert not v1.eof, "an append closed a v1 socket"
            assert not v2.eof, "an append closed a v2 socket"
            assert not [f for f in v1.frames if f[0] == OP_CLOSE], "no sentinel"
            assert [p for op, p in v1.frames if op == OP_TEXT] == \
                [ln.encode() for ln in lines[:6]], "every line, once, in order"
            _h2, _s2, _b2, events, cursors = _split_v2(_decode_texts(v2))
            assert [json.dumps(e) for e in events] == \
                [json.dumps(json.loads(x)) for x in lines[1:6]], events
            assert cursors, "the tail publishes its cursor like any other tail"

            # ...and that cursor resumes. The sig is a CONTENT identity, so it
            # legitimately moves while the head is still growing (it hashes the
            # whole file below SIG_BYTES); what must hold is that the pair the
            # server just published is accepted, and that a pair invalidated by
            # a later append degrades to a snapshot — never to a foreign tail.
            cursor = cursors[-1]["cursor"]
            assert cursor["offset"] == os.path.getsize(ms.EVENTS), cursor
            assert cursor["sig"] == ms.stream_sig(ms.EVENTS), cursor
            p = await WsProbe.connect(
                port, query=f"?v=2&from={cursor['offset']}&sig={cursor['sig']}")
            await p.pull(2)
            hello2, snap2, _b3, gap, _c3 = _split_v2(_decode_texts(p))
            assert hello2["fromApplied"] is True, hello2
            assert snap2 is None, "an accepted resume sends no snapshot"
            assert gap == [], gap
            missed = lines[6:]
            _append_events(missed)                   # the head grows past 4 KB
            await p.pull(len(p.frames) + 1)
            _h4, _s4, _b4, tail, _c4 = _split_v2(_decode_texts(p))
            assert [json.dumps(e) for e in tail] == \
                [json.dumps(json.loads(x)) for x in missed], "the tail resumed"

            stale = await WsProbe.connect(
                port, query=f"?v=2&from={cursor['offset']}&sig={cursor['sig']}")
            await stale.pull(3)
            hello3, snap3, _b5, gap3, _c5 = _split_v2(_decode_texts(stale))
            assert hello3["fromApplied"] is False, hello3
            assert hello3["reason"] == "sig-mismatch", hello3
            assert snap3["evCount"] == len(lines) and gap3 == [], snap3["evCount"]
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_v2_counters_separate_snapshots_from_events_and_skip_poison():
    """m-4/m-5 — what ``events_sent`` counts, and what it deliberately does not.

    A snapshot prelude folds N events into ONE frame: counting it as N would
    claim a v2 client received a replay it never got, and counting it as 0 with
    nothing else would make a v2-only deployment read as idle. It gets its own
    counter. And a ``snap=0`` replay of a stream with a poisoned line writes one
    event fewer than it read — the filter runs before the framing, so the count
    is what actually went out.
    """
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(300)
            _write_events(lines)
            ev_before = ms.STATS["events_sent"]
            snap_before = ms.STATS["snapshots_sent"]
            probe = await WsProbe.connect(port, query="?v=2")
            await probe.pull(3)
            _h, snap, _b, events, _c = _split_v2(_decode_texts(probe))
            assert snap["evCount"] == len(lines) and events == []
            assert ms.STATS["events_sent"] == ev_before, "a fold is not events"
            assert ms.STATS["snapshots_sent"] == snap_before + 1
            probe.close()

            # ...and the poisoned snap=0 replay counts only what it framed
            _write_events(lines[:50])
            with open(ms.EVENTS, "ab") as f:
                f.write(b'{"ts":"2026-07-28T09:00:00.000Z", NOT JSON\n')
            ev_before = ms.STATS["events_sent"]
            p2 = await WsProbe.connect(port, query="?v=2&snap=0")
            await p2.pull(3)
            _h2, _s2, boundary, replayed, _c2 = _split_v2(_decode_texts(p2))
            assert len(replayed) == 50, len(replayed)
            assert boundary["n"] == 50, boundary
            assert ms.STATS["events_sent"] - ev_before == 50, \
                (ms.STATS["events_sent"] - ev_before)
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_http_unknown_task_is_404_on_artifacts_and_file():
    """M11(e)/SERVER-READ-10 — the live-server wrong-answer repro, as a test.

    ``GET /artifacts?task=NOSUCHTASK`` used to answer 200 with the DEFAULT
    task's 18 KB listing. An absent ``?task=`` still falls back (that is the
    documented single-task mode); a name that does not exist is a 404.
    """
    async def run():
        server, port = await _serve()
        try:
            _write_events(gen_stream.make_stream(30))
            status, body = await _http(port, "/artifacts?task=no-such-task-xyz")
            assert b"404" in status, status
            assert json.loads(body)["error"] == "unknown-task", body[:120]
            status, body = await _http(port,
                                       "/file?task=no-such-task-xyz&path=plan/x.md")
            assert b"404" in status, status
            status, body = await _http(port, "/artifacts")
            assert b"200" in status, status
            assert "artifacts" in json.loads(body), body[:120]
            status, body = await _http(port, f"/artifacts?task={ms.DEFAULT_TASK}")
            assert b"200" in status, status
            status, body = await _http(port, "/health")
            assert b"200" in status, status
            health = json.loads(body)
            assert health["status"] == "ok" and "streams" in health, health
        finally:
            await _shutdown(server)

    asyncio.run(run())


def test_tasks_route_serves_the_registry_fold():
    """`/tasks` still answers the home grid, now from the shared fold."""
    async def run():
        server, port = await _serve()
        try:
            lines = gen_stream.make_stream(400)
            _write_events(lines)
            status, body = await _http(port, "/tasks")
            assert b"200" in status, status
            payload = json.loads(body)
            assert payload["default"] == ms.DEFAULT_TASK
            mine = [t for t in payload["tasks"] if t["name"] == ms.DEFAULT_TASK]
            assert mine and mine[0]["events"] is True, payload["tasks"][:2]
            assert mine[0]["status"] in ("done", "running", "failed"), mine[0]
            ref = ms.task_status(ms.EVENTS)
            assert mine[0]["status"] == ref["status"], (mine[0], ref)
            assert mine[0]["tokens"] == ref["tokens"], (mine[0], ref)
        finally:
            await _shutdown(server)

    asyncio.run(run())


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
