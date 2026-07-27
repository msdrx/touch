# sp-e2e-acceptance — test gate, attempt 1

**Verdict: PASS.** The owned suite is 100% green (including the optional live-mongod
arm, which I provisioned and ran rather than accepting the skip). Two failures exist
elsewhere in the full suite; both are pre-existing tree state owned by other
sub-plans, both reproduce in isolation, and neither is attributable to this
sub-plan's change.

Implementer changed exactly one file: `/home/laniakea/Projects/touch/tests/test_e2e_sim.py`
— which is precisely the sub-plan's ownership list (`sp-14 — e2e-acceptance`:
"Owned files: new `tests/test_e2e_sim.py`"). No ownership violation. No commits made.

---

## 1. Targeted suite (owned files) — GREEN

```
cd /home/laniakea/Projects/touch && python3 tests/test_e2e_sim.py
→ exit 0, "ALL E2E ACCEPTANCE TESTS PASSED", 1 skipped (live mirror arm, no URI)
```

Re-run with a real mongod supplied (see §3) → exit 0, **0 skipped**.

19 test functions, all four required arms present and non-vacuous:

**No-mongod arm (R-56 / R-37 phase-1):**
- `test_no_mongod_the_whole_read_api_answers` — sessions, agent rows, loop cards,
  token counters all answer; `/health` `mirror.state == 'absent'`, asserted to be a
  *state* and not an error (GD-22).
- `test_no_mongod_rows_still_update_on_an_incremental_tick` — rows move on a tick.
- `test_a_bare_checkout_reduces_to_the_same_state` — runs a **child interpreter**
  with the driver made unimportable and asserts `pymongo_available is False`,
  `mirror_state == 'absent'`, `/health` says `absent`, and every module imports.
  This is the real "bare checkout" assertion, not a mock of one.
- `test_a_dead_mongod_is_reported_down_and_changes_no_answer` — `STATE_DOWN`,
  `/health` 200 with `mirror: down`, answers unchanged.

**Mirror arm:** `test_double_ingest_of_the_whole_corpus_changes_nothing` (GD-25
fingerprint), `test_wipe_and_rebuild_reproduce_the_corpus` (R-45),
`test_the_killed_run_keeps_its_retry_topology_through_the_api` (wf_455b348c),
`test_the_cross_session_agent_unions_through_the_api` (a2fc883c) — all through the
full path files → ingest → mirror → reducer → API. Plus `test_live_mongod_arm`,
which skips cleanly when `TOUCH_MONGO_URI` is unset and otherwise runs the same
algebra against a real server.

**Budget arm:** `test_a_tick_reads_the_delta_not_the_stream` — builds a real
20 972 036 B stream, pays for it once, appends 1 024 B, then asserts the tick read
**1 024 B < 65 536 B** and that the figure equals the one `/health` publishes, and
that an idle tick opens nothing (stat-first short circuit). LIVEFLOW-15 satisfied
by measurement, not by assertion-on-a-constant.
`test_a_dead_database_never_slows_the_ingest_loop` — MONGOSCHEMA-4 tick duration.

**R-37 phase-1/-3 arms:** `test_phase1_the_real_watcher_emits_no_failed_verdict`
(R-16 replay, no `failed`), `test_phase1_the_broken_watchers_own_stream_relabels`
(`closed — no verdict`, and the genuinely killed run *stays* failed — the re-label
is proven to be a rule, not a blanket amnesty),
`test_phase3_six_distinctly_labelled_researchers` (wf_829e6f58: six distinct
labels over seven full 17-hex agentIds + the synthesizer),
`test_phase3_rollups_are_deduped_and_agree_everywhere` (528 mirrored docs from
1 081 records; agentId/sessionId/runId rollups agree with the database, and the
test also proves that summing the split records *would* over-count — the negative
control that makes the dedup claim mean something),
`test_phase3_three_state_liveness` (done/running/unknown at ONE `now`; no agent
document stores a state), `test_phase3_the_legacy_task_renders_stale_closed_agents`
(touch-repo-recon: 4 stale / 3 superseded / 2 done, one shared string across both
reducers), `test_the_foreign_slugs_are_never_ingested`,
`test_this_file_is_part_of_the_suite` (present in `run_all.sh`, executable bit set).

Assertion quality: checks compare computed values against independently derived
ones (memory-backend vs. server fingerprints, rollup vs. stored documents, byte
counters vs. `/health`), and several carry explicit negative controls. I found no
tautological assertions.

Hygiene: all writes go to `tempfile.mkdtemp()` dirs collected in `TMPDIRS` and
removed in `main()`; fixtures are read-only; the live arm constructs
`touch_test_<pid>` and drops only that name (asserted twice, before the drop).

## 2. Full-suite regression gate — 2 failures, both PRE-EXISTING, neither attributable

```
4/4 monitoring tests: PASS (test_frontend, test_server, test_shell, test_watcher)
20/22 repo tests:     PASS
FAIL tests/test_mirror.py   (3 checks)
FAIL tests/test_sessions.py (1 check)
```

Failing checks:

