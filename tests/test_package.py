#!/usr/bin/env python3
"""The payload gate: what `plugin/touch/` actually ships, byte for byte.

Item 10 (DISTRIBUTION-3, DISTRIBUTION-10, PLUGIN-SPEC-2, PRIOR-AUDIT-6,
CM-9/10/13, PRIOR-AUDIT-14). Run as `python3 test_package.py`; exits non-zero
on failure. No pytest, no runner — `run_all.sh` picks it up by its `test_*.py`
glob.

WHY THIS FILE EXISTS
--------------------
GD-T7 names three layers, and this is the middle one — the only one that looks
at the bytes a consumer receives:

  1. `tests/test_publish_hygiene.py`  the dev repo's INDEX (what a clone gets)
  2. `tests/test_package.py`  (here)  the PAYLOAD (what an install gets)
  3. `claude plugin validate --strict` the MANIFESTS (schema, nothing else)

Neither neighbour can stand in for this one. `git archive` guarantees
*tracked*, not *safe*: probe E6 shipped a tracked `.touch/leak.txt` and a
tracked `__pycache__/a.pyc` verbatim (DISTRIBUTION-3). And `validate --strict`
passes a tree full of `sk-ant-`/`ghp_` blobs without a murmur (P12/E2,
PLUGIN-SPEC-2) — it reads two JSON files and never walks the payload.

THE STAGE IS BUILT HERE, BY GIT
-------------------------------
The test does not inspect `plugin/touch/` on disk. It asks git to build the
release stage exactly as `scripts/release.sh` will — `git archive` of a tree
object — so a release script that quietly `cp -r`s the working tree cannot
route around the gate, and an untracked stray in the working tree cannot make
the gate red for a reason that will never ship.

Two stages are checked, and the second is the reason this gate is useful
*before* the subtree's first commit:

  `HEAD`      `HEAD:plugin/touch`, the tree a release built today would carry.
              Absent until item 07's subtree is committed.
  `pending`   the tree `git add -A -- plugin/touch` would produce, written to
              a THROWAWAY index (`GIT_INDEX_FILE` in a temp dir) and turned
              into a tree with `git write-tree`. Nothing is committed, nothing
              is staged, the repository's own index is never touched; the only
              trace is a few loose blobs that `git gc` reaps. It honours
              `.gitignore` and carries the exec bits git will record, which is
              what makes it a faithful preview of the next commit.

Both are the same checks over the same code. When the two trees agree only one
stage runs.

WHAT IT ASSERTS, IN ORDER
-------------------------
  1. the payload's TOP LEVEL is an allowlist — it fails on ADDITION, because a
     new top-level directory is how a whole tree ships by accident;
  2. recursive DENY patterns — GD-T2's never-ship list, matched per path
     component so a nested `__pycache__/` is caught as readily as a top one;
  3. a CONTENT scan of every decodable file: API keys, GitHub tokens, AWS
     keys, private keys, credentialed `mongodb://` URIs, token-shaped blobs,
     and the author's PII slugs. A hit prints `path:line` and a category, and
     NEVER the matched text — printing a leaked secret to a CI log leaks it;
  4. EXEC BITS — `bin/*` and `hooks/*.py` must be executable (a lost bit fails
     only on the consumer's machine), and nothing that is not a `*.sh`/`*.py`
     entry point may be;
  5. POSTURE source-text (GD-T8): no staged Python carries a quoted `0.0.0.0`
     outside its `OPEN_HOST` constant, and every module that can bind a socket
     carries a per-boot token and an Origin/Host check;
  6. a CANARY self-test: the same scanners are run over a poisoned copy of the
     stage and must go red. A gate that cannot fail is not a gate;
  7. the MANIFESTS, `claude plugin validate --strict`, on the STAGED copies,
     by explicit file path (GD-T7 — a directory-level run leaves remote-source
     entries unchecked).

The matchers for the Mongo URI and the token shape are IMPORTED from
`test_publish_hygiene.py` rather than restated: two copies of a detector is
how one of them quietly stops detecting.

Skips: everything here needs `git archive`, so outside a git checkout the file
prints one skip line and exits 0 (the clean-checkout discipline). The manifest
arm skips when the `claude` CLI is absent (the suite's mongod convention).
"""
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import namedtuple
from pathlib import Path

