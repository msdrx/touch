# sp-refs-mongostore — test gate, attempt 1 — PASS

Gate: read-only. No source or test file was edited. Date 2026-07-25.

## 1. Targeted suites (owned by this sub-plan) — GREEN

Run from repo root, stdlib only, no pytest:

| suite | result | assertions |
|---|---|---|
| `python3 tests/test_refs.py` | exit 0 — "all refs (R-43) tests passed" | 202 `ok:` |
| `python3 tests/test_mongo_store.py` (no pymongo, no mongod) | exit 0 — "all mongo_store (R-44) tests passed" | 134 `ok:`, 1 clean skip |
| `python3 tests/test_mongo_store.py` with pymongo + live mongod | exit 0 | 146 `ok:`, 0 skips |

The default environment has **no pymongo installed** (`ModuleNotFoundError`),
so the default run is exactly the GD-21/R-56 "bare checkout" arm: both owned
modules import, all offline assertions run, and the live arm skips with the
message `live Mongo arm: TOUCH_MONGO_URI is not set (R-42's loopback+auth
recipe)` — a skip, not a failure, and the process still exits 0.

### Live-mongod arm actually exercised (not just "skips cleanly")

I did **not** stop at the skip. A `mongo:7` container provisioned to the R-42
recipe is up (`touch-mongo-sp05`, published on `127.0.0.1:27117` only, root
credentials via `MONGO_INITDB_ROOT_*`). pymongo 4.17.0 + dnspython were
installed into a scratchpad `--target` dir (`PYTHONPATH`, outside the repo —
nothing was added to the working tree) and the suite re-run with
`TOUCH_MONGO_URI=mongodb://touch:touchpw@127.0.0.1:27117/?authSource=admin`.
All twelve live checks passed:

- `ensure_schema` creates GD-24's collections; second call is a no-op.
- no server-side index carries `expireAfterSeconds` (no-TTL law read back from
  the server, not merely grepped).
- the unique `{stream:1, seq:1}` index exists on `events`.
- the server **refuses** a sub-document `_id` (MONGOSCHEMA-6 ≡ CUSTOMSTATE-4 ≡
  LIVEFLOW-2), i.e. GD-24's opening law is enforced by `$jsonSchema`, not only
  by Python.
- **GD-25 acceptance through a real mongod**: normal / shuffled / reversed
  ingest ⇒ one fingerprint (`b72e4348` all three) and equal counts
  `{agents: 7, records: 1091, run_nodes: 7, runs: 1, stream_meta: 34,
  usage: 328}`; the in-memory model agrees with the server byte for byte.
- `explain()` shows **IXSCAN** for both the `(stream, seq)` cursor query and
  the zero-padded `_id` range scan.
- replaying stored events is not an error; a second writer on an existing
  `(stream, seq)` is *counted* as a tolerated duplicate, never swallowed
  (GD-29) — `writeErrors` are inspected as R-44 requires.
- teardown drops only `touch_test_<pid>`, a name the test constructed (GD-27).

The container was left running and untouched; the test dropped only its own
database.

## 2. Full-suite regression gate — GREEN, no new failures

```
cd /home/laniakea/Projects/touch
for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")"); done
for t in tests/test_*.py; do python3 "$t"; done
```

PASS ×12, `SUITE_RC=0`, run with **no services required and no third-party
packages installed**:

- monitoring baseline: `test_frontend`, `test_server`, `test_shell`,
  `test_watcher` — all green (baseline preserved).
- repo suite: `test_bootstrap`, `test_fixtures`, `test_mongo_store`,
  `test_refs`, `test_stdlib_only`, `test_store`, `test_tailer`, `test_ws` —
  all green. In particular SD-2's `test_stdlib_only.py` guard stays green with
  the new `mongo_store.py` present, i.e. the single GD-21 pymongo exception is
  correctly declared and no other third-party import leaked in.

No failure of any kind was observed, so there is nothing to attribute.

## 3. Verification against the plan items

Sub-plan `sp-05 — refs-mongostore` (subplans.md §"sp-05"), items **R-43** and
**R-44** of the amendment. All four owned files exist:
`aggregator/refs.py` (31 KB), `aggregator/mongo_store.py` (50 KB),
`tests/test_refs.py`, `tests/test_mongo_store.py`.

R-43 clauses, each with a matching non-tautological assertion:

- one `ref_key(ref) -> str` in the GD-24 grammar; every ref shape built in up
  to 6 different dict insertion orders ⇒ **one** `_id` and one byte-stable
  canonical sub-document; the test first *demonstrates the hazard*
  (`json.dumps({"s":1,"n":2}) != json.dumps({"n":2,"s":1})`) so the equality
  that follows carries information.
