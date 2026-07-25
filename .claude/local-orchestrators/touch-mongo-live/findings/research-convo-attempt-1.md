# research — perspective: convo (the conversation as requirements source + the session as data)

Agent `a1451612c213c8777`, run `wf_cca84d59-933`, session
`292fc08c-923d-4ab4-8ff2-a9572417dbc8`. Every claim below was re-derived from
disk this run; nothing is inherited from the earlier `touch-full-recon` reports.

Scope covered: the live main-session transcript end-to-end; the five sibling
session transcripts in the same project dir; both workflow trees under this
session; `~/.claude/sessions/`; `~/.claude/history.jsonl`;
`touch-full-recon-plan.md` (normative), `touch-monitor-spawn-plan.md` (G2
precedent), `touch-aggregator-plan.md` (D1–D14 source), `inception.md`,
`.claude/shared/monitoring/`.

---

## Part A — what the conversation actually decided

### The four asks in THIS session (292fc08c)

| record | line | text (verbatim) |
|---|---|---|
| `90cd92b1-0d70-4ecb-ad7b-b3341ba07326` | 181 | "does that plan contains saving claude code session into mongo db and mapping local session agents info to mongo records? also does it contains custom saving implementation, where we can create custom state into another mongodb colection and reference that to mapped session agents info?" |
| `761331dc-c98d-4be9-ba62-2ebd874c6a83` | 195 | "can we create deterministic code for saving session jsonl infos into db?" |
| `b003ac9f-6b8d-4b36-8981-87d7df537f80` | 204 | "can we read session info to correctly show monitoring status about agents and geterministic loops?" |
| `5e05ab4e-10c6-47b8-8512-29cacbab6c9c` | 209 | `/execute-research` — "research current conversation history and explore how to persist current claude code session jsonl files into mongodb, also how to add agents custom state persistence and map them to real session saved copy in mongodb. also research monitoring flow, and how to correctly show subagents and loops live info." |

Answers given, in the same session: `6cd41450-d17d-4ef5-8120-217f3f0ba3f8`
(line 192, "No to both" + the G2 history), `1269105d-9d6a-486c-b4fa-eb4caa8d7cba`
(line 197, the determinism-as-idempotency table + a 5-collection sketch),
`d1973737-ca97-4346-8759-6b02a9d15a62` (line 206, what reads correctly / what
needs the phase-1 fixes / what can never be read).

### Session inventory as data (`292fc08c`, measured at 14:32)

267 lines, 816,378 bytes, one `sessionId`, CLI `2.1.220`, 12 record types:

```
assistant 118  user 54  mode 17  permission-mode 17  last-prompt 16
attachment 15  queue-operation 10  system 6  file-history-snapshot 5
ai-title 5  file-history-delta 3  frame-link 1
```

Whole project corpus: 41 `.jsonl` files, 3,936 records, 15,730,670 bytes
(mean 3,996 B/record); largest single line 872,578 B (a subagent transcript in
`e423cd3c`); largest in this session 108,827 B at line 87; max nesting depth 7.

This research run itself is on disk as: a `Workflow` `tool_use`
(`toolu_01Guc7DFdJ9eGKxEeRM9GZi7`, line 235) → a `toolUseResult` with
`{status:"async_launched", taskId:"www4dk54h", runId:"wf_cca84d59-933",
transcriptDir:…}` (line 236, uuid `f1e56c23…`) → five
`subagents/workflows/wf_cca84d59-933/agent-*.jsonl` + a 5-line `journal.jsonl`
of bare `started` rows + five 63-byte `.meta.json` stubs
(`{"agentType":"workflow-subagent","spawnDepth":1,"model":"opus"}`). No
`workflows/wf_cca84d59-933.json` exists yet — only the *completed* earlier run
has its rich record (`workflows/wf_930e210a-6da.json`, 46,558 B, written 14:14).

---

## CONVO-1 — The binding Mongo requirements are in a DIFFERENT session; this one only re-asks them

**file:line**: `/home/agent/.claude/projects/-home-laniakea-Projects-touch/e423cd3c-f859-45af-9afd-0d6bdec9b4ac.jsonl:145,151,157,162`
**severity**: blocker

**Scenario.** The research subject names `292fc08c…jsonl` as "requirements
source". It is not the primary one. The four decisive user statements about
Mongo and custom state were made on 2026-07-25 03:48–03:53 in session
`e423cd3c`, and this session's line 181 merely asks whether the *plan* covers
them. Verbatim:

- `081b28a7-aee9-43dc-935d-1586407f232e` (e423cd3c:145) — "how to add **hook**
  to store all session infos in mongodb? **separate collections for separate
  session datas**."
- `1ec9c5c1-3921-443e-82c2-f15e372d237a` (e423cd3c:151) — "how to store all
  session infos in mongodb? separate collections for separate session datas."
  (asked twice, ~20 s apart)
- `abe6607f-c8a7-4d65-842b-5dcd241ff4cb` (e423cd3c:157) — "how to save custom
  state for subanget?"
- `70eb3975-8a48-4710-a4de-6d2b20fc513e` (e423cd3c:162) — "yep, Touch's own
  store, we can store infromation in **which line subagent is located on
  original session file**, and map that into our custom jsonl or mongodb
  objects."

The corresponding answers (`071e7c25-3530-4b7e-b879-afb25b516fff` e423cd3c:154,
`a13ef1b0-9791-4e12-9fa8-e681ccd359c7` e423cd3c:159,
`e551fdef-5ed5-44c9-9237-a615a58b424b` e423cd3c:164) contain the *entire*
Mongo schema already shown to the user — including collections, sample
documents, and the line-number decision. An amendment plan written only from
`292fc08c` will silently re-decide things the user has already seen and agreed
to, and will contradict them (see CONVO-2, CONVO-3, CONVO-9).

Corroborating evidence that the conversation spans sessions: at 292fc08c:206
the assistant writes "the SKILLS-1 badge **you asked about**" — no such user
question exists anywhere in `292fc08c`; it refers to `e423cd3c`.

**Recommendation.** Make the amendment plan cite `e423cd3c` record uuids as
the requirements source of record, alongside `292fc08c`. Add a short
"Requirements provenance" section listing the six uuids above with their
verbatim text, so `implement-plan` never has to re-read a transcript. Do not
treat `292fc08c` as complete on its own.

---

## CONVO-2 — Two mutually inconsistent Mongo collection sets have already been promised to the user

**file:line**: `e423cd3c…jsonl:154` vs `292fc08c…jsonl:197`
**severity**: blocker

**Scenario.** The user has been shown two different schemas, three weeks of
context apart in the same day:

| e423cd3c:154 (uuid `071e7c25…`) | 292fc08c:197 (uuid `1269105d…`) |
|---|---|
| `sessions` `_id:"622:10028"` | `sessions` `_id:"<pid>-<procStart>"` (different separator) |
| `records` `_id:uuid` | `records` `_id:uuid` |
| `agents` `_id:agentId` | `agents` `_id:agentId` |
| `runs` `_id:runId` | *absent* |
| `nodes` `_id:"runId:key:ordinal"` | `run_nodes` `_id:"runId:key:ordinal"` (renamed) |
| `usage` `_id:message.id` | `usage` `_id:message.id` |
| `events` (insert-only, seq-indexed) | *absent* |
| `control` (requested→sent→confirmed) | *absent* |
| custom state = **subdocument** on `agents` | custom state = **its own collection** |

Eight collections vs five; `nodes` vs `run_nodes`; `:` vs `-` in the session
key. If the divider hands `implement-plan` a plan that does not pin exactly one
of these, two sub-plans will write two incompatible schemas (and the `_id`
separator choice is unrecoverable once data exists).

