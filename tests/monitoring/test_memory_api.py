#!/usr/bin/env python3
"""The memory feature, end to end, against a BOOTED monitor_server (item I15).

Run: ``python3 test_memory_api.py`` (stdlib only, no pytest, non-zero on
failure). `run_all.sh` picks it up by its `test_*.py` glob; GD-U6 keeps it out of
the payload.

WHY A SECOND FILE, AND WHAT IT ADDS
-----------------------------------
`tests/monitoring/test_server.py` owns the per-rule coverage of monitor_server's
memory code, and says so at its own anchor: it drives `ms.handle` with
`ms.MEMORY_ROOT` / `ms.MEMORY_WRITE` *swapped* to a throwaway tree. That is the
right shape for "does this rule fire", and it is structurally unable to see four
things — which is what this file is for (I15, and the hand-off `test_server.py`
and `tests/test_server_core.py` both write down):

1. **The constants as the module itself resolves them at boot.** Everything the
   file plane's posture rests on — `MEMORY_ROOT` from the project ladder,
   `MEMORY_WRITE` from the flag/env, `memory_unavailable()`'s three refusals — is
   computed once, at import. A test that assigns those globals proves the rules
   downstream of them and nothing about the wiring: `--allow-memory-write` could
   stop reaching `MEMORY_WRITE`, or the ladder could resolve a root nobody meant,
   and every patched arm would stay green. So this file IMPORTS THE MODULE FOUR
   TIMES, once per posture (write-on, default-off, a root inside `~/.claude`, a
   root inside a plugin cache), each with the environment that posture describes,
   and then only ever speaks HTTP to it.
2. **One coherent life, and its cumulative state on disk.** create → read →
   save → save again → a real 409 → delete → trash, as one story, then the
   *directory* is inspected: 0700/0600 at every level, `.history/` holding the
   bytes that were replaced, `.trash/` holding the bytes that were deleted, the
   audit log's lines — and NO `events.jsonl` anywhere, which is the one assertion
   PROTOCOL-20/R-58 turn on and which no single-rule arm makes.
3. **The cross-server `FILE_CSP` byte-equality** GD-20 calls a verbatim twin.
   Neither server's own suite can make it: this is the only file that imports
   both.
4. **One namespace, three spellings.** The flat-name regex and the index budget
   exist in monitor_server.py, in memory.html and in
   `tests/test_memory_hygiene.py`. Each file pins its own copy; nothing compares
   them, and a drift there is a directory the editor offers to write and the git
   carve then publishes (or the reverse).

House rules honoured: ephemeral ports only (`asyncio.start_server(..., 0)`), so
the live monitor on 8931 — which is watching the very run that produced this
file — is never bound; a throwaway `$HOME` and a throwaway project, so no arm can
read or write the repository's own `.touch/memory` (a directory a real session
loads as instructions) or the operator's real settings; and `$ORCH_STATE_DIR`
deliberately OUTSIDE the throwaway project, so "nothing under the project is an
events.jsonl" is a statement about the code rather than about this file's setup.
"""
import asyncio
import atexit
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
sys.path.insert(0, os.path.dirname(HERE))
from _roots import MON, PAYLOAD, REPO                   # noqa: E402

MODULE_PATH = os.path.join(str(MON), "monitor_server.py")
MEMORY_HTML = os.path.join(str(MON), "memory.html")
MONITOR_HTML = os.path.join(str(MON), "monitor.html")
HYGIENE_TEST = os.path.join(str(REPO), "tests", "test_memory_hygiene.py")

_TMP_BASE = os.environ.get("TMPDIR") or "/tmp/claude-1000"
os.makedirs(_TMP_BASE, exist_ok=True)
BASE = tempfile.mkdtemp(prefix="memapi-", dir=_TMP_BASE)
atexit.register(shutil.rmtree, BASE, ignore_errors=True)

# A throwaway HOME. Two things read it and both must be deterministic: the
# `~/.claude` ancestor refusal (Part D-9) and the USER settings layer that
# `aligned` falls back to — an autoMemoryDirectory in the operator's own
# ~/.claude/settings.json would otherwise decide this file's alignment arms.
FAKE_HOME = os.path.join(BASE, "home")
os.makedirs(os.path.join(FAKE_HOME, ".claude"), mode=0o700)
os.environ["HOME"] = FAKE_HOME

#: The project the write-on and default-off instances both serve. `.claude/` is
#: the marker (G10: the marker dir and the state dir are deliberately different)
#: and holds `settings.local.json` for the alignment arms. `.touch/` is
#: deliberately ABSENT: the first write has to create it, which is how the
#: every-level 0700 property (SECURITY-15) becomes observable.
PROJECT = os.path.join(BASE, "proj")
os.makedirs(os.path.join(PROJECT, ".claude"))
SETTINGS_LOCAL = os.path.join(PROJECT, ".claude", "settings.local.json")
ROOT = os.path.join(PROJECT, ".touch", "memory")
AUDIT = os.path.join(PROJECT, ".touch", "memory-audit.jsonl")

#: Outside the project on purpose (see the module docstring's point 2). It also
#: holds the one artifact the `/file` referrer arm needs.
STATE_DIR = os.path.join(BASE, "state", "t1")
os.makedirs(os.path.join(STATE_DIR, "report"))
with open(os.path.join(STATE_DIR, "report", "r.html"), "w", encoding="utf-8") as _fh:
    _fh.write("<h1>a report an agent wrote</h1>")

ENV_OVERRIDES = ("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
                 "CLAUDE_CODE_REMOTE_MEMORY_DIR", "CLAUDE_MEMORY_STORES")
for _name in ENV_OVERRIDES + ("ORCH_PORT", "ORCH_TASKS_ROOT", "ORCH_ALLOW_ORIGIN",
                              "ORCH_ALLOW_HOST", "TOUCH_PROJECT_CWD"):
    os.environ.pop(_name, None)


