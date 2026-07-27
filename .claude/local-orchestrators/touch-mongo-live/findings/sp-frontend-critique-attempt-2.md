# sp-frontend — adversarial critique, attempt 2

**Verdict: REJECTED** — 2 major, 4 minor, 7 nits. `depth: in-scope`,
`critical_defect: false`.

Reviewed (full content; the tree is untracked so `git diff` is empty for all
four):

- `/home/laniakea/Projects/touch/touch-visual/index.html` (111 lines)
- `/home/laniakea/Projects/touch/touch-visual/app.js` (1676 lines)
- `/home/laniakea/Projects/touch/touch-visual/style.css` (351 lines)
- `/home/laniakea/Projects/touch/tests/test_touch_frontend.py` (742 lines)

Against `plan/touch-mongo-live-subplans.md` §sp-13, amendment R-55 / GD-22 /
GD-23 / GD-28, base R-22 / R-32 (+ the R-30/R-31 wire contract restated), and
`aggregator/server.py` as the counterparty.

## Method

The suite is 25 static source guards; a source guard cannot tell you whether the
page *works*, so I re-built the attempt-1 harness and ran the page:

1. `python3 tests/test_touch_frontend.py` → green (232 ok, 0 FAIL), as reported.
2. `node --check touch-visual/app.js` → clean.
3. `<scratchpad>/smoke-c2.js` — fake DOM + `vm`, stubbed
   `document`/`window`/`fetch`/`WebSocket` — drove
   `hello → replay → mode → live burst → a NEW stream's anchors+backfill →
   a full log → three load-older clicks`.
4. `<scratchpad>/smoke-c3.js` — counts DOM churn in the two live regions across
   idle keepalive paints.

Both majors below were **observed in execution**, not argued.

Hygiene, checked and clean: `HEAD` is still `579446e`, no commit;
`find -newermt` shows only the four owned files touched;
`tests/test_touch_frontend.py` imports with a `pymongo` blocker installed in
`sys.meta_path` (GD-21); no `POST/PUT/DELETE/PATCH`, no form, no control verb,
`CONTROL_ROUTES == {}` (R-32/D13); `/api/query` is deliberately unread (GD-22).

All five attempt-1 findings are genuinely fixed, not papered over: per-source
error slots with `delete state.errors[source]` (M1), `resync`'s rewind-only
`Math.min` (m1), the non-truncating `refKey` byte-identical to
`TokenCoalescer.key_of` (m3), and `TASKS_MS` (m4). m2 is fixed in the sense that
the live tail is no longer evicted — but the replacement is inert, which is
**M1** below.

---

## MAJOR

### M1 — "load older" can never paint a row: whenever the button is visible the log is already full, so every click is a no-op that re-reads the stream file and inflates a made-up counter

`touch-visual/app.js:1478-1528` (`loadOlder`), `app.js:1438-1453`
(`renderOlder`), `app.js:1400-1427` (`flushLog`), against
`aggregator/server.py:1222-1247` (`replay_window`) and `server.py:1985-2081`
(`h_events`).

The attempt-1 fix for m2 replaced "evict the live tail" with "only prepend what
fits":

```js
const room = list ? Math.max(0, LOG_MAX - list.childElementCount) : rows.length;
const shown = room >= rows.length ? rows : rows.slice(rows.length - room);
```

`room` is **structurally always 0 at the moment the button exists.** The button
is revealed only by `anchor.truncated === true` (`app.js:1443`), and the server
sets `truncated` only when it cut records off a replay:
`truncated = len(window) < len(ordered)` with `window` capped at
`self.window` (500) or `MAX_REPLAY_EVENTS` (5000) — `server.py:1240-1245`. So a
stream can only be `truncated` after it has already pushed ≥ 500 event frames at
this client, and `LOG_MAX` is 400. The log is pinned at 400 rows from then on
(`flushLog` trims to the cap on every paint), there is no clear-log affordance,
and live frames keep it there. `room` is 0 forever, `shown` is `[]`, and the one
interactive affordance on the page does nothing for the rest of the tab's life.

Observed, with a 420-frame tail and `truncated:true` declared on the selected
stream:

