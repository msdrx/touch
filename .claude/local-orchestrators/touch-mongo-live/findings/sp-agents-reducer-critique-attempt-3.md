# sp-agents-reducer — adversarial critique, attempt 3

Reviewer: read-only. Files reviewed in full (the tree is untracked, so
`git diff` is empty for all three source files):

- `/home/laniakea/Projects/touch/aggregator/agents.py` (2318 lines)
- `/home/laniakea/Projects/touch/tests/test_agents.py` (1390 lines)
- `/home/laniakea/Projects/touch/tests/test_reducer.py` (556 lines)
- `/home/laniakea/Projects/touch/.claude/local-orchestrators/touch-mongo-live/findings/sp-agents-reducer-storage-deviation.md`

Normative: `touch-mongo-live-subplans.md` §sp-10 (R-28 / R-48 / R-54, SD-1,
SD-9, SD-11), `touch-mongo-live-plan.md` (GD-21…GD-30),
`touch-full-recon-plan.md` (GD-7/GD-9/GD-10/GD-11/GD-15).

**Verdict: REJECTED — 2 major, 1 minor, 4 nits.** Both majors are in the
reducer half (R-54) and both are fixable by one more gated attempt on these
three files: `depth: in-scope`, `critical_defect: false`.

---

## What I verified and could not break

Stated so the next implementer does not re-litigate closed ground.

