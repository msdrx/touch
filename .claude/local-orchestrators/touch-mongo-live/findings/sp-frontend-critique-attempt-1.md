# sp-frontend — adversarial critique, attempt 1

**Verdict: REJECTED** — 1 major, 4 minor, 8 nits. `depth: in-scope`,
`critical_defect: false`.

Reviewed (full content; the tree is untracked so `git diff` is empty for all
four):

- `/home/laniakea/Projects/touch/touch-visual/index.html` (99 lines)
- `/home/laniakea/Projects/touch/touch-visual/app.js` (1463 lines)
- `/home/laniakea/Projects/touch/touch-visual/style.css` (340 lines)
- `/home/laniakea/Projects/touch/tests/test_touch_frontend.py` (545 lines)

Against `plan/touch-mongo-live-subplans.md` §sp-13, amendment items R-55 /
GD-22 / GD-23 / GD-28, base items R-22 / R-32 (+ the R-30/R-31 wire contract
it restates), and `aggregator/server.py` as the counterparty.

## Method

Beyond reading, I ran two independent probes, because a suite made entirely of
static source guards cannot tell you whether the page *works*:

1. `node --check touch-visual/app.js` → clean.
2. A stdlib-free fake DOM + `vm` harness
   (`<scratchpad>/smoke.js`) that boots `app.js` with stubbed
   `document`/`window`/`fetch`/`WebSocket`, then drives a full
   `hello → replay event → mode → live events → anchors → tick → unknown` frame
   sequence plus a `load older` click against fixture-shaped API bodies. The
   page renders correctly end to end: the rollup shows the *latest* absolute
   record (300, not 100+300), only the two `live:true` rows carry `fresh`, the
   `<img src=x onerror=1>` payload lands as character data, the load-older
   button unhides only after the anchors arrive, and the loaded-older row is
   painted without the animation class. **The core R-55 properties hold under
   execution, not just under grep.** Two of the findings below were confirmed
   with that harness rather than argued.

Ownership is clean: `find -newermt` shows only the four owned files touched
(20:40–20:51), `HEAD` is still `579446e` (no commit), GD-21 holds
(`import tests.test_touch_frontend` succeeds with a `pymongo` import blocker
installed in `sys.meta_path`). No control verb, no `POST/PUT/DELETE/PATCH`, no
form, `CONTROL_ROUTES == {}`. GD-22 holds: the mirror state is a header label
and nothing on the page is conditional on it.

---

## MAJOR

### M1 — The notice/error surface is append-only: the page permanently displays failures that have already resolved

`touch-visual/app.js:1357-1405` (`refreshModel`, `refreshDetail`,
`refreshHealth`), `app.js:582-586` (`note`), `app.js:806-819`
(`renderNotices`).

`state.error` is assigned in every failure arm (`app.js:1361`, `1364`, `1348`)
and cleared in exactly **one** place — the success path of `loadOlder`
(`app.js:1346`). No success arm of `refreshModel`, `refreshDetail` or
`refreshHealth` clears it. `state.wire.notices` is likewise never reset — not
on reconnect, not on success, not by any affordance; `note()` only dedups
identical text and caps the list at 8.

Confirmed by execution. With `/api/sessions` failing once (503) and succeeding
on every later poll, the harness prints:

```
sessions: [ '1234-1700000000liveorchestrator…', '9-1historicalno transcript' ]   <- loaded fine
notice hidden: false | sessions: transient blip…                                  <- still shown
```

The sidebar is populated from a *successful* refetch while the banner still
claims sessions failed. There is no dismiss, no expiry and no clear-on-success,
so one network blip pins a false statement to the top of the page for the life
of the tab, and stale handshake notices ("resumed from the last position…",
"?from= was not a seq…") survive every subsequent reconnect.

This is the failure class this whole plan is about, pointed at the page itself:
R-32/D13 and GD-23 exist so the UI states only what the server actually
concluded, and a permanently-displayed resolved error is the same lie as a
fabricated badge — with the added twist that `loadOlder` succeeding clears a
`sessions:` error it has nothing to do with (`app.js:1346`), so the banner is
not even consistently wrong.