def _load(name, *, project, write, argv=None):
    """Import monitor_server.py FRESH, with the environment of one posture.

    Every constant the file plane's posture rests on is computed at import
    (`MEMORY_ROOT`, `MEMORY_WRITE`, `TOKEN`, `PORT`, `HOSTS`), so a posture is a
    module instance and not an assignment. `sys.argv` is set too, because
    `--allow-memory-write` is a real carrier and "the flag never reached the
    constant" is exactly the class of bug a patched global hides.

    The call-time environment is left pointing at `PROJECT`/`STATE_DIR`
    afterwards: `memory_settings_value()` re-reads `$CLAUDE_PROJECT_DIR` on every
    alignment answer, so the two main instances must agree about which project
    they are describing. The two refusal instances capture their root at import
    and never need the env again (their refusals come from `MEMORY_ROOT` and
    `expanduser("~")`).
    """
    saved_argv = sys.argv
    os.environ["CLAUDE_PROJECT_DIR"] = project
    os.environ["ORCH_STATE_DIR"] = STATE_DIR
    if write:
        os.environ["TOUCH_ALLOW_MEMORY_WRITE"] = "1"
    else:
        os.environ.pop("TOUCH_ALLOW_MEMORY_WRITE", None)
    sys.argv = ["monitor_server.py"] + list(argv or [])
    try:
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.argv = saved_argv
        os.environ["CLAUDE_PROJECT_DIR"] = PROJECT
        os.environ.pop("TOUCH_ALLOW_MEMORY_WRITE", None)
    return mod


#: The write plane ON, by environment variable.
MS = _load("ms_memory_write_on", project=PROJECT, write=True)
#: The DEFAULT posture: no flag, no env var (G6/SECURITY-1/W14).
MS_OFF = _load("ms_memory_write_off", project=PROJECT, write=False)

SKIPS = []


def _skip(message):
    """One skip, on the wire convention `run_all.sh` counts (`skip` first).

    Two files this one compares against are owned by other sub-plans of the same
    plan (`memory.html`, `tests/test_memory_hygiene.py`). Their absence is "that
    half has not landed here", which is the honest answer in a tree where it has
    not — and it is why those comparisons skip rather than fail: a suite that goes
    red because another owner's file is not in this checkout teaches people to
    ignore red.
    """
    SKIPS.append(message)
    print(f"  skip: {message}")


# --------------------------------------------------------------------------
# One request, one ephemeral server, over a real socket.
# --------------------------------------------------------------------------

def _http(mod, method, target, *, body=None, raw_body=None, headers=(),
          query_token=False, header_token=True, token=None, origin=True,
          content_type="application/json", write_marker=None,
          length=None, omit_length=False):
    """`(status, headers, body_bytes)`. Defaults are what the PAGE sends.

    A browser sets `Origin` on a `fetch` it makes to its own origin and `Host`
    from the URL; memory.html adds `X-Orch-Token` and `X-Touch-Write: 1` itself.
    Every one of those is switchable here because every one of them is a rule
    with its own arm (G5, W2/W3/W4).

    `length`/`omit_length` exist for the two body rules that are about the
    HEADER rather than the bytes: a missing `Content-Length` is a 411, and a
    length over the cap is a 413 the server must answer WITHOUT reading (so this
    helper sends the head and no body, and a 413 that arrives proves the order).
    """
    if write_marker is None:
        write_marker = method in ("POST", "PUT", "DELETE")

    async def run():
        server = await asyncio.start_server(mod.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            path = target
            if query_token:
                path += ("&" if "?" in path else "?") + f"token={mod.TOKEN}"
            head = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}",
                    "Connection: close"]
            if header_token:
                head.append(f"X-Orch-Token: {token or mod.TOKEN}")
            if write_marker:
                head.append("X-Touch-Write: 1")
            if origin is True:
                head.append(f"Origin: http://127.0.0.1:{port}")
            elif origin:
                head.append(f"Origin: {origin}")
            raw = raw_body if raw_body is not None else (
                b"" if body is None else json.dumps(body).encode())
            if content_type and (raw or method in ("POST", "PUT")):
                head.append(f"Content-Type: {content_type}")
            if not omit_length:
                head.append(f"Content-Length: {len(raw) if length is None else length}")
            head.extend(headers)
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + raw)
            await writer.drain()
            data = await asyncio.wait_for(reader.read(-1), 15)
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

    return asyncio.run(run())


def _json(out):
    """The JSON body of an answer, asserting the transport rules that ride along.

    `memory.html` checks the content type BEFORE it parses (UI-1/UI-4), so an
    HTML or text/plain error would be reported to the operator as "this build has
    no memory API". `no-store` and "no CORS header, ever" are checked here rather
    than in one arm each, because they are properties of every answer on the
    plane (G5, W2).
    """
    status, headers, body = out
    assert "application/json" in headers.get("content-type", ""), \
        (status, headers.get("content-type"), body[:120])
    assert headers.get("cache-control") == "no-store", headers
    for key in headers:
        assert not key.startswith("access-control-"), \
            f"the memory group must never emit a CORS header ({key})"
    return status, json.loads(body)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _clear_root():
    """Take the shared memory root back to "not there yet"."""
    shutil.rmtree(ROOT, ignore_errors=True)
    for path in (AUDIT, SETTINGS_LOCAL):
        if os.path.exists(path):
            os.unlink(path)


def _create(name, text, mod=MS, allow_pinned=None):
    payload = {"content": text}
    if allow_pinned is not None:
        payload["allowPinned"] = allow_pinned
    return _json(_http(mod, "POST", f"/api/memory/file?name={name}", body=payload))


def _slice_const(source, pattern, label):
    found = re.search(pattern, source)
    assert found, f"{label}: not found — the spelling this test compares moved"
    return found.group(1)


# --------------------------------------------------------------------------
# 1. The posture: DEFAULT-OFF, and the flag has to reach the constant.
# --------------------------------------------------------------------------

