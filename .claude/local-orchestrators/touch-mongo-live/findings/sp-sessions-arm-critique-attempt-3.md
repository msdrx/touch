# sp-sessions-arm — adversarial critique, attempt 3

**Verdict: APPROVED.** 0 blockers, 0 majors, 2 minors, 6 nits.
Reviewed: `aggregator/sessions.py` (new, 1357 lines) and `tests/test_sessions.py`
(new, 1435 lines) — both untracked, so full-file review — against
`plan/touch-mongo-live-subplans.md` §"sp-07 — sessions-arm", R-46 + GD-21…GD-30
of the amendment, R-25 of the base plan, and SD-1/SD-11/GD-15.

---

## Attempt-2 findings: all eleven land, and the major lands correctly

Verified by re-running my own attempt-2 probes and by **mutation testing**, not
by reading the diff or trusting the test gate. Method: an overlay tree at
`…/scratchpad/mut/` (symlinks to every other module + a writable copy of
`sessions.py` and `test_sessions.py`; `REPO` resolves to the overlay, so the
AST/grep guards read the mutant). Baseline RC=0. Each mutation below reverts one
attempt-3 fix; every one is caught:

| mutation | result |
|---|---|
| `_realpath` back to `except OSError:` | **RC=1** — `ValueError: lstat: embedded null character in path` out of `scan()` |
| `as_element` path guard → `if False:` | **11 FAIL** lines |
| `_scope_entry` drops `skipped=counts` | **3 FAIL** lines |
| empty `.session-aliases` counted `alias_unreadable` again | **3 FAIL** lines |
| rooted ownership → `basename(parent) in slugs` | **2 FAIL** lines |
| `MAX_SCOPE_KEYS` eviction removed | **2 FAIL** lines |

* **M1 (the NUL/`ValueError` hole)** — `_realpath:372-375` now catches
  `(OSError, ValueError)`, the `# pragma: no cover` is gone, and the docstring
  states the escape route rather than asserting unreachability. All four arms of
  my attempt-2 probe pass unchanged, including arm D through
  `mirror.iter_rebuild_observations` (7 observations, no crash). The registry
  half went further than I asked: `read_registry:715-722` refuses a NUL-bearing
  `cwd` *before* the realpath comparison so it counts as `registry_unusable`
  ("what it is") rather than `registry_out_of_scope`. That is the better fix.
* **m1 (empty alias file)** — `read_alias_slugs:425-427` returns early on empty
  text, the JSON arm counts only a non-list document, and the line arm counts
  nothing for blanks/comments. Four spellings (`[]`, comments-only, whitespace,
  blank lines) are asserted at `test_sessions.py:606-617` with
  `alias_unreadable == 0 and alias_rejected == 0`.
* **m2 (memo dropped the counters)** — `_scope_entry:519-540` memoizes
  `(slugs, dirs, counts)`, `scope_skips()` reads back a copy, and
  `test_sessions.py:703-722` asserts a rejection seen *only* through the
  per-path seam is counted, that reading twice is idempotent, and that
  `sum(skips.values()) == 2` (nothing else silently skipped).
* **m3 (path shape unchecked at the mapping boundary)** — `as_element:893-905`
  refuses empty / absolute / backslash / NUL / `..`-segment paths, and
  `test_a_source_path_must_be_root_relative` drives all six through **both**
  doors (`Source(...).as_element` and the replayed-dict path through
  `map_session`), plus the two true negatives (`a..b` inside a segment is legal,
  a normal root-relative path passes through).
* **n1/n2/n3/n5/n6/n7** — annotation fixed (`-> bool`), the dead
  `entry_cwd or (cwd or "")` fallback replaced by `cwd=entry_cwd` with the
  reason inline, the purity walk is now transitive with a self-check that it
  really reaches `as_element`, the ownership test is rooted against
  `scoped_dirs`, the O(corpus) cost is a docstring section addressed to the
  poll-loop owner, and `_SCOPE_CACHE` is bounded.
* **n4** — closed too, contrary to the test gate's report: the four `check(True, …)`
  calls at `:322`, `:381`, `:863`, `:1363` now sit inside `try/except` blocks
  whose handler is `check(False, …)`. (What survives is a *different*, smaller
  instance — see n1 below.)

Also re-verified clean, at the file level: no `pymongo` string (GD-21); no DB
I/O, no client, no lease (GD-22/GD-29); every `_id` from `refs.session_key` /
`refs.hist_session_key`, both declared on `COLLECTIONS["sessions"].id_kinds`,
no sub-document `_id` (GD-24); operators limited to
`$setOnInsert`/`$addToSet`/`$min`/`$max`/`$set`, with `$set` confined to `cwd`,
`registry`, `promotedTo` — none of them in the collection's `accumulable`
tuple, and `merge_ops(collection="sessions")` runs `validate_update` on every
emitted op (GD-25); no delete verb, no `$unset`, no TTL, `present` is a field
(GD-26); no credential or URI anywhere, and `ms.open_client`'s failure messages
(the strings `live_database` prints on a skip) do not embed the URI (GD-27);
`provenance:"harness"` `$setOnInsert` on every document (GD-28).
`iter_promotion_observations` and `iter_session_observations` still take `prior`
keyword-only, so the handoff gap is still asserted rather than quietly closed.

