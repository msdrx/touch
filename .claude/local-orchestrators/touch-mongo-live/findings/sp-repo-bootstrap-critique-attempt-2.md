# sp-repo-bootstrap — adversarial critique, attempt 2

**Verdict: APPROVED** — 0 blockers, 0 majors, 2 minors, 5 nits.

Reviewed (full content, since the tree is largely untracked):

- `/home/laniakea/Projects/touch/.gitignore` — working-tree diff vs HEAD is
  **+3/-0** (a three-line comment above `*.bson`); the eight SD-3 rules
  themselves are in history via C1 (`7444331`, +23/-0 on `.gitignore`).
- `/home/laniakea/Projects/touch/tests/test_bootstrap.py` — new, untracked,
  mode 0755, 224 lines, 9 test functions.

Against `touch-mongo-live-subplans.md` §`sp-01 — repo-bootstrap`; amendment
`touch-mongo-live-plan.md` R-42 (`mongo-data/`, `mongo-dump/`, `*.bson`) +
GD-21…GD-30, SD-3/SD-5/SD-6; base `touch-full-recon-plan.md` GD-1, GD-2, GD-16,
R-01, R-02; RUNSTATE-18; and the attempt-1 critique
(`sp-repo-bootstrap-critique-attempt-1.md`).

Everything below was re-derived from the tree, git, and executed mutations — the
test gate's report was treated as a claim to disprove, not as evidence.

---

## Attempt-1 findings: independently re-verified as fixed

- **F1 (was major — clone-durability of the identity assertion): FIXED.**
  `test_commit_identity` (lines 142-161) now asserts the *commit* author of C1
  and C2 (clone-durable) and skips the `.git/config` half when no repo-local
  identity exists. Reproduced the original failure's environment exactly: fresh
  `git clone` into scratch, `git config --local --unset user.{name,email}`,
  `git config --global --get user.email` → rc 1 (no global identity anywhere).
  Result: **exit 0**, one `skip: no repo-local identity (fresh clone / CI
  checkout)`, zero FAILs. SD-2's "suite is never red between sub-plans" now
  holds off this working copy.
- **F3 (rc-128 vacuity): FIXED.** Line 112 asserts `returncode == 1 and
  stdout == ""`. Confirmed the fatal path really is excluded: `git check-ignore
  -- /etc/passwd` → rc 128 with empty stdout, and `git check-ignore --` → rc 128
  — both make the conjunction False, so a future typo cannot pass vacuously.
  The mirror tightening landed too (line 104: `rc == 0` **and** non-empty
  stdout).
- **F4 (magic `>= 4` floor): FIXED as recommended** — line 185 is now set
  containment (`tracked >= on_disk`). See Finding 1: the recommended shape has
  its own gap, which is on me as much as on the implementer.
- **F5 (subdir sweep): FIXED.** Line 197 sweeps all five
  (`plan`, `report`, `findings`, `orch-scripts`, `context`); the `is_dir()`
  guard keeps a partial task folder (`touch-monitor-spawn/` has only `plan/`) a
  no-op. 18 directories are actually visited in this tree.
- **F6 (`*.bson` escape hatch): FIXED.** `.gitignore:32-34` documents the
  unanchored rule and the explicit-negation escape hatch. Verified the comment
  is inert and the rule still bites: `git check-ignore -v --
  tests/fixtures/x.bson` → `.gitignore:35:*.bson`, rc 0.

## What is correct (verified, not taken from the gate)

- **SD-3 verbatim, byte-identical, strictly additive.** All eight entries exist
  as exact whole lines (`.gitignore:20,21,25,22,14,30,31,35`) and
  `test_bootstrap.py:22-31` restates them character-for-character, so sp-03's
  `test_shell.py` half has an exact text to copy. No pre-existing rule was
  altered or reordered.