def test_the_write_plane_is_off_until_a_flag_or_an_env_var_says_otherwise():
    """G6/SECURITY-1/W14, asserted on the boot-resolved constant, not a patch.

    A user who installed a read-only, loopback, token-gated dashboard cannot be
    talked into a write surface by a leaked token — so the OFF posture must be
    what an unconfigured import produces, and the flag must be what changes it.
    """
    _clear_root()
    assert MS.MEMORY_WRITE is True, "TOUCH_ALLOW_MEMORY_WRITE=1 must reach MEMORY_WRITE"
    assert MS_OFF.MEMORY_WRITE is False, \
        "an import with no flag and no env var must leave the write plane OFF"
    # Both instances resolved the same root from the project ladder — and it is
    # the one G1/G10 name, `<project>/.touch/memory`, not a state-root derivative.
    assert MS.MEMORY_ROOT == MS_OFF.MEMORY_ROOT == ROOT, (MS.MEMORY_ROOT, ROOT)
    assert MS.memory_unavailable() == "", MS.memory_unavailable()

    # The argv carrier, which is the one `touch-monitor --allow-memory-write`
    # actually uses. The flag must NOT be read as a port on the way through
    # (`positional_args` skips `-` arguments), so PORT is asserted too.
    flagged = _load("ms_memory_write_flag", project=PROJECT, write=False,
                    argv=["--allow-memory-write"])
    assert flagged.MEMORY_WRITE is True, "--allow-memory-write must turn the plane on"
    assert flagged.PORT == MS.PORT, (flagged.PORT, MS.PORT)

    # Reads are available in the off posture; every write verb is a 403 that
    # NAMES the flag, and nothing lands on disk.
    sha = _sha("hi\n")
    MS.memory_makedirs(ROOT)
    with open(os.path.join(ROOT, "off.md"), "w", encoding="utf-8") as handle:
        handle.write("hi\n")
    status, body = _json(_http(MS_OFF, "GET", "/api/memory/file?name=off.md",
                               query_token=True, header_token=False))
    assert status == 200 and body["sha256"] == sha, body
    status, body = _json(_http(MS_OFF, "GET", "/api/memory/list"))
    assert status == 200 and body["memoryWrite"] is False, body
    # `writable` and `memoryWrite` stay two booleans: the page words its disabled
    # affordance from whichever is false, and a conflated field prints the wrong
    # reason for a true refusal (G6/UI-6).
    assert body["writable"] is True, body
    for method, target, payload in (
            ("POST", "/api/memory/file?name=new.md", {"content": "x\n"}),
            ("PUT", "/api/memory/file?name=off.md",
             {"content": "x\n", "ifMatch": sha}),
            ("DELETE", f"/api/memory/file?name=off.md&ifMatch={sha}", None)):
        status, body = _json(_http(MS_OFF, method, target, body=payload))
        assert status == 403 and body["category"] == "write-plane-off", (method, body)
        assert "--allow-memory-write" in body["reason"], body
    assert not os.path.exists(os.path.join(ROOT, "new.md"))
    with open(os.path.join(ROOT, "off.md"), encoding="utf-8") as handle:
        assert handle.read() == "hi\n", "the off posture wrote to disk"
    assert not os.path.exists(AUDIT), "a refused write must not write an audit line"

    # /health says the same thing to a supervisor, as a string, with no path.
    status, headers, raw = _http(MS_OFF, "GET", "/health", header_token=False,
                                 origin=False)
    assert status == 200, status
    assert json.loads(raw)["memoryWrite"] == "off", raw[:200]
    status, _, raw = _http(MS, "GET", "/health", header_token=False, origin=False)
    assert json.loads(raw)["memoryWrite"] == "on", raw[:200]


# --------------------------------------------------------------------------
# 2. One file's whole life, and the directory it leaves behind.
# --------------------------------------------------------------------------

