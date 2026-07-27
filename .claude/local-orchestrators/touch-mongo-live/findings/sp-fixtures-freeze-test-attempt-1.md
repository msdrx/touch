# sp-fixtures-freeze — TEST GATE, attempt 1: **PASS**

Read-only gate. No source, test or fixture file was edited, created or moved by
this agent. Only artifact written: this findings file (plus the interpreter's
`tests/__pycache__/`, which `.gitignore:38` already ignores).

Environment: Python 3.13, repo `/home/laniakea/Projects/touch`, no services
started, no packages installed. Bare-checkout conditions held throughout.

---

## 1. Targeted suite (must be 100% green)

Owned test file for this sub-plan: `tests/test_fixtures.py` (the manifest test
*is* the test, per the sub-plan's "**Test:** manifest test is the test").

```
$ python3 tests/test_fixtures.py
… 181 `ok:` checks, 0 FAIL …
all sp-fixtures-freeze tests passed
EXIT=0
```

15 test functions, all green, **181** individual checks:

| test | checks the freeze property |
|---|---|
| `test_manifest_complete_and_stable` | 70 manifest entries; bidirectional (nothing manifested-but-missing, nothing on-disk-but-unmanifested); every byte hash matches |
| `test_fixtures_are_trackable` | `git check-ignore` proves no fixture is gitignored (so the freeze survives a clone) |
| `test_newline_conventions` | `.jsonl` end with `\n`; single-object `.json`/`.meta.json` keep their **missing** trailing newline |
| `test_every_jsonl_line_parses` | 3250 records parse — no torn tail anywhere |
| `test_run_wf_829e6f58_shape` | R-03 completed multi-session run: 8 transcripts / 7 ids, 7 workflow metas + 2 Task-tool metas (both meta *shapes*), 7+7 journal, snapshot in the other session dir, `agentCount:7` vs 9 `workflowProgress` rows, 4 spill bodies |
| `test_cross_session_disjoint_continuations` | R-03-as-amended: 223-record + 2-record fragments, zero uuid overlap, `parentUuid` → fragment 1's last uuid, sessionId rewritten, 17 min start gap / 0.8 min seam |
| `test_legacy_anchors` | all four real streams frozen with exact line counts (590/276/320/103), unattributable counts (33/35/41/20), per-line anchors, ts-format + ts-inversion specimens, duplicate terminal pairs |
| `test_legacy_terminal_conflicts` | R-58/SD-4: the two fabricated-`failed`→corrective-`done` pairs (full-recon 255→276, mongo-live 275→286) with "nothing else touches the plan in between"; the uncorrected 319; the two **genuine** repo-recon failures that must stay `failed` |
| `test_unattributable_twelve_of_first_130` | the amendment's "12 of 130" figure, cross-checked against `legacy/anchors.json` |
| `test_killed_run_shape` | R-41 `wf_455b348c-e17`: 9 started / 2 result, 3 duplicated `(type,key)` retry pairs, `status:killed` + abort error, `agentCount 6` vs 9 started |
| `test_live_run_shape_has_no_snapshot` | R-41 live shape: **no** `workflows/` dir and no `<runId>.json` anywhere, 9 started / 7 result, 9 transcripts + 9 metas |
| `test_r58_replay_journals_have_no_verdict` | `wf_930e210a` + `wf_cca84d59`: 7/7 and 6/6, **no** passed/approved verdict token in either journal, snapshot `completed`, `timestamp == startTime + durationMs` |
| `test_record_specimens` | the 877 395-byte single record, 33 dotted-key `file-history-snapshot` records + index rows, the queue-operation/user pair (no-uuid → uuid) |
| `test_discovery_fixtures` | 4 foreign `/tmp` slugs as negative discovery fixtures; the pid-named registry file proving `(pid, procStart)` identity |
| `test_no_credentials` | credential-shape scan over the whole corpus |

**Non-tautology audit.** These are not shape-echoes of the test's own
constants. Every numeric anchor is asserted against a value the test does not
write (line counts, byte counts, uuid-chain identity, `agentCount` vs
`workflowProgress` length, cross-file `parentUuid` equality, ts arithmetic in
minutes). The manifest test is bidirectional, so it cannot be satisfied by
deleting a fixture. `test_fixtures_are_trackable` shells out to real `git`.
Several checks assert *negatives* that only hold for genuine harness output
(no `w` field in the legacy streams, no `workflows/` dir in the live run, no
`.meta.json` on the continuation fragment, no verdict token in the replay
journals) — a fabricated fixture would fail them.

## 2. Full-suite regression gate

```
$ cd /home/laniakea/Projects/touch && rc=0
  for t in .claude/shared/monitoring/tests/test_*.py; do (cd $(dirname $t) && python3 $(basename $t)) || rc=1; done
  for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc

PASS .claude/shared/monitoring/tests/test_frontend.py
PASS .claude/shared/monitoring/tests/test_server.py
PASS .claude/shared/monitoring/tests/test_shell.py
PASS .claude/shared/monitoring/tests/test_watcher.py
PASS tests/test_bootstrap.py     (sp-repo-bootstrap, prior sub-plan — still green)
PASS tests/test_fixtures.py
SUITE_RC=0
```

Six of six green. All four monitoring baselines still green, so nothing
regressed. No NEW failure of any kind. No skips were required: this sub-plan's
test imports only `hashlib, json, re, subprocess, sys, datetime, pathlib` — no
pymongo, no mongod, no network, no server — so the GD-21/R-56 no-mongod arm is
vacuously satisfied here and the suite is bare-checkout-clean.

Robustness spot-check (not required, done anyway): `cd /tmp && python3
/home/laniakea/Projects/touch/tests/test_fixtures.py` → exit 0. Paths derive
from `Path(__file__).resolve().parents[1]`, so the file is cwd-independent and
survives being run from either the repo root or a runner elsewhere.

## 3. Plan conformance

Against `touch-mongo-live-subplans.md` § **sp-02 — fixtures-freeze** and items
R-03 (base, amended wording), R-41 (amendment), R-58:fixtures.

**Owned-file inventory present in the tree** — 72 files under
`tests/fixtures/` (70 manifested + `MANIFEST.sha256` + `PROVENANCE.md`), 8.0 MiB,
plus `tests/test_fixtures.py`. Every corpus named in the sub-plan exists:

- `run-wf_829e6f58/` — journal, 8 agent transcripts incl. both `a2fc883c…`
  disjoint continuations, 7 workflow metas + 2 Task-tool metas, terminal
  snapshot, 4 `tool-results/*.txt`.
- `legacy/` — all four verbatim streams + `anchors.json`.
- R-41 additions — the cross-session `a2fc883c…` pair (223-line + 2-line, no
  meta); `wf_455b348c-e17/` (3-key retry, killed, agentCount 6 vs 9); a
  live-run-shape dir (journal + 9 agents + 9 metas, **no** `<runId>.json`);
  dotted-key `file-history-snapshot` records + index; the 877 KB line; the
  queue-operation/user pair; four foreign `/tmp` slug dirs; `sessions/15934.json`.
- R-58 replay set — `wf_930e210a` + `wf_cca84d59` journals and snapshots, plus
  the `touch-mongo-live` and `touch-full-recon` stream lines with the 12
  unattributable and the failed-then-done correction lines.

**Verbatim-bytes requirement independently verified by this gate** (not merely
asserted by the test being reviewed):

- The three settled legacy streams are byte-identical to their live sources:
  `sha256` of `tests/fixtures/legacy/{touch-aggregator,touch-repo-recon,touch-full-recon}-events.jsonl`
  == `sha256` of `.claude/local-orchestrators/<task>/events.jsonl`.
  (`touch-mongo-live-events.jsonl` is deliberately a pinned copy of a stream
  this very run is still appending to — divergence there is the point.)
- Every file under `run-wf_829e6f58/` `cmp`s equal to its source under
  `~/.claude/projects/-home-laniakea-Projects-touch/` — 0 diffs, 0 missing
  sources.
- Under `mirror/`: 23 files byte-identical to source, 2 with no live source
  (the settled-history copies), and 2 that differ — the **still-growing**
  `live-run-shape` journal and `agent-a6a927f2ce55ad975.jsonl`. Both were
  checked with `cmp -n <fixture-size>` and are **exact prefixes** of the live
  files (26 523 → 30 998 B and 335 032 → 538 545 B). Append-only prefix of a
  live file is verbatim freezing of an in-flight run, which is precisely the
  "live run, no terminal snapshot" specimen R-41 asks for, and it incidentally
  confirms the SD-10 append-only assumption on real data.
- The discovery fixtures all `cmp` equal to their foreign-slug sources.
- Sanitisation condition honoured: `PROVENANCE.md` documents the credential
  scan, records that the only hits are doc/placeholder strings
  (`$ANTHROPIC_API_KEY`, `'your-api-key'`, `'ghp_your_new_github_token'`), and
  therefore that nothing was altered — matching R-03's "sanitize only if
  inspection finds credentials". `test_no_credentials` re-runs the scan.

**The R-03 "9 agent transcripts" prose discrepancy is resolved, not papered
over.** On disk the run has 8 transcripts over 7 distinct agentIds; the "9" is
the snapshot's `workflowProgress` node-row count (`agentCount` is 7,
SESSIONJSONL-7). Both `PROVENANCE.md` ("Counting note") and the test assert the
real on-disk numbers *and* the 9 node rows, so the reconciliation is recorded in
two places and no downstream sub-plan can re-derive the corpus from the prose.
Similarly the sub-plan says "3 `tool-results/*.txt`" where 4 exist and are
frozen — a superset, and the extra spill body only makes pointer records
resolvable. Both are documentation-vs-reality drifts in the plan text, correctly
resolved in favour of the real bytes (fixtures must be verbatim); neither is a
defect in the delivered work.