- **All 9 attempt-2 findings are genuinely closed in the code**, not just in the
  test names. MAJOR 1: `Fragment.first_record_ts` is the first record's own
  stamp, `identity()` emits it, `_sort_key` tie-breaks on it on both the
  dataclass and the dict path, and `_collapse_identities` repairs documents
  written by the old shape at read time. MINOR 2: `map_agent_spawn` writes
  `spawn.agentType`/`spawn.resolvedModel` only; I re-derived the two mappers'
  field sets from the AST and the sole top-level overlap is `toolUseId` +
  `description`, both `$min`, both one harness fact. MINOR 3: D-5 exists and the
  `Fragment.tip` docstring no longer claims unconditional safety. MINOR 4:
  `META_FIELDS` is the five GD-24 columns. NITs 5–9 all landed
  (`"done — N failed verdict(s)"`, the topology-refId handoff, the
  `derivedFromSeq` docstring, the shared `ingest._transcript_walk` memo,
  `_labels_of`'s coercion into `AgentsError`).
- **GD-21.** `pymongo` does not appear in `agents.py` at all;
  `tests/test_stdlib_only.py` rc=0.
- **GD-24/SD-11 keys.** `refs.agent_key` is the raw 17-hex string, so
  `agents._id`, `run_nodes.agentId` and the reducer's `result_by_agent` really
  do share one key space; `_run_ref` is the only run lookup and closes the
  raw/escaped split (D-4). `derived_id`'s `<kind>:<refId>` is injective for the
  three kinds. No sub-document `_id`, no equality-match sub-document key.
- **GD-25 order-freedom.** `merge_ops(..., collection="agents")` runs the whole
  fence on every mapper return; the AST guard in
  `test_sd1_the_mappers_are_pure_and_write_only_agents` really does prove the
  mapping half builds only the four commuting builders.
- **GD-26.** No delete verb is called; no `$unset`/`$inc` literal.
- **GD-23 storage side.** No `state`/`liveness`/`verdict` field is written by
  either mapper, `failed` is absent from `NODE_STATES`, and `_only_ours` makes
  `derived` unreachable through `MIRROR_MAPPERS`.
- **Ownership / suites.** `HEAD` is still `579446e`; the three newest files
  under `aggregator/` + `tests/` are exactly the three owned ones.
  `tests/test_agents.py` rc=0, `tests/test_reducer.py` rc=0,
  `tests/test_stdlib_only.py` rc=0.

Both majors below survive all of that because they live in the one seam this
sub-plan's tests never exercise: the reducer joined against the **real output of
the other modules** rather than against hand-written fixtures.

---

## MAJOR 1 — every warm agent of a real session reduces to `unknown — session idle`, because `_session_activity` reads a field the harness does not maintain and turns its absence into positive evidence of idleness

**Where:** `aggregator/agents.py:1980-1998` (`_session_activity`), consumed at
`aggregator/agents.py:2126-2134` (agents) and `:2178-2185` (nodes); the
three-valued contract it feeds is `aggregator/agents.py:1790-1826`
(`liveness`).

```python
last = _aware(doc.get("lastTs"))
active = last is not None and (now - last).total_seconds() <= idle_limit
for session_id in ids:
    out[session_id] = out.get(session_id, False) or active
```

The function's own docstring states the rule it must obey:

> a sessionId absent from this map is *unobserved*, and :func:`liveness` is
> passed `None` for it rather than `False`. Demoting a warm agent because Touch
> never saw its session would invent staleness, which is the mirror image of the
> bug this item fixes.

The guard covers only the *absent-key* case. A session Touch **has** observed
but for which no usable timestamp exists lands in the map as `False`, and
`False` is the one value that demotes.

**Why that is the normal case, not an edge.** `sessions.lastTs` has exactly one
writer — `sessions.map_session`, `aggregator/sessions.py:1235-1236` — and
`sessions.py:124-127` says it is written **only for the live registry arm**,
from `entry.updated_at`. `ingest.COLLECTIONS` (`aggregator/ingest.py:359`) is
`("records", "stream_meta", "usage", "runs", "run_nodes")`, so nothing
accumulates record timestamps into `sessions.lastTs` either. Two consequences,
both reproduced below:

1. **The historical arm writes `sessionIds` and no `lastTs` at all**
   (`aggregator/sessions.py:1073-1085`). Every transcript on disk gets such a
   document. `_session_activity` scores it `False`.
2. **The live registry heartbeat is not a 180 s liveness signal.** The one
   registry entry on this machine right now —
   `/home/agent/.claude/sessions/15934.json`, `sessionId`
   `c96f1b66-d6a5-4322-adc6-ddb44f270ddb`, which is *this very session*, running
   as I type — has `updatedAt` **21 601 s (6 h) stale**. Scored against a 180 s
   window it is `False` too.

**Reproduction A — the sub-plan's own headline fixture flips.**
`tests/test_reducer.py::fanout` gives all five agents
`sessions: ["dd469822-2546-47d9-aaa3-31db4cb705e8"]` and then builds **no
`sessions` bucket**, so `active` is `{}` and `session_active` is `None` in every
single reducer test. Add the one document `sessions.py` unconditionally writes
for that session:

```python
s = fanout()                                   # five live siblings, now = T0+2min
print({d["state"] for d in reduce(s, now=T0+minutes(2)).agents.values()})
# -> {'running'}                                (what the suite asserts)

ms.apply_operations(s, sess.map_session(sess.SessionObservation(
    session_id="dd469822-2546-47d9-aaa3-31db4cb705e8", cwd="/home/laniakea/Projects/touch",
    slugs=("slug",), session_ids=("dd469822-2546-47d9-aaa3-31db4cb705e8",), sources=())))
print({(d["state"], d["reason"]) for d in reduce(s, now=T0+minutes(2)).agents.values()})
# -> {('unknown', 'owning session idle')}
```

The run closes too: `closed=True`, `reason="quiet — 5 node(s) idle past 180s"` —
a reason that is itself false, since every node's own activity is 60 s old.

**Reproduction B — the live registry shape, with the real 6 h heartbeat.** An
agent whose transcript was written **5 seconds ago**, in a session carrying both
the `live:` doc (`last_ts = now - 6h`, the observed value) and the `hist:` twin:

```
state  : unknown
reason : owning session idle
label  : unknown — session idle
idleSeconds: 5
```

**Why this is major.** R-54's whole subject is "a live agent renders running, a
dead one renders unknown, and the difference is `now()`". This inverts it on the
live path for every agent of every session, and it does so *silently* — the
label blames the session, so the row looks explained. It is the exact
fabricated-staleness failure the function's own docstring names, arriving
through the branch the docstring did not consider. Nothing in `test_reducer.py`
can see it: GD-10's conjunct is exercised only by calling `liveness(...,
session_active=False)` directly (`tests/test_reducer.py:155-158`), never by
reducing a state that contains a real `sessions` document.

**Fix.** Make `False` require positive evidence, and keep the third value
genuinely three-valued:

- in `_session_activity`, emit an entry **only** when a session document carries
  a usable `lastTs`; a document with none contributes nothing, so the sessionId
  stays unobserved (`None`) rather than idle. One line: drop the
  `out.get(session_id, False) or active` seed and write
  `if last is not None: out[session_id] = out.get(session_id, False) or active`.
- that still leaves case 2 (a stale registry heartbeat demoting a warm agent).
  Since the heartbeat is demonstrably not maintained at this granularity, the
  honest rule is that a session may **promote** but never **demote**: score
  `True` when fresh and leave the key absent when not, so `session_active` is
  `True` or `None` and GD-10's conjunct can only ever confirm liveness Touch has
  independent evidence for. If you want a real demotion, the evidence has to be
  a *positive* observation of session end (the `class`/registry-exit fact
  `sessions.py` already records), not the age of a field nobody refreshes.
- record the choice in the deviation file as a D-7 with the measured
  `updatedAt` age, so sp-07 and sp-12 know `sessions.lastTs` is not a liveness
  clock.

**Regression arms to add** (`tests/test_reducer.py`): build the `sessions`
bucket with `sessions.map_session` itself — a `hist:` doc and a `live:` doc with
a 6 h-stale `last_ts` — and assert the five warm siblings are still `running`;
then assert that a genuinely fresh `live:` doc yields `running` as well, so the
test distinguishes "does not demote" from "ignores the field".

---

## MAJOR 2 — `Reduction.operations()` writes a non-total `$set`, so on the server a derived document keeps conclusions the reducer no longer draws; `apply_derived` hides it because it is the only path any test uses

**Where:** `aggregator/agents.py:1955-1977` (`Reduction.operations`) against
`aggregator/agents.py:2307-2318` (`apply_derived`); the conditional keys are
built at `:2153-2163` (agents) and `:2206-2216` (nodes).

`operations()` `$set`s only the keys the current payload happens to contain, and
its docstring invites the live caller to use it directly:

> A caller enqueues these the same way it enqueues a mapper's output.

`apply_derived` does something different — it **clears the bucket first**
(`state[DERIVED_COLLECTION] = {}`) and then applies the same operations. The two
therefore produce different documents whenever an optional key disappears
between two reductions, and GD-26 forbids the `$unset` that would repair the
server copy. `mirror.py` drops `derived` only inside `Mirror.rebuild`
(`aggregator/mirror.py:2436-2438`), i.e. on `--rebuild` — never on a live tick.

**Reproduction (the `frozen` flag, the worst instance).** One agent, one
terminal run, two ticks 10 minutes apart:

```
tick 1 : state=unknown reason='frozen at run close' frozen=True  idleSeconds=30
tick 2 : state=unknown reason='idle 10m (> 180s)'   (no frozen)  idleSeconds=600
SERVER : state=unknown reason='idle 10m (> 180s)'   frozen=True  idleSeconds=600
```

The stored document claims `frozen: true` while the reducer that wrote it says
the row is not frozen, and `Reduction.counters["frozen"]` says `0`. sp-13 renders
the flag. A second instance is `idleSeconds`: an agent that is `running` with
`idleSeconds: 30` and then resulted with no `resultTs` emits no `idleSeconds`,
and the stale `30` survives beside `state: "done"`. `attemptLabel` and
`nextStage` behave the same way whenever a topology head is retracted.

`ms.fingerprint` over the two paths differs, which matters beyond cosmetics:
`mongo_store`'s memory model is the declared oracle for the server (its module
docstring, and R-56's wipe/rebuild-equivalence arm compares the two). Here the
oracle is *more* correct than the thing it models, so no acceptance test can
catch the divergence.

**The deviation file states the justification that is not true.** D-3's last
line: "`Reduction.operations()` still uses `$set`, legitimately: `derived` is
the one collection GD-23 declares droppable and it is **rebuilt wholesale**."
It is rebuilt wholesale only by `apply_derived` and only in memory; the exported
operation list provides no wholesale rebuild, and `grep -n "operations()"
tests/test_reducer.py tests/test_agents.py` returns **nothing** — the method has
no direct test at all.

**Fix (small, and it keeps `$set`).** Make the payload key set *total* so `$set`
is a full overwrite: always emit `frozen` (`False` when not frozen),
`idleSeconds`, `attemptLabel` and `nextStage` (explicit `None`), for both the
agent and the node payloads. Then `operations()` folded into a state without a
drop is byte-identical to `apply_derived`, which is the property to assert:

```python
server = {}; ms.apply_operations(server, r1.operations()); ms.apply_operations(server, r2.operations())
mem = {}; apply_derived(mem, r1); apply_derived(mem, r2)
check(ms.fingerprint(server) == ms.fingerprint(mem), "…")
```

If you would rather not widen the payload, then `operations()` must stop
claiming a caller may enqueue it like a mapper's output: say in the docstring
**and** in D-3 that the caller must drop `derived` (or the affected `_id`s)
first, and hand sp-06/sp-12 that precondition explicitly. Widening the payload
is the smaller change and needs no other sub-plan.

---

## MINOR 3 — a run that has no nodes yet gets `runState.runId: null`, in exactly the arm the reducer documents as normal

**Where:** `aggregator/agents.py:2110-2111` and `:2283`, against
`aggregator/agents.py:2031-2046` (`_run_ref`) and D-4.

`_run_ref`'s docstring and D-4 both promise "the raw id is carried as a field",
so no reader has to unescape an `_id` (LIVEFLOW-3). The seed is

```python
raw_run = {key: doc.get("runId") for key, doc in run_docs.items() if ... doc.get("runId")}
```

but `ingest.map_run` (`aggregator/ingest.py:2268-2281`) never stores a `runId`
field on a `runs` document — the `_id` *is* the run key, and
`COLLECTIONS["runs"].types` (`aggregator/mongo_store.py:368-378`) declares no
`runId` either. So `raw_run` starts empty and is filled only by an agent or a
node that names the run. A run document with neither:

```
{'state': 'running', 'closed': False, 'reason': 'no nodes observed yet',
 'nodeCount': 0, 'runId': None, ...}
