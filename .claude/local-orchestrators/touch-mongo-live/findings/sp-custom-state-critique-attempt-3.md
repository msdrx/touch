# sp-custom-state — adversarial critique, attempt 3

**Verdict: REJECTED** (1 major, 3 minor, 2 nits; 0 blockers).
Depth: **in-scope** — the major is fixable inside `aggregator/custom_state.py`
plus its two owned suites, in one more gated attempt.
critical_defect: **false**.

Reviewed (untracked-new tree, so full file content):
`aggregator/custom_state.py` (2348 lines), `tests/test_custom_state.py` (1047),
`tests/test_slots.py` (957),
`findings/sp-custom-state-slots-set-fields-deviation.md` (113), plus the granted
`touch-orchestrate/SKILL.md` :52-56 slice (+13/−1, `git diff` confirms it is
confined to the ledger-line bullet; nothing else in that file moved).

Against: `touch-mongo-live-subplans.md` §`sp-11 — custom-state`, amendment
R-52/R-53 + GD-21…GD-30, base plan GD-1…GD-20, SD-1/SD-8/SD-11.

Both owned suites are green here (`python3 tests/test_custom_state.py` rc 0,
`python3 tests/test_slots.py` rc 0, one clean `TOUCH_MONGO_URI`-unset skip each).
Adjacent suites re-run to check for collateral from the two new
`MIRROR_MAPPERS` kinds: `test_stdlib_only`, `test_mongo_store`, `test_refs`,
`test_reducer` all rc 0. Nothing committed; HEAD unchanged.

## Attempt-2 dispositions — verified by mutation, not by reading

Each row below was checked by mutating the shipped source and re-running both
owned suites (source restored afterwards; `md5sum` re-verified against the
pre-mutation copy).

| id | claim | mutation applied | result |
|---|---|---|---|
| M1 | `slots` sets declared + shuffle corpus really populates them | — | **closed**: `SLOT_SET_FIELDS:407` is derived-and-asserted (`test_slots.py:340-349`), the shuffle corpus now carries two `agent_id`s and three `bound_by` channels (`:330-336`) and asserts the sets are non-empty before comparing (`:363-366`); the `mongo_store` half is handed off in `sp-custom-state-slots-set-fields-deviation.md`, exactly the shape attempt 2 asked for |
| M2 | `taskId`/`runNode` no longer `$set` beside `$max` | `ADVANCE_MAX_FIELDS = ()` | **caught** (`test_slots.py` rc 1) |
| m1 | `sessionKeySource` settled by rank, not alphabet | rank map reversed | **caught** |
| m2 | ledger event provenance pinned `asserted` | `provenance="touch"` | **caught** |
| m3 | control reader's `sessionKeySource` asserted per branch | `key_source = "slots"` in the resolved branch | **SURVIVED** — see m2 below |
| m4 | ledger line with no `attempt` is dropped, not defaulted | `payload.get("attempt", 1)` | **caught** |
| n1 | head guard is the literal `$lt` | `$lt` → `$lte` | **caught** |
| n2 | `from_record` raises `CustomStateError`, not `RefError` | — | present, `:882-886` |
| n3 | slot-index memo | — | present, `_SLOT_INDEX_MEMO:2249`, keyed on the ledger set's `(path, st_mtime_ns, st_size)` |

Also re-confirmed directly: no `pymongo` token anywhere in the module (GD-21);
no `deleteOne`/`deleteMany`/`drop(`/`$unset`/`expireAfter` (GD-26 — the only two
hits for "delete" are prose in comments); every `_id` comes from `refs`
(`custom_state_event_key` / `custom_state_key` / `slot_key`), and
`refs.custom_state_key` percent-escapes `# | : %` in a caller-supplied
`stateKey` (verified on `x#y|z:%`), so a control line cannot forge a head id;
`MIRROR_SOURCES` is the rebuild/backfill seam only — `mirror.py` invokes it once
per rebuild (`mirror.py:2634`), never per tick, so the whole-file `_read_lines`
is off GD-30's O(delta) path; no credential literal in any of the three files;
`$`/dot keys in a foreign control payload are wrapped by
`ms.prepare_document` into `_raw` rather than reaching Mongo (GD-24).

