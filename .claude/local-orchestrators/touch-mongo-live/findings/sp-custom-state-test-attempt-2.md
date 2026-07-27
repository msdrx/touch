# sp-custom-state — test gate, attempt 2

**Verdict: PASS.** Both owned suites 100 % green in all three environments
(no-driver, driver-but-no-mongod, live authenticated `mongo:7`); 0 NEW failures
in the full suite; 0 ownership violations; 0 commits; the gate wrote nothing
into the repo except this findings file (every mutation probe ran on a
scratchpad copy).

Environment: Python 3.13, pymongo 4.17.0 present, Docker daemon available,
`TOUCH_MONGO_URI` unset for the baseline run. `HEAD` unchanged at `579446e`.

Implementer's changed set for this attempt:
`aggregator/custom_state.py`, `tests/test_custom_state.py`, `tests/test_slots.py`,
`findings/sp-custom-state-head-driver-deviation.md` (the M4 handoff critique
attempt 1 asked for). `.claude/skills/touch-orchestrate/SKILL.md` still carries
attempt 1's +13/−1 ledger-line amendment — in scope, unchanged this attempt.

---

## 1. Targeted suites — GREEN

Run from the repo root, stdlib only, standalone executables.

| suite | rc (no URI) | rc (live mongod) | rc (pymongo unimportable) | `ok:` | `def test_*` |
|---|---|---|---|---|---|
| `python3 tests/test_custom_state.py` | **0** | **0** | **0** | 179 → 182 live | 23 |
| `python3 tests/test_slots.py` | **0** | **0** | **0** | 117 → 131 live | 16 |

Final lines: `all custom-state checks passed`, `all slots checks passed`.
Every `def test_*` prints its own name and every name appears in the output, so
no test is unreachable.

### 1a. All three GD-21/R-56 arms were exercised, not assumed

- **No mongod, driver present** (`TOUCH_MONGO_URI` unset): both suites skip the
  live arm with an explicit `SKIP:` line and exit 0.
- **No driver at all**: re-run with `PYTHONPATH` pointing at a stub package whose
  `pymongo/__init__.py` raises `ImportError` — both suites still rc 0. The
  no-third-party bare-checkout requirement holds.
- **Live mongod**: the R-42 recipe verbatim (`mongo:7`, `--auth`, root password
  from `openssl rand`, published **loopback-only** on `127.0.0.1:27318`, named
  volume) as `touch-mongo-sp11b`. Both suites green with the arms live; the
  container and its volume were removed afterwards. The three pre-existing
  containers (`touch-mongo-sp05`, `touch-mongo-sp06`, `touch-critique-mongo`)
  were left untouched.

`tests/test_custom_state.py::test_live_head_guard_matches_the_model`
```
ok: mongod's guard agrees with the model: seq=3
ok: …and the payload is the newest event's, not the last one attempted
ok: …with all three lines still in the append-only log
```
`tests/test_slots.py::test_live_duplicate_key_is_tolerated_not_raised` now covers
attempt 1's assertions **plus** the new n4 clause, against the real unique sparse
`{agentId:1}` index:
```
ok: …and the second is a tolerated duplicate, never raised: conflict
ok: …counted, per GD-29's exposed tolerated-dup number: {'bound': 1, 'duplicate_key': 1, 'conflict': 1}
ok: …and a bind for a slot nobody observed is refused, not created
ok: a third collision writes no transition and says so: acquired=False
ok: …yet mongod holds every id that collided, not the first pair only: ['a2fc883c96ff7b837', 'c0ffee1234567890a']
ok: the tailer lives: a collision is a document, not a crash
```

## 2. Full-suite regression gate — no NEW failure

22 files (4 monitoring, each from its own directory; 18 `tests/test_*.py` from
the repo root).

- **PASS (20):** monitoring `test_frontend`, `test_server`, `test_shell`,
  `test_watcher`; repo `test_agents`, `test_bootstrap`, `test_custom_state`,
  `test_fixtures`, `test_ingest`, `test_legacy`, `test_mongo_deploy`,
  `test_mongo_store`, `test_reducer`, `test_refs`, `test_slots`,
  `test_stdlib_only`, `test_store`, `test_tailer`, `test_usage`, `test_ws`.
