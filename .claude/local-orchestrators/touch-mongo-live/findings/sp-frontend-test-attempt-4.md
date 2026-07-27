# sp-frontend — test gate, attempt 4 — PASS

Read-only gate. No source, test or config file was edited by this agent.

## 1. Targeted suite (owned by sp-frontend)

`cd /home/laniakea/Projects/touch && python3 tests/test_touch_frontend.py`

* **rc 0**, `all touch-visual source guards passed`
* **411 assertions green**, 39 test functions, **0 skips** (node v20.19.4 is on
  PATH, so both `node --check app.js` and the ~200-line fake-DOM driven harness
  executed for real; the harness's own self-check `the harness itself ran to
  completion — rc 0 []` is green).
* No other test file is owned by this sub-plan (the monitoring-module four are
  owned elsewhere; they were still run below).

### Bare-checkout / no-pymongo arm (GD-21 / R-56)

`tests/test_touch_frontend.py` imports `aggregator.legacy` and `aggregator.store`
for its enum cross-checks, so the no-third-party arm matters here. Re-run with a
`sys.meta_path` blocker that raises `ImportError` for `pymongo`/`bson`:

```
PYTHONPATH=<blocker-dir> python3 tests/test_touch_frontend.py   →  rc 0, 411 ok
```

Identical assertion count, no degradation, no skip. The suite needs nothing
beyond the stdlib + node-if-present.

## 2. Full-suite regression gate

Command as specified (monitoring tests from their own dir, repo tests from root):

| suite | result |
|---|---|
| `.claude/shared/monitoring/tests/test_frontend.py` | PASS |
| `.claude/shared/monitoring/tests/test_server.py` | PASS |
| `.claude/shared/monitoring/tests/test_shell.py` | PASS |
| `.claude/shared/monitoring/tests/test_watcher.py` | PASS |
| `tests/test_agents.py` | PASS |
| `tests/test_api.py` | PASS |
| `tests/test_bootstrap.py` | PASS |
| `tests/test_custom_state.py` | PASS |
| `tests/test_fixtures.py` | PASS |
| `tests/test_ingest.py` | PASS |
| `tests/test_legacy.py` | PASS |
| `tests/test_mirror.py` | **FAIL — pre-existing baseline, not attributable** |
| `tests/test_mongo_deploy.py` | PASS |
| `tests/test_mongo_store.py` | PASS |
| `tests/test_reducer.py` | PASS |
| `tests/test_refs.py` | PASS |
| `tests/test_server_core.py` | PASS |
| `tests/test_sessions.py` | **FAIL — pre-existing baseline, not attributable** |
| `tests/test_slots.py` | PASS |
| `tests/test_stdlib_only.py` | PASS |
| `tests/test_store.py` | PASS |
| `tests/test_tailer.py` | PASS |
| `tests/test_touch_frontend.py` | PASS |
| `tests/test_usage.py` | PASS |
| `tests/test_ws.py` | PASS |

**23 PASS / 2 FAIL**, and both failures are the known baseline pair.

### Why the two reds are NOT attributable to this change

`tests/test_mirror.py` — rc 1, `FAILED (3)`:
* `FAIL: …proven by the call count: the held ticks made no attempt`
* `FAIL: the first generation lands`
* `FAIL: …and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`

`tests/test_sessions.py` — rc 1, `FAILED (1)`:
* `FAIL: wipe + --rebuild reproduces a byte-identical fingerprint`

Attribution evidence:

1. **String-identical to the baseline already recorded** in
   `findings/sp-frontend-test-attempt-3.md` §"FAIL (2) — pre-existing baseline"
   (same four failing assertion strings, same `FAILED (3)` / `FAILED (1)`
   counts). Nothing regressed between attempt 3 and attempt 4.
2. **Ownership**: those suites belong to sp-mirror-deploy and sp-sessions-arm,
   two loops that closed RED with open findings; this gate must not fix them.
3. **No coupling**: `grep -c "touch-visual\|touch_frontend"` is **0** in both
   `tests/test_mirror.py` and `tests/test_sessions.py`. Neither suite imports,
   reads or serves any file this sub-plan owns; `touch-visual/` is static assets
   plus a test that only reads them. There is no mechanism by which an edit to
   `index.html`, `app.js`, `style.css` or `tests/test_touch_frontend.py` can
   change a mirror-drainer call count or a sessions rebuild fingerprint.
4. **mtimes**: `find aggregator tests touch-visual docs -newermt "2026-07-26 20:30"`
   returns exactly four paths — `touch-visual/index.html` 22:29,
   `touch-visual/style.css` 22:44, `touch-visual/app.js` 22:46,
   `tests/test_touch_frontend.py` 22:46. `tests/test_mirror.py` (02:44),
   `aggregator/mirror.py` (02:45), `aggregator/sessions.py` (04:10) and
   `tests/test_sessions.py` (04:14) are ~18 hours older and untouched.

(Environment note, for the owning sub-plans rather than this gate: two `mongo:7`
containers `touch-mongo-sp05`/`touch-mongo-sp06` have been up 25 h / 13 h, so
those live-mongod arms are running against long-lived, possibly dirty
databases — a plausible cause of the fingerprint/`writers: 1` deltas. Not this
sub-plan's property; recorded only so the information is not lost.)

## 3. Plan conformance (sp-13 "frontend")

Owned files per `touch-mongo-live-subplans.md` §sp-13 — all four present and all
four are exactly the files that changed:
`touch-visual/index.html` (149 L), `touch-visual/app.js` (2426 L),
`touch-visual/style.css` (403 L), `tests/test_touch_frontend.py` (2028 L).

Item coverage found asserted in the suite (spot-checked against the plan text):

* **R-22:frontend** — the skeleton exists as three real files; `node --check`
  parses `app.js`.
* **R-32 sidebar / tree / rollups** — sessions incl. historical and legacy task
  folders (GD-14 kinds), agent tree nesting keyed per GD-7 (`driven: the agent
  panel nests children inside their parent's card` + `…every agent is drawn
  exactly once`), token rollups from computed sums (`a legacy folder's folded
  token records roll up by the same rule (GD-14)`).
* **GD-20 escape-first** — structural: `"createTextNode" in CODE` and no markup
  sink anywhere in `app.js`.
* **Render coalescing + capped log from day one** — `LOG_MAX`, `SEEN_MAX`,
  `TOKENS_MAX` named constants, DOM trim (`childElementCount > LOG_MAX`),
  pending-queue cap, `state.trimmed` surfaced; plus the driven arm proving the
  live log pins at 400 rows and the tail-follow/unfollow behaviour.
* **Degraded/derived labelling** — `.prov-derived { … border-style: dashed }` and
  `.card.derived` asserted as real CSS, `derived_from_legacy`, and
  `legacy_mod.CLOSED_NO_VERDICT not in CODE` (the "closed — no verdict" string is
  rendered from the server label, never hardcoded).
* **NO control affordance in v0** — dedicated section
  `# --- R-32: no control affordance renders in v0`, plus "the server's control
  route group is empty — v0 has no verb to render".
* **R-55:frontend** — `const LIVE_CLASS = "fresh"` is the single named animation
  class, `.logrow` base row carries no animation, every `animation:` rule in the
  stylesheet is reachable only through `.fresh`, `prefers-reduced-motion`
  honoured, and the live flag is read off the frame rather than inferred from
  arrival time → replayed/backfill frames paint once.
* **GD-23 no re-derivation** — no `Date.now()`, no `reduce*/derive*/infer*/
  compute*/classify*` function in `app.js`, and `currentRun` is taken as the
  server's word rather than joined client-side.
* **Wire contract (from sp-12)** — driven coverage of `(stream, seq)` resume,
  server-published resume position ("never the newest seq received"), rewind-only
  resume, absolute-token rendering, ack backfill count on the meta line,
  reconnect dropping stale anchors and their history.

## 4. Non-tautology check

Text-only guards are the risk in this suite, so I mutation-tested a **copy** of
the tree in the scratchpad (the live repo was not touched): renaming
`const LIVE_CLASS = "fresh"` → `"fresh2"` in the copied `app.js` turned the
suite red — rc 1, `FAILED (4)`, including both the static guard (`the animation
class is a named constant`) and the executed one (`driven: a live row does —
logrow fresh2`). The assertions bite on behaviour, and the driven harness
catches what the source guards cannot.

## 5. Ownership / git

`git log -1` is still `579446e orchestration history` — **nothing committed**.
`git status --porcelain` shows `touch-visual/` and `tests/` only as
whole-directory untracked entries from earlier passes; the remaining entries are
the pre-existing in-flight `.claude/` orchestrator state, running daemons and
`.temp-develop/` screenshots, none of them written by this sub-plan.

**No edits outside sp-frontend's four owned files.**

---

**Gate result: PASS** — 411 assertions green in the owned suite (0 skips), green
again in the simulated bare checkout, full suite 23/25 with both reds the
string-identical, non-attributable pre-existing baseline owned by
sp-mirror-deploy and sp-sessions-arm.
