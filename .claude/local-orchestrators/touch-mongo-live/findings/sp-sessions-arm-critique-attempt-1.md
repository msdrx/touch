# sp-sessions-arm — adversarial critique, attempt 1

**Verdict: REJECTED.** 3 blocker/major, 5 minor, 4 nit.
Reviewed: `aggregator/sessions.py` (new, 1079 lines), `tests/test_sessions.py`
(new, 814 lines) — both untracked, so full-file review — against
`plan/touch-mongo-live-subplans.md` §"sp-07 — sessions-arm", R-46 + GD-24/GD-25/
GD-26/GD-28 of the amendment, R-25 of the base plan, and SD-1/SD-11.

What is genuinely good, stated once so the findings are not read as a verdict on
the whole file: the scope rule really does exclude the four frozen foreign slug
dirs and the negative test proves it against the naive `projects/*/*.jsonl`
enumerator (8 leaked ids avoided); `read_proc_start` parses past the last `)` and
the fixture `comm` contains both a space and a paren, so the assertion is not
decoration; the pid-reuse arm is real; `history.jsonl` prompt text is proven
absent from the emitted documents; `_only_sessions` enforces SESSIONJSONL-3
structurally; the `$setOnInsert`/`$addToSet`/`$min`/`$max` algebra is inside
GD-25 and the shuffled/reversed fingerprint test is a real equivalence test; no
pymongo, no delete verb, no `$unset`, no TTL, no `$inc`, no credential, no
sub-document `_id`, no write outside the sub-plan's two owned files (mtimes:
`sessions.py` 03:21, `test_sessions.py` 03:23; `mirror.py` untouched at 02:45).

---

## Blocker / major

### M1 — `_epoch_ms` raises on an absurd registry timestamp, and it kills the whole discovery pass (`aggregator/sessions.py:446-460`, reached from `:527-528`)

`_epoch_ms` guards the *type* of `startedAt`/`updatedAt` but not its *range*:

```python
millis = int(value)                     # OverflowError on float('inf')
...
datetime.datetime.fromtimestamp(millis / 1000.0, tz=...)   # ValueError past year 9999
```

Reproduced against the frozen fixture entry with one field edited:

```
$ # tests/fixtures/mirror/discovery/sessions/15934.json with "startedAt": 1e18
SCAN CRASHED: ValueError year 31690708 is out of range
$ # …and with "startedAt": Infinity (json.load accepts it by default)
SCAN CRASHED (Infinity): OverflowError cannot convert float infinity to integer
```

This is not a hypothetical shape: `read_registry`'s own `try/except` covers only
`json.load`, and the module's docstring makes the opposite promise in bold —
*"Discovery never raises — every unreadable thing on disk is counted and
skipped"* (`sessions.py:181-183`), *"Tolerated, never fatal"* (`:121-128`), and
R-25's test list requires tolerating malformed registry files. It is exactly the
class of bug the implementer already fixed once, for the NUL-bearing alias entry
(`:243-247`), and wrote a test for — the same reasoning was not applied one
function over.

Blast radius is the whole arm, not one entry: the exception escapes
`read_registry` → `scan` → `iter_session_observations` →
`mirror.iter_rebuild_observations` (`mirror.py:2585-2589` calls `source()` with
**no** exception handler), so one corrupt registry file takes down `--rebuild`
and every poll tick that calls the full scan, for every session, not just its
own. `_epoch_ms(0)`, `_epoch_ms("x")` and `_epoch_ms(True)` are tested
(`test_sessions.py:721-723`); the out-of-range arm is not tested at all.

**Fix:** wrap the conversion and return `None` on failure, and count it —

```python
    try:
        moment = datetime.datetime.fromtimestamp(millis / 1000.0, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
```

plus an `isinstance(value, float) and not math.isfinite(value)` guard before
`int(value)` (or simply move `int(value)` inside the same `try`). Add
`skipped["registry_bad_timestamp"]` so the refusal is visible per the module's
own counted-not-logged rule, and extend `test_backfill_observations_carry_no_
timestamp` (or a new case) with `1e18`, `float("inf")`, `float("nan")` and
`10**19`.

### M2 — R-46's promotion is unreachable through the only seam that exists: nothing in production can supply `Prior` (`sessions.py:707-725, 1011-1071`)

`Prior` is the sole gate on both `promotions` (`:814-815`) and GD-26's
`present:false` sources (`:850-863`). Grep of the whole tree: `Prior(` is
constructed **only** inside `tests/test_sessions.py`. The seam `mirror.py`
declares — and the only one that reaches these functions — is fixed at

```
def source(path=None) -> Iterable[observation]        # mirror.py:2481-2492
```

and both call sites pass nothing else: `iter_rebuild_observations` calls
`source()` (`mirror.py:2588`), `iter_backfill_observations` calls `source(path)`
(`mirror.py:2581`). `prior` is keyword-only, so it can never be passed. The
consequences, in production, today:

