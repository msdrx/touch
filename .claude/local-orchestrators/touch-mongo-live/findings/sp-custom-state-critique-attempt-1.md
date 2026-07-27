# sp-custom-state — adversarial critique, attempt 1

**Verdict: REJECTED** (4 major, 5 minor, 4 nits; 0 blockers).
Depth: **in-scope** — every fix lands inside the four owned files.
critical_defect: **false**.

Reviewed: `aggregator/custom_state.py` (1776 lines, new),
`tests/test_custom_state.py` (664, new), `tests/test_slots.py` (620, new),
`.claude/skills/touch-orchestrate/SKILL.md` (+13/−1).
Against: subplans §`sp-11 — custom-state`, amendment R-52/R-53 + GD-21…GD-30,
base plan GD-1…GD-20, SD-1/SD-8/SD-11.

## What holds up (checked, not assumed)

- **GD-21.** `pymongo` appears nowhere in the module (asserted by source text,
  and by `test_stdlib_only`); every Mongo concern is routed through
  `mongo_store`'s pure helpers. Both suites run green with no driver present.
- **GD-24.** Every `_id` is a string from `refs` (`custom_state_event_key`,
  `custom_state_key`, `slot_key`); no sub-document `_id`, no sub-document
  equality match. `slot:622-10028|auth|auth_impl1|001` verified sessionKey-first
  and round-trip-lossless through `refs.parse_ref_key` including `#|:%` in
  `root`/`name`.
- **GD-25.** `custom_state_events` is `$setOnInsert`-only; `map_slot` emits only
  `$setOnInsert/$min/$max/$addToSet`; `seq` advances by `$max` and `custom_state`
  fences `$set` off it; the head payload rides `require={"seq":{"$lt":n}}`.
  Shuffle/reverse fingerprint equality is asserted for both arms and reproduces.
- **GD-26.** Deletes are tombstone events; no `deleteOne/deleteMany/drop_collection/
  $unset/remove(` token anywhere in the module; no TTL in the `slots` index set.
- **GD-28.** `validate_provenance` is the single door, `harness/derived/unknown`
  refused, and the AST walk over the module's own provenance literals is a real
  structural check (not a call-site check).
- **GD-29.** No client is held; `claim_op` isolates the one index-touching write
  onto `bulk_upsert` so `DuplicateKeyError` returns as `tolerated_dups` instead
  of `MongoUnavailable`. This is a genuinely good call and the comment explaining
  it is accurate.
- **GD-15 / ownership.** `_only_ours` fences the three collections; `store.py`
  untouched and asserted untouched; `git status` shows only
  `.claude/skills/touch-orchestrate/SKILL.md` newly modified; the SKILL.md diff is
  confined to the §2 ledger-line bullet, which is exactly the granted slice.
- Tests are non-tautological in the places that matter (the guard matcher is
  compared against `mirror._matches`; the `agentId`-writer set is derived from the
  AST and pinned to `{claim_op, bind_write}`).

The findings below are all about the **foreign-file reading half** and about
what drives the module in production — not about the algebra, which is sound.

---

## MAJOR

### M1 — The control-intent/ack arm ingests **zero** lines of the only control-file format that exists, silently
`aggregator/custom_state.py:1682-1731` (`read_control_file`), esp. `:1710`

`read_control_file`'s own docstring says:

> The two shapes touch-orchestrate writes: `{"action":"stop","name":…}` and
> `{"ack":"stop","name":…,"taskId":…,"result":…}`.

Line 1710 then rejects both:

```python
if not kind or not name or not root or not key:
    continue
```

Neither documented shape carries `root`, and `.touch/control.jsonl` (the path
`touch-orchestrate/SKILL.md:86` mandates) yields no `sessionKey` from
`session_key_from_path` either — so `key` is `None` as well. Reproduced against a
literal SKILL.md-shaped file:

```
$ cat .touch/control.jsonl
{"action":"stop","name":"auth_impl1"}
{"ack":"stop","name":"auth_impl1","taskId":"t1","result":"stopped","ts":"…"}
$ read_control_file(".touch/control.jsonl", "env")  ->  []
```

