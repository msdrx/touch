# sp-mirror-deploy — adversarial critique, attempt 2

**Verdict: REJECTED.** 2 major, 2 minor, 4 nits. depth: `in-scope`.
`critical_defect: false`.

Reviewed: `aggregator/mirror.py` (2278 lines, new/untracked), `docs/mongo.md` (296),
`tests/test_mirror.py` (1160), `tests/test_mongo_deploy.py` (843), against
`touch-mongo-live-subplans.md` §sp-06, plan items **R-45 / R-42 / R-57:mongo-doc**,
and **GD-21…GD-30 / SD-1 / SD-10 / SD-11** plus the base plan's GD-1…GD-20.

Independently re-run, not taken on trust:

- `tests/test_mirror.py` → rc=0 (2 clean skips: no entity module, no `TOUCH_MONGO_URI`).
- `tests/test_mongo_deploy.py` → rc=0 **including the real `mongo:7` docker arm**:
  container provisioned from the page's own fenced recipe on `127.0.0.1:52571`,
  four anonymous-access refusals, the documented `createRole`/`createUser` script
  executed as written, the mirror reaching `live` as the least-privilege user with
  `lastError: None`, the server refusing `deleteMany` on `records` while permitting
  it on `stream_meta`, and `--rebuild`'s `dropCollection` grant proven scoped
  (`derived` yes, `records` no).
- Ownership: `git log -1` still `579446e`; only the four owned paths carry
  post-implementer mtimes (`aggregator/mirror.py`, `docs/mongo.md`,
  `tests/test_mirror.py`, `tests/test_mongo_deploy.py`); `.gitignore` / `CLAUDE.md`
  untouched by this pass.

**All five attempt-1 items I could re-test are genuinely closed**, and closed with
load-bearing assertions rather than restatements: `--backfill` now walks through
`iter_backfill_observations` and `main()` calls it (AST-asserted); the mtime guard
**fails closed** on a 2-tuple carrying `now()` (`refused_no_source`, counted, with
the reason on `/health`); `rebuild()` resolves the registry *before* the drop and
keeps `derived` when a kind is unmapped, reporting `unmappedKinds`; the wipe test
really clears the same store; `lastError` no longer cries wolf (`notes`);
`enqueue` degrades from `starting` and `start()` refuses to promote over a loss;
the DB fence is `touch_`; `key`/`keys` are value-exempt only; both test files are
0755 with shebangs; the docker arm `check(False, …)`s instead of skipping once
docker is established. That is a real attempt-1 close-out.

What follows is new ground, and the first item is a **demonstrated silent data
corruption** — not an argument.

---

## MAJOR 1 — GD-27's document backstop redacts GD-24 **schema** field names inside sub-documents: every mirrored `ref` to a slot or a custom-state head is written with `sessionKey`/`stateKey` = `"[redacted]"`

**`aggregator/mirror.py:235`, `:248-254`, `:346-354`, `:766-774`**
(with `aggregator/refs.py:669` `slot`, `:663` `customState`, `:759` `canonical_ref`)

`SECRET_KEY_RE = re.compile(r"(?i)(token|secret|key|password|auth)")` is applied by
`scrub_value()` to **every string field at every depth** of an operation's values.
GD-24's own table and `refs.KIND_SPECS` contain field names that match it:

