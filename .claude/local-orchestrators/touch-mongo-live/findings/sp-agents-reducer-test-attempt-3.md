# sp-agents-reducer — test gate, attempt 3

**Verdict: PASS.** Owned suites 100% green; 0 NEW failures in the full suite;
0 ownership violations; 0 commits.

Environment: Python 3.13, stdlib only, `TOUCH_MONGO_URI` unset, no services
running, no third-party packages installed. Implementer's changed set:
`aggregator/agents.py`, `tests/test_agents.py`, `tests/test_reducer.py`
(all sp-10-owned) plus the task-state note
`findings/sp-agents-reducer-storage-deviation.md`.

---

## 1. Targeted suites (sp-10 owned) — GREEN

Run standalone from the repo root:

| suite | rc | `ok:` assertions | skipped arms |
|---|---|---|---|
| `tests/test_agents.py` | **0** | 179 | 0 |
| `tests/test_reducer.py` | **0** | 86 | 0 |

Closing lines: `all agents (R-28/R-48) tests passed`,
`all reducer (R-54) tests passed`. Neither suite has a conditional arm — both
run off the frozen fixture corpus and temp dirs, so no hole hides behind an
absent service. Assertion counts up from attempt 2 (162 → 179, 80 → 86),
matching the new coverage in §3.

## 2. Full-suite regression gate — no NEW failure

`.claude/shared/monitoring/tests/*` (from their own dir) + `tests/*` (repo root),
20 files:

- **PASS (18):** monitoring `test_frontend`, `test_server`, `test_shell`,
  `test_watcher`; repo `test_agents`, `test_bootstrap`, `test_fixtures`,
  `test_ingest`, `test_legacy`, `test_mongo_deploy`, `test_mongo_store`,
  `test_reducer`, `test_refs`, `test_stdlib_only`, `test_store`, `test_tailer`,
  `test_usage`, `test_ws`.