# Importing the sibling detector module must not litter `__pycache__/` into a
# tree this suite asserts about (`run_all.sh` sets this for its own runs; a
# direct `python3 tests/test_package.py` does not).
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_publish_hygiene import MONGO_URI, is_placeholder, token_shaped  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

#: The payload root, repo-relative. Everything below is expressed relative to
#: THIS, because that is what the release stage's root looks like.
PAYLOAD = "plugin/touch"

# --- (1) the top level is an allowlist -------------------------------------
#: GD-T2's shipping set. An allowlist fails on ADDITION: a new entry here is a
#: deliberate act by someone who looked at what they were shipping. Absence is
#: fine — `README.md`/`CHANGELOG.md` arrive with item 11 and this gate must be
#: green both before and after that.
TOP_ALLOWLIST = frozenset({
    ".claude-plugin",
    "aggregator",
    "touch-visual",
    "skills",
    "shared",
    "hooks",
    "bin",
    "docs",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
})

# --- (2) the never-ship list, recursive ------------------------------------
#: Matched against EVERY path component, not just the top one: `.git` and
#: `__pycache__` arrive nested, and `fixtures` is 8.0 MB of verbatim transcript
#: two levels down. Exact names first, then globs.
DENY_EXACT = frozenset({
    ".git",
    "__pycache__",
    "local-orchestrators",
    "events.jsonl",
    ".watcher-state.json",
    "orch-config.json",
    "mongo-data",
    "fixtures",
    "tests",
})
DENY_GLOB = (
    "*.pyc",
    ".touch*",      # `.touch/`, `.touch.json` — the server's per-boot token dir
    "*.pid",
    "*.log",
    "*.bson",
)

