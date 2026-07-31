#!/usr/bin/env python3
"""Stdlib-only tests for the server's posture and transport (R-30).
Run as `python3 test_server_core.py`; exits non-zero on failure. No pytest.

R-30's own test list is "no-token ⇒ 401 on every route but `/health`;
cross-origin WS ⇒ 403; unknown route/id ⇒ 404 (no fallback); a path segment
after a registered route 404s". All four are here, against the real objects,
plus the rest of GD-13's posture that a list of four cannot cover:

* the route table is a static `(method, route)` dict with **no** default
  handler — asserted structurally, so a later prefix match cannot sneak in;
* `safe_artifact_path` refuses traversal, absolute paths and symlinks out of
  the task folder, and served files carry the CSP sandbox + `nosniff`;
* the token is compared with `hmac.compare_digest`, is 256 bits, is injected
  into the page at serve time, and never appears in `/health`;
* `.touch/server.json` is created 0600 (GD-27's handling parity with
  `mongo.json`);
* the loopback default and the `--open` opt-in;
* and one **real socket**: an HTTP request, a rejected upgrade, and a full
  WebSocket handshake → replay (`live:false`) → mode switch → live frame,
  driven through `aggregator.ws`'s codec so the framing is checked too.

Static source guards close the two rules that are properties of the *file*
rather than of any one call: this module never derives state (GD-23 — the
reducer decided) and never computes a token delta (GD-25/R-55 — the absolute
model is what makes `(stream, seq)` resume safe).
"""

import ast
import asyncio
import base64
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import tokenize
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The canonical trees are named through `tests/_roots.py`, never by a
# literal under REPO: GD-U1 moves them and this is the single flip point.
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import SRC                # noqa: E402  (path juggling first)
sys.path.insert(0, str(SRC))

from aggregator import server as server_mod                        # noqa: E402
from aggregator import sessions as sessions_mod                    # noqa: E402
from aggregator import store as store_mod                          # noqa: E402
from aggregator import tailer as tailer_mod                        # noqa: E402
from aggregator import tick as tick_mod                            # noqa: E402
from aggregator import ws                                          # noqa: E402
from aggregator.server import (                                    # noqa: E402
    CONTROL_ROUTES,
    DEFAULT_HOST,
    DEFAULT_PORT,
    LEGACY_PORT,
    OPEN_HOST,
    OPEN_ROUTES,
    READ_ROUTES,
    ROUTES,
    TOKEN_BYTES,
    Api,
    Auth,
    HttpServer,
    OriginPolicy,
    ReadModel,
    Response,
    ServerError,
    inject_token,
    safe_artifact_path,
    write_server_json,
)

failures = []
TMPDIRS = []
#: The three observations a discovered file's NAME is made of. Kept as
#: constants because every assertion below is "this string is not in the body".
OBSERVED_SESSION = "b7c1d2e3-4455-4667-8899-aabbccddeeff"
OBSERVED_AGENT = "a" + "1" * 16
OBSERVED_RUN = "wf_secret02-bbb"
SOURCE = (SRC / "aggregator" / "server.py").read_text()
TREE = ast.parse(SOURCE)


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def tmpdir(name):
    path = tempfile.mkdtemp(prefix=f"touch-{name}-")
    TMPDIRS.append(path)
    return path


def body(response):
    return json.loads(response.body.decode("utf-8"))


# --- auth (GD-13) ---------------------------------------------------------


def test_every_route_but_health_needs_the_token():
    print("test_every_route_but_health_needs_the_token")
    api = Api(ReadModel(state={}, store=None), auth=Auth("s3cret"))
    for method, route in sorted(ROUTES):
        response = api.handle(method, route, {}, {})
        if route in OPEN_ROUTES:
            check(response.status == 200, f"{route} is served without a token")
        else:
            check(response.status == 401, f"{route} without a token is 401")
    check(api.handle("GET", "/api/sessions", {}, {}).headers.get("WWW-Authenticate"),
          "a 401 carries a Bearer challenge")
    check(OPEN_ROUTES == frozenset({"/health"}),
          "exactly one route is open, and it is /health")


def test_all_three_token_carriers_work_and_a_wrong_one_does_not():
    print("test_all_three_token_carriers_work_and_a_wrong_one_does_not")
    auth = Auth("s3cret")
    api = Api(ReadModel(state={}, store=None), auth=auth)
    check(api.handle("GET", "/api/sessions", {}, {"authorization": "Bearer s3cret"}).status == 200,
          "Authorization: Bearer is accepted")
    check(api.handle("GET", "/api/sessions", {}, {"x-touch-token": "s3cret"}).status == 200,
          "X-Touch-Token is accepted")
    check(api.handle("GET", "/api/sessions", {"token": ["s3cret"]}, {}).status == 200,
          "?token= is accepted — a browser cannot set a header on a WS handshake")
    check(api.handle("GET", "/api/sessions", {}, {"authorization": "Bearer s3crat"}).status == 401,
          "a wrong token is 401")
    check(api.handle("GET", "/api/sessions", {"token": ["s3cret "]}, {}).status == 401,
          "and so is a nearly-right one (no trimming, no prefix match)")
    check(auth.rejections == 2, "rejections are counted for /health to publish")
    check(len(base64.urlsafe_b64decode(Auth().token + "==")) >= 32,
          f"a generated token carries {TOKEN_BYTES * 8} bits of entropy")
    check(Auth().token != Auth().token, "the token is per boot, not a constant")
    check("compare_digest" in SOURCE, "the comparison is hmac.compare_digest")
    check(not re.search(r"presented\s*==|==\s*self\.token|self\.token\s*==", SOURCE),
          "and there is no plain-equality shortcut beside it")


# --- routing (GD-12) ------------------------------------------------------


def test_the_route_table_is_static_and_has_no_fallback():
    print("test_the_route_table_is_static_and_has_no_fallback")
    api = Api(ReadModel(state={}, store=None), auth=Auth("t"))
    headers = {"authorization": "Bearer t"}
    check(api.handle("GET", "/api/nope", {}, headers).status == 404, "an unknown route is 404")
    check(api.handle("GET", "/api/sessions/extra", {}, headers).status == 404,
          "a path segment after a registered route 404s — there is no prefix match")
    check(api.handle("GET", "/api/sessions/", {}, headers).status == 404,
          "not even a trailing slash resolves to the route")
    check(api.handle("POST", "/api/sessions", {}, headers).status == 404,
          "the key is (method, route): another method is another route")
    check(api.handle("GET", "/ws", {}, headers).status == 404,
          "a plain GET of /ws is never answered with a body (SERVER-3)")
    for key in ROUTES:
        check(isinstance(key, tuple) and len(key) == 2 and key[0].isupper(),
              f"route key {key!r} is a (METHOD, route) pair")
    check(all(route.startswith("/") and "*" not in route and "<" not in route
              for _, route in ROUTES),
          "no route is a pattern — the table is literal (GD-12)")
    check(not any(isinstance(node, ast.Attribute) and node.attr == "startswith"
                  and isinstance(node.value, ast.Name) and node.value.id == "route"
                  for node in ast.walk(TREE)),
          "no handler dispatches on a route prefix")


