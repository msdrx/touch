# research — custom agent-state persistence (perspective: customstate), attempt 1

Scope: what "custom state" IS in this corpus, how it must be modelled in
MongoDB alongside the harness mirror, who writes it, how it references the
mirrored session/agent/node documents, orphan policy, composition with the
touch-events-v2 ref union and the `.touch/` store, migration of state files
already on disk, and the conflict rules that keep custom state from
masquerading as harness fact (GD-7 / D13 honesty).

All facts below were re-verified on disk this run. Empirical probes were run
in throwaway containers/dirs only; the live task folder was touched solely by
the two mandated `status.sh` calls.

## Verified substrate (basis for the findings)

- `grep -rn -i "custom"` over `touch-full-recon-plan.md`,
  `touch-aggregator-plan.md`, `touch-monitor-spawn-plan.md`, `inception.md`
  → **zero hits**. Custom state is entirely greenfield.
- `find / -name spawn-ledger.jsonl -o -name control.jsonl -o -name topology.json`
  and `find /home/laniakea/Projects/touch -type d -name state`
  → **zero results, filesystem-wide**. `/home/laniakea/Projects/touch/.touch`
  does not exist. The `touch-orchestrate` state standard has never been
  exercised.
- What *does* exist under `.claude/local-orchestrators/`: four task folders
  (`touch-aggregator`, `touch-full-recon`, `touch-mongo-live`,
  `touch-repo-recon`), each with `events.jsonl`, `orch-config.json`,
  `.watcher-state.json`, `findings/`, `orch-scripts/`, `plan/`, `report/`,
  and daemon logs.
- `status.sh:28-49` emits `{ts,plan,stage,state,detail}` (+`title` iff
  `ORCH_TITLE`). `decision_watcher.py:138-151` `emit()` with `extra=None`
  emits **the identical five keys**. Both append to the same
  `events.jsonl`. Confirmed on the live stream: 12 lines in
  `touch-mongo-live/events.jsonl` carry exactly those five keys and cannot be
  attributed to a writer.
- Mongo probe (throwaway `mongo:7` container, removed): a **subdocument `_id`
  is field-order sensitive** — `{runId,key,ordinal}` and `{key,runId,ordinal}`
  both inserted successfully as two distinct documents; an exact-subdocument
  equality query for the reordered form returned **0**, dot-notation returned
  1. BSON is type-strict: `{procStart:"10028"}` does not match
  `{procStart:10028}`.
- Provisioning probe: `pip download pymongo` succeeded through the proxy
  (pymongo 4.17.0 + dnspython 2.8.0); `docker pull mongo:7` succeeded (image
  already resident locally, 1.19 GB disk / 297 MB content). No `mongod` or
  `mongosh` on PATH; `pymongo` not importable in the system interpreter;
  Python 3.13.7.

---

## CUSTOMSTATE-1 — "custom state" is undefined in the entire normative corpus

- **file:line**: `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:1-871`
  (whole file); `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md:140-176` (D5)
- **severity**: blocker
- **scenario**: The user's ask (session transcript line 181: *"custom saving
  implementation, where we can create custom state into another mongodb
  collection and reference that to mapped session agents info"*) has no
  counterpart anywhere. `grep -i custom` over all three plans plus
  `inception.md` returns zero hits — the assistant said as much at transcript
  line 192 (*"has no counterpart in the plan at all"*). Meanwhile the corpus
  is full of things that *are* custom state under different names, spread
  across five documents with no unifying concept: touch-orchestrate
  `<task-dir>/state/<name>.json` (`SKILL.md:66-68`), `spawn-ledger.jsonl`
  (`SKILL.md:52-56`), control intents/acks (`SKILL.md:74-83`, GD-4, R-34),
  `state/topology.json` + `plan/<name>-subplans.json` (R-19,
  `touch-full-recon-plan.md:540-559`), `orch-config.json` (R-09), and the
  agent-asserted half of `events.jsonl`. `implement-plan`'s divider partitions
  by file ownership; with no taxonomy, three different sub-plans will each
  invent their own "state" collection shape and the ref semantics will diverge
  before the first gate.
- **recommendation**: The amendment must open with a **closed taxonomy** of
  exactly four classes, each with its owner, writer, and durability rule:
  (1) **mirrored harness fact** — immutable source-of-record, upsert-only from
  `~/.claude` (sessions, records, agents, run nodes, usage);
  (2) **derived** — computed by Touch from (1), always rebuildable, never
  authoritative (labels, rollups, GD-14 re-labels, liveness);
  (3) **orchestration state** — written by agents/scripts as files
  (spawn ledger, topology, sub-plan partition, `orch-config.json`,
  `status.sh` events), *assertions* about the run, not observations of it;
  (4) **Touch application state** — control intents/acks, user annotations,
  tags, pins, per-session UI prefs; the only class Touch itself authors.
  State the disposition explicitly: classes 3 and 4 are what "custom state"
  means; class 1 is never editable; class 2 is droppable at any time.

