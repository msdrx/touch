#!/usr/bin/env python3
"""Stdlib-only tests for sp-repo-bootstrap (R-01 gitignore hardening,
R-42's Mongo additions, R-02 git bootstrap). Run as `python3 test_bootstrap.py`;
exits non-zero on the first failure. No pytest, no runner.

Amended 2026-07-27: .claude/local-orchestrators/ is now ignored and untracked
(run state stays on disk only), inverting R-01's original negative half.

These are *repository state* assertions, not unit tests: they read `.gitignore`
and interrogate git itself, so they are the durable guard that the bootstrap
stays bootstrapped (a later careless ignore rule, a branch rename back to
master, or a re-tracked watcher checkpoint all fail here).

Everything that interrogates git is gated on this tree actually BEING the git
checkout (`git rev-parse --show-toplevel` == REPO): unpacked-archive and
packaged copies carry the files but no `.git`, where `check-ignore` answers
rc=128 and `log` answers nothing, and eight assertions used to fail for the
one reason that is not a defect (RENAME-SCOPE-15). The `.gitignore` guard
itself needs no git and always runs. Comparing the toplevel — rather than just
asking whether git finds *a* repo — also stops an archive unpacked INSIDE some
other checkout from silently asserting against that repo's history.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GITIGNORE = REPO / ".gitignore"
ORCH = ".claude/local-orchestrators"

# SD-3 — the ONE verbatim entry list. sp-03's test_shell.py guard asserts the
# identical text; both sides must stay character-for-character the same.
GITIGNORE_ENTRIES = (
    ".touch/",
    ".touch*/",
    ".claude/settings.local.json",
    "*.pid",
    ".claude/local-orchestrators/*/.watcher-state.json",
    "mongo-data/",
    "mongo-dump/",
    "*.bson",
)

# Paths that MUST be ignored. Hypothetical (untracked, non-existent) on purpose:
# `git check-ignore` consults the index, so a tracked path would answer "not
# ignored" regardless of the rules.
MUST_IGNORE = (
    ".touch/x",
    ".touch/sessions/1-2/pty.log",
    ".touch-dev/x",                 # .touch*/ covers TOUCH_STATE_DIR variants
    ".claude/settings.local.json",
    "server.pid",
    f"{ORCH}/x/.watcher-state.json",
    "mongo-data/x",
    "mongo-dump/x",
    "dump.bson",
    # pre-existing rules that must survive an additive edit
    f"{ORCH}/x/daemon.log",
    # 2026-07-27 amendment: the whole run-state tree is ignored now.
    f"{ORCH}/x/events.jsonl",
    f"{ORCH}/x/plan/p-plan.md",
    f"{ORCH}/x/orch-config.json",
)

# What must never become ignored by a careless rule.
MUST_NOT_IGNORE = (
    ".gitignore",
)

# Entries that must NOT be in .gitignore (item 04, 2026-07-28). These two lines
# used to sanction the monitoring module's state leak: status.sh spooled
# events.jsonl into the shared module dir when ORCH_STATE_DIR was unset, and
# both daemons fell back to it, so the repo ignored the droppings. The
# behaviour is gone — status.sh exits 2, the daemons exit 1 — and the ignore
# rules go with it, because an ignore rule is exactly what would let the
# behaviour return unnoticed.
GITIGNORE_FORBIDDEN = (
    ".claude/shared/monitoring/events.jsonl",
    ".claude/shared/monitoring/.watcher-state.json",
)

failures = []
skips = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  SKIP: {msg}")
    skips.append(msg)


def git(*args):
    """Run git in REPO with a neutral environment; return CompletedProcess."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", *args], cwd=str(REPO), env=env, capture_output=True, text=True,
    )


def git_out(*args):
    return git(*args).stdout.strip()


