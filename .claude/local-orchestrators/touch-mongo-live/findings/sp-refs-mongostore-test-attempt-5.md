# sp-refs-mongostore — test gate, attempt 5 — PASS

Read-only gate. No file in the repo was edited, no commit was made
(`git rev-parse --short HEAD` = `579446e`, unchanged). The only external thing
touched was the **already-running** `mongo:7` container `touch-mongo-sp05`
(`127.0.0.1:27117`, `--auth`, R-42 loopback recipe) — no container was created
or removed, and the live arm dropped only the database it constructed
(`touch_test_238667`, GD-27).

Owned files (per `plan/touch-mongo-live-subplans.md` §"sp-05 — refs-mongostore"):
`aggregator/refs.py`, `aggregator/mongo_store.py`, `tests/test_refs.py`,
`tests/test_mongo_store.py` — exactly the four the implementer reported changing.

---

## 1. Targeted suites (owned) — GREEN, three arms each

| run | interpreter / env | rc | `ok:` lines | skips |
|---|---|---|---|---|
| `python3 tests/test_refs.py` | ambient (pymongo present) | **0** | 254 | 0 |
| `python3 tests/test_mongo_store.py` | ambient, `TOUCH_MONGO_URI` unset | **0** | 360 | 1 (live arm) |
| `TOUCH_MONGO_URI=mongodb://touch:***@127.0.0.1:27117/?authSource=admin python3 tests/test_mongo_store.py` | ambient + real mongod | **0** | 393 | 0 |
| `venv --without-pip` python `tests/test_refs.py` | **no pymongo at all** | **0** | 252 | 0 |
| `venv --without-pip` python `tests/test_mongo_store.py` | **no pymongo at all** | **0** | 333 | 2 |

Both GD-21/R-56 requirements are proven rather than assumed:

- With **no pymongo installed**, `aggregator.refs` and `aggregator.mongo_store`
  both import and `mongo_store.pymongo_available()` returns `False` instead of
  raising; the two conditional arms skip with named reasons
  (`the raw-driver-exception arm needs pymongo's exception classes (GD-21)`,
  `live Mongo arm: TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)`).
- With **no reachable mongod**, `test_live_mongod_arm` skips cleanly and rc stays 0.

Live-mongod evidence (excerpts from the real-server run):

```
ok: ensure_schema created GD-24's collections … and running it again is a no-op
ok: no index on the server carries expireAfterSeconds (GD-26, read back)
ok: the unique {stream:1,seq:1} index exists on the server
ok: the server REFUSES a sub-document _id
ok: normal / shuffled / reversed ingest into a real mongod ⇒ ONE fingerprint
    (normal=b8f05eb7, reversed=b8f05eb7, shuffled=b8f05eb7)
ok: …and equal counts: {'agents': 7, 'records': 1091, 'run_nodes': 16,
    'runs': 2, 'stream_meta': 34, 'usage': 328}
ok: …and the in-memory model agrees with the server byte for byte
ok: the (stream, seq) cursor query is an IXSCAN … and so is the zero-padded _id range scan
ok: a TTL index someone added by hand makes the next ensure_schema REFUSE
ok: dropping only the database this test constructed: touch_test_238667 (GD-27)
```

That is GD-25's acceptance test (normal/shuffled/reversed ⇒ identical
fingerprint **and** identical counts), GD-24's index/`bsonType` pins, the no-TTL
law, the dotted-`_id` prohibition (IXSCAN asserted via `explain()`), and GD-27's
scoped teardown, all against a real server.

`tests/test_mongo_store.py` has 22 `def test_*` and all 22 are dispatched from
`main()` — no orphaned test. 260 `check(...)` sites. `tests/test_refs.py`: 17
tests, 91 `check(...)` sites.

## 2. Full-suite regression gate — SATISFIED (4 pre-existing baseline reds only)

```
26 of 28 files green; SUITE_RC=1 from two files that are the DOCUMENTED baseline
```

Green: all four monitoring tests (`test_frontend`, `test_server`, `test_shell`,
`test_watcher`) plus `test_agents`, `test_api`, `test_bootstrap`,
`test_custom_state`, `test_docs`, `test_e2e_sim`, `test_fixtures`,
`test_ingest`, `test_legacy`, `test_mongo_deploy`, `test_mongo_store`,
`test_reducer`, `test_refs`, `test_register`, `test_server_core`, `test_slots`,
`test_stdlib_only`, `test_store`, `test_tailer`, `test_touch_frontend`,
`test_usage`, `test_ws`.

Red — `tests/test_mirror.py` (3) and `tests/test_sessions.py` (1):

- `…proven by the call count: the held ticks made no attempt`
- `the first generation lands`
- `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`
- `wipe + --rebuild reproduces a byte-identical fingerprint`

### Why these are baseline and not attributable to attempt 5