- `%`-escaping of `% # | :` round-trips, single-pass in both directions
  (`escape_component("%25") == "%2525"`, hard-coded literals — not re-derived
  from the module); a task name full of separators still yields exactly one
  structural `#`.
- type pins: `ref_key({"pid":622,"procStart":"10028") == "live:622-10028"`
  literal; `procStart` as int, `pid` as str, `ordinal` as str and as `True`
  are all rejected.
- unknown shapes ⇒ `kind:"unknown"` with no `refId`; ambiguous key sets demand
  an explicit `kind`; control chars and over-long components rejected.
- cross-checks against the file side: event `_id` is byte-identical to
  `store.cursor_key`, the seven GD-11 union members and their required/optional
  key sets match `store.py` on both sides, and the GD-14
  `legacy:<task>:<id8>` agent exemption agrees.
- purity (SD-1): `refs.py` imports only `__future__` and `re`; no I/O, no
  clock, no environment read.

R-44 clauses:

- GD-24 table verbatim (collections, `$jsonSchema` bsonType pins), unique
  `{stream:1,seq:1}`, no TTL anywhere — asserted statically *and* read back
  from a real server.
- op algebra restricted to `$max`/`$addToSet`/`$min`/`$setOnInsert`; `$inc`
  and bare `$set` on accumulables rejected; `apply_update` is pure.
- dotted / `$`-prefixed keys: 33 real dotted-key specimens wrap at the declared
  path and round-trip byte-identically; an undeclared dotted key left unwrapped
  is **rejected** (MONGOSCHEMA-8), autowrap rescues it losslessly and records
  the declaration gap.
- oversize: the real 877 536-byte line is stored whole; > 8 MB ⇒ stub carrying
  `_id`, key fields, byte count and source location — never a drop.
- `ts` always supplied by the aggregator: `tsRaw` keeps the source spelling,
  `ts` is tz-aware at ms resolution, second-resolution legacy parses, an
  unparseable `ts` is a loud rejection and there is **no** `now()` default;
  static grep confirms the module never reads the clock.
- GD-21 client options verbatim, including the reproduction of MONGOSCHEMA-4
  (the 30 s default would stall the poll loop 30.1 s against a dead port);
  overrides do not mutate the shared dict; `pymongo_available()` answers
  instead of raising and `open_client` raises `MongoUnavailable`, not
  `ImportError`.
- GD-25 acceptance uses the **frozen fixture corpus**
  (`tests/fixtures/run-wf_829e6f58/**.jsonl` plus the two mirror specimens),
  with true line numbers restored from the fixture index / PROVENANCE.md — real
  data, not synthesized inputs, and the mapper used is deliberately test-local
  so it does not co-vary with any sp-07…sp-11 mapper.

Both test files are standalone executables that collect failures and
`sys.exit(1)`; an unexpected exception also propagates to a non-zero exit.

## 4. Ownership / working-tree check

`git status` plus mtimes. Files touched inside this attempt's window
(21:49–22:04) are exactly the four owned ones: `aggregator/refs.py`,
`aggregator/mongo_store.py`, `tests/test_refs.py`, `tests/test_mongo_store.py`
(plus their `__pycache__`). Earlier sub-plans' files
(`aggregator/store.py` 21:22, `tailer.py` 21:07, `ws.py` 21:17,
`tests/test_store.py`, `test_tailer.py`, `test_ws.py`, `test_stdlib_only.py`,
`test_fixtures.py`, `test_bootstrap.py`) are unmodified since their own runs.
No commits were made (`git log` still ends at `579446e`, sp-01's C1/C2
boundary unchanged for this sub-plan — correct per SD-6).

### One observation, NOT attributed to this implementer and NOT gating

`CLAUDE.md` shows as modified with mtime 22:03, inside the attempt window.
The diff is +8 lines under "Rules that bite" about storing generated HTML
artifacts / research notes under `.claude/local-orchestrators/<task>/report|
findings/` — session/monitoring housekeeping, with no relation to R-43/R-44 or
to Mongo, and matching the untracked `report/*.html` files created at the same
minute by other (non-sub-plan) activity. `CLAUDE.md` is sp-15's file (SD-7), so
if a later gate can attribute this edit to the sp-05 implementer it should be
reverted there; I could not attribute it, it changes no behavior, and both
doc-guard suites (`test_shell.py`, `test_frontend.py`) are green, so it does
not fail this gate.

## Verdict

**PASS.** 4 owned files present; targeted suites 100 % green (202 + 146
assertions with the live mongod arm exercised end-to-end, 134 + clean skip
without it); full 12-file suite green with zero third-party packages and zero
services; every R-43/R-44 clause has a substantive, non-tautological
assertion behind it. No failures to report.
