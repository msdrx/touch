# research — sessionjsonl perspective (attempt 1)

**Scope:** the Claude Code session data layer *as an ingestion source for a
database sink*, re-verified LIVE on this machine (CLI 2.1.220, session
`292fc08c-923d-4ab4-8ff2-a9572417dbc8`, 6 transcripts / 4 workflow runs /
15.85 MB of on-disk session data). Every claim below was measured this run, not
inherited from `touch-full-recon-plan.md`.

**Amendment posture:** `touch-full-recon-plan.md` is normative. This report
states, per finding, whether R-23 (tailer) / R-25 (sessions) / R-26 (ingest)
**transfer unchanged**, **transfer with an added clause**, or **change** when the
sink is MongoDB. Findings that merely restate the plan are omitted.

---

## Measured ground truth (the substrate the contract rests on)

Real tree on this machine (`~/.claude/projects/-home-laniakea-Projects-touch/`):

```
<sessionId>.jsonl                                  main transcript
<sessionId>/workflows/<runId>.json                 run snapshot   (terminal only)
<sessionId>/subagents/agent-<17hex>.jsonl          Agent-tool spawns  (+ .meta.json)
<sessionId>/subagents/workflows/<runId>/           Workflow spawns
        agent-<17hex>.jsonl
        agent-<17hex>.meta.json                    (may be MISSING — see -3)
        journal.jsonl
<sessionId>/tool-results/<9-char>.txt              spilled tool output
```

Both `subagents/` layouts are real and coexist: `dd469822.../subagents/` holds
two flat `agent-*.jsonl` (`agentType:"general-purpose"`, with
`description`+`toolUseId`) **and** `subagents/workflows/wf_829e6f58-b2f/`
(`agentType:"workflow-subagent"`, no description, no toolUseId).

The writer (extracted from the 2.1.220 binary, class at byte offset 259266xxx):
`FLUSH_INTERVAL_MS = 100` (→ `10` when remote persistence is on),
`MAX_CHUNK_BYTES = 104857600`, `xI = 65536`, metadata re-append at `xI/2` =
32768 bytes, compaction backstop `tbr = 20971520` gated by `Z2o` (default
`false`, enabled via `EVs(kCm())` on the SDK resume path), tombstone full-rewrite
fallback limit `ZF_ = 52428800`, alias file `Esp = ".session-aliases"`.

---

## SESSIONJSONL-1 — 28 % of main-transcript records have **no `uuid`**; GD-11's `{uuid}` ref gives them no primary key

**file:line:** `/home/agent/.claude/projects/-home-laniakea-Projects-touch/292fc08c-923d-4ab4-8ff2-a9572417dbc8.jsonl:1`
**severity:** blocker

**Scenario.** Measured on the live current transcript: 267 lines, 193 carry
`uuid`, **74 do not** (27.7 %). The uuid-less types and their first lines:
`mode` (:1, ×17), `permission-mode` (:2, ×17), `file-history-snapshot` (:3, ×5),
`last-prompt` (:16, ×16), `file-history-delta` (:45, ×3),
`queue-operation` (:65, ×10), `frame-link` (:172, ×1), `ai-title` (:182, ×5).
Eight of the twelve record types have no uuid at all. GD-11's ref union leads
with `{uuid}` and R-26 keys records by it; a Mongo `records` collection with a
unique index on `uuid` therefore drops or collides on more than a quarter of
every transcript. The file store never noticed because it appends blindly.

Worse, these types split into **two different semantics** that a DB must model
differently, and the file store conflates them:

* **Idempotent last-wins state**, re-appended verbatim every ≥32 KiB by
  `reAppendSessionMetadataAsync` (see the byte-offset series for `mode`:
  `0, 45234, 119223, 162166, …, 794755`; deltas 33.6 KB–138 KB, all ≥ `xI/2`):
  `mode` 17 copies / **1 distinct value**, `permission-mode` 17 / 1,
  `ai-title` 5 / 1. Inserting these produces 39 duplicate documents whose only
  truth is "the last one wins".
* **Genuine per-event log lines**: `last-prompt` 16 copies / **16 distinct**,
  `queue-operation`, `frame-link`, `file-history-delta`. These must all be kept.

None of these types carry a `timestamp` either (`mode`, `permission-mode`,
`ai-title`, `last-prompt` have exactly the keys shown at :1/:2/:16/:182), so
they cannot even be ordered by time — only by file position.

**Recommendation.** Amend **GD-11** (ref union) and **R-26**: define the mirror
`_id` for a transcript record as a two-arm rule, stated once —

