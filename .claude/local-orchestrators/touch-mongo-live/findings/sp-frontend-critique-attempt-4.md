# sp-frontend — adversarial critique, attempt 4

**Verdict: REJECTED** — 1 major, 3 minor, 6 nits. `depth: in-scope`,
`critical_defect: false`.

Reviewed (full content; the tree is untracked, so `git diff` is empty for all
four):

- `/home/laniakea/Projects/touch/touch-visual/index.html` (149 lines)
- `/home/laniakea/Projects/touch/touch-visual/app.js` (2426 lines)
- `/home/laniakea/Projects/touch/touch-visual/style.css` (403 lines)
- `/home/laniakea/Projects/touch/tests/test_touch_frontend.py` (2028 lines)

Against `plan/touch-mongo-live-subplans.md` §sp-13, amendment R-55 / GD-22 /
GD-23, base R-22 / R-32 / GD-20, and `aggregator/server.py` + `store.py` as the
counterparties.

## Method

The suite is now 39 test functions / 411 assertions, one of them a real
`node`+`vm` fake-DOM harness that executes `app.js`. Source guards cannot see
behaviour and the harness only drives scenarios its author imagined, so I
re-used its prelude and drove three it does not — **all three findings below
that matter were observed in execution, not argued**:

| probe | scenario | result |
|---|---|---|
| `sp-frontend-critique-attempt-4-probe1-selection-race.js` | click *load older*, switch the sidebar selection while the page is in flight | **M1 arm A observed** |
| `sp-frontend-critique-attempt-4-probe2-reconnect-race.js` | click *load older*, drop + reconnect the socket while the page is in flight; then the task panel and a failing `/health` | **M1 arm B observed** (task panel and health arms clean) |
| `sp-frontend-critique-attempt-4-probe3-malformed-hash.js` | boot with `#run/%zz` in the location hash | **m1 observed** |

All three are in `findings/` beside this file; each is `node <probe> touch-visual/app.js`.

Baseline re-run before attacking: `python3 tests/test_touch_frontend.py` → rc 0,
`all touch-visual source guards passed`.

Hygiene, checked and clean: `HEAD` is still `579446e`, nothing committed;
`find -newermt "2026-07-26 20:00"` lists `aggregator/server.py` (20:17),
`tests/test_api.py` (20:15) and `tests/test_server_core.py` (20:12) — **2½ hours
older** than this attempt's window (22:29–22:46) and therefore sp-server-api's
closed loop, not this one; the only files this attempt wrote are its four.
GD-21/24/25/26/27/29/30 have no surface in these four files beyond "the suite
imports clean on bare stdlib and needs no mongod", which the gate re-proved with
an import blocker.

Attempt 3's findings are genuinely fixed, and I verified the fixes in execution
rather than in source: per-stream `resetOlder` (M1), pin-to-bottom + trim-shift
in `flushLog` (M2), the runs signature without `state.delivered` plus
`runSeqNodes` written in place (m1), `setDetail`'s payload change-guard and the
`moved` test in `noteTokens` (m1), `clearOlderError` (m2), `state.anchors = {}`
in `connect` (m3), `isLinkableRunId` (m4), the timeline window + `TIMELINE_MAX`
(m5), the toolbar out of `<h2>` (n1), `queueRefresh`'s single flag (n2), LRU
token eviction (n4), `backfilled` on the meta line (n5), the `derived.reason`
guard (n6), registered intervals in the harness (n7), `list.scrollTop = 0` after
a prepend (n8). The major below is **not** an old finding returning by the same
route — it is the one path the per-stream fix did not cover.

---

## MAJOR

### M1 — a `load older` page is painted into `#older` without re-checking whose window it belongs to, so an in-flight request that outlives its selection *or* its connection puts one stream's history above another stream's live tail, under the page's own label saying it is that other stream's

