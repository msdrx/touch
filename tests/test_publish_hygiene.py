#!/usr/bin/env python3
"""Stdlib-only publish-hygiene guard (plan items 01 and 02, GD-P1 blockers 1-2).
Run as `python3 test_publish_hygiene.py`; exits non-zero on failure. No pytest,
no runner — `run_all.sh` picks it up by its `test_*.py` glob.

This is a *repository state* test, not a unit test: it asks git what is tracked
and reads those bytes. Three guards, one per way a secret has actually reached
this index:

  (a) the set of tracked files at the repo ROOT is an explicit allowlist —
      `mytok2` (a live `secrets.token_urlsafe(32)` scratch copy) was committed
      in f3b10a7 precisely because a stray root file draws no attention;
  (b) no tracked file outside `tests/fixtures/` carries a line that is nothing
      but a 40-50 char URL-safe blob — the shape of a Touch API token;
  (c) no tracked file carries a `mongodb://user:password@host` URI with a real
      password (item 02 / SECURITY-PUBLISHING-5: 17 such URIs live in this
      repo's history).

Scope, deliberately: the INDEX and the HEAD tree, i.e. what a future clone or
`git archive` would carry. Older commits are out of reach of a test — history
is purged manually (see
`.claude/local-orchestrators/reflection-plugin/findings/sp-secrets-hygiene-manual-steps.md`)
and neutralized structurally by the fresh-history release repo (GD-P1(4)/GD-P6).

`tests/fixtures/**` is excluded from the content scans: it is frozen verbatim
corpora (GD-P4) that this suite may never edit, and it never ships (GD-P1(3)).

Without git (a `git archive` checkout, a released tarball) every check is
skipped with a printed reason and the file still exits 0 — item 03's
clean-checkout discipline.
"""
import os
import re
import string
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# --- (a) item 01: what may sit at the repo root, tracked.
# An allowlist fails on ADDITION: a new root file must be added here on purpose,
# by someone who looked at it. `LICENSE` is pre-authorized by GD-P6 (MIT, repo
# root + plugin root); nothing else is.
ROOT_ALLOWLIST = frozenset({
    ".gitignore",
    "CLAUDE.md",
    "README.md",
    "inception.md",
    "LICENSE",
})

# Names that are token scratch files by convention — the `.gitignore` courtesy
# rules (`mytok*`, `*.token`, carried by sp-rename-compat-wire) protect only
# UNtracked paths, so this is the half that catches `git add -f` and a path that
# was tracked before the rule existed.
TOKEN_SCRATCH = re.compile(r"(^|/)(mytok[^/]*|[^/]*\.token)$")

# --- (b) item 01: a line that is exactly a URL-safe blob of token length.
# `secrets.token_urlsafe(32)` is 43 chars (aggregator/server.py:244,591).
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{40,50}$")
# ...and the regex alone is not enough: `-` is in its class, so every 40-50 char
# `# ------` comment rule in the tree matches it. Entropy is the discriminator —
# a random 43-char blob has ~30 distinct characters, a comment rule has 1.
DISTINCT_MIN = 12

# --- (c) item 02: a credentialed Mongo URI.
MONGO_URI = re.compile(r"mongodb(?:\+srv)?://([^/\s:]+):([^@\s]+)@")
# Docs must still be able to SHOW the URI shape (docs/mongo.md:194 writes the
# password as `<password>`), so a password that is visibly a stand-in is not a
# leak. Everything else is.
PLACEHOLDER_WORDS = frozenset({
    "password", "pass", "passwd", "pwd", "secret", "redacted", "changeme",
    "yourpassword", "p", "…",
})

# Excluded from the content scans only (never from the tracked/root checks).
CONTENT_SCAN_EXCLUDED = ("tests/fixtures/",)

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def git(*args):
    """Run git in REPO with a neutral environment; return CompletedProcess."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", *args], cwd=str(REPO), env=env, capture_output=True, text=True,
    )


def have_git():
    try:
        return git("rev-parse", "--git-dir").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def tracked_paths():
    """Repo-relative paths in the index (NUL-separated: paths may be odd)."""
    out = git("ls-files", "-z").stdout
    return [p for p in out.split("\0") if p]


def head_paths():
    out = git("ls-tree", "-r", "-z", "--name-only", "HEAD").stdout
    return [p for p in out.split("\0") if p]


def is_placeholder(password):
    """True when a URI's password field is visibly a documentation stand-in."""
    p = password.strip()
    if p.startswith("<") and p.endswith(">"):        # <password>
        return True
    if p.startswith("$") or p.startswith("${"):      # $TOUCH_MONGO_PASS
        return True
    if p.startswith("{") or p.startswith("%"):       # {pass}, %s
        return True
    if set(p) <= set("*.…•x"):                       # ***, …
        return True
    return p.lower() in PLACEHOLDER_WORDS


