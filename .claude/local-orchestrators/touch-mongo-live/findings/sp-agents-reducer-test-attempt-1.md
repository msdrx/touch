# sp-agents-reducer — test gate, attempt 1

**Verdict: PASS.** Owned suites 100% green; 0 NEW failures in the full suite;
0 ownership violations; 0 commits.

Environment: Python 3.13, stdlib only, `TOUCH_MONGO_URI` unset, no services
running. Implementer's changed set (all three sub-plan-owned):
`aggregator/agents.py`, `tests/test_agents.py`, `tests/test_reducer.py`.

---

## 1. Targeted suites (sp-10 owned) — GREEN

Run standalone from the repo root:

| suite | rc | `ok:` assertions | skips |
|---|---|---|---|
| `tests/test_agents.py` | **0** | 112 | 0 |
| `tests/test_reducer.py` | **0** | 68 | 0 |

`test_agents.py` closes with `all agents (R-28/R-48) tests passed`,
`test_reducer.py` with `all reducer (R-54) tests passed`. Neither suite has a
conditional/skipped arm — they run entirely off the frozen fixture corpus, so
there is no hidden hole behind an absent service.

## 2. Full-suite regression gate — no NEW failure

`.claude/shared/monitoring/tests/*` (run from their own dir) + `tests/*` from
the repo root:

- PASS (18): monitoring `test_frontend`, `test_server`, `test_shell`,
  `test_watcher`; repo `test_agents`, `test_bootstrap`, `test_fixtures`,
  `test_ingest`, `test_legacy`, `test_mongo_deploy`, `test_mongo_store`,
  `test_reducer`, `test_refs`, `test_stdlib_only`, `test_store`, `test_tailer`,
  `test_usage`, `test_ws`.