```
=== C. full log, truncation declared ===
  log rows  = 400
  olderBtn hidden = false :: load older · run:wf_old
  click 1: rows=400 meta="403 shown · 22 older dropped · 200 older not shown (log full) · window 500"
  click 2: rows=400 meta="403 shown · 22 older dropped · 400 older not shown (log full) · window 500"
  click 3: rows=400 meta="403 shown · 22 older dropped · 600 older not shown (log full) · window 500"
  fetches of /api/events: …before=1000&limit=200 | …before=1000&limit=200 | …before=1000&limit=200
```

Three consequences, each on its own worth fixing:

1. **R-55's named frontend deliverable is inoperable.** "bounded default replay
   window, explicit `?from=` + load-older" — the load-older half exists in
   source and never executes. `test_load_older_never_evicts_the_live_tail`
   (`test_touch_frontend.py:613-638`) asserts every ingredient of the cause
   (`"LOG_MAX - list.childElementCount" in older`, `removeChild not in older`)
   and none of the effect, so the suite locks the dead behaviour in.
2. **`N older not shown (log full)` is a click counter, not a fact.** `withheld`
   accumulates 200 per click for the *same* 200 records, re-fetched with an
   identical `before=`. After ten clicks the page tells the operator 2 000
   records were withheld when there may be 200. `app.js:1508`.
3. **Each dead click re-reads the whole stream file server-side.**
   `h_events`' own docstring (`server.py:2007-2014`) records that every request
   is a `store.read_all` of the entire stream — this is the route the page now
   hammers for nothing, at the operator's click rate, in the process that also
   serves `/ws`.

And the prepend would still not survive if `room` were positive: `flushLog`
trims `list.firstChild` (`app.js:1410`), and the loaded-older rows are the front
of the list, so the very next live batch evicts them first and calls them
`older dropped`. The two ends genuinely need different budgets.

**Fix.** Give history its own room instead of borrowing the tail's:

- simplest structural fix — a second `<ol id="older" class="log">` above `#log`
  with its own cap (`OLDER_MAX`, say `2 * OLDER_PAGE`), prepended into and
  trimmed from its own far end; `flushLog` keeps owning `#log` untouched. The
  contiguity comment at `app.js:1465-1476` survives unchanged, and "the live
  tail is never evicted" stays literally true;
- or keep one list and make the cap history-aware: `state.olderShown` rows of
  loaded history raise the effective cap to `LOG_MAX + state.olderShown` in
  *both* `loadOlder`'s `room` and `flushLog`'s `while`, with `flushLog` trimming
  from the front only down to `LOG_MAX + state.olderShown` and decrementing the
  counter when it eats a loaded row;
- either way: when no room can be made, do **not** fetch — `if (!room) { note
  the reason; button.disabled = true; return; }` — so a dead click costs neither
  a `read_all` nor a fabricated 200;
- and add the guard the suite is missing, an *effect* guard rather than an
  ingredient one: assert `OLDER_PAGE + LOG_MAX <= <the cap loadOlder computes
  against>`, i.e. that a full log still leaves room for one page.

### M2 — a run that starts after the page connected never appears in the sidebar, while its rows and its tokens are already on screen

