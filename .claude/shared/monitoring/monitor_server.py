#!/usr/bin/env python3
"""Stdlib-only live progress monitor: HTTP page + WebSocket event stream on one port.

Serves monitor.html at "/" and a websocket at "/ws" that replays every line of
events.jsonl and then streams new lines as they are appended. No third-party
dependencies (sandbox egress is proxied), so the websocket protocol is
implemented by hand: server->client text frames, ping keepalive, and a reader
task that drains client pongs/close frames.
"""
import asyncio
import base64
import hashlib
import json
import os
import signal
import sys
import time
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
TASKS_ROOT = os.path.abspath(os.path.join(ROOT, "..", "..", "local-orchestrators"))
# Per-task state (events.jsonl, orch-config.json) lives in $ORCH_STATE_DIR;
# the shared module directory holds only code and stays stateless.


def resolve_state_dir() -> str:
    """State dir: $ORCH_STATE_DIR > newest task folder > script dir (empty fallback).

    The shared module directory (ROOT) is code-only and NEVER an authoritative
    state dir (D6): a stray ``events.jsonl`` written there must not hijack
    auto-discovery, so there is no ROOT short-circuit — we fall through to the
    newest-task-folder glob.
    """
    if os.environ.get("ORCH_STATE_DIR"):
        return os.environ["ORCH_STATE_DIR"]
    import glob
    candidates = glob.glob(os.path.join(TASKS_ROOT, "*", "events.jsonl"))
    if candidates:
        return os.path.dirname(max(candidates, key=os.path.getmtime))
    return ROOT


STATE_DIR = os.path.abspath(resolve_state_dir())
EVENTS = os.path.join(STATE_DIR, "events.jsonl")
HTML = os.path.join(ROOT, "monitor.html")
DEFAULT_TASK = os.path.basename(STATE_DIR.rstrip(os.sep)) or "default"


def discover_tasks() -> dict:
    """name -> state dir, rescanned per request so tasks started later appear live."""
    tasks = {}
    try:
        for entry in sorted(os.listdir(TASKS_ROOT)):
            d = os.path.join(TASKS_ROOT, entry)
            if os.path.isdir(d):
                tasks[entry] = d
    except OSError:
        pass
    # Startup default (e.g. an $ORCH_STATE_DIR outside the tasks root) is
    # always selectable; same-named entry inside the root is the same dir.
    tasks.setdefault(DEFAULT_TASK, STATE_DIR)
    return tasks


# Full-replay results per events file, keyed by (mtime_ns, size) so a task
# that stopped emitting costs one scan total, not one per /tasks poll.
_STATUS_CACHE: dict = {}

# Unparseable lines per events file, surfaced via /health (R-10). A poisoned or
# torn line is skipped silently by the replay — a counter is what turns "the
# dashboard looks wrong" into "line N of this stream is not JSON". Counted once
# per scan (the scan itself is cached by (mtime_ns, size)).
PARSE_FAILURES: dict = {}


