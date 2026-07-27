# sp-agents-reducer — adversarial critique, attempt 2

Reviewer: read-only. Files reviewed (full content; the tree is untracked, so
`git diff` is empty for all three source files):

- `/home/laniakea/Projects/touch/aggregator/agents.py` (2077 lines)
- `/home/laniakea/Projects/touch/tests/test_agents.py` (1100 lines)
- `/home/laniakea/Projects/touch/tests/test_reducer.py` (500 lines)
- `/home/laniakea/Projects/touch/.claude/local-orchestrators/touch-mongo-live/findings/sp-agents-reducer-storage-deviation.md`

Normative: `touch-mongo-live-subplans.md` §sp-10, `touch-mongo-live-plan.md`
(GD-21…GD-30, R-48, R-54), `touch-full-recon-plan.md` (GD-7/GD-9/GD-10/GD-11,
R-28), SD-1/SD-9/SD-11.

**Verdict: REJECTED — 1 major, 3 minor, 4 nits.** Everything found is fixable
by one more gated attempt on these three files (`depth: in-scope`); nothing here
threatens the remaining sub-plans (`critical_defect: false`).

---

## What I verified and could not break

Stated up front so the next implementer does not re-litigate closed ground.

- **All 12 attempt-1 findings are genuinely closed**, not papered over. Re-read
  each closure against the code, not against the test names: no `$set` anywhere
  in the mapping half (`map_agent`/`map_agent_spawn` build only `op_min`,
  `op_max`, `op_add_to_set`, `op_set_on_insert`); `lastMark` really does make the
  tip coherent under reordering; `assemble` folds every contestable scalar
  through `_min_observed`, which is `mongo_store.apply_update` itself; no phantom
  element for a first-record-less fragment; `spawn_agent_conflict` fires because
  `seen` is consulted before the `pending.pop`; both per-path source arms call
  `ingest._in_scope`; all nine skip counters have firing cases.
- **GD-21.** `aggregator/agents.py` imports clean with `pymongo`/`bson`/`dns`
  blocked at `builtins.__import__`; `sys.modules` holds no `pymongo*` afterwards.
- **GD-24 keys.** `refs.agent_key` is the raw 17-hex id, so the `agents` `_id`,
  `run_nodes.agentId` and the reducer's `result_by_agent` share one key space;
  `_run_ref` really is the only run lookup and the escaped/raw split is closed.
  `derived_id`'s `<kind>:<refId>` is injective (the three kinds contain no `:`).
- **GD-25 order-freedom.** Re-ran the frozen corpus through `MIRROR_MAPPERS` in
  forward and reversed observation order: identical `ms.fingerprint`. The
  `fragmentTips` split does commute.
- **GD-23.** No `state`/`liveness`/`verdict` field is written by either mapper;
  `Liveness` is constructed in exactly two functions (`liveness`, `reduce`);
  `failed` is absent from `NODE_STATES`; `derived` is unreachable through
  `MIRROR_MAPPERS` because `_only_ours` rejects it.
- **Ownership.** `git log` unchanged (3 commits, `579446e` at HEAD); the three
  newest files in `aggregator/` + `tests/` are exactly the three owned ones
  (14:12/14:12/14:09, next is 12:51). Full suite reproduces the recorded
  baseline: 14 green, `test_mirror.py` and `test_sessions.py` red — both
  other sub-plans' property.

---

## MAJOR 1 — the `$addToSet` fragment identity carries a value that is NOT append-invariant, so one out-of-order timestamp permanently duplicates the element

**Where:** `aggregator/agents.py:559-570` (`Fragment.identity`), reached from
`aggregator/agents.py:1337` (`map_agent`); the offending value is set at
`aggregator/agents.py:746` (`first_ts=scan.first_ts`).

**The claim the code makes.** Module docstring (`agents.py:64-71`) and the
plan-side record (`sp-agents-reducer-storage-deviation.md`, D-1) both state:

> `fragments[]` holds the fragment's **identity** —
> `{sessionId, path, firstUuid, firstParentUuid, firstTs}` — **every member a
> property of the file's *first* line, which append cannot change.**

That is the entire justification for D-1's deviation from GD-24, and for
`$addToSet`'s field-order-sensitive sub-document equality being safe here.

**Why it is false.** `firstTs` is not the first line's timestamp. It is
`ingest.TranscriptScan.first_ts`, which `aggregator/ingest.py:1137-1138`
computes as the **minimum `timestamp` over every line in the file**:

```python
if first_ts is None or ts < first_ts:
    first_ts = ts
```

