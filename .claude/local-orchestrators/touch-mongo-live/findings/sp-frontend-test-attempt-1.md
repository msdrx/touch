# sp-frontend — test gate, attempt 1

**Verdict: PASS.** Targeted suite 100 % green (186 `ok:`, 0 FAIL, 0 skip);
full-suite regression shows **no new failure** — the only two red files are the
character-for-character pre-existing baseline pair owned by sp-mirror-deploy and
sp-sessions-arm. Ownership clean, no commits.

Environment: Python 3.13, node available (used only as an out-of-band syntax
check), `TOUCH_MONGO_URI` unset, no services running.

Implementer's changed set — all four are sub-plan-owned per
`plan/touch-mongo-live-subplans.md` §"sp-13 — frontend":
`touch-visual/index.html`, `touch-visual/app.js`, `touch-visual/style.css`,
`tests/test_touch_frontend.py`.

---

## 1. Targeted suite (sp-frontend owned) — GREEN

```
$ cd /home/laniakea/Projects/touch && python3 tests/test_touch_frontend.py
… 186 `ok:` lines, 0 FAIL, 0 SKIP
all touch-visual source guards passed
exit 0
```

* 17 `def test_*` in the file, 17 referenced from `main()` — no orphaned test.
* 111 `check(...)` call sites.
* **Zero skips.** Unlike the Mongo-touching suites, this one has no conditional
  arm to hide behind: it reads three files off disk and three sibling modules.

## 2. Full-suite regression gate

```
$ cd /home/laniakea/Projects/touch && rc=0; \
  for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done; \
  for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc
```

- **PASS (23):** the monitoring baseline four (`test_frontend`, `test_server`,
  `test_shell`, `test_watcher`, each run from their own dir) plus
  `test_agents`, `test_api`, `test_bootstrap`, `test_custom_state`,
  `test_fixtures`, `test_ingest`, `test_legacy`, `test_mongo_deploy`,
  `test_mongo_store`, `test_reducer`, `test_refs`, `test_server_core`,
  `test_slots`, `test_stdlib_only`, `test_store`, `test_tailer`,
  `test_touch_frontend`, `test_usage`, `test_ws`.

- **FAIL (2) — pre-existing baseline, NOT attributable:**
  - `tests/test_mirror.py` rc 1, `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` rc 1, `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

  Attribution argument:
  1. **String-identical** to the baseline recorded in
     `sp-server-api-test-attempt-4.md` (§FAIL(2)) and before it in
     `sp-custom-state-test-attempt-4.md` and `sp-agents-reducer-test-attempt-4.md`.
     Same suites, same count, same messages.
  2. **No causal path.** The failures live in `aggregator/mirror.py` and
     `aggregator/sessions.py`. Nothing under `touch-visual/` is imported,
     executed or read by those two suites — the frontend is three static assets
     plus a source-guard test; it exports no Python symbol.
  3. **Mtimes.** The only files modified in this attempt's window are
     `index.html` 20:40:07, `style.css` 20:50:21, `test_touch_frontend.py`
     20:50:21, `app.js` 20:51:56. `mirror.py`/`sessions.py` and their suites are
     hours older and untouched.
  4. Both suites reach their `TOUCH_MONGO_URI is not set` arm and **skip
     cleanly** — the GD-21/R-56 no-mongod behaviour holds; the failures are not
     "missing pymongo/mongod" leakage.
  5. `tests/test_stdlib_only.py` is green: the frontend added no third-party
     import and no new module registration.

  Baseline failures do not fail this gate. **No new failure anywhere.**

## 3. Item verification against the plans

`plan/touch-mongo-live-subplans.md` §sp-13 items, each present in the tree and
covered by a non-tautological assertion:

| item | evidence |
|---|---|
| **R-22:frontend** (skeleton half) | `touch-visual/{index.html,app.js,style.css}` exist (99 / 1463 / 340 lines). `test_the_three_files_are_where_the_server_serves_them_from` also asserts `server.py` names all three and that `server_mod.CONTROL_ROUTES == {}` — a real cross-file check, not a file-exists tautology. Confirmed independently: `aggregator/server.py:2391` `CONTROL_ROUTES = {}`. |
| **R-32** sidebar kinds (GD-14) | `index.html` has three lists — `sessionList`, `runList`, `taskList` (sessions incl. historical, runs, legacy task folders); `app.js:840/867/895` render each. |
| **R-32** agent tree keyed per GD-7 | `test_the_agent_tree_is_keyed_by_harness_facts`. |
| **R-32** token rollups from computed sums | `test_token_rollups_are_sums_of_absolute_records`; header `#rollup` is titled "absolute token records, summed per ref". |
| **R-32** escape-first (GD-20) | `test_no_markup_sink_exists_in_the_page`. Verified independently against the comment-stripped source: `innerHTML` 0, backtick 0, `${` 0, `eval(`/`new Function`/`document.write`/`srcdoc` 0; `createTextNode` present. The raw-file hits (1 `innerHTML`, 62 backticks) are all inside comments — the test's `strip_js_comments` is itself covered by a meta-test (`test_the_stripper_sees_this_file_the_way_it_claims`) with a hand-built sample. |
| **R-32** render coalescing + capped log from day one | `test_the_render_is_coalesced_and_the_log_is_capped`: debounce + `requestAnimationFrame`, `LOG_MAX = 400` (named constant), DOM trim, single-fragment burst append, capped pending queue, a displayed drop counter, and onmessage/onopen/onclose teardown. |
| **R-32** every degraded/derived state labelled | `PROV_CLASS` (`harness`/`derived`/`asserted`/`touch`/`unknown`/`legacy`) at `app.js:134-139`, rendered as chips (`prov-unknown` "no transcript", `prov-derived` "promoted"/"window truncated", `prov-legacy`); `test_degraded_and_derived_states_are_labelled` plus the CSS-side check that every `.prov-*` and `.st-*` class is actually defined in `style.css`. |
| **R-32** NO control affordance in v0 | `test_no_control_verb_reaches_the_page` (verb ladder absent from all three files) + the empty server route group. The footer states it in prose rather than leaving it to inference. |
| **R-55:frontend** replay/backfill paints once | `test_only_live_frames_animate`: `LIVE_CLASS = "fresh"` attached in exactly one place, inside `paint()` gated on `live === true`; `loadOlder` passes `live: false`; **and** a CSS sweep that walks every rule with an `animation:` declaration and requires its selector be `.fresh` or the live-tail chip — so a future animation added elsewhere fails the gate. `prefers-reduced-motion` honoured. |
| **R-55:frontend** wire contract restated verbatim | `test_the_wire_contract_is_restated_verbatim` slices the frame block out of **both** `aggregator/server.py` and `app.js` and compares line-by-line — a genuine sp-12⇄sp-13 contract test that breaks if either side drifts. |
| **R-55:frontend** `(stream,seq)` resume, absolute tokens | `test_the_resume_cursor_is_the_servers_not_ours`: cursor grammar `padStart(12,"0")` matching `store.cursor_key`; resume comes from server-published cursors adopted only in `onMode`/`onSubscribed`; explicitly asserts `state.delivered` is **not** used as the resume point (the held-token-record skip bug); dedup by `(stream,seq)` bounded at `SEEN_MAX = 20000`. |
| **R-55:frontend** load-older anchors | `test_the_load_older_anchors_come_from_the_frames_that_know_them`: `hello` carries none, `mode`/`anchors` supply them, button hidden unless `truncated`, backwards walk via `/api/events?stream=&before=`. |
| **GD-23** the frontend never re-derives | `test_the_page_never_infers_state`: no `Date.now()` / bare `new Date()` / `performance.now()`, no idle threshold, no 180 s liveness constant, no reducer defined in `app.js`; the reducer's version is displayed and the page refetches the reduction. |
| **GD-22** degrade without Mongo | `test_the_page_degrades_without_mongo_and_says_why`: mirror state read from `/health` and rendered as a label; `app.js` does **not** call `/api/query`. |
| **GD-13** token | `test_the_page_carries_the_serve_time_token_where_it_is_valid`: `__TOUCH_TOKEN__` in a meta tag and on the two sub-resource URLs (a `<script src>` cannot carry a header); an un-injected placeholder is treated as *no token* rather than sent. |

