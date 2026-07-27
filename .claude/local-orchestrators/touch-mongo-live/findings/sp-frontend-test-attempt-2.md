# sp-frontend — test gate, attempt 2

**Verdict: PASS.** Targeted suite 100 % green (25 tests, 232 `ok:`, 0 FAIL,
0 skip). Full-suite regression: **no new failure** — the only two red files are
the character-for-character pre-existing baseline pair owned by
sp-mirror-deploy and sp-sessions-arm. Ownership clean, no commits (HEAD is
still `579446e`).

Environment: Python 3.13, `TOUCH_MONGO_URI` unset, no services running, node
present on PATH (now used *inside* the suite, see §4).

Implementer's changed set — all four are sub-plan-owned per
`plan/touch-mongo-live-subplans.md` §"sp-13 — frontend":
`touch-visual/index.html`, `touch-visual/app.js`, `touch-visual/style.css`,
`tests/test_touch_frontend.py`.

---

## 1. Targeted suite (sp-frontend owned) — GREEN

```
$ cd /home/laniakea/Projects/touch && python3 tests/test_touch_frontend.py
… 232 `ok:` lines, 0 FAIL, 0 SKIP
all touch-visual source guards passed
exit 0
```

* 25 `def test_*`, all 25 listed in `main()`'s tuple — no orphaned test
  (up from 17 tests / 186 `ok:` at attempt 1).
* 156 `check(...)` call sites.
* Zero skips on this host. The one conditional arm
  (`test_the_page_parses_as_javascript`) is guarded by `shutil.which("node")`
  and prints a loud `skip:` where node is absent — it does not weaken the
  static guards, which still run.

## 2. Full-suite regression gate

```
$ cd /home/laniakea/Projects/touch && rc=0; \
  for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done; \
  for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc
```

- **PASS (23):** the monitoring baseline four (`test_frontend`, `test_server`,
  `test_shell`, `test_watcher`, each from their own dir) plus `test_agents`,
  `test_api`, `test_bootstrap`, `test_custom_state`, `test_fixtures`,
  `test_ingest`, `test_legacy`, `test_mongo_deploy`, `test_mongo_store`,
  `test_reducer`, `test_refs`, `test_server_core`, `test_slots`,
  `test_stdlib_only`, `test_store`, `test_tailer`, `test_touch_frontend`,
  `test_usage`, `test_ws`.

- **FAIL (2) — pre-existing baseline, NOT attributable:**
  - `tests/test_mirror.py` rc 1, `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` rc 1, `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

  Attribution:
  1. **String-identical** to the baseline recorded in
     `sp-frontend-test-attempt-1.md` §2 and, before it, in
     `sp-server-api-test-attempt-4.md`, `sp-custom-state-test-attempt-4.md`
     and `sp-agents-reducer-test-attempt-4.md`. Same suites, same counts, same
     messages.
  2. **No causal path.** The failures live in `aggregator/mirror.py` and
     `aggregator/sessions.py`. Nothing under `touch-visual/` is imported, read
     or executed by those suites; the frontend exports no Python symbol.
  3. **Mtimes.** The only non-`.claude/` files touched in this attempt's window
     are `app.js` 21:17:11, `index.html` 21:17:37, `style.css` 21:17:55,
     `tests/test_touch_frontend.py` 21:20:43 (UTC). `aggregator/mirror.py`
     11:29, `aggregator/sessions.py` 04:10, `tests/test_mirror.py` 02:44,
     `tests/test_sessions.py` 04:14 — all hours older, untouched.
  4. Both suites reach their `TOUCH_MONGO_URI is not set` arm and **skip
     cleanly** (`skip: live Mongo arm: TOUCH_MONGO_URI is not set (R-42's
     loopback+auth recipe)`) — the GD-21/R-56 no-mongod behaviour holds; these
     are logic failures, not missing-pymongo/mongod leakage.
  5. `tests/test_stdlib_only.py` green — the frontend added no third-party
     import and no module registration.

  Baseline failures do not fail this gate. **No new failure anywhere.**

## 3. Attempt-1 critique findings — all five verified fixed in source

Checked against the source, not only against the new test names.

| id | fix in tree | new guard |
|---|---|---|
| **M1** append-only notice/error surface | `state.errors` is now a **per-source slot map** (`app.js:450`) written only through `setError(source, message)` (`app.js:460-463`), which *deletes* the slot on `null`. Every arm clears its own slot on success (e.g. `setError("load older", null)` on the success path). Wire notices are reset per connection: `state.wire.notices = []` at the top of `connect()` (`app.js:584`), with the rationale in a comment — a notice is scoped to one handshake. A single sticky `bootError` remains, correctly, for "this document was not served by Touch". | `test_the_notice_surface_states_the_current_cycle` (`failures live in per-source slots that can be cleared`; `a /health failure is named, and un-named again when a poll succeeds`) |
| **m1** `resync()` sent `state.delivered` | `resync()` now sends `Math.min`-style logic: `cursors[stream] = (got !== undefined && got < published) ? got : published;` (`app.js:655-663`) — the position may rewind, never advance. | `test_the_resync_never_asks_to_be_moved_forward` (`a lower value is sent only when a frame was actually missed…`) |
| **m2** `loadOlder` evicted the newest rows | Prepend is capped by the room left under `LOG_MAX` (`room = max(0, LOG_MAX - childElementCount)`), the kept slice is taken from the **newest** end (`rows.slice(rows.length - room)`), the remainder is counted as `state.withheld` with its own label, and the anchor rolls back only as far as actually painted (`app.js:1490-1520`). `OLDER_PAGE = 200` is half `LOG_MAX = 400`. | `test_load_older_never_evicts_the_live_tail` (9 checks) |
| **m3** truncating formatter used as rollup identity | `refKey()` (`app.js:368`) is a separate, non-truncating identity joiner mirroring `server.TokenCoalescer.key_of`; `noteTokens` buckets on `stream + "|" + refKey(record.ref)` (`app.js:488`). `refSummary()` stays a display formatter. | `test_the_token_rollup_key_is_an_identity_not_a_display_string` |
| **m4** `/api/tasks` polled on the live cadence | `TASKS_MS = 30000` (`app.js:101`), wired separately at boot (`app.js:1667`); the live-model refresh explicitly excludes `/api/tasks` (comment at `app.js:1535`), and a selected task is served from the list already in hand, with at most one cold-start fetch for a direct `#task/<id>` load. | `test_the_expensive_route_is_polled_on_its_own_slow_cadence` |

