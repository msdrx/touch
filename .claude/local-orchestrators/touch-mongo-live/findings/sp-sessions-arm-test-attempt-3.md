# sp-sessions-arm — test gate, attempt 3

**Verdict: PASS.** Targeted suite green, full-suite regression green, ownership
clean, and the attempt-2 major is verified fixed by re-running the critique's
own reproduction script plus two independent mutation probes.

Owned files (per `plan/touch-mongo-live-subplans.md` §"sp-07 — sessions-arm"):
`aggregator/sessions.py`, `tests/test_sessions.py` — exactly the two the
implementer reported changing.

---

## 1. Targeted suite

```
$ cd /home/laniakea/Projects/touch && python3 tests/test_sessions.py
… 203 `ok:` lines, 0 FAIL
skipped: live Mongo arm: TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)
all sessions tests passed
exit 0
```

* 29 `def test_*` in the file, 29 referenced from `main()` — no orphaned test.
* 190 `check(...)` call sites.
* The one skip is the R-42/R-56 live-mongod arm, skipping *cleanly* with no
  `TOUCH_MONGO_URI` — the required no-mongod behaviour, not a silent hole.

## 2. Full-suite regression gate

```
$ cd /home/laniakea/Projects/touch && rc=0; for t in .claude/shared/monitoring/tests/test_*.py; \
    do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done; \
    for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc
PASS .claude/shared/monitoring/tests/test_frontend.py
PASS .claude/shared/monitoring/tests/test_server.py
PASS .claude/shared/monitoring/tests/test_shell.py
PASS .claude/shared/monitoring/tests/test_watcher.py
PASS tests/test_bootstrap.py
PASS tests/test_fixtures.py
PASS tests/test_mirror.py
PASS tests/test_mongo_deploy.py
PASS tests/test_mongo_store.py
PASS tests/test_refs.py
PASS tests/test_sessions.py
PASS tests/test_stdlib_only.py
PASS tests/test_store.py
PASS tests/test_tailer.py
PASS tests/test_ws.py
SUITE_RC=0
```

15/15 green, no services running, no `TOUCH_MONGO_URI`, `pymongo` present but
every Mongo arm self-skipping. Zero new failures; zero baseline failures either
(the four monitoring tests are green as the known baseline states).

## 3. Attempt-2 findings — verified, not taken on the implementer's word

**M1 (`_realpath` caught `OSError` only; a NUL in `history.jsonl` `project` or a
registry `cwd` killed the whole discovery pass, incl. the wired
`mirror.iter_rebuild_observations` path).** Fixed at `aggregator/sessions.py:372-375`
— `except (OSError, ValueError)`, `# pragma: no cover` dropped, and the docstring
now explains the escape route. Re-ran the critique's own four-arm reproduction
script unchanged:

```
$ python3 .claude/local-orchestrators/touch-mongo-live/findings/sp-sessions-arm-critique-attempt-2-probe_nul.py
A history NUL:  OK 7      (was: SCAN CRASHED ValueError)
B registry NUL: OK 7      (was: SCAN CRASHED ValueError)
C long path:    OK 7      (unchanged control)
D wired rebuild:OK 7      (was: CRASHED ValueError)   ← the arm that mattered
```

**m1 (empty/comment-only `.session-aliases` miscounted `alias_unreadable`)** —
`read_aliases` now counts unreadable only when the file could not be read at all
or announces itself as JSON and is not a JSON array (`:403-436`); the
"produced no entries ⇒ unreadable" test is gone.

**m2 (memo dropped the skip counters on the `--backfill` seam)** — `_scope_entry`
(`:519-540`) now memoizes `(slugs, dirs, counts)` together and `scope_skips()`
(`:574`) returns a copy, so reading twice cannot double-count. Asserted by the
suite: *"a rejected alias entry seen ONLY through the per-path seam is still
counted, though a backfill never builds a `Scan`: 2"* and *"reading the counters
twice reports the same numbers, not double"*.

**m3 (root-relative path invariant enforced only at scan sites, never at the
mapping boundary)** — `Source.as_element` (`:893-905`) now rejects a non-str /
empty / absolute / backslash / NUL-bearing / `..`-segment path with
`SessionsError`, with the fingerprint rationale in the docstring. The suite drives
all five arms through both the dataclass and the replayed-dict path, and keeps two
negative controls (root-relative POSIX passes; `..` *inside* a segment is a legal
directory name).

**Nits:** n5 (rooted ownership test) closed via `scoped_dirs` + the *"a lookalike
directory outside `<root>/projects` is not owned"* assertion. n7 (unbounded
`_SCOPE_CACHE`) closed via `MAX_SCOPE_KEYS`, asserted at *"the scope memo is capped
at 4 keys"*. n1 (`-> bool` annotation) closed. n4 survives — four `check(True, …)`
stand-ins remain (`tests/test_sessions.py:322`, `:381`, `:863`, `:1363`); they still
fail the run via an escaping traceback, so this is cosmetic and stays a nit, not a
gate failure.

## 4. Anti-tautology evidence (mutation probes, run on throwaway copies in the
scratchpad — the repo tree was never modified)

| mutation | result |
|---|---|
| revert `except (OSError, ValueError)` → `except OSError` in `_realpath` | suite dies with `ValueError: lstat: embedded null character in path`, exit 1 |
| neuter the `as_element` path-shape guard to `if False:` | `FAIL: a absolute source path is refused at the mapping boundary` + `FAIL: …(replayed dict)`, exit 1 |

Both fixes are covered by tests that actually fail when the fix is removed — the
assertions are behavioural, not restatements of the implementation.

## 5. Ownership / plan conformance

* `git log --oneline -3` unchanged (`579446e` head) — **no commits made**.
* `git status --porcelain` identical to the pre-run snapshot: the only `.claude/`
  and `.temp-develop/` entries are the pre-existing in-flight orchestrator state;
  `aggregator/` and `tests/` remain whole-directory untracked from the prior pass.
* mtimes confirm the blast radius: `aggregator/sessions.py` (04:10) and
  `tests/test_sessions.py` (04:14) are the only files under `aggregator/` or
  `tests/` written this attempt; `mirror.py` (02:45), `mongo_store.py`, `refs.py`,
  `store.py`, `tailer.py`, `ws.py` and every other test file are untouched.
* R-25/R-46 surface present and exercised: `live:<pid>-<procStart>` /
  `hist:<sessionId>` `_id` grammars, `$addToSet sessionIds` + `promotedTo`
  promotion, the transcriptless seventh session as `sources: []`, discovery scoped
  to cwd slug + `.session-aliases` closure (never `projects/*`), history scoped on
  the `project` field rather than a slug guess, `provenance` `$setOnInsert`
  (GD-28), and `mirror.py` discovering `sessions` dynamically rather than importing
  it statically. The rebuild fingerprint reproduces byte-identically across a
  wipe + `--rebuild`.

## Failures

None.
