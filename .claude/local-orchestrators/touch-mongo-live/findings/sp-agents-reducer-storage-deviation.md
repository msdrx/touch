# sp-agents-reducer — stated storage deviations from GD-24/R-48, and one handoff

Written by the sp-agents-reducer implementer (attempt 2), because attempt 1's
critique (MINOR 9) found the deviation recorded only in `aggregator/agents.py`'s
module docstring — where sp-12 (`server.py`) and sp-13 (`touch-visual/`) have no
plan-visible reason to look. This file is the plan-side record. Nothing here
changes an owned file outside sp-agents-reducer; the last section is a request
to three other sub-plans.

**Updated by attempt 3** for attempt 2's critique: D-1's `firstTs` member is
corrected (MAJOR 1), D-3 gains the two launch-side fields (MINOR 2), D-6 records
the `META_FIELDS` widening (MINOR 4), D-5 is new (MINOR 3), and the handoff
gains sp-11 (NIT 6).

**Updated by attempt 4** for attempt 3's critique: **D-7 is new** and is the one
other sub-plans must read — `sessions.lastTs` is not a liveness clock, and the
reducer's GD-10 conjunct may only promote. D-3's closing claim about
`Reduction.operations()` was **wrong and is corrected** (MAJOR 2): the payload,
not the method, is what makes the `$set` total. The handoff gains sp-07/sp-12.

## D-1 — `agents.fragments[]` is stored split, and read back whole

**Normative text.** GD-24's collection table gives `agents` a
`fragments:[{sessionId, path, firstUuid, lastUuid, lineCount}]` and makes
`fragments` an `$addToSet` set. R-48 repeats the field list.

**Why it cannot be stored that way.** `lastUuid` and `lineCount` change on
*every append*. A 250 ms poll tick re-observing a growing fragment therefore
adds a NEW set element four times a second: an unbounded array on the one
collection GD-16 requires to stay small, and a fragment list that shows the same
file eleven times. GD-26 forbids the delete that would clean it up.

**What is stored instead** (`aggregator/agents.py`, `Fragment.identity` /
`Fragment.tip` / `map_agent`):

| stored field | operator | contents |
| --- | --- | --- |
| `fragments[]` | `$addToSet` | `{sessionId, path, firstUuid, firstParentUuid, firstTs}` — every member a property of the file's FIRST RECORD, which append cannot change |
| `fragmentTips.<firstUuid>.lineCount` | `$max` | monotone under append |
| `fragmentTips.<firstUuid>.records` | `$max` | monotone under append |
| `fragmentTips.<firstUuid>.lastTs` | `$max` | monotone under append |
| `fragmentTips.<firstUuid>.lastMark` | `$max` | `"<lineCount:012d>#<lastUuid>"` |

`lastUuid` is NOT stored on its own. A uuid is not monotone, so `$max` over two
observations of a growing file would pair one observation's tip uuid with the
other's line count — an incoherent tip, which is what attempt 1 shipped (with
`$set`, whose real defect is that it does not commute; see D-3).

**`firstTs` is the first record's OWN timestamp** (`Fragment.first_record_ts`),
not `ingest.TranscriptScan.first_ts`. Attempt 2 shipped the latter, which is the
*minimum over every line in the file* — an aggregate, not a property of the
first record. The harness writes records out of timestamp order (27 of 177
transcripts on this machine are non-monotonic; 20 have `min(ts) != ts[0]`), so
one appended record stamped before the current minimum changed the identity
sub-document and `$addToSet` — field-order-sensitive exact equality — added a
SECOND element for the same file, which GD-26 forbids deleting. The agent-level
`agents.firstTs` still wants the minimum and still gets it: that field is `$min`
and merges across fragments. Regression arm:
`tests/test_agents.py::test_an_out_of_order_timestamp_does_not_duplicate_a_fragment`.

