# sp-custom-state — deviation / handoff: the `slots` spec declares no `set_fields`

**Status: a real GD-25 breach, one line wide, in a file this sub-plan may not
edit.** Raised as M1 in `sp-custom-state-critique-attempt-2.md`; this file is
the handoff that critique asked for, in the same shape the head-driver
deviation (`sp-custom-state-head-driver-deviation.md`) took for M4. The same
paragraph is in `aggregator/custom_state.py`'s module docstring, so the code and
the record cannot drift apart.

## The defect, stated once

`mongo_store.fingerprint` — GD-25's acceptance oracle, and the one R-44/sp-14
runs over every collection — sorts an array **only** when the owning
`CollectionSpec` names it in `set_fields`; every other array keeps its order, so
a genuine ordering regression still shows (`mongo_store.py:1291-1303`, and the
`CollectionSpec` docstring says exactly why).

`aggregator/custom_state.py` builds four `$addToSet` sets on `slots`:

| field | written by |
|---|---|
| `agentIds` | `map_slot` — an observation's agentId, as **evidence** (never the bind) |
| `evidence` | `map_slot` — the bind channel the line came through |
| `conflictAgentIds` | `conflict_write` / `conflict_evidence_op` |
| `conflictWith` | `conflict_write` / `conflict_evidence_op` |

`mongo_store.COLLECTIONS["slots"]` declares `set_fields=()` — the only mirrored
spec that has `$addToSet` arrays and declares none (`sessions`, `agents`,
`run_nodes`, `runs` all declare theirs). So the arrays are compared in **arrival
order**, and two orders of the same two observations fingerprint differently:

```python
o1 = SlotObservation(session_key="622-10028", root="auth", name="i1", attempt=1,
                     agent_id="a"*17, bound_by="ledger")
o2 = SlotObservation(session_key="622-10028", root="auth", name="i1", attempt=1,
                     agent_id="b"*17, bound_by="marker")
ms.fingerprint(build([o1, o2])) != ms.fingerprint(build([o2, o1]))
#  1efbefc2b17b344f…      vs      37a7678f97f768b9…
#  agentIds ['aaa…','bbb…']       agentIds ['bbb…','aaa…']
```

Declaring the four names makes both passes `1efbefc2b17b344f…`. Nothing else
about the documents differs — `tests/test_slots.py` asserts that separately
(every non-set field is byte-identical across six replay orders, and every set
holds the same members), so the divergence is **entirely** the missing
declaration.

## Who must close it, and with what

**sp-05 (`aggregator/refs.py` + `aggregator/mongo_store.py`)** — this sub-plan
owns `aggregator/custom_state.py`, `tests/test_custom_state.py`,
`tests/test_slots.py` and the `touch-orchestrate/SKILL.md` ledger slice, and
nothing else (GD-15, one file one owner).

The paste, on the `"slots"` row of `mongo_store.COLLECTIONS`:

```python
    "slots": CollectionSpec(
        "slots", "slot",
        …
        set_fields=("agentIds", "conflictAgentIds", "conflictWith", "evidence"),
        accumulable=("agentIds", "conflictAgentIds", "conflictWith", "evidence",
                     "taskId", "runNode", "pendingSince", "firstSeenTs", "lastSeenTs"),
        …
    ),
```

`set_fields` is the required half. `accumulable` is the optional second half —
it fences `$set` off the fields this module reaches only through
`$max`/`$min`/`$addToSet`, which is what would have caught the *other* GD-25
defect of this attempt (M2: `taskId`/`runNode` written by `$max` in the mapper
and by a bare `$set` in the bind, fixed here by giving the advance a `$max`
leg). Do **not** add `resolution`, `resolutionRank`, `agentId`, `boundBy`,
`orphanReason` or `resolvedTs` to `accumulable`: those are the state machine's
conclusions, deliberately `$set` behind the `{"resolutionRank": {"$lt": rank}}`
guard, and fencing them would refuse every legitimate transition.

The tuple is also `custom_state.SLOT_SET_FIELDS`, exported precisely so the
paste can be `set_fields=custom_state.SLOT_SET_FIELDS` if sp-05 prefers the
import to the literal — but `mongo_store.py` importing `custom_state.py` would
invert the dependency, so the literal is the recommended form and the literal is
what the test compares against.

## Impact until it lands

1. **R-44 / sp-14's acceptance fingerprint is not yet safe over `slots`.** Today
   the breach is latent — `read_ledger_file` never sets `agent_id`/`bound_by`
   and nothing calls `slot_from_labels`, so the arrays are empty in every live
   path — but it becomes real the moment the head/bind driver of
   `sp-custom-state-head-driver-deviation.md` lands, which is the *same* handoff
   (sp-12 by elimination). Whoever wires `bind_slot(…, by="marker")` should
   land this line first.
2. **A Mongo wipe + replay still reproduces the same documents**, member for
   member; only the stored element ORDER of the four arrays may differ between
   two ingest orders, and no reader in this codebase depends on that order.
3. No other collection is affected: `custom_state_events` is insert-only and
   `custom_state` has no arrays.

## What this sub-plan did instead

- `SLOT_SET_FIELDS` is declared in `custom_state.py` and
  `tests/test_slots.py::test_the_state_machine_is_monotone_under_any_replay_order`
  **derives** the `$addToSet` keys the module actually emits and asserts the two
  agree — so the module cannot grow a fifth set without the assertion naming it,
  and this note cannot go stale silently.
- The same test now shuffles a corpus that really populates the sets (two
  distinct agentIds, three distinct channels — the previous corpus set neither,
  which is why the assertion could not see the defect), asserts the documents
  differ in nothing but those sets' element order, and installs the declaration
  on the spec **for the duration of the fingerprint assertion only**, printing a
  `deviation:` line when it has to. The day sp-05 pastes the tuple the patch
  becomes a no-op and the print disappears; the test does not change.
- The test does **not** sort the arrays itself and call GD-25 held.
