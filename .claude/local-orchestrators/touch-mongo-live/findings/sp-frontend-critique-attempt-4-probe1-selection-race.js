"use strict";
/* A fake DOM + fake socket + fake fetch, driving touch-visual/app.js for real.
 * Prints one `PASS: <label>` / `FAIL: <label> — <detail>` per assertion. */

const fs = require("fs");
const vm = require("vm");

const APP = process.argv[2];

let failed = 0;
function ok(label, cond, detail) {
    if (cond) {
        console.log("PASS: " + label);
    } else {
        failed += 1;
        console.log("FAIL: " + label + " — " + String(detail === undefined ? "" : detail));
    }
}

// --- DOM ------------------------------------------------------------------

class TextNode {
    constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null; }
    get textContent() { return this.data; }
    set textContent(v) { this.data = String(v); }
}

/* Every row is ROW_PX tall and every scroll box shows BOX_PX of them, so a
 * list overflows at 11 rows and `scrollHeight` is a real function of the
 * content. That is enough to drive the pin-to-bottom rule for real: a page
 * whose live edge is off-screen by default is the failure being guarded, and
 * no source guard can see it. */
const ROW_PX = 10;
const BOX_PX = 100;

class Element {
    constructor(tag) {
        this.nodeType = 1;
        this.tagName = String(tag).toUpperCase();
        this.childNodes = [];
        this.attributes = {};
        this.listeners = {};
        this.parentNode = null;
        this.hidden = false;
        this.disabled = false;
        this.id = "";
        this._class = "";
        this.appends = 0;
        this.removes = 0;
        this.scrollTop = 0;
        this.clientHeight = BOX_PX;
        const self = this;
        this.classList = {
            add(name) { const s = self._set(); s.add(name); self._class = [...s].join(" "); },
            remove(name) { const s = self._set(); s.delete(name); self._class = [...s].join(" "); },
            contains(name) { return self._set().has(name); },
        };
    }
    _set() { return new Set(String(this._class).split(/\s+/).filter(Boolean)); }
    get className() { return this._class; }
    set className(v) { this._class = String(v); }
    get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
    get childElementCount() { return this.children.length; }
    get firstChild() { return this.childNodes[0] || null; }
    get scrollHeight() { return this.children.length * ROW_PX; }
    appendChild(node) {
        if (node instanceof Fragment) {
            node.childNodes.slice().forEach((kid) => this.appendChild(kid));
            node.childNodes = [];
            return node;
        }
        if (node.parentNode) node.parentNode.removeChild(node);
        node.parentNode = this;
        this.childNodes.push(node);
        this.appends += 1;
        return node;
    }
    insertBefore(node, ref) {
        if (node instanceof Fragment) {
            const kids = node.childNodes.slice();
            node.childNodes = [];
            kids.forEach((kid) => this.insertBefore(kid, ref));
            return node;
        }
        if (node.parentNode) node.parentNode.removeChild(node);
        const at = ref ? this.childNodes.indexOf(ref) : -1;
        node.parentNode = this;
        if (at === -1) this.childNodes.push(node);
        else this.childNodes.splice(at, 0, node);
        this.appends += 1;
        return node;
    }
    removeChild(node) {
        const at = this.childNodes.indexOf(node);
        if (at !== -1) { this.childNodes.splice(at, 1); node.parentNode = null; this.removes += 1; }
        return node;
    }
    setAttribute(name, value) { this.attributes[String(name)] = String(value); }
    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name)
            ? this.attributes[name] : null;
    }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    fire(type) { (this.listeners[type] || []).forEach((fn) => fn({ type: type })); }
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

class Fragment extends Element {
    constructor() { super("#fragment"); }
}

const ELEMENT_IDS = ["conn", "connText", "modeChip", "mirrorChip", "reducerChip", "rollup",
                     "notice", "sessionList", "runList", "taskList", "sessionCount",
                     "runCount", "taskCount", "detailHead", "detailStatus", "detailBody",
                     "log", "older", "logMeta", "olderBtn"];
const byId = {};
ELEMENT_IDS.forEach((id) => {
    const node = new Element(id === "log" || id === "older" ? "ol" : "div");
    node.id = id;
    byId[id] = node;
});
byId.older.hidden = true;
byId.olderBtn.hidden = true;
byId.notice.hidden = true;

const meta = new Element("meta");
meta.setAttribute("content", "test-token");

const document = {
    readyState: "complete",
    body: new Element("body"),
    createElement: (tag) => new Element(tag),
    createTextNode: (v) => new TextNode(v),
    createDocumentFragment: () => new Fragment(),
    getElementById: (id) => byId[id] || null,
    querySelector: (sel) => (String(sel).indexOf("touch-token") !== -1 ? meta : null),
    addEventListener: () => {},
};

