# sp-agents-reducer — test gate, attempt 2

**Verdict: PASS.** Owned suites 100% green; 0 NEW failures in the full suite;
0 ownership violations; 0 commits.

Environment: Python 3.13, stdlib only, `TOUCH_MONGO_URI` unset, no services
running, no third-party packages needed. Implementer's changed set:
`aggregator/agents.py`, `tests/test_agents.py`, `tests/test_reducer.py`
(all sp-10-owned) plus the task-state note
`findings/sp-agents-reducer-storage-deviation.md`.

---

## 1. Targeted suites (sp-10 owned) — GREEN

Run standalone from the repo root:

| suite | rc | `ok:` assertions | skipped arms |
|---|---|---|---|
| `tests/test_agents.py` | **0** | 162 | 0 |
| `tests/test_reducer.py` | **0** | 80 | 0 |

Closing lines: `all agents (R-28/R-48) tests passed`,
`all reducer (R-54) tests passed`. Neither suite has a conditional arm — both
run entirely off the frozen fixture corpus and temp-dir fixtures, so there is
no hidden hole behind an absent service. Assertion count is up from attempt 1
(112 → 162 and 68 → 80), matching the new coverage listed in §3.

## 2. Full-suite regression gate — no NEW failure

`.claude/shared/monitoring/tests/*` (from their own dir) + `tests/*` (repo root):

- PASS (18): monitoring `test_frontend`, `test_server`, `test_shell`,
  `test_watcher`; repo `test_agents`, `test_bootstrap`, `test_fixtures`,
  `test_ingest`, `test_legacy`, `test_mongo_deploy`, `test_mongo_store`,
  `test_reducer`, `test_refs`, `test_stdlib_only`, `test_store`, `test_tailer`,
  `test_usage`, `test_ws`.
- FAIL (2) — **pre-existing, identical to attempt 1's recorded baseline**:
  - `tests/test_mirror.py` (rc 1), FAILED (3):
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` (rc 1), FAILED (1):
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

**Attribution proof (re-run for this attempt, not carried over).** A clean copy
of `aggregator/`, `tests/`, `docs/` was made in the scratchpad with
`aggregator/agents.py`, `tests/test_agents.py`, `tests/test_reducer.py`
**removed**, and both red suites re-run there:

```
mirror   rc=1  FAILED (3)  — same three assertions, verbatim
sessions rc=1  FAILED (1)  — same assertion, verbatim
```

The failure sets are byte-identical with and without sp-10's files, so they
belong to the two loops that closed RED (`sp-mirror-deploy`, `sp-sessions-arm`).
This gate touched none of their files.

Mongo-absence behaviour (GD-21/R-56): `test_mongo_store`, `test_mongo_deploy`,
`test_mirror`, `test_sessions` all reach their live arms and skip cleanly on
`TOUCH_MONGO_URI` being unset; `tests/test_stdlib_only.py` is green, so
`agents.py` still names no database driver.

## 3. Attempt-1 critique findings — closed in the tree, and each one asserted

| finding | state in the tree | asserting test |
|---|---|---|
| **BLOCKER 1** `$set` on `fragmentTips.*.lastUuid` | `lastUuid` no longer stored on its own; tip is `{lineCount, records, lastTs, lastMark}`, all written with `ms.op_max` (`agents.py:1340-1345`). `lastMark` is `"<lineCount:012d>#<lastUuid>"`, monotone as a unit, so the tip stays coherent | `test_a_growing_fragment_adds_one_element_not_one_per_tick` (both orders + fingerprint equality) |
| **BLOCKER 2** whole-`spawn` `$set` | `spawn` written leaf by leaf: immutables (`recordUuid`, `toolUseId`, `sessionId`) under `$min`, every `fileHint` leaf under `$max` (`agents.py:1394-1402`) | `test_the_spawn_hint_is_order_free_and_coherent` |
| **MAJOR 3** rebuild vs backfill disagree | `assemble()` now resolves meta per-field with `_min_observed`, labels per-leaf, and `unconventional` with `$min` over **every** fragment (not chain-first) | `test_rebuild_and_backfill_resolve_a_disagreement_identically` |
| **MAJOR 4** phantom `fragments[]` element | fragments with no `first_uuid` contribute no `fragments[]`/`files[]`/`sessions[]` entry and are counted `no_first_record` (`agents.py:938`, `:1329`) | `test_a_fragment_with_no_first_record_writes_no_phantom_element` |
| **MINOR 5** per-path arm ignored R-25 scope | scope predicate applied in the per-path source arms | `ok: …and applies R-25's scope: an agent under a FOREIGN slug is not ours, so --backfill and --rebuild see the same corpus (R-56)` and the matching spawn-arm assertion in `test_the_sources_seam_matches_mirrors_contract` |
| **MINOR 6** `spawn_agent_conflict` unreachable | conflict consulted before the `launch is None` return; reported once, not double-counted | `test_every_declared_skip_counter_has_a_firing_case` (doctored one-toolUseId/two-agentIds fixture; also asserts `spawn_without_tool_use == 0`) |
| **MINOR 7** six counters unasserted | every declared counter has a firing case, closed by `fired == set(agents._skips())` | same test |
| **MINOR 8** raw `runId` vs `refs.run_key` | one `_run_ref` helper (`agents.py:1817`), used at every run lookup | `test_every_run_lookup_goes_through_refs_run_key` |
| **MINOR 9** deviation unrecorded | `findings/sp-agents-reducer-storage-deviation.md` (105 lines) records D-1 `fragments`/`fragmentTips` split, the `fragments_of()` reader contract binding on sp-12/sp-13, and the handoff asking sp-05/sp-15 for the `accumulable` addition — no edit to `mongo_store.py` | n/a (plan-side record) |
| **NIT 10/11/12** | docstrings corrected; verdict vocabulary exported | `test_the_verdict_vocabulary_is_exported` |

