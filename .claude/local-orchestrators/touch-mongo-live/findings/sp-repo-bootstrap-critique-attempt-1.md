# sp-repo-bootstrap — adversarial critique, attempt 1

**Verdict: REJECTED** — 1 major, 3 minor, 4 nits.

Reviewed diff/content:

- `/home/laniakea/Projects/touch/.gitignore` (additive edit, in commit `7444331`)
- `/home/laniakea/Projects/touch/.git/config` (repo-local `[user]`)
- `/home/laniakea/Projects/touch/tests/test_bootstrap.py` (new, untracked, 0755)
- `.claude/local-orchestrators/touch-aggregator/report/.gitkeep`
- `.claude/local-orchestrators/touch-repo-recon/plan/.gitkeep`
- `.claude/local-orchestrators/touch-repo-recon/report/.gitkeep`

Against: `touch-mongo-live-subplans.md` §`sp-01 — repo-bootstrap`; amendment
`touch-mongo-live-plan.md` GD-27/R-42 (`mongo-data/`, `mongo-dump/`, `*.bson`),
SD-3/SD-5/SD-6; base `touch-full-recon-plan.md` GD-1, GD-2, GD-16, R-01, R-02;
`research-runstate-attempt-1.md` RUNSTATE-18.

---

## What is genuinely correct (verified independently, not taken from the gate)

- **SD-3 verbatim, strictly additive.** All eight entries present as exact
  lines (`.gitignore:15,25,26,27,32,45,46,47`). The three pre-existing rules
  (`monitoring/events.jsonl`, `monitoring/.watcher-state.json`,
  `local-orchestrators/*/*.log`) survive unchanged; `git diff 97ee7d7 -- .gitignore`
  is +23/-0. `test_bootstrap.py:22-31` restates the list character-for-character,
  so sp-03's `test_shell.py` half has an exact text to copy.
- **Behaviour, not text.** `git check-ignore` confirms all 12 MUST_IGNORE paths
  ignored and all 8 MUST_NOT_IGNORE paths not ignored, including the R-01
  negative half (`.claude/local-orchestrators/` itself and `events.jsonl` under
  it). `git ls-files -i -c --exclude-standard` is empty — the new rules shadow
  no tracked file.
- **R-02 ordering is real.** `.git/logs/HEAD` shows `master`→`main` rename at
  1784992996, C1 at 1784993004, C2 at 1784993049 — rename strictly before
  commit #1 per GD-2. `refs/heads` = `main` only.
- **Repo-local identity was genuinely required**, not cargo-culted:
  `git config --global --get user.name` exits 1 in this environment, and
  `git config --show-origin --get user.email` resolves to `file:.git/config`.
  `.git/config` contains exactly one added section (`[user]`) — no other key
  touched.
- **C2 contains no `.watcher-state.json`**: the four were removed index-only;
  all four working files are still on disk with pre-commit mtimes, so the three
  live watchers were not disturbed. `git ls-tree -r HEAD` and `git ls-files`
  both contain zero `.watcher-state.json`.
- **SD-5's substance held**: the one file under active append
  (`touch-mongo-live/events.jsonl`) is deliberately unstaged and still shows as
  the sole ` M` in `git status`.
- **RUNSTATE-18 is fully discharged for the current tree**: `find
  .claude/local-orchestrators -type d -empty` is empty, and every per-task
  `plan/`, `report/`, `findings/`, `orch-scripts/`, `context/` dir has at least
  one tracked file, so nothing vanishes in a clone (verified by an actual
  `git clone` into scratch).
- **SD-6 respected**: exactly two commits added, no stash, no revert, nothing
  outside the ownership list changed (`tests/test_bootstrap.py` is the only new
  untracked file).
- **The C1-content deviation is forced and correctly documented.** GD-2's C1
  file set already entered history in the user's pre-existing initial commit
  `97ee7d7` (author `msdrx`, 15:07:58 UTC — 15 min before this sub-plan's first
  action; reflog-confirmed as `commit (initial)`), so the plan's "zero commits"
  premise (PRODUCT-3/RUNSTATE-11) was already false. Re-committing those paths
  was impossible without rewriting history, which is forbidden. Keeping the
  GD-2 commit *messages* and explaining the shortfall in the message body is the
  right call — **not** a finding.
- Suite state confirmed independently: `test_bootstrap.py` exit 0;
  `test_server.py`, `test_watcher.py`, `test_shell.py`, `test_frontend.py` all
  exit 0. GD-21 is untouched by this sub-plan (no imports beyond `os`,
  `subprocess`, `sys`, `pathlib`); GD-22/24/25/26/28/29/30 are not in scope —
  nothing in the diff writes, queries, or maps a Mongo document.

---

## Findings

### F1 — major — `tests/test_bootstrap.py:132-139` (`test_repo_local_identity`)

**Defect.** The test asserts on `git config --local --get user.{name,email}`,
i.e. on `.git/config` — state that by construction never travels in a clone or
a fresh checkout. The whole file therefore exits **1** everywhere except this
one working copy.

