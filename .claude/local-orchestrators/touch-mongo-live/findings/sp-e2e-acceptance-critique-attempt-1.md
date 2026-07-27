# sp-e2e-acceptance — adversarial critique, attempt 1

**Verdict: APPROVED** (0 blocker, 0 major, 8 minor, 5 nit).
**depth:** in-scope. **critical_defect:** false.

Reviewed file (the sub-plan's only owned file, new in an untracked tree, so the
full content was read rather than a diff):
`/home/laniakea/Projects/touch/tests/test_e2e_sim.py` (1420 lines).

Reviewed against: `plan/touch-mongo-live-subplans.md` §"sp-14 — e2e-acceptance"
(+ SD-11 and the shared decisions), amendment items R-56 / R-58 / R-42 / R-49,
base-plan R-37's phase-1 and phase-3 arms, and GD-21…GD-30 / GD-15.

## Independent verification I ran (not taken on the test gate's word)

| Check | Result |
|---|---|
| `python3 tests/test_e2e_sim.py` | **rc=0**, 24.9 s wall, 1 skip (live arm, `TOUCH_MONGO_URI` unset), 0 failures |
| Fixture mutation — `find tests/fixtures -newermt '2026-07-26 23:00'` after the run | **empty**; `git status tests/fixtures` unchanged. The corpus really is symlinks; `shutil.rmtree` unlinks the symlink, never the target. The phase-1 arm points `ORCH_WF_DIR` *directly at* `tests/fixtures/run-wf_829e6f58/…` — I traced `decision_watcher.py`: every write goes to `ORCH_STATE_DIR` (`EVENTS`, `STATE`), so pointing at the read-only fixture is safe. |
| Ownership (GD-15) | `find aggregator tests docs README.md CLAUDE.md .claude/shared/monitoring -newermt '2026-07-26 22:00'` ⇒ only `tests/test_e2e_sim.py` (23:30) and `tests/test_touch_frontend.py` (22:46, 45 min earlier, sp-13's). **No violation attributable to this attempt.** No commits. |
| GD-21 | The file imports stdlib + `aggregator.*` only. `pymongo` is reached solely through `mongo_store`/`mirror`. The bare-checkout child genuinely blocks at the meta-path (stronger than "not installed"), and the child's fingerprint equality is a real cross-interpreter oracle, not a re-run of the same objects. |
| GD-22 / the "no mongod" composition | Not a fake path: `server.main()` builds `ReadModel(state={})` and the class docstring pins `state` as "`mirror.MemoryBackend`'s state … shared with whatever is ingesting". The arm's `ReadModel(state=backend.state)` is therefore the *product's* memory-authoritative path, not a test-only shortcut. |
| GD-26 | No delete verb, no `$unset`, no TTL anywhere in the file. The two destructive acts are (a) popping keys of an in-process `MemoryBackend` dict to simulate R-45's wipe (documented, and `Backend.drop_collection` legitimately refuses everything but `derived`), (b) `drop_database` of a `touch_test_<pid>` database the arm itself created. Both are within R-45's "wipe + rebuild" clause. |
| GD-25 / GD-24 | Double-ingest **re-reads the files** rather than replaying the first pass's objects (a `now()` in any source diverges here and nowhere else) and adds a reversed-order replay. That is a stronger form than the sub-plan asked for. |
| Anti-tautology spot checks | The rollup-agreement arms compare `ingest.rollup` against a hand-summed fold over the mirror mapper's `$max` documents — two paths, not one. The `naive > total > biggest` triple (5 729 009 < 5 879 468 < 11 175 286) makes both the under-report and the over-count non-vacuous. The `touch-repo-recon` "stays failed" control keeps the whole SD-4 re-label arm from being blanket amnesty. |

The arms map onto sp-14's bullets one-for-one, and several go beyond the letter
of R-56 (reversed replay; the watcher's own stream read back through
`legacy.py` **and** `/api/tasks`; the R-25 foreign-slug negative control). I
could not find an assertion that passes while the product is broken. Everything
below is hygiene, not correctness.

---

## Findings

### 1. minor — the bare-checkout child's "nothing third-party" claim is narrower than its message
`tests/test_e2e_sim.py:516-517` (message) / `:540,585` (the check).
`third_party` is `sorted(n for n in sys.modules if n.split(".")[0] in BLOCKED)`
where `BLOCKED = {pymongo, bson, dns, dnspython, gridfs}` — so the message
"…and nothing third-party reached sys.modules" is only ever a statement about
the Mongo driver. A stdlib-only regression via any other package (say a stray
`requests`) passes this arm silently.
**Fix:** in the child, compute
`sorted(n for n in sys.modules if n.split(".")[0] not in sys.stdlib_module_names and n.split(".")[0] not in {"aggregator"})`
minus the sandbox site hook `tests/test_stdlib_only.py` already tolerates — or,
if you'd rather keep that guard in its owner, reword to "no Mongo driver module
reached `sys.modules`".

### 2. minor — R-56's "full suite green on a bare checkout" is reinterpreted, not implemented
`tests/test_e2e_sim.py:19-21` ("That last equality is what 'the suite is green on
a bare checkout' means operationally").
R-56 and sp-14 both list "every module imports; **full suite green** (the suite
runs with no services on a bare checkout)". The file proves module imports and
byte-identical reduction; nothing here proves the *sibling test files* stay
green with the driver blocked. It is genuinely covered elsewhere —
`tests/test_stdlib_only.py::test_every_module_imports_without_third_party_packages`
(child + `sys.stdlib_module_names`) and the per-file `pymongo_available()` skip
guards in `test_mirror.py` (12 sites) / `test_mongo_store.py` (25) /
`test_mongo_deploy.py` — which is why this is minor and not major, but the
docstring currently redefines the clause instead of delegating it.
**Fix (cheap):** name those two owners in the docstring so a reader knows where
the clause lives. **Fix (literal):** have the child arm run
`tests/run_all.sh --keep-going` with a `sitecustomize.py` blocker on
`PYTHONPATH` and this file excluded — correct but roughly doubles suite runtime,
so only if you want the clause discharged in one place.

### 3. minor — one raised exception aborts the remaining arms and leaks the 20 MB tmpdir
`tests/test_e2e_sim.py:1400-1404`. `main()` calls each test bare and only
`rmtree`s after the loop. Live reachable raise sites: `test_live_mongod_arm`'s
`finally` (opens a client and drops a database with no guard, `:829-833`), and
`killed.plans.get("research").badge` at `:1140`, which is `AttributeError` —
not a clean `FAIL` — if the fixture ever loses that plan. In an *acceptance*
file, an early raise means the budget, phase-1 and phase-3 arms silently never
run, and ~20 MB stays in `$TMPDIR`.
**Fix:**
```python
for test in TESTS:
    try:
        test()
    except Exception:
        traceback.print_exc()
        failures.append(f"{test.__name__} raised")
```
and move the `rmtree` loop into a `finally`.

### 4. minor — a malformed `TOUCH_MONGO_URI` crashes the arm *and* prints the credential
`tests/test_e2e_sim.py:848-851`. Only `ms.MongoUnavailable` is caught, but
`ms.open_client` ends in `MongoClient(uri, **client_options())`, which raises
`pymongo.errors.InvalidURI` / `ConfigurationError` for a bad URI — and those
messages embed the URI, password included. Two consequences: sp-14's "skips
cleanly without mongod" is violated (it aborts, see finding 3), and a credential
reaches the test log, which is exactly the class of leak GD-27 closes
everywhere else.
**Fix:**
```python
try:
    client = ms.open_client(uri)
except ms.MongoUnavailable as exc:
    return None, str(exc)
except Exception:
    return None, "TOUCH_MONGO_URI is unusable (see R-42's recipe)"   # never interpolate the driver message
```
Same rule for the `finally` block's `ms.open_client(uri)` at `:829`.

### 5. minor — the drop-database safety check reports but does not gate
`tests/test_e2e_sim.py:819-820` and `:830-833`. `check(name.startswith("touch_test_"), …)`
is asserted twice and then `client.drop_database(name)` runs **unconditionally**
either way. A guard that records a failure and proceeds to do the dangerous
thing is not a guard. (`name` is constructed by `live_database()`, so it cannot
be false today — which also makes the doubled assertion pure noise.)
**Fix:** one check, and make it control flow:
```python
if not name.startswith("touch_test_"):
    check(False, f"refusing to drop a database this test did not construct: {name}")
    return
```
then drop inside the `finally` only after that early return has been passed.

### 6. minor — the cross-session arm accepts either route spelling
`tests/test_e2e_sim.py:770-772`:
```python
status, payload = get("/api/run/node", agent=CROSS_AGENT)
if status != 200:
    status, payload = get("/api/run/node", run=RUN_829, agent=CROSS_AGENT)
```
An acceptance file that tries two spellings cannot detect a route-contract
regression: if `?agent=` alone stops working tomorrow, this arm still passes.
**Fix:** assert the single documented form; if both are genuinely supported,
assert both return 200 **and** that the two payloads are equal — that turns the
fallback into the contract statement it is pretending to be.

### 7. minor — no end-to-end assertion on GD-28 provenance or GD-24 `_id` shape
This file is the only place the *whole* corpus exists as mirrored documents, and
two of the amendment's cheapest global invariants go unasserted here: every
document carries a legal `provenance` pin (`{asserted,touch}` for custom state,
`{harness,derived}` for the mirror — GD-28) and every `_id` is a `str` from the
`refs` grammar with no subdocument key (GD-24). `test_mongo_store.py` /
`test_refs.py` / `test_mirror.py` own the *grammar*, but a mapper regression
would surface first in the composed corpus.
**Fix:** two folds over `backend.state` in `test_no_mongod_the_whole_read_api_answers`:
```python
bad_ids = [(c, k) for c, b in observation_state(backend.state).items()
           for k in b if not isinstance(k, str)]
check(not bad_ids, f"every _id is a string (GD-24): {bad_ids[:3]}")
no_prov = [(c, k) for c, b in observation_state(backend.state).items()
           for k, d in b.items() if d.get("provenance") not in ms.PROVENANCE]
check(not no_prov, f"every document is provenance-pinned (GD-28): {no_prov[:3]}")
```
(`ms.PROVENANCE` is already exported by `mongo_store.__all__`.)

### 8. minor — the foreign-slug negative control only enumerates the top level
`tests/test_e2e_sim.py:1339-1341` collects `foreign_ids` from
`os.listdir(directory)` only. Two of the three foreign slugs are the 100-char
nested-run slugs; a transcript one level down would never enter `foreign_ids`,
so the control could narrow silently while `len(foreign_ids) == 4` still holds
by coincidence of the current fixture.
**Fix:** `os.walk(directory)` for the id set (the `== 4` assertion then also
becomes a real freeze on the fixture rather than a count of one directory).

### 9. nit — `obs` is shadowed inside two comprehension-adjacent loops
`tests/test_e2e_sim.py:791` and `:1215`: `for obs in usage:` rebinds the
observation-list name from `ingest_corpus()`. Nothing breaks today because the
list is not read afterwards, but the next person to add a line after that loop
gets a `UsageObservation` where they expect a list.
**Fix:** rename the loop variable to `record`.

### 10. nit — identity dict
`tests/test_e2e_sim.py:1202-1203`:
`field = {"agentId": "agentId", "sessionId": "sessionId", "runId": "runId"}[key]`
is `field = key` spelled in four lines. **Fix:** delete it and use `key`.

### 11. nit — `imported == modules` cannot fail when the child exits 0
`tests/test_e2e_sim.py:513-515` against the child's unguarded
`for name in MODULES: __import__(...)` at `:555-558`. A failed import kills the
child, which the `returncode != 0` branch already reports with stderr — so this
check is documentation, not a test.
**Fix (optional):** have the child catch per module and report
`{"imported": [...], "failed": {name: repr(exc)}}`, so a single broken module is
named instead of collapsing into a traceback tail.

### 12. nit — the bespoke `note()` outcome is neither asserted nor summarised
`tests/test_e2e_sim.py:141-148` and `:1280-1284`. `note()` records that
`wf_b297177a-d11` carries a transcript-derived `endedAt` despite having no
terminal snapshot, and `main()` never mentions notes in its tail, so the
observation is invisible in a captured log. I checked R-49: its acceptance for
the no-snapshot fixture is only "run doc exists, no error" — so nothing is being
hidden here, which is why this is a nit and not a finding about swallowed
behaviour.
**Fix:** either promote it to a real `check` of the behaviour R-49 does specify
(`endedAt` derived from transcript activity, `status` absent), or drop the
`note` machinery entirely and leave the comment.

### 13. nit — loose thresholds and one thin timing margin
Frozen corpus, so exact numbers are available: `sessions["count"] >= 5`
(`:369`, `:529`, `:645`) is 5, `len(model.sizes()) >= 6` (`:398`) is 9,
`len(events) > 20` (`:1044`) is 42. `>=` catches under-ingest but not
over-ingest — an R-25 scoping regression that added the four foreign sessions
would pass every one of them (only `test_the_foreign_slugs_are_never_ingested`
would catch it). Separately, `per_op < 0.001` at `:986` measured 138.8 µs on an
idle box — 7× headroom, the tightest wall-clock margin in the file, and the one
most likely to flake when the suite runs under load.
**Fix:** pin the exact counts (they are fixture constants, and sp-02 froze them
precisely so they could be), and widen the per-op bound to `< 0.005` — the claim
being defended is "nowhere near a 250 ms tick", which 5 ms still makes.

---

## Attack checklist, dispositions

| Clause | Disposition |
|---|---|
| GD-21 lazy pymongo, bare-stdlib imports | **Pass** — verified in a meta-path-blocked child; fingerprint equality across interpreters is the strongest form of this available. Findings 1, 2 are wording/delegation. |
| GD-22 Mongo off the liveness path | **Pass** — `absent` and `down` both exercised; the read model is the product's own memory-authoritative wiring, not a stand-in. |
| GD-24 string `_id`s | Not asserted here (finding 7); no violation introduced. |
| GD-25 upsert algebra, deltas wire-only | **Pass**, and exceeded (re-read double ingest + reversed replay). |
| GD-26 no deletes/`$unset`/TTL | **Pass** — the only destructive acts are R-45's own wipe clause and a self-created `touch_test_<pid>` database (finding 5 is about gating, not about legality). |
| GD-27 security | **Pass** with finding 4 — no credential is written to the repo, events, `/health`, the API or a prompt; the one leak path is an unhandled driver exception echoing a malformed URI. |
| GD-28 provenance pins | Not asserted here (finding 7). |
| GD-29 no agent holds a client | N/A to this file; nothing here spawns an agent. |
| GD-30 latency budget | **Pass** — 1 024 B tick on a 20 MB stream, idle tick opens nothing, 0.57 s to map 4 096 ops against a dead mongod, overflow dropped-and-counted rather than awaited. |
| GD-15 one file one owner | **Pass** — mtime evidence above. |
| Real behaviour, not tautologies | **Pass** — see the anti-tautology row in the verification table. |
| Skips cleanly without mongod | **Pass** for the normal case (1 clean skip); finding 4 for the malformed-URI case. |
| No needless rewrites / docs match behaviour | **Pass** — new file, and its docstring is accurate except for the clause in finding 2. |