```

That is precisely the case `reduce`'s own comment calls out as legitimate and
live ("the journal's first `started` creates the run document before it creates
any node (R-49 — a live run with no `<runId>.json` is on disk right now)"), so
the freshest run on the page is the one whose `runId` is `null`.

**Fix.** Last-resort fallback through the grammar's proven inverse, which is
already imported: `refs.parse_ref_key("run", run_ref).get("runId")`, guarded by
`refs.RefError`. Assert it in
`test_every_run_lookup_goes_through_refs_run_key` with a runId containing one of
`% # | :` so the escaped and raw spellings genuinely differ.

---

## NIT 4 — `fragmentTips.<firstUuid>.…` builds an update path out of harness text with no plain-field-name guard, while the sibling module raises on exactly that hazard

`aggregator/agents.py:1524-1528`. `ingest._launch_paths`
(`aggregator/ingest.py:2258-2268`) refuses a launch key containing `.` or a
leading `$` because "it becomes the dotted path `launch.<name>` and mongod would
read the dot as a nesting level". The same construction here is unguarded. The
live path is safe today — `ingest.bucket_of` (`aggregator/ingest.py:526-533`)
routes any record whose `uuid` fails `_UUID_RE` to `stream_meta`, so
`Fragment.first_uuid` is always a UUID — but `_fragment_of` explicitly accepts
"the dict a replay carries", and there the value is unvalidated. Two lines in
`map_agent` (or in `Fragment.identity`) restore parity with `ingest.py`, and the
error then arrives as an `AgentsError` that `mirror.Mapper` names.

