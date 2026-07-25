# touch-mongo-live — partition into sub-plans

Divider output, 2026-07-25. Inputs: `touch-mongo-live-plan.md` (AMENDMENT,
GD-21…GD-30, R-38…R-58, §2 dispositions) + `touch-full-recon-plan.md` (BASE,
GD-1…GD-20, R-01…R-37). Tree verified: no application source exists; only the
monitoring module (`.claude/shared/monitoring/` + tests) and the two skill
templates. 15 sub-plans, listed in **execution order** (the driver runs them
serially in array order). File ownership is absolute: every file below has
exactly one owning sub-plan across the whole partition; cross-file items are
split into per-file halves with the shared decision restated in each half.

Item-id suffixes used for split items: `R-nn:<half>` (e.g. `R-42:gitignore`).
Both halves cite the same plan item; neither half may drift from it.

---

## Shared decisions (restated where owned; stated once here in full)

- **SD-1 — Mirror mapper registry (closes the mirror.py ownership trap).**
  `aggregator/mirror.py` has ONE owner (sp-06). The per-entity Mongo mappings
  that R-46…R-51 describe ("+ mapping in `mirror.py`") are implemented as
  **pure mapper functions exported by the owning entity module**
  (`sessions.py`, `ingest.py`, `legacy.py`, `agents.py`, `custom_state.py`)
  under a module-level `MIRROR_MAPPERS` registry: no I/O, no pymongo import,
  each returns `(collection, _id-string built via refs.ref_key, update-op dict
  using only mongo_store's op-builder vocabulary — $max/$addToSet/$min/
  $setOnInsert)`. `mirror.py` discovers and drives them lazily and, with
  `mongo_store.py`, remains the only module that may import pymongo (GD-21)
  and the only module importing both file-side and Mongo-side (GD-15).
- **SD-2 — Stdlib guard born with the exception.** The R-22 stdlib-only static
  guard is created (sp-04) already naming its single exception: pymongo
  importable only from `aggregator/mongo_store.py` and `aggregator/mirror.py`
  (GD-21). The suite is never red between sub-plans.
- **SD-3 — One verbatim `.gitignore` entry list.** Entries added by sp-01 and
  asserted by sp-03's `test_shell.py` guard, identical text both sides:
  `.touch/`, `.touch*/`, `.claude/settings.local.json`, `*.pid`,
  `.claude/local-orchestrators/*/.watcher-state.json`, `mongo-data/`,
  `mongo-dump/`, `*.bson` — plus the negative assertions (nothing ignores
  `.claude/local-orchestrators/` itself or `events.jsonl` under it).
- **SD-4 — R-58 terminal-conflict rule, verbatim both sides.** Forward fix
  (sp-03) and read-time legacy rule (sp-09): conflicting terminals on the same
  `(task, plan, stage='plan')` resolve **last-event-wins in file order** — a
  later corrective `done` beats an earlier fabricated `failed`; RUNSTATE-7's
  watcher-wins dedup applies ONLY to same-state duplicates. Re-label
  ("`plan failed` + detail `loop exited ->` + all stage agents resulted" ⇒
  "closed — no verdict", `derived_from_legacy:true`) applies to all three
  affected historic streams; no stream is ever rewritten.
- **SD-5 — Amended GD-1 commit gate.** The no-commit-while-watcher-writes gate
  checks only watchers whose `ORCH_STATE_DIR` is inside the paths being
  committed (three orphan watchers may be live). sp-01 applies this scoping;
  watcher self-exit (R-40) lands later and is NOT a precondition of C1/C2.
- **SD-6 — Commit boundary.** ONLY sp-01 commits (exactly C1 + C2 per GD-2,
  after branch rename master→main and repo-local identity). Every later
  sub-plan leaves its changes uncommitted. Never revert/stash.