---

## MAJOR

### M1 — the `custom_state` head is order-dependent across streams, and GD-25's oracle says so
`aggregator/custom_state.py:1456-1502` (`head_write`'s `require={"seq": {"$lt":
obs.seq}}`), `:1730-1744` (`replay`), `:1747-1758` (`rebuild_heads`);
test blind spot: `tests/test_custom_state.py:139-156` and `:268-272`.

The head's `_id` is `<refId>#<stateKey>` and its ordering is `seq` alone. `seq`
is **per-stream and positional** — the WAL's own counter for `custom-state`, the
line number for a `control:<scope>` or `ledger:<scope>` file (`:2126`, `:2186`).
The module nevertheless funnels every stream into one head space, so two events
from *different* streams that share `(refId, stateKey, seq)` are ordered by
nothing: `{"seq": {"$lt": 3}}` refuses the second one whichever it is, and the
stored head is whichever arrived first.

The module docstring claims the opposite in two places — "Order-independent by
construction: applying the events of one `(refId, stateKey)` in any order leaves
the head at the highest `seq`" (`:1465-1466`) and "a Mongo wipe followed by this
is byte-identical to the mirror that was lost" (`:1735-1737`).

Reproduced end-to-end through the module's own documented source API (SD-8's
`TOUCH_CONTROL_PATHS` list with two files, one spawn ledger, no test helpers):

```
index entries: [('622-10028', 'auth', 'impl1', 1)]
o1 control:ctl-4e7c324f 1 control_intent:stop slot:622-10028|auth|impl1|001
o2 control:ctl-9e0039ff 1 control_intent:stop slot:622-10028|auth|impl1|001
ms.fingerprint(replay(o1+o2)) == ms.fingerprint(replay(o2+o1))  ->  False
head payload order1: {"action":"stop","name":"impl1","note":"a"}
head payload order2: {"action":"stop","name":"impl1","note":"b"}
counts identical both ways: {'custom_state': 1, 'custom_state_events': 2}
```

Two control files, each with a stop line on line 1 for the same agent name. The
name→slot hop resolves both to the same slot (`SlotIndex` is built from **all**
ledgers, not per file), the `stateKey` is derived from the line's own verb
(`f"{kind}:{payload.get('action')…}"`, `:2236`) so it is the same string, and the
line numbers are both 1. Same head `_id`, same `seq`, two different payloads.
The counts assertion GD-25 pairs with the fingerprint does **not** catch it —
both events are stored — which is precisely the "silent collapse the count
assertion cannot see" case.

The same tie is reachable without a second control file: `Writer.append` accepts
`kind="control_intent"` with any `state_key`, so a WAL record at `seq` 1 and a
control line at line 1 addressing one slot collide identically. And
`rebuild_heads` (`:1756`) iterates `state["custom_state_events"].values()` —
dict insertion order, i.e. arrival order — so R-52's "drop `custom_state`,
rebuild, document-for-document equal" reproduces whatever the tie happened to
be, not a defined answer.

Why the suites are green: every GD-25 order assertion in
`tests/test_custom_state.py` runs on a **single-stream** corpus.
`test_three_out_of_order_writes_leave_the_head_at_the_highest_seq:139` permutes
three records of one `Writer` (`wal_with_three`, all stream `custom-state`,
seq 1/2/3), and `test_a_mongo_wipe_followed_by_a_wal_replay…:268-272` shuffles
four records of one `Writer`. No corpus in either suite contains two streams
with a shared `(refId, stateKey)` — this is the same fixture-blindness attempt 2
flagged as M1 for `slots`, moved one collection over: the operator that can
break the invariant is never exercised by the assertion that names it.

This is not the `slots`/`set_fields` deviation wearing a new hat. That one is a
one-line declaration in a file this sub-plan may not edit, and it is recorded.
This one is entirely inside `custom_state.py`: `head_write` chooses the ordering
key, and `custom_state` is R-52's collection, owned here.