def test_a_memory_file_lives_a_whole_life_over_real_http():
    """create -> read -> save -> save again -> 409 -> delete, as one story.

    The interesting assertions are the ones only a sequence can make: that the
    sha a save RETURNS is the sha the next save may present (the adoption
    contract memory.html's editor depends on — without it, save-then-save 409s
    against the operator's own previous write and trains them to click
    "overwrite"), that the bytes replaced along the way are all still recoverable
    from `.history`, and that the whole story leaves nothing that looks like a
    plan event.
    """
    _clear_root()
    name = "topic-note.md"
    url = f"/api/memory/file?name={name}"

    # An empty root lists as empty rather than failing: the directory does not
    # exist yet, and `writable` is about the nearest existing ancestor (D13 read
    # the other way round — a create affordance the server would honour).
    status, body = _json(_http(MS, "GET", "/api/memory/list"))
    assert status == 200 and body["files"] == [] and body["count"] == 0, body
    assert body["root"] == ROOT and body["writable"] is True, body
    assert not os.path.isdir(ROOT), "listing must not create the root"
    assert body["limits"] == {"maxBytes": MS.MAX_MEMORY_BYTES,
                              "maxFiles": MS.MAX_MEMORY_FILES,
                              "indexLines": MS.MEM_INDEX_LINES,
                              "indexBytes": MS.MEM_INDEX_BYTES}, body["limits"]

    # --- create ---
    first = "one\n"
    status, body = _create(name, first)
    assert status == 201, (status, body)
    assert body == {"name": name, "size": len(first.encode()),
                    "sha256": _sha(first), "mtime_ns": body["mtime_ns"]}, body
    full = os.path.join(ROOT, name)
    with open(full, encoding="utf-8") as handle:
        assert handle.read() == first
    # SECURITY-15: EVERY level 0700, not just the leaf — `.touch/` sits one level
    # above the memory root and holds the per-boot token and the Mongo password.
    assert _mode(ROOT) == 0o700, oct(_mode(ROOT))
    assert _mode(os.path.dirname(ROOT)) == 0o700, oct(_mode(os.path.dirname(ROOT)))
    assert _mode(full) == 0o600, oct(_mode(full))
    # ...and a create never overwrites (409 `exists`, not a silent save).
    status, body = _create(name, "clobber\n")
    assert status == 409 and body["category"] == "exists", body
    with open(full, encoding="utf-8") as handle:
        assert handle.read() == first

    # --- read: the shape the page's editor loads from ---
    status, read = _json(_http(MS, "GET", url, query_token=True, header_token=False))
    assert status == 200, read
    assert set(read) == {"name", "content", "size", "sha256", "mtime_ns",
                         "hasFrontmatter"}, read
    assert read["content"] == first and read["sha256"] == _sha(first)
    assert read["hasFrontmatter"] is False, read

    # --- save, then save again with the sha the server just returned ---
    second = "two\n"
    status, saved = _json(_http(MS, "PUT", url,
                                body={"content": second, "ifMatch": read["sha256"]}))
    assert status == 200 and saved["sha256"] == _sha(second), saved
    third = "three\n"
    status, again = _json(_http(MS, "PUT", url,
                                body={"content": third, "ifMatch": saved["sha256"]}))
    assert status == 200, (status, again)
    assert again["sha256"] == _sha(third), again

    # --- the precondition, both refusals ---
    status, body = _json(_http(MS, "PUT", url, body={"content": "x\n"}))
    assert status == 412 and body["category"] == "no-precondition", body
    status, body = _json(_http(MS, "PUT", url,
                               body={"content": "x\n", "ifMatch": "*"}))
    assert status == 412, (status, body)
    status, body = _json(_http(MS, "PUT", url,
                               body={"content": "x\n", "ifMatch": _sha(first)}))
    assert status == 409 and body["category"] == "precondition", body
    # The 409 publishes the state the page needs for reload / show-both /
    # overwrite — a bare retry is what makes a conflict unresolvable (UI-3).
    assert body["sha256"] == _sha(third) and body["content"] == third, body
    assert body["size"] == len(third.encode()) and body["mtime_ns"] > 0, body
    with open(full, encoding="utf-8") as handle:
        assert handle.read() == third, "a refused save must not have written"

    # --- every replaced byte-set is still recoverable ---
    history = os.path.join(ROOT, ".history", name)
    kept = sorted(os.listdir(history))
    bodies = []
    for leaf in kept:
        assert _mode(os.path.join(history, leaf)) == 0o600, leaf
        with open(os.path.join(history, leaf), encoding="utf-8") as handle:
            bodies.append(handle.read())
    assert sorted(bodies) == [first, second], bodies
    assert _mode(os.path.join(ROOT, ".history")) == 0o700

    # --- delete is a MOVE, and needs the same precondition a save does ---
    status, body = _json(_http(MS, "DELETE", url))
    assert status == 412 and body["category"] == "no-precondition", body
    status, body = _json(_http(MS, "DELETE", url + f"&ifMatch={_sha(first)}"))
    assert status == 409, (status, body)
    status, body = _json(_http(MS, "DELETE", url + f"&ifMatch={_sha(third)}"))
    assert status == 200 and body["deleted"] is True, body
    trash = os.path.join(ROOT, body["trash"])
    assert body["trash"].startswith(".trash" + os.sep + name + os.sep), body
    with open(trash, encoding="utf-8") as handle:
        assert handle.read() == third, "the deleted bytes must be in the trash"
    assert not os.path.exists(full), "delete left the file behind"

    # --- the list never lists the write path's own bookkeeping ---
    status, body = _json(_http(MS, "GET", "/api/memory/list"))
    assert [row["name"] for row in body["files"]] == [], body["files"]
    assert body["listTruncated"] is False, body

    # --- the audit log: one line per mutation, `w` on every one, no content ---
    with open(AUDIT, encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle if line.strip()]
    assert [line["op"] for line in lines] == ["create", "update", "update",
                                              "delete"], lines
    for line in lines:
        assert set(line) == {"ts", "op", "name", "bytes", "sha256", "w"}, line
        assert line["w"] == "monitor" and line["name"] == name, line
    assert _mode(AUDIT) == 0o600, oct(_mode(AUDIT))

    # --- PROTOCOL-20 / R-58 / Part D-6: a memory edit is not a plan card ---
    strays = []
    for where, _dirs, files in os.walk(PROJECT):
        strays += [os.path.join(where, f) for f in files if f == "events.jsonl"]
    assert not strays, f"a memory edit must append to no events.jsonl: {strays}"
    assert not os.path.exists(os.path.join(STATE_DIR, "events.jsonl")), \
        "a memory edit must not fabricate a plan card in the task folder"


# --------------------------------------------------------------------------
# 3. The transport gates, one refusal at a time.
# --------------------------------------------------------------------------

def test_every_transport_gate_refuses_on_its_own():
    """G5/W2/W3/W4/SERVER-1/2/7, over the wire, in the OFF-by-default order.

    Each of these is a separate wall, and a wall that only holds because another
    one already refused is a wall nobody tested. The order matters too: a write
    with a bad token must not be told whether the write plane is on, so the
    token gate answers first.
    """
    _clear_root()
    MS.memory_makedirs(ROOT)
    with open(os.path.join(ROOT, "gate.md"), "w", encoding="utf-8") as handle:
        handle.write("gate\n")
    sha = _sha("gate\n")
    write = "/api/memory/file?name=gate.md"
    payload = {"content": "changed\n", "ifMatch": sha}

    # the method table: a known route on the wrong verb is 405 + Allow
    status, headers, raw = _http(MS, "POST", "/api/memory/list", body={"x": 1})
    assert status == 405, (status, raw[:120])
    assert headers.get("allow") == "GET", headers
    status, headers, _ = _http(MS, "PATCH", write, body=payload)
    assert status == 405 and "PUT" in headers.get("allow", ""), headers

    # an unknown route under the prefix is a JSON 404 — never the 151 KB page,
    # which a client's res.json() turns into a silent empty render (UI-1)
    status, body = _json(_http(MS, "GET", "/api/memory/fiel?name=gate.md"))
    assert status == 404 and body["category"] == "unknown-route", body

    # the token: query carrier on a read, header ONLY on a write (W4)
    status, body = _json(_http(MS, "PUT", write, body=payload,
                               query_token=True, header_token=False))
    assert status == 401 and body["category"] == "unauthorized", body
    status, body = _json(_http(MS, "PUT", write, body=payload, token="wrong"))
    assert status == 401, (status, body)
    status, headers, _ = _http(MS, "PUT", write, body=payload, token="wrong")
    assert "bearer" in headers.get("www-authenticate", "").lower(), headers

    # the write marker: a simple cross-origin request cannot set it, and the
    # preflight it forces has nothing to succeed against (no CORS, ever)
    status, body = _json(_http(MS, "PUT", write, body=payload, write_marker=False))
    assert status == 403 and body["category"] == "write-marker", body

    # Origin/Host on a PLAIN HTTP route, not only on the /ws upgrade
    status, body = _json(_http(MS, "PUT", write, body=payload,
                               origin="http://evil.example"))
    assert status == 403 and body["category"] == "origin", body
    status, body = _json(_http(MS, "PUT", write, body=payload, origin=False))
    assert status == 403 and body["category"] == "origin", body
    # ...and a MISSING Origin is still fine on a read: rule 3 is for non-browser
    # clients that already presented the token (curl, and this suite).
    status, body = _json(_http(MS, "GET", "/api/memory/file?name=gate.md",
                               origin=False))
    assert status == 200, (status, body)

    # the body rules
    status, body = _json(_http(MS, "PUT", write, body=payload,
                               content_type="text/plain"))
    assert status == 415 and body["category"] == "content-type", body
    status, body = _json(_http(MS, "PUT", write, raw_body=b"{}", omit_length=True))
    assert status == 411 and body["category"] == "no-length", body
    status, body = _json(_http(MS, "PUT", write, raw_body=b"{}",
                               headers=["Transfer-Encoding: chunked"]))
    assert status == 400 and body["category"] == "chunked", body
    # cap-BEFORE-read: the head claims more than the cap and no body follows, so
    # an answer at all proves the length was checked before a byte was awaited.
    status, body = _json(_http(MS, "PUT", write, raw_body=b"",
                               length=MS.MAX_MEMORY_BODY_BYTES + 1))
    assert status == 413 and body["category"] == "body-too-large", body
    status, body = _json(_http(MS, "PUT", write, raw_body=b"not json",
                               content_type="application/json"))
    assert status == 400 and body["category"] == "bad-json", body
    status, body = _json(_http(MS, "PUT", write, raw_body=b'"a string"'))
    assert status == 400 and body["category"] == "bad-json", body

    # the flat namespace, before any filesystem call (G7 step 1)
    for name in ("..%2fx.md", "%2Fetc%2Fpasswd", ".hidden.md", "settings.json",
                 "notes.token", "a" * 70 + ".md", "sub%2Fnote.md", ""):
        status, body = _json(_http(MS, "GET", f"/api/memory/file?name={name}"))
        assert status in (400, 404), (name, status, body)
        if status == 400:
            assert body["category"] == "bad-name", (name, body)
        status, body = _json(_http(MS, "POST", f"/api/memory/file?name={name}",
                                   body={"content": "x\n"}))
        assert status == 400 and body["category"] == "bad-name", (name, body)

    # nothing on the wire got written by any of the above
    with open(os.path.join(ROOT, "gate.md"), encoding="utf-8") as handle:
        assert handle.read() == "gate\n"
    assert sorted(os.listdir(ROOT)) == ["gate.md"], os.listdir(ROOT)
    assert not os.path.exists(AUDIT), "a refused request must write no audit line"