# --- (3) the content scan ---------------------------------------------------
#: Category -> pattern. The CATEGORY is what gets printed; the match never is.
#: `gh[pousr]_` is deliberately the bare prefix (the plan's wording): GitHub's
#: own revocation scanner keys on it, and a redacted `ghp_…` in prose is still
#: a thing this repository should not ship. `AKIA` carries its 16-char body so
#: a sentence about AWS is not a leak.
SECRET_PATTERNS = (
    ("anthropic-api-key", re.compile(r"sk-ant-")),
    ("github-token", re.compile(r"gh[pousr]_")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private-key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
)

#: The author's slugs. These may be literal here because `tests/` is on
#: GD-T2's never-ship list and is denied by (2) above — this file can never
#: become part of the payload it is scanning.
PII_SLUGS = (
    ("author-slug", re.compile(r"laniakea", re.IGNORECASE)),
    ("author-handle", re.compile(r"michaelsadradze", re.IGNORECASE)),
)

#: A home directory or a Claude project slug, with the account name captured.
#: `/home/user`, `/Users/you` and `-home-user-Projects-touch` are how the docs
#: and comments MUST spell these paths (item 03 generalized
#: `aggregator/sessions.py:279` for exactly this reason); a real account name
#: in the same shape is PII.
HOME_PATH = re.compile(r"/(?:home|Users)/([A-Za-z0-9_.<${-]+)")
PROJECT_SLUG = re.compile(r"-home-([A-Za-z0-9_<${-]+?)-")

#: Archetypal stand-ins only. A real account name does not belong here — it
#: belongs generalized in the file that carries it. Names are compared after
#: `<`/`$`/`{` decoration is stripped, so `<user>`, `$USER` and `${USER}` all
#: land on `user` and need no entries of their own.
PLACEHOLDER_USERS = frozenset({
    "user", "users", "username", "someone", "somebody", "you", "youruser",
    "your-user", "me", "alice", "bob", "example", "name", "home",
})

# --- (4) exec bits ----------------------------------------------------------
#: Paths (payload-relative, glob) that MUST carry the exec bit. A `bin/`
#: wrapper or a hook that lost it fails on the consumer's machine and nowhere
#: else — the dev loop never notices.
MUST_BE_EXEC = ("bin/*", "hooks/*.py")
#: Extensions that MAY carry it (shipped entry points). Everything else that is
#: executable is a mistake: a `*.md`, `*.json` or `*.html` with `+x` is noise
#: at best and an invitation at worst.
MAY_BE_EXEC = (".sh", ".py")

# --- (5) the posture assertions (GD-T8) ------------------------------------
#: A staged module that can open a listening socket. `OPEN_HOST` is included so
#: a module that only *declares* the open-bind opt-in is still covered.
BINDS_A_SOCKET = re.compile(
    r"asyncio\.start_server|HTTPServer|socketserver|socket\.socket"
    r"|create_server|^OPEN_HOST\s*=", re.MULTILINE)
#: The two that must always be in that set. Derivation alone is not enough: a
#: refactor that renamed the bind would silently empty the set and the arm
#: would pass by finding nothing to check.
KNOWN_SERVERS = frozenset({
    "aggregator/server.py",
    "shared/monitoring/monitor_server.py",
})
OPEN_LITERAL = re.compile(r"""['"]0\.0\.0\.0['"]""")
OPEN_HOST_ASSIGN = re.compile(r"""^\s*OPEN_HOST\s*[:=]""")

# --- the stdlib/interpreter floor for the shipped wrappers ------------------
#: GD-21. `jq` is this repo's status-line-only exception and does not travel;
#: everything else here is simply not a thing a consumer is guaranteed to have.
FOREIGN_INTERPRETERS = re.compile(r"\b(jq|node|npx|deno|bun|ruby|perl|php|Rscript)\b")
ALLOWED_SHEBANGS = ("#!/usr/bin/env bash", "#!/usr/bin/env sh",
                    "#!/usr/bin/env python3", "#!/bin/bash", "#!/bin/sh")

Entry = namedtuple("Entry", "path mode data kind")

failures = []
skips = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def skip(msg):
    print(f"  skip: {msg}")
    skips.append(msg)


def git(*args, env_extra=None):
    """Run git in REPO with a neutral environment; return CompletedProcess."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(env_extra or {})
    return subprocess.run(
        ["git", *args], cwd=str(REPO), env=env, capture_output=True, text=True,
    )


def have_git():
    try:
        return git("rev-parse", "--git-dir").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def have_cli():
    return shutil.which("claude") is not None


# --------------------------------------------------------------------------
# Building the stage
# --------------------------------------------------------------------------
def head_spec():
    """`HEAD:plugin/touch` once the subtree is committed, else None."""
    res = git("rev-parse", "--verify", "--quiet", f"HEAD:{PAYLOAD}")
    return f"HEAD:{PAYLOAD}" if res.returncode == 0 else None


def pending_spec(tmp):
    """`<tree>:plugin/touch` for the tree the next commit would carry, or None.

    Built in a throwaway index so the repository's own index is untouched — no
    `git add`, no commit, no stash reaches it (the sub-plan forbids all three).
    """
    if not (REPO / PAYLOAD).is_dir():
        return None
    index = tmp / "throwaway-index"
    env = {"GIT_INDEX_FILE": str(index)}
    add = git("add", "-A", "--", PAYLOAD, env_extra=env)
    if add.returncode != 0:
        return None
    tree = git("write-tree", env_extra=env)
    if tree.returncode != 0 or not tree.stdout.strip():
        return None
    return f"{tree.stdout.strip()}:{PAYLOAD}"


def read_stage(spec, tmp, label):
    """(entries, extracted_root) for an archive spec, or (None, None).

    Modes come from the tar members, which are git's recorded modes; the
    extraction on disk exists only so `claude plugin validate` has a real file
    path to read.
    """
    tar_path = tmp / f"{label}.tar"
    res = git("archive", "--format=tar", f"--output={tar_path}", spec)
    if res.returncode != 0:
        return None, None
    entries = []
    root = tmp / f"{label}-stage"
    root.mkdir(exist_ok=True)
    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            if m.isdir():
                continue
            if m.isfile():
                fh = tf.extractfile(m)
                entries.append(Entry(m.name, m.mode, fh.read() if fh else b"", "file"))
            else:
                kind = "link" if (m.issym() or m.islnk()) else "other"
                entries.append(Entry(m.name, m.mode, None, kind))
        try:
            tf.extractall(root, filter="data")
        except TypeError:                     # Python < 3.12 has no `filter`
            tf.extractall(root)
        except (OSError, tarfile.TarError):
            root = None
    return entries, root


def build_stages(tmp):
    """[(label, entries, extracted_root)] — HEAD first, then pending if it differs."""
    stages = []
    seen = set()
    for label, spec in (("HEAD", head_spec()), ("pending", pending_spec(tmp))):
        if spec is None:
            continue
        # `<rev>:<path>` resolves straight to the subtree's object id, so two
        # stages that would archive identical bytes are recognized as one.
        tree = git("rev-parse", "--verify", "--quiet", spec)
        ident = tree.stdout.strip() or spec
        if ident in seen:
            print(f"  note: stage '{label}' is byte-identical to an earlier "
                  f"stage — not re-checked")
            continue
        entries, root = read_stage(spec, tmp, label)
        if entries is None:
            continue
        seen.add(ident)
        stages.append((label, entries, root))
    return stages


# --------------------------------------------------------------------------
# The scanners — pure functions, so the canary can run them over a poisoned
# copy of the very same stage.
# --------------------------------------------------------------------------
def top_level_strays(entries):
    tops = sorted({e.path.split("/")[0] for e in entries})
    return [t for t in tops if t not in TOP_ALLOWLIST], tops


def _denied_component(name):
    if name in DENY_EXACT:
        return name
    for pat in DENY_GLOB:
        if fnmatch.fnmatchcase(name, pat):
            return pat
    return None


def deny_hits(entries):
    hits = []
    for e in entries:
        for part in e.path.split("/"):
            denied = _denied_component(part)
            if denied:
                hits.append(f"{e.path} (matches {denied})")
                break
    return hits


def _account_name(raw):
    """A captured path segment reduced to the account name it stands for."""
    return raw.lower().strip("<>${}.-")


def _home_slug_kind(line):
    """A category when the line names a real account, else None."""
    for match in HOME_PATH.finditer(line):
        if _account_name(match.group(1)) not in PLACEHOLDER_USERS:
            return "home-path"
    for match in PROJECT_SLUG.finditer(line):
        if _account_name(match.group(1)) not in PLACEHOLDER_USERS:
            return "project-slug"
    return None


def line_hit(line):
    """The category of the first leak on a line, or None. Never the text."""
    for kind, pattern in SECRET_PATTERNS:
        if pattern.search(line):
            return kind
    match = MONGO_URI.search(line)
    if match and not is_placeholder(match.group(2)):
        return "mongodb-credentials"
    if token_shaped(line):
        return "token-shaped-blob"
    for kind, pattern in PII_SLUGS:
        if pattern.search(line):
            return kind
    return _home_slug_kind(line)


def content_hits(entries):
    """[(location, category)] — location is `path:line`, and that is all."""
    hits, scanned, binary = [], 0, 0
    for e in entries:
        if e.kind != "file" or e.data is None:
            continue
        try:
            text = e.data.decode("utf-8")
        except UnicodeDecodeError:
            binary += 1
            continue
        scanned += 1
        for n, line in enumerate(text.splitlines(), 1):
            kind = line_hit(line)
            if kind:
                hits.append((f"{e.path}:{n}", kind))
    return hits, scanned, binary


def exec_violations(entries):
    """(missing the bit where it is required, carrying it where it is not)."""
    missing, extra = [], []
    for e in entries:
        if e.kind != "file":
            continue
        executable = bool(e.mode & 0o111)
        required = any(fnmatch.fnmatchcase(e.path, pat) for pat in MUST_BE_EXEC)
        if required:
            if not executable:
                missing.append(e.path)
        elif executable and not e.path.endswith(MAY_BE_EXEC):
            extra.append(e.path)
    return missing, extra


def python_entries(entries):
    """[(path, text)] for every decodable staged `*.py`."""
    out = []
    for e in entries:
        if e.kind != "file" or not e.path.endswith(".py") or e.data is None:
            continue
        try:
            out.append((e.path, e.data.decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return out


# --------------------------------------------------------------------------
# The detectors, before they are trusted over the payload. Both synthetic
# samples are ASSEMBLED at runtime: a literal key in this file would be found
# by the scan itself the day `tests/` stops being denied.
# --------------------------------------------------------------------------
def test_detectors():
    print("test_detectors")
    check(line_hit("KEY=" + "sk-" + "ant-" + "api03-xyz") == "anthropic-api-key",
          "an Anthropic key prefix is flagged")
    check(line_hit("token: " + "gh" + "p_" + "0123456789") == "github-token",
          "a GitHub token prefix is flagged")
    check(line_hit("id = " + "AKIA" + "B" * 16) == "aws-access-key",
          "an AWS access key is flagged")
    check(line_hit("-----" + "BEGIN RSA PRIVATE KEY" + "-----") == "private-key",
          "a private key header is flagged")
    check(line_hit("mongodb://touch:" + "hunter2x" + "@h/db") == "mongodb-credentials",
          "a credentialed Mongo URI is flagged")
    check(line_hit("mongodb://touch:<password>@h/db") is None,
          "the documented <password> form is not a leak")
    # The author's own slugs are caught by name, whichever shape they arrive in
    # — that is why both of these report `author-slug` and not the positional
    # categories below them.
    check(line_hit("/home/" + "lani" + "akea/Projects/touch") == "author-slug",
          "the author's home path is flagged")
    check(line_hit("-home-" + "lani" + "akea-Projects-touch") == "author-slug",
          "the author's project slug is flagged")
    # ...and the shapes catch ANY account, which is the half that survives the
    # day this payload is built on a different machine.
    check(line_hit("/home/jdoe/Projects/touch") == "home-path",
          "a stranger's home path is flagged too")
    check(line_hit("-home-jdoe-Projects-touch") == "project-slug",
          "a stranger's project slug is flagged too")
    check(line_hit("/home/user/Projects/touch") is None,
          "a generalized `/home/user` path is not a leak")
    check(line_hit("/home/<user>/Projects/touch") is None,
          "the documented `/home/<user>` form is not a leak")
    check(line_hit("-home-user-Projects-touch") is None,
          "a generalized `-home-user-` slug is not a leak")
    check(line_hit("# " + "-" * 60) is None,
          "a comment rule is not a token (entropy, via test_publish_hygiene)")
    check(_denied_component("__pycache__") == "__pycache__",
          "a `__pycache__` component is denied")
    check(_denied_component("a.pyc") == "*.pyc" and _denied_component(".touch") == ".touch*",
          "the deny globs match a `.pyc` and a `.touch` path")
    check(_denied_component("aggregator") is None,
          "an ordinary directory name is not denied")


# --------------------------------------------------------------------------
# (1)-(5): the gate itself
# --------------------------------------------------------------------------
def test_top_level_allowlist(label, entries):
    print(f"test_top_level_allowlist [{label}]")
    strays, tops = top_level_strays(entries)
    check(not strays,
          f"the payload's top level is allowlisted (unexpected: {strays}) — "
          f"add it to TOP_ALLOWLIST on purpose or keep it out of plugin/touch/")
    check(bool(entries), f"the stage is not empty ({len(entries)} file(s))")
    print(f"  note: top level: {tops}")


def test_deny_patterns(label, entries):
    print(f"test_deny_patterns [{label}]")
    hits = deny_hits(entries)
    check(not hits, f"no never-ship path is staged ({len(hits)}: {hits[:8]})")


def test_content_scan(label, entries):
    print(f"test_content_scan [{label}]")
    hits, scanned, binary = content_hits(entries)
    # `path:line` and a category. The matched text is NEVER printed — a gate
    # that echoes the secret it found has published it to every CI log.
    check(not hits, f"no staged file carries a secret or a PII slug "
                    f"({len(hits)} hit(s): {hits[:8]})")
    print(f"  note: scanned {scanned} decodable file(s), skipped {binary} binary")


def test_exec_bits(label, entries):
    print(f"test_exec_bits [{label}]")
    missing, extra = exec_violations(entries)
    check(not missing,
          f"every bin/ wrapper and hooks/*.py is executable (missing: {missing})")
    check(not extra,
          f"nothing else carries the exec bit (unexpected: {extra})")
    links = [e.path for e in entries if e.kind == "link"]
    # PLUGIN-SPEC-12: a symlink out of the plugin root survives a marketplace
    # install and is SKIPPED under `--plugin-dir`, i.e. it breaks in the loop a
    # developer runs all day. The payload is copies, only ever copies.
    check(not links, f"the payload contains no symlink (found: {links})")


def test_posture_source_text(label, entries):
    print(f"test_posture_source_text [{label}]")
    modules = python_entries(entries)
    check(bool(modules), f"the stage carries Python to check ({len(modules)} module(s))")
    open_binds, servers = [], []
    for path, text in modules:
        for n, line in enumerate(text.splitlines(), 1):
            if OPEN_LITERAL.search(line) and not OPEN_HOST_ASSIGN.match(line):
                open_binds.append(f"{path}:{n}")
        if BINDS_A_SOCKET.search(text):
            servers.append(path)
    check(not open_binds,
          f"no staged module spells a `0.0.0.0` literal outside its OPEN_HOST "
          f"constant (at: {open_binds})")
    # A derived set can silently empty out; the two that must be in it are
    # named, so a rename goes red instead of vacuously green.
    check(KNOWN_SERVERS <= set(servers),
          f"both server modules are still recognized as binding "
          f"(missing: {sorted(KNOWN_SERVERS - set(servers))})")
    by_path = dict(modules)
    for path in sorted(servers):
        text = by_path[path]
        check("secrets.token_urlsafe" in text,
              f"{path} mints a per-boot token (GD-T8)")
        check("Origin" in text and re.search(r"\bHost\b", text) is not None,
              f"{path} enforces an Origin/Host check on the upgrade (GD-T8)")


def test_shipped_wrappers_are_stdlib(label, entries):
    print(f"test_shipped_wrappers_are_stdlib [{label}]")
    wrappers = [e for e in entries
                if e.kind == "file" and (e.path.startswith("bin/")
                                         or e.path.endswith(".sh"))]
    check(bool(wrappers), f"the stage carries shell entry points ({len(wrappers)})")
    for e in sorted(wrappers):
        try:
            text = e.data.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            check(False, f"{e.path} is decodable text")
            continue
        first = text.splitlines()[0] if text.splitlines() else ""
        check(first in ALLOWED_SHEBANGS,
              f"{e.path} starts with an allowed shebang (got {first!r})")
        # Full-line comments are stripped first: a wrapper's header explains
        # WHY it uses no jq, and a naive substring search would flag the
        # rationale (the `sync_plugin.sh` precedent in test_plugin_tree.py).
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        found = sorted({m.group(0) for m in FOREIGN_INTERPRETERS.finditer(code)})
        check(not found,
              f"{e.path} shells out to no non-stdlib interpreter (GD-21; found: {found})")


# --------------------------------------------------------------------------
# (6) the canary — the gate's own self-test
# --------------------------------------------------------------------------
def test_canary_goes_red(label, entries):
    print(f"test_canary_goes_red [{label}]")
    # DISTRIBUTION-3's two real escapees, reproduced: a tracked dotfile that
    # carries a credential and a tracked bytecode dropping. Assembled at
    # runtime for the reason test_detectors gives.
    poison = [
        Entry("secret.env", 0o644,
              ("ANTHROPIC_API_KEY=" + "sk-" + "ant-" + "api03-deadbeef").encode(),
              "file"),
        Entry("__pycache__/x.pyc", 0o644, b"\xed\x0c\r\n", "file"),
    ]
    poisoned = list(entries) + poison

    denied = deny_hits(poisoned)
    check(len(denied) > len(deny_hits(entries)) and
          any("__pycache__" in d for d in denied),
          f"the deny scan flags an injected __pycache__/x.pyc ({denied[:3]})")

    hits, _, _ = content_hits(poisoned)
    baseline, _, _ = content_hits(entries)
    check(len(hits) > len(baseline) and
          any(loc.startswith("secret.env:") for loc, _ in hits),
          "the content scan flags an injected API key")
    check(all("sk-" + "ant-" not in f"{loc}{kind}" for loc, kind in hits),
          "the reported hits carry the location and the category, never the secret")

    strays, _ = top_level_strays(poisoned)
    check(set(strays) >= {"secret.env", "__pycache__"},
          f"the top-level allowlist flags both injected paths ({strays})")

    # And the poison is the only difference: a canary that goes red because the
    # real stage was already red proves nothing.
    check(not deny_hits(entries) and not baseline,
          "the un-poisoned stage is clean, so the canary measured the poison")


# --------------------------------------------------------------------------
# (7) the manifests, on the STAGED bytes, by explicit path
# --------------------------------------------------------------------------
def test_staged_manifests_validate(label, root):
    print(f"test_staged_manifests_validate [{label}]")
    if root is None:
        skip("the stage could not be extracted — manifest validation not run")
        return
    if not have_cli():
        skip("`claude` CLI not on PATH — staged manifest validation not run")
        return
    for name in ("plugin.json", "marketplace.json"):
        path = root / ".claude-plugin" / name
        if not path.is_file():
            check(False, f"the stage carries .claude-plugin/{name}")
            continue
        # GD-T7: ALWAYS by explicit file path. A directory-level run walks
        # locally-resolvable entries but leaves remote-source ones unchecked;
        # the explicit form is immune to that trap. And note what this proves
        # and what it does not: schema only. The leak gate is arms (1)-(3).
        try:
            res = subprocess.run(
                ["claude", "plugin", "validate", "--strict", str(path)],
                cwd=str(REPO), capture_output=True, text=True, timeout=180,
            )
        except (OSError, subprocess.SubprocessError):
            skip(f"`claude plugin validate` did not complete for staged {name}")
            continue
        check(res.returncode == 0,
              f"claude plugin validate --strict (staged {name}) passes "
              f"(rc={res.returncode}, {(res.stdout + res.stderr).strip()[-300:]})")


def main():
    if not have_git():
        print("skip: not a git checkout (no `git rev-parse --git-dir`) — the "
              "payload gate builds its stage with `git archive`, which has no "
              "meaning in an archive/tarball checkout")
        return
    test_detectors()
    with tempfile.TemporaryDirectory(prefix="touch-package-") as td:
        stages = build_stages(Path(td))
        if not stages:
            print(f"skip: git built no stage for {PAYLOAD} — neither HEAD nor "
                  f"the working tree carries the plugin subtree yet (item 07)")
        for label, entries, root in stages:
            print(f"--- stage {label}: {len(entries)} file(s)")
            for t in (test_top_level_allowlist, test_deny_patterns,
                      test_content_scan, test_exec_bits,
                      test_posture_source_text, test_shipped_wrappers_are_stdlib,
                      test_canary_goes_red):
                t(label, entries)
            test_staged_manifests_validate(label, root)
    print()
    if skips:
        print(f"skipped: {len(skips)} check(s)")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all package-gate checks passed")


if __name__ == "__main__":
    main()
