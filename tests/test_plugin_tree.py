#!/usr/bin/env python3
"""The shipping subtree `plugin/touch/`: manifests, pinned copies, LICENSE.

Item 07 (CM-1, PLUGIN-SPEC-9/12/14/17/18, DISTRIBUTION-1). Run as
`python3 test_plugin_tree.py`; exits non-zero on failure. No pytest, no
runner — `run_all.sh` picks it up by its `test_*.py` glob.

WHAT THIS FILE IS FOR
---------------------
GD-T2 ships the plugin as a tracked subtree, `plugin/touch/`, because the
manifest schema has no `files`/`exclude`/`ignore` field: the directory
boundary is the only exclusion primitive there is (PLUGIN-SPEC-9,
DISTRIBUTION-1). Half of that subtree is MOVED and canonical there
(`hooks/`, later `skills/`). The other half — `aggregator/`, `touch-visual/`,
`docs/`, `shared/monitoring/` — is a PINNED COPY of a canonical tree that
stays where this repo already lives with it.

A copy with no gate is a fork with extra steps. `scripts/sync_plugin.sh`
re-makes the copies mechanically; this file is the half that notices when
someone edits `aggregator/store.py` and forgets to re-run it. The mechanism is
deliberately dumb — byte equality, path by path — because the failure mode is
a payload that silently ships last month's code.

Two directions are checked, and the asymmetry is on purpose:

  canonical -> payload   every pinned path exists and is byte-equal, with the
                         same exec bit (`status.sh` is exec'd by the payload's
                         bin/ wrappers; a copy that lost the bit fails only on
                         a consumer's machine).
  payload -> canonical   no file exists in a pinned tree that the sync script
                         does not name (this is where an untracked
                         `__pycache__/` or a hand-edit would show up), AND —
                         for `aggregator/`, `touch-visual/` and `docs/` only —
                         every canonical file is pinned, so a new module
                         cannot quietly stay out of the payload.

`shared/monitoring/` gets no reverse coverage BY DESIGN: the payload carries
five core files out of a directory that also holds `tests/`, module fixtures
and three dev-only scripts, four of which are PII-slug carriers on GD-T2's
never-ship list. So instead of "everything is pinned", that tree is checked
for "none of the never-ship names arrived".

The pinned SET is not restated here. `scripts/sync_plugin.sh --list` prints
`dest<TAB>src` for every pinned path and this file reads that — one list, one
owner. A second copy of the list in Python would be exactly the drift this
file exists to catch.

Skips: `claude plugin validate` and `claude plugin details` need the CLI, which
a clean checkout or a CI image may not have; those arms skip with a printed
reason (the suite's mongod convention). Everything else is pure filesystem and
always runs.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin" / "touch"
SYNC = REPO / "scripts" / "sync_plugin.sh"
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
MARKETPLACE = PLUGIN / ".claude-plugin" / "marketplace.json"

#: The pinned trees, payload-relative. Every file under one of these must be
#: named by the sync script; nothing else in `plugin/touch/` is this file's
#: business (`hooks/`, `bin/`, `skills/`, `README.md`, `CHANGELOG.md` have
#: their own owners, and the whole-payload allowlist is item 10's gate).
PINNED_TREES = ("aggregator", "touch-visual", "docs", "shared")

#: Canonical trees that must be pinned in FULL, and the filename filter that
#: says which of their files count. `shared/monitoring` is absent on purpose —
#: see the module docstring.
FULL_COVERAGE = {
    "aggregator": lambda n: n.endswith(".py"),
    "touch-visual": lambda n: True,
    "docs": lambda n: n.endswith(".md"),
}

#: GD-T2's never-ship list, restricted to the one tree this file owns a subset
#: of. `tests/` alone is 700 KB of the monitoring module and its fixtures and
#: dev scripts carry the author's home-directory slug.
MONITORING_NEVER_SHIP = (
    "tests", "fixtures", "gen_stream.py", "test_perf_work.py", "test_ws_e2e.py",
)

#: GD-T6 keeps the component table closed: these are wired up by nobody, in
#: either manifest, until a plan says otherwise.
COMPONENT_KEYS = (
    "commands", "agents", "skills", "hooks", "mcpServers", "lspServers",
    "workflows", "monitors", "outputStyles", "themes", "channels",
)

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

#: Verbs the shipped description may not use while Touch is read-only. The
#: `/plugin` UI shows the description and never the README (DISTRIBUTION-7), so
#: this string is the entire pre-install surface — the one place a claimed
#: capability cannot be contradicted by the UI before the user has believed it.
#: `README.md:23` and `docs/control-semantics.md:7` both say the control plane
#: is unshipped; D13 ("a control is rendered only where it can be honest") and
#: the R-58 precedent (a fabricated FAILED badge) are the law being kept here.
#: When a control verb really ships, DELETE the matching alternative from this
#: pattern deliberately — that edit is the point of the assertion.
UNSHIPPED_VERBS = re.compile(r"\b(steer|control|stop|restart|terminate|kill)\b",
                             re.IGNORECASE)

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


def load_json(path):
    """Parsed JSON, or None (with a recorded failure) if it will not parse."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        check(False, f"{path.relative_to(REPO)} parses as JSON ({exc})")
        return None


