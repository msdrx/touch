# sp-legacy-arm — adversarial critique, attempt 1

**Verdict: APPROVED** (0 blocker, 0 major, 4 minor, 5 nit) · depth: in-scope · critical_defect: false

Reviewed files (both new in an untracked tree, so full content reviewed, not a diff):

- `/home/laniakea/Projects/touch/aggregator/legacy.py` (1902 lines)
- `/home/laniakea/Projects/touch/tests/test_legacy.py` (1081 lines)

Against `plan/touch-mongo-live-subplans.md` §sp-09, amendment items R-51 / R-58 +
GD-14/GD-21…GD-30/SD-1/SD-4/SD-11, base-plan item R-27 + GD-14/GD-15.

---

## What I tried to break, and could not

I went after this arm on eleven fronts before settling on the findings below.
Recording the negatives because they are the load-bearing part of an approval:

1. **GD-21 dependency policy.** `legacy.py` imports only `datetime, hashlib,
   json, os, re, dataclasses` + `mongo_store, refs, sessions, store`. The string
   `pymongo` does not occur in the file. Re-ran the whole owned suite with
   `builtins.__import__` patched to raise `ImportError` for `pymongo*`:
   **green**. `tests/test_stdlib_only.py`: **green**.
2. **GD-24 identity.** `_id`s are strings only. `legacy_events._id` is
   `refs.legacy_event_key` ⇒ `legacy:<task>#<line:08d>`; I confirmed
   `ms.check_id("legacy_events", key) == key` and a `refs.parse_ref_key`
   round-trip on the hostile task name `touch#recon|v2:stage%1`. No BSON
   sub-document is ever used as `_id` or an equality key.
3. **GD-25 algebra.** `map_legacy_event` routes through
   `ms.merge_ops(..., collection="legacy_events")`, so `validate_update` runs:
   no `$inc`, no `$unset`, and the `$set` fence is satisfied because
   `legacy_events` declares no accumulable fields. `$setOnInsert` carries
   exactly `{provenance, task, lineNo}`, all functions of the (immutable) source
   line, so the payload cannot vary between two observations of one `_id`. I
   independently re-ran the shuffled / reversed / doubled fingerprint pass on
   `touch-full-recon`: identical.
4. **GD-26.** No delete verb, no `$unset`, no TTL anywhere in the module.
5. **GD-28 provenance.** The pin in `mongo_store` is `("derived","asserted",
   "unknown")` for `legacy_events` and `("asserted","touch")` for
   `custom_state_events`; `provenance_of` can emit only the first three and
   `map_artifact` only `"touch"`. There is no code path to `"harness"`. The
   ordering (`agent|tokens` before `title`) matches GD-28 verbatim, and the
   `w`-wins arm is a superset that never invents a fifth value — I checked
   `w:"future"` falls back to the shape rules.
6. **GD-29 / GD-22 / GD-30.** No client, no socket, no clock, no poll-loop
   participation. The AST guard in the test proves the "no clock at all" claim
   both by call and by import, which is the right pair of assertions.
7. **SD-4 read-time half.** Verified directly against the frozen bytes:
   `touch-full-recon` research `failed@255 → done@276`, `touch-mongo-live`
   research `failed@275 → done@286` both render `done` with the beaten terminal
   retained in `plan.conflicting`; `touch-mongo-live` divide `failed@319` with no
   correction relabels to `closed — no verdict`; and `touch-repo-recon`'s
   *genuine* `failed@101/102` ("stopped by user before completion") survive —
   honesty in both directions, which is the half that is easy to get wrong.
   The symmetry test (early `done`, later `failed` ⇒ `failed`) proves the rule is
   file order and not "done beats failed".
8. **`.watcher-state.json`.** Never read: the literal occurs exactly once (the
   exclusion constant), it is in `NEVER_REGISTERED`, the registry skips it in a
   folder walk, and a backfill handed its path directly gets `[]`. Three
   independent assertions — correct, since a digest of a file you must not read
   is still a read of it.