SD-8 scopes this arm as "reads a configured path list from `TOUCH_CONTROL_PATHS`,
records `pathSource`, **until R-20 lands**" — i.e. it is supposed to work *now*
against a configured path. As written it cannot: every real line is dropped. The
only lines it accepts are lines nothing in the repo writes, which is also why the
test passes — `test_control_paths_are_configured_and_the_path_is_never_restated`
feeds hand-built lines carrying `root` and `attempt`, a fixture written to the
implementation rather than to the documented input. There is no test at all for
the shape SKILL.md actually specifies.

Worse, the resolution the code needs is already available: SKILL.md:90 says the
orchestrator "resolve[s] the name to its `taskId` **via the spawn ledger**", and
the ledger is what `read_ledger_file` already parses into slots carrying
`root` + `sessionKey` + `attempt`. Name→slot is precisely the hop `slots` exists
to perform (R-53: "the SINGLE place the name↔agentId hop happens"); demanding the
control line restate `root` re-implements the join by fiat and then fails it.

**Fix:** resolve `(name[, attempt]) → slot` against the observed slot set (pass
the slot keys/`SlotTable` in, or take a resolver callable) instead of requiring
`root` on the line; keep the skip for names that resolve to nothing, and count
those (see M2). If the maintainers judge that the join belongs to a later
sub-plan, then the docstring must stop claiming it parses those two shapes and
must say plainly that the arm is inert against today's format — and a handoff has
to name the owner.

### M2 — Both foreign-file readers drop lines silently, while two comments claim they are counted
`aggregator/custom_state.py:1711-1714`, `:126-135` (module docstring),
`:1656-1679` (`read_ledger_file`), `:1696-1720` (`read_control_file`)

`:1713` — "Skipped, and the caller's counters say how many."
`:134` — "the source yields nothing and **says so through its counters**".

Neither reader has a counter. `read_control_file` and `read_ledger_file` return
plain lists; `_read_lines` swallows `OSError` and returns `[]`; malformed JSON,
non-dict payloads, missing `root`/`name`/`sessionKey` and non-int `attempt` all
`continue` with no record anywhere. `Writer` and `SlotTable` both maintain
counters, so the omission is inconsistent within the same file as well as untrue
to the comments.

This is what makes M1 invisible in production: an operator with a real control
file configured sees zero documents, zero errors, zero counters, and no way to
tell "nothing happened yet" from "everything I wrote was rejected" — the quiet
drop GD-26's posture and D13's honesty rule both exist to forbid.

**Fix:** give both readers a `counters` mapping (same shape as `Writer.counters`:
`{"read", "parsed", "skipped_unaddressable", "skipped_malformed", "unreadable"}`),
populate it, return it (or accept one from the caller as `bind_slot` does), and
assert non-zero skip counts in the tests for each skip reason.

### M3 — `attempt` silently defaults to 1, fabricating the component of the address the code says it must never fabricate
`aggregator/custom_state.py:1707` (`attempt = payload.get("attempt", 1)`)

Three lines below, `:1711-1714` states the rule: "a fabricated address is worse
than none (D13)". Defaulting `attempt` to 1 breaks exactly that rule, and it is
not harmless: `touch-orchestrate/SKILL.md:95` — "A stopped slot may be re-run only
as a fresh spawn with `attempt` + 1". So an intent for a slot on its third attempt
is addressed, with no warning, to `slot:<session>|<root>|<name>|001` — a stale,
already-terminal slot. The rendered result is a stop card attached to the wrong
agent, which is a strictly worse outcome than a skipped line. Verified:

```python
read_control_file(<line without "attempt">) -> refId "slot:700-11|auth|auth_impl1|001"
```

Note `read_ledger_file:1670` does the opposite and correct thing for the same
field (non-int `attempt` ⇒ skip). The two readers disagree about the same rule.

