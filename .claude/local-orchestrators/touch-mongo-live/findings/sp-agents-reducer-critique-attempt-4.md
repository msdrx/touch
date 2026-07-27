# sp-agents-reducer — adversarial critique, attempt 4

**Verdict: REJECTED.** 1 blocker, 1 major, 2 minor, 3 nits.
depth: `in-scope` — every finding is fixable by one more gated attempt on
`aggregator/agents.py` + `tests/test_reducer.py`.
critical_defect: `false`.

Reviewed: `aggregator/agents.py` (2486 lines, new/untracked),
`tests/test_agents.py`, `tests/test_reducer.py`,
`findings/sp-agents-reducer-storage-deviation.md`, against
`plan/touch-mongo-live-subplans.md` §"sp-10 — agents-reducer", items R-28
(base), R-48 + R-54 (amendment), GD-7/GD-9/GD-10/GD-15/GD-21…GD-30, SD-1/SD-9/SD-11.

Reproduced the test gate's numbers independently: `tests/test_agents.py` rc 0,
`tests/test_reducer.py` rc 0, full suite 16 files → 14 pass, `test_mirror.py`
FAILED(3) and `test_sessions.py` FAILED(1). I checked the two red files are not
this sub-plan's: `test_sessions.py`'s failing arm rebuilds only
`sessions.scan(...).observations()` through `mirror.Mirror`, and `test_mirror.py`'s
three are lease/generation counters inside `mirror.py`. Neither touches
`agents.py`. Ownership is clean (only the four listed files, HEAD still
`579446e`, nothing committed), and GD-21 holds: `agents.py` contains no
`pymongo`/`bson` token and imports on bare stdlib.

The mapping half is, as far as I can attack it, correct: the `$min`/`$max`/
`$addToSet`/`$setOnInsert` algebra is order-free, `_only_ours` is a real wall,
`_plain_field` closes the dotted-path hazard on `fragmentTips.<uuid>`,
`tip_mark` genuinely makes the (count, uuid) pair win as a unit, and the
identity/tip split (D-1) is justified and read back into R-48's exact shape by
`fragments_of`. The attempt-3 findings do look closed. **The reducer half is
not**: the freeze trigger reads a field that does not mean what the reducer
thinks it means, and the consequence is the exact defect R-54 exists to remove.

---

## BLOCKER 1 — `_run_is_terminal` treats `runs.endedAt` as "the harness said this run ended", but `ingest` derives `endedAt` from transcript *activity*, so **every live run is terminal** and every warm row renders `unknown — frozen at run close`

`aggregator/agents.py:2171-2178`

```python
def _run_is_terminal(doc) -> bool:
    if doc.get("endedAt") is not None:
        return True
    status = doc.get("status")
    return isinstance(status, str) and status not in ("running", "started", "async_launched")
```

`runs.endedAt` is not a terminal observation. `ingest._run_observation`
(`aggregator/ingest.py:1741-1750`) computes it as

```python
for node in nodes:
    if node.ended_at is not None and (ended is None or node.ended_at > ended):
        ended = node.ended_at
```