**Manifest discipline.** `MANIFEST.sha256` is checked in, `<sha256>  <path>`
throughout, 70 entries, excluding only itself and `PROVENANCE.md` (documented,
with the rationale that prose may be improved without re-freezing). The
"regenerating the manifest is a deliberate act, never a fix for a red test"
rule is written into `PROVENANCE.md`, as is the read-only rule for every
downstream sub-plan.

**No edits outside owned files.** `git status --porcelain`:

```
 M .claude/local-orchestrators/touch-mongo-live/events.jsonl   (live monitoring stream)
 M .gitignore                                                  (sp-repo-bootstrap, mtime 15:37:43)
?? .claude/local-orchestrators/touch-mongo-live/findings/…      (orchestrator findings)
?? tests/                                                      (test_bootstrap.py = sp-01; test_fixtures.py + fixtures/ = THIS sub-plan)
```

`.gitignore`'s working-tree change is a comment about `*.bson` written at
15:37:43, before `tests/test_fixtures.py` (16:10:27) — it belongs to
sp-repo-bootstrap, whose ownership list includes `.gitignore`; it is not
attributable to this sub-plan. Nothing under `aggregator/`, `touch-visual/`,
`docs/`, `.claude/shared/monitoring/` or `.claude/skills/` was touched. No
commit was made (SD-6: only sp-01 commits) — `git log` still shows exactly the
three expected commits (`researche source…`, `tooling and docs`,
`orchestration history`). Working tree not reverted or stashed.

## 4. Failures

None. Nothing to attribute, nothing to fix.

## Verdict

**PASS** — targeted suite 181/181 checks green, full suite 6/6 files green with
zero new failures, every owned item present in the tree, verbatim-byte fidelity
independently re-verified against the live sources, and no edits outside the
sub-plan's ownership.