* records where `type ∈ {user, assistant, attachment, system}` → `_id = uuid`
  (these are exactly `Fse(e)` in the binary: the four types the harness itself
  treats as uuid-bearing);
* everything else → `_id = {sessionId, type, lineOrdinal}` for log-like types
  and an **upsert on `{sessionId, type}`** into a separate `session_state`
  collection for `mode` / `permission-mode` / `ai-title` / `custom-title` /
  `tag` (last-wins, one doc per session per type).

Add a new R-item for `session_state`; do **not** let last-wins state into the
records collection. Record `lineOrdinal` (and `byteOffset`) on every mirrored
record — GD-11 already says "order = file line order, never ts sort", and with a
DB sink that ordering must become an explicit stored field, not an implicit
consequence of append order.

---

## SESSIONJSONL-2 — the transcript is **not append-only**: `performRemoveByUuid` truncates in place, so an upsert-only mirror accumulates ghost records forever

**file:line:** `/home/agent/.local/share/claude/versions/2.1.220:259275213` (`performRemoveByUuid`)
**severity:** blocker

**Scenario.** Verbatim from the binary: `performRemoveByUuid(e,t)` opens the
session file `r+`, reads the last `min(size, xI=65536)` bytes, finds the **last**
`"uuid":"<t>"`, then `await n.truncate(A)` and rewrites the bytes after the
removed line. If the uuid is older than the 64 KiB tail window it falls back to
`readFile` → `split` → `filter` → whole-file rewrite (skipped with a warning
only above `ZF_ = 50 MiB`). Callers: `Dfn(e)` and `LVs(e)` (guarded by a
membership check) — the rewind / remove-queued-message paths. Separately
`performCompactTranscript` rewrites the whole file once `bytesSinceCompact ≥
tbr = 20 MiB`; it is gated by `Z2o`, which is `false` at module init but is set
at runtime by `EVs(kCm())` on the resume/hydrate path, so it is reachable.

R-23's checkpoint identity `(st_dev, st_ino, size, offset)` with
"`size < offset` ⇒ full idempotent re-ingest from 0" correctly *detects* both
events — that part **transfers unchanged and is exactly right**. But
"idempotent re-ingest" in a file store means "rewrite the derived file"; in a DB
it means "upsert every record you see". **Upsert never deletes.** A record the
harness tombstoned stays in Mongo forever, and Touch renders a message the user
explicitly rewound away — a correctness *and* a trust failure, since GD-4/GD-13's
honesty rules are the product's premise.

**Recommendation.** Amend **R-23/R-26** with an explicit deletion protocol, and
say so in the amendment as a named disposition (the file-based plan had no
reason to have one):

* every full re-ingest of a source file runs under a monotonically increasing
  `ingestGeneration` per `(sourceFile)`;
* the re-ingest upserts with `$set: {gen: G}`, then issues
  `deleteMany({source: <file>, gen: {$lt: G}})` — mark-and-sweep;
* incremental (append-only) ticks do **not** bump the generation and never
  delete;
* the sweep is the *only* delete path, so a partially-failed re-ingest cannot
  silently truncate the mirror.

Also record `size < offset` as the *shrink* trigger explicitly — inode identity
alone does not catch `truncate()`, because `performRemoveByUuid` keeps the same
inode.

---

## SESSIONJSONL-3 — one agent's transcript is **split across two session directories** by `/clear`; the second fragment is stitchable only via `parentUuid`, has a rewritten `sessionId`, and no `.meta.json`

**file:line:** `/home/agent/.claude/projects/-home-laniakea-Projects-touch/e423cd3c-f859-45af-9afd-0d6bdec9b4ac/subagents/workflows/wf_829e6f58-b2f/agent-a2fc883c96ff7b837.jsonl:1`
**severity:** blocker

**Scenario.** Measured, not hypothesised. Run `wf_829e6f58-b2f` was started under
session `dd469822`. At `2026-07-25T03:16:16.916Z` the user ran `/clear`
(`e423cd3c….jsonl:4`, `<command-name>/clear</command-name>`), which opened a new
sessionId `e423cd3c` **in the same process**. The in-flight agent
`a2fc883c96ff7b837` was still running. Result on disk:

| artifact | lives under |
|---|---|
| `journal.jsonl` for `wf_829e6f58-b2f` (14 records, mtime 03:26:40 — *after* the `/clear`) | `dd469822/` |
| 6 agent transcripts | `dd469822/` |
| tail of agent `a2fc883c96ff7b837` (2 lines) | `e423cd3c/` |
| a *new* agent `a2ed16d57db0e9887` (451 KB) spawned into the same run | `e423cd3c/` |
| `workflows/wf_829e6f58-b2f.json` snapshot | `e423cd3c/` |

