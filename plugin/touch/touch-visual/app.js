/* touch-visual/app.js — the v0 page's wire and render (R-22, R-32, R-55).
 *
 * What this file is NOT
 * ---------------------
 * **It never derives state** (GD-23 / R-54). There is exactly one reducer and
 * it runs server-side; `/api/*` and `/ws` serve its output, and this file
 * copies that output into text nodes. So there is no idle threshold here, no
 * "what time is it now" (`Date.now()` and a bare `new Date()` are both absent,
 * asserted by `tests/test_touch_frontend.py`), no verdict arithmetic and no
 * freeze-to-stale rule — monitor.html had one and R-54 moved it INTO the
 * reducer precisely so the page and the API cannot disagree. Every badge word
 * on the screen is a string the server computed: `derived.state`,
 * `derived.label`, `plan.badge`, `plan.label`, `node.state`.
 *
 * **It never differences a token counter.** The store's token records are
 * absolute four-key documents (`in`, `out`, `cached`, `cache_write`) and the
 * socket carries them verbatim (GD-25/R-55). A rollup here is therefore a
 * *sum over the latest absolute record per ref* — never an accumulation of
 * deltas, which is the model a bounded replay silently under-counts. The two
 * halves ("resume from a cursor" and "absolute tokens") are a package.
 *
 * **It never builds markup from data.** Escape-first (GD-20) is structural in
 * this file: the only text sink is a text node (`textContent` /
 * `createTextNode`), the only attribute sink is `setAttribute`, and every CSS
 * class comes from a literal whitelist keyed by the server's vocabulary. There
 * is no `innerHTML`, no `insertAdjacentHTML`, no `document.write`, no template
 * interpolated into a DOM parser — so an agent-authored session name, plan
 * detail or artifact path cannot become an element no matter what it contains.
 *
 * **It renders no control affordance** (R-32/D13). v0 observes; the verb
 * ladder is a later phase, `CONTROL_ROUTES` is empty server-side, and the page
 * says so in its footer rather than leaving a gap.
 *
 * **It reads no artifact route.** `/api/artifacts` and `/file` exist on the
 * server and are not in R-32's v0 scope, so nothing here fetches them and no
 * preview renderer exists on this side yet. `aggregator/server.py`'s note that
 * "the page renders the preview with its own escape-first mini renderer"
 * describes the phase that adds them, not this file — recorded here because a
 * docstring about a counterparty that does not exist is how drift starts.
 *
 * The wire contract (R-55), restated verbatim from `aggregator/server.py`
 * -----------------------------------------------------------------------
 * Frames are JSON text messages. Every one carries `live`:
 *
 *     {"type":"hello","live":false,"mode":"replay","streams":[...],
 *      "currentRun":"run:wf_x","window":500,"reducerVersion":"1",
 *      "cursors":{"<stream>":<seq>},"resumed":true,
 *      "from":12,"fromApplied":false,"fromRejected":null,
 *      "cursorsRejected":[...],"streamsRejected":[...],
 *      "streamsUnobserved":[...]}
 *     {"type":"event","live":false,"stream":"run:wf_x","seq":12,
 *      "cursor":"run:wf_x#000000000012","record":{...}}
 *     {"type":"mode","live":true,"mode":"tail",         <- the ONE boundary
 *      "cursors":{"<stream>":<seq>},"oldest":{"<stream>":<seq>},
 *      "truncated":{"<stream>":true}}
 *     {"type":"event","live":true, ...}
 *     {"type":"anchors","live":true,"stream":"<stream>",
 *      "oldest":<seq>,"truncated":true}      <- a backfill AFTER the boundary
 *     {"type":"subscribed","live":true,"cursors":{"<stream>":<seq>},
 *      "accepted":{"<stream>":<seq>},"rejected":[...],
 *      "backfilled":{"<stream>":<count>}}    <- follows the frames it re-sent
 *     {"type":"tick","live":true,"ts":"…Z"}            <- idle keepalive marker
 *
 * Consequences this file implements rather than re-invents:
 *
 * * `live:false` frames **paint once, with no animation**. The animation class
 *   is attached in exactly one function, which requires `live === true`.
 * * the load-older anchors live on `mode` and on post-boundary `anchors`
 *   frames, never on `hello` (hello could only publish `{}`), so the button is
 *   revealed by those two frames alone. Loaded history gets its own list and
 *   its own budget (`#older` / `OLDER_MAX`) because a stream is only ever
 *   `truncated` once the live log is already full — sharing one cap made the
 *   affordance structurally dead. That budget, that list and those anchors are
 *   all scoped to ONE stream and ONE connection: a selection change empties the
 *   history, and a reconnect drops the anchors, because both describe a window
 *   that no longer exists.
 * * **a frame may be the first mention of a stream.** `hello.streams` is one
 *   instant; the server re-evaluates its set every tick and backfills a run
 *   that started afterwards. `noteStream` learns those, so the sidebar cannot
 *   list fewer runs than the log is already printing.
 * * **the resume cursor is the server's, not ours.** A token record held by
 *   the ≥1 s coalescer keeps the published cursor *behind* records that have
 *   already gone out, so "max seq I received" is exactly the position that
 *   skips it forever on reconnect. We track what we received (for de-dup) and
 *   resume from what the server published (`mode.cursors`, then the
 *   `subscribed` ack, which we re-ask for on a timer while the tail is live).
 */

"use strict";

// ---------------------------------------------------------------- 0. limits

/** Capped log, from day one (GD-20 do-not-inherit: the monitor's unbounded one). */
const LOG_MAX = 400;
/** De-dup ring: `(stream, seq)` keys already applied. Bounded like everything. */
const SEEN_MAX = 20000;
/**
 * Rollup entries kept, FIFO — one per `(stream, ref)`.
 *
 * The thesis of this file is "capped from day one", and the token map was the
 * one collection that grew for the life of the tab: a ref per agent per stream
 * the socket ever mentions, never released. It is capped by the same rule as
 * everything else, and what the cap threw away is *named* beside the sum
 * (`rollupTitle`) rather than quietly subtracted from it.
 */
const TOKENS_MAX = 4000;
/** Coalesced render: at most one paint per animation frame, never one per frame. */
const RENDER_DEBOUNCE_MS = 120;
/** Model refetch throttle — the reducer's output is pulled, not recomputed. */
const REFRESH_MS = 2000;
/**
 * `/api/tasks` poll, on its OWN slow cadence.
 *
 * That route is the most expensive one this server has: it re-reads and
 * re-reduces every legacy folder's `events.jsonl` from disk on each call
 * (megabytes, in the same process that serves `/ws`). Legacy task
 * folders are *history* — they do not change at the live cadence — so pulling
 * them at `REFRESH_MS` alongside the run data would spend the socket's process
 * time re-parsing files that did not move.
 *
 * This constant is a client-side workaround for a server-side cost, and it is
 * recorded as such: the fix is for `server.h_tasks` to keep the reduction
 * behind the same `(st_mtime_ns, st_size)` key
 * `shared/monitoring/monitor_server.py` already uses for `/tasks`
 * (63 ms cold, 1.3 ms warm there; ~250-400 ms per call here). Deliberately NOT
 * done in this pass — one caching story for the same files, decided once, is
 * the point (PRIOR-ART-TOUCH-5); a second improvised one is how the next
 * divergence starts. Lowering `TASKS_MS` before that lands only moves the cost.
 */
const TASKS_MS = 30000;
/** `/health` poll. Slow on purpose: it is a label, not a liveness path (GD-22). */
const HEALTH_MS = 10000;
/** Re-ask the socket for its authoritative cursors while the tail is live. */
const RESYNC_MS = 15000;
/** Reconnect backoff, in fixed steps (no clock arithmetic anywhere in this file). */
const RECONNECT_MS = [500, 1000, 2000, 5000, 10000];
/** One page of `/api/events?before=` when walking a truncation backwards. */
const OLDER_PAGE = 200;
/**
 * How many rows of loaded history the page holds — in its OWN list, `#older`.
 *
 * The live tail and the loaded history are two budgets, not one. While they
 * shared `LOG_MAX` the affordance was structurally dead: a stream is only ever
 * `truncated` once the server cut a replay at its window (500 records), by
 * which point the log has been pinned at its 400-row cap for a hundred frames
 * and never leaves it — so "the room left under `LOG_MAX`" was always zero,
 * every click fetched a page it then discarded, and the fetch cost a
 * whole-stream `read_all` in the process that also serves `/ws`.
 *
 * History therefore lives above the tail with `OLDER_MAX` rows of its own,
 * `flushLog` keeps owning `#log` alone, and "the live tail is never evicted"
 * is literally true instead of being achieved by refusing to paint. When the
 * budget is spent the button says so and stops fetching (`olderRoom`).
 */
const OLDER_MAX = 400;
/**
 * Distance from the bottom, in CSS pixels, still counted as "at the live edge".
 *
 * The live tail scrolls (`.log { max-height: 46vh }`, ~17 rows) and a burst
 * overflows it inside the first replay, so without this the newest row — the
 * one `live:true` and `.fresh` exist to point at — is painted permanently
 * below the fold and the operator watches a frozen window of the OLDEST rows
 * in the buffer. `flushLog` therefore follows the newest row, and stops
 * following the moment the operator scrolls away by more than this slack.
 * (`monitor.html` solves the same problem the same way; GD-20 says inherit the
 * substrate where it was right, and "the newest row is visible" is not on the
 * do-not-inherit list.)
 */
const PIN_SLACK_PX = 40;
/** Bounded session timeline page (the corpus holds an 872 KB line — no bodies). */
const TIMELINE_PAGE = 120;
/**
 * How much of a session's timeline the panel will hold, in records.
 *
 * The panel shows a *window* over the session, grown a page at a time by the
 * button under the list, and this is where growing stops. It must stay at or
 * below the server's `MAX_PAGE` (1000): `positive_int` **clamps** an oversized
 * `limit` rather than refusing it, so a window wider than that would ask for
 * rows the server silently declines to send and the button would never reach
 * its end.
 */
const TIMELINE_MAX = 960;
/** Longest `data`/detail string that reaches a text node. */
const DETAIL_CHARS = 200;

/** The animation class. Attached in `paint()` and nowhere else (R-55). */
const LIVE_CLASS = "fresh";

/** The four absolute token keys (GD-11). Order is the display order. */
const TOKEN_KEYS = ["in", "out", "cached", "cache_write"];

/* Class whitelists. A class is looked up *by* a server value, never built from
 * one: `className = "node " + doc.state` is the shape that turns a hostile or
 * simply unexpected string into a selector, and it is the shape monitor.html's
 * FRONTEND-1 regression was. An unknown value lands on `st-other` and still
 * shows its own text. */

/** `agents.NODE_STATES` — the reducer's whole liveness vocabulary. `failed` is
 *  deliberately not in it: R-58 exists to end that badge being fabricated. */
const NODE_STATE_CLASS = {
    running: "st-running",
    done: "st-done",
    unknown: "st-unknown",
};

/** `legacy.STATES` + `legacy.DERIVED_STATES` (GD-14). A legacy `failed` IS a
 *  real observed verdict and keeps its badge (D13) — it is not derived here. */
const LEGACY_STATE_CLASS = {
    queued: "st-queued",
    running: "st-running",
    done: "st-done",
    failed: "st-failed",
    info: "st-info",
    stale: "st-stale",
    superseded: "st-superseded",
    closed: "st-closed",
};

/** `store.PROVENANCE` (GD-28) + the legacy namespace. Anything but `harness`
 *  renders dashed: "we did not observe this, we concluded it" is a visible
 *  property of the row, not a footnote. */
const PROV_CLASS = {
    harness: "prov-harness",
    derived: "prov-derived",
    asserted: "prov-asserted",
    touch: "prov-touch",
    unknown: "prov-unknown",
    legacy: "prov-legacy",
};

/** `mirror.health().state` (R-45). `absent`/`down` are normal, not failures. */
const MIRROR_CLASS = {
    ok: "st-done",
    live: "st-done",
    degraded: "st-stale",
    down: "st-failed",
    absent: "st-info",
};

/**
 * The socket's own mode word — the ONE place a `st-*` class may still describe
 * something that is not a reducer verdict, because `tail` is not a conclusion
 * about a run, it is a property of this connection.
 *
 * It is a table rather than a ternary so that the whole file obeys one rule:
 * **every `st-*` class is a whitelist value, never a literal at a call site**.
 * A liveness class painted onto something the reducer did not conclude is the
 * GD-23 failure in miniature — that is why the run list's `current` marker
 * (which the server picks by file mtime, explicitly "not a verdict") wears a
 * neutral `chip-current` and not `st-running`.
 */
const MODE_CLASS = {
    tail: "st-running",
    replay: "st-info",
    connecting: "st-info",
    offline: "st-stale",
};

// ------------------------------------------------------- 1. token and fetch

/** The serve-time placeholder. Present ⇒ the page was NOT served by Touch. */
const TOKEN_PLACEHOLDER = "__TOUCH" + "_TOKEN__";

