# sp-custom-state — adversarial critique, attempt 2

**Verdict: REJECTED** (2 major, 4 minor, 3 nits; 0 blockers).
Depth: **in-scope** — every fix lands inside the four owned files (one of them
needs a deviation note of the shape this sub-plan has already written once).
critical_defect: **false**.

Reviewed (untracked-new, so full content):
`aggregator/custom_state.py` (2182 lines), `tests/test_custom_state.py` (933),
`tests/test_slots.py` (759),
`findings/sp-custom-state-head-driver-deviation.md` (88).
Against: subplans §`sp-11 — custom-state`, amendment R-52/R-53 + GD-21…GD-30,
base plan GD-1…GD-20, SD-1/SD-8/SD-11.

Both owned suites are green here (`rc 0` each, live arms skipping cleanly with
`TOUCH_MONGO_URI` unset). Ownership is clean: `git status` shows only
`aggregator/`, `tests/`, the granted `touch-orchestrate/SKILL.md` :52-56 slice
(+13/−1, confined to the ledger-line bullet) and the new findings file; HEAD is
still `579446e`; nothing committed.

## Attempt-1 dispositions — verified present, not taken on trust

| id | claim | verified |
|---|---|---|
| M1 | control arm reads the SKILL.md shape via the name→slot hop | **yes** — `SlotIndex` (`:886-976`), `read_control_file:2096`; `test_a_control_line_in_the_skill_files_own_shape_is_ingested` |
| M2 | both foreign readers count their drops | **yes** — `new_counters():979`, and mutating away the `read`/`unreadable`/`skipped_ambiguous` bumps is caught |
| M3 | control `attempt` never defaults | **yes** — `attemptSource` resolved/stated only, `ATTEMPT_SOURCES:387`; mutating `attempt_source` to a constant is caught |
| M4 | head/bind driver handoff named | **yes** — module docstring `:161-182` + the deviation file, naming sp-12 (sp-14 fallback) with paste-in code |
| m1 | stream scope disambiguated by realpath digest | **yes** — `_path_scope:721`; removing the digest is caught |
| m2 | 16 KB cap value pinned | **yes** — `test_custom_state.py:337` |
| m3 | `ref` vs `refId` must agree | **yes** — `resolve_ref_id:525` |
| m4 | session key only under a named parent dir | **yes** — `SESSION_PATH_PARENTS:380`; removing the gate is caught |
| m5 | the wipe test really wipes | **yes** — `test_custom_state.py:252-260` clears the populated state and asserts `counts == 0` first |
| n1/n3/n4 | mode 755 / `resolvedTs` / unguarded conflict evidence | **yes** — `_slot_advance:1394-1412`, `conflict_evidence_op:1510`; removing the unguarded evidence write is caught |

Also re-confirmed by direct check: no `pymongo` token in the module (GD-21);
every `_id` comes from `refs` (`custom_state_event_key` / `custom_state_key` /
`slot_key`), no sub-document `_id` or equality-match subdocument key (GD-24);
`custom_state_events` is `$setOnInsert`-only and no delete verb / `$unset` / TTL
appears anywhere (GD-26); `validate_provenance` is the single door and no code
path reaches a mirror-class value (GD-28); `MIRROR_MAPPERS` kinds
(`customState`, `slot`) are unique across all five entity modules (SD-1/GD-15);
no credential literal, and the live arm drops only `touch_test_<pid>` (GD-27).

The two majors below are both **GD-25**, and both are demonstrable with the
project's own oracle rather than by reading.

---

## MAJOR