- **Behaviour, not just text.** 12/12 `MUST_IGNORE` paths ignored, 8/8
  `MUST_NOT_IGNORE` not ignored, including R-01's negative half
  (`.claude/local-orchestrators/` itself, `events.jsonl`, `plan/*.md`,
  `report/*.html`, `orch-config.json`, `findings/*.md`).
  `git ls-files -i -c --exclude-standard` is empty — the rules shadow no tracked
  file. R-42's `mongo-data/x`, `mongo-dump/x`, `dump.bson` all ignored.
  `.touch/` also covers R-42's `.touch/mongo.json` secret, so GD-27's
  "no credential in the repo" has a rule behind it.
- **R-02/GD-2 substance.** `refs/heads` = `main` only; reflog shows the
  `master`→`main` rename strictly before C1; repo-local `user.name`/`user.email`
  are set and were genuinely needed (no global identity in this environment).
  C1 = `.gitignore`; C2 = `.claude/local-orchestrators/**` (the two new driver
  artifacts, three `.gitkeep`s, and index-only removal of four
  `.watcher-state.json`). `git ls-tree -r HEAD` and `git ls-files` contain
  **zero** `.watcher-state.json`. GD-2's full C1 path set is in history: README,
  CLAUDE.md, inception.md, .gitignore, `.claude/settings.json`,
  `.claude/statusline.sh`, all 6 `.claude/skills/**` files and all 9
  `.claude/shared/monitoring/**` files are tracked — the only untracked files
  under `monitoring/` are 8 `__pycache__/*.pyc`, every one of them correctly
  ignored.
- **The C1-content shortfall remains forced, not a finding.** The pre-existing
  initial commit `97ee7d7` (`msdrx`, 15:07:58 UTC, reflog `commit (initial)`)
  already carried GD-2's C1 paths, so the plan's zero-commits premise
  (PRODUCT-3/RUNSTATE-11) was false before this sub-plan started.
  Re-committing them was impossible without rewriting history.
- **RUNSTATE-18 discharged.** `find .claude/local-orchestrators -type d -empty`
  is empty; so is `find . -type d -empty` repo-wide; every one of the 18 per-task
  subdirs has at least one tracked file, so nothing vanishes in a clone
  (re-verified against a real clone).
- **SD-6 / ownership clean.** `git status --porcelain -uall` is exactly:
  ` M touch-mongo-live/events.jsonl` (live watcher append), ` M .gitignore`
  (the F6 comment), and `?? tests/test_bootstrap.py` + three `??` gate/critique
  findings files. No stash, no revert, no commit beyond C1+C2, nothing outside
  the ownership list.
- **Non-tautology proven by mutation** (in a throwaway clone, each mutation
  reverted): ignore `*/events.jsonl` → 1 FAIL, exit 1; rename `main`→`master` →
  3 FAILs, exit 1; `git rm --cached` a stream → 1 FAIL, exit 1; drop the
  `mongo-data/` line → 2 FAILs, exit 1. The guard is load-bearing.
- **Suite green, no services, no third-party imports.** `tests/test_bootstrap.py`
  exit 0 (9 functions, 65 `ok`, 0 FAIL — 65 rather than the gate's 57 because 18
  task subdirs are swept in the current tree, not 10); `test_server.py`,
  `test_watcher.py`, `test_shell.py`, `test_frontend.py` all exit 0. Imports are
  `os`, `subprocess`, `sys`, `pathlib` only.
- **Mongo GDs not exercised, correctly.** GD-21 (pymongo confined to
  `mongo_store.py`/`mirror.py`), GD-22, GD-24, GD-25, GD-26, GD-28, GD-29, GD-30
  have no surface here: the diff adds no module that imports pymongo, defines no
  collection or `_id`, builds no update operator, starts no service and holds no
  client. GD-27's only in-scope surface is the ignore rules, checked above.
  GD-15 holds — `.gitignore` and `tests/test_bootstrap.py` are sp-01's, and
  `test_shell.py`'s R-01 guard half is sp-03's by SD-3, so R-01 is not left
  half-done here.

---

## Findings

### Finding 1 — minor — `tests/test_bootstrap.py:179-186` (`test_event_streams_tracked`) asserts the containment in the less useful direction