**Fix (all inside ownership).** Give the head a total, stream-aware order
instead of a bare per-stream integer. The cheapest form that keeps `mongo_store`
untouched: `$max` a composite string alongside `seq` — e.g.
`order = f"{obs.seq:012d}|{obs.stream}"` — and guard the payload on
`{"order": {"$lt": order}}` (`$lt` on a string is inside
`mongo_store.GUARD_OPS`, and `_guard_matches`/`apply_guarded` already handle it),
keeping `seq`/`fromSeq` on the document for R-52's literal text and for the
`{seq:{$lt:newSeq}}` semantics *within* a stream. Any deterministic tie-break
will do; what may not stay is "arrival order decides". Then extend the two order
assertions to a corpus that actually contains the tie — at minimum two
`control:` streams and the WAL, all resolving to one `(refId, stateKey)` at
equal `seq` — and assert one fingerprint across shuffled *and* reversed passes,
plus the same after `rebuild_heads`. Do not close this by asserting that the
counts match: the counts already match in the failing case.

If the intended reading of R-52 is instead "one head space per stream", then the
head `_id` has to carry the stream and the docstring paragraph at `:44-62` has to
say so — but that changes R-52's stated `_id` grammar and would be a plan
question, not an implementation choice to make silently.

---

## MINOR

### m1 — `author` is validated on the write door and nowhere on the read door
`aggregator/custom_state.py:1179-1183` (`Writer._append` enforces the literal)
vs `:1291` and `:1480` (`obs.author or AUTHOR`, unvalidated).

Mutating `_event_document`'s `"author": obs.author or AUTHOR` to the constant
`"author": AUTHOR` leaves both suites green — nothing asserts that the field
travels, and nothing asserts that it may not travel a value the module refuses to
*write*. `CustomStateObservation.from_record` reads `data.get("author", AUTHOR)`
(`:896`) straight off a WAL line, so a `custom-state.jsonl` record that was not
produced by `Writer` — and R-52's own wording is "**writers** append to the
`.touch/` WAL", with GD-29 explicitly contemplating agent-side file appends —
lands in `custom_state_events` and in the head carrying, say,
`author: "michael@host"`. CUSTOMSTATE-16's rule is that Touch has no user
identity model and this field is the literal `local`, never a hostname or a
username; the module docstring (`:337-342`) states it as an invariant of the
module, not of one method.

**Fix:** route `_event_document`/`head_write` through the same check the writer
uses (or a `validate_author` helper next to `validate_provenance`, rejecting with
`CustomStateError` so `mirror.Mapper` counts it), and assert both directions: a
WAL line carrying a foreign `author` is refused/normalised, and the refusal is
counted rather than silent.

### m2 — attempt-2's m3 is only half closed: the resolved branch's `sessionKeySource` still passes as a constant
`aggregator/custom_state.py:2232`

Replacing `key_source = "ledger" if stated else ("path" if from_path else "slots")`
with the constant `"slots"` keeps both suites green. The three assertions added
for this (`tests/test_custom_state.py:604`, `:616`, `:625`) do cover
`slots`/`ledger`/`path` — but the `ledger` and `path` cases go through the
**other** branch (`:2212-2219`, lines that state `root` + `attempt`), so only the
`slots` value of the resolved branch is pinned. A control line that states its own
`sessionKey` (or sits under a `sessions/<pid>-<procStart>/` directory) but leaves
`root` or `attempt` to the hop is therefore free to be labelled as an attribution
Touch inferred, when the writer stated it — the exact credit-misassignment
CUSTOMSTATE-10 asks the field to prevent. The shipped code is correct; nothing
holds it there.

**Fix:** two more lines in
`test_a_control_line_in_the_skill_files_own_shape_is_ingested` — a line with
`sessionKey` but no `root` ⇒ `session_key_source == "ledger"`, and a line under
`sessions/622-10028/` with no `root` ⇒ `"path"` — both with `attempt` omitted so
they take the resolved branch.

### m3 — `resolution_of` ignores `conflictWith`, so a half-written conflict reads as `pending`
`aggregator/custom_state.py:1764-1785` (`:1775`), reached from `:1997-2010`
(`_drive_conflict`) and `:1884-1896` (`SlotTable._conflict`).

