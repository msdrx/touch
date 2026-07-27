# sp-mirror-deploy — test gate, attempt 3

**Verdict: PASS.** Both owned suites green; the 14-file full-suite regression
green in all three environment arms (installed / bare-checkout / live `mongo:7`);
no ownership violation; no commit. There are no failures to attribute.

Implementer-declared changes: `aggregator/mirror.py`, `docs/mongo.md`,
`tests/test_mirror.py`, `tests/test_mongo_deploy.py` — exactly the four files
sub-plan §sp-06 owns.

Environment: Python 3.13.7, pymongo 4.17.0 (the single GD-21 exception),
Docker available (`mongo:7`).

---

## 1. Targeted suites (must be 100% green)

Run from the repo root as standalone executables.

| suite | rc | notes |
|---|---|---|
| `python3 tests/test_mirror.py` | 0 | 23 test functions; banner `all mirror (R-45) tests passed`; 2 clean skips (no entity module yet, no `TOUCH_MONGO_URI`) |
| `python3 tests/test_mongo_deploy.py` | 0 | real `mongo:7` container provisioned from the page's own recipe; banner `all mongo deployment (R-42 / R-57 mongo-doc) tests passed` |

## 2. Full-suite regression gate

The prescribed loop (four monitoring suites from their own dir, then every
`tests/test_*.py` from the repo root):

```
PASS .claude/…/test_frontend.py   PASS tests/test_mirror.py       PASS tests/test_stdlib_only.py
PASS .claude/…/test_server.py     PASS tests/test_mongo_deploy.py PASS tests/test_store.py
PASS .claude/…/test_shell.py      PASS tests/test_mongo_store.py  PASS tests/test_tailer.py
PASS .claude/…/test_watcher.py    PASS tests/test_refs.py         PASS tests/test_ws.py
PASS tests/test_bootstrap.py      PASS tests/test_fixtures.py
SUITE_RC=0
```

14/14. The four monitoring baselines are green as at baseline — no baseline
failure to discount, and no NEW failure anywhere.

### 2a. Bare-checkout arm (GD-21 / R-56) — the binding requirement

A fresh venv with **pymongo absent** plus a shadowed `PATH` (symlink farm of
`/usr/bin` + `/bin` minus `docker`, so `shutil.which("docker") is None`; `git`
still present, which the suite needs):

```
pymongo absent: True    which docker -> None    which git -> …/scratchpad/bare/bin2/git
BARE_RC=0   (14/14 PASS)
```

Every Mongo-dependent arm skips cleanly **and by name**:

- `test_mirror.py` — *"the dead-port arm needs pymongo to have a driver to time out (GD-21)"*, *"live mirror arm: TOUCH_MONGO_URI is not set (R-42's loopback+auth recipe)"*, *"no entity module exists yet"*
- `test_mongo_deploy.py` — *"live docker arm: docker is not installed"*

### 2b. Live-mongod arm — exercised, not merely skipped

A mongod was provisioned from the documented R-42 recipe verbatim
(`-p 127.0.0.1:27231:27017`, `--auth`, named volume, `mongo:7`) and
`TOUCH_MONGO_URI` set. `tests/test_mirror.py` live arm rc=0, 17 assertions
including the two that matter for this attempt:

```
ok: the mirror reaches 'live' against a real mongod
ok: replaying the mirror's own output against a REAL server changes nothing (GD-25)
ok: MemoryBackend and a real mongod produce the SAME fingerprint
ok: the sweep retracted rather than deleted, server-side
ok: a second writer is refused by the real conditional write (GD-29)
ok: --rebuild drops the reducer-owned collection at the server (GD-23)
ok: a mirrored ref resolves under GD-24's dot-notation join against a real mongod:
    {'kind': 'slot', 'sessionKey': '622-10028', 'root': 'r', 'name': 'n', 'attempt': 1}
ok: dropping only the database this test constructed: touch_test_110255 (GD-27/GD-12)
```

The repo suite re-run with `TOUCH_MONGO_URI` set: `LIVE_SUITE_RC=0` (10/10).

**Cleanup verified:** `list_database_names()` afterwards returned exactly
`admin config local` — no `touch_test_*` residue. The gate's own container and
volume were removed (`docker ps` back to only the unrelated `touch-mongo-sp05`).

`test_mongo_deploy.py` independently provisioned its own throwaway `mongo:7`
(random loopback port, `docker rm -f -v` + `volume rm` in `finally`) on
`127.0.0.1:55305` and proved the deployment end to end: the loopback-only publish
as the kernel reports it, four anonymous-access refusals, the documented
`createRole`/`createUser` bootstrap executed from the page's own text, the mirror
reaching `live` as the least-privilege user with `lastError: None`, the server
refusing `deleteMany` on `records` while permitting the one legal `stream_meta`
delete, and `dropCollection` permitted on `derived` but refused on `records`.

## 3. Plan verification

### Owned files present, items covered

`aggregator/mirror.py`, `docs/mongo.md`, `tests/test_mirror.py`,
`tests/test_mongo_deploy.py` — all four on disk; both test files 0755 with
shebangs (asserted by `test_the_owned_suites_are_executable_like_their_siblings`).
R-45 (queue/breaker/lease/cursors/sweep/rebuild/backfill), R-42 (mongo.json 0600,
loopback+auth recipe, user bootstrap, zero-users refusal, derived DB name) and
R-57:mongo-doc (rebuild/backfill commands, "Mongo down is a non-event", growth
table, never-publish-27017) all have live assertions, several of which parse the
doc text itself so a weakening doc edit reddens the suite.

### Attempt-2 critique items — all closed, each with a load-bearing assertion