**Fix.** Make both surfaces reflect the current cycle:

- give `state.error` a clear on every success arm — simplest is to reset
  `state.error = null` at the top of `refreshModel()` before the `Promise.all`
  and let the failing arms re-set it (keeping the boot no-token message in a
  separate, sticky `state.bootError` that `renderNotices` always prepends);
- scope notices to the connection: clear `state.wire.notices` in `connect()`
  (or in `sock.onopen`) so a handshake report describes *this* handshake, and
  drop wire notices that a later frame has superseded (see m1 below);
- add one guard to `tests/test_touch_frontend.py` asserting the clear exists —
  e.g. `check("state.error = null" in slice_fn(CODE, "async function refreshModel", "\n}"))`
  and `check("notices = []" in slice_fn(CODE, "function connect", "\n}"))` — so
  the property is defended, not just fixed.

---

## MINOR

### m1 — `resync()` sends `state.delivered`, which the server refuses on the normal path — manufacturing the permanent notice from M1

`touch-visual/app.js:565-575`, against `aggregator/server.py:1625-1641`
(`WsSession._advance`) and `server.py:1740-1815` (`subscribe`).

The file's own header (`app.js:64-69`) and the suite
(`test_touch_frontend.py:427`) are emphatic that "max seq I received" must
never be a resume position. `resync()` then sends exactly that, every
`RESYNC_MS`:

```js
Object.keys(state.delivered).forEach((stream) => { cursors[stream] = state.delivered[stream]; });
socket.send(JSON.stringify({ type: "subscribe", cursors: cursors }));
```

Server-side, `_advance` clamps the *published* cursor to `pending_floor - 1`
whenever the token coalescer is holding a record, while frames after the held
one are still sent. So for a stream with token traffic the two diverge for the
duration of the hold: server publishes seq 11 while the client has *received*
seq 13. `subscribe` then hits
`if held is not None and seq > _int_or(held, 0)` → `rejected` with
`"ahead of this socket at seq 11"`, `onSubscribed` (`app.js:701-708`) turns
that into a `note()`, and by M1 it stays on screen forever. The safety property
is intact (`state.resume` is only ever adopted from the ack), but the page
generates its own alarming, permanent, incorrect notice on the ordinary path.

**Fix.** Send the server's own published position, which is what makes the ack
a no-op-with-fresh-truth the comment at `app.js:525-529` describes:

```js
const cursors = {};
Object.keys(state.resume).forEach((s) => { cursors[s] = state.resume[s]; });
```

If the intent is genuinely to *ask for a rewind* when a frame was lost, send
`Math.min(state.resume[s], state.delivered[s] ?? state.resume[s])` — never a
value above the published cursor — and classify an `"ahead of this socket"`
rejection as expected rather than notice-worthy.

### m2 — `loadOlder` evicts the **newest** rows and counts them as "older dropped"

`touch-visual/app.js:1334-1342`, with the counter rendered at `app.js:1272`.

```js
list.insertBefore(fragment, list.firstChild);
state.logCount += added;
while (list.childElementCount > LOG_MAX) {
    list.removeChild(list.lastChild);
    state.dropped += 1;
}
```

`OLDER_PAGE` is 200 and `LOG_MAX` is 400, so one click on a full log silently
deletes up to 200 rows off the **live tail** — the thing the operator is
watching — and then `flushLog` labels them `"N older dropped"`. Subsequent live
frames append below the gap, so the list is no longer contiguous and nothing
says so. (`flushLog`'s own trim at `app.js:1266-1269` drops `firstChild`, i.e.
genuinely older rows, so the two trims disagree about which end is expendable.)

**Fix.** Either cap the prepend so the combined list never exceeds `LOG_MAX`
(prepend `Math.min(rows.length, LOG_MAX - list.childElementCount)` and report
the remainder as "not shown"), or keep the eviction but count it separately —
`state.trimmedNewer` with its own label — so the meta line stops mislabelling
which end was cut.