## NIT 5 — the dict-replay path stores `unconventional: true` next to a `name`

`aggregator/agents.py:1409-1422` + `680`. `_as_observation` coerces
`labels` into a `Labels` correctly, but `AgentObservation.unconventional`
defaults to `True` and is never re-derived from the coerced labels, so

```python
map_agent({"agent_id": "a"*17, "labels": {"name": "impl", "unconventional": False}})
# $min: {'unconventional': True, 'name': 'impl'}
```

R-28's precedence says a named agent is not unconventional. `$min` means a real
observation later corrects it, so the blast radius is a replay-only corpus —
but the fix is one line in `_as_observation` (`unconventional=` from the coerced
labels when the dict did not state it), or drop the claim from the docstring.

## NIT 6 — `node_ref` / `node_key` are exported, documented as GD-7's identity, and called by nothing

`aggregator/agents.py:524-547`; the only callers are
`tests/test_agents.py:280-282`. Every real `_id` in this module comes from
`refs.agent_key`. Either route `map_agent`'s key through `node_key(agent_id=…)`
so the exported function is the one that is exercised, or say in the docstring
that it exists for sp-12's `(runId, key, ordinal)` half and is not on this
module's own write path.

## NIT 7 — `scan(paths=[...])` bypasses ingest's memo

`aggregator/agents.py:1333-1334` calls `ingest.read_transcript` where the
whole-corpus arm goes through `ingest._transcript_walk` →
`ingest._cached_transcript`. A caller that hands `scan` an explicit path list
re-reads files ingest already has in hand. Not on either registered source's
path, so it is cosmetic — but the docstring of `_corpus_scans` sells memo
sharing as the property of the function, and half of it does not share.

---

## Verdict

`approved = false` — either major alone requires another attempt.
`depth = in-scope`: MAJOR 1 is a predicate change inside one 18-line function
plus a `sessions`-bearing fixture, MAJOR 2 is four always-emitted keys plus one
fingerprint assertion, MINOR 3 is a fallback expression, and the nits are local.
No plan item needs re-research and no sub-plan boundary moves — in particular,
neither major requires touching `sessions.py`, `ingest.py` or `mirror.py`; both
are about how `agents.py` *interprets* what those modules already write.
`critical_defect = false` — the reducer's output shape, `fragments_of()` and the
storage split are all unchanged, so sp-11…sp-15 are not building on sand.
