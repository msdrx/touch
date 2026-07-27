# sp-custom-state — adversarial critique, attempt 4

**Verdict: REJECTED** (2 major, 2 minor, 4 nits; 0 blockers).
Depth: **in-scope** — both majors are fixable inside `aggregator/custom_state.py`
plus its two owned suites (one of them by adding the deviation handoff this
sub-plan has already used three times), in one more gated attempt.
critical_defect: **false**.

Reviewed (untracked-new tree ⇒ full file content):
`aggregator/custom_state.py` (2537 lines), `tests/test_custom_state.py` (1347),
`tests/test_slots.py` (1038),
`findings/sp-custom-state-head-order-deviation.md` (56), plus the granted
`.claude/skills/touch-orchestrate/SKILL.md` :52-56 slice (`git diff` confirms
+13/−1, confined to the ledger-line bullet; nothing else in that file moved).

Against: `touch-mongo-live-subplans.md` §`sp-11 — custom-state`, amendment
R-52/R-53 + GD-21…GD-30, base plan GD-1…GD-20, SD-1/SD-8/SD-9/SD-11, and
`sp-agents-reducer-storage-deviation.md` §2 (sp-10's explicit handoff to this
sub-plan).

Both owned suites reproduce green here (`tests/test_custom_state.py` rc 0,
`tests/test_slots.py` rc 0, one clean `TOUCH_MONGO_URI`-unset skip each).
Nothing committed; HEAD unchanged at `579446e`. `md5sum` re-verified identical
to the pre-review copy after every mutation below.

## Attempt-3 dispositions — verified by mutation, not by reading

| id | claim | mutation applied | result |
|---|---|---|---|
| M1 (cross-stream head order) | closed by `HEAD_ORDER_FIELD` | `head_order` → `seq` alone; head guard → `{"seq":{"$lt":n}}` | **closed** — `test_two_streams_that_share_a_seq_still_leave_one_head:210` builds three real streams (two control files + the WAL) on one `(refId, stateKey)` at `seq` 1 through the module's own sources, asserts ONE fingerprint over 8 arrangements *and* after `rebuild_heads`, and proves the naive guard would have accepted both |
| m1 (`author` on the read door) | closed by `validate_author` | — | **closed**, `:624-648`, asserted at `:373` by call and by AST walk |
| m2 (resolved branch `sessionKeySource`) | closed | — | **closed**, both extra branches asserted |
| m3 (`conflictWith` read) | closed | `resolution_of` clause → `len(conflictAgentIds or ()) > 1` (the pre-fix form) | **caught** (`test_slots.py` rc 1) |
| n1 (`SESSION_PATH_PARENTS` widening) | closed | tuple widened with `"state"` | **caught** (`test_custom_state.py` rc 1) |
| n2 (`_by_agent` registration) | closed | — | **closed**, `:2057-2065` registers on `doc["agentId"] == agent_id`, guard or no guard |

Also re-confirmed directly: no `pymongo` token in the module (GD-21 — it imports
and every test runs on bare stdlib); no `deleteOne`/`deleteMany`/`drop(`/
`$unset`/`expireAfterSeconds` (GD-26); every `_id` is a `refs` product
(`custom_state_event_key` / `custom_state_key` / `slot_key`); no credential
literal and no `TOUCH_MONGO_URI` read in any of the three files (GD-27);
`MIRROR_SOURCES` is the rebuild/backfill seam only, so the whole-file
`_read_lines` stays off GD-30's O(delta) tick; the SKILL.md slice matches R-53's
amendment text exactly.

---

## MAJOR

### M1 — every `topology` head this module writes is unreadable by the ONLY consumer SD-9 names
`aggregator/custom_state.py:1634` (`ms.prepare_document("custom_state", …)` in
`head_write`), against `:94-105` and `:427-430` (the `REF_KINDS_BY_KIND`
widening whose stated purpose this defeats); consumer:
`aggregator/agents.py:1950-1962` (`_topology_from`), `:1965-1987`
(`topology_index`).