**Recommendation.** The amendment must contain ONE normative collection table
with, per collection: name, `_id` construction rule (with the literal
separator), required fields, indexes, and which GD-11 ref-union member it
corresponds to. Explicitly record the disposition of the discarded variant
("`run_nodes` chosen over `nodes` because …", "`runs` folded into / kept
because …"). `events` and `control` must be decided in or out — dropping them
silently would delete the audit trail GD-11 and R-34/R-35 depend on.

---

## CONVO-3 — "Custom state": separate collection (user's words) vs subdocument (the answer given) is an open contradiction

**file:line**: `292fc08c…jsonl:181` and `:209` vs `e423cd3c…jsonl:159`
**severity**: blocker

**Scenario.** The user has asked twice, in his own words, for a *separate
collection*: "we can create custom state into **another mongodb colection** and
reference that to mapped session agents info" (292fc08c:181) and "add agents
custom state persistence and **map them to real session saved copy** in
mongodb" (292fc08c:209). The already-given answer (`a13ef1b0…`, e423cd3c:159)
proposes the opposite materialization:

```js
db.agents.updateOne({_id: "a2fc883c…"}, {$set: {custom: {...}}})
// plus the event record inserted into `events` for the audit trail
```

i.e. custom state as a subdocument on the agent doc plus an append to `events`.
292fc08c:197 then says "your **custom-state collection** referencing any of
those ids". Three statements, two designs. Additionally e423cd3c:159 raises a
real timing problem the user has not seen resolved: *"`agentId` doesn't exist
until spawn. If you need state attached before creation … key it by your minted
`touchName` … and re-point it to the `agentId` when `.meta.json` appears."*
That re-pointing is a schema migration on live data and needs a decided rule.

**Recommendation.** Decide, and state the reasoning against the user's own
phrasing (he asked for a collection; if the answer is a subdocument, say why).
Recommended shape given the evidence: a first-class `agent_state` collection
with `_id` = `(scope, key, name)` where `scope ∈ {agent, node, session, run}`
and `key` is the corresponding ref-union value, so pre-spawn state keyed
`(agent, "touchName:<name>", …)` can be re-pointed by one `updateOne` that
rewrites `key` — with the pre-spawn alias retained as `aliases[]` so nothing
dangles. Whatever is chosen, define: (a) who writes it (Touch only, never the
agent), (b) the pre-spawn→post-spawn re-point rule, (c) whether an audit event
is also appended, (d) what happens on re-ingest (custom state must survive a
full mirror rebuild — it is the ONE collection that is not derivable from
`~/.claude`).

---

## CONVO-4 — `sessions._id = "<pid>-<procStart>"` cannot be built for 5 of the 6 sessions on this machine

**file:line**: `~/.claude/sessions/15934.json` (only entry); `292fc08c…jsonl:197`
**severity**: blocker

**Scenario.** Both Mongo answers key `sessions` on `(pid, procStart)`.
Measured this run:

- `~/.claude/sessions/` contains exactly ONE file, `15934.json`:
  `{"pid":15934,"sessionId":"292fc08c-…","procStart":"4101211","version":"2.1.220","status":"busy",…}`.
- The project dir contains SIX transcripts (`292fc08c`, `e423cd3c`, `dd469822`,
  `ad7b421c`, `c2f92a2c`, `e144bb01`).
- Scanning all six transcripts for a `pid` or `procStart` field: **0
  occurrences of each**. Every record carries `sessionId`, `cwd`, `version`,
  `gitBranch` — no process identity.
- `~/.claude/history.jsonl` names a seventh sessionId
  (`8084340e-a56b-499f-b54d-cec64e52da78`) with **no transcript at all** in the
  project dir.

So the promised primary key exists for 1 of 6 (17%) of the sessions Touch must
mirror, and for 0% of anything the CLI retention sweep has already touched.
`procStart` is also not a timestamp — it is `"4101211"`, a clock-tick string
from `/proc/<pid>/stat` field 22 — so it cannot be reconstructed after the pid
is gone.

Notably, `touch-full-recon-plan.md` already solved this (GD-6 three session
classes; R-25 "historical arm: sessions keyed `sessionId`, `liveness:
historical` … the 1-registry-entry vs 6-transcripts case" — plan lines 100-108,
629-640). **Both Mongo answers ignore it.**

**Recommendation.** `sessions._id` must be a tagged union, decided once:
`"live:<pid>-<procStart>"` vs `"hist:<sessionId>"`, with a documented promotion
rule (when a registry entry names a `sessionId` already stored as `hist:`,
either rewrite the `_id` under a transaction or — better — keep `_id` immutable
and carry `pid`/`procStart` as indexed attributes with `sessionIds[]` as the
join array). Add an explicit test: mirror this project dir and assert 6 session
documents, exactly one of them live. The amendment must state that the
`sessionIds: ["dd469822-…","e423cd3c-…"]` example in e423cd3c:154 was an
illustration, not an observed fact — nothing on disk proves those two sessions
shared a process.

---

## CONVO-5 — `records._id = uuid` covers only 72% of records; 39 records in this session are byte-identical duplicates

**file:line**: `292fc08c…jsonl:1,2,17,18,29,30,…` (see table)
**severity**: blocker

**Scenario.** The whole "deterministic = idempotent upsert on a natural key"
argument (292fc08c:197) rests on every record having a natural key. Measured on
this session:

| type | n | with `uuid` | with `timestamp` | with `sessionId` |
|---|---|---|---|---|
| assistant | 118 | 118 | 118 | 118 |
| user | 54 | 54 | 54 | 54 |
| attachment | 15 | 15 | 15 | 15 |
| system | 6 | 6 | 6 | 6 |
| **mode** | 17 | **0** | **0** | 17 |
| **permission-mode** | 17 | **0** | **0** | 17 |
| **last-prompt** | 16 | **0** | **0** | 16 |
| **queue-operation** | 10 | **0** | 10 | 10 |
| **file-history-snapshot** | 5 | **0** | **0** | **0** |
| **ai-title** | 5 | **0** | **0** | 5 |
| **file-history-delta** | 3 | **0** | 3 | **0** |
| **frame-link** | 1 | **0** | 1 | 1 |

193 of 267 records (72%) carry a `uuid`; 193 distinct, 0 duplicates — the uuid
half is sound, and it holds across all six sessions (697 uuid-bearing records,
zero collisions) and across all 35 subagent transcripts (750 records, zero
collisions). The other 74 records have **no key at all**, and worse:

- all **17** `mode` lines are byte-identical: `{"type":"mode","mode":"normal","sessionId":"292fc08c-…"}`
- all **17** `permission-mode` lines are byte-identical
- all **5** `ai-title` lines are byte-identical

A content-hash `_id` collapses 39 records into 3. An `ObjectId()` `_id` makes
re-ingestion non-idempotent (a compaction rewrite → full re-ingest from 0 →
39 new documents every time). `file-history-snapshot` / `file-history-delta`
carry no `sessionId` either, so their only join is the file path they were
read from.

**Recommendation.** Two decisions, both required:

1. **Bucket the 12 types explicitly.** `inception.md:74-76` names the four CLI
   buckets (`transcript` / `boundary-cleared` / `accumulate` / `last-wins`) but
   never maps types to them, and `queue-operation` and `frame-link` appear in
   no bucket table anywhere. Write the full table into the amendment. Suggested,
   evidence-based: `transcript` = user/assistant/system/attachment (`_id: uuid`);
   `last-wins` = mode/permission-mode/ai-title/last-prompt (`_id:
   "<sessionId>:<type>"`, one doc, overwritten — which is also *semantically*
   right: only the last value matters); `accumulate` = queue-operation /
   frame-link / file-history-delta (`_id: "<sessionId>:<type>:<sha256(line)>"`);
   `file-history-snapshot` (`_id: "<sessionId>:fhs:<messageId>"` — `messageId`
   is the uuid of the record it snapshots, verified: line 3's `messageId`
   `2fdbc0f0…` == the `uuid` of the user record at line 4).
2. **Inject `sessionId` from the file path at ingest time**, since 8 records in
   this session have none in their body.

Test to require: ingest this session's 267 lines twice; assert the document
count is identical both times AND that all 17 `mode` occurrences are
represented (as one last-wins doc, not seventeen and not silently one-of-many).

---

## CONVO-6 — The amendment's own anchor is broken: `D8` is labelled two different decisions

**file:line**: `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:27`
**severity**: major

**Scenario.** Every Mongo statement in this conversation says the change
"amends D5/D8" (292fc08c:192, :197; e423cd3c:154). In
`touch-aggregator-plan.md:217` **D8 is "Stack"** — "Python 3.11+, **stdlib only
at runtime**, one asyncio process, one port…". But
`touch-full-recon-plan.md:27` — the normative plan — writes:

```
| D8 "journal `result` opaque, never parsed" | **SUPERSEDED** — `result` is polymorphic (AUDIT-2); … | GD-11 |
```

"journal `result` … never parsed" is a *sub-clause buried at the end of D8's
Graph data contract* (`touch-aggregator-plan.md:242-243`), not D8 itself. Read
literally, the normative plan declares the **stdlib-only pin SUPERSEDED** — the
exact constraint the Mongo amendment exists to lift. An implementer reading
either way is defensible: "D8 is already superseded, pymongo is fine, no
amendment needed" or "D8 was superseded so there is nothing to amend".

**Recommendation.** The amendment must open by re-stating the disposition
unambiguously: **D8.1 (Stack / stdlib-only-at-runtime) — AMENDED by this
document**; **D8.2 (journal `result` opaque) — remains SUPERSEDED per
touch-full-recon GD-11**. Add a one-line correction note to
`touch-full-recon-plan.md:27` as an implementation item so the ambiguity is
fixed at the source, not just worked around.

---

## CONVO-7 — The Mongo discard and the stdlib pin survive only inside a *superseded* document

**file:line**: `.claude/local-orchestrators/touch-monitor-spawn/plan/touch-monitor-spawn-plan.md:24-28` and `:222-229`; `touch-full-recon-plan.md:10-11`
**severity**: major

**Scenario.** The precedent everyone cites —

> **G2 — Storage**: `.touch/` JSONL per D5, stdlib only per D8. MongoDB was
> discussed and is NOT adopted here; adopting it later is an explicit D5/D8
> amendment, not an implementation choice. (`touch-monitor-spawn-plan.md:24-27`)

and the discard register —

> Mongo storage (needs D5/D8 amendment); per-session Mongo collections
> (anti-pattern — cross-session queries, index duplication); line numbers as
> identity (rot on /clear, compaction, truncation — cursor/hint only)
> (`touch-monitor-spawn-plan.md:224-226`)

— live in a file that `touch-full-recon-plan.md:10-11` explicitly supersedes
("**This plan supersedes … `touch-monitor-spawn-plan.md` (P1–P12, G1–G9)**…
Those two files are historical records"). And `touch-full-recon-plan.md`
contains **zero occurrences of the string "mongo"** (verified by grep) and
never restates the stdlib pin as a GD — its only mention is incidental, inside
R-22's approach line (`plan:605`, "stdlib-only (statusline's `jq` is the
recorded exception)").

So the amendment is being written against a normative document that neither
forbids Mongo nor pins stdlib. Its "amendment" framing has no anchor in the
document it amends.

**Recommendation.** The amendment must (a) quote G2 and the discard register as
*historical* provenance while stating they are formally superseded, and (b)
introduce a NEW global decision in the amendment's own numbering — e.g.
`GD-21 — Runtime dependency policy` — that restates the pin, its single
exception (the Mongo driver), and the exception's blast radius (which modules
may import it, and that everything else stays importable without it so the
tests still run with no mongod). Without (b) there is no written rule left that
stops the next implementer adding a second dependency by the same argument.

---

## CONVO-8 — "Separate collections for separate session datas" was asked twice and must be dispositioned in the user's own words

**file:line**: `e423cd3c…jsonl:145,151`
**severity**: major

**Scenario.** The user's literal ask, repeated 20 seconds apart, is per-session
collections. It has been rejected twice on his behalf (e423cd3c:154 "that's a
MongoDB anti-pattern here … the sidebar is a cross-session query … every
collection duplicates its indexes"; 292fc08c:197 "the earlier discard of
per-session collections still applies — one collection per entity type, not per
session"), and recorded as a discard in a now-superseded plan
(`touch-monitor-spawn-plan.md:224-225`). The task brief for this run requires
"states every disposition explicitly". A plan that simply omits per-session
collections reads, to the person who asked for them, as the request being
dropped.

**Recommendation.** Give it a numbered disposition entry with the measured
justification from *this* machine, not a generic anti-pattern claim: 6 sessions
in one project dir today, 7 sessionIds in `history.jsonl`, one of them with no
transcript — a per-session collection scheme creates 7 collections for one
project and makes "all sessions, newest activity first" (the README's left
sidebar) an N-collection scan; the equivalent indexed query on one `records`
collection is one index on `(sessionKey, ts)`. State the compromise that gives
the user what he actually wanted: per-session *isolation* is expressed as an
indexed `sessionKey` field plus a per-session partial index / view, not a
namespace.

---

## CONVO-9 — The line-number→uuid mapping decision was made with the user and is absent from the normative plan

**file:line**: `e423cd3c…jsonl:162` (ask) and `:164` (decision); `touch-full-recon-plan.md` (no counterpart)
**severity**: major

**Scenario.** The user proposed the mapping himself: *"we can store infromation
in which line subagent is located on original session file, and map that into
our custom jsonl or mongodb objects"* (`70eb3975…`). The answer
(`e551fdef-5ed5-44c9-9237-a615a58b424b`) accepted the idea and moved the key one
level up, with a concrete shape:

```js
{ _id: "a2fc883c96ff7b837",
  spawn: { recordUuid: "…", toolUseId: "toolu_01AB…", sessionKey: "622:10028",
           fileHint: { path: "…/dd469822….jsonl", line: 1042,
                       ino: 5301245, size: 2262411, ts: ISODate("…") } } }
```

— *"`fileHint` is allowed as a perishable cache … valid only while `(ino, size)`
still match … Offset-as-cursor: fine. Offset-as-identity: never."*

`touch-full-recon-plan.md` has **no** `fileHint`, no per-record file position,
and no "where is this agent in the session" concept at all (grep for
`fileHint`/`line number` returns only the tailer's `(st_dev, st_ino, size,
offset)` checkpoint, which is a different thing — a *file* cursor, not a
*record* locator). This is a user-visible feature ("jump to the spawn point")
that was agreed and then lost.

**Recommendation.** Carry it into the amendment as a named item: `agents.spawn`
sub-document with `recordUuid` + `toolUseId` as identity and a `fileHint`
validated against `(st_dev, st_ino, size)` before use, invalidated (not
deleted — keep it for diagnostics) on mismatch. Add Touch's own per-record
`seq` so the UI's "jump to spawn" is a query against the mirror
(`db.records.findOne({_id: spawn.recordUuid}, {seq:1})`) and never re-reads the
original file. Test: mutate a fixture transcript's size, assert `fileHint`
is marked stale and the `recordUuid` lookup still resolves.

---

## CONVO-10 — Live specimen: the label-collision defect is reproducing in the very run researching it

**file:line**: `.claude/shared/monitoring/decision_watcher.py:550` (also 623, 637, 683, 777, 787)
**severity**: major

**Scenario.** Read from the live stream at 14:35, `touch-mongo-live/events.jsonl`
— all five researchers of this run are emitted under ONE label:

```
'research #1' -> ['a0df37b1', 'a1451612', 'a3ad69b2', 'a68d1d2c', 'a69c966d']
```

The watcher's own checkpoint has the distinguishing field and throws it away:

```json
{"a1451612c213c8777": {"plan":"research","role":"research","attempt":1,"stage":"convo"},
 "a69c966d46ecd64b6": {"…","stage":"liveflow"},
 "a68d1d2cf53f4e546": {"…","stage":"customstate"},
 "a0df37b1b112e2a60": {"…","stage":"mongoschema"},
 "a3ad69b25fa3c9c9f": {"…","stage":"sessionjsonl"}}
```

because `decision_watcher.py:550` computes
`label = f"{info['role']} #{info['attempt']}"` — `info['stage']` is in scope and
unused — and emits `"id": aid[:8]`, truncating the harness's 17-hex `agentId`.
This is the direct, empirical answer to the user's third question ("correctly
show subagents"): the data is deterministic and correct; the *emitter* discards
it. It is already item R-13 in the normative plan, and it is still shipping.

**Recommendation.** The amendment should promote this from "phase 1 item" to a
precondition of the Mongo work, because the mirror will otherwise persist the
collision: if `agents._id` is fed from the event stream it gets 8-hex ids
(which GD-11's validator explicitly rejects as "non-17-hex") and five documents
that cannot be told apart. Make the amendment state that Mongo ingest reads
harness files directly (transcripts + journal + `.meta.json`), never the legacy
`events.jsonl`, except through the GD-14 `legacy:<task>:<id8>` namespace. Fix
`decision_watcher.py:550/557/623/637/683/787` to emit the full `agentId` and
`f"{role} {stage} #{attempt}"`; assert in a test that N concurrent agents
produce N distinct labels.

---

## CONVO-11 — Real session records contain field names with dots and strings with NUL bytes — a verbatim BSON mirror is unsafe

**file:line**: `292fc08c…jsonl:180,194,203,208` (`snapshot.trackedFileBackups`); 55 lines corpus-wide with ` `
**severity**: major

**Scenario.** `file-history-snapshot.snapshot.trackedFileBackups` is a **map
keyed by relative file path**. Observed keys on disk right now:

```
".claude/local-orchestrators/touch-full-recon/orch-scripts/research.workflow.js"
".claude/local-orchestrators/touch-full-recon/report/research-report.html"
".gitignore"   "CLAUDE.md"   "inception.md"
```

66 such dotted keys across the corpus. MongoDB has allowed `.`/`$` in field
names only since 5.0, and the manual is explicit that support is partial: such
fields need `$getField`/`$setField`/`$literal` to be queried, `mongoimport` /
`mongoexport` "may not work as expected", and a top-level field named `"a.b"`
must not coexist with an embedded `{a:{b:…}}` — which is exactly what happens
if any record ever carries a real `CLAUDE` object. Additionally 55 lines
contain embedded ` ` inside `tool_result` strings (agent output captured
verbatim). And the 872,578-byte record in `e423cd3c`'s subagent tree is 5% of
the 16 MB BSON document cap — one more order of magnitude of tool output and a
single record becomes unstorable.

**Recommendation.** Decide the `raw` storage policy explicitly in the
amendment. Recommended: store the original line as a **BSON string**
(`raw: "<the exact line>"`) plus typed, projected fields for everything the UI
queries (`type`, `ts`, `sessionKey`, `agentId`, `toolUseId`, `messageId`,
`bucket`). That makes the mirror byte-exact and replayable, sidesteps dotted
keys and NUL-in-key entirely, and keeps queries on indexed scalars. If a parsed
`raw` object is kept instead, the amendment must specify a key-escaping rule
(e.g. `.`→`．`) and note that it breaks byte-exactness. Either way add: a
size guard that diverts records over ~8 MB to GridFS or to a path pointer, and
a test using the real 872 KB line and a real dotted-key
`file-history-snapshot` as fixtures.

---

## CONVO-12 — A Workflow run *does* have a `taskId`; the plan's "stop unavailable" for the Workflow profile is too strong

**file:line**: `292fc08c…jsonl:57` and `:236`; `touch-full-recon-plan.md:118-121` (GD-8) and `:569-571` (R-33)
**severity**: major

**Scenario.** GD-8 says of the Workflow profile: *"agents have no `taskId`,
**stop is unavailable** and rendered disabled with that reason"*, and R-33
instructs the ledger to write `"taskId": null`. But the main transcript records,
for each Workflow launch, a `toolUseResult`:

```json
{"status":"async_launched","taskId":"w4hiywrt6","taskType":"local_workflow",
 "workflowName":"touch-full-recon-research","runId":"wf_930e210a-6da",
 "transcriptDir":"…/subagents/workflows/wf_930e210a-6da",
 "scriptPath":"…/orch-scripts/research.workflow.js"}
```

(line 57 for the previous run; line 236, `taskId:"www4dk54h"`,
`runId:"wf_cca84d59-933"` for this one). The *individual agents* indeed have no
taskId — but the *run* is a harness-tracked task, and the queue-operation at
line 65 confirms the harness tracks it by that id
(`<task-id>w4hiywrt6</task-id>`). So run-level stop is available in the
Workflow profile; only per-agent stop is not.

This record is also the **only** deterministic main-session → run join that
exists: `.meta.json` for Workflow spawns is the 63-byte stub with no
`toolUseId` (verified), and `workflows/<runId>.json` appears only after
completion (present for `wf_930e210a-6da`, absent for `wf_cca84d59-933`).

**Recommendation.** Amend GD-8 to a three-level statement: *run* stop available
(via the run `taskId` from `toolUseResult`), *agent* stop unavailable in the
Workflow profile, and the UI must render the two granularities distinctly
(README's "terminate agent loops" is satisfiable at run level today). Persist
`toolUseResult.{taskId, runId, transcriptDir, scriptPath, workflowName,
summary}` on the `runs` document as first-class fields — this is the join key
for everything else, and `transcriptDir` removes the need to glob for the
run's agent transcripts. Add the `runs` collection back if CONVO-2 resolves in
favour of the 5-collection sketch.

---

## CONVO-13 — Change streams were promised as "a one-flag setup"; nothing on this machine can run them yet

**file:line**: `e423cd3c…jsonl:154`
**severity**: minor

**Scenario.** The answer the user has already read says *"change streams on
`events` replace the hand-rolled replay-then-tail WebSocket logic — but they
require mongod to run as a (single-node) replica set, which is a one-flag
setup."* Verified environment this run: no `mongod` binary, `pymongo` not
importable, Docker daemon up with **zero** containers, egress through an
allowlist proxy. So "one flag" today means: pull an image through the proxy,
run it with `--replSet`, `rs.initiate()` it, persist a volume, and supervise it
— and the whole test suite (four stdlib-only suites, currently green) must keep
passing with none of that present.

**Recommendation.** Make deployment an explicit global decision in the
amendment: image + tag, `--replSet` yes/no, bind address and auth (GD-13's
posture applies — a Mongo listening on 0.0.0.0 in a sandbox is a wider hole
than the monitor's read-only WS), data volume path, and whether Touch supervises
mongod or requires it pre-running. Then decide the fallback: if change streams
are unavailable (standalone mongod), Touch must poll the `events` collection by
`(stream, seq)` cursor — that fallback must exist regardless, because the WS
replay contract is `(stream, seq)` per GD-11. Require that every module remains
importable and every existing test remains green with no mongod and no pymongo
installed (a hard CI condition, not a preference).

---

## CONVO-14 — Three orphaned `decision_watcher` processes are running; nothing ever reaps them, and GD-1 blocks committing while they do

**file:line**: live `ps`; `touch-full-recon-plan.md:51-56` (GD-1)
**severity**: minor

**Scenario.** Right now:

| pid | `ORCH_STATE_DIR` | started |
|---|---|---|
| 4929 | `…/touch-aggregator` | 03:00 (11.5 h ago; that run is long finished) |
| 16627 | `…/touch-full-recon` | 14:32 (run completed 14:14) |
| 20223 | `…/touch-mongo-live` | 14:32 (this run — legitimate) |
| 20763 | `monitor_server.py`, `…/touch-mongo-live` | 14:35 |

`touch-full-recon/events.jsonl` was last written 14:21 — its watcher has been
idle-spinning for 14 minutes past run end, and `touch-aggregator`'s for half a
day. GD-1 states: *"No commit while any watcher is writing (check `ps -eo cmd |
grep "[d]ecision_watcher"`)"*. Under current practice that check never clears,
so R-01/R-02 (the repo's first commit) can never legally run.

**Recommendation.** Two amendment items. (1) A run-close protocol: the driver
that starts the daemons must stop them on `orchestrator complete`, and the
watcher should self-exit after the journal has been quiet for N seconds *and* a
terminal `complete` event exists (this also makes "is the loop still running?"
answerable from process state, which the UI needs). (2) Soften GD-1's gate from
"any watcher" to "any watcher whose `ORCH_STATE_DIR` is inside the paths being
committed" — otherwise the precondition is unsatisfiable in practice. Both are
prerequisites for a Mongo ingester daemon, which will have exactly the same
lifecycle problem one level up.

---

## CONVO-15 — `queue-operation` records duplicate content already stored as `user` records

**file:line**: `292fc08c…jsonl:65` (queue-operation, 9,964 B) vs `:67` (user, uuid `2d50261c-41ae-4a0d-bc51-04fa845c9931`, 9,604 B)
**severity**: minor

**Scenario.** Line 65 is `{"type":"queue-operation","operation":"enqueue",…,"content":"<task-notification>…"}`
and line 67 is the `user` record carrying the *same* `<task-notification>` text
as the actual conversation turn. 10 `queue-operation` records exist in this
session; they have no `uuid`, so they cannot be deduped against the `user`
record they anticipate. A verbatim mirror stores both, roughly doubling the
bytes for every task notification, and a naive timeline render shows each
notification twice.

**Recommendation.** Classify `queue-operation` as `accumulate` with a synthetic
`_id`, mark it `render:false` by default (it is queue mechanics, not
conversation), and record in the amendment that it is *intentionally* not
deduped against the corresponding `user` record (they are different events —
enqueue time vs delivery time — and the enqueue timestamp is the only place the
queue latency is observable: 14:14:59.374 enqueue vs 14:14:59.444 delivery
here). Add both lines to the fixture set.

---

## CONVO-16 — Corpus volume and the `raw` duplication cost are undecided

**file:line**: `touch-full-recon-plan.md:231-237` (GD-16, covers only `events.jsonl`)
**severity**: nit

**Scenario.** Measured: 41 `.jsonl` files, 3,936 records, 15.7 MB, for ~40
hours of work on one project on one machine — of which the two research runs in
this session's tree alone account for ~5.6 MB of subagent transcripts. Storing
`raw` per record (the e423cd3c:154 sketch) roughly doubles the on-disk figure
inside Mongo before indexes. GD-16 sets a growth policy ("revisit only if the
repo exceeds ~20 MB") but only for `events.jsonl` in git — there is no policy
for the mirror, which is precisely the store that is supposed to outlive the
CLI retention sweep and therefore only ever grows.

**Recommendation.** State a retention/compaction policy for the mirror in the
amendment: what is kept forever (sessions, agents, runs, nodes, usage, custom
state — all small), what is prunable (`records.raw` for sessions older than N
days, once the derived fields are materialized), and the index list with an
estimated size. Include the measured 15.7 MB / 3,936 records / 3,996 B-per-record
baseline so the estimate is grounded.

---

## Cross-cutting note for the synthesizer

The amendment cannot be written as "add Mongo to R-24". The evidence above says
it must, at minimum, take an explicit position on: GD-6/R-25 (historical
sessions have no `(pid,procStart)` — CONVO-4), GD-7 (node identity is already
the `_id` design — reuse verbatim, do not restate differently), GD-8 (run-level
`taskId` exists — CONVO-12), GD-11 (the ref union IS the foreign-key model; the
Mongo `_id` rules must be a 1:1 rendering of it, and the 17-hex validator will
reject the watcher's current 8-hex ids — CONVO-10), GD-13 (a Mongo port is a
new network surface — CONVO-13), GD-14 (legacy `events.jsonl` ingest is the ONE
path that may carry 8-hex ids), GD-16 (growth — CONVO-16), GD-19 (the user
asked for a *hook*-based ingester; hooks are gated on the R-04 probe), GD-20
(the copy-verbatim list already contains every idempotency rule the Mongo
upserts need), plus a new GD for the dependency policy (CONVO-7) and the
D8.1/D8.2 disambiguation (CONVO-6).
