# research — liveflow perspective — attempt 1

Perspective: the live monitoring flow end to end, and how a MongoDB sink must
not break it. Everything below is either read out of the named file:line or
measured this run; measurements are labelled **[measured]** with the command
context. Findings are written as amendments to
`.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md`
(GD-n / R-nn ids) — they do not re-plan what that file already covers.

---

## 0. The two pipelines, traced (context for every finding below)

**Today (working, in production this very run):**

```
agent  ──status.sh:28-49────────────────┐
                                        ├─> <task>/events.jsonl ─> monitor_server.py ─ws─> monitor.html
harness journal.jsonl ─decision_watcher─┘        append-only          stream_events():378       render()
   (per-agent transcripts, 100 ms flush)          text lines          0.5 s poll, full replay
```

Per-hop cost, as coded:

| hop | source | cadence |
|---|---|---|
| CLI writes a completed content block to the transcript | `inception.md:83-88` | 100 ms timer |
| watcher polls the journal + re-reads every running agent's transcript | `decision_watcher.py:796` (`time.sleep(1)`), `:764-795` | 1 s |
| watcher classifies a fresh spawn (transcript may not have flushed) | `decision_watcher.py:330-367` | up to 3 × 0.5 s inline |
| server tails `events.jsonl` and pushes WS frames | `monitor_server.py:365-378` | 0.5 s |
| server WS keepalive ping | `monitor_server.py:375-377` | ~20 s |

**Worst-case observable latency today ≈ 0.1 + 1.0 + 0.5 = 1.6 s**, plus up to
1.5 s more on the very first event of an agent whose transcript has not
flushed. Typical ≈ 0.8 s.

**Planned (`touch-full-recon-plan.md`):** R-23 tailer (250 ms stat-first poll,
GD-7/D8 `inception.md:226-228`) → R-26 ingest → R-24 store → R-30/R-31
API/WS → R-32 UI. **Planned worst case ≈ 0.1 + 0.25 + push ≈ 0.4 s.**

**Measured Mongo cost of inserting into that path** (mongo 7.0.39, the
`touch-mongo-probe` container another researcher started this run):

| operation | **[measured]** |
|---|---|
| `insertOne` `w:1` | 0.61 ms |
| `insertOne` `w:1, j:true` | 1.65 ms |
| `bulkWrite` 2000 docs, unordered | 37 ms (≈54k docs/s) |
| replay of the same 2000 docs (deterministic `_id`) | 275 ms, count stays 2000 |
| full-collection replay, 20 000 docs, indexed sort | 84 ms |
| indexed tail query, 999 of 20 000 docs | 7 ms |

**Conclusion for the whole perspective: MongoDB is nowhere near the latency
budget — the poll interval is. Every risk below is a correctness or
availability risk, not a throughput one.** The plan should say so explicitly so
nobody "optimises" the wrong thing.

---

## LIVEFLOW-1 — The live path must read from memory; Mongo is a write-behind mirror. Change streams are not available on the mongod this plan will actually produce
**File**: `touch-full-recon-plan.md:226-232` (D8 stack, restated), `inception.md:226-228`
**Severity**: blocker (it is a decision that must be made before any R-24/R-30 work)

**Scenario.** The obvious "Mongo-native" design is: ingest writes to Mongo, and
the WS layer subscribes to a change stream so any number of reader processes
stay live. I tested this against the mongod that this environment actually
produces — the `mongo:7` container started by a peer researcher this run,
launched the ordinary way (`docker inspect` shows `Cmd: ["mongod"]`, no
`--replSet`):

```
db.watch()  ->  Location40573: The $changeStream stage is only supported on replica sets   [measured]
db.hello()  ->  isWritablePrimary=true  setName=NONE  version=7.0.39                       [measured]
```

Change streams therefore require `mongod --replSet rs0` **plus** a one-time
`rs.initiate()`. That init step has a total failure mode: a `--replSet` mongod
that was never initiated rejects *reads as well as writes* (`NotYetInitialized`),
so a container recreated without the init leaves Touch with no live view at all
— strictly worse than today, where the live view depends only on a local file.

There is also no benefit to buy with that risk. Per D8/G5 (`inception.md:226-228`,
`touch-monitor-spawn-plan.md:39-44`) Touch v0 is **one asyncio process** that
both ingests and serves. A change stream would deliver back to that process
exactly the documents it just wrote, one network round trip later, and would
introduce a *third* cursor space (resume tokens) alongside GD-11's
`(stream, seq)` and the tailer's byte offsets.

**Recommendation.** State as a new global decision (suggested **GD-21**):

> The live path is **memory-authoritative**. The aggregator's in-memory
> reduction is the single source for `/ws` and the read API. MongoDB is a
> **write-behind mirror** used for durable history, cross-restart backfill and
> queries. Change streams are **not adopted**; a standalone (non-replica-set)
> mongod is a supported deployment. If a second reader process is ever needed,
> it polls `(stream, seq)` over the indexed cursor of LIVEFLOW-3 — not a change
> stream, not a tailable cursor (tailable requires a capped collection, which
> the main store cannot be).

Consequence to state in the same decision: **when Mongo is down or absent, the
live view is fully functional.** Only history/backfill degrades. Test:
`test_e2e_sim.py` arm with no mongod reachable — sessions, agent rows, loop
cards and token counters all still update; `/health` reports
`mirror: "degraded"`.