# --------------------------------------------------------------------------
# 4. Content hygiene: these bytes become model instructions.
# --------------------------------------------------------------------------

def test_content_that_would_become_an_instruction_is_refused_by_category():
    """G7 step 7 / SECURITY-6 / W10 / PROTOCOL-16, end to end.

    Two properties per case, and the second is the one that is easy to lose: the
    refusal NAMES a category the page can print, and it never echoes the
    offending text — a token-shaped line quoted back into a JSON body, a browser
    and a screenshot is the leak the check exists to prevent.
    """
    _clear_root()
    # 43 URL-safe characters, 41 of them distinct: the shape the detector calls a
    # token. Split across two literals so this file is not itself a line
    # `test_publish_hygiene` has to reason about.
    secret = "Zk8Qv2Lm5Rt9Wx3Yb7" + "Nd4Fg6Hj0PsQwErTyUiOpAsDf"
    mongo_pw = "s3cr3tRealPassword"
    cases = (
        ("import-directive", "see @/etc/passwd for the key\n", "@/etc/passwd"),
        ("html-comment", "text\n<!-- hidden from the model -->\nmore\n", None),
        ("token-shape", f"a note\n{secret}\n", secret),
        ("credentialed-uri", f"mongodb://touch:{mongo_pw}@localhost/db\n", mongo_pw),
        ("nul-byte", "a\x00b\n", None),
        ("lone-cr", "a\rb\n", None),
    )
    for index, (category, text, never) in enumerate(cases):
        name = f"hygiene-{index}.md"
        status, body = _create(name, text)
        assert status == 400, (category, status, body)
        assert body["category"] == category, (category, body)
        assert body["reason"], body
        if never is not None:
            assert never not in json.dumps(body), \
                f"{category}: the refusal echoed the offending text back"
        assert not os.path.exists(os.path.join(ROOT, name)), \
            f"{category}: a refused write left a file behind"

    # An `@path` inside a code span is documentation, not an import — a hygiene
    # rule people have to route around is a rule they turn off.
    status, body = _create("hygiene-span.md", "write `@notes.md` to import it\n")
    assert status == 201, body
    # ...and one over the per-file cap is a 413 about the FILE, not the body.
    status, body = _create("hygiene-big.md", "x" * (MS.MAX_MEMORY_BYTES + 1))
    assert status == 413 and body["category"] == "too-large", body

    # `pinned:` loads a file into EVERY session, unasked (DOCS-6). It needs the
    # word "yes" in the request, and the refusal is a 422 the page can act on.
    pinned = "---\ntitle: t\npinned: true\n---\n\nbody\n"
    status, body = _create("hygiene-pin.md", pinned)
    assert status == 422 and body["category"] == "pinned", body
    assert not os.path.exists(os.path.join(ROOT, "hygiene-pin.md"))
    status, body = _create("hygiene-pin.md", pinned, allow_pinned=True)
    assert status == 201, body
    with open(os.path.join(ROOT, "hygiene-pin.md"), encoding="utf-8") as handle:
        landed = handle.read()
    assert "pinned: true" in landed and "modified: " in landed, landed


