# sp-server-api — test gate, attempt 2

**Verdict: PASS.** Both owned suites 100 % green, the attempt-1 regression is
gone, the full suite shows only the two known baseline failures, ownership is
clean, and no commit was made.

Environment: Python 3.13, pymongo 4.17.0 present, Docker daemon available
(`mongo:7` local), `TOUCH_MONGO_URI` unset, no services running.

Implementer's changed set (all three sub-plan-owned — no violation):
`aggregator/server.py`, `tests/test_server_core.py`, `tests/test_api.py`.

---

## 1. Targeted suites (sp-server-api owned) — GREEN

Run from the repo root as standalone executables:

| suite | rc | `ok:` assertions | banner |
|---|---|---|---|
| `tests/test_server_core.py` | **0** | 150 | `all server core tests passed` |
| `tests/test_api.py` | **0** | 128 | `all read API + wire tests passed` |

Assertion counts grew over attempt 1 (127 → 150, 102 → 128) — the fix did not
come at the cost of coverage. Assertions remain behavioural, not tautological:
they bind real sockets and exercise the real wire contract, e.g. GD-13
(`an unauthenticated API call is 401 on the wire`, `a cross-origin WS upgrade
is 403`, `an unsupported WS version is 426 advertising 13, never the page
body`, `the accept key is RFC 6455's`, `nosniff is on the wire`); R-55 replay/
resume (`the bounded replay arrives with live:false`, `exactly one mode frame
marks the replay->tail boundary`, `a reconnect replays exactly the records
after the client's (stream, seq)`, `no record is delivered twice`, `the union
of the two sessions equals a full replay — no gap either`, and its
`(stream, seq)` cursor, so the client can resume from it); absolute-token
coalescing (`the LAST absolute value … never a sum of deltas`, `coalescing is
per (stream, ref)`); R-45 health (`the R-45 mirror block is served verbatim`,
`a tailer whose target is gone is visible (AUDIT-15)`, `no mirror configured
reports absent — never an error (GD-22)`); route-table posture (`a garbage
head does not raise — it yields no method, which the table 404s`).

## 2. Full suite regression gate — PASS (no new failures)

`for t in .claude/shared/monitoring/tests/test_*.py … ; for t in tests/test_*.py …`
at the repo root, no services running, `TOUCH_MONGO_URI` unset. 24 files run.

- **PASS (22):** monitoring `test_frontend`, `test_server`, `test_shell`,
  `test_watcher`; repo `test_agents`, `test_api`, `test_bootstrap`,
  `test_custom_state`, `test_fixtures`, `test_ingest`, `test_legacy`,
  **`test_mongo_deploy`**, `test_mongo_store`, `test_reducer`, `test_refs`,
  `test_server_core`, `test_slots`, `test_stdlib_only`, `test_store`,
  `test_tailer`, `test_usage`, `test_ws`.
- **FAIL (2) — pre-existing baseline, NOT attributable:**
  - `tests/test_mirror.py` rc 1, `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` rc 1, `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

  Character-for-character identical to the baseline recorded in
  `sp-server-api-test-attempt-1.md` §2 and `sp-custom-state-test-attempt-4.md`
  §2, predating this attempt. They belong to the loops that closed RED
  (`sp-mirror-deploy`, `sp-sessions-arm`) and their files were not touched
  here. Baseline failures do not fail this gate.

### The attempt-1 regression is FIXED

`tests/test_mongo_deploy.py` now exits **0** (including the live `mongo:7`
Docker arm). `grep -n 27017 aggregator/*.py` yields exactly one hit —
`aggregator/mirror.py:1791`, a genuine `#` comment owned by another sub-plan.
`aggregator/server.py` no longer spells a mongod port anywhere; the `_usage()`
text at line 2396 now reads

```
"                   Never publish the mongod port (GD-27: Mongo stays\n"
```

i.e. the advice is preserved and the numeral is gone. The guard was **not**
worked around by inserting a `#` before the literal (the escape hatch the
attempt-1 findings warned against) — the literal is simply absent.

## 3. Bare-checkout / no-third-party arm (GD-21 / R-56)

Re-ran with `pymongo` shadowed by a stub that raises `ImportError` on import
(`PYTHONPATH` shim): `test_server_core`, `test_api`, `test_mongo_deploy`,
`test_mongo_store` all rc 0 — Mongo-dependent tests skip cleanly and the
server path needs no driver. `grep 'import pymongo\|from pymongo'
aggregator/server.py` → no hits (GD-21 respected; the suite's own static guard
also asserts the server does not import `mirror.py` — the health block is
injected).

## 4. Plan conformance (sp-12 / R-30 / R-31 / R-55:server)

Owned files present and non-empty: `aggregator/server.py` (113 355 B),
`tests/test_server_core.py` (37 756 B), `tests/test_api.py` (46 445 B).
Every owned item is backed by live assertions: GD-13 in full (loopback
default, opt-in `0.0.0.0`, per-boot token via `hmac.compare_digest` on every
route but `/health`, Origin/Host allowlist at WS upgrade), the static
`(method,route)` dict with a default 404 and no fallback, `safe_artifact_path`
containment plus CSP sandbox and nosniff, `/health` per-tailer liveness +
parse-failure counters extended with the R-45 `mirror:{…}` block served
verbatim, R-31 query-string-only endpoints with one shared regex id validator
and `(stream,seq)` cursors, and R-55:server (bounded default replay window,
explicit `?from=` load-older, reconnect resume from the client's last
`(stream,seq)` with no duplicate and no gap, `live:true|false` frames with a
single mode-switch frame at the replay→tail boundary, ≥1 s absolute-token
coalescing per `(stream, ref)`). Reads stay memory-authoritative (GD-22); the
server serves the sp-10 reduction and derives nothing.

**Ownership:** clean. `ls -lt` over `aggregator/ tests/ docs/` shows only
`tests/test_api.py` (19:15), `aggregator/server.py` (19:15) and
`tests/test_server_core.py` (19:12) in this attempt's window; every other
module predates it (next newest `aggregator/custom_state.py` at 18:21).
`git status --short` matches the pre-attempt snapshot — no file outside the
three owned paths changed.

**Commits:** none. `git log --oneline -1` is still `579446e orchestration
history`, the prior pass's HEAD. Unrelated in-flight `.claude/` state was left
untouched.

## 5. Nothing outstanding

No new failures, no ownership violation, no unmet owned item. Gate is green.
