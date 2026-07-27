# sp-repo-bootstrap — test gate, attempt 1

**Verdict: PASS** (green). 1 non-blocking portability advisory recorded in §5.

Sub-plan identity: `sp-repo-bootstrap` == `sp-01 — repo-bootstrap` in
`.claude/local-orchestrators/touch-mongo-live/plan/touch-mongo-live-subplans.md:88-109`
(the subplans file uses the `sp-01` numbering; matched by title/owned-files, not by the
monitor plan id, which does not appear literally in that file).

Owned items: R-01:gitignore (base), R-02 (base — the one commit exception),
R-42:gitignore (amendment). Shared decisions: SD-3, SD-5, SD-6, GD-2.

---

## 1. Targeted suite (must be 100% green)

| Test file | Command | Exit |
|---|---|---|
| `tests/test_bootstrap.py` (new, owned) | `cd /home/laniakea/Projects/touch && python3 tests/test_bootstrap.py` | **0** — "all sp-repo-bootstrap tests passed" |

All 9 test functions green, 47 individual assertions, zero FAIL lines:
`test_gitignore_entries` (8 ok), `test_check_ignore_positive` (12 ok),
`test_check_ignore_negative` (8 ok), `test_head_exists` (4 ok),
`test_branch_is_main` (3 ok), `test_repo_local_identity` (3 ok),
`test_no_tracked_watcher_state` (2 ok), `test_event_streams_tracked` (1 ok),
`test_empty_dirs_have_gitkeep` (10 ok).

No other test file is owned by this sub-plan. In particular `test_shell.py`'s
SD-3 mirror guard is **sp-03's** half (SD-3 explicitly splits it), and the
implementer correctly left `.claude/shared/monitoring/tests/test_shell.py`
untouched — that guard still asserts only the two pre-existing module-dir
entries, which is the expected pre-sp-03 state, not a miss.

## 2. Full-suite regression gate

Exact gate command run from the repo root:

```
rc=0; for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done; for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc
```

`SUITE_EXIT=0`. Per-file evidence (run individually as well):

- `test_frontend.py` → 0, "all assertions passed"
- `test_server.py` → 0, "all 16 tests passed"
- `test_shell.py` → 0, "all sp-shell tests passed"
- `test_watcher.py` → 0, "ALL WATCHER TESTS PASSED"
- `tests/test_bootstrap.py` → 0

No services were started, no packages installed. Nothing in this sub-plan
imports pymongo or touches Mongo, so the GD-21/R-56 no-mongod arm is not
exercised here and cannot regress. **No new failures. No baseline failures.**

## 3. Item-by-item verification against the plans

**R-01 (base) + R-42:gitignore (amendment) — `.gitignore` hardening.** All eight
SD-3 entries are present verbatim, one per line, in a strictly additive edit
that preserved the three pre-existing monitoring rules
(`.claude/shared/monitoring/events.jsonl`,
`.claude/shared/monitoring/.watcher-state.json`,
`.claude/local-orchestrators/*/*.log`). Behaviour verified through git itself,
not by reading text: `git check-ignore -q` returns 0 for all 12 MUST_IGNORE
paths (incl. `.touch-dev/x` proving the `.touch*/` TOUCH_STATE_DIR variant
coverage, `mongo-data/x`, `mongo-dump/x`, `dump.bson`) and non-zero for all 8
MUST_NOT_IGNORE paths — the R-01 negative half (nothing ignores
`.claude/local-orchestrators/` itself, nor `events.jsonl`, `plan/`, `report/`,
`findings/`, `orch-config.json` beneath it) is asserted, as the item requires.
Independent cross-check: `git ls-files -i -c --exclude-standard` is **empty**
(no tracked file is now ignored) and no `.log`, `*.pid`, or
`settings.local.json` path is tracked.

**R-02 (base) — repository bootstrap.** Verified directly:

- Branch: `refs/heads/main` exists, is checked out, `refs/heads/master` is gone.
  Reflog confirms the rename happened *before* both commits
  (`97ee7d7 … Branch: renamed refs/heads/master to refs/heads/main`, 15:23:16),
  as GD-2 demands.
- Repo-local identity: `.git/config` gained exactly one `[user]` section
  (`Michael Sadradze` / `michaelsadradze@gmail.com`) and nothing else — no
  stray core/excludesfile/hook tampering.
- Commits: `7444331 "tooling and docs"` (C1) then `579446e "orchestration
  history"` (C2), in that order, both authored with the new identity.
- **C2 contains no `.watcher-state.json`** — in fact C2 *removes* the four that
  the pre-existing initial commit had tracked (`D` status on
  touch-aggregator/, touch-full-recon/, touch-mongo-live/, touch-repo-recon/
  checkpoints). `git ls-files` and `git ls-tree -r HEAD` both confirm zero
  tracked checkpoints now. This is stronger than the literal requirement and is
  the correct reading of GD-2 ("ignored by then").
- C1 file set: only `.gitignore`. This is a **justified, documented deviation**
  from GD-2's verbatim C1 list, not a defect: a pre-existing initial commit
  `97ee7d7 "researche source and generate implementation plan"` (author
  `msdrx <sadradzemikheil@gmail.com>`, 19:07:58 +0400, 65 files) already carried
  README.md, CLAUDE.md, inception.md, `.claude/settings.json`,
  `.claude/statusline.sh`, `.claude/skills/**` and
  `.claude/shared/monitoring/**` into history — i.e. the "zero commits"
  precondition the plans were written against no longer held when this sub-plan
  ran. The implementer detected this and recorded the reasoning in C1's commit
  message body. Net content of history is identical to what GD-2 intends.
  Nothing in the tree assumed a HEAD before C1.