**Ownership and gates.** `HEAD` is still `579446e` (no commits). `git status`
shows no change outside `.claude/` and `.temp-develop/` pre-existing noise;
`aggregator/sessions.py` and `tests/test_sessions.py` are the only two files
under `aggregator/` or `tests/` with a new mtime. `python3 tests/test_sessions.py`
→ exit 0, live-mongod arm skipping cleanly on absent `TOUCH_MONGO_URI`.
`bash tests/run_all.sh` → **15 passed, 0 failed** in 33 s. `tests/test_sessions.py`
is 0755, 29 test functions, 29 entries in `main()`.

---

## Minor

### m1 — the "counted, never silent" contract is false in three places, and one of them is named in the docstring's own tolerated list (`sessions.py:154-159`, `:686-687`, `:788-789`, `:845`)

`SessionsError`'s docstring (`:229`) says *"every unreadable thing on disk is
counted and skipped"*, and the module's "Tolerated, never fatal" section
enumerates the cases — including, verbatim, **"a NUL byte inside a registry
`cwd` or a `history.jsonl` `project`"** — then asserts each *"is skipped … and
**counted** on `Scan.skipped`, so 'we ingested nothing' and 'we ingested nothing
because six files were unreadable' are different, visible answers."*
`_realpath`'s own docstring repeats it: *"the record is refused on its own
merits, at the site that knows which counter to bump."*

Three sites bump nothing. Measured, not inferred (probe:
`…/scratchpad/probe1.py`, four arms against a real temp `~/.claude`):

```
A history `project` NUL   -> skipped: {registry_unreadable:1, registry_not_json:1,
                                       history_bad_line:1}    # the fixture's own two
                                                              # + the fixture's bad line
                                                              # NOTHING for the NUL line
B <root>/sessions/ chmod 000 -> skipped: {history_bad_line:1}   live docs: []
C <root>/projects/<slug>/ chmod 000 -> 3 sessions (of 7), skipped: {alias_unreadable:1}
D <root>/history.jsonl chmod 000 -> skipped: {history_unreadable:1}    # correct
```