**Fix:** require `attempt` on a control line the same way the ledger reader does,
or — better, together with M1 — resolve it from the slot set (latest live attempt
for that name), and if a default is ever kept, record it as
`attemptSource:"default"` on the document so the UI can say the address was
inferred.

### M4 — Nothing drives `head_write` / `bind_slot` / `SlotTable` outside the tests, and no handoff records who must
`aggregator/custom_state.py:44-62`, `:1064-1069`, `:1094-1137`, `:1494-1555`

`mirror.py` discovers exactly two things from an entity module: `MIRROR_MAPPERS`
(triples) and `MIRROR_SOURCES` (files). Grepping the whole `aggregator/` package,
no module references `head_write`, `apply_guarded`, `replay`, `rebuild_heads`,
`bind_slot` or `SlotTable`. So in the running system:

- `custom_state` (the derived head) is **never written** — only
  `custom_state_events` is;
- `slots` documents never leave `resolution:"pending"` — no bind, no orphan
  sweep, no conflict ever materializes.

The consequence is not local. `agents.py:1966` builds `topology_index` from
`custom_state` heads of kind `topology`; with the head permanently empty, SD-9's
"absent topology" arm becomes permanent for every run — which is precisely the
failure the module docstring (`:64-75`) says the `topology` refId widening exists
to prevent. sp-12 also reads these collections.