**Verified, not theorised.** `git clone /home/laniakea/Projects/touch <scratch>`
then running the same file gives:

```
test_repo_local_identity
  FAIL: repo-local user.name is set
  FAIL: repo-local user.email is set
  FAIL: repo-local user.email looks like an address
FAILED (3):
EXIT=1
```

44 checks pass, 3 fail, deterministically, on every machine — this environment
has no global identity either, so no developer configuration rescues it.

**Why it matters here.** sp-04 creates `tests/run_all.sh` and every later
sub-plan's gate runs the full suite; SD-2's contract is that "the suite is never
red between sub-plans". This file bakes in a red that no downstream sub-plan can
fix by doing its own work correctly — the only fix is editing a file sp-01 owns
and later sub-plans do not. It is also a false proxy for the requirement: GD-2
wants the *commits* to carry a real identity, which is clone-durable, rather
than a config key, which is not. And the assertion is over-tight in the other
direction too — a clone whose owner sets a perfectly valid `--global` identity
still fails.

**Concrete fix** (replaces lines 132-139; keeps the local-config guard where it
is meaningful and adds the durable assertion):

```python
# --- GD-2: identity — commits carry a real author (clone-durable), and the
# repo-local override is present when this IS the bootstrap working copy.
def test_commit_identity():
    print("test_commit_identity")
    for sha_subject in ("tooling and docs", "orchestration history"):
        who = git_out("log", "-1", "--format=%an|%ae", f"--grep=^{sha_subject}$")
        name, _, email = who.partition("|")
        check(name.strip() != "", f"C '{sha_subject}' has an author name")
        check("@" in email, f"C '{sha_subject}' has an email address")
    # .git/config never travels in a clone: assert it only where it exists.
    if git("config", "--local", "--get", "user.email").returncode != 0:
        print("  skip: no repo-local identity (fresh clone / CI checkout)")
        return
    for key in ("user.name", "user.email"):
        proc = git("config", "--local", "--get", key)
        check(proc.stdout.strip() != "", f"repo-local {key} is set")
```

and update the tuple in `main()` (line 181) accordingly. Same for
`test_branch_is_main`'s `"master" not in heads` check — that one *is*
clone-durable (a clone of this repo has only `main`), so it can stay as-is.

### F2 — minor — commit `579446e` message, paragraph 3 (SD-5 justification)

**Defect.** The message states: *"three watchers are live (touch-aggregator,
touch-full-recon, touch-mongo-live) and none of their ORCH_STATE_DIRs lies
inside a committed path, so the scoped gate passes."* That premise is false.
C2's path scope is `.claude/local-orchestrators/**`, and the files it actually
staged include `touch-mongo-live/orch-config.json`,
`touch-mongo-live/plan/touch-mongo-live-subplans.md` and the index-removal of
`touch-mongo-live/.watcher-state.json` — all *inside* the live
`touch-mongo-live` watcher's `ORCH_STATE_DIR` (`ps -eo cmd | grep
"[d]ecision_watcher"` still shows it running against `wf_b297177a-d11`). SD-5's
scoping does not exempt this commit; the gate had to be argued, not declared
inapplicable.

**Impact is documentary, not substantive.** The thing SD-5 guards against — a
torn read of a file under append — did not happen: `events.jsonl` was excluded,
`.watcher-state.json` was staged as a deletion (no read), and `orch-config.json`
/ `subplans.md` are write-once driver output. So the outcome is correct and no
history is corrupt. But the reasoning now sits in immutable history and will be
cited by the next person who needs to commit during a live run, who will
conclude the gate never applies to `.claude/local-orchestrators/**`.

**Fix.** Cannot amend (no rewriting history). Record the correction where the
next reader will see it: sp-15 owns the docs, so add one line to CLAUDE.md's
git-policy section (or to the plan's SD-5 note) stating the operative rule —
*"the SD-5 gate is satisfied per-file: a path inside a live watcher's
`ORCH_STATE_DIR` may be committed only if it is not under active append;
`events.jsonl` never qualifies, index-only removals always do"* — and note that
`579446e`'s message states this incorrectly.

### F3 — minor — `tests/test_bootstrap.py:103-107` (`test_check_ignore_negative`)

**Defect.** `check-ignore` is asserted with `returncode != 0`. `git check-ignore`
exits **1** for "not ignored" but **128** for a fatal error (bad path spec,
outside-repo path, broken `.gitignore` syntax). So the entire negative half of
R-01 — the assertion that protects the irreplaceable `events.jsonl` streams
from ever being ignored — passes vacuously if git errors out instead of
answering. A typo'd future entry in `MUST_NOT_IGNORE` is green forever.

**Fix.** Assert the exact code and prove git answered:

```python
def test_check_ignore_negative():
    print("test_check_ignore_negative")
    for path in MUST_NOT_IGNORE:
        proc = git("check-ignore", "--", path)
        check(proc.returncode == 1 and proc.stdout == "",
              f"NOT ignored (history): {path} (rc={proc.returncode})")
```

(drop `-q` so a non-empty stdout also disproves the claim). Consider the mirror
tightening in `test_check_ignore_positive` (line 99): require `returncode == 0`
**and** non-empty stdout naming the matching rule.

### F4 — minor — `tests/test_bootstrap.py:154-158` (`test_event_streams_tracked`)

**Defect.** `check(len(streams) >= 4, …)` is a magic floor, and there are
exactly 4 streams today. Losing a stream drops it to 3 and fails — but the
guard silently stops guarding the moment a fifth task folder appears (which the
current run will produce), because 5 streams minus one still satisfies `>= 4`.
For the "never delete a stream" invariant — the single most consequential rule
in CLAUDE.md — a floor is the wrong shape.

**Fix.** Assert set equality against what is on disk, so the test tracks reality
and still catches a de-tracked stream:

```python
def test_event_streams_tracked():
    print("test_event_streams_tracked")
    root = REPO / ORCH
    on_disk = {p.parent.name for p in root.glob("*/events.jsonl")}
    tracked = {p.split("/")[-2] for p in git_out("ls-files", "--", ORCH).splitlines()
               if p.endswith("/events.jsonl")}
    check(on_disk and tracked >= on_disk,
          f"every on-disk stream is tracked (missing: {sorted(on_disk - tracked)})")