/**
 * The per-boot token (GD-13), from the three carriers, in order of trust.
 *
 * The `<meta>` is `inject_token`'s placeholder arm and is the live one for
 * *this* document, which always ships the placeholder. `window.TOUCH_TOKEN` is
 * `inject_token`'s other arm (a JSON-encoded script tag) and is read here as
 * defence in depth for a document that predates the placeholder — not because
 * anything currently serves the page that way. `?token=` is how an operator
 * first navigates here. The uninjected placeholder is treated as *no token*
 * rather than sent: the literal placeholder in an Authorization header is a
 * 401 with a confusing message, and the honest failure is "this page was not
 * served by the Touch server". (The placeholder is spelled by concatenation
 * below so this file can never be mistaken for an injection target — only the
 * document is one.)
 */
function readToken() {
    const meta = document.querySelector('meta[name="touch-token"]');
    const injected = meta ? meta.getAttribute("content") : "";
    if (injected && injected !== TOKEN_PLACEHOLDER) return injected;
    if (typeof window.TOUCH_TOKEN === "string" && window.TOUCH_TOKEN) {
        return window.TOUCH_TOKEN;
    }
    const fromUrl = new URLSearchParams(window.location.search).get("token");
    return fromUrl || "";
}

const TOKEN = readToken();

/** A same-origin URL with the query built by URLSearchParams (never by hand). */
function apiUrl(path, params) {
    const url = new URL(path, window.location.href);
    Object.keys(params || {}).forEach((name) => {
        const value = params[name];
        if (value !== undefined && value !== null && value !== "") {
            url.searchParams.set(name, String(value));
        }
    });
    return url;
}

/**
 * One authenticated read. Returns the parsed body, or throws with the server's
 * own `message` — every 4xx this server produces names the rule it enforced,
 * and repeating that text is more useful than "request failed".
 */
async function getJson(path, params) {
    const headers = { Accept: "application/json" };
    if (TOKEN) headers["X-Touch-Token"] = TOKEN;
    const response = await fetch(apiUrl(path, params).toString(), {
        headers: headers,
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
    });
    let body = null;
    try {
        body = await response.json();
    } catch (err) {
        body = null;
    }
    if (!response.ok) {
        const message = body && body.message ? body.message : "HTTP " + response.status;
        const error = new Error(message);
        error.status = response.status;
        throw error;
    }
    return body;
}

// ------------------------------------------- 2. DOM helpers (escape-first)

/**
 * The one text sink. `createTextNode` is the browser's own escape: whatever
 * the string contains, it becomes character data and never a tag, which is why
 * this file needs no `esc()` regex and cannot forget to call one.
 */
function text(value) {
    return document.createTextNode(value === undefined || value === null ? "" : String(value));
}

function setText(node, value) {
    if (!node) return;
    const next = value === undefined || value === null ? "" : String(value);
    if (node.textContent !== next) node.textContent = next;
}

function el(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.appendChild(text(value));
    return node;
}

/** A labelled chip. `className` is always caller-literal or whitelist-derived. */
function chip(className, value, title) {
    const node = el("span", "chip " + className, value);
    if (title) node.setAttribute("title", String(title));
    return node;
}

function field(label, value) {
    const wrap = el("div", "field");
    wrap.appendChild(el("span", "flabel", label));
    wrap.appendChild(el("span", "fvalue", value));
    return wrap;
}

function clear(node) {
    if (!node) return;
    while (node.firstChild) node.removeChild(node.firstChild);
}

/**
 * The ONLY place the animation class is attached (R-55's source guard).
 *
 * `live === true` is required rather than truthy-tested: a replayed or
 * backfilled frame carries `live:false` and must paint once, and a frame whose
 * `live` key went missing is not a live one either.
 */
function paint(node, live) {
    if (live === true) node.classList.add(LIVE_CLASS);
    return node;
}

/** A whitelist lookup. Never `"prefix " + serverValue`. */
function classOf(table, value) {
    return Object.prototype.hasOwnProperty.call(table, value) ? table[value] : "st-other";
}

// --------------------------------------------------------- 3. formatting

function fmtInt(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "0";
    return Math.round(n).toLocaleString("en-US");
}

function fmtTokens(totals) {
    return TOKEN_KEYS.map((key) => key + " " + fmtInt(totals[key] || 0)).join("  ·  ");
}

/**
 * A record timestamp, rendered in the viewer's locale.
 *
 * `new Date(value)` parses a *given* instant; there is deliberately no
 * `Date.now()` and no bare `new Date()` in this file, because "now" is the
 * ingredient every liveness derivation needs and the reducer owns that
 * (GD-23). A ts that does not parse is shown verbatim.
 */
function fmtTs(value) {
    if (!value) return "";
    const parsed = new Date(String(value));
    if (Number.isNaN(parsed.getTime())) return String(value);
    return parsed.toLocaleTimeString([], { hour12: false }) +
        "." + String(parsed.getMilliseconds()).padStart(3, "0");
}

function truncate(value, limit) {
    const s = String(value === undefined || value === null ? "" : value);
    return s.length <= limit ? s : s.slice(0, limit) + "…";
}

/** `<stream>#<seq:012d>` — `store.cursor_key`'s grammar, byte-identical. */
function cursorKey(stream, seq) {
    return String(stream) + "#" + String(Math.max(0, Number(seq) || 0)).padStart(12, "0");
}

/** `run:<runId>` is the stream id `h_events` builds; the runId is the suffix. */
function runIdOf(stream) {
    return String(stream).startsWith("run:") ? String(stream).slice(4) : null;
}

/**
 * Whether that suffix is a runId the run routes will accept.
 *
 * `store.validate_stream` accepts multi-component stream ids and
 * `run:legacy:<task>` is a documented one, but `server.ID_PATTERNS["run"]` is
 * `_NAME_RE`, which has no colon in its character class — so
 * `/api/run/graph?run=legacy:touch-repo-recon` is a 400 "malformed runId". A
 * sidebar row linking there is a dead link, and if such a stream is ever the
 * newest-written one the server names as `currentRun` the page would boot
 * straight into the error panel. Those rows render as rows, labelled, with no
 * link — the honest rendering of "this stream exists and has no graph route".
 */
function isLinkableRunId(runId) {
    return !!runId && String(runId).indexOf(":") === -1;
}

/**
 * A ref, for a human to read. Truncated, so it is a *display* string only.
 */
function refSummary(ref) {
    if (!ref || typeof ref !== "object") return "";
    return Object.keys(ref).sort().map((k) => k + "=" + truncate(ref[k], 48)).join(" ");
}

/**
 * A ref, as an identity — byte-identical to `TokenCoalescer.key_of` in
 * `aggregator/server.py`: sorted `name=value` pairs joined with `|`, never
 * truncated.
 *
 * Separate from `refSummary` on purpose. A truncating display formatter is not
 * an identity: `orchAgent.name`, `orchAgent.root` and `runNode.key` are
 * free-form (GD-11), and two refs that merely agree in their first 48
 * characters would collapse into one rollup entry — the later record silently
 * replacing the earlier one and the counter reading low, which is exactly the
 * failure the absolute-token model exists to prevent (R-55). Joining with `|`
 * rather than a space also keeps the mapping injective, and matching the
 * server's own key means the two sides bucket a ref the same way.
 */
function refKey(ref) {
    if (!ref || typeof ref !== "object") return "";
    return Object.keys(ref).sort().map((k) => k + "=" + String(ref[k])).join("|");
}

function dataSummary(record) {
    const data = record && record.data;
    if (!data || typeof data !== "object") return "";
    if (record.kind === "token") {
        return TOKEN_KEYS.map((k) => k + ":" + fmtInt(data[k] || 0)).join(" ");
    }
    const parts = [];
    Object.keys(data).sort().forEach((k) => {
        const value = data[k];
        const flat = (value && typeof value === "object")
            ? JSON.stringify(value)
            : String(value);
        parts.push(k + "=" + truncate(flat, 64));
    });
    return truncate(parts.join(" "), DETAIL_CHARS);
}

// ------------------------------------------------------------- 4. page state

const state = {
    /** Sidebar data, all of it fetched from the server's reduction. */
    sessions: [],
    tasks: [],
    streams: [],
    currentRun: null,
    streamsUnobserved: [],

    /** `{kind: "session"|"run"|"task"|null, id}` — the sidebar selection. */
    sel: { kind: null, id: null },
    /** The selected thing's server payload, whatever kind it is. */
    detail: null,
    detailError: null,

    /** Wire: what the handshake and the boundary told us. */
    wire: {
        mode: "connecting",
        live: false,
        window: 0,
        reducerVersion: null,
        protocol: null,
        resumed: false,
        notices: [],
    },
    /** Server-published cursors — the ONLY thing we reconnect from. */
    resume: {},
    /** Max seq actually delivered per stream (display + de-dup, never resume). */
    delivered: {},
    /** `{stream: {oldest, truncated}}` from `mode` and `anchors` frames. */
    anchors: {},

    /** `(stream, seq)` keys already applied, FIFO-capped. */
    seen: new Set(),
    seenOrder: [],

    /** refKey -> the latest ABSOLUTE token record for that ref. FIFO-capped. */
    tokens: new Map(),
    /** Bumped whenever a rollup entry actually moved (the render short-circuit). */
    tokensRev: 0,
    /** What the sum does not contain: refs the cap released, records with no ref. */
    tokensEvicted: 0,
    tokensRefless: 0,

    /** Log entries not yet in the DOM (appended in one fragment per render). */
    pending: [],
    /**
     * Three counters that close: every entry the log was offered is on screen,
     * trimmed off the top, or dropped before it was ever painted.
     * `seen = shown + trimmed + queueDropped`, and "shown" is read off the DOM
     * rather than accumulated, so the meta line cannot drift from the list.
     */
    logSeen: 0,
    trimmed: 0,
    queueDropped: 0,
    /** Rows of loaded history in `#older` — its own budget (`OLDER_MAX`). */
    olderShown: 0,
    /**
     * WHICH stream those rows belong to.
     *
     * History is per-stream by construction — the anchor, the truncation and
     * the backwards page all are — so the list needs the stream's identity or
     * it becomes a global pool: one stream's rows sitting above another
     * stream's live tail, under a button whose "full · 400 rows" is true of the
     * list and false of the thing the operator is looking at, with the budget
     * spent for every stream for the life of the tab. It is emptied and
     * re-identified whenever `currentStream()` moves (and on reconnect).
     */
    olderStream: null,
    duplicates: 0,
    /** Records the server re-sent to satisfy a resume ack (`subscribed`). */
    backfilled: 0,

    health: null,

    /**
     * Failures, one slot per source, each owned by the code that produced it.
     *
     * A notice surface must describe the *current* cycle. An append-only one
     * pins a resolved failure to the top of the page for the life of the tab,
     * which is the same class of lie as a fabricated badge (GD-23/D13): the
     * page would be stating something the server no longer concludes. So every
     * arm clears its own slot on success and sets it on failure — never a
     * shared flag one arm's success can wipe on another's behalf.
     */
    errors: {},

    /**
     * The one sticky message: this document was not served by Touch, so no
     * route but `/health` can work and no later success can contradict it.
     */
    bootError: null,

    /** Bumped when `detail`/`detailError` actually MOVED — the panel's signature. */
    detailRev: 0,
    /** The serialized payload the revision above describes (the change-guard). */
    detailKey: null,
    /**
     * How wide the selected session's timeline window is, in records.
     *
     * Grown a page at a time by the button under the list, reset by `select`.
     * It is a window rather than an accumulating cursor walk because this panel
     * is re-fetched on the live cadence: `refreshDetail` runs every couple of
     * seconds for the current selection, so pages appended on this side would
     * be thrown away by the next poll. The server's `nextSince`/`nextSinceId`
     * are the right tool for a client paging a *static* list; the right tool
     * for a polled panel is to ask for the window it means to show.
     */
    timelineLimit: TIMELINE_PAGE,
    /**
     * The last painted signature per region.
     *
     * `render()` runs on a debounced frame, which during a burst is ~8 Hz, and
     * a region that clears and rebuilds itself that often destroys any text
     * selection inside it and swallows a click whose `mousedown` and `mouseup`
     * straddle the rebuild. Each signature is built from exactly the values its
     * region renders, so an unchanged region keeps its DOM.
     */
    sigs: {},
    /** The notice box's last painted text (it is a live region — see below). */
    noticeText: null,
};

/** Set (`message`) or clear (`null`) one source's error slot. */
function setError(source, message) {
    if (message === null || message === undefined) delete state.errors[source];
    else state.errors[source] = source + ": " + String(message);
}

const dom = {};

function bindDom() {
    ["conn", "connText", "modeChip", "mirrorChip", "reducerChip", "rollup", "notice",
     "sessionList", "runList", "taskList", "sessionCount", "runCount", "taskCount",
     "detailHead", "detailStatus", "detailBody", "log", "older", "logMeta",
     "olderBtn"].forEach((id) => {
        dom[id] = document.getElementById(id);
    });
}

// -------------------------------------------- 5. token rollups (absolute)

/**
 * Remember one absolute token record, keyed by its ref.
 *
 * Latest-wins per ref, and the sum is taken across refs at render time: the
 * records are cumulative per agent, so adding two observations of the same ref
 * double-counts and subtracting them is the delta model R-55 forbids. Keyed by
 * `(stream, ref)` because the same agent id can legitimately appear in two
 * streams (a legacy folder and a live run) and they are two observations.
 */
