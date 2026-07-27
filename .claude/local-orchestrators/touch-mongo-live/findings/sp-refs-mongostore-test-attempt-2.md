# sp-refs-mongostore — test gate, attempt 2

**Verdict: FAILED** — 1 new failure. Everything else is green, including a full
live-`mongo:7` arm and every prior-attempt critique item.

Files under review (implementer-reported, all four confirmed touched in this
attempt's window 22:31–22:33):

- `/home/laniakea/Projects/touch/aggregator/refs.py`
- `/home/laniakea/Projects/touch/aggregator/mongo_store.py`
- `/home/laniakea/Projects/touch/tests/test_refs.py`
- `/home/laniakea/Projects/touch/tests/test_mongo_store.py`

---

## 1. Targeted suites (this sub-plan's owned files)

Run from the repo root with the ambient interpreter (Python 3.13, **pymongo
4.17.0 installed**):

| suite | rc | notes |
|---|---|---|
| `python3 tests/test_refs.py` | **0** | all R-43 tests pass; 14 test functions, incl. purity AST guard |
| `python3 tests/test_mongo_store.py` | **0** | all R-44 tests pass; live arm skipped cleanly (`TOUCH_MONGO_URI` unset) |

## 2. Full-suite regression gate

```
PASS .claude/shared/monitoring/tests/test_frontend.py
PASS .claude/shared/monitoring/tests/test_server.py
PASS .claude/shared/monitoring/tests/test_shell.py
PASS .claude/shared/monitoring/tests/test_watcher.py
PASS tests/test_bootstrap.py
PASS tests/test_fixtures.py
PASS tests/test_mongo_store.py
PASS tests/test_refs.py
PASS tests/test_stdlib_only.py
PASS tests/test_store.py
PASS tests/test_tailer.py
PASS tests/test_ws.py
SUITE_RC=0
```

Green with the ambient interpreter. **But the gate's baseline is explicitly
"a bare checkout with NO services running and NO third-party packages
installed"**, and that arm is where the failure is (§3 below).

## 3. FAILURE — `test_mongo_store.py` exits non-zero when pymongo is absent (GD-21)

**Test id:** `test_bulk_upsert_applies_the_same_guards_as_the_memory_pass`,
assertion `"an empty batch is a no-op, not a connection"` —
`tests/test_mongo_store.py:396`.

**Reproduction (two independent methods, same result):**

1. A `venv --without-pip` interpreter that genuinely has no pymongo:
   `python3 -m venv --without-pip <v> && <v>/bin/python3 tests/test_mongo_store.py`
2. A `sys.meta_path` hook that raises `ImportError` for `pymongo`/`bson`.

Result of (1), the authoritative one:

```
PASS tests/test_bootstrap.py
PASS tests/test_fixtures.py
FAIL tests/test_mongo_store.py      <-- exit 1
PASS tests/test_refs.py
PASS tests/test_stdlib_only.py
PASS tests/test_store.py
PASS tests/test_tailer.py
PASS tests/test_ws.py
```

**Traceback essence:**

```
File "tests/test_mongo_store.py", line 396, in
     test_bulk_upsert_applies_the_same_guards_as_the_memory_pass
  check(ms.bulk_upsert(NoDb(), "records", []) == { ... },
        "an empty batch is a no-op, not a connection")
File "aggregator/mongo_store.py", line 1355, in bulk_upsert
  raise MongoUnavailable(f"pymongo is not installed: {exc}") from None
aggregator.mongo_store.MongoUnavailable: pymongo is not installed:
  No module named 'pymongo'
```

Note this is an **uncaught exception that aborts the whole file** — the four
later test functions (`test_gd25_acceptance_normal_shuffled_reversed`,
`test_the_disjoint_continuations_union`, `test_dotted_keys_…`,
`test_oversize_…`, `test_ts_…`, `test_no_delete_verbs_…`,
`test_client_options_are_gd21s`, `test_live_mongod_arm`) never run at all on a
bare checkout. It is not a soft failure of one assertion.

**Why it is attributable to this change.** Both halves are new in attempt 2 and
both come from the attempt-1 critique's **M2** fix ("`bulk_upsert` skips both
guards `apply_operations` applies"). The implementer correctly hoisted
`spec_for` / `check_id` / `validate_update` **above** the pymongo import — the
docstring even says so verbatim ("Both are refused *before* pymongo is even
imported, so the guard is testable with nothing third-party installed") — and
added the new `NoDb()`-driven test to prove it. The empty-batch assertion on the
last line of that new test is the one call in the block that is expected to
*succeed* rather than raise, and it falls through the guard loop (which never
executes for an empty `operations` list) straight into the import. The
short-circuit that would save it, `if not requests: return {...}`
(`aggregator/mongo_store.py:1357`), sits **one line after** the import that
raises. So the module's stated contract and its own new test disagree with the
code by exactly two lines of ordering.

**Concrete fix (one line moved, in `aggregator/mongo_store.py:1339-1358`):**
return the zero-result dict before touching pymongo. Either

```python
    checked = []
    for key, update in operations:
        ...
        checked.append((key, update))
    if not checked:                      # <-- moved above the import
        return {"matched": 0, "upserted": 0, "modified": 0,
                "tolerated_dups": 0, "errors": []}
    try:
        from pymongo import UpdateOne
        from pymongo.errors import BulkWriteError
    except ImportError as exc:
        raise MongoUnavailable(...) from None
    requests = [UpdateOne(...) for key, update in checked]
    try:
        result = db[collection].bulk_write(requests, ordered=ordered)
```

(and drop the now-dead `if not requests:` block), which also makes the
assertion's own wording true — "not a connection" should mean not even an
import.

Recommended hardening so this class of regression cannot come back silently:
add to `tests/test_stdlib_only.py` (or a new guard in `test_mongo_store.py`) an
arm that re-executes the pure-path assertions under a `sys.meta_path` blocker
for `pymongo`/`bson`, i.e. assert that `bulk_upsert(NoDb(), "records", [])`
and every `SchemaError`/`OperatorError` refusal above it behave identically with
pymongo unimportable. GD-21's "every module imports without pymongo installed"
is currently tested at *import* granularity only; this failure is at
*call* granularity, which is where the guarantee actually has to hold.

---

## 4. Green evidence for everything else

### 4a. Live `mongo:7` arm (R-42 loopback+auth, container `touch-mongo-sp05`,
`127.0.0.1:27117`, `authSource=admin`)

`TOUCH_MONGO_URI=... python3 tests/test_mongo_store.py` → **rc 0**, live arm
fully exercised rather than skipped:

```
test_live_mongod_arm
  ok: ensure_schema created GD-24's collections
  ok: …and running it again is a no-op (it must be safe on every boot)
  ok: no index on the server carries expireAfterSeconds (GD-26, read back)
  ok: the unique {stream:1,seq:1} index exists on the server
  ok: the server REFUSES a sub-document _id
  ok: normal / shuffled / reversed ingest into a real mongod ⇒ ONE fingerprint
      (normal=b72e4348, reversed=b72e4348, shuffled=b72e4348)
  ok: …and equal counts: {'agents': 7, 'records': 1091, 'run_nodes': 7,
      'runs': 1, 'stream_meta': 34, 'usage': 328}
  ok: …and the in-memory model agrees with the server byte for byte
  ok: the (stream, seq) cursor query is an IXSCAN
  ok: …and so is the zero-padded _id range scan
  ok: replaying an event we already stored is not an error
  ok: a second writer landing on an existing (stream, seq) is COUNTED as a
      tolerated duplicate, never swallowed (GD-29)
  ok: the server REFUSES a mirrored document with no provenance (GD-28)
  ok: a TTL index someone added by hand makes the next ensure_schema REFUSE
  ok: dropping only the database this test constructed: touch_test_58353 (GD-27)
```

The GD-27 scoping is honoured: the database is `touch_test_<pid>` and only that
constructed name is dropped.

### 4b. Every attempt-1 critique finding is now covered by a non-tautological test

| id | claim | evidence in this run |
|---|---|---|
| **B1** | `sessions` `_id` is a tagged union | new `test_sessions_id_is_a_tagged_union`: `check_id`/`validate_document`/`apply_operations` all accept **both** `live:622-10028` and `hist:292fc08c-…`; a bogus id is still rejected; the rejection message names **both** grammars |
| **M1** | null-vs-absent on pinned fields | "an omitted byte_offset leaves the field OUT of the stub, not null"; "a present null on a pinned field is refused client-side, because that is what the server's own $jsonSchema does" |
| **M2** | `bulk_upsert` guards | new `test_bulk_upsert_applies_the_same_guards_as_the_memory_pass`: off-table name, non-canonical `_id`, wrong padding, sub-document `_id`, `$inc` all refused; plus a source-text check that `check_id(`/`spec_for(` are named in `bulk_upsert`'s body. **This is also the test that fails §3** |
| **M3** | `$addToSet` field-order semantics | "two sub-documents differing ONLY in field order are two set elements, as they are on the server"; byte-identical re-add still idempotent |
| **M4** | mandatory `provenance` | live arm: "the server REFUSES a mirrored document with no provenance — the $jsonSchema requires it" |
| **m1** | nested-path conflicts | "a field and a path INSIDE it conflict, exactly as they do at the server"; "…while two paths that merely share a prefix STRING do not conflict" (the negative arm makes it non-vacuous) |
| **m2** | spoofable `_raw` wrapper | "a dict carrying the wrapper's fields AND hostile siblings is not a wrapper"; "…so its dotted sibling is caught, not skipped over"; "…and a non-string _raw is not a wrapper either" |
| **m4 / n2 / n4** | `guard_oversize` ownership documented; TTL reconciled on the server; delete verbs unreachable via `getattr` | `bulk_upsert` docstring names `mirror.py` (R-45) as the caller; live "a TTL index someone added by hand makes the next ensure_schema REFUSE"; "…and no delete verb is SPELLED as a string either" |

`test_stdlib_only.py` remains green, confirming GD-21 at import granularity:
`refs.py` imports `{__future__, re}` only; `mongo_store.py` module level is
`{__future__, datetime, hashlib, json}` with pymongo imported inside functions.

### 4c. Ownership / git hygiene (GD-15, git rules)

Only the four owned files carry mtimes inside this attempt's window
(`refs.py` 22:31, `test_refs.py` 22:31, `test_mongo_store.py` 22:32,
`mongo_store.py` 22:33). Sibling aggregator/test files are older
(`store.py`/`test_store.py`/`test_tailer.py` 21:22, `ws.py` 21:17,
`tailer.py` 21:07, `__init__.py` 19:28, `test_ws.py`/`test_stdlib_only.py`
20:26, `run_all.sh` 19:41, `test_bootstrap.py`/`test_fixtures.py` 15:37–16:10)
and the monitoring module is untouched.

Tracked non-`.claude` modifications are `.gitignore` (15:37) and `CLAUDE.md`
(22:03) — both **predate** this attempt's window; `CLAUDE.md` is sp-15's file
and was already flagged as an out-of-band edit by the attempt-1 critique. Not
charged to this implementer.

No commit was made; nothing was reverted or stashed; the only Docker artefacts
are the pre-existing critique/sp-05 containers, and the live arm dropped only
its own `touch_test_<pid>` database.

---

## Bottom line

One two-line ordering defect stands between this sub-plan and green. The
substance of R-43/R-44 — GD-24's table (now including the `sessions` tagged
union), GD-25's order-independence oracle proven against a real mongod, GD-26's
no-delete/no-TTL laws, GD-27/28/29 — is in place and independently verified.
Fix the `bulk_upsert` empty-batch short-circuit so it precedes the pymongo
import, and add the pymongo-absent call-granularity arm, and this passes.