- **SD-7 — Docs have one late owner.** `README.md`, `CLAUDE.md`,
  `inception.md`, `touch-full-recon-plan.md`, `probes.md`,
  `docs/control-semantics.md` are owned solely by sp-15 (last), so docs
  describe shipped reality; the doc halves of R-40/R-05/R-33/R-57 are executed
  there. Exception by necessity: `docs/mongo.md` is owned by sp-06 (its
  acceptance test reads the recipe; the R-57 mongo.md clauses land there too).
  The hard preconditions of M1+ are only R-01…R-03 + R-08/R-09/R-13 green
  (amendment §3), so deferring R-04/R-05/R-06 to the tail is legal.
- **SD-8 — R-20 fallback.** Control-intent/ack ingest (R-53 arm) reads a
  configured path list from `TOUCH_CONTROL_PATHS`, records
  `pathSource`, and never restates the control-file path (CUSTOMSTATE-11).
  R-20 itself is out of scope this pass.
- **SD-9 — Topology-optional reducer.** R-54 consumes topology from the
  `custom_state` collection (kind `topology`) strictly per the GD-24 schema —
  a shape, not a code dependency — and its absent-topology arm ("attempt N",
  no denominator, no next-stage arrow) is normal. This lets sp-10 (agents.py,
  M2 content + reducer) run before sp-11 (custom state, M3) without violating
  one-file-one-ownership of `agents.py`.
- **SD-10 — Shrink/sweep boundary.** `tailer.py` (sp-04) detects shrink via
  checkpoint identity `(st_dev, st_ino, size, offset)` with `size < offset`
  explicit, and signals a full idempotent re-ingest; the GD-26 generation
  mark-and-sweep that re-ingest must run is `mirror.py`'s (sp-06). Restated in
  both.
- **SD-11 — Key/algebra law everywhere.** All `_id`s are strings from
  `refs.ref_key` (GD-24 grammar, %-escaping of `% # | :`, zero-padded ints);
  accumulation is `$max`, multi-value `$addToSet`, immutables `$setOnInsert`;
  no `$inc`, no bare `$set` on accumulables; deltas wire-only; no TTL; no
  deletes except the one `stream_meta` renumber case; provenance field
  mandatory per GD-28. Every sub-plan that writes or maps documents honors
  this without local variation.

---

## sp-01 — repo-bootstrap
**Title:** Gitignore hardening + git bootstrap (identity, main, C1/C2)
**Owned files:**
- `.gitignore` (additive edit)
- `.git/config` / branch state (repo-local identity, master→main rename)
- `.gitkeep` files in legitimately-empty task dirs
  (`touch-full-recon/report/` is populated; add `.gitkeep` to empty `plan/`
  and `report/` dirs per RUNSTATE-18)