`custom_state`'s spec declares `raw_paths=("data.custom",)`
(`mongo_store.py:452`), and `prepare_document` wraps **declared paths
unconditionally** ("so a field's stored shape does not depend on whether *this*
instance happened to contain a dotted key", `mongo_store.py:676-678`). So the
head `head_write` produces stores:

```
data.custom = {"_raw": "{\"stages\":[…],\"maxAttempts\":4}",
               "_rawEncoding": "json", "_rawKeys": 2}
```

`agents._topology_from` reads `doc["data"]["custom"].get("stages")` /
`.get("maxAttempts")` directly — no `unwrap_raw`. Reproduced end to end through
this module's own `replay`, no test helpers:

```
stored data.custom: {"_raw": "{\"stages\": …, \"maxAttempts\": 4}", "_rawEncoding": "json", "_rawKeys": 2}
topology_index:     {'wf_1a3ffcdd-c60': Topology(ref_id='wf_1a3ffcdd-c60',
                                                 max_attempts=None, stages=(), stage_attempts=None)}
denominator:        None
attempt_label(2):   'attempt 2'          # SD-9's ABSENT-topology arm
```

The ref *key* half of sp-10's handoff is satisfied (`refId = refs.run_key(runId)`,
which is exactly why `:427-430` widens `REF_KINDS` for this one kind), and the
module docstring justifies that widening with: "Refusing a `runs` refId for
`topology` would leave every run silently on the 'absent topology' arm forever,
which is the failure that handoff exists to prevent" (`:98-105`). As shipped the
run lands on that arm anyway — the key joins, the payload is empty — so the
stated reason for the one documented widening is **not delivered**, and the
failure mode is the silent one D13 forbids: no counter moves,
`topology_missing` stays 0, and the UI shows "attempt 2" forever instead of
"attempt 2 of 4".

Why neither side caught it: `tests/test_reducer.py:564` builds its topology head
**by hand** ("GD-24's shape, written by hand") with a plain, unwrapped
`data.custom`, so sp-10 asserted a shape no producer emits; and
`tests/test_custom_state.py` never hands a `topology` head to
`agents.topology_index`, even though the module docstring names
`agents.topology_index` as the contract. This module already knows the wrapping
happens — `CustomStateObservation.from_document:1039-1040` calls
`ms.is_raw_wrapper`/`ms.unwrap_raw` on its *own* read door — it simply never
checked the one foreign reader it widened the grammar for.

**Fix (inside ownership).** Two lines of work, both in this sub-plan's files:

1. Add `tests/test_custom_state.py::test_a_topology_head_is_readable_by_the_reducer`
   — build the head through `head_write`/`replay`, call
   `agents.topology_index(state)` and `agents.attempt_label(2, index[run_key])`,
   and assert the denominator actually appears (`"attempt 2 of 4"`). That single
   assertion is the whole defect.
2. Record the one-line fix as a deviation handoff, the way this sub-plan already
   records three others: `agents._topology_from` must unwrap —
   `custom = ms.unwrap_raw(custom) if ms.is_raw_wrapper(custom) else custom` —
   in `findings/sp-custom-state-topology-raw-deviation.md`, naming sp-10's file
   as the owner and quoting `sp-agents-reducer-storage-deviation.md` §2 as the
   contract it completes. Do **not** edit `agents.py` here (GD-15).

If instead the intended answer is that a topology head must store an unwrapped
payload, that is a change to `mongo_store.COLLECTIONS["custom_state"].raw_paths`
— sp-05's file, and a plan question about GD-24's raw-wrapping rule, not an
implementation choice to make silently. Either way the round-trip assertion in
(1) is the thing that must land here.

### M2 — `rebuild_heads` raises on a document this module itself stores, and the head skips `guard_oversize`
`aggregator/custom_state.py:1449-1450` (`map_custom_state` stubs the event) vs
`:1610-1639` (`head_write` does not stub anything), reached from `:1909-1925`
(`rebuild_heads`) and `:1890-1906` (`replay`).

`map_custom_state` runs `ms.guard_oversize("custom_state_events", prepared, …)`;
`head_write` does not. Above `mongo_store.OVERSIZE_LIMIT` (8 MB) the two halves
of one event diverge, and the divergence is not cosmetic — `guard_oversize`
keeps only `("_id","provenance","ts","tsRaw","gen") + spec.required`, and
`custom_state_events.required` is `("kind","seq","provenance")`, so the stub
**loses `refId`, `stateKey` and `stream`**. Reproduced:

```
event doc keys : ['_id', 'bytes', 'kind', 'oversize', 'provenance', 'seq']
head size      : 9 437 527 bytes,  oversize marker: None      # never stubbed
rebuild_heads(state)
  -> aggregator.custom_state.RefRejected: a custom-state write needs a ref or a refId
     (custom_state.py:714, via head_write:1613)
```

Three separate things are wrong there, all in owned code:

* **`rebuild_heads` crashes.** R-52's own acceptance clause is "drop
  `custom_state`, rebuild, document-for-document equal", and the docstring calls
  it "the recovery procedure the derived head exists to make possible"
  (`:1910-1914`). A single stored document the module itself wrote aborts the
  whole rebuild with an uncaught exception — and `rebuild_heads` has no error
  containment at all, so the same happens for any stored event missing `stream`
  (`head_order` → `CustomStateError`). A recovery path that dies on its own
  input is worse than one that counts and skips.
