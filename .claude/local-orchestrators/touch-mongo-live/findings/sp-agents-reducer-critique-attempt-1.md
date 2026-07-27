# sp-agents-reducer — adversarial critique, attempt 1

**Verdict: REJECTED.** 2 blockers, 2 majors, 6 minors/nits.
Depth: **in-scope** (every finding is fixable by one more gated attempt on the
three owned files). critical_defect: **false**.

Reviewed (full content — the tree is bulk-untracked, no per-file diff exists):

- `/home/laniakea/Projects/touch/aggregator/agents.py` (1820 lines)
- `/home/laniakea/Projects/touch/tests/test_agents.py` (669 lines, rc 0, 112 ok)
- `/home/laniakea/Projects/touch/tests/test_reducer.py` (424 lines, rc 0, 68 ok)

Against `plan/touch-mongo-live-subplans.md` §sp-10, R-28/R-48/R-54 +
GD-21…GD-30 (amendment), GD-7/GD-9/GD-10/GD-11/GD-15/GD-16 (base), SD-1/SD-9/SD-11.

I re-ran both owned suites (green) and `tests/test_stdlib_only.py` (green), and
I confirmed the test gate's attribution of the two red suites. **The test gate's
verdict is not wrong about what it measured** — the reducer half (R-54) is, as
far as I could attack it, correct, complete and non-tautological. Every finding
below is in the R-48 *mapping* half, and every one of them is a case the frozen
static corpus cannot express: the corpus is a set of files that never change,
and the defects only appear when a file is observed twice in different states
(the live path) or when two fragments disagree.

Four of the findings were **reproduced with probe scripts** against the real
module; the reproductions are inlined.

---

## BLOCKER 1 — `$set` on `fragmentTips.*.lastUuid` breaks GD-25's commuting algebra

`aggregator/agents.py:1153-1158` (written), `:68-70` and `:1084-1087` (claimed)

```python
if "lastUuid" in tip:
    tips_set[f"fragmentTips.{item.first_uuid}.lastUuid"] = tip["lastUuid"]
...
if tips_set:
    ops.append(ms.op_set(tips_set))
```

