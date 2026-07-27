# sp-refs-mongostore — test gate, attempt 3

**Verdict: PASSED** — 0 failures. All targeted suites green on three
interpreters (ambient with pymongo 4.17.0, a genuinely pymongo-free venv, and
against a live `mongo:7` mongod), and the full-suite regression gate is green in
both the ambient and the bare-checkout arm.

Files under review (implementer-reported; mtimes confirm these four and only
these four moved in this attempt's window, 22:54–23:07):

- `/home/laniakea/Projects/touch/aggregator/refs.py` (22:54)
- `/home/laniakea/Projects/touch/aggregator/mongo_store.py` (23:05)
- `/home/laniakea/Projects/touch/tests/test_refs.py` (23:02)
- `/home/laniakea/Projects/touch/tests/test_mongo_store.py` (23:07)

---

## 1. Targeted suites (sub-plan-owned files)

Run from the repo root, ambient Python 3.13 with pymongo 4.17.0 installed:

| suite | rc |
|---|---|
| `python3 tests/test_refs.py` | **0** — 16 test functions, incl. the AST purity guard |
| `python3 tests/test_mongo_store.py` | **0** — 19 test functions; live arm skipped cleanly (`TOUCH_MONGO_URI` unset) |

## 2. Full-suite regression gate

Ambient interpreter:

```
PASS .claude/shared/monitoring/tests/test_frontend.py
PASS .claude/shared/monitoring/tests/test_server.py
PASS .claude/shared/monitoring/tests/test_shell.py
PASS .claude/shared/monitoring/tests/test_watcher.py
PASS tests/test_bootstrap.py   PASS tests/test_fixtures.py
PASS tests/test_mongo_store.py PASS tests/test_refs.py
PASS tests/test_stdlib_only.py PASS tests/test_store.py
PASS tests/test_tailer.py      PASS tests/test_ws.py
SUITE_RC=0
```

## 3. The attempt-2 blocker (B1) — FIXED, verified on the arm that found it

Attempt 2 failed because `bulk_upsert` imported pymongo one line *before* its
empty-batch short-circuit, so `tests/test_mongo_store.py` aborted with an
uncaught `MongoUnavailable` on a bare checkout. Re-run on a genuinely
pymongo-free interpreter (`python3 -m venv --without-pip`, `import pymongo` →
`ModuleNotFoundError` confirmed first):

```
$ <nopm>/bin/python3 tests/test_refs.py        rc=0
$ <nopm>/bin/python3 tests/test_mongo_store.py rc=0
  ...
  skipped: guarded_update's driver arm needs pymongo's exception classes (GD-21)
  skipped: the raw-driver-exception arm needs pymongo's exception classes (GD-21)
  skipped: live Mongo arm: TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)
  all mongo_store (R-44) tests passed
```

The **whole suite** on that same pymongo-free interpreter: all 12 files PASS,
`BARE_SUITE_RC=0`. This is the GD-21/R-56 no-mongod, no-third-party baseline the
gate names, and it is green — the degradation is a clean skip, never an
`ImportError` and never a red file.

## 4. Live `mongo:7` arm (R-42 loopback+auth recipe)

Container `touch-mongo-sp05` (`mongo:7`, published **127.0.0.1**:27117 only,
root user/password set — loopback + auth per GD-27/R-42):

```
TOUCH_MONGO_URI="mongodb://touch:touchpw@127.0.0.1:27117/?authSource=admin" \
  python3 tests/test_mongo_store.py     rc=0
```

`test_live_mongod_arm` ran fully (22 assertions), all green, including the ones
that can only be answered by a real server:

- `ensure_schema` creates GD-24's collections and is idempotent on re-run.
- No index on the server carries `expireAfterSeconds`; a hand-added TTL makes the
  next `ensure_schema` **refuse** (GD-26).
- The unique `{stream:1,seq:1}` index exists server-side.
- The server itself refuses a sub-document `_id` (MONGOSCHEMA-6) and refuses a
  document with no provenance (GD-28) — the client-side validator and the
  `$jsonSchema` agree rather than one covering for the other.
- **GD-25 acceptance against a real mongod**: normal / shuffled / reversed ingest
  ⇒ ONE fingerprint (`b8f05eb7` all three) *and* equal counts
  `{'agents': 7, 'records': 1091, 'run_nodes': 16, 'runs': 2, 'stream_meta': 34,
  'usage': 328}`, with the in-memory model matching the server byte for byte.
- Both cursor queries — `(stream, seq)` and the zero-padded `_id` range — are
  **IXSCAN** per `explain()`; no dotted-`_id` query anywhere.
- Duplicate-key on a second writer is *counted* as a tolerated dup, never
  swallowed and never raised (GD-29); lease take-over is one round trip with no
  read-then-write window; a late low-`seq` custom-state write leaves the head
  alone (R-52).
- Teardown drops only the database the test constructed (`touch_test_61876`).

## 5. Plan conformance (item 3)

- **Ownership:** `git status --porcelain` outside `.claude/`, `.gitignore`,
  `CLAUDE.md` and `.temp-develop/` shows only `?? aggregator/` and `?? tests/`
  (both wholly untracked from earlier sub-plans). File mtimes show no file
  outside the four owned paths was touched in this attempt's window. **No commit
  was made** (correct — only sp-01 commits, SD-6).
- **R-43 present and asserted, not tautologically:** `test_refs.py` proves key
  independence from dict insertion order across every kind (each kind rebuilt
  under multiple insertion orders ⇒ one `_id` *and* a byte-stable ref
  sub-document), grammar↔`parse_ref_key` round trips, escaping of the structural
  characters, component bounds, bsonType pins, zero-padding ⇒ lexicographic ==
  numeric order, single-spelling identity hex, unknown shapes retained-never-keyed,
  and byte-identity between `event` ids and the file-side store cursor key. An AST
  guard proves the module is pure (no clock, no I/O).
- **R-44 present and asserted:** `COLLECTIONS` is exactly GD-24's 15 rows
  (`sessions, records, stream_meta, agents, runs, run_nodes, usage, events,
  legacy_events, custom_state_events, custom_state, slots, derived, writers,
  cursors` — cross-checked line by line against the plan's table); `_id` pinned to
  `bsonType: string` on every collection; the `sessions` tagged union stores both
  arms; grammars and collections are mutually total (a grammar `refs.py` can emit
  that `mongo_store` would not accept is caught); forbidden operators and
  `$unset` rejected; `$inc` refused; `_raw`-wrapping for dotted/variable-key
  subtrees round-trips; >8 MB ⇒ stub that keeps its `_id` and key fields (never a
  drop, never a null on a pinned field); the aggregator supplies every `ts`
  (tz-aware, ms resolution, `tsRaw` keeps the source spelling, an unparseable `ts`
  is a loud rejection with **no** `now()` default); an AST guard proves no delete
  verb is called *or spelled as a string*, and no clock read; pymongo is imported
  inside functions only.
- **Attempt-2 major "count half not applied to the newest keying rule" —
  addressed:** `expected_counts()` now derives `run_nodes` and `runs` straight
  from the journal files without calling the mapper's own helpers, per
  `(key, ordinal)` bounded by `max(#started, #result)`, and the acceptance test
  asserts every collection's count against it. The ordinal derivation is shown
  doing work (9 nodes over 6 keys, ordinals `{0,1}` reached) — a constant
  `ordinal = 0` would leave both fingerprint and pass-to-pass counts identical, so
  this is the only assertion that can see it.
- **Non-vacuity is proven, not claimed:** the acceptance test carries two negative
  arms — an inconsistent `$setOnInsert` payload *does* change the fingerprint
  under reordering, and dropping a whole keying rule *does* show up in the counts
  though not the fingerprint.

## 6. Failures

None. No new failure, no baseline failure, no regression.