A single appended (or surviving-after-removal) record whose `timestamp`
precedes the current minimum changes the identity sub-document, and
`$addToSet` — which dedupes on exact, field-order-sensitive equality — adds a
**second element for the same file**. GD-26 forbids the delete that would
repair it, on the one collection GD-16 requires to stay small. This is
precisely the failure D-1 exists to prevent, re-entering through a member that
identity does not need.

**Reproduction** (stdlib, ~20 lines, run from the repo root):

```python
import json, sys, tempfile, pathlib
sys.path.insert(0, '.')
from aggregator import agents, mongo_store as ms
BIG = pathlib.Path('tests/fixtures/run-wf_829e6f58/dd469822-2546-47d9-aaa3-31db4cb705e8'
                   '/subagents/workflows/wf_829e6f58-b2f/agent-a2fc883c96ff7b837.jsonl')
tpl = json.loads(BIG.read_text().split("\n", 1)[0]); AG = 'd' * 17
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    d = root / 'projects/slug/sess-1/subagents/workflows/wf_1'; d.mkdir(parents=True)
    p = d / f'agent-{AG}.jsonl'
    u1, u2 = '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'
    p.write_text(json.dumps(dict(tpl, uuid=u1, parentUuid=None,
                                 timestamp='2026-07-25T03:00:10.000Z')) + "\n")
    st = {}
    ms.apply_operations(st, agents.map_agent(agents.assemble(
        [agents.read_fragment(str(p), root=str(root))])))
    with p.open('a') as h:                       # one record stamped 5 s EARLIER
        h.write(json.dumps(dict(tpl, uuid=u2, parentUuid=u1, type='assistant',
                                timestamp='2026-07-25T03:00:05.000Z')) + "\n")
    ms.apply_operations(st, agents.map_agent(agents.assemble(
        [agents.read_fragment(str(p), root=str(root))])))
    doc = st['agents'][AG]
    print(len(doc['fragments']), len(agents.fragments_of(doc)))
```

Observed: `2 2`. Both stored elements carry the **same** `path` and the **same**
`firstUuid` and differ only in `firstTs`; `fragments_of()` therefore hands sp-12
and sp-13 the same file twice, with identical `lineCount`, one agent showing two
fragments where one exists.

**Reachability is not hypothetical.** Out-of-order `timestamp` values are
written by the harness on this machine today: of 177 transcripts under
`~/.claude/projects` with ≥2 timestamps, **27 are non-monotonic and 20 have
`min(ts) != ts[0]`** (19 of those 20 are the second line stamped a few ms before
the first). Agent transcripts happen to be clean at this instant (0 of 149 have
`min(ts) != ts[0]`, 1 of 149 is internally non-monotonic), and the frozen fixture
corpus is clean 8/8 — which is exactly why no existing test catches this and why
the first occurrence would land in production, permanently.

**Fix (one line plus a field).** `firstTs` should be the first *record's* own
timestamp, not the scan minimum:

- add `first_record_ts: object = None` to `Fragment`
  (`aggregator/agents.py:534-557`);
- in `read_fragment` (`aggregator/agents.py:737-753`) set
  `first_record_ts=(first.ts if first is not None else None)` — `scan.first_ts`
  stays where it is, because `assemble`'s agent-level `firstTs` (`$min`) *wants*
  the minimum;
- in `identity()` emit `ms.ts_fields(self.first_record_ts)["ts"]`.

Simply dropping `firstTs` from `identity()` also closes it (nothing in R-48's
reader shape needs it); the only cost is that `_sort_key`'s dict branch loses
its timestamp tie-break and falls back to `path`.

**Regression arm to add** (`tests/test_agents.py`, beside
`test_a_growing_fragment_adds_one_element_not_one_per_tick:378`): make
`growing_fragment`'s `append()` write a record stamped *before* the first
record, and assert `len(doc["fragments"]) == 1` and `len(fragments_of(doc)) == 1`.
Today that assertion fails.

---

## MINOR 2 — the two mappers are NOT field-disjoint; `agents.model` is decided by lexicographic accident and contradicts R-48's "the meta-bearing fragment wins"