**Defect.** `check(tracked >= on_disk, …)` catches *de-tracking* but not
*deletion*, and deletion is the rule CLAUDE.md calls out ("Never delete a
finished task folder or its `events.jsonl`"). Deleting a tracked stream from
disk shrinks `on_disk`, so the containment holds trivially and the suite stays
green.

**Verified, not theorised** (throwaway clone, each mutation reverted):

```
M3: rm .claude/local-orchestrators/touch-repo-recon/events.jsonl   -> exit=0 fails=0
M6: rm -rf .claude/local-orchestrators/touch-monitor-spawn         -> exit=0 fails=0
M4: git rm --cached .../touch-repo-recon/events.jsonl              -> exit=1 fails=1
```

M6 removes an entire task folder — the exact cardinal sin — and the guard that
exists to notice it says nothing.

Secondary, lower-probability risk in the same expression: because `on_disk` is
live disk state, the first *new* orchestrator task folder to appear turns the
suite red, and SD-6 forbids every remaining sub-plan from committing it, so no
downstream sub-plan could clear it by doing its own work correctly. I checked the
partition: no sub-plan sp-02…sp-15 creates a task folder (sp-15 only adds files
inside already-tracked dirs), so this is not a live break — but it is the same
shape as attempt-1's F1 and worth designing out.

**Concrete fix** — assert the direction that encodes the invariant, keeping the
existing one; this closes the deletion gap *and* makes the guard indifferent to
new, not-yet-committed folders:

```python
def test_event_streams_tracked():
    print("test_event_streams_tracked")
    on_disk = {p.parent.name for p in (REPO / ORCH).glob("*/events.jsonl")}
    prefix = f"{ORCH}/"
    in_head = {p[len(prefix):].split("/")[0]
               for p in git_out("ls-tree", "-r", "--name-only", "HEAD").splitlines()
               if p.startswith(prefix) and p.endswith("/events.jsonl")}
    check(bool(on_disk), f"per-task events.jsonl streams exist ({len(on_disk)} found)")
    # THE rule: a stream that is history must still be on disk (never deleted).
    check(on_disk >= in_head,
          f"no stream in history was deleted (missing on disk: {sorted(in_head - on_disk)})")
    # And a stream on disk that is already in the index must not be de-tracked.
    tracked = {p[len(prefix):].split("/")[0]
               for p in git_out("ls-files", "--", ORCH).splitlines()
               if p.startswith(prefix) and p.endswith("/events.jsonl")}
    check(not (in_head - tracked),
          f"no historic stream was de-tracked (missing: {sorted(in_head - tracked)})")
```

### Finding 2 — minor — `tests/test_bootstrap.py:75-80` (`git()`) neutralizes `GIT_*` env but not global/system `core.excludesFile`

**Defect.** The helper strips `GIT_*` from the environment for determinism, which
is right, but `core.excludesFile` from `~/.gitconfig` or `/etc/gitconfig` still
feeds `git check-ignore`. Consequences in both directions:

- *False green:* a developer whose global excludes happen to contain `.touch/`
  or `*.pid` keeps `test_check_ignore_positive` green even if the repo rule is
  deleted. (`test_gitignore_entries` still catches the deletion, so the pair is
  not fully vacuous — this is why it is minor, not major.)
- *False red:* a global excludes entry matching any `MUST_NOT_IGNORE` path
  (e.g. `*.json`, which would hit `orch-config.json`) turns the suite red on a
  clean checkout, exactly the class of environment-dependent red F1 was rejected
  for.

**Verified mechanism:** `git -c core.excludesFile=/tmp/ge check-ignore -v --
.claude/local-orchestrators/x/events.jsonl` → `/tmp/ge:1:…` rc 0, i.e. an
out-of-repo excludes file alone flips a `MUST_NOT_IGNORE` path to ignored.

**Concrete fix** — pin the ignore sources to the repo (line 78):

```python
    env["GIT_CONFIG_NOSYSTEM"] = "1"           # after the GIT_* filter, on purpose
    return subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", *args],
        cwd=str(REPO), env=env, capture_output=True, text=True,
    )
```

(`-c` before the subcommand works with the existing `["git", *args]` splat.
Keep `.git/info/exclude` out of scope — it is repo-local state, same class as
`.git/config`.)

### Finding 3 — nit — nothing guards SD-6's commit boundary

`test_head_exists:117-125` asserts C1 and C2 are *present*; nothing notices a
third commit. SD-6 makes "sp-01 is the only sub-plan that commits" an invariant
for fourteen remaining sub-plans, and this file is explicitly framed (docstring,
lines 6-9) as the durable guard that the bootstrap stays bootstrapped.

**Fix (optional, and deliberately not a count).** Assert the two bootstrap
commits are the tip, which self-documents and goes green again the moment the
user legitimately commits on top after this pass:

```python
    check(git_out("log", "-2", "--format=%s").splitlines()
          == ["orchestration history", "tooling and docs"],
          "C2,C1 are the two most recent commits (SD-6 commit boundary)")
```

A hard `rev-list --count == 3` would be wrong here — `97ee7d7` is a
pre-existing commit and post-pass commits are legal.

### Finding 4 — nit — `tests/test_bootstrap.py:121` hard-codes a 40-char object id

`len(proc.stdout.strip()) == 40` fails on a SHA-256 repository (64 chars).
**Fix:** `check(len(sha) in (40, 64), …)` or `re.fullmatch(r"[0-9a-f]{40,64}", sha)`.

### Finding 5 — nit — `tests/test_bootstrap.py:192` name no longer matches the assertion

`test_empty_dirs_have_gitkeep` asserts "this directory has ≥1 tracked file",
which is the right property (three `.gitkeep`s exist; the other 15 dirs are
populated with real files) but not what the name says. A future reader will look
for `.gitkeep` and not find the check. **Fix:** rename to
`test_task_dirs_survive_clone` and update the tuple in `main()` (line 212).

### Finding 6 — nit — `tests/test_bootstrap.py:182` / `:167` derive a task name at any depth

`p.split("/")[-2]` maps `…/local-orchestrators/x/sub/events.jsonl` to `"sub"`,
so a nested tracked path could satisfy the containment for a task whose own
stream was de-tracked. Improbable in this layout, but free to fix — anchor on the
prefix and take `[0]` after it, as in Finding 1's snippet.

### Finding 7 — nit — carried forward from attempt 1, correctly out of scope here

Recorded so they are not lost, not held against this attempt:

- **F2** — commit `579446e`'s message misstates the SD-5 gate ("none of their
  `ORCH_STATE_DIR`s lies inside a committed path"); C2 did stage paths inside the
  live `touch-mongo-live` `ORCH_STATE_DIR`. Outcome was still safe
  (`events.jsonl` excluded, `.watcher-state.json` staged as an index-only
  deletion). History cannot be amended → sp-15 records the operative per-file
  rule in CLAUDE.md, per SD-7.
- **F7** — two identities for one human (`msdrx` vs `Michael Sadradze`); a
  `.mailmap` is now owed. sp-15 or the user.
- **F8** — `.temp-develop/synthesis_failed.png` (594 KB) is tracked by `97ee7d7`
  and `.temp-develop/` is still absent from `.gitignore`, so the next screenshot
  is committable. Outside SD-3's verbatim list; flag to the next `.gitignore`
  owner.
- The F6 comment on `.gitignore` and `tests/test_bootstrap.py` itself stay
  **uncommitted** — correct under SD-6, which caps this sub-plan at C1+C2. A
  clone therefore gets all eight rules but not the comment, and does not get the
  test file at all; both land whenever the pass is committed.

---

## Conclusion

Every blocking attempt-1 finding is fixed and independently re-verified,
including the one that mattered (F1's clone-durable identity assertion, proven by
an actual identity-less clone). The plan's stated tests for R-01, R-02 and
R-42:gitignore are all present and behavioural, the guard is mutation-proven
non-tautological, ownership and the SD-6 commit boundary are respected, and the
Mongo global decisions have no surface in this diff. The two minors are test
robustness improvements — the assertion direction in Finding 1 is the one worth
taking, and the whole file's remaining risk is confined to test-only code.

**approved = true** (0 blocker, 0 major).