def test_the_server_owns_the_trailing_newline_and_never_invents_frontmatter():
    """UI-3 / SERVER-14 / DOCS-16 — the three rules about the bytes themselves."""
    _clear_root()
    # exactly one trailing newline, whatever the textarea sent
    for index, (sent, want) in enumerate((("x", "x\n"), ("y\n\n\n", "y\n"),
                                          ("keep  \n", "keep  \n"))):
        name = f"nl-{index}.md"
        status, body = _create(name, sent)
        assert status == 201, body
        with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
            got = handle.read()
        assert got == want, (sent, got)
        assert body["sha256"] == _sha(want), body

    # a file with no frontmatter gets none: adding a block would opt it into
    # `modified` stamping AND into the `pinned` scan, which nobody asked for
    plain = "# note\n\nbody\n"
    status, body = _create("plain.md", plain)
    assert status == 201, body
    with open(os.path.join(ROOT, "plain.md"), encoding="utf-8") as handle:
        assert handle.read() == plain, "frontmatter was invented"

    # a file that HAS frontmatter is stamped — the CLI reads `modified` back to
    # judge how current a fact is, so leaving it alone would make it lie
    status, body = _create("front.md", "---\ntitle: t\n---\n\nbody\n")
    assert status == 201, body
    with open(os.path.join(ROOT, "front.md"), encoding="utf-8") as handle:
        stamped = handle.read()
    assert re.search(r"(?m)^modified: \d{4}-\d\d-\d\dT[\d:]+\+00:00$", stamped), stamped
    assert stamped.endswith("\nbody\n") and "title: t" in stamped, stamped
    # ...and stamping an already-stamped file REPLACES the line, never duplicates it
    status, read = _json(_http(MS, "GET", "/api/memory/file?name=front.md"))
    status, body = _json(_http(MS, "PUT", "/api/memory/file?name=front.md",
                               body={"content": read["content"] + "more\n",
                                     "ifMatch": read["sha256"]}))
    assert status == 200, body
    with open(os.path.join(ROOT, "front.md"), encoding="utf-8") as handle:
        twice = handle.read()
    assert twice.count("modified:") == 1, twice


# --------------------------------------------------------------------------
# 5. The two documents, and the headers that keep the token out of a Referer.
# --------------------------------------------------------------------------

def test_the_memory_page_is_served_with_no_referrer():
    """G4 + SECURITY-5: both pages' URLs carry the token in their query string."""
    _clear_root()
    if not os.path.isfile(MEMORY_HTML):
        _skip("memory.html is not in this tree, so the /memory page and its "
              "referrer header cannot be checked (the page's own sub-plan owns it)")
        return
    status, headers, body = _http(MS, "GET", "/memory", query_token=True,
                                  header_token=False)
    assert status == 200 and "text/html" in headers["content-type"], headers
    with open(MEMORY_HTML, "rb") as handle:
        assert body == handle.read(), "/memory must serve memory.html verbatim"
    assert headers.get("referrer-policy") == MS.NO_REFERRER, headers
    assert headers.get("cache-control") == "no-store", headers
    # the page is tokened like every other route on this server
    status, _, raw = _http(MS, "GET", "/memory", header_token=False)
    assert status == 401, (status, raw[:120])

    # the dashboard itself, and a report opened FROM it
    status, headers, _ = _http(MS, "GET", "/", header_token=False)
    assert status == 200 and headers.get("referrer-policy") == MS.NO_REFERRER, headers
    status, headers, _ = _http(MS, "GET", "/file?path=report/r.html",
                               query_token=True, header_token=False)
    assert status == 200, status
    assert headers.get("content-security-policy") == MS.FILE_CSP, headers
    assert headers.get("referrer-policy") == MS.NO_REFERRER, headers

    # ...and the meta tag, which covers the fetches a browser starts before it
    # has parsed a header. Both documents carry it: both are loaded from a URL
    # with the token in it, and that token now also authorizes memory writes.
    for path in (MEMORY_HTML, MONITOR_HTML):
        with open(path, encoding="utf-8") as handle:
            page = handle.read()
        assert re.search(r'<meta\s+name="referrer"\s+content="no-referrer"\s*/?>',
                         page), f"{os.path.basename(path)}: no referrer meta tag"


def test_file_csp_is_one_string_in_both_servers():
    """GD-20/SECURITY-4: the verbatim twin, machine-checked across the pair.

    This is the file the two suites hand the comparison to — `test_server.py`
    pins the monitor's copy and `tests/test_server_core.py` pins the
    aggregator's, and neither can import the other's module. `sandbox` with NO
    `allow-scripts` is the decision: an opaque origin stops a report reading this
    server's responses, but it does not stop a script in the report reading its
    own `location.search` — which carries the token that now also authorizes
    memory writes.
    """
    sys.path.insert(0, str(PAYLOAD))
    from aggregator import server as agg          # noqa: E402  the other server

    assert agg.FILE_CSP == MS.FILE_CSP, (agg.FILE_CSP, MS.FILE_CSP)
    assert agg.NO_REFERRER == MS.NO_REFERRER, (agg.NO_REFERRER, MS.NO_REFERRER)
    assert MS.FILE_CSP == "sandbox", MS.FILE_CSP
    assert "allow-scripts" not in MS.FILE_CSP, MS.FILE_CSP
    # The SOURCE spelling too, and byte-for-byte: a constant that is equal today
    # because one of them is computed is a twin that drifts on the next edit.
    lines = {}
    for label, path in (("monitor", MODULE_PATH),
                        ("aggregator", os.path.join(str(PAYLOAD), "aggregator",
                                                    "server.py"))):
        with open(path, encoding="utf-8") as handle:
            found = [ln.rstrip("\n") for ln in handle
                     if ln.startswith(("FILE_CSP", "NO_REFERRER"))]
        assert len(found) == 2, (label, found)
        lines[label] = found
    assert lines["monitor"] == lines["aggregator"], lines
    assert lines["monitor"] == ['FILE_CSP = "sandbox"',
                               'NO_REFERRER = "no-referrer"'], lines["monitor"]


# --------------------------------------------------------------------------
# 6. /health, and the alignment answer the page reads from the tokened route.
# --------------------------------------------------------------------------

def test_health_publishes_counts_and_booleans_and_nothing_path_shaped():
    """SERVER-10/SECURITY-1: /health is the one route with no token in front."""
    _clear_root()
    over = "".join(f"line {n}\n" for n in range(MS.MEM_INDEX_LINES + 5))
    status, body = _create("MEMORY.md", over)
    assert status == 201, body
    status, _, raw = _http(MS, "GET", "/health", header_token=False, origin=False)
    assert status == 200, status
    payload = json.loads(raw)
    block = payload["memory"]
    assert set(block) == {"present", "writable", "aligned", "files", "bytes",
                          "indexOverLimit"}, block
    assert block["present"] is True and block["files"] == 1, block
    assert block["bytes"] == len(over.encode()), block
    assert block["indexOverLimit"] is True, block
    assert payload["memoryWrite"] in ("on", "off"), payload["memoryWrite"]
    # nothing path-shaped, and nothing filename-shaped: a memory filename is a
    # topic name, which is a disclosure in its own right.
    rendered = json.dumps(block)
    for shape in ("/", "\\", ".md", "MEMORY", os.path.basename(BASE)):
        assert shape not in rendered, (shape, rendered)