## CUSTOMSTATE-2 — `source` is a channel, not a trust class; nothing in touch-events-v2 separates fact from assertion

- **file:line**: `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md:119-137` (D4);
  `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:155-173` (GD-11)
- **severity**: blocker
- **scenario**: D4's record is
  `{v,seq,ts,source,kind,ref,data}` with
  `source: "ingest|hook|control|pty|legacy"`. That enum mixes trust levels:
  `ingest`/`hook`/`pty` are harness-observed fact, `control` is Touch-asserted,
  `legacy` is *both* (watcher-derived and agent-asserted lines in one file —
  see CUSTOMSTATE-3). Once custom state lands in Mongo, the single most
  important query the UI must run is "give me only what the harness actually
  says about this agent" — and it is **inexpressible** on `source`. GD-7's
  whole design ("harness facts create nodes, markers label them") and D13's
  honesty rules depend on a discriminator the record shape does not carry.
  In the file store this stayed tolerable because each `kind` had a
  hard-coded reader; in a shared Mongo collection, any consumer can query
  anything and a wrong `find()` silently renders an agent's self-report as
  ground truth.
- **recommendation**: Amend GD-11 to add a mandatory, **orthogonal**
  `provenance` field with exactly four values matching CUSTOMSTATE-1's
  classes: `"harness" | "derived" | "asserted" | "touch"`. Make it structurally
  impossible to forge: the custom-state writer module has no code path that can
  emit `provenance:"harness"` (assert it in a unit test), and the Mongo
  `$jsonSchema` validator on `custom_state` pins
  `provenance: {enum: ["asserted","touch"]}` while the mirror collections pin
  `{enum: ["harness","derived"]}`. Every UI surface that renders a non-`harness`
  value must carry the D13 provenance marker (dashed / "reported by agent").

## CUSTOMSTATE-3 — the two legacy writers are byte-identically shaped, so provenance for existing state is unrecoverable

- **file:line**: `.claude/shared/monitoring/status.sh:28-49`;
  `.claude/shared/monitoring/decision_watcher.py:138-151`
- **severity**: blocker
- **scenario**: `status.sh` builds
  `{"ts","plan","stage","state","detail"}` (+`title` iff `ORCH_TITLE` is set).
  `decision_watcher.emit()` with no `extra` builds *the same five keys in the
  same order* and appends to the same file. Calls like
  `decision_watcher.py:648` (`emit("plan", st, f"loop exited -> {info['plan']}", ts=ts0, plan=prev)`)
  and `:753` produce lines indistinguishable from an agent's `status.sh` call.
  Measured on `touch-mongo-live/events.jsonl` right now: 12 of 130 lines have
  exactly that shape. GD-14's dedup rule (`touch-full-recon-plan.md:207-209`,
  *"Dedup duplicate stage terminals on `(task, plan, stage, terminal-state)`,
  **watcher-wins**"*) presumes the two writers are separable — they are not.
  If the Mongo ingest stamps `provenance:"harness"` on watcher lines by
  guessing from the presence of `agent`/`tokens`/`quiet` keys, it will stamp
  agent assertions as harness fact for every terminal `plan done`/`plan failed`
  line — precisely the lines that drive verdict badges.
- **recommendation**: For `source:"legacy"`, ingest **must not guess**. Rules:
  lines carrying `agent` or `tokens` ⇒ `provenance:"derived"` (only the watcher
  emits those); lines carrying `title` ⇒ `provenance:"asserted"` (only
  `status.sh` reads `ORCH_TITLE`); **everything else ⇒
  `provenance:"unknown"`**, rendered with an explicit "writer unknown" marker,
  and excluded from any query that claims harness authority. Separately, fix
  it forward as a one-line change: add `"w":"watcher"` in
  `decision_watcher.emit()` and `"w":"agent"` in `status.sh`'s payload, and
  note in the amendment that this makes streams written *after* the change
  cleanly attributable (it is additive — the monitor's readers ignore unknown
  keys).

## CUSTOMSTATE-4 — Mongo composite refs are field-order sensitive and type-strict; the ref union breaks determinism as written

- **file:line**: `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:155-160` (GD-11 ref union);
  `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md:99-110` (D3)