Two attempt-1 observations were also closed by the implementer: the suite now
runs `node --check` itself (`test_the_page_parses_as_javascript`, skipping
cleanly without node), and the `aria-live` region was narrowed to the one-line
`#detailStatus` (`test_the_live_region_is_a_status_line_not_the_rebuilt_panel`).

## 4. Item verification against the plans

`plan/touch-mongo-live-subplans.md` §sp-13 items — every owned item present in
the tree and covered by a non-tautological assertion. Attempt-1's table (§3 of
`sp-frontend-test-attempt-1.md`) still holds verbatim for R-22:frontend, R-32
(sidebar kinds / agent tree / rollups / escape-first / coalescing + capped log
/ degraded labelling / no control affordance), R-55:frontend (paint-once,
verbatim wire contract, `(stream,seq)` resume with absolute tokens, load-older
anchors), GD-23, GD-22 and GD-13 — re-confirmed green this run, with the eight
new tests above added on top. Spot-checks re-run independently this attempt:
`aggregator/server.py` `CONTROL_ROUTES == {}`; the three assets are 1676 / 111 /
351 lines; no `POST/PUT/DELETE/PATCH` and no control verb in any of the three.

## 5. Non-tautology proof (mutation probes)

The suite is static source guards, so I re-proved it is not vacuous by mutating
a **scratchpad copy** (`<scratchpad>/mut2`, `aggregator/` symlinked in; the
unmutated copy runs green there). Repo untouched; each mutation reverted after
its run.

| probe | mutation | result |
|---|---|---|
| P1 | `setError` early-returns on `null` (never clears) | **caught** — `failures live in per-source slots that can be cleared` |
| P2 | `resync` sends `delivered` unconditionally (can skip forward) | **caught** — `a lower value is sent only when a frame was actually missed…` |
| P3 | `noteTokens` buckets on `refSummary` instead of `refKey` | **caught** — `noteTokens buckets by the identity, never by the display formatter` |
| P4 | `refreshTasks` wired on `MODEL_MS` | **caught** — `the slow poll is wired at boot` |
| P5 | `loadOlder` keeps `rows.slice(0, room)` (oldest end) | **caught** — `what fits is taken from the NEWEST end of the page…` |
| P6 | add `dom.conn.innerHTML = label` in `setConn` | **caught** — `app.js contains no innerHTML (agent-authored text is data)` |

6/6 caught; the copy returned green after every restore, so each probe isolates
its mutation. All five critique fixes are therefore guarded, not merely present.

## 6. Ownership

`git status` + mtimes: the only non-`.claude/` files modified in the attempt
window (21:17–21:21 UTC) are the four owned ones. `aggregator/server.py`
(20:17) and every other aggregator/test file predate it. `touch-visual/` and
`tests/` remain untracked directories from the bootstrap pass — **no commit was
made** by the implementer or by this gate (`HEAD` = `579446e`). No file
belonging to another sub-plan was edited, including the two RED loops'
`mirror.py` / `sessions.py`.

## 7. Observations (non-blocking)

1. Guards still prove *shape*, not *behaviour*: a correctly-shaped
   `adoptCursors` assigning the wrong stream would pass. `node --check` now
   rules out an unparseable file, which was attempt-1's main gap. A real
   execution harness (the fake-DOM `vm` smoke the attempt-1 critic wrote) would
   be the next upgrade and belongs to sp-14/e2e, not here.
2. `slice_fn`-anchored tests key on function names (`function paint(`,
   `function onMode`). A behaviour-preserving rename raises `AssertionError`
   ("marker not found") rather than a clean FAIL — fail-loud, acceptable, but a
   future `app.js` refactor must move the markers in step.
3. The 2 baseline failures remain open and are, across five prior gates,
   sp-mirror-deploy's and sp-sessions-arm's to fix. Restated only to keep the
   baseline auditable.