GD-25's vocabulary for `agents` is exactly `$max` / `$addToSet` / `$min` /
`$setOnInsert` ("scalars-first/last are `$min`/`$max`, immutables are
`$setOnInsert`"). `$set` is not in it, and the whole downstream machine is built
on the assumption that these operations commute:

- `mirror._take_batches` (`aggregator/mirror.py:2013-2016`) deliberately keeps
  **two updates to one `_id` as two operations in one batch**, "because the
  algebra is `$max`/`$addToSet`";
- that batch goes to `bulk_upsert(..., ordered=False)`
  (`aggregator/mongo_store.py:1461`, `mirror.py:1420`) — mongod may apply the
  two in either order;
- `mirror._requeue` (`mirror.py:2048-2054`) appends unwritten operations **at
  the tail**, explicitly justified by "`$max`/`$addToSet`/`$min`/`$setOnInsert`
  commute".

`$set` does not commute, and `mongo_store`'s accumulable fence cannot catch it:
`fragmentTips` is not declared in the `agents` spec's `accumulable` set
(`mongo_store.py:365`, sp-05's file), so `validate_update`'s `$set` check at
`mongo_store.py:1072` waves it through.

**Reproduced** (two observations of one growing fragment — the exact fixture of
`test_a_growing_fragment_adds_one_element_not_one_per_tick`, applied in both
orders):

```
forward tip : {'lastUuid': '2222…', 'lineCount': 2, 'records': 2, 'lastTs': 03:00}
reversed tip: {'lastUuid': '1111…', 'lineCount': 2, 'records': 2, 'lastTs': 03:00}
fingerprint equal: False
```

The reversed order stores `lastUuid == firstUuid` next to `lineCount: 2` — an
incoherent tip that `fragments_of()` hands straight to `/api/*` and the page —
and `ms.fingerprint` differs, which is the property GD-25's acceptance test
(R-44) exists to certify.

`test_gd25_algebra_over_the_frozen_corpus` misses this because every fragment in
the frozen corpus produces the *identical* `$set` value on every pass, so
shuffling is a no-op for that operator.
`test_a_growing_fragment_adds_one_element_not_one_per_tick` misses it because it
applies the two observations in forward order only.

**Fix.** Make the tip monotone, so the operator matches the claim in the
docstring at `:69-70`. Concretely: write the tip as ONE `$max` value whose first
element is the monotone counter —
`ms.op_max({f"fragmentTips.{uuid}": [line_count, records, last_uuid, last_ts]})`
(BSON array comparison is element-wise, so the observation with the highest
`lineCount` wins as a unit, and `fragments_of` unpacks it) — or drop `lastUuid`
from storage entirely and let `fragments_of` report only the `$max`-safe leaves.
Then add the missing test: apply two observations of a growing fragment in
**both** orders and assert `ms.fingerprint` equality plus tip coherence
(`lastUuid` belongs to the line `lineCount` counts).

---

## BLOCKER 2 — `$set` on the whole `spawn` sub-document is order-dependent on the live path

`aggregator/agents.py:1206`, rationale at `:1181-1184`

```python
ops = [ms.op_set_on_insert({"provenance": PROVENANCE}), ms.op_set({"spawn": spawn})]
```

The docstring defends this with "the sub-document's contents are a pure function
of that pair plus the file's current `(stDev, ino, size)`, so two passes over
unchanged bytes write the same value". The premise is false in the case that
matters: `spawn.fileHint` is stat'd from the **parent session transcript**
(`:970`), which grows continuously while the session is alive, so
`fileHint.size` and `fileHint.ts` differ on essentially every re-observation.
This file's own test proves it —
`tests/test_agents.py:527-533` appends one line to the session transcript
specifically to show the hint "goes stale the moment the file changes".

Combined with unordered bulk + `_requeue` (see Blocker 1), the stored spawn is
whichever observation mongod happened to apply last.

**Reproduced** (same spawn, two observations at size 1000 and 2000):

```
fwd size: 2000
rev size: 1000
fingerprint equal: False
```

**Fix.** Split `spawn` by mutability, the same way `fragments`/`fragmentTips`
was split: the immutable leaves (`spawn.recordUuid`, `spawn.toolUseId`,
`spawn.sessionId`) under `$setOnInsert`/`$min`, and the perishable hint under a
monotone `$max` composite ordered by its own `ts` (e.g.
`ms.op_max({"spawn.fileHint": [ts, size, ino, st_dev, line]})`, unpacked by the
"jump to spawn" reader). `merge_ops`'s `_conflicting_path` permits this — the
paths are disjoint. Add a both-orders test with two different hint sizes.

---

## MAJOR 3 — the rebuild arm and the backfill arm store DIFFERENT documents for the same agent

`aggregator/agents.py:777-787` (`assemble`) vs `:1120-1130` (`map_agent`);
invariant claimed at `:1238-1240`

`iter_agent_observations`'s docstring states the contract outright:

> With a concrete path (a `--backfill` …) the single fragment is emitted as an
> agent of its own — **which is precisely the case the `$min`/`$addToSet`/`$max`
> algebra above exists to make identical to the assembled one.**

It is not identical, because the two arms use two different conflict-resolution
rules:

- `assemble()` resolves a meta/label disagreement **chain-first**
  (`meta.setdefault(...)` at `:783`; `labels = marker_bearing[0].labels` at
  `:787`; `unconventional = labels.unconventional` at `:804`);
- `map_agent()` resolves it per-leaf with **`$min`** (`:1130`).

**Reproduced (meta):** two fragments whose `.meta.json` disagree —

```
rebuild : {'agentType': 'workflow-subagent', 'model': 'opus'}
backfill: {'agentType': 'general-purpose',   'model': 'haiku'}
fingerprint equal: False
```

**Reproduced (labels — worse):** chain-first fragment carries a `[monitor]`
marker with no `name=`, the continuation carries `[touch] name=critique` —

```
rebuild  {'name': None,       'unconventional': True } labels={'plan':'sp-x','stage':'impl'}
backfill {'name': 'critique', 'unconventional': False} labels={'plan':'sp-x','stage':'critique'}
```

The rebuild arm **drops a name the harness actually stated** and flags a named
agent `unconventional: True`, which is R-28's flag inverted: the UI renders that
agent as a raw 17-hex id after a `--rebuild` and by its name after a
`--backfill`. It also directly defeats the module docstring's own argument at
`:90-93` ("`unconventional` is written by every observation and merged with
`$min` … so 'some observation found a name' wins") — on the rebuild path `$min`
never sees two values, because `assemble` collapsed them first.

This breaks R-56's "wipe + rebuild equivalence" arm and GD-25's fingerprint
property across ingest modes. The frozen corpus hides it: `BIG` has meta and a
marker, `SMALL` has neither, so there is nothing to disagree about
(`test_the_meta_bearing_fragment_wins_without_seeing_the_other` tests only the
absent-vs-present case, never present-vs-present-and-different).

**Fix.** Make `assemble()` use the mapper's rule: per-field `min()` over every
meta-bearing fragment, per-leaf `min()` over the label fields of every
marker-bearing fragment, and `unconventional = min(...)` across all fragments
(not chain-first's). Then add a fixture with two disagreeing metas AND two
disagreeing markers and assert
`fingerprint(rebuild_state) == fingerprint(backfill_state)`.

---

## MAJOR 4 — a fragment observed before its first record is readable leaves a permanent phantom `fragments[]` element

`aggregator/agents.py:477-488` (`Fragment.identity`), `:643-659`, `:1139-1142`

`identity()` filters out `None` members, so the sub-document's *field set*
depends on what was readable at observation time:

- file created, first line not yet flushed (or the first line torn mid-write —
  `read_fragment`'s own docstring at `:621-624` says a torn tail is deferred):
  identity is `{"path": …}`;
- one tick later: identity is `{"sessionId", "path", "firstUuid",
  "firstParentUuid", "firstTs"}`.

Those are two different BSON sub-documents, so `$addToSet` — which the module
correctly notes dedupes on exact field-order-sensitive equality (`:481-483`) —
adds a **second element for the same file**, permanently (GD-26: no deletes).

**Reproduced**, both for an empty file and for a torn first line:

```
fragments: [ {"path": "…/agent-ccc….jsonl"},
             {"path": "…/agent-ccc….jsonl", "firstUuid": "1111…", "firstTs": …} ]
fragments_of -> 2 elements
```

This is precisely the defect the whole storage split was introduced to prevent
("a fragment list that shows the same file eleven times", `:59-60`), reappearing
in the live-tail case rather than the per-tick one. It is reachable on every
newly spawned agent: the tailer sees `agent-<id>.jsonl` the moment it is
created, which is before the first record is complete.

**Fix.** A fragment with no `first_uuid` has no identity yet — refuse to emit a
`fragments[]` element (and the matching `files[]` entry) for it, count it in
`_skips()` (`no_first_record`), and let the next tick add the real one. Add the
test: observe empty ⇒ append a record ⇒ observe ⇒ assert exactly one
`fragments[]` element and one `fragments_of()` row.

---

## MINOR 5 — the per-path source arms ignore R-25's scope, so `--backfill` mirrors agents `--rebuild` excludes

`aggregator/agents.py:1244-1247`, `:1254-1257` vs `:1009-1011`

The `path=None` arm goes through `scan()` → `ingest.iter_transcript_paths`,
which is scoped to the cwd slug plus `.session-aliases` (R-25 as amended, and
the module docstring at `:1002-1005` makes a point of it: "the four foreign
`/tmp` slug directories on this machine are not this project's agents"). The
per-path arm applies no scope check at all, and `mirror.iter_backfill_sources`
(`mirror.py:2589-2617`) walks all of `projects/**` with only the GD-27 deny-list.
So `--backfill` produces a strict superset of `--rebuild`, and R-56's
wipe+rebuild-equivalence arm compares two different corpora.

**Fix.** Either apply the same scope predicate in the per-path arms (return `[]`
for a path outside `sessions.scoped_dirs`), or state the widening explicitly in
the docstring and hand sp-14 the note that the two modes are not comparable
without a scope filter. Flagging as minor because the resolution may legitimately
belong to sp-06/sp-14 rather than here — but the current docstring implies the
two arms agree, and they do not.

---

## MINOR 6 — `spawn_agent_conflict` is structurally unreachable

`aggregator/agents.py:955-964`

```python
launch = pending.pop(tool_use_id, None) if tool_use_id else None
if launch is None:
    skipped["spawn_without_tool_use"] += 1
    continue                                   # <- always taken on the 2nd result
record, arguments = launch
if seen.get(tool_use_id, agent_id) != agent_id and skipped is not None:
    skipped["spawn_agent_conflict"] += 1       # <- unreachable
```

The conflict the counter describes ("one toolUseId naming two agentIds", `:576`)
requires a *second* result for a `tool_use_id` already consumed — but the launch
was `pop`ped on the first, so the second takes the `continue` at `:960` and is
miscounted as `spawn_without_tool_use`. The counter can only fire if a second
`tool_use` block re-registers the same id, which the corpus never does.

**Fix.** Consult `seen` before the `launch is None` early return (or don't pop
until after the check), and add a doctored-fixture test that fires it — the same
shape R-50 uses for its agentId-conflict counter.

---

## MINOR 7 — six of the nine declared skip counters are never asserted; one is unreachable from `scan()`

`aggregator/agents.py:562-578`

`_skips()`'s docstring argues the set is declared so that "nothing was skipped"
is assertable. Only `spawn_without_result` and `unchained_fragment` are ever
asserted (`tests/test_agents.py:552`, `:367`). `meta_conflict`,
`marker_conflict`, `not_an_agent_file`, `spawn_without_tool_use`,
`spawn_agent_conflict` and `unstattable_hint` have no test on either side, and
`not_an_agent_file` (`:630`) is unreachable from `scan()` because `:1017` already
guards on `ingest.agent_id_for_path(path)`. `meta_conflict` and `marker_conflict`
are the two that Major 3's fixture would exercise for free.

**Fix.** Assert each counter's firing case in the tests added for Majors 3/4, and
either delete `not_an_agent_file` or keep it and note it is a `read_fragment`
direct-call counter only.

---

## MINOR 8 — run lookups mix the raw `runId` with `refs.run_key(runId)`

`aggregator/agents.py:1630`, `:1652`, `:1657`, `:1700`, `:1705`, `:1740`, `:1754`

`terminal` and `run_docs`/`per_run` are keyed by the **raw** `doc["runId"]`,
while topology is looked up with `refs.run_key(run_id)` (`:1657`, `:1705`,
`:1771`). They agree today only because `_build_run` is `escape_component`
(`refs.py:497`) and `wf_<hex>` contains none of `% # | :`. A runId carrying any
of those characters splits one run into two `Reduction.runs` entries — the
escaped `run_docs` key with `nodeCount: 0`, and the raw `per_run` key with no
`startedAt`/`endedAt` — and the freeze rule silently stops firing, because
`terminal.get(run_id)` misses.

**Fix.** Key every run lookup in `reduce` through `refs.run_key(run_id)` (one
helper, applied at all five sites), and add a test with a runId containing a `%`.

---

## MINOR 9 — the `fragments`/`fragmentTips` deviation is recorded only in this module's docstring

`aggregator/agents.py:52-73`; GD-24's normative table, amendment plan lines 199-230

GD-24's collection table is declared "the ONE normative collection table" and
gives `agents` a `fragments[]` whose elements carry `lastUuid`/`lineCount`.
The implementation's split is well-argued (unbounded `$addToSet` growth on a
GD-16 collection is a real defect) and the reader contract is preserved by
`fragments_of()`, so I am not asking for it to be reverted — but:

1. nothing in `.claude/local-orchestrators/touch-mongo-live/` records the
   deviation, so sp-12/sp-13 have no plan-visible reason to call
   `fragments_of()` instead of reading `doc["fragments"]` directly;
2. `mongo_store.COLLECTIONS["agents"]` (`:353-367`) knows nothing about
   `fragmentTips` — it is in neither `types` nor `accumulable` — which is
   exactly why the `$set` fence was blind to Blocker 1.

**Fix.** Write the deviation into a findings note under the task folder (CLAUDE.md
makes the repo copy the durable record), and raise the one-line
`accumulable=(… "fragmentTips")` addition to sp-05's owner / sp-15's docs pass so
the fence covers the field this sub-plan invented. Do not edit `mongo_store.py`
here — it is sp-05's file.

---

## NIT 10 — docstring says the tip is `$max`; `lastUuid` is `$set`

`aggregator/agents.py:68-70` and `:1085-1087` both describe `fragmentTips` as
"`$max` on the monotone counters" with `lastUuid`/`lastTs` "written beside them",
which reads as if the whole tip were order-safe. It is not (Blocker 1). Obsolete
once Blocker 1 is fixed; if the fix keeps a non-`$max` leaf, say so plainly.

## NIT 11 — `unchained_fragment`'s comment is broader than what fires

`aggregator/agents.py:572` says "stitch chain does not reach this fragment", but
`order_fragments` (`:726-728`) counts only *leftovers* — cycle members. A
fragment whose parent was compacted away becomes its own chain head and is
deliberately **not** counted (`tests/test_agents.py:353-355` pins that). Reword
the comment to "fragment reachable from no chain head (a cycle)".

## NIT 12 — `PASSED` / `FAILED` / `CLOSED_NO_VERDICT` are module-level but not exported

`aggregator/agents.py:1299-1306` — `NODE_STATES` is in `__all__`, the verdict
vocabulary is not, yet sp-12's API and sp-13's page need the same three strings
to render a node row. Add them to `__all__` rather than letting the next
sub-plan re-spell them (the same argument the file already makes at `:1303-1305`
for importing `legacy.CLOSED_NO_VERDICT`).

---

## What I attacked and could NOT break

Recorded so the next attempt does not re-litigate settled ground:

- **GD-21** — `pymongo` appears nowhere in `agents.py`; `mongo_store` is imported
  for pure op-builders only; `tests/test_stdlib_only.py` green.
- **GD-22** — no DB I/O, no client, no socket anywhere in the file; `reduce` is
  pure over `(state, now)` and the AST guard at `test_reducer.py:373-397` is real
  (it also pins "only `liveness` and `reduce` construct a state").
- **GD-24 `_id`s** — every key goes through `refs.agent_key`; the AST guard at
  `test_agents.py:381-390` proves no other `*_key` builder is reachable from the
  mappers; `derived_id` is legitimately reducer-owned (`derived`'s `id_kinds` is
  empty by declaration, `mongo_store.py:467-473`, and `check_id` exempts it by
  that declaration rather than by omission).
- **GD-26** — no delete verb, no `$unset`, no TTL; `apply_derived`'s bucket
  replacement is the one collection GD-23 declares droppable, and it is the
  memory model only (the server-side drop is correctly handed to `mirror.py`).
- **GD-28** — `harness` on every mapped document, `derived` on every reducer
  document, asserted across all 11 docs in
  `test_derived_documents_are_droppable_and_versioned`.
- **R-54 in full** — the three-state predicate, the strict `> 180 s` boundary,
  the `session_active is None` three-valued conjunct, the freeze-to-stale
  migration, the five-sibling run close with zero `failed`, `reducerVersion`
  drop-and-rebuild producing the same conclusion, and "API answer == page render"
  (the label *is* a field). I tried to find a path that produces `failed` as a
  liveness state and there is none.
- **SD-9** — topology is read as a shape with no import of `custom_state`; the
  absent arm, the present arm, the per-stage cap override and the unparsable
  attempt are all covered.
- **GD-9 marker layer** — the window, two markers on one line, the
  prose-vs-payload distinction for `markerMisplaced`, and cross-checking the
  grammar against `decision_watcher.py` on real corpus text. Solid.
- **Ownership** — nothing outside the three owned files was modified; no commit.

---

## Reproduction scripts

Written to the session scratchpad, not the repo (they are throwaway probes, and
`tests/` outside the three owned files is not this sub-plan's):
`probe.py` (Blocker 1), `probe2.py` (Blocker 2), `probe3.py` (Major 4),
`probe4.py`/`probe5.py` (Major 3), under
`/tmp/claude-1000/-home-laniakea-Projects-touch/c96f1b66-d6a5-4322-adc6-ddb44f270ddb/scratchpad/`.
Each is ~20 lines and re-derivable from the finding text above; the next
implementer should turn 1, 2, 3 and 4 into permanent assertions in
`tests/test_agents.py` rather than re-running the probes.