* **(a) `read_history_sessions:845`** — a NUL-bearing `project` falls through
  `project != cwd and _realpath(project) != target` as "some other project".
  The pass survives (that is M1's fix working), but the docstring's claim that
  this specific case is counted is untrue.
* **(b) `read_registry:686-687`** — `except OSError: return []` on
  `os.listdir(<root>/sessions)`. An unreadable registry directory silently
  demotes **every live session in the project to the `hist:` arm** with zero
  signal. No corruption follows (that is exactly the collision R-46's immutable
  `_id` + promotion pair is designed to absorb), but "no session is live" and
  "we could not read the registry" become the same answer — which is the
  distinction the counter set exists to preserve.
* **(c) `discover_transcripts:788-789`** — `except OSError: continue` on a slug
  directory. Arm C loses four of seven sessions, and the only trace is an
  incidental `alias_unreadable` (the `.session-aliases` `open()` failing with
  the same `EACCES`), which points a reader at the wrong file.

Not a major: nothing is corrupted, no wrong `_id` is written, and every arm
still completes. It is a documentation-versus-behaviour defect in the one
property this module argues for at length.

**Fix:** three counters and three one-line bumps —
`counts["history_bad_project"] += 1` in the `project`-mismatch branch when
`"\x00" in project` (or, simpler and more honest, refuse a NUL-bearing
`project` explicitly the way `read_registry:715` already refuses a NUL-bearing
`cwd`); `counts["registry_dir_unreadable"] += 1` before `return []`;
`counts["transcript_dir_unreadable"] += 1` before `continue`. Add the three keys
to `_skips()` so "nothing was skipped" stays printable, and assert them with the
`chmod 000` shape above (guard the test with `os.geteuid() != 0`, since root
ignores the mode). Then the docstring's list is true as written.

### m2 — a `--rebuild`/poll pass runs the full O(corpus) scan **twice**, and the docstring's cost note reports one (`sessions.py:1026-1033`, `:1305`, `:1350`)

`MIRROR_SOURCES` registers two callables, and `mirror.iter_rebuild_observations`
invokes *each registered source* with no arguments:

```python
for kind, source in iter_sources(...):
    for observation in source() or ():
```

Both of this module's sources call `scan()` — `iter_session_observations:1305`
and `iter_promotion_observations:1350` — so one rebuild pass performs two
complete discovery passes. Measured (probe:
`…/scratchpad/probe2.py`, patching `sess.scan` with a counter and driving
`mr.iter_rebuild_observations(registry_modules=["sessions"])`):

```
full scan() invocations per --rebuild pass: 2   observations: 7
```

The "Cost, for whoever wires the poll loop" section is the right instinct and is
the handoff a future reader will budget from — but it says *"every call re-lists
each slug directory and re-reads all of `history.jsonl`"* and leaves the reader
to conclude one pass per tick. It is two, against GD-30's O(delta) target, and
the two passes see the tree at different instants: a session created between
them appears as a `session` observation with no matching promotion, or (in the
other order) a promotion whose live session is not in the same batch. Harmless
today because promotions are inert without a `Prior`, which makes this exactly
the kind of thing that gets wired in later without being re-measured.

**Fix:** either say so in the cost section ("the wired seam calls `scan()` once
per registered kind — two full passes per rebuild tick — so the poll-loop owner
should scan once and split the result"), or give `scan()` a one-tick memo the
caller invalidates explicitly (`reset_scan_cache()`, mirroring
`reset_scope_cache()`), keyed on `(cwd, root, proc_root)`. A `Scan` already
carries both `sessions` and `promotions`, so the seam could also be collapsed by
having `iter_promotion_observations` consume a `Scan` the caller passes in — but
that changes `mirror.iter_sources`' declared signature and belongs to sp-06, so
the docstring sentence is the in-scope answer.

---

## Nits

* **n1** — the guard tests fail as **tracebacks**, not as `FAIL:` lines. With
  `_realpath` reverted to `except OSError:`, the run exits 1 (good) but prints a
  `ValueError` traceback out of `check(sess._realpath("\x00") == "\x00", …)`
  (`test_sessions.py:1095`) — the argument is evaluated before `check` is
  entered, so the file's careful `FAILED (n):` report never appears and a reader
  greping for `FAIL` sees zero. Same shape at `:1108`, `:1119`, `:1124`.
  `raises(...)`-style wrapping (or a `try/except` around the arm, as `:319-324`
  now does) makes the regression legible.
* **n2** — `MAX_SCOPE_KEYS` overflow **clears the whole memo**
  (`sessions.py:536-537`), which the docstring calls *"last keys win — a policy
  a reader can hold in their head"*. With five or more interleaved
  `(cwd, root)` pairs the access pattern thrashes to a ~0 % hit rate, i.e. the
  memo degrades to *no memo*, which is precisely the per-file
  `.session-aliases` re-read m2's fix exists to prevent. The test only asserts
  `len(_SCOPE_CACHE) <= MAX_SCOPE_KEYS` (it observes 1 after 13 inserts, which
  is the thrash). Popping the oldest key (`_SCOPE_CACHE.pop(next(iter(...)))`)
  is the same number of lines and degrades gracefully.
* **n3** — `RegistryEntry.live` (`sessions.py:631`) is always `True`: a
  non-live entry is `continue`d at `:729-731` and never constructed. A field
  that cannot be false invites a caller to branch on it.
* **n4** — `read_alias_slugs` counts `NotADirectoryError` / `IsADirectoryError`
  as `alias_unreadable` (`:419-424`). If `<root>/projects/<slug>` is a regular
  file — or `.session-aliases` is a directory — the count says "unreadable alias
  file" about a thing that is not one. Cheap: special-case those two under the
  `FileNotFoundError` arm, or add a distinct counter.
* **n5** — for the wiring owner, not this file:
  `mirror.iter_backfill_observations(root=…)` passes `root` to
  `iter_backfill_sources` but **not** to the sources it calls
  (`mirror.py:2578-2584`), while `iter_session_observations` resolves its root
  from `$TOUCH_CLAUDE_ROOT`. A caller that passes an explicit `root` differing
  from the environment gets `scoped_dirs` computed against the env root and
  therefore **zero** owned paths — a silent empty backfill. sessions.py behaves
  correctly against the declared seam; the mismatch is `mirror.py`'s (sp-06's
  file). Worth one line in whoever's handoff wires `--backfill`.
* **n6** — `as_element`'s new `"\\" in path` rejection (`:899`) turns a legal
  POSIX filename containing a backslash into a hard `SessionsError` for the
  **whole** session (the mapper rejects the observation, not the element). Only
  reachable via a registry file literally named `we\ird.json` that also parses,
  is in scope and is live, so this is theory — but the module's own rule is that
  a *disk* oddity is tolerated and a *caller* bug raises, and this one is on the
  disk side of that line.

---

## Verdict fields

* **approved:** true — zero blockers, zero majors. Both minors are
  observability/documentation defects with no effect on stored data, on any
  `_id`, or on the algebra; every attempt-2 finding is fixed and the fixes are
  mutation-verified rather than taken on the test gate's word.
* **depth:** in-scope. m1 is three counters and three one-line bumps; m2 is one
  docstring paragraph (or a memo with an explicit reset). Nothing crosses a
  sub-plan boundary or wants new research — n5 is recorded *for* the file that
  owns it rather than fixed here, which is correct.
* **critical_defect:** false. `sessions.py`'s tagged union, `_id` grammar,
  upsert algebra and `sources[]` shape are sound and confirmed against a real
  mongod by the (skipping) live arm; sp-08…sp-11 can be built on top of them as
  they stand.