- new `tests/test_bootstrap.py` (top-level tests/ — shell-check style)
**Items:** R-01:gitignore (base), R-02 (base, THE one commit exception),
R-42:gitignore (amendment — `mongo-data/`, `mongo-dump/`, `*.bson`).
**Shared decisions:** SD-3 (verbatim entry list; the guard half lands in
sp-03), SD-5, SD-6. GD-2 verbatim: C1 "tooling and docs" (README, CLAUDE.md,
inception.md, .gitignore, .claude/settings.json, .claude/statusline.sh,
.claude/skills/**, .claude/shared/monitoring/**), C2 "orchestration history"
(.claude/local-orchestrators/**), C2 contains no `.watcher-state.json`
(ignored by then). Nothing before C1 may assume HEAD exists. Do not touch any
in-flight orchestrator state; C2 is taken with SD-5's scoped gate.
**Test:** `git rev-parse HEAD` succeeds; `git check-ignore .touch/x` and
`mongo-data/x` pass; negative: `git check-ignore` fails for
`.claude/local-orchestrators/x/events.jsonl`.

## sp-02 — fixtures-freeze
**Title:** Freeze all reference fixtures (base + mirror corpora) with manifest
**Owned files:**
- `tests/fixtures/**` (all of it): `run-wf_829e6f58/` (journal, 9 agent
  transcripts incl. both `a2fc883c…` disjoint continuations, 7 `.meta.json`,
  snapshot, 3 `tool-results/*.txt`), `legacy/` (verbatim event lines:
  two-wave respawn, `plan|failed "loop exited -> synthesis"`, duplicate
  terminals, mixed ts formats), the R-41 additions: cross-session
  `a2fc883c96ff7b837` pair (223-line + 2-line-no-meta),
  `wf_455b348c-e17/` (3-key retry, killed run, agentCount 6 vs 9), a
  live-run-shape dir (journal + agents, NO `<runId>.json`), dotted-key
  `file-history-snapshot` records, the 872 KB line, the
  queue-operation/user pair, four foreign `/tmp` slug dirs as negative
  discovery fixtures, verbatim `touch-mongo-live/events.jsonl` lines incl.
  the 12 unattributable ones and the failed-then-done correction lines, plus
  the `touch-full-recon` stream lines and `wf_930e210a`/`wf_cca84d59`
  journals (R-58 replay set)
- new `tests/test_fixtures.py` (sha256 manifest completeness + byte-stability)
**Items:** R-03 (base, wording per amended R-03: "disjoint continuations"),
R-41 (amendment), R-58:fixtures.
**Shared decisions:** copy NOW (retention-sweep clock); fixtures are verbatim
bytes, sanitize only if credentials found; manifest checked in. Downstream
sub-plans reference these paths read-only — no other sub-plan may add or edit
fixtures.
**Test:** manifest test is the test.

## sp-03 — watcher-templates-firstwave
**Title:** Monitoring first wave: watcher truth, terminal events, w-field,
lifecycle, write integrity (kills the fabricated FAILED badge at the source)
**Owned files:**
- `.claude/shared/monitoring/decision_watcher.py`
- `.claude/shared/monitoring/status.sh`
- `.claude/shared/monitoring/monitor_server.py` (R-10 slice ONLY:
  flock/health parse-failure counter; R-11's fixes are out of scope)
- `.claude/shared/monitoring/monitoring.md` (R-39 schema note only; R-17's
  full refresh is out of scope)
- `.claude/shared/monitoring/tests/test_shell.py`, `tests/test_watcher.py`,
  `tests/test_server.py` (test_frontend.py untouched)
- `.claude/skills/execute-research/templates/research.workflow.js`
- `.claude/skills/implement-plan/templates/implement.workflow.js`
**Items (base):** R-07, R-08, R-09, R-10, R-13, R-01:guard (extend
`test_gitignore` per SD-3, incl. the negative assertions).
**Items (amendment):** R-39, R-40:watcher+templates (self-exit after journal
quiet ≥N s AND terminal `complete` event; driver epilogue stops daemons on
`orchestrator complete` — CLAUDE.md/plan-file wording halves go to sp-15),
R-58:watcher+templates (execution scope IS R-08+R-09+R-13; replay the three
real streams from sp-02 fixtures ⇒ zero `failed` badges on research/synthesis
plans; failed-then-done fixture renders `done`; static guard that templates
contain the terminal `plan done` + `orchestrator complete done` calls).
**Shared decisions:** SD-3, SD-4 (forward half), GD-10 close predicate
verbatim (`decisive.get(p) if p in decisive else last_result_ok.get(p,
False)`; verdict-less non-failure ⇒ "closed, no verdict", never `failed`;
`last_plan` heuristic gated on `strategy=="serial"`); GD-11 detail cap 1 KB;
full 17-hex agentId emitted (8-char only as `shortId`); template edits stay
inside the R-09/R-40 scope (R-14/R-15/R-18/R-19/R-21 explicitly NOT done
here); five-key event shape preserved, `w` purely additive.
**Gate:** this sub-plan green = the GD-23 hard precondition for every mirror
write; nothing after sp-04 may start if it is red.

## sp-04 — aggregator-core
**Title:** Scaffold + file-side leaf modules: tailer, store (v2 WAL), ws codec
**Owned files:** new `aggregator/__init__.py`, `aggregator/tailer.py`,
`aggregator/store.py`, `aggregator/ws.py`, `tests/run_all.sh`,
`tests/test_tailer.py`, `tests/test_store.py`, `tests/test_ws.py`, and the
stdlib-only static guard (in `tests/run_all.sh` or its own small test file).
**Items:** R-22:aggregator (touch-visual skeleton half moves to sp-13), R-23
(base + amended clause), R-24 (base, **spec stands unchanged — Mongo does not
replace store.py**; scalar stream/seq additions live in mongo_store, not
here), R-29.
**Shared decisions:** SD-2 (guard born with the two-file pymongo exception),
SD-10 (tailer detects `size < offset` shrink + inode change ⇒ signals full
idempotent re-ingest; sweep itself is sp-06's), SD-11; GD-20 copy-verbatim
list (torn-tail cut at last `\n`, flock'd appends, checkpoint keyed to
source); incremental reads, never per-tick full re-parse; store: single
writer per stream, per-file `seq`, `(stream,seq)` cursors, ref union
open-tail validator, one ts format `…Z`, four-key token records.
**Test:** each module's item-specified tests; `run_all.sh` green.

## sp-05 — refs-mongostore
**Title:** Mongo foundations: ref_key canonicalizer + collections/indexes/
upsert algebra/validators
**Owned files:** new `aggregator/refs.py`, `aggregator/mongo_store.py`,
`tests/test_refs.py`, `tests/test_mongo_store.py`.
**Items:** R-43, R-44 (amendment).
**Shared decisions:** SD-1 (mongo_store exports the op-builder vocabulary the
mappers use; never touches files), SD-2, SD-11; GD-24 table verbatim
($jsonSchema bsonType pins, unique `{stream:1,seq:1}`, no TTL anywhere);
GD-25 acceptance test (normal/shuffled/reversed ingest ⇒ identical
fingerprint AND expected counts); `_raw`-wrapping for variable-key subtrees;
oversize >8 MB ⇒ stub, never dropped; `writeErrors` always inspected;
aggregator supplies every `ts` (Date + `tsRaw`); dotted-`_id` queries
forbidden (IXSCAN via explain() asserted). All Mongo tests skip cleanly
without a reachable mongod; every module imports without pymongo installed.

## sp-06 — mirror-deploy
**Title:** Mirror runtime (queue/breaker/lease/cursors/sweep/rebuild/backfill)
+ Mongo deployment & security baseline + mongo.md
**Owned files:** new `aggregator/mirror.py`, `docs/mongo.md`,
`tests/test_mirror.py`, `tests/test_mongo_deploy.py`.
**Items:** R-45, R-42:mirror+docs (mongo.json 0600 handling, refuse
world-readable; loopback+auth `docker run` recipe + user bootstrap +
zero-users refusal + derived DB name `touch_<sha1(repo-realpath)[:8]>`;
gitignore half already landed in sp-01), R-57:mongo-doc (rebuild/backfill
commands + "Mongo down is a non-event" + growth/retention numbers +
"never publish 27017 via sbx ports" — README/CLAUDE halves are sp-15's).
**Shared decisions:** SD-1 (mirror is the sole mapper consumer + lease
holder), SD-7 (mongo.md exception), SD-10 (owns the GD-26 generation sweep:
retraction updateMany for `records`; the ONE legal deleteMany+reinsert for
renumbered `stream_meta`), SD-11; GD-21 client options verbatim
(`serverSelectionTimeoutMS=500, connectTimeoutMS=500, socketTimeoutMS=2000,
retryWrites=True`, AsyncMongoClient); GD-22 (memory-authoritative; Mongo
never on the liveness path; no blocking DB I/O in the poll loop); GD-27 in
full (credentials never in repo/events/health/API; deny-list; tests use
`touch_test_<pid>` and drop only names they constructed); GD-29 lease;
GD-30 budgets (dead-port tick test; queue-full drops mirror writes never
live frames); `--backfill` hard-codes `live=False`, stamps
`ingestMode:"backfill"`. Live-mongod test arms use the R-42 loopback+auth
recipe via Docker and **skip cleanly without it**.

## sp-07 — sessions-arm
**Title:** Session discovery/registry + tagged-union session mirror
**Owned files:** new `aggregator/sessions.py`, `tests/test_sessions.py`.
**Items:** R-25 (base, as amended: discovery scoped to cwd slug +
`.session-aliases` slugs, never `projects/*`; historical arm never a grouping
key for agent records), R-46 (amendment: `live:<pid>-<procStart>` /
`hist:<sessionId>` immutable `_id`s; promotion via `$addToSet sessionIds` +
`promotedTo`; transcriptless seventh sessionId as `sources:[]`).
**Shared decisions:** SD-1 (session mapper exported here, pure), SD-11;
GD-24 session-key separator `-`; tolerate `lost+found` and zero-byte registry
files; four foreign slug dirs are negative fixtures (sp-02 paths).

## sp-08 — ingest-pipelines
**Title:** Harness ingest (transcripts/journals/snapshots) + records/
stream_meta bucketing + runs/run_nodes + usage mirror
**Owned files:** new `aggregator/ingest.py`, `tests/test_ingest.py`,
`tests/test_usage.py`.
**Items:** R-26 (base, with ALL six amendment amendments: uuid-less keying,
cross-session assembly deferred to sp-10's R-48 where agent-shaped, journal
timestamps from transcripts with `now()` forbidden, snapshot back-fill never
an error, `agentCount`→`nodeCount` display-only, tokens as upserted docs;
persisted-output regex + message-id dedup transfer unchanged; `tool-results/`
directory scan keyed `(sessionId, basename)`, `linkedToolUseId:null` ⇒
"unlinked spilled output"), R-47 (the 12-type bucket table stated ONCE here:
`user|assistant|system|attachment` ⇒ `records` by uuid; every other/unknown
type ⇒ `stream_meta` positional; `queue-operation` `render:false`, never
deduped against its `user` twin; `sessionId` injected from path; `lineNo` +
`byteOffset` on every mirrored record), R-49 (run doc from FIRST journal
`started`; GD-7 amended ordinal = 0-based count of preceding `started` with
same key, stored `journalSeq`, never a DB counter; launch `toolUseResult`
persisted on `runs` as the ONLY deterministic session→run join + run-level
stop handle), R-50 (usage `_id = message.id`, `$max` four fields +
`$setOnInsert` ids; agentId-conflict counter, never overwrite; rollups =
`$group` sums, never `$inc`, never `harnessTotals`).
**Shared decisions:** SD-1 (all mappers here are pure exports), SD-11; GD-25
verbatim ($max accumulation — `$set`/`$inc` forbidden on accumulables;
deltas wire-only). This is the heaviest sub-plan; it owns exactly one source
module and its two test files — do not let it touch `agents.py`,
`sessions.py`, `legacy.py`, or `mirror.py`.

## sp-09 — legacy-arm
**Title:** Legacy events adapter + legacy: mirror namespace + artifact
registry + R-58 read-time re-labels
**Owned files:** new `aggregator/legacy.py`, `tests/test_legacy.py`.
**Items:** R-27 (base: full GD-14 rule set — synthesized runId/taskId/
ordinal, ts normalization + line-order seq, two-writer dedup, token folding,
`derived_from_legacy:true` re-labels, never read `.watcher-state.json`,
plan-only folders, derived archive label, `legacy:<task>:<id8>` exemption),
R-51 (amendment: R-27's reduction IS the input, no separate adapter;
`_id = legacy:<task>#<line:08d>` positional — depends on the never-delete
rule, recorded; GD-28 no-guess provenance: `agent|tokens` ⇒ derived,
`title` ⇒ asserted, else `unknown`; byte-identical duplicates stay distinct
by position; artifact registry as `custom_state_events` kind `artifact` —
paths + sha256 only, never bodies, `.watcher-state.json` excluded; nothing
else to migrate), R-58:legacy (SD-4 read-time rule applied to all three
affected streams; the failed-then-done fixture renders `done`).
**Shared decisions:** SD-1, SD-4 (read-time half verbatim), SD-11; GD-14
percent-escaping of `% # | :` in task names.

## sp-10 — agents-reducer
**Title:** Agent/node graph join + fragments/spawn-locator assembly + the
single server-side reducer with read-time liveness
**Owned files:** new `aggregator/agents.py`, `tests/test_agents.py`,
`tests/test_reducer.py`.
**Items:** R-28 (base: harness facts create nodes — `(runId,key,ordinal)` /
full 17-hex agentId; GD-9 marker layer labels, never creates; unnamed ⇒
`unconventional` flag), R-48 (amendment: `fragments[]` ordered by the
`parentUuid → uuid` stitch chain, never directory order; `sessionId` NEVER a
grouping key; `.meta.json` optional, meta-bearing fragment wins; union
writes; `spawn{recordUuid, toolUseId, fileHint}` — fileHint perishable,
validated against `(st_dev, st_ino, size)`, stale-marked on mismatch,
identity never offset; "jump to spawn" via `records.findOne`, never file
re-read), R-54 (amendment: GD-23 reducer — observations in, derived out,
`reducerVersion` + drop-and-rebuild; three-state liveness from `now()` at
read time, no `state` field in storage; idle >180 s ⇒ `unknown`, leaves the
running set, never running/failed; freeze-to-stale moves INTO the reducer;
topology per SD-9).
**Shared decisions:** SD-1, SD-9, SD-11; GD-7/GD-10 as amended. The reducer
is the ONLY derivation site — `/api/*`, `/ws`, and the page all serve its
output (binding on sp-12/sp-13).

## sp-11 — custom-state
**Title:** Custom-state WAL + events/head collections + slots binding
**Owned files:** new `aggregator/custom_state.py`,
`tests/test_custom_state.py`, `tests/test_slots.py`;
`.claude/skills/touch-orchestrate/SKILL.md` (ONLY the :52-56 ledger-line
amendment: add `root` + `sessionKey <pid>-<procStart>`; pre-amendment lines
derive `sessionKey` from path with `sessionKeySource:"path"`).
**Items:** R-52 (WAL-first via store.py's existing append machinery —
`store.py` itself UNCHANGED, owned by sp-04; `custom_state_events`
append/insert-only + `custom_state` head `$max`-seq-guarded
`{seq:{$lt:newSeq}}`; ONE events + ONE head collection installation-wide,
kind-discriminated, never per task/session; tombstone deletes; refId
validated against agents/run_nodes/slots grammars, unknown refs rejected;
annotations `author:"local"`, 16 KB cap REJECTS 413; provenance pinned
`{asserted,touch}`; writer cannot emit `provenance:"harness"` —
unit-asserted), R-53 (slots per GD-24: the SINGLE name↔agentId hop;
`pending|bound|orphaned|conflict` + `pendingSince`; orphaned is normal and
rendered honestly; DuplicateKeyError on the unique sparse agentId index ⇒
`conflict` doc with BOTH ids, caught, counted, tailer lives; decision:
slots arm lives in `custom_state.py`, not `agents.py`).
**Shared decisions:** SD-1, SD-8 (TOUCH_CONTROL_PATHS + `pathSource` until
R-20 lands — R-20 is NOT in this pass), SD-11; GD-28 taxonomy; Mongo wipe +
WAL replay reproduces both collections exactly.

## sp-12 — server-api
**Title:** HTTP/WS server: auth posture, route table, read API, bounded
replay + resume
**Owned files:** new `aggregator/server.py`, `tests/test_server_core.py`,
`tests/test_api.py`.
**Items:** R-30 (GD-13 in full: 127.0.0.1 default, opt-in 0.0.0.0, per-boot
token everywhere but `/health` via `hmac.compare_digest`, Origin/Host
allowlist at WS upgrade, static `(method,route)` dict + default 404 — never
a fallback; `safe_artifact_path` containment + CSP sandbox + nosniff copied
verbatim; `/health` per-tailer liveness + parse-failure counters, extended
with the R-45 `mirror:{state, lastError, queued, dropped, tolerated_dups,
lease}` block — mirror.py supplies the data, server.py serves it), R-31
(query-string-only endpoints per GD-12; ids regex-validated by one shared
helper; cursors `(stream,seq)`), R-55:server (bounded default replay window,
explicit `?from=` + load-older; reconnect resumes from client's last
`(stream,seq)` — requires the absolute-token model, they are a package;
frames carry `live:true|false` or handshake mode-switch at replay→tail;
token frames coalesce ≥1 s; optional `/api/query` from Mongo with documented
file-store fallback; existing routes keep reading the in-memory reduction —
the UI never depends on Mongo).
**Shared decisions:** SD-11; GD-22 (memory-authoritative reads); the reducer
(sp-10) is the only state derivation — server serves, never derives.
Frontend half of R-55 is sp-13's; the wire contract above is restated there
verbatim.

## sp-13 — frontend
**Title:** touch-visual v0 (read-only) + live/backfill frame rendering
**Owned files:** new `touch-visual/index.html`, `touch-visual/app.js`,
`touch-visual/style.css`, `tests/test_touch_frontend.py`.
**Items:** R-22:frontend (the skeleton half — created here, not in sp-04),
R-32 (sidebar: sessions incl. historical + legacy task folders per GD-14
kinds; agent tree keyed per GD-7; token rollups from computed sums;
escape-first rendering; render coalescing + capped log from day one; every
degraded/derived state labelled — dashed provenance, `derived_from_legacy`,
"closed — no verdict"; NO control affordance renders in v0), R-55:frontend
(replayed/backfill frames paint once, no animation; source guards: no
animation class on non-live frames, no state-inference in `app.js` — the
reducer already decided).
**Shared decisions:** SD-11; wire contract restated from sp-12 verbatim
(live flag, `(stream,seq)` resume, absolute tokens); GD-20 escape-first; the
frontend never re-derives (GD-23).

## sp-14 — e2e-acceptance
**Title:** End-to-end simulation: no-mongod, mirror, and budget arms
**Owned files:** new `tests/test_e2e_sim.py`.
**Items:** R-56 (amendment) + R-37:acceptance-arms (base — pulled in ONLY
because R-56 "extends R-37" and the file does not exist; this sub-plan
implements the phase-1 and phase-3 acceptance arms R-56 builds on; R-37's
phase-4 control arm stays excluded with the control plane).
- **No-mongod arm:** pymongo absent AND mongod unreachable ⇒ sessions, agent
  rows, loop cards, token counters all update; `/health` `mirror:
  absent|down`; every module imports; full suite green on a bare checkout.
- **Mirror arm** (skips cleanly without mongod — R-42 recipe via Docker
  when available): GD-25 double-ingest fingerprint; wipe + `--rebuild`
  equivalence; wf_455b348c retry topology and a2fc883c cross-session union
  render through the FULL path (files → ingest → mirror → reducer → API).
- **Budget arm:** per-tick byte-counter O(delta) test (append 1 KB to a
  20 MB fixture ⇒ tick reads < 64 KB); dead-mongo tick-duration test.
- Phase-1/-3 arms from R-37: R-16-style e2e watcher replay yields no
  `failed` verdict on the research plan (uses sp-02 fixtures + sp-03 rules);
  wf_829e6f58 renders six distinctly-labelled researcher nodes, correct
  deduped rollups, three-state liveness; legacy path renders touch-repo-recon
  stale-closed + "closed — no verdict".
**Shared decisions:** SD-11; fixtures are sp-02's, read-only; live smoke
against `~/.claude` is manual, never acceptance.

## sp-15 — docs-register
**Title:** Probes, docs truth pass, anchor repair, findings register, run docs
**Owned files:** `README.md`, `CLAUDE.md`, `inception.md`,
`.claude/local-orchestrators/touch-full-recon/plan/touch-full-recon-plan.md`,
new `.claude/local-orchestrators/touch-full-recon/report/probes.md`,
new `.claude/local-orchestrators/touch-full-recon/plan/findings-register.md`,
new `docs/control-semantics.md`, new `tests/test_register.py`,
new `tests/test_docs.py` (the static doc guards — test_shell.py genre but a
NEW file; `test_shell.py` stays sp-03's).
**Items:** R-04 (run the settling probes, record with command + date — they
gate the EXCLUDED phases 2/4; recorded now so the next pass starts unblocked),
R-05 (docs truth pass per GD-3/GD-4/GD-5: verb table, true inventory,
omnigent deletion, deduped token figure ≈29.5M in / 316k out, two labelled
serve blocks 8931/8932 ports *reserved*, "when a run ends, stop its
watcher"), R-06 (findings register — every finding id under
`.claude/local-orchestrators/*/findings/*.md` exactly once, now including the
five touch-mongo-live findings files; register the R-58 aliases
SKILLS-1 ≡ RUNSTATE-4 ≡ PRODUCT-7), R-38 (anchor repair: plan-file D8 row
relabelled D8.1/D8.2 per amendment §0.2; R-03 wording → "disjoint
continuations"; inception.md:78-80 "usage copied" → running counter + $max
rule; probes.md append: Mongo provisioning evidence — pymongo 4.17.0 via
proxy, mongo:7 runs, subdocument-`_id` field-order sensitivity, BSON type
strictness), R-33 (README run section + docs/control-semantics.md verb
ladder/session classes), R-40:docs (CLAUDE.md "stop its watcher" rule +
plan-file GD-1 wording per SD-5), R-57:docs (README/CLAUDE halves:
per-session-collection disposition in the user's words per §0.3, GD-21
pymongo exception named in CLAUDE.md, run-level vs agent-level stop in
control-semantics.md, no `sbx ports` 27017).
**Shared decisions:** SD-5, SD-7; static guards: CLAUDE.md contains
`inception.md`/`touch-aggregator-plan.md`/`touch-full-recon-plan.md`/
`touch-orchestrate` and NOT `omnigent`; plan file contains "D8.1" and
"D8.2"; inception.md no longer contains "copied onto every split record";
README/docs contain the loopback recipe reference and no `0.0.0.0` mongo
example; README has the verb table and no unqualified "pause" promise.

---

## Scope exclusions (recorded so nobody re-litigates them)

1. **Base R-11 / R-12** (monitor server correctness fixes, dashboard
   scalability/link whitelist) — OUT. No amendment item touches those
   concerns. `monitor_server.py` receives ONLY R-10's slice (sp-03);
   `monitor.html` is untouched this pass.
2. **Base R-14 … R-17** (template id validation, attempt bookkeeping,
   tautological-test replacement, monitoring.md normative refresh) — OUT by
   omission from this pass's mandate: they are not preconditions of
   R-42…R-58 and were not named in scope. The files they edit are owned this
   pass by sp-03 for OTHER items; a future pass must re-divide ownership.
3. **Base phase 2 (R-18 … R-21)** — OUT, gated on probes/R-20. Amendment
   arms that depend on R-20 (control-intent/ack ingest in R-53) use the
   `TOUCH_CONTROL_PATHS` fallback with `pathSource`, exactly as specified
   (SD-8). R-53's SKILL.md:52-56 ledger amendment still lands (sp-11); the
   rest of R-20's SKILL.md rewrite does not.
4. **Base phase 4 (R-34 … R-37)** — OUT, blocked on R-04 probe results.
   ONE carve-out: R-37's non-control acceptance arms are implemented inside
   sp-14 because R-56 extends a file that does not otherwise exist; R-37's
   control-plane arm (cooperating-session round-trip, observed 403) remains
   excluded with the rest of phase 4.
5. **R-24 / `store.py`** — IN scope but base spec UNCHANGED: Mongo does not
   replace `store.py`; it keeps sole ownership of `.touch/`; store-level
   Mongo affordances live in `mongo_store.py` (amendment §2 R-24 row).
6. **T/P-plan deferred tier** (T2, T9, T13, T15, T17, T19, T22) — remains
   deferred unchanged, per the base plan's own record.

## Sequencing rationale

sp-01…sp-03 are the first wave (phase 0 + R-38…R-41 doc-halves-deferred +
R-58 scope); sp-03 green is the GD-23 hard precondition for every mirror
write. sp-04 (file-side base core) precedes M1. sp-05/sp-06 are M1
foundations. sp-07…sp-10 are M2 pipelines (sp-10 carries R-54 because
`agents.py` has one owner; SD-9 removes the M3 dependency). sp-11 is M3.
sp-12…sp-14 are M4 serve/accept. sp-15 runs last so every doc it writes
describes shipped reality (SD-7); the only hard preconditions of M1+ are
R-01…R-03 done + R-08/R-09/R-13 green, which sp-01…sp-03 deliver.