```

### F5 — nit — `tests/test_bootstrap.py:162-175` (`test_empty_dirs_have_gitkeep`)

Only `plan` and `report` are swept. RUNSTATE-18's recommendation names
`plan/`/`report/`/**`findings/`**, and `orch-scripts/` and `context/` are
equally clone-fragile (the workflow scripts write into all of them). All are
populated today, so this is not a live gap — but the guard will not notice the
day a new task folder has an empty `findings/`.

**Fix.** Widen the inner loop to
`for sub in ("plan", "report", "findings", "orch-scripts", "context"):` — the
`if not d.is_dir(): continue` guard already makes absent dirs a no-op, so no
task folder is forced to have all five.

### F6 — nit — `.gitignore:47` (`*.bson`)

Plan-verbatim (R-42), so not a deviation — but the pattern is unanchored, and
sp-02/later Mongo work may want a `mongodump`-shaped byte fixture under
`tests/fixtures/`. It would be silently untrackable, and the failure mode
("the fixture manifest says a file exists that a clone doesn't have") is
confusing to debug.

**Fix.** No behaviour change now; add one comment line above it — e.g.
`# unanchored on purpose (dumps land anywhere); if a .bson ever needs to be a`
`# fixture, negate it explicitly: !tests/fixtures/**/*.bson` — so the next
owner sees the escape hatch instead of rediscovering the rule.

### F7 — nit — history now carries two identities for one human

`97ee7d7` is `msdrx <sadradzemikheil@gmail.com>`; C1/C2 are
`Michael Sadradze <michaelsadradze@gmail.com>`. Both are the repo owner. Not
wrong, but `git shortlog` will show two contributors forever, and a future
`.mailmap` question is now owed.

**Fix.** Optional: sp-15 (docs owner) can add a two-line `.mailmap` mapping
`msdrx <sadradzemikheil@gmail.com>` onto the canonical identity, or the user can
confirm which address is canonical for the repo-local override.

### F8 — nit — `.temp-develop/synthesis_failed.png` (594 KB binary, tracked by `97ee7d7`)

Outside SD-3's verbatim list, so correctly not touched by this sub-plan — but a
`.gitignore` *hardening* pass that ran against this tree is the natural place
for someone to notice that a scratch directory with a half-megabyte PNG is in
history and still untracked-by-rule (`.temp-develop/` is absent from
`.gitignore`, so the next screenshot lands in a commit too). GD-16 sets the
review threshold at ~20 MB; this is 3 % of it in one file.

**Fix.** Not for this attempt. Flag to whoever next owns `.gitignore` (sp-15 for
the doc half): add `.temp-develop/` and note in CLAUDE.md that `97ee7d7`'s PNG
stays in history because history is never rewritten.

---

## Attack checklist — items that do not apply, and why

GD-21 (lazy pymongo), GD-22 (Mongo off the liveness path), GD-24 (`ref_key`
`_id` grammar), GD-25 (upsert algebra), GD-26 (no delete verbs / no TTL),
GD-27 (mongod recipe, 0600 secrets, zero-users refusal), GD-28 (provenance
pins), GD-29 (no agent holds a Mongo client), GD-30 (latency budget): **not
exercised** — the diff adds no Python module that imports anything beyond four
stdlib names, defines no collection, writes no document, and starts no service.
The only Mongo-adjacent surface is the three `.gitignore` entries, checked in F6
and in the "correct" list above. GD-15 (one file one owner) holds: every path in
the diff is on sp-01's ownership list, and `touch-mongo-live/orch-config.json`
in C2 was modified by the *driver*, not by this sub-plan — committing it is
GD-2's C2 scope, not an ownership breach.

**Blocking set for attempt 2: F1.** F2-F4 are cheap and should ride along
(F2's fix belongs to sp-15, so record it and move on). F5-F8 are optional.
