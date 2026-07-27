# sp-mirror-deploy — test gate, attempt 2

**Verdict: PASS.** Both owned suites green, the 14-file full-suite regression
green in all three environment arms (bare / installed / live `mongo:7`), no
ownership violation, no commit. Nothing to attribute — there are no failures.

Implementer-declared changes: `aggregator/mirror.py`, `docs/mongo.md`,
`tests/test_mirror.py`, `tests/test_mongo_deploy.py` — exactly the four files
sub-plan §sp-06 owns.

---

## 1. Targeted suites (must be 100% green)

Repo root, Python 3.13.7, standalone executables, stdlib + the GD-21 pymongo
exception (4.17.0 installed).

| suite | rc | notes |
|---|---|---|
| `python3 tests/test_mirror.py` | 0 | 19 test functions; banner `all mirror (R-45) tests passed` |
| `python3 tests/test_mongo_deploy.py` | 0 | 12 test functions incl. a real `mongo:7` container; banner `all mongo deployment (R-42 / R-57 mongo-doc) tests passed` |

## 2. Full-suite regression gate

The prescribed loop (four monitoring suites from their own dir, then every
`tests/test_*.py` from the root):

```
PASS .claude/…/test_frontend.py   PASS tests/test_mongo_deploy.py  PASS tests/test_store.py
PASS .claude/…/test_server.py     PASS tests/test_mongo_store.py   PASS tests/test_tailer.py
PASS .claude/…/test_shell.py      PASS tests/test_refs.py          PASS tests/test_ws.py
PASS .claude/…/test_watcher.py    PASS tests/test_stdlib_only.py
PASS tests/test_bootstrap.py      PASS tests/test_fixtures.py      PASS tests/test_mirror.py
SUITE_RC=0
```

14/14. The four monitoring baselines are green as at baseline — no baseline
failure to discount, and no NEW failure anywhere.

### 2a. Bare-checkout arm (GD-21 / R-56) — the binding requirement

Verified by construction, not by assertion: a fresh venv with **pymongo absent**
plus a shadowed `PATH` (symlink farm of `/usr/bin` minus `docker`, so
`shutil.which("docker") is None`; `git` still present, which the suite needs).

```
pymongo absent: OK      which docker -> None      which git -> …/scratch/bin/git
BARE_RC=0     (14/14 PASS)
```

Every Mongo-dependent arm skips **cleanly and by name**:

- `test_mirror.py` — *"the dead-port arm needs pymongo to have a driver to time out (GD-21)"*, *"live mirror arm: TOUCH_MONGO_URI is not set"*, *"no entity module exists yet"*
- `test_mongo_deploy.py` — *"live docker arm: docker is not installed"*

### 2b. Live-mongod arm — exercised, not merely skipped

A mongod was provisioned from the documented R-42 recipe (loopback bind,
`--auth`, named volume, `mongo:7`) on a free port (`127.0.0.1:27219`) and
`TOUCH_MONGO_URI` set. `tests/test_mirror.py` live arm, rc=0:

```
ok: the mirror reaches 'live' against a real mongod
ok: …holding the GD-29 writer lease
ok: a real bulk_write lands every document: {'records': 6, 'writers': 1}
ok: replaying the mirror's own output against a REAL server changes nothing (GD-25)
ok: MemoryBackend and a real mongod produce the SAME fingerprint
ok: the sweep retracted rather than deleted, server-side
ok: a second writer is refused by the real conditional write (GD-29)
ok: --rebuild drops the reducer-owned collection at the server (GD-23)
ok: …and the replay reproduces a byte-identical fingerprint against a real mongod
ok: dropping only the database this test constructed: touch_test_94877 (GD-27/GD-12)
```

The full 14-suite loop re-run with `TOUCH_MONGO_URI` set: `LIVE_SUITE_RC=0`.

**Cleanup verified:** `listDatabases` afterwards returned exactly
`admin config local` — no `touch_test_*` residue. The gate's own container and
volume were removed (`docker ps` back to only the unrelated `touch-mongo-sp05`).

`test_mongo_deploy.py` independently provisioned its own throwaway `mongo:7`
(random loopback port, `docker rm -f -v` + `volume rm` in `finally`) and proved
the deployment end to end: loopback-only publish as the kernel reports it, four
anonymous-access refusals, the documented `createRole`/`createUser` bootstrap
executed from the page's own text, the mirror reaching `live` as the
least-privilege user with **no `lastError`**, the server refusing `deleteMany`
on `records` while permitting the one legal `stream_meta` delete, and
`dropCollection` on `derived` permitted but scoped.

## 3. Plan verification

### Owned files present

`aggregator/mirror.py`, `docs/mongo.md`, `tests/test_mirror.py`,
`tests/test_mongo_deploy.py` — all four on disk, the two test files now `0755`
with shebangs (attempt-1 minor **m5** fixed, and asserted by
`test_the_owned_suites_are_executable_like_their_siblings`).

### Attempt-1 critique items — all closed, each with a test that would fail if reverted

