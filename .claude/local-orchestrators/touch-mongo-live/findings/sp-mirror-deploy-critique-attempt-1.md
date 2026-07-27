# sp-mirror-deploy — adversarial critique, attempt 1

**Verdict: REJECTED.** 2 major, 5 minor, 5 nits. depth: `in-scope`.
`critical_defect: false`.

Reviewed: `aggregator/mirror.py` (1998 lines, new/untracked), `tests/test_mirror.py`
(928), `tests/test_mongo_deploy.py` (794), against `touch-mongo-live-subplans.md`
§sp-06, plan items **R-45 / R-42 / R-57:mongo-doc**, and **GD-21…GD-30 / SD-1 /
SD-10 / SD-11**. `docs/mongo.md` is the fourth owned file but was not in the diff
list; it is read here only where a test parses it, and nothing below asks for an
edit to it that a test does not already force.

Independently re-run, not taken on trust:

- `tests/test_mirror.py` → rc=0 (2 clean skips: no entity module, no `TOUCH_MONGO_URI`).
- `tests/test_mongo_deploy.py` → rc=0 **including the real `mongo:7` docker arm** —
  the container came up on `127.0.0.1:34123`, the four anonymous-access refusals
  fired server-side, the documented `createRole`/`createUser` script ran as
  written, the mirror reached `live` as the least-privilege user, and the server
  itself refused `deleteMany` on `records` while permitting it on `stream_meta`.
- `tests/run_all.sh --keep-going` → **14 passed, 0 failed, 39 s**. No regression.
- Mutation probe (scratch copy, source untouched): dropping `retracted:true` from
  the sweep's `$set` turns `test_mirror.py` red. The GD-26 assertion is
  load-bearing, not decorative.

This is strong work — the redaction discipline, the `Backend` surface that
enforces GD-26 by *omission*, the memory-vs-real fingerprint equivalence, and the
docs-parsed-then-provisioned docker arm are all better than the plan asked for.
The two majors are both **wiring gaps at the CLI/seam boundary**, not design
faults, and both are demonstrated below with a running probe rather than argued.

---

## MAJOR 1 — `--backfill` does not do what R-45, `--help` and `docs/mongo.md` all say it does; the mtime guard is inert on the only production path

**`aggregator/mirror.py:1975-1976`** (with `:1917-1936`, `:1860`, `:1948`)

R-45's Approach is explicit: `--backfill` *"walks `~/.claude/projects/**` once,
hard-codes `live=False`, refuses any `ts` newer than source mtime, stamps
`ingestMode:"backfill"`."* Three of the four are implemented. The walk is not
wired, and its absence silently disables the third.

`iter_backfill_sources()` (`:1917`) implements the walk correctly — deny-list at
the source, `.jsonl` only, stable order — and **is never called by `main()`**. The
only caller in the repo is `tests/test_mongo_deploy.py:488`:

```
$ grep -rn "iter_backfill_sources" --include=*.py .
aggregator/mirror.py:1917:def iter_backfill_sources(...)      # definition
tests/test_mongo_deploy.py:488:        found = mr.iter_backfill_sources(root)
```

What `main()` actually runs for `--backfill` is byte-identical to `--rebuild`'s
source — `iter_sources()` (`MIRROR_SOURCES`) — and the comprehension **structurally
discards any source path**, forever:

```python
observations = [(kind, obs) for kind, source in iter_sources()   # mirror.py:1975
                for obs in source()]                             #        :1976
```

Every element is a 2-tuple, so in `backfill()` the unpack at `:1858` yields
`source = None`, and the guard at `:1860` falls back to
`_mtime(None, moment) → moment`, i.e. **`now()`**. The refusal is then
`ts > now()`, which no mapper reading a historical file can ever trip. The exact
failure R-45 names — *"a mapper that reaches for `now()` because a journal record
has no timestamp (SESSIONJSONL-5) would stamp a whole historical corpus with
today's clock"* — passes:

```
PROBE1  ("record", {"ts": now()})  →  {'live': False, 'stamped': 1, 'refused': 0}
        stored ts = 2026-07-26T01:16:23Z      refused_future_ts = 0
```

That is a document stamped with the import's clock, accepted by the backfill path,
counted as a success. This is not hypothetical for later sub-plans either: even
once sp-07…sp-11 land their `MIRROR_SOURCES`, line 1975 cannot pass a source
path, so the guard stays inert by construction.

