# sp-mirror-deploy — test gate, attempt 4

**Verdict: PASS.** 0 new failures, 0 ownership violations, 0 commits.
Owned suites 100 % green; full suite 14/14 green.

Environment: Python 3.13, pymongo 4.17.0 present, Docker daemon available
(`mongo:7` local), `TOUCH_MONGO_URI` unset.

Implementer's changed set (all four are sub-plan-owned):
`aggregator/mirror.py`, `docs/mongo.md`, `tests/test_mirror.py`,
`tests/test_mongo_deploy.py`.

---

## 1. Targeted suites (sp-mirror-deploy owned) — GREEN

Run from the repo root, standalone executables, stdlib only:

| suite | rc | assertions | skips |
|---|---|---|---|
| `tests/test_mirror.py` | **0** | 254 `ok:` | 2 |
| `tests/test_mongo_deploy.py` | **0** | 151 `ok:` | 0 |

Both skips in `test_mirror.py` are the designed conditional arms, not silent
holes:

- `SKIP: no entity module exists yet — SD-1's purity rule has nothing to check`
  (correct: `aggregator/{sessions,agents,…}.py` are sp-07…sp-11's, not on disk).
- `SKIP: live mirror arm: TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)`.

`tests/test_mongo_deploy.py` did **not** skip its live arm — it parsed the
`docker run` recipe out of `docs/mongo.md`, started that container verbatim, and
asserted against a real mongod. Evidence lines from the run:

```
ok: the documented `docker run` recipe starts, verbatim
ok: the running container publishes on loopback only: '27017/tcp -> 127.0.0.1:50381'
ok: …and on no other address
ok: an unauthenticated client cannot list databases / enumerate users / read / write (GD-27)
ok: the documented role/user bootstrap runs as written
ok: Touch reaches 'live' against the documented deployment, as the least-privilege user
ok: …with NO lastError on /health
ok: the SERVER refuses a delete on `records` … (GD-26)
ok: …while the ONE legal delete (renumbered positional stream_meta) is permitted
ok: --rebuild drops the reducer-owned collection AS THE DOCUMENTED ROLE
ok: …and the grant is scoped: the same role cannot drop `records` (GD-26)
```

Container and volume were torn down by the suite (no Mongo residue).

### Bare-checkout arm (GD-21 / R-56) — verified, not assumed

Re-ran both owned suites with a shadow `pymongo.py` on `PYTHONPATH` that raises
`ImportError` on import, simulating a checkout with no third-party packages:

```
PASS(no-pymongo) tests/test_mirror.py        (3 SKIP lines)
PASS(no-pymongo) tests/test_mongo_deploy.py  (1 SKIP line)
```

Both exit 0 and skip cleanly. GD-21's "absence is a state, never a startup
failure" holds at the suite level as well as inside `Mirror`.

## 2. Full-suite regression gate — GREEN (14/14)

```
PASS .claude/shared/monitoring/tests/test_frontend.py
PASS .claude/shared/monitoring/tests/test_server.py
PASS .claude/shared/monitoring/tests/test_shell.py
PASS .claude/shared/monitoring/tests/test_watcher.py
PASS tests/test_bootstrap.py   PASS tests/test_fixtures.py
PASS tests/test_mirror.py      PASS tests/test_mongo_deploy.py
PASS tests/test_mongo_store.py PASS tests/test_refs.py
PASS tests/test_stdlib_only.py PASS tests/test_store.py
PASS tests/test_tailer.py      PASS tests/test_ws.py
TOTAL_RC=0
```

`tests/run_all.sh` globs, so both owned suites are in the full run without
editing a file this sub-plan does not own.

### One transient failure, diagnosed and NOT attributable — resolved during the gate

The first full-suite pass had `.claude/shared/monitoring/tests/test_frontend.py`
failing:

```
File ".claude/shared/monitoring/tests/test_frontend.py", line 229, in main
    assert "done === plansSeen" in srender
AssertionError: STATS-2: idle + all plans green must fold to done
```

Diagnosis — **not this sub-plan's change**, and I proved it rather than assumed it:

- `test_frontend.py` opens exactly one file (`open(HTML…)` at line 31 is its only
  file read) — `.claude/shared/monitoring/monitor.html`. It never imports or
  reads anything under `aggregator/` or `tests/`, so no edit to `mirror.py`,
  `docs/mongo.md`, `test_mirror.py` or `test_mongo_deploy.py` can reach it.
- `monitor.html:1413` had been rewritten from `done === plansSeen` to
  `else if (plansSeen > 0 && done === plansAll) flow = "done";` — a monitoring
  UI change, unrelated to Mongo.
- mtimes showed a **concurrent Claude Code session** (pid 114274's command line
  names scratchpad `0ff58ac5-…`, a different session id) actively editing the
  monitoring module during this gate: `monitor.html` at 02:49:20, then
  `test_frontend.py` at 02:51:41, alongside `status.sh`, `monitoring.md` and the
  skill templates. `find -newermt 02:40` confirms the only repo files this
  sub-plan's work touched in that window are `aggregator/mirror.py`,
  `tests/test_mirror.py`, `tests/test_mongo_deploy.py`.
- The failure was the momentary half-landed state of *that* session's paired
  edit (HTML updated, test not yet). Once its `test_frontend.py` edit landed at
  02:51:41, the test passed on re-run and the full suite went 14/14. This is the
  "unrelated in-flight state under `.claude/`" the sub-plan brief tells this gate
  to leave alone; I did not touch either file.

## 3. Plan conformance

Checked against `plan/touch-mongo-live-subplans.md` §`sp-06 — mirror-deploy`,
`touch-mongo-live-plan.md` (GD-21…GD-30, R-42/R-45/R-57) and
`touch-full-recon-plan.md`.

Every owned item is present in the tree: `aggregator/mirror.py` (mirror runtime
— queue, breaker, lease, cursors, generation sweep, rebuild, backfill),
`docs/mongo.md` (deployment recipe, role bootstrap, `/health` block, growth and
retention numbers, `--rebuild`/`--backfill`, "Mongo down is a non-event", "never
publish 27017"), and both owned suites.

The attempt-3 critique's two majors and its minors are closed in code **and**
carry new behavioural tests (not tautologies — I read them):

- **MAJOR 1** (lease-path exception kills the drainer while `/health` says
  `live`): `aggregator/mirror.py:1709/1737/1821` now widen the guards;
  `mirror.py:2078` and `:2093` branch on `if not await self.acquire():`. New
  test `test_a_driver_surprise_on_the_lease_path_degrades_instead_of_killing_the_drainer`
  (`tests/test_mirror.py:430`) forces a due renewal, injects both real specimens
  (`RuntimeError("Cannot use AsyncMongoClient in different event loop")` and
  `ms.SchemaError`), and asserts `tick()` returns a report, `state` is
  `degraded|down`, `lastError` names the exception, and the mirror recovers.
  This test fails against the attempt-3 code — it is load-bearing.
- **MAJOR 2** (`ref` sub-document exempted from the GD-27 scrub while GD-24
  leaves an open tail): `scrub_op_update` (`mirror.py:968`) now delegates to
  `_scrub_ref` (`mirror.py:472`), and `tests/test_mirror.py:876-892` builds the
  hostile `kind:"unknown"` ref with `authToken`/`password`, confirms
  `refs.validate_ref` still classifies it `unknown`, and asserts the open tail
  is scrubbed while declared-kind refs stay byte-identical.
- **MINOR 1** covered by `test_a_renewal_that_failed_stops_the_tick_even_when_it_was_not_a_refusal`
  (`tests/test_mirror.py:531`).
- **MINOR 3**: `refused_policy` counter exists (`mirror.py:1612/1883`), is split
  from `refused_no_lease` (`mirror.py:1867`), is published on `/health`, and the
  `docs/mongo.md` counter list is asserted **equal in both directions** to
  `health()["counters"]` — the doc-drift hole the critique flagged is now closed
  (`ok: …and the list matches health()['counters'] exactly`).
- **MINOR 4**: the deny-list is now genuinely load-bearing in the backfill walk
  test — `ok: …and the deny-list is genuinely consulted, for the transcript as
  well as for the credential file: ['.credentials.json', 'notes.txt',
  'session.jsonl']` and `ok: …and a deny rule that DOES name a .jsonl file is
  honoured … []`.
- **NIT 1**: the minimum literal-redaction length is now documented and tested
  (`ok: the literal redaction pass has a documented minimum length` /
  `ok: …while the structural pass still removes a two-character password`).

Not closed (carried, non-blocking for this gate): **MINOR 2** — `_requeue` still
puts already-scrubbed ops back on the queue and the next drain re-scrubs them.
`grep -n _scrubbed aggregator/mirror.py` is empty. This is a performance /
docstring-accuracy issue (the scrub is idempotent, so the result stays correct),
not a behavioural failure, and it is the critique's own classification as a
minor. Flagging it for the critique gate rather than failing the test gate.

## 4. Ownership

`git log -1` is still `579446e orchestration history` — **nothing committed**.

`find . -newermt "2026-07-26 02:40" -type f` (excluding `__pycache__` and
`.git`) lists, from this sub-plan: `aggregator/mirror.py`,
`tests/test_mirror.py`, `tests/test_mongo_deploy.py` — plus `docs/mongo.md` at
02:39:10, also owned. Every other `aggregator/*.py` and `tests/*.py` carries an
mtime from the prior pass (19:41–23:36), including `aggregator/__init__.py`,
`aggregator/mongo_store.py`, `aggregator/refs.py` and `tests/run_all.sh`. The
remaining recent paths (`.claude/shared/monitoring/*`,
`.claude/skills/*/templates/*`, `.claude/local-orchestrators/…/events.jsonl`,
`.watcher-state.json`) belong to the concurrent monitoring session and the
running daemons, not to this sub-plan's implementer.

No edits outside the ownership list.

---

**Gate result: PASS** — 405 assertions green across the two owned suites
(254 + 151), 2 designed skips, full suite 14/14, bare-checkout arm verified,
live-mongod arm exercised for real.