and `RunNodeObservation.ended_at` is `ended_at=last` (`aggregator/ingest.py:1683`)
— the **last record timestamp of the node's agent transcript**, per R-49's own
rule ("node `startedAt/endedAt` derived from the agent transcript's first/last
record timestamps, `now()` forbidden"). A running agent has a last record too.
So `runs.endedAt` is populated the moment *any* node of the run has *any*
transcript record, and `_run_is_terminal` answers `True` for every run that has
ever produced a line.

The freeze then fires on both populations (`agents.py:2307-2310` and
`:2356-2359`): a node/agent whose own transcript is warm and which `liveness()`
correctly called `RUNNING` is overwritten with
`Liveness(UNKNOWN, "frozen at run close", "unknown — frozen at run close")`.
With `tally[RUNNING]` thereby forced to 0, `closed = not tally[RUNNING]`
(`:2406`) closes the run as well, `reason = "terminal observation"`,
`label = "closed — no verdict"`.

**Demonstrated, not argued**, on the repo's own frozen live-run fixture —
`tests/fixtures/mirror/live-run-shape/…/workflows/wf_b297177a-d11`, which
`ingest.py:146` and `tests/test_ingest.py:1029` both describe as *the* specimen
of a run that is still running (no `<runId>.json` snapshot at all). Building the
state through the real `ingest`/`agents` mappers and reducing at
`now = max(lastTs) + 30s` (i.e. squarely inside the 180 s window):

```
run doc: status=None  endedAt=2026-07-25 16:03:42.258+00:00
_run_is_terminal -> True
agent states : {'done': 7, 'unknown': 2}
agent reasons: {'result observed': 7, 'idle 8m': 1, 'frozen at run close': 1}
run          : state=done closed=True reason='terminal observation'
               label='closed — no verdict'
```

With the `endedAt` clause removed (status-only terminal), the same bytes and the
same clock give the right answer:

```
agent states : {'done': 7, 'running': 1, 'unknown': 1}
run          : state=running closed=False reason='1 node(s) active'
```

Severity is blocker, not major, for three reasons:

1. It is **unconditional on the live path.** Not an edge case, not a rare
   ordering — the first transcript line of the first node arms it for the rest
   of the run, for every run, forever.
2. It is **the defect this sub-plan was written to remove, inverted.**
   R-54/LIVEFLOW-5/LIVEFLOW-6: "a live agent renders running", "no state is
   fabricated". This fabricates a *run close* out of an activity timestamp and
   then fabricates a per-row state from it. `"frozen at run close"` is worse
   than the old `running`-forever bug, because it is a confident explanation:
   the row looks accounted for.
3. It **poisons the one thing sp-12/sp-13 are forbidden to second-guess.**
   GD-23 makes this the only derivation site and binds `/api/*`, `/ws` and the
   page to serve its output verbatim; a wrong conclusion here cannot be
   corrected anywhere downstream by design.

**Fix.** Base the freeze on a signal that only a finished run produces. The
harness has exactly one: the run snapshot. `runs.status` is snapshot-only and
snapshot-only-exists-when-finished — I surveyed every `workflows/*.json` on this
machine and the value set is `{killed: 3, completed: 3, failed: 1}`, no live
value at all — so the minimal, entirely local fix is to drop the `endedAt`
clause and keep the `status` clause:

```python
def _run_is_terminal(doc) -> bool:
    """Did the harness itself say this run ended? (the freeze trigger)

    `endedAt` is deliberately NOT consulted: `ingest._run_observation` derives it
    as max(node.endedAt) and a node's endedAt is its agent transcript's LAST
    RECORD timestamp (R-49 — `now()` is forbidden there), which a *running*
    agent has as well. Reading it as a terminal makes every live run closed and
    freezes every warm row to "unknown — frozen at run close", which is
    LIVEFLOW-5's fabricated badge in a new spelling.
    """
    status = doc.get("status") if isinstance(doc, dict) else None
    return isinstance(status, str) and status not in ("running", "started", "async_launched")
```

If a second trigger is wanted (a snapshot whose `status` the harness omitted),
key it on a field only the snapshot writes — `harnessTotals`/`phases`/`summary`
are all snapshot-sourced in `ingest.map_run` — or ask sp-05/sp-06 for an
explicit `snapshotSeen` observation. What it may **not** be is `endedAt`.

The docstrings that describe the trigger must move with it: `reduce`'s
`:2241` ("is there a terminal observation? (the freeze trigger)") and the
module docstring's `freezePlan` paragraph (`:189-195`) both currently describe a
rule that fires on activity.

---

## MAJOR 2 — the reducer suite has no fixture that ever passed through the real mappers, which is precisely why BLOCKER 1 survived four attempts

`tests/test_reducer.py:92-124`, `:345-368`

Every state in `tests/test_reducer.py` is hand-built by `fanout()`. That helper
writes `runs[RUN] = {"_id": RUN, "startedAt": T0}` and adds `endedAt` **only**
when the caller asks for it (`ended=`), and writes `run_nodes[…]["endedAt"]`
**only** for nodes it has already decided are dead. Both are the opposite of
what `ingest.map_run`/`map_run_node` actually produce, so the suite's model of a
"live run" is a shape ingest cannot emit and its model of a "terminal run" is a
shape ingest emits for everything.

`test_freeze_to_stale_moved_into_the_reducer:350` then triggers the freeze with
`fanout(dead=5, ended=T0+3m)` and asserts it fires — i.e. the one test of the
freeze rule asserts the buggy trigger is the trigger. The `run_status=`
parameter `fanout` accepts (`:92`, `:102-103`) is never passed by any test in
the file, so the *correct* trigger is completely unexercised. `:366-368`
("no clock-only freeze") looks like the guard against this and is not: it varies
the clock while holding `endedAt` absent, which is the one combination ingest
never produces.

The gate's "non-tautological assertions" claim is right about attempt 3's
findings and wrong about this: an assertion can be perfectly non-tautological
and still be an assertion about a fixture the system cannot produce.

**Fix.** Add one arm that reduces a state built by the real mappers, and make it
the acceptance for BLOCKER 1. `tests/test_ingest.py:1029` already names the
fixture and `ingest.read_run` already returns the observations:

```python
def test_a_live_run_from_the_real_mappers_is_not_frozen():
    run_dir = FIX / "mirror" / "live-run-shape" / A8D4 / "subagents" / "workflows" / LIVE_RUN
    state = {}
    for kind, obs in ingest.read_run(str(run_dir)).observations():
        mapper = ingest.MIRROR_MAPPERS.get(kind)
        if mapper:
            ms.apply_operations(state, mapper(obs))
    for path in sorted(glob.glob(str(run_dir / "agent-*.jsonl"))):
        ms.apply_operations(state, map_agent(assemble([read_fragment(path, root=str(LIVE))])))
    run = state["runs"][refs.run_key(LIVE_RUN)]
    check(run.get("status") is None and run.get("endedAt") is not None,
          "the frozen LIVE-run fixture has no status and a non-null endedAt — "
          "endedAt is max(node transcript last-record ts), not a terminal")
    now = max(d["lastTs"] for d in state["agents"].values() if d.get("lastTs")) + 30s
    reduction = reduce(state, now=now)
    check(RUNNING in {d["state"] for d in reduction.nodes.values()},
          "a run with no snapshot keeps its warm node running")
    check(reduction.runs[refs.run_key(LIVE_RUN)]["closed"] is False, "…and does not close")
```

and re-point `test_freeze_to_stale_moved_into_the_reducer` at
`fanout(dead=5, run_status="killed")` (the value `wf_455b348c-e17`'s real
snapshot carries), keeping one arm proving `ended=` alone does **not** freeze.

---

## MINOR 3 — the module docstring says `description` is the `unconventional` fallback render; the only renderer in the file falls back to the `_id`

`aggregator/agents.py:368` and `:694-696` vs `:2322`

`META_FIELDS`' comment states "`description` is what R-28's `unconventional`
fallback renders when no `[touch] name=` exists", and `AgentObservation`'s
comment repeats it. The reducer — the single derivation site, and the only place
in this module that produces a render string — does
`"display": doc.get("name") or key`, so an unnamed agent renders its 17-hex
agentId and `description` is never consulted. The code matches R-28 ("unnamed
agents get `agentId` display + `unconventional` flag"); the prose does not, and
sp-13 reading this file will believe the prose.

**Fix:** either change the comments to "`description` is a stored column sp-13
may render beside the agentId; the reducer's own `display` fallback is the
agentId per R-28", or make `display` `doc.get("name") or doc.get("description")
or key` and say so in `reduce`'s docstring. Do not leave the two disagreeing.

---

## MINOR 4 — `counters["frozen"]` mixes the agent and node populations, which the adjacent comment gives as the reason no other counter does

`aggregator/agents.py:2273-2277`, `:2335-2336`, `:2380-2381`

The counter block's own comment says the state counters are population-prefixed
because "one `done` counter over agents AND nodes reads as a total and is not
one (an agent and its node are the same fact seen twice), so a reader comparing
it against `nodeCount` would find a discrepancy that is not there". `frozen` is
incremented in both loops and carries no prefix, so it is exactly that counter —
`tests/test_reducer.py:359` pins the value at 10 for a five-node/five-agent
fixture and explains it in the assertion message, which is a test compensating
for a name.

**Fix:** `agent_frozen` / `node_frozen` beside the existing prefixed states (and
update the two assertions), or keep `frozen` and add the population note to the
comment block so a `/health` consumer cannot read it as a row count.

---

## NIT 5 — `_max_seq` rescans two whole collections on every read for a value its own docstring calls informational

`aggregator/agents.py:2151-2168`, called unconditionally at `:2263` when
`derived_from_seq` is None (which sp-12's read path will be).

`reduce`'s cost is otherwise O(agents + nodes + runs); this one call makes it
O(|events| + |custom_state_events|), collections that grow for the life of the
installation. GD-30 budgets "aggregator reduce + WS push ≤ 50 ms" at a 250 ms
tick. Cheap fix: have the caller (sp-12/sp-06) carry the watermark forward — the
parameter already exists — or have the mirror maintain a running max, and say in
the docstring that omitting it is the rebuild path only.

## NIT 6 — `reduce` propagates `refs.RefError` out of the read path

`aggregator/agents.py:2196` (`_run_ref`) calls `refs.run_key` unguarded, unlike
its sibling `_raw_run_id` (`:2214-2220`), which funnels `RefError` to `None`
"rather than a guess". Verified:
`reduce({"agents": {…: {"runId": "x"*600}}}, now=…)` raises
`RefError: ref.runId is over the 512-char cap` and takes the whole reduction —
i.e. the whole live view — down for one bad document. `mongo_store` type-pins
`runId` as a string but puts no length pin on it (it is not an `_id`), so the
cap case is reachable. GD-22 wants the live view functional under adverse
storage; one unkeyable run should degrade to "no run join", counted, not to a
500.

## NIT 7 — `find_spawns` never emits a `SpawnObservation` for a running agent, so "jump to spawn" is unavailable for exactly the agents a user is looking at

`aggregator/agents.py:1320-1321`. A `tool_use` with no matching result is
counted `spawn_without_result` and dropped, which is correct and unavoidable
today (the agentId only appears on the result, and the document is keyed by it).
Worth one sentence in the deviation file's handoff so sp-12 does not plan a UI
affordance on a sub-document that is absent for every live Agent-tool agent —
the `agents` row does exist by then, via its own transcript fragment.

---

## What I checked and could not break

- GD-21: no `pymongo`/`bson` token anywhere in `agents.py`; `import aggregator.agents`
  succeeds on bare stdlib; `tests/test_stdlib_only.py` green.
- GD-22: no I/O in `reduce` (`test_the_reducer_is_pure_over_state_and_now`'s AST
  guard is real, and I re-derived it by hand); the reading half's `os.stat` calls
  are confined to `file_hint`/`check_file_hint`, neither reachable from `reduce`.
- GD-24: every `_id` on this module's write path comes from `node_key` →
  `refs.ref_key`; `_plain_field` refuses a dotted/`$`/NUL `fragmentTips` head;
  `Fragment.identity()` has fixed key order and every member is a property of the
  first record, so `$addToSet` cannot double an element under append (I re-ran
  the backdated-append case).
- GD-25: `merge_ops(collection="agents")` on both mappers; no `$inc`, no `$set`
  in the mapping half at all; the `$set` in `Reduction.operations` targets
  `derived`, which `mongo_store.SPECS` declares with no accumulables and
  `check_id` exempts by declaration — the legal case, and total-payload emission
  makes the wire and the memory model fingerprint-equal (verified).
- GD-26: no delete verb; the one bucket clear is `apply_derived`'s, confined to
  `DERIVED_COLLECTION`, which GD-23 declares droppable.
- GD-28: `PROVENANCE = "harness"` on the mapping half, `"derived"` on the
  reducer; both inside `SPECS[...].provenance`.
- GD-29: no client, no lease, no driver reference.
- GD-7/GD-9: markers label and never create; window is 4 physical lines with
  leading blanks skipped; `touch_marker_misplaced` requires a `key=value` payload
  so prose cannot trip it. I checked the empirical premise the marker layer rests
  on: of 156 agent transcripts under `~/.claude`, 12 begin with an `assistant`
  record (so `read_fragment`'s "first record, if `user`" rule sees no prompt), but
  **every one of the 142 distinct agentIds has at least one `user`-first
  fragment**, and `assemble`/`map_agent` merge labels across fragments — so the
  narrower-than-GD-9 rule is safe on today's corpus. Worth keeping in mind if a
  compaction ever removes an agent's opening record.
- SD-9: topology is read as a shape, no import of `custom_state`; absent arm is
  "attempt N", null `nextStage`, counted once per run.
- SD-11: `_run_ref`/`_raw_run_id` keep one key space (the escaping test with
  `wf_a%b#c` is a real test); `derived`'s `refId` is a `refs` key and is stored
  as a field.
- GD-15: `_only_ours` restricts to `agents`; the cross-module reach is to
  `ingest._transcript_walk`/`_cached_transcript`/`_in_scope`, which is reuse of
  one memo rather than a second path grammar — defensible and documented.