def token_shaped(line):
    """True when a line is a high-entropy URL-safe blob of token length."""
    s = line.strip()
    return bool(TOKEN_SHAPE.match(s)) and len(set(s)) >= DISTINCT_MIN


def scan_targets(paths):
    return [p for p in paths if not p.startswith(CONTENT_SCAN_EXCLUDED)]


def read_text(path):
    """Decoded text, or None for binary/unreadable (documented blind spot)."""
    try:
        return (REPO / path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


# --- the detectors themselves, before they are trusted over 76 files.
# Both synthetic samples are ASSEMBLED at runtime: a literal token or a literal
# credentialed URI in this file would be found by the very scan below.
def test_detectors():
    print("test_detectors")
    synth_token = (string.ascii_letters + string.digits)[:43]
    check(token_shaped(synth_token), "token detector flags a 43-char blob")
    check(not token_shaped("#" + " " + "-" * 44),
          "token detector ignores a comment rule (low entropy)")
    check(not token_shaped("-" * 43), "token detector ignores 43 dashes")
    check(not token_shaped(synth_token + "x" * 20),
          "token detector ignores an over-long blob")

    uri = "mongodb://" + "touch" + ":" + "hunter2x" + "@" + "127.0.0.1:27017/db"
    m = MONGO_URI.search(uri)
    check(m is not None and not is_placeholder(m.group(2)),
          "mongo detector flags a real credentialed URI")
    doc = "mongodb://" + "touch" + ":" + "<" + "password" + ">" + "@" + "h/db"
    m = MONGO_URI.search(doc)
    check(m is not None and is_placeholder(m.group(2)),
          "mongo detector accepts the documented <password> form")


# --- (a) GD-P1(1): the repo root is an allowlist
def test_root_allowlist():
    print("test_root_allowlist")
    root_files = sorted(p for p in tracked_paths() if "/" not in p)
    unexpected = [p for p in root_files if p not in ROOT_ALLOWLIST]
    check(not unexpected,
          f"tracked root files are all allowlisted (unexpected: {unexpected})")
    print(f"  note: {len(root_files)} tracked root file(s): {root_files}")


# --- (a) item 01: the token scratch file itself, by name, in index AND HEAD
def test_no_token_scratch_tracked():
    print("test_no_token_scratch_tracked")
    staged = [p for p in tracked_paths() if TOKEN_SCRATCH.search(p)]
    check(not staged, f"no token-scratch path is tracked (found: {staged})")
    in_head = [p for p in head_paths() if TOKEN_SCRATCH.search(p)]
    check(not in_head, f"HEAD tracks no token-scratch path (found: {in_head})")


# --- (b) item 01: no token-shaped blob anywhere in tracked content
def test_no_token_shaped_blob():
    print("test_no_token_shaped_blob")
    hits, scanned, binary = [], 0, 0
    for path in scan_targets(tracked_paths()):
        text = read_text(path)
        if text is None:
            binary += 1
            continue
        scanned += 1
        for n, line in enumerate(text.splitlines(), 1):
            if token_shaped(line):
                # the hit is NEVER printed — reporting a leaked token leaks it
                hits.append(f"{path}:{n}")
    check(not hits, f"no tracked file carries a token-shaped line (at: {hits})")
    print(f"  note: scanned {scanned} tracked file(s), skipped {binary} binary")


# --- (c) item 02: no credentialed Mongo URI in tracked content
def test_no_mongo_credentials():
    print("test_no_mongo_credentials")
    hits = []
    for path in scan_targets(tracked_paths()):
        text = read_text(path)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            m = MONGO_URI.search(line)
            if m and not is_placeholder(m.group(2)):
                hits.append(f"{path}:{n}")     # the password stays unprinted
    check(not hits, f"no tracked Mongo URI carries a password (at: {hits})")


def main():
    if not have_git():
        print("skip: not a git checkout (no `git rev-parse --git-dir`) — "
              "publish hygiene is asserted against the index, which does not "
              "exist in an archive/tarball checkout")
        return
    for t in (test_detectors, test_root_allowlist, test_no_token_scratch_tracked,
              test_no_token_shaped_blob, test_no_mongo_credentials):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all publish-hygiene tests passed")


if __name__ == "__main__":
    main()
