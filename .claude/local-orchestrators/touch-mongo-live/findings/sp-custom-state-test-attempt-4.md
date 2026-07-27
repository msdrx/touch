# sp-custom-state — test gate, attempt 4

**Gate result: PASS.**

Both owned suites green (392 assertions: 234 + 158 across 27 + 21 test
functions, 2 designed skips). Full-suite regression clean: the only two failing
files are the pre-existing baseline pair, proved not attributable to this
sub-plan. Ownership boundary clean. Nothing committed (`git log -1` is still
`579446e orchestration history`).

## 1. Targeted suites (owned) — GREEN

| suite | rc | assertions | skips |
|---|---|---|---|
| `python3 tests/test_custom_state.py` (repo root) | 0 | 234 `ok:` / 27 tests | `test_live_head_guard_matches_the_model` — `TOUCH_MONGO_URI` unset |
| `python3 tests/test_slots.py` (repo root) | 0 | 158 `ok:` / 21 tests | `test_live_duplicate_key_is_tolerated_not_raised` — `TOUCH_MONGO_URI` unset |

Both skips print the R-42 loopback+auth recipe line, so the GD-21/R-56
no-mongod arm is honest rather than silent.

## 2. Full suite regression gate

`for t in .claude/shared/monitoring/tests/test_*.py … ; for t in tests/test_*.py …`

- **PASS (20):** monitoring `test_frontend`, `test_server`, `test_shell`,
  `test_watcher`; repo `test_agents`, `test_bootstrap`, `test_custom_state`,
  `test_fixtures`, `test_ingest`, `test_legacy`, `test_mongo_deploy`,
  `test_mongo_store`, `test_reducer`, `test_refs`, `test_slots`,
  `test_stdlib_only`, `test_store`, `test_tailer`, `test_usage`, `test_ws`.
