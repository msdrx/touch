# sp-agents-reducer — test gate, attempt 4

**Verdict: PASS.** Both owned suites 100% green; 0 NEW failures in the full
suite; the two red files are the same pre-existing baseline recorded in attempts
1–3 and belong to other sub-plans.

Gate run read-only: no source or test file was edited, nothing was committed,
`HEAD` is still `579446e`.

---

## 1. Targeted suites (must be 100% green)

Run from the repo root, stdlib only, standalone executables:

| suite | rc | evidence |
|---|---|---|
| `python3 tests/test_agents.py` | **0** | `all agents (R-28/R-48) tests passed` |
| `python3 tests/test_reducer.py` | **0** | `all reducer (R-54) tests passed` |

No monitoring-module test file is owned by this sub-plan (owned files are
`aggregator/agents.py`, `tests/test_agents.py`, `tests/test_reducer.py`), so the
`.claude/shared/monitoring/tests/` set is covered only by the regression gate
below.

## 2. Full-suite regression gate — no NEW failure

20 files: `.claude/shared/monitoring/tests/test_*.py` each from its own
directory, then `tests/test_*.py` from the repo root.

- **PASS (18):** monitoring `test_frontend`, `test_server`, `test_shell`,
  `test_watcher`; repo `test_agents`, `test_bootstrap`, `test_fixtures`,
  `test_ingest`, `test_legacy`, `test_mongo_deploy`, `test_mongo_store`,
  `test_reducer`, `test_refs`, `test_stdlib_only`, `test_store`, `test_tailer`,
  `test_usage`, `test_ws`.