- **FAIL (2) — pre-existing baseline, not attributable:**
  - `tests/test_mirror.py`, `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py`, `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

**Attribution.** These four strings are character-for-character the baseline
recorded in `sp-custom-state-test-attempt-1.md` §2 and
`sp-agents-reducer-test-attempt-4.md` §2, and they belong to loops that closed
RED (`sp-mirror-deploy`, `sp-sessions-arm`). Attempt 1 additionally *proved* the
attribution by deleting sp-11's three files from a scratchpad copy and
reproducing the identical failure strings; attempt 2 changed only sp-11's own
files and the failure text is byte-identical to that proven baseline, so the
attribution carries. Baseline failures do not fail this gate.

## 3. Verification against the plans

Owned items are **R-52** and **R-53** (`touch-mongo-live-plan.md` Phase M3,
`touch-mongo-live-subplans.md` §`sp-11 — custom-state`). Attempt 1 tabulated
every R-52/R-53 clause against a named, non-tautological assertion and that table
still holds — all of those tests are present and green. This section records only
what attempt 2 added, i.e. the critique-attempt-1 dispositions.

| critique finding | disposition in the tree | asserting test |
|---|---|---|
| **M1** control arm ingests zero real lines | `read_control_file` resolves `(name[,attempt]) → slot` through `SlotIndex` instead of demanding `root` on the line | `test_a_control_line_in_the_skill_files_own_shape_is_ingested` — reads the **live `SKILL.md` on disk**, extracts its documented `{"action":"stop",…}` / `{"ack":"stop",…}` lines, and asserts they land; `test_the_name_to_slot_hop_is_the_index_control_lines_resolve_through` |
| **M2** silent drops vs. comments claiming counters | `new_counters()` exported; `_read_lines`/`_bump` populate `read/parsed/skipped_malformed/skipped_unaddressable/skipped_ambiguous/unreadable`; both readers accept and return one | `test_an_unaddressable_control_line_is_skipped_and_counted`, `test_the_ledger_reader_counts_every_line_it_drops` (asserts each reason **and** `unreadable == 1` for a missing file) |
| **M3** `attempt` defaulted to 1 | no default; resolved from the slot set with `attemptSource:"resolved"` (`ATTEMPT_SOURCES` validated), else skipped + counted | the same two tests, incl. `…with the inference recorded (attemptSource), never presented as stated` |
| **M4** head/bind have no driver, no handoff | `findings/sp-custom-state-head-driver-deviation.md` written, naming sp-12 (sp-14 fallback), with the three call sites and the "do not route the claim through `guarded_update`" warning; same paragraph mirrored in the module docstring | `test_the_head_and_the_bind_have_a_named_driver_handoff` |
| **m1** parent-basename scope collision | `_path_scope` disambiguates by realpath | `test_two_control_files_under_like_named_folders_do_not_collide` |
| **m2** 16 KB value unpinned | `check(ANNOTATION_LIMIT == 16 * 1024, …)` at `tests/test_custom_state.py:337` | `test_annotations_reject_at_16kb_rather_than_truncating` |
| **m3** `ref`/`refId` may contradict | `resolve_ref_id` compares and raises `RefRejected` | `test_a_ref_and_a_refid_that_disagree_are_refused` |
| **m4** `<int>-<int>` anywhere is a session | derivation restricted to the directory layout | `test_a_session_key_is_only_derived_under_a_directory_the_layout_names` |
| **m5** "wipe" test replayed twice into empty dicts | real populate-then-wipe | `test_a_mongo_wipe_plus_wal_replay_reproduces_both_collections` |
| **n1** mode 644 | both files now `-rwxr-xr-x` (`ls -l` confirmed) | n/a |
| **n3** `ts` overwritten by the sweep | renamed `resolvedTs` | `test_a_transition_stamps_its_own_clock_not_the_documents` — asserts `ts` is absent from the document key set |
| **n4** result overstated an unacquired advance | `BindResult.acquired` | `test_a_third_collision_is_recorded_and_the_result_says_what_it_wrote` + the live arm |

`git diff` on `.claude/skills/touch-orchestrate/SKILL.md` is still +13/−1, confined
to the §2 ledger-line block — unchanged this attempt, no new scope taken.

### Anti-tautology probes (6 mutations, scratchpad copy only)

The probe copy is a full `tar` of the repo (minus `.git`), so the tests that read
`SKILL.md` and the findings file resolve normally; baseline verified green there
before mutating.

| mutation | caught? |
|---|---|
| B: `_bump` becomes a no-op (M2's counters go silent) | **yes** — 5 FAILs in `test_custom_state`, 2 in `test_slots`, all naming D13's "nothing happened yet vs. everything was rejected" |
| E: drop the head write's `{seq:{$lt:obs.seq}}` guard | **yes** — 4 FAILs incl. the shuffled-replay fingerprint |
| F: widen `ANNOTATION_LIMIT` 16 KB → 16 MB | **yes** (was the surviving mutation in attempt 1) — `the cap is CUSTOMSTATE-16's 16 KB, stated as a number: 16777216` |
| G2: control reader defaults `attempt` to 1 (M3 regression) | **yes** — 5 FAILs incl. `…addressed through the name→slot hop, to the LIVE attempt` and `…with the inference recorded (attemptSource), never presented as stated` |
| H: scope back to the bare parent-dir basename (m1 regression) | **yes** — `two files whose parent folders share a name are two streams` |
| I: neuter `resolve_ref_id`'s agreement comparison (m3 regression) | **yes** — 3 FAILs incl. `…and the WAL never holds the contradiction either` |