function noteTokens(stream, record) {
    if (!record || record.kind !== "token") return;
    const ref = record.ref;
    if (!ref || typeof ref !== "object" || !Object.keys(ref).length) {
        // A token record that names no ref names no agent, so it has no bucket.
        // Filing it under the empty key would collapse every such record in a
        // stream into ONE slot, latest-wins — the same collapsing-key failure
        // the untruncated `refKey` exists to prevent, and it reads low. It is
        // counted and said out loud beside the sum instead.
        state.tokensRefless += 1;
        return;
    }
    const key = stream + "|" + refKey(ref);
    const totals = {};
    TOKEN_KEYS.forEach((k) => {
        const value = Number((record.data || {})[k]);
        totals[k] = Number.isFinite(value) && value >= 0 ? value : 0;
    });
    totals.stream = stream;
    totals.seq = Number(record.seq) || 0;
    const held = state.tokens.get(key);
    // Out-of-order delivery is possible after a resume (a backfill re-sends an
    // older record); the newest seq for a ref is the current absolute value.
    if (held && held.seq > totals.seq) return;
    // Whether the RENDERED value moved. A re-delivered or simply unchanged
    // record carries a new seq and the same four numbers, and bumping the
    // revision for it tears down and rebuilds the whole detail panel — nested
    // agent tree included — on the socket's cadence. The revision is the
    // render's signature, so it may only move when the render would.
    const moved = !held || TOKEN_KEYS.some((k) => held[k] !== totals[k]);
    // Re-inserted rather than overwritten, which makes the cap LRU instead of
    // first-seen: `Map.set` on an existing key keeps its original position, so
    // a FIFO eviction released the busiest long-lived ref while a one-shot ref
    // observed later survived.
    state.tokens.delete(key);
    state.tokens.set(key, totals);
    if (moved) state.tokensRev += 1;
    // Bounded like `state.seen`: a Map iterates in insertion order, so with the
    // re-insertion above the first key is the ref *touched* longest ago. An
    // eviction makes the sum a floor, which `rollupTitle` says on the element.
    while (state.tokens.size > TOKENS_MAX) {
        const oldest = state.tokens.keys().next().value;
        state.tokens.delete(oldest);
        state.tokensEvicted += 1;
    }
}

/**
 * One `ctx` occupancy block off a row — validated whole — or `null`.
 *
 * Deliberately NOT a member of the rollup family above, and this is the guard
 * that keeps it out: `tokens` is cumulative SPEND, so summing the latest record
 * per ref is the right answer; `ctx` is a LEVEL — how full one agent's context
 * window was at one instant — and every arithmetic over two of them is a
 * fabrication. Two agents' occupancies do not add (separate windows), and two
 * readings of ONE agent do not either (the same window, twice). Nothing in this
 * file sums, maxes, deltas or clamps it; each row shows its own number or none.
 *
 * `null` is what "no reading" looks like, and it is the whole discipline: a
 * missing block is spelled by rendering nothing, never by a `0`. `0` would say
 * "this agent's window is empty", which is never true of a live agent — a fresh
 * window already holds tens of thousands of tokens of system prompt, tools and
 * always-on files before its first word. So `snapNum`-style validation, never
 * `| 0` or `Number(x) || 0` coercion: any invalid field invalidates the whole
 * block, because half a reading is a made-up one.
 *
 * No percentage and no bar is derived here even when `cap` is present: 8932
 * renders the absolute number only (the gauge is 8931's contract), and there is
 * no window constant in this file to fall back to when `cap` is absent.
 */
function ctxOf(row) {
    const ctx = row && row.ctx;
    if (!ctx || typeof ctx !== "object") return null;
    const used = Number(ctx.used);
    if (!Number.isFinite(used) || used <= 0 || used >= 1e9) return null;
    if (typeof ctx.at !== "string" || !ctx.at) return null;
    const out = { used: used, at: ctx.at };
    ["peak", "cap"].forEach((key) => {
        const value = Number(ctx[key]);
        if (Number.isFinite(value) && value > 0 && value < 1e9) out[key] = value;
    });
    if (typeof ctx.model === "string" && ctx.model) out.model = ctx.model;
    if (typeof ctx.src === "string" && ctx.src) out.src = ctx.src;
    return out;
}

/** The hover text for one reading: provenance and staleness, never a percentage. */
function ctxTitle(ctx) {
    const parts = ["context occupancy — input tokens only, the level at " + fmtTs(ctx.at)];
    if (ctx.model) parts.push("model " + ctx.model);
    if (ctx.peak) parts.push("peak " + fmtInt(ctx.peak));
    // Named "declared" on purpose: it is an `orch-config.json` assertion, not
    // something Touch measured, and it is absent whenever nobody declared it.
    if (ctx.cap) parts.push("declared window " + fmtInt(ctx.cap));
    if (ctx.src === "compact") parts.push("read from a compaction boundary");
    return parts.join(" · ");
}

/** Sum of the latest absolute record per ref — "token rollups from computed sums". */
function rollup(filter) {
    const totals = {};
    TOKEN_KEYS.forEach((k) => { totals[k] = 0; });
    state.tokens.forEach((entry) => {
        if (filter && !filter(entry)) return;
        TOKEN_KEYS.forEach((k) => { totals[k] += entry[k] || 0; });
    });
    return totals;
}

/**
 * Fold a legacy task's token list the same way (R-32's rollup half).
 *
 * `legacy.Reduction.tokens` holds at most one folded record per agent per
 * throttle window (GD-14), each one cumulative — so a record that names an
 * agent rolls up latest-per-ref summed, by the same rule and the same helper
 * vocabulary as the socket's.
 *
 * A record that names NO agent is the other kind. The reducer kept it whole
 * because no cumulative could be attributed to an agent, so its four numbers
 * are that line's own delta, and nothing else IN THIS LIST states them — the
 * reducer's fold only sees token-stage lines. (The raw stream may state it
 * elsewhere; an agent's terminal event carries a higher cumulative. That gap is
 * a measured 1.9 %, and it is why this sum and a cumulative-from-any-event
 * model must never be combined — see `legacy._fold_tokens`.)
 *
 * Such a record also has no bucket: every agent-less record of a plan shares
 * the key `plan|stage|null|null`, so putting them in the latest-wins map kept
 * the last one and silently dropped the rest — the same collapsing-key failure
 * `noteTokens` refuses for a ref-less live record, and here it cost 14.7 M
 * tokens (1.65 %) against the monitor's delta-sum over the very same bytes.
 * They are summed instead, which lands on that sum exactly, plan by plan, on
 * all four frozen corpora (`tests/test_legacy.py` pins it).
 *
 * Reading `agentId` is a proxy for the reducer's `absolute` flag, which the
 * wire does not carry (`server.py`'s token payload is an explicit seven-field
 * dict). The reducer derives `absolute` from `agent_id`, so the proxy is exact
 * by construction rather than by luck; the follow-up that would remove the
 * proxy — putting `absolute` on the wire — is recorded on `TokenRecord`.
 */
function rollupList(entries) {
    const latest = new Map();
    const whole = [];
    (entries || []).forEach((entry) => {
        const tokens = entry.tokens || {};
        if (entry.agentId === undefined || entry.agentId === null) {
            whole.push(tokens);
            return;
        }
        const key = [entry.plan, entry.stage, entry.agentId, entry.label].join("|");
        latest.set(key, tokens);
    });
    const totals = {};
    TOKEN_KEYS.forEach((k) => { totals[k] = 0; });
    whole.concat(Array.from(latest.values())).forEach((tokens) => {
        TOKEN_KEYS.forEach((k) => {
            const value = Number(tokens[k]);
            if (Number.isFinite(value) && value > 0) totals[k] += value;
        });
    });
    return totals;
}

// ------------------------------------------------------------ 6. the socket

let socket = null;
let reconnectAt = 0;
let reconnectTimer = null;
let resyncTimer = null;

function wsUrl() {
    const url = new URL("/ws", window.location.href);
    url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    if (TOKEN) url.searchParams.set("token", TOKEN);
    // The resume position, one `?cursor=` per stream, in the server's own
    // grammar. Empty on a first connect, which is a full bounded replay.
    Object.keys(state.resume).sort().forEach((stream) => {
        url.searchParams.append("cursor", cursorKey(stream, state.resume[stream]));
    });
    return url.toString();
}

/**
 * Drop the socket and its handlers.
 *
 * Both handlers are nulled before `close()` (the monitor's FRONTEND-4 lesson):
 * a socket that is closing still delivers queued messages, and a stale
 * `onmessage` writing into the state a fresh connection is rebuilding is a
 * double-count nothing later reconciles.
 */
function dropSocket() {
    if (resyncTimer) { window.clearInterval(resyncTimer); resyncTimer = null; }
    if (!socket) return;
    socket.onmessage = null;
    socket.onopen = null;
    socket.onclose = null;
    socket.onerror = null;
    try { socket.close(); } catch (err) { /* already gone */ }
    socket = null;
}

function connect() {
    dropSocket();
    // Wire notices are scoped to ONE connection. Every one of them ("resumed
    // from the last position…", "?from= was not a seq…", "stream selector
    // refused…") reports on a specific handshake, so carrying it across a
    // reconnect makes the page describe a socket that no longer exists. The
    // next hello re-states whatever is still true.
    state.wire.notices = [];
    // The anchors are the same kind of thing and get the same treatment: they
    // describe what THIS connection's replay cut off. Kept across a reconnect
    // they offer "load older" for a stream the new socket never called
    // truncated, against an `oldest` that points into a range the resumed
    // replay may have already re-delivered — so the walk starts in the wrong
    // place and, because those seqs are in `state.seen`, paints nothing while
    // spending a whole-stream `read_all` on the server. The next mode frame
    // publishes the new connection's anchors, and the loaded history goes with
    // them: it is history of a window this socket has not described.
    state.anchors = {};
    resetOlder(null);
    setConn("connecting", false);
    let sock;
    try {
        sock = new WebSocket(wsUrl());
    } catch (err) {
        scheduleReconnect();
        return;
    }
    socket = sock;
    sock.onopen = () => {
        reconnectAt = 0;
        state.wire.mode = "replay";
        state.wire.live = false;
        setConn("connected", true);
        // While the tail is live, re-ask for the authoritative cursors: the
        // server holds a coalesced token record back and keeps its published
        // cursor behind it, so this is the only way our resume position stays
        // both current and safe. A pair equal to the server's is a no-op; a
        // pair ahead of it is refused, and the ack carries the truth.
        resyncTimer = window.setInterval(resync, RESYNC_MS);
        render();
    };
    sock.onmessage = (event) => {
        let frame = null;
        try {
            frame = JSON.parse(event.data);
        } catch (err) {
            note("a socket frame was not JSON and was ignored");
            return;
        }
        onFrame(frame);
    };
    sock.onclose = () => {
        state.wire.live = false;
        state.wire.mode = "offline";
        setConn("offline", false);
        dropSocket();
        scheduleReconnect();
        render();
    };
    sock.onerror = () => { setConn("error", false); };
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    const delay = RECONNECT_MS[Math.min(reconnectAt, RECONNECT_MS.length - 1)];
    reconnectAt += 1;
    reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
    }, delay);
}

/**
 * Ask the socket to re-publish its cursors (see `connect`'s comment).
 *
 * What goes out is the server's OWN published position, never
 * `state.delivered`. The two diverge by design: while the coalescer holds a
 * token record, `WsSession._advance` clamps the published cursor to
 * `pending_floor - 1` yet keeps sending the frames after the held one — so
 * "max seq I received" is *ahead* of the socket, and `subscribe` refuses an
 * ahead cursor by name ("ahead of this socket at seq N"). Handing back what
 * the server published is the no-op this timer wants: the ack answers with
 * fresh, authoritative cursors and nothing is rewound.
 *
 * The one case where a *lower* value is right is a frame we never saw; then we
 * ask for a rewind, which the server re-delivers as `live:false` backfill.
 * `Math.min` is the whole rule — a resume position may go backwards, never
 * forwards, because forwards is a skip.
 */
function resync() {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const cursors = {};
    Object.keys(state.resume).forEach((stream) => {
        const published = state.resume[stream];
        const got = state.delivered[stream];
        cursors[stream] = (got !== undefined && got < published) ? got : published;
    });
    if (!Object.keys(cursors).length) return;
    try {
        socket.send(JSON.stringify({ type: "subscribe", cursors: cursors }));
    } catch (err) { /* the close handler will deal with it */ }
}

function setConn(label, on) {
    state.wire.connLabel = label;
    state.wire.connOn = !!on;
}

function note(message) {
    const list = state.wire.notices;
    if (list.indexOf(message) === -1) list.push(message);
    if (list.length > 8) list.shift();
}

function remember(key) {
    state.seen.add(key);
    state.seenOrder.push(key);
    if (state.seenOrder.length > SEEN_MAX) {
        const oldest = state.seenOrder.shift();
        state.seen.delete(oldest);
    }
}

/**
 * Learn a stream from the frame that named it.
 *
 * `hello.streams` is a snapshot of ONE instant — the handshake — and a socket
 * only handshakes again when it drops. The server, meanwhile, re-evaluates its
 * stream set on every tick and has a dedicated arm for a stream that came into
 * existence afterwards: it publishes an `anchors` frame naming the stream and
 * then sends its backlog as `live:false` backfill. Those records were already
 * landing in the log, in `state.delivered` and in the token rollup while the
 * sidebar still listed only the runs that existed at connect — so "start a
 * run and watch it" showed the run's rows and its tokens under a run list that
 * said the run did not exist. There is no `/api/runs` route; the socket is the
 * only channel that can teach the page about a run, and it was being told.
 *
 * This adds the stream and nothing else. `currentRun` stays the server's word
 * (GD-23) — a run learned here renders without the `current` marker until a
 * handshake names it.
 */
