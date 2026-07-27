# sp-frontend — adversarial critique, attempt 3

**Verdict: REJECTED** — 2 major, 5 minor, 8 nits. `depth: in-scope`,
`critical_defect: false`.

Reviewed (full content; the tree is untracked, so `git diff` is empty for all
four):

- `/home/laniakea/Projects/touch/touch-visual/index.html` (131 lines)
- `/home/laniakea/Projects/touch/touch-visual/app.js` (2057 lines)
- `/home/laniakea/Projects/touch/touch-visual/style.css` (378 lines)
- `/home/laniakea/Projects/touch/tests/test_touch_frontend.py` (1460 lines)

Against `plan/touch-mongo-live-subplans.md` §sp-13, amendment R-55 / GD-22 /
GD-23 / GD-24 / GD-28, base R-22 / R-32 / GD-7 / GD-14 / GD-20, and
`aggregator/server.py` + `aggregator/agents.py` + `aggregator/store.py` as the
counterparties.

## Method

The suite is now 33 tests, 32 of them static source guards plus one driven
`node`+`vm` DOM harness. A source guard cannot tell you whether the page
*works*, and the harness only drives the scenarios its author thought of — so I
re-used its DOM prelude and drove three scenarios it does not:

1. `python3 tests/test_touch_frontend.py` → green (311 ok, 0 FAIL), as reported.
2. `atk1.js` — two runs, both truncated: load history for run A to the budget,
   then switch the selection to run B. **M1 observed.**
3. `atk2.js` — DOM-mutation counters on `#runList` / `#detailBody` across three
   live `event` paints and three *identical* `token` paints. **m1 measured.**
4. `atk3.js` — a failing `/api/events`, then a stream switch; then a socket
   close + clean reconnect whose replay cuts nothing. **m2 and m3 observed.**

Every major and minor below except m4/m5 was **observed in execution**, not
argued.

Hygiene, checked and clean: `HEAD` is still `579446e`, no commit; `git status`
shows only `aggregator/`, `docs/`, `tests/`, `touch-visual/` untracked plus the
pre-existing `.gitignore`/`CLAUDE.md` edits; `find -newermt` shows no file
outside the four owned ones touched in this window;
`tests/test_touch_frontend.py` is `+x` and is picked up by `run_all.sh`'s
`tests/test_*.py` glob. GD-21/24/25/26/27/29/30 have no surface in these four
files beyond "the test imports clean on bare stdlib and needs no mongod", which
holds.

All of attempt 2's findings are genuinely fixed, not papered over: `#older`
with its own `OLDER_MAX` budget (M1), `noteStream` on `onEvent`/`onAnchors`
(M2), `[hidden] { display: none !important }` (m1), the `state.noticeText`
change-guard (m2), `agentTree`'s real containment (m3), `chip-current` (m4),
`TOKENS_MAX` (n1), the closing meta line (n2), `loadingOlder` (n3), `region()`
(n4), `fetchTasks`' in-flight join (n5), `render === false` (n6),
`tokensRefless` (n7). The two majors below are **new consequences of the M1
fix**, not the old ones returning.

---

## MAJOR

### M1 — the history budget is global and is never released, so after one stream's history fills it the load-older affordance is permanently dead for every other stream — and `#older` keeps showing the first stream's rows under the new selection, with a button that states something false about it

`touch-visual/app.js:143` (`OLDER_MAX`), `app.js:500` (`state.olderShown`),
`app.js:1764-1766` (`olderRoom`), `app.js:1782-1807` (`renderOlder`),
`app.js:1821-1825` (`currentStream`), `app.js:1848-1896` (`loadOlder`,
`state.olderShown += rows.length` at 1885), `touch-visual/index.html:118`.

`state.olderShown` is monotonic — it is incremented in exactly one place and
decremented nowhere — and `#older` is never cleared or trimmed by anything (the
only writers are `loadOlder`'s `insertBefore` and `renderOlder`'s `hidden`
toggle). But `olderRoom()` is what gates *every* stream:

```js
function olderRoom() { return Math.max(0, OLDER_MAX - state.olderShown); }
```

and `currentStream()` is per-selection. So the budget is spent globally and
charged per-stream.

Observed, two runs both declaring a truncation (`run:wf_a` oldest 1000,
`run:wf_b` oldest 2000), two clicks on wf_a, then a selection change to wf_b:

```
after 2 clicks on wf_a: older rows = 400  btn: older list full · 400 rows disabled= true
--- after switching selection to run:wf_b ---
older rows still shown = 400   hidden = false
first older row text = …run:wf_a#600 event harness agent=old note=history replay
olderBtn: "older list full · 400 rows"  hidden=false  disabled=true
olderBtn title = "the history list holds its full 400 rows; nothing more is
                  fetched, and the live tail below is untouched"
click on wf_b: fetches 0   older rows = 400
logMeta = "0 seen · 0 shown · 400 older loaded of 400 · window 500"
```

Three separate problems, each on its own worth fixing:

1. **The affordance is dead again, permanently.** Attempt 2's M1 was "the button
   can never paint a row because the budget it draws on is structurally zero".
   The fix gave history its own budget — and then made that budget
   un-releasable and shared across streams. Two clicks on any one run disable
   load-older for every run, for the life of the tab, with no clear affordance
   and no reload-free recovery. The failure mode is the same shape as the one
   just closed, reached by a plainer route: click twice, click a sidebar row.
2. **The panel shows one stream's history above another stream's live tail.**
   `#older` sits directly above `#log` inside `#logPanel`, `renderOlder` reveals
   it on `state.olderShown` alone (`app.js:1784`), and nothing scopes it to the
   selection. The rows do print `run:wf_a#600`, so it is not silent — but the
   list's own `aria-label` is "older records, loaded on demand" and the visual
   grammar (`style.css:323-327`: history above, dashed edge, then the tail)
   reads as "this is the older part of what you are looking at". It is not.
3. **The button's label and title are false for the selected stream.** "the
   history list holds its full 400 rows" is true of the list; the operator reads
   it as a statement about wf_b, whose loaded history is zero. D13's honesty
   rule is the one this sub-plan is graded hardest on, and this is the page
   stating something that is not true of the thing it is pointing at.

Within *one* stream the 400-row cap is a deliberate, honestly-labelled budget
and I am not asking for it to grow. The defect is that it is not the stream's
budget.

**Fix** (one attempt, entirely inside these files):

- give the history list a stream identity — `state.olderStream` — and reset it
  in `renderOlder`/`select` when `currentStream()` differs:
  `if (state.olderStream !== stream) { clear(dom.older); state.olderShown = 0;
  state.olderStream = stream; }`. History belongs to one stream by construction
  (the button already says "this walks `<stream>` only", `app.js:1804`), so
  discarding it on a switch is the semantically correct move, not a compromise;
- or key it: `state.older = {}` mapping stream → `{rows, shown}`, with `#older`
  rebuilt from the entry for `currentStream()` — more code, and it keeps the
  work when the operator switches back;
- either way `renderOlder`'s `history.hidden` must be `!state.olderShown ||
  state.olderStream !== stream`, so a stale list cannot render at all;
- and add the *effect* guard the suite is missing — the same class of guard
  `test_load_older_has_its_own_room_and_can_therefore_paint` correctly added
  last round. Extend the driven harness: two truncated streams, fill the budget
  on the first, select the second, assert `olderBtn.disabled === false` and
  that a click paints rows. The static guards cannot see this: every one of them
  passes today.

### M2 — the live tail never shows the live edge: `#log` appends at the bottom of a 46 vh scroll box with no scroll management, so once the log exceeds the box (≈17 rows) every new row lands permanently below the fold

`touch-visual/style.css:318` (`.log { max-height: 46vh; overflow: auto }`),
`touch-visual/app.js:1726-1741` (`flushLog` — `list.appendChild(fragment)` then
`removeChild(list.firstChild)`), `touch-visual/index.html:119`. No `scrollTop`,
`scrollIntoView` or `scrollHeight` appears anywhere in `touch-visual/`
(`grep -rn "scroll" touch-visual/` → the CSS `overflow` declarations only).

`.logrow` is `font-size: 11px` with `padding: 3px 12px`, so ≈24 px a row; 46 vh
on a 900 px viewport is ≈414 px, i.e. ≈17 visible rows against a 400-row cap.
The box therefore overflows within the first replay burst and never un-overflows.
From that moment the scroll position stays at 0 and every subsequent live frame
— the ones R-55's whole `live:true` machinery exists to mark, and that
`.fresh`/`@keyframes freshin` (`style.css:350-355`) exists to flash — is painted
outside the viewport. The operator watching a run sees a frozen window of the
*oldest* 17 rows in the buffer.