The divergence is also *documented as fact* in two places this sub-plan owns:
`_usage()` at `:1948` (`"--backfill   one-shot historical walk of
~/.claude/projects (live=False)"`) and `docs/mongo.md` §5 (`"one-shot historical
walk of ~/.claude/projects/** (live=False, always)"` … `"refuses any operation
carrying a timestamp newer than its source file's mtime"`). Under GD-27/D13's
own posture, docs that describe an unimplemented guard are worse than no docs.

**Fix.** Make `--backfill` carry the source through. Minimum viable shape:

```python
# main(), the --backfill branch only — --rebuild keeps iter_sources() as-is
if mode == "--backfill":
    root = os.environ.get("TOUCH_CLAUDE_ROOT") or os.path.expanduser("~/.claude")
    paths = iter_backfill_sources(root)
    observations = [(kind, obs, path)                    # 3-tuple: backfill() already
                    for kind, source in iter_sources()   # accepts it (mirror.py:1858)
                    for path in paths
                    for obs in source(path)]
```

If `MIRROR_SOURCES` callables cannot yet accept a path (they do not exist), then
either (a) declare the per-source signature here so sp-08 implements against it,
or (b) keep the deferral but **say so**: change `_usage()` and `docs/mongo.md` §5
to state that the walk lands with the ingest modules, and make `backfill()` refuse
— rather than silently widen to `now()` — when it is handed an observation with no
source and no `mtimes` entry (`self.stats["refused_no_source"] += 1`). A guard
that cannot be evaluated must fail closed, not open. Add a test that feeds
`backfill()` a **2-tuple with a `now()` timestamp** and asserts it is refused;
today's test only refuses a ts an hour in the *future*, which no real mapper
produces.

## MAJOR 2 — `rebuild()` drops `derived` and then aborts on the first unmapped kind, leaving a half-built store and a `/health` that says `live`

**`aggregator/mirror.py:1809-1833`** (root cause at **`:1510-1516`**)