The two halves of agent `a2fc883c96ff7b837` are **223 lines in `dd469822`** and
**2 lines in `e423cd3c`**. The stitch is exact and verifiable:
`e423cd3c/…/agent-a2fc883c96ff7b837.jsonl:1` has
`parentUuid = 22745683-c12a-4514-be89-bcc05c5435ce`, which is precisely the
`uuid` of the **last** record of `dd469822/…/agent-a2fc883c96ff7b837.jsonl`.
Critically, the fragment's own `sessionId` field says `e423cd3c…` — the harness
rewrote it. And the fragment has **no `agent-a2fc883c96ff7b837.meta.json`**;
`agentType` / `model` for that agent exist only in the `dd469822` copy.

Consequences for a DB mirror, none of which the plan states:

1. **`sessionId` is not a valid grouping key for agent records.** Grouping by it
   silently splits one agent into two agents, one of which has 2 messages and
   looks like a crashed stub. Agent identity must be the 17-hex `agentId` (GD-7
   already says this — but R-25's "historical arm keyed `sessionId`" and R-26's
   per-session ingest must be amended to say the agent *document* is assembled
   across sessions).
2. **A run's artifacts are not confined to one session directory.** R-26 already
   handles this for the *snapshot* ("never the launching session — it lands under
   the session current at run END"); it does **not** handle it for the
   `journal.jsonl` or for agent transcripts, and here the journal stayed with the
   *old* session while new agents went to the *new* one. Run discovery must be
   `glob(~/.claude/projects/*/*/subagents/workflows/<runId>/)` — plural — and
   the run document must carry a `sessionIds: [...]` array, not a scalar.
3. **`.meta.json` is optional.** Any ingest that reads it unconditionally throws
   on this exact file. `agentType`/`model`/`description`/`toolUseId`/`spawnDepth`
   must be nullable, and when two fragments disagree the one that *has* the meta
   wins.

**Recommendation.** Amend **R-25** (sessions) and **R-26** (ingest):
per-source checkpoint identity is **per file**, never per session; the agent
document is keyed `agentId` with `fragments: [{sessionId, path, firstUuid,
lastUuid, lineCount}]` ordered by the `parentUuid → uuid` chain; message order
within an agent is the chain order, not the concatenation order of the
directory listing. Add a fixture for exactly this pair of files (they are on
disk right now, 223 + 2 lines) — GD-18 requires fixtures before features.

---

## SESSIONJSONL-4 — `ordinal` in `(runId, key, ordinal)` is never defined for Workflow nodes; the empirical rule is journal occurrence index, and there is a live 3-key retry specimen

**file:line:** `/home/agent/.claude/projects/-home-laniakea-Projects-touch/e423cd3c-f859-45af-9afd-0d6bdec9b4ac/subagents/workflows/wf_455b348c-e17/journal.jsonl:9`
**severity:** major

**Scenario.** GD-7 (`plan:110`) and GD-11 (`plan:156`) make `(runId, key,
ordinal)` the Workflow node identity, and R-28 (`plan:684`) builds the graph on
it — but `ordinal` is defined **only for the legacy adapter** (`plan:198`,
"per-`(plan,stage)`"). For Workflow nodes it is undefined. A DB needs it as part
of the shard/unique key on day one; you cannot add it later without a migration.

The rule is derivable and there is a specimen. `wf_455b348c-e17`'s journal has
9 `started` and 2 `result` records, and **three keys appear twice** with
*different* agentIds:

| key (short) | 1st agentId (line) | 2nd agentId (line) |
|---|---|---|
| `b1a88af8253f` | `abc69d2e545b15f8c` (2) | `a434fe01631f31f97` (11) |
| `6e1d7b33b480` | `a36d6a70098b1253c` (4) | `abf33f08ab9c57483` (9) |
| `8e65e3592dd1` | `a4fa0e2d12f51a2c7` (5) | `a446960c3cb4c3ce8` (10) |

The two keys that **did** produce a `result` (`862952691534` at :6,
`93b2ef39ec58` at :7) were *not* re-run. This is memoised replay: the run was
aborted (`wf_455b348c-e17.json` → `status: "killed"`,
`error: "Error: Workflow aborted"`), restarted under the same `runId`, and the
engine re-executed exactly the nodes whose keys had no cached result. So:
**`key` is stable across replays, `agentId` is not.** `agentId` alone as the node
key makes a retried node look like a brand-new sibling — which is exactly the
"deterministic loop" the user wants rendered correctly.

**Recommendation.** Amend **GD-7** with the derivation, verbatim:
`ordinal` = 0-based count of preceding `{"type":"started"}` records with the
same `key` in the *same* `journal.jsonl`, in file line order. Store it, never
recompute it from a DB counter (concurrent ingest workers would race). Note the
inverse mapping too: `agentId → (runId, key, ordinal)` is 1:1 and is the join
key from an agent transcript back to its node.

---

## SESSIONJSONL-5 — `journal.jsonl` records carry **no timestamp**; run timing cannot come from the journal

**file:line:** `/home/agent/.claude/projects/-home-laniakea-Projects-touch/292fc08c-923d-4ab4-8ff2-a9572417dbc8/subagents/workflows/wf_cca84d59-933/journal.jsonl:1`
**severity:** major

**Scenario.** Every record in all four journals on disk has exactly one of two
shapes: `("agentId","key","type")` for `started` and
`("agentId","key","result","type")` for `result`. **No `timestamp`, no
sequence number, no duration.** The deterministic event source that GD-8 names
as *the* Workflow-profile source is ordered but untimed.

In a file store this is invisible because `events.jsonl` stamps events at
*derivation* time. In a DB the same trick is needed but must be explicit and
must be **stable across re-ingest** — if the mirror stamps `now()` on every
re-read, a full re-ingest (SESSIONJSONL-2) rewrites all timings and the UI's
durations jump. Deriving wall-clock from the *agent transcript* is possible and
better: the agent's first record's `timestamp` ≈ node start, its last record's
`timestamp` ≈ node end (agent records do carry `timestamp`).

**Recommendation.** Amend **R-26**: journal-derived node documents store
`journalSeq` (line ordinal, the ordering authority) plus `startedAt` /
`endedAt` **derived from the agent transcript's first/last record timestamps**,
with `ingestedAt` kept separate and never used for display. Explicitly forbid
`now()` as a node timestamp. GD-11's "order = file line order, never ts sort"
transfers unchanged and here becomes load-bearing rather than stylistic.

---

## SESSIONJSONL-6 — `<runId>.json` is a **terminal** artifact; a live run has none, and there is one on disk right now to prove it

**file:line:** `/home/agent/.claude/projects/-home-laniakea-Projects-touch/292fc08c-923d-4ab4-8ff2-a9572417dbc8/workflows/`
**severity:** major

**Scenario.** The run producing this very report, `wf_cca84d59-933`, has 5
`started` records, 5 agent transcripts actively growing, and **no
`workflows/wf_cca84d59-933.json`**. The only snapshot in that directory is
`wf_930e210a-6da.json` for the completed prior run. Each snapshot's own
`timestamp` equals its *end* time (`wf_930e210a-6da.json`:
`timestamp 2026-07-25T14:14:59.370Z`, `startTime 1784987796953`,
`durationMs 1102415`, `status "completed"`), and it carries only end-state
fields: `result`, `durationMs`, `status`, `error`, `totalTokens`,
`totalToolCalls`, `logs`, `workflowProgress`.

R-26 says the snapshot is "resolved by `glob(...)`" but never says what its
*absence* means. An ingest that treats a missing snapshot as an error, or that
waits for it before creating a run document, shows the user nothing for the
entire 18-minute lifetime of a research run — which is precisely the
"deterministic-loop live status" the user is asking for.

**Recommendation.** Amend **R-26** with an explicit disposition: the run
document is created from the **first `journal.jsonl` `started` record**, not
from the snapshot; missing snapshot ⇒ `runStatus` derived from journal +
liveness (see -8), never `error`; the snapshot, when it appears, is a
**back-fill only** and must never overwrite an observed non-null field with a
snapshot null (R-26 already says this — it transfers unchanged and applies here).
Add the live `wf_cca84d59-933` shape (started-without-snapshot) to the R-03
fixture set.

---

## SESSIONJSONL-7 — snapshot `agentCount` is the **distinct node count**, not the agent count

**file:line:** `/home/agent/.claude/projects/-home-laniakea-Projects-touch/e423cd3c-f859-45af-9afd-0d6bdec9b4ac/workflows/wf_455b348c-e17.json:1`
**severity:** major

**Scenario.** `wf_455b348c-e17.json` reports `agentCount: 6`. Its journal has
**9** `started` records and 9 distinct agentIds — but only **6 distinct `key`
values** (the three retries of SESSIONJSONL-4). So `agentCount` counts nodes,
not agents. Any back-fill or reconciliation that asserts
`len(agents) == agentCount` fails on this file, and any UI that renders
"6 agents" over a 9-row table is lying. (`wf_829e6f58-b2f.json` and
`wf_930e210a-6da.json` both say `agentCount: 7` against 7 started — they agree
only because nothing retried.)

**Recommendation.** Amend **R-26**'s back-fill list: `agentCount` maps to
`nodeCount`, is **display-only "harness reported"**, and is never used as a
count check — the same treatment GD-11 already gives `totalTokens` /
`totalToolCalls`. Extend GD-11's "never substituted" sentence to name
`agentCount` explicitly.

---

## SESSIONJSONL-8 — agent transcripts have **no terminal record**, and `started`-without-`result` does not mean running

**file:line:** `/home/agent/.claude/projects/-home-laniakea-Projects-touch/292fc08c-923d-4ab4-8ff2-a9572417dbc8/subagents/workflows/wf_930e210a-6da/agent-a98c064f0ec19ff5f.jsonl:88`
**severity:** major

**Scenario.** A finished agent's file ends with an ordinary `type:"user"` record
carrying the `tool_result` for its final `StructuredOutput` call; the only
distinguishing field is `toolEndsTurn: true` — measured distribution across that
file's 88 records: `{None: 87, True: 1}`. There is no `type:"result"`, no
`stop_reason` record, no sentinel. Record types in an agent transcript are only
`assistant` / `user` / `attachment`; every record carries
`isSidechain: true` and `agentId` (88/88), so records self-identify — but
nothing says "done".

Meanwhile `wf_455b348c-e17`'s journal has **9 started / 2 result** and the run's
`status` is `killed`. Four of those agents will never produce a `result`. So the
naive live rule "`started` and no `result` ⇒ running" permanently paints 7 dead
agents as running — which is the exact bug GD-10 already forbids for plan cards
("Touch treats *no complete event + journal quiet* as **unknown**, never
running") and which R-27's own test names for the legacy corpus
("`touch-repo-recon`'s 7 phantom running agents"). The rule was never extended to
**Workflow agent nodes**, and that is where the user is actually looking.

**Recommendation.** Amend **GD-10** to cover agent nodes with a three-state
predicate, stated once and computed at read time (never persisted as a scalar):

* `result` present in the journal ⇒ **done** (verdict from the polymorphic
  `result` per GD-11);
* no `result`, and the agent transcript's mtime is within the liveness window,
  and the owning session appears in `~/.claude/sessions/` with `status:"busy"`
  ⇒ **running**;
* otherwise ⇒ **unknown/stale**, never `running`, never `failed`.

Store `lastRecordAt` and `toolEndsTurn` on the agent document so the predicate
is a cheap indexed read rather than a transcript re-scan.

---

## SESSIONJSONL-9 — message-id token dedup transfers unchanged, but the DB sink must make it an **upsert**, not a sum

**file:line:** `/home/agent/.claude/projects/-home-laniakea-Projects-touch/292fc08c-923d-4ab4-8ff2-a9572417dbc8.jsonl:11`
**severity:** major

**Scenario.** Re-verified live on this session: the main transcript has **118
`assistant` rows, 46 distinct `message.id`, and all 118 carry `usage`** — a
2.57× over-count if summed naively (top repeats: `msg_011CdNtTSZpMBznvePAaHjmm`
×5, `msg_011CdNtSomVbQpKqNemmiuBY` ×4). The same holds inside agent transcripts:
`agent-a98c064f0ec19ff5f.jsonl` has 53 assistant rows / 22 distinct ids. GD-11's
dedup-by-`message.id` is confirmed and **transfers unchanged**.

The DB-specific hazard is different: in the file store the dedup set is rebuilt
from scratch on every derivation pass, so it is inherently idempotent. In a DB,
if token accounting is implemented as `$inc` on a node/run document, then any
re-ingest — and re-ingest is *mandatory* after every `performRemoveByUuid`
(SESSIONJSONL-2) — double-counts the entire run.

**Recommendation.** Amend **R-26** and **R-24** (store): tokens are stored as one
document per `(nodeRef, message.id)` with `_id` = that pair and the four keys
`{in,out,cached,cache_write}`; run/node totals are an **aggregation over those
documents**, never an incrementing counter. This makes re-ingest naturally
idempotent and preserves GD-11's "run tokens = Σ over nodes of per-node deduped
totals" exactly. Explicitly forbid `$inc` for token accounting in the amendment.

---

## SESSIONJSONL-10 — `promptId` is per-turn, not per-agent; it is not an agent key

**file:line:** `/home/agent/.claude/projects/-home-laniakea-Projects-touch/dd469822-2546-47d9-aaa3-31db4cb705e8/subagents/workflows/wf_829e6f58-b2f/`
**severity:** minor

**Scenario.** `promptId` is present on agent `user` records and is tempting as a
per-invocation key — and in the two newest runs it *looks* 1:1
(`wf_930e210a-6da`: 7 `(agentId, promptId)` pairs / 7 agents;
`wf_cca84d59-933`: 5 / 5). It is not: `wf_829e6f58-b2f` has **21 distinct pairs
across 6 agents** and `wf_455b348c-e17` has **16 across 9**. Within
`agent-a98c064f0ec19ff5f.jsonl` it is also sparse — 33 of 88 records carry it,
55 have `None`.

**Recommendation.** State in the amendment (a one-line disposition under GD-7)
that `promptId` is a *turn* label, is nullable, and is never part of any
identity or index. Keep it as a payload field only.

---

## SESSIONJSONL-11 — discovery scope: the registry lists only **live** sessions, `~/.claude/projects/` contains foreign project slugs, and `.session-aliases` can alias two slugs to one project

**file:line:** `/home/agent/.claude/sessions/15934.json:1`
**severity:** minor

**Scenario.** Three separate discovery traps, all present or code-reachable now:

1. `~/.claude/sessions/` holds exactly **one** file, `15934.json`
   (`{"pid":15934,"sessionId":"292fc08c…","procStart":"4101211","status":"busy",
   "name":"touch-36","nameSource":"derived",…}`), against **6** transcripts on
   disk. R-25's "1-registry-entry vs 6-transcripts case" already names this and
   **transfers unchanged**. Worth noting the registry filename is the raw
   **pid** — pid reuse overwrites — which is exactly why GD-7's `(pid,procStart)`
   identity is right; keep `procStart` in the Mongo `_id`, not just the pid.
2. `~/.claude/projects/` currently contains four *foreign* slug dirs created by
   nested `claude` runs under `/tmp` during earlier research
   (`-tmp-claude-1000-liveio`, `-tmp-claude-1000-models-probe`,
   `-tmp-claude-1000--home-laniakea-Projects-touch-dd469822-…-castprobe`,
   `…-castprobe2`). R-25's "discovered from `projects/*/*.jsonl`" would ingest
   all of them as Touch sessions.
3. `recordSessionAlias` (binary @259265480) writes the *original* project slug
   into `<realpath-slug>/.session-aliases` whenever `realpath(cwd) ≠ cwd`. None
   exists on this machine, but under a symlinked checkout one logical project
   occupies two slug directories and a naive enumerator either double-counts or
   loses half the sessions.

**Recommendation.** Amend **R-25**: discovery is scoped to the slug(s) for the
configured project cwd — the slug of `cwd` **plus** every slug listed in
`<slug>/.session-aliases` — not `projects/*`. Store the project slug set on the
session document. Add the four foreign slug dirs as a negative fixture.

---

## SESSIONJSONL-12 — Mongo document sizing: max observed transcript line is 877 KB; one document per record is mandatory, with a 16 MiB BSON guard

**file:line:** `/home/agent/.claude/projects/-home-laniakea-Projects-touch/e423cd3c-f859-45af-9afd-0d6bdec9b4ac/subagents/workflows/wf_455b348c-e17/agent-a2c3883fe5a0bb9c2.jsonl:1`
**severity:** minor

**Scenario.** Measured maxima across all 30 transcript files: **877 395 B** for a
single line in `agent-a2c3883fe5a0bb9c2.jsonl`, 109 989 B in the current main
transcript, 73 461 B in `e423cd3c….jsonl`; total corpus **15 854 414 B** for 6
sessions accumulated in roughly 12 hours. Individual agent files reach 1 013 548 B.

MongoDB's hard limit is 16 MiB per BSON document. One-document-per-record is
comfortably under it today, but the margin on a single record is only ~19×, and
a single `Read` of a large file or a `Bash` dump lands verbatim in one
`toolUseResult`. Any design that stores "an agent" or "a session" as one
document with an embedded message array hits the cap inside one research run.

**Recommendation.** Fix the grain in the amendment: **one Mongo document per
transcript line**, in `records`; `sessions` / `agents` / `runs` / `nodes` hold
metadata and refs only, never embedded message arrays. Add a writer-side guard:
if a serialized record exceeds ~12 MiB, store it truncated with
`{truncated: true, originalBytes: N, sourcePath, byteOffset}` so the record is
never silently dropped (GD-4 honesty). Note the ~1.3 MB/hour/session growth rate
for index sizing.

---

## SESSIONJSONL-13 — the ingestion contract: source list, checkpoint identity, re-ingest triggers, ordering guarantees

**file:line:** `/home/laniakea/Projects/touch/.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:609` (R-23)
**severity:** minor

This is the deliverable the perspective was asked for, consolidated. It is
written as an amendment table so the plan can absorb it by reference.

**Sources and their checkpoint identity** (checkpoint is always **per file**,
never per session — SESSIONJSONL-3):

| # | source (glob) | checkpoint | re-ingest trigger | ordering authority |
|---|---|---|---|---|
| S1 | `projects/<slug>/<sessionId>.jsonl` | `(st_dev, st_ino, size, offset)` | inode change, or `size < offset` (`performRemoveByUuid` / compaction) | line ordinal |
| S2 | `projects/<slug>/<sessionId>/subagents/agent-<17hex>.jsonl` | same | inode change or shrink | `parentUuid`→`uuid` chain, then line ordinal |
| S3 | `projects/<slug>/<sessionId>/subagents/workflows/<runId>/agent-<17hex>.jsonl` | same | same | same |
| S4 | `projects/<slug>/<sessionId>/subagents/workflows/<runId>/journal.jsonl` | same (append-only in practice) | inode change | line ordinal = `journalSeq`; **no timestamps** (-5) |
| S5 | `projects/<slug>/<sessionId>/subagents/**/agent-<17hex>.meta.json` | `(mtime, size)`, whole-file re-read | mtime change | n/a; **may be absent** (-3) |
| S6 | `projects/<slug>/<sessionId>/workflows/<runId>.json` | `(mtime, size)`, whole-file re-read | appears once at run end (-6) | n/a; back-fill only |
| S7 | `projects/<slug>/<sessionId>/tool-results/<id>.txt` | `(mtime, size)` | directory scan | n/a (-14) |
| S8 | `~/.claude/sessions/<pid>.json` | `(mtime)` | mtime change; missing file ⇒ session not live | n/a; `(pid, procStart)` identity |
| S9 | `~/.claude/history.jsonl` | `(size, offset)` | append-only | line ordinal |

**Ordering guarantees actually available.** Within a file: line order, total and
reliable. Across files: **none** — journal has no clock (-5), and
`mode`/`ai-title`/`last-prompt` have no clock either (-1). Cross-file merge must
be by explicit join keys (`agentId`, `(runId,key,ordinal)`, `uuid`), never by
timestamp sort. GD-11's existing sentence covers this and **transfers unchanged**.

**Latency floor.** Writes batch on a 100 ms timer (`FLUSH_INTERVAL_MS = 100`,
dropping to `Zip = 10` only when remote persistence is configured), so no poller
beats ~100 ms + poll interval. Torn tails are structural: `drainQueuesOnce`
concatenates a whole batch into one string and issues a single
`fs.appendFile` (up to `MAX_CHUNK_BYTES = 100 MB`), so a reader can observe a
partial final line. R-23's "copy the monitor's torn-tail semantics verbatim"
**transfers unchanged** — hold the partial line in offset state, never parse it.
(Five consecutive live reads of the current transcript happened to land on
newline boundaries; that is luck, not a guarantee.)

**Item dispositions, explicitly:**

* **R-23 (tailer) — TRANSFERS UNCHANGED** as a mechanism; the
  `(st_dev, st_ino, size, offset)` checkpoint and shrink-detection are exactly
  right for `performRemoveByUuid`. **Add one clause**: the re-ingest it triggers
  must run the mark-and-sweep generation of SESSIONJSONL-2, because a DB sink
  cannot express deletion by rewriting.
* **R-25 (sessions) — AMENDED**: discovery scoped to the cwd slug +
  `.session-aliases` (-11); the "historical arm keyed `sessionId`" stands for
  *sessions* but must not be used to group *agent* records (-3).
* **R-26 (ingest) — AMENDED** in five places: uuid-less record keying (-1),
  cross-session run/agent assembly (-3), journal timestamps (-5), snapshot
  absence semantics (-6), `agentCount` (-7), and tokens as upserted documents
  rather than counters (-9). Its `persistedOutputPath`-does-not-exist finding and
  its message-id dedup **transfer unchanged and are re-confirmed** (-9, -14).
* **D6 (tailer / no auto-discovery) — STANDS**, with the note that Touch *must*
  glob for run directories across sessions (-3); that is reading a configured
  project root, not auto-discovery.

---

## SESSIONJSONL-14 — spilled tool results are **unreferenced** from the transcript; R-26's regex approach is re-confirmed and needs a directory-scan complement

**file:line:** `/home/agent/.claude/projects/-home-laniakea-Projects-touch/e423cd3c-f859-45af-9afd-0d6bdec9b4ac/tool-results/bqnq7bbbo.txt:1`
**severity:** minor

**Scenario.** Re-verified: `toolUseResult.persistedOutputPath` has **zero real
records** on disk. The two grep hits for the string `persistedOutputPath` are
agent-authored prose inside a `tool_result` (`292fc08c….jsonl:87` and
`e423cd3c….jsonl:77`, both with `toolUseResult` keys `['file','type']`). The
field exists in the CLI's zod schema (binary @257109526, "Path to the persisted
full output in tool-results dir") but is not emitted here. Independently, the
spill *filenames* (`bqnq7bbbo`, `b3ixg5aki`, `bc6wwd73z`, `b0ky2licz`) appear
**nowhere** in any transcript — the only files matching are agent transcripts of
the current run, i.e. researchers reading the directory, not back-references.

So the four spill files in `e423cd3c/tool-results/` are, on this machine,
genuinely orphaned: there is no link from any tool_use to them. R-26's plan to
detect them by parsing `tool_result` content for `^<persisted-output>` +
`Full output saved to: (?P<path>\S+)` is the correct fallback and
**transfers unchanged** — but it will find nothing for these four, so it cannot
be the only mechanism.

**Recommendation.** Amend **R-26**: ingest `tool-results/` by **directory scan**,
keyed `(sessionId, basename)`, with `linkedToolUseId: null` when no
back-reference is found — surfaced in the UI as "unlinked spilled output"
rather than dropped (GD-4). Keep the regex path for the sessions that do emit
the marker, and keep the realpath containment check under
`~/.claude/projects/*/*/tool-results/` (D9 / GD-13) since the recorded path is
agent-authored text.

---

## SESSIONJSONL-15 — there is **no supported push/mirror hook**; file tailing is the only contract

**file:line:** `/home/agent/.local/share/claude/versions/2.1.220:259284935` (`setRemoteIngressUrl`)
**severity:** minor

**Scenario.** The 2.1.220 writer class does contain a mirror/ingress mechanism —
`addMirror(fn)` / `fireMirror(file, entries)` called on every successful append,
`setRemoteIngressUrl(url)`, `setInternalEventWriter/Reader`,
`setInternalSubagentEventReader`, gated by `Yt("true")` and tagged "CCR v2
internal event writer registered for transcript persistence". This is
Anthropic's own remote session-persistence path; it is registered from inside
the bundle and is not reachable from an external process, no env var exposes it,
and it is not a documented interface. Its existence is worth recording precisely
so the amendment does **not** spend an item chasing a push API, and so a future
CLI version that exposes it can be adopted as a clean upgrade.

One consequential detail if it is ever enabled on a user's machine:
`setRemoteIngressUrl` and `setInternalEventWriter` both drop
`FLUSH_INTERVAL_MS` from 100 to `Zip = 10`, i.e. 10× more, smaller appends —
the tailer must not assume batch granularity.

**Recommendation.** Record as a one-line disposition in the amendment under D6:
"no supported push hook exists in 2.1.220; tailing S1–S9 is the contract. The
internal `addMirror` / `remoteIngressUrl` machinery is noted for a future
version and must not be relied on or monkey-patched." Do not add an item.

---

## SESSIONJSONL-16 — `session_id` duplicates `sessionId` on every message record

**file:line:** `/home/agent/.claude/projects/-home-laniakea-Projects-touch/292fc08c-923d-4ab4-8ff2-a9572417dbc8.jsonl:4`
**severity:** nit

**Scenario.** `user`, `assistant` and `attachment` records in the **main**
transcript carry both `sessionId` and `session_id`; 0 mismatches across all 267
records. Agent transcripts carry only `sessionId`. Mirroring both wastes ~40 B
per record (~3 % of a small record) and invites a future consumer to read the
wrong one.

**Recommendation.** Normalize to `sessionId` at the mirror boundary and drop
`session_id`; note the drop in the record document's `_normalized` list so the
mirror stays auditable against the source file.

---

## Cross-cutting note for the synthesizer (not a finding)

The environment moved during this run: a `mongo:7` container
(`touch-mongo-probe`) is now **up**, while `pymongo` is still not importable by
the system `python3`. GD/D8's "stdlib-only runtime" cannot survive a MongoDB
sink — speaking the wire protocol from stdlib is not reasonable. The amendment
must state D8's disposition explicitly (**AMENDED**, naming `pymongo` as the one
permitted runtime dependency) rather than leaving it implied, and should keep the
touch-events-v2 file store as the fallback path so R-23/R-24 remain testable
without a database — which also preserves GD-18's fixtures-before-features.