def test_the_read_and_control_groups_are_declared_before_a_control_exists():
    print("test_the_read_and_control_groups_are_declared_before_a_control_exists")
    # R-30 names two route *groups*. Declaring the split while one of them is
    # empty is the whole point: the first control endpoint then arrives into a
    # named group instead of into a flat table where "is this a control?" is
    # answered by reading the handler.
    check(set(READ_ROUTES) | set(CONTROL_ROUTES) == set(ROUTES),
          "the two groups partition the route table")
    check(not (set(READ_ROUTES) & set(CONTROL_ROUTES)),
          "and nothing is in both — a control route is not also a read route")
    check(CONTROL_ROUTES == {},
          "v0 ships no control route: sp-13 renders no control affordance, so "
          "serving one would be a capability with no consent behind it")
    check(all(method == "GET" for method, _ in READ_ROUTES),
          "every read route is a GET — a read that needs a verb is not a read")


def test_a_handler_bug_is_a_500_and_not_a_traceback():
    print("test_a_handler_bug_is_a_500_and_not_a_traceback")

    def broken(api, query, headers):
        raise KeyError("secret-looking-internal-detail")

    ROUTES[("GET", "/api/broken")] = broken
    try:
        api = Api(ReadModel(state={}, store=None), auth=Auth("t"))
        response = api.handle("GET", "/api/broken", {}, {"authorization": "Bearer t"})
        text = response.body.decode()
        check(response.status == 500, "an unexpected handler failure is a 500")
        check("KeyError" in text and "secret-looking-internal-detail" not in text,
              "naming the exception type and nothing else — a traceback here is a disclosure")
        check("Traceback" not in text, "and no traceback reaches the wire")
    finally:
        ROUTES.pop(("GET", "/api/broken"), None)


def test_an_unknown_id_is_never_answered_with_another_ones_data():
    print("test_an_unknown_id_is_never_answered_with_another_ones_data")
    root = tmpdir("tasks")
    os.makedirs(os.path.join(root, "real-task", "report"))
    with open(os.path.join(root, "real-task", "report", "r.html"), "w") as fh:
        fh.write("<h1>report</h1>")
    api = Api(ReadModel(state={}, store=None, tasks_root=root), auth=Auth("t"))
    good = api.get("/api/artifacts?task=real-task", {"authorization": "Bearer t"})
    check(good.status == 200 and body(good)["artifacts"][0]["path"] == "report/r.html",
          "a real task lists its artifacts, reports first")
    missing = api.get("/api/artifacts?task=ghost-task", {"authorization": "Bearer t"})
    check(missing.status == 404,
          "an unknown task is 404 — never the monitor's silent STATE_DIR fallback")
    bad = api.get("/api/artifacts?task=../../etc", {"authorization": "Bearer t"})
    check(bad.status == 400, "a traversing task name is refused by the id validator")


# --- artifact containment (copied verbatim, GD-20) ------------------------


def test_safe_artifact_path_contains_everything():
    print("test_safe_artifact_path_contains_everything")
    base = tmpdir("artifacts")
    inside = os.path.join(base, "report", "r.html")
    os.makedirs(os.path.dirname(inside))
    with open(inside, "w") as fh:
        fh.write("<p>hi</p>")
    outside = os.path.join(tmpdir("outside"), "secret.md")
    with open(outside, "w") as fh:
        fh.write("secret")
    os.symlink(outside, os.path.join(base, "link.md"))

    check(safe_artifact_path(base, "report/r.html") == os.path.realpath(inside),
          "a contained artifact resolves")
    check(safe_artifact_path(base, "../../etc/passwd") is None, "traversal is refused")
    check(safe_artifact_path(base, "/etc/passwd") is None, "an absolute path is refused")
    check(safe_artifact_path(base, "link.md") is None,
          "a symlink pointing outside the task folder is refused (realpath, not prefix)")
    check(safe_artifact_path(base, "report/r.py") is None,
          "an extension outside the whitelist is refused")
    check(safe_artifact_path(base, "") is None, "an empty path is refused")


def test_served_files_carry_the_csp_sandbox_and_nosniff():
    print("test_served_files_carry_the_csp_sandbox_and_nosniff")
    root = tmpdir("files")
    task = os.path.join(root, "t1", "report")
    os.makedirs(task)
    with open(os.path.join(task, "r.html"), "w") as fh:
        fh.write("<script>1</script>")
    with open(os.path.join(root, "t1", "note.md"), "w") as fh:
        fh.write("# note")
    api = Api(ReadModel(state={}, store=None, tasks_root=root), auth=Auth("t"))
    headers = {"authorization": "Bearer t"}
    html = api.get("/file?task=t1&path=report/r.html", headers)
    # `sandbox` with NO `allow-scripts` (SECURITY-4). The opaque origin stops a
    # report reading anything of this server's, but it does NOT stop a script in
    # the report reading its own `location.search` — which carries the token —
    # and POSTing it somewhere. Agent-generated reports are exactly the documents
    # nobody audited. The monitoring server refused the flag from the start; this
    # file was copied from it (GD-20) without the CSP, so the twin had drifted.
    check(html.status == 200 and html.headers["Content-Security-Policy"] == "sandbox",
          "a report HTML is served in a bare CSP sandbox — no allow-scripts")
    check("allow-scripts" not in server_mod.FILE_CSP,
          "…and the constant itself carries no script grant, so a new use site "
          "cannot inherit one")
    check(html.headers.get("Referrer-Policy") == "no-referrer",
          "…with Referrer-Policy: no-referrer — the URL that fetched it holds "
          "the token in its query string (SECURITY-5)")
    check(b"X-Content-Type-Options: nosniff" in html.to_bytes(), "and with nosniff")
    md = api.get("/file?task=t1&path=note.md", headers)
    check(md.content_type.startswith("text/plain"),
          "a .md is served as text — the page renders it escape-first, the server never does")
    check(md.headers.get("Referrer-Policy") == "no-referrer",
          "…and the text arm is no-referrer too: navigated to directly it is "
          "still a document of this origin, loaded from a tokened URL")
    check(api.get("/file?task=t1&path=missing.md", headers).status == 404,
          "a missing artifact is 404")
    check(b"Cache-Control: no-store" in html.to_bytes(),
          "every response is no-store: a cached observation is a lie with a timestamp")
    # Both use sites of the constant, from the source: `/file`'s HTML arm above is
    # exercised live, `/api/toolresult`'s needs a spilled tool result to reach, so
    # the pairing is asserted structurally rather than left to one route.
    csp_sites = [n for n, line in enumerate(SOURCE.splitlines(), 1)
                 if '"Content-Security-Policy": FILE_CSP' in line]
    # `>= 2`, not `== 2`: a third defensive use site (giving some other served
    # document the same sandbox) must not redden a security test for ADDING
    # security. The load-bearing half is the per-site pairing below.
    check(len(csp_sites) >= 2,
          f"the CSP is used at both document routes, at least: {csp_sites}")
    for site in csp_sites:
        window = "\n".join(SOURCE.splitlines()[site - 1:site + 3])
        check("NO_REFERRER" in window,
              f"the use site at :{site} sets Referrer-Policy alongside the CSP — "
              "they are one decision, not two")