- **FAIL (2) — pre-existing baseline, failure strings character-for-character
  identical to the sets recorded in attempts 1, 2 and 3:**
  - `tests/test_mirror.py` (rc 1), `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` (rc 1), `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

Attribution: both belong to loops that closed RED (`sp-mirror-deploy`,
`sp-sessions-arm`); neither failure path names `agents.py` or an sp-10 symbol,
and attempt 2 already proved the set by re-running those suites with sp-10's
three files removed. Attempt 4 changed only `agents.py`, `test_agents.py`,
`test_reducer.py` — none of which `test_mirror.py` or `test_sessions.py`
imports. Baseline failures do not fail this gate.

**Mongo-absence arm (GD-21 / R-56).** `TOUCH_MONGO_URI` is unset; every live arm
skips cleanly and prints its skip line (`test_mongo_store`, `test_mongo_deploy`,
`test_mirror`, `test_sessions` all reach `SKIP: … TOUCH_MONGO_URI is not set
(R-42's loopback+auth recipe)`). `grep -c 'pymongo\|bson' aggregator/agents.py`
= **0**; `tests/test_stdlib_only.py` rc 0.

## 3. Attempt-3 critique findings — closed in the tree, each with a real assertion

| finding | state in the tree | asserting test |
|---|---|---|
| **MAJOR 1** — `_session_activity` turned a missing/stale `sessions.lastTs` into positive evidence of idleness, demoting every warm agent of a real session to `unknown — session idle` | **fixed.** `agents.py::_session_activity` now `continue`s when `lastTs` is absent or older than the limit, so the map contains only `{sessionId: True}`; the sessionId stays **unobserved** (`None`) otherwise. Docstring records the evidence rule and points at D-7. | `test_reducer.py::test_a_session_may_promote_a_node_and_never_demote_it` — builds the `sessions` bucket through `sessions.map_session` itself (new `session_bucket()` helper), not by hand: (1) the historical doc (asserted to carry no `lastTs`) does not demote and the run stays open; (2) a six-hour-stale registry heartbeat does not demote, and the reason stays `active …`, on agents **and** nodes; (3) a 30 s-fresh heartbeat *is* read (`_session_activity(...) == {SESSION: True}`), which distinguishes "does not demote" from "ignores the field"; plus `_session_conjunct` is `True`/`None`, never `False`. Not a tautology — arm (3) fails if the field is simply dropped. |
| **MAJOR 2** — `Reduction.operations()` emitted a non-total `$set`, so the server kept retracted conclusions (`frozen: true`, stale `idleSeconds`, retracted `attemptLabel`/`nextStage`); `apply_derived` hid it by clearing the bucket first | **fixed by widening the payload**, which keeps `$set` and needs no `$unset` (GD-26): `reduce` now always emits `idleSeconds`, `frozen`, `attemptLabel`, `nextStage` (explicit `None`/`False`) on both agent and node payloads. The docstring states totality as a property of the payload and hands sp-06/sp-12 the drop-only-on-rebuild precondition. | `test_reducer.py::test_the_operation_list_is_a_total_overwrite_of_each_derived_document` — the method's first direct test. Asserts the two-tick divergence is gone (`server["derived"][…]["frozen"] is False`), that `ms.fingerprint(server) == ms.fingerprint(memory)` **and** document-for-document equality, the `idleSeconds`-beside-`done` instance, and totality as a *property*: one key set per `kind` across four different reductions. |
| **MINOR 3** — a run with no nodes yet got `runState.runId: null` | **fixed.** last-resort fallback through `refs.parse_ref_key("run", …)`, guarded by `RefError`. | green line: `a run with no nodes yet still names itself, through the grammar's proven inverse: 'wf_a%b#c' (not None, not the escaped 'wf_a%25b%23c')`, plus `…and a key the run grammar cannot parse yields None rather than a guess` — the `%`/`#` id makes escaped and raw genuinely differ. |
| **NIT 4** — unguarded `fragmentTips.<firstUuid>` dotted path | **fixed.** `_plain_field(item.first_uuid, "a fragment's firstUuid")` at `agents.py:1611`, raising as `AgentsError` like `ingest._launch_paths`. |
| **NIT 5** — dict-replay stored `unconventional: true` beside a `name` | **fixed.** `unconventional=not name` in `_as_observation` (`agents.py:528`); the `map_agent` replay path re-derives from the coerced labels. |
| **NIT 6** — `node_key` exported but called by nothing | **fixed.** `map_agent`/the node writer now key through `node_key(agent_id=…)` (`agents.py:1565`, `:1682`); the docstring states it is identical to `refs.agent_key` and names sp-11/sp-12's `(runId, key, ordinal)` half. |
| **NIT 7** — `scan(paths=[…])` bypassed ingest's memo | **fixed.** routed through `ingest._transcript_walk` (`agents.py:1366`). Asserted: `scan(paths=[…]) re-reads nothing ingest already has in hand: 17 reads, still 17` / `…and still answers with the agent`. |

The deviation file gained **D-7** (`sessions.lastTs` is not a liveness clock;
the conjunct may only PROMOTE) with the measured six-hour `updatedAt`, and the
handoff section now tells sp-07, sp-12 and sp-13 that a "session idle" badge
needs a positive end observation, not the age of that field.

## 4. Sub-plan verification (sp-10, subplans.md:288-308)

- **Owned files present and only those touched.** The three newest files under
  `aggregator/`+`tests/` are exactly `aggregator/agents.py` (15:32),
  `tests/test_agents.py` (15:29), `tests/test_reducer.py` (15:27); the next
  newest, `tests/test_legacy.py` (12:51), predates this attempt by hours. Plus
  the sub-plan's own `findings/sp-agents-reducer-storage-deviation.md`. No file
  outside the ownership list was modified.
- **R-28** — harness facts create nodes on the full 17-hex agentId; marker
  layer only labels (`_labels_of` never creates); unnamed ⇒ `unconventional`.
  Covered by `test_agents.py`'s label/precedence arms, all green.
- **R-48** — `fragments[]` ordered by the `parentUuid → uuid` stitch chain
  (`_chain_fields`, `first_parent_uuid`), never directory order; `sessionId`
  never a grouping key; meta-bearing fragment wins; union writes; perishable
  `spawn.fileHint` validated against `(st_dev, st_ino, size)` and stale-marked;
  "jump to spawn" via `records.findOne`. Green in `test_agents.py`.
- **R-54 / GD-23** — observations in, derived out; `reducerVersion` +
  drop-and-rebuild (`needs_rebuild`, `apply_derived` clears the bucket, the
  ghost-document arm asserts the drop); three-state liveness computed from the
  `now` **argument** (`test_the_reducer_is_pure_over_state_and_now` proves the
  reducer touches no file, no socket, and takes its clock as a parameter); no
  `state` field written by either mapper; idle > 180 s ⇒ `unknown`, which stays
  in the running set and is never `failed` (`failed` is absent from
  `NODE_STATES`); freeze-to-stale lives in the reducer; topology per SD-9 with
  `attempt N of M` / `nextStage`.
- **GD-25 / SD-1** — the AST guard
  `test_sd1_the_mappers_are_pure_and_write_only_agents` still proves the mapping
  half builds only commuting builders and writes only `agents`; `_only_ours`
  keeps `derived` unreachable through `MIRROR_MAPPERS` (GD-23's single writer).
- **GD-26** — no delete verb, no `$unset`/`$inc` literal; MAJOR 2 was fixed by
  widening the payload precisely so no retraction verb is needed.
- **Tests assert behavior, not tautologies.** Spot-checked the three new arms:
  each one constructs the failing state from the *real* producing module
  (`sessions.map_session`, `ms.apply_operations`, `refs`), reduces it, and
  asserts an observable string/flag — and each carries a counter-arm that fails
  if the fix were "ignore the input" rather than "interpret it correctly".

## 5. Commands run

```
python3 tests/test_agents.py                                  # rc 0
python3 tests/test_reducer.py                                 # rc 0
# full gate
cd /home/laniakea/Projects/touch && rc=0; \
  for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done; \
  for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc
# -> 18 pass, 2 baseline fail (test_mirror, test_sessions), no NEW failure
```
