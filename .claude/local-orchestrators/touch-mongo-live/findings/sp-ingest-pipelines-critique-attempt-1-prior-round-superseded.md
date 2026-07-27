# sp-ingest-pipelines — adversarial critique, attempt 1

**Verdict: REJECTED.** 1 blocker, 1 major, 6 minor, 3 nits.
**depth: in-scope** — every finding is fixable by one more gated attempt on
`aggregator/ingest.py` + `tests/test_ingest.py` (+ `tests/test_usage.py` if the
conflict-counter note is acted on). No architectural rework, no sub-plan
boundary crossing, no missing research.
**critical_defect: false** — the blocker is a scope check missing from one arm
of one module; the remaining sub-plans are not built on top of the wrong thing.

Reviewed: full content of the three owned files (untracked tree, so full-file
review, not `git diff`), against sp-08 in `touch-mongo-live-subplans.md`,
R-26/R-47/R-49/R-50 + GD-21…GD-30 in `touch-mongo-live-plan.md`, and R-26 in
`touch-full-recon-plan.md`.

## What is genuinely right (verified, not taken on trust)

I re-ran both suites (`tests/test_ingest.py` rc=0, `tests/test_usage.py` rc=0)
and independently re-derived the load-bearing claims rather than reading the
assertions:

- **Order independence on the real corpus, not just the fixtures.** I built
  every `run` + `runNode` observation from the live `~/.claude` (14 runs /
  105 nodes), applied them normally, reversed and shuffled, and the three
  fingerprints are identical. GD-25's property holds on data the fixtures do
  not contain.
- **No clock.** Confirmed by grep and by the `--backfill` mtime property: every
  stored `ts` predates its source file's mtime.
- **GD-21.** `ingest.py` names no driver; `mongo_store`'s pymongo import is
  lazy, so the module imports on bare stdlib.
- **GD-26.** No `deleteOne`/`deleteMany`/`drop`/`$unset`/`$inc`/
  `expireAfterSeconds` anywhere in the file.
- **GD-15 wall.** `_only_ours` is a real gate, not a comment; there is no code
  path emitting an `agents` or `sessions` operation.
- **R-50 `$setOnInsert` consistency.** I checked all 1 086 corpus usage
  observations for a `message.id` observed under two different
  `(sessionId, agentId, runId)` triples — zero. The `$setOnInsert` payload is
  genuinely invariant per `_id` on this corpus.
- **Tests are behavioral.** They recount from the raw bytes (`raw_lines`) and
  compare against the ingest, assert both-orders equality through the real
  mapper registry, and use negative arms (quoted marker, `..` traversal,
  orphan result, foreign slug). I found no tautology.

---

## BLOCKER

### B1 — the per-path (`--backfill`) source arm applies no project scope, so `--backfill` mirrors foreign slug directories that `--rebuild` correctly refuses

`aggregator/ingest.py:1948` (`_transcript_scans`) and `:1973` (`_run_scans`).

Both source arms diverge:

```python
def _transcript_scans(path, cwd, root, env):
    root = sess.claude_root(env) if root is None else os.fspath(root)
    if path is not None:
        if not is_transcript_path(path):     # <-- the ONLY ownership test
            return []
        return [_cached_transcript(path, root)]
    return [_cached_transcript(one, root) for one in iter_transcript_paths(root, cwd)]
```

`path=None` (the `--rebuild` arm) scopes correctly through
`sess.scoped_dirs(sess.project_cwd(cwd), root)`. `path=<file>` (the
`--backfill` arm) tests only the *basename grammar*, so any `<uuid>.jsonl`
anywhere under `~/.claude/projects/**` is ingested.