`touch-visual/app.js:714-717` (the only writer of `state.streams` /
`state.currentRun`), `app.js:970-996` (`renderRuns`), `app.js:1455-1459`
(`currentStream`), against `aggregator/server.py:1403-1411` (`streams()`) and
`server.py:1672-1679` (`tick`'s late-stream arm).

`state.streams` and `state.currentRun` are assigned in `onHello` and nowhere
else. `hello` is sent once per connection, and the socket only reconnects when
it drops. The server, meanwhile, re-evaluates `store.streams()` on **every
tick** and has a dedicated arm for a stream that came into existence after the
boundary — it emits an `anchors` frame naming it and then its backlog as
`live:false` backfill (`server.py:1674-1679`, whose comment says exactly this).
The page consumes those frames into the log, into `state.delivered` and into the
token rollup, and still refuses to list the run.

Observed:

```
=== B. after a NEW run stream appeared mid-connection ===
  runList:
    li.row selected :: wf_oldcurrentseq 1     <- only the run known at handshake
  runCount  = "1"
  rollup    = in 100  ·  out 5  ·  cached 0  ·  cache_write 0   <- wf_new's tokens
  log rows  = 3                                                  <- wf_new's rows
```

So the header counts the new run's tokens and the log prints its records while
the sidebar says there is one run. There is no `/api/runs` route
(`server.READ_ROUTES`), so the socket is the only channel that can teach the
page about a run — and it is being told and discarding it. Every other sidebar
section refreshes (`/api/sessions` at `REFRESH_MS`, `/api/tasks` at `TASKS_MS`);
the run list is the one that is frozen at connect, which is the tell.

Second-order, same root: `state.currentRun` also never moves, so

- the `current` chip stays on a run that has finished (`app.js:980`);
- `currentStream()` falls back to the stale run, so load-older and its label
  point at the wrong stream when nothing is selected (`app.js:1457`);
- `onHello`'s auto-select (`app.js:741-744`) can never re-target, because it is
  guarded on `!state.sel.kind` and only runs on a handshake.

For a page whose whole job is "watch the orchestrator run", "start a new run and
it does not appear" is the defect that matters most.

**Fix.** Learn streams from the frames that name them, which is one function:

```js
function noteStream(stream) {
    const s = String(stream || "");
    if (!s || state.streams.indexOf(s) !== -1) return;
    state.streams.push(s);
    state.streams.sort();
}
```

called from `onEvent` (`app.js:753`) and `onAnchors` (`app.js:788`). Do **not**
re-derive `currentRun` from it (that selection is the server's — GD-23);
instead render the freshly-seen run without the `current` chip, or have sp-12
re-publish `currentRun` on the `anchors` frame and consume it. Guard it:
feed `onAnchors`/`onEvent` a stream absent from `hello.streams` and assert
`state.streams` contains it — the suite currently has no behavioural guard at
all for the sidebar, only `check("runIdOf" in CODE …)`.

---

## MINOR

### m1 — `.notice { display: flex }` defeats `hidden`, so the "it empties itself" box is a permanent empty bar

`touch-visual/style.css:94-104`, with `app.js:915-919` and
`index.html:54`.

`renderNotices` does the right thing in the DOM — `box.hidden = true; clear(box)`
— but the UA rule `[hidden] { display: none }` is an *author-beats-UA* cascade
loss against `.notice { display: flex; … }`, and this stylesheet defines no
`[hidden]` reset anywhere (`grep -n "\[hidden\]" style.css` → nothing). So the
element keeps `display: flex` with its `padding: 8px 16px` and
`border-bottom: 1px solid var(--line)` and renders as an empty grey strip under
the header at all times. `.older` is safe by accident (it sets no `display`),
which is why the button's `hidden` works and this one does not.

**Fix.** One line, and it protects every future `hidden` too:

```css
[hidden] { display: none !important; }
```

Guard it in the suite the same way the dashed-border rules are guarded
(`test_touch_frontend.py:337`): assert the stylesheet defines a `[hidden]` rule,
or that no rule setting `display:` on a class also used with `hidden` exists.

### m2 — `#notice` is a second live region, and it *is* the "clear and rebuild on every paint" pattern attempt 1 removed from `#detail`; the guard that should catch it counts the wrong attribute

`touch-visual/index.html:54` (`role="status"`), `app.js:905-923`
(`renderNotices`), `tests/test_touch_frontend.py:664-678`.

`role="status"` is an implicit `aria-live="polite"` region — the same live
region `aria-live="polite"` creates. `renderNotices` unconditionally
`clear(box)`s and re-appends every line on every `render()`, and `render()` runs
on every debounced frame (including idle `tick` keepalives), every
`refreshModel`, every `refreshHealth`, every `select`. Measured over three idle
paints with a notice displayed:

```
notice DOM churn over 3 unchanged paints: +8 appends, +8 removes
detailStatus textContent writes over 3 unchanged paints: 0 (the setText guard)
```

`#detailStatus` is correct precisely because `setText` writes only on change
(`app.js:254-258`); `#notice` never got that treatment. A screen reader
re-announces the whole notice text — including the sticky "no per-boot token…"
line — indefinitely, at up to ~8 Hz during a frame burst.

The guard written to prevent this cannot see it:

```python
regions = re.findall(r'aria-live\s*=\s*"[^"]*"', HTML)
check(len(regions) == 1, f"the page declares exactly one live region — {regions}")
```

`role="status"` appears **twice** in `index.html` (lines 54 and 82), so the
assertion's claim ("exactly one live region") is false while the test passes.

**Fix.** Render the notice through the same change-guard: build
`lines.join("\n")`, compare with a stored `state.wire.noticeText`, and return
early when identical (or `setText` a single child and only toggle `hidden`).
Then correct the guard to count live regions properly:
`re.findall(r'(?:aria-live\s*=|role\s*=\s*"(?:status|alert|log)")', HTML)` and
assert each one is written through `setText`.

### m3 — R-32's "per-session agent tree" is not rendered: the session panel has no agents, and the run panel's agent list is flat although `parent`/`root` are in the payload

`touch-visual/app.js:1184-1215` (`renderRunDetail`'s agents section),
`app.js:1218-1270` (`renderSessionDetail`), against R-32 ("per-session agent
tree keyed per GD-7") and `aggregator/server.py:1157-1173` (`_agent_payload`).

Selecting a session renders `sessionDoc` facts plus a record timeline and no
agents at all. Selecting a run renders agents as a flat `<ul class="cards">`;
`observed.parent`, `observed.root` and `observed.sessions` are all projected by
`_agent_payload` and none of the three is read — only `spawnDepth` is printed,
as a bare `depth N` chip that shows the hierarchy exists without showing it.

GD-7 supersedes "P6's name-only tree", so the *keying* requirement (identity =
`(runId, key, ordinal)` / 17-hex `agentId`) is satisfied and the guard at
`test_touch_frontend.py:282-297` is checking the right thing. What is missing is
the nesting R-32 still names. Honest split of blame: the *per-session* half
needs a route sp-12 does not have (no agents-by-session endpoint exists in
`READ_ROUTES`) and should be recorded for that sub-plan rather than forced here;
the *tree* half is one attempt's work on data already in hand.

**Fix.** Nest the run panel's agent cards by `observed.parent` (roots =
`parent` absent or not present in the set), rendering depth as containment
rather than as a chip, and cap the recursion. Record the per-session gap in
`findings/` for sp-12/sp-15 rather than inventing a client-side join across
`agent.observed.sessions`.

### m4 — the `current` chip paints a liveness class onto a run the server selected by file mtime

`touch-visual/app.js:980`:

```js
if (stream === state.currentRun) meta.appendChild(chip("st-running", "current"));
```

`st-running` is the liveness vocabulary (`NODE_STATE_CLASS`, i.e.
`agents.NODE_STATES`) and here it is applied to a value that is explicitly not a
liveness verdict: `_current_run_stream` picks `max(runs, key=written_at)` by
`os.stat().st_mtime` and its docstring is emphatic — "A file mtime is not a
record timestamp, so this is not the ts-ordering GD-11 forbids: nothing is
*sequenced* by it, one stream is *selected* by it"
(`aggregator/server.py:1431-1467`). A finished run is still the most recently
written one, so the sidebar shows it in the running colour with no reducer
having said so. Small, but it is the GD-23 failure mode in miniature: a badge
the server did not conclude.

**Fix.** Use a neutral class — `chip("chip-plain", "current")`, or a new
`.chip-current` — and keep `st-*` for values that came out of the reducer. Worth
a guard: assert no `st-` class is attached outside a `classOf(...)` call.

---

## NITS

- **n1** — `app.js:428` `state.tokens` is the one unbounded collection in a file
  whose thesis is "capped from day one" (`LOG_MAX`, `SEEN_MAX`, the pending
  queue, `note()`'s 8). One entry per `(stream, ref)` across every stream the
  socket ever mentions. Cap it FIFO like `state.seen`, or key it per stream and
  drop a stream's map when it leaves `state.streams`.
- **n2** — `app.js:1414` the meta line's arithmetic does not close: the harness
  showed `403 shown` beside 400 rows and `22 older dropped`. `logCount` is
  "ever appended", `dropped` mixes the pending-queue trim (`app.js:1377`) with
  the DOM trim (`app.js:1411`). Say `N seen · M shown` and keep the two drops
  apart, or the operator cannot use any of the three numbers.
- **n3** — `app.js:1478` `loadOlder` has no re-entrancy guard beyond
  `button.disabled`; a keyboard `Enter` repeat or a programmatic call can
  overlap two fetches, and the second one's `anchor.oldest` write races the
  first's. A `let loadingOlder = false;` flag is the usual one line.
- **n4** — `app.js:860-866` `render()` clears and rebuilds `#sessionList`,
  `#runList`, `#taskList`, `#detailHead` and `#detailBody` in full on every
  paint — at minimum every 2 s during activity, up to ~8 Hz in a burst. Text
  selection in the detail panel dies each time, and a `mousedown`/`mouseup` that
  straddles a rebuild produces no click on a sidebar row. The file already
  invented the "write only when changed" idea for one line (`setText`); the
  sidebar deserves at least a `state`-hash short-circuit.
- **n5** — `app.js:1653-1666` a direct `#task/<id>` load fetches `/api/tasks`
  twice at boot: `refreshDetail`'s cold-start arm (`app.js:1585`) and
  `refreshTasks` (`app.js:1554`) both run, unordered. Await `refreshTasks()`
  before `refreshModel()` at boot, or let the cold-start arm reuse the in-flight
  promise.
- **n6** — `app.js:1256-1261` `stream_meta` rows are labelled `meta` purely by
  `record.collection`; R-47's own `render:false` field is projected
  (`STREAM_META_FIELDS`, `server.py:1106`) and ignored, so a row the ingest
  marked non-renderable looks the same as one it did not.
- **n7** — `app.js:488` `noteTokens` keys a ref-less token record as
  `stream + "|"`, collapsing every such record in a stream into one rollup slot.
  Defensive rather than observed, but the whole point of the attempt-1 `refKey`
  fix was that a collapsing key reads low; `if (!record.ref) return;` (and
  count it) is the consistent ending.

---

## What I attacked and could NOT break

Recorded so attempt 3 does not re-litigate settled ground.

- **Escape-first (GD-20).** Still structural: no markup sink of any kind, the
  only text sink is `createTextNode`/`textContent`, the only attribute sink
  `setAttribute`, every class from `classOf()` against a literal whitelist,
  `href` always `"#" + kind + "/" + encodeURIComponent(id)`. `data-view` is
  written from the routed `kind`, which `route()` validates against a
  three-value whitelist.
- **R-55's animation rule.** `classList.add(LIVE_CLASS)` appears exactly once,
  in `paint()`, gated on `live === true`; `loadOlder` builds rows with
  `live: false`; the CSS sweep in the suite is a real sweep over every rule
  carrying `animation:`.
- **Absolute tokens / no deltas.** Latest-per-ref with the
  `held.seq > totals.seq` out-of-order guard, no subtraction anywhere near a
  token field, `refKey` byte-identical to `TokenCoalescer.key_of` and
  untruncated — attempt 1's m3 is properly closed, and the harness confirmed
  100-then-300 yields 300.
- **Resume semantics (attempt 1's m1).** `wsUrl` emits `?cursor=` from
  `state.resume` only; `resync` sends
  `min(published, delivered)` so a resume position can rewind and never skip;
  `subscribe`'s `"ahead of this socket"` refusal is classified as the
  coalescer's ordinary hold and not surfaced. Cross-checked against
  `WsSession._advance` and `subscribe` line by line.
- **The notice/error model (attempt 1's M1).** Per-source slots, each cleared by
  the arm that set it, plus one sticky `bootError`; nothing is append-only any
  more. (Its *rendering* is m1/m2 above; the model itself is right.)
- **GD-23.** No `Date.now`, no bare `new Date()`, no `performance.now`, no idle
  threshold, no local reducer; `legacy.CLOSED_NO_VERDICT` asserted absent from
  the source. m4 is the only place a server word and a page class disagree.
- **GD-22.** Mirror state is a header chip and a title string; nothing renders
  conditionally on it and `/api/query` is unread. With `/health` failing
  entirely the page still runs on the socket.
- **GD-21.** JS side is irrelevant; the Python test file imports clean under a
  `sys.meta_path` pymongo blocker.
- **Wire-shape drift.** Every field the page reads exists in the counterparty's
  projection: `SESSION_FIELDS`, `RECORD_FIELDS`/`STREAM_META_FIELDS`,
  `_node_payload`, `_agent_payload`, `h_run_graph`'s `observed` tuple,
  `h_events`' `records/oldest/hasOlder`, `h_timeline`'s
  `sessionDoc/count/hasMore`. `OLDER_PAGE` (200) and `TIMELINE_PAGE` (120) are
  both inside `MAX_PAGE` (1000). The verbatim frame-block guard is a real
  cross-file comparison.
- **Right call on a trap, again:** `runs.harnessTotals` is projected and
  deliberately not rendered — correct, it is the 1 089 990 over-count R-26
  warns about.