The trim makes it worse rather than better: `flushLog` removes `list.firstChild`
on every over-cap paint, i.e. mutates content *above* the scroll position, which
is precisely the case CSS scroll anchoring exists to paper over and does not
always win.

This is not me inventing a requirement out of nothing — the substrate GD-20
tells this file to inherit from solved it, and this page dropped the solution
without replacing it. `monitor.html:672` is `p.log.insertBefore(li,
p.log.firstChild); // createdAt DESC — newest on top`, which needs no scroll
code at all because the live edge is always at scrollTop 0; and
`monitor.html:1153-1155` is the other idiom, an explicit `pinned` check that
follows the live edge unless the user has scrolled away. "The render-everything
loop and the unbounded log" are on GD-20's do-not-inherit list; "newest row
visible" is not.

Honest counter-argument, recorded so the next attempt can push back if it
disagrees: no plan item names autoscroll, and the box *is* scrollable by hand.
I still call it major, because the page's stated job is the live view and its
default state is that the live view is off-screen.

**Fix.** Either idiom, both small and both already in the repo:

- pin-to-bottom with a user-scroll escape, in `flushLog` right after the
  fragment is appended:
  `const pinned = list.scrollTop + list.clientHeight >= list.scrollHeight - 40;`
  captured *before* the append, then `if (pinned) list.scrollTop =
  list.scrollHeight;` after it (and after the trim). Copy the comment from
  `monitor.html:1145-1155`; it is the same rule;
- or newest-on-top: `list.insertBefore(fragment, list.firstChild)` with the
  fragment built in reverse and the cap trimming `list.lastChild`. This also
  makes `#older`'s placement natural (history *below* the tail rather than
  above) and removes the "content mutates above the viewport" problem entirely.
- guard it in the driven harness: the fake DOM would need `scrollTop`/
  `scrollHeight`/`clientHeight` stubs, which is ~10 lines; assert `scrollTop`
  moved after an append and did **not** move after an append made while
  `scrollTop` was parked away from the bottom.

---

## MINOR

### m1 — the `region()` short-circuit never engages for the two regions it was written for: the run list carries a per-event seq in its signature and the detail panel is keyed by revision counters that bump on byte-identical payloads

`touch-visual/app.js:1095-1100` (`region`), `app.js:1153-1157` (the runs
signature, `state.delivered[stream]` at 1155), `app.js:1246-1247` (the detail
signature), `app.js:1227-1231` (`setDetail` bumps `detailRev` unconditionally),
`app.js:592` (`state.tokensRev += 1`, also unconditional), and
`tests/test_touch_frontend.py:1338-1346`.

Measured with mutation counters on the real code:

```
3 live EVENT paints  -> runList    +3 appends / +3 removes
3 live EVENT paints  -> detailBody +15 appends / +15 removes
3 IDENTICAL token records -> detailBody +30 appends
3 IDENTICAL token records -> runList   +3 appends
```

Attempt 2's n4 was "these regions are cleared and rebuilt on every paint, which
kills a text selection and swallows a click whose mousedown/mouseup straddle
the rebuild". The machinery added to fix it is inert on the live path:

- `renderRuns`' signature contains `state.delivered[stream]`, which by
  definition changes on every event for that stream, so the run list is rebuilt
  on every live frame — the sidebar the operator clicks, during exactly the
  traffic that made it a problem;
- `setDetail` bumps `detailRev` on *every* `refreshDetail`, including the
  ~2 s poll returning a byte-identical payload, so the panel (nodes + the whole
  nested agent tree) is torn down and rebuilt on the cadence;
- `state.tokensRev` bumps even when the stored totals did not move, so three
  identical token records rebuild the detail panel three times.