- **FAIL (2) — pre-existing baseline, NOT attributable to this sub-plan:**
  - `tests/test_mirror.py` rc 1, `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` rc 1, `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

### Attribution evidence (three independent legs)

1. **Character-for-character identical** to the baseline set already recorded in
   `sp-agents-reducer-test-attempt-4.md` §2 (and in that sub-plan's attempts 1–3),
   which predates this attempt. Those failures belong to loops that closed RED
   (`sp-mirror-deploy`, `sp-sessions-arm`).
2. **Removal probe.** `aggregator/`, `tests/`, `docs/` copied to the scratchpad
   with `aggregator/custom_state.py` **deleted** (so `mirror.discover_mappers`
   never registers this sub-plan's `customState`/`slot` kinds or its
   `MIRROR_SOURCES`): `tests/test_mirror.py` still rc 1 with the same 3 failures,
   `tests/test_sessions.py` still rc 1 with the same 1 failure. The regression
   survives this sub-plan's total absence.
3. **Determinism.** `test_mirror.py` re-run ×3 and `test_sessions.py` ×2 — the
   same failure sets every time, so this is not flake noise masking a new break.
   Neither failure path names `custom_state`, a slot, or a custom-state kind;
   the failing assertions are about the mirror's breaker call count, its
   generation sweep, and a `writers` (lease) document in a rebuild count.

Baseline failures do not fail this gate.

## 3. Plan conformance (sp-11 / R-52 / R-53)

Owned files all present and non-empty: `aggregator/custom_state.py`,
`tests/test_custom_state.py`, `tests/test_slots.py`, plus the
`touch-orchestrate/SKILL.md` :52-56 ledger amendment (`git diff --stat`:
+13/−1, that file only).

Verified in this run's suite output:

- R-52 head/log split: `custom_state_events` insert-only (`$setOnInsert` only),
  `custom_state` head guarded; three out-of-order writes leave the head at the
  highest `seq`; **and** the new cross-stream case
  (`test_two_streams_that_share_a_seq_still_leave_one_head`) — three streams at
  `seq` 1 on one head `_id` now yield ONE fingerprint over every arrival order
  and over a rebuild, with the stored payload selected by the order key rather
  than by arrival. This is critique-3's M1, closed.
- The order field is reached by `$max` and never by `$set`, guarded by a strict
  `$lt`, and sorts identically to `seq` within a stream (zero-padded);
  `seq`/`fromSeq` stay on the document with their R-52 meaning. The
  `mongo_store.accumulable` fence for `order` is correctly handed off in
  `sp-custom-state-head-order-deviation.md` (sp-05's one-line paste) rather than
  edited across ownership.
- Unknown refId rejected (with the documented `topology` widening), 16 KB
  annotation cap **rejects** with 413 (never truncates), tombstone deletes with
  no delete verb anywhere, provenance pinned `{asserted,touch}` with no writer
  path to `harness`, wipe+replay and drop+rebuild both document-for-document
  equal, one installation-wide events+head pair, kind-discriminated.
- R-53 slots: `pending|bound|orphaned|conflict` + `pendingSince`, both ids on
  conflict, unique **sparse** `{agentId:1}` index, duplicate key tolerated and
  counted, ledger/control readers count every dropped line by reason,
  `sessionKeySource` ∈ `{ledger, marker, path, slots}`.
- GD-21: `grep -c 'pymongo\|bson' aggregator/custom_state.py` = **0**;
  `tests/test_stdlib_only.py` rc 0.

## 4. Anti-tautology probes (mutations applied to a scratchpad copy only; the
repo tree was never modified)

| mutation | result |
|---|---|
| head guard reverted to `{"seq": {"$lt": obs.seq}}` | **CAUGHT** (`test_custom_state` rc 1) |
| order value drops its stream component (`order.split("\|")[0]`) | **CAUGHT** |
| `_event_document` author → `obs.author or AUTHOR` (unvalidated) | **CAUGHT** |
| head payload author → `obs.author or AUTHOR` | **CAUGHT** |
| resolved-branch `key_source` → constant `"slots"` | **CAUGHT** |
| `SESSION_PATH_PARENTS` widened with `"state"` | **CAUGHT** |
| `resolution_of`'s new `or doc.get("conflictWith")` clause removed | **SURVIVED** — see below |

Critique-3's M1, m1, m2 and n1 are therefore each held by a real assertion, not
by a restatement of the implementation.

## 5. Non-blocking observation for the critique gate

**critique-3 m3 is fixed in the code but not held by a test.**
`aggregator/custom_state.py:1954-1955` now reads
`… or doc.get("conflictAgentIds") or doc.get("conflictWith")`, which is exactly
what the critique asked for — but deleting the `conflictWith` disjunct leaves
both owned suites green. Nothing exercises the crash-between-writes shape
(unguarded `conflict_evidence_op` applied, guarded state write lost ⇒
`conflictWith` present, `conflictAgentIds` length 1, `resolution:"pending"`),
so the clause that stops `sweep` from promoting a contested stop to `orphaned`
is currently unasserted.

Suggested fix (test-only, inside ownership): in `tests/test_slots.py`, apply
`conflict_evidence_op(..., conflict_with=[holder])` alone to a `pending` slot
and assert `resolution_of(doc) == "conflict"` and that `SlotTable.sweep` leaves
it alone. This is a coverage gap on a MINOR hardening, not a behavioural
failure of a plan item, so it does not fail this gate.

## 6. Ownership

`find . -newermt "2026-07-26 17:50"` over the repo, excluding `.git`,
`__pycache__` and `.claude/`, lists exactly three files — all owned:
`aggregator/custom_state.py` (18:08), `tests/test_custom_state.py` (18:06),
`tests/test_slots.py` (17:59). Every other `aggregator/*.py` and `tests/*.py`
carries an mtime from an earlier sub-plan's loop. Under `.claude/`, the only
files written in that window are this sub-plan's own deviation note
(`sp-custom-state-head-order-deviation.md`) and the concurrent monitoring /
cycle-reporter daemons' state and `report/cycles/*.html` — not implementer edits.

`git status --porcelain` shows `aggregator/`, `tests/`, `docs/` still whole-
directory untracked from the prior pass; the one tracked file this sub-plan
owns a slice of, `.claude/skills/touch-orchestrate/SKILL.md`, is +13/−1 and
confined to the ledger-line bullet. **No commits made.**

## Failures

None attributable to sp-custom-state.
