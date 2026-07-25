# touch-mongo-live — MongoDB adoption amendment plan

Synthesized 2026-07-25 from five research reports under
`.claude/local-orchestrators/touch-mongo-live/findings/` (convo, sessionjsonl,
mongoschema, customstate, liveflow — cited by finding id). Findings stay in
those files; this plan references them by id + path:

- `findings/research-convo-attempt-1.md` (CONVO-1…16)
- `findings/research-sessionjsonl-attempt-1.md` (SESSIONJSONL-1…16)
- `findings/research-mongoschema-attempt-1.md` (MONGOSCHEMA-1…20)
- `findings/research-customstate-attempt-1.md` (CUSTOMSTATE-1…19)
- `findings/research-liveflow-attempt-1.md` (LIVEFLOW-1…19)

## 0. Authority, scope, and what this document is

**This is an AMENDMENT to `touch-full-recon-plan.md`** (the normative plan,
GD-1…GD-20, R-01…R-37). It does not re-plan anything that plan covers. Every
GD/R/D it touches gets an explicit disposition in §2; everything not named
there **stands unchanged**. New global decisions continue the numbering at
GD-21; new items continue at R-38. Hand `implement-plan` this file **plus**
`touch-full-recon-plan.md` context — this file alone is not a complete plan of
the repo, it is the Mongo layer on top of it.

**Numbering collision note:** `research-mongoschema-attempt-1.md` proposed
items named R-38…R-42 inside its findings. Those were proposals; this plan's
numbering supersedes them. Mapping: their R-38→**R-42**, R-39→**R-44**,
R-40→**R-45**, R-41→**R-52**, R-42→rejected (Arm A, see Discards).

### 0.1 Requirements provenance (the record of what the user actually asked)

The binding user statements span TWO sessions (CONVO-1). `implement-plan` must
never need to re-read a transcript; the six decisive uuids and their verbatim
asks are:

Session `e423cd3c-f859-45af-9afd-0d6bdec9b4ac` (2026-07-25 03:48–03:53):
- `081b28a7-aee9-43dc-935d-1586407f232e` (:145) — "how to add hook to store all
  session infos in mongodb? separate collections for separate session datas."
- `1ec9c5c1-3921-443e-82c2-f15e372d237a` (:151) — same ask repeated ~20 s later.
- `abe6607f-c8a7-4d65-842b-5dcd241ff4cb` (:157) — "how to save custom state for
  subanget?"
- `70eb3975-8a48-4710-a4de-6d2b20fc513e` (:162) — "yep, Touch's own store, we
  can store infromation in which line subagent is located on original session
  file, and map that into our custom jsonl or mongodb objects."

Session `292fc08c-923d-4ab4-8ff2-a9572417dbc8` (this session):
- `90cd92b1-0d70-4ecb-ad7b-b3341ba07326` (:181) — does the plan cover Mongo
  mirroring + "custom state into **another mongodb colection**" referencing
  mapped session agents.
- `761331dc-c98d-4be9-ba62-2ebd874c6a83` (:195) — "can we create deterministic
  code for saving session jsonl infos into db?"
- `b003ac9f-6b8d-4b36-8981-87d7df537f80` (:204) — "can we read session info to
  correctly show monitoring status about agents and geterministic loops?"
- `5e05ab4e-10c6-47b8-8512-29cacbab6c9c` (:209) — the /execute-research mandate.

Two mutually inconsistent Mongo schemas were shown to the user in those answers
(8 collections at e423c:154 vs 5 at 292f:197 — CONVO-2). **§1/GD-24 is the ONE
normative schema**; both prior sketches are superseded by it, with the deltas
dispositioned in §0.3.

### 0.2 The D8 anchor repair (read this before citing "D8")

`touch-full-recon-plan.md:27` labels "D8" as the journal-`result` sub-clause
and marks it SUPERSEDED, while D8 in `touch-aggregator-plan.md:217` is the
Stack decision (stdlib-only). Read literally, the normative plan already
superseded the constraint this amendment lifts (CONVO-6). Disposition, stated
unambiguously:

- **D8.1 (Stack / stdlib-only-at-runtime)** — **AMENDED by this document**
  (see GD-21). It was never superseded.
- **D8.2 (journal `result` opaque, never parsed)** — **remains SUPERSEDED**
  per touch-full-recon GD-11. Unchanged here.

R-38 fixes the label at the source. Similarly: the Mongo discard and the
stdlib pin survive only inside the *superseded*
`touch-monitor-spawn-plan.md` (G2, discards register) — this document quotes
them as historical provenance and re-legislates what must survive (GD-21,
GD-24) so the amendment has a real anchor (CONVO-7).

### 0.3 User-facing dispositions (in the user's own words)

- **"separate collections for separate session datas"** (asked twice,
  e423c:145/:151) — **DECLINED, with this machine's numbers** (CONVO-8,
  CUSTOMSTATE-17): 6 transcripts + 7 sessionIds in `history.jsonl` (one with
  no transcript) would already mean 7+ collections for one project; the
  sidebar's "all sessions, newest first" becomes an N-collection scan, and
  every collection duplicates its indexes. What the user actually wants —
  per-session isolation — is delivered as an indexed `sessionId`/`sessionKey`
  field on every document plus per-session filtered queries. G2's
  "one collection per entity type, never per session" carries forward verbatim
  as a hard invariant of GD-24, and extends to custom state.
- **"custom state into another mongodb colection"** (292f:181, :209) —
  **ADOPTED**. The e423c:154/:159 answer sketched custom state as a
  subdocument on `agents` — that sketch is **overturned** (CONVO-3): a mutable
  subdocument destroys transition history (D4/D7 audit), is not rebuildable,
  and contradicts the user's stated shape. Custom state is its own collection
  pair (`custom_state_events` + `custom_state`, R-52) referencing mirrored
  docs by `refId`.
- **"which line subagent is located on original session file"** (e423c:162)
  — **ADOPTED as agreed at e423c:164** (CONVO-9): identity is
  `spawn.recordUuid` + `toolUseId`; the line/offset is a perishable `fileHint`
  cache validated against `(st_dev, st_ino, size)`, invalidated (kept for
  diagnostics) on mismatch. Offset-as-cursor: fine. Offset-as-identity: never
  (R-48).
- **"hook to store all session infos"** (e423c:145) — the ingest is a file
  tailer, not a hook: no supported push hook exists in CLI 2.1.220
  (SESSIONJSONL-15); hooks remain gated on the R-04 probe (GD-19 stands). The
  internal `addMirror`/`remoteIngressUrl` machinery is noted for a future CLI
  version and must not be relied on or monkey-patched.

---

## 1. New global decisions (GD-21 … GD-30)

Decided once; downstream work must not diverge.

### GD-21 — Runtime dependency policy (the D8.1 amendment)
[CONVO-6, CONVO-7, MONGOSCHEMA-5, CUSTOMSTATE-18, LIVEFLOW-19,
SESSIONJSONL cross-cutting note]

D8.1 is amended from "stdlib-only at runtime" to:

> **Stdlib-only on the ingest and serve critical path.** `pymongo` (pinned
> `==4.17.0`, with `dnspython`) is the ONE permitted third-party runtime
> dependency, importable **only** from `aggregator/mongo_store.py` and
> `aggregator/mirror.py` (lazy import). Its absence degrades the mirror to
> `mirror: "absent"` in `/health` — it never fails startup, never breaks an
> agent, never blocks a test. Every module outside those two stays importable
> with no third-party packages installed; all Mongo tests skip cleanly when no
> mongod is reachable. R-22's stdlib-only static guard gains this single named
> exception **in the same edit** that introduces the import (else the suite
> goes red).

Chosen: **Arm B (pymongo)** over Arm A (a vendored ~250-line stdlib
BSON/OP_MSG client — proven feasible by MONGOSCHEMA-5's working ~90-line
probe). Reason: GD-27 makes SCRAM-SHA-256 auth mandatory, and auth/TLS wire
code is security-sensitive surface Touch should not own; pymongo 4.17.0
installs through the proxy today (verified twice: MONGOSCHEMA env probe,
CUSTOMSTATE-19). Arm A is recorded as the viable fallback if the dependency
policy ever hardens — do not re-research it. Driver mode: **pymongo's async
API (`AsyncMongoClient`)** inside the one asyncio process (Motor is EOL —
LIVEFLOW-9); if the async API is unusable, a sync client is legal only behind
`asyncio.to_thread`. Client options are fixed: `serverSelectionTimeoutMS=500,
connectTimeoutMS=500, socketTimeoutMS=2000, retryWrites=True`
(MONGOSCHEMA-4: the 30 s default stalls the poll loop).

No second dependency may be added by analogy; this GD is the written rule
CONVO-7 found missing.

### GD-22 — Mongo is a derived, rebuildable mirror; the live path is memory-authoritative
[LIVEFLOW-1, MONGOSCHEMA-3, MONGOSCHEMA-12, CUSTOMSTATE-18, LIVEFLOW-9,
CONVO-13]

