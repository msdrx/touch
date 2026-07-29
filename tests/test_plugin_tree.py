#!/usr/bin/env python3
"""The shipping subtree `plugin/touch/`: manifests, the monitoring subset, LICENSE.

Item 07 (CM-1, PLUGIN-SPEC-9/12/14/17/18, DISTRIBUTION-1), rewritten by GD-U1.
Run as `python3 test_plugin_tree.py`; exits non-zero on failure. No pytest, no
runner — `run_all.sh` picks it up by its `test_*.py` glob.

WHAT THIS FILE IS FOR
---------------------
GD-T2 ships the plugin as a tracked subtree, `plugin/touch/`, because the
manifest schema has no `files`/`exclude`/`ignore` field: the directory boundary
is the only exclusion primitive there is (PLUGIN-SPEC-9, DISTRIBUTION-1).

GD-U1 finished the job: `plugin/touch/` is now the SINGLE canonical home for
`aggregator/`, `touch-visual/`, `docs/` and the monitoring core. There is no
second copy anywhere, so the arms that used to police one — the sync script,
byte-equality per pinned path, strays-in-a-pinned-tree, canonical-fully-pinned
— have no subject and are gone with `scripts/sync_plugin.sh`. Their properties
did not weaken; they dissolved. A new `aggregator/` module now ships because it
IS the payload, not because a copier was re-run.

Two properties survive the move, and one is new:

  the monitoring subset   the payload's `shared/monitoring/` holds EXACTLY the
                          five shipping files. Its dev-only material (tests,
                          fixtures, `gen_stream.py`) lives at `tests/monitoring/`
                          (GD-U6) — 1.1 MB of PII-slug carriers that must never
                          creep back across the directory boundary.
  the manifests           name, version, description, disclosure clauses,
                          component keys, the single marketplace entry.
  LICENSE (new, GD-U7)    PLUGIN-SPEC-17 wants a LICENSE at the repo root AND
                          at the plugin root, so it is the ONE deliberate
                          duplicate in the tree. The byte-equality check the
                          sync script used to make is re-homed here.

Skips: `claude plugin validate` and `claude plugin details` need the CLI, which
a clean checkout or a CI image may not have; those arms skip with a printed
reason (the suite's mongod convention). Everything else is pure filesystem and
always runs.
"""
import json
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True   # no .pyc droppings in the payload tree
from _roots import PAYLOAD, REPO                        # noqa: E402

PLUGIN = PAYLOAD
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
MARKETPLACE = PLUGIN / ".claude-plugin" / "marketplace.json"

#: The payload's monitoring tree holds exactly these five files and nothing
#: else. Stated positively (an exact set, not a denylist) because that is the
#: form a silent SHRINK also fails: an empty directory passes every "none of
#: the bad names arrived" check ever written.
MONITORING_CORE = {
    "status.sh", "monitor_server.py", "decision_watcher.py",
    "monitor.html", "monitoring.md",
}

#: GD-T2's never-ship list, kept as an explicit arm on top of the exact-set one
#: so the failure message names the thing that came back. These are the paths
#: GD-U6 moved OUT to `tests/monitoring/`; `tests/` alone is ~1.1 MB and its
#: fixtures and dev scripts carry the author's home-directory slug.
MONITORING_NEVER_SHIP = (
    "tests", "fixtures", "gen_stream.py", "test_perf_work.py", "test_ws_e2e.py",
    "test_server.py", "test_shell.py", "test_watcher.py", "test_frontend.py",
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


# --- the payload's monitoring subset: exactly the five shipping files
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
    # EXACT equality, not a subset in either direction. GD-U1 makes this tree
    # canonical, so "extra" now means "someone put a dev-only file in the
    # module" rather than "the copier over-copied" — and the shrink half still
    # matters: an empty directory passes every denylist ever written.
    check(present == MONITORING_CORE,
          f"the payload's shared/monitoring/ holds exactly the five shipping "
          f"files (missing: {sorted(MONITORING_CORE - present)}, "
          f"extra: {sorted(present - MONITORING_CORE)})")


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
    # (PLUGIN-SPEC-17). This is the "and it really is MIT" half.
    check("MIT License" in text, "LICENSE is the MIT licence")
    check("Michael Sadradze" in text, "LICENSE names the copyright holder")
    # GD-U7: after GD-U1 moved every other pinned tree INTO the payload, this
    # pair is the ONE deliberate duplicate left in the repo — PLUGIN-SPEC-17
    # wants a LICENSE at the repo root and at the plugin root, and no layout
    # dissolves that. `scripts/sync_plugin.sh` used to keep the two equal; the
    # script is gone, so the check is re-homed here, two lines, byte for byte.
    # No `if` around it: a deleted plugin LICENSE must fail HERE too, not
    # degrade this arm to silence while only the existence arm above speaks.
    plugin_license = PLUGIN / "LICENSE"
    check(plugin_license.is_file()
          and root_license.read_bytes() == plugin_license.read_bytes(),
          "the plugin's LICENSE is byte-equal to the root's (GD-U7: the one "
          "deliberate duplicate)")


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
    for t in (test_monitoring_subset_ships_nothing_extra,
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