function noteStream(stream) {
    const key = String(stream || "");
    if (!key || state.streams.indexOf(key) !== -1) return;
    state.streams.push(key);
    state.streams.sort();
}

function onFrame(frame) {
    if (!frame || typeof frame !== "object") return;
    switch (frame.type) {
    case "hello": onHello(frame); break;
    case "event": onEvent(frame); break;
    case "mode": onMode(frame); break;
    case "anchors": onAnchors(frame); break;
    case "subscribed": onSubscribed(frame); break;
    case "tick": state.wire.lastTick = frame.ts; break;
    default:
        // An unknown frame type is a newer server, not an error: named once,
        // never rendered as state.
        note("unknown frame type: " + truncate(frame.type, 32));
        break;
    }
    schedule();
}

function onHello(frame) {
    state.wire.mode = frame.mode || "replay";
    state.wire.live = frame.live === true;
    state.wire.window = Number(frame.window) || 0;
    state.wire.reducerVersion = frame.reducerVersion || null;
    state.wire.protocol = frame.protocol || null;
    state.wire.resumed = frame.resumed === true;
    state.streams = Array.isArray(frame.streams) ? frame.streams.slice() : [];
    state.currentRun = frame.currentRun || null;
    state.streamsUnobserved = Array.isArray(frame.streamsUnobserved)
        ? frame.streamsUnobserved.slice() : [];
    // Every handshake parameter the server could not use is named on hello
    // (after the 101 there is no status code left) — so it is surfaced, not
    // swallowed.
    (frame.cursorsRejected || []).forEach((raw) => {
        note("resume cursor refused: " + truncate(raw, 64));
    });
    (frame.streamsRejected || []).forEach((raw) => {
        note("stream selector refused: " + truncate(raw, 64));
    });
    // The two `?from=` arms cannot fire for a socket THIS page opened: `wsUrl`
    // sends `?cursor=` and nothing else. They are here because `/ws` is a
    // documented endpoint an operator can open by hand (or through a proxy
    // that rewrites the query), and a handshake that quietly ignored half of
    // what it was given is the failure the hello report exists to prevent.
    if (frame.fromRejected) note("?from= was not a seq: " + truncate(frame.fromRejected, 32));
    if (frame.from !== null && frame.from !== undefined && frame.fromApplied === false) {
        note("?from= needs exactly one stream selector; it was not applied");
    }
    state.streamsUnobserved.forEach((stream) => {
        note("never observed, watching for it: " + truncate(stream, 64));
    });
    if (state.wire.resumed) {
        note("resumed from the last position the socket published — the replay " +
             "continues after it, with no gap and no duplicate");
    }
    // A fresh connection with no selection yet lands on the current run, which
    // is the freshest thing the server can name. `currentRun` is only ever an
    // *observed* stream (the server refuses to name an unobserved one).
    if (!state.sel.kind && state.currentRun) {
        const runId = runIdOf(state.currentRun);
        // …and only if the run routes can answer for it: booting straight into
        // a 400 panel because the newest-written stream happens to be a
        // `run:legacy:<task>` one is a worse first screen than no selection.
        if (isLinkableRunId(runId)) select("run", runId, { replaceHash: true });
    }
    queueRefresh();
}

function onEvent(frame) {
    const key = frame.cursor || cursorKey(frame.stream, frame.seq);
    if (state.seen.has(key)) { state.duplicates += 1; return; }
    remember(key);
    const seq = Number(frame.seq) || 0;
    const stream = String(frame.stream || "");
    // A frame is allowed to be the first mention of a stream (the late-stream
    // backfill arm is exactly that), so the sidebar learns it here.
    noteStream(stream);
    if (!(stream in state.delivered) || state.delivered[stream] < seq) {
        state.delivered[stream] = seq;
    }
    noteTokens(stream, frame.record);
    pushLog({
        stream: stream,
        seq: seq,
        live: frame.live === true,
        record: frame.record || {},
    });
    // A live record means the server's reduction moved; the refetch is
    // throttled so a burst costs one request, not one per frame.
    if (frame.live === true) queueRefresh();
}

function onMode(frame) {
    // The ONE replay -> tail boundary. It is sent exactly once and may never be
    // repeated, so this is where the page leaves the replay of history behind and
    // starts being a live view — and where the load-older anchors first exist.
    state.wire.mode = frame.mode || "tail";
    state.wire.live = frame.live === true;
    adoptCursors(frame.cursors);
    Object.keys(frame.oldest || {}).forEach((stream) => {
        anchorOf(stream).oldest = frame.oldest[stream];
    });
    Object.keys(frame.truncated || {}).forEach((stream) => {
        anchorOf(stream).truncated = frame.truncated[stream] === true;
    });
    queueRefresh();
}

function onAnchors(frame) {
    // A backfill that happened after the boundary publishes its own anchors,
    // immediately before the frames it describes — and for a stream that did
    // not exist at the handshake this frame is its first mention anywhere.
    noteStream(frame.stream);
    const anchor = anchorOf(frame.stream);
    anchor.oldest = frame.oldest;
    anchor.truncated = frame.truncated === true;
}

function onSubscribed(frame) {
    // The ack's cursors are adopted first: they are the answer, and they are
    // correct whether or not anything in the same frame was refused.
    adoptCursors(frame.cursors);
    // `backfilled` is the server's count of records it re-sent to satisfy the
    // rewind — the one number that says the resume actually re-delivered
    // something rather than being a no-op. It goes on the meta line beside the
    // duplicates, because those two together explain a row count that is
    // larger than the number of distinct records the run produced.
    const counts = frame.backfilled || {};
    Object.keys(counts).forEach((stream) => {
        const n = Number(counts[stream]);
        if (Number.isFinite(n) && n > 0) state.backfilled += n;
    });
    (frame.rejected || []).forEach((entry) => {
        const label = entry && entry.cursor ? entry.cursor : String(entry);
        const why = entry && entry.reason ? String(entry.reason) : "refused";
        // "ahead of this socket" is the coalescer's ordinary hold, not a fault:
        // a resync can race a held token record, the server declines to adopt a
        // position it never published, and the same frame hands back the one to
        // use. Naming that on screen would be alarming and wrong.
        if (why.indexOf("ahead of this socket") === 0) return;
        note("cursor " + truncate(label, 48) + ": " + truncate(why, 64));
    });
}

/** The server's published position is authoritative; ours is never adopted. */
function adoptCursors(cursors) {
    if (!cursors || typeof cursors !== "object") return;
    Object.keys(cursors).forEach((stream) => {
        const seq = Number(cursors[stream]);
        if (Number.isFinite(seq) && seq >= 0) state.resume[stream] = seq;
    });
}

function anchorOf(stream) {
    const key = String(stream);
    if (!state.anchors[key]) state.anchors[key] = { oldest: null, truncated: false };
    return state.anchors[key];
}

// -------------------------------------------------------- 7. the render loop

let renderTimer = null;
let renderFrame = null;
let refreshTimer = null;

/**
 * Render coalescing, from day one (GD-20 do-not-inherit: the monitor's
 * render-everything loop). A burst of a thousand replayed frames is one paint,
 * not a thousand: frames accumulate in `state`, a timer collapses them, and
 * `requestAnimationFrame` puts the single paint where the browser wants it.
 */
function schedule() {
    if (renderTimer) return;
    renderTimer = window.setTimeout(() => {
        renderTimer = null;
        if (renderFrame) return;
        renderFrame = window.requestAnimationFrame(() => {
            renderFrame = null;
            render();
        });
    }, RENDER_DEBOUNCE_MS);
}

/**
 * Refetch the server's reduction, at most once per `REFRESH_MS`.
 *
 * The pending timer IS the flag — there is no second boolean, because the two
 * could only ever disagree: the old pair guarded on `refreshPending` first and
 * then on `refreshTimer`, and the second guard was unreachable by construction
 * (the timer is non-null exactly while the flag is true).
 */
function queueRefresh() {
    if (refreshTimer) return;
    refreshTimer = window.setTimeout(() => {
        refreshTimer = null;
        refreshModel();
    }, REFRESH_MS);
}

function render() {
    renderHeader();
    renderNotices();
    renderSidebar();
    renderDetail();
    flushLog();
}

function renderHeader() {
    const conn = dom.conn;
    if (conn) conn.className = "conn" + (state.wire.connOn ? " on" : "");
    setText(dom.connText, state.wire.connLabel || "connecting");

    // The mode chip is the replay/tail boundary made visible: a replayed frame
    // is history painted once, and the page says which it is showing.
    const mode = state.wire.live ? "live" : state.wire.mode;
    if (dom.modeChip) {
        dom.modeChip.className = "chip chip-mode " +
            classOf(MODE_CLASS, state.wire.live ? "tail" : String(state.wire.mode || ""));
        setText(dom.modeChip, state.wire.live ? "live tail" : String(mode));
    }

    const mirror = (state.health && state.health.mirror) || null;
    const mirrorState = mirror ? String(mirror.state) : "—";
    if (dom.mirrorChip) {
        dom.mirrorChip.className = "chip " + classOf(MIRROR_CLASS, mirrorState) +
            (mirrorState === "ok" || mirrorState === "live" ? "" : " prov-derived");
        setText(dom.mirrorChip, "mirror " + mirrorState);
        dom.mirrorChip.setAttribute(
            "title",
            mirror && mirror.lastError
                ? "mirror " + mirrorState + " — " + mirror.lastError
                : "Mongo is a rebuildable mirror; the live view never depends on it");
    }

    if (dom.reducerChip) {
        setText(dom.reducerChip, "reducer " + (state.wire.reducerVersion || "—"));
        dom.reducerChip.setAttribute(
            "title",
            "every badge on this page is this reducer's output (GD-23)" +
            (state.wire.protocol ? " · wire schema " + state.wire.protocol : ""));
    }
    setText(dom.rollup, fmtTokens(rollup(null)));
    if (dom.rollup) dom.rollup.setAttribute("title", rollupTitle());
}

/** What the header's sum is, and what it leaves out — stated, not implied. */
function rollupTitle() {
    const parts = ["absolute token records, summed per ref"];
    if (state.tokensEvicted) {
        parts.push(fmtInt(state.tokensEvicted) +
                   " ref(s) released by the rollup cap — this sum is a floor");
    }
    if (state.tokensRefless) {
        parts.push(fmtInt(state.tokensRefless) +
                   " token record(s) named no ref and are not summed");
    }
    return parts.join(" · ");
}

function renderNotices() {
    const box = dom.notice;
    if (!box) return;
    // Sticky first, then this cycle's live failures, then this connection's
    // handshake report. Every line here is re-derived from state that some arm
    // owns and clears, so the box empties itself when the trouble ends.
    const lines = [];
    if (state.bootError) lines.push(state.bootError);
    Object.keys(state.errors).sort().forEach((source) => lines.push(state.errors[source]));
    state.wire.notices.forEach((line) => lines.push(line));
    // `#notice` is a live region (`role="status"` is an implicit
    // `aria-live="polite"`), so every rebuild is an ANNOUNCEMENT: clearing and
    // re-appending the same lines on each paint makes a screen reader re-read
    // them at the paint rate — up to ~8 Hz in a burst, forever for a sticky
    // line. The text is compared first and an unchanged box is left completely
    // alone, which is the same rule `setText` applies to the status line.
    const next = lines.join("\n");
    if (next === state.noticeText) return;
    state.noticeText = next;
    clear(box);
    box.hidden = !lines.length;
    lines.forEach((line) => box.appendChild(el("span", "noticeline", line)));
}

// ------------------------------------------------------------- 8. sidebar

function renderSidebar() {
    renderSessions();
    renderRuns();
    renderTasks();
}

/**
 * Rebuild a region only when the values it renders actually moved.
 *
 * Every list here used to be cleared and rebuilt on every paint — at least
 * every two seconds, ~8 Hz during a burst. That drops any text selection made
 * inside the region and loses a click whose `mousedown` and `mouseup` land on
 * either side of the rebuild, which on a sidebar is the difference between
 * "clicked a run" and "nothing happened". The signature is built from exactly
 * the fields the region paints, so this changes when the region would.
 *
 * It is a *rendering* short-circuit and not a cache of anything derived: the
 * values compared are the server's, unchanged (GD-23).
 */
function region(name, signature, build) {
    if (state.sigs[name] === signature) return false;
    state.sigs[name] = signature;
    build();
    return true;
}

function rowLink(kind, id, label) {
    const li = el("li", "row" + (state.sel.kind === kind && state.sel.id === id
        ? " selected" : ""));
    const link = el("a", "rowlink");
    link.setAttribute("href", "#" + kind + "/" + encodeURIComponent(id));
    link.appendChild(el("span", "rowtitle", label));
    li.appendChild(link);
    return { li: li, link: link };
}

/**
 * The same row, with no link — for a thing the read API has no route for.
 *
 * Same shape as `rowLink` on purpose (the caller fills `.link` either way), so
 * an unroutable row is a visibly ordinary row that simply cannot be clicked,
 * rather than a missing row or a link to a 400.
 */
