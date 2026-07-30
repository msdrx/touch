#!/usr/bin/env python3
"""memory.html driven against a LIVE monitor_server, over real HTTP (item I15).

Run: ``python3 test_memory_ui.py`` (stdlib only + `node`, no pytest, non-zero on
failure). `run_all.sh` picks it up by its `test_*.py` glob; GD-U6 keeps it out of
the payload. SKIPS LOUDLY where `node` is absent — and the parity half below
still runs, so a machine with no node still checks that the page and the server
agree about the wire.

WHY A SECOND UI FILE
--------------------
`tests/monitoring/test_frontend.py` owns memory.html's source guards AND a
node+`vm` harness with a FAKE `fetch`: it invents the answers (`sha-note-1`,
`{root, files: […]}`) and drives the page's behaviour against them. That is the
right shape for the races — a held answer, an out-of-order load, a 409 arriving
after the operator moved on — because only a fake can hold a response open.

Its blind spot is the seam. Every invented answer is this repository asserting
that it agrees with itself: rename a field on the server, mis-spell
`allowPinned`, answer `text/plain` where the page checks for JSON, stamp
`modified:` into the bytes so the sha the page adopted no longer describes the
file, and the fake-fetch harness stays green while the real editor is dead on
arrival. So this file removes the fake:

* a real `monitor_server` module, booted over a synthetic project on an
  ephemeral loopback port, in BOTH postures (write plane on, and the default
  off — two module instances, because `MEMORY_WRITE` is resolved at import);
* the page's own script run under `vm` with a fake DOM but a REAL `fetch`, so
  every request it makes is served by the code that ships and every field it
  reads is a field the server really sent;
* the operator's story asserted from the DOM and from the disk at once: a save
  lands the buffer, the sha the response carried is the sha the next save may
  present, a real 409 does not touch the textarea, a real hygiene refusal is
  named on screen, `pinned:` needs the checkbox, delete really moves the file
  into the server's trash directory and restore really re-creates it;
* and a NODE-FREE parity layer that compares the field names the page reads with
  the keys the server's own payload builders produce — in both directions,
  including the `allowPinned` key memory.html's comment flags as the one addition
  it makes to G5's body ("a name mismatch there turns the ticked checkbox into a
  permanent 4xx that no test on either side would see").

House rules honoured: ephemeral ports only (the live monitor on 8931 is never
bound), a throwaway `$HOME` and project (the repository's own `.touch/memory` is
never read or written), and no wall-clock assertions — the harness waits on
conditions with a deadline so a hung request fails loudly instead of hanging the
suite.
"""
import asyncio
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
sys.path.insert(0, os.path.dirname(HERE))
from _roots import MON                                   # noqa: E402

MODULE_PATH = os.path.join(str(MON), "monitor_server.py")
MEMORY_HTML = os.path.join(str(MON), "memory.html")

_TMP_BASE = os.environ.get("TMPDIR") or "/tmp/claude-1000"
os.makedirs(_TMP_BASE, exist_ok=True)
BASE = tempfile.mkdtemp(prefix="memui-", dir=_TMP_BASE)
atexit.register(shutil.rmtree, BASE, ignore_errors=True)

FAKE_HOME = os.path.join(BASE, "home")
os.makedirs(os.path.join(FAKE_HOME, ".claude"), mode=0o700)
os.environ["HOME"] = FAKE_HOME

PROJECT = os.path.join(BASE, "proj")
os.makedirs(os.path.join(PROJECT, ".claude"))
ROOT = os.path.join(PROJECT, ".touch", "memory")
os.makedirs(ROOT, mode=0o700)
STATE_DIR = os.path.join(BASE, "state", "t1")
os.makedirs(STATE_DIR)

#: What the synthetic root holds when the page boots. One index (so the
#: index-only affordances render), one topic note (the file the story is told
#: about) and one file WITH frontmatter (so the server's `modified:` stamping is
#: exercised through the page rather than described in a comment).
SEED = {
    "MEMORY.md": "# Memory index\n\n- [note](note.md) — a thing worth keeping\n",
    "note.md": "note one\n",
    "front.md": "---\ntitle: t\n---\n\nbody\n",
}
for _name, _text in SEED.items():
    with open(os.path.join(ROOT, _name), "w", encoding="utf-8") as _fh:
        _fh.write(_text)