* `iter_promotion_observations()` always returns `[]` — R-46's *"promotion via
  `$addToSet sessionIds` + `promotedTo`"* never fires;
* no `sources[].present:false` element is ever written — GD-26's *"source
  disappearance is a field, never a removal"* is likewise dead code.

Both are nevertheless presented in the module docstring as operating behaviour
(`:62-82`: *"the promotion path exists because both orders really happen"*), and
the acceptance test buys its green by hand-constructing
`Prior(ids=frozenset(state["sessions"]))` (`test_sessions.py:398`) — i.e. the
test supplies the one input production cannot.

I accept that `sessions.py` cannot query Mongo (SD-1 purity, GD-29) and that
`mirror.py` is sp-06's file, so wiring is not this sub-plan's to do. What is
this sub-plan's to do is to **stop claiming it works and record the handoff**.

**Fix (in-scope, no other file touched):** (a) add a short, explicit
"Not yet wired" paragraph to the module docstring naming who must supply
`Prior` (the caller that owns a mirror handle) and that until then both features
are inert; (b) name the same gap in the sub-plan's findings file so the
sub-plan owning the poll loop / `mirror.py` seam inherits it as a task; (c) make
the promotion test say what it is — a unit test of the mapper contract, not
evidence of an end-to-end path — by asserting alongside it that
`iter_promotion_observations()` through `mirror.iter_sources` yields nothing
today. A wrong green is worse than a red.

### M3 — the `/clear` case the docstring promises is not implemented, and the test that "proves" it hand-builds an observation `scan()` can never emit (`sessions.py:67-76, 810-815`; `test_sessions.py:421-441`)

Docstring `:67-71`: *"`/clear` gives a running process a **new** sessionId and
rewrites its registry entry, so the process's previous sessionId becomes
historical while the process is still alive"* — followed by `:73-76` *"In both
cases the answer is the same … the live document gains the sessionId through
`$addToSet sessionIds`"*.

`scan()` only ever asks about the **current** registry sessionId:

```python
hist_key = refs.hist_session_key(entry.session_id)     # :813
if hist_key in prior.ids:
    promotions.append(PromotionObservation(entry.session_id, key))
```

There is no code path by which the *previous* sessionId is associated with the
live pid. Reproduced (registry names `a8d43bb1…`, prior mirror holds
`hist:292fc08c…`, both transcripts on disk):

```
promotions: ()
live sessionIds: ('a8d43bb1-0313-45d4-8784-4827af443ead',)
keys: ['live:15934-4101211', 'hist:292fc08c-923d-4ab4-8ff2-a9572417dbc8']
```

The pre-`/clear` session stays an unlinked `hist:` document forever. The
behaviour is arguably *correct* — CONVO-4 says nothing on disk proves two
sessions shared a process, and guessing would be worse — but then the docstring
must not say the opposite, and `test_a_process_that_cleared_accumulates_
session_ids` must not present the join as discovered fact. That test builds

```python
first  = SessionObservation(session_id=SIX[2], pid=PID, proc_start=PROC_START, …)
second = SessionObservation(session_id=LIVE_ID, pid=PID, proc_start=PROC_START, …)
```

by hand — two live observations on one pid with different sessionIds, a pair
`scan()` cannot produce from any tree — so what it verifies is `$addToSet`
merging, which `test_the_algebra_is_order_independent` already covers. As
written it reads as coverage of a discovery behaviour that does not exist.

