# sp-repo-bootstrap — test gate, attempt 2

**Verdict: PASS** (green). Zero failures, zero baseline failures. 2 non-blocking
advisories in §6.

Sub-plan identity: `sp-repo-bootstrap` == `sp-01 — repo-bootstrap`
(`touch-mongo-live-subplans.md:88-109`). Owned items: R-01:gitignore (base),
R-02 (base — the one commit exception), R-42:gitignore (amendment).
Shared decisions: SD-3, SD-5, SD-6, GD-2, RUNSTATE-18.

Implementer-reported changes this attempt:
`/home/laniakea/Projects/touch/tests/test_bootstrap.py`,
`/home/laniakea/Projects/touch/.gitignore`.

---

## 1. Targeted suite (must be 100% green)

| Test file | Command | Exit |
|---|---|---|
| `tests/test_bootstrap.py` (new, owned) | `cd /home/laniakea/Projects/touch && python3 tests/test_bootstrap.py` | **0** — "all sp-repo-bootstrap tests passed" |

All 9 test functions green, **57** individual `ok:` lines, zero `FAIL:` lines:
`test_gitignore_entries` (8), `test_check_ignore_positive` (12),
`test_check_ignore_negative` (8), `test_head_exists` (4),
`test_branch_is_main` (3), `test_commit_identity` (7),
`test_no_tracked_watcher_state` (2), `test_event_streams_tracked` (2),
`test_empty_dirs_have_gitkeep` (19).

No other test file is owned here. `test_shell.py`'s SD-3 mirror guard is sp-03's
half and was correctly left untouched (`git status` shows no change under
`.claude/shared/monitoring/`).

## 2. Full-suite regression gate

Exact gate command run from the repo root:

```
rc=0; for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done; for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done; exit $rc
```

`SUITE_EXIT=0`. Per-file exits:

- `.claude/shared/monitoring/tests/test_frontend.py` → 0
- `.claude/shared/monitoring/tests/test_server.py` → 0
- `.claude/shared/monitoring/tests/test_shell.py` → 0
- `.claude/shared/monitoring/tests/test_watcher.py` → 0
- `tests/test_bootstrap.py` → 0

No services started, no packages installed. This sub-plan adds no pymongo /
mongod surface at all (the new test imports only `os`, `subprocess`, `sys`,
`pathlib`), so the GD-21/R-56 no-mongod arm is not exercised and cannot regress.
**No new failures.**

## 3. Attempt-1 critique findings — disposition (each re-verified independently)

- **F1 (major, blocking) — FIXED.** `test_repo_local_identity` is gone; its
  replacement `test_commit_identity` (`tests/test_bootstrap.py:142-161`) asserts
  the clone-durable half (C1/C2 commits carry a non-empty author name and an
  `@`-bearing author email, matched by subject via `git log --format=%s\x1f%an\x1f%ae`)
  and treats the `.git/config` override as skip-when-absent.
  **Verified by an actual fresh clone**: `git clone /home/laniakea/Projects/touch <scratch>`,
  local identity unset, no `--global` identity in this environment
  (`git config --global --get user.email` → rc 1) → file exits **0** and prints
  `skip: no repo-local identity (fresh clone / CI checkout)` after the 4 durable
  author checks. The SD-2 "suite is never red between sub-plans" contract now
  holds on a bare checkout.
- **F3 (minor) — FIXED.** `test_check_ignore_negative` (`:108-113`) now asserts
  `returncode == 1 and proc.stdout == ""`, so a `128` fatal can no longer pass
  the negative half vacuously. `test_check_ignore_positive` (`:100-105`) got the
  mirror tightening (`rc == 0` **and** non-empty stdout).
- **F4 (minor) — FIXED.** `test_event_streams_tracked` (`:179-186`) replaced the
  `>= 4` magic floor with set containment `tracked >= on_disk` against
  `glob("*/events.jsonl")`, plus a non-empty guard. It reported 4 on-disk
  streams, none missing, and now auto-covers a 5th task folder.
- **F5 (nit) — FIXED.** `test_empty_dirs_have_gitkeep` (`:192-205`) sweeps all
  five subdirs (`plan`, `report`, `findings`, `orch-scripts`, `context`) with the
  `is_dir()` no-op guard; 18 dirs across 5 task folders all clone-survivable.
- **F6 (nit) — FIXED.** `.gitignore:31-33` gained the three-line escape-hatch
  comment above `*.bson` naming `!tests/fixtures/**/*.bson`. Strictly a comment:
  `git diff -- .gitignore` is +3/-0 and every SD-3 entry line is byte-identical
  to before (all 8 verbatim checks green).
- **F2 (minor), F7, F8 — correctly NOT actioned here.** F2's fix lands in
  CLAUDE.md, F7's in `.mailmap`, F8's in `.gitignore` + CLAUDE.md prose — all
  owned by sp-15 per SD-7, and history is never rewritten. Deferring them is the
  in-scope behaviour, not a miss. Re-recorded in §6 so sp-15 inherits them.

## 4. Item-by-item verification against the plans