---

## LIVEFLOW-2 — A BSON sub-document `_id` silently defeats deterministic persistence; the ref-union shapes in GD-11 are exactly the shape that breaks
**File**: `touch-full-recon-plan.md:155-160` (GD-11 ref union)
**Severity**: blocker

**Scenario.** GD-11 defines the ref union as sub-documents:
`{uuid} | {toolUseId} | {agentId} | {runId,key,ordinal} | {pid,procStart}`.
The natural Mongo translation of "persist deterministically" is to make the ref
(or `{stream, seq}`) the `_id`, so a re-ingest upserts instead of duplicating.
BSON sub-document equality is **field-order sensitive**, and Mongo does not warn:

```
insertOne({_id:{stream:"s1",seq:1}, ...})
findOne({_id:{stream:"s1",seq:1}})  ->  the document
findOne({_id:{seq:1,stream:"s1"}})  ->  null                                   [measured]
insertOne({_id:{seq:1,stream:"s1"}, ...})  ->  ACCEPTED; countDocuments() == 2 [measured]
```

So the same logical event stored under a differently-ordered key is a *second
document*, not a duplicate-key error. This will happen the first time two code
paths build the ref literal in different field order — for `{runId,key,ordinal}`
and `{pid,procStart}` that is a near-certainty across `ingest.py`, `legacy.py`
and `agents.py` (GD-15's separate modules). The failure is silent: no error,
duplicated events, double-counted rollups, an agent that appears twice in the
tree.

**Recommendation.** Amend GD-11 with a persistence clause:

> The ref union stays the *logical* identity. Its **storage key is always a
> scalar string** produced by one shared `ref_key(ref)` helper with a fixed,
> tested field order, e.g. `run:<runId>|<key>|<ordinal>`, `agent:<agentId17>`,
> `sess:<pid>-<procStart>`, `rec:<uuid>`. Sub-documents are never used as `_id`
> or as an equality-match key.

Test (R-24): `ref_key` is order-insensitive on its input dict and round-trips;
a fixture asserting no `_id` in any collection is of BSON type `object`.

---

## LIVEFLOW-3 — The `(stream, seq)` cursor becomes a COLLSCAN if it is stored inside `_id`; the indexed forms are proven
**File**: `touch-full-recon-plan.md:161-163` (GD-11 seq/cursor), `:714-724` (R-31 cursors)
**Severity**: major

**Scenario.** GD-11 mandates `(stream, seq)` cursors and R-31 builds
`/api/events?session=&after=` on them. If the pair lives inside a composite
`_id`, the natural query does not use the `_id` index:

```
find({"_id.stream":"s1","_id.seq":{$gt:0}}).explain()  ->  stage: COLLSCAN      [measured]
```

Every WS reconnect and every `after=` page then scans the whole events
collection. Today that is invisible (a per-task `events.jsonl` is ~100 KB —
`touch-full-recon` is 102 220 bytes / 276 lines **[measured]**), but the whole
point of the Mongo amendment is one store across all sessions and all history.

Both indexed alternatives were verified:

```
find({stream:"s1",seq:{$gt:19000}}).sort({seq:1})       -> IXSCAN stream_1_seq_1   [measured]
find({_id:{$gt:"s1#000000019000"}})                     -> IXSCAN _id_             [measured]
tail 999 of 20 000 docs = 7 ms; full indexed replay of 20 000 = 84 ms             [measured]
```

**Recommendation.** In the amendment's store item (extends **R-24**): events
carry top-level scalar `stream` (string) and `seq` (int) **and** a scalar
`_id = "<stream>#<seq zero-padded to 12>"`. Create
`{stream:1, seq:1}` unique. The zero-padding makes lexicographic `_id` order
equal numeric `seq` order, so both cursor forms are index-served and agree.
Test: an `explain()` assertion in `tests/test_store.py` that the cursor query's
winning plan contains `IXSCAN` — a plan regression is otherwise invisible until
it is slow in production.

---

## LIVEFLOW-4 — Token **deltas** must not be the persisted representation; mirror absolute per-`(agentId, message.id)` usage documents
**File**: `decision_watcher.py:705-722` (result-time delta), `:764-795` (1 Hz live delta), `touch-full-recon-plan.md:168-171` (GD-11 tokens)
**Severity**: blocker

**Scenario.** The whole existing stream is *deltas against a watcher-private
baseline* (`state["tok_emitted"]`, held in `.watcher-state.json`, which GD-14
line 213-214 forbids Touch from reading). The page derives grand totals by
summing them (`monitoring.md:44`). Summed deltas are correct **only** if every
event is delivered exactly once. Mongo's idempotence story is
"deterministic `_id` + tolerate duplicate-key on replay" — which I verified
works for *storage*:

```
bulkWrite(2000 docs, ordered:false)             -> 37 ms
bulkWrite(same 2000, ordered:false)             -> MongoBulkWriteError, count stays 2000  [measured]
```

…but for *deltas that is exactly wrong in the other direction*: if the mirror
ever writes a delta twice under two different keys (LIVEFLOW-2's field-order
trap, a restart that re-derives seq differently, a legacy re-ingest at a new
line ordinal), the rollup silently doubles, and there is nothing in the data to
detect it. The token counter is the single most-watched number on the
dashboard and would be quietly wrong.

The absolute representation is both idempotent *and smaller*. Measured on the
`touch-full-recon` run (7 agents, `wf_930e210a-6da`):

```
distinct usage message ids across all agent transcripts : 133
quiet token-delta events in events.jsonl for the same run: 217
events.jsonl total lines 276, of which 217 (79%) are quiet token deltas        [measured]
```

Across all four task folders the quiet share is 66–90 % **[measured]**
(`touch-aggregator` 533/590, `touch-full-recon` 217/276,
`touch-mongo-live` 60/80, `touch-repo-recon` 68/103).

**Recommendation.** New item for the amendment (suggested **R-A-tokens**),
amending GD-11's tokens clause:

> Mongo stores **absolute** usage documents, one per API message:
> `_id = "usage:<agentId17>:<message.id>"`,
> `{in, out, cached, cache_write, agentId, runId, ts}`. Re-ingest is a no-op by
> construction. Per-node and per-run rollups are `$group` sums over those
> documents (GD-11's "run tokens = Σ over nodes of per-node deduped totals" is
> then true by construction, and `totalTokens` is never involved).
> **Deltas exist only on the WS wire**, computed by the aggregator from its
> in-memory absolute state for the benefit of the existing UI counter — they
> are never persisted, never replayed, never summed from storage.

This also collapses the mirror's write volume by ~40 % and removes the 1 Hz
write amplification entirely: an absolute doc is written once per API message,
not once per second per running agent.

---

## LIVEFLOW-5 — Never persist a derived verdict as authoritative: this run is *currently* fabricating a `research → failed` badge, and a durable mirror would make it permanent history
**File**: `decision_watcher.py:639-648`; `touch-mongo-live/orch-scripts/research.workflow.js:172`; `touch-full-recon/events.jsonl` (the 14:06:23 and 14:21:24 lines); `touch-full-recon-plan.md:142-152` (GD-10)
**Severity**: blocker

**Scenario — reproduced live, not hypothetical.** The watcher closes the
previous plan when a new plan's first agent spawns:

```python
# decision_watcher.py:646
st = "done" if state["decisive"].get(prev) else "failed"
```

`decisive[plan]` is only ever set from a result dict containing `passed` or
`approved` (`:689-691`). A **research** plan's agents return `{findings: …}`,
so `decisive["research"]` is never set, so `research` closes **`failed`**. In
the completed `touch-full-recon` run this produced, verbatim:

```
14:06:23.241Z  {"plan":"research","stage":"plan","state":"failed","detail":"loop exited -> synthesis"}
14:21:24.167Z  {"plan":"research","stage":"plan","state":"done",
                "detail":"all 6 researchers returned - earlier failed badge was watcher defect SKILLS-1"}
```

— a fabricated failure, then a *hand-appended corrective event* 15 minutes
later. **The same thing is queued to happen in this run**: this task's own
script `touch-mongo-live/orch-scripts/research.workflow.js` emits
`status.sh synthesis plan done` (line 172) but has no corresponding
`status.sh research plan done` anywhere, so the moment the synthesizer spawns,
`research` will be badged `failed` again. R-08/R-09 fix this forward but are
not implemented (the repo has no application source).

Why this is a *Mongo* finding: today the wrong verdict is repaired by appending
one more line to an append-only file, and any restarted monitor re-derives the
truth from the raw stream. If the amendment materialises plan/agent state into
a mutable Mongo collection, a wrong verdict becomes **durable history that is
re-served on every future page load**, with the raw evidence and the derived
lie sitting in the same database looking equally authoritative.

**Recommendation.** State as a global decision in the amendment
(suggested **GD-22**), and make it the load-bearing invariant of the whole
Mongo design:

> **Only observations are persisted as facts.** The `events` collection and the
> mirrored transcript/journal/usage collections are immutable and append-only.
> Every derived value — plan badge, agent liveness, loop stage, verdict,
> rollup — lives in a separate `derived` collection whose every document
> carries `reducerVersion` and `derivedFromSeq`. On startup, if
> `reducerVersion` differs from the running code's, the `derived` collection is
> **dropped and rebuilt by replay**, never migrated. Nothing outside the
> reducer ever writes to `derived`; no UI action, no control endpoint.

Corollary to record explicitly: the amendment must **not** be sequenced ahead
of R-08/R-09. Mirroring a stream that is known to fabricate verdicts, into a
store whose selling point is permanence, is the wrong order of work. Recommend
the amendment declare R-08 + R-09 a hard precondition of the mirror item.

---

## LIVEFLOW-6 — Liveness must be derived at read time from `now()`, never stored
**File**: `inception.md:96-100` (three-state liveness), `touch-monitor-spawn-plan.md:57-61` (G8), `monitoring.md:38` (`agent.state` in the event)
**Severity**: major

**Scenario.** The current event schema carries `agent.state: "running"` inside
the event (`monitoring.md:38`, emitted at `decision_watcher.py:636-638`). That
is honest in a *stream* — it means "was running at this timestamp" and the next
event supersedes it. Written into a document store and read back an hour later,
the same field asserts "is running", which is a lie whenever the writer died.

This is already visible in the corpus: GD-14 line 204-206 exists precisely
because `touch-repo-recon` left seven agents frozen in `running` (R-27's test
asserts they close `stale`). With a durable mirror, every such frozen row
becomes a permanent claim rather than a stream artefact, and GD-10's rule
("no complete event + journal quiet ⇒ **unknown**, never running",
`touch-full-recon-plan.md:151`) has nowhere to be enforced.

**Recommendation.** In the mirror schema (extends **R-24**/**R-28**): agent and
session documents store **observations only** —
`{firstActivityTs, lastActivityTs, resultSeen: bool, resultTs, procCheckedAt,
procAlive}`. There is no `state` field in storage. The three-state liveness
(`running` / `finished` / `unknown ≥180 s idle`, `inception.md:98-100`) is
computed in the reducer at read time from `now() - lastActivityTs`. Add a
`writers` heartbeat document (see LIVEFLOW-11) so that when the aggregator
itself is dead the UI renders "unknown — aggregator offline for N s" instead of
a frozen `running`. Test: a fixture whose last observation is 10 minutes old
renders `unknown`, and the *same* fixture read with a faked `now()` inside the
window renders `running` — proving the value is derived, not stored.

---

## LIVEFLOW-7 — A backfill path that re-ingests an old journal with `live=True` writes today's clock into permanent history
**File**: `decision_watcher.py:305-327` (`result_ts`), `:563-576` (`caught_up`)
**Severity**: major

**Scenario.** `result_ts(agent_id, live)` deliberately returns **`now()`** when
tailing live and the transcript's last line is older than 30 s — because the
journal has no timestamps and a transcript can stop flushing mid-run
(`decision_watcher.py:310-314`). The guard that keeps this honest is the
`caught_up` flag (`:568`, `:576`, `:730`), which is `False` only for the very
first chunk after startup.

A Mongo backfill item ("mirror the archived runs / re-ingest a task folder")
that reuses this code without forcing `live=False` will stamp **every historical
result with the moment of the backfill**. In an append-only text file that
would be an obvious anomaly a human notices; in a document store it becomes the
`ts` that sorts, filters and renders forever, and it is not recoverable — the
true timestamp is not stored anywhere else.

**Recommendation.** In the amendment's backfill item: backfill is a **distinct
entry point** that hard-codes `live=False` and additionally refuses to write
any `ts` newer than the source file's `mtime`. Every backfilled document
carries `ingestMode: "backfill"` next to GD-14's `derived_from_legacy`. Test:
backfill a fixture journal whose transcripts are dated 2026-07-25T03:00Z and
assert no stored `ts` is within 24 h of the test's `now()`.

---

## LIVEFLOW-8 — "Deterministic persistence" needs *two different* key rules, because transcripts are rewritten and `events.jsonl` is not
**File**: `inception.md:69-80` (transcripts NOT append-only), `touch-full-recon-plan.md:161-163` (seq per event-log file), `status.sh:28`
**Severity**: major

**Scenario.** The user's request is "persist the session jsonl into MongoDB
deterministically". The obvious key — position in the file — is right for one
source and wrong for the other:

- `<task>/events.jsonl` is written only by `status.sh:28` (`>>`, O_APPEND) and
  `decision_watcher.py:150-151` (`open(..., "a")`). It is genuinely append-only
  and never rewritten, so **line ordinal is a stable identity**, and a
  re-ingest from offset 0 (which R-23's checkpoint rule mandates on inode
  change or shrink) reproduces exactly the same `_id`s. Deterministic.
- Harness transcripts are explicitly **not** append-only:
  `performRemoveByUuid` truncates and rewrites the tail and
  `performCompactTranscript` rewrites the whole file via tmp+rename
  (`inception.md:70-73`). A line ordinal there shifts under every rewrite, so
  keying on it produces a *fresh duplicate of every surviving record* on the
  next full re-ingest. Non-deterministic, and the corruption grows with each
  compaction.

**Recommendation.** State the rule per source, once, in the amendment:

| source | storage `_id` | why |
|---|---|---|
| legacy `events.jsonl` | `"legacy:<task>#<lineOrdinal:012d>"` | append-only; ordinal is stable |
| session/agent transcript records | `"rec:<uuid>"` | globally unique, survives rewrite (`inception.md:73-75`) |
| workflow journal entries | `"run:<runId>#<lineOrdinal:012d>"` | append-only within a run |
| usage rows | `"usage:<agentId17>:<message.id>"` | LIVEFLOW-4 |
| workflow nodes | `"node:<runId>|<key>|<ordinal>"` | GD-7 |

Second-order consequence worth writing down: because transcripts can have
records *removed*, Mongo becomes a **superset** of the current file. That is
intentional — inception.md:117-119 says Touch must own its history because the
CLI's retention sweep deletes it — but the UI must not present a removed record
as current. Recommend a per-`(session, generation)` marker bumped whenever the
tailer detects an inode change/shrink, and records not seen in the newest
generation rendered as `retracted` (D13 honesty), not silently shown.

---

## LIVEFLOW-9 — Mongo must never be awaited inside the ingest/poll loop, and the JSONL store must stay the primary durable log
**File**: `monitor_server.py:449-458` (the existing precedent), `touch-full-recon-plan.md:618-627` (R-24), `:749-761`
**Severity**: major

**Scenario.** The monitoring module already learned this lesson: `/tasks` and
`/artifacts` were moved onto `asyncio.to_thread` explicitly "so they never
stall live WS streams (SERVER-5)" (`monitor_server.py:450-451`). A Mongo write
in the 250 ms ingest tick reintroduces the same class of stall with a worse
blast radius — a paused container, a full disk, an election, or a `docker pause`
turns a 0.6 ms insert into an unbounded wait, and the live view (which per
LIVEFLOW-1 does not need Mongo at all) freezes anyway.

Note also that **pymongo is a blocking driver**: a synchronous `insert_one` in
an asyncio process stalls the entire event loop, including every WS client.

**Recommendation.** In the amendment's mirror item:

- The `.touch/` JSONL store (R-24) **remains the primary durable log**. Mongo is
  downstream of it, never in front of it. Nothing is lost when Mongo is absent.
- Mirror writes go through a **bounded `asyncio.Queue`** drained by one worker
  task. On queue-full, **drop mirror writes** (never live frames), increment a
  counter, and set `mirror: "degraded"` in `/health` (R-30 already reports
  per-tailer liveness and parse-failure counters, `:707-709` — extend it).
- Reconnect/backfill is watermark-driven: query `max(seq)` per stream, replay
  the JSONL from there. Because keys are deterministic (LIVEFLOW-8) an
  over-eager replay is a no-op.
- Driver choice must be named explicitly: **`pymongo`'s async API
  (`AsyncMongoClient`)**, not Motor — Motor was deprecated 2025-05-14 in favour
  of the GA PyMongo Async API and reached end of life 2026-05-14. If a
  synchronous client is used instead, every call must be wrapped in
  `asyncio.to_thread`, and the plan must say so.

---

## LIVEFLOW-10 — Publish the latency budget as acceptance numbers; the poll interval is the budget, not the database
**File**: `decision_watcher.py:796`, `monitor_server.py:378`, `inception.md:83-88`, `:226-228`
**Severity**: major (it is the mandate's decide-input, and it is currently written nowhere)

**Scenario.** `monitoring.md:176-178` claims "live events lag ≤1 s (poll
interval)", which is wrong on two counts as coded: the watcher's 1 s poll
(`decision_watcher.py:796`) and the server's 0.5 s tail
(`monitor_server.py:378`) compose to **1.5 s**, plus the 100 ms transcript
flush. Nothing anywhere states what Touch's number must be, so "did the DB sink
break the live flow?" has no answer to test against.

**Recommendation.** Put this table in the amendment as **acceptance criteria**,
not prose:

| budget line | value | source |
|---|---|---|
| transcript block flush | 100 ms | `inception.md:83-88` |
| Touch tailer poll (stat-first) | 250 ms | D8, `inception.md:228` |
| aggregator reduce + WS push | ≤ 50 ms | new |
| **end-to-end, agent action → pixel** | **≤ 400 ms p95, ≤ 1 s p99** | new |
| Mongo mirror write, off the critical path | 0.61 ms (`w:1`) / 1.65 ms (`j:true`) | **[measured]** |
| Mongo mirror allowed to add to the above | **0 ms** (async, bounded queue) | LIVEFLOW-9 |

And one deliberate regression to record: today's watcher emits live token ticks
every 1 s (`decision_watcher.py:764`); Touch at 250 ms must **not** emit 4×
more token frames. Coalesce token updates to ≥ 1 s on the wire while ingesting
at 250 ms — the counter is a human-readable number, not a metric feed.

---

## LIVEFLOW-11 — Deterministic `_id` + "tolerate duplicate key" silently hides a real double-writer bug; add a writer lease
**File**: `touch-full-recon-plan.md:621` (R-24 "single writer per stream"), `:161-163`
**Severity**: major

**Scenario.** LIVEFLOW-4/8 require tolerating `E11000 duplicate key` as success
so replay is idempotent — I confirmed the error is what you get
(`MongoBulkWriteError`, count unchanged **[measured]**). But "duplicate key" is
*also* the signature of the one failure R-24's "single writer per stream" rule
is meant to prevent: two aggregator processes mirroring the same stream (a
stale instance the user forgot to kill, a second `python3 aggregator/server.py`,
a sandbox restart racing the old process). Both allocate the same `seq` for
*different* events; one wins, the other's event is silently discarded as a
"duplicate". Events vanish with no error anywhere.

This is not theoretical in this repo: `CLAUDE.md` documents `pkill -f
"[m]onitor_server"` as a routine operation precisely because duplicate daemons
happen.

**Recommendation.** Add to the amendment's store item: a `writers` collection,
`_id = <stream>`, holding `{holderPid, holderBoot, leaseExpiresAt}`, renewed
each tick. A process that cannot hold the lease **refuses to mirror** and says
so in `/health` (it may still serve reads). Separately, **count** tolerated
duplicate-key results and expose the counter — a healthy replay produces a
burst at startup and zero thereafter; a nonzero steady-state rate means a
second writer or a key bug. Test: two writers against one stream — the second
refuses; a replay of the first's own output produces dups but no data change.

---

## LIVEFLOW-12 — There must be exactly one reducer, server-side; the UI currently invents state the stream does not contain
**File**: `monitor.html:334-344` (freeze-on-close), `:295-299` (`NODE_STATES` whitelist), `touch-full-recon-plan.md:714-724` (R-31), `:726-738` (R-32)
**Severity**: major

**Scenario.** `monitor.html:334-344` implements a rule that exists nowhere in
the event stream: when a plan card closes with agent rows still `running`,
those rows are frozen to `stale` (and the flow-strip role nodes with them). It
is a good rule — and it is *UI-local*. The moment the amendment adds a
Mongo-backed read API (R-31 `/api/run/node?run=&agent=`), that API will answer
`running` for the identical agent, because it reduces the same events without
that rule. Page and API disagree; whichever the user looks at second looks
broken. Add the "unknown ≥180 s idle" rule (LIVEFLOW-6), which today exists in
*neither* place, and there are three inconsistent notions of agent state.

**Recommendation.** In the amendment: the reduction from events → state is
**one server-side module** (natural home: extend R-28 `agents.py` / the
`derived` collection of LIVEFLOW-5). `/api/*`, `/ws` and the page all serve or
render *that* state. The frontend renders, it never re-derives — R-32's guards
should assert the absence of state-inference in `app.js`, the same genre as the
existing `test_frontend.py` source guards. The `NODE_STATES` whitelist at
`monitor.html:299` (untrusted-input defence) stays, as validation, not as
derivation.

---

## LIVEFLOW-13 — One never-resulting sibling in a parallel fan-out wedges the run badge forever *and* pins a 1 Hz transcript re-read; this run has five such siblings
**File**: `decision_watcher.py:459-460` (`run_outcome`), `:610-616` (stale-close guard), `:764-767`
**Severity**: major

**Scenario.** `run_outcome` bails immediately while any agent is tracked
running:

```python
# decision_watcher.py:459
if not state["plans"] or state["running"]:
    return None
```

The only mechanism that ever removes a resultless agent from `state["running"]`
is the stale-close at `:610-625`, which is deliberately guarded on a **strictly
greater attempt** (`info["attempt"] <= oinfo["attempt"]` ⇒ skip, `:615`) so that
parallel siblings do not stale-close each other (DRIVER-1, per the comment at
`:606-609`). In a research fan-out every agent is `plan=research role=research
attempt=1` — verified in this run's own journal, five `started` entries at the
same plan/role/attempt **[measured]** — so if one agent dies without a result,
**nothing ever closes it**: the run badge never settles, the row ticks
`running` forever, and the 1 Hz loop keeps re-reading its transcript for the
life of the watcher.

GD-10 already legislates the answer ("no complete event + journal quiet ⇒
*unknown*, never running", `touch-full-recon-plan.md:151`) but no code path
implements the transition for this case, and R-08's scope is the plan badge, not
`state["running"]`.

**Recommendation.** In the amendment's liveness item (extends R-08/R-28): an
agent with no result and `now() - lastActivityTs > IDLE_UNKNOWN` (180 s per
`inception.md:98-100`) transitions to **`unknown`**, leaves the running set for
the purpose of run-close, and is rendered "unknown — idle N m", never
`running` and never `failed`. Test: a fan-out fixture of five same-attempt
siblings where one has no result — the run closes, the four honest rows read
`done`, the fifth reads `unknown`, and no row reads `failed`.

---

## LIVEFLOW-14 — Unbounded replay-on-connect does not survive a shared history store
**File**: `monitor.html:701-704` (rebuild on every connect), `monitor_server.py:358-368` (`offset = 0`), `touch-full-recon-plan.md:714-724` (R-31)
**Severity**: minor (today) / major (after the mirror lands)

**Scenario.** Both ends assume full replay: the server starts every WS stream
at `offset = 0` (`monitor_server.py:361`) and the page resets and rebuilds from
scratch on every (re)connect (`monitor.html:701-704`, comment: "server replays
full history on every connect — rebuild from scratch, no double counting").
That is a *correct and elegant* design against a 100 KB per-task file. Against
one Mongo collection spanning every session and every run it is unbounded: the
first paint cost grows monotonically with the age of the installation, and a
flaky connection re-pays it on every reconnect.

Server-side cost is not the problem (20 000 docs replay in 84 ms **[measured]**)
— the wire and the DOM are.

**Recommendation.** R-31's cursors get a **bounded default window** (e.g. the
current run, or the last N events, whichever is larger) with an explicit
`?from=` for older data and a "load older" affordance in R-32. The reconnect
path must resume from the client's last `(stream, seq)` rather than restarting
at 0 — and because the page's counters are sums, resume must be paired with the
absolute-token model of LIVEFLOW-4, otherwise a partial replay yields a partial
total. These two findings are a package; implementing resume without absolute
tokens produces silently low counters.

---

## LIVEFLOW-15 — The 1 Hz full-transcript re-parse scales with the run, not with the delta; carry a measured acceptance number
**File**: `decision_watcher.py:154-197` (`agent_tokens`), `:86-100` (`agent_paths` glob), `:764-795`; `touch-full-recon-plan.md:268-270` (GD-20 "do not inherit")
**Severity**: minor

**Scenario.** Every tick, for every running agent, `agent_tokens()` globs
`~/.claude/projects/*/*/subagents/workflows/<wf>/agent-<id>.jsonl` and
`json.loads` **every line of every copy**. Measured against this run's live
corpus:

```
5 agents, 844 179 bytes total  ->  3.4 ms per tick;  glob 0.08 ms per call    [measured]
```

Comfortable now. Linearly extrapolated to a full `implement-plan` run
(≈40 agents × ~2 MB transcripts) that is ≈320 ms per tick — over the entire
250 ms budget of LIVEFLOW-10, on the same thread that services journal tailing
and run-close. GD-20 already lists "1 Hz full-transcript re-parse" under
*do not inherit* and R-23 says reads are incremental, but no number is attached,
so nothing fails if the implementation regresses.

**Recommendation.** Attach the acceptance number to R-23 in the amendment:
per-tick ingest CPU must be O(bytes appended since last tick), asserted by a
test that appends 1 KB to a 20 MB fixture transcript and requires the tick to
read < 64 KB (instrument the tailer's byte counter — do not time it, timing
tests flake). The absolute-usage model of LIVEFLOW-4 makes this natural: a new
usage doc is produced only from newly-read bytes.

---

## LIVEFLOW-16 — Mongo must never stamp time, and `$natural` is not an ordering
**File**: `touch-full-recon-plan.md:163-164` (GD-11 ts rule), `decision_watcher.py:141`
**Severity**: minor

**Scenario.** GD-11 already says "order = file line order, never ts sort", and
the writer emits one `ts` format. Mongo makes two tempting violations
available: `ObjectId` embeds a generation timestamp (from the *client*, and
only second-granular), and inserted documents *appear* to come back in
insertion order. My probe did return `$natural` order 1,2,3 even after an
update that grew a document 5000× **[measured]** — but that is an artefact of
WiredTiger's current behaviour, not a contract, and it is exactly the kind of
observation that becomes a load-bearing assumption.

There is a second-order hazard specific to this environment: the mongod runs in
a container with its own clock view, so a server-side `new Date()` and the
aggregator's `datetime.now(timezone.utc)` are two different clocks that will
disagree under drift.

**Recommendation.** One line in the amendment: **the aggregator supplies every
`ts`; the server never generates one** (no `$currentDate`, no relying on
`ObjectId` time, no `_id: ObjectId()` where a deterministic key is required per
LIVEFLOW-2). Ordering is `(stream, seq)` only; `$natural` and `_id`-as-time are
never used for ordering. Test: a fixture inserted out of `ts` order still
replays in `seq` order.

---

## LIVEFLOW-17 — Backfill frames must be marked so the UI does not animate a finished run as if it were live
**File**: `monitor.html:254-263` (`replaying` pulse suppression), `:47-48` (`.badge.running` animation), `:74`, `:84`
**Severity**: minor

**Scenario.** The page already suppresses token-counter pulse animations during
a replay burst (`monitor.html:254-263`) — good instinct, partial coverage: the
running badge's spinner and the chip/dot blink animations (`:47-48`, `:74`,
`:84`) still fire during replay. Today that is a 300 ms cosmetic flicker on
connect. Once Mongo enables "open a run from three days ago" and "backfill an
archived task folder while you watch", a *completed* run visibly spins up,
blinks through its stages and settles — a user cannot tell a historical replay
from a live run.

**Recommendation.** Frames carry `live: true|false` (or the WS stream declares
its mode at handshake and switches once, at the replay→tail boundary). Replayed
frames are reduced without animation and painted once at the end of the burst;
only `live` frames animate. Extend the existing `replaying` mechanism rather
than adding a second one, and extend R-32's source guards to assert no
animation class is applied on a non-live frame.

---

## LIVEFLOW-18 — "Attempt N of MAX / which stage next" has no source unless R-19's topology is mirrored; without it the UI must say so
**File**: `touch-full-recon-plan.md:540-559` (R-19), `decision_watcher.py:104-109` (caps from `orch-config.json`), `monitor.html:289` (`p.roles` flow strip)
**Severity**: minor

**Scenario.** The mandate asks for loop shape — "attempt 2 of 4, next stage
critique". Neither the journal nor the events stream contains it. The watcher
reads the caps from `orch-config.json` (`decision_watcher.py:104-109`, defaults
4/3/3) purely to *narrate* retry decisions; the page's flow strip
(`monitor.html:289`) infers role order from first-seen order. R-19 is the item
that would make the shape deterministic (`state/topology.json` with
`max_attempts`, `phases[]`, `expected_stages[]`) — and R-19 is unimplemented,
and the shipped templates (including this run's own
`touch-mongo-live/orch-scripts/research.workflow.js`) write no topology.

If the mirror stores loop cards without the topology, "attempt 2" renders with
an invented denominator (the default 4) that may not be the run's actual cap —
a D13 honesty violation in the most prominent widget on the page.

**Recommendation.** Mirror `topology.json` as a document keyed by `runId`
(`_id = "topo:<runId>"`), written by R-19's templates. **Where it is absent,
the UI renders "attempt N" with no denominator and no next-stage arrow** — the
GD-14/D13 pattern of labelling degraded knowledge rather than filling it in.
Make the absent-topology arm a required test case, because every legacy folder
and every run predating R-19 will take it.

---

## LIVEFLOW-19 — The mongod this environment produces has **no authentication and is published on 0.0.0.0**, while the data being mirrored is unredacted transcripts
**File**: `inception.md:117-119`, `:188-194`; `touch-full-recon-plan.md:184-194` (GD-13)
**Severity**: blocker

**Scenario.** GD-13 is unusually strict for a dev tool — 127.0.0.1 by default,
a per-boot 256-bit token on every route, an Origin allowlist at WS upgrade —
and `inception.md:190-192` gives the reason: *"transcripts hold unredacted
secrets and controls are command execution."* The Mongo container running in
this sandbox right now (started by a peer researcher this run, and the shape
anyone gets from a plain `docker run mongo:7`) is:

```
docker inspect:  Cmd ["mongod"]           (no --auth, no --replSet)
                 PortBindings 27017/tcp -> HostIp:"" HostPort:"27017"   = 0.0.0.0
mongosh:         admin.system.users.countDocuments() == 0                        [measured]
                 anonymous connect from the sandbox succeeds                     [measured]
```

So the amendment as-requested would take the exact data GD-13 protects behind a
token and copy it into an unauthenticated service listening on every interface
of the sandbox — one `sbx ports … 27017` (or any co-resident process) away from
being readable by anything that can reach the box. Every control the plan
carefully specified is bypassed by reading the mirror directly.

Two further container-level hazards, same probe:

- `/data/db` is an **anonymous volume**
  (`/var/lib/docker/volumes/5bd0a1fc…/_data` **[measured]**). `docker rm` of the
  container orphans it, and the store that exists *specifically because the CLI's
  retention sweep deletes `~/.claude`* (`inception.md:117-119`) is itself
  ephemeral and unnamed.
- `mongod` is not installed on the host (`command -v mongod` → not found
  **[measured]**), so Docker is the only delivery route and the container's
  lifecycle is now part of Touch's availability story.

**Recommendation.** The amendment must carry a security clause of its own,
suggested **GD-23**, before any mirror code:

> The mirror connects to a mongod that (a) binds **127.0.0.1 only** — publish
> `127.0.0.1:27017:27017`, never `0.0.0.0` — and (b) runs with `--auth` and a
> dedicated least-privilege user scoped to the Touch database, whose credentials
> live in `.touch/` at mode 0600 (the same handling GD-13 gives the server
> token at `touch-full-recon-plan.md:786-789`) and never in a committed file or
> a `docker run` command line. `/data/db` is a **named** volume or a bind mount
> under a documented path, and `.gitignore` covers it (extends **R-01**).
> Touch refuses to start the mirror against a mongod that reports zero
> configured users, and says why.

Also record the D8 amendment explicitly, since the user's request forces it:
D8's "stdlib-only at runtime" (`inception.md:177-181`) is broken by `pymongo`
(4.17.0 is available through the proxy **[measured]**; nothing is installed
today). Per `inception.md:178-181` the established pattern is
*vendor pinned, committed artifacts with a sha256 manifest* — the amendment
should either apply that pattern to `pymongo` or state plainly that D8 now
reads "stdlib-only **except** the pinned Mongo driver", and that **Touch must
run with full functionality when the driver is absent** (LIVEFLOW-1 makes this
cheap: no driver ⇒ no mirror ⇒ live view unaffected).

---

## Summary of decide-inputs the synthesizer asked for

1. **Memory or Mongo for the live path?** → **Memory.** Change streams need a
   replica set that this environment's mongod is not (verified); the v0 server
   already holds the reduction; a change stream would only echo its own writes
   back. Mongo = write-behind mirror. (LIVEFLOW-1)
2. **Change streams or polling?** → **Neither, in v0.** If a second reader
   process ever appears, poll the indexed `(stream, seq)` cursor — 7 ms for a
   999-doc tail out of 20 000 (measured). (LIVEFLOW-1, LIVEFLOW-3)
3. **What happens when Mongo is down?** → **Live view is unaffected**: JSONL
   stays primary, mirror writes go to a bounded queue, `/health` reports
   `mirror: degraded`, UI shows a banner, backfill resumes from the per-stream
   `max(seq)` watermark. (LIVEFLOW-9)
4. **How do loop cards / agent rows / token counters stay truthful across
   backfill and live tail?** → Persist **observations only, never derived
   state** (LIVEFLOW-5); persist **absolute** usage docs, deltas only on the
   wire (LIVEFLOW-4); derive liveness at read time from `now()` (LIVEFLOW-6);
   force `live=False` on backfill (LIVEFLOW-7); mark replayed frames so nothing
   animates (LIVEFLOW-17); one server-side reducer (LIVEFLOW-12).
5. **Latency budget** → 100 ms flush + 250 ms poll + ≤50 ms reduce/push ⇒
   ≤400 ms p95; Mongo contributes 0 ms because it is off the critical path
   (0.61 ms `w:1` when it is on it). (LIVEFLOW-10)
6. **Ordering / sequencing** → aggregator-supplied `ts`, `(stream, seq)` only,
   scalar string keys, indexed compound. (LIVEFLOW-2, -3, -8, -16)
7. **Hard precondition** → R-08/R-09 land before the mirror. This run is
   *currently* fabricating a `research → failed` badge; mirroring that into a
   permanent store is the wrong order of work. (LIVEFLOW-5)

**Findings: 19** (blockers: LIVEFLOW-1, -2, -4, -5, -19).