- The aggregator's **in-memory reduction is the single source for `/ws` and
  the read API**. `.touch/` JSONL (R-24) remains the system of record and the
  crash-durable WAL. **Mongo is a write-behind projection, fully rebuildable
  from files** (`--rebuild` exists and is tested), with ONE exception: custom
  state, which therefore gets its own file WAL first (R-52).
- **Change streams are NOT adopted.** Verified twice, independently:
  `db.watch()` on the standalone mongod this environment produces fails with
  `Location40573` (MONGOSCHEMA-3, LIVEFLOW-1). A standalone mongod is a
  supported deployment. A second reader process, if ever needed, polls the
  indexed `(stream, seq)` cursor (7 ms for a 999-doc tail of 20 k, measured).
  The capped-collection `TAILABLE_AWAIT` notify channel (works on standalone,
  verified) is recorded as the future push option — notification-only
  `{stream,seq,ts}`, never storage.
- **When Mongo is down or absent, the live view is fully functional.** Only
  history/backfill degrades; `/health` reports `mirror: degraded|down|absent`;
  recovery is watermark-driven replay from the file store.
- Measured basis: mirror costs 0.53× raw text on disk; full-corpus ingest
  0.40 s cold / 0.16 s idempotent second pass; `insertOne` 0.61 ms `w:1` —
  the poll interval is the latency budget, not the database (GD-30).

GD-20's do-not-inherit list is **extended** with: "the database is never on
the liveness path" and "no blocking DB I/O in the poll loop".

### GD-23 — Observations only; derived state lives apart and is droppable
[LIVEFLOW-5, LIVEFLOW-6, LIVEFLOW-12, SESSIONJSONL-8]

- The mirror collections persist **observations only** — never verdicts,
  never liveness, never plan badges. There is no `state` field in any mirror
  document; agent/session docs store
  `{firstActivityTs, lastActivityTs, resultSeen, resultTs}` and liveness is
  computed at read time from `now()` (three-state: running / finished /
  unknown ≥180 s idle — GD-10 extended to agent nodes).
- Every derived value lives in the `derived` collection; every document there
  carries `reducerVersion` + `derivedFromSeq`. On reducer-version mismatch,
  `derived` is **dropped and rebuilt by replay**, never migrated. Nothing
  outside the reducer writes it.