**R-01 + R-42:gitignore.** All eight SD-3 entries present as exact standalone
lines (`.touch/`, `.touch*/`, `.claude/settings.local.json`, `*.pid`,
`.claude/local-orchestrators/*/.watcher-state.json`, `mongo-data/`,
`mongo-dump/`, `*.bson`), edit still strictly additive — the three pre-existing
monitoring rules survive. Behaviour asserted through git, not text: 12/12
MUST_IGNORE paths ignored (incl. `.touch-dev/x` for the TOUCH_STATE_DIR variant,
`mongo-data/x`, `mongo-dump/x`, `dump.bson`) and 8/8 MUST_NOT_IGNORE paths not
ignored — the R-01 negative half (nothing ignores
`.claude/local-orchestrators/` itself, nor `events.jsonl`, `plan/`, `report/`,
`findings/`, `orch-config.json` beneath it). Independent cross-check:
`git ls-files -i -c --exclude-standard` is **empty** — no new rule shadows a
tracked file.

**R-02.** Unchanged from attempt 1 and re-confirmed: `refs/heads` = `main` only
(`master` gone), rename preceded both commits, `7444331 "tooling and docs"` (C1)
→ `579446e "orchestration history"` (C2) in order, both authored
`Michael Sadradze <michaelsadradze@gmail.com>`, and zero `.watcher-state.json`
in either `git ls-files` or `git ls-tree -r HEAD`. The forced C1-content
deviation (the user's pre-existing initial commit `97ee7d7` already carried
GD-2's C1 file set into history, so the plans' "zero commits" premise was
already false) stands as documented and accepted by the attempt-1 critique.
SD-6 holds: still exactly two commits, `git stash list` empty, nothing reverted.

**Tests assert intended behaviour, not tautologies — proven by mutation**, in a
throwaway clone so the live repo was never touched:

1. appending `.claude/local-orchestrators/*/events.jsonl` to `.gitignore` →
   `FAIL: NOT ignored (history): .claude/local-orchestrators/x/events.jsonl (rc=0)`,
   exit 1. The single most consequential CLAUDE.md invariant is genuinely guarded.
2. `git branch -m main master` → 3 FAILs (`checked-out branch is main`,
   `refs/heads/main exists`, `no refs/heads/master remains`), exit 1.

The suite interrogates git (`check-ignore`, `rev-parse`, `for-each-ref`,
`ls-files`, `ls-tree`, `log`, `config --local`) rather than re-reading the string
it wrote; `MUST_IGNORE`/`MUST_NOT_IGNORE` paths are deliberately hypothetical and
untracked (documented at `:33-35`) because `check-ignore` consults the index; the
`git()` helper strips `GIT_*` from the environment so assertions cannot be
spoofed by an inherited hook/worktree env.

## 5. Ownership / scope check

`git status --porcelain --untracked-files=all` at gate time:

```
 M .claude/local-orchestrators/touch-mongo-live/events.jsonl
 M .gitignore
?? .claude/local-orchestrators/touch-mongo-live/findings/sp-repo-bootstrap-critique-attempt-1.md
?? .claude/local-orchestrators/touch-mongo-live/findings/sp-repo-bootstrap-test-attempt-1.md
?? tests/test_bootstrap.py
```

Against the ownership list (`.gitignore`, `.git/config`/branch state, `.gitkeep`
files, new `tests/test_bootstrap.py`): clean. The two owned files are the only
implementer-touched paths; `events.jsonl` is this run's live monitoring stream;
the two `findings/` files are gate/critique output (task state, required).
**No file outside the ownership list was modified**, and nothing under
`.claude/shared/monitoring/`, `.claude/skills/`, or any other sub-plan's future
territory was touched. Mode 0755 on the new test matches the standalone-executable
convention.

## 6. Advisories (non-blocking, do NOT fail this gate)

- **A1 — the F6 `.gitignore` comment and `tests/test_bootstrap.py` are
  uncommitted, by design.** SD-6 caps this sub-plan at *exactly* C1 + C2, both
  already taken in attempt 1, so a third commit would itself be the violation.
  Consequence to be aware of: a fresh clone of HEAD has neither the comment nor
  the test file (my clone probe copied the test in). Whoever takes the next
  legitimate commit (per SD-6 that is nobody in this pass; realistically the
  user or sp-15) should include `tests/` and the `.gitignore` comment. Not a
  sp-01 obligation and explicitly not a gate failure.
- **A2 — inherited, unactioned, owned by sp-15 (SD-7).** F2: commit `579446e`'s
  message misstates the SD-5 scoped gate as inapplicable when C2 did stage paths
  inside the live `touch-mongo-live` watcher's `ORCH_STATE_DIR`; the outcome was
  nevertheless safe (`events.jsonl` excluded, checkpoint staged as an index-only
  removal) but the wrong reasoning is now immutable and needs a correcting line
  in CLAUDE.md. F7: `.mailmap` for the two identities of one human
  (`msdrx <sadradzemikheil@gmail.com>` vs `Michael Sadradze
  <michaelsadradze@gmail.com>`). F8: `.temp-develop/` is absent from `.gitignore`
  and `.temp-develop/synthesis_failed.png` (594 KB) is tracked from `97ee7d7`.