Both conflict paths write the unguarded evidence first and the guarded state
second — deliberately, and documented. In the **cross-slot** case the evidence is
`conflictAgentIds: [agent_id]` (one element) plus `conflictWith: [holder]`
(`_conflict_ids:1604-1612`). If the guarded write does not land — the process
dies between the two calls, or `guarded_update` reports `acquired: false` —
the document carries conflict evidence, no `agentId` (the claim lost the unique
index), and `resolution: "pending"`. `resolution_of`'s conflict clause tests
`doc["resolution"] == "conflict" or len(conflictAgentIds) > 1`, so it answers
`"pending"`, and `SlotTable.sweep` will later promote that slot to `"orphaned"` —
a slot whose stop went to a real, contested agent rendered as one that went
nowhere. Mutating the threshold to `> 0` leaves both suites green, which shows
nothing asserts what that clause is for either.

**Fix:** make the clause read the evidence it actually has —
`or len(conflictAgentIds or ()) > 1 or bool(conflictWith)` — and assert the
crash-between-writes shape directly: apply `conflict_evidence_op` alone to a
`pending` slot and check `resolution_of` says `conflict` (and that `sweep` then
leaves it alone).

---

## NITS

- **n1** — `SESSION_PATH_PARENTS:441` is asserted against *removal* (attempt-1's
  m4) but not against *widening*: adding `"state"` to the tuple — which would
  make any `<task>/state/2026-07/…` path yield a phantom session — keeps both
  suites green. Pin the tuple itself, the way `SLOT_SET_FIELDS` is pinned.
- **n2** — `SlotTable.bind:1875-1882` only records `_by_agent[agent_id]` when the
  rank guard fires. A `SlotTable` seeded from stored documents (`state` is a
  public field) whose first call is an idempotent re-bind of an already-`bound`
  slot returns without registering the holder, so a later bind of that same
  agentId to a *different* slot misses the in-memory unique-index check that the
  class exists to provide. Register the holder whenever `doc["agentId"] ==
  agent_id`, guard fired or not.

---

## Checklist disposition

| item | verdict |
|---|---|
| GD-21 pymongo lazy, only `mongo_store`/`mirror`; module imports on bare stdlib | PASS |
| GD-22 Mongo off the liveness path | PASS (sources are the rebuild seam, `mirror.py:2634`) |
| GD-24 string `_id`s via `ref_key` only; no subdocument `_id`/equality key | PASS |
| GD-25 `$max/$addToSet/$min/$setOnInsert`; no `$inc`; no bare `$set` on accumulables; shuffled/reversed identical | **FAIL** (M1 — `custom_state`; the `slots` half is closed and its `mongo_store` line is deviation-recorded) |
| GD-26 no delete verbs / `$unset` / TTL; tombstones only | PASS |
| GD-27 no credential in repo/events/health/API/prompts; scoped live-arm drop | PASS |
| GD-28 `{asserted,touch}` pinned three ways; no `harness` code path | PASS on provenance; the sibling honesty field `author` is m1 |
| GD-29 no Mongo client held; dup-key tolerated and counted via `claim_op` | PASS |
| GD-30 bounded / O(delta) on the tick | PASS |
| GD-15 one file one owner; SKILL.md slice only | PASS |
| R-52 clauses (3 out-of-order ⇒ highest seq, unknown refId rejected, wipe+replay, drop+rebuild, 16 KB 413, tombstones, one installation-wide pair, no `provenance:"harness"`) | PASS except order-independence across streams (M1) |
| R-53 clauses (pending/bound/orphaned/conflict, `pendingSince`, both ids on conflict, never raises, ledger amendment, `sessionKeySource:"path"`) | PASS, with m3's half-written-conflict read |
| SD-1 (registry, unique kinds) / SD-8 (`TOUCH_CONTROL_PATHS` + `pathSource`, no restated path) / SD-11 | PASS |
| tests assert real behaviour; skip cleanly without mongod | PASS with the M1 corpus gap and m1/m2/m3/n1 mutation survivors |
| docs match implemented behaviour | **FAIL** on the two docstring claims M1 contradicts (`:1465`, `:1735`) |
