# sp-custom-state — deviation / handoff: the head and the bind have no driver

**Status: deliberate, in-scope, and named here so it is not discovered by
whoever first queries an empty `custom_state`.** Raised as M4 in
`sp-custom-state-critique-attempt-1.md`; this file is the handoff that critique
asked for. The same paragraph is in `aggregator/custom_state.py`'s module
docstring, so the code and the record cannot drift apart.

## What runs today

`mirror.py` discovers exactly two things from an entity module: `MIRROR_MAPPERS`
and `MIRROR_SOURCES`. sp-11 registers both (`customState`, `slot`), so on every
mirror tick:

- `custom_state_events` is written — one insert-only document per WAL line, per
  ledger line and per addressable control line;
- `slots` is written — the **evidence** half: identity, `$max`/`$min` scalars,
  `agentIds`/`evidence` sets, `resolution:"pending"` on insert.

## What does not run

Nothing in `aggregator/` calls `head_write`, `bind_slot`, `SlotTable.bind`,
`SlotTable.sweep`, `orphan_write` or `rebuild_heads`. `custom_state.py` cannot
call them itself: they need a database handle, and GD-21 permits one only in
`mongo_store.py` and `mirror.py`. R-52's head needs
`require={"seq":{"$lt":newSeq}}` and R-53's bind needs the tolerated-duplicate
path, and neither shape fits `mirror.py`'s `(collection, _id, update)` triple
queue — which is why they are pure `GuardedWrite` descriptions rather than
mappers (see the module docstring, "Why the head is a guarded write").

Consequences, stated plainly:

1. **`custom_state` (the derived head) is never written.** Its documents exist
   only in tests and in `replay`/`rebuild_heads`.
2. **Every slot stays `resolution:"pending"`.** No bind, no orphan sweep, no
   conflict document is ever materialized in a live database.
3. **`agents.topology_index` therefore takes SD-9's absent-topology arm
   permanently** — "attempt N", no denominator, no next-stage arrow — because it
   reads `custom_state` heads of kind `topology`, and the head is empty.
4. The read API and the UI must **not** present the head or any non-`pending`
   resolution as fact until (5) lands.

## Who must close it, and with what

**sp-12 (server/API + the mirror tick's consumers), by elimination** — sp-06
(`mirror.py`) is closed, and sp-11 owns no file that may hold a client. sp-14 is
the fallback owner if sp-12's scope excludes the tick.

The work is small and entirely additive; all four entry points are already pure
and unit-tested:

```python
# after each map_custom_state batch on the tick / rebuild:
write = custom_state.head_write(obs)
backend.guarded_update(write.collection, write.key, write.update, require=write.require)

# on each bind evidence (marker / ledger line / Agent-tool description):
custom_state.bind_slot(db, slot_key, agent_id, by="marker", counters=counters)

# on the tick after a run reaches a terminal, and on the TTL sweep:
for write in table.sweep(now=now, terminal=terminal_slot_keys):
    backend.guarded_update(*write.as_call()[0], **write.as_call()[1])
```

Notes for whoever takes it:

- `bind_slot` never raises on a collision: the id claim is isolated in
  `claim_op` and driven through `bulk_upsert`, whose `tolerated_dups` is
  GD-29's exposed count. Do not route the claim through `guarded_update` —
  it converts `DuplicateKeyError` into `MongoUnavailable` and would trip
  GD-30's breaker on healthy traffic.
- `head_write` must be driven **per observation**, in any order: the
  `{"seq":{"$lt":newSeq}}` guard is what makes order irrelevant, and
  `apply_guarded` is the in-memory twin to test against with no database.
- `rebuild_heads(state)` is the recovery procedure (drop `custom_state`,
  replay the log); wire it to whatever `--rebuild` entry point sp-12 owns.

## Control-file arm — related, but closed

The same critique's M1/M2/M3 (the control arm read zero lines of the only
format that exists, dropped lines silently, and defaulted `attempt` to 1) are
**fixed inside sp-11**: `SlotIndex` performs the name→slot hop, both foreign
readers carry `new_counters()`, and `attempt` is resolved to the highest
observed attempt with `attemptSource:"resolved"` or the line is skipped and
counted. What remains out of scope here is R-20 itself (relocating the control
file and making it aggregator-owned) — SD-8 defers it, and `TOUCH_CONTROL_PATHS`
is the configured seam until it lands.