### m3 — a truncating *display* formatter is used as the token-rollup *identity* key

`touch-visual/app.js:325-328` (`refSummary`), used as an identity at
`app.js:420` (`noteTokens`) and `app.js:1251` (display).

```js
return Object.keys(ref).sort().map((k) => k + "=" + truncate(ref[k], 48)).join(" ");
...
const key = stream + "|" + refSummary(record.ref);
```

Two distinct refs whose field values agree in their first 48 characters collapse
to one map entry, so the later record silently *replaces* the earlier one and
the rollup under-counts — the exact "silently low counters" failure R-55 pairs
with the absolute-token model. The GD-11 ref grammar makes this reachable:
`orchAgent.name`/`root` and `runNode.key` are free-form strings, and
`legacy:<task>:<id8>` grows with the folder name. The `" "`/`"="` join is also
not injective (`{a:"b c"}` vs `{a:"b", c:""}`), and it diverges from the
server's own key for the same concept — `TokenCoalescer.key_of`
(`aggregator/server.py:1268-1276`) joins untruncated `name=value` pairs with
`"|"`.

**Fix.** Split the two jobs: keep `refSummary` for display and add

```js
function refKey(ref) {
    if (!ref || typeof ref !== "object") return "";
    return Object.keys(ref).sort().map((k) => k + "=" + String(ref[k])).join("|");
}
```

used only by `noteTokens`. That is byte-identical to the server's coalescer key,
which is the property worth asserting in the suite.

### m4 — a selected task refetches `/api/tasks` in the same cycle that just fetched it, and `/api/tasks` is the most expensive route on the server

`touch-visual/app.js:1381-1385`:

```js
} else if (kind === "task") {
    const body = await getJson("/api/tasks");
    state.tasks = (body && body.tasks) || [];
    payload = state.tasks.filter((task) => task.task === id)[0] || null;
```

`refreshModel` (`app.js:1358-1366`) already fetched `/api/tasks` into
`state.tasks` and awaits `refreshDetail()` in the same `Promise.all`, so a
selected task doubles the call every refresh cycle. `h_tasks`
(`aggregator/server.py:2221-2236`) is not served from the in-memory reduction —
it runs `legacy_mod.scan(root)`, re-reading and re-reducing every
`.claude/local-orchestrators/*/events.jsonl` from disk (**4.4 MB / ~11 000
lines** in this repo today). With `queueRefresh()` firing off live events at
`REFRESH_MS = 2000`, an active run makes the page re-parse ~22 000 JSON lines
per two seconds on the same single process that serves `/ws`.

**Fix.** Use what you already have — `payload = state.tasks.filter(…)[0]` with
no second fetch (`refreshModel` resolves `/api/tasks` in the same
`Promise.all`; if ordering matters, hoist the tasks fetch and pass the result
into `refreshDetail`). Separately, consider polling `/api/tasks` on its own
slower cadence than the run/session data — legacy folders are history and do
not change at 0.5 Hz.

---

## NITS

- **n1** — `test_touch_frontend.py:472`: `check("180" not in CODE, …)` is a
  substring test on the whole file, so it will fire on any future `1800`,
  `180000` or an `x180` identifier. Anchor it: `not re.search(r"\b180\b", CODE)`.
- **n2** — `test_touch_frontend.py:126-132`: `slice_fn` is name-prefix anchored
  and takes the *first* match, so `slice_fn(CODE, "function rollup", "\n}")`
  would silently slice `rollupList` if the two were reordered. Anchor on
  `"function rollup("` (with the paren) where a prefix collision exists.
- **n3** — `touch-visual/index.html:18`: the doc comment spells
  `__TOUCH_TOKEN__` literally, and `inject_token`
  (`aggregator/server.py:800-801`) replaces *every* occurrence — so the served
  page carries the raw per-boot token a third time, inside a comment that then
  reads "`<real-token>` is `server.inject_token`'s placeholder". Harmless but
  untidy; break the word the way `app.js:154` does.