**Fix:** either delete the second bullet of the docstring's promotion list and
retitle the test (`test_two_session_ids_on_one_live_id_merge_via_addToSet`,
docstring: "the mapper's algebra; `scan()` cannot currently produce this pair —
see the `/clear` gap"), or implement the join from evidence this module is
allowed to read and test it through `scan()`. Do not leave the claim standing.

---

## Minor

### m1 — `$setOnInsert` payloads for one `_id` disagree, and the assertion that would have caught it is filtered (`sessions.py:952-958`; `test_sessions.py:606-610`)

`map_session` puts `sources: []` into `$setOnInsert` for a transcriptless
session; `map_promotion` never puts `sources` in its payload. `op_set_on_insert`
states the invariant in bold: *"**Every** operation targeting a given `_id` must
carry the *same* `$setOnInsert` payload"* (`mongo_store.py:947-955`), and
`PromotionObservation`'s own docstring (`sessions.py:696-699`) claims the two
payloads are identical. They are not, for exactly the `hist:` id of a
transcriptless session — and if the promotion is the operation that creates the
document (a legal order; the test shuffles orders on purpose), the document is
created with no `sources` field, i.e. R-46's "present but empty" fact is lost.

The one test that checks the invariant papers over it:

```python
check(promo == {k: v for k, v in map_session(hist)[0][2]["$setOnInsert"].items()
                if k != "sources"}, …)
```

**Fix:** have `_identity_on_insert` own `sources: []` for the historical arm
unconditionally (both mappers then agree by construction), and drop the
`if k != "sources"` filter from the assertion.

### m2 — the alias-file counter is a cumulative total used as a per-call flag (`sessions.py:359-360`)

```python
if not out and stripped and skipped is not None and not skipped["alias_rejected"]:
    skipped["alias_unreadable"] += 1
```

`skipped` is the *scan-wide* counter set. Once any earlier alias file in the
transitive closure rejected one entry, every later unrecognised-format alias
file is silently not counted — defeating the module's own "counted, never
silent" rule (`:121-128`) precisely in the multi-slug case the closure exists
for. **Fix:** count rejections locally (`rejected_here = 0` inside the loop) and
test the local count.

### m3 — the per-path source re-derives the slug closure for every file, against `mirror`'s documented ownership contract (`sessions.py:1042`)

`mirror.iter_backfill_observations` states the rule in its docstring: *"the
ownership decision each one makes must be made from the path alone … Returning
`()` for a path you do not own is the whole contract, and it must cost one `str`
comparison"* (`mirror.py:2565-2572`). `iter_session_observations(path)` calls
`project_slugs(cwd, root)` on **every** invocation, which `open()`s
`<slug>/.session-aliases` once per slug in the closure — for every uuid-named
`.jsonl` in the whole corpus, owned or foreign, times five entity modules once
sp-08…sp-11 land. Small today (28 transcripts, ≤32 slugs); wrong by contract and
trivially fixed. **Fix:** memoize the closure on `(cwd, root)` in a module-level
dict (or resolve `parent` against `slug_for(cwd)`/`slug_for(realpath(cwd))`
first and only fall back to the alias closure on a miss).

### m4 — `_with_absent` docstring says `(path, kind)`, the code keys on `path` alone (`sessions.py:850-863`)

`here = {source.path for source in out}` — the kind is never part of the key, and
`_kind_for` derives it from the path anyway, so the docstring's claim that "a
source that came back is recorded present again and the two elements coexist" is
about a set the code does not build. Harmless today; a reader trusting the
comment will key a future source kind wrongly. **Fix:** either key on
`(path, kind)` for real or restate the docstring as "keyed by path".

### m5 — `tests/test_sessions.py` is not executable (mode 0644)

Every other file in `tests/` is 0755 and this one carries `#!/usr/bin/env
python3`. R-22/D12 is *"each file is executable and exits non-zero on failure"*;
`run_all.sh` masks the omission because it invokes `"$PY" "$(basename "$f")"`,
so nothing goes red. **Fix:** `chmod +x tests/test_sessions.py`.

---

## Nits

* **n1** — `read_alias_slugs:353` rebinds the function parameter `slug` inside
  the loop (`slug = slug_for(entry) if "/" in entry else entry`). It works only
  because `path` is computed before the loop; rename to `candidate`.
* **n2** — an alias entry of `/` slugifies to `-`, passes `_SLUG_OK_RE`, and
  widens scope to `projects/-`. Confirmed:
  `project_slugs` returned `['-home-laniakea-Projects-touch', '-b', '-']`.
  Harmless (no such directory) but a bare `-`/`--` is never a real slug — reject
  entries with no alphanumeric character.
* **n3** — `test_claude_root_agrees_with_mirrors:759` asserts on
  `mirror.py`'s raw text split on the literal `"ENTITY_MODULES"`. Any future
  comment mentioning "sessions" above that constant reddens a test about
  `claude_root`. Assert the absence of an `import` instead (AST, as the purity
  check already does).
* **n4** — a malformed `sources` element dict raises `TypeError`, not
  `SessionsError` (`sessions.py:949-950`): `Source(**source)` bypasses
  `_as_observation`. `mirror.Mapper` wraps any exception into `MapperError`
  (`mirror.py:759-760`) so nothing escapes, but the module's stated contract
  (`:180-188`) is that the mapping half surfaces `SessionsError`. Route it
  through `_as_observation(source, Source)`.

---

## Checklist items that pass (verified, not assumed)

GD-21 — no `pymongo` string anywhere in `sessions.py`; imports are `datetime,
json, os, re, dataclasses` + `mongo_store` + `refs`, both stdlib-clean at import
time. GD-22 — no DB I/O of any kind here. GD-24 — every `_id` is a string from
`refs.session_key`/`refs.hist_session_key`; no sub-document `_id`, no
equality-match sub-document key; `-` separator; both grammars declared on
`COLLECTIONS["sessions"].id_kinds`. GD-25 — only `$setOnInsert`/`$addToSet`/
`$set`/`$min`/`$max`; `$set` confined to `cwd` and the fixed `registry`
allowlist, both non-accumulable; `merge_ops(..., collection="sessions")`
validates at build time. GD-26 — no delete verb, no `$unset`, no TTL; `present`
is a field. GD-27 — no credential, no URI, no secret path; `history.jsonl`
prompt text proven absent from the documents. GD-28 — `provenance:"harness"` on
every document, `$setOnInsert`, never guessed. GD-29 — no client, no lease, no
write path. GD-15 — no transcript *content* is parsed here. Ownership — only the
two owned files changed; `MANIFEST.sha256` clean; no commits.
