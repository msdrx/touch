# sp-server-api — test gate, attempt 4

**Verdict: PASS (green).** Both owned suites are 100% green, the full suite shows
only the two known pre-existing baseline failures (character-for-character
identical to the attempt-1/2/3 records), ownership is clean (only the three
owned files carry this attempt's mtimes, `HEAD` still `579446e`, nothing
committed), the bare-checkout / no-pymongo arm is green, and every attempt-3
critique finding now has a named behavioural test behind it.

Window of this attempt: owned-file mtimes 20:12–20:17 UTC; gate run 20:18–20:22 UTC.

## 1. Targeted suites (must be 100% green)

Run from the repo root, stdlib-only, standalone executables:

| suite | rc | result |
|---|---|---|
| `python3 tests/test_server_core.py` | 0 | `all server core tests passed` — 23 test functions, 176 `ok:` assertions |
| `python3 tests/test_api.py` | 0 | `all read API + wire tests passed` — 35 test functions, 204 `ok:` assertions |

No failures, no errors, no unexpected skips. The only skips anywhere in the
wider suite are the deliberate `TOUCH_MONGO_URI is not set` live-mongod arms
(R-42) — the required clean-skip behaviour.

## 2. Full-suite regression gate

```
cd /home/laniakea/Projects/touch && rc=0
for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done
for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done
exit $rc
```

- **PASS (22):** `.claude/shared/monitoring/tests/test_frontend.py`,
  `test_server.py`, `test_shell.py`, `test_watcher.py` (all four monitoring
  baselines green); `tests/test_agents.py`, `test_api.py`, `test_bootstrap.py`,
  `test_custom_state.py`, `test_fixtures.py`, `test_ingest.py`, `test_legacy.py`,
  `test_mongo_deploy.py`, `test_mongo_store.py`, `test_reducer.py`,
  `test_refs.py`, `test_server_core.py`, `test_slots.py`, `test_stdlib_only.py`,
  `test_store.py`, `test_tailer.py`, `test_usage.py`, `test_ws.py`.

- **FAIL (2) — pre-existing baseline, NOT attributable:**
  - `tests/test_mirror.py` rc 1, `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` rc 1, `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

  Identical strings to the baseline recorded in `sp-server-api-test-attempt-1.md`
  and re-confirmed in attempts 2 and 3. They live in `aggregator/mirror.py` /
  `aggregator/sessions.py` and their suites — files owned by **sp-mirror-deploy**
  and **sp-sessions-arm**, two loops that closed RED with open findings. This
  attempt touched none of them (mtimes `mirror.py` 07-26 11:29,
  `test_mirror.py` 02:44, `sessions.py` 04:10, `test_sessions.py` 04:14 — all
  many hours before this attempt's 20:12–20:17 edit window). Baseline failures
  do not fail this gate. **No new failure anywhere.**

## 3. Bare-checkout / no-third-party arm (GD-21, R-56)

Both owned suites re-run with `pymongo` shadowed by a stub module that raises
`ImportError` on import (`PYTHONPATH` shim), no services running:

```
PYTHONPATH=<shim> python3 tests/test_server_core.py  → rc 0
PYTHONPATH=<shim> python3 tests/test_api.py          → rc 0
```

`server.py`'s import block is stdlib + relative siblings only
(`asyncio, datetime, hashlib, hmac, json, os, re, secrets, sys, time,
urllib.parse, dataclasses` + `. import agents/ingest/legacy/mongo_store/refs/
store/ws`). No `pymongo`, no `bson`, no `import mirror` — the R-45 `/health`
mirror block is injected, as the suite's own static guard
`test_the_server_imports_no_driver` asserts.

## 4. Ownership

`find aggregator tests docs -newermt "19:50"` returns exactly:

```
tests/test_server_core.py   20:12
tests/test_api.py           20:15
aggregator/server.py        20:17
```

— the three owned files and nothing else. `git status` otherwise shows only
the unrelated in-flight `.claude/` orchestrator state that predates this
attempt. `git log -1` = `579446e` (no commit made).

## 5. Plan conformance — attempt-3 critique findings all have live tests

Verified by name in the green run, not by reading alone:

| critique-3 finding | covering test | evidence it is behavioural |
|---|---|---|
| M1 — late stream's truncation recorded but never sent | `test_a_late_streams_truncation_is_published_not_just_recorded` (test_api.py:935) | drives the exact reproducer (window=5, 60 records into a stream born after `switch()`); asserts a real `anchors` frame with `oldest == 56`, `truncated is True`, ordered **before** the frames it describes, absent from the `mode` frame, and key-checked against the normative docstring table via `_contract_frame_keys` |
| M2 — `subscribe` acks a rewind it never replays | `test_subscribe_resumes_and_never_acks_a_position_it_did_not_send` (test_api.py:1208) + `test_the_idle_marker_is_sent_and_a_subscribe_is_answered_in_order` (core:860) | on a session that has replayed/switched/ticked: the rewound range is re-delivered over the wire as backfill, the ack comes **last**, an ahead-of-position cursor is refused by name, per-pair rejections are individual, the echo of an unusable cursor is truncated, out-of-selection pairs refused, and the tail continues without duplicates |
| m1 — unobserved `?stream=` presented as `currentRun` | `test_a_selector_for_a_run_that_has_not_started_is_labelled_unobserved` (test_api.py:972) | asserts `currentRun is None`, the ghost id in `streamsUnobserved`, and the mixed case where a real run beside a ghost is the current run |
| m2 — unbounded tick burst (GD-30) | `test_one_tick_cannot_write_an_unbounded_burst` (test_api.py:1001) | appends `MAX_TICK_EVENTS + 7`; asserts the first tick emits exactly `MAX_TICK_EVENTS`, `session.capped == 1`, the cursor stops where the cap fell, the remainder arrives next tick, and seqs are contiguous with no duplicate. `MAX_TICK_EVENTS = MAX_REPLAY_EVENTS` (server.py:257) |
| m3 — malformed `?from=` dropped silently | `test_from_is_applied_or_reported_never_silently_dropped` (test_api.py:824) + `test_a_handshake_names_the_parameters_it_could_not_use` (core:765) | "a `?from=` that does not parse comes back raw on hello, like every other parameter this socket could not use" and "a handshake that sent none is a distinguishable case, not the same null" |

Owned sp-12 items remain covered end-to-end: R-30 (GD-13 posture — loopback
default, `--open` opt-in, three token carriers via `hmac.compare_digest`,
Origin/Host allowlist at upgrade only, static `(method,route)` table with a
default 404 and no prefix match, `safe_artifact_path` containment incl. symlink
realpath, CSP sandbox + nosniff + no-store, `/health` credential-free with
per-tailer liveness, parse-failure counters and the R-45 mirror block that
reports `down` instead of 500), R-31 (one shared id validator: 400 malformed /
404 unknown / never another id's data; query-string-only; `(stream,seq)`
cursors that round-trip), R-55:server (bounded replay with published edge,
single `mode` boundary, `live` flags, `?from=`, resume via `?cursor=` and
`subscribe` with no duplicates, absolute token frames coalesced ≥1 s, capped
tail, `/api/query` Mongo arm with labelled memory fallback). Guards for
GD-22/23 (server derives nothing — the reducer decides; no field is ever
differenced), GD-27 (0600 `.touch/server.json`, 0700 dir, no path/URI/token on
`/health`) and header-injection totality (CRLF, NUL, non-latin-1) all pass.

**Gate result: PASS.**