- **MAJOR 1 (`--backfill` walk unwired, mtime guard inert).** `main()` now
  branches: `--rebuild` → `iter_rebuild_observations()`, `--backfill` →
  `iter_backfill_observations()`, which walks `$TOUCH_CLAUDE_ROOT/projects/**`
  via `iter_backfill_sources` and yields **3-tuples** carrying the source path.
  `test_the_backfill_walk_is_wired_to_the_cli` asserts `main()`'s call graph
  names the walk, that the per-file source signature is declared, and that the
  deny-list is applied at the source. The guard now **fails closed**: a 2-tuple
  carrying `now()` is refused (`refused_no_source`-style counter + `/health`
  message *"a guard that cannot be checked fails closed, it does not widen to
  now()"*) — the exact probe the critique demanded. `docs/mongo.md` §5 and
  `_usage()` were brought in line (`TOUCH_CLAUDE_ROOT`, the fail-closed row in
  the §-206 table).
- **MAJOR 2 (`rebuild()` drops `derived` then aborts on an unmapped kind).**
  `map_and_enqueue` is now total (`self.enqueue(self.map_total(...))`), the
  registry is resolved **before** the destructive drop, and `rebuild()` returns
  `{'replayed': 2, 'unmapped': 1, 'unmappedKinds': ['session'], 'droppedDerived': False, …}`.
  `test_rebuild_survives_an_unmapped_kind_and_keeps_derived` asserts `derived`
  survives, `/health` reports `degraded` with a non-null `lastError` naming the
  kind, and that `map_observation` still raises while the wrapper does not.
- **m1 (rebuild fingerprint test never wiped).** Now one backend: store emptied,
  fingerprint compared across the wipe (`ok: the store really is empty before
  the rebuild`), and the rebuild scenario is additionally inside the live arm
  against a real mongod, exercising `AsyncBackend.drop_collection` and the
  `dropCollection` grant.
- **m2 (24-h assertion restated its input).** Replaced by *"an operation stamped
  with the IMPORT's clock is refused against the source file's 03:00Z mtime —
  the failure R-45 names, not a synthetic hour-in-the-future literal"*.
- **m3 (spurious `lastError` on a healthy mirror).** `usersInfo`-denied
  commentary now lives in a `notes` field; `/health` carries `lastError: None`
  for the least-privilege user, asserted both in unit form and against the real
  mongod.
- **m4 (`enqueue` degraded only from `live`).** `if self.state in (STATE_LIVE,
  STATE_STARTING)`; `start()` no longer promotes to `live` over a startup loss.
  Asserted with the count and reason on `/health`.
- **n1** — `key`/`keys` removed from the unconditional exempt list; a credential
  under `key` is redacted while `{'key': 'Enter', 'keys': 'none'}` survives.
- **n2** — fence tightened to `touch_`; `touchdown_prod`, `touchy`, `touch` are
  all in the hostile list and rejected.
- **n3** — the `fixtures/` conditional is gone (no `fixtures` reference remains
  in `tests/test_mirror.py`).
- **n4** — the docker arm now `check(False, …)`s on a failed `docker run` or a
  90 s readiness timeout, with an in-source comment that only the
  `docker_unavailable()` pre-flight reasons may skip.
- **n5** — `holderBoot` is a truncated hash of `boot_id`
  (`'41fd8688a2866ca1'`), asserted stable within a boot and never the raw value.

### Tests assert behavior, not tautologies

Spot-audited again after the rewrite. Assertions remain behavioural: wall-clock
timings against a dead port (0.51 s connect vs MONGOSCHEMA-4's 30.1 s), driver
**call counts** proving the held breaker makes zero attempts, fingerprint
equality across shuffled write orders and across two independent backends,
server-side authorization denials from a real mongod, and AST/source walls for
the GD-26 verb prohibitions. `check()` accumulates into a `failures` list and
`main()` exits 1 with the names, so a red assertion genuinely reddens the file.
The two remaining skips are legitimate and self-describing (`no entity module
exists yet` is exactly SD-1's expected state at sp-06).

### Ownership — clean

Only the four owned paths carry post-implementer mtimes:

```
01:33 aggregator/mirror.py   01:35 tests/test_mongo_deploy.py
01:37 docs/mongo.md          01:38 tests/test_mirror.py
```

Nothing under `.claude/`, `.gitignore` or `CLAUDE.md` was touched by this
sub-plan (their mtimes predate the attempt; they are sp-01/other-orchestrator
state). `git log -1` is still `579446e` — no commit was made.

## Observations (not gate failures, not this sub-plan's to fix)

1. **Leftover container `touch-mongo-sp05`** still holds `127.0.0.1:27117`. It
   is sp-05's, not sp-06's; this gate worked around it on port 27219 and removed
   its own container and volume. Worth reaping before a future fixed-port gate.

## Evidence summary

- Owned suites: **2/2 green**, 0 failures.
- Full suite: **14/14 green, rc=0** in the installed env, the bare env (no
  pymongo, no docker), and the live-mongod env.
- All 2 majors, 5 minors and 5 nits from attempt 1 closed, each with an
  assertion that fails if the fix is reverted.
- No new failures, no baseline regressions, no ownership violations, no commits,
  no Mongo residue.