def is_git_checkout():
    """True when REPO is the working tree of a git repo (not just inside one)."""
    proc = git("rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        return False
    try:
        return Path(proc.stdout.strip()).resolve() == REPO
    except OSError:
        return False


GIT = is_git_checkout()


# --- R-01/R-42: the entry list is present verbatim, one entry per line
def test_gitignore_entries():
    print("test_gitignore_entries")
    lines = [ln.strip() for ln in GITIGNORE.read_text().splitlines()]
    for entry in GITIGNORE_ENTRIES:
        check(entry in lines, f".gitignore has a line exactly `{entry}`")


# --- item 04: the two sanctioned-leak lines are gone and cannot come back.
# Asserted on the whole text (not just exact lines) so a re-added rule cannot
# sneak back in as a prefix/suffix variant of the same path, and paired with a
# behavioural half below: git must NOT ignore those paths any more.
def test_gitignore_has_no_module_dir_state_lines():
    print("test_gitignore_has_no_module_dir_state_lines")
    text = GITIGNORE.read_text()
    for entry in GITIGNORE_FORBIDDEN:
        # The explanatory comment names both paths on purpose; only a real rule
        # (a line that is not a comment) counts as a re-added ignore.
        rules = [ln.strip() for ln in text.splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
        check(not any(entry in ln for ln in rules),
              f".gitignore no longer sanctions `{entry}`")
    if not GIT:
        skip("not a git checkout; skipping the check-ignore half")
        return
    for entry in GITIGNORE_FORBIDDEN:
        proc = git("check-ignore", "--", entry)
        check(proc.returncode == 1 and proc.stdout == "",
              f"git no longer ignores {entry} (rc={proc.returncode})")


# --- R-01/R-42 behaviour: what git actually ignores
# `check-ignore` exits 0 = ignored, 1 = not ignored, 128 = fatal (bad pathspec,
# unreadable .gitignore). Both directions assert the EXACT code and that git
# actually answered (stdout echoes the path when, and only when, it is ignored),
# so a future typo that makes git error out cannot pass either half vacuously.
def test_check_ignore_positive():
    print("test_check_ignore_positive")
    for path in MUST_IGNORE:
        proc = git("check-ignore", "--", path)
        check(proc.returncode == 0 and proc.stdout.strip() != "",
              f"ignored: {path} (rc={proc.returncode})")


def test_check_ignore_negative():
    print("test_check_ignore_negative")
    for path in MUST_NOT_IGNORE:
        proc = git("check-ignore", "--", path)
        check(proc.returncode == 1 and proc.stdout == "",
              f"NOT ignored (history): {path} (rc={proc.returncode})")


# --- R-02: a HEAD exists at all (nothing may assume it before C1)
def test_head_exists():
    print("test_head_exists")
    proc = git("rev-parse", "HEAD")
    check(proc.returncode == 0, "git rev-parse HEAD succeeds")
    check(len(proc.stdout.strip()) == 40, "HEAD resolves to a full sha")
    log = git_out("log", "--format=%s")
    check("tooling and docs" in log.splitlines(), "C1 'tooling and docs' is in history")
    check("orchestration history" in log.splitlines(),
          "C2 'orchestration history' is in history")


# --- GD-2: branch is main, not master
def test_branch_is_main():
    print("test_branch_is_main")
    check(git_out("rev-parse", "--abbrev-ref", "HEAD") == "main", "checked-out branch is main")
    heads = git_out("for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    check("main" in heads, "refs/heads/main exists")
    check("master" not in heads, "no refs/heads/master remains")


# --- GD-2: identity. The durable half is that the bootstrap COMMITS carry a
# real author — that travels in every clone. The repo-local override in
# .git/config never travels (it is not part of a clone or an archive), so it is
# asserted only where it exists; otherwise this test would be red on every
# machine but the one that ran the bootstrap.
def test_commit_identity():
    print("test_commit_identity")
    authored = {}
    for line in git_out("log", "--format=%s\x1f%an\x1f%ae").splitlines():
        subject, _, rest = line.partition("\x1f")
        name, _, email = rest.partition("\x1f")
        authored.setdefault(subject, (name, email))
    for subject in ("tooling and docs", "orchestration history"):
        name, email = authored.get(subject, ("", ""))
        check(name.strip() != "", f"C '{subject}' has an author name")
        check("@" in email, f"C '{subject}' has an author email")

    if git("config", "--local", "--get", "user.email").returncode != 0:
        # Routed through skip() like every other skip in this file, so the
        # file's own trailer and run_all.sh's count agree: a bare print is
        # counted by the runner but invisible to `skips`.
        skip("test_commit_identity: no repo-local identity "
             "(fresh clone / CI checkout)")
        return
    for key in ("user.name", "user.email"):
        proc = git("config", "--local", "--get", key)
        check(proc.stdout.strip() != "", f"repo-local {key} is set")
    check("@" in git_out("config", "--local", "--get", "user.email"),
          "repo-local user.email looks like an address")


# --- GD-2: C2 contains no .watcher-state.json, and none is tracked since
def test_no_tracked_watcher_state():
    print("test_no_tracked_watcher_state")
    tracked = [p for p in git_out("ls-files").splitlines()
               if p.endswith(".watcher-state.json")]
    check(not tracked, f"no .watcher-state.json is tracked (found: {tracked})")
    in_head = [p for p in git_out("ls-tree", "-r", "--name-only", "HEAD").splitlines()
               if p.endswith(".watcher-state.json")]
    check(not in_head, f"HEAD tracks no .watcher-state.json (found: {in_head})")


# --- 2026-07-27 amendment: run state lives on disk only. Nothing under
# .claude/local-orchestrators/ may be in the index — a re-tracked file there
# (e.g. via `git add -f`) fails here.
def test_run_state_not_tracked():
    print("test_run_state_not_tracked")
    tracked = git_out("ls-files", "--", ORCH).splitlines()
    check(not tracked,
          f"nothing under {ORCH} is tracked (found: {len(tracked)} file(s))")


def main():
    test_gitignore_entries()
    # Its git half skips itself, so it runs in both worlds.
    test_gitignore_has_no_module_dir_state_lines()
    needs_git = (test_check_ignore_positive, test_check_ignore_negative,
                 test_head_exists, test_branch_is_main, test_commit_identity,
                 test_no_tracked_watcher_state, test_run_state_not_tracked)
    if not GIT:
        for t in needs_git:
            skip(f"{t.__name__}: {REPO} is not a git checkout "
                 f"(unpacked archive / packaged copy)")
    else:
        for t in needs_git:
            t()
    print()
    for message in skips:
        print(f"skipped: {message}")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all sp-repo-bootstrap tests passed ({len(skips)} skipped)")


if __name__ == "__main__":
    main()