9. **SD-1 / GD-15 seam.** `mirror.discover_mappers()` finds both kinds with no
   change on mirror's side (I ran it). `_only_ours` structurally refuses any
   collection outside `("legacy_events","custom_state_events")`, so a
   synthesized 8-hex identity can never become an `agents`/`run_nodes` document.
   `MIRROR_SOURCES` matches the declared `source(path=None)` contract and decides
   ownership from the path alone (one `basename` comparison) — I confirmed both
   sources return `[]` for `~/.claude/projects/**` paths, which is what makes
   `iter_backfill_observations`' five-sources-per-file walk affordable.
10. **Non-tautology.** The anchors (`tests/fixtures/legacy/anchors.json`,
    mtime 16:06 Jul 25) predate this attempt and belong to sp-fixtures-freeze —
    the oracle is not self-authored. I re-derived the reduction of all four
    frozen streams by hand and the badge/node/stat numbers match what the tests
    assert.
11. **Ownership.** Only `aggregator/legacy.py` (12:45) and `tests/test_legacy.py`
    (12:51) carry mtimes in this attempt's window; every other `aggregator/*.py`
    and `tests/test_*.py` is older. No tracked file newly modified. `run_all.sh`
    globs `tests/test_*.py`, so no edit was needed there — correctly not made.

---

## Findings

### 1. minor — module docstring names a field path the mapper deliberately does not use
`aggregator/legacy.py:129`

The module docstring closes the artifact-registry section with:

> Ordering artifacts by `seq` is therefore meaningless — order them by
> `data.custom.path`, which is what a reader wants.

but `map_artifact` (`legacy.py:1744-1764`) argues at length for the opposite and
stores the path at **`artifact.path`**, explicitly *not* under `data.custom`
(which `mongo_store` `_raw`-wraps and would make unqueryable). The test at
`tests/test_legacy.py:808` asserts `set(doc["artifact"]) == {...}`, confirming
the implementation.