def test_an_empty_task_list_says_which_kind_of_empty_it_is():
    print("test_an_empty_task_list_says_which_kind_of_empty_it_is")
    # UI-13: the panel that renders this cannot tell an empty answer from a lost
    # one, so both shapes of "nothing to list" answer 200 WITH a note. The
    # second shape is the one the tasks-root move creates: the resolvers now say
    # `.touch/local-orchestrators` while the directory may not be there yet, and
    # `legacy.scan` answers `()` for a missing root — correctly and silently.
    unconfigured = Api(ReadModel(state={}, store=None, tasks_root=None),
                       auth=Auth("t")).get("/api/tasks",
                                           {"authorization": "Bearer t"})
    body = json.loads(unconfigured.body)
    check(unconfigured.status == 200 and body["count"] == 0
          and body["note"] == "no local-orchestrators root configured",
          f"no root configured ⇒ 200 with the note that says so: {body}")

    absent = os.path.join(tmpdir("tasks-note"), "not-there")
    answer = Api(ReadModel(state={}, store=None, tasks_root=absent),
                 auth=Auth("t")).get("/api/tasks", {"authorization": "Bearer t"})
    body = json.loads(answer.body)
    check(answer.status == 200 and body["tasks"] == [] and body["count"] == 0,
          f"a configured-but-absent root is still a 200 with an empty list: {body}")
    check(body.get("note") and body.get("exists") is False,
          "…carrying BOTH a note the page can render and the machine-readable "
          f"`exists: false`, so nobody parses English: {body}")
    check(os.sep not in body["note"] and "not-there" not in body["note"],
          f"…and the note names no path — it is rendered on the page, and the "
          f"root is already its own field: {body['note']!r}")

    present = tmpdir("tasks-present")
    os.makedirs(os.path.join(present, "some-task"))
    listed = Api(ReadModel(state={}, store=None, tasks_root=present),
                 auth=Auth("t")).get("/api/tasks", {"authorization": "Bearer t"})
    body = json.loads(listed.body)
    check(listed.status == 200 and "note" not in body and body["count"] == 1,
          f"a root that exists carries no note at all — the slot clears: {body}")


def test_no_status_is_ever_labelled_ok_when_it_is_not():
    print("test_no_status_is_ever_labelled_ok_when_it_is_not")
    status_text = server_mod.status_text
    # The bug this replaces: BOTH readers of the table spelled their own
    # fallback, and both were wrong. `head_bytes` used `.get(status, "OK")`, so
    # the first route to answer 409 would have sent `HTTP/1.1 409 OK` — a
    # conflict wearing a success reason phrase — and `Response.error` used
    # `.get(status, "Error")`, a body field that says nothing (SERVER-8).
    check(status_text(409) == "Conflict", "409 is Conflict, not OK")
    check(b"HTTP/1.1 409 Conflict" in Response.error(409, "taken").head_bytes(),
          "…and that is what goes on the wire")
    check(json.loads(Response.error(409, "taken").body)["error"] == "Conflict",
          "…and in the body's `error` field, which used to read 'Error'")
    for status in (200, 201, 204, 400, 401, 403, 404, 405, 409, 411, 412, 413,
                   415, 422, 426, 500, 503):
        # Every status either server can emit has a phrase. The memory routes on
        # the monitoring server emit the ones this one has no route for yet, and
        # one complete table beats two half-tables with two fallbacks — a table
        # listing only what today's caller needs is how the bug above was
        # written. Nothing machine-checks the two servers' tables against each
        # other, so this is not asserted as a GD-20 twin; `FILE_CSP` is.
        check(status in server_mod.STATUS_TEXT,
              f"{status} has a named reason phrase")
    for status in (207, 418, 451, 599):
        phrase = status_text(status)
        check(phrase != "OK" and phrase != "Error",
              f"an unnamed {status} falls back to its CLASS ({phrase!r}), which "
              "is derived from the code and so cannot contradict it")
    check(status_text(418) == "Client Error" and status_text(599) == "Server Error",
          "…4xx is a Client Error and 5xx a Server Error")
    check(b"HTTP/1.1 418 Client Error" in Response.error(418, "x").head_bytes(),
          "…and an unknown status still produces a well-formed status line")


# --- the page -------------------------------------------------------------


def test_the_token_is_injected_into_the_page():
    print("test_the_token_is_injected_into_the_page")
    check(inject_token(b"<html><head><title>t</title></head>", "abc")
          == b'<html><head><script>window.TOUCH_TOKEN="abc";</script><title>t</title></head>',
          "with no placeholder, a script tag lands right after <head>")
    check(inject_token(b'<script>const T="__TOUCH_TOKEN__";</script>', "abc")
          == b'<script>const T="abc";</script>',
          "the __TOUCH_TOKEN__ placeholder is the contract sp-13 codes against")
    check(b'window.TOUCH_TOKEN="a\\"b"' in inject_token(b"<head>", 'a"b'),
          "the fallback arm JSON-encodes, so a quote cannot break out of the string")

    # The two arms escape differently and the docstring has to say which is
    # which: the placeholder is substituted RAW, so sp-13 may only put it where
    # a raw token is already valid. That is sound because the generator's
    # alphabet needs no escaping anywhere — assert the alphabet, not the habit.
    check(inject_token(b"fetch('/ws?token=__TOUCH_TOKEN__')", 'a"b')
          == b"fetch('/ws?token=a\"b')",
          "the placeholder arm substitutes the raw token — no quoting, no encoding")
    check(re.match(r"^[A-Za-z0-9_-]+$", Auth().token),
          "and the real token is URL-safe base64, so raw substitution is safe in a "
          "URL, an attribute or a JS string literal")
    check("raw" in inject_token.__doc__ and "__TOUCH_TOKEN__" in inject_token.__doc__,
          "which is what the docstring now tells sp-13, instead of claiming both arms encode")

    assets = tmpdir("assets")
    with open(os.path.join(assets, "index.html"), "w") as fh:
        fh.write("<head></head><body>touch</body>")
    api = Api(ReadModel(state={}, store=None), auth=Auth("pagetoken"), assets=assets)
    page = api.get("/", {"authorization": "Bearer pagetoken"})
    check(b"pagetoken" in page.body, "the served page carries the token (GD-13)")
    missing = Api(ReadModel(state={}, store=None), auth=Auth("t"),
                  assets=tmpdir("empty")).get("/", {"authorization": "Bearer t"})
    check(missing.status == 503 and "not present yet" in body(missing)["message"],
          "an unwritten touch-visual/ is a 503 that names the missing file, not a 404")


# --- health ---------------------------------------------------------------


def test_health_never_carries_a_credential():
    print("test_health_never_carries_a_credential")
    class LeakyMirror:
        def health(self):
            return {"state": "live", "lastError": None, "notes": [], "queued": 0,
                    "dropped": 0, "tolerated_dups": 0, "lease": {}, "backend": "async",
                    "db": "touch_abc1234", "counters": {}}

    api = Api(ReadModel(state={}, store=None, mirror=LeakyMirror()), auth=Auth("s3cret"))
    text = api.get("/health").body.decode()
    check("s3cret" not in text, "the token is never on /health")
    check("mongodb://" not in text and "password" not in text.lower(),
          "and no connection string or password (GD-27)")
    check(body(api.get("/health"))["mirror"]["state"] == "live",
          "the mirror block is still served")

    class AngryMirror:
        def health(self):
            raise RuntimeError("driver exploded")

    down = Api(ReadModel(state={}, store=None, mirror=AngryMirror()), auth=Auth("t"))
    payload = body(down.get("/health"))
    check(payload["mirror"]["state"] == "down" and payload["ok"] is True,
          "a mirror that raises is reported down; /health itself never 500s (GD-22)")
    check("driver exploded" not in json.dumps(payload),
          "and the raised text is not echoed — only the exception type")