A document written by attempt 2's code already holds both spellings for any file
whose timestamps were out of order, and GD-26 forbids deleting one, so
`fragments_of()` now collapses elements that name the same first record
(`agents._collapse_identities`, merged through the store's `$min`). Read time is
the only place such a duplicate can be repaired. For every monotone transcript —
8/8 of the frozen corpus, 157/177 on this machine — the two timestamps are the
same value and nothing changed.

**Reader contract — unchanged.** `agents.fragments_of(doc)` returns exactly
R-48's list, in `parentUuid → uuid` chain order, with `lastUuid` unpacked and
`lastMark` removed. **sp-12 and sp-13 must call `fragments_of()` and must not
read `doc["fragments"]` directly.** Tests:
`tests/test_agents.py::test_a_growing_fragment_adds_one_element_not_one_per_tick`
(one element per file, both application orders, coherent tip, no `lastMark`
above the reader).

## D-2 — a fragment with no readable first record writes no element

Not a shape change; a rule. `agent-<id>.jsonl` is visible to the tailer the
moment it is created, which is before its first line is complete. Such a
fragment has no identity, so it contributes **no** `fragments[]` element, no
`files[]` entry and no `sessions[]` entry — only the agent document's existence
(R-28: harness facts create nodes). It is counted `no_first_record` in
`AgentScan.skipped`. Without the rule, `{path}` now and
`{sessionId, path, firstUuid, …}` one tick later are two BSON sub-documents and
`$addToSet` keeps both, permanently.

## D-3 — `spawn` is written leaf by leaf, and carries the launch's vocabulary

Recorded because the *operators* differ from the obvious reading of "the `spawn`
sub-document", and because the stored sub-document has two members beyond R-48's
`{recordUuid, toolUseId, fileHint}`: `sessionId` (the launching session, which
is deliberately NOT an entry in `sessions[]` — SESSIONJSONL-3) and, as of
attempt 3, `agentType` + `resolvedModel`.

**Why the last two moved in here.** They come off the `toolUseResult` and they
collide with `.meta.json`'s `agentType`/`model` on the top-level columns —
different vocabularies for one field: meta says `model: "opus"`, the launch says
`resolvedModel: "claude-opus-5[1m]"`, and `run_nodes.model` says `opus`/`fable`
again. Written to one column under `$min` the winner is BSON collation (`c` <
`o`, so the resolved id wins in either order), which silently overrides R-48's
stated precedence — "the fragment that HAS meta wins on disagreement" — with a
lexicographic accident. Namespaced, both facts survive, `map_agent` owns
`agentType`/`model` alone, and the precedence is structural again. The only two
columns both mappers write are `toolUseId` and `description`, which are one
harness fact stated twice (`agent-a342353f7b157760b`'s `.meta.json` carries the
launch record's own `tool_use` id and `input.description`, verbatim), so `$min`
over them is a no-op rather than a race. Asserted by
`tests/test_agents.py::test_the_two_mappers_do_not_fight_over_agent_type_and_model`.

**Reader note for sp-12/sp-13.** An agent whose `.meta.json` states no `model`
(the frozen Agent-tool specimen is one) has no top-level `model`; the concrete
id is `spawn.resolvedModel` and it is the LAUNCH's answer, not the agent's.
Render whichever you mean, and do not merge the two into one badge.

`spawn.recordUuid` / `.toolUseId` / `.sessionId` are `$min` (immutable per
agent). Every leaf of `spawn.fileHint` is `$max`. The hint's `size` and `ts` are
stat'd from the **parent session transcript**, which grows while the session is
alive, so two observations of one spawn disagree; a whole-value `$set` stores
whichever operation mongod's unordered bulk applied last. Per leaf the answer is
order-free AND coherent, because `size` and `ts` are the only churning leaves,
both monotone under append and both taken from one `os.stat`. A file replaced
under the same name can leave a `$max`-pinned `ino`/`size` from the older file,
which makes `check_file_hint` answer `stale` — the honest outcome for a cache
whose identity is `spawn.recordUuid`.

**There is no `$set` anywhere in `agents.py`'s mapping half.** `mirror.py`
batches two updates of one `_id` as two operations in one *unordered* bulk
(`_take_batches`) and re-queues unwritten operations at the tail (`_requeue`),
both justified in their own comments by "the algebra is `$max`/`$addToSet`". A
`$set` leaf breaks that justification silently. Guarded by
`tests/test_agents.py::test_sd1_the_mappers_are_pure_and_write_only_agents`
(the mappers may build only `op_max`/`op_min`/`op_add_to_set`/`op_set_on_insert`).

`Reduction.operations()` still uses `$set`, and attempt 3 justified that with
"`derived` … is rebuilt wholesale". **That was false as written** and attempt 4
fixes the thing rather than the sentence. The wholesale rebuild existed only in
`apply_derived` (which clears the bucket in memory); the exported operation list
had no drop in it, `mirror.py` drops `derived` on `--rebuild` alone, and GD-26
forbids `$unset` — so a key emitted on one tick and not the next survived on the
server document as a conclusion the reducer no longer drew (`frozen:true` beside
a reason that is not the freeze; an `idleSeconds` from ten minutes ago beside
`state:"done"`). The memory model that is `mongo_store`'s declared oracle was
the *more* correct of the two, so no acceptance test could see it.

**What makes `$set` legitimate now is the PAYLOAD, not the collection.**
:func:`reduce` emits every optional key on every payload — `idleSeconds`,
`frozen`, `attemptLabel`, `nextStage`, `None`/`False` included — so the `$set`
is a total overwrite of each `_id` and folding `operations()` into a state is
byte-identical to `apply_derived`. Asserted by fingerprint in
`tests/test_reducer.py::test_the_operation_list_is_a_total_overwrite_of_each_derived_document`,
which is also the method's first direct test. The one case per-`_id` totality
cannot express — an `_id` that stops being produced — is why the drop still
exists; on the live path it cannot happen (the reduction's keys come from
`agents`/`run_nodes`/`runs`, and the mirror is upsert-only), so the drop is
needed exactly where `Mirror.rebuild` already performs it.

**Reader note for sp-12/sp-13.** A derived document always carries those four
keys. "No attempt label" is `attemptLabel: null`, not an absent field; render on
`is None`, never on `in doc`.

## D-4 — the reduction is keyed by `refs.run_key(runId)` throughout

`Reduction.runs` and `derived`'s `runState:<refId>` use the escaped run key, not
the raw `runId`, which is carried as a `runId` **field** on the payload. `runs`
and the topology index were already keyed that way while `agents.runId` /
`run_nodes.runId` are raw; mixing the two split one run into two entries and
silently switched the freeze-to-stale rule off for any runId containing
`% # | :`. See `agents._run_ref` and
`tests/test_reducer.py::test_every_run_lookup_goes_through_refs_run_key`.

## D-5 — `fragmentTips` is a high-water mark, and GD-26 allows shrinks

`fragmentTips.<firstUuid>.{lineCount, records, lastTs, lastMark}` is `$max`,
justified in D-1 by "monotone under append". GD-26's own first sentence is that
the transcript is **not** append-only: `performRemoveByUuid` truncates and
rewrites, `performCompactTranscript` rewrites whole files (both extracted from
the 2.1.220 binary). After a shrink the stored tip keeps the pre-shrink counts
and the pre-shrink `lastMark` — `$max` cannot go down — so `fragments_of()` can
report a `lineCount` larger than the file and a `lastUuid` naming a record the
mirror has retracted.

This is an exposure, not a regression: the specified whole-sub-document
`$addToSet` is strictly worse in the same case, and no operator that commutes
can decrease. It is bounded — a stale *tip*, never a wrong identity, never a
duplicate element, never a wrong `_id` — and it self-corrects for `records`
because SD-10 puts shrink detection in `tailer.py` and the repair sweep in
`mirror.py` (sp-06).

**Ask (sp-06), stated not fixed.** The sweep supersedes a shrunk file's
*records* by generation; nothing supersedes the tip. If the sweep can carry the
generation into the agents document — `fragmentTips.<firstUuid>.<gen>.…`, or a
`gen` prefix inside `lastMark` so a post-shrink re-ingest wins on `$max` — the
exposure closes without giving up commutativity. That change spans the sweep
contract and this document's key grammar, so it belongs to whichever attempt
owns both; it is deliberately NOT made here.

## D-6 — `META_FIELDS` reads five fields, not three

`("agentType", "model", "spawnDepth", "description", "toolUseId")`. All five are
GD-24 `agents` columns (`toolUseId` carries the sparse index GD-24 declares, and
`description` is the human-readable string R-28's `unconventional` fallback
renders when no `[touch] name=` exists). The last two were previously written
only by the spawn arm, so they were absent for every agent whose launch pair is
not observable: a Workflow-profile agent has no `(tool_use, tool_result)` pair
at all, an agent counted `spawn_without_result` has no result yet, and an agent
whose parent transcript was compacted has lost the pair. The values agree with
the launch arm's (see D-3), so the merge stays a `$min` no-op.

## D-7 — `sessions.lastTs` is not a liveness clock; the session conjunct may only PROMOTE

**Normative text.** GD-10 conjoins "the owning session is busy" into the running
state, and R-54 computes liveness from `now()` at read time.

**What the field actually is.** `sessions.lastTs` has exactly one writer —
`sessions.map_session`, and only its **live registry** arm, from the registry
entry's `updatedAt` (`aggregator/sessions.py`, "What this module does not
timestamp"). Two measured consequences:

1. The **historical** arm writes no timestamp at all, and `ingest.COLLECTIONS`
   is `("records", "stream_meta", "usage", "runs", "run_nodes")` — nothing
   accumulates record stamps into `sessions`. Every transcript on disk yields a
   `sessions` document with **no `lastTs`**.
2. The **registry heartbeat is not refreshed at liveness granularity.** The one
   entry on this machine, `/home/agent/.claude/sessions/15934.json`, sessionId
   `c96f1b66-d6a5-4322-adc6-ddb44f270ddb`, is the session that was actively
   running while attempt 3 was reviewed *and* while attempt 4 was implemented:
   its `updatedAt` measured **21 601 s** stale then and **22 688 s** (6 h 18 m)
   stale now. Against a 180 s window it scores idle in both readings.

**The rule, therefore.** `agents._session_activity` records only *positive*
evidence: a session appears in the map, as `True`, when it carries a `lastTs`
inside the idle window, and is **absent otherwise**. `agents._session_conjunct`
turns that into `True` or `None` — never `False`. `liveness()` keeps all three
values (a `False` still demotes), because the predicate is the contract; what
changed is that nothing derives `False` from the *age of a field nobody
refreshes*. A real demotion needs a positive observation of session end — the
registry-exit fact `sessions.py` could record — and when one exists it lands in
`_session_activity` as its own arm.

**Why it is a deviation worth writing down.** Scored the other way (absence ⇒
idle) every warm agent of every real session reduced to `unknown — session
idle`, and the run closed `quiet — 5 node(s) idle past 180s` while every node's
own activity was 60 s old. The label blames the session, so the row looks
explained: R-54's subject ("a live agent renders running, a dead one renders
unknown, and the difference is `now()`") was inverted *silently* on the live
path. Regression arms:
`tests/test_reducer.py::test_a_session_may_promote_a_node_and_never_demote_it`
builds the `sessions` bucket with `sessions.map_session` itself — the `hist:`
document and a `live:` document with the measured 6 h heartbeat — and asserts
the warm siblings stay `running`, that a *fresh* heartbeat is genuinely read
(`{sessionId: True}`, so the arm is not merely ignoring the field), and that
promotion cannot revive a node silent for ten minutes.

## Handoff — five one-line asks this sub-plan may not make itself

1. **sp-05 (`aggregator/mongo_store.py`).** `COLLECTIONS["agents"]` declares
   `accumulable=("firstTs", "lastTs", "sessions", "files", "fragments")`. It
   knows nothing about `fragmentTips` or `spawn`, so `validate_update`'s
   `$set`-on-accumulable fence is blind to both — which is exactly why attempt
   1's `$set` reached a stored document. Requested addition:
   `accumulable=(…, "fragmentTips", "spawn")`. `agents.py` no longer emits a
   `$set` on either, so the change is a fence, not a fix; it is sp-05's file and
   is deliberately NOT made here.
2. **sp-11 (whoever writes the `topology` `custom_state` head).** The reducer
   joins a topology to a run by **`refs.run_key(runId)`** and by nothing else
   (`agents._run_ref`, `agents.topology_index`). SD-9 fixes the head's shape
   (kind `topology`, payload under `data.custom`) but says nothing about its
   `refId`, and amended GD-11 would also allow a `{task, plan, stage?, attempt?}`
   ref — which would produce an index this reduction never hits, leaving every
   run on the "absent topology" arm silently and forever. **A topology head that
   describes a run must carry `refId = refs.run_key(runId)`.** Asserted from
   this side by
   `tests/test_reducer.py::test_topology_is_optional_and_read_as_a_shape`
   (a head keyed by any other ref does not join, and the run is counted
   `topology_missing`).
3. **sp-07 (`aggregator/sessions.py`).** D-7: `lastTs` is a registry echo, not a
   heartbeat, and the reducer treats it as promote-only. If sp-07 ever adds a
   *positive* observation of a session ending (the registry entry disappearing,
   a recorded exit), say so on the document and the reducer gains a real
   demotion arm; until then no consumer may read `lastTs` as "is this session
   alive". Not reached into here — `sessions.py` is sp-07's file.
4. **sp-12 (`aggregator/server.py`) / sp-13 (the page).** Two reader rules from
   this pass: a derived document always carries `idleSeconds`, `frozen`,
   `attemptLabel` and `nextStage`, so "no value" is `null` and not an absent key
   (D-3, MAJOR 2); and a row is never demoted because its session looks quiet
   (D-7) — if the page wants a "session idle" badge it must come from an
   observation, not from the age of `sessions.lastTs`.
5. **sp-15 (docs register).** D-1…D-7 belong in the plan-side record of what
   shipped. D-1's reader rule (`fragments_of()`, never `doc["fragments"]`) is
   binding on sp-12 and sp-13, as is D-3's note on `model` vs
   `spawn.resolvedModel` and its new totality clause.