function rowPlain(label) {
    const li = el("li", "row");
    // `rowstatic` turns off the hover the link rows have: a row that lights up
    // under the pointer and then does nothing is a worse affordance than a row
    // that never claimed to be one.
    const body = el("span", "rowlink rowstatic");
    body.appendChild(el("span", "rowtitle", label));
    li.appendChild(body);
    return { li: li, link: body };
}

function renderSessions() {
    const list = dom.sessionList;
    if (!list) return;
    setText(dom.sessionCount, state.sessions.length ? String(state.sessions.length) : "");
    const signature = JSON.stringify([
        state.sel.kind === "session" ? state.sel.id : null,
        state.sessions.map((session) => [session.id, session.kind, session.class,
                                         session.transcriptless, session.promotedTo,
                                         session.lastTs, session.cwd])]);
    region("sessions", signature, () => {
        clear(list);
        state.sessions.forEach((session) => {
            const built = rowLink("session", session.id, session.id);
            const meta = el("span", "rowmeta");
            // Both session classes are listed (GD-6/R-46's tagged union): a
            // historical session is a real session with no process, and it is
            // labelled rather than hidden.
            meta.appendChild(chip("kind-" + (session.kind === "live" ? "live" : "hist"),
                                  session.kind || "session"));
            if (session.class) meta.appendChild(chip("chip-plain", String(session.class)));
            if (session.transcriptless) {
                meta.appendChild(chip("prov-unknown", "no transcript",
                                      "observed in the registry, with nothing to show"));
            }
            if (session.promotedTo) meta.appendChild(chip("prov-derived", "promoted"));
            if (session.lastTs) meta.appendChild(el("span", "rowts", fmtTs(session.lastTs)));
            built.link.appendChild(meta);
            if (session.cwd) {
                built.link.appendChild(el("span", "rowsub", truncate(session.cwd, 64)));
            }
            list.appendChild(built.li);
        });
        if (!state.sessions.length) list.appendChild(el("li", "row empty", "none observed"));
    });
}

/**
 * The `seq N` text node of each run row, by stream.
 *
 * The delivered seq changes on EVERY frame for a stream — that is what it
 * means — so putting it in the region signature rebuilt the sidebar on every
 * live frame: exactly the list the operator clicks, during exactly the traffic
 * that made a mid-click rebuild a problem. The number still has to be current,
 * so the row keeps a handle on the one text node that carries it and that node
 * is written in place, through `setText`, which touches nothing when the text
 * is unchanged. Bounded by the rendered list: cleared and refilled by the
 * builder below, never accumulated across builds.
 */
let runSeqNodes = new Map();

function renderRuns() {
    const list = dom.runList;
    if (!list) return;
    const runs = state.streams.filter((s) => String(s).startsWith("run:"));
    setText(dom.runCount, runs.length ? String(runs.length) : "");
    const signature = JSON.stringify([
        state.sel.kind === "run" ? state.sel.id : null, state.currentRun, runs,
        runs.map((stream) => [(state.anchors[stream] || {}).truncated,
                              state.streamsUnobserved.indexOf(stream) !== -1])]);
    region("runs", signature, () => {
        clear(list);
        runSeqNodes = new Map();
        runs.forEach((stream) => {
            const runId = runIdOf(stream);
            // A `run:legacy:<task>` stream is a real stream with no run route
            // (`ID_PATTERNS["run"]` has no colon): it renders as a row that
            // says so, never as a link to a 400.
            const linkable = isLinkableRunId(runId);
            const built = linkable
                ? rowLink("run", runId, runId)
                : rowPlain(runId || stream);
            const meta = el("span", "rowmeta");
            if (!linkable) {
                meta.appendChild(chip("prov-unknown", "no graph route",
                                      "a multi-component run stream id; the run " +
                                      "routes accept a single-component runId only"));
            }
            if (stream === state.currentRun) {
                // NEUTRAL on purpose. `_current_run_stream` picks this by
                // `os.stat().st_mtime` and its own docstring insists that is a
                // selection, not a verdict — a finished run is still the most
                // recently written one. `st-running` here would paint the
                // reducer's liveness vocabulary onto something no reducer
                // concluded, which is the GD-23 failure in miniature.
                meta.appendChild(chip("chip-current", "current",
                                      "the newest-written run stream — the " +
                                      "server's selection, not a liveness verdict"));
            }
            if (state.streamsUnobserved.indexOf(stream) !== -1) {
                meta.appendChild(chip("prov-unknown", "unobserved"));
            }
            const anchor = state.anchors[stream];
            if (anchor && anchor.truncated) {
                meta.appendChild(chip("prov-derived", "window truncated",
                                      "older records exist beyond the replay window"));
            }
            // Built unconditionally and remembered, so the seq can be written
            // into it later without rebuilding the row (see `runSeqNodes`).
            const seqNode = el("span", "rowts");
            runSeqNodes.set(stream, seqNode);
            meta.appendChild(seqNode);
            built.link.appendChild(meta);
            list.appendChild(built.li);
        });
        if (!runs.length) list.appendChild(el("li", "row empty", "no run streams yet"));
    });
    // In place, every paint, rebuild or not — the one value on this list that
    // moves at the frame rate.
    runSeqNodes.forEach((node, stream) => {
        const seq = state.delivered[stream];
        setText(node, seq === undefined ? "" : "seq " + fmtInt(seq));
    });
}

function renderTasks() {
    const list = dom.taskList;
    if (!list) return;
    setText(dom.taskCount, state.tasks.length ? String(state.tasks.length) : "");
    const signature = JSON.stringify([
        state.sel.kind === "task" ? state.sel.id : null,
        state.tasks.map((task) => [task.task, task.kind, task.archive])]);
    region("tasks", signature, () => {
        clear(list);
        state.tasks.forEach((task) => {
            const built = rowLink("task", task.task, task.task);
            const meta = el("span", "rowmeta");
            // GD-14 kinds: a folder with a plan and no run is a real row, not
            // an error and not an empty task.
            meta.appendChild(chip("kind-" + (task.kind === "run" ? "run" : "plan"),
                                  task.kind || "task"));
            meta.appendChild(chip("prov-legacy", "legacy"));
            if (task.archive) {
                meta.appendChild(chip(
                    task.archive.state === "present" ? "chip-plain" : "prov-derived",
                    String(task.archive.label || task.archive.state),
                    task.archive.path || ""));
            }
            built.link.appendChild(meta);
            list.appendChild(built.li);
        });
        if (!state.tasks.length) list.appendChild(el("li", "row empty", "none found"));
    });
}

// -------------------------------------------------------------- 9. detail

/**
 * Write the panel's payload, moving the revision its signature is built on
 * **only when the payload actually differs**.
 *
 * `refreshDetail` runs on the live cadence and the overwhelmingly common case
 * is a byte-identical answer: the same run graph, the same nodes, the same
 * nested agent tree. A revision counter that bumps on every write makes the
 * region short-circuit inert for the largest region on the page — it was
 * measured tearing down and rebuilding the whole panel every two seconds and
 * on every token record — so the comparison is the payload itself. The
 * payloads here are small and this file already JSON-stringifies every
 * signature; the alternative (rebuild and hope nobody was selecting text or
 * mid-click) is what the short-circuit exists to end.
 */
function setDetail(payload, error) {
    const next = payload === undefined ? null : payload;
    const failure = error === undefined ? null : error;
    const key = JSON.stringify([next, failure]);
    if (key === state.detailKey) return;
    state.detailKey = key;
    state.detail = next;
    state.detailError = failure;
    state.detailRev += 1;
}

function renderDetail() {
    const head = dom.detailHead;
    const body = dom.detailBody;
    if (!head || !body) return;
    const view = state.sel.kind || "empty";
    // Written only when it moved, like every other repeated write in this file.
    if (document.body.getAttribute("data-view") !== view) {
        document.body.setAttribute("data-view", view);
    }
    // The payload is replaced wholesale by `setDetail`, so a revision counter
    // is a complete signature for it; the token revision is in because the run
    // panel prints a rollup line, and it is the only value here the socket can
    // move without a refetch.
    const signature = JSON.stringify([state.sel.kind, state.sel.id, state.detailRev,
                                      state.detailError, state.tokensRev,
                                      state.timelineLimit]);
    region("detail", signature, () => {
        clear(head);
        clear(body);
        if (state.detailError) {
            head.appendChild(el("h2", "", state.sel.id || "—"));
            body.appendChild(el("p", "error", state.detailError));
            announceDetail(String(state.sel.kind || "selection") + " " +
                           String(state.sel.id || "") + " — " + state.detailError);
            return;
        }
        if (!state.sel.kind || !state.detail) {
            head.appendChild(el("h2", "", "nothing selected"));
            body.appendChild(el("p", "hint",
                "pick a session, a run or a task folder on the left"));
            announceDetail("nothing selected");
            return;
        }
        if (state.sel.kind === "run") renderRunDetail(head, body, state.detail);
        else if (state.sel.kind === "session") renderSessionDetail(head, body, state.detail);
        else if (state.sel.kind === "task") renderTaskDetail(head, body, state.detail);
        announceDetail(detailSummary());
    });
}

/**
 * The panel's own live region (`#detailStatus`, `aria-live="polite"`).
 *
 * It is a short *status line*, not the panel: `renderDetail` clears and
 * rebuilds `#detailHead`/`#detailBody` whenever their signature moves, so a
 * live region around them would make a screen reader re-announce the whole
 * panel. `setText` writes only when the string differs, so an unchanged
 * summary produces no DOM mutation and therefore no announcement — the same
 * rule `renderNotices` applies to the page's other live region.
 */
function announceDetail(line) {
    setText(dom.detailStatus, truncate(line, DETAIL_CHARS));
}

/** One line naming what the panel is showing — the server's words, not ours. */
function detailSummary() {
    const payload = state.detail || {};
    if (state.sel.kind === "run") {
        const derived = payload.derived || null;
        return "run " + String(state.sel.id) +
            (derived ? " — " + String(derived.label || derived.state || "") : "") +
            " · " + fmtInt((payload.nodes || []).length) + " nodes, " +
            fmtInt((payload.agents || []).length) + " agents";
    }
    if (state.sel.kind === "session") {
        return "session " + String(state.sel.id) + " — " +
            fmtInt(payload.count) + " records" + (payload.hasMore ? ", more beyond" : "");
    }
    if (state.sel.kind === "task") {
        return "task folder " + String(state.sel.id) + " — " +
            fmtInt(Object.keys(payload.plans || {}).length) + " plans, " +
            fmtInt((payload.nodes || []).length) + " nodes";
    }
    return "nothing selected";
}

/** A `derived`-block renderer shared by nodes and agents (GD-23's output). */
function renderDerivedBlock(target, derived) {
    if (!derived) {
        target.appendChild(chip("prov-unknown", "no reducer verdict",
                                "the reducer has not seen this document yet"));
        return;
    }
    target.appendChild(chip(classOf(NODE_STATE_CLASS, derived.state),
                            String(derived.label || derived.state || "")));
    if (derived.frozen === true) {
        target.appendChild(chip("prov-derived", "frozen at run close"));
    }
    if (derived.attemptLabel) {
        target.appendChild(chip("chip-plain", String(derived.attemptLabel)));
    }
    if (derived.nextStage) {
        target.appendChild(chip("chip-plain", "→ " + String(derived.nextStage)));
    }
    if (derived.verdict) {
        target.appendChild(chip("chip-plain", "verdict " + String(derived.verdict)));
    }
    if (derived.unconventional === true) {
        target.appendChild(chip("prov-derived", "unnamed",
                                "no marker named this agent; the node still exists (GD-7)"));
    }
}

function provChip(target, provenance) {
    const value = provenance || "unknown";
    const node = chip(classOf(PROV_CLASS, value), String(value));
    if (value !== "harness") {
        node.setAttribute("title", "not directly observed — " + value + " (GD-28)");
    }
    target.appendChild(node);
    return node;
}

