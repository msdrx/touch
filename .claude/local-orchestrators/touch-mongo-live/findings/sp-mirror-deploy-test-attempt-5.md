# sp-mirror-deploy — test gate, attempt 5 — PASS

Date: 2026-07-27. Python 3.13, pymongo 4.17.0 installed, Docker available
(`mongo:7`). Repo: /home/laniakea/Projects/touch.

Implementer-changed files: `aggregator/mirror.py`, `docs/mongo.md`,
`tests/test_mirror.py` — all three inside sp-06's ownership list
(`aggregator/mirror.py`, `docs/mongo.md`, `tests/test_mirror.py`,
`tests/test_mongo_deploy.py`). `tests/test_mongo_deploy.py` was left untouched
by this attempt and still passes.

## 1. Targeted suites (owned) — GREEN

| suite | result | evidence |
|---|---|---|
| `python3 tests/test_mirror.py` (no live URI) | PASS | 26 test functions, 336 `ok:` assertions, 1 clean SKIP (`live mirror arm: TOUCH_MONGO_URI is not set`) |
| `python3 tests/test_mirror.py` with `TOUCH_MONGO_URI` → real `mongo:7` (loopback+auth, 127.0.0.1:27217) | PASS | live arm executed, 0 skips; see §4 |
| `python3 tests/test_mongo_deploy.py` | PASS | 12 test functions, 163 `ok:` assertions, 0 skips — spins its own container, verifies loopback-only publish, unauth denial, user bootstrap, role-scoped `dropCollection`, server-side refusal of `records` deletes |
| `python3 tests/test_docs.py` | PASS | documentation guards |
| `python3 tests/test_stdlib_only.py` | PASS | GD-21 exception intact |

## 2. Full-suite regression gate — GREEN, 28/28

All four monitoring tests run from their own dir, then every `tests/test_*.py`:

```
PASS .claude/shared/monitoring/tests/{test_frontend,test_server,test_shell,test_watcher}.py
PASS tests/{test_agents,test_api,test_bootstrap,test_custom_state,test_docs,
     test_e2e_sim,test_fixtures,test_ingest,test_legacy,test_mirror,
     test_mongo_deploy,test_mongo_store,test_reducer,test_refs,test_register,
     test_server_core,test_sessions,test_slots,test_stdlib_only,test_store,
     test_tailer,test_touch_frontend,test_usage,test_ws}.py
SUITE_RC=0
```

No failures at all — no baseline failures to excuse, no new ones.

## 3. Bare-checkout arm (GD-21 / R-56) — GREEN

pymongo was masked by a `PYTHONPATH` shim whose `pymongo/__init__.py` raises
`ImportError`. Re-ran the Mongo-touching suites:

```
PASS tests/test_mirror.py  → "skipped: the dead-port arm needs pymongo …"
                             "skipped: live mirror arm: TOUCH_MONGO_URI is not set"
PASS tests/test_mongo_deploy.py → "skipped: live docker arm: pymongo is not installed (GD-21: absence is legal)"
PASS tests/test_mongo_store.py, tests/test_refs.py, tests/test_api.py, tests/test_stdlib_only.py
```

Every Mongo-dependent arm skips with a named reason; nothing errors.

## 4. Live-mongod arm — exercised, GREEN

Run against a real `mongo:7` with the R-42 loopback+auth recipe. Highlights
(verbatim from the run):

- `the mirror reaches 'live' against a real mongod, got 'live'` and
  `…holding the GD-29 writer lease`
- `a real bulk_write lands every document: {'records': 6, 'writers': 1}`
- `replaying the mirror's own output against a REAL server changes nothing (GD-25)`
- `MemoryBackend and a real mongod produce the SAME fingerprint` — this is what
  makes the bare-checkout (memory-backend) suite meaningful rather than a
  self-consistent fiction
- `the sweep retracted rather than deleted, server-side` (GD-26) and
  `a record with no generation is not swept`
- `a second writer is refused by the real conditional write (GD-29)`
- `--rebuild drops the reducer-owned collection at the server (GD-23)` and the
  replay reproduces a byte-identical fingerprint
- `a mirrored ref resolves under GD-24's dot-notation join against a real mongod`
- teardown: `dropping only the database this test constructed: touch_test_<pid>`
  (GD-27/GD-12)

## 5. Item coverage against the sub-plan

`sp-06 — mirror-deploy` items: R-45, R-42 (mirror + docs half), R-57 (mongo-doc half).

- **R-45 runtime** — present in `aggregator/mirror.py` (2946 lines): bounded
  queue + drop accounting (`enqueue`, `_take_batches`, `_requeue`), breaker
  (`_record_failure`/`_record_success`/`breaker_open`), GD-29 lease
  (`acquire`, `_lease_due`), cursors (`save_cursor`/`load_cursor`), GD-26
  generation sweep (`sweep`, retraction `update_many` + the one legal
  `delete_many`+reinsert path guarded by `SweepScopeError`), `rebuild`,
  `backfill`, `tick`/`run`/`flush`, and the `/health` block (`health()`).
  Tests assert the health block's field set and counter set match `health()`
  *exactly* against `docs/mongo.md` (documented-only `[]`, undocumented `[]`) —
  not a tautology: it is a two-way set difference between code and prose.
- **GD-21 client options verbatim** — `AsyncBackend.connect` opens
  `AsyncMongoClient` with the mandated timeouts; import of pymongo is lazy and
  confined to `mirror.py`/`mongo_store.py` (asserted per-module in the suite:
  `ingest.py`, `legacy.py`, `agents.py`, `custom_state.py` import no pymongo).
- **R-42 mirror half** — `load_credentials`/`save_credentials` enforce 0600
  (0400 accepted, symlinks refused, group/other bits refused, `os.open` with
  mode rather than post-hoc `chmod`); `database_name()` derives
  `touch_<sha1(repo-realpath)[:8]>`; zero-users mongod refusal at
  `if users == 0:` in `_start`.
- **R-42 docs half / R-57** — `docs/mongo.md` (356 lines) has §0 "Mongo down is
  a non-event", §1 exact loopback+auth `docker run` recipe with a
  "Never publish 27017" subsection, §2 least-privilege user bootstrap (executed
  as written by `test_mongo_deploy.py`), §3 how Touch reaches it + "Never
  mirrored", §4 what Touch refuses, §5 rebuild/backfill commands, §6 growth and
  retention, §7 teardown.
- **GD-27 credential hygiene** — asserted live: a password inside a driver
  exception never reaches `/health`, nor does the URI; redaction is *visible*
  (not a silent blank); host survives for operator use; `holderBoot` is a hash
  of `boot_id`, never the raw value.
- **GD-30 budgets** — dead-port tick arm present (skips without pymongo);
  queue-full drops mirror writes only.
- **Backfill discipline** — `live = False` is a literal with no parameter that
  can flip it, and every op is stamped `ingestMode:"backfill"`.

## 6. Ownership / stray-edit check

`find aggregator tests docs touch-visual .claude/shared -newermt '2026-07-27 08:15'`
returns exactly `docs/mongo.md`, `tests/test_mirror.py`, `aggregator/mirror.py`.
`git status` shows no new modifications outside the sub-plan's files (the
pre-existing in-flight `.claude/` orchestrator state is untouched by this
attempt). No commit was made.

## Verdict

**PASS.** Targeted suites green including the live-mongod arm; full suite
28/28 green; bare-checkout (no pymongo) arm green with clean skips; owned items
verified present and asserted non-tautologically; no edits outside ownership.