| declared name | where | matches on |
|---|---|---|
| `sessionKey` | `slots` key field; `custom_state_events` type pin; `refs` kind `slot` | `Key` |
| `stateKey` | `custom_state` `_id` = `<refId>#<stateKey>`; `refs` kind `customState` | `Key` |
| `author` | `custom_state_events` type pin (GD-28's writer field) | `auth` |

`SECRET_KEY_EXEMPT` (`:248`) enumerates five names — `apiKeySource`, `authType`,
`keyType`, `toolUseKey`, `publicKeyId` — so the hazard was seen; the schema's own
names were not added to the list.

Reproduced against the real code, no test doubles:

```
$ python3 -c "from aggregator import mirror as mr; print(mr.scrub_value(
      {'sessionKey':'622-10028','author':'agent:driver','stateKey':'foo'}))"
{'sessionKey': '[redacted]', 'author': '[redacted]', 'stateKey': '[redacted]'}
```

and, end to end, through `validate_op()` on a shape sp-11/sp-12 will emit constantly —
a `custom_state_events`/`events` document carrying GD-24's mandatory
`ref{}` + `refId` pair, built by the sanctioned `refs.canonical_ref`:

```
scrubbed ref: {'kind': 'slot', 'sessionKey': '[redacted]', 'root': 'r',
               'name': 'n', 'attempt': 1}
refId       : 'slot:622-10028|r|n|001'          # the same datum, intact
queue revalidation: OK      (the corrupted op is accepted and written)
validate_document : OK      (no validator catches it either)
```

Three separate GD-24 clauses are broken by that one document:

1. *"structured refs are queried by dot notation only"* — `{"ref.sessionKey": …}`
   now matches **nothing**, for every slot ref in the store. The join path the
   schema mandates is dead for the one collection R-53's name↔agentId hop needs.
2. The document contradicts **itself**: `refId` carries `622-10028`, `ref.sessionKey`
   carries `[redacted]`. `refs.ref_id_kinds()` / `_parse_slot()` round-tripping the
   two against each other cannot agree.
3. It is **inconsistent across collections** for the same datum: `slots.sessionKey`
   is top-level and survives (`scrub_op_update` at `:773` only descends into
   *values*, never checks top-level field names), while the same value nested one
   level inside `ref` is destroyed. Nobody downstream can predict which copy is real.

And it is **permanent**: this is a mirror that exists because the CLI deletes
history (GD-26), it is upsert-only, `$set` of `[redacted]` wins, and no later
re-ingest restores the value — the mapper produces the same redacted output every
time. A rebuild does not fix it; only a code change plus a re-ingest does.

Neither owned suite catches it: `test_secrets_never_survive_redaction`
(`test_mongo_deploy.py:434-483`) tests `authToken`, `password`, `apiKeySource`,
`key`/`keys` — every case is a *value* the backstop should or should not touch, and
none is a **schema field name**. That is the hole.

**Fix** (small, and in the one owned module):

1. Never scrub inside the canonical `ref` sub-document — its shape and value
   grammar are already validated by `refs.validate_ref`, so nothing arbitrary can
   hide there:
   ```python
   def scrub_op_update(update):
       return {op: {field: (value if field == "ref" else scrub_value(value))
                    for field, value in fields.items()}
               for op, fields in update.items()}
   ```
2. **And** widen the exemption to the declared vocabulary rather than to more
   hand-picked strings, so sp-07…sp-11 cannot re-open it by adding a schema field:
   ```python
   SCHEMA_FIELD_NAMES = frozenset(
       name for spec in refs.KIND_SPECS.values()
       for name in (spec.required + spec.optional))
   # plus mongo_store's declared per-collection `types` keys
   ```
   and skip `_is_secret_key` for any name in it. Keep the closed
   `CLASSIFICATION_VALUES` rule for `key`/`keys` inside `data.custom` — agent-asserted
   payloads are exactly where a quoted credential can appear, and that half is right.
3. Add the test the hole passed through: build an `events` op whose `ref` is
   `refs.canonical_ref({"kind":"slot", …})`, push it through `validate_op` and the
   queue, and assert `stored["ref"]["sessionKey"] ==
   refs.parse_ref_key("slot", stored["refId"])["sessionKey"]` — i.e. the two copies
   of the datum agree. Same for `customState`/
   `stateKey` and for `custom_state_events.author`.

## MAJOR 2 — the GD-27 scrub is an O(document) walk run **on the poll-loop side** of GD-30's line, and then run again on the drainer side

**`aggregator/mirror.py:763`** (with `:1661-1668`, `:1708`, `:366-392`)

The module's own contract is explicit — `map_and_enqueue` (`:1664`): *"Mapping is
pure and **cheap**, so it happens on the caller's side of the line; only the write
crosses it"*, and the module docstring's invariant 1 is GD-30's pinned
**"Mongo contribution to the critical path: 0 ms"**. But `validate_op()` — called
by `Mapper.__call__` for every operation, i.e. inside `map_and_enqueue`, i.e. inside
the 250 ms poll loop — ends with `scrub_op_update(update)`, which deep-walks every
dict, list and `_raw` wrapper of the document.

Measured on this machine, against a document the size the corpus actually contains
(R-44 records an 872 KB real maximum):

```
document bytes                     553 791
ms.validate_update  (the guard)      0.006 ms   # skips _raw subtrees
mr.scrub_op_update  (the backstop)   8.79 ms
mr.validate_op      (both)           9.73 ms
```

So the *validation* GD-25/GD-24 require is free, and the mirror-only backstop is
1600× more expensive — 8.8 ms per 550 KB document, ≈14 ms for the corpus maximum,
**inline in the loop the plan budgets at 0 ms for Mongo and 50 ms for reduce+push**.
A burst of large tool results in one tick spends the whole reduce budget scrubbing
for a database that is not on the path.

It is also **redundant**: `_take_batches()` (`:1708`) re-runs `validate_op(op,
source="queue")` on every operation as it leaves the queue — on the drainer side,
where the cost is free by design. The scrub therefore happens twice, and the second
one is the one that matters (it is the last thing before `bulk_upsert`).

The existing guard cannot see this: `test_enqueue_never_blocks_never_raises_and_never_awaits`
(`test_mirror.py:228-261`) asserts over the **AST** that `enqueue` contains no
`Await` node. Synchronous-but-slow passes that test perfectly.

**Fix.** Give `validate_op` a `scrub=True` keyword and pass `scrub=False` at the
registry boundary (`Mapper.__call__`, `stamp_gen`, `stamp_backfill`), keeping the
scrub where it already runs anyway, in `_take_batches`. Then assert the property
instead of the syntax: enqueue N operations carrying a ~500 KB `_raw` payload and
check the wall time of `map_and_enqueue` stays a small fraction of `TICK_BUDGET`,
plus a call-counting assertion that `scrub_value` runs **once** per operation.

---

## Minor

**m1 — `rebuild()`'s defensive ordering covers unregistered kinds but not failing
mappers, and the report hides the difference.** `aggregator/mirror.py:1998-2016`.
`unmapped` is computed as `kind not in self.registry` *before* anything is mapped,
and the drop is skipped only on that. A kind that IS registered but whose mapper
raises (`Mapper.__call__` wraps every mapper bug as `MapperError`, `:632-646`) takes
the other branch: `derived` is dropped at `:2007`, then every observation is rejected
by `map_total`, and the report says `{"replayed": 0, "unmapped": 0,
"unmappedKinds": [], "droppedDerived": true}` — the reducer's collection destroyed,
nothing replayed, and no number in the report that says so (only `stats["rejected"]`,
which the report does not carry). That is the same shape as attempt-1's MAJOR 2, one
step over. *Fix:* map the whole batch first — mapping is pure, that is the module's
own claim — and drop `derived` only if the mapping pass produced zero rejections;
add `"rejected"` to the returned report either way.

**m2 — a mirror that loses the writer lease never tries to take it back.**
`aggregator/mirror.py:1578-1584` with `:1751-1752`. `acquire()` sets
`state = STATE_REFUSED` and `_lease["held"] = False`; `tick()` returns immediately
for `STATE_REFUSED`; `enqueue()` (`:1619-1622`) counts everything as
`refused_no_lease`; and no code path ever calls `acquire()` again — the only other
call site is `start()`, which runs once. So after a *transient* takeover (this
process stalled past the 30 s TTL, another took the lease, then exited) this
aggregator mirrors nothing for the rest of its lifetime while `/health` reports
`refused` forever, with no operator-visible remedy but a restart. GD-29 requires
that a process which cannot hold the lease refuse to mirror; it does not ask for
that refusal to be terminal, and the lease is TTL-based precisely so it can be
re-taken. *Fix:* in `tick()`, when `state == STATE_REFUSED and not _lease["held"]`,
retry `acquire()` at most once per `lease_ttl` and clear the refusal on success;
test the full cycle (lose → other holder expires → re-acquire → writes resume).

---

## Nits

**n1 — `docs/mongo.md:39` mis-states the `/health` block it documents.** The page
says `{state, lastError, notes, queued, dropped, tolerated_dups, lease, counters}`;
`health()` (`mirror.py:1460-1471`) also returns `backend` and `db`. sp-12 serves this
verbatim (R-30), so the doc is the contract — list both, or drop them from the dict.

**n2 — `backfill()` stats the same file once per observation.**
`aggregator/mirror.py:2060`: `_mtime(source, None)` runs inside the per-observation
loop, so a walk of 4 000 observations over 300 files performs 4 000 `stat()` calls.
Memoize into the `mtimes` dict on first sight (`mtimes.setdefault(source, _mtime(...))`) —
it is already the parameter for exactly this.

**n3 — `iter_backfill_observations` calls every registered source once per file.**
`aggregator/mirror.py:2197-2200`: five entity modules × N transcripts. The declared
contract ("a source handed a path it does not own returns nothing", `:2127`) makes
that correct but not necessarily cheap. Say in the docstring that the ownership
decision must be made from the path (extension/parent dir), never by opening or
parsing the file, so sp-07…sp-11 implement it that way.

**n4 — `MongoConfig.secrets` does not percent-decode the URI's password.**
`aggregator/mirror.py:551-564`. A password written `p%40ss` in the URI yields the
literal `p%40ss` for `redact()`'s literal pass, so a driver message quoting the
*decoded* password would survive it. The structural pass still covers the URI form,
so this is only the belt of the belt-and-braces — decode with
`urllib.parse.unquote` before appending.

---

## What was checked and found clean (so a re-attempt does not churn it)

- **GD-21** — no module-level pymongo; every import is inside a function
  (`connect`, `bulk_upsert`, `guarded_update`, `update_many`, `delete_many`,
  `drop_collection`); `AsyncMongoClient` is the driver; `ms.client_options()` is the
  single source of the four pinned timeouts; absence is `absent`, never an exception.
- **GD-22 / GD-30** — `enqueue` is a plain `def`, never awaits, never raises, drops
  and counts on a full queue; the drainer is a separate task woken by an event; the
  dead-port arm shows connect < 10 s and every post-breaker tick under the 250 ms
  budget with at most `BREAKER_FAILURES` ticks paying a timeout; a mid-tick outage
  requeues rather than losing writes and the overflow is counted.
- **GD-24 / GD-25 / SD-11** — every `_id` comes from `refs.ref_key`; `validate_op`
  runs `spec_for` + `check_id` + `validate_update` at the registry boundary **and**
  at the queue; no `$inc`, no `$unset` anywhere (AST- and text-asserted); replay of
  own output changes nothing, against the memory model **and** against a real mongod,
  with identical fingerprints between the two.
- **GD-26** — `Backend` exposes no delete verb but the scoped one; both concrete
  backends refuse every collection but `stream_meta`/`derived` in their own bodies
  (AST-asserted); `_assert_scoped` refuses `{}` and gen-only scopes; Mongo's
  missing-field semantics for `{gen:{$lt:G}}` are reproduced so incremental appends
  survive a sweep; no `expireAfterSeconds`; the server-side role proves the wall
  independently of the code.
- **GD-27** — the docker arm provisions the page's own recipe and verifies the
  loopback publish as the kernel reports it, four anonymous refusals, the documented
  bootstrap script, `usersInfo`-denied read as healthy rather than zero, and the
  zero-users refusal naming `mongo.md`. Credentials: `0o177` mask, symlink refusal,
  `O_EXCL`+0600 at open time, `describe()`/`/health` password-free, both redaction
  passes, no connection-string literal under `aggregator/`, `touch_` fence with
  `touchdown_prod` in the hostile list, `touch_test_<pid>` dropped by name,
  `docker rm -f -v` + `volume rm` in `finally`, `holderBoot` a truncated digest.
- **GD-28** — mirror.py never emits a `provenance` of its own; the pins live in
  `mongo_store`'s specs where sp-05 owns them.
- **GD-29** — take / renew / expiry-takeover are all correct and proven against a
  real unique index; tolerated dups are counted, not swallowed (see m2 for the one
  gap, which is recovery, not correctness).
- **SD-1 / SD-10** — one kind one owner enforced at discovery with both owners
  named; mapper output validated with the mapper named; the `MIRROR_SOURCES(path=None)`
  signature is declared in `iter_sources`'s docstring and asserted by a test;
  cursors round-trip the whole `(st_dev, st_ino, size, offset, gen)` identity with
  `$set` so a shrink can rewind them.
- **R-57 / docs** — `docs/mongo.md` carries the loopback+auth recipe, the
  never-publish-27017 clause, the derived-DB-name rule with the `touch_` underscore,
  the rebuild/backfill commands (matching the implemented behaviour, including the
  keep-`derived` branch and the fail-closed mtime guard), "Mongo down is a
  non-event" with the four degradation states, and R-57's measured growth table
  verbatim. The docker arm parses the recipe out of the page, so a doc edit that
  weakens it fails the suite.

## Verdict fields

- **approved:** `false` (2 major).
- **depth:** `in-scope`. Both majors are edits to two functions of the one owned
  module (`scrub_op_update` / `validate_op`) plus tests; the minors are localized to
  `rebuild()` and `tick()`. No architectural rework, no sub-plan boundary crossed,
  no missing upstream research — the exemption set MAJOR 1 needs is already
  derivable from `refs.KIND_SPECS` and `mongo_store`'s specs, both of which exist.
- **critical_defect:** `false`. MAJOR 1 corrupts a field that no sub-plan has
  written yet (no entity module exists, so no mapper emits a `ref` today), which is
  exactly why fixing it now is cheap and fixing it after sp-07…sp-11 is a re-ingest
  of a permanent store. Nothing already on disk is corrupted, and the
  queue/breaker/lease/cursor/sweep core those sub-plans build on is sound and proven
  against a real mongod.