## 4. Non-tautology proof (mutation probes)

Because this suite is entirely static source guards, I verified it is not
vacuous by mutating a **scratchpad copy** of the three assets (repo untouched —
`/tmp/.../scratchpad/mut`, with `aggregator/` symlinked in; the unmutated copy
runs green there). Each mutation was reverted after its run.

| probe | mutation | result |
|---|---|---|
| M1 | `paint(li, entry.live)` → `paint(li, true)` (hardcode a live paint on a replayed row) | **caught** — 2 FAILs: "a log row's animation is decided by the frame's own live flag", "no call site hardcodes a live paint" |
| M2 | insert `const NOW = Date.now();` (page infers time) | **caught** — 1 FAIL: "no Date.now(): the page never asks what time it is (GD-23)" |
| M3 | delete `running: "st-running"` from `NODE_STATE_CLASS` | **caught** — 1 FAIL: "NODE_STATE_CLASS == agents.NODE_STATES" |

The copy returned green after restore, so the probes isolate the mutations.

## 5. Ownership

`git status` + mtimes: the only non-`.claude/` files modified in the attempt
window (20:40–20:52) are the four owned ones. `aggregator/server.py` (20:17) and
everything else predate it. `touch-visual/` and `tests/` are still untracked
directories from the bootstrap pass — **no commit was made by this gate**, and
none by the implementer (HEAD is still `579446e`). No file belonging to another
sub-plan was edited, including the two RED loops' `mirror.py`/`sessions.py`.

## 6. Observations (non-blocking, not gate failures)

1. **No execution-level test exists for `app.js`.** Every assertion is a source
   guard, which is exactly what §sp-13 specified ("source guards: no animation
   class on non-live frames, no state-inference in `app.js`") and it is
   defensible for a browser asset in a stdlib-only, pytest-less repo. Still, the
   guards can only prove *shape*, not *behaviour*: a correctly-shaped
   `adoptCursors` that assigns the wrong stream would pass. Out of band I ran
   `node --check touch-visual/app.js` → **SYNTAX OK**, which at least rules out a
   file that cannot parse; note the suite itself does not do this, so a syntax
   error introduced later would ship green. If sp-14/e2e wants one cheap
   upgrade, a `node --check` line in this suite (guarded by
   `shutil.which("node")`, skipping cleanly when absent, per the no-third-party
   rule) would close that hole without adding a dependency.
2. `slice_fn`-based tests are anchored on function names (`function paint(`,
   `function onMode`). A rename that keeps behaviour identical will raise an
   `AssertionError` ("marker not found") rather than report a clean FAIL. That
   is fail-loud, so it is acceptable, but a future refactor of `app.js` will
   have to update the markers in step.
3. The 2 baseline failures remain open and are, as recorded across four prior
   gates, sp-mirror-deploy's and sp-sessions-arm's to fix. They are unchanged by
   this pass and are re-stated here only to keep the baseline auditable.