def test_health_publishes_no_observation_to_an_unauthenticated_caller():
    print("test_health_publishes_no_observation_to_an_unauthenticated_caller")
    # /health is open by design, so what it may say is bounded by *what it is
    # for*: operational facts. A real `tailer.Tailer` holds
    # `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl` — the home directory,
    # the directory Claude was started in, and the session uuid — and a
    # `store.Store` holds the state root. None of that is operational.
    session = "a8d43bb1-0313-45d4-8784-4827af443ead"
    root = tmpdir("observed")
    path = os.path.join(root, "projects", "-home-someone-secret-repo", f"{session}.jsonl")
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as fh:
        fh.write("{}\n")
    store = store_mod.Store(tmpdir("touchroot"))
    store.append("run:wf_secret01-aaa", kind="node", provenance="harness",
                 ref={"runId": "wf_secret01-aaa", "key": "k", "ordinal": 0}, data={})
    model = ReadModel(state={}, store=store,
                      tailers={"session": tailer_mod.Tailer(path)})
    api = Api(model, auth=Auth("t"))
    raw = api.get("/health").body.decode()          # NO token presented
    payload = json.loads(raw)
    check(payload["tailers"][0]["name"] == "session" and "path" not in payload["tailers"][0],
          "a tailer is named and its target is not")
    check(session not in raw, "no session uuid reaches an unauthenticated caller")
    check("secret-repo" not in raw and os.path.dirname(path) not in raw,
          "nor the cwd-derived project slug, nor any absolute path")
    check("wf_secret01-aaa" not in raw, "nor the id of a run that is being observed")
    check(not re.search(r'"[^"]*/[^"]*/[^"]*"', raw),
          "in fact nothing path-shaped is on the open route at all")
    check(len(payload["tailers"][0]["target"] or "") == 12,
          "the target is a stable hash instead — 'same target' stays answerable")
    check(payload["tailers"][0]["target"]
          == ReadModel.target_hash(path) != ReadModel.target_hash(path + "x"),
          "stable across restarts, and different targets differ")
    check(payload["store"]["configured"] is True and payload["store"]["streamCount"] == 1,
          "the store reports that it exists and how much it holds, not where or what")


def test_the_ingest_block_is_operational_facts_and_nothing_else():
    print("test_the_ingest_block_is_operational_facts_and_nothing_else")
    # D-01 put a new block on the OPEN route, so it inherits the rule above:
    # counts and states are operational, names and paths are observations.

    class Tick:
        """The shape `/health` reads — a health() and nothing more."""

        def __init__(self, payload):
            self.payload = payload

        def health(self):
            return dict(self.payload)

    secret = "/home/someone/.claude/projects/-secret-repo/deadbeef.jsonl"
    model = ReadModel(state={}, store=None, ingest=Tick({
        "state": "running", "files": 3, "filesSeen": 3, "linesRead": 41,
        "lastTick": "2026-07-30T00:00:00.000+00:00", "ticks": 9, "ops": 12,
        "walRecords": 4, "errors": 1, "lastError": "OSError"}))
    raw = Api(model, auth=Auth("t")).get("/health").body.decode()
    payload = json.loads(raw)
    check(payload["ingest"]["state"] == "running" and payload["ingest"]["files"] == 3,
          "the tick's own block reaches /health verbatim")
    check(secret not in raw and "-secret-repo" not in raw,
          "…and no path the tick reads is on it")
    check(payload["ingest"]["lastError"] == "OSError",
          "a failure is published as its TYPE — an OSError's message carries the "
          "filename, which is a home directory, a cwd slug and a session uuid")
    check(payload["writers"]["customState"] == "none",
          "the writers block names the stream with no producer, so an empty "
          "collection is legible as correct rather than as broken")

    absent = ReadModel(state={}, store=None)
    check(json.loads(Api(absent, auth=Auth("t")).get("/health").body)["ingest"]["state"]
          == "absent",
          "with no tick at all the state is `absent` — the pre-D-01 condition, "
          "which used to be indistinguishable from a running-but-quiet server")


def observed_corpus(root, cwd):
    """A `~/.claude`-shaped tree the tick OWNS: a session, an agent, a run.

    Every name in it is an observation — the session uuid, the 17-hex agent id
    and the `wf_` run id — which is the whole point: they are what a tailer's
    basename is made of, and what `/health` may not publish.
    """
    slug = os.path.join(root, "projects", sessions_mod.slug_for(cwd))
    run_dir = os.path.join(slug, OBSERVED_SESSION, "subagents", "workflows",
                           OBSERVED_RUN)
    os.makedirs(run_dir)
    with open(os.path.join(slug, f"{OBSERVED_SESSION}.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "user", "uuid": OBSERVED_SESSION,
                             "sessionId": OBSERVED_SESSION}) + "\n")
    with open(os.path.join(run_dir, f"agent-{OBSERVED_AGENT}.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "assistant", "uuid": OBSERVED_SESSION,
                             "sessionId": OBSERVED_SESSION}) + "\n")
    with open(os.path.join(run_dir, "journal.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "started", "key": "sp-01/implement",
                             "agentId": OBSERVED_AGENT}) + "\n")
    sessions_mod.reset_scope_cache()
    return slug


def test_a_real_ticks_tailer_names_publish_no_observation_either():
    print("test_a_real_ticks_tailer_names_publish_no_observation_either")
    # The regression this exists for: the tick registers one tailer per file it
    # discovers, into `ReadModel.tailers`, whose KEYS `/health` publishes
    # verbatim on the one unauthenticated route. A name built from the basename
    # therefore republishes exactly what `target_hash` refuses to — a session
    # transcript's basename IS the session uuid, an agent transcript's is the
    # agent id, and a journal's directory is the run id.
    #
    # It is asserted over a REAL `IngestTick` that has ticked over a real
    # corpus, because a stub with no tailers cannot fail for any naming scheme
    # at all — a tautology guarding the property that actually regressed.
    cwd = "/home/someone/Projects/secret-repo"
    root = tmpdir("ticked")
    observed_corpus(root, cwd)
    store = store_mod.Store(tmpdir("tickstore"))
    model = ReadModel(state={}, store=store, claude_root=root)
    tick = tick_mod.IngestTick(model, claude_root=root, cwd=cwd)
    model.ingest = tick
    tick.poll()

    raw = Api(model, auth=Auth("t")).get("/health").body.decode()   # NO token
    payload = json.loads(raw)
    check(payload["ingest"]["files"] >= 3 and len(payload["tailers"]) >= 3,
          f"the tick really did discover and register the corpus "
          f"({payload['ingest']['files']} files)")
    check(OBSERVED_SESSION not in raw,
          "…and the session uuid — which is a transcript's whole basename — is "
          "nowhere in the open route's body")
    check(OBSERVED_AGENT not in raw, "nor the agent id an `agent-*.jsonl` is named for")
    check(OBSERVED_RUN not in raw, "nor the run id its journal's directory is named for")
    check("secret-repo" not in raw, "nor the cwd slug every one of them sits under")
    check(all(row["name"].startswith("tick:") for row in payload["tailers"]),
          "each row is named by the poller that owns it plus an opaque digest")
    check(all(len(row["name"].split(":", 1)[1]) == 12 for row in payload["tailers"]),
          "…the same 12-hex digest `target_hash` publishes, so `name` and `target` "
          "are one function of one path and a reader can pair them")
    check(len({row["name"] for row in payload["tailers"]}) == len(payload["tailers"]),
          "…and distinct files still get distinct rows")
    check(not re.search(r'"[^"]*/[^"]*/[^"]*"', raw),
          "nothing path-shaped is on the route, exactly as before D-01")