`touch-visual/app.js:2199-2260` (`loadOlder`), specifically the `await` at
`app.js:2222`, the paint at `app.js:2241-2249` and `state.olderShown +=
rows.length` at `app.js:2249`; interacting with `app.js:2059-2067`
(`resetOlder`), `app.js:2107-2126` (`renderOlder`) and `app.js:777-778`
(`connect`'s anchor/history reset).

`loadOlder` captures `stream` and `anchor` before the request and re-checks
neither after it. Every other async path in this file does re-check —
`refreshDetail` has the guard twice, in both the success and the failure arm
(`app.js:2342`, `app.js:2345`: *"A slower answer for a selection the user
already left must not paint"*) — and `resetOlder`'s whole purpose is that the
history list is per-stream and per-connection. The request this races is the one
the file itself documents as expensive: *"`h_events` re-reads the whole stream
file per call, in the process that also serves `/ws`"* (`app.js:2193-2194`), so
the window is not a microsecond, it is however long a `read_all` of a multi-MB
stream takes.

**Arm A — the selection moved (probe 1, verbatim):**

```
selected wf_a; btn=load older · run:wf_a hidden=false
after click, fetch in flight: older rows=0 deferred=true
switched to wf_b: older rows=0 hidden=true btn=load older · run:wf_b
AFTER the wf_a page landed:
  older rows      = 200  hidden=false
  first row text  = …run:wf_a#800 event harness agent=old note=history replay
  aria-label      = older records of run:wf_b, loaded on demand
  button          = load older · run:wf_b
  button title    = …this walks run:wf_b only — records older than seq 2,000…
  logMeta         = 0 seen · 0 shown · 200 older loaded of 400 · window 500
  rows belonging to wf_a while wf_b is selected = 200
```

This is attempt 3's M1 in full, with one addition that makes it worse than the
version just fixed: the page no longer merely *implies* the rows are the
selected stream's, it **states** it. `renderOlder` writes
`aria-label="older records of run:wf_b"` (`app.js:2123-2125`) over 200 rows that
are all `run:wf_a`, and 200 of `run:wf_b`'s 400-row budget is spent on them
(`logMeta`, and `olderRoom()` is now short by 200 for the stream that never
loaded anything). D13's honesty rule is the one this sub-plan is graded hardest
on; a screen reader is told the wrong thing outright.

**Arm B — the connection moved (probe 2, verbatim):**

```
after reconnect (no truncation declared): older rows=0 hidden=true btnHidden=true
after the dead connection's page landed: older rows=200 hidden=false btnHidden=true
                                         meta=… 200 older loaded of 400 …
```

The reconnect's `mode` frame declared **no truncation at all**, so `connect()`
correctly dropped the anchors and the history — and then the dead socket's page
landed and made `#older` visible again, with the button hidden. The result is
200 history rows above the live tail with no affordance that explains them, no
way to clear them short of another stream switch, and a direct contradiction of
the invariant `connect()` states in prose four lines above the code
(`app.js:774-776`: *"the loaded history goes with them: it is history of a
window this socket has not described"*). `index.html:112-114` makes the same
promise to the reader.

Why the existing guards do not catch it: `resetOlder` is a no-op once
`state.olderStream` already equals the current stream (`app.js:2060`), and by
the time the response lands the selection change has *already* handed the list
over to the new stream. `renderOlder`'s belt-and-braces
`state.olderStream !== stream` (`app.js:2118`) compares the list's stream to the
current stream — both are `run:wf_b` — not to the stream the rows came from. The
guard is on the wrong quantity.

**Fix** (small, entirely inside these files):

- add a connection epoch — `state.epoch = 0` in state, `state.epoch += 1` in
  `connect()` beside `state.anchors = {}` — and in `loadOlder` capture
  `const epoch = state.epoch;` with `stream` before the fetch;
- after the `await`, before anything is painted or counted:
  `if (currentStream() !== stream || state.olderStream !== stream ||
  state.epoch !== epoch) return;` (in the `finally`, `loadingOlder = false;
  render();` still runs, which is correct — the button must come back). The
  error arm needs the same treatment or a failed page pins its line to a stream
  it does not describe;
- alternatively (equivalent, fewer moving parts): stamp the rows —
  `resetOlder(stream)` then `if (state.olderStream !== stream) return;` *after*
  the await — the epoch is only needed because a reconnect can leave the same
  stream selected, which is exactly arm B;
- **and add the effect guards**, because both arms pass all 39 current test
  functions. The harness already has everything needed: make `fakeFetch` return
  a deferred promise for `/api/events` (probe 1 does this in ~10 lines by
  reassigning `sandbox.fetch`), then assert (a) after a selection change the
  landed page paints **zero** rows and `state.olderShown` is 0, and (b) after
  `sock.onclose()` + a clean reconnect the landed page paints zero rows and
  `#older` stays `hidden`. A static guard is available too and is worth having
  beside them: `slice_fn(CODE, "async function loadOlder", "\n}")` must contain
  a post-`await` identity check, in the same shape
  `test_the_expensive_route_is_polled_on_its_own_slow_cadence` asserts for the
  tasks join.

---

## MINOR

### m1 — a malformed percent-escape in the location hash throws out of `route()`, and at boot that kills the whole page: no socket, no render, no polls, no message

`touch-visual/app.js:2390` (`decodeURIComponent(raw.slice(cut + 1))`),
`app.js:2412` (`route()` inside `boot()`), `app.js:2411` (the `hashchange`
listener).

Observed (probe 3 — the page loaded at `#run/%zz`):

```
URIError: URI malformed
    at decodeURIComponent (<anonymous>)
    at route (touch-visual/app.js:2390:34)
    at boot (touch-visual/app.js:2412:5)
```

`route()` is the fifth statement of `boot()`, so the throw skips `render()`,
`refreshModel()`, `refreshTasks()`, `refreshHealth()`, both `setInterval`s and
`connect()`. What ships is the static skeleton with "connecting" in the header
forever — no notice, no error panel, nothing in the one place
(`state.bootError`) that exists to say "this page cannot work and here is why".
On a later `hashchange` the same throw is quieter but still wrong: the handler
dies, the selection silently stays on the previous thing, and the URL bar now
disagrees with the page.

The page never *generates* such a hash (`rowLink` always
`encodeURIComponent`s), so the trigger is external — a hand-edited URL, a
chat client that truncated a shared link mid-escape. That is why this is minor
and not major; the consequence, a completely dead page, is why it is not a nit.

**Fix.** Wrap the decode and treat a failure as "no selection", with a line the
operator can read:

```js
let id = "";
try { id = cut === -1 ? "" : decodeURIComponent(raw.slice(cut + 1)); }
catch (err) { note("the address bar's #fragment is not valid percent-encoding"); }
```

and, independently, make `boot()` robust to the class rather than the instance:
put `route()` after the listeners and inside a `try`, or call `connect()` before
it — a page that cannot parse its own hash should still be a live view. Guard:
drive the harness with `window.location.hash = "#run/%zz"` before the app is
evaluated and assert a socket is still opened.

### m2 — a page of history is discarded whenever the selection moves to a session or a task folder, because `currentStream()` falls back to `currentRun`

`touch-visual/app.js:2172-2176` (`currentStream`), `app.js:2107-2111`
(`renderOlder` calls `resetOlder(currentStream())` on **every** paint).

Selecting a session while run `wf_b`'s 400 loaded rows are on screen makes
`currentStream()` return `state.currentRun` (`run:wf_a`), which hands the list
over and empties it — two `read_all`s worth of work thrown away by a click that
had nothing to do with the log, and re-selecting `wf_b` starts from zero. Within
one stream the discard is correct and deliberate; across a *kind* change it is
collateral.

**Fix.** Either keep the last run-kind stream (`state.logStream`, updated only
when `sel.kind === "run"` or when `currentRun` changes) and let the history
follow that, or key the history per stream (`state.older = {stream: {rows,
shown}}`, `#older` rebuilt from the entry) so a return trip is free. Both keep
M1's per-stream identity; neither reintroduces the shared budget.

### m3 — `dataSummary` JSON-stringifies a record's whole `data` value in order to display 64 characters of it

`touch-visual/app.js:470-485` (`const flat = (value && typeof value ===
"object") ? JSON.stringify(value) : String(value); parts.push(k + "=" +
truncate(flat, 64))`).

The truncation happens *after* the serialization, so a single record whose
`data` carries a large nested object is fully materialized as a string, per row,
to render an ellipsis. The corpus this page renders is the one whose worst line
is 872 KB (the constant is quoted twice in these very files), `h_events` returns
raw store records, and the page's own budget is 400 live rows plus 400 history
rows. It is not a hot path today because nothing in `aggregator/` writes a large
`data` payload, which is exactly the property that changes without this file
being touched.

**Fix.** One line: `JSON.stringify(value).slice(0, 200)` is not enough (it
serializes first) — bound the input instead, e.g. serialize only
`Object.keys(value).length <= 12 ? value : {"…": Object.keys(value).length + " keys"}`,
or catch the size at the top: if the record carries `oversize`/`persistedOutput`
render the marker chip the timeline already has and skip the walk.

---

## NITS

- **n1** — `app.js:2124`: the specialised `aria-label` interpolates the raw
  stream id with no length bound while every other display path in the file goes
  through `truncate`. Harmless (it is an attribute, not markup) but inconsistent
  with `app.js:2147`'s `truncate(stream, 40)` two lines away.
- **n2** — `index.html:136-137`: `#older` and `#log` are scroll boxes with no
  `tabindex="0"`, so a keyboard-only operator cannot scroll either without a
  focusable child. The page just gained deliberate scroll management; the
  keyboard arm of it is one attribute per list.
- **n3** — `app.js:901-906`: `noteStream` re-`sort()`s the whole list on every
  new stream while `onHello` (`app.js:933`) adopts the server's order verbatim,
  so the sidebar can visibly re-order itself the first time a mid-connection
  stream is learned. Sort in both places or neither.
- **n4** — `app.js:1052-1058` (`adoptCursors`): `state.resume` is never pruned,
  so a stream that disappears server-side keeps sending a cursor that
  `subscribe` refuses on every reconnect, and `onHello` renders
  "resume cursor refused: …" for it forever. Drop resume keys absent from
  `hello.streams` after the first handshake that omits them.
- **n5** — `app.js:2118`: `state.olderStream !== stream` in `renderOlder` cannot
  be false — `resetOlder(stream)` on the line above makes them equal
  unconditionally. It reads as a second guard and is dead; the *real* second
  guard is the one M1 asks for (post-`await`, in `loadOlder`).
- **n6** — `tests/test_touch_frontend.py:1400` (`runGraph` in the harness) is
  reassigned but the session/task panels are never driven: `renderTaskDetail`,
  `renderSessionDetail`'s chips and `rollupList` are asserted by source text
  only. I drove them by hand in probe 2 and they render correctly (including
  `rollupList` folding two cumulative records for one agent to `in 30`, not
  `in 40`) — so this is coverage, not a defect. Ten lines of the probe's
  `tasksBody` would close it.

---

## What I attacked and could NOT break

Recorded so a fifth attempt does not re-litigate settled ground.

- **Attempt 3's M2 (the live edge).** Really fixed, and correctly: `atLiveEdge`
  is read before the append, the follow is applied after the trim, and the
  scrolled-away arm gives back exactly the height the trim freed. The harness
  drives all three arms against a fake DOM with real `scrollHeight` arithmetic;
  I re-ran it and re-derived the 40 px shift by hand.
- **Attempt 3's m1 (inert region short-circuit).** Measured again with mutation
  counters: three live `event` frames now leave `#runList` and `#detailBody`
  untouched, the run row's `seq` is still current (`seq 6,002` written in place
  through `setText`), three byte-identical token records do not rebuild the
  panel, and one that moved does. `noteTokens`' `moved` test and `setDetail`'s
  `detailKey` are both real change-guards.
- **Escape-first (GD-20).** Still structural: no markup sink in any spelling, no
  template literal at all, the only text sink is `createTextNode`/`textContent`,
  every class is a call-site literal or a `classOf()` whitelist hit, every
  `href` is `"#" + <whitelisted kind> + "/" + encodeURIComponent(id)`.
- **R-55's animation rule.** `classList.add(LIVE_CLASS)` appears once, in
  `paint()`, gated on `live === true`; history rows are built `live:false`;
  every `animation:` rule in the stylesheet is reachable only through `.fresh`
  or the mode chip, and `prefers-reduced-motion` kills both.
- **Absolute tokens.** Latest-per-ref with the `held.seq > totals.seq` guard,
  LRU eviction, refless records counted and named in the title rather than
  bucketed under `""`. 100→300→(identical)→900 verified in execution. No delta
  arithmetic anywhere.
- **GD-23.** No `Date.now`, no bare `new Date()`, no idle threshold, no local
  reducer; every badge word traced to a server field; `current` is a neutral
  `chip-current` whose title says it is a selection, not a verdict; the session
  panel still *refuses* to join agents to a session and says so on the page
  (R-32's "per-session agent tree" is genuinely blocked on a route sp-12 does
  not have — accepted in attempts 2 and 3, and I agree: the honest refusal beats
  a client-side join).
- **GD-22.** `/health` failing produces `mirror —` plus one notice line, and the
  line clears the moment a poll succeeds (probe 2). Nothing on the page renders
  conditionally on the mirror; `/api/query` is never fetched.
- **The other stale-response paths.** `refreshDetail` (both arms), `fetchTasks`'
  in-flight join, `widenTimeline` → `refreshDetail`, and `dropSocket`'s
  null-the-handlers-before-close are all correct. `loadOlder` is the only one
  missing the guard, which is what M1 is.
- **Wire-shape drift.** Re-walked the fields the page reads against
  `server.py`: `RECORD_FIELDS`/`STREAM_META_FIELDS`, `h_events`
  (`records`/`oldest`/`hasOlder`, `page = older[-limit:]` ascending),
  `h_timeline` (`sessionDoc`/`count`/`hasMore`, `positive_int` clamping at
  `MAX_PAGE` 1000 vs `TIMELINE_MAX` 960), `h_run_graph`'s `derived`,
  `_agent_payload`'s `parent`/`root`/`sessions`, `ID_PATTERNS["run"]` vs
  `validate_stream`'s multi-component ids. No drift.
- **The suite is not tautological.** It cross-checks four server-side
  vocabularies, asserts source *ordering* (guard before fetch, edge check before
  append, comparison before clear), and the driven harness carries a
  `HARNESS_EXPECTED` roster so a silently shortened run fails. The gate's own
  mutation test (`LIVE_CLASS` renamed → 4 failures, static and driven) matches
  what I saw.