function renderRunDetail(head, body, payload) {
    const derived = payload.derived || null;
    head.appendChild(el("h2", "", payload.runId || payload.run || ""));
    const chips = el("div", "chips");
    if (derived) {
        chips.appendChild(chip(classOf(NODE_STATE_CLASS, derived.state),
                               String(derived.label || derived.state)));
        // Guarded like every other optional field on this head: `agents.reduce`
        // always sets `reason` today, and an empty chip is what an unguarded
        // one renders the day it does not.
        if (derived.reason) chips.appendChild(chip("chip-plain", String(derived.reason)));
        chips.appendChild(chip("chip-plain", "nodes " + fmtInt(derived.nodeCount)));
        if (derived.verdicts) {
            chips.appendChild(chip("chip-plain",
                "verdicts passed " + fmtInt(derived.verdicts.passed) +
                " / failed " + fmtInt(derived.verdicts.failed)));
        }
        if (derived.terminalObserved === false) {
            chips.appendChild(chip("prov-derived", "no terminal observed"));
        }
    }
    const observed = payload.observed || {};
    if (observed.ingestMode) chips.appendChild(chip("prov-derived", String(observed.ingestMode)));
    provChip(chips, observed.provenance);
    head.appendChild(chips);

    const stream = "run:" + (payload.runId || payload.run || "");
    const totals = rollup((entry) => entry.stream === stream);
    head.appendChild(el("div", "rollup-line", "tokens  " + fmtTokens(totals)));

    const facts = el("div", "facts");
    if (observed.workflowName) facts.appendChild(field("workflow", observed.workflowName));
    if (observed.taskId) facts.appendChild(field("task", observed.taskId));
    if (observed.startedAt) facts.appendChild(field("started", fmtTs(observed.startedAt)));
    if (observed.endedAt) facts.appendChild(field("ended", fmtTs(observed.endedAt)));
    if (observed.transcriptDir) {
        facts.appendChild(field("transcripts", truncate(observed.transcriptDir, 72)));
    }
    body.appendChild(facts);

    body.appendChild(el("h3", "", "nodes"));
    const nodes = el("ul", "cards");
    (payload.nodes || []).forEach((node) => {
        const item = el("li", "card");
        const obs = node.observed || {};
        const title = el("div", "cardhead");
        // GD-7: the node's identity is `(runId, key, ordinal)`; the marker's
        // label is a separate layer and a missing one degrades the label only.
        title.appendChild(el("span", "cardtitle",
            String(obs.key || "?") + " #" + String(obs.ordinal === undefined ? "?" : obs.ordinal)));
        renderDerivedBlock(title, node.derived);
        provChip(title, obs.provenance);
        item.appendChild(title);
        const sub = el("div", "cardsub");
        if (obs.agentId) sub.appendChild(el("span", "mono", String(obs.agentId)));
        if (obs.label) sub.appendChild(el("span", "", String(obs.label)));
        if (obs.startedAt) sub.appendChild(el("span", "dim", fmtTs(obs.startedAt)));
        item.appendChild(sub);
        nodes.appendChild(item);
    });
    if (!(payload.nodes || []).length) nodes.appendChild(el("li", "card empty", "no nodes observed"));
    body.appendChild(nodes);

    body.appendChild(el("h3", "", "agents"));
    body.appendChild(agentTree(payload.agents || []));
}

/** One agent card. Split out so the tree can nest it at any depth. */
function agentCard(agent) {
    const obs = agent.observed || {};
    const item = el("li", "card");
    const title = el("div", "cardhead");
    title.appendChild(el("span", "cardtitle",
        String((agent.derived && agent.derived.display) || agent.id || "")));
    renderDerivedBlock(title, agent.derived);
    provChip(title, obs.provenance);
    item.appendChild(title);
    const sub = el("div", "cardsub");
    sub.appendChild(el("span", "mono", String(agent.id || "")));
    if (obs.agentType) sub.appendChild(el("span", "", String(obs.agentType)));
    if (obs.model) sub.appendChild(el("span", "dim", String(obs.model)));
    if (obs.spawnDepth !== undefined) {
        sub.appendChild(el("span", "dim", "depth " + fmtInt(obs.spawnDepth)));
    }
    if (obs.root) sub.appendChild(el("span", "dim", "root " + truncate(obs.root, 32)));
    if ((obs.sessions || []).length > 1) {
        // The a2fc883c shape: one agent's records live in more than one
        // session file. R-48 makes `sessionId` never a grouping key, so this
        // is a fact about the agent, not two agents.
        sub.appendChild(chip("chip-plain", (obs.sessions || []).length + " sessions",
                             "this agent was observed across more than one session file"));
    }
    if ((agent.fragments || []).length > 1) {
        // R-48: fragments are chain-ordered by the module that owns the
        // split; the page reports how many there were, it does not stitch.
        sub.appendChild(chip("prov-derived", (agent.fragments || []).length + " fragments"));
    }
    if (agent.spawn && agent.spawn.recordUuid) {
        sub.appendChild(chip("chip-plain", "spawn recorded",
                             "identity is recordUuid + toolUseId; the line hint is a cache"));
    }
    item.appendChild(sub);
    return item;
}

/** How deep the panel nests before it says "deeper, not nested" and stops. */
const AGENT_TREE_DEPTH = 6;

/**
 * R-32's agent tree: cards nested by the harness's own spawn edge.
 *
 * `observed.parent` and `observed.root` are projected by `_agent_payload` and
 * they are *facts* — the edge the harness recorded — so drawing them as
 * containment is rendering, not deriving (GD-23 is untouched: no verdict, no
 * ordering, no liveness is inferred from the shape). A flat list beside a bare
 * `depth N` chip showed that a hierarchy exists without showing the hierarchy.
 *
 * A root is an agent whose `parent` is absent or points outside this run's set
 * — which is the ordinary case for the top of a run and for a fragment whose
 * parent was never ingested. Depth is capped and every id is drawn once,
 * because a corpus that lost a record can name a parent that names it back and
 * a page must not hang on one; anything left over is rendered flat and labelled
 * rather than dropped.
 */
function agentTree(agents) {
    const list = (agents || []).filter((agent) => agent && agent.id !== undefined);
    const byId = new Map();
    list.forEach((agent) => byId.set(String(agent.id), agent));
    const children = new Map();
    const roots = [];
    list.forEach((agent) => {
        const id = String(agent.id);
        const parent = String((agent.observed || {}).parent || "");
        if (parent && parent !== id && byId.has(parent)) {
            if (!children.has(parent)) children.set(parent, []);
            children.get(parent).push(agent);
        } else {
            roots.push(agent);
        }
    });
    const drawn = new Set();

    function branch(members, depth) {
        const ul = el("ul", depth ? "cards nested" : "cards");
        members.forEach((agent) => {
            const id = String(agent.id);
            if (drawn.has(id)) return;
            drawn.add(id);
            const item = agentCard(agent);
            const kids = children.get(id) || [];
            if (kids.length && depth < AGENT_TREE_DEPTH) {
                item.appendChild(branch(kids, depth + 1));
            } else if (kids.length) {
                item.appendChild(chip("prov-unknown", fmtInt(kids.length) +
                                      " deeper, not nested",
                                      "the tree is capped; these agents are listed flat below"));
            }
            ul.appendChild(item);
        });
        return ul;
    }

    const tree = branch(roots, 0);
    // Whatever the walk could not reach — a parent cycle, or a subtree below
    // the depth cap — is still shown, flat, and says why. Hiding an agent
    // because its edges are broken is the one outcome worse than a flat list.
    list.filter((agent) => !drawn.has(String(agent.id))).forEach((agent) => {
        drawn.add(String(agent.id));
        const item = agentCard(agent);
        item.classList.add("derived");
        item.appendChild(chip("prov-unknown", "not placed in the tree",
                              "its parent chain is cyclic or below the depth cap"));
        tree.appendChild(item);
    });
    if (!list.length) tree.appendChild(el("li", "card empty", "no agents joined"));
    return tree;
}

function renderSessionDetail(head, body, payload) {
    const doc = payload.sessionDoc || {};
    head.appendChild(el("h2", "", String(doc.id || payload.session || "")));
    const chips = el("div", "chips");
    chips.appendChild(chip("kind-" + (doc.kind === "live" ? "live" : "hist"),
                           String(doc.kind || "session")));
    if (doc.class) chips.appendChild(chip("chip-plain", String(doc.class)));
    if (doc.transcriptless) {
        chips.appendChild(chip("prov-unknown", "no transcript",
                               "observed in the registry, with nothing to show"));
    }
    if (doc.promotedTo) chips.appendChild(chip("prov-derived", "promoted to " + doc.promotedTo));
    head.appendChild(chips);

    const facts = el("div", "facts");
    if (doc.cwd) facts.appendChild(field("cwd", truncate(doc.cwd, 88)));
    if (doc.pid) facts.appendChild(field("pid", String(doc.pid)));
    if (doc.firstTs) facts.appendChild(field("first", fmtTs(doc.firstTs)));
    if (doc.lastTs) facts.appendChild(field("last", fmtTs(doc.lastTs)));
    if ((doc.sessionIds || []).length) {
        facts.appendChild(field("session ids", (doc.sessionIds || []).join(", ")));
    }
    if ((doc.slugs || []).length) facts.appendChild(field("slugs", (doc.slugs || []).join(", ")));
    body.appendChild(facts);

    // R-32 also names a per-session agent tree, and this panel cannot honestly
    // draw one: the read API has no agents-by-session route (`READ_ROUTES`),
    // and the only join key in hand is `agent.observed.sessions`, so building
    // the set here would be a client-side derivation of exactly the kind GD-23
    // reserves for the one server-side reducer. The gap is stated on the page
    // and belongs to sp-12 (the route) — a silently missing section reads as
    // "this session has no agents", which is a different and false claim.
    body.appendChild(el("h3", "", "agents"));
    body.appendChild(el("p", "hint",
        "not listed here: the read API has no agents-by-session route yet, and " +
        "joining agents to a session on this side would be a derivation the " +
        "page must not make. The run panel shows the same agents, nested."));

    body.appendChild(el("h3", "", "timeline"));
    // What the panel is showing, in its own words. "more beyond this page" with
    // no way to reach it is a labelled dead end; the window below is the way,
    // and when it is spent the line says that instead.
    const room = TIMELINE_MAX - state.timelineLimit;
    // `count` is what is on screen, not what the window asked for — a session
    // shorter than the window would otherwise be described by a number the
    // list does not contain.
    const meta = el("p", "hint",
        (payload.hasMore
            ? "showing the first " + fmtInt(payload.count) + " records of this session" +
              (room > 0 ? " — more on the server" : " — and the panel holds no more")
            : "showing all " + fmtInt(payload.count) + " records of this session") +
        " · bodies omitted (the corpus holds an 872 KB line)");
    body.appendChild(meta);
    const rows = el("ol", "timeline");
    (payload.records || []).forEach((record) => {
        const li = el("li", "trow");
        li.appendChild(el("span", "tline", "L" + String(record.lineNo === undefined
            ? "?" : record.lineNo)));
        li.appendChild(el("span", "tkind", String(record.type || record.collection || "")));
        li.appendChild(el("span", "dim", fmtTs(record.ts)));
        if (record.collection === "stream_meta") {
            // R-47's 12-type bucket table: everything that is not a
            // user/assistant/system/attachment record is positional metadata.
            // The ingest's own `render` field is projected by
            // `STREAM_META_FIELDS` and it is the row's word for "this is not a
            // turn" — reading it is the difference between a labelled meta row
            // and one that merely looks like every other meta row.
            li.appendChild(record.render === false
                ? chip("prov-derived", "meta · not rendered",
                       "the ingest marked this record non-renderable (R-47)")
                : chip("prov-derived", "meta"));
        }
        // Observed, not concluded: the record was superseded by a rewrite of
        // its file. A neutral class, because `st-*` is the reducer's liveness
        // vocabulary and nothing here is a liveness verdict.
        if (record.retracted === true) li.appendChild(chip("chip-warn", "retracted"));
        if (record.oversize === true) li.appendChild(chip("prov-derived", "oversize stub"));
        if (record.persistedOutput) li.appendChild(chip("chip-plain", "spilled output"));
        li.appendChild(el("span", "mono dim", truncate(record._id || "", 40)));
        rows.appendChild(li);
    });
    if (!(payload.records || []).length) rows.appendChild(el("li", "trow empty", "no records"));
    body.appendChild(rows);
    if (payload.hasMore === true) body.appendChild(timelineButton(room));
}

/**
 * "Widen the timeline by one page" — the other end of R-32's session view.
 *
 * The panel is polled, so this grows `state.timelineLimit` and lets the next
 * fetch return the wider window rather than stitching pages on this side (see
 * `state.timelineLimit`). The ceiling is `TIMELINE_MAX`, and a spent one is
 * said out loud on the button rather than left as a control that does nothing.
 */
function timelineButton(room) {
    const button = el("button", "pagebtn");
    button.setAttribute("type", "button");
    if (room <= 0) {
        button.disabled = true;
        setText(button, "window full · " + fmtInt(TIMELINE_MAX) + " records");
        button.setAttribute("title",
            "this panel holds at most " + fmtInt(TIMELINE_MAX) + " records of a " +
            "session; the rest are on the server and not requested");
        return button;
    }
    setText(button, "load more records");
    button.setAttribute("title",
        "widen the window by " + fmtInt(TIMELINE_PAGE) + " records — room for " +
        fmtInt(room) + " more");
    button.addEventListener("click", widenTimeline);
    return button;
}

/** One click: a wider window, one refetch, one paint. */
function widenTimeline() {
    if (state.sel.kind !== "session") return;
    const next = Math.min(TIMELINE_MAX, state.timelineLimit + TIMELINE_PAGE);
    if (next === state.timelineLimit) return;
    state.timelineLimit = next;
    refreshDetail().then(render);
}

/** The class table a row's state is looked up in — chosen by its own source.
 *
 * A harness row's `state` is the REDUCER's vocabulary
 * (`agents.NODE_STATES` = running | done | unknown); a legacy or asserted
 * row's is `legacy.STATES` (queued | running | done | failed | …). The two
 * overlap but neither contains the other, and reading a harness row out of the
 * legacy table sends `unknown` — the state of every resultless node in a
 * killed run, which is exactly what D-04 exists to surface — to `st-other`.
 */
function stateClassFor(node) {
    return node.source === "harness" ? NODE_STATE_CLASS : LEGACY_STATE_CLASS;
}

/** One `<ul>` of node cards — the same shape whatever produced the rows.
 *
 * `node.source` is the server's own word for where a row came from
 * (`harness` = observed, `asserted` = an events.jsonl line, `legacy` = the
 * unjoined fallback). It is rendered as a chip rather than a class name so the
 * distinction survives a screenshot.
 */
