# `tests/fixtures/` — the frozen reference corpus

Owned by sub-plan **sp-fixtures-freeze** (items **R-03** base, **R-41**
amendment, **R-58**:fixtures). Every byte here is a **verbatim copy** of a real
file that existed on this machine on **2026-07-25**. Nothing is synthesised,
reformatted, pretty-printed, sanitised or truncated mid-line.

**Inventory:** 70 manifested files, ~8.0 MiB, in six groups —
`run-wf_829e6f58/` (3.9 M), `legacy/` (0.5 M), `mirror/live-run-shape/` (2.2 M),
`mirror/records/` (0.9 M), `mirror/r58-replay/` (0.3 M), and the small
`mirror/wf_455b348c-e17/` + `mirror/discovery/` sets. Verified by
`python3 tests/test_fixtures.py` (181 checks).

Frozen because the harness corpus sits on a retention-sweep deletion clock
(GD-18, AUDIT-7, RUNSTATE-17) and is the only real specimen of several shapes
that cannot be reconstructed — a completed multi-session run, a run killed by
the user, and a run with **no terminal snapshot yet**.

## Rules for everyone downstream

- **Read-only.** No other sub-plan may add, edit, move or re-copy anything under
  `tests/fixtures/`. If you need a shape that is missing, say so in your
  findings; do not improvise a fixture next to these.
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