The suite's guard cannot see any of this: `an unchanged run list survives three
paints` drives three `tick` frames (`test_touch_frontend.py:1340-1346`), and a
`tick` is the one frame type that changes nothing in either signature. It
asserts the property in the only case where it was never at risk.

**Fix.** Make the signatures describe what is *rendered*, not what *happened*:

- drop `state.delivered[stream]` from the runs signature (or render the seq
  through a `setText` on a child node the rebuild does not own — the run row
  already builds `el("span","rowts", …)`, so bucketing the seq to, say, the
  nearest 100 or moving it out of the signature both work);
- key the detail region on the payload itself, e.g.
  `JSON.stringify([kind, id, detailError, payload])` — the payloads here are
  small and this is already a JSON-stringify site — or have `setDetail` bump
  `detailRev` only when the serialized payload differs;
- bump `tokensRev` in `noteTokens` only when a value actually changed
  (compare the four keys against `held` before `set`);
- and change the harness assertion to drive live `event` frames, not `tick`s.

### m2 — a failed `loadOlder` pins its line to the notice bar for the life of the tab, in the box whose stated contract is "it empties itself when the trouble ends"

`touch-visual/app.js:1890-1891` (`setError("load older", err.message)`),
`app.js:1889` (the only clearer), `app.js:1053-1055` (the contract, in a
comment), `app.js:539-543` (`setError`).

`setError("load older", …)` is cleared by exactly one statement, on the success
path of `loadOlder`. `loadOlder` can only run when the button is visible and
enabled, and the button hides whenever `currentStream()`'s anchor is not
truncated. Observed:

```
notice after a failed click = "load older: the store is unreadable"
olderBtn hidden on wf_b = true
notice STILL             = "load older: the store is unreadable"
notice after 5 more paints = "load older: the store is unreadable"
```

Attempt 1's M1 was precisely "an append-only notice pins a resolved failure to
the top of the page for the life of the tab", and every other slot got a real
owner-clears-it arm (`sessions`, `tasks`, `health`). This one has a clearer that
the operator can be locked out of.

**Fix.** Clear the slot when the affordance goes away — one line in
`renderOlder`: `if (!available) { setError("load older", null); button.hidden =
true; return; }` — and/or clear it in `select()`/the stream-change reset M1 asks
for. Guard: assert `renderOlder` clears the slot on the not-available arm.

### m3 — anchors are never scoped to a connection, so a clean reconnect keeps the dead connection's `oldest`/`truncated` and offers "load older" against a window that no longer exists

`touch-visual/app.js:897-911` (`onMode` iterates only the keys the frame
carries), `app.js:913-921` (`onAnchors`), `app.js:677-684` (`connect` resets
`state.wire.notices` and nothing else), `app.js:948-952` (`anchorOf`).

Observed — socket close, reconnect, `hello` with `resumed:true`, then a `mode`
frame that declares no truncation at all:

```
anchors before close:    {"run:wf_a":{"oldest":1000,"truncated":true}}
anchors after reconnect: {"run:wf_a":{"oldest":1000,"truncated":true}}
olderBtn after a clean reconnect: hidden = false  text = "load older · run:wf_a"
```

`connect()` already reasons correctly about this exact class of state — "Wire
notices are scoped to ONE connection… carrying it across a reconnect makes the
page describe a socket that no longer exists" (`app.js:679-683`). Anchors are
the same kind of thing (they describe what *this* replay cut) and did not get
the same treatment. Consequence: the button is offered for a stream the current
connection never said was truncated, and `anchor.oldest` points into a range the
resumed replay may already have re-delivered — so the click walks backwards from
the wrong place and, if those seqs are in `state.seen`, paints nothing while
spending a whole-stream `read_all` on the server.

**Fix.** `state.anchors = {};` beside `state.wire.notices = []` in `connect()`,
with the same comment. (If M1's per-stream history lands, reset
`state.olderShown` / `#older` there too — the loaded page is history of a window
the new connection has not described.)

### m4 — `runIdOf` assumes every `run:` stream id is a bare runId, but `run:legacy:<task>` is a documented stream shape the run detail route rejects with a 400