- **MAJOR 1 (GD-27 backstop redacted GD-24 schema field names inside `ref`).**
  Closed twice over. The `ref` sub-document is now passed through untouched
  (`scrub_op_update` at `mirror.py:880`: `value if field == REF_FIELD else
  scrub_value(value)`), and the exemption set is **derived**, not hand-listed —
  `mr.SCHEMA_FIELD_NAMES` is built from `refs.KIND_SPECS` plus `mongo_store`'s
  declared per-collection types. Reproduced against the real code:
  `scrub_value({'sessionKey':…,'author':…,'stateKey':…,'apiToken':…})` now keeps
  the first three and redacts `apiToken`. `test_the_scrub_never_corrupts_a_schema_field_or_a_ref`
  asserts the property the critique asked for — `event["ref"]["sessionKey"] ==
  refs.parse_ref_key("slot", event["refId"])["sessionKey"]`, i.e. the two copies
  of the datum agree — plus the same for `customState`/`stateKey` and
  `custom_state_events.author`, and it keeps the backstop honest: `authToken` and
  a credential-shaped `key` inside `data.custom` are still `[redacted]` while
  `{'key': 'Enter'}` survives (the value-exempt rule is checked *before* the
  schema vocabulary, so `run_nodes.key` buys no unconditional exemption). The
  live arm proves the dot-notation join resolves against a real mongod.
- **MAJOR 2 (O(document) scrub on the poll-loop side, then again on the drainer
  side).** `validate_op` now takes `scrub=True` and is called `scrub=False` at
  the three loop-side seams (`Mapper.__call__` :721, `stamp_gen` :898,
  `stamp_backfill` :916); the scrub survives only in `_take_batches`, documented
  as *"the only place it runs, and the right one"*.
  `test_the_scrub_runs_once_per_operation_and_off_the_poll_loop` counts calls
  (0 on the loop side, exactly 8 for 8 operations overall) and asserts wall time
  machine-independently: the whole 8-operation loop-side pass must cost less than
  a **single** scrub of one of those half-megabyte operations, and less than
  `TICK_BUDGET/5`. A synchronous-but-slow regression fails it.
- **m1 (rebuild dropped `derived` on a failing-but-registered mapper).**
  `rebuild()` (:2127) now maps the **whole batch first**, computes `rejected` as
  a delta on `stats["rejected"]`, and drops `derived` only when that pass
  produced zero rejections; `rejected` is in the returned report and the reason
  is on `/health`. Asserted by `test_rebuild_survives_an_unmapped_kind_and_keeps_derived`.
- **m2 (a lost lease was terminal).** `_retake_at = monotonic() + lease_ttl` is
  set on refusal (:1706) and `tick()` retries `acquire()` at most once per TTL
  (:1907-1910). `test_a_lost_lease_is_retaken_once_the_previous_holder_expires`
  drives the whole lose → expire → re-acquire → writes-resume cycle.
- **n1** — `docs/mongo.md`'s `/health` field list now matches `health()`
  exactly; the test reports `documented-only [], undocumented []`.
- **n2** — `backfill()` memoizes: `if source not in mtimes: mtimes[source] = _mtime(...)` (:2236).
- **n3** — `iter_backfill_observations`'s docstring now states the ownership
  decision must be made *from the path alone* and "must cost one `str` comparison".
- **n4** — `MongoConfig.secrets` appends `urllib.parse.unquote(password)` when it
  differs from the literal (:632-634).

### Tests assert behavior, not tautologies

Spot-audited the two new tests in full and the changed ones by diff-of-behaviour.
Assertions are properties, not restatements: cross-copy agreement between `ref`
and `refId` (not "is not the redaction marker", which any string passes),
call **counts** and comparative wall-clock for the scrub placement, fingerprint
equality across a wipe and across two independent backends, server-side
authorization denials from a real mongod, and AST/source walls for the GD-26
verb prohibitions. `check()` accumulates into a `failures` list and `main()`
exits 1 naming them, so a red assertion genuinely reddens the file. The two
remaining skips in the installed env are legitimate and self-describing
(`no entity module exists yet` is exactly SD-1's expected state at sp-06).

### Ownership — clean

Only the four owned paths carry post-implementer mtimes:

```
07-26 02:14 aggregator/mirror.py    07-26 02:07 tests/test_mongo_deploy.py
07-26 02:10 docs/mongo.md           07-26 02:14 tests/test_mirror.py
```

`.gitignore` (07-25 15:37) and `CLAUDE.md` (07-25 22:03) predate this attempt —
sp-01/other-orchestrator state, untouched here. Every other `aggregator/` and
`tests/` file predates 07-26 02:00. `git log -1` is still `579446e` — no commit
was made.

## Observations (not gate failures, not this sub-plan's to fix)

1. **Leftover container `touch-mongo-sp05`** still holds `127.0.0.1:27117` five
   hours on. It is sp-05's, not sp-06's; this gate worked around it on port 27231
   and removed its own container and volume. Worth reaping before a future
   fixed-port gate.
2. A mongod started with an explicit `--bind_ip 127.0.0.1` inside the container
   is unreachable through a `-p 127.0.0.1:…` publish (connection reset). This is
   a docker-networking fact, not a defect — and `docs/mongo.md`'s recipe is
   correct as written (it relies on the publish for the loopback restriction and
   does **not** add `--bind_ip`). Noted only so a future gate does not "harden"
   the recipe into a broken one.

## Evidence summary

- Owned suites: **2/2 green**, 0 failures.
- Full suite: **14/14 green, rc=0** in the installed env, the bare env (no
  pymongo, no docker), and the live-mongod env.
- Both attempt-2 majors, both minors and all four nits closed, each with an
  assertion that fails if the fix is reverted.
- No new failures, no baseline regressions, no ownership violations, no commits,
  no Mongo residue.
