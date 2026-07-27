# sp-refs-mongostore — test gate, attempt 4 — PASS

Read-only gate. Nothing was edited; the only side effect was a throwaway
`mongo:7` container (`touch-mongo-test`, loopback-published on 127.0.0.1:27018,
`--auth`), removed at the end of the run.

## 1. Targeted suites (owned files) — GREEN

Run from the repo root, stdlib only, no runner:

| suite | rc | passing checks | skips |
|---|---|---|---|
| `python3 tests/test_refs.py` | 0 | 230 | 0 |
| `python3 tests/test_mongo_store.py` (no mongod, no `TOUCH_MONGO_URI`) | 0 | 313 | 1 — the live arm, skipped cleanly |
| `TOUCH_MONGO_URI=… python3 tests/test_mongo_store.py` (live `mongo:7`) | 0 | 339 | 0 |

Both no-mongod and live arms are exercised, so the R-56/GD-21 "skips cleanly
without a reachable mongod" requirement and the real-server behaviour are both
proven in this attempt rather than assumed.

## 2. Full-suite regression gate — GREEN (`SUITE_RC=0`)

`for t in .claude/shared/monitoring/tests/test_*.py … ; for t in tests/test_*.py …`

- monitoring baseline: `test_server`, `test_watcher`, `test_shell`,
  `test_frontend` — all rc=0.
- repo suites: `test_bootstrap` (65), `test_fixtures` (181), `test_stdlib_only`
  (21, 1 skip), `test_store` (161, 2 skips), `test_tailer` (91), `test_ws`
  (128), `test_refs` (230), `test_mongo_store` (313) — all rc=0.

No new failures; no baseline failure to discount. The suite was run with **no
services running** and the live arm off, i.e. the bare-checkout shape the gate
specifies.

## 3. Item verification

### R-43 (`aggregator/refs.py`)
- Present, pure: the suite asserts the module imports only `__future__` and
  `re`, calls nothing that touches the world, and reads neither environment nor
  clock — a key is a function of its ref (SD-1).
- GD-24 grammar is cross-checked *against the file-side implementation*, not
  restated: `ref_key` ≡ `store.cursor_key` on shared shapes, only the first `:`
  of a stream id is structural, `%`-escaping of `% # | :` verified, zero-padded
  ints, directory-traversal stream ids rejected on both sides.
- The seven GD-11 union members are asserted identical in `refs.py` and
  `store.py` (required/optional key sets *and* classification of the same dict),
  so drift between the two is a test failure. The GD-14
  `legacy:<task>:<id8>` exemption is checked on both sides, including the
  negative (a bare 8-hex agentId is rejected).
- Non-tautology spot-check: I called `ref_key({'kind':'uuid','sessionId':…,
  'uuid':…})` by hand and got
  `RefError: ref kind 'uuid' has unexpected fields ['sessionId']` — the
  validator really is closed-world, not a pass-through.

### R-44 (`aggregator/mongo_store.py`)
- Imports with **nothing third-party installed**: verified independently of the
  suite by blocking `pymongo`/`bson` in `sys.meta_path` — the module imports and
  `pymongo_available()` returns `False` instead of raising (GD-21 degrade).
  Module-level imports are only `__future__`, `datetime`, `hashlib`, `json`.
- GD-24 table: `ensure_schema` creates the collections on a real mongod, is
  idempotent on a second boot, and the unique `{stream:1,seq:1}` index exists on
  the server.
- GD-26: no index carries `expireAfterSeconds` (read back from the server), a
  hand-added TTL makes the next `ensure_schema` **refuse**, and an AST walk plus
  string-literal scan proves no delete verb is callable or spellable in the
  module; `$unset` is absent from the algebra.
- GD-25 acceptance test: normal / shuffled / reversed ingest into the real
  mongod produce ONE fingerprint (`b8f05eb7` three times) **and** equal counts
  (`agents 7, records 1091, run_nodes 16, runs 2, stream_meta 34, usage 328`),
  with the in-memory model agreeing with the server byte for byte.
- Index usage: both the `(stream, seq)` cursor query and the zero-padded `_id`
  range scan are asserted IXSCAN via `explain()`.
- Oversize: an 8 388 762-byte document becomes a stub that keeps `_id` and key
  fields and records where the bytes are; the real 877 KB line stores whole.
- `ts` is always aggregator-supplied: `tsRaw` keeps source spelling, `ts` is a
  tz-aware ms-resolution datetime, an unparseable `ts` is a loud rejection and
  there is no `now()` default.
- GD-28: the server itself refuses a `records` document with no `provenance`.
- GD-27: the live arm builds `touch_test_<pid>` (`touch_test_66785` this run),
  asserts the prefix before both the wipe and the drop, and drops only that
  database; no URI/credential appears in any message.

### Previous round's blocker (critique attempt 3, M1) — closed
`guarded_update` now has an explicit `DOCUMENT_VALIDATION_FAILED = 121` arm, and
the live run asserts the exact shape that was broken: the **payload-only**
custom-state write "loses the guard and SAYS SO against a real `$jsonSchema`,
instead of reporting the server unreachable", and the lease-renewal partial
update returns `acquired:False` on a lost race rather than `MongoUnavailable`
(which GD-30's breaker would have counted toward taking a healthy mirror down).
Both are checked against the real server, plus a mocked-`OperationFailure(…,121)`
arm for the no-pymongo path.

## 4. Ownership

`git log` is unchanged at `579446e` — **no commit was made**, correctly (only
sp-repo-bootstrap may commit). `aggregator/` and `tests/` are untracked as a
whole, so ownership was checked by mtime instead: files modified after 23:20 UTC
are exactly the four owned ones —

```
23:27:00 aggregator/refs.py
23:36:01 aggregator/mongo_store.py
23:32:11 tests/test_refs.py
23:34:14 tests/test_mongo_store.py
```

Observation (not a finding, not attributable to this sub-plan):
`.claude/shared/monitoring/monitoring.md` (23:30) and `monitor.html` (23:40) also
carry recent mtimes. They are owned by the concurrently running
sp-watcher-templates-firstwave sub-plan, are absent from this implementer's
declared change set, and both monitoring suites that guard them
(`test_shell`, `test_frontend`) are green.

## Verdict

**PASS** — targeted suites green in both mongod-absent and live-mongod arms,
full suite green with no new failures, R-43/R-44 present and asserted on real
behaviour, no out-of-scope edits, no commit.