def test_the_list_route_answers_aligned_from_the_documented_layers_only():
    """SERVER-4/DOCS-1/DOCS-13 — a tri-state and a sentence, never a guess.

    `autoMemoryDirectory` is silently rejected when it is not absolute: the CLI
    returns `undefined` and falls back to its default with no error and no
    warning. That silence is why this route reports the comparison instead of
    assuming it, and why the answer has three values.
    """
    _clear_root()

    def aligned():
        status, body = _json(_http(MS, "GET", "/api/memory/list"))
        assert status == 200, body
        return body["aligned"], body["effective"]

    # (a) nothing configured anywhere (the throwaway HOME makes this honest)
    state, effective = aligned()
    assert state is False and "no autoMemoryDirectory is set" in effective, effective

    # (b) the relative value everybody writes first — inert, and named as such
    with open(SETTINGS_LOCAL, "w", encoding="utf-8") as handle:
        json.dump({"autoMemoryDirectory": ".touch/memory"}, handle)
    state, effective = aligned()
    assert state is False and "not an absolute path" in effective, effective

    # (c) the absolute value `touch-selfcheck` writes
    with open(SETTINGS_LOCAL, "w", encoding="utf-8") as handle:
        json.dump({"autoMemoryDirectory": ROOT}, handle)
    state, effective = aligned()
    assert state is True and effective == ROOT, (state, effective)

    # (d) an UNDOCUMENTED env override outranks every settings layer, so the
    #     question cannot be answered from settings at all — None, not True.
    os.environ["CLAUDE_CODE_REMOTE_MEMORY_DIR"] = "/somewhere/else"
    try:
        state, effective = aligned()
    finally:
        os.environ.pop("CLAUDE_CODE_REMOTE_MEMORY_DIR", None)
    assert state is None, (state, effective)
    assert "CLAUDE_CODE_REMOTE_MEMORY_DIR" in effective, effective
    os.unlink(SETTINGS_LOCAL)


# --------------------------------------------------------------------------
# 7. The three refusals that are about WHERE the root is.
# --------------------------------------------------------------------------

def test_a_symlink_at_the_root_the_target_or_the_trash_is_refused():
    """G7 step 2, at all three levels it can be planted at (SERVER-5/W6).

    A symlink is the one hazard `realpath` containment cannot see, because a
    resolved root IS its own base — so the root is checked where it is resolved,
    the target in `safe_memory_path`, and the history/trash directory in
    `memory_side_dir`. All three, from outside, over HTTP.
    """
    _clear_root()
    outside = os.path.join(BASE, "outside")
    os.makedirs(outside, exist_ok=True)
    victim = os.path.join(outside, "victim.md")
    with open(victim, "w", encoding="utf-8") as handle:
        handle.write("bytes that live outside the project\n")
    MS.memory_makedirs(ROOT)
    with open(os.path.join(ROOT, "real.md"), "w", encoding="utf-8") as handle:
        handle.write("real\n")
    # Hoisted rather than spelled inline: a backslash inside an f-string
    # expression is a SyntaxError before Python 3.12, and nothing here needs a
    # version floor a test file introduced on its own.
    real_sha = _sha("real\n")

    # (a) the TARGET is a link out of the root: refused without being resolved
    os.symlink(victim, os.path.join(ROOT, "link.md"))
    for out in (_http(MS, "GET", "/api/memory/file?name=link.md"),
                _http(MS, "PUT", "/api/memory/file?name=link.md",
                      body={"content": "x\n", "ifMatch": real_sha}),
                _http(MS, "DELETE",
                      "/api/memory/file?name=link.md&ifMatch=" + real_sha)):
        status, body = _json(out)
        assert status == 409 and body["category"] == "symlink", body
    with open(victim, encoding="utf-8") as handle:
        assert handle.read() == "bytes that live outside the project\n"
    # ...and the row for it is listed, honestly unwritable, with no stat of the
    # target — a size and mtime there would publish a fact about a file outside.
    status, body = _json(_http(MS, "GET", "/api/memory/list"))
    row = [r for r in body["files"] if r["name"] == "link.md"][0]
    assert row["writable"] is False and "symlink" in row["reason"], row
    assert row["size"] == 0 and row["mtime_ns"] == 0, row
    os.unlink(os.path.join(ROOT, "link.md"))

    # (b) `.trash` is a link: every deleted file would leave the project
    elsewhere = os.path.join(outside, "trashcan")
    os.makedirs(elsewhere, exist_ok=True)
    os.symlink(elsewhere, os.path.join(ROOT, ".trash"))
    status, body = _json(_http(MS, "DELETE",
                               "/api/memory/file?name=real.md&ifMatch=" + real_sha))
    assert status == 409 and body["category"] == "symlink", body
    assert os.listdir(elsewhere) == [], "the trash link was followed"
    assert os.path.isfile(os.path.join(ROOT, "real.md")), "the file was removed anyway"
    os.unlink(os.path.join(ROOT, ".trash"))

    # (c) the ROOT itself is a link: the whole family goes away, with a reason
    real_root = ROOT + ".real"
    os.rename(ROOT, real_root)
    os.symlink(real_root, ROOT)
    try:
        for out in (_http(MS, "GET", "/api/memory/list"),
                    _http(MS, "GET", "/api/memory/file?name=real.md"),
                    _http(MS, "POST", "/api/memory/file?name=new.md",
                          body={"content": "x\n"})):
            status, body = _json(out)
            assert status == 503 and body["category"] == "memory-unavailable", body
            assert "symlink" in body["reason"], body
        # the PAGE still answers, so the operator reads the reason as a banner
        status, _, _ = _http(MS, "GET", "/memory", query_token=True,
                             header_token=False)
        assert status == 200, status
    finally:
        os.unlink(ROOT)
        os.rename(real_root, ROOT)