`map_and_enqueue()` is documented as the poll-loop-side seam (*"Mapping is pure
and cheap, so it happens on the caller's side of the line"*) but is **not total**:
`map_observation()` raises `MapperError` for any unregistered kind — by design, and
correctly (GD-26: data is never dropped quietly) — and `Mapper.__call__` re-raises
every mapper bug as `MapperError`. Nothing between that and the caller catches it.

`rebuild()` calls it in a bare loop *after* the destructive step:

```python
if drop_derived:
    await self.backend.drop_collection("derived")     # mirror.py:1825  destructive
replayed = 0
for kind, observation in observations:
    replayed += self.map_and_enqueue(kind, observation)   # :1828  can raise
```

Probe (three observations, the middle one an unmapped kind — the guaranteed state
today, since `ENTITY_MODULES` register nothing):

```
REBUILD ABORTED -> MapperError no mapper registered for observation kind 'session'
derived after: None      records replayed: 0 of 2
health: live             lastError: None
```

So an operator running `python3 -m aggregator.mirror --rebuild` against a store
whose corpus contains one kind an entity module has not registered yet gets: the
reducer-owned `derived` collection **dropped**, zero of the mappable observations
written (the queue was never flushed), an unhandled traceback out of
`asyncio.run()` in `main()`, and a `/health` block still reporting
`state: "live", lastError: null`. Every one of those is a thing this module's own
docstring promises cannot happen (invariant 2, *"no import failure, no startup
failure, no test failure"*; `note_error` described as *"the only writer of
`lastError`"* — here nothing writes it at all).

The module has already made the opposite decision one layer down and should be
consistent with itself: `_take_batches()` at `:1556-1564` explicitly refuses to let
a poison operation raise (*"dropped and counted, never re-queued"*). The same rule
belongs on the mapping side.

**Fix**, three small pieces:

1. `map_and_enqueue()` becomes total, mirroring `enqueue`'s contract:
   ```python
   def map_and_enqueue(self, kind, observation, *, stream=None) -> int:
       try:
           ops = map_observation(self.registry, kind, observation)
       except MapperError as exc:
           self.stats["rejected"] += 1
           self.note_error(exc)
           if self.state == STATE_LIVE:
               self.state = STATE_DEGRADED
           return 0
       return self.enqueue(ops, stream=stream)
   ```
   Keep `map_observation()` raising for callers that want it to.
2. `rebuild()` reports the skipped kinds instead of dying:
   add `"unmapped": <count>` (and the sorted kind names) to its return dict, so a
   rebuild that could not replay everything is visible rather than fatal.
3. Order the destructive step defensively: resolve/validate the registry against
   the observation kinds *before* `drop_collection("derived")`, or drop only after
   the first successful flush. Dropping first and discovering the replay cannot run
   second is the wrong order for the one operation whose entire purpose is proving
   GD-22.

Add a test: `rebuild()` over observations containing an unregistered kind must
return, must have replayed every *mappable* observation, and must leave
`/health` reporting `degraded` with a non-null `lastError`.

---

## Minor

**m1 — `test_wipe_and_rebuild_produce_the_same_fingerprint` never wipes anything.**
`tests/test_mirror.py:619-638`. It builds two *independent* `MemoryBackend`s and
asserts they agree. That is determinism of the mapper, which
`test_replay_of_own_output…` already covers; R-45's clause is *"Mongo wipe +
`--rebuild` ⇒ fingerprint equal to pre-wipe"*, i.e. rebuild **into the same store**
after clearing it. The current shape would pass even if `rebuild` left residue or
diverged when replaying onto a non-empty store. Compounding: the live arm
(`:832-892`) never calls `rebuild` or `drop_collection` at all, so the whole
rebuild path — including `AsyncBackend.drop_collection` and the `dropCollection`
grant in `docs/mongo.md` §2 — is unexercised against a real mongod.
*Fix:* keep one backend, snapshot the fingerprint, `backend.state.clear()` (and in
the live arm, drop + recreate via the documented role), rebuild, compare. Move the
rebuild scenario into `_live_checks` behind the existing skip.

**m2 — the R-45 24-hour assertion restates its own input.**
`tests/test_mirror.py:603-606`. The mapper is *handed* `historic =
2026-07-20T03:00Z`, so `all((now - s) > 24h)` cannot fail for a reason the guard
is responsible for. R-45's fixture clause is about a *file* dated 03:00Z and a
mapper that might reach for `now()`. As written this asserts the test's own
literal. See MAJOR 1's fix for the assertion that would carry weight.

**m3 — `Mirror.start()` leaves a spurious `lastError` on a healthy mirror.**
`aggregator/mirror.py:1393-1397`. Any exception from `user_count()` is recorded
via `note_error()`, then `users = None` is treated as the *healthy* answer and the
run continues to `STATE_LIVE`. `/health` then publishes `state:"live"` with a
non-null `lastError` from a condition the code decided was fine — the one thing
`/health` must not do is cry wolf on the route operators use to decide whether to
page. *Fix:* record it as `self.last_error = None` on reaching `STATE_LIVE`, or
keep the text in a separate `notes` field that is not `lastError`.

**m4 — `enqueue()` degrades only from `live`, so early drops are invisible.**
`aggregator/mirror.py:1501-1502`. `if self.state == STATE_LIVE: self.state =
STATE_DEGRADED`. A queue-full burst while the mirror is still `starting` (the
default state whenever a backend is injected, `:1324`) increments `dropped` but
leaves `/health` reporting `starting`; `start()` then overwrites it with `live`
and the state never reflects the loss. GD-30 says queue-full ⇒ `mirror:"degraded"`
without qualifying the prior state. *Fix:* degrade from `STATE_LIVE` **or**
`STATE_STARTING`, and do not let `start()` promote to `live` when
`stats["dropped"]` grew during startup.

**m5 — the two new test files are the only non-executable files in `tests/`.**
`tests/test_mirror.py`, `tests/test_mongo_deploy.py` are `0644`; all eight
siblings (`test_refs.py`, `test_store.py`, …) are `0755`. `run_all.sh` invokes
`"$PY" "$(basename "$f")"` so the suite is green either way, but CLAUDE.md and the
sub-plan both state the convention as *"each file is executable and exits non-zero
on failure"*. *Fix:* `chmod +x tests/test_mirror.py tests/test_mongo_deploy.py`.

---

## Nits

**n1 — `SECRET_KEY_EXEMPT` exempts bare `"key"` and `"keys"`.**
`aggregator/mirror.py:233-234`. `{"key": "sk-…"}` in a quoted environment dump
survives the backstop. `apiKeySource` / `authType` / `keyType` are well-justified;
`key` and `keys` are the two names most likely to hold the real thing. Consider
exempting them only when the value matches a short classification vocabulary.

**n2 — the DB-name fence is `startswith("touch")`, not `"touch_"`.**
`aggregator/mirror.py:376`, `:542`. `touchdown_prod` passes the "Touch only writes
to databases it constructed" check. `touch_test_<pid>` and `touch_<sha1>` both
satisfy the tighter `"touch_"`; tighten it and update
`test_the_database_name_is_derived_and_fenced` (`test_mongo_deploy.py:396`) with
`"touchdown_prod"` in the hostile list.

**n3 — a test arm gated on an unrelated path.**
`tests/test_mirror.py:609-616`: the "guard is against the SOURCE's mtime" checks
run only `if (HERE / "fixtures").exists()`, and nothing inside them touches
`fixtures/`. On a tree without that directory the assertion silently vanishes.
Drop the conditional.

**n4 — the docker arm converts genuine failures into skips.**
`tests/test_mongo_deploy.py:625-631`: `docker run` returning non-zero, or the
container never becoming ready within 90 s, `skip()`s. Those are gates that
already passed (`docker info` ok, image present locally), so a failure there is a
real failure of the documented recipe — precisely what R-42 asks this arm to
catch. *Fix:* `check(False, …)` once docker has been established as usable; keep
`skip()` only for the pre-flight reasons in `docker_unavailable()`.

**n5 — `/health` publishes the host's `boot_id`.**
`aggregator/mirror.py:1265-1288` → `health()["lease"]["holderBoot"]`. Not a
credential, and GD-24 does pin `holderBoot` as a string, but `/health` is the one
unauthenticated route (GD-13) and `/proc/sys/kernel/random/boot_id` is a stable
host fingerprint. A truncated hash of it would satisfy the lease's only
requirement (change across reboots, never collide) without publishing it.

---

## What was checked and found clean (so a re-attempt does not churn it)

- **GD-21** — no module-level `pymongo` import; 6 lazy in-function import sites;
  `tests/test_stdlib_only.py` allow-list already names `mirror.py`; the full suite
  is green and the gate independently proved a bare venv (pymongo absent + shadowed
  `docker`) skips every Mongo arm by name.
- **GD-22 / GD-30** — `enqueue` is a plain `def` with zero `Await` nodes (AST-asserted
  at `test_mirror.py:229-234`); 200 enqueues against an unreachable server measured
  in single-digit ms; breaker verified by *driver call count*, not by timing alone;
  requeue-on-outage loses nothing and the overflow that follows is counted.
- **GD-24 / GD-25 / SD-11** — every `_id` goes through `refs.ref_key`; `validate_op`
  runs `spec_for` + `check_id` + `validate_update` at the registry boundary *and*
  again at the queue; `$inc` and `$unset` absent from the source entirely; shuffled
  and reversed write orders produce a byte-identical fingerprint.
- **GD-26** — `Backend` exposes no delete verb but the scoped one; both concrete
  backends refuse every collection but `stream_meta` / `derived` **in their own
  bodies** (AST-asserted); `_assert_scoped` refuses `{}` and gen-only scopes;
  Mongo's missing-field semantics for `{gen:{$lt:G}}` are reproduced in
  `_matches`, so incremental appends survive a later sweep; no `expireAfterSeconds`
  anywhere. Mutation-tested: removing `retracted:true` turns the suite red.
- **GD-27** — verified live: loopback-only publish as the *kernel* reports it, four
  anonymous-access refusals, the documented least-privilege bootstrap executed
  from the page's own text, `usersInfo`-denied read as healthy rather than zero,
  zero-users refusal with `mongo.md` named in the message. Credentials: `0o177`
  mask, symlink refusal, `O_EXCL`+0600 at open time, `describe()` and `/health`
  both proven password-free, structural + literal redaction passes. No connection
  string literal and no `27017` under `aggregator/`. `touch_test_<pid>` created and
  dropped by name; `docker rm -f -v` + `volume rm` in `finally`.
- **GD-29** — lease take / renew / expiry-takeover all correct; independently
  probed the nastiest case: holder stalls past TTL, second process takes over,
  first returns and ticks → it detects the loss at renewal, flips to `refused`,
  and writes **zero** documents.
- **SD-1 / SD-10** — one kind one owner enforced at discovery; mapper output
  validated with the mapper named; cursor round-trips the whole
  `(st_dev, st_ino, size, offset, gen)` identity and `$set` (not `$max`) lets a
  shrink rewind it.
- **Ownership** — only the four owned paths carry post-implementer mtimes; nothing
  committed; no edit to any other sub-plan's files.

## Verdict fields

- **approved:** `false` (2 major).
- **depth:** `in-scope`. Both majors are localized edits to `main()`,
  `map_and_enqueue()` and `rebuild()` in the one owned module, plus three test
  additions. No architectural rework, no sub-plan boundary crossed, no missing
  upstream research.
- **critical_defect:** `false`. Nothing here corrupts work already done or work
  the remaining sub-plans will do: the queue/breaker/lease/cursor/sweep core that
  sp-07…sp-11 build on is sound and proven against a real mongod. MAJOR 1 makes a
  *documented* guard inert and MAJOR 2 makes one operator command destructive-then-
  fatal; both are contained to this module and cheaper to fix now than after five
  sub-plans call the seam.
