# `tests/fixtures/` — the frozen reference corpus

Owned by sub-plan **sp-fixtures-freeze** (items **R-03** base, **R-41**
amendment, **R-58**:fixtures), extended once by **sp-fixtures** (**LC-01**).

Every byte of the six **captured** groups is a **verbatim copy** of a real file
that existed on this machine on **2026-07-25** (`dup-snapshot-wf_617adbe5/`:
2026-07-30). Nothing there is synthesised, reformatted, pretty-printed,
sanitised or truncated mid-line.

The seventh group, `context/`, is the one exception and is **constructed, not
captured** — every specimen in it is labelled **synthetic** below, with the real
2.1.220 record its shape was copied from. Labelling is the condition: a labelled
synthetic is a fixture, an unlabelled one is a lie about the corpus.

**Inventory:** 77 manifested files, ~8.2 MiB, in seven groups —
`run-wf_829e6f58/` (3.9 M), `legacy/` (0.5 M), `mirror/live-run-shape/` (2.2 M),
`mirror/records/` (0.9 M), `mirror/r58-replay/` (0.3 M), the small
`mirror/wf_455b348c-e17/` + `mirror/discovery/` sets, and `context/` (40 K).
Verified by `python3 tests/test_fixtures.py` (296 checks).

Frozen because the harness corpus sits on a retention-sweep deletion clock
(GD-18, AUDIT-7, RUNSTATE-17) and is the only real specimen of several shapes
that cannot be reconstructed — a completed multi-session run, a run killed by
the user, and a run with **no terminal snapshot yet**.

## Rules for everyone downstream

- **Read-only.** No other sub-plan may add, edit, move or re-copy anything under
  `tests/fixtures/`. If you need a shape that is missing, say so in your
  findings; do not improvise a fixture next to these. `context/` was added by a
  named plan item after exactly that conversation happened (AGGREGATOR-TESTS-11
  and -15 said the shapes were missing); the rule now covers it too.
- **`MANIFEST.sha256` is the freeze.** `tests/test_fixtures.py` fails if any
  file changes by one byte, disappears, or appears unmanifested. Regenerating
  the manifest is a deliberate act, never a fix for a red test — see the bottom
  of this file.
- **Not covered by the manifest:** `MANIFEST.sha256` itself and this
  `PROVENANCE.md` (prose may be improved). Everything else is frozen, including
  `legacy/anchors.json` and the `*.index.json` sidecars, because tests assert
  against them.
- Trailing newlines are as the harness wrote them: every `.jsonl` ends with
  `\n`; the single-object `.json` and `.meta.json` files have **no** trailing
  newline. Both facts are asserted, so a well-meaning editor cannot "fix" them.

## Credential scan (the R-03 sanitisation condition)