- **severity**: blocker
- **scenario**: Two of the five ref members are multi-field
  (`{runId,key,ordinal}`, `{pid,procStart}`), and the transcript's design
  (line 197) proposes `run_nodes` with `_id: "runId:key:ordinal"` and
  `sessions` with `_id: "<pid>-<procStart>"`. Probed in a throwaway `mongo:7`:
  inserting `_id:{runId,key,ordinal}` and then `_id:{key,runId,ordinal}`
  **both succeed** — two documents for one logical node — and
  `find({ref:{key:…,runId:…,ordinal:…}})` against a document stored in the
  other order returns **0**. BSON also distinguishes types:
  `find({procStart:10028})` does not match a stored `procStart:"10028"`.
  Custom-state refs are constructed in at least four places under GD-15's
  layout (`legacy.py`, `agents.py`, `control.py`, plus whatever writes the
  ledger/topology), and `topology.json` / `subplans.json` are authored by
  JavaScript templates (`R-19`) whose key order is independent of Python's.
  One reordered dict literal produces a custom-state document that references
  a node nothing will ever join to — silently, with no error. That directly
  falsifies the "re-running ingestion converges to the same DB state"
  guarantee the whole Mongo case rests on.
- **recommendation**: (a) Never use a subdocument as `_id`. Add **one** shared
  `refkey(ref) -> str` canonicalizer that emits a delimiter-joined string with
  a stated escaping rule (`:` is safe for 17-hex ids, `wf_…` runIds, hex
  `key`s and `[a-z0-9_]` names; percent-escape it in the legacy
  `legacy:<task>:<id8>` arm, where `<task>` is a user-chosen folder name).
  (b) Store the structured `ref` **as well**, always with a fixed field order
  produced by the same helper, and query it **only by dot notation**
  (`ref.runId`, `ref.key`, `ref.ordinal`) — probed working and indexable.
  (c) Pin the BSON type of every ref field in the amendment: `pid` int,
  `procStart` **string** (it is `/proc/<pid>/stat` field 22, and D5's path
  form is a string), `ordinal` int, everything else string; enforce with
  `$jsonSchema` `bsonType` so a type drift fails loudly at write instead of
  silently at read. (d) A round-trip test: build every ref shape twice with
  keys inserted in different orders, assert equal `_id` and one document.

## CUSTOMSTATE-5 — the mirror must be upsert-only/no-delete, or the "durable record" claim is false

- **file:line**: `inception.md:115-119`;
  `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md:140-145` (D5 rationale)
- **severity**: blocker
- **scenario**: The headline justification for Mongo (transcript line 197,
  *"the DB becomes the durable record"*) is that the CLI retention sweep
  unlinks transcripts and `rm -rf`s whole subagent trees. But D6/G7's
  checkpoint rule is *inode change or `size < offset` ⇒ full idempotent
  re-ingest from 0*. If the aggregator implements resync as "clear this
  source's documents, then replay", a swept or truncated source destroys the
  mirrored copy — the only copy — and every custom-state document pointing at
  it becomes an orphan in the same tick. Nothing in the corpus states a
  no-delete invariant; D5 only says Touch never *writes* into `~/.claude`.
- **recommendation**: State as an amendment-level invariant: **the harness
  mirror is append/upsert-only; no code path issues `deleteOne`, `deleteMany`,
  `drop`, or `$unset` on a mirror collection.** Source disappearance sets
  `sourceAvailable:false` + `lastSeenAt`, never removes. Re-ingest from 0 is a
  replay of upserts keyed on natural ids (uuid / agentId / message.id /
  refkey), which is a no-op by construction. Enforce with a static test that
  greps the aggregator package for delete verbs outside a single explicitly
  named admin module, and with a Mongo role for the ingest user that grants
  `insert`/`update` but not `remove` on the mirror collections. Custom-state
  deletion is a *tombstone event*, never a physical delete (see
  CUSTOMSTATE-14).

## CUSTOMSTATE-6 — writer topology is undecided; agents must never hold a Mongo client

- **file:line**: `.claude/skills/touch-orchestrate/SKILL.md:52-56, 74-83`;
  `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:752-764` (R-34)