// --- timers, socket, fetch ------------------------------------------------

const timers = [];
function later(fn) { timers.push(fn); return timers.length; }
function drain() {
    let guard = 0;
    while (timers.length && guard < 5000) { timers.shift()(); guard += 1; }
}

/* Intervals are REGISTERED rather than dropped on the floor, and fired only
 * when the driver says so.
 *
 * `setInterval: () => 0` left `resync` — half of R-55's resume mechanism, the
 * half the plan calls "a package" with the absolute tokens — asserted by
 * source text alone. Firing them on a timer would make the run
 * non-deterministic; firing them on demand is what a test wants. */
const intervals = [];
function everyTick(fn) { intervals.push(fn); return intervals.length; }
function tickIntervals() { intervals.slice().forEach((fn) => fn()); }
function stopInterval(id) {
    if (id >= 1 && id <= intervals.length) intervals[id - 1] = () => {};
}

const sockets = [];
function FakeWS(url) {
    this.url = String(url);
    this.readyState = 1;
    this.sent = [];
    sockets.push(this);
}
FakeWS.OPEN = 1;
FakeWS.prototype.close = function () { this.readyState = 3; };
FakeWS.prototype.send = function (data) { this.sent.push(data); };

let runGraph = { runId: "wf_a", observed: {}, nodes: [], agents: [] };
/** The session the timeline arm pretends to hold, in records. */
const SESSION_RECORDS = 300;
/** Flipped on to make the backwards-paging route fail for one click. */
let eventsFail = false;
const fetched = [];
const fetchedUrls = [];
function fakeFetch(url) {
    const u = new URL(String(url));
    fetched.push(u.pathname);
    fetchedUrls.push(u.pathname + "?" + u.searchParams.toString());
    if (u.pathname === "/api/events" && eventsFail) {
        return Promise.resolve({
            ok: false, status: 503,
            json: () => Promise.resolve({ message: "the store is unreadable" }),
        });
    }
    let body = {};
    if (u.pathname === "/api/sessions") body = { sessions: [] };
    else if (u.pathname === "/api/tasks") body = { tasks: [] };
    else if (u.pathname === "/health") body = { mirror: { state: "absent" } };
    else if (u.pathname === "/api/run/graph") body = runGraph;
    else if (u.pathname === "/api/session/timeline") {
        // A real window: `limit` records of a session that holds more, with
        // the server's own `hasMore` and its `(lineNo, _id)` cursor pair.
        const limit = Number(u.searchParams.get("limit")) || 0;
        const count = Math.min(limit, SESSION_RECORDS);
        const records = [];
        for (let n = 1; n <= count; n += 1) {
            records.push({ lineNo: n, _id: "r" + n, type: "user",
                           ts: "2026-07-26T00:00:00.000Z" });
        }
        body = { session: u.searchParams.get("session"),
                 sessionDoc: { id: u.searchParams.get("session"), kind: "historical" },
                 records: records, count: count,
                 nextSince: count, nextSinceId: "r" + count,
                 hasMore: SESSION_RECORDS > count };
    } else if (u.pathname === "/api/events") {
        const before = Number(u.searchParams.get("before"));
        const limit = Number(u.searchParams.get("limit"));
        const start = Math.max(0, before - limit);
        const records = [];
        for (let seq = start; seq < before; seq += 1) {
            records.push({ seq: seq, kind: "event", ts: "2026-07-26T00:00:00.000Z",
                           provenance: "harness", ref: { agent: "old" },
                           data: { note: "history" } });
        }
        body = { stream: u.searchParams.get("stream"), records: records,
                 count: records.length, hasOlder: start > 0,
                 oldest: records.length ? records[0].seq : null };
    }
    return Promise.resolve({
        ok: true, status: 200, json: () => Promise.resolve(body),
    });
}

const window = {
    TOUCH_TOKEN: "",
    location: {
        href: "http://127.0.0.1:8930/", search: "", hash: "", protocol: "http:",
        replace(value) { window.location.hash = String(value); },
    },
    setTimeout: (fn) => later(fn),
    clearTimeout: () => {},
    setInterval: (fn) => everyTick(fn),
    clearInterval: (id) => stopInterval(id),
    requestAnimationFrame: (fn) => later(fn),
    addEventListener: () => {},
};