# `aligned: true` is the interesting posture for the page (its banner, and the
# sentence that tells the operator an edit here reaches a session), and the
# project-local layer is the nearest documented one, so this decides it whatever
# the machine's own settings say.
with open(os.path.join(PROJECT, ".claude", "settings.local.json"), "w",
          encoding="utf-8") as _fh:
    json.dump({"autoMemoryDirectory": ROOT}, _fh)

for _name in ("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE", "CLAUDE_CODE_REMOTE_MEMORY_DIR",
              "CLAUDE_MEMORY_STORES", "ORCH_PORT", "ORCH_TASKS_ROOT",
              "ORCH_ALLOW_ORIGIN", "ORCH_ALLOW_HOST", "TOUCH_PROJECT_CWD"):
    os.environ.pop(_name, None)

SKIPS = []


def _load(name, *, write):
    """monitor_server.py, imported fresh — one module instance per posture.

    `MEMORY_WRITE` and `MEMORY_ROOT` are resolved at import, so "the write plane
    is off" is a whole server and not an assignment. `$CLAUDE_PROJECT_DIR` stays
    set afterwards because the alignment answer re-reads it per request.
    """
    import importlib.util
    os.environ["CLAUDE_PROJECT_DIR"] = PROJECT
    os.environ["ORCH_STATE_DIR"] = STATE_DIR
    if write:
        os.environ["TOUCH_ALLOW_MEMORY_WRITE"] = "1"
    else:
        os.environ.pop("TOUCH_ALLOW_MEMORY_WRITE", None)
    saved_argv = sys.argv
    sys.argv = ["monitor_server.py"]
    try:
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.argv = saved_argv
        os.environ.pop("TOUCH_ALLOW_MEMORY_WRITE", None)
    return mod


MS = _load("ms_memory_ui_on", write=True)
MS_OFF = _load("ms_memory_ui_off", write=False)
assert MS.MEMORY_ROOT == ROOT and MS.MEMORY_WRITE is True, MS.MEMORY_ROOT
assert MS_OFF.MEMORY_WRITE is False


# --------------------------------------------------------------------------
# The node-free half: does the page read the fields the server writes?
# --------------------------------------------------------------------------
#: The page's script, sliced by function, so a field name is attributed to the
#: answer it is read from rather than matched anywhere in 1,600 lines.
#:
#: Read defensively: `memory.html` belongs to another sub-plan of this same plan
#: (G4/I13), so a tree where it has not landed must SKIP loudly — "the page is not
#: here" is the honest answer, and a suite that reddens because another owner's
#: file is absent teaches people to ignore red.
HAS_PAGE = os.path.isfile(MEMORY_HTML)
PAGE, SCRIPT = "", ""
if HAS_PAGE:
    with open(MEMORY_HTML, encoding="utf-8") as _fh:
        PAGE = _fh.read()
    SCRIPT = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]


def _skip(message):
    """One skip, on the wire convention `run_all.sh` counts (`skip` first)."""
    SKIPS.append(message)
    print(f"  skip: {message}")


def _slice(text, start, end, label):
    at = text.find(start)
    assert at != -1, f"{label}: {start!r} is gone from memory.html"
    to = text.find(end, at)
    assert to != -1, f"{label}: {end!r} is gone from memory.html"
    return text[at:to]


def test_the_page_reads_only_fields_the_server_actually_sends():
    """G5's shapes, compared against the payload builders themselves.

    Not against a copy of the table in a comment: the payloads are BUILT here, by
    the module, over the synthetic root, and the field names are read out of
    memory.html. A rename on either side fails this test with both spellings in
    the message.
    """
    if not HAS_PAGE:
        _skip("memory.html is not in this tree, so the page/server field parity "
              "cannot be compared (the memory page's own sub-plan owns it)")
        return
    listing = MS.memory_list_payload()
    read = MS.memory_read_payload("note.md")
    row = listing["files"][0]

    wanted = set(re.findall(r"\bb\.([A-Za-z_][A-Za-z0-9_]*)",
                            _slice(SCRIPT, "async function memRefreshList()",
                                   "function noteDiskDrift", "list slice")))
    assert wanted, "the list slice reads no fields — the slice markers moved"
    assert wanted <= set(listing), (wanted - set(listing), sorted(listing))

    got = set(re.findall(r"\bb\.([A-Za-z_][A-Za-z0-9_]*)",
                         _slice(SCRIPT, "async function memOpen(",
                                "function noteOutcome(", "read slice")))
    assert got, "the read slice reads no fields — the slice markers moved"
    assert got <= set(read), (got - set(read), sorted(read))

    rows = set(re.findall(r"\bf\.([A-Za-z_][A-Za-z0-9_]*)",
                          _slice(SCRIPT, "function renderRows()", "function tag(",
                                 "row slice")))
    assert rows, "the row slice reads no fields"
    assert rows <= set(row), (rows - set(row), sorted(row))

    limits = set(re.findall(r'(?:limitNum|capText)\("([A-Za-z]+)"', SCRIPT))
    assert limits, "the page prints no caps"
    assert limits <= set(listing["limits"]), (limits, listing["limits"])