- **n4** — `touch-visual/index.html:13`: "nothing here is a template string an
  id, a name or a detail is interpolated into" — missing "that".
- **n5** — `touch-visual/index.html:69`: `aria-live="polite"` sits on `#detail`,
  whose two children are fully cleared and rebuilt by `renderDetail` on every
  paint; a screen reader re-announces the entire panel every refresh. Move the
  live region to a small status line, or rebuild only what changed.
- **n6** — `app.js:1283-1301`: `renderOlder`/`loadOlder` are scoped to
  `currentStream()` while the log interleaves **all** streams
  (`onEvent`/`pushLog` never filter). The button therefore walks one stream's
  history and prepends it into a mixed list, and it is hidden whenever the
  truncated stream is not the selected one. Either filter the log to the current
  stream or label the button with the stream it will walk.
- **n7** — `app.js:172-174`: the `window.TOUCH_TOKEN` arm is unreachable given
  this document always carries the placeholder (`inject_token` takes the
  placeholder branch first). Defensible as defence-in-depth; worth a word in the
  comment saying it is for a page that predates the placeholder, not a live arm.
- **n8** — cross-file drift worth telling sp-12/sp-15 about (not fixable here):
  `aggregator/server.py:2273-2276` claims "the page renders the preview with its
  own escape-first mini renderer", but this page reads neither `/api/artifacts`
  nor `/file`, and the suite's route list
  (`test_touch_frontend.py:255-256`) does not include them, so nothing catches
  the drift.

---

## What I attacked and could NOT break

Recorded so the next attempt does not re-litigate settled ground:

- **Escape-first (GD-20).** No markup sink exists at all — the only text sink is
  `createTextNode`/`textContent`, the only attribute sink `setAttribute`, every
  class comes from `classOf()` against a literal whitelist, `href` is always
  `"#" + kind + "/" + encodeURIComponent(id)` (unreachable by `javascript:`).
  The harness confirmed an `onerror=` payload renders as character data.
- **R-55 animation rule.** `classList.add(LIVE_CLASS)` appears exactly once, in
  `paint()`, gated on `live === true`; `logRow` is the only caller and passes
  `entry.live`; `loadOlder` constructs rows with `live: false`. The CSS sweep in
  the suite (`test:407-412`) is a real sweep over every rule with an
  `animation:` declaration, not a spot check, and `@media
  (prefers-reduced-motion)` is honoured.
- **Resume semantics.** `wsUrl()` emits `?cursor=` from `state.resume` only,
  which is written *solely* by `adoptCursors` from `mode`/`subscribed`; hello's
  client-echoed cursors are correctly ignored, and hello carries no anchors
  (matching `server.hello`'s docstring). See m1 for the one place this slips.
- **Absolute tokens.** Latest-per-ref with an explicit
  `if (held && held.seq > totals.seq) return;` out-of-order guard; no
  subtraction anywhere near a token field; the harness verified 100 then 300
  yields 300.
- **GD-23.** No `Date.now`, no bare `new Date()`, no `performance.now`, no idle
  threshold, no locally-defined reducer; every badge word is `derived.state` /
  `derived.label` / `plan.badge` / `plan.label`, and `legacy.CLOSED_NO_VERDICT`
  is asserted *absent* from the source so the page cannot invent the re-label.
- **GD-22.** The mirror is a header chip; nothing renders conditionally on it,
  and `/api/query` is deliberately not read.
- **Not-a-tautology.** The class whitelists are compared against
  `agents.NODE_STATES`, `legacy.STATES | DERIVED_STATES` and
  `store.PROVENANCE`, the routes against `server.READ_ROUTES`, and the frame
  block against `server.py`'s own docstring text — all four fail if either side
  drifts. The comment stripper has a self-test on its own assumptions.
- **Right call on a trap:** `runs.harnessTotals` is projected into
  `/api/run/graph` and the page deliberately does **not** render it. That is
  correct — `ingest.py:1804` and `mongo_store.py:378` pin it "display-only,
  never summed", and it is the source of the 1 089 990 over-count R-26 warns
  about.
