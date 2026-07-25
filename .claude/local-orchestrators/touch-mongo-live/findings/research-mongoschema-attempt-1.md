# research — mongoschema perspective — attempt 1

Perspective: **the MongoDB layer itself.** Feasibility probed empirically in this
sandbox; then collections, natural-key `_id` scheme, idempotent upsert
semantics, indexes, ref materialization, retention, failure modes, and the
exact amendment set that adopting Mongo forces on
`touch-full-recon-plan.md` (GD-1…GD-20, R-01…R-37) and the G2 precedent in
`touch-monitor-spawn-plan.md`.

## Environment probe results (all run 2026-07-25, throwaway dirs under `/tmp/claude-1000/mongoprobe`)

| Probe | Command | Result |
|---|---|---|
| pymongo install | `pip install --target=… pymongo` | **works** — `pymongo 4.17.0` + `dnspython 2.8.0`, no build step, exit 0 |
| mongod binary | `which mongod mongosh mongo` | **absent** |
| apt package | `apt-cache policy mongodb-org mongodb-server` | **no candidate** (not in the image's repos) |
| docker image | `docker pull mongo:7` | **works through the proxy** — digest `sha256:9bdaeb6d…`, 297 MB content / 1.19 GB disk |
| mongod runtime | `docker run -d -p 27017:27017 mongo:7` | **works** — server `7.0.39`, `maxWireVersion 21`, `maxBsonObjectSize 16777216`, `maxWriteBatchSize 100000` |
| stdlib client | hand-written BSON + `OP_MSG` (opcode 2013) over `socket` | **works** — `hello`, `insert`, `update` with `$max`, `find`, `createIndexes` all `ok:1` in ~90 lines, zero third-party code |
| change streams | `db.watch()` on the standalone | **fails** — `OperationFailure 40573 "The $changeStream stage is only supported on replica sets"` |
| tailable cursors | capped collection + `TAILABLE_AWAIT` | **works** on the standalone (received `[0,1,2,3]` live) |

Corpus used for every schema probe: **all 50 `*.jsonl` under
`~/.claude/projects/**`** (3 793 records, 15.86 MB raw), i.e. this session's
transcript, its `subagents/workflows/wf_930e210a-6da/` + `wf_cca84d59-933/`
trees, and the five sibling session trees including the `wf_829e6f58-b2f`
cross-session split.

**Teardown owed by this run:** `docker rm -f touch-mongo-probe` (left running
because a sibling researcher's probe is using the same daemon; see
MONGOSCHEMA-20).

---

## MONGOSCHEMA-1 — 8.8 % of transcript records carry **no `uuid`**; the D3/GD-11 "records keyed by uuid" identity has no arm for them, and every obvious fallback silently destroys data

**file:line**: `touch-full-recon-plan.md:156-160` (ref union: `{uuid} | …`),
`touch-full-recon-plan.md:618-627` (R-24 store),
`touch-monitor-spawn-plan.md:29-34` (G3 "records `uuid` (upsert)"),
`inception.md:223-225` (D3)
**severity**: blocker

**Scenario.** Measured over the full corpus (3 793 records at the time of the
run; the live session file grows during the run, so absolute counts drift by a
few): 3 460 records carry a `uuid` (**0 duplicates globally** — `uuid` *is* a
sound global primary key for those), and **333 do not**, i.e. 8.8 %.
By type: `last-prompt` 69, `mode` 67, `ai-title` 49,
`queue-operation` 45, `permission-mode` 44, `file-history-snapshot` 44,
`file-history-delta` 12, `frame-link` 2. In Mongo every document needs an
`_id`; the plan's identity table has no key for these 332.

The natural reflex — content hash as `_id` — was tried and **loses 43 % of
them**: 333 uuid-less records reduced to **191 distinct content hashes; 142
records vanished**. Worst collisions measured: `mode` ×24 and ×17,
`permission-mode` ×24 and ×17, `ai-title` ×19 and ×5 — i.e. the same
`{"type":"mode","mode":"…"}` line recurs byte-identically dozens of times per
session and each recurrence is a distinct real event at a distinct point in the
stream. An `_id` on `(type, sessionId)` is worse still.

Timestamp is not a key either: these records mostly have no `timestamp` field
at all (only 194 of 254 records in this session's transcript carry one).

**Recommendation.** Split them, do not force one key:

- `records` — `_id = <uuid>` (string). Only records that *have* a uuid. This
  is where upsert idempotence is free and exact.
- `stream_meta` — the uuid-less records, `_id = "<sessionId>#<lineNo>"` where
  `lineNo` is the 1-based line index in the transcript file. Line number is
  legitimate here **only** because the record has no other identity and because
  the collection is rebuilt wholesale when the tailer detects rotation/shrink
  (D6 / GD-20's checkpoint identity `(st_dev, st_ino, size, offset)` already
  mandates a full idempotent re-ingest from 0 in exactly those cases). Store
  `lineNo` as a real field too, and re-ingest with `deleteMany({sessionId})`
  before the re-insert so a compaction that renumbers lines converges rather
  than accumulating ghosts.
- State the rule in the amended GD-11 explicitly: **the ref union's `{uuid}`
  member covers only `user | assistant | system | attachment`; the CLI's
  four other buckets are stream metadata, not history, and are keyed
  positionally.**

Add to the amended GD-11 a positive test: ingesting the **frozen** fixture
corpus (R-03, not the live tree) twice yields exactly the fixture's
uuid-bearing count in `records` and its uuid-less count in `stream_meta` —
the counts are the assertion that nothing collapsed. On the live corpus at
probe time those were 3 460 and 333.

---

## MONGOSCHEMA-2 — `output_tokens` **grows** across the split records of one `message.id`; `inception.md:78` states the opposite, and a last-wins `$set` upsert makes the persisted token figure depend on write order

**file:line**: `inception.md:78-80`, `touch-full-recon-plan.md:168-171` (GD-11
tokens), `touch-full-recon-plan.md:644-646` (R-26 dedup)
**severity**: blocker

**Scenario.** `inception.md:78` says "`usage` is copied onto every split record
of one API response — naive summing over-counts output tokens 2.09x; dedupe by
`message.id`." The dedup conclusion is right; the premise "copied" is **false**.
Measured across the corpus: 901 distinct `message.id`s bearing `usage`, of which
**571 have *differing* usage objects across their split records**. The fields
that differ are exactly `output_tokens`, `iterations`, `server_tool_use`,
`speed` — `input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens` are constant per message.id. Concrete rows:
`msg_011CdNthEWuooih3feCoMuGq` → output_tokens `[43 … 205]`;
`msg_011CdNthURa7sjmTaMF9hsrC` → `[2 … 390]`;
`msg_011CdNtinKNsvK8pnefmBxtQ` → `[2 … 330]`. It is a **running counter**, not a
copy.

Totals for the whole corpus under four reduction rules:

| rule | output_tokens |
|---|---|
| naive sum over all records | 1 436 174 |
| **first-wins** | **381 025** (2.8× under-report) |
| last-wins | 1 054 459 |
| `$max` | 1 054 459 |

So a Mongo `usage` collection keyed `_id = message.id` with
`{"$set": {...}}` is correct **only if writes arrive in file-line order**.
`bulk_write(ordered=False)`, retries, a resumed tail that re-sends an early
partial, or two writers (dual-sink, or a re-ingest racing the live tail) can
each land the early partial last — a silently wrong, non-reproducible token
figure. I reproduced this failure class deliberately: shuffling the op list and
using `$set` gives a different total on each pass.

**Recommendation.** Upsert usage with **`$max` per numeric field**, never
`$set`:

```
UpdateOne({"_id": message.id},
          {"$max": {"in": input_tokens, "out": output_tokens,
                    "cached": cache_read_input_tokens,
                    "cache_write": cache_creation_input_tokens},
           "$setOnInsert": {"agentId": …, "sessionId": …, "runId": …}},
          upsert=True)
```

Verified: two passes over the corpus with the op list **randomly shuffled each
time** produced byte-identical totals (`in 27 593 / out 1 062 413`) —
order-independent and idempotent. This also matches GD-20's inherited
"monotonic token deltas clamped ≥ 0" and makes the four-key GD-11 token shape a
storage-level invariant rather than an in-memory discipline. Correct
`inception.md:78` in the same edit (R-05 already touches that file).

---

## MONGOSCHEMA-3 — Mongo cannot be the live event bus for a standalone deployment: change streams need a replica set (verified failure), so requirement (3) must keep the file tail as the live source

**file:line**: `touch-full-recon-plan.md:262-272` (GD-20 do-not-inherit),
`touch-full-recon-plan.md:609-616` (R-23 tailer),
`touch-monitor-spawn-plan.md:39-44` (G5/G7)
**severity**: blocker (for any design that routes live status through Mongo)

**Scenario.** The obvious "Mongo makes monitoring live" design is: aggregator
writes, the web server opens a change stream, pushes over WS. On the deployment
that actually exists here — `docker run mongo:7`, no `--replSet` — `db.watch()`
fails outright: `OperationFailure 40573: The $changeStream stage is only
supported on replica sets`. `hello` confirms no `setName`. Building phase-3
live monitoring on change streams would mean the whole feature silently
requires a replica-set bootstrap (`--replSet rs0` + `rs.initiate()` + a
readiness wait + a `directConnection=true` URI quirk) that nobody has planned
for, and would make the dashboard's liveness depend on a database being up —
directly contradicting GD-20's "one deterministic, always-available source".

Two working alternatives were verified:

1. `--replSet rs0` + `rs.initiate()` → real change streams with resume tokens.
   Cost: a stateful init step, an oplog sized on disk, and a failure mode
   (`NotPrimaryNoSecondaryOk`) with no analogue in the file design.
2. Capped collection + `TAILABLE_AWAIT` cursor — **works on the standalone**
   (verified receiving live inserts). Cost: capped collections silently
   overwrite the oldest documents when full, forbid growth-in-place updates
   (so `$max` usage upserts cannot live there), and cannot be sharded or
   TTL'd. Usable only as a *notification* channel carrying `{stream, seq}`
   cursors, never as storage.

**Recommendation.** Adopt the explicit split and write it into the amendment:

> **Mongo is a queryable durable mirror, never the liveness path.** The live
> path stays exactly as GD-20/R-23 specify — poll + tail `*.jsonl` +
> `.touch/` event log, push over the existing WS. Mongo is written from the
> same reduction, asynchronously, and a Mongo outage degrades *querying and
> history*, never the dashboard.

If a Mongo-driven push is wanted later, use option 2 (`touch_notify` capped
collection, 16 MB, documents `{stream, seq, ts}` only) — it needs no
topology change. Record option 1 as a rejected-for-v0 discard with this
reason, so it is not re-hunted.

---

## MONGOSCHEMA-4 — pymongo's default `serverSelectionTimeoutMS` is 30 s; a dead Mongo freezes the 250 ms poll loop for 30 s per tick

**file:line**: `touch-monitor-spawn-plan.md:52-56` (G7 250 ms cadence),
`touch-full-recon-plan.md:699-712` (R-30 `/health`)
**severity**: major

**Scenario.** Measured against a port with nothing listening: the default
`MongoClient(...).insert_one()` raised `ServerSelectionTimeoutError` after
**30.1 s**. The aggregator's ingest tick is 250 ms. A stopped container, a
`docker restart`, or an OOM-killed mongod therefore stalls session discovery,
tailing and WS fan-out for two minutes per four ticks — the UI reports agents
as idle/unknown for reasons that have nothing to do with the agents. With
`serverSelectionTimeoutMS=300, connectTimeoutMS=300` the same call failed in
**0.5 s** — still 2 ticks.

**Recommendation.** Mongo I/O never runs inline in the poll loop:

- Dedicated writer thread/task with a bounded in-memory queue
  (drop-oldest with a counter, never unbounded).
- `serverSelectionTimeoutMS=500, connectTimeoutMS=500, socketTimeoutMS=2000,
  retryWrites=True`.
- A circuit breaker: after N consecutive failures, stop attempting for 30 s;
  surface `mongo: {state: up|degraded|down, lastError, queued, dropped}` on
  `/health` (R-30 already owns that endpoint and the "tailer whose target is
  gone exits, never polls forever" rule — same genre).
- Because `.touch/` remains the system of record (MONGOSCHEMA-12), the
  recovery action is simply "replay from the file store", not a lost window.

Test to add: a test that points the store at a dead port and asserts the poll
loop's tick duration stays under one tick budget and `/health` reports
`mongo.state == "down"`.

---

## MONGOSCHEMA-5 — Adopting Mongo does **not** require breaking D8 (stdlib-only runtime); a ~90-line stdlib wire client was proven working, so the D8 amendment is a real choice with two costed arms, not a forced concession

**file:line**: `inception.md:226-229` (D8), `touch-monitor-spawn-plan.md:24-28`
(G2 "stdlib only per D8… adopting it later is an explicit D5/D8 amendment"),
`touch-full-recon-plan.md:600-607` (R-22 "stdlib-only; statusline's `jq` is the
recorded exception")
**severity**: major

**Scenario.** The plan treats "adopt Mongo" and "break stdlib-only" as the same
decision. They are separable. I implemented BSON encode/decode plus `OP_MSG`
(opcode 2013) directly on `socket` — no third-party code — and ran, against the
live `mongo:7`:

```
hello         -> ok=1.0 maxWire=21 maxBson=16777216 maxWriteBatch=100000
insert        -> ok=1.0 n=1        (nested doc + array)
update $max   -> ok=1.0 n=1 nModified=1 (upsert:true)
find          -> ok=1.0, document returned
createIndexes -> ok=1.0 numIndexesAfter=2
```

~90 lines for the subset Touch needs. What it does **not** get: TLS, SCRAM
auth, replica-set discovery/failover, retryable writes, connection pooling,
compression, DBRef decoding, cursor `getMore` paging past the first batch
(easy to add), server-side sessions.

**Recommendation.** State the arms explicitly in the amendment and pick one:

- **Arm A — vendored stdlib client** (`aggregator/mongo_wire.py`, ~250 lines
  with `getMore`, SCRAM-SHA-256 and a retry wrapper). D8 survives verbatim;
  the runtime keeps zero third-party imports; the vendoring/pinning machinery
  R-22 already forbids is not needed. Cost: Touch owns a wire-protocol
  implementation and its tests; SCRAM is the fiddly part.
- **Arm B — pymongo, D8 amended to "stdlib + one pinned driver"**. Costs a
  real dependency (`pymongo` + `dnspython`), a lockfile/vendor step, and a
  named exception in R-22's "stdlib-only" guard test — which today would fail
  the moment `import pymongo` appears in `aggregator/`. Buys auth, TLS,
  failover, retryable writes for free.

Either way the amendment must name the consequence for **R-22's test**: the
stdlib-only static guard has to be updated in the *same* item, or it becomes a
red suite that the implementer disables. Prefer Arm A if Mongo stays a local
sidecar; Arm B the moment a remote/authenticated Atlas-style target is in
scope.

---

## MONGOSCHEMA-6 — compound sub-document `_id`s are **field-order sensitive**: `{s,n}` and `{n,s}` insert as two distinct documents

**file:line**: `touch-full-recon-plan.md:162-163` (GD-11: "a cursor is
`(stream, seq)`"), `touch-full-recon-plan.md:109-116` (GD-7 node identity
`(runId, key, ordinal)`)
**severity**: major

**Scenario.** The plan's two compound identities (`(stream, seq)` and
`(runId, key, ordinal)`) map most naturally onto a sub-document `_id`. BSON
document equality is **ordered**, and Mongo compares `_id` sub-documents by
byte equality. Verified:

```
insert {_id: {s:"x", n:1}}
count({_id: {s:"x", n:1}})  -> 1
count({_id: {n:1, s:"x"}})  -> 0        # same fields, reversed order
insert {_id: {n:1, s:"x"}}  -> ACCEPTED as a second, distinct document
```

Python dict insertion order is trivially divergent between the live-ingest path
and the re-ingest/backfill path (different constructor sites), or between a
stdlib client and pymongo. The failure is silent duplication of every node and
every cursor, which then double-counts in any `$group` rollup — precisely the
class of bug GD-11 exists to prevent.

**Recommendation.** Every `_id` in Touch's Mongo schema is a **string with a
fixed separator**, never a sub-document:

| entity | `_id` |
|---|---|
| session (live) | `"sess:<pid>-<procStart>"` |
| session (historical) | `"sess:sid:<sessionId>"` |
| record | `"<uuid>"` |
| stream_meta | `"<sessionId>#<lineNo>"` |
| agent | `"<agentId>"` (17-hex, validated) |
| run node | `"<runId>|<key>|<ordinal>"` |
| usage | `"<message.id>"` |
| touch event | `"<streamId>#<seq>"` |
| legacy event | `"legacy:<task>:<lineNo>"` |
| agent_state | `"<agentId>:<stateKey>"` |

Keep the components as ordinary indexed fields alongside for querying. Add a
store-level test asserting `_id` is always `str` and matches the documented
grammar.

---

## MONGOSCHEMA-7 — legacy `events.jsonl` contains byte-identical duplicate lines and dozens of duplicate timestamps; any content- or ts-derived `_id` deletes real events

**file:line**: `touch-full-recon-plan.md:662-679` (R-27 legacy adapter),
`touch-full-recon-plan.md:196-221` (GD-14),
`.claude/shared/monitoring/status.sh:28-46` (the writer)
**severity**: major

**Scenario.** `status.sh` emits `{ts, plan, stage, state, detail[, title]}` —
no id, no sequence, ms-precision `ts`. Measured over the four task folders:

| task | lines | byte-identical duplicate lines | duplicate `ts` values |
|---|---|---|---|
| touch-aggregator | 590 | 1 | 27 |
| touch-full-recon | 276 | 0 | 24 |
| touch-mongo-live | 210 | 2 | 11 |
| touch-repo-recon | 103 | 2 | 16 |

Two agents calling `status.sh` in the same millisecond, or two identical
`running` heartbeats, are indistinguishable by content or time. A hash `_id`
loses them; a `(task, ts)` `_id` loses up to 27 per file.

**Recommendation.** `_id = "legacy:<task>:<lineNo>"`, with `task`, `lineNo`,
`ts`, `plan`, `stage`, `state` as fields. Line number is stable because
`events.jsonl` is strictly append-only (unlike `~/.claude` transcripts) — and
GD-14's *never delete a finished task's `events.jsonl`* rule is exactly the
invariant that makes it safe. Note this in the amendment as a *dependency* of
the Mongo schema on GD-16/CLAUDE.md's never-delete rule, so nobody relaxes it
later. If `status.sh` is ever extended (R-10 already opens it for `flock` and
the 1 KB `detail` cap), adding a monotonic `seq` there is the durable fix —
worth doing in the same edit.

---

## MONGOSCHEMA-8 — real transcript records contain **dotted field names** (filenames used as object keys); they store but are not addressable by dotted path

**file:line**: `touch-full-recon-plan.md:618-627` (R-24 store),
`touch-full-recon-plan.md:641-660` (R-26 ingest)
**severity**: major

**Scenario.** Scanning the corpus for keys containing `.` or starting with `$`
found **76 occurrences**, all filename-shaped map keys inside
`file-history-snapshot` / patch structures: `research.workflow.js` ×11,
`inception.md` ×17, `subagent-uml.html` ×13, `CLAUDE.md` ×10, `.gitignore` ×9,
`driver-context.md` ×5, `SKILL.md` ×4, `research-report.html` ×4,
`touch-monitor-spawn-plan.md` ×3. MongoDB ≥ 5.0 *stores* these, so a naive
"insert the record verbatim" works and looks fine — until:

- `$set: {"snapshot.CLAUDE.md.x": 1}` means "field `md` inside field `CLAUDE`
  inside `snapshot`" — a silently wrong nested write.
- `$lookup.localField`, `$unwind`, projections and index keys cannot address
  them at all.
- `.gitignore` as a key is fine, but any future record with a `$`-prefixed key
  is rejected by many aggregation stages.

The maximum JSON nesting depth measured is 7 (BSON's limit is 100), so depth is
not a risk — only key shape.

**Recommendation.** Wrap variable-key subtrees rather than storing them raw:
in `ingest.py`, any object whose keys are not from a known fixed set (today:
`snapshot`, `structuredPatch`/`backup` payloads, arbitrary tool `input`
objects) is stored as `{"_raw": "<json string>"}` or as
`[{"k": "<name>", "v": …}]`. Records remain byte-reconstructible (assert it in
the round-trip test), and every field Touch actually queries stays addressable.
Add a store-level validator that rejects a document containing a dotted or
`$`-prefixed key outside a declared `_raw` wrapper, with a test using the real
`file-history-snapshot` records as the fixture.

---

## MONGOSCHEMA-9 — the "same agentId in two session dirs" case is **not** two copies: they are disjoint continuations; per-file token rollups would under-report and `_id=agentId` must union, not overwrite

**file:line**: `touch-full-recon-plan.md:311-320` (R-03: "both `a2fc883c…`
copies"), `touch-full-recon-plan.md:320-323` ("same agentId in two dirs"),
`touch-full-recon-plan.md:681-690` (R-28)
**severity**: major

**Scenario.** `agent-a2fc883c96ff7b837.jsonl` exists under both
`dd469822-…/subagents/workflows/wf_829e6f58-b2f/` (552 313 B, 223 records) and
`e423cd3c-…/subagents/workflows/wf_829e6f58-b2f/` (12 598 B, 2 records).
Measured: **zero uuid overlap, zero `message.id` overlap**, different
`sessionId` per file (each file's records carry the enclosing session's id),
first timestamps 17 minutes apart (`02:59:29.846Z` vs `03:16:39.958Z`). They
are two *segments* of one agent's life, split by a `/clear`-class session
rotation mid-run — not duplicates. It is the only such case on disk (1 agentId
of 31), and it is the fixture GD-18 freezes.

Consequences the plan does not currently state:

- `_id = agentId` is right, but the write must be `$addToSet` over
  `sessions[]` and `files[]` and `$max`/`$min` over first/last activity —
  a `$set` of a per-file document overwrites the 223-record segment with the
  2-record one depending on scan order.
- Per-agent token rollups must come from `usage` grouped by `agentId`, never
  from summing a per-file total.
- The plan's own wording ("both copies") is what invites the wrong
  implementation; correct it in R-03/R-26 while amending.

**Recommendation.** `agents` document:

```
{_id:"a2fc883c96ff7b837", agentType, model, spawnDepth,
 description?, toolUseId?,            // Agent-tool spawns only
 runId, sessions:[…], files:[…],
 firstTs, lastTs, records, source:"harness"}
```

written with `$addToSet {sessions:{$each:…}, files:{$each:…}}`,
`$min {firstTs}`, `$max {lastTs, records}`, `$setOnInsert {agentType, …}`.
Test: ingesting the two `a2fc883c` files **in either order** yields one
document with two sessions, two files, `firstTs == 02:59:29.846Z`, and a token
rollup equal to the union — assert on both orders.

---

## MONGOSCHEMA-10 — a default `docker run -p 27017:27017 mongo:7` publishes an **unauthenticated** database on `0.0.0.0`, which violates GD-13's posture in a sandbox whose ports get published to the host

**file:line**: `touch-full-recon-plan.md:184-194` (GD-13),
`inception.md:189-194` (0.0.0.0 bind compensated by token + Origin allowlist)
**severity**: major

**Scenario.** My probe container is `0.0.0.0:27017->27017/tcp` with no
credentials; `list_database_names()` succeeds unauthenticated from any process.
Transcripts hold unredacted secrets (inception.md:190) — GD-13 exists precisely
because of that, and it currently governs only the HTTP surface. Adding Mongo
adds a **second, wholly ungoverned network surface** with no token, no Origin
check and no route table. A user who runs `sbx ports … --publish 27017:27017`
(or any sibling container on the docker network) reads every transcript Touch
ever mirrored. A sibling researcher's container in this very run is bound
correctly (`127.0.0.1:27099`) — the two side by side show how easy the wrong
default is.

**Recommendation.** Amend GD-13 with a Mongo clause, and put the exact command
in the docs (R-33) rather than leaving it to the reader:

```
docker run -d --name touch-mongo \
  -p 127.0.0.1:27017:27017 \
  -e MONGO_INITDB_ROOT_USERNAME=touch \
  -e MONGO_INITDB_ROOT_PASSWORD="$(openssl rand -hex 24)" \
  -v touch-mongo-data:/data/db mongo:7
```

plus: credentials live in `.touch/mongo.json` mode 0600 (same treatment as
`server.json`'s token, R-34), `.touch/` is already gitignored by R-01,
the connection URI is **never** echoed into `events.jsonl`, `/health` or any
API response, and the port is *not* published by the `sbx ports` instructions.
If Arm A (stdlib client, MONGOSCHEMA-5) is chosen, SCRAM-SHA-256 becomes a
requirement of that module, not an optional extra — call that out, because it
is the single largest chunk of the stdlib client's cost.

---

## MONGOSCHEMA-11 — the exact amendment set (this is the deliverable G2 asks for)

**file:line**: `touch-monitor-spawn-plan.md:24-28` (G2),
`touch-monitor-spawn-plan.md:222-229` (discards)
**severity**: major

G2 says adopting Mongo "is an explicit D5/D8 amendment, not an implementation
choice", and discards per-session collections as an anti-pattern. Neither the
G2 text nor `touch-full-recon-plan.md` enumerates what an adoption actually
touches. Concretely, it is:

| Target | Disposition |
|---|---|
| **D5** (`.touch/` state layout) | **AMENDED, additively** — `.touch/` remains the system of record; add `.touch/mongo.json` (0600 URI/creds) and `.touch/mongo-cursor.json` (per-stream mirror high-water marks). No existing `.touch/` file changes meaning. |
| **D8** (stdlib-only runtime) | **AMENDED** — one of the two arms in MONGOSCHEMA-5, stated by name. If Arm B, R-22's stdlib-only guard gets a named exception in the same item. |
| **G2** (`touch-monitor-spawn-plan.md:24-28`) | **SUPERSEDED** for the storage clause; **STANDS** for "one collection per entity type, never per session" — carried forward verbatim into the new GD as a hard invariant. |
| **GD-7** (node identity) | **STANDS**, materialized as `_id` strings per MONGOSCHEMA-6. |
| **GD-11** (touch-events-v2 shapes) | **AMENDED** — (a) the `{uuid}` ref member covers only the four uuid-bearing types; positional key for the rest (MONGOSCHEMA-1); (b) token upserts are `$max`, and `inception.md:78`'s "copied" premise is corrected (MONGOSCHEMA-2); (c) all `_id`s are strings (MONGOSCHEMA-6); (d) the ref union's open tail is preserved — unknown ref shapes are stored in a `ref` sub-document as-is and are *never* promoted to `_id`. |
| **GD-13** (security) | **EXTENDED** — Mongo clause per MONGOSCHEMA-10. |
| **GD-15** (module layout) | **EXTENDED** — new files, one owner each: `aggregator/mongo_wire.py` (Arm A only), `aggregator/mongo_store.py` (schema + upsert ops + index bootstrap), `aggregator/mirror.py` (queue, circuit breaker, cursor persistence). `store.py` keeps sole ownership of `.touch/`; `mongo_store.py` never touches files; `mirror.py` is the only module importing both. `tests/test_mongo_store.py`, `tests/test_mirror.py`, and (Arm A) `tests/test_mongo_wire.py`. |
| **GD-16** (git policy) | **EXTENDED** — `.touch/mongo.json` gitignored (already covered by R-01's `.touch/`); no Mongo data directory inside the repo (named docker volume, not a bind mount under the repo). |
| **GD-20** (copy-verbatim / do-not-inherit) | **EXTENDED** — add to *do not inherit*: "the database is never on the liveness path" (MONGOSCHEMA-3) and "no blocking DB I/O in the poll loop" (MONGOSCHEMA-4). |
| **R-24** (`store.py`) | **UNCHANGED** — deliberately. Mongo does not replace it. Say so explicitly so the divider does not hand `store.py` to a Mongo implementer. |
| **R-26 / R-27** (ingest / legacy) | **UNCHANGED in behaviour**; they emit the same reduction, which `mirror.py` consumes. R-03's "both copies" wording corrected per MONGOSCHEMA-9. |
| **R-30 / R-31** (server, read API) | **EXTENDED** — `/health` gains the `mongo` block (MONGOSCHEMA-4); optionally one new read route `/api/query?…` served from Mongo with a documented fallback to the file store when Mongo is down. Existing routes keep reading the in-memory reduction so the UI never depends on Mongo. |
| **Phase placement** | New items belong **after R-26/R-27** and **before or parallel to R-32** — they consume the reduction and nothing consumes them. They must not enter phase 0/1. |

New items to add (ids continue the R-series):

- **R-38 — Mongo deployment + security baseline**: the compose/run recipe of
  MONGOSCHEMA-10, `.touch/mongo.json`, docs. Test: a script asserts the
  published bind is loopback and that an unauthenticated connect fails.
- **R-39 — `mongo_store.py`**: collections, string `_id` grammar, `$max`/
  `$addToSet` upsert ops, index bootstrap (MONGOSCHEMA-14), dotted-key
  validator (MONGOSCHEMA-8). Test: full-corpus double-ingest converges
  (MONGOSCHEMA-16).
- **R-40 — `mirror.py`**: queue + circuit breaker + per-stream cursor
  persistence + `/health` block. Test: dead-port test of MONGOSCHEMA-4.
- **R-41 — `agent_state` custom-state collection**: MONGOSCHEMA-19.
- **R-42 — (Arm A only) `mongo_wire.py`**: stdlib BSON/OP_MSG + SCRAM.
  Test: RFC-style vectors + a live round-trip guarded by a skip when no
  mongod is reachable.

---

## MONGOSCHEMA-12 — dual-sink vs Mongo-only: measured storage cost makes dual-sink nearly free, and it is the only option that keeps "Mongo down" a non-event

**file:line**: `inception.md:213-215` (D5), `touch-full-recon-plan.md:230-236`
(GD-16 growth policy)
**severity**: major

**Scenario.** Measured on the full corpus mirrored into Mongo:

| | bytes |
|---|---|
| raw `~/.claude/projects` JSONL | 15 858 773 |
| `records` collection, logical (`collstats.size`) | 15 298 030 |
| `records` collection, on disk (`storageSize`, snappy) | 8 400 896 |
| `records` total index size | 192 512 |
| `usage` (897 docs) size / storage / indexes | 150 332 / 61 440 / 40 960 |

So the mirror costs ≈ **0.53×** the raw text on disk. Ingest throughput: 3 793
upserts + 2 026 usage upserts + 31 agent upserts in **0.40 s** cold, **0.16 s**
on the idempotent second pass — three orders of magnitude inside the 250 ms
budget even without the async writer.

Mongo-only would mean: no store when the container is down; no bootstrap before
the container exists; a `docker` dependency in the test suite (the current
suite is `python3 test_x.py`, no runner, no services — CLAUDE.md's "Commands");
and the loss of the append-only file that GD-16 tracks in git as history.

**Recommendation.** **Dual sink, with an explicit asymmetry**: `.touch/`
JSONL is the *system of record*; Mongo is a **derived projection, fully
rebuildable from files**. Consequences to write down: (a) a rebuild command
(`python3 -m aggregator.mirror --rebuild`) exists and is tested; (b) no data
lives only in Mongo *except* the custom agent-state of MONGOSCHEMA-19, which
therefore needs its own file-backed journal; (c) all Mongo tests skip cleanly
when no mongod is reachable, so `tests/run_all.sh` stays green on a bare
checkout — assert that skip behaviour in the test itself.

---

## MONGOSCHEMA-13 — the mirror must survive the CLI's retention sweep, which means **no TTL index anywhere** and a `sourcePresent` flag rather than deletion

**file:line**: `inception.md:115-119` ("the CLI's retention sweep unlinks
transcripts and `rm -rf`s whole subagent trees — Touch must own its history"),
`touch-full-recon-plan.md:216-220` (GD-14 archive label)
**severity**: major

**Scenario.** The single strongest argument for Mongo here is that
`~/.claude` is on a deletion clock (GD-18 freezes fixtures for exactly this
reason). That value is destroyed by two easy mistakes:

1. A TTL index (`expireAfterSeconds`) on a date field — the idiomatic Mongo
   retention tool — silently deletes precisely the history Touch exists to
   preserve, asynchronously, with no event.
2. A reconciliation pass that deletes documents whose source file no longer
   exists (a natural way to "keep the mirror in sync") turns the sweep into
   data loss with extra steps.

**Recommendation.** Write both as invariants of the amendment:

- **No TTL index on any Touch collection, ever.** Add a static test that
  asserts no index in the schema definition carries `expireAfterSeconds`.
- Mirror writes are **insert/upsert only**. The single legal delete is the
  scoped `deleteMany({sessionId})` that precedes a *rotation-triggered* full
  re-ingest of `stream_meta` (MONGOSCHEMA-1) — and it is immediately followed
  by the re-insert in the same code path.
- Source liveness is a *field*: `sources[].present:false, lastSeenTs` set by a
  stat pass. This is the same three-state derived-archive-label rule GD-14
  already mandates for `wf_dir` — reuse the vocabulary rather than inventing a
  second one.
- Mirror-before-sweep is then automatic: the tailer already ingests
  continuously, so the only gap is a session that existed before Touch ran.
  Give R-40 a `--backfill` mode that walks `~/.claude/projects/**` once and
  upserts everything; it is idempotent by construction (MONGOSCHEMA-16).

---

## MONGOSCHEMA-14 — concrete collection + index list (the schema deliverable)

**file:line**: `touch-full-recon-plan.md:154-173` (GD-11),
`touch-monitor-spawn-plan.md:224-226` (G2 per-session anti-pattern)
**severity**: minor (design artifact, not a defect)

**One collection per entity type; never per session** (G2 carried forward — a
per-session collection makes cross-session queries impossible, duplicates every
index, and hits the WiredTiger file-per-collection ceiling after a few hundred
sessions).

| collection | `_id` | key fields | indexes |
|---|---|---|---|
| `sessions` | `sess:<pid>-<procStart>` / `sess:sid:<sessionId>` | `pid, procStart, sessionIds[], cwd, class(owned\|cooperating\|observed), liveness, firstTs, lastTs` | `{sessionIds:1}`, `{class:1, lastTs:-1}` |
| `records` | `<uuid>` | `sessionId, agentId?, type, ts, parentUuid, message._raw…` | `{sessionId:1, ts:1}`, `{agentId:1, ts:1}`, `{parentUuid:1}`, `{"toolUseId":1}` sparse |
| `stream_meta` | `<sessionId>#<lineNo>` | `sessionId, lineNo, type` | `{sessionId:1, lineNo:1}` |
| `agents` | `<agentId>` (17-hex validated) | `runId?, toolUseId?, agentType, model, spawnDepth, sessions[], files[], firstTs, lastTs, name?, parent?, root?, plan?, stage?, attempt?, markerSource` | `{runId:1}`, `{toolUseId:1}` sparse, `{parent:1}`, `{root:1, name:1}` |
| `run_nodes` | `<runId>\|<key>\|<ordinal>` | `runId, key, ordinal, journalLine, agentId, state, result_kind, result` | `{runId:1, journalLine:1}`, `{agentId:1}` |
| `runs` | `<runId>` | `snapshotPath?, status, phases[], startTime, durationMs, taskId, workflowName, harnessTotals{}` | `{status:1, startTime:-1}` |
| `usage` | `<message.id>` | `in,out,cached,cache_write, agentId, sessionId, runId` | `{agentId:1}`, `{sessionId:1}`, `{runId:1}` |
| `events` | `<streamId>#<seq>` | `stream, seq, ts, source, kind, ref{}, data{}` | `{stream:1, seq:1}` **unique**, `{kind:1, ts:-1}`, `{"ref.agentId":1}` sparse |
| `legacy_events` | `legacy:<task>:<lineNo>` | `task, lineNo, ts, plan, stage, state, detail, title` | `{task:1, lineNo:1}`, `{task:1, plan:1, stage:1}` |
| `agent_state` | `<agentId>:<stateKey>` | see MONGOSCHEMA-19 | `{agentId:1, updatedTs:-1}`, `{stateKey:1}` |
| `cursors` | `<streamId>` | `offset, stDev, stIno, size, lastSeq, updatedTs` | (`_id` only) |

Notes: `harnessTotals` holds `totalTokens`/`totalToolCalls` **display-only**
(GD-11 forbids substituting them — keep them in a namespaced sub-document so a
`$sum` over the wrong field is impossible). Every `ts` is stored as a BSON
`Date` **and** the original `…Z` string in `tsRaw`, so GD-11's "writer emits
exactly one format" survives a round trip through a driver that normalizes
dates.

---

## MONGOSCHEMA-15 — DBRef works with `$lookup` on Mongo 7 (contrary to folklore) but is still the wrong choice for the ref union

**file:line**: `touch-full-recon-plan.md:156-160` (ref union)
**severity**: minor

**Scenario.** I tested the assumption directly rather than repeating received
wisdom: a document `{ref: DBRef("b","agent-a1")}` **does** join via
`$lookup: {localField: "ref.$id", foreignField: "_id", …}` on 7.0.39 — the
`$`-prefixed path resolves and returns the joined document. So "DBRef can't be
`$lookup`ed" is false here and should not be used as the reason to reject it.

The real reasons to reject it: (a) DBRef decodes to a driver-specific object
(`bson.DBRef` in pymongo, a plain dict with `$ref`/`$id` keys in a stdlib
client) — the schema would behave differently under MONGOSCHEMA-5's two arms;
(b) it re-introduces `$`-prefixed keys, which MONGOSCHEMA-8's validator exists
to ban; (c) it buys nothing — Mongo does not dereference server-side either
way.

**Recommendation.** Materialize the GD-11 ref union **flat and denormalized**:
store the discriminant plus flat scalar fields —
`ref: {kind:"agent", agentId:"a2fc…"} | {kind:"record", uuid:"…"} |
{kind:"node", runId, key, ordinal} | {kind:"session", pid, procStart} |
{kind:"tool", toolUseId}` — plus a precomputed `refId` string equal to the
target collection's `_id`, so every join is `localField:"refId"`. Unknown ref
shapes (GD-11's open tail) are stored under `ref` verbatim with
`kind:"unknown"` and **no** `refId`, satisfying "retained and passed through"
without ever becoming a dangling join.

---

## MONGOSCHEMA-16 — "re-ingest converges byte-identical" is achievable and was verified; make it the schema's acceptance test

**file:line**: `touch-full-recon-plan.md:624-627` (R-24 tests)
**severity**: minor

**Scenario.** I ran the full corpus through the upsert path twice and hashed
every document of `records`, `usage`, `agents` sorted by `_id`: identical
fingerprint on both passes, with counts stable (3 651 / 897 / 31 in that run).
Pass 2 took 0.16 s vs 0.40 s. This is only true because of the four decisions
above — string `_id`s, `$max` for counters, `$addToSet` for multi-valued agent
fields, positional keys for uuid-less records. It breaks under any of: `$set`
on usage, sub-document `_id`, content-hash `_id`.

**Recommendation.** Ship it as the definitive test for R-39:

```
ingest(corpus); f1 = fingerprint()
ingest(corpus, shuffled)          # adversarial order
ingest(corpus[::-1])              # reverse file order
assert fingerprint() == f1
assert counts == expected_counts  # catches silent collapse (MONGOSCHEMA-1)
```

Both the count assertion and the shuffled pass are load-bearing: the
fingerprint alone would have passed the content-hash design that lost 142
records.

---

## MONGOSCHEMA-17 — 16 MB document limit is not a risk for records (max observed line 872 KB), but tool-result spills must stay out of the document

**file:line**: `inception.md:76-80`, `touch-full-recon-plan.md:646-651` (R-26
persisted-output detection)
**severity**: minor

**Scenario.** Largest single transcript line measured across the corpus:
**872 577 bytes** (5 % of the 16 MB BSON limit; `maxBsonObjectSize` confirmed
16 777 216 from `hello`). Headroom is real but not unlimited: the CLI already
spills large tool results to `tool-results/*.txt` (4 files present, 30–46 KB)
with a `<persisted-output>` pointer record. If a future ingest "helpfully"
inlines the spill file into the record document, a single 20 MB build log
becomes an unrecoverable `BSONObjectTooLarge` that fails the whole batch —
and with `ordered=False` it fails silently as one entry in `writeErrors` that
nobody reads.

**Recommendation.** Keep the pointer semantics: store
`toolResult: {persisted:true, path:"…", bytes:N, sha256:"…"}` and serve the
body through the existing containment-checked `/file`-style route (R-26/R-30
already own the realpath containment). Add a hard guard in `mongo_store.py`:
refuse any document over 8 MB, log it, and write a stub with
`oversize:true, bytes:N` — plus a test that **`writeErrors` is always
inspected** and surfaced on `/health`, since `ordered=False` never raises for
partial failures.

---

## MONGOSCHEMA-18 — journal `(type,key)` repeats are real, so `ordinal` is required; but an in-memory ordinal counter is not restart-safe — derive it from the journal line number

**file:line**: `touch-full-recon-plan.md:109-116` (GD-7),
`inception.md:102-109`, `touch-full-recon-plan.md:196-200` (GD-14 ordinal rule)
**severity**: minor

**Scenario.** Across the four journals on disk, `wf_455b348c-e17` has **3
distinct `key`s each with 2 `started` entries** (the stall-watchdog respawn
case GD-7 predicts); the other three journals have none. So `(runId, key)`
genuinely collides and `ordinal` is necessary. But GD-14 defines ordinal as "a
per-(plan,stage) counter incremented on each `state:"running"` spawn" — an
in-memory counter. In Mongo that is a correctness problem the file store did
not have: restart the aggregator mid-run and the counter restarts at 0, so the
next `started` upserts onto `runId|key|0`, **overwriting** the first node.
Recovering the counter by `count_documents({runId, key})` is racy and wrong
after a partial ingest.

**Recommendation.** Derive ordinal from position, not from a counter:
`ordinal = (index of this line among lines in this journal with the same
(type,key))`, computed from `journalLine`, which the tailer already knows.
Store `journalLine` in the document. `journal.jsonl` is append-only, so the
value is stable across restarts and re-ingests, and the `_id` is a pure
function of the file — the same property that makes MONGOSCHEMA-16 hold.
Keep GD-14's counter definition only for the *legacy* `events.jsonl` arm,
where there is no journal, and note the divergence explicitly so the two
ordinals are never assumed interchangeable.

---

## MONGOSCHEMA-19 — the custom agent-state collection (user requirement 2): design it as an append-only journal with a derived head, not as a mutable document

**file:line**: user turn at
`~/.claude/projects/-home-laniakea-Projects-touch/292fc08c-923d-4ab4-8ff2-a9572417dbc8.jsonl:181`
("custom saving implementation, where we can create custom state into another
mongodb collection and reference that to mapped session agents info"),
`touch-full-recon-plan.md:681-690` (R-28)
**severity**: major

**Scenario.** This is the one collection whose data does **not** exist in
`~/.claude` and is therefore *not* rebuildable from files (MONGOSCHEMA-12's
exception). Two design traps:

1. If it is a mutable document (`{_id: agentId, state: {...}}` updated in
   place), a crash between read and write loses state and there is no audit —
   inconsistent with D13/GD-4's honesty rules, which require every intent and
   transition to be *observable*, and with the control plane's
   `requested/sent/confirmed` audit (R-34).
2. If it references agents by anything other than the harness-derived 17-hex
   `agentId` / `(runId,key,ordinal)`, it dangles the moment a label changes —
   GD-7's whole point is that markers label nodes and never create them.

**Recommendation.**

- `agent_state_log` — append-only, `_id = "<agentId>:<seq>"`,
  fields `{agentId, refId, seq, ts, author("touch"|"user"|"skill"),
  stateKey, value, prevSeq}`. `seq` is per-agent and allocated by
  `findOneAndUpdate` on a `counters` document (atomic) or, better, mirrored
  from the same `.touch/agent-state.jsonl` line number so it survives a Mongo
  wipe.
- `agent_state` — the derived head, `_id = "<agentId>:<stateKey>"`, upserted
  with `$max: {seq}` guarded by a `{seq: {$lt: newSeq}}` filter so a
  late-arriving older write can never clobber a newer one (same ordering
  hazard as MONGOSCHEMA-2, same fix).
- References are the **flat `refId`** of MONGOSCHEMA-15 pointing at
  `agents._id` (or `run_nodes._id` for Workflow nodes), never a DBRef, never a
  name. Validate on write: reject a `refId` that does not match
  `^[0-9a-f]{17}$` or the `runId|key|ordinal` grammar, and reject writes for an
  agent with no document in `agents` (a dangling custom state is worse than a
  rejected one — the UI would render a card for a node that does not exist).
- Because it is not rebuildable, it gets the *file-backed journal* of
  MONGOSCHEMA-12(b): the aggregator writes `.touch/agent-state.jsonl` first,
  then mirrors. That makes Mongo optional even for this feature and keeps
  "Mongo down" a degradation rather than a data-loss event.
- Test: write 3 states out of order for one agent; assert the head equals the
  highest `seq`; assert the log has 3 entries; assert a `refId` for an unknown
  agent is rejected; assert a Mongo wipe + replay of `.touch/agent-state.jsonl`
  reproduces both collections exactly.

---

## MONGOSCHEMA-20 — concurrent agents share one mongod and one database namespace; the plan needs a namespace rule before two Touch instances (or two tests) collide

**file:line**: `touch-full-recon-plan.md:180-182` (GD-12 "never fall back to
another task/session/stream")
**severity**: nit

**Scenario.** While probing I found a `touchprobe_liveflow` database inside
*my* container that I did not create — a sibling researcher in this same run is
using the same daemon — and a second container `cs_probe` on
`127.0.0.1:27099`. Harmless here, but it is the same wrong-target hazard GD-12
names for tasks and streams: two Touch instances (two repos, or a dev instance
and a test run) pointed at one mongod will silently share collections, and the
test suite will delete the developer's data.

**Recommendation.** Database name is explicit and derived, never a constant:
`touch_<sha1(repo-realpath)[:8]>`, overridable by `TOUCH_MONGO_DB`. Tests use
`touch_test_<pid>` and drop it in a `finally`. Never `db.drop_database()` on a
name the process did not construct. State it alongside GD-12, since it is the
same invariant.

---

## Summary of dispositions this perspective recommends

**Adopt Mongo, as a derived mirror only**, with: string `_id`s;
`$max`/`$addToSet` upserts; a separate positional-key collection for the 332
uuid-less records; no TTL and no source-driven deletes; a loopback-bound,
authenticated deployment; async writer + circuit breaker; and `.touch/` JSONL
kept as the system of record so the live monitoring path (requirement 3) never
depends on a database. Custom agent state (requirement 2) gets an append-only
log + derived head, file-journaled first. D8 can be preserved via a vendored
stdlib wire client (proven) or amended for pymongo — an explicit, costed choice
rather than a silent concession.