The whole copied set was scanned before freezing for `sk-ant-…`, `sk-…`,
`AKIA/ASIA…`, `gh[pousr]_…`, `-----BEGIN … PRIVATE KEY-----`, `Bearer <token>`
and `password|secret|api_key|token = <value>`. The only hits are documentation
and placeholder text inside a research agent's transcript — `x-api-key:
$ANTHROPIC_API_KEY`, `apiKey: 'your-api-key'`, `'ghp_your_new_github_token'`,
`auth_token=environment_key`. **No real credential is present, so nothing was
sanitised** and the copies are byte-exact (R-03: "sanitize only if inspection
finds credentials").

---

## `run-wf_829e6f58/` — the completed multi-session run (R-03)

Source: `~/.claude/projects/-home-laniakea-Projects-touch/`. The two session
directories are kept with their real UUID names and their real internal layout,
so a discovery/ingest test can point at this directory as a project slug.

| path | source | what it proves |
|---|---|---|
| `dd469822…/subagents/workflows/wf_829e6f58-b2f/journal.jsonl` | same path under the real session | 14 records: 7 `started` + 7 `result`, written **after** the `/clear` that moved the session |
| `…/wf_829e6f58-b2f/agent-*.jsonl` (6) + `agent-*.meta.json` (6) | idem | the first-session half of the run; every meta is the `{agentType:"workflow-subagent",spawnDepth,model}` shape |
| `dd469822…/subagents/agent-a483cae616edffe81.meta.json`, `agent-a4e343a0f7d73268c.meta.json` | idem | the **other** meta shape — a Task-tool spawn, with `description` + `toolUseId`. Their transcripts (0.4 MB + 0.9 MB) are deliberately not copied; only the shape is needed |
| `e423cd3c…/subagents/workflows/wf_829e6f58-b2f/agent-a2ed16d57db0e9887.jsonl` + `.meta.json` | idem | the synthesizer, which ran after the `/clear` and so landed in the second session |
| `e423cd3c…/subagents/workflows/wf_829e6f58-b2f/agent-a2fc883c96ff7b837.jsonl` | idem | **the cross-session pair** — see below |
| `e423cd3c…/workflows/wf_829e6f58-b2f.json` | idem | the terminal snapshot, sitting in the "wrong" session dir (the journal is under `dd469822…`) |
| `e423cd3c…/tool-results/*.txt` (4) | idem | `<persisted-output>` spill bodies, so pointer records resolve |

**The cross-session pair (MONGOSCHEMA-9, SESSIONJSONL-3; R-03 as amended).**
`agent-a2fc883c96ff7b837.jsonl` exists under **both** session dirs and the two
files are **disjoint continuations**, not two copies: `dd469822…` holds 223
records (552 313 B), `e423cd3c…` holds 2 records (12 598 B) and has **no
`.meta.json`**. Zero uuid overlap; the fragment's first record's `parentUuid`
points into the first file and its `sessionId` has been rewritten to the new
session. So `_id = agentId` must **union**, and per-file token rollups
under-report. R-03's original "both copies" wording is wrong; the amendment's
"disjoint continuations" is the binding wording.

**Counting note (resolves R-03's "9 agent transcripts" prose).** On disk this
run has **8** agent transcript files over **7** distinct agentIds, plus **7**
workflow `.meta.json` (matching R-03's "7 `.meta.json`") and the 2 Task-tool
metas above. The snapshot reports `agentCount: 7` while its `workflowProgress`
array has **9** rows — 9 is the distinct *node* count, which is where R-03's
"9" came from (SESSIONJSONL-7). `tests/test_fixtures.py` asserts the real
numbers, so nobody re-derives the corpus from the prose.

## `legacy/` — the four real `events.jsonl` streams (R-03, R-41, R-58)

Source: `.claude/local-orchestrators/<task>/events.jsonl` (tracked repo files;
copied so the fixture set is self-contained and so a still-live stream is
pinned).

| file | source lines | note |
|---|---|---|
| `touch-aggregator-events.jsonl` | all 590 | 7 duplicate stage terminals, 2 ts inversions, the fabricated `plan failed` at 571 |
| `touch-repo-recon-events.jsonl` | all 103 | the **only** two-wave respawn sample in existence (RUNSTATE-2); a run the user killed, so its `plan failed` terminals at 101/102 are **genuine** and must stay failed |
| `touch-full-recon-events.jsonl` | all 276 | fabricated `plan failed` (255) **followed later in file order by** a corrective `plan done` (276) |
| `touch-mongo-live-events.jsonl` | **lines 1–320** | prefix, because this stream was still being appended to while the fixture was frozen. Contains the fabricated `research plan failed` (275) + its corrective `done` (286) **and** a `divide plan failed` (319) with **no** correction |

Together these are R-58's "three real streams" plus the two-wave control. Line
anchors are machine-readable in `legacy/anchors.json`, which the test verifies
against the bytes — so an anchor can never silently drift.

Two rules read off these bytes (SD-4, restated in sp-03 and sp-09):
conflicting terminals on the same `(task, plan, stage='plan')` resolve
**last-event-wins in file order** (276 beats 255; 286 beats 275), while
RUNSTATE-7's watcher-wins dedup applies **only** to same-state duplicates. A
`plan failed` with detail `loop exited -> …` and all stage agents resulted (319)
re-labels to "closed — no verdict", `derived_from_legacy: true`.

**Writer attribution (CUSTOMSTATE-3, GD-28).** `status.sh` and
`decision_watcher.emit()` produce the same five keys in the same order, so
provenance is unrecoverable for these streams: lines carrying `agent` or
`tokens` ⇒ `derived`, lines carrying `title` ⇒ `asserted`, **everything else ⇒
`unknown`**. Counts of the unknown ("unattributable") shape in the frozen files:
aggregator 33, repo-recon 20, full-recon 35, mongo-live 41 — and exactly **12**
in the first 130 lines of `touch-mongo-live-events.jsonl`, which is the
"12 of 130" the amendment measures. The forward fix is the additive `w` field
(R-39); no line here has one, which is what makes them the legacy specimen.

## `mirror/wf_455b348c-e17/` — the killed run (R-41)

`journal.jsonl` + `wf_455b348c-e17.json`, from session `e423cd3c…`. 11 journal
records: **9 `started`, 2 `result`**, and **3** distinct `(type,key)` pairs that
each occur **twice** — the "3-key retry" that forces `ordinal` into the run-node
key and forces it to be derived from journal line order, not an in-memory
counter (SESSIONJSONL-4, MONGOSCHEMA-18). Snapshot: `status: "killed"`,
`error: "Error: Workflow aborted…"`, `agentCount: 6` over **8**
`workflowProgress` rows and 9 started agents — proof that `agentCount` counts
nodes, so `len(agents) == agentCount` is never a valid assertion
(SESSIONJSONL-7). This run is the one named in
`touch-repo-recon/events.jsonl:103`, so it is also the join specimen for
"`runId` = `basename(orch-config.json.wf_dir)`" in the legacy adapter.

## `mirror/live-run-shape/` — a run with no snapshot (R-41, SESSIONJSONL-6)

Session `a8d43bb1…`, run `wf_b297177a-d11`: `journal.jsonl` (16 records —
**9 `started`, 7 `result`**) plus all 9 agent transcripts and their metas,
copied **while the run was still going**, which is why there is **no
`a8d43bb1…/workflows/` directory at all**. The absence is the fixture: a
missing `<runId>.json` means "still running", never "error", and the run
document must be created from the first journal `started` record. This shape
is unreproducible once a run ends — the snapshot appears and it is gone.

Consequences of the mid-run copy, all intentional: agent transcripts are
prefixes of what those files eventually became, and one of them is the
transcript of the agent that created this corpus.

## `mirror/r58-replay/` — verdict-less research journals (R-58)

Session `292fc08c…`, runs `wf_930e210a-6da` (14 records: 7 `started` /
7 `result`) and `wf_cca84d59-933` (12 records: 6 / 6), with both terminal
snapshots (`status: "completed"`; each snapshot's own `timestamp` is the run's
**end** time, and it carries only end-state fields). Neither journal contains a
`passed`/`approved`-shaped verdict anywhere — that is precisely the input that
made the watcher fabricate `plan failed "loop exited -> synthesis"`. Replaying
these through the fixed rules must yield **zero** `failed` badges on research or
synthesis plans.

## `mirror/records/` — single-record specimens (R-41)

| file | source | what it proves |
|---|---|---|
| `oversize-line.jsonl` | `e423cd3c…/wf_455b348c-e17/agent-a2c3883fe5a0bb9c2.jsonl` line 17 | the largest single record in the corpus: **877 395 B** (record 877 394 B + `\n`), ~5 % of the 16 MiB BSON limit. The specimen for the `mongo_store.py` size guard and for "never inline a `tool-results/` spill" (MONGOSCHEMA-17, SESSIONJSONL-12). The plan calls this "the 872 KB line"; 872 577 B was an earlier measurement of the then-largest line, and this is the same specimen re-measured after the transcript grew |
| `file-history-snapshot-dotted.jsonl` | 33 records from the main session transcripts, listed per line in `file-history-snapshot-dotted.index.json` | **dotted field names** — filenames (`CLAUDE.md`, `.gitignore`, `research.workflow.js`) used as object keys under `snapshot.trackedFileBackups`. Storable in Mongo ≥ 5.0 but not addressable by `$set`/`$lookup`/index key, hence the `_raw` wrapper rule (MONGOSCHEMA-8). These records also have **no `uuid` and no `timestamp`** |
| `queue-operation-user-pair.jsonl` | `292fc08c….jsonl` lines **65 and 67** (66 skipped on purpose) | a `queue-operation` record with **no `uuid`** immediately followed by the `user` record it became, which has one — the pair behind the two-arm `_id` rule (`uuid` for user/assistant/attachment/system, synthesised `(session, file-offset)` key otherwise) (SESSIONJSONL-1) |

## `mirror/discovery/` — negative discovery fixtures (R-41, SESSIONJSONL-11)

`projects/` holds the **four foreign project slugs** created by nested `claude`
runs under `/tmp` during earlier research
(`-tmp-claude-1000-liveio` with 5 transcripts, `-tmp-claude-1000-models-probe`
with 2, and the two `…-castprobe`/`…-castprobe2` slugs with 1 each). A
`projects/*/*.jsonl` enumerator ingests all of them as Touch sessions; scoping
discovery to the configured project's slug(s) must ingest **none**.

`sessions/15934.json` is the live-session registry entry: **one** file against
six transcripts on disk, and its filename is the raw **pid** — pid reuse
overwrites it, which is why session identity is `(pid, procStart)`.

## `dup-snapshot-wf_617adbe5/` — one run, two disagreeing snapshots (D-02)

Source: `~/.claude/projects/-home-laniakea-Projects-touch/<session>/workflows/`,
copied 2026-07-30 from the two sessions that observed run `wf_617adbe5-42a`
(the `touch-memory-home` implement run, resumed after an infrastructure
outage). The session directory names are kept, so the subtree mounts as a
project slug and `find_snapshots`' cross-session glob finds both copies.

| copy | timestamp | status | agentCount | totalTokens | totalToolCalls | durationMs |
|---|---|---|---|---|---|---|
| `1be0c928…` (earlier, sorts FIRST by path) | `2026-07-30T06:14:25.815Z` | `failed` | 37 | 3 659 088 | 1 178 | 16 446 655 |
| `f6fa2bbd…` (later, authoritative) | `2026-07-30T13:11:45.232Z` | `killed` | 59 | 4 319 298 | 1 437 | 24 968 896 |

**The fixture is the disagreement.** A resumed run writes one snapshot per
observing session; `find_snapshot`'s `sorted()[0]` orders on the *session
UUID*, a value with no relation to time, and here that hands back the earlier
`failed`/37/3.66 M copy for a run that was `killed` after 59 agents and 4.32 M
tokens. `fold_snapshots` (D-02) takes the later `status` and the `$max` of
every total; `tests/test_ingest.py` asserts both against these bytes. One of 27
on-disk run ids is duplicated today, and the mechanism recurs on every resume,
so the count is a floor.

Both files are the harness's own single-object JSON: no trailing newline, and
they carry `promptPreview`/`lastToolSummary`/`resultPreview` text from this
repo's own run. The credential scan above was re-run over both — no hits.

## `context/` — the context-occupancy specimens (LC-01) — **SYNTHETIC**

Five subagent transcripts for the live-context feature (`GD-LC-1`, `GD-LC-2`,
`GD-LC-3`). **Constructed on 2026-07-31, not captured.** They are synthetic for
a stated reason, not for convenience: the shapes they carry do not exist in any
subagent transcript on this machine.

| shape | occurrences in the real subagent corpus |
|---|---|
| a `compact_boundary` record | **0** in 689 transcripts (GD-LC-3: "the branch is cold") |
| `usage.iterations` with `len > 1` | **0** in 7,256 sampled billed rows (GD-LC-2) |
| a usage row with a non-int prompt field | **0** — the harness always writes ints |

A verbatim capture was looked for first and none was found, which is the whole
point: without these bytes the correct implementation of GD-LC-2 and the
tempting-and-wrong one are **indistinguishable**, because `max`-over-turns
coincides with greatest-timestamp on 100 % of the real corpus.

### Why these five are NOT named `agent-*.jsonl`

Every real subagent transcript is `agent-<17 hex>.jsonl`, and these are not.
That is deliberate, and it is the one place the group departs from the shape of
the real thing.

`tests/test_token_crosscheck.py`'s corpus arm is
`sorted(FIX.glob("**/agent-*.jsonl"))` — recursive, unfiltered — and its stated
domain is **real harness bytes**: it asserts that `decision_watcher` and
`aggregator.ingest` agree over transcripts the harness actually wrote. A
labelled synthetic must not join that corpus. One of these specimens
(`ctx-agent-no-usable-turn.jsonl`) cannot satisfy the arm's GD-M2.2 invariant
**by construction** — carrying a float, a null and a bool where the harness only
ever writes ints is the entire specimen — and the other four would quietly mix
constructed numbers into aggregate totals that are meant to be evidence.

So the boundary is drawn in the **filenames**, not by a filter inside a test
this sub-plan does not own: it is structural, it needs no maintenance, it covers
the whole group rather than the single file that happened to go red, and
`test_context_group_stays_out_of_the_corpus_glob` fails if a sixth specimen ever
re-opens it. (LC-01's item text names the five files without the `ctx-` prefix;
this is the one deviation from it, recorded here and in the sub-plan's findings.)

Two consequences for whoever reads these next:

- **The filenames are descriptive, not agentId-encoded.** A consumer that
  resolves the agentId **from the path** — which GD-LC-2 requires — must stage
  each specimen as `agent-<its agentId>.jsonl` in a temp tree first. The
  embedded agentIds are 17-hex-shaped (`ac0badc0ffee00001`,
  `adeadbeef00000002`, `afeedface00000031`/`…32`, `a5eeded000000000f`) for
  exactly that.
- **No specimen carries an agentId-mismatched row**, so GD-LC-2's "mismatches
  are dropped and counted, never attributed" branch has no fixture here. The
  captured corpus has none either (0 mismatches in 1,769 records). LC-01 did not
  ask for one; a consumer that wants to exercise that branch constructs it
  inline.

**What is real in them.** The numbers, the identifiers and the filenames are
invented. The record **envelope** and the `message.usage` field set are copied
field-for-field from a real CLI **2.1.220** record:

- the `user` / `assistant` envelope, its key order, and the `message.usage`
  object — from `run-wf_829e6f58/.../agent-a2fc883c96ff7b837.jsonl` (in this
  corpus). Billed assistant rows carry `attributionAgent: "workflow-subagent"`,
  `attributionSkill: "implement-plan"` and `effort: "xhigh"` — each a real
  value — because **667/667** billed assistant records under `run-wf_829e6f58/`
  carry all three (1,061/1,065 across the whole captured corpus; the four
  exceptions are `general-purpose` Agent-tool records, the arm GD-LC-7 puts out
  of scope). Touch reads none of the three (`grep` over `decision_watcher.py`
  and `aggregator/*.py` finds no mention); they are here so the envelope is not
  quietly thinner than the real thing;
- the `type: "system"` / `subtype: "compact_boundary"` record, with
  `compactMetadata.{trigger,preTokens,durationMs,preservedSegment,
  preservedMessages,postTokens,cumulativeDroppedTokens}`, and the
  `isCompactSummary: true` user line that follows it — from a real forced
  `/compact`, read (never copied) out of `~/.claude/projects/` on 2026-07-31.
  On that capture every `preservedSegment`/`preservedMessages` uuid resolves to
  a record in the same file and `tailUuid == logicalParentUuid`; the fixture
  reproduces both, and `test_context_compaction_separates_latest_from_max`
  asserts it, so the uuids cannot rot into dangling placeholders;
- the `<synthetic>` 529 row — `model: "<synthetic>"`, a uuid-shaped
  `message.id`, every usage field `0`, `iterations: null`, `stop_reason:
  "stop_sequence"` with an empty `stop_sequence`, `error: "server_error"`,
  `isApiErrorMessage: true`, `apiErrorStatus: 529` — from a real killed agent,
  same date. A real one carries **no** `attributionAgent`/`attributionSkill`/
  `effort`, and neither does this one; that asymmetry is measured, not an
  oversight, and it is asserted.

**Where the prompt text sits.** Each first user prompt opens with a leading
`"\n"` and the `[monitor] …` marker on line **2**, because that is what the
harness writes: every workflow template opens the prompt with a template-literal
newline (`skills/implement/templates/implement.workflow.js:221` and five more
sites; `research.workflow.js:186/216`), and 16/16 marker-carrying user records
in the captured corpus start with `"\n"`. The marker is FENCED (GD-D1a) and both
readers search for it rather than indexing line 1 —
`decision_watcher.py`'s `MARKER_SPLIT` and `agent_lifecycle.py`'s
`parse_labels` — but a fixture that put it on line 1 would be a false claim
about the harness that no downstream sub-plan could correct, since this tree is
frozen.

**Two shapes deliberately NOT normalised.** Consecutive `assistant` records with
no interleaved `user`/tool-result record are **real** (426 such pairs in the
1,769 captured records here), so `ctx-agent-no-usable-turn.jsonl`'s four-in-a-row
run is faithful, not a simplification. And `usage.iterations` is a real key
(501/1,065 captured billed rows carry it), so `"iterations": null` on
single-call rows is faithful too.

Identifiers are deliberately unmistakable: every uuid is
`<tag>-0000-4000-8000-<12 digits>`, so no synthetic id can ever be confused with
a captured one. Written the way the harness writes a `.jsonl`: compact
separators, one record per line, trailing newline.

| file | records | `[monitor]` stage | what it proves |
|---|---|---|---|
| `ctx-agent-compaction-boundary.jsonl` | 12 | `ctxcompact` | occupancy **goes down** |
| `ctx-agent-iterations-multi.jsonl` | 4 | `ctxiter` | top level ≠ `iterations[-1]` |
| `ctx-agent-retry-attempt1.jsonl` | 6 | `ctxretry` | a retry's window ends high |
| `ctx-agent-retry-attempt2.jsonl` | 4 | `ctxretry` | …and its successor starts fresh |
| `ctx-agent-no-usable-turn.jsonl` | 5 | `ctxnousable` | unknown, never `0` |

**The stages are distinct on purpose.** `(plan, stage, role, attempt)` is the key
the watcher's `[monitor]` join uses, so two specimens staged into one `wf_*` tree
under the same marker would arrive as two agents claiming one card identity. The
retry pair **must** share `plan`/`stage`/`role` (that is what makes it a retry);
every other pair must differ. `test_context_markers_do_not_collide` pins both
halves, and this is fixable here or never — the tree is frozen after this.

**`ctx-agent-compaction-boundary.jsonl`** (agent `ac0badc0ffee00001`). Five billed
turns at occupancy **40,000 → 80,000 → 120,000**, then the boundary, then
**12,000 → 18,000**. Greatest-timestamp therefore reads **18,000** where
`max`-over-turns reads **120,000** — the only bytes in the corpus that separate
the two rules, and the only evidence that the number is **non-monotonic** (so
the D7 monotone clamp must never touch it). Three further facts are pinned
on purpose:

- `preTokens` is **120,030**, deliberately **30 above** the last pre-compaction
  usage-row sum. `preTokens` is a *different estimator* (CC-STORES-3 measured
  exactly this 30-token gap on real bytes) and must never be mixed into
  GD-LC-1's arithmetic; `postTokens` (**11,970**) likewise differs from the next
  usage row (12,000). `preTokens − postTokens == cumulativeDroppedTokens`.
- The `isCompactSummary` line **follows** the boundary in file order while
  carrying a timestamp **2 ms earlier** — measured verbatim on the real pair.
  File order and timestamp order genuinely disagree at the seam. Neither line is
  a candidate row so GD-LC-2 is unaffected, but a whole-file
  `sorted(by timestamp)[-1]` is.
- Truncating the file **after the summary line (line 8)** yields GD-LC-3's
  provisional branch: the newest boundary is newer than the newest qualifying
  row, so the reading is `postTokens` stamped with the boundary's own
  `timestamp` and labelled `src: "compact"`. That prefix is the `src` specimen;
  no separate file is needed.
- `trigger` is `"auto"`. Only `"manual"` has ever been observed (HOOK-PLANE-3);
  `"auto"` is inferred-not-observed, and carrying it here is what makes
  GD-LC-3's "handle any `trigger` value" testable at all.

**`ctx-agent-iterations-multi.jsonl`** (agent `adeadbeef00000002`). One row with
`len(usage.iterations) == 1` where the top level **equals** its single iteration
(all 522 + 6,734 measured rows behave this way, which is why the top level is
what GD-LC-2 reads there), then one row with `len == 3` where the top level is
the **sum** of the three — the pessimistic assumption. A top-level read reports
**65,690**, a prompt that never existed; `iterations[-1]` reports **22,131**,
unambiguously one API call. Each iteration is a realistic ~22 k prompt, not a
filler row.

**`ctx-agent-retry-attempt1.jsonl` + `ctx-agent-retry-attempt2.jsonl`** (agents
`afeedface00000031` / `…32`). Same `plan`/`stage`/`role` (`stage=ctxretry`),
`attempt=1` and `attempt=2` in the `[monitor]` marker heading each first user
prompt. Attempt 1 runs **28,985 → 96,410 → 148,900**; attempt 2 starts at **27,140** on
a fresh window and ends at **41,200**. Two independent windows: GD-LC-7's "each
retry row is its own agent with its own meter", and proof that summing or
merging them would fabricate a level neither agent ever held. Attempt 2's first
row is deliberately **> 20,000** — a fresh window is never empty (HOOK-PLANE-7
measured min 21,641 over 610 agents), so `ctx 0` at spawn would understate by
21 k–45 k, which is the R-58 defect class.

This group does **not** duplicate the already-frozen retry run
`mirror/wf_455b348c-e17/` or the cross-session split
`agent-a2fc883c96ff7b837.jsonl` — those stay where they are and are reused in
place.

**`ctx-agent-no-usable-turn.jsonl`** (agent `a5eeded000000000f`). Five records —
one user prompt and four unusable assistant rows — and **zero** qualifying rows,
so occupancy resolves **unknown — the key absent, never `0`**. Three rows are
refused on type alone —
`input_tokens: 12.5` (float), `cache_read_input_tokens: null`,
`cache_creation_input_tokens: true` (bool) — and each one **would** produce a
positive, plausible-looking number under a lenient reader: `isinstance(v, int)`
accepts `True` because bool is an int subclass in Python, and `v or 0` swallows
the null. Only `type(v) is int` refuses all three. The fourth is the
`<synthetic>` 529 row, all-zero, **last** in the file — the shape 30 of 649 real
transcripts end on, and the exact input that would put a fabricated `ctx 0` on a
killed agent's card.

### What `ctx-agent-no-usable-turn.jsonl` found on its first run (a note for LC-02)

Recorded because it is the one thing this group discovered rather than merely
demonstrated, and because it is evidence, not a task assignment.

While the specimen was still named `agent-no-usable-turn.jsonl` it joined
`tests/test_token_crosscheck.py`'s corpus glob and immediately exposed a live
divergence the captured corpus cannot, because the harness never writes a
non-int token field:

| | `in` | `cached` | `cache_write` | `out` |
|---|---|---|---|---|
| `decision_watcher._transcript_usage` | **85518.5** | 31000 | **54501** | 516 |
| `aggregator.ingest.rollup` | 2 | 0 | 24500 | 180 |

The watcher adds `12.5` as a float — so it reports a **fractional token count**
(`30012.5` on one row) — and stores `True` as a cache-write value; `ingest`
refuses all three malformed rows outright. Producing a number from bytes that
say nothing is the R-58 defect class in miniature, and GD-LC-2's
`type(v) is int` rule (never `isinstance`, because `bool` is an `int` subclass)
is precisely what closes it. **LC-02 hardening `_transcript_usage` the same way
is the intended consequence of this fixture existing.**

The `ctx-` filenames keep the specimen out of that arm's corpus (see *Why these
five are NOT named `agent-*.jsonl`* above), so the cross-implementation equality
still ranges over the 18 captured transcripts and nothing else, and the finding
above stands on its own as a recorded measurement rather than as a red build.
Whoever hardens the watcher can reproduce it in one line:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=plugin/touch/shared/monitoring python3 -c \
  "import decision_watcher as d; print(d._transcript_usage('tests/fixtures/context/ctx-agent-no-usable-turn.jsonl'))"
```

Do **not** resolve it by softening the specimen: the float/null/bool row is the
whole specimen, and making it well-formed deletes the evidence.

Because the group is synthetic, the credential scan is vacuous for it — nothing
was copied from anywhere — but `tests/test_fixtures.py`'s
`test_no_credentials` runs over it anyway, since it globs the whole tree.

---

## Regenerating `MANIFEST.sha256`

Only after a deliberate, reviewed change to the fixture set:

```bash
cd tests/fixtures
python3 - <<'PY'
import hashlib, pathlib
root = pathlib.Path('.')
skip = {'MANIFEST.sha256', 'PROVENANCE.md'}
rows = sorted((str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest())
              for p in root.rglob('*')
              if p.is_file() and str(p.relative_to(root)) not in skip)
pathlib.Path('MANIFEST.sha256').write_text(''.join(f'{h}  {r}\n' for r, h in rows))
print(len(rows), 'files')
PY
python3 ../test_fixtures.py
```
