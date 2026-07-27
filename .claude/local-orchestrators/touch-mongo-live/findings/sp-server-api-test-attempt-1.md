# sp-server-api — test gate, attempt 1

**Verdict: FAIL.** 1 NEW regression, attributable to this sub-plan's
`aggregator/server.py`. Owned suites are 100 % green; ownership is clean;
no commits.

Environment: Python 3.13, pymongo 4.17.0 present, Docker daemon available
(`mongo:7` local), `TOUCH_MONGO_URI` unset.

Implementer's changed set (all three are sub-plan-owned — no violation):
`aggregator/server.py`, `tests/test_server_core.py`, `tests/test_api.py`.

---

## 1. Targeted suites (sp-server-api owned) — GREEN

Run from the repo root, standalone executables, stdlib only:

| suite | rc | `ok:` assertions |
|---|---|---|
| `tests/test_server_core.py` | **0** | 127 |
| `tests/test_api.py` | **0** | 102 |

Both end with their own banner (`all server core tests passed`,
`all read API + wire tests passed`). Assertions are behavioural, not
tautological — they exercise real sockets and the real wire contract, e.g.:

- GD-13: `an unauthenticated API call is 401 on the wire`;
  `a cross-origin WS upgrade is 403`; `an untokened WS upgrade is 401`;
  `an unsupported WS version is 426 advertising 13, never the page body`;
  `nosniff is on the wire`; `the accept key is RFC 6455's`.
- GD-21 static guard: `only mongo_store.py and mirror.py may import the
  driver` — `and the server does not import mirror.py either — the health
  block is injected`; `every import is stdlib: []`.
- R-55 replay/resume: `the bounded replay arrives with live:false`;
  `exactly one mode frame marks the replay->tail boundary`;
  `a reconnect replays exactly the records after the client's (stream, seq)`;
  `no record is delivered twice`; `the union of the two sessions equals a full
  replay — no gap either`.
- R-55 absolute tokens: `one frame is released after the window and it is the
  LAST absolute value` / `30 — the newest absolute count, never a sum of
  deltas`; `coalescing is per (stream, ref)`.
- R-45 health: `the R-45 mirror block is served verbatim`; `a tailer whose
  target is gone is visible (AUDIT-15)`; `no mirror configured reports absent
  — never an error`.
- Route-table posture: `a garbage head does not raise — it yields no method,
  which the table 404s`.

## 2. Full suite regression gate — FAIL (1 new)

`for t in .claude/shared/monitoring/tests/test_*.py … ; for t in tests/test_*.py …`
run at the repo root, no services running, `TOUCH_MONGO_URI` unset.

- **PASS (20):** monitoring `test_frontend`, `test_server`, `test_shell`,
  `test_watcher`; repo `test_agents`, `test_api`, `test_bootstrap`,
  `test_custom_state`, `test_fixtures`, `test_ingest`, `test_legacy`,
  `test_mongo_store`, `test_reducer`, `test_refs`, `test_server_core`,
  `test_slots`, `test_stdlib_only`, `test_store`, `test_tailer`, `test_usage`,
  `test_ws`.
- **FAIL (2) — pre-existing baseline, NOT attributable:**
  - `tests/test_mirror.py` rc 1, `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` rc 1, `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

  These are **character-for-character identical** to the baseline set recorded
  in `sp-custom-state-test-attempt-4.md` §2 (and back through
  `sp-agents-reducer-test-attempt-4.md`), which predates this attempt. They
  belong to the loops that closed RED (`sp-mirror-deploy`, `sp-sessions-arm`).
  Baseline failures do not fail this gate.

- **FAIL (1) — NEW, attributable to `aggregator/server.py`:**

### NEW FAILURE — `tests/test_mongo_deploy.py` rc 1

```
test_no_connection_string_literal_under_aggregator
  FAILED (1):
    - server.py hardcodes no mongod port — lines [2124]
```

**Traceback essence.** `tests/test_mongo_deploy.py:282-286` is a static guard
over every `aggregator/*.py`: no line may contain the literal `27017` unless a
`#` precedes it on that line (i.e. it is inside a comment). It flags

```
aggregator/server.py:2124
        "                   Never publish 27017 (GD-27: Mongo stays loopback).\n"
```

— a line inside the `_usage()` help text added by this attempt. It is a string
literal, not a comment, so the guard's `"#" not in line.split("27017")[0]`
escape does not apply. (`aggregator/mirror.py:1791` also mentions `27017` but
is a real `#` comment and passes.)

**Why it is attributable to this change.** `tests/test_mongo_deploy.py` is
listed as **PASS** in the immediately preceding baseline
(`sp-custom-state-test-attempt-4.md` §2, PASS list). The only new `27017`
occurrence under `aggregator/` is at `server.py:2124`, in a function this
attempt authored, and `aggregator/server.py` is one of exactly three files
this implementer touched. `ls -lt` over `aggregator/ tests/ docs/` confirms
only `server.py`, `test_server_core.py`, `test_api.py` were modified in this
attempt's window (18:47–18:48); every other module predates it.

**Removal probe (decisive).** `aggregator/ tests/ docs/` copied to a scratchpad
and the single line rewritten to
`"                   Never publish the mongod port (GD-27: Mongo stays loopback).\n"`
— nothing else changed. `python3 tests/test_mongo_deploy.py` in the copy exits
**0** (`all mongo deployment (R-42 / R-57 mongo-doc) tests passed`, including
the full live-`mongo:7` Docker arm). The failure is caused by this line and
nothing else.

**Concrete fix.** In `aggregator/server.py` `_usage()`, drop the numeral from
the `--open` help text — e.g.

```
"                   Never publish the mongod port (GD-27: Mongo stays\n"
"                   loopback-only; its port comes from config, never code).\n"
```

The advice is preserved and no `aggregator/` module then spells a mongod port,
which is exactly the R-42 invariant the guard defends (the URI, host and port
come from `.touch/mongo.json`, never from source). Do **not** work around the
guard by inserting a `#` — that would satisfy the regex while re-introducing
the literal the rule exists to keep out of shipped strings. Also note the
`8932:8932` sample two lines above is fine: it is the Touch port, not Mongo's.

## 3. Plan conformance (sp-12 / R-30 / R-31 / R-55:server)

Owned files present and non-empty: `aggregator/server.py` (98 905 B),
`tests/test_server_core.py` (28 285 B), `tests/test_api.py` (33 645 B).
Every owned item has live assertions behind it — GD-13 auth/Origin/Host
posture, the static `(method,route)` dict with a default 404 (no fallback),
`safe_artifact_path` containment plus CSP/nosniff, `/health` per-tailer
liveness + parse counters extended with the R-45 `mirror:{…}` block served
verbatim (server never imports `mirror.py`), query-string-only ids with a
shared regex validator, `(stream,seq)` cursors, bounded replay + `?from=`
load-older, reconnect resume with no duplicate and no gap, `live:true|false`
frames with a single mode-switch frame, ≥1 s absolute-token coalescing per
`(stream, ref)`, and memory-authoritative reads (GD-22) with no driver import
(GD-21). No derivation happens in the server; it serves the reduction.

**Ownership:** clean. No file outside the three owned paths was modified. No
commit was made; the working tree's unrelated `.claude/` in-flight state was
not touched.

## 4. What must change for attempt 2

One line: remove the `27017` literal from `aggregator/server.py:2124`. Nothing
else in this sub-plan is red — both owned suites and the other 20 files stay
green.