function nodeList(rows, emptyText) {
    const nodes = el("ul", "cards");
    (rows || []).forEach((node) => {
        const item = el("li", "card");
        const title = el("div", "cardhead");
        title.appendChild(el("span", "cardtitle",
            String(node.label || node.key || node.agentId || "")));
        title.appendChild(chip(classOf(stateClassFor(node), node.state), String(node.state || "")));
        if (node.source === "asserted") {
            item.classList.add("derived");
            title.appendChild(chip("prov-asserted", "asserted",
                                   "written by an agent into events.jsonl, not observed"));
        } else if (node.source === "harness") {
            title.appendChild(chip("prov-harness", "harness",
                                   "derived from the run's journal and snapshot"));
        }
        if (node.derivedFromLegacy === true) {
            item.classList.add("derived");
            title.appendChild(chip("prov-derived", "derived",
                                   node.relabel ? String(node.relabel) : ""));
        }
        if (node.unconventional === true) title.appendChild(chip("prov-derived", "unnamed"));
        (node.flags || []).forEach((flag) => title.appendChild(chip("chip-plain", String(flag))));
        item.appendChild(title);
        const sub = el("div", "cardsub");
        sub.appendChild(el("span", "dim", String(node.plan || "") + " · " + String(node.stage || "")));
        if (node.agentId) sub.appendChild(el("span", "mono", String(node.agentId)));
        // The occupancy reading, absolute and alone: no bar, no percentage, no
        // "of 200k" fallback — 8932 is the number-only surface (the gauge and
        // its declared denominator are 8931's). A row with no reading renders
        // NOTHING here rather than a zero or a dash, which is the same
        // absent-vs-zero rule the wire itself follows: the readings that exist
        // are visible and the ones that do not are not claimed.
        const ctx = ctxOf(node);
        if (ctx) {
            const meter = el("span", "mono", "ctx " + fmtInt(ctx.used));
            meter.setAttribute("title", ctxTitle(ctx));
            sub.appendChild(meter);
        }
        // Display only: `lastToolSummary` is truncated by the harness itself,
        // so nothing here or downstream may parse a marker out of it.
        if (node.detail) sub.appendChild(el("span", "dim", truncate(node.detail, DETAIL_CHARS)));
        item.appendChild(sub);
        nodes.appendChild(item);
    });
    if (!(rows || []).length) nodes.appendChild(el("li", "card empty", String(emptyText)));
    return nodes;
}

function renderTaskDetail(head, body, payload) {
    head.appendChild(el("h2", "", String(payload.task || "")));
    const chips = el("div", "chips");
    chips.appendChild(chip("kind-" + (payload.kind === "run" ? "run" : "plan"),
                           String(payload.kind || "task")));
    chips.appendChild(chip("prov-legacy", "legacy",
                           "reduced from events.jsonl under GD-14's rules"));
    if (payload.archive) {
        // GD-14: the archive label is derived, never a constant.
        chips.appendChild(chip(payload.archive.state === "present" ? "chip-plain" : "prov-derived",
                               String(payload.archive.label || payload.archive.state),
                               payload.archive.path || ""));
    }
    if (payload.runId) chips.appendChild(chip("chip-plain", String(payload.runId)));
    head.appendChild(chips);
    head.appendChild(el("div", "rollup-line",
        "tokens  " + fmtTokens(rollupList(payload.tokens))));

    body.appendChild(el("h3", "", "plans"));
    const plans = el("ul", "cards");
    Object.keys(payload.plans || {}).sort().forEach((name) => {
        const plan = payload.plans[name];
        const item = el("li", "card");
        const title = el("div", "cardhead");
        title.appendChild(el("span", "cardtitle", String(plan.plan || name)));
        // The badge is the legacy reducer's, verbatim. A `failed` that survived
        // R-58's re-label is a genuine observed failure and keeps it (D13); a
        // fabricated one has already been re-labelled "closed — no verdict"
        // server-side, and the relabel travels with the row.
        title.appendChild(chip(classOf(LEGACY_STATE_CLASS, plan.badge),
                               String(plan.label || plan.badge || "")));
        if (plan.derivedFromLegacy === true) {
            item.classList.add("derived");
            title.appendChild(chip("prov-derived", "derived",
                                   plan.relabel ? String(plan.relabel) : "re-labelled at read time"));
        }
        if ((plan.conflictingTerminals || []).length) {
            title.appendChild(chip("prov-derived",
                (plan.conflictingTerminals || []).length + " conflicting terminals",
                "last-event-wins in file order (SD-4)"));
        }
        if (plan.duplicates) {
            title.appendChild(chip("prov-derived", fmtInt(plan.duplicates) + " duplicates"));
        }
        item.appendChild(title);
        if (plan.title || plan.detail || plan.agentDetail) {
            const sub = el("div", "cardsub");
            if (plan.title) sub.appendChild(el("span", "", truncate(plan.title, 120)));
            if (plan.detail) sub.appendChild(el("span", "dim", truncate(plan.detail, DETAIL_CHARS)));
            if (plan.agentDetail) {
                sub.appendChild(el("span", "dim", truncate(plan.agentDetail, DETAIL_CHARS)));
            }
            item.appendChild(sub);
        }
        plans.appendChild(item);
    });
    if (!Object.keys(payload.plans || {}).length) {
        plans.appendChild(el("li", "card empty", "plan only — this folder never ran"));
    }
    body.appendChild(plans);

    // D-04/GD-D12: once the server has joined this folder to the harness run
    // its `wf_dir` names, `nodes` IS the harness set and the events.jsonl rows
    // arrive separately as `assertedNodes`. The two lists are rendered by one
    // function and told apart by a chip, because a page that renders an
    // assertion and an observation identically is a page that teaches its
    // reader the wrong thing about where the numbers came from.
    const harness = payload.harness || null;
    body.appendChild(el("h3", "", harness ? "nodes — harness" : "nodes"));
    if (harness) {
        const src = el("div", "cardsub");
        src.appendChild(el("span", "dim",
            "derived from the run's own journal and snapshot"));
        if (harness.wfDir) src.appendChild(el("span", "mono", String(harness.wfDir)));
        body.appendChild(src);
    }
    body.appendChild(nodeList(payload.nodes || [], "no nodes"));

    const asserted = payload.assertedNodes || [];
    if (asserted.length) {
        body.appendChild(el("h3", "", "asserted — events.jsonl"));
        const why = el("div", "cardsub");
        why.appendChild(el("span", "dim",
            "kept as annotation: these lines were written by agents, not observed"));
        body.appendChild(why);
        body.appendChild(nodeList(asserted, "no asserted nodes"));
    }

    if ((payload.notes || []).length) {
        body.appendChild(el("h3", "", "notes"));
        const notes = el("ul", "notes");
        (payload.notes || []).forEach((line) => {
            notes.appendChild(el("li", "note", truncate(line, 240)));
        });
        body.appendChild(notes);
    }
}

// ------------------------------------------------- 10. the log (capped, once)

function pushLog(entry) {
    state.pending.push(entry);
    state.logSeen += 1;
    // The cap applies to the queue too: a replay burst larger than the log
    // cannot be allowed to sit in memory waiting for a paint that will throw
    // most of it away. What it discards is counted apart from what the DOM cap
    // evicts — they are different ends of the list and different facts.
    if (state.pending.length > LOG_MAX) {
        state.queueDropped += state.pending.length - LOG_MAX;
        state.pending = state.pending.slice(-LOG_MAX);
    }
}

function logRow(entry) {
    const record = entry.record || {};
    const li = el("li", "logrow");
    // The animation class is attached here and only here, for `live === true`
    // frames only: a replayed or backfilled burst paints once, silently (R-55).
    paint(li, entry.live);
    li.appendChild(el("span", "lts", fmtTs(record.ts)));
    li.appendChild(el("span", "lseq", String(entry.stream) + "#" + fmtInt(entry.seq)));
    li.appendChild(el("span", "lkind", String(record.kind || "")));
    const prov = el("span", "lprov " + classOf(PROV_CLASS, record.provenance));
    prov.appendChild(text(String(record.provenance || "unknown")));
    li.appendChild(prov);
    li.appendChild(el("span", "lref", refSummary(record.ref)));
    li.appendChild(el("span", "ldata", dataSummary(record)));
    if (entry.live !== true) li.appendChild(chip("chip-plain", "replay"));
    return li;
}

/**
 * Whether a scroll box is parked at its live edge.
 *
 * Read BEFORE the append, because appending is what moves the edge. The `||`
 * arm is the empty/unscrollable box (and any DOM without scroll metrics): a
 * list that does not overflow is trivially at its edge, and the alternative —
 * treating "no numbers" as "the operator scrolled away" — would silently
 * disable the follow for the whole first screen.
 */
function atLiveEdge(list) {
    const top = Number(list.scrollTop);
    const view = Number(list.clientHeight);
    const full = Number(list.scrollHeight);
    if (!Number.isFinite(top) || !Number.isFinite(view) || !Number.isFinite(full)) {
        return true;
    }
    return top + view >= full - PIN_SLACK_PX;
}

function flushLog() {
    const list = dom.log;
    if (!list) return;
    if (state.pending.length) {
        // Captured before the mutation: after it, every box looks scrolled away.
        const following = atLiveEdge(list);
        const fragment = document.createDocumentFragment();
        state.pending.forEach((entry) => fragment.appendChild(logRow(entry)));
        state.pending = [];
        list.appendChild(fragment);
        // `#log` is the LIVE tail and this cap governs it alone. Loaded history
        // lives in `#older`, with its own budget, so a page of it can never be
        // paid for out of the tail's rows (and the tail can never evict it).
        const heightBeforeTrim = Number(list.scrollHeight);
        while (list.childElementCount > LOG_MAX) {
            list.removeChild(list.firstChild);
            state.trimmed += 1;
        }
        if (following) {
            // The live edge, followed. Without this the box overflows inside
            // the first replay burst (~17 rows of a 400-row cap) and every
            // frame after that — every `live:true` row the `.fresh` flash
            // exists to point at — is painted below the fold, permanently.
            list.scrollTop = list.scrollHeight;
        } else {
            // The operator scrolled away, so their rows stay put. The trim
            // removes content ABOVE the viewport, which drags everything up by
            // exactly the height it freed; giving that back is what keeps the
            // rows they are reading under their eyes.
            const shift = heightBeforeTrim - Number(list.scrollHeight);
            if (Number.isFinite(shift) && shift > 0) {
                list.scrollTop = Math.max(0, Number(list.scrollTop) - shift);
            }
        }
    }
    // Numbers that close: seen = shown + trimmed + dropped-before-paint, with
    // "shown" read off the DOM rather than accumulated, so the line cannot
    // claim more rows than the list holds.
    const parts = [fmtInt(state.logSeen) + " seen",
                   fmtInt(list.childElementCount) + " shown"];
    if (state.trimmed) parts.push(fmtInt(state.trimmed) + " older dropped");
    if (state.queueDropped) {
        parts.push(fmtInt(state.queueDropped) + " dropped before paint");
    }
    if (state.olderShown) {
        parts.push(fmtInt(state.olderShown) + " older loaded of " + fmtInt(OLDER_MAX));
    }
    if (state.duplicates) parts.push(fmtInt(state.duplicates) + " duplicate frames ignored");
    // The other half of that number: what a resume actually re-delivered.
    if (state.backfilled) parts.push(fmtInt(state.backfilled) + " re-sent on resume");
    // The keepalive marker: `onmessage` never fires for a protocol pong, so a
    // quiet run and a dead socket look identical without this frame's ts.
    if (state.wire.lastTick) parts.push("keepalive " + fmtTs(state.wire.lastTick));
    if (state.wire.window) parts.push("window " + fmtInt(state.wire.window));
    setText(dom.logMeta, parts.join(" · "));
    renderOlder();
}

/** What is left of the CURRENT stream's history budget. Zero: do not fetch. */
function olderRoom() {
    return Math.max(0, OLDER_MAX - state.olderShown);
}

/**
 * Give the history list to `stream`, discarding whatever it held.
 *
 * `OLDER_MAX` is one stream's budget, not the tab's. Spent globally it was
 * dead again by a plainer route than the one attempt 2 closed — two clicks on
 * any run disabled load-older for every run, forever — and the rows themselves
 * were a lie in two directions: one stream's history sitting above another
 * stream's live tail, under a button whose "full · 400 rows" is true of the
 * list and false of the stream it names.
 *
 * Discarding on a switch is the semantically correct move rather than a
 * compromise: the button already says "this walks `<stream>` only", the anchor
 * it walks from is per-stream, and the rows are re-fetchable from the same
 * anchor. The de-dup set is deliberately NOT cleared — those records were
 * genuinely delivered once, and re-painting them into the tail on a later
 * backfill is the duplicate `state.seen` exists to stop.
 */
function resetOlder(stream) {
    if (state.olderStream === stream) return;
    state.olderStream = stream;
    state.olderShown = 0;
    clear(dom.older);
    if (dom.older) dom.older.scrollTop = 0;
    // The failure slot goes with the rows it described (see `clearOlderError`).
    clearOlderError();
}