const sandbox = {
    document: document, window: window, WebSocket: FakeWS, fetch: fakeFetch,
    URL: URL, URLSearchParams: URLSearchParams, console: console,
    Math: Math, JSON: JSON, Date: Date, Number: Number, String: String,
    Object: Object, Array: Array, Set: Set, Map: Map, Promise: Promise, Error: Error,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(APP, "utf8"), sandbox, { filename: APP });

// --- driving --------------------------------------------------------------

async function settle() {
    for (let round = 0; round < 12; round += 1) {
        drain();
        await new Promise((resolve) => setImmediate(resolve));
    }
    drain();
}

function send(sock, frame) {
    if (sock.onmessage) sock.onmessage({ data: JSON.stringify(frame) });
}

function eventFrame(stream, seq, live, record) {
    return { type: "event", live: live, stream: stream, seq: seq,
             cursor: stream + "#" + String(seq).padStart(12, "0"),
             record: record || { kind: "event", ts: "2026-07-26T01:00:00.000Z",
                                 provenance: "harness", ref: { agent: "a" },
                                 data: { note: "n" } } };
}

function rowsOf(node) { return node.children.length; }

function findLists(node, out) {
    node.children.forEach((kid) => {
        if (kid.tagName === "UL") out.push(kid);
        findLists(kid, out);
    });
    return out;
}

function findTags(node, tag, out) {
    node.children.forEach((kid) => {
        if (kid.tagName === tag) out.push(kid);
        findTags(kid, tag, out);
    });
    return out;
}

/** Any anchor with an href anywhere under `node` — i.e. "is this row clickable". */
function hasLink(node) {
    return findTags(node, "A", []).some((a) => a.getAttribute("href") !== null);
}

function rowNamed(list, needle) {
    return list.children.filter((row) => row.textContent.indexOf(needle) !== -1)[0] || null;
}

/** At the bottom of its own scroll box, by the same rule the page uses. */
function atEdge(node) {
    return node.scrollTop + node.clientHeight >= node.scrollHeight - 40;
}


// ---- probe 1: a load-older page that lands AFTER the selection moved -------
let deferred = null;
const realFetch = fakeFetch;
function slowFetch(url) {
    const u = new URL(String(url));
    if (u.pathname === "/api/events") {
        return new Promise((resolve) => {
            deferred = () => resolve(realFetch(url));
        });
    }
    return realFetch(url);
}

(async () => {
    const sock = sockets[0];
    sock.onopen();
    send(sock, { type: "hello", live: false, mode: "replay",
                 streams: ["run:wf_a", "run:wf_b"], currentRun: "run:wf_a",
                 window: 500, reducerVersion: "3", cursors: {} });
    send(sock, { type: "mode", live: true, mode: "tail",
                 cursors: { "run:wf_a": 2 }, oldest: {}, truncated: {} });
    send(sock, { type: "anchors", live: true, stream: "run:wf_a", oldest: 1000, truncated: true });
    send(sock, { type: "anchors", live: true, stream: "run:wf_b", oldest: 2000, truncated: true });
    window.location.hash = "#run/wf_a";
    sandbox.route();
    await settle();
    console.log("selected wf_a; btn=" + byId.olderBtn.textContent +
                " hidden=" + byId.olderBtn.hidden);

    sandbox.fetch = slowFetch;
    byId.olderBtn.fire("click");
    await settle();
    console.log("after click, fetch in flight: older rows=" + byId.older.children.length +
                " deferred=" + (deferred !== null));

    // the operator changes their mind mid-fetch
    window.location.hash = "#run/wf_b";
    sandbox.route();
    await settle();
    console.log("switched to wf_b: older rows=" + byId.older.children.length +
                " hidden=" + byId.older.hidden +
                " btn=" + byId.olderBtn.textContent);

    // the wf_a page now lands
    deferred();
    await settle();
    const rows = byId.older.children;
    console.log("AFTER the wf_a page landed:");
    console.log("  older rows      = " + rows.length + "  hidden=" + byId.older.hidden);
    console.log("  first row text  = " + (rows[0] ? rows[0].textContent : "-"));
    console.log("  aria-label      = " + byId.older.getAttribute("aria-label"));
    console.log("  button          = " + byId.olderBtn.textContent);
    console.log("  button title    = " + byId.olderBtn.getAttribute("title"));
    console.log("  logMeta         = " + byId.logMeta.textContent);
    const wrong = rows.filter((r) => r.textContent.indexOf("run:wf_a") !== -1).length;
    console.log("  rows belonging to wf_a while wf_b is selected = " + wrong);
    process.exit(0);
})().catch((e) => { console.log("PROBE ERROR " + e.stack); process.exit(2); });