`touch-visual/app.js:394-397` (`runIdOf`), `app.js:1162` (`rowLink("run",
runId, runId)`), `app.js:866-869` (`onHello`'s auto-select of `currentRun`),
`app.js:1957` (`getJson("/api/run/graph", {run: id})`), against
`aggregator/store.py:402-406` (which names `run:legacy:touch-recon` as the
canonical multi-colon stream id) and `aggregator/server.py:477`
(`"run": (lambda v: bool(_NAME_RE.match(v)), "a runId")` with
`_NAME_RE = ^[A-Za-z0-9][A-Za-z0-9._@=+-]{0,127}$` — **no colon**).

`runIdOf("run:legacy:touch-repo-recon")` returns `"legacy:touch-repo-recon"`;
`/api/run/graph?run=legacy:touch-repo-recon` is a 400 "malformed runId". So such
a row is a dead sidebar link, and if that stream is ever the newest-written one
the server names as `currentRun`, `onHello` auto-selects it and the page boots
straight into the error panel. Not reachable today — nothing in `aggregator/`
writes a `run:` stream yet — which is why this is minor rather than major, and
why it is exactly the kind of thing that ships.

**Fix.** One guard in `renderRuns`: a `run:` stream whose runId contains `:`
renders as a row with a `prov-unknown` chip ("legacy run stream — no graph
route") and no link, rather than a link to a 400. Cross-file guard for the
suite: for every `run:`-prefixed id shape `store.validate_stream` accepts,
assert `runIdOf`'s output satisfies `server.ID_PATTERNS["run"]` or is rendered
unlinked.

### m5 — the session timeline is a first-page-only view of the *oldest* 120 records, and the server's own paging cursor is ignored

`touch-visual/app.js:1958-1960` (`limit: TIMELINE_PAGE, meta: 1`),
`app.js:1558-1563` (the "more beyond this page" hint), against
`aggregator/server.py:1974-1983`, which sorts ascending by `(lineNo, _id)` and
returns `nextSince` / `nextSinceId` precisely so a client never builds a cursor
itself.

The panel therefore shows the first 120 records a session ever wrote, says
"more beyond this page", and gives no way to reach them — and for a live session
the interesting end is the other one. R-55's "load older" is about the `.touch/`
event stream and this is a different route, so this is not a plan violation; it
is a labelled dead end in the one panel R-32 calls the session view.

**Fix.** Either wire the cursor (a second "load more" button below the timeline,
same pattern as `loadOlder`, driven by `nextSince`/`nextSinceId`) or say what
the page actually shows: "the first 120 records of this session". The current
hint implies a continuation that does not exist.

---

## NITS

- **n1** — `index.html:94-105`: the `load older` button and the `#logMeta` span
  live *inside* `<h2>`, so the heading's accessible name is
  "events 0 seen · 0 shown · 400 older loaded of 400 · window 500 load older ·
  run:wf_a". Move both out of the heading into a sibling toolbar row.
- **n2** — `app.js:979-988`: `queueRefresh`'s `if (refreshTimer) return;` is
  dead. `refreshTimer` is non-null only while `refreshPending` is true, and the
  earlier `if (refreshPending) return;` has already returned. Delete it, or drop
  `refreshPending` and keep the timer as the single flag.
- **n3** — `app.js:852-855`: two notice paths (`fromRejected`, `fromApplied ===
  false`) can never fire from this client — `wsUrl` (`app.js:646-656`) sends
  `?cursor=` only and never `?from=`. Harmless, but it reads as if the page uses
  `?from=`; say "the server may be driven with `?from=` by hand" or drop them.
- **n4** — `app.js:596-600`: the `TOKENS_MAX` FIFO is by *first* insertion — a
  JS `Map.set` on an existing key does not move it — so the cap evicts the ref
  observed longest ago even if it is the busiest, while a one-shot ref that
  arrived later survives. `delete` + `set` on update makes it LRU for one line.
- **n5** — `app.js:923-937`: `onSubscribed` ignores `frame.backfilled`, the
  server's count of records it re-sent for the ack. It is the one number that
  tells the operator a rewind actually re-delivered something; it belongs on the
  meta line beside `duplicate frames ignored`.
- **n6** — `app.js:1352`: `chips.appendChild(chip("chip-plain",
  String(derived.reason || "")))` is unconditional, so a reducer payload without
  `reason` renders an empty chip. Every other optional field on that head is
  guarded; this one is not. (`agents.reduce` always sets `reason` today, so it is
  defensive only.)
- **n7** — `tests/test_touch_frontend.py:1152`: the driven harness stubs
  `setInterval: () => 0`, so `resync`, the `TASKS_MS` poll and the `HEALTH_MS`
  poll are never executed by the one non-source guard in the file. R-55's resume
  mechanism — the half the plan calls "a package" with absolute tokens — is
  still asserted only by source text. Make `setInterval` register into `timers`
  with a fire-once-per-`drain` semantics and drive one `resync` round trip.
- **n8** — `app.js:1884` + `style.css:323-327`: `#older` is prepended into its
  own 26 vh scroll box with no scroll management either, so a freshly loaded
  page of history lands above the current scroll position and the operator sees
  the same rows they saw before the click. Whatever M2's fix is, apply the
  mirror of it here (`list.scrollTop += <height of the inserted fragment>`, or
  simply `list.scrollTop = 0` after a prepend).

---

## What I attacked and could NOT break

Recorded so a fourth attempt does not re-litigate settled ground.

- **Escape-first (GD-20).** Still structural and still airtight: no markup sink
  in any spelling, the only text sink is `createTextNode`/`textContent`, the
  only attribute sinks are `setAttribute("title"|"href"|"data-view")`, `href` is
  always `"#" + <whitelisted kind> + "/" + encodeURIComponent(id)`, `data-view`
  comes from `route()`'s three-value whitelist, and every class is either a
  call-site literal or a `classOf()` whitelist hit. `test_no_markup_sink…`'s
  FRONTEND-1 regex is a real regex over the stripped source.
- **R-55's animation rule.** `classList.add(LIVE_CLASS)` appears exactly once,
  in `paint()`, gated on `live === true`; `loadOlder` builds `live:false`; the
  CSS sweep in the suite iterates every rule carrying `animation:` and is a real
  sweep. Verified in execution: replayed row has no `.fresh`, live row does,
  history rows do not.
- **Absolute tokens, no deltas.** Latest-per-ref with the `held.seq >
  totals.seq` guard; I confirmed the seq is present on both paths (`WsSession.
  frame` sends the raw store record and `h_events` returns raw records, both
  carrying `seq`), so a backfilled older record cannot regress a live counter —
  the failure I went looking for. 100-then-300 yields 300, verified.
- **Resume semantics.** `wsUrl` emits `?cursor=` from `state.resume` only;
  `resync` rewinds with `Math.min` semantics and never advances; the "ahead of
  this socket" refusal is classified as the coalescer's ordinary hold. Read line
  by line against `WsSession._advance` and `subscribe`.
- **GD-23.** No `Date.now`, no bare `new Date()`, no `performance.now`, no idle
  threshold, no `180`, no local reducer, `legacy.CLOSED_NO_VERDICT` absent from
  the source. Every badge string traced to a reducer field; `chip-current` is
  neutral and its title says why. The `agentTree` nesting is on `observed.parent`
  — a harness fact — and the session panel correctly *refuses* to join agents to
  a session and says so instead (`app.js:1545-1556`), which is the right call.
- **GD-22.** The mirror is a header chip and a title string; nothing renders
  conditionally on it, `/api/query` is unread, and with `/health` failing
  entirely the page still runs on the socket.
- **Wire-shape drift.** Re-walked every field the page reads against the
  counterparty: `SESSION_FIELDS`, `RECORD_FIELDS`/`STREAM_META_FIELDS`,
  `_node_payload`, `_agent_payload` (incl. `parent`/`root`/`sessions`),
  `h_run_graph`'s `derived` (state/closed/reason/label/nodes/verdicts/
  nodeCount/terminalObserved), `agents.reduce`'s node and agent payloads
  (display/frozen/attemptLabel/nextStage/verdict/unconventional),
  `_task_payload`'s `tokens` (cumulative per `legacy.TokenFold`, so
  latest-per-key is the right fold), `h_events`' `records/oldest/hasOlder`,
  `h_timeline`'s `sessionDoc/count/hasMore`. `PASSED`/`FAILED`/`DONE`/`RUNNING`
  literals match. `OLDER_PAGE` (200) and `TIMELINE_PAGE` (120) are inside
  `MAX_PAGE`. No drift found.
- **`before=0`.** `apiUrl` does not drop a numeric zero (only `""`/null/
  undefined), so an anchor at seq 0 sends `before=0`, gets an empty page, and
  the button hides. Correct.
- **Right calls on traps, again:** `runs.harnessTotals` projected and
  deliberately unrendered (the 1 089 990 over-count); no `POST/PUT/DELETE/PATCH`,
  no form, no control verb in code or markup, `CONTROL_ROUTES == {}`; the
  uninjected `__TOUCH_TOKEN__` treated as *no token* rather than sent;
  `credentials: "omit"`, `redirect: "error"`, `<meta name="referrer"
  content="no-referrer">`.