/**
 * Drop a `load older` failure line that no longer describes anything.
 *
 * Every other slot is cleared by the arm that owns it, on the success that
 * contradicts it. This one's owner is a button the operator can be locked out
 * of — it hides the moment the selected stream is not truncated — so a failed
 * click pinned its line to the notice bar for the life of the tab, in the box
 * whose stated contract is that it empties itself when the trouble ends.
 *
 * `renderNotices` has already run by the time `renderOlder` is reached in a
 * paint, so it is re-run here; it is a text change-guard, so it costs nothing
 * unless the line really went away.
 */
function clearOlderError() {
    if (!state.errors["load older"]) return;
    setError("load older", null);
    renderNotices();
}

/**
 * The load-older affordance, revealed only by a frame that declared a cut.
 *
 * It is labelled with the stream it will walk, because the log interleaves
 * *every* stream (`onEvent` filters nothing) while a truncation, an anchor and
 * a backwards page are all per-stream. An unlabelled button here would silently
 * mean "one of the streams you are looking at" and would vanish whenever the
 * truncated stream is not the selected one.
 *
 * A spent budget disables it and says so. The alternative — a button that
 * still fetches and then paints nothing — costs a whole-stream `read_all` on
 * the server per click and tells the operator a number that is only a count of
 * their own clicks.
 *
 * The list under it belongs to that same one stream (`resetOlder`), so a
 * selection change empties it: history above the tail must be the history OF
 * the tail, and a per-tab budget spent on one run would kill the affordance for
 * every other run for the life of the tab.
 */
function renderOlder() {
    const stream = currentStream();
    // The list belongs to whichever stream the button would walk; if that
    // moved, the rows it holds are another stream's and go.
    resetOlder(stream);
    const history = dom.older;
    // Hidden unless the list holds rows AND they belong to this stream. The
    // reset above already guarantees the second half; stating it anyway is what
    // makes a stale list impossible to render at all, rather than impossible
    // as long as every future writer remembers to go through `resetOlder`.
    if (history) {
        history.hidden = !state.olderShown || state.olderStream !== stream;
        // The list's own name says whose history it is. The document ships a
        // generic label because markup cannot know; the page can, and "older
        // records" sitting directly above a live tail is exactly the place a
        // reader assumes the two belong to the same thing.
        history.setAttribute("aria-label", state.olderShown && stream
            ? "older records of " + stream + ", loaded on demand"
            : "older records, loaded on demand");
    }
    const button = dom.olderBtn;
    if (!button) return;
    const anchor = stream ? state.anchors[stream] : null;
    const available = !!(anchor && anchor.truncated && anchor.oldest !== null &&
                         anchor.oldest !== undefined);
    if (!available) {
        button.hidden = true;
        // Its failure line goes with it: the affordance that would clear it is
        // the one being taken away.
        clearOlderError();
        return;
    }
    button.hidden = false;
    const room = olderRoom();
    button.disabled = !room || loadingOlder;
    if (!room) {
        // Named, because the budget is this stream's: "full" said of a list
        // that belongs to something else is the sentence this whole arrangement
        // exists to stop the page from saying.
        setText(button, "history full · " + fmtInt(state.olderShown) + " rows of " +
                truncate(stream, 40));
        button.setAttribute("title",
            "the history list holds its full " + fmtInt(OLDER_MAX) + " rows for " +
            stream + "; nothing more is fetched, and the live tail below is untouched");
        return;
    }
    setText(button, "load older · " + truncate(stream, 40));
    button.setAttribute("title",
        "the log interleaves every stream; this walks " + stream + " only — " +
        "records older than seq " + fmtInt(anchor.oldest) +
        " were cut from its replay window · room for " + fmtInt(room) + " more rows");
}

/**
 * Which stream the log's backwards walk belongs to.
 *
 * The selection wins; with nothing selected this falls back to the run the
 * *server* named at the handshake, which may by now have finished — the page
 * cannot know, because `currentRun` is published on `hello` alone and picking
 * a fresher one here would be the page deciding which run is current (GD-23
 * says the server decides). That is survivable precisely because nothing is
 * implied by it: the button prints the stream it will walk, and the row that
 * carries the `current` marker says in its title that the marker is a
 * newest-written selection rather than a liveness verdict.
 */
function currentStream() {
    if (state.sel.kind === "run" && state.sel.id) return "run:" + state.sel.id;
    if (state.currentRun) return state.currentRun;
    return null;
}

/** One page is in flight. A keyboard repeat must not race its own anchor. */
let loadingOlder = false;

/**
 * Walk one page backwards through what the replay window cut off
 * (`/api/events?stream=&before=`) and paint it as history — into `#older`,
 * above the live tail, `live:false` and therefore never animated.
 *
 * **History has its own budget, and an exhausted one does not fetch.**
 * `OLDER_MAX` rows of it live in their own list, so a page of history is never
 * paid for out of the tail's `LOG_MAX` (the arrangement that made this button
 * a no-op: the anchors only ever exist once the log is already full) and the
 * tail's cap never evicts history it did not put there. Only what fits is
 * requested — `limit` is clamped to the room left — so nothing is fetched and
 * then discarded, and when the room is gone the click costs nothing at all:
 * `h_events` re-reads the whole stream file per call, in the process that also
 * serves `/ws`.
 *
 * The anchor then moves to `page.oldest`, which is exactly what was painted,
 * so the next click resumes there with no gap.
 */
async function loadOlder() {
    if (loadingOlder) return;
    const stream = currentStream();
    const anchor = stream ? state.anchors[stream] : null;
    if (!stream || !anchor || anchor.oldest === null || anchor.oldest === undefined) return;
    // Before the budget is read, not after: the room left is THIS stream's, and
    // a list still holding another stream's rows would report a full one. (A
    // no-op on the ordinary path — the render already handed the list over —
    // but this is the function that grows `state.olderShown`, so it is the
    // function that must be sure whose rows it is growing.)
    resetOlder(stream);
    const room = olderRoom();
    if (!room) {
        // Never a fetch that cannot paint: no request, no fabricated counter.
        setError("load older", "the history list already holds its full " +
                 fmtInt(OLDER_MAX) + " rows for " + stream + "; nothing was requested");
        render();
        return;
    }
    loadingOlder = true;
    const button = dom.olderBtn;
    if (button) button.disabled = true;
    try {
        const page = await getJson("/api/events", {
            stream: stream, before: anchor.oldest, limit: Math.min(OLDER_PAGE, room),
        });
        // A record the socket already delivered is not painted twice: the
        // de-dup set is the same one the wire uses, keyed the same way.
        const rows = (page.records || []).filter(
            (record) => !state.seen.has(cursorKey(stream, record.seq)));
        const list = dom.older;
        const fragment = document.createDocumentFragment();
        rows.forEach((record) => {
            remember(cursorKey(stream, record.seq));
            noteTokens(stream, record);
            // live:false by construction — this is history fetched on demand.
            fragment.appendChild(logRow({
                stream: stream, seq: Number(record.seq) || 0, live: false, record: record,
            }));
        });
        // Prepended, because `h_events`' backwards arm returns `older[-limit:]`
        // ascending: this page is older than everything already in the list.
        if (list) {
            list.insertBefore(fragment, list.firstChild);
            // …and shown. A prepend lands ABOVE the scroll position, so without
            // this the operator clicks "load older" and sees the exact rows
            // they were already looking at. The top of the box is where the
            // oldest thing just loaded is, which is what the click asked for.
            list.scrollTop = 0;
        }
        state.olderShown += rows.length;
        anchor.oldest = page.oldest !== null && page.oldest !== undefined
            ? page.oldest : anchor.oldest;
        anchor.truncated = page.hasOlder === true;
        setError("load older", null);
    } catch (err) {
        setError("load older", err.message);
    } finally {
        loadingOlder = false;
        render();
    }
}

// ------------------------------------------------------ 11. model refresh

/**
 * The live cadence: sessions + the current selection.
 *
 * `/api/tasks` is deliberately NOT here — see `refreshTasks` and `TASKS_MS`.
 * Each arm clears its own error slot on success, so a blip that has resolved
 * stops being displayed instead of accumulating (M1).
 */
async function refreshModel() {
    const jobs = [
        getJson("/api/sessions").then((body) => {
            state.sessions = (body && body.sessions) || [];
            setError("sessions", null);
        }, (err) => { setError("sessions", err.message); }),
        refreshDetail(),
    ];
    await Promise.all(jobs);
    render();
}

/**
 * The ONE `/api/tasks` request, shared by everyone who needs the list.
 *
 * Two callers can want it at once — the slow poll and a direct `#task/<id>`
 * load's cold start, both of which run at boot — and that route is the most
 * expensive one this server has (it re-reads and re-reduces every legacy
 * folder from disk, in the process that also serves `/ws`). A second
 * concurrent caller joins the request in flight instead of starting another.
 */
let tasksInFlight = null;

function fetchTasks() {
    if (tasksInFlight) return tasksInFlight;
    const job = getJson("/api/tasks").then((body) => {
        state.tasks = (body && body.tasks) || [];
        // A 200 with an empty list and a note is the server saying WHY it is
        // empty — no root configured, or a configured root that is not there —
        // and an empty panel with a blank count says nothing at all. The note
        // goes through the same per-source slot as a fetch failure, so a panel
        // that has forgotten its history explains itself instead of looking like
        // history that ended (UI-13). The trailing "|| null" clears the slot on
        // the ordinary answer, which carries no note; the slot is a plain
        // string, so no template literal is involved.
        setError("tasks", (body && body.note) || null);
    }, (err) => {
        setError("tasks", err.message);
    });
    tasksInFlight = job.then(() => { tasksInFlight = null; });
    return tasksInFlight;
}

/** The slow cadence: the legacy task folders (`TASKS_MS`). */
async function refreshTasks() {
    await fetchTasks();
    // A selected task's payload IS a row of that list, so the panel is
    // recomputed from what we just fetched rather than by a second request.
    if (state.sel.kind === "task") await refreshDetail();
    render();
}

async function refreshDetail() {
    const kind = state.sel.kind;
    const id = state.sel.id;
    if (!kind || !id) { setDetail(null, null); return; }
    try {
        let payload = null;
        if (kind === "run") payload = await getJson("/api/run/graph", { run: id });
        else if (kind === "session") {
            // The window the panel means to show, not a fixed first page.
            payload = await getJson("/api/session/timeline",
                                    { session: id, limit: state.timelineLimit, meta: 1 });
        } else if (kind === "task") {
            // The task payload is a row of the list `refreshTasks` already
            // holds. Re-fetching `/api/tasks` here would re-read and re-reduce
            // every legacy folder from disk on the live cadence — the most
            // expensive route on the server, called for data we have. The
            // call below is the cold-start arm only: a direct `#task/<id>`
            // load that lands before the first list has arrived, and it joins
            // the poll's request rather than issuing a second one.
            payload = state.tasks.filter((task) => task.task === id)[0] || null;
            if (!payload) {
                await fetchTasks();
                payload = state.tasks.filter((task) => task.task === id)[0] || null;
            }
            if (!payload) throw new Error("no task folder " + id);
        }
        // A slower answer for a selection the user already left must not paint.
        if (state.sel.kind !== kind || state.sel.id !== id) return;
        setDetail(payload, null);
    } catch (err) {
        if (state.sel.kind !== kind || state.sel.id !== id) return;
        setDetail(null, err.message);
    }
}

async function refreshHealth() {
    try {
        state.health = await getJson("/health");
        setError("health", null);
    } catch (err) {
        // `/health` is the one open route; if even this fails the header's
        // "mirror —" needs a reason beside it, and it must disappear again the
        // moment a poll succeeds.
        state.health = null;
        setError("health", err.message);
    }
    render();
}

// ------------------------------------------------------------ 12. routing

function select(kind, id, options) {
    state.sel = { kind: kind, id: id };
    // The timeline window belongs to ONE session, like the history list belongs
    // to one stream: carried across a selection it would ask for a width the
    // operator chose for something else.
    state.timelineLimit = TIMELINE_PAGE;
    setDetail(null, null);
    if (options && options.replaceHash) {
        window.location.replace("#" + kind + "/" + encodeURIComponent(id));
    }
    refreshDetail().then(render);
    render();
}

function route() {
    const raw = window.location.hash.replace(/^#/, "");
    if (!raw) {
        state.sel = { kind: null, id: null };
        setDetail(null, null);
        render();
        return;
    }
    const cut = raw.indexOf("/");
    const kind = cut === -1 ? raw : raw.slice(0, cut);
    const id = cut === -1 ? "" : decodeURIComponent(raw.slice(cut + 1));
    if (["session", "run", "task"].indexOf(kind) === -1 || !id) {
        state.sel = { kind: null, id: null };
        setDetail(null, null);
        render();
        return;
    }
    if (state.sel.kind === kind && state.sel.id === id) return;
    select(kind, id, null);
}

// ------------------------------------------------------------- 13. boot

function boot() {
    bindDom();
    if (!TOKEN) {
        // Sticky, not a cycle error: no later success can make this untrue.
        state.bootError = "no per-boot token: this page must be served by the Touch " +
            "server (or opened with ?token=…) — every route but /health needs one";
    }
    if (dom.olderBtn) dom.olderBtn.addEventListener("click", loadOlder);
    window.addEventListener("hashchange", route);
    route();
    render();
    refreshModel();
    refreshTasks();
    refreshHealth();
    window.setInterval(refreshTasks, TASKS_MS);
    window.setInterval(refreshHealth, HEALTH_MS);
    connect();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
} else {
    boot();
}