def pinned_pairs():
    """[(dest, src)] repo-relative, straight from the sync script's own list.

    Read through `--list` rather than by parsing the script's bash: the script
    prints what it would copy, so the two can never describe different sets.
    """
    try:
        out = subprocess.run(
            ["bash", str(SYNC), "--list"], cwd=str(REPO),
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        check(False, f"scripts/sync_plugin.sh --list runs ({exc})")
        return []
    if out.returncode != 0:
        check(False, f"scripts/sync_plugin.sh --list exits 0 (rc={out.returncode}, "
                     f"stderr={out.stderr.strip()[:200]})")
        return []
    pairs = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            check(False, f"--list line is dest<TAB>src (got: {line[:80]!r})")
            continue
        pairs.append((parts[0], parts[1]))
    return pairs


def have_cli():
    return shutil.which("claude") is not None


def run_cli(args, timeout=180):
    """CompletedProcess, or None when the CLI is missing/hangs/errors out."""
    try:
        return subprocess.run(
            ["claude", *args], cwd=str(REPO),
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


# --- the sync script itself is the manifest of the pinned set
def test_sync_script():
    print("test_sync_script")
    check(SYNC.is_file(), "scripts/sync_plugin.sh exists")
    if not SYNC.is_file():
        return
    check(os.access(SYNC, os.X_OK), "scripts/sync_plugin.sh is executable")
    # Comment lines are stripped before the source-text checks below: this
    # script's header explains WHY it uses no jq and no symlinks, and a naive
    # substring search would flag its own rationale.
    code = "\n".join(
        line for line in SYNC.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    # GD-21: the sync path is bash + coreutils. `jq` is a status-line-only
    # exception in this repo and is not licensed here.
    check(not re.search(r"\bjq\b", code), "sync_plugin.sh uses no jq (GD-21)")
    # PLUGIN-SPEC-12: a symlink out of the plugin root survives a marketplace
    # install and is SKIPPED under `--plugin-dir`, i.e. it breaks in the loop a
    # developer runs all day. The payload is copies, only ever copies.
    check("ln -s" not in code, "sync_plugin.sh symlinks nothing (PLUGIN-SPEC-12)")

    pairs = pinned_pairs()
    check(len(pairs) > 0, f"--list names at least one pinned path ({len(pairs)})")
    dests = [d for d, _ in pairs]
    dupes = sorted({d for d in dests if dests.count(d) > 1})
    check(not dupes, f"no destination is pinned twice (dupes: {dupes})")
    outside = [d for d in dests if not d.startswith("plugin/touch/")]
    check(not outside,
          f"every pinned destination is inside plugin/touch/ (stray: {outside})")

    # `--check` is read-only by contract and reports BOTH directions (drift and
    # strays), i.e. it is the standalone pre-commit form of the two arms below.
    # Running it here keeps that contract honest: if the script ever stops
    # agreeing with this file, one of the two goes red instead of both quietly
    # checking different things.
    try:
        res = subprocess.run(
            ["bash", str(SYNC), "--check"], cwd=str(REPO),
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        check(False, f"scripts/sync_plugin.sh --check runs ({exc})")
        return
    check(res.returncode == 0,
          f"sync_plugin.sh --check reports a clean payload (rc={res.returncode}, "
          f"{(res.stdout + res.stderr).strip()[-300:]})")


# --- canonical -> payload: byte equality, path by path
def test_pinned_copies_are_byte_equal():
    print("test_pinned_copies_are_byte_equal")
    pairs = pinned_pairs()
    if not pairs:
        return
    missing_src, missing_dest, drifted, mode_drift = [], [], [], []
    for dest, src in pairs:
        s, d = REPO / src, REPO / dest
        if not s.is_file():
            missing_src.append(src)
            continue
        if not d.is_file():
            missing_dest.append(dest)
            continue
        if s.read_bytes() != d.read_bytes():
            drifted.append(dest)
        # Only the exec bit is compared: git tracks that and nothing else of
        # the mode, and it is the bit whose loss breaks `status.sh` at run
        # time on someone else's machine.
        if bool(s.stat().st_mode & 0o111) != bool(d.stat().st_mode & 0o111):
            mode_drift.append(dest)
    check(not missing_src, f"every canonical source exists (missing: {missing_src})")
    check(not missing_dest,
          f"every pinned path exists in the payload (missing: {missing_dest}) — "
          "run scripts/sync_plugin.sh")
    check(not drifted,
          f"every pinned path is byte-equal to its canonical source "
          f"(drifted: {drifted}) — run scripts/sync_plugin.sh")
    check(not mode_drift, f"exec bits match canonical (differs: {mode_drift})")
    print(f"  note: {len(pairs)} pinned path(s) compared")


# --- payload -> canonical: nothing unlisted rode along
def test_no_strays_in_pinned_trees():
    print("test_no_strays_in_pinned_trees")
    pairs = pinned_pairs()
    if not pairs:
        return
    listed = {d for d, _ in pairs}
    strays = []
    for tree in PINNED_TREES:
        root = PLUGIN / tree
        if not root.is_dir():
            check(False, f"pinned tree plugin/touch/{tree}/ exists")
            continue
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(REPO).as_posix()
            if rel not in listed:
                strays.append(rel)
    check(not strays,
          f"no unlisted file lives in a pinned tree (strays: {strays}) — "
          "either add it to scripts/sync_plugin.sh or delete it")


def test_canonical_trees_are_fully_pinned():
    print("test_canonical_trees_are_fully_pinned")
    pairs = pinned_pairs()
    if not pairs:
        return
    pinned_srcs = {s for _, s in pairs}
    for tree, keep in FULL_COVERAGE.items():
        root = REPO / tree
        if not root.is_dir():
            check(False, f"canonical tree {tree}/ exists")
            continue
        # Top level only: none of these three has a shipping subdirectory, and
        # `__pycache__/` is the only subdirectory that ever appears.
        want = sorted(
            p.relative_to(REPO).as_posix()
            for p in root.iterdir()
            if p.is_file() and keep(p.name)
        )
        unpinned = [p for p in want if p not in pinned_srcs]
        check(not unpinned,
              f"every canonical {tree}/ file is pinned into the payload "
              f"(unpinned: {unpinned})")


def test_monitoring_subset_ships_nothing_extra():
    print("test_monitoring_subset_ships_nothing_extra")
    root = PLUGIN / "shared" / "monitoring"
    check(root.is_dir(), "plugin/touch/shared/monitoring/ exists")
    if not root.is_dir():
        return
    present = {p.name for p in root.iterdir()}
    leaked = sorted(present & set(MONITORING_NEVER_SHIP))
    check(not leaked,
          f"none of GD-T2's never-ship monitoring paths is in the payload "
          f"(found: {leaked})")
    # And the five that must be there, named so a silent shrink is a failure
    # too — an empty directory would otherwise pass every check above.
    core = {"status.sh", "monitor_server.py", "decision_watcher.py",
            "monitor.html", "monitoring.md"}
    check(core <= present,
          f"all five monitoring core files ship (missing: {sorted(core - present)})")


# --- the manifests
def test_plugin_manifest():
    print("test_plugin_manifest")
    check(MANIFEST.is_file(), "plugin/touch/.claude-plugin/plugin.json exists")
    if not MANIFEST.is_file():
        return
    m = load_json(MANIFEST)
    if m is None:
        return
    check(m.get("$schema") ==
          "https://json.schemastore.org/claude-code-plugin-manifest.json",
          "$schema points at the plugin-manifest schema (editor validation, P2)")
    # GD-T1: no rename. `name` is the namespace (`/touch:orchestrate`);
    # `displayName` is the sanctioned escape hatch for any later rebrand.
    check(m.get("name") == "touch", "name is 'touch' (GD-T1)")
    check(m.get("displayName") == "Touch", "displayName is 'Touch' (GD-T1)")
    check(SEMVER.match(str(m.get("version", ""))),
          f"version is semver (got {m.get('version')!r})")
    desc = m.get("description", "")
    # DISTRIBUTION-7: the /plugin UI shows the description and never the
    # README, so this string carries the whole pitch AND the disclosure.
    check(isinstance(desc, str) and len(desc) >= 80,
          f"description is a real sentence, not a label ({len(desc)} chars)")
    check("hook" in desc.lower(),
          "description discloses the hook (DISTRIBUTION-7/GD-T8)")
    claimed = sorted({m_.group(0).lower() for m_ in UNSHIPPED_VERBS.finditer(desc)})
    check(not claimed,
          f"description claims no control verb — v0 is read-only "
          f"(README.md:23, D13; found: {claimed})")
    # GD-T8's other two disclosure clauses, asserted rather than assumed: the
    # transcript read and the project-local write. Both were written into the
    # string once and could be edited back out by anyone trimming for length.
    check("~/.claude/projects" in desc,
          "description names the transcript directory it reads (GD-T8)")
    check(m.get("license") == "MIT", "license is MIT")
    check(m.get("homepage", "").endswith("msdrx/touch-plugin"),
          "homepage points at the release repo (GD-T3)")
    check(str(m.get("repository", "")).endswith("msdrx/touch-plugin"),
          "repository points at the release repo, never the dev repo (GD-T3)")
    check(isinstance(m.get("keywords"), list) and m["keywords"],
          "keywords is a non-empty list")
    # PLUGIN-SPEC-14: a plugin whose hook fires on every tool call installs
    # DISABLED. The user opts in after reading the description.
    check(m.get("defaultEnabled") is False, "defaultEnabled is false (GD-T6)")

    author = m.get("author") or {}
    check(author.get("name") and author.get("url"),
          "author carries a name and a url")
    check("email" not in author,
          "author carries no email (it would ship in every install)")

    uc = (m.get("userConfig") or {}).get("run_scope_guard")
    check(isinstance(uc, dict), "userConfig declares run_scope_guard (GD-T6)")
    if isinstance(uc, dict):
        check(uc.get("type") == "boolean", "run_scope_guard is a boolean")
        check(uc.get("default") is True,
              "run_scope_guard defaults to true (the guard is on by default)")
    declared = [k for k in COMPONENT_KEYS if k in m]
    check(not declared,
          f"plugin.json declares no component paths — the conventional "
          f"directories are discovered (GD-T6; declared: {declared})")


def test_marketplace_manifest():
    print("test_marketplace_manifest")
    check(MARKETPLACE.is_file(),
          "plugin/touch/.claude-plugin/marketplace.json exists")
    if not MARKETPLACE.is_file():
        return
    m = load_json(MARKETPLACE)
    if m is None:
        return
    check(m.get("name") == "msdrx-tools", "marketplace name is 'msdrx-tools' (GD-T3)")
    # --strict turns a missing marketplace description into an error (E2).
    check(bool(m.get("description")), "marketplace carries a description")
    owner = m.get("owner") or {}
    check(bool(owner.get("name")), "owner carries a name")
    check("email" not in owner, "owner carries no email")

    plugins = m.get("plugins") or []
    check(len(plugins) == 1, f"exactly one plugin entry ({len(plugins)})")
    if len(plugins) != 1:
        return
    e = plugins[0]
    # PLUGIN-SPEC-14: keep the entry name and plugin.json's name identical, so
    # the "which name namespaces components" ambiguity cannot bite.
    check(e.get("name") == "touch", "entry name matches plugin.json's name")
    # DISTRIBUTION-1/GD-T3: the release repo is FLAT — repo root == plugin root
    # == marketplace root.
    check(e.get("source") == "./", "entry source is './' (flat release repo)")
    # DISTRIBUTION-5: version resolution is plugin.json > entry > commit SHA,
    # first set wins. Two versions is a way to ship a stale one forever.
    check("version" not in e,
          "entry declares NO version — semver lives in plugin.json only (GD-T9)")
    # Same reasoning one field over. `plugin.json` owns the description, and it
    # is the copy carrying GD-T8's disclosure clauses; an entry description is
    # a second copy of a trust-bearing string with nothing keeping the two
    # equal, and the precedence between them is not established by any finding
    # in this run's research. One owner, as with `version`.
    check("description" not in e,
          "entry carries no description — plugin.json owns it (GD-T9 pattern)")
    declared = [k for k in COMPONENT_KEYS if k in e]
    # PLUGIN-SPEC-18: with strict: true (the default) the entry SUPPLEMENTS
    # plugin.json; declaring components in both is a hard conflict error.
    check(not declared,
          f"entry declares no components (strict-mode conflict; found: {declared})")


def test_license():
    print("test_license")
    root_license = REPO / "LICENSE"
    check(root_license.is_file(), "LICENSE exists at the repo root")
    check((PLUGIN / "LICENSE").is_file(), "LICENSE exists at the plugin root")
    if not root_license.is_file():
        return
    text = root_license.read_text(encoding="utf-8")
    # A `"license": "MIT"` manifest field with no LICENSE file is a false claim
    # (PLUGIN-SPEC-17). Byte equality between the two copies is covered by the
    # pinned-copy arm; this is the "and it really is MIT" half.
    check("MIT License" in text, "LICENSE is the MIT licence")
    check("Michael Sadradze" in text, "LICENSE names the copyright holder")


# --- the CLI arms (skip when `claude` is absent)
def test_manifests_validate():
    print("test_manifests_validate")
    if not have_cli():
        skip("`claude` CLI not on PATH — manifest validation not run")
        return
    # GD-T7: ALWAYS by explicit file path. A directory-level run does walk into
    # locally-resolvable entries, but remote-source entries go unchecked, and
    # the explicit form is immune to that trap.
    for path in (MANIFEST, MARKETPLACE):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO)
        res = run_cli(["plugin", "validate", "--strict", str(path)])
        if res is None:
            skip(f"`claude plugin validate` did not complete for {rel}")
            continue
        check(res.returncode == 0,
              f"claude plugin validate --strict {rel} passes "
              f"(rc={res.returncode}, {(res.stdout + res.stderr).strip()[-300:]})")


def test_plugin_details():
    print("test_plugin_details")
    if not have_cli():
        skip("`claude` CLI not on PATH — plugin details not run")
        return
    res = run_cli(["--plugin-dir", str(PLUGIN), "plugin", "details", "touch"])
    # `have_cli()` above already covers the one legitimate skip (prerequisite
    # absent) and `run_cli` returns None only for a spawn failure or a timeout.
    # A CLI that RAN and refused the payload is a failure, never a skip — this
    # is the only arm covering DISTRIBUTION-14's hook inventory, so a green
    # skip here would hide exactly the thing it exists to catch.
    if res is None:
        skip("`claude --plugin-dir ...` did not spawn/finish — details not run")
        return
    check(res.returncode == 0,
          f"claude --plugin-dir ... plugin details touch loads the payload "
          f"(rc={res.returncode}, {(res.stdout + res.stderr).strip()[-300:]})")
    out = res.stdout
    check("Hooks (1)" in out,
          f"the payload registers exactly one hook (inventory: "
          f"{[l.strip() for l in out.splitlines() if 'Hooks (' in l]})")
    # Skills are NOT asserted here: they arrive with item 09. When they do,
    # this file keeps working and the skills test owns that count.
    check("(touch)" in out or "Touch" in out,
          "details resolves the plugin by its manifest name")


def main():
    for t in (test_sync_script,
              test_pinned_copies_are_byte_equal,
              test_no_strays_in_pinned_trees,
              test_canonical_trees_are_fully_pinned,
              test_monitoring_subset_ships_nothing_extra,
              test_plugin_manifest,
              test_marketplace_manifest,
              test_license,
              test_manifests_validate,
              test_plugin_details):
        t()
    print()
    if skips:
        print(f"skipped: {len(skips)} check(s)")
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all plugin-tree tests passed")


if __name__ == "__main__":
    main()