**Where:** `aggregator/agents.py:1362-1366` (the docstring claim),
`aggregator/agents.py:1404-1410` (`map_agent_spawn`'s `agentType`/`model`),
against `aggregator/agents.py:1305-1308` (`map_agent`'s meta scalars).

`map_agent_spawn`'s docstring asserts:

> disjoint from :func:`map_agent`'s fields by construction — the fragment arm
> never writes `spawn`, `toolUseId`, `description`, `resultSeen` or `resultTs`,
> and this one never writes `fragments`, `files` or `sessions`.

Both mappers write **`agentType` and `model`**, which the sentence omits. They
carry different vocabularies from different sources:

| source | `agentType` | `model` |
| --- | --- | --- |
| `agent-<id>.meta.json` (`META_FIELDS`) | `general-purpose` | `opus` |
| `toolUseResult` (`find_spawns:1156-1157`) | `general-purpose` | `claude-opus-5[1m]` (`resolvedModel`) |

Both real, both on disk for the same agent — e.g. `a483cae616edffe81`:
`.meta.json` says `"model":"opus"`, its launch record says
`"resolvedModel":"claude-opus-5[1m]"`. `$min` on the pair resolves to
`claude-opus-5[1m]` (`c` < `o`) **in either observation order** (verified), so:

- an Agent-tool agent stores `model = "claude-opus-5[1m]"` while a Workflow
  agent stores `model = "opus"` — one field, two vocabularies, and sp-13 renders
  it beside `run_nodes.model`, which the journal spells `opus`/`fable`;
- R-48's stated precedence ("`.meta.json` … the fragment that HAS meta wins on
  disagreement") is silently overridden by a non-fragment observation.

It is order-free, so nothing is corrupted — but the value is chosen by BSON
collation rather than by a rule anyone wrote down.

**Fix.** Keep the launch-side values in this mapper's own namespace, which
restores the disjointness the docstring claims:
`spawn.resolvedModel` and `spawn.agentType` (both `$min`; `spawn` is already
`map_agent_spawn`'s exclusive sub-document), and delete `model`/`agentType` from
the top-level `scalars` dict at `aggregator/agents.py:1405-1406`. Then correct
the docstring to name the real disjoint set. If you would rather keep them
top-level, state the precedence explicitly in the docstring **and** in the
deviation file, and add a test asserting which source wins.

---

## MINOR 3 — `fragmentTips` is `$max` on a "monotone under append" premise that GD-26 opens by denying; a shrink leaves a permanently wrong tip, and no one records the exposure

**Where:** `aggregator/agents.py:72-83` and `572-585` (`Fragment.tip`),
`aggregator/agents.py:1341-1346`; deviation file D-1.

D-1 justifies `$max` with "`lineCount`/`records`/`lastTs` are monotone under
append". GD-26's first sentence is that the transcript **is not append-only**:
`performRemoveByUuid` truncates and rewrites, `performCompactTranscript`
rewrites whole files — both extracted from the 2.1.220 binary. SD-10 puts shrink
detection in `tailer.py` and the repair sweep in `mirror.py` (sp-06), but that
sweep is a `records`-generation sweep; nothing supersedes a `$max` tip.

**Reproduction:** write a 4-record agent transcript, map it, then rewrite it with
the third record removed (the `performRemoveByUuid` shape) and map again.
Observed `fragments_of(doc)[0]`:

```
lineCount: 4, records: 4, lastUuid: <pre-removal tip>     # file now holds 3 lines
```

Remove the *last* record instead and `lastUuid` names a record `mirror.py`'s
sweep has retracted. Nothing detects it; `$max` cannot go down.

This is not a regression relative to the specified shape (a whole-subdocument
`$addToSet` is worse), so it is not a blocker — but the deviation record
presents `$max` as unconditionally safe, and it is safe only for the growth
half of GD-26's model.

**Fix (documentation-first is acceptable).** Add a **D-5** to
`sp-agents-reducer-storage-deviation.md` stating the shrink exposure plainly and
handing it to sp-06 the way D-1 already hands the `accumulable` fence to sp-05;
amend `agents.py:72-83` to say "monotone under append; a shrink leaves a stale
high-water tip until the generation sweep supersedes it". If you want the code
fix instead, key the tip `fragmentTips.<firstUuid>.<gen>.…` (or carry `gen` in
`lastMark`'s prefix) so a post-shrink re-ingest at a higher generation wins on
`$max` — that is a larger change and belongs to whichever attempt also touches
sp-06's sweep contract.

---

## MINOR 4 — `META_FIELDS` drops two fields GD-24's `agents` row names, and they are the only source when the launch pair is unavailable

**Where:** `aggregator/agents.py:296-298`.

```python
META_FIELDS = ("agentType", "model", "spawnDepth")
```

GD-24's `agents` row lists `agentType?, model?, spawnDepth?, **description?**,
**toolUseId?**, runId?, …` and declares a `{toolUseId:1}` sparse index. Every
real `.meta.json` on disk carries both of the missing ones:

```json
{"agentType":"general-purpose","description":"Assess data-layer feasibility",
 "toolUseId":"toolu_011Ug5qnU1bc2nEdXq57eRg7","spawnDepth":1,"model":"opus"}
```

Today they land **only** via `map_agent_spawn`, so they are absent whenever the
launch pair is not observable: a Workflow-profile agent (no `tool_use`/`
tool_result` pair at all — `find_spawns`'s own docstring says so), an agent
counted `spawn_without_result`, or one whose parent session transcript was
compacted. `description` is exactly the human-readable string R-28's
`unconventional` fallback needs when there is no `[touch] name=`.

**Fix.** `META_FIELDS = ("agentType", "model", "spawnDepth", "description",
"toolUseId")`. The values agree with the spawn arm's (checked on
`a483cae616edffe81`: meta `toolUseId` == the launch `tool_use.id`, meta
`description` == `input.description`), so the existing `$min` merge stays a
no-op rather than a second collision. Assert it in
`test_the_meta_bearing_fragment_wins_without_seeing_the_other:334`.

---

## NIT 5 — a run that closed with failing verdicts renders as the bare word `done`

`aggregator/agents.py:2036`. R-54 makes "API answer == page render" a property
of the derived document, and `label` is that render string. It is
`CLOSED_NO_VERDICT` only when there are **no** verdicts at all; a run with
`verdicts: {passed: 3, failed: 2}` renders `label: "done"`, indistinguishable
from a clean one. `failed` correctly stays out of `NODE_STATES`, but the label
can carry the tally honestly — e.g. `"done — 2 failed verdict(s)"` — without
becoming a state.

## NIT 6 — the topology key space is an unstated contract with sp-11

`aggregator/agents.py:1665-1678` indexes topology heads by `doc["refId"]` and
`reduce` looks them up with `refs.run_key(runId)` only. SD-9 fixes the *shape*
(`custom_state`, kind `topology`, payload under `data.custom`) but not the refId
space, and `test_topology_is_optional_and_read_as_a_shape:330` writes its own
document with `refId = <run key>` — so the join is asserted against the
reducer's own assumption. If sp-11 keys a topology by `{task, plan, stage?,
attempt?}` (a validated ref member per amended GD-11) the denominator arm
silently never fires and every run takes the "absent topology" path forever.
Add the requirement to the deviation file's handoff section (it already binds
sp-12/sp-13 on `fragments_of`), naming `refs.run_key(runId)` as the refId a
topology head must carry.

## NIT 7 — `derivedFromSeq` is a global max across two independent counters

`aggregator/agents.py:1781-1789` takes the maximum `seq` over `events` **and**
`custom_state_events`. Those are per-stream counters (GD-24 keys both
`<stream>#<seq:012d>`), so the result is not a watermark either stream can
resume from — R-55's resume is explicitly `(stream, seq)`. Harmless while the
value is informational; say so in the docstring, or store
`derivedFromSeq: {<stream>: <seq>}`.

## NIT 8 — `--rebuild` walks and parses the whole corpus twice

`iter_agent_observations(None)` (`:1451`) and
`iter_agent_spawn_observations(None)` (`:1468`) each call `scan()`, and `scan`
reads every agent transcript *and* every session transcript on both passes.
`AgentScan.observations()` already yields both kinds from one pass; the two
`MIRROR_SOURCES` entries could share a memoized scan keyed on
`(cwd, root, id(env))`.

## NIT 9 — the dict-observation path raises `AttributeError`, not `AgentsError`

`_as_observation` (`:1236-1246`) explicitly accepts "the plain dict a
replay/fixture hands back", but `map_agent` then calls `obs.labels.fields()`
(`:1311`). With `labels` as a dict — the only way a serialized observation could
carry it — this raises `AttributeError: 'dict' object has no attribute
'fields'`, bypassing the `AgentsError` funnel that `mirror.Mapper` converts into
a `MapperError` naming this module. Either coerce (`labels=Labels(**value)` in
`_as_observation`) or drop the dict claim from the docstring.

---

## Verdict

`approved = false` — MAJOR 1 alone requires another attempt. `depth = in-scope`:
MAJOR 1 is a field plus one line, MINOR 2 and MINOR 4 are edits to two constant
lists and a docstring, MINOR 3 is a paragraph in the deviation file, and the
nits are local. No plan item needs re-research and no sub-plan boundary moves.
`critical_defect = false` — nothing here makes the remaining sub-plans wasted
work; `fragments_of()` remains the reader contract sp-12/sp-13 must call
either way.