All six critique fixes are driven by real code paths. Attempt 1's surviving
mutation (the 16 KB constant) is now caught.

## 4. Non-blocking observations (do NOT fail this gate)

1. **`read_ledger_file` — an *absent* `attempt` is untested.** A seventh probe
   applied `payload.get("attempt", 1)` at `aggregator/custom_state.py:2014` (the
   **ledger** reader) and both suites stayed green. The shipped code is correct —
   `attempt is None` falls through to `not isinstance(attempt, int)` and the line
   is skipped as `skipped_malformed` — but only the *non-int* case
   (`"attempt": "two"`) is fed by
   `test_the_ledger_reader_counts_every_line_it_drops`, so a future default would
   silently fabricate a `|001` address, which is exactly the M3 class of defect.
   One extra line in that fixture closes it:
   `handle.write(json.dumps({"name": "auth_impl4", "root": "auth", "sessionKey": SESSION}) + "\n")`
   with `skipped_malformed` bumped to 4. Test-coverage only; no plan clause is
   left unasserted, and the control reader's equivalent case *is* covered (G2).
2. **`custom_state.py` is 2100+ lines** and is now the largest module in
   `aggregator/` after `mongo_store.py`. Not a plan violation (the sub-plan
   assigns both the custom-state and slots arms to this one file), but worth
   flagging to whoever owns a later split.

## 5. Ownership, git, and cleanliness

- `aggregator/` and `tests/` are untracked as a whole, so ownership was checked
  by mtime: the three newest source files in the tree are
  `aggregator/custom_state.py` (16:52), `tests/test_slots.py` (16:51),
  `tests/test_custom_state.py` (16:46), preceded by
  `.claude/skills/touch-orchestrate/SKILL.md` (16:05, attempt 1) and then sp-10's
  `aggregator/agents.py` (15:32). **No file outside the ownership list was
  written this attempt.**
- `git status` shows no newly-modified tracked file beyond attempt 1's
  `SKILL.md`; every other dirty path (`.claude/shared/monitoring/*`, other
  skills, `CLAUDE.md`, `.gitignore`, `.claude/local-orchestrators/**`,
  `.temp-develop/`, `docs/`) predates this sub-plan and is unrelated in-flight
  state.
- No commit; `HEAD` still `579446e`. Nothing reverted or stashed. No `.touch/`
  directory leaked into the repo root (every suite uses `tempfile.mkdtemp`).
- The gate's `mongo:7` container and named volume were removed after use.