- `tests/test_mirror.py :: test_the_breaker_holds_then_lets_the_mirror_recover`
  → `FAIL: …proven by the call count: the held ticks made no attempt`
- `tests/test_mirror.py :: test_the_generation_sweep_retracts_and_never_deletes`
  → `FAIL: the first generation lands`
- `tests/test_mirror.py :: test_wipe_and_rebuild_produce_the_same_fingerprint`
  → `FAIL: …and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`
- `tests/test_sessions.py :: test_a_rebuild_through_mirror_reproduces_the_scan`
  → `FAIL: wipe + --rebuild reproduces a byte-identical fingerprint`

**Why they are not attributable to this change:**

1. The implementer changed exactly one file, `tests/test_e2e_sim.py`. It is a
   standalone executable run in its own interpreter; it imports the aggregator
   modules read-only and writes only into `mkdtemp` directories (verified by
   source inspection: every `open(..., "w"|"a")` target is under a `tmpdir()`
   path, the single `shutil.rmtree` only walks `TMPDIRS`). There is no shared
   on-disk state through which it could affect a sibling test file.
2. Both failures **reproduce when the file is run alone**, with no other test
   having executed in the same session (`python3 tests/test_mirror.py`,
   `python3 tests/test_sessions.py`) — so they are not ordering or pollution
   effects either.
3. Timeline (mtimes): `aggregator/mirror.py` 2026-07-26 **11:29:20**;
   `tests/test_mirror.py` **02:44:01**; `tests/test_sessions.py` **04:14:23**;
   `tests/test_e2e_sim.py` **23:30:52**. The sp-mirror-deploy gate closed green at
   02:53 and sp-sessions-arm at 04:18 (see their attempt findings). `mirror.py`
   was then modified ~7 h later and ~12 h *before* this sub-plan's edit. The
   failures date from that `mirror.py` change, i.e. from the interrupted
   sp-mirror-deploy work the run brief warns about — files owned by
   **sp-mirror-deploy**, not by sp-e2e-acceptance.
4. The failure signature confirms it: the counts diff is exactly
   `{'writers': 1}` — `mirror.py` now materialises a writer-lease document that
   `test_mirror.py`'s wipe+rebuild expectation (and the fingerprint
   `test_sessions.py` derives from it) does not exclude. That is a `mirror.py` /
   `test_mirror.py` contract question, wholly inside another sub-plan's ownership.

**Fix suggestion (for sp-mirror-deploy's implementer, not for this sub-plan):**
exclude the `writers` collection from the wipe+rebuild count comparison and from
the replayable fingerprint the way `derived` is already excluded — note that
`tests/test_e2e_sim.py::replayable()` handles `derived` only and *does* include
`writers`, which is consistent because both of its fingerprints are taken after a
lease exists. For the breaker/generation-sweep failures, the call-count and
first-generation assertions need to be reconciled with whatever `mirror.py` gained
at 11:29 (a lease/generation write that now happens on a held-breaker tick would
explain both).

I did **not** edit any of those files: this is a read-only gate and they are
another sub-plan's property.

## 3. Live-mongod evidence (R-42 recipe, optional arm exercised)

Docker is available, so rather than accept the skip I provisioned a dedicated
mongod exactly per `docs/mongo.md` §1 — loopback bind `127.0.0.1:27317:27017`,
`--auth`, generated password, named volume — on a port that does not collide with
the two containers other sub-plans left running (27117/27217), ran the file with
`TOUCH_MONGO_URI` set, then removed the container and its volume.

```
test_live_mongod_arm
  ok: the live arm uses a name it constructed: touch_test_213328
  ok: the mirror reaches 'live' against a real mongod, got 'live'
  ok: the whole real corpus lands on a real server: []
  ok: …producing the SAME fingerprint as MemoryBackend  (GD-25)
  ok: double-ingest against a real mongod changes nothing (GD-25)
  ok: …and creates no documents: {'agents': 17, 'custom_state_events': 4,
      'legacy_events': 1289, 'records': 1807, 'run_nodes': 25, 'runs': 3,
      'sessions': 5, 'stream_meta': 15, 'usage': 528, 'writers': 1} == (same)
  ok: the real database really is empty
  ok: …a new writer takes the lease the wipe removed (GD-29)
  ok: wipe + --rebuild reproduces the corpus against a real mongod (GD-22)
  ok: dropping only the database this test constructed: touch_test_213328
```

Exit 0, zero skips. So the mirror arm is genuinely exercised, not merely
skip-shaped — and the MemoryBackend fingerprint that the bare-checkout arms rely
on is proven identical to a real server's.

## 4. Ownership / tree check

`git status --porcelain` shows `tests/` still wholly untracked (the directory was
never committed), `aggregator/`, `docs/`, `touch-visual/` likewise; the only file
in `tests/` with a post-gate mtime is `test_e2e_sim.py` (23:30:52) plus a
regenerated `__pycache__`. No file outside the sub-plan's ownership list was
modified by this attempt, and nothing was committed.

## Failures attributable to this sub-plan

None.