- **FAIL (2) — pre-existing baseline, byte-identical to the sets recorded in
  attempts 1 and 2:**
  - `tests/test_mirror.py` (rc 1), `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` (rc 1), `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

Both belong to loops that closed RED (`sp-mirror-deploy`, `sp-sessions-arm`);
neither names `agents.py` or a sp-10 symbol in its failure path. The failure
strings are character-for-character the same as attempt 2's recorded baseline,
which was itself proven attributable by re-running those suites with sp-10's
three files removed. This gate touched none of their files.

Mongo-absence behaviour (GD-21 / R-56 no-mongod arm): `test_mongo_store`,
`test_mongo_deploy` reach their live arms and skip cleanly with
`TOUCH_MONGO_URI` unset and pymongo not importable; `tests/test_stdlib_only.py`
is green; `grep` confirms `pymongo`/`bson` appear nowhere in `aggregator/agents.py`.

## 3. Attempt-2 critique findings — closed in the tree, each one asserted

| finding | state in the tree | asserting test |
|---|---|---|
| **MAJOR 1** `fragments[]` identity carried `scan.first_ts` (a file-wide minimum), so an out-of-order append permanently duplicated the `$addToSet` element | `Fragment.first_record_ts` added (`agents.py:609`), set from the first record's own stamp in `read_fragment` (`:830`), and `identity()` emits it (`:634-635`); `scan.first_ts` still feeds the agent-level `$min` `firstTs`, which wants the minimum | `test_an_out_of_order_timestamp_does_not_duplicate_a_fragment` — asserts ONE element and ONE `fragments_of()` entry after an append stamped 5 s before the first record, both spellings collapsing, and the two observations still commuting |
| **MINOR 2** both mappers wrote top-level `agentType`/`model`, so `$min` picked by BSON collation and overrode R-48's meta-wins rule | launch-side values namespaced to `spawn.agentType` / `spawn.resolvedModel` (`:1605`), removed from the top-level scalars; docstring corrected (`:1557-1560`) | `test_the_two_mappers_do_not_fight_over_agent_type_and_model` — asserts the meta-bearing fragment keeps `model` in BOTH observation orders, the launch spelling is kept beside it, and the stored spawn shape has no top-level `model` |
| **MINOR 3** `$max` tips presented as unconditionally safe while GD-26 allows shrinks | new **D-5** in the deviation file (`:141-166`) states the shrink exposure and hands it to sp-06's generation sweep; module docstring amended | n/a (documentation-first fix, which the critique accepted) |
| **MINOR 4** `META_FIELDS` dropped `description`/`toolUseId`, the only source when no launch pair exists | `META_FIELDS = ("agentType", "model", "spawnDepth", "description", "toolUseId")` (`:351`); recorded as **D-6** | `test_the_meta_bearing_fragment_wins_without_seeing_the_other` + `test_the_two_mappers_do_not_fight_over_agent_type_and_model` (`toolUseId`/`description` agree with the launch, so `$min` stays a no-op) |
| **NIT 5** a run closing with failing verdicts rendered the bare word `done` | `label = f"{DONE} — {verdicts[FAILED]} failed verdict(s)"` (`:2265`), distinct from `CLOSED_NO_VERDICT`; `failed` still absent from `NODE_STATES` | `test_a_failing_verdict_is_a_verdict_not_a_state` — asserts the tally in the label, that it is not `closed — no verdict`, and that the state stays one of the three |
| **NIT 6** topology refId key space was an unstated contract with sp-11 | handoff item 2 in the deviation file names `refs.run_key(runId)` as the refId a topology head must carry | `test_topology_is_optional_and_read_as_a_shape` (a foreign-ref topology does not join and is counted honestly) |
| **NIT 7** `derivedFromSeq` is a global max over two independent counters | documented at `:2002`; a caller holding a real watermark may pass it in | `test_derived_documents_are_droppable_and_versioned` — asserts the custom stream alone answers 7 while the max is 42, i.e. the pair is explicitly not recoverable |
| **NIT 8** `--rebuild` walked the corpus twice | scan memoized across the two `MIRROR_SOURCES` entries | `test_the_two_rebuild_arms_read_the_corpus_once` — 8 reads total, not 16; an appended file still busts the key and is re-read |
| **NIT 9** dict-labels observation raised `AttributeError`, bypassing the `AgentsError` funnel | coerced in `_as_observation`; unusable values raise `AgentsError` | `test_the_mapper_refuses_what_it_cannot_key` (`a replayed observation whose labels are a dict maps like a Labels`, `…and an unusable labels value is an AgentsError`) |

## 4. Anti-tautology check — 6 mutation probes, 6 caught

Applied to a *copy* of `aggregator/agents.py` in the scratchpad
(`…/scratchpad/probe3`); the repo tree was never modified. The copy was verified
green before the run and each mutation reverted between probes.

| # | mutation | caught by |
|---|---|---|
| 1 | `first_record_ts=first.ts` → `first_record_ts=scan.first_ts` (MAJOR 1 reintroduced) | `test_agents.py` |
| 2 | `immutable["spawn.resolvedModel"]` → `immutable["model"]` (MINOR 2 reintroduced) | `test_agents.py` |
| 3 | `META_FIELDS` back to the three-field tuple (MINOR 4 reintroduced) | `test_agents.py` |
| 4 | `label = DONE` (NIT 5 reintroduced) | `test_reducer.py` |
| 5 | `ms.op_max(tips)` → `ms.op_set(tips)` (attempt-1 Blocker 1, checked for erosion) | `test_agents.py` |
| 6 | `IDLE_LIMIT_SECONDS = 180` → `10**9` (never demote to `unknown`) | `test_reducer.py` |

Every fix is asserted, not merely applied, and the earlier attempts' pins have
not eroded.

## 5. Plan conformance (sp-10 / R-28, R-48, R-54; SD-1, SD-9, SD-11)

- **R-28** — harness facts create nodes (`(runId,key,ordinal)` and full 17-hex
  `agentId`); markers are a label layer that never creates; unnamed ⇒
  `unconventional`, merged with `$min` on both arms.
- **R-48** — `fragments[]` ordered by the `parentUuid → uuid` stitch chain, never
  directory order; `sessionId` never a grouping key (7 agents from 8 transcripts,
  the split pair one document); `.meta.json` optional with the meta-bearing
  fragment winning; union writes; `spawn{recordUuid, toolUseId, fileHint}` with
  the hint validated against `(st_dev, st_ino, size)`, stale-marked and kept,
  identity never offset, jump-to-spawn via `records.findOne`.
- **R-54 / GD-23** — `reduce(state, *, now=…)` is the single derivation site and
  is pure over `(state, now)` (AST guard: only `liveness` and `reduce` construct
  a state); `reducerVersion` + drop-and-rebuild; three-state liveness with
  `IDLE_LIMIT_SECONDS = 180`, no stored `state` field, idle > 180 s ⇒ `unknown`
  which leaves the running set and is never `failed`; freeze-to-stale inside the
  reducer; topology per SD-9 read as an optional shape.
- **SD-1 / SD-11** — mappers pure, `_only_ours("agents")` (rejects `derived`),
  operator vocabulary `$max`/`$min`/`$addToSet`/`$setOnInsert` only, `_id`s only
  via `refs.agent_key`/`refs.run_key`.
- **GD-25** — shuffled, reversed, assembled and re-ingested passes over the frozen
  corpus all yield the identical fingerprint and counts.
- **GD-21** — no database driver named in `agents.py`; `test_stdlib_only` green.

## 6. Ownership / git

`git log --oneline -3` unchanged (`579446e`, `7444331`, `97ee7d7`) — **no commit
was made**. `aggregator/`, `tests/`, `docs/` are bulk-untracked (they predate the
C1/C2 bootstrap commits), so mtime ordering was used: the three most recently
written files in the whole tree are exactly

```
2026-07-26 14:59:06  aggregator/agents.py
2026-07-26 14:57:27  tests/test_agents.py
2026-07-26 14:54:01  tests/test_reducer.py
2026-07-26 12:51:50  tests/test_legacy.py   ← next down, 2h older, prior sub-plan
```

No file outside sp-10's ownership list was touched. `git status` for tracked
paths shows only the pre-existing in-flight `.claude/` state; the only other new
path is the required findings note.

## 7. Failures attributable to this change

**None.**