def test_the_write_body_keys_the_page_sends_are_the_keys_the_server_reads():
    """The `allowPinned` seam, and the two G5 keys beside it.

    memory.html's own comment names this risk: "monitor_server.py's write path
    must accept exactly `allowPinned` … a name mismatch there turns the ticked
    checkbox into a permanent 4xx that no test on either side would see." So the
    keys are read out of the page and looked for in the server, and then the
    round trip is made for real: a `pinned:` body is refused, and the same body
    with the page's own flag is accepted.
    """
    if not HAS_PAGE:
        _skip("memory.html is not in this tree, so the page/server field parity "
              "cannot be compared (the memory page's own sub-plan owns it)")
        return
    save = _slice(SCRIPT, "async function memSave()", "function adoptSaved(",
                  "save slice")
    keys = set(re.findall(r"\bpayload\.([A-Za-z]+) =", save))
    keys |= set(re.findall(r"const payload = \{ ([A-Za-z]+):", save))
    keys |= set(re.findall(r", ([A-Za-z]+): mem\.base\.sha256", save))
    assert {"content", "ifMatch", "allowPinned"} <= keys, keys
    with open(MODULE_PATH, encoding="utf-8") as handle:
        module = handle.read()
    for key in keys:
        assert f'payload.get("{key}")' in module, \
            f"the page sends `{key}` and monitor_server.py never reads it"

    pinned = "---\ntitle: t\npinned: true\n---\n\nbody\n"
    try:
        MS.memory_mutate("create", "parity-pin.md", {"content": pinned}, "")
        raise AssertionError("a pinned: create was accepted without the flag")
    except MS.MemoryRefusal as exc:
        assert exc.status == 422 and exc.category == "pinned", exc.category
    status, body = MS.memory_mutate("create", "parity-pin.md",
                                    {"content": pinned, "allowPinned": True}, "")
    assert status == 201, (status, body)
    assert {"sha256", "mtime_ns", "size"} <= set(body), body
    status, body = MS.memory_mutate("delete", "parity-pin.md", {}, body["sha256"])
    assert status == 200 and "trash" in body, body
    assert "r.body.trash" in SCRIPT, "the page must read the trash path it is sent"


# --------------------------------------------------------------------------
# The driven half: the real page, the real server, a fake DOM.
# --------------------------------------------------------------------------
#: Every label the driver must print. A harness whose assertions silently stop
#: running is worse than no harness, so the Python side asserts the whole set
#: arrived — the discipline `test_frontend.py` established for its own driver.
EXPECTED = (
    "the page boots against the live server and names its real memory root",
    "the boot list is the tokened list route",
    "the caps on screen are the caps the server enforces",
    "the aligned banner is the server's own answer",
    "every seeded file gets a row",
    "a load fills the editor with the bytes that are on disk",
    "a save puts the operator's buffer on disk",
    "the badge reads saved with the server's own mtime",
    "a second save does not conflict with the first",
    "the server stamps modified: and the page still saves twice",
    "a real 409 leaves the buffer alone and offers three exits",
    "overwrite re-sends with the sha the server published, and lands",
    "a refused @-import is named on screen and nothing was written",
    "a pinned: save is refused until the checkbox says yes in words",
    "...and then it saves, with the flag the server requires",
    "create posts a new file into the memory root",
    "delete moves the file into the server's own trash directory",
    "restore re-creates the file from the bytes the server removed",
    "the poll does not clobber a dirty buffer",
    "the write plane being off is read from the server, not assumed",
    "save is disabled with the server's own reason",
    "no create affordance renders while the plane is off",
    "a save attempt over an off write plane sends nothing at all",
)