1. **The identical four strings are on record as the known baseline**, verbatim,
   in `findings/sp-docs-register-test-attempt-1.md` §"The two reds are
   pre-existing baseline" (2026-07-27 00:21Z — written *before* this attempt's
   edits), which itself cites the same set in
   `sp-frontend-test-attempt-4.md`, `sp-server-api-test-attempt-{2,4}.md`,
   `sp-legacy-arm-test-attempt-1.md`, `sp-e2e-acceptance-test-attempt-1.md`,
   `sp-custom-state-test-attempt-{2,4}.md` and
   `sp-ingest-pipelines-test-attempt-1.md`. Nothing new went red and nothing
   previously green went red in this attempt.
2. **Ownership.** They live in `aggregator/mirror.py` (sp-06, the interrupted
   loop) and `aggregator/sessions.py` (sp-07) — neither owned nor touched here.
3. I reproduced the mirror reds under a probe to be sure of the shape rather
   than trusting the summary line: the breaker test's `bulk_upsert` count is 0
   because the lease `guarded_update` fails first under
   `backend.fail = MongoUnavailable`, and the sweep test now sees
   `{'records': 3, 'stream_meta': 3, 'writers': 1}` — i.e. `mirror.MemoryBackend`
   creates the GD-29 lease document because `writers.required =
   ('holderPid','holderBoot','leaseExpiresAt')`, which is **GD-24's table
   verbatim** (`plan/touch-mongo-live-plan.md:231`). The mongo_store side is the
   plan-conformant one; the stale expectations are in `tests/test_mirror.py` /
   `tests/test_sessions.py`, which belong to sp-06/sp-07 and are theirs to fix.

Per the gate's rule ("baseline failures do not fail the gate; any OTHER failure
is NEW and fails it"), the regression gate is satisfied.

## 3. Item verification

### R-43 — `aggregator/refs.py` (972 lines)
Present and pure: the suite asserts the module imports only `__future__` and
`re`, calls nothing that touches the world, and reads neither environment nor
clock — a key is a function of its ref (SD-1). Covered and *cross-checked
against the file side* rather than restated: `ref_key` ≡ `store.cursor_key` on
shared shapes; only the first `:` of a stream id is structural; `%`-escaping of
`% # | :` (and `. $` for field keys) round-trips single-pass; zero-padded ints
make lexicographic order numeric; component 512-char and whole-key 1024-byte
caps (UTF-8 bytes); lowercase-hex identity pins; the seven GD-11 union members
are asserted key-set-identical **in both `refs.py` and `store.py`**, so drift is
a test failure; GD-14's `legacy:<task>:<id8>` exemption holds on both sides,
negative included; unknown shapes are retained (`kind:'unknown'`, no `refId`),
never keyed; colliding `{stream,seq}` grammars require an explicit kind (GD-12).

### R-44 — `aggregator/mongo_store.py` (2040 lines)
GD-24's table is asserted verbatim (bsonType pins, unique `{stream:1,seq:1}`,
no TTL anywhere, `_id`-only indexes where the table says so), `_raw`-wrapping of
variable-key subtrees round-trips, oversize > 8 MB becomes a stub that keeps its
`_id`/key fields and says where the bytes are (never a drop), the aggregator
supplies every `ts` (Date + `tsRaw`, unparseable ⇒ loud rejection, never
`now()`), `writeErrors` are always inspected, no delete verb is called *or
spelled as a string*, pymongo is imported inside functions only, and GD-21's
client options are pinned verbatim.

### Non-tautology spot-checks (my own, not the suite's)
- `split_write_errors` on three synthetic `BulkWriteError` details:
  a **secondary** unique-index dup (`keyPattern {'agentId': 1}`) →
  `conflicts: 1, tolerated: 0`; an `_id` dup → `tolerated: 1, conflicts: 0`;
  code 121 → `fatal: 1`. Attempt-4's MAJOR M1 ("a secondary-index dup counted as
  a tolerated dup, so a lost write reports success") is genuinely closed, and the
  distinction is real code, not a renamed key.
- The same distinction is proven **against the real server** in the live arm:
  a second slot claiming the same `agentId` comes back as a conflict naming
  `{'agentId': 1}` with `identity_dups 0`, from both the bulk door and the
  guarded door.
- `classify_write_errors` is kept as the compatibility pair
  (`len(tolerated)+len(conflicts)`, `fatal`) that `mirror.MongoBackend` and
  `custom_state.bind_slot` (R-53) unpack — so the M1 fix did not silently change
  the number those two callers read.

## 4. Ownership / hygiene

`ls -la --time-style=full-iso aggregator/*.py tests/test_*.py`: the only files
with mtimes inside this attempt's window (2026-07-27 07:54–08:11Z) are
`aggregator/refs.py`, `aggregator/mongo_store.py`, `tests/test_refs.py`,
`tests/test_mongo_store.py`. Every other aggregator module and test file is
2026-07-26 or older. `git status` shows `?? aggregator/`, `?? tests/` (still
untracked, so there is no diff to show) and no staged or committed change;
`HEAD` is `579446e`.

**Verdict: PASS.** Targeted suites 100 % green across the no-pymongo, no-mongod
and live-mongod arms; full suite carries only the four documented baseline reds
owned by sp-06/sp-07; ownership clean; no commit.