### M1 — `slots` diverges under GD-25's shuffled/reversed pass, and the test that claims to assert it cannot see it
`aggregator/custom_state.py:1272-1281` (`map_slot`'s `$addToSet` sets),
`tests/test_slots.py:294-304` + `:105-109` (`slot_obs`)

GD-25's acceptance is literal: "ingest … normally, shuffled, and reversed;
fingerprint over all documents sorted by `_id` is identical on every pass".
`map_slot` writes two `$addToSet` arrays — `agentIds` (`:1276`) and `evidence`
(`:1281`) — and `conflict_write`/`conflict_evidence_op` add two more
(`conflictAgentIds`, `conflictWith`, `:1483-1486`). `mongo_store.fingerprint`
sorts an array **only** when its collection spec declares it in `set_fields`
(`mongo_store.py:1291-1303`), and the `slots` spec declares
`set_fields=()` — the only mirrored spec with `$addToSet` arrays that declares
none (`sessions`, `run_nodes`, `agents` all declare theirs).

Reproduced against the shipped code, two orders of the same two observations:

```python
o1 = SlotObservation(session_key="622-10028", root="auth", name="impl1",
                     attempt=1, agent_id="a"*17, bound_by="ledger")
o2 = SlotObservation(..., agent_id="b"*17, bound_by="marker")
ms.fingerprint(build([o1, o2])) != ms.fingerprint(build([o2, o1]))
# e68909752623e01c…  vs  5609600d6c969b97…
# doc: agentIds ['aaa…','bbb…'] / evidence ['ledger','marker']  (arrival order)
```

The suite does not catch it because
`test_the_state_machine_is_monotone_under_any_replay_order:294-304` shuffles
four observations built by `slot_obs()` (`:105-109`), and `slot_obs` sets
neither `agent_id` nor `bound_by` — so no `$addToSet` array in the shuffled
corpus ever holds more than zero elements. The one assertion that names GD-25
for this arm is fixture-blind to the only operator that can break it, which is
the same "written to the implementation rather than to the contract" shape
attempt 1 flagged as M1.

Impact is not confined here: R-44/sp-14's acceptance test runs exactly this
fingerprint over **all** collections, and the deviation file's own paste-in
driver (`bind_slot(..., by="marker")` on every marker/ledger/description
evidence) is what will start populating those arrays. Today the arrays stay
empty only because `read_ledger_file` never sets `agent_id`/`bound_by` and
nothing calls `slot_from_labels` — i.e. the breach is latent precisely until
sp-12 closes the handoff this sub-plan just wrote.

**Fix (inside ownership):** extend the shuffle corpus to include observations
carrying `agent_id` + `bound_by` (at least two distinct values each), so the
assertion actually exercises the sets. The one-line correction it will expose —
`set_fields=("agentIds", "evidence", "conflictAgentIds", "conflictWith")` on the
`slots` spec — lives in `aggregator/mongo_store.py`, which sp-05 owns and this
sub-plan may not edit: record it in a deviation note next to the head-driver
one, naming the owner and the exact tuple, exactly as M4 was closed. Do not
"fix" it by sorting inside the test and calling the invariant held.

### M2 — `taskId` and `runNode` are written by `$max` *and* by a bare `$set`, so their stored value is write-order dependent
`aggregator/custom_state.py:1263-1270` (mapper `$max`) vs `:1430-1432`
(`bind_write`) and `:1460-1461` (`bind_advance_write`), via `_slot_advance:1404`

`map_slot` accumulates `taskId` and `runNode` with `$max` ("deterministic under
shuffle, where `$set` is write-order dependent" — its own comment, `:1268`).
`_slot_advance` then `$set`s the same two fields on every bind. `$set` can
*lower* what `$max` raised, so the final document depends on which arrived last.
Demonstrated on the shipped code:

```python
t.observe(SlotObservation(..., task_id="t9"));  # then, in two orders:
bind(task_id="t1") ; observe(task_id="t9")   ->  taskId == "t9"
observe(task_id="t9") ; bind(task_id="t1")   ->  taskId == "t1"
```

GD-25 names this failure verbatim ("`$set` is write-order dependent"), and the
`slots` spec's `accumulable=()` means nothing fences it — the module is the only
thing that can. `resolution`/`resolutionRank` are safe (the rank guard makes
them monotone) and `boundBy` is a conclusion, so this is specifically about the
two fields the mapper already accumulates.

Removing `taskId`/`runNode` from the advance's `$set` entirely keeps every test
green, which confirms nothing asserts the current behaviour either.

**Fix:** drop `taskId`/`runNode` from `_slot_advance`'s `$set` payload (the
mapper already carries them monotonically), or emit them as a `$max` leg of the
same guarded update via `ms.merge_ops(ms.op_set(...), ms.op_max({...}))`; then
assert the two orders above land on one document.

---

## MINOR

### m1 — `sessionKeySource` settles by alphabet, not by trust
`aggregator/custom_state.py:1265` (inside the `$max` loop)

`sessionKeySource` rides `$max` with the mapper's other scalars, so for the
three values that reach a slot document the winner is lexicographic:
`path` > `marker` > `ledger`. A slot whose session was **stated** by an amended
ledger line but also observed once through a path-derived line therefore reports
`"path"`. It is the conservative direction (it understates confidence rather
than overstating it), but CUSTOMSTATE-10's field exists to say *how* the
attribution was obtained, and deciding that by string ordering is an accident,
not a rule. Renaming the field in the `$max` loop (so it is never written) keeps
both suites green — nothing asserts it on a slot document at all.

**Fix:** either accumulate the sources as an `$addToSet` set (`sessionKeySources`)
and derive the label at read time, or map the three values to an explicit rank
and `$max` that, with a one-line comment saying which direction wins and why.

### m2 — Nothing pins the provenance of a ledger event, and `touch` passes
`aggregator/custom_state.py:1309` (`_ledger_event(..., provenance="asserted")`)

Changing `"asserted"` to `"touch"` leaves both suites green. GD-28's whole point
is the asserted/touch split — a ledger line is an *agent's* claim, and labelling
it `touch` says Touch authored it. The module docstring states the rule
(`:300-303`) and the code gets it right; only the test is missing.
**Fix:** assert `map_slot(ledger_obs)`'s `custom_state_events` document carries
`provenance == "asserted"`, and that a `Writer`-authored record carries
`"touch"`.

### m3 — The control reader's `sessionKeySource` is unasserted; a constant passes
`aggregator/custom_state.py:2094` and `:2107`

Replacing both branches with the constant `"slots"` — i.e. labelling a
writer-stated `sessionKey` as one Touch inferred from the slot index — keeps
every test green. This is the same honesty field m1 is about, on the arm M1/M3
of attempt 1 were about.
**Fix:** one assertion per branch (stated ⇒ `"ledger"`, path-derived ⇒
`"path"`, resolved ⇒ `"slots"`) in
`test_a_control_line_in_the_skill_files_own_shape_is_ingested`.

### m4 — `read_ledger_file`'s missing-`attempt` case is untested, and the M3 defect reappears there unnoticed
`aggregator/custom_state.py:2014-2022`

Mutating `payload.get("attempt")` to `payload.get("attempt", 1)` — literally
attempt-1's M3, in the other reader — survives both suites. The shipped code is
correct (a missing attempt is not an int, so the line is `skipped_malformed`),
and the module docstring holds this reader up as "the opposite and correct
thing" (`:2018-2020`), but nothing holds it in place. The test gate flagged the
same gap.
**Fix:** a ledger line with no `attempt` ⇒ zero observations and
`counters["skipped_malformed"] == 1`.

---

## NITS

- **n1** — `head_write`'s guard is R-52's contract text (`{seq:{$lt:newSeq}}`),
  but weakening it to `$lte` (`:1391`) passes both suites. Behaviourally benign
  (an equal-seq rewrite is the same event) yet the literal is part of the item;
  pin it: `check(write.require == {"seq": {"$lt": seq}}, …)`.
- **n2** — `CustomStateObservation.from_record:787` calls `refs.ref_id(ref)`
  outside any `try`, so a malformed `ref` on a WAL record escapes as
  `refs.RefError` rather than the `CustomStateError` every other door in this
  module raises (`mirror.Mapper` converts only the latter). One `try/except`.
- **n3** — `iter_custom_state_observations(path=…)` rebuilds the whole
  `slot_index` (walking every task folder's ledger) for **each** control file it
  is asked about (`:2143`). Documented as the rebuild/backfill seam and off the
  liveness path, so GD-30 is not violated, but a memoised index per call site
  would make a `--backfill` over many files O(n) instead of O(n·ledgers).

---

## Checklist disposition

| item | verdict |
|---|---|
| GD-21 lazy pymongo, only mongo_store/mirror | PASS |
| GD-22 Mongo off the liveness path | PASS |
| GD-24 string `_id`s via ref_key only, no subdocument `_id`/match | PASS |
| GD-25 `$max/$addToSet/$min/$setOnInsert`, no `$inc`, no bare `$set` on accumulables, shuffled/reversed identical | **FAIL** (M1, M2) |
| GD-26 no delete verbs / `$unset` / TTL; tombstones only | PASS |
| GD-27 loopback+auth recipe, no credential anywhere, scoped drop | PASS |
| GD-28 `{asserted,touch}` pinned three ways, no `harness` path | PASS (m2 is a test gap, not a code path) |
| GD-29 no client held; dup-key tolerated and counted | PASS |
| GD-30 bounded / O(delta) | PASS (n3 is the rebuild seam) |
| GD-15 one file one owner; no out-of-scope edits | PASS |
| R-52 clauses | PASS (head still driverless — deviation-recorded, accepted) |
| R-53 clauses | PASS except the slot document's order-independence (M1/M2) |
| SD-1 / SD-8 / SD-11 | PASS |
| tests assert real behaviour, skip cleanly without mongod | PASS with M1, m1–m4 gaps |
| docs match implemented behaviour | PASS |