DRIVER_JS = r""""use strict";
/* Drives plugin/touch/shared/monitoring/memory.html against a LIVE
 * monitor_server.py. Prints one `PASS: <label>` / `FAIL: <label> — <detail>`
 * per assertion; exits non-zero if the harness itself breaks.
 *
 * The DOM is fake (there is no browser here) and `fetch` is REAL: node's global
 * fetch, pointed at the ephemeral loopback port the Python side booted. The one
 * thing this wrapper does that the page cannot is add the `Origin` header a
 * browser adds for it — the server requires a present, same-origin Origin on
 * every write (W3), and it is unforgeable precisely because the page does not
 * set it. */
const fs = require("fs");
const vm = require("vm");

const CFG = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const HTML = fs.readFileSync(CFG.html, "utf8");
const SCRIPT = HTML.split("<script>")[1].split("</script>")[0];

let failed = 0;
function ok(label, cond, detail) {
    if (cond) {
        console.log("PASS: " + label);
    } else {
        failed += 1;
        console.log("FAIL: " + label + " — "
            + String(detail === undefined ? "" : detail).slice(0, 400));
    }
}

/* --- the fake DOM: only what memory.html actually touches ---------------- */
class TextNode {
    constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null; }
    get textContent() { return this.data; }
    set textContent(v) { this.data = String(v); }
}
class Element {
    constructor(tag) {
        this.nodeType = 1;
        this.tagName = String(tag).toUpperCase();
        this.childNodes = [];
        this.listeners = {};
        this.parentNode = null;
        this.hidden = false;
        this.disabled = false;
        this.checked = false;
        this.value = "";
        this.title = "";
        this.href = "";
        this.type = "";
        this.id = "";
        this._class = "";
    }
    get className() { return this._class; }
    set className(v) { this._class = String(v); }
    get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
    appendChild(node) {
        if (node.parentNode) node.parentNode.removeChild(node);
        node.parentNode = this;
        this.childNodes.push(node);
        return node;
    }
    removeChild(node) {
        const at = this.childNodes.indexOf(node);
        if (at !== -1) { this.childNodes.splice(at, 1); node.parentNode = null; }
        return node;
    }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    fire(type) {
        const ev = { type: type, preventDefault() { ev.defaulted = true; }, returnValue: "" };
        (this.listeners[type] || []).forEach((fn) => fn(ev));
        return ev;
    }
    get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
    set textContent(v) {
        this.childNodes.forEach((n) => { n.parentNode = null; });
        this.childNodes = [];
        if (String(v) !== "") {
            const t = new TextNode(v);
            t.parentNode = this;
            this.childNodes.push(t);
        }
    }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
/* Conditions with a deadline, never a fixed sleep: the requests are real, so a
 * wall-clock guess is either flaky or slow, and a deadline that expires prints a
 * failing assertion rather than hanging the suite (GD-G's work-not-clock rule as
 * far as real I/O allows). */
async function waitFor(fn, label) {
    for (let spent = 0; spent < 8000; spent += 25) {
        if (fn()) return true;
        await sleep(25);
    }
    console.log("FAIL: " + label + " — timed out waiting for the server");
    failed += 1;
    return false;
}

/** One page, in its own vm context, talking to one posture's server. */
function makePage(posture) {
    const byId = {};
    CFG.ids.forEach((id) => { const n = new Element("div"); n.id = id; byId[id] = n; });
    const document = {
        body: new Element("body"),
        createElement: (tag) => new Element(tag),
        createTextNode: (v) => new TextNode(v),
        getElementById: (id) => (Object.prototype.hasOwnProperty.call(byId, id)
            ? byId[id] : null),
        addEventListener: () => {},
    };
    const origin = new URL(posture.base).origin;
    const requests = [];
    const intervals = [];
    const store = {};
    const answers = { confirm: true };
    async function pageFetch(url, init) {
        const absolute = new URL(String(url), posture.base);
        const method = String((init && init.method) || "GET").toUpperCase();
        const headers = Object.assign({}, (init && init.headers) || {});
        if (method !== "GET") headers.origin = origin;     // the browser's half
        requests.push({ path: absolute.pathname, search: absolute.search,
                        method: method, headers: headers,
                        body: (init && init.body) || null });
        return fetch(absolute.href, { method: method, headers: headers,
                                      body: (init && init.body) || undefined });
    }
    const window = { addEventListener: (t, fn) => { window["on" + t] = fn; } };
    const sandbox = {
        document: document, window: window, fetch: pageFetch,
        location: { search: "?token=" + posture.token,
                    href: posture.base + "/memory?token=" + posture.token },
        localStorage: {
            getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
            setItem: (k, v) => { store[k] = String(v); },
        },
        confirm: () => answers.confirm,
        setInterval: (fn) => { intervals.push(fn); return intervals.length; },
        clearInterval: () => {},
        URL: URL, URLSearchParams: URLSearchParams, console: console, Intl: Intl,
        Math: Math, JSON: JSON, Date: Date, Number: Number, String: String,
        Boolean: Boolean, Object: Object, Array: Array, Set: Set, Map: Map,
        Promise: Promise, Error: Error, RegExp: RegExp, isFinite: isFinite,
        encodeURIComponent: encodeURIComponent, setTimeout: setTimeout,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(SCRIPT, sandbox, { filename: CFG.html });
    return { byId: byId, sandbox: sandbox, requests: requests,
             intervals: intervals, answers: answers, posture: posture };
}

/** The directory, as the server reports it — deliberately NOT through the page. */
async function rawList(posture) {
    const res = await fetch(posture.base + "/api/memory/list",
                            { headers: { "x-orch-token": posture.token } });
    return res.json();
}

/** One file, likewise. */
async function raw(posture, method, name, payload, ifMatch) {
    let url = posture.base + "/api/memory/file?name=" + encodeURIComponent(name);
    if (ifMatch) url += "&ifMatch=" + encodeURIComponent(ifMatch);
    const headers = { "x-orch-token": posture.token };
    if (method !== "GET") {
        headers["x-touch-write"] = "1";
        headers.origin = new URL(posture.base).origin;
    }
    if (payload) headers["content-type"] = "application/json";
    const res = await fetch(url, { method: method, headers: headers,
        body: payload ? JSON.stringify(payload) : undefined });
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    return { status: res.status, body: body };
}

function type(page, text) {
    page.byId.edText.value = text;
    page.byId.edText.fire("input");
}

(async () => {
    // ---- the write plane ON -------------------------------------------------
    const P = makePage(CFG.on);
    await waitFor(() => P.byId.memRoot.textContent === CFG.root,
                  "the page boots against the live server and names its real memory root");
    ok("the page boots against the live server and names its real memory root",
       P.byId.memRoot.textContent === CFG.root, P.byId.memRoot.textContent);
    const first = P.requests[0] || {};
    ok("the boot list is the tokened list route",
       first.path === "/api/memory/list"
       && first.search.indexOf("token=" + CFG.on.token) !== -1,
       JSON.stringify(first.path) + " " + JSON.stringify(first.search));
    const caps = P.byId.memCaps.textContent;
    ok("the caps on screen are the caps the server enforces",
       caps.indexOf("at most " + CFG.limits.maxFiles + " files") !== -1
       && caps.indexOf(Math.round(CFG.limits.maxBytes / 1024) + " KB per file") !== -1
       && P.byId.memTiers.textContent.indexOf("first " + CFG.limits.indexLines
                                              + " lines") !== -1,
       caps);
    ok("the aligned banner is the server's own answer",
       P.byId.memAligned.textContent.indexOf("aligned — this is the directory") === 0,
       P.byId.memAligned.textContent);
    // Counted against the SERVER's own answer rather than against this file's
    // seed list: the directory is shared with the parity checks above, and a row
    // count pinned to a hard-coded 3 would fail for a reason that is about the
    // fixture instead of about the page.
    const onList = await rawList(CFG.on);
    ok("every seeded file gets a row",
       P.byId.memRows.children.length === onList.files.length
       && CFG.seeded.every((n) => P.byId.memRows.textContent.indexOf(n) !== -1),
       P.byId.memRows.children.length + "/" + onList.files.length + " "
       + P.byId.memRows.textContent.slice(0, 160));

    // ---- a load, a save, and the sha the page has to adopt ------------------
    await P.sandbox.memOpen("note.md");
    ok("a load fills the editor with the bytes that are on disk",
       P.byId.edText.value === CFG.seed["note.md"]
       && P.byId.edName.textContent === "note.md",
       JSON.stringify(P.byId.edText.value));
    const edited = "note one\nedited from the dashboard\n";
    type(P, edited);
    const savedOnce = await P.sandbox.memSave();
    const onDisk = await raw(CFG.on, "GET", "note.md");
    ok("a save puts the operator's buffer on disk",
       savedOnce === true && onDisk.status === 200 && onDisk.body.content === edited,
       savedOnce + " " + JSON.stringify(onDisk.body && onDisk.body.content));
    ok("the badge reads saved with the server's own mtime",
       P.byId.edState.textContent.indexOf("saved · ") === 0,
       P.byId.edState.textContent);
    // The adoption contract: without it the next save 409s against the operator's
    // OWN previous write, which is the failure that trains people to click
    // "overwrite" (UI-3). Only a real server can prove it — the sha it returns has
    // to be the sha it will accept.
    type(P, edited + "and again\n");
    const savedTwice = await P.sandbox.memSave();
    ok("a second save does not conflict with the first",
       savedTwice === true && P.byId.edState.textContent.indexOf("saved · ") === 0
       && P.byId.edConflict.hidden === true,
       savedTwice + " " + P.byId.edState.textContent);

    // ---- the server rewrites the bytes it stores (frontmatter) --------------
    // `modified:` is stamped server-side, so the response's sha describes bytes
    // the page never had. It adopts the sha anyway — and must, or every save to
    // every file with frontmatter would conflict with itself on the next click.
    await P.sandbox.memOpen("front.md");
    type(P, "---\ntitle: t\n---\n\nbody two\n");
    const frontOnce = await P.sandbox.memSave();
    type(P, "---\ntitle: t\n---\n\nbody three\n");
    const frontTwice = await P.sandbox.memSave();
    const stamped = await raw(CFG.on, "GET", "front.md");
    const stampCount = (String(stamped.body && stamped.body.content || "")
        .match(/^modified:/gm) || []).length;
    ok("the server stamps modified: and the page still saves twice",
       frontOnce === true && frontTwice === true && stampCount === 1
       && P.byId.edState.textContent.indexOf("saved · ") === 0,
       frontOnce + "/" + frontTwice + " stamps=" + stampCount + " "
       + P.byId.edState.textContent);

    // ---- a REAL 409, from a real second writer ------------------------------
    await P.sandbox.memOpen("note.md");
    const current = await raw(CFG.on, "GET", "note.md");
    await raw(CFG.on, "PUT", "note.md",
              { content: "another writer got here first\n" },
              current.body.sha256);
    const mine = "my precious edit\n";
    type(P, mine);
    const refused = await P.sandbox.memSave();
    ok("a real 409 leaves the buffer alone and offers three exits",
       refused === false && P.byId.edText.value === mine
       && P.byId.edConflict.hidden === false
       && P.byId.edState.textContent.indexOf("conflict") === 0
       && P.byId.cfOverwrite.textContent === "overwrite with mine"
       && P.byId.cfText.textContent.indexOf("on disk now:") === 0,
       refused + " " + P.byId.edState.textContent);
    P.byId.cfOverwrite.fire("click");
    await waitFor(() => P.byId.edState.textContent.indexOf("saved · ") === 0,
                  "overwrite re-sends with the sha the server published, and lands");
    const overwritten = await raw(CFG.on, "GET", "note.md");
    ok("overwrite re-sends with the sha the server published, and lands",
       overwritten.body && overwritten.body.content === mine
       && P.byId.edConflict.hidden === true,
       JSON.stringify(overwritten.body && overwritten.body.content));

    // ---- a REAL content refusal --------------------------------------------
    type(P, "see @/etc/passwd for the key\n");
    const hygiene = await P.sandbox.memSave();
    const untouched = await raw(CFG.on, "GET", "note.md");
    ok("a refused @-import is named on screen and nothing was written",
       hygiene === false
       && P.byId.edState.textContent.indexOf("error — ") === 0
       && P.byId.edReason.textContent.indexOf("@-import") !== -1
       && P.byId.edText.value === "see @/etc/passwd for the key\n"
       && untouched.body.content === mine,
       P.byId.edState.textContent + " || " + P.byId.edReason.textContent);

    // ---- the `pinned:` confirmation, both halves --------------------------
    await P.sandbox.memOpen("note.md");
    type(P, "---\npinned: true\n---\n\nkeep me in every session\n");
    const pinRefused = await P.sandbox.memSave();
    ok("a pinned: save is refused until the checkbox says yes in words",
       pinRefused === false
       && P.byId.edState.textContent.indexOf("error — ") === 0
       && P.byId.edReason.textContent.indexOf("pinned") !== -1
       && P.byId.edPinWrap.hidden === false,
       P.byId.edState.textContent + " || " + P.byId.edReason.textContent);
    P.byId.edPin.checked = true;
    P.byId.edPin.fire("change");
    const pinSaved = await P.sandbox.memSave();
    const pinnedDisk = await raw(CFG.on, "GET", "note.md");
    ok("...and then it saves, with the flag the server requires",
       pinSaved === true
       && String(pinnedDisk.body && pinnedDisk.body.content).indexOf("pinned: true") !== -1,
       pinSaved + " " + JSON.stringify(pinnedDisk.body && pinnedDisk.body.content));

    // ---- create, delete, restore ------------------------------------------
    P.byId.newName.value = "fresh-note.md";
    P.byId.newBtn.fire("click");
    await waitFor(() => P.byId.edName.textContent === "fresh-note.md",
                  "create posts a new file into the memory root");
    const created = await raw(CFG.on, "GET", "fresh-note.md");
    ok("create posts a new file into the memory root",
       created.status === 200 && P.byId.newErr.hidden === true,
       created.status + " " + JSON.stringify(created.body));
    P.answers.confirm = true;               // the empty-file delete asks once
    P.byId.edDelete.fire("click");
    await waitFor(() => P.byId.trashCard.hidden === false,
                  "delete moves the file into the server's own trash directory");
    const deleted = await raw(CFG.on, "GET", "fresh-note.md");
    ok("delete moves the file into the server's own trash directory",
       deleted.status === 404
       && P.byId.trashRows.textContent.indexOf(".trash") !== -1,
       deleted.status + " || " + P.byId.trashRows.textContent.slice(0, 200));
    const restored = await P.sandbox.memRestore("fresh-note.md");
    const back = await raw(CFG.on, "GET", "fresh-note.md");
    ok("restore re-creates the file from the bytes the server removed",
       restored === true && back.status === 200
       && P.byId.trashCard.hidden === true,
       restored + " " + back.status);

    // ---- the poll is the list's, never the editor's ------------------------
    await P.sandbox.memOpen("note.md");
    const kept = "an edit the poll must not touch\n";
    type(P, kept);
    await P.intervals[0]();                 // the 12 s cadence, fired by hand
    ok("the poll does not clobber a dirty buffer",
       P.byId.edText.value === kept
       && P.byId.edState.textContent === "unsaved changes",
       JSON.stringify(P.byId.edText.value) + " " + P.byId.edState.textContent);

    // ---- the DEFAULT posture: the write plane off --------------------------
    const Q = makePage(CFG.off);
    // Waiting on the ROWS, not on the posture line: `memBoot` renders the head
    // before it has an answer, so "write plane: OFF" is on screen from the first
    // paint (the honest default). Rows exist only after a list has landed, which
    // is what makes the assertions below statements about the server's answer
    // rather than about the page's initial state.
    await waitFor(() => Q.byId.memRows.children.length > 0,
                  "the write plane being off is read from the server, not assumed");
    const offList = await rawList(CFG.off);
    ok("the write plane being off is read from the server, not assumed",
       Q.byId.memWrite.textContent.indexOf("write plane: OFF") === 0
       && offList.memoryWrite === false
       && Q.byId.memRows.children.length === offList.files.length,
       Q.byId.memWrite.textContent.slice(0, 120) + " rows="
       + Q.byId.memRows.children.length + "/" + offList.files.length);
    await Q.sandbox.memOpen("note.md");
    ok("save is disabled with the server's own reason",
       Q.byId.edSave.disabled === true && Q.byId.edDelete.disabled === true
       && Q.byId.edSave.title.indexOf("--allow-memory-write") !== -1
       && Q.byId.memRows.textContent.indexOf("the write plane is off") !== -1,
       Q.byId.edSave.disabled + " || " + Q.byId.edSave.title);
    ok("no create affordance renders while the plane is off",
       Q.byId.createRow.hidden === true, Q.byId.createRow.hidden);
    const before = await raw(CFG.off, "GET", "note.md");
    const spent = Q.requests.length;
    const attempted = await Q.sandbox.memSave();
    const after = await raw(CFG.off, "GET", "note.md");
    ok("a save attempt over an off write plane sends nothing at all",
       attempted === false && Q.requests.length === spent
       && after.body.sha256 === before.body.sha256,
       attempted + " requests " + spent + "->" + Q.requests.length);

    process.exit(failed ? 1 : 0);
})().catch((e) => {
    console.log("FAIL: the harness itself threw — " + (e && e.stack || e));
    process.exit(2);
});
"""


