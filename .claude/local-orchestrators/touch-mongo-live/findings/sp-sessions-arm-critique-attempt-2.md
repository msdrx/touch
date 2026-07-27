# sp-sessions-arm — adversarial critique, attempt 2

**Verdict: REJECTED.** 1 major, 3 minor, 7 nits.
Reviewed: `aggregator/sessions.py` (new, 1209 lines) and `tests/test_sessions.py`
(new, 1090 lines) — both untracked, so full-file review — against
`plan/touch-mongo-live-subplans.md` §"sp-07 — sessions-arm", R-46 + GD-21…GD-30
of the amendment, R-25 of the base plan, and SD-1/SD-11/GD-15.

## Attempt-1 findings: all four land, and two of them land properly

Verified, not taken on the test gate's word:

* **M1 (absurd registry timestamp)** — `_epoch_ms` (`:536-565`) now returns
  `None` for `inf`/`NaN`/`1e18`/`10**19`/`-1`/a string, the conversion is inside
  one `try` catching `OverflowError, OSError, ValueError`, the refusal is counted
  on `skipped["registry_bad_timestamp"]` (`:629-632`), and
  `test_an_absurd_registry_timestamp_cannot_kill_the_pass` drives it through a
  real registry file with `Infinity`/`10**19` and asserts the pass completes with
  six documents and no fabricated timestamp. Real fix, real test.
* **M2 (`Prior` unwired)** — the "Not yet wired" docstring section (`:92-110`)
  names the seam, the two call sites and both inert features;
  `test_a_promotion_is_inert_on_the_wired_path` asserts the inertness *through*
  `mr.iter_rebuild_observations`, not by inspection alone. This is the right
  shape: a stated gap instead of a wrong green.
* **M3 (`/clear` claim)** — the docstring section is retitled "What is
  deliberately NOT joined" and now says the opposite of what it used to; the test
  is renamed and its second half drives `scan()` and asserts the pre-`/clear`
  session is *not* promoted and has no `promotedTo`. Honest.
* **m1 (`$setOnInsert` disagreement)** — `sources` is out of the immutables in
  both mappers; the comparison at `:783-786` is now unfiltered.

I also settled the one item the test gate flagged as prose-only, against the
running `mongo:7` container (7.0.39), because a permanent store's shape should
not rest on an unpinned assertion. **Both halves of the `:1066-1076` rationale
are true:**

```
$addToSet:{sources:{$each:[]}} + upsert  ->  {"_id":"k1","cls":"observed","sources":[]}
$setOnInsert:{sources:[]} + $addToSet:{sources:{$each:[{path:"p"}]}}
                                         ->  MongoServerError: Updating the path
                                             'sources' would create a conflict at 'sources'
```

So R-46's "transcriptless seventh ⇒ `sources: []`" really does survive a real
server, in both insert orders. Not a finding — recorded so the next reader does
not have to re-derive it.

Also verified clean: no `pymongo` string in the module (GD-21); no DB I/O
anywhere (GD-22); every `_id` is a `refs` string, both grammars declared on
`COLLECTIONS["sessions"].id_kinds`, no sub-document `_id` (GD-24); only
`$setOnInsert`/`$addToSet`/`$min`/`$max`/`$set`, `$set` confined to `cwd`,
`registry`, `promotedTo`, none of them in the collection's `accumulable` tuple
(GD-25); no delete verb, no `$unset`, no TTL, `present` is a field (GD-26); no
credential, URI or secret path (GD-27); `provenance:"harness"` `$setOnInsert` on
every document (GD-28); no client, no lease, no write path (GD-29). Ownership is
clean — `aggregator/sessions.py` and `tests/test_sessions.py` are the only two
`.py` files newer than the prior sub-plan's last write, `mirror.py` is untouched,
no commits. `tests/test_sessions.py` is now 0755 (attempt-1 m5). Full suite
`run_all.sh`: 15 passed, 0 failed, RC=0; `test_sessions.py` alone: 161 `ok`,
exit 0, 26 defs and 26 entries in `main()`.

---

## Major

### M1 — the third instance of the bug class this loop has already fixed twice: `_realpath` catches `OSError` only, so one NUL byte in `history.jsonl` or in a registry `cwd` kills the entire discovery pass (`aggregator/sessions.py:337-341`, reached from `:705`, `:734`, `:587`, `:615-616`)

```python
def _realpath(path):
    try:
        return os.path.realpath(os.fspath(path))
    except OSError:                                              # pragma: no cover
        return os.fspath(path)
```

`os.path.realpath` does not raise only `OSError`. `posixpath.realpath` wraps its
`os.lstat` calls in `except OSError`, and an embedded NUL raises **`ValueError`**
out of `lstat` — which the stdlib does not catch and neither does this helper:

```
>>> os.path.realpath("\x00")
ValueError: lstat: embedded null character in path
```

Two of this module's three untrusted string inputs flow straight into it:

* `read_history_sessions:734` — `_realpath(project)`, where `project` is a
  `str` taken verbatim from a `history.jsonl` line;
* `read_registry:615` — `_realpath(entry_cwd)`, where `entry_cwd` is a `str`
  taken verbatim from `~/.claude/sessions/<pid>.json`.

Reproduced against the frozen fixture tree (script:
`.claude/local-orchestrators/touch-mongo-live/findings/sp-sessions-arm-critique-attempt-2-probe_nul.py`, four arms):

```
A history NUL:    SCAN CRASHED: ValueError lstat: embedded null character in path
B registry NUL:   SCAN CRASHED: ValueError lstat: embedded null character in path
C long path:      OK 7                      # ENAMETOOLONG is an OSError — caught
D wired rebuild:  CRASHED: ValueError lstat: embedded null character in path
```

Arm D is the one that matters: it goes through
`mirror.iter_rebuild_observations(registry_modules=["sessions"])`, which calls
`source()` with **no** exception handler, so the blast radius is the whole
`--rebuild`/poll pass for *every* session, not the one bad line. Arm C is
included to show the guard is not useless — it is just incomplete.

This is not a new class of defect for this file. It is the *same* class, for the
third time:

* the alias-file NUL, already fixed and tested (`_SLUG_OK_RE` at `:279`, whose
  own comment says "an entry containing a NUL raised `ValueError: embedded null
  byte` out of `open()` and took the whole scan with it, which is a discovery
  pass killed by a file it was tolerating");
* the absurd registry timestamp, fixed this attempt (`_epoch_ms:563` catches
  `ValueError` explicitly, with a docstring explaining exactly this escape
  route);
* and this one, in the helper that exists *for* absorbing path failures, still
  carrying `# pragma: no cover` — a marker asserting the fallback is unreachable
  when it is reachable from two files on disk.

The module's contract is stated in bold twice and is unambiguous:
`SessionsError`'s docstring (`:214-215`) — *"Discovery never raises — every
unreadable thing on disk is counted and skipped"* — and the "Tolerated, never
fatal" section (`:149-158`), which even enumerates "a registry timestamp that is
out of range or not finite (`json.load` accepts bare `Infinity`)" while missing
the sibling case one field over.

**Fix (small, and the correct fallback is already written):**

```python
    except (OSError, ValueError):
        return os.fspath(path)
```

Drop the `# pragma: no cover`. Returning the raw string is the right answer for
both call sites: a NUL-bearing `project` then simply fails the `!= target`
comparison and the record is passed over, and a NUL-bearing registry `cwd` fails
both the realpath check and `slug_for(...) in slug_set`, so the entry is counted
as `registry_out_of_scope` — no new counter needed, though a distinct one would
be more honest. Test both arms end to end (a `history.jsonl` line and a
`sessions/15934.json` whose `cwd` carries `chr(0)`), asserting `scan()` completes
with the expected document count and the skip is counted — the same shape as
`test_an_absurd_registry_timestamp_cannot_kill_the_pass`, which is the model to
copy.

---

## Minor

### m1 — a well-formed *empty* `.session-aliases` is counted as unreadable (`sessions.py:399-400`)

```python
if not out and stripped and not rejected_here and skipped is not None:
    skipped["alias_unreadable"] += 1
```

A file whose whole content is `[]` — the obvious spelling of "this project has
no aliases", and what `recordSessionAlias` would leave behind after the last
alias was removed — parses fine, yields `entries == []`, rejects nothing, and is
counted as unreadable. Same for a file that is only comments
(`# written by recordSessionAlias\n`), which the test's own second format proves
is a shape this module expects to meet. The counters exist so *"we ingested
nothing"* and *"we ingested nothing because six files were unreadable"* stay
different answers (`:149-158`); a false positive here corrupts exactly that
distinction, in the direction that manufactures alarm.

**Fix:** track whether the file was *understood* rather than whether it produced
entries — e.g. set `parsed = True` when `json.loads` returned a list, or when at
least one non-blank, non-comment line was seen, and only count
`alias_unreadable` when `not parsed`. Test `[]` and a comments-only file
explicitly.

### m2 — the new memo drops the counters on the floor (`sessions.py:478`)

```python
slugs = tuple(project_slugs(cwd, root))
```

`scoped_slugs` — the function attempt-1's m3 asked for, and the one the whole
per-path backfill seam now goes through — calls `project_slugs` with **no**
`skipped`. So on a `--backfill` (the mode that reads every `.jsonl` in the
corpus) every rejected alias entry, every unreadable alias file and every
`slug_cap` hit is silent. The module's counted-never-silent rule is applied to
`scan()` and to nothing else, and the newly added path is precisely where a
widened or truncated scope is hardest to notice.

**Fix:** give `scoped_slugs` an optional `skipped=` it forwards (cache the
slug tuple, not the counting), or memoize `(slugs, skips)` together and expose
the accumulated counts so `mirror.iter_backfill_observations`' caller can report
them. Assert in a test that a rejected alias entry seen only through the
per-path seam is still counted somewhere.

### m3 — the root-relative invariant that the fingerprint depends on is enforced at the scan site, never at the mapping boundary (`sessions.py:763-773`, `:1077-1078`)

`Source`'s docstring is explicit about why paths are root-relative: *"an absolute
path would make a rebuild's fingerprint depend on the home directory it ran in,
and GD-25's acceptance test compares fingerprints across passes"* (`:749-752`).
The only thing that makes it true is `_rel()` at the four `scan`/per-path
construction sites. `as_element` validates `kind` against `SOURCE_KINDS` and
nothing about `path`, and `map_session` explicitly accepts plain dicts for
replay/fixture use (`:1077`, tested at `test_source_elements_have_a_pinned_field_order`),
so `map_session({"session_id": …, "sources": [{"path": "/home/x/y.jsonl"}]})`
stores an absolute path and silently makes that document's fingerprint
machine-dependent. The module's stated contract for the mapping half is that a
malformed observation surfaces as `SessionsError` *before* a wrong value reaches
a permanent store (`:212-220`) — path shape is exactly such a value.

**Fix:** in `as_element`, refuse a `path` that is empty, starts with `/`, or
contains `\\` or a `..` segment, with a `SessionsError` naming the invariant;
add a test that a dict source with an absolute path is refused.

---

## Nits

* **n1** — `_usable_slug` is annotated `-> str` and returns `bool(...)`
  (`sessions.py:404`, `:413`).
* **n2** — `read_registry:637`'s `cwd=entry_cwd or (cwd or "")` fallback is
  unreachable from `scan`: when `target is not None` (always, from `scan`) an
  empty `entry_cwd` is already refused as out of scope at `:614-619`. Dead
  branch implying a tolerated case that is not tolerated.
* **n3** — the SD-1 purity check
  (`test_sessions.py:723-736`) inspects only *direct* calls inside four named
  functions. `map_session` reaches `_as_observation`, `Source.as_element` and
  `ms.merge_ops`, none of which are walked, so a helper doing I/O would pass.
  Cheap upgrade: resolve the module-local callees transitively (one level is
  enough here).
* **n4** — three `check(True, "…")` calls (`test_sessions.py:317`, `:369`,
  `:743`) stand in for "the loop above did not raise". They do fail the run (the
  traceback escapes `main`), but as an unhandled traceback rather than a `FAIL:`
  line in the report the file is otherwise careful to produce. Wrap in
  `try/except` and `check(False, …)`.
* **n5** — the per-path ownership test is
  `os.path.basename(os.path.dirname(path)) in slugs` (`sessions.py:1173-1175`),
  not a rooted prefix test: any path anywhere on disk whose *parent directory
  name* equals a scoped slug is claimed as owned. Harmless while only
  `mirror`'s rooted walk calls it; one `os.path.commonpath`-style check against
  `<root>/projects/<slug>` would close it at the same cost.
* **n6** — `scan()` re-reads all of `history.jsonl` and re-lists every slug
  directory on every call, and `:883` says it is "the one a live poll uses".
  33 KB and 5 directories today, so nowhere near GD-30's budget — but it is
  O(corpus) by construction where GD-30 asks for O(delta) ticks. Worth one
  sentence for the poll-loop owner (a `(st_ino, size)` checkpoint on
  `history.jsonl`, or a lower discovery cadence than the 250 ms tailer tick).
* **n7** — `_SCOPE_CACHE` (`:454`) is a module-global dict with no eviction. One
  entry in practice; a long-lived process serving several `(cwd, root)` pairs
  grows it forever. A two-entry bound or an explicit "last key wins" would cost
  nothing.

---

## Verdict fields

* **approved:** false — one major.
* **depth:** in-scope. M1 is a two-token change to one `except` clause plus a
  test; m1–m3 and the nits are all inside these two files. Nothing here crosses
  a sub-plan boundary or wants new research. (The `Prior` wiring gap is real but
  is correctly *recorded* rather than fixed here — it belongs to whoever owns the
  poll loop and the `mirror` handle, and this attempt hands it over properly.)
* **critical_defect:** false. Nothing here corrupts or wastes the remaining
  sub-plans; `sessions.py`'s schema, `_id` grammar and upsert algebra are sound
  and verified against a real mongod, so sp-08…sp-11 can be built on top of them
  while this fix lands.