def replay_plan_states(events_path: str):
    """Replay one stream into ``(plan_states, last, tokens, parse_failures)``.

    Per-plan badge state is **last-event-wins in FILE ORDER** — the SD-4/R-58
    conflict rule: when a stream holds both a fabricated ``plan failed`` and a
    later corrective ``plan done`` for the same plan, the correction wins, and no
    stream is ever rewritten to achieve that. Order is file order, never a ts
    sort (ts values are written by several writers and are not monotonic).

    Continuation reopen (FRONTEND-6, server half): one task folder hosts several
    phases appending to one stream, so events can continue PAST a run-level
    ``orchestrator complete done``. Activity after a terminal orchestrator badge
    — a sub-plan ``plan`` event opening as running/queued (seed lines included),
    or any ``running``-state orchestrator event outside the reserved stages —
    flips the orchestrator badge back to ``running``, exactly as the replaying
    dashboard does. Without this the home-grid tile reads "done" while loops
    are visibly running.
    """
    plan_states: dict = {}
    last = None
    tok_in = tok_out = tok_cached = tok_write = 0
    failures = 0
    with open(events_path, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                failures += 1
                continue
            if not isinstance(ev, dict):
                failures += 1
                continue
            stage, state = ev.get("stage"), ev.get("state")
            if stage in ("plan", "complete"):
                plan_states[ev.get("plan")] = state
                if (stage == "plan" and ev.get("plan") != "orchestrator"
                        and state in ("running", "queued")
                        and plan_states.get("orchestrator") in ("done", "failed")):
                    plan_states["orchestrator"] = "running"
            elif (stage != "tokens" and state == "running"
                    and ev.get("plan") == "orchestrator"
                    and plan_states.get("orchestrator") in ("done", "failed")):
                plan_states["orchestrator"] = "running"
            tok = ev.get("tokens")
            if tok:
                tok_in += tok.get("in") or 0
                tok_out += tok.get("out") or 0
                tok_cached += tok.get("cached") or 0
                tok_write += tok.get("cache_write") or 0
            if not ev.get("quiet"):
                last = {k: ev[k] for k in ("ts", "plan", "stage", "state", "detail") if k in ev}
    tokens = {"in": tok_in, "out": tok_out, "cached": tok_cached, "cache_write": tok_write}
    return plan_states, last, tokens, failures


def health_payload() -> dict:
    """`/health`: liveness plus the per-stream parse-failure counters (R-10).

    The counters are a by-product of the `/tasks` stream scan (that is what makes
    them free), so a probe taken before the first scan honestly reports zero — it
    means "nothing scanned yet", not "no bad lines". A dashboard polls `/tasks`
    continuously, so every counter here is as current as that stream's last scan,
    and a stream that stops being scannable (deleted, rotated) drops out of the
    map instead of contributing forever.
    """
    return {"status": "ok",
            "parse_failures_total": sum(PARSE_FAILURES.values()),
            "parse_failures": dict(PARSE_FAILURES)}


def task_status(events_path: str) -> dict:
    """Overall run status + last meaningful event, for the home-grid tile.

    Replays the whole stream the same way the dashboard does — badge events
    (stage ``plan``/``complete``) set per-plan state, last-event-wins in file
    order, continuation activity reopens a stale orchestrator close — then folds
    those into one verdict. The reserved ``orchestrator`` card is authoritative:
    the run-level ``complete done|failed`` that nothing reopened marks the run
    finished. Until it lands, LIVE ACTIVITY WINS: any running plan means the
    flow is running (same rule as the stats page's flow tile — a plan that
    already exhausted its attempts must not flag the whole run failed while
    later loops are still working); with nothing running, a failed plan wins,
    else all-done folds to done.
    """
    try:
        st = os.stat(events_path)
    except OSError:
        # A stream that no longer exists (deleted or rotated after a poisoned
        # scan) must not keep contributing to `/health`'s parse_failures_total
        # for the life of the server — there is nothing left to fix (m-2).
        PARSE_FAILURES.pop(events_path, None)
        return {"status": "empty", "last": None, "tokens": {"in": 0, "out": 0}}
    key = (st.st_mtime_ns, st.st_size)
    cached = _STATUS_CACHE.get(events_path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        plan_states, last, tokens, failures = replay_plan_states(events_path)
    except OSError:
        PARSE_FAILURES.pop(events_path, None)  # unreadable now: same rule (m-2)
        return {"status": "empty", "last": None, "tokens": {"in": 0, "out": 0}}
    if failures:
        PARSE_FAILURES[events_path] = failures
    else:
        PARSE_FAILURES.pop(events_path, None)
    tok_in, tok_out = tokens["in"], tokens["out"]
    tok_cached, tok_write = tokens["cached"], tokens["cache_write"]
    orch = plan_states.get("orchestrator")
    plans = [s for p, s in plan_states.items() if p != "orchestrator"]
    if orch in ("done", "failed"):
        status = orch
    elif "running" in plans:
        status = "running"
    elif "failed" in plans:
        status = "failed"
    elif plans and all(s == "done" for s in plans):
        # every plan card closed but the driver never closed the
        # orchestrator card — effectively finished, show it as such
        status = "done"
    elif last:
        status = "running"
    else:
        status = "empty"
    payload = {"status": status, "last": last,
               "tokens": {"in": tok_in, "out": tok_out, "cached": tok_cached,
                          "cache_write": tok_write}}
    _STATUS_CACHE[events_path] = (key, payload)
    return payload


def tasks_payload() -> dict:
    entries = []
    for name, d in discover_tasks().items():
        events = os.path.join(d, "events.jsonl")
        try:
            st = os.stat(events)
            entries.append({"name": name, "events": True, "mtime": st.st_mtime,
                            **task_status(events)})
        except OSError:
            entries.append({"name": name, "events": False, "mtime": 0,
                            "status": "empty", "last": None, "tokens": {"in": 0, "out": 0}})
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return {"default": DEFAULT_TASK, "tasks": entries}


def resolve_task_dir(query: str) -> str:
    """State dir for ?task=<name>; names only match discovered dirs (no traversal)."""
    task = urllib.parse.parse_qs(query).get("task", [None])[0]
    return discover_tasks().get(task, STATE_DIR)


def resolve_events_path(query: str) -> str:
    return os.path.join(resolve_task_dir(query), "events.jsonl")


# Task-page artifacts: the final HTML report plus agent-written .md handoff
# notes (findings/, reviews/, plan/, ...). Extension-whitelisted both when
# listing and when serving.
ARTIFACT_EXTS = {".md", ".html", ".htm"}


def task_artifacts(state_dir: str) -> list:
    """List report HTMLs + .md notes under a task folder, report(s) first.

    Hidden entries and __pycache__ are skipped and paths are task-relative
    with forward slashes. The walk is bounded (depth 4, 300 files) so a
    runaway folder cannot stall the endpoint.
    """
    out = []
    base = os.path.realpath(state_dir)
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth >= 4:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d != "__pycache__")
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if fn.startswith(".") or ext not in ARTIFACT_EXTS:
                continue
            try:
                st = os.stat(os.path.join(dirpath, fn))
            except OSError:
                continue
            rel = fn if rel_dir == "." else os.path.join(rel_dir, fn)
            out.append({"path": rel.replace(os.sep, "/"),
                        "kind": "note" if ext == ".md" else "report",
                        "size": st.st_size, "mtime": st.st_mtime})
            if len(out) >= 300:
                return sorted(out, key=lambda a: (a["kind"] != "report", a["path"]))
    out.sort(key=lambda a: (a["kind"] != "report", a["path"]))
    return out


def safe_artifact_path(state_dir: str, rel: str):
    """Absolute path for a task-relative artifact, or None if not servable.

    Extension whitelist + realpath containment in the task dir, so a hostile
    ``?path=`` (.. traversal, absolute path, or a symlink pointing outside)
    can never read beyond the task folder.
    """
    if not rel or os.path.splitext(rel)[1].lower() not in ARTIFACT_EXTS:
        return None
    base = os.path.realpath(state_dir)
    full = os.path.realpath(os.path.join(base, rel))
    if not full.startswith(base + os.sep):
        return None
    return full if os.path.isfile(full) else None


def read_config() -> dict:
    for base in (STATE_DIR, ROOT):
        try:
            with open(os.path.join(base, "orch-config.json")) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def resolve_port() -> int:
    """Port: argv > $ORCH_PORT > orch-config.json (state dir, then module dir) > 8931.

    A non-integer argv/env exits cleanly with a one-line message rather than a
    raw ``ValueError`` traceback at import (SERVER-2).
    """
    for source, label in ((sys.argv[1] if len(sys.argv) > 1 else None, "argv"),
                          (os.environ.get("ORCH_PORT"), "ORCH_PORT")):
        if source:
            try:
                return int(source)
            except ValueError:
                sys.exit(f"invalid port from {label}: {source!r}")
    try:
        return int(read_config().get("port") or 8931)
    except (TypeError, ValueError):
        return 8931


PORT = resolve_port()
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

STATS = {"started": time.monotonic(), "ws_clients": 0, "ws_active": 0,
         "events_sent": 0, "page_hits": 0}


def stats_line() -> str:
    up = int(time.monotonic() - STATS["started"])
    uptime = (f"{up // 3600}h{up % 3600 // 60:02d}m" if up >= 3600
              else f"{up // 60}m{up % 60:02d}s" if up >= 60 else f"{up}s")
    try:
        with open(EVENTS, "rb") as f:
            event_count = sum(1 for line in f if line.strip())
    except OSError:
        event_count = 0
    return (f"stopped after {uptime} · {STATS['ws_clients']} ws clients "
            f"({STATS['ws_active']} still connected) · {STATS['events_sent']:,} events streamed · "
            f"{STATS['page_hits']} page loads · {event_count} events in default task ({EVENTS})")


def ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += n.to_bytes(2, "big")
    else:
        header.append(127)
        header += n.to_bytes(8, "big")
    return bytes(header) + payload


def parse_client_frames(buf: bytearray) -> bool:
    """Consume whole client->server frames from ``buf`` in place.

    Returns True if a CLOSE frame (opcode 0x8) was seen. Client frames are
    always masked (RFC 6455); we only need enough parsing to detect CLOSE and
    to skip over pong/other frames so a following CLOSE in the same read is not
    missed. Incomplete trailing bytes are left in ``buf`` for the next read.
    """
    saw_close = False
    while len(buf) >= 2:
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
        if masked:
            idx += 4  # 4-byte masking key
        if len(buf) < idx + length:
            break  # frame body not fully arrived yet
        del buf[:idx + length]
        if opcode == 0x8:
            saw_close = True
    return saw_close


async def drain_client(reader: asyncio.StreamReader, closed: asyncio.Event) -> None:
    """Discard incoming frames (pongs); flag close on CLOSE frame/EOF so the writer stops."""
    buf = bytearray()
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            buf += data
            if parse_client_frames(buf):
                break
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        closed.set()


def read_frames(events_path: str, offset: int):
    """Read complete newline-terminated records appended since ``offset``.

    Returns ``(frames, new_offset)`` where ``frames`` is the list of stripped
    non-empty lines up to the last ``\\n`` and ``new_offset`` advances by exactly
    those bytes, leaving any incomplete trailing line for the next tick (D5).
    A negative sentinel offset of ``-1`` signals truncation (``size < offset``);
    the caller closes the stream so the client reconnects cleanly (D10).
    """
    try:
        size = os.path.getsize(events_path)
    except OSError:
        return [], offset
    if size < offset:  # truncated/rotated under a live socket
        return [], -1
    if size <= offset:
        return [], offset
    with open(events_path, "rb") as f:
        f.seek(offset)
        data = f.read()
    nl = data.rfind(b"\n")
    if nl == -1:
        return [], offset  # no complete line yet; defer the partial tail
    complete = data[:nl + 1]
    frames = [line for line in (ln.strip() for ln in complete.splitlines()) if line]
    return frames, offset + len(complete)


async def stream_events(reader, writer, events_path: str) -> None:
    closed = asyncio.Event()
    drainer = asyncio.create_task(drain_client(reader, closed))
    offset = 0
    ticks = 0
    try:
        while not closed.is_set():
            frames, new_offset = await asyncio.to_thread(read_frames, events_path, offset)
            if new_offset == -1:  # truncation: close so the client rebuilds cleanly (D10)
                break
            offset = new_offset
            if frames:
                for line in frames:
                    writer.write(ws_frame(line))
                    STATS["events_sent"] += 1
                await writer.drain()
            ticks += 1
            if ticks % 40 == 0:  # ~20s keepalive ping
                writer.write(ws_frame(b"", 0x9))
                await writer.drain()
            await asyncio.sleep(0.5)
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        drainer.cancel()
        try:  # best-effort CLOSE frame so a conforming client tears down cleanly
            writer.write(ws_frame(b"", 0x8))
            await writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass


CONNECTIONS: set = set()  # live handler tasks, cancelled on shutdown


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    task = asyncio.current_task()
    CONNECTIONS.add(task)
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except Exception:
        CONNECTIONS.discard(task)
        writer.close()
        return
    head = raw.decode("latin1")
    request_line = head.split("\r\n", 1)[0].split(" ")
    path = request_line[1] if len(request_line) > 1 else "/"
    route, _, query = path.partition("?")
    headers = {}
    for ln in head.split("\r\n")[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    try:
        if route == "/ws" and (
            "sec-websocket-key" not in headers
            or headers.get("sec-websocket-version") not in (None, "13")
        ):
            # Malformed/unsupported upgrade: never serve the HTML body on /ws.
            # Missing key -> 400; wrong version -> 426 advertising 13 (SERVER-3).
            if headers.get("sec-websocket-version") not in (None, "13"):
                writer.write(
                    b"HTTP/1.1 426 Upgrade Required\r\n"
                    b"Sec-WebSocket-Version: 13\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            else:
                writer.write(
                    b"HTTP/1.1 400 Bad Request\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            await writer.drain()
        elif route == "/ws" and "sec-websocket-key" in headers:
            accept = base64.b64encode(
                hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest()
            ).decode()
            writer.write(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            STATS["ws_clients"] += 1
            STATS["ws_active"] += 1
            try:
                await stream_events(reader, writer, resolve_events_path(query))
            finally:
                STATS["ws_active"] -= 1
        elif route in ("/health", "/tasks", "/artifacts"):
            # tasks_payload/task_artifacts rescan disk (possibly multi-MB files);
            # run them off the event loop so they never stall live WS streams (SERVER-5).
            if route == "/health":
                payload = health_payload()
            elif route == "/tasks":
                payload = await asyncio.to_thread(tasks_payload)
            else:
                arts = await asyncio.to_thread(task_artifacts, resolve_task_dir(query))
                payload = {"artifacts": arts}
            body = json.dumps(payload).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
        elif route == "/file":
            qs = urllib.parse.parse_qs(query)
            full = safe_artifact_path(resolve_task_dir(query), qs.get("path", [""])[0])
            body = None
            if full:
                try:
                    body = await asyncio.to_thread(lambda: open(full, "rb").read())
                except OSError:
                    body = None
            if body is None:
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: 9\r\nConnection: close\r\n\r\nnot found"
                )
            else:
                if full.lower().endswith(".md"):
                    # served as plain text: the dashboard fetches and renders
                    # the preview itself with its escape-first mini renderer
                    extra = b"Content-Type: text/plain; charset=utf-8\r\n"
                else:
                    # report HTML renders in a new tab; CSP sandbox keeps its
                    # scripts in an opaque origin, cut off from this server
                    extra = (b"Content-Type: text/html; charset=utf-8\r\n"
                             b"Content-Security-Policy: sandbox allow-scripts\r\n")
                writer.write(
                    b"HTTP/1.1 200 OK\r\n" + extra +
                    b"X-Content-Type-Options: nosniff\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
                )
            await writer.drain()
        else:
            STATS["page_hits"] += 1
            try:
                with open(HTML, "rb") as f:
                    body = f.read()
            except OSError:
                body = b"monitor.html missing"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        CONNECTIONS.discard(task)
        try:
            writer.close()
        except Exception:
            pass


async def main() -> None:
    try:
        server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    except OSError as exc:
        sys.exit(f"cannot bind 0.0.0.0:{PORT} ({exc}); is another monitor_server "
                 f"still running? stop it with: pkill -f \"[m]onitor_server\"")
    print(f"monitor listening on 0.0.0.0:{PORT}", flush=True)
    print(f"state dir: {STATE_DIR}", flush=True)
    print(f"events:    {EVENTS}", flush=True)

    stop = asyncio.Event()

    def confirm_stop() -> None:
        # Ctrl-C: confirm on a TTY; stop immediately when non-interactive.
        # input() briefly blocks the event loop — fine for a local dev tool.
        if stop.is_set():
            return
        if not sys.stdin.isatty():
            stop.set()
            return
        try:
            answer = input("\nStop monitor_server? [y/N] ").strip().lower()
        except (EOFError, RuntimeError):
            answer = "y"
        if answer in ("y", "yes"):
            stop.set()
        else:
            print("continuing", flush=True)

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, confirm_stop)
        loop.add_signal_handler(signal.SIGTERM, stop.set)  # no prompt on kill
    except NotImplementedError:
        pass  # non-Unix: fall back to default KeyboardInterrupt behavior
    await stop.wait()
    # server.close() alone isn't enough: wait_closed() (and Server.__aexit__)
    # blocks until every open connection finishes, and websocket streams never
    # end on their own — cancel them explicitly so shutdown can't hang.
    server.close()
    for task in list(CONNECTIONS):
        task.cancel()
    await asyncio.gather(*CONNECTIONS, return_exceptions=True)
    await server.wait_closed()
    print(stats_line(), flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Only reachable where signal handlers are unavailable — still exit clean.
        print(stats_line(), flush=True)