def test_a_memory_root_inside_the_home_claude_tap_is_refused():
    """PROTOCOL-7 / Part D-9: `~/.claude` is a read-only tap, always.

    Reached the way it actually would be — a `touch-monitor` started with
    `CLAUDE_PROJECT_DIR` pointing inside the CLI's own configuration directory —
    so the refusal is asserted over a root the module RESOLVED, not one a test
    assigned.
    """
    project = os.path.join(FAKE_HOME, ".claude", "proj")
    os.makedirs(project, exist_ok=True)
    mod = _load("ms_memory_home_claude", project=project, write=True)
    assert mod.MEMORY_ROOT.startswith(os.path.join(FAKE_HOME, ".claude")), \
        mod.MEMORY_ROOT
    for out in (_http(mod, "GET", "/api/memory/file?name=MEMORY.md"),
                _http(mod, "POST", "/api/memory/file?name=MEMORY.md",
                      body={"content": "x\n"})):
        status, body = _json(out)
        assert status == 403 and body["category"] == "home-claude", body
        assert "~/.claude" in body["reason"], body
    assert not os.path.exists(os.path.join(project, ".touch")), \
        "a refused write created a directory under ~/.claude"


def test_a_memory_root_inside_a_plugin_cache_disables_the_family():
    """SERVER-16 / W8 / Part D-8: an installed plugin cache is swept.

    A memory file written into a version-stamped cache directory is data loss
    with extra steps — the file vanishes on the next update, and it is an
    INSTRUCTION file, so what vanishes is behaviour nobody can account for. The
    family is disabled rather than made to write there.
    """
    cache = os.path.join(BASE, "cache", "touch", "0.2.0")
    os.makedirs(os.path.join(cache, ".claude-plugin"), exist_ok=True)
    with open(os.path.join(cache, ".claude-plugin", "plugin.json"), "w",
              encoding="utf-8") as handle:
        json.dump({"name": "touch"}, handle)
    mod = _load("ms_memory_plugin_cache", project=cache, write=True)
    assert mod.in_plugin_cache(mod.MEMORY_ROOT) is True, mod.MEMORY_ROOT
    for out in (_http(mod, "GET", "/api/memory/list"),
                _http(mod, "GET", "/api/memory/file?name=MEMORY.md"),
                _http(mod, "POST", "/api/memory/file?name=MEMORY.md",
                      body={"content": "x\n"})):
        status, body = _json(out)
        assert status == 503 and body["category"] == "memory-unavailable", body
        assert "plugin cache" in body["reason"], body
    status, _, raw = _http(mod, "GET", "/health", header_token=False, origin=False)
    block = json.loads(raw)["memory"]
    assert block == {"present": False, "writable": False, "aligned": None,
                     "files": 0, "bytes": 0, "indexOverLimit": False}, block
    assert not os.path.exists(os.path.join(cache, ".touch")), \
        "the family wrote inside a plugin cache"


# --------------------------------------------------------------------------
# 8. One namespace, one budget — in every file that spells them.
# --------------------------------------------------------------------------

def test_the_flat_namespace_and_the_index_budget_have_one_spelling_each():
    """G5/G7 step 1: three files, one decision, and nothing compared them.

    monitor_server.py enforces the namespace, memory.html validates a new name
    against it before it becomes a request, and `tests/test_memory_hygiene.py`
    asserts it over what git tracks. If those drift, the editor offers a name the
    server refuses (harmless) or the git carve publishes a file the write path
    would never have allowed (not harmless). Same for the two index numbers,
    which the API reports to the page as `limits` and the repository gate
    measures the index against.

    Read as TEXT, not imported: `test_memory_hygiene.py` imports the aggregator
    package and the publish-hygiene detectors, and a comparison should not drag
    another suite's import side effects into this one.
    """
    missing = [path for path in (MEMORY_HTML, HYGIENE_TEST)
               if not os.path.isfile(path)]
    if missing:
        _skip("one spelling of the namespace is not in this tree yet, so the "
              "three cannot be compared: " + ", ".join(os.path.basename(m)
                                                       for m in missing))
        return
    with open(MEMORY_HTML, encoding="utf-8") as handle:
        page = handle.read()
    with open(HYGIENE_TEST, encoding="utf-8") as handle:
        gate = handle.read()

    page_re = _slice_const(page, r"const MEM_NAME_RE = /(.+?)/;\n", "memory.html")
    gate_re = _slice_const(gate, r'FLAT_MD = re\.compile\(r"(.+?)"\)',
                           "test_memory_hygiene.py")
    assert MS.MEMORY_NAME_RE.pattern == page_re == gate_re, \
        (MS.MEMORY_NAME_RE.pattern, page_re, gate_re)

    fallback = _slice_const(page, r"const MEM_LIMIT_FALLBACK = \{(.+?)\};",
                            "memory.html limits")
    for label, served in (("Lines", MS.MEM_INDEX_LINES),
                          ("Bytes", MS.MEM_INDEX_BYTES)):
        page_value = int(_slice_const(fallback, r"index%s: (\d+)" % label,
                                      f"memory.html index{label}"))
        gate_value = int(_slice_const(gate, r"(?m)^INDEX_%s = (\d+)$" % label.upper(),
                                      f"test_memory_hygiene INDEX_{label.upper()}"))
        assert served == page_value == gate_value, (label, served, page_value,
                                                    gate_value)
    assert MS.MEM_INDEX_NAME == _slice_const(gate, r'INDEX_NAME = "(.+?)"',
                                             "INDEX_NAME")


# --------------------------------------------------------------------------

def run_all():
    """Definition order, not alphabetical.

    These arms share one throwaway memory root and each begins by taking it back
    to "not there yet", so they are independent — but they read as one narrative
    (posture, then a file's whole life, then the walls around it), and a runner
    that sorted them by name would print that narrative shuffled.
    """
    tests = sorted((v for k, v in globals().items()
                    if k.startswith("test_") and callable(v)),
                   key=lambda fn: fn.__code__.co_firstlineno)
    failed = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except Exception as exc:                       # noqa: BLE001 (a runner)
            failed += 1
            print(f"FAIL {test.__name__}: {exc!r}")
    print()
    for message in SKIPS:
        print(f"skipped: {message}")
    if failed:
        print(f"\n{failed}/{len(tests)} tests FAILED")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed ({len(SKIPS)} skipped)")


if __name__ == "__main__":
    run_all()