def test_the_open_route_counts_requests_without_publishing_the_route_table():
    print("test_the_open_route_counts_requests_without_publishing_the_route_table")
    # Per-route hit counts were collected and never served — an observability
    # feature that was only a dict. They are not published per route on
    # purpose: a route name is path-shaped, and the rule for the one
    # unauthenticated route (the test above) is that nothing path-shaped
    # appears on it. Totals answer the operational question without putting the
    # URL table on an anonymous response.
    def broken(api, query, headers):
        raise KeyError("boom")

    ROUTES[("GET", "/api/broken")] = broken
    try:
        api = Api(ReadModel(state={}, store=None), auth=Auth("t"))
        token = {"authorization": "Bearer t"}
        api.get("/api/sessions", token)
        api.get("/api/sessions", token)
        api.get("/api/no-such-route", token)
        api.handle("GET", "/api/broken", {}, token)
        api.get("/api/sessions")                      # 401: refused before dispatch
        raw = api.get("/health").body.decode()
        counts = json.loads(raw)["requests"]
        check(counts["handled"] == 4 and counts["notFound"] == 1 and counts["failed"] == 1,
              "served, unrouted and failed are three totals an operator can act on")
        check(json.loads(api.get("/health").body.decode())["auth"]["rejections"] == 1,
              "an unauthenticated call never reaches a handler, and is counted as a rejection")
        check("/api/sessions" not in raw and "/api/broken" not in raw,
              "and no route name is published to an unauthenticated caller")
        check(not re.search(r'"[^"]*/[^"]*/[^"]*"', raw),
              "the open route stays free of anything path-shaped, ours included")
    finally:
        ROUTES.pop(("GET", "/api/broken"), None)


# --- response framing -----------------------------------------------------


def test_a_header_value_can_never_split_the_response():
    print("test_a_header_value_can_never_split_the_response")
    # `X-Touch-Basename` carries `os.path.basename` of an **agent-authored**
    # path out of a transcript, and a POSIX filename may contain CR and LF. A
    # response split here would serve an attacker-authored body from this
    # origin *without* the CSP sandbox the same handler attaches — so the
    # sanitizer lives in `head_bytes`, where every header passes through it.
    evil = "evil.txt\r\nX-Injected: yes\r\nSet-Cookie: a=b\r\n\r\n<html>pwned"
    raw = Response(status=200, body=b"ok", headers={"X-Touch-Basename": evil}).to_bytes()
    head = raw.split(b"\r\n\r\n")[0].split(b"\r\n")
    check(not [line for line in head
               if line.startswith(b"X-Injected") or line.startswith(b"Set-Cookie")],
          "CRLF in a header value cannot start a new header line")
    check(raw.count(b"\r\n\r\n") == 1 and raw.endswith(b"ok"),
          "nor split one response into two")
    check(len([line for line in head if line.startswith(b"X-Touch-Basename")]) == 1,
          "the value survives — flattened onto its own single line")

    # Second failure mode, no attacker required: a non-latin-1 basename made
    # `head_bytes` raise `UnicodeEncodeError` — in `HttpServer.handle`, outside
    # `Api.handle`'s try, so the connection was dropped with no response at all.
    unicode_raw = Response(status=200, body=b"ok",
                           headers={"X-Touch-Basename": "文件.txt"}).to_bytes()
    check(b"X-Touch-Basename" in unicode_raw and unicode_raw.endswith(b"ok"),
          "a non-latin-1 header value is replaced, never raised")
    check(server_mod.header_value("a\x00b\tc") == "a?b\tc",
          "NUL is scrubbed; tab is legal and kept")

    # `Content-Type` was interpolated straight into the head, exempt from the
    # sanitizer the same method installs. Every content type is server-authored
    # today — but "every header this server will ever add passes through it,
    # including the ones a later change adds" is either true of the method or
    # it is not a rule.
    sneaky = Response(status=200, body=b"ok",
                      content_type="text/plain\r\nX-Injected: yes").to_bytes()
    sneaky_head = sneaky.split(b"\r\n\r\n")[0].split(b"\r\n")
    check(not [line for line in sneaky_head if line.startswith(b"X-Injected")]
          and sneaky.count(b"\r\n\r\n") == 1,
          "a CRLF content type cannot start a header line or split the response "
          "either — no field in this method is exempt")
    check(Response(body=b"ok", content_type="text/plain; charset=文字"
                   ).to_bytes().endswith(b"ok"),
          "nor can a non-latin-1 one drop the connection in head_bytes")

    # A header *name* is always written by this file, so a bad one is a code
    # bug and says so — the one case where raising is the honest answer.
    try:
        Response(headers={"X-Bad\r\nName": "1"}).to_bytes()
        raised = False
    except ServerError:
        raised = True
    check(raised, "an illegal header NAME raises ServerError — that is a bug, not a request")


def test_the_api_survives_the_state_being_rewritten_under_it():
    print("test_the_api_survives_the_state_being_rewritten_under_it")
    # `ReadModel.state` is shared with the ingest tick on purpose (GD-22: a
    # tick's writes are visible to the next request with no copy). The tick
    # runs on the event loop and every handler runs on a `to_thread` worker, so
    # a handler that iterated the shared mapping raced `dict.__setitem__` and
    # answered 500 — under exactly the load GD-22 promises to survive.
    state = {"records": {f"{n:08d}-0000-4000-8000-000000000000":
                         {"_id": f"{n:08d}-0000-4000-8000-000000000000", "lineNo": n}
                         for n in range(20000)}}
    stop = threading.Event()

    def churn():
        n = 0
        while not stop.is_set():
            key = f"churn-{n}"
            state["records"][key] = {"_id": key}
            state[f"scratch{n % 3}"] = {key: {}}
            state["records"].pop(key, None)
            state.pop(f"scratch{n % 3}", None)
            n += 1

    api = Api(ReadModel(state=state, store=None, reduce_ttl=0), auth=Auth("t"))
    headers = {"authorization": "Bearer t"}
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    worker = threading.Thread(target=churn, daemon=True)
    worker.start()
    try:
        statuses = set()
        for _ in range(60):
            statuses.add(api.get("/api/query?collection=records&limit=1000", headers).status)
            statuses.add(api.get("/health", headers).status)
    finally:
        stop.set()
        worker.join(timeout=5)
        sys.setswitchinterval(old_interval)
    check(statuses == {200},
          f"120 requests against a state being rewritten, all 200 (got {sorted(statuses)})")
    check(getattr(api, "errors", 0) == 0,
          "and not one of them was a swallowed handler exception")


# --- Origin/Host allowlist ------------------------------------------------