- FAIL (2) — **pre-existing, not attributable to this change**:
  - `tests/test_mirror.py` (rc 1, 254 `ok:`), FAILED (3):
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` (rc 1), FAILED (1):
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

Attribution proof (the interesting risk here was that `mirror.py` discovers
`MIRROR_MAPPERS` lazily, so a brand-new `agents.py` could perturb generation
counts and rebuild fingerprints). A clean copy of `aggregator/`, `tests/`,
`docs/` was made in the scratchpad with `aggregator/agents.py`,
`tests/test_agents.py`, `tests/test_reducer.py` **removed**, and both suites
re-run there: **identical failure sets, same three + same one assertions**.
The failures therefore exist with and without sp-10's files and belong to the
two loops that closed RED (`sp-mirror-deploy`, `sp-sessions-arm`) — this gate
does not touch their files. `TOUCH_MONGO_URI`-gated live arms in both suites
skip cleanly, as GD-21/R-56 requires.

Mongo-absence behaviour verified: `test_mongo_store`, `test_mongo_deploy`,
`test_mirror`, `test_sessions` all reach their live arms and print
`skip: … TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)`.
`tests/test_stdlib_only.py` green ⇒ `agents.py` respects SD-2/GD-21 (its own
suite additionally asserts no database driver is named in the file at all).

## 3. Plan conformance (sp-10 / R-28, R-48, R-54; SD-1, SD-9, SD-11)

Present in the tree and asserted non-tautologically:

- **R-28** — nodes from harness facts: `node_ref/node_key` build
  `(runId,key,ordinal)` or full 17-hex `agentId`; `is_agent_id` enforces
  `^[0-9a-f]{17}$`. Marker labels are a *layer*: `test_a_node_exists_with_no_marker_at_all`,
  `test_labels_are_a_layer_never_an_identity`, and `unconventional` defaults to
  `True` (`Labels.unconventional`, `AgentObservation.unconventional`) with
  `$min` accumulation so any named observation wins.
- **R-48** — `order_fragments` orders by the `parentUuid → uuid` stitch chain
  with directory order and mtime explicitly rejected
  (`test_the_cross_session_pair_is_one_agent_in_chain_order`,
  `test_an_unchained_fragment_is_kept_and_counted`);
  `test_sessionid_is_never_a_grouping_key` pins the non-grouping rule;
  `read_meta` optional + `test_the_meta_bearing_fragment_wins_without_seeing_the_other`;
  union writes + `test_a_growing_fragment_adds_one_element_not_one_per_tick`;
  `spawn{recordUuid,toolUseId,fileHint}` with `check_file_hint` validating
  `(st_dev, st_ino, size)` and returning a `stale` (non-error) `HintStatus`;
  `spawn_record_filter` gives "jump to spawn" as a `records.findOne`, no file
  re-read; a spawn missing `recordUuid` is refused ("an offset is not an identity").
- **R-54 / GD-23** — `reduce(state, *, now=…)` is the single derivation site;
  `REDUCER_VERSION`, `reducerVersion` on every derived doc,
  `stale_derived`/`apply_derived` implement drop-and-rebuild (never migrate);
  `liveness()` is the three-state predicate with `IDLE_LIMIT_SECONDS = 180`
  (idle > 180 s ⇒ `unknown`, never `running`, never `failed`); freeze-to-stale
  lives inside the reducer (`test_freeze_to_stale_moved_into_the_reducer`); no
  `state` field is stored — the suite proves the clock is an argument
  (`test_the_reducer_is_pure_over_state_and_now`,
  `test_the_same_fixture_is_running_or_unknown_depending_only_on_now`).
- **SD-9** — `topology_index`/`attempt_label` read the `custom_state` topology
  as a shape; `test_topology_is_optional_and_read_as_a_shape` covers the absent
  arm ("attempt N", no denominator, no next-stage arrow), the present arm
  ("attempt 1 of 5", `next stage: synthesis`), a per-stage cap override, and an
  unparsable attempt rendered verbatim.
- **SD-1** — `MIRROR_MAPPERS` + `MIRROR_SOURCES` exported; purity is asserted
  by call-graph/attribute inspection (no clock, no filesystem, no pymongo),
  `_only_ours` refuses any collection other than `agents` (including
  `derived`), and no `$inc`/`$unset`/delete verb appears.
- **SD-11** — `_id`s via `refs.agent_key`/`refs.run_key`; ops restricted to
  `$max`/`$addToSet`/`$min`/`$setOnInsert`; `provenance` `harness` on mapped
  docs and `derived` on reducer output (`test_gd25_algebra_over_the_frozen_corpus`,
  `…is provenance:derived (GD-28) … checked across every document`).
- **GD-15** — `test_the_sources_seam_matches_mirrors_contract`: every
  registered kind has both a mapper and a source; the per-path arm returns
  nothing (never raises) for a path it does not own; the spawn arm skips agent
  transcripts because a spawn lives in the parent session.

### Anti-tautology check (mutation probe, on a scratchpad copy only)

Four mutations were applied to a *copy* of `aggregator/agents.py` outside the
repo; every one was caught (rc 1):

| mutation | caught by |
|---|---|
| `if idle > idle_limit:` → `if False:` (never demote to `unknown`) | `test_reducer.py` |
| `_AGENT_ID_RE` `{17}` → `+` (accept any hex length) | `test_agents.py` |
| `order_fragments` → return path-sorted (directory order) | `test_agents.py` |
| `if result_seen:` → `if False:` (result never ends a node) | `test_reducer.py` |

The copy was restored and re-run green after each. The repo tree was never
modified by this gate.

## 4. Ownership / git

`git status --short` shows `aggregator/`, `tests/`, `docs/` as untracked
directories (bulk-untracked since sp-01's C1/C2 predate them), so per-file
diffing is not available; mtime ordering was used instead. The three most
recently written source files in the tree are exactly
`aggregator/agents.py` (13:31:21), `tests/test_agents.py` and
`tests/test_reducer.py` (both 13:33:49). Everything else — including
`aggregator/ingest.py`, `aggregator/legacy.py`, `tests/test_ingest.py`,
`tests/test_legacy.py` — predates them and matches the earlier sub-plans'
timestamps. No file outside the sub-plan's ownership list was touched, and no
commit was made.

## 5. Failures attributable to this change

**None.**