- SD-5 / GD-1 scoped commit gate: honoured in effect. C2 committed only
  `orch-config.json`, `orch-scripts/implement.workflow.js`,
  `plan/touch-mongo-live-subplans.md`, three `.gitkeep`s and the four checkpoint
  deletions — the in-flight `touch-mongo-live/events.jsonl` (actively appended
  to by this pass's live watcher/status writers) was deliberately **left
  uncommitted**, so no mid-write stream entered history.
- SD-6 commit boundary: exactly two commits were made and no more; `git stash
  list` is empty and `.git/refs/stash` does not exist — nothing was
  reverted or stashed.
- RUNSTATE-18 `.gitkeep`s: three added and tracked
  (`touch-aggregator/report/`, `touch-repo-recon/plan/`,
  `touch-repo-recon/report/`). `touch-full-recon/report/` correctly got none —
  it already holds `research-report.html`. `test_empty_dirs_have_gitkeep`
  enumerates *every* per-task `plan/` and `report/` dir dynamically and asserts
  each has ≥1 tracked file, so all 9 survive a clone; the dynamic enumeration
  also means a future new task folder is covered without editing the test.

**Tests assert intended behaviour, not tautologies.** The suite interrogates git
(`check-ignore`, `rev-parse`, `for-each-ref`, `ls-files`, `ls-tree`,
`config --local`) rather than re-reading the string it just wrote; the
`check-ignore` paths are deliberately hypothetical/untracked (documented at
`tests/test_bootstrap.py:33-35`) because `check-ignore` consults the index and a
tracked path would answer "not ignored" regardless of rules — a real trap, and
avoided. Both a positive and a negative direction are asserted for the ignore
rules, and for the branch (`main` present *and* `master` absent). The `git()`
helper strips `GIT_*` from the environment, so the assertions cannot be spoofed
by an inherited hook/worktree env. Verified empirically that the suite is not
vacuous: unsetting the repo-local identity in a throwaway clone produced 3
FAILs and exit 1 (see §5), i.e. the assertions do bite.

## 4. Ownership / scope check

`git status --porcelain --untracked-files=all` at gate time:

```
 M .claude/local-orchestrators/touch-mongo-live/events.jsonl
?? tests/test_bootstrap.py
```

Against the declared ownership list — `.gitignore`, `.git/config` + branch
state, `.gitkeep`s in empty task dirs, new `tests/test_bootstrap.py` — this is
clean: `.gitignore` and the three `.gitkeep`s are inside C1/C2 (hence not shown
as dirty), `tests/test_bootstrap.py` is the new owned file, and
`.git/config` holds only the `[user]` addition. The lone `M` is
`touch-mongo-live/events.jsonl`, the live monitoring stream written by
`status.sh`/the watcher for this very run — in-flight orchestrator state, not an
implementer edit. **No file outside the ownership list was modified.** File mode
0755 on the new test matches the "each file is a standalone executable"
convention.

## 5. Advisory (non-blocking, does NOT fail this gate)

`test_repo_local_identity` (`tests/test_bootstrap.py:132-139`) asserts
`git config --local --get user.name/user.email` are set. Repo-local config lives
in `.git/config`, which is **never part of a clone or an archive**, so this test
is not portable: probed by cloning the repo to a scratch dir and clearing the
local identity, the file exits 1 with

```
FAILED (3):
  - repo-local user.name is set
  - repo-local user.email is set
  - repo-local user.email looks like an address
```

The other 44 assertions pass in that clone. This is *not* counted as a gate
failure: both prescribed gate commands (§1, §2) are green in the live repo the
plan governs, the "bare checkout / no services / no third-party packages"
baseline clause is about runtime dependencies (Mongo, pymongo, daemons) of which
this sub-plan has none, and GD-2 genuinely mandates the repo-local identity, so
guarding it here is defensible. It will, however, break the first CI job or
fresh-clone run of the suite.

Concrete fix, for whoever owns the follow-up (suggest sp-14/sp-15, since
`tests/test_bootstrap.py` stays sp-01's file — do not widen scope now):
assert the part of GD-2 that actually travels with history — that C1's and C2's
commits carry a non-empty author name and an `@`-bearing author email
(`git log -2 --format='%an|%ae'`) — and keep the `--local` config check as a
skip-when-absent advisory, e.g.:

```python
if git("config", "--local", "--get", "user.email").returncode != 0:
    print("  skip: no repo-local identity (fresh clone) - checking commit authorship instead")
else:
    ...existing assertions...
# always:
for line in git_out("log", "-2", "--format=%an|%ae").splitlines():
    name, _, email = line.partition("|")
    check(name and "@" in email, f"commit is attributed: {line}")
```

Minor, out of scope, recorded only: `.temp-develop/synthesis_failed.png` is
tracked from the pre-existing initial commit and is not covered by the SD-3
entry list; if it is meant to be transient, a future pass should add the ignore
and `git rm --cached` it. Not a sp-01 obligation.