class _Servers:
    """Both postures, served on ephemeral loopback ports by one background loop.

    A thread rather than a subprocess, so the modules under test are the ones this
    file imported (their `TOKEN`, their `MEMORY_ROOT`, their booted `MEMORY_WRITE`)
    and nothing has to be re-resolved from an environment a child would inherit.
    """

    def __init__(self, mods):
        self.mods = mods
        self.loop = asyncio.new_event_loop()
        self.ports = {}
        self._servers = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def __enter__(self):
        self.thread.start()
        for label, mod in self.mods.items():
            future = asyncio.run_coroutine_threadsafe(
                asyncio.start_server(mod.handle, "127.0.0.1", 0), self.loop)
            server = future.result(10)
            self._servers.append(server)
            self.ports[label] = server.sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc):
        async def stop():
            for server in self._servers:
                server.close()
                await server.wait_closed()
        try:
            asyncio.run_coroutine_threadsafe(stop(), self.loop).result(10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
        return False


def test_memory_html_drives_the_live_server():
    """UI-10: the guard that EXECUTES the page — against the real thing.

    Skips loudly without node: the parity assertions above still hold the seam,
    but nothing in memory.html has been executed, and that is worth saying out
    loud rather than counting as coverage.
    """
    if not HAS_PAGE:
        _skip("memory.html is not in this tree, so nothing was executed against "
              "the live server (the memory page's own sub-plan owns it)")
        return
    print("  memory.html: driving the page under node + vm, against a live server")
    node = shutil.which("node") or ("/usr/bin/node"
                                    if os.path.exists("/usr/bin/node") else None)
    if node is None:
        SKIPS.append("no node: memory.html was not executed against the live server")
        print("  skip: no node on PATH — the parity checks above still apply, but "
              "NOTHING in memory.html has been executed")
        return
    ids = sorted(set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', PAGE)))
    assert "edText" in ids and "memRows" in ids, \
        "the harness needs the page's own id set — this one looks wrong"

    with _Servers({"on": MS, "off": MS_OFF}) as servers, \
            tempfile.TemporaryDirectory() as tmp:
        config = {
            "html": MEMORY_HTML,
            "ids": ids,
            "root": ROOT,
            "seed": SEED,
            "seeded": sorted(SEED),
            "limits": {"maxBytes": MS.MAX_MEMORY_BYTES,
                       "maxFiles": MS.MAX_MEMORY_FILES,
                       "indexLines": MS.MEM_INDEX_LINES,
                       "indexBytes": MS.MEM_INDEX_BYTES},
            "on": {"base": f"http://127.0.0.1:{servers.ports['on']}",
                   "token": MS.TOKEN},
            "off": {"base": f"http://127.0.0.1:{servers.ports['off']}",
                    "token": MS_OFF.TOKEN},
        }
        driver = os.path.join(tmp, "drive-memory-live.js")
        with open(driver, "w", encoding="utf-8") as handle:
            handle.write(DRIVER_JS)
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        proc = subprocess.run([node, driver, config_path], capture_output=True,
                              text=True, timeout=240)

    passed, fails = set(), []
    for line in proc.stdout.splitlines():
        if line.startswith("PASS: "):
            passed.add(line[6:])
        elif line.startswith("FAIL: "):
            fails.append(line[6:])
    assert not fails, "UI-10 (live): " + " || ".join(fails)
    missing = [label for label in EXPECTED if label not in passed]
    assert not missing, ("UI-10 (live): these assertions never ran — "
                         + " || ".join(missing)
                         + (("\n" + proc.stderr.strip()[-2000:])
                            if proc.stderr.strip() else ""))
    assert proc.returncode == 0, \
        (f"the harness must run to completion — rc {proc.returncode} "
         f"{proc.stderr.strip().splitlines()[:3]}")


# --------------------------------------------------------------------------

def run_all():
    """Definition order (the parity checks first, then the driven pass)."""
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