- **severity**: blocker
- **scenario**: Custom state is *inherently multi-writer* in the corpus as it
  stands: the orchestrating **session (an LLM agent)** writes the spawn ledger
  and the ack lines (`SKILL.md:52-56, 78-82`), while R-34 makes the control
  file *aggregator-owned*. The naive Mongo reading — "give agents a
  `mongo-status.sh`" — is a trap on three counts. (i) The connection string
  is a credential and would have to appear in every agent prompt, i.e. in
  every transcript on disk and in the mirror itself. (ii) pymongo's default
  `serverSelectionTimeoutMS` is 30 s; a Mongo-writing helper invoked from an
  agent's Bash call stalls that agent for 30 s per call when the daemon is
  down — flatly violating the rule already settled as discard #4
  (`touch-full-recon-plan.md:846-848`: *"a best-effort writer must never break
  an agent"*, cf. R-10). (iii) It puts a pip dependency inside agent
  execution, which is a far wider D8 amendment than the aggregator needs.
- **recommendation**: Decide explicitly, in the amendment: **agents write
  files only; the aggregator process is the sole MongoDB writer.** Agent-side
  helpers stay single-`write()` line appends under `flock` exactly like
  `status.sh` — no network, no credential, no new dependency. The aggregator
  tails those files with the R-23 tailer and projects them into Mongo. This
  keeps the D5/D8 amendment scoped to one process, keeps the file the
  crash-durable WAL (so a mongod outage loses nothing), and preserves the
  "never break an agent" invariant. Connection string comes from
  `TOUCH_MONGO_URI` in the aggregator's environment only — never a repo file,
  never a prompt.

## CUSTOMSTATE-7 — the ref union cannot address what custom state actually attaches to

- **file:line**: `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:155-160`
- **severity**: major
- **scenario**: GD-11's union is
  `{uuid} | {toolUseId} | {agentId} | {runId,key,ordinal} | {pid,procStart}`.
  Every real piece of custom state in the corpus attaches to something *not*
  in that list: a control intent addresses a **logical slot name**
  (`{"action":"stop","name":"<name>"}`, `SKILL.md:78`), `topology.json`
  attaches to a **sub-plan id** (`sp-…`, R-19), `orch-config.json` and the
  legacy adapter attach to a **task folder** (GD-14 synthesizes
  `taskId = folder name`), and a `status.sh` line attaches to a
  `(plan, stage)` pair. GD-11 says the validator is "open at the tail —
  unknown ref shapes are retained and passed through", which in a JSONL store
  means "harmless"; in Mongo it means **unvalidated, untyped, and unindexed**,
  so those refs get no `$jsonSchema` coverage and every lookup on them is a
  collection scan.
- **recommendation**: Promote two shapes from tail-retained to **validated
  members** of the union in the amendment:
  `{root, name, attempt}` (Touch logical slot) and
  `{task, plan, stage?, attempt?}` (orchestration scope, covers legacy folders
  and sub-plan ids via `plan`). Give each a `refkey()` encoding
  (CUSTOMSTATE-4) and a compound index. Keep the tail open for genuinely
  unknown shapes but record them with `provenance:"unknown"` and exclude them
  from joins rather than letting them scan.

## CUSTOMSTATE-8 — the (name, attempt) → agentId binding needs a first-class `slots` collection

- **file:line**: `.claude/skills/touch-orchestrate/SKILL.md:11-13, 30-32`;
  `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:109-116` (GD-7)
- **severity**: major
- **scenario**: `SKILL.md:11-13` states the constraint plainly — *"The harness
  `agentId` is read-only and unknowable before spawn"* — and `:30-32` states
  the contract — *"Each (name, attempt) pair binds to exactly one harness
  `agentId`"*. GD-7 then puts names in a *separate label layer* that a missing
  marker may never populate. So every custom-state document authored by an
  orchestrator is addressed by **name**, while every mirrored document is
  addressed by **agentId**, and the mapping between them is a runtime join
  that (a) does not exist as a collection, (b) is populated from three
  different evidence channels (`[touch]` marker, ledger line, Agent-tool
  `description`), and (c) may never complete. Without a materialized join,
  "show me the custom state for this agent" is a scan of the marker layer per
  render.
- **recommendation**: Add a `slots` collection as the amendment's central new
  entity:
  `_id: refkey({sessionKey, root, name, attempt})`, fields
  `{sessionKey, root, name, parent, role, attempt, agentId|null, taskId|null,
  runNode:{runId,key,ordinal}|null, boundBy: "marker"|"ledger"|"description"|null,
  boundAt, resolution}`. Unique **sparse** index on `agentId`; compound index
  on `(sessionKey, root, name, attempt)`. All class-3/4 custom state refs the
  slot; the slot is the only place the name↔agentId hop happens. This is also
  where GD-8's two profiles reconcile without a second code path: Agent-tool
  profile fills `agentId` + `taskId` (stop available); Workflow profile fills
  `agentId` + `runNode` with `taskId:null` (stop rendered disabled with the
  reason).

## CUSTOMSTATE-9 — orphan policy: refs are forward-references by construction, and some never resolve

- **file:line**: `.claude/skills/touch-orchestrate/SKILL.md:50-56`;
  `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:109-116, 131-140` (GD-7, GD-9)
- **severity**: major
- **scenario**: MongoDB enforces no referential integrity, and this design
  *requires* dangling refs to be normal: the ledger line is written
  "immediately after each spawn" with a name whose `agentId` is not yet
  mirrored (the tailer may be a tick behind, and the marker only becomes
  visible once the agent's first `user` record lands). Worse, GD-7 explicitly
  permits nodes with **no marker at all** ("a missing marker degrades the
  label, never the node") and GD-9 rejects markers outside the first-4-line
  window as `marker-misplaced` — so a slot can be *permanently* unbindable.
  Separately, the "exactly one agentId per (name, attempt)" contract is
  enforced by nothing but an LLM's care; a copy-pasted marker yields two
  agentIds for one slot, and a unique index on `slots.agentId` would then
  **raise `DuplicateKeyError` inside the ingest loop and kill the tailer** —
  agent-authored data taking down the ingest process.
- **recommendation**: Make resolution an explicit, queryable state machine on
  every custom-state and slot document:
  `resolution: "pending" | "bound" | "orphaned" | "conflict"`, with
  `pendingSince`. Rules: refs start `pending`; a bind attempt that collides
  with an existing `agentId` writes `conflict` (recording **both** agentIds)
  and **never raises** — catch `DuplicateKeyError` explicitly, log to
  `/health`'s parse-failure counter genre, continue; a `pending` ref older
  than the run's terminal event *or* older than a stated TTL (suggest 300 s,
  aligned with the 180 s idle threshold plus slack) becomes `orphaned`. The UI
  renders all three non-`bound` states honestly (D13) — "not yet joined",
  "never joined — no marker", "conflicting binds" — and **never** hides an
  orphan, because an orphaned control intent means a stop request that went
  nowhere.

## CUSTOMSTATE-10 — the ledger line carries no session scope and no root; ROOT_NAME collides across sessions

- **file:line**: `.claude/skills/touch-orchestrate/SKILL.md:52-56`
- **severity**: major
- **scenario**: The normative ledger line is
  `{"name","parent","role","attempt","taskId","ts"}` — no `root`, no
  `sessionId`, no `(pid,procStart)`, even though the `[touch]` marker
  (`SKILL.md:42`) carries `root=`. Today the only session attribution is *the
  path the line was read from*, which R-20 is in the middle of relocating
  (see CUSTOMSTATE-11). Once these lines become Mongo documents the path is
  gone. Two sessions in this same repo running `/execute-research` on
  successive days both choose `ROOT_NAME` from the task name — the corpus
  already shows near-identical task names (`touch-repo-recon`,
  `touch-full-recon`) — and a slot `_id` of `<root>:<name>:<attempt>` collides
  across them, cross-linking one session's custom state onto another
  session's agents.
- **recommendation**: Amend the ledger line to
  `{"name","parent","root","role","attempt","taskId","sessionKey","ts"}` where
  `sessionKey` is the `(pid,procStart)` string form — the orchestrating
  session knows its own pid, so this is derivable with no new capability. Make
  `sessionKey` the first component of every slot `_id` (CUSTOMSTATE-8). For
  ledger lines already lacking it (none exist today — CUSTOMSTATE-12), the
  ingest derives `sessionKey` from the containing path and records
  `sessionKeySource:"path"`.

## CUSTOMSTATE-11 — the control-file path on disk contradicts the plan; custom-state ingest cannot be sequenced first

- **file:line**: `.claude/skills/touch-orchestrate/SKILL.md:74-76` vs
  `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:571-577` (R-20)
  and `:757-760` (R-34)
- **severity**: major
- **scenario**: The skill file **as it exists on disk right now** says: watch
  `.touch/control.jsonl`, *"fall back to `<task-dir>/control.jsonl` if no
  `.touch/` exists"*. R-20 says that relative-path fallback is **deleted** and
  the control file becomes per-session and aggregator-owned at
  `<TOUCH_STATE_DIR>/sessions/<pid>-<procStart>/control.jsonl`, addressed via
  `ledger=<abspath>` in the `[touch]` marker; R-34 repeats it. Neither path
  exists on disk (verified: zero `control.jsonl` filesystem-wide). If the
  Mongo amendment adds a custom-state ingest item that is scheduled before
  R-20, the implementer reads the skill (the normative-looking artifact) and
  tails the wrong location; if scheduled after, it depends on an item in
  phase 2/4 that is itself gated (R-34 is blocked on the R-04 probe, GD-19).
- **recommendation**: State the dependency explicitly in the amendment:
  custom-state ingest of control intents/acks **depends on R-20** and is
  sequenced after it; do not restate the path in the amendment (that would
  create a third conflicting source). Until R-20 lands, the ingest reads a
  **configured list** of control-file paths from `orch-config.json` /
  `TOUCH_CONTROL_PATHS` and records `pathSource` on each ingested intent, so a
  later relocation is a config change rather than a re-ingest. Add the
  amendment's own disposition line for `SKILL.md:74-76` so the contradiction
  is closed rather than inherited.

## CUSTOMSTATE-12 — "migrate the existing state files" is a phantom item; the real migration surface is different

- **file:line**: `.claude/skills/touch-orchestrate/SKILL.md:52, 66-68`
  (the standard); `.claude/local-orchestrators/` (the actual disk contents)
- **severity**: major
- **scenario**: The obvious amendment item — "migrate the state files that
  already exist on disk into Mongo" — would be a **no-op that consumes a
  sub-plan and a gate cycle**. Verified filesystem-wide this run: zero
  `state/` directories, zero `spawn-ledger.jsonl`, zero `control.jsonl`, zero
  `topology.json`, and no `.touch/` anywhere. Nothing has ever written to the
  touch-orchestrate state standard. Meanwhile the custom state that *does*
  exist is not what that item would look for: four task folders' `events.jsonl`
  (mixed-writer, class 2+3 — CUSTOMSTATE-3), four `orch-config.json` (class 3
  config that GD-14 mines for `runId` via `basename(wf_dir)`), four
  `.watcher-state.json` (explicitly **not** to be read — GD-14,
  `touch-full-recon-plan.md:213-214`), `findings/*.md` and `plan/*.md`
  (artifacts referenced by path in the handoff protocol), `orch-scripts/`, and
  two daemon logs.
- **recommendation**: Replace "migration" with two precisely scoped items.
  (a) **Backfill**: run the R-27 legacy adapter's GD-14 rule set as the Mongo
  ingest's `source:"legacy"` arm over the four task folders — this is already
  planned work, so the amendment should say "R-27's output is a Mongo sink,
  not a new adapter", not re-plan it. (b) **Artifact registry**: one
  `artifacts` custom-state kind recording `{taskId, kind:
  "findings"|"plan"|"report"|"script"|"config"|"log", path, sha256, size,
  mtime}` — paths and digests only, **never bodies** (they are repo source and
  can be large). Explicitly state that `.watcher-state.json` is excluded and
  that there is nothing else to migrate, so a later implementer does not go
  looking.

## CUSTOMSTATE-13 — secrets and unredacted content must have a written deny-list before the first mirror write

- **file:line**: `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:184-194` (GD-13),
  `:24` (D5 amendment: *"`server.json` holds live token 0600"*),
  `:287-297` (R-01); `inception.md:115-119`
- **severity**: major
- **scenario**: The `.touch/` tree that a Mongo mirror would naively slurp
  contains `server.json` with the **per-boot 256-bit auth token at mode
  0600**, and the session event streams contain unredacted transcript content
  (the exact reason `.touch/` is gitignored — PRODUCT-2's blocker rationale).
  A default `mongo:7` container runs **with no authentication**; publishing it
  for convenience (`sbx ports … 27017:27017`) exposes an unauthenticated
  database holding the Touch auth token and every prompt, tool result and file
  excerpt the session ever saw. Additionally, a Docker volume or `mongodump`
  directory placed under the repo would not be covered by R-01's ignore list
  (which adds `.touch/`, `.touch*/`, `*.pid`) and could be committed.
- **recommendation**: Write the deny-list into the amendment as an invariant,
  not a note: **never mirrored** — `.touch/server.json` (any field),
  `~/.claude/.credentials.json`, `~/.claude.json`, any env var matching
  `(?i)(token|secret|key|password|auth)`. Provisioning rules: `mongod` binds
  `127.0.0.1` only; auth enabled with a dedicated `touch` user; URI supplied
  via `TOUCH_MONGO_URI` in the aggregator's environment only (CUSTOMSTATE-6);
  the ingest DB user holds `insert`/`update` on mirror collections and no
  `remove` (CUSTOMSTATE-5). Extend R-01's ignore list additively with
  `mongo-data/`, `mongo-dump/`, `*.bson` and add a negative test in the
  `test_shell.py` genre asserting `git check-ignore` passes for a
  repo-relative Mongo data path.

## CUSTOMSTATE-14 — custom state must be an append-only event log with a derived reduction, not mutable documents

- **file:line**: `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md:126-127` (D4),
  `:190-196` (D7 control audit)
- **severity**: major
- **scenario**: D4 is explicit — *"Event log, not last-writer-wins; state is
  derived by reduction"* — and D7 makes the audit load-bearing: *"The harness
  records nothing about stops … the control log is the only record"*, which
  GD-4's `requested / pending / sent / confirmed` state machine and R-15's
  stopped-vs-crashed arbitration both consume. Mongo's ergonomics push hard the
  other way: `updateOne({_id}, {$set:{state:"confirmed"}})` is the obvious
  implementation, and it **destroys the transition history** — after which
  "was this agent stopped or did it crash?" becomes unanswerable and R-15's
  arbitration silently loses its only input.
- **recommendation**: Two collections, stated explicitly. `custom_state_events`
  — append-only, immutable, insert-only (no `update` grant for any writer),
  one document per appended line with `(stream, seq)` preserved from the source
  file so the file remains a replayable WAL; this is the source of truth.
  `custom_state` — the reduced current view, every document carrying
  `derived:true` + `fromSeq`, fully rebuildable by replaying the events
  collection, and **droppable** as a recovery procedure. The read API and the
  UI write only to the events collection; nothing writes the reduction except
  the reducer. Deletion of a user annotation is a tombstone event, never a
  `deleteOne`. Test: build a state, drop the reduction, rebuild, assert
  document-for-document equality.

## CUSTOMSTATE-15 — the "never masquerade as fact" rule needs structural enforcement, not convention

- **file:line**: `.claude/local-orchestrators/touch-aggregator/plan/touch-aggregator-plan.md:302-315` (D13);
  `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:109-116` (GD-7),
  `:142-152` (GD-10)
- **severity**: major
- **scenario**: The concrete conflict is already on record in this repo:
  `touch-aggregator/events.jsonl` contains an agent-asserted
  `research plan failed "loop exited -> synthesis"` nine minutes before
  `synthesis plan done "plan written"` — *"the repo's flagship success is on
  record as a failure"* (transcript line 67). GD-10 fixes the semantics but
  not the shape: in a single Mongo database, any module can
  `db.agents.updateOne({_id: agentId}, {$set: {state: "failed"}})` from an
  agent-asserted line and the mirror now *is* the lie, with no trace of who
  wrote it. Convention ("don't do that") does not survive a divider splitting
  the work across sub-plans with different implementers.
- **recommendation**: Enforce with three mechanisms, all cheap. (1) **Payload
  namespace**: every custom-state document's payload lives under
  `data.custom.*`; a `$jsonSchema` validator with
  `additionalProperties:false` at the top level rejects any custom document
  that grows harness-named fields (`usage`, `message`, `toolUseResult`).
  (2) **Write-path isolation**: exactly one module owns handles to the mirror
  collections (GD-15 gives it a name and an owner file); a static test asserts
  no other module in `aggregator/` references those collection names — the
  same genre as the existing `test_shell.py` / `test_frontend.py` source
  guards. (3) **Read-side default**: the reader helper that serves the UI takes
  an explicit `provenance` filter with **no default**, so a consumer must state
  which trust class it wants; a call site that forgets fails at import-time
  argument checking rather than silently returning a mixture.

## CUSTOMSTATE-16 — user annotations are the first user-authored durable data and need their own rules

- **file:line**: `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:172-173` (GD-11 detail cap);
  `:184-194` (GD-13 token auth)
- **severity**: minor
- **scenario**: Everything else in the corpus is machine-generated and
  reconstructible. Annotations, tags and pins are not: they survive the run,
  the session and the retention sweep, and losing one is real data loss. Three
  concrete gaps. (i) There is **no user identity** anywhere in Touch —
  GD-13's per-boot token authenticates a *browser*, not a person — so an
  `author` field would be invented data, violating D13. (ii) GD-11 caps
  `detail` at 1 KB *by truncation at the writer* ("the real reason is
  shell/JS-template embedding"); applying that cap to a user's prose silently
  eats the tail of what they typed. (iii) Edit/delete semantics are undefined
  against CUSTOMSTATE-14's append-only rule.
- **recommendation**: `author: "local"` recorded literally, with a written note
  that Touch has no user identity model and the field exists for a future one —
  never a hostname, username or fabricated id. Annotations get their **own**
  cap (suggest 16 KB) that **rejects with 413 rather than truncating** —
  machine detail strings truncate, user prose does not. Edits are new events
  superseding by `(target ref, annotationId)`; deletes are tombstones; the
  reduction shows the latest non-tombstoned version and the UI can surface
  "edited N times". Annotation text is escape-first rendered (GD-20) exactly
  like event text — it is the one field a human can put markup in.

## CUSTOMSTATE-17 — restate the per-session/per-task collection discard for custom state specifically

- **file:line**: `.claude/local-orchestrators/touch-monitor-spawn/plan/touch-monitor-spawn-plan.md:24-28` (G2)
- **severity**: minor
- **scenario**: G2's discard register rejected **per-session Mongo
  collections** as an anti-pattern (breaks cross-session queries, duplicates
  indexes) — a decision the amendment inherits, since the amendment is exactly
  the "explicit D5/D8 amendment" G2 anticipated. Custom state is where that
  anti-pattern will re-appear under a new name, because the file layout it
  mirrors *is* per-scope: `<task-dir>/state/`, `.touch/sessions/<pid>-<procStart>/`,
  `.touch/runs/<runId>/`. A literal path→collection mapping produces
  `custom_state_touch_mongo_live`, `custom_state_touch_full_recon`, … and the
  first cross-run question ("every stop intent that was never confirmed")
  becomes a loop over collections.
- **recommendation**: State the disposition explicitly rather than assuming it
  carries: **G2's per-session-collection discard STANDS and extends to custom
  state.** One `custom_state` collection and one `custom_state_events`
  collection for the whole installation, discriminated by
  `kind` (`"ledger"|"control_intent"|"control_ack"|"topology"|"agent_state"|"annotation"|"tag"|"artifact"`)
  and scoped by `ref` + `sessionKey`. Directory structure is a *source path*,
  never a collection name.

## CUSTOMSTATE-18 — the `.touch/` store's fate and the exact D5/D8 amendment wording are unstated

- **file:line**: `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:618-627` (R-24),
  `:222-228` (GD-15), `:601-607` (R-22 stdlib-only)
- **severity**: minor
- **scenario**: R-24 makes `aggregator/store.py` the touch-events-v2 store
  under `.touch/`; R-22 pins stdlib-only. The amendment can mean three
  different things and the plan does not say which: (a) Mongo **replaces**
  `.touch/` — kills crash-durability whenever mongod is down, makes a database
  a hard dependency of watching a local session, and orphans R-24's tests;
  (b) `.touch/` stays the **WAL** and Mongo is a projection; (c) Mongo holds
  **only** custom state while harness data stays in files — which defeats the
  user's stated ask (referencing mirrored session/agent records across
  collections). GD-15's "one file, exactly one owner" also means a new sink
  file needs an owner *before* the divider runs, or two sub-plans will both
  claim `store.py`.
- **recommendation**: Adopt (b) and say so: `.touch/` JSONL remains the
  crash-durable write-ahead log and the boot-replay source; Mongo is a
  **projection** the aggregator syncs asynchronously, and ingestion **never
  blocks** on mongod. A Mongo outage degrades to a visible "sync lagging N
  events" indicator (D13), and the UI continues to read the in-memory
  reduction. Amend D8's wording precisely — from "stdlib-only at runtime" to
  "**stdlib-only on the ingest and serve critical path**; `pymongo` is
  permitted in the projection sink only, and its absence degrades sync rather
  than failing startup" — and amend GD-15 with the new owned file
  (`aggregator/mongo_sink.py` + `tests/test_mongo_sink.py`) so file ownership
  is unambiguous for the divider. R-24 is *extended* (a sink interface), not
  replaced.

## CUSTOMSTATE-19 — provisioning is verified; record it so the amendment is not blocked on an unproven path

- **file:line**: `.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md:328-340` (R-04 probe genre);
  `inception.md:167-170` (installs work through the proxy)
- **severity**: nit
- **scenario**: The environment brief for this run recorded "no mongod binary,
  pymongo not installed, Docker daemon running with zero containers" — which
  reads as an open provisioning risk, and GD-19's precedent is that an
  unverified delivery path blocks its items ("Never ship this item while its
  delivery path is unverified", R-36). Both paths were probed this run and
  **both work**, so the amendment should not carry a phantom blocker.
- **recommendation**: Record in the R-04 probes artifact
  (`touch-full-recon/report/probes.md`, AUDIT-16 provenance convention —
  command + date): `pip download pymongo` → pymongo **4.17.0** +
  dnspython 2.8.0, manylinux wheel, succeeded through the proxy;
  `docker pull mongo:7` → succeeded, digest
  `sha256:9bdaeb6d…`, image resident locally (1.19 GB disk / 297 MB content);
  a throwaway `mongo:7` container started and answered `ping` in under 30 s on
  `127.0.0.1:27099`; no `mongod`/`mongosh` on the host PATH (use the container
  or `docker exec … mongosh`); host Python is **3.13.7** (pymongo 4.17 has a
  cp313 wheel, so no compiler needed). Also record the two probe results from
  CUSTOMSTATE-4 (subdocument `_id` field-order sensitivity; BSON type
  strictness) as normative constraints, since they change the schema rather
  than just the provisioning story.
