# sp-mirror-deploy — test gate, attempt 1

**Verdict: PASS.** Every owned suite green, the full-suite regression green, and
green in all three environment arms (bare / installed / live mongod). No
failure to attribute.

Implementer-declared changes: `aggregator/mirror.py`, `tests/test_mirror.py`,
`tests/test_mongo_deploy.py`. `docs/mongo.md` (also owned) was already on disk
from the interrupted attempt and is exercised by the deploy suite.

---

## 1. Targeted suites (must be 100% green)

Run from the repo root, stdlib Python 3.13.7, standalone executables.

| suite | rc | notes |
|---|---|---|
| `python3 tests/test_mirror.py` | 0 | 17 test functions, ~120 assertions |
| `python3 tests/test_mongo_deploy.py` | 0 | 12 test functions, ~110 assertions, incl. a real `mongo:7` container |

Both print their own terminal banner (`all mirror (R-45) tests passed`,
`all mongo deployment (R-42 / R-57 mongo-doc) tests passed`) and exit 0.

## 2. Full-suite regression gate

The prescribed loop (all four monitoring suites from their own dir, then every
`tests/test_*.py` from the root):

```
PASS tests/test_bootstrap.py          PASS tests/test_stdlib_only.py
PASS tests/test_fixtures.py           PASS tests/test_store.py
PASS tests/test_mirror.py             PASS tests/test_tailer.py
PASS tests/test_mongo_deploy.py       PASS tests/test_ws.py
PASS tests/test_mongo_store.py        PASS .claude/…/test_frontend.py
PASS tests/test_refs.py               PASS .claude/…/test_server.py
                                      PASS .claude/…/test_shell.py
                                      PASS .claude/…/test_watcher.py
SUITE_RC=0
```

14/14 green. The four monitoring baselines (`test_server`, `test_watcher`,
`test_shell`, `test_frontend`) are green as at baseline — no baseline failure
to discount, and no NEW failure anywhere.

### 2a. Bare-checkout arm (GD-21 / R-56 no-mongod arm) — the one that mattered

The gate's hard requirement is that the suite stays green on a bare checkout
with **no third-party packages and no services**. Verified by construction
rather than by assertion: a fresh venv with **pymongo absent**, plus a shadowed
`PATH` (644 symlinks of `/usr/bin` minus `docker`) so `shutil.which("docker")`
genuinely returns `None`.

```
$ python -c "import pymongo"  -> ModuleNotFoundError
$ shutil.which('docker')      -> None      (git still present, as the suite needs)
```

All 14 suites PASS, `BARE_RC=0`, and every Mongo-dependent arm skips **cleanly
and by name** rather than erroring:

- `test_mirror.py` — *"the dead-port arm needs pymongo to have a driver to time out (GD-21)"*, *"live mirror arm: TOUCH_MONGO_URI is not set"*
- `test_mongo_deploy.py` — *"live docker arm: pymongo is not installed (GD-21: absence is legal)"*
- `test_mongo_store.py` — 3 driver-exception / live arms skipped (sp-05's, unaffected)

`test_stdlib_only.py` self-reports `pymongo is NOT installed here … either way
the suite passes and Mongo tests skip cleanly (GD-21)` — i.e. the guard sub-plan
and this one agree about the environment.

### 2b. Live-mongod arm — exercised, not just skipped

`test_mirror.py`'s live arm is opt-in via `TOUCH_MONGO_URI` and skipped by
default, so the gate provisioned a mongod **from the documented R-42 recipe**
(loopback bind, `--auth`, named volume, `mongo:7`) on a free port and ran it:

```
ok: the mirror reaches 'live' against a real mongod, got 'live'
ok: …holding the GD-29 writer lease
ok: a real bulk_write lands every document: {'records': 6, 'writers': 1}
ok: replaying the mirror's own output against a REAL server changes nothing (GD-25)
ok: MemoryBackend and a real mongod produce the SAME fingerprint — which is what
    makes the bare-checkout suite meaningful
ok: a record with no generation is not swept (incremental appends never retract)
ok: the sweep retracted rather than deleted, server-side
ok: a second writer is refused by the real conditional write (GD-29)
ok: dropping only the database this test constructed: touch_test_83555 (GD-27/GD-12)
```

That MemoryBackend-vs-mongod fingerprint equality is the assertion that makes
the whole no-mongod arm honest, and it holds against a real server.

The full 14-suite loop was then re-run with `TOUCH_MONGO_URI` set:
`LIVE_SUITE_RC=0`.

**Cleanup verified:** after the run, `listDatabases` returned exactly
`admin config local` — no `touch_test_*` residue; the tests drop only names they
constructed (GD-27). The gate's own container and volume were removed.

`test_mongo_deploy.py` independently ran its own throwaway `mongo:7` container
(random loopback port, `docker rm -f -v` in a `finally`) and proved the
deployment end to end:

```
ok: the running container publishes on loopback only: '27017/tcp -> 127.0.0.1:55613'
ok: an unauthenticated client cannot list databases / enumerate users / read / write
ok: the documented role/user bootstrap runs as written
ok: Touch reaches 'live' against the documented deployment, as the least-privilege user
ok: the SERVER refuses a delete on `records` — … no longer a code review (GD-26)
ok: …while the ONE legal delete (renumbered positional stream_meta) is permitted
```

## 3. Plan verification

### Owned files — all four present

`aggregator/mirror.py` (1998 L), `docs/mongo.md` (268 L),
`tests/test_mirror.py` (928 L), `tests/test_mongo_deploy.py` (794 L).

### Items

**R-45 (mirror runtime)** — every named mechanism is present in
`aggregator/mirror.py` and asserted behaviourally:

- *queue*: `enqueue` is a plain `def` containing **no `await` at all**; 200 ops
  against an unreachable server took **0.1 ms**. Bounded — overflow is counted
  and surfaced as `degraded`, and the test proves the queue holds `MirrorOp`s
  only, so *live frames are structurally not in it to be dropped* (GD-30).
- *breaker*: dead port leaves `state='down'`; connect capped at **0.51 s**
  (MONGOSCHEMA-4's 30.1 s stall gone); after `BREAKER_FAILURES` the held ticks
  cost `0.000 s` and make **zero driver calls** (proven by call count); the hold
  expires at 30 s and the mirror recovers to `live`, writing everything queued.
- *no data loss*: a transient outage **re-queues** the in-flight batch rather
  than dropping it, and nothing is counted as dropped.
- *lease* (GD-29): second writer refused, `/health` says `'refused'` with a
  reason, the loser mirrors nothing and its drainer writes nothing, the race is
  recorded as a tolerated duplicate, an **expired** lease is taken over, and
  renewal happens in place near TTL.
- *cursors*: round-trip as SD-10's whole `Checkpoint` identity tuple
  (`st_dev, st_ino, size, offset, line_no, gen, mtime_ns`), and a shrink
  **rewinds** via `$set`, not `$max`.
- *sweep* (GD-26/SD-10): older generations are **retracted** by `updateMany`
  carrying `retracted:true, retractedGen:G`; every record document still exists;
  the ONE legal `deleteMany`+reinsert is confined to renumbered `stream_meta`;
  unscoped sweeps, generation-only scopes, non-positive generations and
  non-`stream_meta` reinserts are all refused.
- *rebuild*: wipe + `--rebuild` reproduces a **byte-identical fingerprint** and
  identical counts; drops `derived` (exactly once) rather than migrating it.
- *backfill*: `live` is **not a parameter** — signature is
  `(self, observations, mtimes, now)` and the body carries a literal
  `live = False` (R-45 exactly); every doc stamped `ingestMode:"backfill"` as a
  *field*; acceptance holds (no stored ts within 24 h of now); a record newer
  than its own source file's mtime is refused and counted.
- CLI surface present: `--check | --health | --rebuild | --backfill`.

**GD-21** — `pymongo` has **no module-level import** (7 lazy sites); the
`AsyncMongoClient` is built from `mongo_store.CLIENT_OPTIONS`, which is verbatim
`serverSelectionTimeoutMS: 500, connectTimeoutMS: 500, socketTimeoutMS: 2000,
retryWrites: True`. Sourcing it from the one constant instead of re-spelling it
is correct, not a deviation — `test_stdlib_only.py` confirms exactly two files
may import pymongo (`mongo_store.py`, `mirror.py`), "no third by analogy".

**GD-26 walls** — no forbidden delete/replace verb is ever *called*;
`MemoryBackend` and `AsyncBackend` each refuse in **their own body** (a wall,
not a convention); no `expireAfterSeconds`, no `$inc`, and `$unset` appears
nowhere in the file, prose included.

**GD-27 / R-42 security** — credentials file refused at 0644/0640/0604/0666 and
on any symlink, accepted at 0600/0400 via `mode & 0o177`; `save_credentials`
writes 0600 and refuses to clobber; malformed content is a `CredentialError`,
never a silent fallback to "no mirror". Passwords are scrubbed from driver
exceptions, `/health` and `describe()` — visibly redacted (`[redacted]`) so
nobody mistakes it for empty — while the host survives for diagnosis. Deny-list
(`server.json`, `.credentials.json`, `.claude.json`, `mongo.json`) matched by
basename and never read; empty path fails closed. DB name is
`touch_<sha1(realpath)[:8]>`, stable, per-checkout distinct, `TOUCH_MONGO_DB`
override fenced to the `touch` prefix (rejects `local`, `config`, `production`).
Zero configured users is a refusal, while *"we could not enumerate users"* is
treated as **healthy** — the false positive that would actually matter, and it
is tested against the real least-privilege user.

**R-57 (mongo.md)** — every clause asserted on the page: `--rebuild` and
`--backfill` as runnable command lines, "Mongo down is a non-event" stated
verbatim, the no-TTL law, the v0 retention policy, `.touch/mongo.json` + 0600,
the derived DB name, the least-privilege user, and *measured* growth numbers
(15.7 MB corpus, 3 936 records, 0.53 mirror-vs-raw ratio, 1.3 per session-hour
— measurements, not estimates). Every `sbx ports … 27017` mention is a
prohibition, and non-loopback Mongo appears only inside prohibitions.

**SD-1 (mapper registry)** — discovery works; two modules claiming one kind is
refused; a non-existent module is skipped silently (four of five are, by
design); bad mapper output (non-triple, unknown collection, raising mapper,
`$inc`, an `_id` not from `refs.ref_key`) is a `MapperError` **naming the
mapper**, not a mystery bulk failure later; an unmapped observation is a
refusal, never a quiet drop.

### Tests assert behavior, not tautologies

Spot-audited. `check()` records into a `failures` list and `main()` exits 1 with
a named list, so a red assertion genuinely fails the file. The assertions are
behavioural: wall-clock timings against a dead port, driver **call counts** to
prove the breaker makes no attempt, fingerprint equality across write orders and
across two independent backends, server-side authorization denials from a real
mongod, and AST/source-level walls for the GD-26 verb prohibitions. The two
skips are legitimate and self-describing (`no entity module exists yet` is
exactly SD-1's expected state at sp-06; entity modules land in sp-07…sp-11).

### Ownership — clean

Only the four owned paths carry post-implementer mtimes:

```
01:02  tests/test_mirror.py
01:01  tests/test_mongo_deploy.py
00:58  aggregator/mirror.py
00:09  docs/mongo.md
```

No file outside the ownership list was written by this sub-plan. `git status`
is otherwise unchanged from the pre-attempt snapshot, and nothing was committed.

## Observations (not gate failures, not this sub-plan's to fix)

1. **Leftover container from sp-05.** `touch-mongo-sp05` is still running and
   holding `127.0.0.1:27117`. It is not sp-06's — this gate worked around it on
   another port and removed its own container and volume. Worth reaping before
   the next Docker-using gate, since a fixed-port test would collide.
2. **`monitoring.md` / `monitor.html` carry mtimes (01:06) later than the
   implementer's last write.** Their diffs contain **zero** `mirror`/`mongo`
   matches, so this is sp-03-era content touched by the concurrent in-flight
   orchestrator state the brief warns about — not sp-06 leakage.

## Evidence summary

- Owned suites: **2/2 green**, 0 failures.
- Full suite: **14/14 green**, `rc=0` — in the installed env, the bare env
  (no pymongo, no docker), and the live-mongod env.
- Live arms exercised for real against `mongo:7` rather than only skipped.
- No new failures, no baseline regressions, no ownership violations, no commits.