def test_origin_policy():
    print("test_origin_policy")
    policy = OriginPolicy.default(DEFAULT_HOST, DEFAULT_PORT)
    ok = {"host": f"127.0.0.1:{DEFAULT_PORT}", "origin": f"http://127.0.0.1:{DEFAULT_PORT}"}
    check(policy.refusal(ok) is None, "the page this server served may connect")
    check(policy.refusal({"host": f"127.0.0.1:{DEFAULT_PORT}"}) is None,
          "a non-browser client with no Origin may connect (it still needs the token)")
    evil = dict(ok, origin="http://evil.example")
    check(policy.refusal(evil) is not None, "a cross-origin page is refused")
    check(policy.rejections == 1, "and the refusal is counted")
    rebind = {"host": "evil.example", "origin": "http://evil.example"}
    check(policy.refusal(rebind) is not None,
          "a DNS-rebinding Host is refused by name, not just by consequence")
    strict = OriginPolicy(hosts=(), origins=("http://ui.local",), allow_missing_origin=False)
    check(strict.refusal({"origin": "http://ui.local"}) is None, "an allowlisted Origin passes")
    check(strict.refusal({}) is not None, "with allow_missing_origin off, no Origin is refused")
    open_policy = OriginPolicy.default(OPEN_HOST, DEFAULT_PORT)
    check(open_policy.hosts == frozenset(),
          "an --open bind has no host allowlist to guess, and rule 3 still applies")
    check(open_policy.refusal({"host": "box:8932", "origin": "http://box:8932"}) is None,
          "same-origin still passes on an open bind")
    check(open_policy.refusal({"host": "box:8932", "origin": "http://elsewhere"}) is not None,
          "an open bind is not an open Origin policy")


# --- .touch/server.json ---------------------------------------------------


def test_server_json_is_0600():
    print("test_server_json_is_0600")
    root = os.path.join(tmpdir("state"), ".touch")
    path = write_server_json(root, {"token": "abc", "url": "http://127.0.0.1:8932/"})
    mode = os.stat(path).st_mode & 0o777
    check(mode == 0o600, f"server.json is 0600 (got {mode:o}) — GD-27's handling parity")
    check(json.load(open(path))["token"] == "abc", "and it carries the per-boot token")
    check(os.stat(os.path.dirname(path)).st_mode & 0o077 == 0,
          "its directory is not group/world accessible either")

    # `makedirs(mode=…)` is a no-op on a directory that already exists, and
    # `.touch/` is routinely created first by `store.Store` under whatever umask
    # the operator has. The mode is therefore asserted, not requested.
    existing = os.path.join(tmpdir("state-loose"), ".touch")
    os.makedirs(existing)
    os.chmod(existing, 0o777)
    write_server_json(existing, {"token": "abc"})
    check(os.stat(existing).st_mode & 0o077 == 0,
          "a pre-existing world-readable .touch/ is repaired, not trusted")


# --- defaults -------------------------------------------------------------


def test_the_defaults_are_gd13s():
    print("test_the_defaults_are_gd13s")
    check(DEFAULT_HOST == "127.0.0.1", "the default bind is loopback")
    check(OPEN_HOST == "0.0.0.0" and "--open" in server_mod._usage(),
          "0.0.0.0 exists only as an explicit opt-in flag")
    check(DEFAULT_PORT == 8932 and LEGACY_PORT == 8931,
          "8932 is Touch's reserved port; 8931 stays the legacy monitor's")
    check("sbx ports" in server_mod._usage()
          and re.search(r"(?i)never publish the mongod port", server_mod._usage()),
          "the opt-in is documented with the publish flow and the never-publish-mongod rule")
    check("27017" not in server_mod._usage(),
          "spelled without the numeral: R-42's rule is that no aggregator source "
          "line names a mongod port — it comes from .touch/mongo.json "
          "(tests/test_mongo_deploy.py guards every aggregator/*.py for it)")
    server = HttpServer(ReadModel(state={}, store=None), host=DEFAULT_HOST, port=0)
    check(server.api.bind["loopback"] is True, "/health reports the posture it actually has")


# --- source guards --------------------------------------------------------


def test_the_server_derives_nothing_and_differences_nothing():
    print("test_the_server_derives_nothing_and_differences_nothing")
    # GD-23: the reducer is the only derivation site. The server may *read*
    # `agents.reduce`'s output and may not reimplement any of it.
    calls = {node.func.attr for node in ast.walk(TREE)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    check("reduce" in calls, "the server calls the one reducer")
    for forbidden in ("IDLE_LIMIT", "liveness(", "verdict_of(", "attempt_label("):
        check(forbidden not in SOURCE.replace("agents_mod.IDLE_LIMIT_SECONDS", ""),
              f"the server never computes {forbidden.strip('(')} itself — R-54 decided it")
    check("failed" not in re.findall(r'"state":\s*"failed"', SOURCE),
          "no state string is manufactured here")

    # GD-25/R-55: absolute tokens are what make (stream, seq) resume safe.
    subtractions = [node for node in ast.walk(TREE)
                    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)]
    token_names = {"in", "out", "cached", "cache_write", "tokens"}
    for node in subtractions:
        text = ast.get_source_segment(SOURCE, node) or ""
        check(not (token_names & set(re.findall(r"[a-z_]+", text))),
              f"no token field is ever differenced: {text!r}")
    # "delta" may appear in the prose that explains why there is none; it may
    # not appear in the code. Comments and strings are stripped with the
    # tokenizer rather than by a regex, so the guard cannot be satisfied by
    # spelling a variable name inside a docstring.
    code = []
    with open(SRC / "aggregator" / "server.py", "rb") as fh:
        for token in tokenize.tokenize(fh.readline):
            if token.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL,
                                  tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
                code.append(token.string)
    executable = " ".join(code).lower()
    check("delta" not in executable,
          "no executable line in this file mentions a delta — the wire carries absolutes")


def test_the_server_imports_no_driver():
    print("test_the_server_imports_no_driver")
    imported = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module.split(".")[0])
    check(not ({"pymongo", "bson", "dns"} & imported),
          "GD-21: only mongo_store.py and mirror.py may import the driver")
    check("mirror" not in imported,
          "and the server does not import mirror.py either — the health block is injected")
    stdlib = {"asyncio", "datetime", "hashlib", "hmac", "json", "os", "re", "secrets",
              "sys", "time", "urllib", "dataclasses", "__future__"}
    check(imported <= stdlib, f"every import is stdlib: {sorted(imported - stdlib)}")


# --- the transport (one real socket) --------------------------------------


def http_request(port, raw):
    conn = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn.sendall(raw)
    chunks = []
    while True:
        data = conn.recv(65536)
        if not data:
            break
        chunks.append(data)
    conn.close()
    return b"".join(chunks)