The module docstring is admirably explicit that the split exists ("two kinds of
work, declared separately and honestly"), but "honestly declared" is not the same
as "assigned". The topology case is handled correctly — it names `agents.topology_index`
and sp-10's deviation record. The head/bind case names nobody, and no
deviation/handoff note exists in `findings/`.

**Fix (documentation-scale, inside owned files):** add a short handoff paragraph
to the module docstring and a deviation note in `findings/` naming which sub-plan
must call `head_write` after each `map_custom_state` on the mirror tick and
`bind_slot` on marker/ledger evidence (sp-12 or sp-14 by elimination — sp-06 is
closed), and state that until then `custom_state` and every non-`pending` slot
state are test-only surfaces. Without that, the gap is invisible to the remaining
sub-plans.

---

## MINOR

### m1 — A control stream's scope is the *parent directory's basename*, so two configured files can collide into insert-only ids
`aggregator/custom_state.py:1691-1693`

`scope = session_key or session_key_from_path(path) or basename(dirname(path))`.
Two configured control files whose parent directories share a name — e.g.
`<taskA>/state/control.jsonl` and `<taskB>/state/control.jsonl`, both scoping to
`state` — produce the same stream id, so line 1 of each maps to the same
`custom_state_events` `_id`. Because that collection is `$setOnInsert`-only, the
second file's line is swallowed as a *tolerated duplicate*: silent collapse, the
exact failure mode MONGOSCHEMA-1/GD-25's count assertion was written to catch.
(GD-27's per-repo database name limits the blast radius to one repo; it does not
remove it.)

**Fix:** derive the scope from the full realpath — e.g.
`f"{basename}-{sha1(realpath)[:8]}"` — or reject a scope that is already claimed
by a different path within one run.

### m2 — The 16 KB cap's *value* is unpinned; widening it keeps every test green
`aggregator/custom_state.py:288`, `tests/test_custom_state.py:320`

The test builds its payload from `ANNOTATION_LIMIT` itself, so changing the
constant to 16 MB is a passing mutation. CUSTOMSTATE-16's number is part of the
contract, not an implementation detail. **Fix:** one line —
`check(ANNOTATION_LIMIT == 16 * 1024, …)`, the way `PENDING_TTL_SECONDS == 300`
and `len(KINDS) == 8` are already pinned.

### m3 — `ref` and `ref_id` may contradict each other and both get stored
`aggregator/custom_state.py:824-827`, `:948-952`

When a caller supplies both, `ref_id` is validated in isolation and `ref` is
stored verbatim; nothing checks `refs.ref_id(ref) == ref_id`. GD-24's
"flat + denormalized" pairing then admits a document whose `ref{}` and `refId`
point at different entities, which is undetectable downstream.
**Fix:** when both are given, require agreement and raise `RefRejected` otherwise
(one comparison); assert it.

### m4 — `session_key_from_path` accepts any `<int>-<int>` path component
`aggregator/custom_state.py:350`, `:599-614`

`^[1-9]\d*-\d+$` matches `2026-07`, `1-2`, etc. A pre-amendment ledger under a
date- or version-named folder is therefore attributed to an invented
"session" and becomes an addressable slot that can never bind — a phantom the
sweep will later render as `orphaned`. It is recorded as `sessionKeySource:"path"`,
so it is not dishonest, but it is fabricated identity where the surrounding code
prefers to skip. **Fix:** tighten the derivation to components under a directory
the session layout actually names (`sessions/<key>/…`), rather than any component
anywhere in the path.

### m5 — The "Mongo wipe + WAL replay" test does not exercise a wipe
`tests/test_custom_state.py:240-244`

```python
before = replay(writer.observations())
after  = replay(writer.observations())   # "the wipe"
```

Two independent replays from an empty dict are trivially equal; nothing is ever
populated and then cleared. R-52's clause is "Mongo wipe + WAL replay reproduces
both collections **exactly**". **Fix:** replay into a `state`, mutate/populate it
further (or `state.clear()`), then replay again into a fresh dict and compare —
and do the equivalent on the live arm, where `drop_database` makes the wipe real.

---

## NITS

- **n1** — `tests/test_custom_state.py` and `tests/test_slots.py` are mode 644
  while every other `tests/test_*.py` is 755 and carries the same shebang.
  `run_all.sh` invokes `$PY <file>`, so nothing breaks; it is a consistency wart.
  `chmod +x`.
- **n2** — `custom_state.py:493` uses `legacy._stream_safe` (a *private* helper of
  another sub-plan's file) as its correctness reference. The test asserts the two
  agree, which is the right mitigation, but a shared escaper belongs in `refs.py`
  where both can import it publicly. Worth a note to sp-15/sp-09 rather than a
  change here.
- **n3** — `_slot_advance:1145` writes `ts` on every transition, so an orphan
  sweep overwrites the bind's timestamp. `firstSeenTs`/`lastSeenTs` survive, so
  nothing is lost; consider naming it `resolvedTs`.
- **n4** — `conflict_write`'s guard is `{"resolutionRank": {"$lt": 3}}`, so a
  *third* colliding agentId on an already-conflicted slot is discarded while
  `SlotTable._conflict` still returns `BindResult("conflict")` and skips the
  counter (`:1462-1466`). Same shape in `bind_slot:1550-1555`, which returns
  `"bound"` even when the guarded advance did not acquire. Both are honest enough
  at read time (`resolution_of` re-derives from the evidence) but the returned
  value overstates what was written.

---

## Checklist disposition

| item | verdict |
|---|---|
| GD-21 lazy pymongo, only mongo_store/mirror | PASS |
| GD-22 Mongo off the liveness path | PASS (module is pure; no blocking I/O) |
| GD-24 string `_id`s via ref_key only | PASS |
| GD-25 `$max/$addToSet/$min/$setOnInsert`, no `$inc`, no bare `$set` on accumulables | PASS |
| GD-26 no delete verbs / no `$unset` / no TTL | PASS |
| GD-27 no credential in repo/events/health/API/prompts; tests drop only `touch_test_<pid>` | PASS |
| GD-28 `{asserted,touch}` pinned three ways, no `harness` path | PASS |
| GD-29 no agent holds a client; dup-key tolerated and counted | PASS |
| GD-30 bounded/O(delta) | N/A here (sources are the rebuild seam, documented as such) |
| GD-15 one file one owner; no out-of-scope edits | PASS |
| tests assert real behaviour, skip cleanly without mongod | PASS with m2/m5 gaps |
| docs match implemented behaviour | **FAIL** (M1, M2, M4) |
| R-52 clauses | PASS except the head has no driver (M4) |
| R-53 clauses | PASS for ledger/marker; control arm inert (M1/M3) |
| SD-8 control ingest | **FAIL** (M1) |