`mirror.iter_backfill_sources` walks the whole of `<root>/projects` with no
slug filter (that is its documented design: *"one-shot historical walk of
$TOUCH_CLAUDE_ROOT/projects"*), so nothing upstream compensates.

**Reproduced end-to-end through the real pipeline** — a root containing only
the foreign fixture slug `-tmp-claude-1000-liveio`:

```
$ python3 -c '... mirror.iter_backfill_observations(root=root, env=env) ...'
kinds emitted by --backfill over a FOREIGN-slug-only root:
  {'record': 46, 'streamMeta': 15, 'usage': 20}
```

versus the rebuild arm on the same root: `0` records.

Why this is a blocker and not a style point:

1. **R-25 as amended is violated** — "discovery scoped to cwd slug +
   `.session-aliases` slugs, **never `projects/*`**". Four foreign slug
   directories exist on this machine right now; the sub-plan's own fixture set
   froze them *as negative fixtures* (sp-02).
2. **GD-12 wrong-target write.** 81 permanent documents per foreign session get
   written into `touch_<sha1(this repo)>`. GD-26 forbids deleting them
   afterwards, so the mistake is not undoable by design.
3. **GD-13/GD-27 data minimisation.** Those are other projects' unredacted
   transcripts, entering the one database GD-27's whole loopback+auth posture
   exists to fence.
4. **`--rebuild` ≠ `--backfill`.** R-55's "wipe + rebuild equivalence"
   acceptance is unsatisfiable while the two arms disagree about which files
   exist.
5. **It leaves dangling documents.** `sessions.py:1310-1322` — the sibling
   entity module already landed — *does* apply the rooted `scoped_dirs` test in
   its per-path arm and returns `[]` for a foreign slug, documenting it as
   *"the contract's 'a source handed a path it does not own returns nothing'"*.
   So the backfill produces `records`/`stream_meta`/`usage` rows whose
   `sessionId` has no `sessions` document at all.

**Fix.** Apply the same rooted membership test `sessions.py` uses, in
`_transcript_scans` and `_run_scans`, before the memo:

```python
if path is not None:
    if not is_transcript_path(path):
        return []
    if not _in_scope(path, cwd, root, env):     # parent dir ∈ sess.scoped_dirs(...)
        return []
    return [_cached_transcript(path, root)]
```

Use `os.path.dirname(os.path.abspath(path))` membership in
`sess.scoped_dirs(sess.project_cwd(cwd, env), root)` for session transcripts,
and for agent transcripts / journals walk up to the `<slug>/<sessionId>` anchor
(`session_id_for_path` already finds it) and test *that* parent. Do not use a
basename-only test — `sessions.py:1319-1322` explains why the cheap version
stops scoping the moment the caller's walk is rooted elsewhere.

**Test to add.** The mirror image of the existing
`test_a_rebuild_through_mirror_reproduces_the_scan` foreign-slug arm
(`tests/test_ingest.py:960-971`), which today covers `path=None` only: build a
root with one owned slug and one foreign slug, drive
`mr.iter_backfill_observations`, and assert the emitted `(kind, path)` set
contains nothing under the foreign slug — and, stronger, that
`--backfill`'s document set equals `--rebuild`'s.

---

## MAJOR

### M1 — `spill_containment` is a basename test, not R-26's realpath containment, so `contained:true` is persisted for paths outside `~/.claude` entirely

`aggregator/ingest.py:517-529`.

R-26 (base plan, verbatim): *"the recorded path is agent-authored text —
realpath-contain under `~/.claude/projects/*/*/tool-results/` **only**"*.

The implementation checks one thing: that the normalized path's parent
directory is *named* `tool-results`. There is no root anchor and no `realpath`.

```
>>> spill_containment('/tmp/evil/tool-results/passwd.txt')
True
>>> spill_containment('tool-results/x.txt')
True
```

The `..` arm is handled (`normpath` first — good, and tested), but the actual
containment predicate R-26 specifies is absent, and
`tests/test_ingest.py:490-492` freezes the weakened rule as intended behaviour
(*"containment is the parent directory being tool-results/, nothing else"*),
so the deviation is now load-bearing on the test side too.

Nothing in `ingest.py` opens the file, so there is no traversal *here* — but
the boolean is **persisted** on the `records` document as
`persistedOutput.contained`, under a name that reads as "safe to open", and
`persistedOutput.path` is agent-authored text stored verbatim beside it. The
first consumer that treats the pair as a resolved, trusted location (the
sp-12 server's spill viewer is the obvious one) inherits an arbitrary-file-read
from a field this module vouched for. A security predicate that is weaker than
its name is exactly the defect class GD-27 was written to pre-empt.

**Fix.** Give the predicate the root it needs and make it decide what its name
claims:

```python
def spill_containment(path, *, root=None) -> bool:
    """True when realpath(path) is a file inside <root>/projects/*/*/tool-results/."""
```

`realpath` the candidate and the root, require the `tool-results` parent
*and* that the resolved path is under `<root>/projects/<slug>/<sessionId>/`
(`os.path.commonpath`, not `startswith`), and thread `root` through
`find_persisted_output(record, root=...)` ← `read_transcript(..., root=root)`,
which already has it. With no `root` available, return `False` and count it —
"unknown" must not read as "contained".

**Tests to change/add.** Replace the `:490-492` check with: a symlink escape
(`<session>/tool-results/x -> /etc/passwd`) ⇒ `False`; a same-named directory
outside the root (`/tmp/evil/tool-results/x`) ⇒ `False`; the three real frozen
spill pointers still ⇒ `True` (they must stay green — that is the regression
guard).

---

## MINOR

### m1 — `runs` routes launch-vs-snapshot contested scalars through `$set`, which is order-dependent by construction

`aggregator/ingest.py:1656-1685` (`_split_ops`), `:1780-1813` (`map_run`).

`_split_ops` sends everything that is neither `provenance` nor a declared
accumulator to `$set`. For `runs` that is `taskId`, `workflowName`,
`transcriptDir`, `scriptPath`, `status`, `summary`, `harnessTotals`, `phases` —
and `runs` is the **one** collection this module writes from two independent
sources (`_launch_scan` at `:1992` and `_run_observation` at `:1381`). Two
non-null disagreeing observations of one `runId` therefore give a
write-order-dependent document:

```
map_run(launch) then map_run(journal) -> summary = "(snapshot text)"
map_run(journal) then map_run(launch) -> summary = "(launch text)"
fingerprints equal: False
```

To be fair to the implementer, I could not make this fire on real data: I
parsed every launch `toolUseResult` in the live `~/.claude` (7 runs) and
compared each against its snapshot — `summary`, `workflowName`, `scriptPath`
and `taskId` agree on all 7, and `status` was deliberately left off
`_launch_scan` precisely to avoid the one clash that would exist. So this is a
latent violation of GD-25's *construction* rule, not an observed one; hence
minor. But the mitigation is undocumented luck (the CLI happens to copy the
workflow description into both), and `_launch_scan`'s omission of `status`
shows the hazard was seen and then only half-closed.

**Fix (cheapest).** Add a comment at `_launch_scan` naming the invariant
("every field emitted here must be one the snapshot cannot contradict; `status`
is excluded for that reason") **and** make it enforceable: either move
`taskId`/`workflowName`/`scriptPath`/`transcriptDir`/`summary` to
`$setOnInsert` on `runs` (they are immutable facts about the run) or namespace
the launch's copy (`launch{taskId, summary, …}`) so the two sources cannot
collide at all.

**Test to add.** The both-orders assertion that
`test_the_snapshot_backfills_without_clobbering:715-719` already does for
`run_nodes`, done for `runs` with a *deliberately disagreeing* launch and
snapshot pair.

### m2 — `tsRaw` is re-derived from the parsed datetime, so it is not "the source's own spelling" GD-11(g) asks for

`aggregator/ingest.py:1050-1058` (`_record_ts` returns `ms.ts_fields(raw)["ts"]`
and discards the string) → `:1708-1709` (`map_record` calls `ms.ts_fields(obs.ts)`
on the **datetime**, and `mongo_store.ts_fields:894-897` then synthesizes
`tsRaw` from it).

Lossless on the frozen corpus — every `timestamp` on disk is the single shape
`2026-07-25T14:14:59.374Z`, and I verified the round-trip is byte-identical for
it. But any other spelling the CLI might emit (`…:59Z`, `+00:00`, microsecond
precision) is silently normalized while the field still claims to be the
original, which is precisely what GD-11(g) pairs `ts` with `tsRaw` to prevent.

**Fix.** `_record_ts` returns `(ts, raw_string, error)`; carry `ts_raw` on
`RecordObservation`/`StreamMetaObservation`; `map_record`/`map_stream_meta`
store `{"ts": obs.ts, "tsRaw": obs.ts_raw}` directly. Test: a record whose
`timestamp` is `"2026-07-25T14:14:59Z"` stores that exact string in `tsRaw`
while `ts` is the truncated Date.

### m3 — R-50's agentId-conflict counter has no runtime path; nothing will ever increment it

`aggregator/ingest.py:1575-1591` (`usage_conflicts`).

The function is correct and well tested, but it is a pure function over a list
of observations and **no caller exists**: `grep` across `aggregator/` shows
`usage_conflicts` referenced only in `ingest.py` and `tests/test_usage.py`.
`mirror.py` maps observations one at a time and never accumulates them, so
R-50's *"if an incoming doc's agentId differs from the stored one, increment a
conflict counter"* is, in production, never incremented and never surfaced in
`/health`. The anomaly it exists to catch is silent.

Wiring the aggregate belongs to `mirror.py`/the server (not this sub-plan's
files), so the in-scope part is to make the count *travel*:

**Fix.** Add a `usage_agent_conflict` key to `_skips()` and have
`read_transcript` bump it when a `message.id` recurs within one scan under a
different `agentId`; add the cross-file case to the module docstring's
"Deliberately not stored" section as an explicit handoff to sp-12/sp-15 (with
`usage_conflicts` named as the function to call). A counter no code path can
raise should say so in the file that owns it.

### m4 — `link_spills` matches on basename alone, contradicting the `(sessionId, basename)` key it documents

`aggregator/ingest.py:1116-1134`.

`scan_tool_results`'s docstring and `Spill` both say the key is
`(sessionId, basename)` (SESSIONJSONL-14), but `link_spills` builds
`by_basename` with no session component, so pointers from session A can link
spills of session B. Harmless today (basenames are 9-char random ids and the
frozen corpus has one spill), but the invariant the code states is not the
invariant the code enforces.

**Fix.** Key the map on `(pointer_session, basename)` — the pointer's session
is the `TranscriptScan.session_id` of the record that carried it, so
`link_spills(spills, pointers)` needs pointers paired with their session, or
the whole call scoped to one session and asserted as such. Test: two sessions,
same basename, cross-link refused.

### m5 — the rebuild arm walks and re-reads the corpus several times over; the one-entry memo is sized for the backfill call pattern only

`aggregator/ingest.py:1866-1911` (memo), `:1973-1989` (`_run_scans`),
`:2014-2028`.

The memo comment is accurate for `iter_backfill_observations` (sources are
called consecutively for one path). It does not hold for
`iter_rebuild_observations`, which calls each source over the *whole* corpus
before moving to the next: the memo's single entry is always the previous
file's, so it never hits. Worse, `_run_scans(path=None)` is executed **twice**
(once by `iter_run_observations:2017`, once by `iter_run_node_observations:2028`),
and each `read_run(times=True)` reads every `agent-*.jsonl` in every run
directory of the run. A `--rebuild` therefore reads the transcript corpus on
the order of five to seven times.

Not a correctness bug and not on GD-30's latency path (rebuild is off-path),
but it is the difference between a rebuild that is I/O-bound-once and one that
is I/O-bound-seven-times on a corpus that is already >1 000 records per run.

**Fix.** Either raise the memo to a small LRU keyed on `_identity(path)`, or
have `iter_run_node_observations` reuse `_run_scans`' result via the same memo
(`_LAST_RUN` already keys on the journal path — the second full walk defeats
it, not the memo itself). A one-line note in the docstring that the memo serves
backfill only would at least stop the comment from being wrong.

### m6 — the "unkeyable positional" rule is a deviation from R-47 and is recorded only inside this module

`aggregator/ingest.py:885-913`, `:937-951`.

R-47 says *every* other type ⇒ `stream_meta` positional. This module instead
**does not mirror** uuid-less lines that live in a file which is not the
session's own transcript, because `stream_meta._id = <sessionId>#<line:08d>`
would alias the session transcript's line N with an agent transcript's line N.
The reasoning is correct, the counter (`unkeyable_positional`) is real, the
`Unkeyable` report carries everything a future fixture needs, and the corpus
cost is zero (asserted at `tests/test_ingest.py:392-400`).

But it is still a plan deviation whose only record is this file's docstring. If
a future CLI writes a `mode` line into an agent transcript, that line is
silently not in the store, and sp-14's acceptance will have no reason to look
for it.

**Fix.** No code change required. Write the deviation into the sub-plan's
findings handoff so sp-15 lands it in the docs, and state the amendment it
needs in one line: GD-24's `stream_meta` row wants
`<sessionId>#<fileDiscriminator>#<line:08d>` (or a `filePath` key member)
before those lines can be mirrored. Leaving an unratified table deviation
undocumented across a sub-plan boundary is how R-47 quietly becomes untrue.

---

## NITS

### n1 — a test reports itself as skipped and then runs all its assertions

`tests/test_ingest.py:518-576`. `test_the_launch_tool_use_result_is_the_taskid_join`
calls `skip(...)` at line 523 and then executes eleven `check()`s, all of which
pass. The suite footer therefore prints *"skipped: no frozen fixture carries a
launch toolUseResult"* for an arm that in fact ran and is green. The intent
(flagging that the *shape* is not fixture-proven) is right; the mechanism
misreports coverage. Use a `print("  note: …")` line, or keep `skip()` and
return, but not both.

### n2 — `_launch_scan` stores an absolute `source_path` where every other observation stores a root-relative one

`aggregator/ingest.py:2008` uses `source_path=scan.path`; everything else uses
`_rel(root, path)` (`:412-425`), whose whole documented purpose is that a
rebuild's fingerprint must not depend on the home directory it ran in. Harmless
today only because `map_run` does not store `source_path`. Use `_rel`.

### n3 — a journal `result` with no `key` is dropped without a counter

`aggregator/ingest.py:1185`: `elif kind == "result" and key is not None:` — a
`result` record lacking `key` falls through every branch and increments
nothing, so a lost verdict is invisible. I checked all five frozen journals:
every record carries both `key` and `agentId`, so this is unobservable today.
`skipped["unmatched_result"]` is the natural home; add an `else` that counts it.

---

## Checklist disposition

| item | verdict |
|---|---|
| GD-21 driver policy | PASS — no driver name in the file; imports clean on bare stdlib |
| GD-22 Mongo off the liveness path | PASS — no client, no blocking DB I/O |
| GD-24 string `_id`s via `refs` only | PASS — all five keys via `refs.*_key`; no sub-document `_id` |
| GD-25 upsert algebra | PASS for `usage`/`run_nodes`/accumulables; **m1** for `runs` `$set` scalars; no `$inc`, deltas wire-only |
| GD-26 no delete verbs / no TTL | PASS |
| GD-27 security posture | **M1** — containment predicate weaker than R-26; no credential anywhere |
| GD-28 provenance | PASS — `PROVENANCE="harness"`, `$setOnInsert`, no guessing |
| GD-29 no agent holds a client | PASS |
| GD-30 latency budget | PASS on the live path; **m5** on rebuild I/O |
| GD-15 one file one owner | PASS — mtimes show only the three declared files touched; `_only_ours` enforces it structurally |
| R-26 six amendments | PASS except the containment clause (**M1**) |
| R-47 bucket table | PASS except the unratified unkeyable rule (**m6**) |
| R-49 runs/run_nodes | PASS — ordinals 0,0,0,0,0,0,1,1,1 verified; `journalSeq` stored; no `state`; snapshot back-fill is `$min`/null-stripped |
| R-50 usage | PASS on the algebra; **m3** on the conflict counter having no runtime path |
| scope of edits | PASS |
| tests real, skip cleanly | PASS — behavioral, recount-from-bytes, negative arms; **n1** |
| **project scope (R-25 amended)** | **B1 — FAIL on the per-path arm** |