def test_a_real_socket_round_trip():
    print("test_a_real_socket_round_trip")

    async def run():
        root = tmpdir("sock")
        store = store_mod.Store(root)
        stream = "run:wf_socket01"
        store.append_many(stream, [
            {"kind": "node", "provenance": "harness", "ref": {"agentId": "a" * 17},
             "data": {"n": n}, "source": "ingest", "ts": None} for n in range(3)])
        model = ReadModel(state={}, store=store)
        server = HttpServer(model, host="127.0.0.1", port=0, auth=Auth("sock3t"),
                            tick=0.05)
        await server.start()
        port = server.port
        try:
            raw = await asyncio.to_thread(
                http_request, port, b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            check(raw.startswith(b"HTTP/1.1 200 OK"), "an HTTP request over a real socket works")
            check(b"X-Content-Type-Options: nosniff" in raw, "nosniff is on the wire")

            raw = await asyncio.to_thread(
                http_request, port, b"GET /api/sessions HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            check(raw.startswith(b"HTTP/1.1 401"), "an unauthenticated API call is 401 on the wire")

            # The 413 branch only fires if asyncio's own stream limit is above
            # MAX_HEAD_BYTES; with the default (the same 64 KiB) `readuntil`
            # raised first and the oversized head got a silent close instead.
            oversized = (b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Big: "
                         + b"a" * (server_mod.MAX_HEAD_BYTES + 4096) + b"\r\n\r\n")
            raw = await asyncio.to_thread(http_request, port, oversized)
            check(raw.startswith(b"HTTP/1.1 413"),
                  "an oversized request head is answered 413, not dropped in silence")

            key = base64.b64encode(b"0123456789abcdef").decode()
            bad = (f"GET /ws?token=sock3t HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   f"Origin: http://evil.example\r\n\r\n").encode()
            raw = await asyncio.to_thread(http_request, port, bad)
            check(raw.startswith(b"HTTP/1.1 403"), "a cross-origin WS upgrade is 403 (GD-13)")

            noauth = (f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                      f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()
            raw = await asyncio.to_thread(http_request, port, noauth)
            check(raw.startswith(b"HTTP/1.1 401"), "an untokened WS upgrade is 401")

            old = (f"GET /ws?token=sock3t HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 8\r\n\r\n").encode()
            raw = await asyncio.to_thread(http_request, port, old)
            check(raw.startswith(b"HTTP/1.1 426") and b"Sec-WebSocket-Version: 13" in raw,
                  "an unsupported WS version is 426 advertising 13, never the page body")

            # The real handshake, then hello -> replay -> switch -> live frame.
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write((f"GET /ws?token=sock3t HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                          f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                          f"Origin: http://127.0.0.1:{port}\r\n\r\n").encode())
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            check(head.startswith(b"HTTP/1.1 101"), "a same-origin, tokened upgrade is 101")
            check(f"Sec-WebSocket-Accept: {ws.accept_key(key)}".encode() in head,
                  "the accept key is RFC 6455's")

            decoder = ws.FrameDecoder("client")
            frames = []

            async def pull(until):
                while len(frames) < until:
                    chunk = await asyncio.wait_for(reader.read(65536), 5)
                    if not chunk:
                        return
                    for message in decoder.feed(chunk):
                        if message.is_text:
                            frames.append(json.loads(message.text))

            await pull(5)
            check(frames[0]["type"] == "hello" and frames[0]["live"] is False,
                  "the first frame is the handshake, and it is not live")
            events = [f for f in frames if f["type"] == "event"]
            check(len(events) == 3 and all(f["live"] is False for f in events),
                  "the bounded replay arrives with live:false — painted once, never animated")
            switch = [f for f in frames if f["type"] == "mode"]
            check(len(switch) == 1 and switch[0]["live"] is True,
                  "exactly one mode frame marks the replay->tail boundary")

            store.append(stream, kind="token", provenance="harness",
                         ref={"agentId": "a" * 17},
                         data={"tokens": {"in": 5, "out": 1, "cached": 0, "cache_write": 0}})
            await pull(len(frames) + 1)
            live = [f for f in frames if f["type"] == "event" and f["live"]]
            check(live and live[-1]["record"]["kind"] == "token",
                  "an append after the switch arrives as a live frame")
            check(live[-1]["record"]["data"]["tokens"]["in"] == 5,
                  "carrying the absolute token count, never a delta")
            check(live[-1]["cursor"] == store_mod.cursor_key(stream, live[-1]["seq"]),
                  "and its (stream, seq) cursor, so the client can resume from it")

            writer.write(ws.encode_close(ws.CLOSE_NORMAL, "", mask=os.urandom(4)))
            await writer.drain()
            writer.close()
        finally:
            await server.close()

    asyncio.run(run())


def test_a_handshake_names_the_parameters_it_could_not_use():
    print("test_a_handshake_names_the_parameters_it_could_not_use")
    # The two silent drops, over a real socket, because both were transport-level
    # swallows that no unit test of the session object could see: `stream()`
    # caught the 400 from a malformed `?cursor=` and started from `{}` (losing
    # the client's *other*, valid resume positions), and `or None` turned a
    # `?stream=` that matched nothing into "serve every stream".

    async def run():
        root = tmpdir("handshake")
        store = store_mod.Store(root)
        stream = "run:wf_hand0001"
        store.append_many(stream, [
            {"kind": "node", "provenance": "harness", "ref": {"agentId": "b" * 17},
             "data": {"n": n}, "source": "ingest", "ts": None} for n in range(6)])
        store.append("session:900-11000", kind="session", provenance="harness",
                     ref={"pid": 900, "procStart": "11000"}, data={})
        server = HttpServer(ReadModel(state={}, store=store), host="127.0.0.1",
                            port=0, auth=Auth("h4nd"), tick=0.05)
        await server.start()
        port = server.port

        async def frames_until_mode(params):
            """Upgrade with `params`, collect frames through the mode switch."""
            key = base64.b64encode(os.urandom(16)).decode()
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write((f"GET /ws?token=h4nd&{params} HTTP/1.1\r\n"
                          f"Host: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                          f"Sec-WebSocket-Version: 13\r\n"
                          f"Origin: http://127.0.0.1:{port}\r\n\r\n").encode())
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert head.startswith(b"HTTP/1.1 101"), head[:40]
            decoder, out = ws.FrameDecoder("client"), []
            while not any(f["type"] == "mode" for f in out):
                chunk = await asyncio.wait_for(reader.read(65536), 5)
                if not chunk:
                    break
                for message in decoder.feed(chunk):
                    if message.is_text:
                        out.append(json.loads(message.text))
            writer.close()
            return out

        try:
            cursor = urllib.parse.quote(store_mod.cursor_key(stream, 3), safe="")
            mixed = await frames_until_mode(f"cursor={cursor}&cursor=garbage")
            hello = mixed[0]
            check(hello["cursorsRejected"] == ["garbage"],
                  "a malformed ?cursor= is named on the hello frame — the only refusal "
                  "left after a 101")
            check(hello["cursors"] == {stream: 3} and hello["resumed"] is True,
                  "and the valid cursor in the same handshake still applies")
            seqs = [f["seq"] for f in mixed if f["type"] == "event" and f["stream"] == stream]
            check(seqs == [4, 5, 6],
                  "so the socket resumes after it: one typo no longer re-sends every "
                  "record the client already holds (R-55)")

            nothing = await frames_until_mode("stream=not%20a%20stream")
            check(nothing[0]["streamsRejected"] == ["not a stream"],
                  "a malformed ?stream= is named too")
            check(nothing[0]["streams"] == [] and
                  not [f for f in nothing if f["type"] == "event"],
                  "and the socket serves NOTHING — a failed selector must never widen "
                  "into every stream in the store (GD-12)")

            everything = await frames_until_mode("")
            check({f["stream"] for f in everything if f["type"] == "event"}
                  == {stream, "session:900-11000"},
                  "while no selector at all still means the whole store")

            # The third silent drop: `?from=abc` and no `?from=` at all both
            # produced `{"from": null, "fromApplied": false}`, so a client could
            # not tell a typo from a parameter it never sent — while `?cursor=`
            # and `?stream=` both came back raw.
            unparseable = await frames_until_mode("from=abc")
            check(unparseable[0]["fromRejected"] == "abc"
                  and unparseable[0]["from"] is None,
                  "a ?from= that does not parse comes back raw on hello, like every "
                  "other parameter this socket could not use")
            check(everything[0]["fromRejected"] is None,
                  "and a handshake that sent none is a distinguishable case, not the same null")

            ghost = await frames_until_mode("stream=run%3Awf_notyet0001")
            check(ghost[0]["streamsUnobserved"] == ["run:wf_notyet0001"]
                  and ghost[0]["currentRun"] is None,
                  "a well-formed selector for a run that has not started is served but "
                  "labelled unobserved — it never becomes the currentRun the page titles itself with")
        finally:
            await server.close()

    asyncio.run(run())


def test_the_idle_marker_is_sent_and_a_subscribe_is_answered_in_order():
    print("test_the_idle_marker_is_sent_and_a_subscribe_is_answered_in_order")
    # Two contract clauses that only exist at the transport, so no unit test of
    # `WsSession` could see either. (1) The frame table advertises
    # `{"type":"tick"}` as the idle keepalive marker; the loop sent a protocol
    # ping and nothing else, and a browser cannot observe a pong — `onmessage`
    # never fires for one — so sp-13 would have restated a frame that was never
    # sent. (2) A `subscribe` is a resume: its backfill must reach the wire
    # BEFORE the ack that publishes the cursor those records justify.

    async def run():
        root = tmpdir("idle")
        store = store_mod.Store(root)
        stream = "run:wf_idle0001"
        store.append_many(stream, [
            {"kind": "node", "provenance": "harness", "ref": {"agentId": "c" * 17},
             "data": {"n": n}, "source": "ingest", "ts": None} for n in range(6)])
        server = HttpServer(ReadModel(state={}, store=store), host="127.0.0.1",
                            port=0, auth=Auth("1dle"), tick=0.02, keepalive=0.05)
        await server.start()
        port = server.port
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write((f"GET /ws?token=1dle HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                          f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                          f"Origin: http://127.0.0.1:{port}\r\n\r\n").encode())
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            check(head.startswith(b"HTTP/1.1 101"), "the socket is up")

            decoder, frames = ws.FrameDecoder("client"), []

            async def pull(ready):
                while not ready(frames):
                    chunk = await asyncio.wait_for(reader.read(65536), 5)
                    if not chunk:
                        return
                    for message in decoder.feed(chunk):
                        if message.is_text:
                            frames.append(json.loads(message.text))

            await asyncio.wait_for(
                pull(lambda f: any(x["type"] == "tick" for x in f)), 5)
            idle = [f for f in frames if f["type"] == "tick"][0]
            check(idle["live"] is True and idle["ts"].endswith("Z"),
                  "the idle marker the contract advertises is a real frame, with a "
                  "GD-11 ts a quiet page can show")
            check(set(idle) >= {"type", "live", "ts"},
                  "carrying exactly the keys the normative table shows on it")

            writer.write(ws.encode_text(
                json.dumps({"type": "subscribe", "cursors": {stream: 2}}),
                mask=os.urandom(4)))
            await writer.drain()
            await asyncio.wait_for(
                pull(lambda f: any(x["type"] == "subscribed" for x in f)), 5)
            mode_at = [i for i, f in enumerate(frames) if f["type"] == "mode"][0]
            ack_at = [i for i, f in enumerate(frames) if f["type"] == "subscribed"][0]
            between = frames[mode_at + 1:ack_at]
            resent = [f for f in between if f["type"] == "event"]
            check([f["seq"] for f in resent] == [3, 4, 5, 6],
                  "the rewound range is re-delivered over the wire, not merely acknowledged")
            check(all(f["live"] is False for f in resent),
                  "as backfill — the client repaints it, it does not animate it")
            check(frames[ack_at]["accepted"] == {stream: 2}
                  and frames[ack_at]["cursors"][stream] == 6,
                  "and the ack follows the frames it accounts for, naming only what was sent")

            writer.write(ws.encode_close(ws.CLOSE_NORMAL, "", mask=os.urandom(4)))
            await writer.drain()
            writer.close()
        finally:
            await server.close()

    asyncio.run(run())


def test_parse_head_is_total():
    print("test_parse_head_is_total")
    method, route, query, headers = HttpServer.parse_head(
        b"GET /api/events?after=&stream=run%3Ax HTTP/1.1\r\nHost: h\r\nX-A: 1\r\n\r\n")
    check((method, route) == ("GET", "/api/events"), "the request line parses")
    check(query["after"] == [""],
          "an empty parameter is kept — 'given and empty' must be visible to the 400")
    check(query["stream"] == ["run:x"], "percent-escapes are decoded")
    check(headers["host"] == "h" and headers["x-a"] == "1", "headers are lower-cased")
    check(HttpServer.parse_head(b"\r\n\r\n")[0] == "",
          "a garbage head does not raise — it yields no method, which the table 404s")
    check(HttpServer.parse_head(b"GET\r\n\r\n")[1] == "/", "a truncated request line does not raise")


def main():
    try:
        for t in (test_every_route_but_health_needs_the_token,
                  test_all_three_token_carriers_work_and_a_wrong_one_does_not,
                  test_the_route_table_is_static_and_has_no_fallback,
                  test_the_read_and_control_groups_are_declared_before_a_control_exists,
                  test_a_handler_bug_is_a_500_and_not_a_traceback,
                  test_an_unknown_id_is_never_answered_with_another_ones_data,
                  test_safe_artifact_path_contains_everything,
                  test_served_files_carry_the_csp_sandbox_and_nosniff,
                  test_an_empty_task_list_says_which_kind_of_empty_it_is,
                  test_no_status_is_ever_labelled_ok_when_it_is_not,
                  test_the_token_is_injected_into_the_page,
                  test_health_never_carries_a_credential,
                  test_health_publishes_no_observation_to_an_unauthenticated_caller,
                  test_the_ingest_block_is_operational_facts_and_nothing_else,
                  test_a_real_ticks_tailer_names_publish_no_observation_either,
                  test_the_open_route_counts_requests_without_publishing_the_route_table,
                  test_a_header_value_can_never_split_the_response,
                  test_the_api_survives_the_state_being_rewritten_under_it,
                  test_origin_policy,
                  test_server_json_is_0600,
                  test_the_defaults_are_gd13s,
                  test_the_server_derives_nothing_and_differences_nothing,
                  test_the_server_imports_no_driver,
                  test_parse_head_is_total,
                  test_a_real_socket_round_trip,
                  test_a_handshake_names_the_parameters_it_could_not_use,
                  test_the_idle_marker_is_sent_and_a_subscribe_is_answered_in_order):
            t()
    finally:
        for path in TMPDIRS:
            shutil.rmtree(path, ignore_errors=True)
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all server core tests passed")


if __name__ == "__main__":
    main()