- There is exactly **one reducer, server-side** (R-54). `/api/*`, `/ws` and
  the page all serve/render its output; the frontend never re-derives
  (monitor.html's freeze-to-stale UI-local rule migrates into the reducer).
- **Hard precondition:** R-08 + R-09 + R-13 (normative plan, phase 1) must be
  green before the mirror writes any live stream. This run is *currently*
  fabricating a `research → failed` badge (LIVEFLOW-5, live specimen) and
  emitting five researchers under one 8-hex label (CONVO-10, live specimen);
  mirroring a stream that fabricates verdicts into a permanent store is the
  wrong order of work.

### GD-24 — Identity: one string-`_id` grammar, one canonical collection table
[MONGOSCHEMA-6 ≡ CUSTOMSTATE-4 ≡ LIVEFLOW-2 (field-order sensitivity, probed
three times independently); MONGOSCHEMA-14, CONVO-2, CONVO-4, CONVO-5,
SESSIONJSONL-1, MONGOSCHEMA-1, LIVEFLOW-3, LIVEFLOW-8, MONGOSCHEMA-7]

**A BSON sub-document is never used as `_id` or as an equality-match key.**
(`{s,n}` vs `{n,s}` insert as two distinct documents; probed and reproduced by
three perspectives.) Every `_id` is a **string** produced by one shared
`ref_key()` canonicalizer (R-43) with a fixed grammar; components are stored
as ordinary indexed fields alongside; structured refs are queried by dot
notation only. Two different key rules exist because transcripts are rewritten
while event logs are not (LIVEFLOW-8): **uuid/content keys for rewritable
sources, positional keys only for append-only sources.**

The ONE normative collection table (supersedes both schemas shown to the
user):

| collection | `_id` | key fields | indexes |
|---|---|---|---|
| `sessions` | `live:<pid>-<procStart>` \| `hist:<sessionId>` | `pid`(int), `procStart`(string), `sessionIds[]`, `cwd`, `slugs[]`, `class`, `firstTs`, `lastTs` | `{sessionIds:1}`, `{lastTs:-1}` |
| `records` | `<uuid>` | `sessionId, agentId?, type, ts, parentUuid, toolUseId?, lineNo, byteOffset, gen, retracted?` | `{sessionId:1,ts:1}`, `{agentId:1,ts:1}`, `{parentUuid:1}`, `{toolUseId:1}` sparse |
| `stream_meta` | `<sessionId>#<line:08d>` | `sessionId, lineNo, type, render:false for queue-operation` | `{sessionId:1,lineNo:1}` |
| `agents` | `<agentId>` (17-hex validated) | `agentType?, model?, spawnDepth?, description?, toolUseId?, runId?, sessions[], files[], fragments[], spawn{recordUuid,toolUseId,fileHint}, firstTs, lastTs` | `{runId:1}`, `{toolUseId:1}` sparse, `{root:1,name:1}` |
| `runs` | `<runId>` | `taskId, workflowName, transcriptDir, scriptPath, sessionIds[], status?, harnessTotals{}, startedAt, endedAt?` | `{startedAt:-1}` |
| `run_nodes` | `<runId>\|<key>\|<ordinal>` | `runId, key, ordinal(int), journalSeq, agentId, resultSeen, result, startedAt?, endedAt?` | `{runId:1,journalSeq:1}`, `{agentId:1}` |
| `usage` | `<message.id>` | `in,out,cached,cache_write, agentId?, sessionId, runId?` | `{agentId:1}`, `{sessionId:1}`, `{runId:1}` |
| `events` | `<stream>#<seq:012d>` | `stream, seq(int), ts, source, provenance, kind, ref{}, refId?, data{}` | `{stream:1,seq:1}` unique, `{kind:1,ts:-1}` |
| `legacy_events` | `legacy:<task>#<line:08d>` | `task, lineNo, ts, plan, stage, state, detail, title?, provenance` | `{task:1,lineNo:1}`, `{task:1,plan:1,stage:1}` |
| `custom_state_events` | `<stream>#<seq:012d>` | append-only; `kind, ref{}, refId?, sessionKey, seq, ts, author, data.custom{}` | `{refId:1,seq:1}`, `{kind:1,ts:-1}` |
| `custom_state` | `<refId>#<stateKey>` | derived head; `seq`-guarded, `derived:true, fromSeq` | `{refId:1}`, `{kind:1}` |
| `slots` | `slot:<sessionKey>\|<root>\|<name>\|<attempt>` | `sessionKey, root, name, parent, role, attempt, agentId?, taskId?, runNode?, boundBy?, resolution` | `{agentId:1}` unique sparse, `{sessionKey:1,root:1,name:1,attempt:1}` |
| `derived` | reducer-owned | `reducerVersion, derivedFromSeq, …` | reducer-owned |
| `writers` | `<stream>` | `holderPid, holderBoot, leaseExpiresAt` | (`_id` only) |
| `cursors` | `<streamId>` | `offset, stDev, stIno, size, lastSeq, gen, updatedTs` | (`_id` only) |

Decisions folded in, each previously contradictory between the two schemas
shown to the user (CONVO-2):

- **`run_nodes`** chosen over `nodes` (the later answer's name; "node" alone
  collides with UI vocabulary). **`runs` kept** (CONVO-12 makes it the
  taskId/stop join). **`events` kept** (it is the touch-events-v2 mirror the
  audit trail lives in). **`control` folded into** `custom_state_events`
  kinds `control_intent`/`control_ack` — not dropped.
- **Session key separator is `-`** (`<pid>-<procStart>`) — matches the
  normative plan's R-25/R-34 path form; e423c:154's `"622:10028"` was an
  illustration, as was its `sessionIds` example (nothing on disk proves those
  sessions shared a process — CONVO-4).
- `<task>` in `legacy:` ids is a user-chosen folder name: percent-escape
  `% # | :` in it (CUSTOMSTATE-4's escaping rule). Zero-padding makes
  lexicographic `_id` order equal numeric order so `_id`-range and
  `(stream,seq)` cursors agree and both IXSCAN (LIVEFLOW-3: dotted-`_id`
  queries are COLLSCAN; both indexed forms verified).
- BSON type pins via `$jsonSchema` `bsonType`: `pid` int, `procStart`
  **string** (clock-tick string from `/proc/<pid>/stat` f22), `ordinal` int,
  `seq` int, everything else string (CUSTOMSTATE-4: BSON is type-strict).
- `harnessTotals` (`totalTokens`/`totalToolCalls`/`agentCount`→`nodeCount`)
  is a namespaced display-only sub-document, never summed, never used as a
  count check (GD-11 extension; SESSIONJSONL-7).
- Refs are **flat + denormalized**: `ref{kind,…}` (fixed field order from
  `ref_key`) plus precomputed scalar `refId` = target `_id`; DBRef rejected
  (works with $lookup on Mongo 7 — folklore corrected — but decodes
  driver-specifically and reintroduces `$`-keys; MONGOSCHEMA-15). Unknown ref
  shapes: retained under `ref` with `kind:"unknown"`, no `refId`, excluded
  from joins (GD-11 open tail preserved).

### GD-25 — Idempotency algebra (what "deterministic persistence" means)
[MONGOSCHEMA-2 ≡ SESSIONJSONL-9 ≡ LIVEFLOW-4 (tokens); MONGOSCHEMA-9,
MONGOSCHEMA-16]

- **Numeric accumulation is `$max` per field, never `$set`, never `$inc`.**
  Empirical basis: `output_tokens` GROWS across split records of one
  `message.id` (571 of 901 corpus message.ids differ; first-wins under-reports
  2.8×; `$set` is write-order dependent; `$max` verified byte-identical across
  randomly shuffled passes). `inception.md:78`'s "usage is copied onto every
  split record" is **false** and gets corrected (R-38). `$inc` for token
  accounting is forbidden — re-ingest is mandatory after every
  `performRemoveByUuid`, and summed deltas double.
- **Multi-valued agent/run fields are `$addToSet`**, scalars-first/last are
  `$min`/`$max`, immutables are `$setOnInsert` (MONGOSCHEMA-9: the two
  `a2fc883c` files are disjoint continuations — `$set` overwrites the
  223-record segment with the 2-record one depending on scan order).
- **Deltas exist only on the WS wire**, computed from in-memory absolute
  state; never persisted, never replayed, never summed from storage
  (LIVEFLOW-4: 133 absolute usage docs vs 217 delta events for the same run —
  fewer AND idempotent).
- **The schema's acceptance test** (R-44, verified achievable by
  MONGOSCHEMA-16): ingest the frozen fixture corpus normally, shuffled, and
  reversed; fingerprint over all documents sorted by `_id` is identical on
  every pass AND counts equal expected counts (the count assertion catches
  silent collapse — a content-hash key silently lost 142 of 333 uuid-less
  records in the probe).

### GD-26 — Mirror durability: upsert-only, generation sweep, retraction, no TTL
[CUSTOMSTATE-5 ≡ MONGOSCHEMA-13 (no-delete) vs SESSIONJSONL-2 (tombstones) vs
LIVEFLOW-8 (superset) — reconciled here]

The transcript is not append-only (`performRemoveByUuid` truncates+rewrites;
`performCompactTranscript` whole-file rewrites — extracted from the 2.1.220
binary), and upsert never deletes. The reconciled rule set:

- **The harness mirror is insert/upsert-only.** No code path issues
  `deleteOne`/`deleteMany`/`drop`/`$unset` on a mirror collection, with
  exactly ONE named exception below. Enforced by a static grep test AND a
  Mongo role for the ingest user granting insert/update but not remove.
- **Generation mark-and-sweep with retraction, not deletion, for uuid-keyed
  records:** every rotation/shrink-triggered full re-ingest of a source file
  runs under a monotonically increasing per-file `gen`; upserts carry
  `$set:{gen:G}`; after the pass, records of that file with `gen < G` get
  `$set:{retracted:true, retractedGen:G}` — an updateMany, never a delete.
  Rationale: the mirror exists BECAUSE the CLI deletes history
  (inception.md:115-119); physically deleting rewound records re-imports the
  CLI's destruction. D13 honesty is satisfied by rendering: retracted records
  are hidden by default, visible on demand, never shown as current
  (LIVEFLOW-8's `retracted` rendering wins over SESSIONJSONL-2's deleteMany
  for `records`).
- **The one legal delete:** positional-keyed `stream_meta` docs of a file
  whose lines renumbered — `deleteMany({sessionId, lineNo-scope})` immediately
  followed by the re-insert in the same code path (stale positional docs are
  aliasing garbage, not history; MONGOSCHEMA-1). Incremental append ticks
  never bump `gen` and never delete/retract.
- **No TTL index on any Touch collection, ever** — a static test asserts no
  index definition carries `expireAfterSeconds` (MONGOSCHEMA-13).
- **Source disappearance is a field, never a removal:**
  `sources[].present:false, lastSeenTs` set by a stat pass — the same
  three-state derived-archive-label vocabulary GD-14 already mandates.

### GD-27 — Mongo security posture (GD-13 extended before any mirror code)
[MONGOSCHEMA-10 ≡ LIVEFLOW-19 ≡ CUSTOMSTATE-13; MONGOSCHEMA-20]

A plain `docker run -p 27017:27017 mongo:7` is an unauthenticated database on
0.0.0.0 with an anonymous volume (probed: zero users, anonymous connect
succeeds) — a second, wholly ungoverned network surface holding the exact
unredacted transcripts GD-13 exists to protect. Invariants:

- mongod binds **`127.0.0.1` only** (`-p 127.0.0.1:27017:27017`), runs with
  `--auth` and a dedicated least-privilege `touch` user; `/data/db` is a
  **named** volume; the exact `docker run` recipe lives in the docs (R-42),
  not the reader's imagination.
- Credentials/URI live in `.touch/mongo.json` mode **0600** (same handling as
  `server.json`'s token), supplied to the aggregator via `TOUCH_MONGO_URI`
  env; **never** in a repo file, a prompt, `events.jsonl`, `/health`, or any
  API response. The port is NOT in the `sbx ports` instructions.
- **Touch refuses to start the mirror against a mongod reporting zero
  configured users**, and says why in `/health`.
- Never-mirrored deny-list: `.touch/server.json` (any field),
  `~/.claude/.credentials.json`, `~/.claude.json`, env vars matching
  `(?i)(token|secret|key|password|auth)`.
- `.gitignore` (extends R-01 additively): `mongo-data/`, `mongo-dump/`,
  `*.bson`; no Mongo data directory inside the repo.
- Database name is derived, never a constant: `touch_<sha1(repo-realpath)[:8]>`,
  overridable via `TOUCH_MONGO_DB`; tests use `touch_test_<pid>` and drop only
  names they constructed (GD-12's wrong-target invariant, extended).

### GD-28 — Custom-state taxonomy and provenance (what "custom state" IS)
[CUSTOMSTATE-1, CUSTOMSTATE-2, CUSTOMSTATE-3, CUSTOMSTATE-15, CONVO-3]

"Custom state" has zero occurrences in the entire normative corpus — it is
greenfield, defined here once as a closed taxonomy of four classes:

1. **mirrored harness fact** — upsert-only from `~/.claude`; never editable.
2. **derived** — computed by Touch; rebuildable; droppable (GD-23).
3. **orchestration state** — asserted by agents/scripts via files (ledger,
   topology, `status.sh` events, `orch-config.json`).
4. **Touch application state** — control intents/acks, annotations, tags,
   pins; the only class Touch authors.

Classes 3+4 are what the user's "custom state" means. GD-11 is amended with a
mandatory **orthogonal `provenance` field**:
`"harness" | "derived" | "asserted" | "touch" | "unknown"` — orthogonal to
D4's `source` channel enum, which conflates trust levels (CUSTOMSTATE-2).
Structural enforcement, all three cheap (CUSTOMSTATE-15): the custom-state
writer has no code path emitting `provenance:"harness"` (unit-tested);
`$jsonSchema` pins `custom_state*` to `{asserted,touch}` and mirror
collections to `{harness,derived}`; the reader helper takes a provenance
filter with **no default**. Legacy attribution must not guess
(CUSTOMSTATE-3): lines with `agent`/`tokens` keys ⇒ `derived` (watcher-only
shapes); lines with `title` ⇒ `asserted` (only status.sh reads `ORCH_TITLE`);
everything else ⇒ `unknown`, rendered "writer unknown", excluded from
harness-authority queries. Fixed forward by R-39's one-key `w` field.

### GD-29 — Writer topology: agents write files; the aggregator is the sole Mongo writer
[CUSTOMSTATE-6, LIVEFLOW-11]

- **No agent ever holds a Mongo client.** Agent-side helpers stay
  single-`write()` file appends under `flock` (status.sh genre): no network,
  no credential in prompts/transcripts, no dependency in agent execution, no
  30 s stall when the daemon is down ("a best-effort writer must never break
  an agent" — already settled law). The aggregator tails those files (R-23)
  and projects them into Mongo.
- **Writer lease** (`writers` collection): `_id=<stream>`,
  `{holderPid, holderBoot, leaseExpiresAt}` renewed per tick; a process that
  cannot hold the lease refuses to mirror (may still serve reads) and says so
  in `/health`. Needed because idempotent replay must tolerate duplicate-key
  as success, and duplicate-key is ALSO the signature of two live writers
  racing one stream — so tolerated-dup counts are exposed: a burst at startup
  is healthy, a nonzero steady state means a second writer or a key bug.

### GD-30 — Latency budget and async mirror I/O (acceptance numbers, not prose)
[LIVEFLOW-9, LIVEFLOW-10, MONGOSCHEMA-4, LIVEFLOW-15]

| budget line | value | source |
|---|---|---|
| transcript block flush (CLI) | 100 ms | inception.md:83-88 |
| Touch tailer poll (stat-first) | 250 ms | D6/R-23 |
| aggregator reduce + WS push | ≤ 50 ms | new |
| **end-to-end, agent action → pixel** | **≤ 400 ms p95, ≤ 1 s p99** | new |
| Mongo mirror write (measured) | 0.61 ms `w:1` / 1.65 ms `j:true` | measured |
| Mongo contribution to the critical path | **0 ms** (async, off-path) | GD-22 |

- Mirror writes go through a **bounded queue** drained by one worker task; on
  queue-full, drop mirror writes (never live frames), count drops, set
  `mirror:"degraded"`. Circuit breaker: after N consecutive failures, stop
  attempting 30 s. pymongo is blocking unless the async client is used —
  never call it inline in the poll loop (the module already learned this:
  SERVER-5 / `asyncio.to_thread` precedent).
- Per-tick ingest CPU is **O(bytes appended since last tick)**, asserted by a
  byte-counter test (append 1 KB to a 20 MB fixture ⇒ tick reads < 64 KB) —
  not a timing test. (Today's 1 Hz full re-parse extrapolates to ≈320 ms/tick
  on a 40-agent run — over the entire budget.)
- Token frames coalesce to ≥1 s on the wire even though ingest runs at
  250 ms.

---

## 2. Dispositions of existing law (every touched GD/R/D; all others stand)

| Target | Disposition |
|---|---|
| **D5** (`.touch/` layout) | **AMENDED additively** — `.touch/` remains system of record; adds `mongo.json` (0600), `custom-state.jsonl` WAL (R-52); no existing file changes meaning. |
| **D6** (tailer / no auto-discovery) | **STANDS** — cross-session run-dir globbing is reading a configured project root, not discovery (SESSIONJSONL-3/-13). |
| **D8.1** (stdlib) | **AMENDED** → GD-21. |
| **D8.2** (journal result opaque) | remains **SUPERSEDED** (unchanged). |
| **G2** (monitor-spawn storage) | Storage clause formally superseded already; "one collection per entity type, never per session" **carried forward verbatim** into GD-24 (also for custom state — CUSTOMSTATE-17). |
| **GD-1** | **AMENDED** — commit gate scoped to "any watcher whose `ORCH_STATE_DIR` is inside the paths being committed" (otherwise unsatisfiable — 3 orphan watchers live now, CONVO-14); run-close protocol added (R-40). |
| **GD-7** | **AMENDED** — ordinal derivation stated verbatim: `ordinal` = 0-based count of preceding `started` records with the same `key` in the same `journal.jsonl`, in file line order; stored, never recomputed from a DB counter (restart-unsafe — MONGOSCHEMA-18); `agentId → (runId,key,ordinal)` is 1:1 (live 3-key retry specimen `wf_455b348c-e17`, SESSIONJSONL-4). `promptId` is a nullable turn label, never identity or index (SESSIONJSONL-10). |
| **GD-8** | **AMENDED** — three-level stop statement: **run-level stop IS available** in the Workflow profile via the launch `toolUseResult.taskId` (verified `w4hiywrt6`/`www4dk54h` — CONVO-12); per-agent stop stays unavailable there; UI renders the two granularities distinctly. R-33's `"taskId": null` refers to agents only. |
| **GD-10** | **AMENDED** — extended to Workflow agent nodes: three-state read-time predicate (result ⇒ done; no result + transcript mtime in window + owning session busy ⇒ running; else unknown/stale, never running, never failed — SESSIONJSONL-8, LIVEFLOW-13). |
| **GD-11** | **AMENDED** — (a) the `{uuid}` ref member covers ONLY `user|assistant|system|attachment`; other types key positionally (GD-24); (b) tokens are `$max`-upserted absolute docs (GD-25); (c) all storage keys are strings (GD-24); (d) ref union gains two validated members `{root,name,attempt}` and `{task,plan,stage?,attempt?}` (CUSTOMSTATE-7), open tail preserved; (e) mandatory `provenance` field (GD-28); (f) "never substituted" extends to `agentCount` (nodeCount — SESSIONJSONL-7); (g) `ts` stored as BSON Date + original string `tsRaw`; the aggregator supplies every `ts`, the server never generates one, `$natural`/ObjectId-time never order anything (LIVEFLOW-16). |
| **GD-12** | **EXTENDED** — DB-namespace rule of GD-27 is the same wrong-target invariant. |
| **GD-13** | **EXTENDED** → GD-27. |
| **GD-14** | **AMENDED** — its in-memory ordinal counter applies to the LEGACY arm only (no journal there); never interchangeable with GD-7's journal-derived ordinal. Legacy `_id` = `legacy:<task>#<line:08d>` — safe precisely because of the never-delete-events.jsonl rule, which the schema now DEPENDS on (MONGOSCHEMA-7). Legacy provenance rules per GD-28. |
| **GD-15** | **EXTENDED** — new owned files, one owner each: `aggregator/refs.py`, `aggregator/mongo_store.py`, `aggregator/mirror.py`, `aggregator/custom_state.py`, matching `tests/test_*.py`. `store.py` keeps sole ownership of `.touch/`; `mongo_store.py` never touches files; `mirror.py` is the only module importing both sides (CUSTOMSTATE-18's divider trap closed). |
| **GD-16** | **EXTENDED** — gitignore additions (GD-27); mirror growth policy in R-57 (measured baseline 15.7 MB / 3 936 records / ≈4 KB per record / ≈1.3 MB h⁻¹ per active session). |
| **GD-18** | **EXTENDED** — fixture set grows per R-41. |
| **GD-19** | **STANDS** — the mirror needs no hooks; the user's "hook" ask is satisfied by tailing (§0.3). |
| **GD-20** | **EXTENDED** — do-not-inherit adds the two GD-22 clauses; copy-verbatim list remains the source of the upsert rules. |
| **R-03** | **AMENDED** — "both copies of `a2fc883c…`" wording corrected to "disjoint continuations" (zero uuid overlap, 17 min apart — MONGOSCHEMA-9); fixture additions in R-41. |
| **R-05** | **EXTENDED** — coordinates with R-38's inception.md:78 correction. |
| **R-08 / R-09 / R-13** | **STAND**, promoted to **hard preconditions** of the mirror (GD-23; CONVO-10, LIVEFLOW-5) — and **scheduled into this pass's first wave + proven against the real streams via R-58**. Mongo ingest reads harness files directly, never legacy `events.jsonl` except through the GD-14 `legacy:` namespace. |
| **R-10** | **EXTENDED** — R-39's `w` field lands in the same files (status.sh, watcher emit). |
| **R-22** | **AMENDED** — stdlib guard gains the named pymongo exception in the same edit (GD-21). |
| **R-23** | **TRANSFERS UNCHANGED + one clause** — the checkpoint `(st_dev, st_ino, size, offset)` and shrink detection are exactly right; the re-ingest it triggers must run GD-26's generation sweep. `size < offset` recorded explicitly as the shrink trigger (inode identity alone misses in-place truncate). |
| **R-24** | **STANDS unchanged** — Mongo does not replace `store.py`; say so to the divider. Store-level additions (scalar `stream`/`seq` fields, `_id` grammar assertions, explain()/IXSCAN test) live in `mongo_store.py`, not here. |
| **R-25** | **AMENDED** — discovery scoped to the cwd slug + every slug in `<slug>/.session-aliases`, never `projects/*` (four foreign slug dirs exist now — SESSIONJSONL-11); the historical arm stands for sessions but is NEVER a grouping key for agent records (SESSIONJSONL-3); sessions arm per R-46. |
| **R-26** | **AMENDED in six places** — uuid-less keying (GD-24); cross-session run/agent assembly (R-48/R-49); journal has no timestamps ⇒ node times derived from agent-transcript first/last record ts, `now()` forbidden (SESSIONJSONL-5); missing snapshot ⇒ run doc created from first journal `started`, snapshot is back-fill only, never an error (SESSIONJSONL-6); `agentCount`→`nodeCount` display-only (SESSIONJSONL-7); tokens as upserted docs (GD-25). Its persisted-output regex + message-id dedup transfer unchanged and re-confirmed; `tool-results/` additionally ingested by directory scan keyed `(sessionId, basename)`, `linkedToolUseId:null` surfaced as "unlinked spilled output" (SESSIONJSONL-14). |
| **R-27** | **STANDS in behaviour** — its output becomes the Mongo `legacy:` arm's input (R-51); there is NO separate migration adapter (CUSTOMSTATE-12). |
| **R-28** | **EXTENDED** — is the natural home of the single reducer (R-54). |
| **R-30** | **EXTENDED** — `/health` gains `mirror:{state, lastError, queued, dropped, tolerated_dups, lease}` (R-45). |
| **R-31** | **EXTENDED** — bounded default replay window + `(stream,seq)` resume; optional `/api/query` served from Mongo with file-store fallback (R-55). |
| **R-32** | **EXTENDED** — live/backfill frame marking; source guard asserting no state-inference in `app.js` (R-54/R-55). |
| **R-33** | **EXTENDED** — Mongo run recipe + `sbx ports` non-instructions (R-42/R-57). |
| **R-34 / R-35** | **STAND (gated)** — control-intent/ack mirror ingest **depends on R-20** and is sequenced after it; until R-20 lands the ingest reads a configured path list (`TOUCH_CONTROL_PATHS`) recording `pathSource`, because `touch-orchestrate/SKILL.md:74-76` on disk still names the path R-20 deletes (CUSTOMSTATE-11). |

### Merged findings (one defect, multiple perspectives — ids kept as aliases)

- MONGOSCHEMA-6 ≡ CUSTOMSTATE-4 ≡ LIVEFLOW-2 (sub-document `_id` field-order) → GD-24/R-43
- CONVO-5 ≡ SESSIONJSONL-1 ≡ MONGOSCHEMA-1 (uuid-less records; the differing 28 %/8.8 % figures are session-vs-corpus denominators, not a contradiction) → GD-24/R-47
- MONGOSCHEMA-3 ≡ LIVEFLOW-1 (+CONVO-13) (change streams / liveness path) → GD-22
- MONGOSCHEMA-10 ≡ LIVEFLOW-19 ≡ CUSTOMSTATE-13 (unauthenticated mongod) → GD-27/R-42
- MONGOSCHEMA-2 ≡ SESSIONJSONL-9 ≡ LIVEFLOW-4 (token idempotency) → GD-25/R-50
- MONGOSCHEMA-4 ≡ LIVEFLOW-9 (dead-Mongo stall / async I/O) → GD-30/R-45
- CUSTOMSTATE-5 ≡ MONGOSCHEMA-13 ≡ SESSIONJSONL-2 ≡ LIVEFLOW-8 (delete/durability) → GD-26/R-45
- MONGOSCHEMA-19 ≡ CUSTOMSTATE-14 (append-only log + derived head) → R-52
- SESSIONJSONL-3 ≡ MONGOSCHEMA-9 (cross-session agent split) → R-48
- CONVO-6 ≡ CONVO-7 ≡ MONGOSCHEMA-5 ≡ CUSTOMSTATE-18 ≡ LIVEFLOW-19(D8 half) (D8/dependency) → GD-21/R-38
- MONGOSCHEMA-12 ≡ CUSTOMSTATE-18 ≡ LIVEFLOW-9(JSONL-primary) (dual sink) → GD-22
- SESSIONJSONL-4 ≡ MONGOSCHEMA-18 (ordinal) → GD-7 amendment/R-49
- CONVO-10 ≡ (existing R-13) (label collision, live specimen) → precondition, GD-23
- CONVO-11 ≡ MONGOSCHEMA-8 (dotted keys / BSON-hostile shapes) → R-44
- CONVO-16 ≡ MONGOSCHEMA-13(growth aspect) → R-57

### Discards (non-items, one line each)

1. **Arm A (vendored stdlib wire client)** — rejected for v0: SCRAM/TLS is
   security-sensitive surface Touch should not own; kept on record as the
   proven fallback (MONGOSCHEMA-5). Do not re-research.
2. **Change streams / replica-set bootstrap** — rejected for v0 with measured
   reason (Location40573 on standalone; init-step total-failure mode);
   capped-collection notify channel recorded as the future push option
   (GD-22). Do not re-hunt.
3. **"Migrate existing state files" item** — phantom: zero `state/` dirs,
   ledgers, control files, or `.touch/` exist filesystem-wide
   (CUSTOMSTATE-12); replaced by R-51's backfill + artifact registry.
4. **Per-session collections** — declined with the user-facing disposition in
   §0.3 (CONVO-8, CUSTOMSTATE-17).
5. **DBRef** — declined; flat `refId` (GD-24; MONGOSCHEMA-15's folklore
   correction recorded so it is not re-litigated).
6. **Content-hash `_id` for uuid-less records** — falsified by probe (loses
   142 of 333); positional keys win (MONGOSCHEMA-1).
7. **A push/mirror hook into the CLI** — no supported interface in 2.1.220;
   one-line disposition under D6, no item (SESSIONJSONL-15).
8. **`session_id` duplicate field** — normalized to `sessionId` at the
   boundary, drop noted in `_normalized` (SESSIONJSONL-16); folded into R-47,
   no own item.
9. **Storing the full raw line as a BSON string per record** (CONVO-11's
   primary option) — declined: doubles storage (CONVO-16) vs the measured
   0.53× parsed shape; byte-fidelity is preserved instead by `_raw`-wrapping
   only variable-key subtrees + a round-trip test (R-44). NUL bytes are legal
   in BSON string *values*; only key shape needs the wrap.
10. **`$set`-last-wins one-doc `session_state` collection** (SESSIONJSONL-1's
    second arm) — declined as a write-path special case: ALL uuid-less types
    go positionally to `stream_meta` (full fidelity — `last-prompt` is 16/16
    distinct), and last-wins currents are served from `derived` (GD-23), which
    is also semantically where a "current value" belongs.

---

## 3. Ordered implementation items (R-38 … R-57)

Not partitioned into sub-plans — `implement-plan`'s divider owns that. Note
for the divider: R-39/R-40 edit `status.sh`/`decision_watcher.py` (group with
the normative plan's R-07/R-08/R-10/R-13); R-44/R-45/R-52 own three separate
new files (GD-15 extension); nothing here touches `store.py`.

**Sequencing constraints (hard):** R-38…R-41 may run any time after phase 0
of the normative plan. R-42…R-57 require normative R-01…R-03 done and R-08 +
R-09 + R-13 green (GD-23). Mirror items consume R-26/R-27's reduction and
nothing consumes them, so they sit after R-26/R-27 and before/parallel to
R-32. R-52/R-53's control-intent arm additionally waits on R-20
(CUSTOMSTATE-11). **R-58 makes R-08/R-09/R-13 part of THIS pass's execution
scope** (first sub-plan wave), not merely an external precondition.

### Phase M0 — anchors, writers, lifecycle, fixtures

---

**R-38 — Anchor repair + measured-truth doc corrections**
- Files: `touch-full-recon-plan.md:27` (D8 row relabelled per §0.2);
  `touch-full-recon-plan.md:311-320` (R-03 "both copies" → "disjoint
  continuations"); `inception.md:78-80` ("usage copied" → running counter,
  `$max` rule); `touch-full-recon/report/probes.md` (append Mongo provisioning
  probe results with command + date: pymongo 4.17.0 via proxy, mongo:7 pulls
  and runs, no host mongod/mongosh, Python 3.13.7, subdocument-`_id`
  field-order sensitivity, BSON type strictness).
- Resolves: CONVO-6, CONVO-7 (doc half), MONGOSCHEMA-2 (doc half),
  MONGOSCHEMA-9 (wording half), CUSTOMSTATE-19.
- Approach: three surgical text edits + one evidence append; coordinate with
  R-05 (same files) so the divider groups them.
- Test: static guard (test_shell.py genre): the plan file contains "D8.1" and
  "D8.2"; inception.md no longer contains "copied onto every split record".

**R-39 — Writer attribution: the `w` field**
- Files: `status.sh:28-49` (add `"w":"agent"`);
  `decision_watcher.py:138-151` (add `"w":"watcher"` in `emit()`);
  `monitoring.md` (schema note: additive key, readers ignore unknown keys);
  `tests/test_shell.py` / `tests/test_watcher.py` extensions.
- Resolves: CUSTOMSTATE-3 (forward half; GD-28's legacy rules handle the
  12-of-130 already-unattributable lines).
- Approach: one key added to each writer's payload; byte-identical five-key
  shape is otherwise preserved so existing readers keep working.
- Test: a line written by each writer carries the right `w`; the monitor
  still renders streams containing the new key.

**R-40 — Daemon lifecycle: run-close protocol + GD-1 gate scoping**
- Files: `decision_watcher.py` (self-exit after journal quiet ≥N s AND a
  terminal `complete` event); both skills' `templates/*.workflow.js` driver
  epilogue (stop daemons on `orchestrator complete`); `CLAUDE.md` (the
  "stop its watcher" rule); `touch-full-recon-plan.md:51-56` (GD-1 wording).
- Resolves: CONVO-14 (three orphaned watchers running now; GD-1's commit gate
  never clears as written).
- Approach: per amended GD-1 — the gate checks only watchers whose
  `ORCH_STATE_DIR` is inside the commit path set; watcher self-exit makes
  "is the loop still running" answerable from process state (the mirror
  daemon inherits the same protocol).
- Test: watcher exits within the window on a fixture journal with a terminal
  complete event; stays alive without one.

**R-41 — Mirror fixture set (extends R-03 / GD-18)**
- Files: new entries under `tests/fixtures/`: the cross-session pair
  `dd469822/…/agent-a2fc883c96ff7b837.jsonl` (223 lines) +
  `e423cd3c/…/agent-a2fc883c96ff7b837.jsonl` (2 lines, no `.meta.json`);
  `wf_455b348c-e17/journal.jsonl` + snapshot (3-key retry, killed run,
  agentCount 6 vs 9 started); a live-run-shape dir (journal + agents, NO
  `<runId>.json`); the dotted-key `file-history-snapshot` records; the
  872 KB line; the queue-operation/user pair (292f:65/:67); the four foreign
  `/tmp` slug dirs as negative discovery fixtures; verbatim
  `touch-mongo-live/events.jsonl` lines incl. the 12 unattributable ones.
- Resolves: fixture halves of SESSIONJSONL-3/-4/-6/-7, MONGOSCHEMA-8/-9,
  CONVO-11/-15, CUSTOMSTATE-3, SESSIONJSONL-11.
- Approach: copy now — the corpus is on the retention-sweep clock; sha256
  manifest like R-03.
- Test: manifest completeness + byte-stability.

**R-58 — Fabricated FAILED badge: schedule and prove the fix** *(appended
2026-07-25 by user directive: "add fix for incorrect synthesis Failed status
to the final plan"; numbered after R-57 to keep all existing cross-references
stable — it executes HERE, in phase M0)*
- Files: none of its own — its execution scope IS normative R-08 + R-09
  (with R-13): `decision_watcher.py:639-663,450-467,748-753` and both
  `templates/*.workflow.js`; plus the R-41 fixture lines
  (`touch-full-recon` and `touch-mongo-live` `events.jsonl`, the
  `wf_930e210a`/`wf_cca84d59` journals).
- Resolves: LIVEFLOW-5 (live-specimen half), CONVO-10; normative-corpus
  aliases SKILLS-1 ≡ RUNSTATE-4 ≡ PRODUCT-7 (register in R-06).
- Defect record: the watcher writes `<plan> plan failed "loop exited ->
  <next-phase>"` whenever a phase advances and the plan's agents returned no
  `passed`/`approved`-shaped verdict — every research fan-out triggers it.
  Reproduced in BOTH runs of this session (touch-full-recon 14:06:23Z,
  touch-mongo-live 18:44:09Z) while all researchers had succeeded; the user
  saw it twice. The driver appended corrective `plan done` events to both
  streams — those correction lines are now themselves part of the historic
  record and must render correctly.
- Approach: (a) R-08/R-09/R-13 are pulled into the SAME implement-plan pass
  as this amendment, ordered before every M1+ item — the divider places them
  in the first sub-plan wave; their content is specified in the normative
  plan and is NOT restated here (one owner per file). (b) Read-time rules for
  the already-written history (no stream is ever rewritten): the GD-14/R-51
  re-label ("`plan failed` + detail `loop exited ->` + all stage agents
  resulted" ⇒ "closed — no verdict", `derived_from_legacy:true`) applies to
  all three affected task streams; additionally **conflicting terminals on
  the same `(task, plan, stage='plan')` resolve last-event-wins in file
  order** — a later corrective `done` beats an earlier fabricated `failed`
  (RUNSTATE-7's watcher-wins dedup applies only to same-state duplicates,
  stated here so the legacy adapter cannot resurrect the failed badge).
- Test: replay the three real streams (R-41 fixtures) through the fixed
  watcher rules + legacy re-labeler ⇒ zero `failed` badges on research or
  synthesis plans; the failed-then-done fixture (verbatim touch-mongo-live
  lines) renders `done`; a synthetic verdict-less research journal closes
  `done` + `orchestrator complete done` (shares R-08's fixture); static
  guard — templates contain the terminal `plan done` calls (R-09's guard,
  cited as this item's acceptance too).

### Phase M1 — foundations (deployment, keys, store)

---

**R-42 — Mongo deployment + security baseline (GD-27 made runnable)**
- Files: new `docs/mongo.md` (or R-33's run section): the exact
  `docker run -d --name touch-mongo -p 127.0.0.1:27017:27017 -e
  MONGO_INITDB_ROOT_USERNAME=touch -e MONGO_INITDB_ROOT_PASSWORD=… -v
  touch-mongo-data:/data/db mongo:7` recipe + user bootstrap; new
  `.touch/mongo.json` handling in `aggregator/mirror.py` (0600, refuse
  world-readable); `.gitignore` additions (`mongo-data/`, `mongo-dump/`,
  `*.bson`).
- Resolves: MONGOSCHEMA-10, LIVEFLOW-19, CUSTOMSTATE-13, MONGOSCHEMA-20,
  CONVO-13 (deployment half).
- Approach: per GD-27 in full, including the zero-users refusal and the
  derived DB name.
- Test: script asserts the documented bind is loopback and an
  unauthenticated connect fails against a recipe-provisioned container
  (skips if no docker); `git check-ignore` passes for `mongo-data/x`;
  test_shell.py-genre guard that no file under `aggregator/` contains a
  connection string literal.

**R-43 — `aggregator/refs.py`: the `ref_key` canonicalizer + ref-union extension**
- Files: new `aggregator/refs.py`, `tests/test_refs.py`.
- Resolves: CUSTOMSTATE-4, LIVEFLOW-2, MONGOSCHEMA-6 (shared arm),
  CUSTOMSTATE-7.
- Approach: ONE `ref_key(ref) -> str` emitting the GD-24 grammar
  (fixed field order, `%`-escaping of `% # | :` in user-chosen components,
  zero-padded ints); structured `ref` emitted alongside in canonical order;
  the two promoted union members `{root,name,attempt}` and
  `{task,plan,stage?,attempt?}` validated; unknown shapes → `kind:"unknown"`,
  no `refId`. All modules (including JS-template-authored JSON that later
  gets ingested) construct refs via this helper or its documented grammar.
- Test: every ref shape built twice with different dict insertion orders ⇒
  equal `_id`, one document; type pins (procStart string, ordinal int)
  round-trip; escaping round-trips a task name containing `#|:%`.

**R-44 — `aggregator/mongo_store.py`: collections, indexes, upsert algebra, validators**
- Files: new `aggregator/mongo_store.py`, `tests/test_mongo_store.py`.
- Resolves: MONGOSCHEMA-14, MONGOSCHEMA-16, MONGOSCHEMA-8, CONVO-11,
  MONGOSCHEMA-17, SESSIONJSONL-12, LIVEFLOW-3, LIVEFLOW-16, GD-24/GD-25
  (store half).
- Approach: the GD-24 table verbatim — collection defs, `$jsonSchema`
  bsonType pins, index bootstrap (unique `{stream:1,seq:1}` on events; no
  TTL anywhere); upsert op builders (`$max`/`$addToSet`/`$min`/`$setOnInsert`
  only — no `$inc`, no bare `$set` on accumulables); dotted/`$`-key policy:
  variable-key subtrees (`snapshot.trackedFileBackups`, patch/backup maps,
  arbitrary tool `input`) stored `_raw`-wrapped, validator rejects dotted or
  `$`-prefixed keys outside declared wrappers; oversize guard: document
  > 8 MB ⇒ stub `{oversize:true, bytes, sourcePath, byteOffset}` (never
  silently dropped — 872 KB real max is 5 % of the 16 MiB cap, headroom is
  real but finite); `writeErrors` of unordered bulks ALWAYS inspected and
  surfaced; aggregator supplies every `ts` (Date + `tsRaw` string).
- Test: **the GD-25 acceptance test** — frozen fixture corpus ingested
  normally/shuffled/reversed ⇒ identical fingerprint AND expected counts;
  `explain()` asserts IXSCAN on the cursor query; static no-TTL and
  no-delete-verb greps; dotted-key fixture stores, round-trips
  byte-identically, and is rejected unwrapped; oversize fixture ⇒ stub.

**R-45 — `aggregator/mirror.py`: queue, breaker, lease, cursors, sweep, rebuild/backfill**
- Files: new `aggregator/mirror.py`, `tests/test_mirror.py`.
- Resolves: MONGOSCHEMA-4, LIVEFLOW-9, LIVEFLOW-11, MONGOSCHEMA-12,
  MONGOSCHEMA-13, SESSIONJSONL-2, LIVEFLOW-7, CUSTOMSTATE-5.
- Approach: bounded `asyncio.Queue` + one drainer using `AsyncMongoClient`
  with GD-21's timeouts; circuit breaker (N failures ⇒ 30 s hold);
  `/health` `mirror` block `{state, lastError, queued, dropped,
  tolerated_dups, lease}`; per-stream `cursors` watermarks; the GD-29 writer
  lease; GD-26 generation sweep (retraction updateMany for `records`;
  scoped deleteMany+reinsert for `stream_meta` only); `--rebuild` (drop
  derived + replay everything from files); `--backfill` (walks
  `~/.claude/projects/**` once, **hard-codes `live=False`**, refuses any
  `ts` newer than source mtime, stamps `ingestMode:"backfill"`).
- Test: dead-port test — tick duration stays under one tick budget and
  `/health` reports `mirror:"down"` (MONGOSCHEMA-4's 30.1 s stall
  reproduced-then-fixed); queue-full drops mirror writes, never live frames;
  two writers on one stream ⇒ second refuses; replay of own output ⇒ dups
  tolerated, zero data change; backfill of a 03:00Z-dated fixture ⇒ no
  stored ts within 24 h of `now()`; Mongo wipe + `--rebuild` ⇒ fingerprint
  equal to pre-wipe.

### Phase M2 — mirror pipelines (consume R-26/R-27's reduction)

---

**R-46 — Sessions arm: tagged union, promotion, discovery scope**
- Files: `aggregator/sessions.py` (extends R-25), mirror mapping in
  `mirror.py`; `tests/test_sessions.py`.
- Resolves: CONVO-4, SESSIONJSONL-11 (aliases: GD-6/R-25 historical arm,
  which both prior Mongo answers ignored).
- Approach: `_id` per GD-24 — `live:<pid>-<procStart>` only for sessions
  observed in `~/.claude/sessions/` (1 of 6 today); `hist:<sessionId>`
  otherwise; **`_id` is immutable** — when a registry entry later names a
  sessionId stored as `hist:`, the live doc gains `sessionIds[]` via
  `$addToSet` and the hist doc gets `promotedTo:<liveId>`; discovery scoped
  to cwd slug + `.session-aliases` slugs; the seventh
  `history.jsonl`-only sessionId recorded as `sources:[]` (transcriptless).
- Test: mirror the fixture project dir ⇒ 6 session documents, exactly one
  `live:`; the four foreign slug dirs are NOT ingested; promotion leaves
  both docs queryable with no `_id` rewrite.

**R-47 — Records + stream_meta bucketing (the 12-type table)**
- Files: `aggregator/ingest.py` (extends R-26) + `mirror.py` mapping;
  `tests/test_ingest.py`.
- Resolves: CONVO-5, SESSIONJSONL-1, MONGOSCHEMA-1, CONVO-15,
  SESSIONJSONL-16.
- Approach: the normative bucket table, stated here ONCE —
  `user|assistant|system|attachment` ⇒ `records` (`_id=uuid`; these are
  exactly the harness's own uuid-bearing set); **every other type**
  (`mode, permission-mode, ai-title, last-prompt, queue-operation,
  file-history-snapshot, file-history-delta, frame-link`, plus any
  unknown/future type) ⇒ `stream_meta` positional. `sessionId` injected from
  the file path (8 records in this session carry none);
  `file-history-snapshot.messageId` stored as a join field (verified: equals
  the uuid of the snapshotted record); `queue-operation` gets `render:false`
  and is intentionally NOT deduped against its `user` twin (different
  events: enqueue vs delivery; the pair is the only observable queue
  latency); `session_id` dropped, noted in `_normalized`; `lineNo` +
  `byteOffset` stored on every mirrored record (order is an explicit field
  now, not append order).
- Test: ingest the 267-line fixture twice ⇒ identical counts both times;
  all 17 `mode` occurrences present positionally; last-wins currents served
  from `derived` match the file's final values; uuid coverage assertion
  (fixture's uuid-bearing count in `records`, uuid-less count in
  `stream_meta` — nothing collapsed).

**R-48 — Agents assembly: fragments, union writes, spawn locator**
- Files: `aggregator/agents.py` (extends R-28) + mapping; `tests/test_agents.py`.
- Resolves: SESSIONJSONL-3, MONGOSCHEMA-9, CONVO-9.
- Approach: `_id = agentId` (17-hex validated; 8-hex only via the `legacy:`
  namespace); `fragments:[{sessionId, path, firstUuid, lastUuid, lineCount}]`
  ordered by the `parentUuid → uuid` stitch chain (verified exact on the
  live pair); message order = chain order, never directory order;
  `sessionId` is NEVER a grouping key for agent records; `.meta.json` is
  optional — all its fields nullable, the fragment that HAS meta wins on
  disagreement; writes are `$addToSet` sessions/files/fragments +
  `$min/$max` first/last + `$setOnInsert` immutables; `spawn` sub-document
  `{recordUuid, toolUseId, fileHint:{path, line, ino, size, ts}}` — fileHint
  valid only while `(st_dev, st_ino, size)` match, marked stale on mismatch
  (kept for diagnostics); "jump to spawn" resolves via
  `records.findOne({_id: spawn.recordUuid})`, never re-reads the file.
- Test: ingest the two `a2fc883c` fixture files in BOTH orders ⇒ one
  document, two fragments in chain order, firstTs `02:59:29.846Z`, token
  rollup = union; mutate fixture size ⇒ fileHint stale, recordUuid lookup
  still resolves; missing-meta fragment does not throw.

**R-49 — Runs + run_nodes: ordinal, timestamps, snapshot back-fill, run stop join**
- Files: `aggregator/ingest.py` + mapping; `tests/test_ingest.py` extension.
- Resolves: SESSIONJSONL-4, SESSIONJSONL-5, SESSIONJSONL-6, SESSIONJSONL-7,
  MONGOSCHEMA-18, CONVO-12.
- Approach: run doc created from the FIRST journal `started` (a live run has
  no `<runId>.json` — one is on disk right now proving it); run discovery
  globs `projects/*/*/subagents/workflows/<runId>/` across sessions
  (plural), `sessionIds[]` via `$addToSet`; `run_nodes._id` uses GD-7's
  amended ordinal (position-derived, stored `journalSeq`); node
  `startedAt/endedAt` derived from the agent transcript's first/last record
  timestamps — journal records carry NO timestamp, `now()` forbidden,
  `ingestedAt` kept separate and never displayed; snapshot when it appears
  is back-fill only — never overwrites an observed non-null, `agentCount`
  lands as `harnessTotals.nodeCount`; the launch `toolUseResult`
  (`taskId, runId, transcriptDir, scriptPath, workflowName`) is persisted on
  `runs` as first-class fields — the ONLY deterministic main-session→run
  join, and the run-level stop handle per amended GD-8.
- Test: `wf_455b348c-e17` fixture ⇒ 9 nodes across 6 keys with ordinals
  0/0/0/0/0/0,1,1,1; killed run's 7 resultless nodes render unknown/stale,
  never running (with R-54); live-run fixture (no snapshot) ⇒ run doc
  exists, no error; snapshot arrival back-fills without clobbering; the
  taskId join resolves from the fixture `toolUseResult` line.

**R-50 — Usage mirror: absolute `$max` documents**
- Files: mapping in `mirror.py`/`ingest.py`; `tests/test_usage.py`.
- Resolves: MONGOSCHEMA-2, SESSIONJSONL-9, LIVEFLOW-4.
- Approach: `_id = <message.id>`;
  `$max:{in,out,cached,cache_write}` + `$setOnInsert:{agentId, sessionId,
  runId}` — decided over the `(agentId, message.id)` compound (a message.id
  never spans agents; if an incoming doc's agentId differs from the stored
  one, increment a conflict counter, never overwrite); rollups are `$group`
  sums over usage docs, never `$inc` counters, never `harnessTotals`;
  deltas wire-only per GD-25.
- Test: corpus passes shuffled ⇒ identical totals (the verified
  `in 27 593 / out 1 062 413` property on the frozen fixture subset);
  re-ingest after simulated `performRemoveByUuid` ⇒ totals unchanged;
  agentId-conflict counter fires on a doctored fixture.

**R-51 — Legacy events mirror + artifact registry (no migration item)**
- Files: `aggregator/legacy.py` (extends R-27) + mapping;
  `tests/test_legacy.py` extension.
- Resolves: MONGOSCHEMA-7, CUSTOMSTATE-3 (ingest half), CUSTOMSTATE-12.
- Approach: R-27's reduction is the input — NOT a new adapter;
  `_id = legacy:<task>#<line:08d>` (append-only file ⇒ positional is
  stable; the schema now depends on the never-delete rule — recorded);
  provenance per GD-28's no-guess rules (`agent|tokens` ⇒ derived,
  `title` ⇒ asserted, else `unknown`); byte-identical duplicate lines and
  duplicate ts values (measured: up to 27 per file) are distinct events by
  position — content/ts keys forbidden; the **artifact registry**: one
  `custom_state_events` kind `artifact` per task-folder file
  `{taskId, kind: findings|plan|report|script|config|log, path, sha256,
  size, mtime}` — paths + digests only, never bodies;
  `.watcher-state.json` explicitly excluded (GD-14); there is nothing else
  to migrate — stated so nobody goes looking.
- Test: the touch-mongo-live fixture lines incl. 2 byte-identical
  duplicates ⇒ N docs for N lines; the 12 unattributable lines carry
  `provenance:"unknown"`; artifact registry lists the fixture folder's
  files with correct digests.

### Phase M3 — custom state (user requirement 2)

---

**R-52 — Custom-state WAL, events collection, derived head**
- Files: new `aggregator/custom_state.py`, `tests/test_custom_state.py`;
  `.touch/custom-state.jsonl` (via `store.py`'s existing append machinery —
  `store.py` itself unchanged).
- Resolves: CONVO-3, MONGOSCHEMA-19, CUSTOMSTATE-14, CUSTOMSTATE-16,
  CUSTOMSTATE-17.
- Approach: custom state is the ONE dataset not rebuildable from `~/.claude`,
  so it is file-journaled FIRST: writers append to the `.touch/` WAL
  (touch-events-v2 shapes, `(stream, seq)`); the mirror projects to
  `custom_state_events` (append-only, insert-only — no update grant) and
  reduces to `custom_state` (`_id = <refId>#<stateKey>`, `$max`-seq-guarded
  `{seq:{$lt:newSeq}}` so a late old write never clobbers); ONE events +
  ONE head collection installation-wide, discriminated by `kind`
  (`ledger|control_intent|control_ack|topology|agent_state|annotation|tag|
  artifact`) — never per task/session (G2 carried forward); deletes are
  tombstone events; `refId` validated against `agents`/`run_nodes`/`slots`
  grammars, writes for unknown refs rejected (a dangling state card is
  worse than a rejected one); annotations: `author:"local"` literal (Touch
  has no user identity — never fabricate one), own 16 KB cap that REJECTS
  with 413 rather than truncating (machine detail strings truncate, user
  prose does not), escape-first rendered; provenance pinned
  `{asserted,touch}` by `$jsonSchema` (GD-28).
- Test: 3 out-of-order writes ⇒ head = highest seq, log has 3; unknown
  refId rejected; Mongo wipe + WAL replay reproduces both collections
  exactly; drop `custom_state`, rebuild, document-for-document equal;
  writer module cannot emit `provenance:"harness"` (unit-asserted).

**R-53 — Slots: the name↔agentId binding**
- Files: `aggregator/custom_state.py` (slots arm) or `agents.py` join side;
  `touch-orchestrate/SKILL.md:52-56` (ledger line amendment — coordinate
  with R-20's edit); `tests/test_slots.py`.
- Resolves: CUSTOMSTATE-8, CUSTOMSTATE-9, CUSTOMSTATE-10, CUSTOMSTATE-11.
- Approach: `slots` per GD-24 — the SINGLE place the name↔agentId hop
  happens (custom state addresses by NAME because agentId is unknowable
  pre-spawn; the mirror addresses by agentId); resolution state machine
  `pending | bound | orphaned | conflict` with `pendingSince` — GD-7
  explicitly permits nodes that never get a marker, so `orphaned` is a
  normal outcome, rendered honestly, never hidden (an orphaned stop intent
  is a stop that went nowhere); bind evidence channels: `[touch]` marker,
  ledger line, Agent-tool `description` (`boundBy`); a bind colliding on
  the unique sparse `agentId` index writes `conflict` recording BOTH
  agentIds and **never raises** — `DuplicateKeyError` is caught, counted,
  the tailer lives; ledger line amended to add `root` + `sessionKey`
  (`<pid>-<procStart>` — the orchestrating session knows its own pid);
  ingest of pre-amendment lines derives `sessionKey` from the containing
  path with `sessionKeySource:"path"`; control-intent/ack ingest waits on
  R-20 and reads `TOUCH_CONTROL_PATHS` until then (never restates the
  path — CUSTOMSTATE-11).
- Test: pre-spawn state keyed by name binds when the marker lands
  (pending→bound); duplicate bind ⇒ conflict doc with both ids, process
  alive; two same-named roots in different sessions do NOT cross-link
  (sessionKey in `_id`); markerless node ⇒ orphaned after the TTL, run
  terminal.

### Phase M4 — reduce, serve, accept, document

---

**R-54 — The single reducer + `derived` collection + read-time liveness**
- Files: `aggregator/agents.py` (extends R-28; the reducer home) +
  `derived` mapping; `tests/test_reducer.py`.
- Resolves: LIVEFLOW-5, LIVEFLOW-6, LIVEFLOW-12, LIVEFLOW-13,
  SESSIONJSONL-8, LIVEFLOW-18.
- Approach: per GD-23 — observations in, derived out, `reducerVersion` +
  drop-and-rebuild; three-state liveness computed from `now()` at read
  time (no `state` field anywhere in storage); the run-close rule: an
  agent with no result and idle > 180 s transitions to `unknown`, LEAVES
  the running set for run-close purposes, renders "unknown — idle N m",
  never running/failed (five same-attempt siblings in this very run are
  the specimen); monitor.html's freeze-to-stale rule moves INTO the
  reducer so page and API cannot disagree; topology (`custom_state`
  kind `topology`, from R-19's files when they exist) supplies attempt
  denominators — absent topology ⇒ "attempt N" with NO denominator and no
  next-stage arrow (D13; every pre-R-19 run takes this arm).
- Test: fixture with last observation 10 min old ⇒ `unknown`; same fixture
  with faked `now()` inside the window ⇒ `running` (proves derivation);
  five-sibling fan-out with one dead ⇒ run closes, four `done`, one
  `unknown`, zero `failed`; reducerVersion bump ⇒ derived rebuilt, same
  output; API answer == page render for the frozen-stale case.

**R-55 — Read API + WS: bounded replay, resume, live/backfill marking**
- Files: `aggregator/server.py` (extends R-30/R-31), `touch-visual/app.js`
  (extends R-32), `tests/test_api.py`.
- Resolves: LIVEFLOW-14, LIVEFLOW-17, LIVEFLOW-10 (wire half),
  MONGOSCHEMA-11 (R-30/R-31 rows).
- Approach: default replay window bounded (current run or last N events,
  whichever larger), explicit `?from=` + "load older"; reconnect resumes
  from the client's last `(stream, seq)` — REQUIRES the absolute-token
  model (partial replay of deltas = silently low counters; the two are a
  package); frames carry `live:true|false` (or the stream declares mode at
  handshake, switching once at the replay→tail boundary) — replayed frames
  paint once, no animation; token frames coalesce ≥1 s; optional
  `/api/query` served from Mongo with documented file-store fallback;
  existing routes keep reading the in-memory reduction so the UI never
  depends on Mongo.
- Test: reconnect mid-stream ⇒ no duplicate events, counters equal
  full-replay totals; backfill burst carries `live:false`; frontend source
  guard: no animation class on non-live frames, no state-inference in
  `app.js`.

**R-56 — End-to-end acceptance (extends R-37)**
- Files: `tests/test_e2e_sim.py` extension.
- Resolves: LIVEFLOW-10, LIVEFLOW-15, MONGOSCHEMA-12(c), GD-30 acceptance.
- Acceptance, all against fixtures:
  - **No-mongod arm**: with pymongo absent AND with mongod unreachable —
    sessions, agent rows, loop cards, token counters all update; `/health`
    reports `mirror: absent|down`; every module imports; full suite green
    (the suite runs with no services on a bare checkout).
  - **Mirror arm** (skips cleanly if no mongod): double-ingest fingerprint
    (GD-25); wipe + rebuild equivalence; the wf_455b348c retry topology and
    the a2fc883c cross-session union render correctly through the FULL
    path (files → ingest → mirror → reducer → API).
  - **Budget arm**: per-tick byte-counter test (O(delta) — LIVEFLOW-15);
    dead-mongo tick-duration test (MONGOSCHEMA-4).
- Test: is the test.

**R-57 — Documentation: run recipe, dispositions, growth policy**
- Files: `README.md` (run section), `docs/control-semantics.md` (run-level
  vs agent-level stop per amended GD-8), `docs/mongo.md` (R-42's recipe +
  rebuild/backfill commands + "Mongo down is a non-event" statement),
  `CLAUDE.md` (mirror daemon lifecycle rule per R-40; GD-21 dependency
  policy note).
- Resolves: CONVO-8 (user-visible disposition), CONVO-16, MONGOSCHEMA-11
  (docs rows), CONVO-13 (docs half).
- Approach: state the per-session-collection disposition in user-facing
  words (§0.3); the retention/growth policy: kept forever — sessions,
  agents, runs, run_nodes, usage, custom state (all small); prunable —
  nothing in v0 (no TTL; revisit at the GD-16 threshold with the measured
  baseline: 15.7 MB / 3 936 records / ≈4 KB per record / ≈1.3 MB h⁻¹ per
  active session; mirror ≈0.53× raw on disk); document that `sbx ports`
  must NOT publish 27017.
- Test: R-05's static doc guard extended: README/docs contain the loopback
  recipe and no `0.0.0.0`-mongo example; CLAUDE.md names the pymongo
  exception.

---

*21 items (R-38…R-58; R-58 executes in phase M0), 10 new global decisions
(GD-21…GD-30). Preconditions: normative R-01…R-03 done; R-08 + R-09 + R-13
green before any mirror write (GD-23) — scheduled and proven via R-58. Hand
`implement-plan` this file together with `touch-full-recon-plan.md`.*