* **The head is never stubbed.** R-44's rule is that an oversize mirror document
  is *marked*, never carried whole; the head carries 9.4 MB where its event
  carries a 6-key stub. `custom_state` and `custom_state_events` then disagree
  about the same event, which is precisely what the "wipe + WAL replay
  reproduces both collections exactly" claim (`:1891-1899`) denies can happen.
* **"Nothing else writes the head, so nothing else can make this untrue"**
  (`:1913-1914`) is false as written — the head is made untrue by
  `map_custom_state`, one function above.

Reachability: `store.MAX_RECORD_BYTES` is 1 MB, so the `Writer`/WAL arm cannot
get there. The **foreign** arms can: `_read_lines` reads whole lines with no
cap, `read_control_file` stores the entire parsed line as `custom`
(`:2426`), and touch-orchestrate's ack shape carries a `result` field. It is
not the common case, but it is the case this module explicitly wired
`guard_oversize` up for on one side and forgot on the other.

**Fix (inside ownership).** Make the two halves agree and make the rebuild
survivable: run `ms.guard_oversize("custom_state", prepared, source_path=…)` in
`head_write` on the same payload and threshold; and in `rebuild_heads`, skip a
document `CustomStateObservation.from_document` + `head_write` refuse, counting
it (`{"rebuilt": n, "skipped": n}`) rather than propagating — a stub has no
`refId` by construction and there is no head it could belong to. Assert both: an
over-limit observation ⇒ event stub *and* head stub, and `rebuild_heads` over a
log containing one stub returns a count instead of raising.

---

## MINOR