Remaining `$set` in `agents.py` is a single site (`:1755`) on the **`derived`**
collection, which GD-23 explicitly declares drop-and-rebuild — it is not part
of the `agents` commuting algebra and is correct there.

## 4. Anti-tautology check — 9 mutation probes, 9 caught

Applied to a *copy* of `aggregator/agents.py` in the scratchpad; the repo tree
was never modified. Each mutation was reverted and the copy re-verified green
between probes.

| # | mutation | rc | caught by |
|---|---|---|---|
| 1 | `fragmentTips` written with `op_set` instead of `op_max` (Blocker 1 reintroduced) | 1 | `test_agents.py` |
| 2 | `spawn.fileHint` written as one whole `$set` (Blocker 2 reintroduced) | 1 | `test_agents.py` |
| 3 | `map_agent` emits `fragments[]` for unidentified fragments (Major 4 reintroduced) | 1 | `test_agents.py` |
| 4 | `assemble` resolves meta chain-first instead of `$min` (Major 3, meta half) | 1 | `test_agents.py` |
| 5 | `unconventional` taken chain-first from `marker_bearing[0]` (Major 3, label half) | 1 | `test_agents.py` |
| 6 | `_run_ref` returns the raw `runId` (Minor 8 reintroduced) | 1 | `test_reducer.py` |
| 7 | `if idle > idle_limit:` → `if False:` (never demote to `unknown`) | 1 | `test_reducer.py` |
| 8 | `_AGENT_ID_RE` `{17}` → `+` | 1 | `test_agents.py` |
| 9 | `order_fragments` returns directory (path-sorted) order | 1 | `test_agents.py` |

Every previously reported defect now has a test that fails when the defect is
put back — the fixes are asserted, not merely applied.

## 5. Plan conformance (sp-10 / R-28, R-48, R-54; SD-1, SD-9, SD-11)

All items previously verified in attempt 1 §3 remain present and green; the
re-verified points this attempt:

- **R-28** — `(runId,key,ordinal)` / 17-hex `agentId` node refs; markers are a
  layer that never creates; `unconventional` defaults `True` and is merged with
  `$min` on **both** arms now (mutation 5 pins it).
- **R-48** — `parentUuid → uuid` chain order (mutation 9), `sessionId` never a
  grouping key, meta-bearing fragment wins, union writes with a bounded
  `fragments[]`, `spawn{recordUuid, toolUseId, fileHint}` with the hint
  validated against `(st_dev, st_ino, size)` and stale-marked, `records.findOne`
  jump-to-spawn, offset-never-identity refusal.
- **R-54 / GD-23** — `reduce(state, *, now=…)` is the single derivation site,
  pure over `(state, now)` (AST guard lists only `liveness` and `reduce` as
  state constructors), `REDUCER_VERSION` + drop-and-rebuild, three-state
  liveness with `IDLE_LIMIT_SECONDS = 180` and no stored `state` field,
  freeze-to-stale inside the reducer.
- **SD-1/SD-9/SD-11** — mapper purity + `_only_ours("agents")`, topology read as
  an optional shape, `_id`s only via `refs.agent_key`/`refs.run_key`, operator
  vocabulary `$max`/`$addToSet`/`$min`/`$setOnInsert` on `agents`.
- **GD-21** — `pymongo` named nowhere in `agents.py`; `test_stdlib_only` green.

## 6. Ownership / git

`git log --oneline -3` unchanged (`579446e`, `7444331`, `97ee7d7`) — **no
commit** was made. `aggregator/`, `tests/`, `docs/` are bulk-untracked (they
predate the C1/C2 bootstrap commits), so mtime ordering was used: the three
most recently written files in the whole tree are exactly
`aggregator/agents.py` (14:12:59), `tests/test_agents.py` (14:12:21),
`tests/test_reducer.py` (14:09:39). The next file down
(`tests/test_legacy.py`, 12:51:50) is 78 minutes older and belongs to a prior
sub-plan. No file outside sp-10's ownership list was touched; the only other
new path is the task-state findings note, which is required output.

## 7. Failures attributable to this change

**None.**