This is the one sentence in the module that a downstream consumer (sp-12's
`/api/*`, sp-13's artifacts strip) would copy verbatim into a query, and it names
a path that does not exist — the sort would silently do nothing. Two statements
in one file that contradict each other is also how the *next* implementer picks
the wrong one.

**Fix:** change line 129 to `order them by `artifact.path`` (and, since it is the
reader's only usable order, say so in the same breath as the "`seq` is
meaningless" clause).

### 2. minor — `artifact_stream()` can mint a stream id that `refs` later refuses, and the refusal escapes as `RefError`, not `LegacyError`
`aggregator/legacy.py:1436-1445`, surfacing at `legacy.py:1743`

`_stream_safe` percent-escapes everything outside `[A-Za-z0-9._+@=,-]`, but `.`
is in the keep set, so a task folder named e.g. `touch-v1..v2` yields the
perfectly-happy-looking `artifact:v1..v2`. `refs.escape_stream` (called inside
`refs.custom_state_event_key`) rejects any stream containing `..`:

```
>>> lg.artifact_stream("v1..v2")
'artifact:v1..v2'
>>> refs.custom_state_event_key(lg.artifact_stream("v1..v2"), 3)
RefError: stream id may not contain '..': 'artifact:v1..v2'
```

Two problems, both small but real:

* the validation happens one layer too late — `artifact_stream` is the declared
  keying entry point and its own docstring promises "the exact inverse" grammar,
  so it should be the thing that refuses (it already refuses over-long names with
  a `LegacyError`, so the fence exists, it is just incomplete);
* the escaping leak becomes a `refs.RefError` out of a `MIRROR_MAPPERS` callable.
  `Mirror.rebuild` counts that as a rejection, which by design makes the *whole*
  rebuild keep `derived` and set a sticky degrade (`mirror.py:2424-2434`) — one
  awkwardly-named history folder degrades the entire mirror rebuild, and the
  operator's error text names `refs`, not the legacy arm.

**Fix:** escape `.` too in `_STREAM_UNSAFE_RE` (and extend `_stream_unsafe`'s
round-trip test to cover `"v1..v2"` and a name ending in `.`), or — cheaper —
have `artifact_stream` validate its own output with a `try: refs.escape_stream(…)
except refs.RefError as exc: raise LegacyError(…) from None`. Add `"v1..v2"` to
the `test_the_artifact_stream_id_round_trips_any_folder_name` table.

### 3. minor — the artifact scan reads every byte of every file in a task folder with no deny-list, no symlink guard, no size cap
`aggregator/legacy.py:1480-1529` (`_digest`, `iter_artifacts`)

`os.walk` + `open(path,"rb")` over an arbitrary user-chosen directory. `mirror.py`
takes the opposite posture for its own walk and says why
(`iter_backfill_sources`: *"a credentials file is not redacted downstream, it is
never read"*, deny-list consulted **before** the extension filter). This module
has exactly one exclusion — `.watcher-state.json` — and no deny-list at all, so a
`.env`, a `credentials.json`, or a symlink pointing at `~/.claude/.credentials.json`
dropped into a task folder is opened and hashed in full.

Nothing leaks: only `{path, sha256, size, mtime}` is stored, never a body (the
test at `tests/test_legacy.py:814` proves that against the serialized state,
which is the right way to assert it). So this is defence-in-depth, not a
disclosure — but GD-27's stated rule is *never read*, and the module currently
reads.

**Fix:** fold `mirror.DENY_BASENAMES` (or a shared copy of it, to avoid the
`legacy → mirror` import cycle) into `NEVER_REGISTERED`; skip
`os.path.islink(path)` entries in the walk; and cap `_digest` at a documented
size, registering the file with a `truncated: true` marker beyond it rather than
streaming a multi-GB blob on every rebuild.

### 4. minor — `map_artifact` puts a non-content field in an insert-only payload, and is the one mapper that skips the `merge_ops` validation fence
`aggregator/legacy.py:1766-1780`

The `_id` slot is content-addressed on `(path, sha256)` — deliberately, and the
reasoning at `legacy.py:1457-1471` is right. But the `$setOnInsert` body also
carries `ts`/`tsRaw`/`artifact.mtime`, which are **not** part of the slot seed:

```
slot same: True   mtime differs: True   setOnInsert payload identical: False
```

(reproduced by `os.utime`-ing an unchanged file). That breaks the invariant
`op_set_on_insert`'s own docstring states — *"Every operation targeting a given
`_id` must carry the same `$setOnInsert` payload"* — and falsifies this module's
claim at `legacy.py:1768-1770` that the document is "a pure function of the file
(so a rebuild reproduces it byte for byte)". In practice: after a fresh clone
(all mtimes reset) the stored document silently keeps the *old* mtime, and a
cross-machine GD-25 fingerprint comparison of `custom_state_events` diverges even
though nothing changed. Within one ingest pass it is stable, which is why the
suite is green.

Secondary, same site: `map_artifact` returns `ms.op_set_on_insert(body)` raw,
where `map_legacy_event` returns `ms.merge_ops(..., collection=…)`. The GD-25
fence therefore does not run at map time for this mapper (it still runs later in
`apply_update`/`bulk_upsert`, so nothing escapes — but the two mappers in one
file behaving differently is the kind of asymmetry that gets copied).

**Fix:** either seed the slot with mtime as well (making the document genuinely
content-addressed over everything it stores), or — better, since R-51 mandates
both `mtime` and an insert-only posture — drop the "pure function of the file /
byte for byte" claim from the docstring and state the actual property: *the
document is a function of `(path, content)`; a re-stamped mtime keeps the first
observation's value.* Independently, wrap the return in
`ms.merge_ops(ms.op_set_on_insert(body), collection="custom_state_events")` so
both mappers are validated identically.

### 5. nit — a replayed spawn after a result mints a phantom second node; terminals are attributed by `(plan, stage)` without checking the agent id
`aggregator/legacy.py:1076-1077`, `legacy.py:1116-1134`

`fresh = (node is None or node.resulted or (agent_id and node.agent_id and
node.agent_id != agent_id))` — the `node.resulted` arm means a watcher restarted
onto a checkpoint that predates a result, replaying that agent's `running` row,
creates a *second* node for the same agent (which `_close_stale` will then close
`stale`). Symmetrically, the terminal branch attaches to `open_nodes[key]`
without comparing `event.agent_ref_id()` to `node.agent_id`, so interleaved
respawn waves on one `(plan, stage)` would credit wave 1's result to wave 2's
node.

Neither fires on the four frozen streams (I checked: zero multi-id nodes, zero
id-less nodes, node counts match the anchors), and `touch-repo-recon` is the only
two-wave specimen in existence, so this is speculative — but it is one condition
away from being closed.

**Fix:** add `and not (agent_id and agent_id in node.agent_ids)` to the `fresh`
predicate, and in the terminal branch prefer an already-created node whose
`agent_ids` contains the event's agent id over `open_nodes[key]`.

### 6. nit — dead conditional-expression statement that can only ever record an empty failure
`tests/test_legacy.py:540-541`

```python
for record in reduction.tokens:
    check(set(record.tokens) == set(lg.TOKEN_KEYS), "") \
        if set(record.tokens) != set(lg.TOKEN_KEYS) else None
```

The guard makes the condition always `False` when the body runs, so the only
reachable effect is `failures.append("")` — a failure with no message. Line 542
already asserts the same property properly.

**Fix:** delete lines 540-541.

### 7. nit — `LegacyEvent.is_node_event` is unused
`aggregator/legacy.py:495-504`

`reduce_events` re-implements the predicate inline (`is_token` / `is_badge` /
`plan == RESERVED_PLAN`). The property is public (the dataclass is exported), so
it is now a second definition of "is this a node row" that nothing keeps honest.

**Fix:** either use it in `reduce_events` or drop it.

### 8. nit — ts-less quiet token lines all collapse into one window
`aggregator/legacy.py:1326-1329`

`slot = None` when `event.ts is None`, so **every** unparseable-ts quiet delta
for one agent folds into a single `(agent_id, None)` bucket regardless of
position in the file. Still lossless (the last one carries the cumulative
total), but "at most one record per agent per throttle window" quietly becomes
"one record per agent, full stop" for that arm, and it is undocumented.

**Fix:** one comment at the `slot = None` line stating the degenerate window, or
fall back to `line_no // N` so ordering still segments the records.

### 9. nit — the duplicate-terminal anchors carry line pairs, the test only counts them
`tests/test_legacy.py:492-494`

`anchors.json` records the exact pairs (`[241,244], [247,256], …`) but the
assertion is `stats["deduped_terminals"] == len(pairs)`. A rule change that
deduped a *different* seven pairs would pass. (`deduped_terminals` also sums the
plan-card and node dedup paths into one counter, which is why the count happens
to line up.)

**Fix:** record the deduped `(first, second)` line pairs in `stats` (or on the
node/plan) and assert set equality against the anchor.

---

## Scope / depth

Everything above is a comment edit, a predicate tweak, a two-line deletion and a
deny-list constant, all inside the two owned files. No architectural rework, no
cross-sub-plan boundary, no missing research ⇒ **in-scope**, and since there are
no blocker/major findings the arm should proceed as-is; the nine items are best
folded into whatever pass next touches this file.

`critical_defect: false` — nothing here would waste or corrupt the remaining
sub-plans. sp-10 (`agents.py`, the reducer) consumes this module's reduction in
memory and its contract (derived state stays out of `legacy_events`, enforced by
`_only_ours`) is sound; sp-11 (`custom_state.py`) inherits the `artifact` vs
`legacyArtifact` kind split, which this module got right and documented.