### m1 — the bind paths still read the raw `resolution`, so attempt-3's m3 is only half closed
`aggregator/custom_state.py:2043-2071` (`SlotTable.bind`), `:2148`
(`bind_slot`'s `current.get("resolution") == "conflict"`), against `:1931-1965`
(`resolution_of`, which attempt 4 correctly taught to read the evidence).

`resolution_of` now answers `conflict` from `conflictAgentIds`/`conflictWith`;
the two *writers* still branch on the stored word. On the crash-between-writes
shape m3 named (evidence landed, guarded state did not), a **third** agentId
binds cleanly:

```
stored resolution: pending  conflictAgentIds: ['a2fc…'] conflictWith: ['slot:otherkey']
resolution_of      -> conflict
t.bind(key, 'b1de…', by='marker')  -> BindResult('bound', acquired=True)
after bind: agentId='b1de…'  resolution='bound'  rank=2
resolution_of      -> conflict
```

The document now stores `resolution: "bound"` with an `agentId` while read-time
says `conflict` — the mirror image of the state `bind_slot`'s own docstring
calls "the one shape D13 forbids" (`:2129-2133`), and exactly what
`conflict_write` says must never happen ("Deliberately does **not** write
`agentId`: … a slot with two claims has no single answer to 'which agent is
this'", `:1779-1782`). Worse, `_by_agent[b1de…] = key` is then registered
(`:2065`), so the in-memory unique index now believes a contested slot holds
that agentId and the *next* legitimate bind of it elsewhere is answered with a
fabricated conflict.

**Fix:** branch both writers on `resolution_of(current)` rather than on
`current.get("resolution")` (`SlotTable.bind` before the `current`/`holder`
comparison, `bind_slot` at `:2148`), and assert the shape directly: seed a slot
with `conflict_evidence_op` alone, bind a different agentId, and check the call
returns `conflict` with no `agentId` written and `_by_agent` untouched.

### m2 — the WAL arm of `iter_custom_state_observations` is invisible to the counters
`aggregator/custom_state.py:2483-2518` — `read_control_file(…, counters=counters)`
is threaded, `_wal_observations(store)` takes no `counters` at all and swallows
`OSError` into `[]` (`:2512-2518`).

`new_counters`' docstring is explicit about why the counters exist: "an operator
who cannot tell 'nothing happened yet' from 'everything I wrote was rejected' is
looking at the quiet drop GD-26 and D13 both forbid" (`:1203-1209`). With a
readable WAL and no control paths configured — today's shipped configuration —
the counter dict comes back all zeros while records were in fact ingested, and
an **unreadable** WAL (a `chmod`'d `.touch/custom-state.jsonl`, the exact case
`_read_lines` counts as `unreadable` for foreign files) is indistinguishable
from an empty one. This is the one dataset a rebuild from `~/.claude` cannot
reconstruct; silence is the wrong answer for it specifically.

**Fix:** give `_wal_observations` the `counters` argument, bump `read`/`parsed`
per record and `unreadable` on the `OSError`, and assert that a WAL with three
records reports `parsed == 3` and that an unopenable WAL reports
`unreadable == 1`.

---

## NITS

- **n1** — `resolution_of:1954-1955`'s `or doc.get("conflictWith")` clause is
  redundant: every path that writes `conflictWith` (`_conflict_ids:1764-1772`)
  writes `conflictAgentIds` in the same operator, so the preceding clause
  already fires. The test gate observed it can be deleted with both suites
  green. Either drop it, or add the shape that needs it (a document carrying
  `conflictWith` alone) so the clause is held by an assertion rather than by a
  comment.
- **n2** — `read_control_file:2425` builds the `stateKey` as
  `f"{kind}:{payload.get('action') or payload.get('ack')}"` with no type check
  on the verb, so `{"action": {"a": 1}, "name": "x"}` produces the stateKey
  `control_intent:{'a': 1}` — a Python `repr` inside a head `_id`.
  `refs.custom_state_key` escapes it safely, so nothing breaks; it is still an
  un-normalised foreign value in an identity. Require `isinstance(verb, str)`
  and count a non-string verb as `skipped_malformed`, beside the `attempt` type
  check two lines up that already does exactly this.
- **n3** — `head_order:777-801` widens past `PADDED_INTS["seq"]` (12) rather
  than truncating, which the docstring acknowledges — but a widened value sorts
  *below* a padded one (`"1000000000000|s" < "999999999999|s"`), so order
  inverts across the width boundary rather than merely degrading. Harmless at
  10^12 events per stream; worth one sentence saying "inverts", not "holds
  within a width class".
- **n4** — `_SLOT_INDEX_MEMO:2438` is module-global and its two fields are
  written non-atomically (`:2477-2479`). If a backfill ever runs off the mirror's
  worker thread while a rebuild runs on the main one, a reader can observe the
  new signature with the old index and be served a stale hop. One tuple
  assignment (`_SLOT_INDEX_MEMO["entry"] = (signature, index)`) removes the
  window.

---

## Checklist disposition

| item | verdict |
|---|---|
| GD-21 pymongo lazy, only `mongo_store`/`mirror`; module imports on bare stdlib | PASS |
| GD-22 Mongo off the liveness path; live view works with Mongo down | PASS (sources are the rebuild seam; every owned test runs with no DB) |
| GD-24 string `_id`s via `ref_key` only; no subdocument `_id`/equality key | PASS |
| GD-25 `$max/$addToSet/$min/$setOnInsert`; no `$inc`; shuffled/reversed identical | PASS for the in-memory corpora (M1 of attempt 3 genuinely closed); **the head/event pair diverges above 8 MB** (M2) |
| GD-26 no delete verbs / `$unset` / TTL; tombstones only | PASS |
| GD-27 loopback+auth recipe, no credential in repo/events/health/API/prompts | PASS |
| GD-28 `{asserted,touch}` pinned three ways; no `harness` code path; `author` literal on both doors | PASS |
| GD-29 no Mongo client held; dup-key tolerated and counted via `claim_op` | PASS |
| GD-30 bounded / O(delta) on the tick | PASS |
| GD-15 one file one owner; SKILL.md slice only; three deviations recorded | PASS (a fourth handoff is missing — M1) |
| SD-1 registry + unique kinds / SD-8 `TOUCH_CONTROL_PATHS` + `pathSource`, no restated path / SD-11 | PASS |
| **SD-9 topology consumed from `custom_state` kind `topology`** | **FAIL** (M1 — the payload the reducer reads is `_raw`-wrapped) |
| R-52 (3 out-of-order ⇒ highest, unknown refId rejected, wipe+replay, drop+rebuild equal, 16 KB 413, tombstones, one installation-wide pair, no `provenance:"harness"`) | PASS except **drop+rebuild raises on a stored stub** (M2) |
| R-53 (pending/bound/orphaned/conflict, `pendingSince`, both ids on conflict, never raises, ledger amendment, `sessionKeySource:"path"`) | PASS, with m1's storage/read-time disagreement |
| tests assert real behaviour; skip cleanly without mongod | PASS on the mutations tried; blind to M1 (no reducer round-trip), M2 (no oversize corpus) and m2 |
| docs match implemented behaviour | **FAIL** — `:98-105` ("would leave every run on the absent-topology arm forever" — it does), `:1891-1899` ("byte-identical"), `:1913-1914` ("nothing else can make this untrue") |
